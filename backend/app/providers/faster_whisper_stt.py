"""faster-whisper local speech-to-text adapter.

Wraps the faster-whisper library (https://github.com/SYSTRAN/faster-whisper)
so the voice pipeline can transcribe entirely on-device with no audio
leaving the host. faster-whisper uses CTranslate2 for fast CPU/GPU
inference of OpenAI Whisper models; this adapter exposes the standard
:class:`STTProvider` contract so it is interchangeable with the cloud
STT adapters at the registry level.

The adapter expects 16 kHz mono S16LE PCM frames in ``audio_iter`` —
the format produced by the meet-worker audio bridge. PCM bytes are
concatenated into a single utterance buffer and passed to the model
in one call; this honours VAD boundaries supplied by the pipeline.
``VoicePipeline._utterances()`` already segments audio into speech
chunks bounded by silence, so the STT adapter treats each
``transcribe_stream`` invocation as one complete utterance rather than
imposing its own fixed window — no buffering windows, no stride.

Model files (CTranslate2 weights + tokenizer JSON) are downloaded by
faster-whisper on first use and cached to a directory mounted as a
Docker volume in production so they persist across container
rebuilds. Default location is ``/var/lib/johnny/whisper-models``;
override via the ``model_dir`` provider option or the
``JOHNNY_WHISPER_MODEL_DIR`` environment variable.

Latency profile: model load happens once per adapter instance and
ranges from ~1 s for ``tiny`` (CPU) to ~10 s for ``large-v3`` (CPU).
Subsequent transcriptions on the same instance are bounded by model
size and utterance duration.
"""

from __future__ import annotations

import array
import asyncio
import logging
import math
import os
from collections.abc import AsyncIterator
from importlib import import_module
from typing import Any, Protocol, runtime_checkable

from app.providers.base import (
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

PROVIDER_NAME = "faster-whisper"
DEFAULT_MODEL_SIZE = "base"
DEFAULT_MODEL_DIR = "/var/lib/johnny/whisper-models"
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE_TYPE = "int8"
DEFAULT_BEAM_SIZE = 5
DEFAULT_VAD_FILTER = False
DEFAULT_NO_SPEECH_THRESHOLD = 0.6
"""Drop segments whose ``no_speech_prob`` exceeds this (Johnny-31g).

Whisper is trained to always emit *something* for each chunk it sees;
when the input is pure silence the model fabricates a plausible-sounding
short string ("Does Olam A.P.I.", "Thanks for watching", random Welsh
nonsense, runs of dots, etc.) and tags it with a high ``no_speech_prob``
indicating it doesn't actually believe the audio was speech. The
adapter reads that signal directly and drops the segment before it
becomes a :class:`TranscriptEvent`, so the pipeline's text-only noise
gate (Johnny-ckz.14) is no longer the only line of defence — the gate
catches a curated stoplist of known patterns, but ``no_speech_prob``
catches novel hallucinations the stoplist hasn't seen yet.

``0.6`` matches faster-whisper's own internal default for the same
field; raising it loosens the gate (more hallucinations pass through);
lowering it tightens it (some quiet but real speech may be dropped).
Set to ``1.0`` to disable the filter and rely solely on the model's
internal silence detection.
"""
DEFAULT_CONDITION_ON_PREVIOUS_TEXT = False
"""Whether each chunk's decoding is conditioned on the previous chunk's text.

Default is ``False`` (Johnny-31g): the upstream library defaults this
to ``True`` for transcription continuity across long-form audio, but
that's exactly what lets a single silence hallucination ("Thanks for
watching") seed *more* hallucinations on subsequent silent chunks
within the same utterance. The voice pipeline already feeds the
adapter VAD-bounded single-utterance buffers, so cross-utterance
continuity is not relevant — disabling conditioning breaks the
hallucination drift loop with no quality cost.
"""
ALLOWED_MODEL_SIZES = frozenset(
    {
        "tiny",
        "tiny.en",
        "base",
        "base.en",
        "small",
        "small.en",
        "medium",
        "medium.en",
        "large-v3",
    }
)


@runtime_checkable
class _TranscriptionInfo(Protocol):
    """Minimal subset of ``faster_whisper.transcribe.TranscriptionInfo``."""

    language: str
    language_probability: float


@runtime_checkable
class _WhisperModel(Protocol):
    """Minimal protocol matching ``faster_whisper.WhisperModel.transcribe``."""

    def transcribe(
        self,
        audio: Any,
        *,
        language: str | None = ...,
        beam_size: int = ...,
        vad_filter: bool = ...,
        no_speech_threshold: float = ...,
        condition_on_previous_text: bool = ...,
        **kwargs: Any,
    ) -> tuple[Any, _TranscriptionInfo]: ...


class FasterWhisperSTT(STTProvider):
    """Streaming STT via the faster-whisper library.

    Configuration ``options`` (any key may be omitted):

    * ``model_size`` — one of ``tiny``, ``base``, ``small``, ``medium``,
      ``large-v3`` (default ``base``). English-only variants
      (``tiny.en``, ``base.en``, ``small.en``, ``medium.en``) are
      accepted too.
    * ``model_dir`` — directory holding cached model files. Falls back
      to ``JOHNNY_WHISPER_MODEL_DIR``, then
      ``/var/lib/johnny/whisper-models`` (mounted as a Docker volume in
      production).
    * ``device`` — ``cpu`` (default), ``cuda``, ``auto``.
    * ``compute_type`` — CTranslate2 quantisation; default ``int8``.
    * ``beam_size`` — beam search size (default 5).
    * ``language`` — force a language code; default lets Whisper detect.
    * ``vad_filter`` — whether to use Whisper's built-in VAD filter
      (default ``False`` — the pipeline already segments by VAD before
      handing audio to STT).
    * ``no_speech_threshold`` — drop segments whose ``no_speech_prob``
      exceeds this (default ``0.6``). The primary defence against
      silence hallucinations (Johnny-31g); set to ``1.0`` to disable.
    * ``condition_on_previous_text`` — whether each chunk is decoded
      conditioned on the previous chunk's text (default ``False``).
      Disabling breaks the hallucination drift loop on long silent
      segments without affecting accuracy on VAD-cut single utterances.
    """

    def __init__(self, config: ProviderConfig) -> None:
        if config.kind is not ProviderKind.STT:
            raise ValueError(
                f"FasterWhisperSTT requires ProviderKind.STT; got {config.kind.value}"
            )
        opts = config.options
        model_size = str(opts.get("model_size") or DEFAULT_MODEL_SIZE)
        if model_size not in ALLOWED_MODEL_SIZES:
            raise ValueError(
                f"model_size {model_size!r} must be one of {sorted(ALLOWED_MODEL_SIZES)}"
            )
        self._model_size = model_size
        self._model_dir = str(
            opts.get("model_dir")
            or os.environ.get("JOHNNY_WHISPER_MODEL_DIR")
            or DEFAULT_MODEL_DIR
        )
        self._device = str(opts.get("device") or DEFAULT_DEVICE)
        self._compute_type = str(opts.get("compute_type") or DEFAULT_COMPUTE_TYPE)
        beam_size_opt = opts.get("beam_size")
        beam_size = int(beam_size_opt) if beam_size_opt is not None else DEFAULT_BEAM_SIZE
        if beam_size <= 0:
            raise ValueError(f"beam_size must be positive; got {beam_size}")
        self._beam_size = beam_size
        language = opts.get("language")
        self._language: str | None = (
            str(language) if language not in (None, "") else None
        )
        self._vad_filter = bool(opts.get("vad_filter", DEFAULT_VAD_FILTER))
        no_speech_opt = opts.get("no_speech_threshold")
        no_speech_threshold = (
            float(no_speech_opt)
            if no_speech_opt is not None
            else DEFAULT_NO_SPEECH_THRESHOLD
        )
        if not 0.0 <= no_speech_threshold <= 1.0:
            raise ValueError(
                f"no_speech_threshold must be in [0, 1]; got {no_speech_threshold}"
            )
        self._no_speech_threshold = no_speech_threshold
        self._condition_on_previous_text = bool(
            opts.get(
                "condition_on_previous_text", DEFAULT_CONDITION_ON_PREVIOUS_TEXT
            )
        )
        self._model: _WhisperModel | None = None
        self._model_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @classmethod
    def field_schema(cls) -> ProviderSchema:
        return ProviderSchema(
            kind=ProviderKind.STT,
            provider_name=PROVIDER_NAME,
            display_name="Local Whisper (faster-whisper)",
            summary="Runs entirely on-device via CTranslate2. No audio leaves your host.",
            signup_url=None,
            fields=(
                FieldDef(
                    name="model_size",
                    label="Model size",
                    type=FieldType.SELECT,
                    default=DEFAULT_MODEL_SIZE,
                    help_text="Larger models are more accurate but slower.",
                    options=tuple(
                        FieldOption(value=m, label=m) for m in sorted(ALLOWED_MODEL_SIZES)
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="language",
                    label="Language",
                    placeholder="en",
                    help_text="Leave blank to auto-detect.",
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="model_dir",
                    label="Model directory",
                    default=DEFAULT_MODEL_DIR,
                    help_text="Where the CTranslate2 model files live on disk.",
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
                        FieldOption(value="auto", label="auto"),
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="compute_type",
                    label="Compute type",
                    type=FieldType.SELECT,
                    default=DEFAULT_COMPUTE_TYPE,
                    options=(
                        FieldOption(value="int8", label="int8 (fastest CPU)"),
                        FieldOption(value="int8_float16", label="int8_float16"),
                        FieldOption(value="float16", label="float16"),
                        FieldOption(value="float32", label="float32"),
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="beam_size",
                    label="Beam size",
                    type=FieldType.NUMBER,
                    default=DEFAULT_BEAM_SIZE,
                    help_text="Higher = more accurate, slower.",
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="vad_filter",
                    label="VAD filter",
                    type=FieldType.CHECKBOX,
                    default=DEFAULT_VAD_FILTER,
                    help_text="Drop non-speech segments before transcription.",
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="no_speech_threshold",
                    label="No-speech threshold",
                    type=FieldType.NUMBER,
                    default=DEFAULT_NO_SPEECH_THRESHOLD,
                    help_text=(
                        "Drop segments whose probability of being silence "
                        "exceeds this. Lower = stricter (fewer "
                        "hallucinations, slight risk of dropping quiet "
                        "real speech)."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="condition_on_previous_text",
                    label="Condition on previous text",
                    type=FieldType.CHECKBOX,
                    default=DEFAULT_CONDITION_ON_PREVIOUS_TEXT,
                    help_text=(
                        "Carry context across chunks. Disabled by default "
                        "to break Whisper hallucination drift on silence."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
            ),
            tips=(
                ProviderTip(
                    topic="Model size is the dominant knob",
                    body=(
                        "tiny / base / small / medium roughly trade "
                        "accuracy for latency: on a modern CPU expect "
                        "~80 ms (tiny), ~150 ms (base), ~350 ms "
                        "(small), ~900 ms (medium) per ~3 s utterance. "
                        "Default base is the sweet spot for English "
                        "meetings; bump to small only if you keep "
                        "seeing wrong words. medium is rarely worth it "
                        "without a GPU."
                    ),
                ),
                ProviderTip(
                    topic="CPU vs GPU — set device=auto if unsure",
                    body=(
                        "On NVIDIA hardware CUDA shrinks transcription "
                        "by 4-8x on small / medium models; on CPU-only "
                        "boxes it harmlessly falls back. Pair with "
                        "compute_type=float16 on GPU, int8 on CPU. "
                        "int8 on a recent x86 CPU is comparable to "
                        "float32 in accuracy and roughly 2x faster."
                    ),
                ),
                ProviderTip(
                    topic="Beam size — leave at 5 for speech",
                    body=(
                        "Beam size 1 (greedy) is ~30% faster but the "
                        "Whisper paper measures a real accuracy hit on "
                        "noisy speech. 5 (default) is the documented "
                        "sweet spot; values above 10 produce diminishing "
                        "returns and burn latency."
                    ),
                ),
                ProviderTip(
                    topic="Specify the language if you know it",
                    body=(
                        "Leaving language blank costs ~100 ms on every "
                        "utterance for auto-detection — set it to 'en' "
                        "(or your meeting language) for a free win. The "
                        "auto-detector also has a small chance of "
                        "guessing wrong on short utterances and "
                        "switching mid-meeting."
                    ),
                ),
                ProviderTip(
                    topic="no_speech_threshold filters silence hallucinations",
                    body=(
                        "Whisper sometimes fabricates a plausible string "
                        "on silent input ('Thanks for watching', 'Does "
                        "Olam A.P.I.'). The adapter drops segments whose "
                        "no_speech_prob exceeds this threshold. 0.6 is "
                        "the safe default — lower (0.4) if you still see "
                        "hallucinations, higher (0.8) if you suspect "
                        "the gate is dropping quiet real speech."
                    ),
                ),
                ProviderTip(
                    topic="VAD filter — leave on",
                    body=(
                        "The faster-whisper internal VAD trims non-"
                        "speech ranges before transcription, which "
                        "shrinks the work per utterance and rarely "
                        "drops anything useful. Disable only when "
                        "debugging a missing-word complaint and you "
                        "want raw model output."
                    ),
                ),
            ),
        )

    @property
    def model_size(self) -> str:
        return self._model_size

    @property
    def model_dir(self) -> str:
        return self._model_dir

    @property
    def device(self) -> str:
        return self._device

    @property
    def compute_type(self) -> str:
        return self._compute_type

    @property
    def beam_size(self) -> int:
        return self._beam_size

    @property
    def language(self) -> str | None:
        return self._language

    @property
    def vad_filter(self) -> bool:
        return self._vad_filter

    @property
    def no_speech_threshold(self) -> float:
        return self._no_speech_threshold

    @property
    def condition_on_previous_text(self) -> bool:
        return self._condition_on_previous_text

    async def transcribe_stream(
        self,
        audio_iter: AsyncIterator[bytes],
    ) -> AsyncIterator[TranscriptEvent]:
        """Consume PCM utterance from ``audio_iter`` and yield TranscriptEvents.

        Treats the input iterator as one logical utterance — the pipeline
        already chops audio into VAD-bounded segments before handing it
        to STT, so the adapter concatenates whatever arrives and runs a
        single transcribe call. No fixed-window assumption is imposed on
        the input audio.
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
        try:
            model = await self._ensure_model()
            segments_iter, _ = await asyncio.to_thread(
                self._run_transcribe, model, waveform
            )
        except STTError:
            raise
        except Exception as exc:
            raise STTError(f"faster-whisper transcribe failed: {exc}") from exc

        emitted = False
        for segment in segments_iter:
            text = getattr(segment, "text", "") or ""
            text = text.strip()
            if not text:
                continue
            # Johnny-31g: drop silence-hallucinated segments before they
            # become TranscriptEvents. Whisper sets ``no_speech_prob``
            # high (>0.6 by default) when it thinks the audio is silence
            # but emits a fabricated string anyway ("Does Olam A.P.I.",
            # "Thanks for watching", runs of dots, random other-language
            # nonsense). Filtering here keeps the downstream noise gate
            # (Johnny-ckz.14) focused on the curated stoplist instead of
            # chasing every novel hallucination, and stops the row from
            # ever reaching the transcripts table.
            no_speech_prob = _coerce_no_speech_prob(
                getattr(segment, "no_speech_prob", None)
            )
            if (
                no_speech_prob is not None
                and no_speech_prob > self._no_speech_threshold
            ):
                logger.info(
                    "faster-whisper dropped silence-hallucinated segment "
                    "(no_speech_prob=%.3f > %.3f) text=%r",
                    no_speech_prob,
                    self._no_speech_threshold,
                    text,
                )
                continue
            start_ms = int(getattr(segment, "start", 0.0) * 1000)
            confidence = _logprob_to_confidence(
                getattr(segment, "avg_logprob", None)
            )
            yield TranscriptEvent(
                text=text,
                is_final=True,
                timestamp_ms=start_ms,
                confidence=confidence,
            )
            emitted = True

        if not emitted:
            logger.debug(
                "faster-whisper produced no usable segments for %d-byte utterance",
                len(buffer),
            )

    async def close(self) -> None:
        """Release the underlying model handle."""
        self._model = None

    # --- Hooks (overridable in tests) -------------------------------------

    async def _ensure_model(self) -> _WhisperModel:
        async with self._model_lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._load_model)
        return self._model

    def _load_model(self) -> _WhisperModel:
        """Load and return a ``WhisperModel`` instance.

        Imports ``faster_whisper`` lazily so this adapter module can be
        imported in tests / lightweight containers without the optional
        dep installed.
        """
        try:
            faster_whisper = import_module("faster_whisper")
        except ImportError as exc:
            raise STTError(
                "faster-whisper is not installed; install it via "
                "`pip install faster-whisper` (the meet-worker image "
                "ships it pre-installed)"
            ) from exc
        try:
            model_cls = faster_whisper.WhisperModel
        except AttributeError as exc:
            raise STTError(
                "faster-whisper module is missing WhisperModel — incompatible version?"
            ) from exc
        try:
            model = model_cls(
                self._model_size,
                device=self._device,
                compute_type=self._compute_type,
                download_root=self._model_dir,
            )
        except Exception as exc:
            raise STTError(
                f"failed to load faster-whisper model {self._model_size!r} "
                f"from {self._model_dir!r}: {exc}"
            ) from exc
        return _cast_to_model_protocol(model)

    def _run_transcribe(
        self,
        model: _WhisperModel,
        waveform: Any,
    ) -> tuple[Any, _TranscriptionInfo]:
        """Run the blocking transcribe call; overridable in tests."""
        return model.transcribe(
            waveform,
            language=self._language,
            beam_size=self._beam_size,
            vad_filter=self._vad_filter,
            no_speech_threshold=self._no_speech_threshold,
            condition_on_previous_text=self._condition_on_previous_text,
        )


def _pcm16_bytes_to_float32(pcm: bytes) -> Any:
    """Convert 16-bit signed-LE PCM bytes into a float32 waveform in [-1, 1].

    Returns a numpy ``ndarray`` when numpy is importable (faster-whisper
    requires numpy at runtime, so this is the normal production path).
    Falls back to an ``array.array("f")`` when numpy is absent, so the
    module remains importable in lightweight test environments that use
    fake models and never call into the real library.
    """
    samples = array.array("h")
    samples.frombytes(pcm)
    try:
        np = import_module("numpy")
    except ImportError:
        return array.array("f", [s / 32768.0 for s in samples])
    arr = np.asarray(samples, dtype=np.float32)
    return arr / 32768.0


def _coerce_no_speech_prob(value: Any) -> float | None:
    """Best-effort parse of a Whisper segment's ``no_speech_prob`` field.

    Returns ``None`` when the upstream field is missing or unparseable,
    so the caller defaults to keeping the segment (fail-open). When the
    field is parseable but out of the [0, 1] range we still surface it
    as-is — the silence filter uses ``>`` comparison so any value above
    1.0 (shouldn't happen, but defensive) still drops the segment.
    """
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


def _logprob_to_confidence(logprob: float | None) -> float | None:
    """Map Whisper's segment-level average log-probability to a [0, 1] proxy.

    ``avg_logprob`` is the natural-log probability averaged over the
    segment's tokens; ``exp`` returns the geometric-mean per-token
    probability, a reasonable confidence proxy. Returns ``None`` when
    the upstream field is missing or unparseable.
    """
    if logprob is None:
        return None
    try:
        value = float(logprob)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    confidence = math.exp(value)
    return max(0.0, min(1.0, confidence))


def _cast_to_model_protocol(model: Any) -> _WhisperModel:
    """Narrow the dynamic faster-whisper model to the adapter's protocol."""
    return model  # type: ignore[no-any-return]


def register(*, replace: bool = False) -> None:
    """Register :class:`FasterWhisperSTT` under ``(ProviderKind.STT, "faster-whisper")``.

    Safe to call from :mod:`app.providers` import even when
    ``faster-whisper`` is not installed — the library is only imported
    lazily inside :meth:`FasterWhisperSTT._load_model`. Misconfigured
    deployments fail loudly when the model is actually needed, not at
    package import.
    """
    get_registry().register(
        ProviderKind.STT, PROVIDER_NAME, FasterWhisperSTT, replace=replace
    )


__all__ = [
    "ALLOWED_MODEL_SIZES",
    "DEFAULT_BEAM_SIZE",
    "DEFAULT_COMPUTE_TYPE",
    "DEFAULT_CONDITION_ON_PREVIOUS_TEXT",
    "DEFAULT_DEVICE",
    "DEFAULT_MODEL_DIR",
    "DEFAULT_MODEL_SIZE",
    "DEFAULT_NO_SPEECH_THRESHOLD",
    "DEFAULT_VAD_FILTER",
    "FasterWhisperSTT",
    "PROVIDER_NAME",
    "register",
]
