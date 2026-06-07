"""NVIDIA Parakeet local speech-to-text adapter.

Wraps the NVIDIA NeMo ASR toolkit (https://github.com/NVIDIA/NeMo) so the
voice pipeline can transcribe entirely on-device with no audio leaving
the host. Parakeet is a family of fast, accurate ASR models from
NVIDIA; the default :data:`DEFAULT_MODEL_ID` is
``nvidia/parakeet-tdt-0.6b-v3`` — a Token-Duration Transducer model
that supports streaming inference and achieves state-of-the-art WER
on English audio. Other published Parakeet checkpoints (TDT, RNNT,
CTC; 110 M / 0.6 B / 1.1 B parameters) are accepted via the
``model_id`` option.

Model weights are downloaded by NeMo / HuggingFace on first use and
cached to a directory that is bind-mounted from the host in production
so they survive container rebuilds. Default location is
``/var/lib/johnny/parakeet-models``; override via the ``model_dir``
provider option or the ``JOHNNY_PARAKEET_MODEL_DIR`` environment
variable. We point ``HF_HOME`` at that directory before calling
``ASRModel.from_pretrained`` so all HuggingFace assets land in one
predictable place — matching the host-bind-mount pattern Piper /
faster-whisper use (see ``piper-voice-catalog-307-fix`` memory).

The adapter expects 16 kHz mono S16LE PCM frames in ``audio_iter`` —
the format produced by the meet-worker audio bridge. PCM bytes are
concatenated into a single utterance buffer and passed to the model
in one call. The pipeline (``VoicePipeline._utterances()``) segments
audio into VAD-bounded chunks before handing them to STT, so the
adapter treats each ``transcribe_stream`` invocation as one complete
utterance rather than imposing its own fixed window. A v1 batch
implementation that emits one final :class:`TranscriptEvent` per
utterance ships here; Johnny-stt.3 will wire up Parakeet's
Cache-Aware Streaming inference so partial deltas flow to the live
chat surface.

Latency profile: model load happens once per adapter instance and
ranges from ~3–5 s for the 0.6 B variants on CPU to ~1 s on CUDA.
Subsequent transcriptions on the same instance are bounded by model
size and utterance duration — Parakeet TDT 0.6 B is roughly 5–10×
faster than ``whisper-base`` on CPU for the same audio.

**License**: the upstream NeMo toolkit ships under the Apache 2.0
license. The default model checkpoint ``nvidia/parakeet-tdt-0.6b-v3``
is distributed by NVIDIA under CC-BY-4.0 — usable in commercial
products with attribution. See the model card at
https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3 for the
full license text and citation requirements.
"""

from __future__ import annotations

import array
import asyncio
import logging
import os
from collections.abc import AsyncIterator
from importlib import import_module
from typing import Any, Protocol, runtime_checkable

from app.providers.base import (
    PCM_SAMPLE_RATE_HZ,
    PCM_SAMPLE_WIDTH_BYTES,
    ProviderConfig,
    ProviderKind,
    STTError,
    STTProvider,
    TranscriptEvent,
    get_registry,
)
from app.providers.schema import (
    FieldDef,
    FieldGroup,
    FieldOption,
    FieldType,
    ProviderSchema,
    ProviderTip,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "parakeet"
DEFAULT_MODEL_ID = "nvidia/parakeet-tdt-0.6b-v3"
DEFAULT_MODEL_DIR = "/var/lib/johnny/parakeet-models"
DEFAULT_DEVICE = "cpu"
DEFAULT_BEAM_SIZE = 1
DEFAULT_LANGUAGE = "en"
ALLOWED_MODEL_IDS = frozenset(
    {
        # The default — recommended for new deployments. SOTA English ASR.
        "nvidia/parakeet-tdt-0.6b-v3",
        # Previous release of the same architecture; kept for reproducibility.
        "nvidia/parakeet-tdt-0.6b-v2",
        # Smaller TDT+CTC hybrid for resource-constrained hosts.
        "nvidia/parakeet-tdt_ctc-110m",
        # Larger RNN-T variant — slightly better WER, ~2× slower.
        "nvidia/parakeet-rnnt-1.1b",
        # Pure CTC 1.1 B — useful when downstream tooling needs frame-aligned
        # token probabilities rather than transducer hypotheses.
        "nvidia/parakeet-ctc-1.1b",
    }
)
ALLOWED_DEVICES = frozenset({"cpu", "cuda", "mps", "auto"})


@runtime_checkable
class _Hypothesis(Protocol):
    """Minimal subset of NeMo's ``Hypothesis`` return type.

    Recent NeMo releases return :class:`nemo.collections.asr.parts.utils.rnnt_utils.Hypothesis`
    instances from :meth:`ASRModel.transcribe`; older releases return raw
    strings. The adapter accepts both via :func:`_hypothesis_text`.
    """

    text: str


@runtime_checkable
class _ASRModel(Protocol):
    """Minimal protocol matching ``nemo.collections.asr.models.ASRModel``."""

    def transcribe(
        self,
        audio: Any,
        *,
        batch_size: int = ...,
        **kwargs: Any,
    ) -> list[Any]: ...


class ParakeetSTT(STTProvider):
    """Streaming-compatible STT via NVIDIA NeMo / Parakeet.

    Configuration ``options`` (any key may be omitted):

    * ``model_id`` — HuggingFace repo id of the Parakeet checkpoint
      (default ``nvidia/parakeet-tdt-0.6b-v3``). Must be one of
      :data:`ALLOWED_MODEL_IDS`; submitting an unknown id raises
      :class:`ValueError` at config time so a typo can't silently
      download the wrong model at first transcription.
    * ``model_dir`` — directory holding cached weights. Falls back to
      ``JOHNNY_PARAKEET_MODEL_DIR``, then
      ``/var/lib/johnny/parakeet-models`` (bind-mounted from the host
      in production so downloads survive container rebuilds).
    * ``device`` — ``cpu`` (default), ``cuda``, ``mps``, ``auto``.
    * ``beam_size`` — transducer beam search width (default 1 / greedy).
      Higher values trade latency for accuracy.
    * ``language`` — force a language code (default ``en``). Most
      Parakeet checkpoints are English-only; multilingual variants
      accept the standard ISO 639-1 codes.

    The adapter is **batch-oriented** in v1: it buffers the whole
    utterance from ``audio_iter`` and runs a single
    :meth:`ASRModel.transcribe` call, emitting one final
    :class:`TranscriptEvent` per non-empty hypothesis. Johnny-stt.3
    will wire up NeMo's Cache-Aware Streaming inference to emit
    partial-result deltas for the live-chat surface.
    """

    def __init__(self, config: ProviderConfig) -> None:
        if config.kind is not ProviderKind.STT:
            raise ValueError(
                f"ParakeetSTT requires ProviderKind.STT; got {config.kind.value}"
            )
        opts = config.options
        model_id = str(opts.get("model_id") or DEFAULT_MODEL_ID)
        if model_id not in ALLOWED_MODEL_IDS:
            raise ValueError(
                f"model_id {model_id!r} must be one of {sorted(ALLOWED_MODEL_IDS)}"
            )
        self._model_id = model_id
        self._model_dir = str(
            opts.get("model_dir")
            or os.environ.get("JOHNNY_PARAKEET_MODEL_DIR")
            or DEFAULT_MODEL_DIR
        )
        device = str(opts.get("device") or DEFAULT_DEVICE)
        if device not in ALLOWED_DEVICES:
            raise ValueError(
                f"device {device!r} must be one of {sorted(ALLOWED_DEVICES)}"
            )
        self._device = device
        beam_size_opt = opts.get("beam_size")
        beam_size = int(beam_size_opt) if beam_size_opt is not None else DEFAULT_BEAM_SIZE
        if beam_size <= 0:
            raise ValueError(f"beam_size must be positive; got {beam_size}")
        self._beam_size = beam_size
        language = opts.get("language")
        self._language: str | None = (
            str(language) if language not in (None, "") else None
        )
        self._model: _ASRModel | None = None
        self._model_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @classmethod
    def field_schema(cls) -> ProviderSchema:
        return ProviderSchema(
            kind=ProviderKind.STT,
            provider_name=PROVIDER_NAME,
            display_name="NVIDIA Parakeet (NeMo)",
            summary=(
                "Fast on-device ASR from NVIDIA. Streaming-capable "
                "architecture; no audio leaves your host."
            ),
            signup_url="https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3",
            fields=(
                FieldDef(
                    name="model_id",
                    label="Model",
                    type=FieldType.SELECT,
                    default=DEFAULT_MODEL_ID,
                    help_text=(
                        "Pick a Parakeet checkpoint. The 0.6 B TDT v3 model "
                        "is the recommended default for new deployments."
                    ),
                    options=tuple(
                        FieldOption(value=m, label=m) for m in sorted(ALLOWED_MODEL_IDS)
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="language",
                    label="Language",
                    placeholder="en",
                    default=DEFAULT_LANGUAGE,
                    help_text=(
                        "ISO 639-1 code. Most Parakeet checkpoints are "
                        "English-only; leave blank to use the model's default."
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="model_dir",
                    label="Model directory",
                    default=DEFAULT_MODEL_DIR,
                    help_text=(
                        "Where Parakeet / HuggingFace weights live on disk. "
                        "Bind-mounted from the host in production so the "
                        "~600 MB download survives container rebuilds."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="device",
                    label="Device",
                    type=FieldType.SELECT,
                    default=DEFAULT_DEVICE,
                    options=(
                        FieldOption(value="cpu", label="cpu"),
                        FieldOption(value="cuda", label="cuda (NVIDIA GPU)"),
                        FieldOption(value="mps", label="mps (Apple Silicon)"),
                        FieldOption(value="auto", label="auto"),
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="beam_size",
                    label="Beam size",
                    type=FieldType.NUMBER,
                    default=DEFAULT_BEAM_SIZE,
                    help_text=(
                        "Transducer beam search width. 1 = greedy decode "
                        "(fastest). Higher values trade latency for accuracy."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
            ),
            tips=(
                ProviderTip(
                    topic="Parakeet shines on GPU; CPU is workable but slow",
                    body=(
                        "On an NVIDIA GPU, Parakeet 0.6B TDT v3 "
                        "transcribes a 3-second utterance in ~80-150 "
                        "ms — comparable to Deepgram cloud. On CPU "
                        "the same utterance can take 1-2 s. If you "
                        "don't have a GPU, faster-whisper int8 is "
                        "usually faster on CPU."
                    ),
                ),
                ProviderTip(
                    topic="0.6B TDT v3 is the default for a reason",
                    body=(
                        "Newer Transducer-Decoder Transducer (TDT) "
                        "architecture is markedly faster than the "
                        "older RNN-T checkpoints at comparable "
                        "accuracy. Stay on the 0.6B unless you're "
                        "specifically benchmarking a larger build."
                    ),
                ),
                ProviderTip(
                    topic="Beam size 1 is fine for greedy speech",
                    body=(
                        "TDT beam decode adds latency without much "
                        "accuracy gain on clear conversational "
                        "speech. Bump to 4-8 only if you're seeing "
                        "wrong words on noisy / accented input and "
                        "have GPU headroom."
                    ),
                ),
                ProviderTip(
                    topic="English-only by default",
                    body=(
                        "Most public Parakeet checkpoints are "
                        "English-only. For other languages, prefer "
                        "faster-whisper (multilingual) or ElevenLabs "
                        "Scribe."
                    ),
                ),
            ),
        )

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def model_dir(self) -> str:
        return self._model_dir

    @property
    def device(self) -> str:
        return self._device

    @property
    def beam_size(self) -> int:
        return self._beam_size

    @property
    def language(self) -> str | None:
        return self._language

    async def transcribe_stream(
        self,
        audio_iter: AsyncIterator[bytes],
    ) -> AsyncIterator[TranscriptEvent]:
        """Consume PCM utterance from ``audio_iter`` and yield TranscriptEvents.

        Treats the input iterator as one logical utterance — the pipeline
        already chops audio into VAD-bounded segments before handing it
        to STT, so the adapter concatenates whatever arrives and runs a
        single transcribe call. The current implementation emits a
        single final event per utterance; partial deltas land in
        Johnny-stt.3 via NeMo Cache-Aware Streaming inference.
        """
        buffer = bytearray()
        async for chunk in audio_iter:
            if chunk:
                buffer.extend(chunk)
        if not buffer:
            return
        if len(buffer) % PCM_SAMPLE_WIDTH_BYTES:
            raise STTError(
                f"audio buffer {len(buffer)} bytes is not aligned to "
                f"{PCM_SAMPLE_WIDTH_BYTES}-byte S16 samples"
            )

        waveform = _pcm16_bytes_to_float32(bytes(buffer))
        audio_ms = int(len(buffer) * 1000 / (PCM_SAMPLE_RATE_HZ * PCM_SAMPLE_WIDTH_BYTES))
        try:
            model = await self._ensure_model()
            hypotheses = await asyncio.to_thread(
                self._run_transcribe, model, waveform
            )
        except STTError:
            raise
        except Exception as exc:
            raise STTError(f"parakeet transcribe failed: {exc}") from exc

        emitted = False
        for hypothesis in hypotheses:
            text = _hypothesis_text(hypothesis).strip()
            if not text:
                continue
            yield TranscriptEvent(
                text=text,
                is_final=True,
                timestamp_ms=0,
                confidence=_hypothesis_confidence(hypothesis),
            )
            emitted = True

        if not emitted:
            logger.debug(
                "parakeet produced no usable hypotheses for %d ms utterance "
                "(%d-byte buffer)",
                audio_ms,
                len(buffer),
            )

    async def close(self) -> None:
        """Release the underlying model handle."""
        self._model = None

    # --- Hooks (overridable in tests) -------------------------------------

    async def _ensure_model(self) -> _ASRModel:
        async with self._model_lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._load_model)
        return self._model

    def _load_model(self) -> _ASRModel:
        """Load and return an :class:`ASRModel` instance.

        Imports the NeMo toolkit lazily so this adapter module can be
        imported in tests / lightweight containers without the optional
        dep installed. Points ``HF_HOME`` at the configured model
        directory before the ``from_pretrained`` call so HuggingFace
        downloads land in the bind-mounted host path rather than
        ``~/.cache``.
        """
        os.environ.setdefault("HF_HOME", self._model_dir)
        os.environ.setdefault("NEMO_CACHE_DIR", self._model_dir)
        try:
            nemo_asr = import_module("nemo.collections.asr")
        except ImportError as exc:
            # NeMo is not baked into the api/meet-worker images — it
            # ships as a runtime install via the Parakeet provider
            # card's Install button (matches the Piper voice catalog
            # UX). Embed the underlying ImportError detail so version-
            # conflict failures inside NeMo's own import chain (e.g.
            # transformers rejecting tokenizers) surface their real
            # cause instead of getting flattened into "not installed".
            raise STTError(
                f"NeMo not importable: {exc}. Click 'Install package' "
                "on the Parakeet provider card in Settings → Providers "
                "to install nemo_toolkit[asr] into the runtime package "
                "directory (~/.johnny/parakeet-packages)."
            ) from exc
        try:
            asr_model_cls = nemo_asr.models.ASRModel
        except AttributeError as exc:
            raise STTError(
                "nemo.collections.asr.models.ASRModel missing — incompatible NeMo version?"
            ) from exc
        try:
            model = asr_model_cls.from_pretrained(self._model_id)
        except Exception as exc:
            raise STTError(
                f"failed to load Parakeet model {self._model_id!r} "
                f"from cache {self._model_dir!r}: {exc}"
            ) from exc
        _maybe_move_to_device(model, self._device)
        _maybe_set_beam_size(model, self._beam_size)
        return _cast_to_model_protocol(model)

    def _run_transcribe(
        self,
        model: _ASRModel,
        waveform: Any,
    ) -> list[Any]:
        """Run the blocking transcribe call; overridable in tests.

        NeMo's :meth:`ASRModel.transcribe` accepts a list of waveforms
        (numpy float32 arrays or paths) and returns a list of
        :class:`Hypothesis` objects (or raw strings on older releases).
        We pass exactly one waveform and unwrap the single-element list.
        """
        return model.transcribe([waveform], batch_size=1)


def _pcm16_bytes_to_float32(pcm: bytes) -> Any:
    """Convert 16-bit signed-LE PCM bytes into a float32 waveform in [-1, 1].

    Returns a numpy ``ndarray`` when numpy is importable (NeMo requires
    numpy at runtime, so this is the normal production path). Falls
    back to an ``array.array("f")`` when numpy is absent so the module
    remains importable in lightweight test environments that use fake
    models and never call into the real library.
    """
    samples = array.array("h")
    samples.frombytes(pcm)
    try:
        np = import_module("numpy")
    except ImportError:
        return array.array("f", [s / 32768.0 for s in samples])
    arr = np.asarray(samples, dtype=np.float32)
    return arr / 32768.0


def _hypothesis_text(hypothesis: Any) -> str:
    """Extract the transcript string from a NeMo ``transcribe`` return value.

    Recent NeMo (>=1.20) returns :class:`Hypothesis` objects with a
    ``.text`` attribute. Older releases return raw strings. A few
    inference variants return ``(text, raw)`` tuples. Returns the empty
    string on any shape we don't recognize so the adapter degrades to
    "no transcript" rather than crashing on a NeMo version bump.
    """
    if isinstance(hypothesis, str):
        return hypothesis
    text_attr = getattr(hypothesis, "text", None)
    if isinstance(text_attr, str):
        return text_attr
    if isinstance(hypothesis, tuple) and hypothesis and isinstance(hypothesis[0], str):
        return hypothesis[0]
    return ""


def _hypothesis_confidence(hypothesis: Any) -> float | None:
    """Extract a confidence proxy from a NeMo hypothesis, when available.

    NeMo's :class:`Hypothesis` objects optionally carry a per-token
    ``y_sequence`` and a ``score`` (cumulative log-probability). We
    return ``None`` when the field is absent or NaN so the catalog UI
    omits the confidence column rather than rendering ``-Infinity``.
    """
    score = getattr(hypothesis, "score", None)
    if score is None:
        return None
    try:
        value = float(score)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN check without importing math
        return None
    # Parakeet hypothesis scores are unnormalized log-probabilities, not
    # in [0, 1]. We clamp the exp into the unit interval so consumers
    # treat it as a relative confidence — same convention as the
    # faster-whisper adapter's avg_logprob → exp() mapping.
    try:
        import math
        exp_value = math.exp(value)
    except (OverflowError, ValueError):
        return None
    return max(0.0, min(1.0, exp_value))


def _maybe_move_to_device(model: Any, device: str) -> None:
    """Best-effort device placement that survives NeMo API drift.

    ``ASRModel`` instances are :class:`torch.nn.Module`s under the hood
    and respond to ``.to("cuda")`` etc. Skipped for ``auto`` so PyTorch
    picks the default device. Swallows AttributeError so the adapter
    still works if NeMo changes its base class.
    """
    if device in ("", "auto"):
        return
    move = getattr(model, "to", None)
    if not callable(move):
        return
    try:
        move(device)
    except Exception as exc:  # noqa: BLE001 — device move is best effort
        logger.warning(
            "parakeet: could not move model to device %r: %s", device, exc
        )


def _maybe_set_beam_size(model: Any, beam_size: int) -> None:
    """Best-effort beam size config that survives NeMo API drift.

    NeMo exposes decoder settings via ``change_decoding_strategy``;
    older releases used ``decoding.cfg.beam.beam_size``. Both paths
    are swallowed on failure so a missing knob doesn't break model
    loading.
    """
    if beam_size <= 1:
        return
    change = getattr(model, "change_decoding_strategy", None)
    if callable(change):
        try:
            change({"strategy": "beam", "beam": {"beam_size": beam_size}})
            return
        except Exception as exc:  # noqa: BLE001 — knob is optional
            logger.warning(
                "parakeet: change_decoding_strategy(beam=%d) failed: %s",
                beam_size,
                exc,
            )


def _cast_to_model_protocol(model: Any) -> _ASRModel:
    """Narrow the dynamic NeMo model to the adapter's protocol."""
    return model  # type: ignore[no-any-return]


def register(*, replace: bool = False) -> None:
    """Register :class:`ParakeetSTT` under ``(ProviderKind.STT, "parakeet")``.

    Safe to call from :mod:`app.providers` import even when NeMo is not
    installed — the library is only imported lazily inside
    :meth:`ParakeetSTT._load_model`. Misconfigured deployments fail
    loudly when the model is actually needed, not at package import.
    """
    get_registry().register(
        ProviderKind.STT, PROVIDER_NAME, ParakeetSTT, replace=replace
    )


__all__ = [
    "ALLOWED_DEVICES",
    "ALLOWED_MODEL_IDS",
    "DEFAULT_BEAM_SIZE",
    "DEFAULT_DEVICE",
    "DEFAULT_LANGUAGE",
    "DEFAULT_MODEL_DIR",
    "DEFAULT_MODEL_ID",
    "PROVIDER_NAME",
    "ParakeetSTT",
    "register",
]
