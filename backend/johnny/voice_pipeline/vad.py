"""Voice Activity Detection adapters for the pipeline.

The pipeline depends only on the :class:`VADAnalyzer` ABC. Two concrete
implementations ship:

* :class:`EnergyVAD` — pure-stdlib RMS-amplitude detector. Default in
  tests and a sensible fallback when no ML model is available.
* :class:`SileroVAD` — wrapper around the ``silero-vad`` PyPI package
  (PyTorch + ONNX-backed neural VAD). Optional dependency; the class
  imports it lazily and raises a friendly error if not installed.

Per-meeting threshold tuning is supported via the ``threshold`` argument
on :class:`VADAnalyzer`. The pipeline reads
:attr:`PipelineConfig.vad_threshold` and constructs the analyzer with it.
"""

from __future__ import annotations

import array
import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_VAD_THRESHOLD = 0.5
"""Default speech-probability / amplitude threshold for VAD analyzers.

EnergyVAD treats this as a normalised RMS amplitude (0..1 relative to
full-scale s16). SileroVAD treats it as a speech probability in the same
range. 0.5 is a sensible starting point for both.
"""

PCM_SAMPLE_WIDTH_BYTES = 2  # s16le mono
DEFAULT_SAMPLE_RATE = 16_000


@dataclass(frozen=True, slots=True)
class VADResult:
    """Outcome of analysing one audio frame.

    ``is_speech`` is the final boolean decision after thresholding.
    ``score`` is the raw analyzer output (RMS or probability) so callers
    can log / tune without re-running detection.
    """

    is_speech: bool
    score: float


class VADAnalyzer(ABC):
    """Voice activity detector contract.

    Analysers are *stateful*: implementations may hold internal buffers
    (e.g. Silero's recurrent state). Call :meth:`reset` between distinct
    audio streams.
    """

    @property
    @abstractmethod
    def threshold(self) -> float:
        """Decision threshold currently in effect."""

    @abstractmethod
    def analyze(self, pcm_frame: bytes) -> VADResult:
        """Score a single PCM frame and return the thresholded decision.

        ``pcm_frame`` is 16-bit signed little-endian mono PCM at the
        configured sample rate. Frame size is implementation-specific.
        """

    def reset(self) -> None:  # noqa: B027 — intentional default no-op
        """Clear internal state. Default is a no-op for stateless analysers."""


class EnergyVAD(VADAnalyzer):
    """RMS-amplitude voice activity detector — no ML deps.

    Computes the root-mean-square amplitude of the frame and normalises
    against full-scale s16 (32 768). Frames whose RMS exceeds
    :attr:`threshold` are flagged as speech. Adequate for quiet
    environments and unit-test fixtures; in noisy meetings prefer
    :class:`SileroVAD`.
    """

    def __init__(self, threshold: float = DEFAULT_VAD_THRESHOLD) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold must be in [0, 1]; got {threshold}")
        self._threshold = threshold

    @property
    def threshold(self) -> float:
        return self._threshold

    def analyze(self, pcm_frame: bytes) -> VADResult:
        if not pcm_frame:
            return VADResult(is_speech=False, score=0.0)
        if len(pcm_frame) % PCM_SAMPLE_WIDTH_BYTES:
            raise ValueError("PCM byte length must be even for 16-bit samples")
        samples = array.array("h")
        samples.frombytes(pcm_frame)
        if not samples:
            return VADResult(is_speech=False, score=0.0)
        sumsq = 0
        for s in samples:
            sumsq += s * s
        rms = math.sqrt(sumsq / len(samples))
        normalised = min(1.0, rms / 32_768.0)
        return VADResult(is_speech=normalised >= self._threshold, score=normalised)


class SileroVAD(VADAnalyzer):
    """Silero VAD wrapper. Requires the ``silero-vad`` PyPI package.

    Import is lazy: constructing :class:`SileroVAD` triggers the model
    load, which raises a clear error if the dependency is missing. Tests
    that don't need a real model use :class:`EnergyVAD` directly.

    Silero expects 16 kHz mono and 32 ms frames (512 samples) — the
    pipeline aligns chunks accordingly before calling :meth:`analyze`.
    """

    def __init__(
        self,
        threshold: float = DEFAULT_VAD_THRESHOLD,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold must be in [0, 1]; got {threshold}")
        if sample_rate not in (8_000, 16_000):
            raise ValueError(f"Silero supports 8000 or 16000 Hz; got {sample_rate}")
        self._threshold = threshold
        self._sample_rate = sample_rate
        self._model: Any = None
        self._torch: Any = None
        self._load_model()

    def _load_model(self) -> None:
        try:
            import torch  # type: ignore[import-not-found]
            from silero_vad import load_silero_vad  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "SileroVAD requires the 'silero-vad' and 'torch' packages; "
                "install them or use EnergyVAD."
            ) from exc
        self._torch = torch
        self._model = load_silero_vad()

    @property
    def threshold(self) -> float:
        return self._threshold

    def analyze(self, pcm_frame: bytes) -> VADResult:
        if not pcm_frame:
            return VADResult(is_speech=False, score=0.0)
        if len(pcm_frame) % PCM_SAMPLE_WIDTH_BYTES:
            raise ValueError("PCM byte length must be even for 16-bit samples")
        samples = array.array("h")
        samples.frombytes(pcm_frame)
        # Normalise s16 → float32 in [-1, 1]
        tensor = self._torch.tensor(
            [s / 32_768.0 for s in samples], dtype=self._torch.float32
        )
        prob = float(self._model(tensor, self._sample_rate).item())
        return VADResult(is_speech=prob >= self._threshold, score=prob)

    def reset(self) -> None:
        if self._model is not None and hasattr(self._model, "reset_states"):
            self._model.reset_states()


__all__ = [
    "DEFAULT_VAD_THRESHOLD",
    "EnergyVAD",
    "SileroVAD",
    "VADAnalyzer",
    "VADResult",
]
