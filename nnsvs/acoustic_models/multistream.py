import torch
from nnsvs.acoustic_models.util import pad_inference
from nnsvs.base import BaseModel, PredictionType
from nnsvs.multistream import split_streams
from torch import nn

__all__ = [
    "MultistreamSeparateF0ParametricModel",
    "NPSSMultistreamParametricModel",
    "NPSSMDNMultistreamParametricModel",
    "MultistreamSeparateF0MelModel",
    "HybridMultistreamSeparateF0MelModel",
]


class MultistreamSeparateF0ParametricModel(BaseModel):
    def __init__(
        self,
        in_dim: int,
        out_dim: int,
        stream_sizes: list,
        reduction_factor: int,
        encoder: nn.Module,
        mgc_model: nn.Module,
        lf0_model: nn.Module,
        vuv_model: nn.Module,
        bap_model: nn.Module,
        vib_model: nn.Module,
        vib_flags_model: nn.Module,
        # NOTE: you must carefully set the following parameters
        in_rest_idx=1,
        in_lf0_idx=300,
        in_lf0_min=5.3936276,
        in_lf0_max=6.491111,
        out_lf0_idx=180,
        out_lf0_mean=5.953093881972361,
        out_lf0_scale=0.23435173188961034,
        lf0_teacher_forcing=True,
    ):
        super().__init__()
        self.in_dim = in_dim
        self.out_dim = out_dim
        self.stream_sizes = stream_sizes
        self.reduction_factor = reduction_factor
        self.lf0_teacher_forcing = lf0_teacher_forcing

        assert len(stream_sizes) in [4, 5, 6]

        self.encoder = encoder
        if self.encoder is not None:
            assert not encoder.is_autoregressive()
        self.mgc_model = mgc_model
        self.lf0_model = lf0_model
        self.vuv_model = vuv_model
        self.bap_model = bap_model
        self.vib_model = vib_model
        self.vib_flags_model = vib_flags_model
        self.in_rest_idx = in_rest_idx
        self.in_lf0_idx = in_lf0_idx
        self.in_lf0_min = in_lf0_min
        self.in_lf0_max = in_lf0_max
        self.out_lf0_idx = out_lf0_idx
        self.out_lf0_mean = out_lf0_mean
        self.out_lf0_scale = out_lf0_scale

    def _set_lf0_params(self):
        # Special care for residual F0 prediction models
        # NOTE: don't overwrite out_lf0_idx and in_lf0_idx
        if hasattr(self.lf0_model, "out_lf0_mean"):
            self.lf0_model.in_lf0_min = self.in_lf0_min
            self.lf0_model.in_lf0_max = self.in_lf0_max
            self.lf0_model.out_lf0_mean = self.out_lf0_mean
            self.lf0_model.out_lf0_scale = self.out_lf0_scale

    def is_autoregressive(self):
        return (
            self.mgc_model.is_autoregressive()
            or self.lf0_model.is_autoregressive()
            or self.vuv_model.is_autoregressive()
            or self.bap_model.is_autoregressive()
            or (
                self.vib_model.is_autoregressive()
                if self.vib_model is not None
                else False
            )
            or (
                self.vib_flags_model.is_autoregressive()
                if self.vib_flags_model is not None
                else False
            )
        )

    def forward(self, x, lengths=None, y=None):
        self._set_lf0_params()
        assert x.shape[-1] == self.in_dim

        if y is not None:
            # Teacher-forcing
            outs = split_streams(y, self.stream_sizes)
            if self.vib_model is None and self.vib_flags_model is None:
                y_mgc, y_lf0, y_vuv, y_bap = outs
            elif self.vib_flags_model is None:
                y_mgc, y_lf0, y_vuv, y_bap, y_vib = outs
            else:
                y_mgc, y_lf0, y_vuv, y_bap, y_vib, y_vib_flags = outs
        else:
            # Inference
            y_mgc, y_lf0, y_vuv, y_bap, y_vib, y_vib_flags = (
                None,
                None,
                None,
                None,
                None,
                None,
            )

        # Predict continuous log-F0 first
        lf0, lf0_residual = self.lf0_model(x, lengths, y_lf0)

        if self.encoder is not None:
            encoder_outs = self.encoder(x, lengths)
            # Concat log-F0, rest flags and the outputs of the encoder
            # This may make the decoder to be aware of the input F0
            rest_flags = x[:, :, self.in_rest_idx].unsqueeze(-1)
            if self.lf0_teacher_forcing and y is not None:
                encoder_outs = torch.cat([encoder_outs, rest_flags, y_lf0], dim=-1)
            else:
                encoder_outs = torch.cat([encoder_outs, rest_flags, lf0], dim=-1)
        else:
            encoder_outs = x

        # Decoders for each stream
        mgc = self.mgc_model(encoder_outs, lengths, y_mgc)
        vuv = self.vuv_model(encoder_outs, lengths, y_vuv)
        bap = self.bap_model(encoder_outs, lengths, y_bap)

        if self.vib_model is not None:
            vib = self.vib_model(encoder_outs, lengths, y_vib)
        if self.vib_flags_model is not None:
            vib_flags = self.vib_flags_model(encoder_outs, lengths, y_vib_flags)

        # make a concatenated stream
        has_postnet_output = (
            isinstance(mgc, list)
            or isinstance(lf0, list)
            or isinstance(vuv, list)
            or isinstance(bap, list)
        )
        if has_postnet_output:
            outs = []
            for idx in range(len(mgc)):
                mgc_ = mgc[idx] if isinstance(mgc, list) else mgc
                lf0_ = lf0[idx] if isinstance(lf0, list) else lf0
                vuv_ = vuv[idx] if isinstance(vuv, list) else vuv
                bap_ = bap[idx] if isinstance(bap, list) else bap
                if self.vib_model is None and self.vib_flags_model is None:
                    out = torch.cat([mgc_, lf0_, vuv_, bap_], dim=-1)
                elif self.vib_flags_model is None:
                    out = torch.cat([mgc_, lf0_, vuv_, bap_, vib], dim=-1)
                else:
                    out = torch.cat([mgc_, lf0_, vuv_, bap_, vib, vib_flags], dim=-1)
                assert out.shape[-1] == self.out_dim
                outs.append(out)
            return outs, lf0_residual
        else:
            if self.vib_model is None and self.vib_flags_model is None:
                out = torch.cat([mgc, lf0, vuv, bap], dim=-1)
            elif self.vib_flags_model is None:
                out = torch.cat([mgc, lf0, vuv, bap, vib], dim=-1)
            else:
                out = torch.cat([mgc, lf0, vuv, bap, vib, vib_flags], dim=-1)
            assert out.shape[-1] == self.out_dim

        return out, lf0_residual

    def inference(self, x, lengths=None):
        return pad_inference(
            model=self, x=x, lengths=lengths, reduction_factor=self.reduction_factor
        )


class NPSSMDNMultistreamParametricModel(BaseModel):
    """NPSS-like cascaded multi-stream parametric model with mixture density networks.

    Different from the original NPSS, we don't use spectral parameters
    for the inputs of aperiodicity and V/UV prediction models.
    This is because
    (1) D4C does not use spectral parameters as input for aperiodicity estimation.
    (2) V/UV detection is done from aperiodicity at 0-3 kHz in WORLD.
    In addition, f0 and VUV models dont use MDNs.

    Empirically, we found the above configuration works better than the original one.

    Inputs:
        lf0_model: musical context
        mgc_model: musical context + lf0
        bap_model: musical context + lf0
        vuv_model: musical context + lf0 + bap
    """

    def __init__(
        self,
        in_dim: int,
        out_dim: int,
        stream_sizes: list,
        reduction_factor: int,
        lf0_model: nn.Module,
        mgc_model: nn.Module,
        bap_model: nn.Module,
        vuv_model: nn.Module,
        # NOTE: you must carefully set the following parameters
        in_rest_idx=0,
        in_lf0_idx=51,
        in_lf0_min=5.3936276,
        in_lf0_max=6.491111,
        out_lf0_idx=60,
        out_lf0_mean=5.953093881972361,
        out_lf0_scale=0.23435173188961034,
    ):
        super().__init__()
        self.in_dim = in_dim
        self.out_dim = out_dim
        self.stream_sizes = stream_sizes
        self.reduction_factor = reduction_factor

        assert len(stream_sizes) in [4]

        self.lf0_model = lf0_model
        self.mgc_model = mgc_model
        self.bap_model = bap_model
        self.vuv_model = vuv_model
        self.in_rest_idx = in_rest_idx
        self.in_lf0_idx = in_lf0_idx
        self.in_lf0_min = in_lf0_min
        self.in_lf0_max = in_lf0_max
        self.out_lf0_idx = out_lf0_idx
        self.out_lf0_mean = out_lf0_mean
        self.out_lf0_scale = out_lf0_scale

    def _set_lf0_params(self):
        # Special care for residual F0 prediction models
        # NOTE: don't overwrite out_lf0_idx and in_lf0_idx
        if hasattr(self.lf0_model, "out_lf0_mean"):
            self.lf0_model.in_lf0_min = self.in_lf0_min
            self.lf0_model.in_lf0_max = self.in_lf0_max
            self.lf0_model.out_lf0_mean = self.out_lf0_mean
            self.lf0_model.out_lf0_scale = self.out_lf0_scale

    def prediction_type(self):
        return PredictionType.MULTISTREAM_HYBRID

    def is_autoregressive(self):
        return (
            self.mgc_model.is_autoregressive()
            or self.lf0_model.is_autoregressive()
            or self.vuv_model.is_autoregressive()
            or self.bap_model.is_autoregressive()
        )

    def forward(self, x, lengths=None, y=None):
        self._set_lf0_params()
        assert x.shape[-1] == self.in_dim
        is_inference = y is None

        if is_inference:
            y_mgc, y_lf0, y_vuv, y_bap = (
                None,
                None,
                None,
                None,
            )
        else:
            # Teacher-forcing
            outs = split_streams(y, self.stream_sizes)
            y_mgc, y_lf0, y_vuv, y_bap = outs

        # Predict continuous log-F0 first
        if is_inference:
            lf0, lf0_residual = self.lf0_model.inference(x, lengths), None
        else:
            lf0, lf0_residual = self.lf0_model(x, lengths, y_lf0)

        # Predict spectral parameters
        if is_inference:
            mgc_inp = torch.cat([x, lf0], dim=-1)
            mgc = self.mgc_model.inference(mgc_inp, lengths)
        else:
            mgc_inp = torch.cat([x, y_lf0], dim=-1)
            mgc = self.mgc_model(mgc_inp, lengths, y_mgc)

        # Predict aperiodic parameters
        if is_inference:
            bap_inp = torch.cat([x, lf0], dim=-1)
            bap = self.bap_model.inference(bap_inp, lengths)
        else:
            bap_inp = torch.cat([x, y_lf0], dim=-1)
            bap = self.bap_model(bap_inp, lengths, y_bap)

        # Predict V/UV
        if is_inference:
            vuv_inp = torch.cat([x, lf0, bap[1]], dim=-1)
            vuv = self.vuv_model.inference(vuv_inp, lengths)
        else:
            vuv_inp = torch.cat([x, lf0, y_bap], dim=-1)
            vuv = self.vuv_model(vuv_inp, lengths, y_vuv)

        if is_inference:
            out = torch.cat([mgc[0], lf0, vuv, bap[0]], dim=-1)
            assert out.shape[-1] == self.out_dim
            # TODO: better design
            return out, out
        else:
            return (mgc, lf0, vuv, bap), lf0_residual

    def inference(self, x, lengths=None):
        return pad_inference(
            model=self,
            x=x,
            lengths=lengths,
            reduction_factor=self.reduction_factor,
            mdn=True,
        )


class NPSSMultistreamParametricModel(BaseModel):
    """NPSS-like cascaded multi-stream parametric model with no mixture density networks.

    Non-MDN version
    """

    def __init__(
        self,
        in_dim: int,
        out_dim: int,
        stream_sizes: list,
        reduction_factor: int,
        lf0_model: nn.Module,
        mgc_model: nn.Module,
        bap_model: nn.Module,
        vuv_model: nn.Module,
        # NOTE: you must carefully set the following parameters
        in_rest_idx=0,
        in_lf0_idx=51,
        in_lf0_min=5.3936276,
        in_lf0_max=6.491111,
        out_lf0_idx=60,
        out_lf0_mean=5.953093881972361,
        out_lf0_scale=0.23435173188961034,
        npss_style_conditioning=False,
    ):
        super().__init__()
        self.in_dim = in_dim
        self.out_dim = out_dim
        self.stream_sizes = stream_sizes
        self.reduction_factor = reduction_factor
        self.npss_style_conditioning = npss_style_conditioning

        assert len(stream_sizes) in [4]

        self.lf0_model = lf0_model
        self.mgc_model = mgc_model
        self.bap_model = bap_model
        self.vuv_model = vuv_model
        self.in_rest_idx = in_rest_idx
        self.in_lf0_idx = in_lf0_idx
        self.in_lf0_min = in_lf0_min
        self.in_lf0_max = in_lf0_max
        self.out_lf0_idx = out_lf0_idx
        self.out_lf0_mean = out_lf0_mean
        self.out_lf0_scale = out_lf0_scale

    def _set_lf0_params(self):
        # Special care for residual F0 prediction models
        # NOTE: don't overwrite out_lf0_idx and in_lf0_idx
        if hasattr(self.lf0_model, "out_lf0_mean"):
            self.lf0_model.in_lf0_min = self.in_lf0_min
            self.lf0_model.in_lf0_max = self.in_lf0_max
            self.lf0_model.out_lf0_mean = self.out_lf0_mean
            self.lf0_model.out_lf0_scale = self.out_lf0_scale

    def prediction_type(self):
        return PredictionType.DETERMINISTIC

    def is_autoregressive(self):
        return (
            self.mgc_model.is_autoregressive()
            or self.lf0_model.is_autoregressive()
            or self.vuv_model.is_autoregressive()
            or self.bap_model.is_autoregressive()
        )

    def forward(self, x, lengths=None, y=None):
        self._set_lf0_params()
        assert x.shape[-1] == self.in_dim
        is_inference = y is None

        if is_inference:
            y_mgc, y_lf0, y_vuv, y_bap = (
                None,
                None,
                None,
                None,
            )
        else:
            # Teacher-forcing
            outs = split_streams(y, self.stream_sizes)
            y_mgc, y_lf0, y_vuv, y_bap = outs

        # Predict continuous log-F0 first
        if is_inference:
            lf0, lf0_residual = self.lf0_model.inference(x, lengths), None
        else:
            lf0, lf0_residual = self.lf0_model(x, lengths, y_lf0)

        # Predict spectral parameters
        if is_inference:
            mgc_inp = torch.cat([x, lf0], dim=-1)
            mgc = self.mgc_model.inference(mgc_inp, lengths)
        else:
            mgc_inp = torch.cat([x, y_lf0], dim=-1)
            mgc = self.mgc_model(mgc_inp, lengths, y_mgc)

        # Predict aperiodic parameters
        if is_inference:
            if self.npss_style_conditioning:
                bap_inp = torch.cat([x, mgc, lf0], dim=-1)
            else:
                bap_inp = torch.cat([x, lf0], dim=-1)
            bap = self.bap_model.inference(bap_inp, lengths)
        else:
            if self.npss_style_conditioning:
                bap_inp = torch.cat([x, y_mgc, y_lf0], dim=-1)
            else:
                bap_inp = torch.cat([x, y_lf0], dim=-1)
            bap = self.bap_model(bap_inp, lengths, y_bap)

        # Predict V/UV
        if is_inference:
            if self.npss_style_conditioning:
                vuv_inp = torch.cat([x, mgc, bap, lf0], dim=-1)
            else:
                vuv_inp = torch.cat([x, bap, lf0], dim=-1)
            vuv = self.vuv_model.inference(vuv_inp, lengths)
        else:
            if self.npss_style_conditioning:
                vuv_inp = torch.cat([x, y_mgc, y_bap, y_lf0], dim=-1)
            else:
                vuv_inp = torch.cat([x, y_bap, y_lf0], dim=-1)
            vuv = self.vuv_model(vuv_inp, lengths, y_vuv)

        # make a concatenated stream
        has_postnet_output = (
            isinstance(mgc, list) or isinstance(bap, list) or isinstance(vuv, list)
        )
        if has_postnet_output:
            outs = []
            for idx in range(len(mgc)):
                mgc_ = mgc[idx] if isinstance(mgc, list) else mgc
                lf0_ = lf0[idx] if isinstance(lf0, list) else lf0
                vuv_ = vuv[idx] if isinstance(vuv, list) else vuv
                bap_ = bap[idx] if isinstance(bap, list) else bap
                out = torch.cat([mgc_, lf0_, vuv_, bap_], dim=-1)
                assert out.shape[-1] == self.out_dim
                outs.append(out)
        else:
            outs = torch.cat([mgc, lf0, vuv, bap], dim=-1)
            assert outs.shape[-1] == self.out_dim

        return outs, lf0_residual

    def inference(self, x, lengths=None):
        return pad_inference(
            model=self,
            x=x,
            lengths=lengths,
            reduction_factor=self.reduction_factor,
            mdn=False,
        )


class MultistreamSeparateF0MelModel(BaseModel):
    def __init__(
        self,
        in_dim: int,
        out_dim: int,
        stream_sizes: list,
        reduction_factor: int,
        encoder: nn.Module,
        mel_model: nn.Module,
        lf0_model: nn.Module,
        vuv_model: nn.Module,
        # NOTE: you must carefully set the following parameters
        in_rest_idx=1,
        in_lf0_idx=300,
        in_lf0_min=5.3936276,
        in_lf0_max=6.491111,
        out_lf0_idx=180,
        out_lf0_mean=5.953093881972361,
        out_lf0_scale=0.23435173188961034,
    ):
        super().__init__()
        self.in_dim = in_dim
        self.out_dim = out_dim
        self.stream_sizes = stream_sizes
        self.reduction_factor = reduction_factor

        assert len(stream_sizes) == 3

        self.encoder = encoder
        if self.encoder is not None:
            assert not encoder.is_autoregressive()
        self.mel_model = mel_model
        self.lf0_model = lf0_model
        self.vuv_model = vuv_model
        self.in_rest_idx = in_rest_idx
        self.in_lf0_idx = in_lf0_idx
        self.in_lf0_min = in_lf0_min
        self.in_lf0_max = in_lf0_max
        self.out_lf0_idx = out_lf0_idx
        self.out_lf0_mean = out_lf0_mean
        self.out_lf0_scale = out_lf0_scale

    def _set_lf0_params(self):
        # Special care for residual F0 prediction models
        # NOTE: don't overwrite out_lf0_idx and in_lf0_idx
        if hasattr(self.lf0_model, "out_lf0_mean"):
            self.lf0_model.in_lf0_min = self.in_lf0_min
            self.lf0_model.in_lf0_max = self.in_lf0_max
            self.lf0_model.out_lf0_mean = self.out_lf0_mean
            self.lf0_model.out_lf0_scale = self.out_lf0_scale

    def is_autoregressive(self):
        return (
            self.mel_model.is_autoregressive()
            or self.lf0_model.is_autoregressive()
            or self.vuv_model.is_autoregressive()
        )

    def forward(self, x, lengths=None, y=None):
        self._set_lf0_params()
        assert x.shape[-1] == self.in_dim

        if y is not None:
            # Teacher-forcing
            outs = split_streams(y, self.stream_sizes)
            y_mel, y_lf0, y_vuv = outs
        else:
            # Inference
            y_mel, y_lf0, y_vuv = (
                None,
                None,
                None,
            )

        # Predict continuous log-F0 first
        lf0, lf0_residual = self.lf0_model(x, lengths, y_lf0)

        if self.encoder is not None:
            encoder_outs = self.encoder(x, lengths)
            # Concat log-F0, rest flags and the outputs of the encoder
            # This may make the decoder to be aware of the input F0
            rest_flags = x[:, :, self.in_rest_idx].unsqueeze(-1)
            if y is not None:
                encoder_outs = torch.cat([encoder_outs, rest_flags, y_lf0], dim=-1)
            else:
                encoder_outs = torch.cat([encoder_outs, rest_flags, lf0], dim=-1)
        else:
            encoder_outs = x

        # Decoders for each stream
        mel = self.mel_model(encoder_outs, lengths, y_mel)
        vuv = self.vuv_model(encoder_outs, lengths, y_vuv)

        # make a concatenated stream
        has_postnet_output = (
            isinstance(mel, list) or isinstance(lf0, list) or isinstance(vuv, list)
        )
        if has_postnet_output:
            outs = []
            for idx in range(len(mel)):
                mel_ = mel[idx] if isinstance(mel, list) else mel
                lf0_ = lf0[idx] if isinstance(lf0, list) else lf0
                vuv_ = vuv[idx] if isinstance(vuv, list) else vuv
                out = torch.cat([mel_, lf0_, vuv_], dim=-1)
                assert out.shape[-1] == self.out_dim
                outs.append(out)
            return outs, lf0_residual
        else:
            out = torch.cat(
                [
                    mel,
                    lf0,
                    vuv,
                ],
                dim=-1,
            )
            assert out.shape[-1] == self.out_dim

        return out, lf0_residual

    def inference(self, x, lengths=None):
        return pad_inference(
            model=self, x=x, lengths=lengths, reduction_factor=self.reduction_factor
        )


class HybridMultistreamSeparateF0MelModel(BaseModel):
    """V/UV prediction from mel-spectrogram"""

    def __init__(
        self,
        in_dim: int,
        out_dim: int,
        stream_sizes: list,
        reduction_factor: int,
        mel_model: nn.Module,
        lf0_model: nn.Module,
        vuv_model: nn.Module,
        # NOTE: you must carefully set the following parameters
        in_rest_idx=0,
        in_lf0_idx=51,
        in_lf0_min=5.3936276,
        in_lf0_max=6.491111,
        out_lf0_idx=60,
        out_lf0_mean=5.953093881972361,
        out_lf0_scale=0.23435173188961034,
    ):
        super().__init__()
        self.in_dim = in_dim
        self.out_dim = out_dim
        self.stream_sizes = stream_sizes
        self.reduction_factor = reduction_factor

        assert len(stream_sizes) in [3]

        self.mel_model = mel_model
        self.lf0_model = lf0_model
        self.vuv_model = vuv_model
        self.in_rest_idx = in_rest_idx
        self.in_lf0_idx = in_lf0_idx
        self.in_lf0_min = in_lf0_min
        self.in_lf0_max = in_lf0_max
        self.out_lf0_idx = out_lf0_idx
        self.out_lf0_mean = out_lf0_mean
        self.out_lf0_scale = out_lf0_scale

    def _set_lf0_params(self):
        # Special care for residual F0 prediction models
        # NOTE: don't overwrite out_lf0_idx and in_lf0_idx
        if hasattr(self.lf0_model, "out_lf0_mean"):
            self.lf0_model.in_lf0_min = self.in_lf0_min
            self.lf0_model.in_lf0_max = self.in_lf0_max
            self.lf0_model.out_lf0_mean = self.out_lf0_mean
            self.lf0_model.out_lf0_scale = self.out_lf0_scale

    def prediction_type(self):
        return PredictionType.MULTISTREAM_HYBRID

    def is_autoregressive(self):
        return (
            self.mel_model.is_autoregressive()
            or self.lf0_model.is_autoregressive()
            or self.vuv_model.is_autoregressive()
        )

    def forward(self, x, lengths=None, y=None):
        self._set_lf0_params()
        assert x.shape[-1] == self.in_dim
        is_inference = y is None

        if y is not None:
            # Teacher-forcing
            outs = split_streams(y, self.stream_sizes)
            y_mel, y_lf0, y_vuv = outs
        else:
            # Inference
            y_mel, y_lf0, y_vuv = (
                None,
                None,
                None,
            )

        # Predict continuous log-F0 first
        if is_inference:
            lf0, lf0_residual = self.lf0_model.inference(x, lengths), None
        else:
            lf0, lf0_residual = self.lf0_model(x, lengths, y_lf0)

        # Predict mel
        if is_inference:
            mel_inp = torch.cat([x, lf0], dim=-1)
            mel = self.mel_model.inference(mel_inp, lengths)
        else:
            mel_inp = torch.cat([x, y_lf0], dim=-1)
            mel = self.mel_model(mel_inp, lengths, y_mel)

        # Predict V/UV
        if is_inference:
            vuv_inp = torch.cat([x, lf0, mel[1]], dim=-1)
            vuv = self.vuv_model.inference(vuv_inp, lengths)
        else:
            vuv_inp = torch.cat([x, lf0, y_mel], dim=-1)
            vuv = self.vuv_model(vuv_inp, lengths, y_vuv)

        if is_inference:
            out = torch.cat([mel[0], lf0, vuv], dim=-1)
            assert out.shape[-1] == self.out_dim
            # TODO: better design
            return out, out
        else:
            return (mel, lf0, vuv), lf0_residual

    def inference(self, x, lengths=None):
        return pad_inference(
            model=self,
            x=x,
            lengths=lengths,
            reduction_factor=self.reduction_factor,
            mdn=True,
        )
