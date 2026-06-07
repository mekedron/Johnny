"""NVIDIA Parakeet local speech-to-text adapter.

Three runtimes are supported, selected via the ``runtime`` option:

* ``in-container`` (default) — NVIDIA NeMo toolkit imported into the api
  process. Runs PyTorch on CPU inside the api container; warm test
  latency ~500–900 ms per 5 s of audio after the process-wide model
  cache kicks in. Model weights ship at runtime via the Parakeet
  provider card's Install button.
* ``mlx-sidecar`` — Apple MLX (Metal) Parakeet sidecar process on the
  macOS host. The adapter POSTs PCM to
  ``http://host.docker.internal:8765`` by default; the sidecar lives
  at ``sidecars/parakeet-mlx/``. ~2-3× faster than the in-container
  CPU path on M-series Macs.
* ``coreml-sidecar`` — Swift FluidAudio sidecar wrapping the same
  CoreML + ANE stack VoiceInk uses. Default URL
  ``http://host.docker.internal:8766``. Source at
  ``sidecars/parakeet-coreml/``. Matches VoiceInk's speed
  (~150 ms / 5 s of audio on the Apple Neural Engine).

Wire protocol for both sidecars: ``POST /transcribe`` with the raw
16 kHz mono S16LE PCM as the request body; response JSON
``{"text": "...", "confidence": 0.95}``. ``GET /health`` reports
``{"ready": true, "model_id": "..."}`` once the sidecar has loaded.

In-container details (only relevant when ``runtime == 'in-container'``):
weights live at ``/var/lib/johnny/parakeet-models`` (bind-mounted from
``~/.johnny/parakeet-models`` on the host); ``HF_HOME`` /
``NEMO_CACHE_DIR`` are pointed at that directory before
``ASRModel.from_pretrained`` runs. The loaded model is cached at
module scope keyed by
``(model_id, model_dir, device, beam_size, language)`` so subsequent
``/stt_test`` calls reuse the in-memory weights instead of paying the
multi-second from-pretrained cost on every click. ``close()`` releases
the per-instance reference but **does not** evict the process cache —
call :meth:`ParakeetSTT.evict_process_cache` explicitly for that.

The adapter expects 16 kHz mono S16LE PCM frames in ``audio_iter`` —
the format produced by the meet-worker audio bridge. PCM bytes are
concatenated into a single utterance buffer and passed to the runtime
in one call. The pipeline (``VoicePipeline._utterances()``) segments
audio into VAD-bounded chunks before handing them to STT, so the
adapter treats each ``transcribe_stream`` invocation as one complete
utterance. A v1 batch implementation that emits one final
:class:`TranscriptEvent` per utterance ships here; Johnny-stt.3 will
wire up Parakeet's Cache-Aware Streaming inference so partial deltas
flow to the live chat surface.

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
import threading
import time
from collections.abc import AsyncIterator
from importlib import import_module
from typing import Any, Protocol, runtime_checkable

import httpx

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
# Surface the structured ``parakeet.load:`` / ``parakeet.transcribe:`` lines in
# ``docker logs`` so a regression to per-request loading shows up immediately.
# Without this, the api process's root logger defaults to WARNING and our
# timing breadcrumbs get dropped — the user has nowhere to inspect them.
# Attach a stderr handler only if the logger chain has none of our own;
# don't shadow the project's logging setup when one is added later.
logger.setLevel(logging.INFO)
if not any(getattr(h, "_johnny_parakeet", False) for h in logger.handlers):
    _h = logging.StreamHandler()
    _h.setLevel(logging.INFO)
    _h.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    _h._johnny_parakeet = True  # type: ignore[attr-defined]
    logger.addHandler(_h)
    # Don't double-emit if the root logger picks up a handler later.
    logger.propagate = False

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

# Runtime selector. The in-container path is the historical behaviour and
# remains the default so unconfigured installs keep working. The two sidecar
# runtimes talk HTTP to a separate process running natively on the macOS
# host — that is the only way to reach Metal (MLX) or the Apple Neural
# Engine (CoreML via FluidAudio) from inside our arm64 Linux container.
RUNTIME_IN_CONTAINER = "in-container"
RUNTIME_MLX_SIDECAR = "mlx-sidecar"
RUNTIME_COREML_SIDECAR = "coreml-sidecar"
DEFAULT_RUNTIME = RUNTIME_IN_CONTAINER
ALLOWED_RUNTIMES = frozenset(
    {RUNTIME_IN_CONTAINER, RUNTIME_MLX_SIDECAR, RUNTIME_COREML_SIDECAR}
)
# Default per-runtime URLs. ``host.docker.internal`` resolves to the host's
# loopback interface from inside Docker Desktop on macOS — no extra wiring
# needed. Ports 8765 / 8766 are chosen so both sidecars can run side-by-side
# without a collision.
SIDECAR_DEFAULT_URLS: dict[str, str] = {
    RUNTIME_MLX_SIDECAR: "http://host.docker.internal:8765",
    RUNTIME_COREML_SIDECAR: "http://host.docker.internal:8766",
}
DEFAULT_SIDECAR_URL = SIDECAR_DEFAULT_URLS[RUNTIME_MLX_SIDECAR]
# 60 s is generous: a warm sidecar transcribes 5 s of audio in
# ~150–500 ms (ANE / MLX respectively), and the cold first-call may
# spend a few seconds loading the model. Anything past 60 s is a hung
# sidecar and the user should see a clear error.
SIDECAR_HTTP_TIMEOUT_SECONDS = 60.0


# --- Process-wide model cache --------------------------------------------
#
# Cache key includes every config knob that influences the loaded weights or
# how they decode. ``language`` is currently dead (NeMo's ``transcribe``
# call doesn't receive it yet), but Johnny-stt.3 will plumb it through —
# include it now so a future commit can't silently serve the wrong-language
# model from cache.
CacheKey = tuple[str, str, str, int, str | None]

# Single-slot cache: holding two 0.6 B models would peak at ~1.2 GB RSS.
# On a config-key change we evict the previous entry; in-flight transcribes
# still hold a reference to the old model via ``self._model``, so it stays
# alive until GC'd. The peak memory beat is bounded by the
# ``_GLOBAL_LOAD_GATE`` below.
_LAST: tuple[CacheKey, "_ASRModel"] | None = None
# Per-key asyncio.Lock so concurrent first-load requests for the same key
# coalesce into one load. Cleared by :func:`_evict_process_cache`.
_LOAD_LOCKS: dict[CacheKey, asyncio.Lock] = {}
# Guards the ``_LAST`` slot and ``_LOAD_LOCKS`` dict. A sync lock is
# sufficient because every operation it protects is O(1).
_CACHE_LOCK = threading.Lock()
# Serialise the heavy ``_load_model`` call across keys so a config-key
# change doesn't briefly hold two 0.6 B models in RAM at once. NeMo's
# model load is single-threaded inside torch anyway, so no real
# parallelism is lost.
_GLOBAL_LOAD_GATE = asyncio.Lock()


def _get_or_make_lock(key: CacheKey) -> asyncio.Lock:
    """Return the asyncio.Lock for ``key`` from :data:`_LOAD_LOCKS`.

    Creates a fresh lock on first sight. Guarded by :data:`_CACHE_LOCK`
    so two concurrent coroutines don't race on the ``setdefault``.
    The lock map only grows with distinct config-keys (handful of provider
    rows in practice), so leaks here are negligible.
    """
    with _CACHE_LOCK:
        lock = _LOAD_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _LOAD_LOCKS[key] = lock
        return lock


def _evict_process_cache() -> None:
    """Drop the cached model and every per-key load lock.

    Public API is :meth:`ParakeetSTT.evict_process_cache`; this is the
    module-level helper it dispatches to so tests / admin restart paths
    can wipe state without touching the class.
    """
    global _LAST
    with _CACHE_LOCK:
        _LAST = None
        _LOAD_LOCKS.clear()


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

    * ``runtime`` — ``in-container`` (default, NeMo PyTorch inside the
      api container), ``mlx-sidecar`` (Apple MLX on the macOS host), or
      ``coreml-sidecar`` (Swift FluidAudio on the host, CoreML + ANE).
      The sidecar runtimes require running the matching sidecar process
      via ``./scripts/start-parakeet-sidecar.sh``.
    * ``sidecar_url`` — base URL of the sidecar process. Used only when
      ``runtime`` is a sidecar option; default depends on the runtime
      (port 8765 for MLX, 8766 for CoreML).
    * ``model_id`` — HuggingFace repo id of the Parakeet checkpoint
      (default ``nvidia/parakeet-tdt-0.6b-v3``). Used only by the
      in-container runtime; sidecars hardcode the v3 model.
    * ``model_dir`` — directory holding cached weights. In-container
      only. Falls back to ``JOHNNY_PARAKEET_MODEL_DIR``, then
      ``/var/lib/johnny/parakeet-models`` (bind-mounted from the host).
    * ``device`` — ``cpu`` (default), ``cuda``, ``mps``, ``auto``.
      In-container only; MPS / CUDA do not work inside the Linux api
      container (the ``.to(device)`` call swallows the RuntimeError and
      logs a warning, then falls back to CPU).
    * ``beam_size`` — transducer beam search width (default 1 / greedy).
      In-container only.
    * ``language`` — force a language code (default ``en``). Sent to the
      sidecar as an ``X-Language`` header when non-empty.

    The adapter is **batch-oriented** in v1: it buffers the whole
    utterance from ``audio_iter`` and runs a single transcribe call,
    emitting one final :class:`TranscriptEvent` per non-empty
    hypothesis. Johnny-stt.3 will wire up streaming inference.
    """

    def __init__(self, config: ProviderConfig) -> None:
        if config.kind is not ProviderKind.STT:
            raise ValueError(
                f"ParakeetSTT requires ProviderKind.STT; got {config.kind.value}"
            )
        opts = config.options
        runtime = str(opts.get("runtime") or DEFAULT_RUNTIME)
        if runtime not in ALLOWED_RUNTIMES:
            raise ValueError(
                f"runtime {runtime!r} must be one of {sorted(ALLOWED_RUNTIMES)}"
            )
        self._runtime = runtime
        # Sidecar URL: explicit value wins; otherwise pick the per-runtime
        # default. In-container runtime keeps an empty string — it's
        # ignored either way.
        sidecar_url_opt = opts.get("sidecar_url")
        if sidecar_url_opt:
            self._sidecar_url = str(sidecar_url_opt).rstrip("/")
        elif runtime in SIDECAR_DEFAULT_URLS:
            self._sidecar_url = SIDECAR_DEFAULT_URLS[runtime]
        else:
            self._sidecar_url = ""

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
        # Per-instance reference — points at the cached process-wide model
        # once :meth:`_ensure_model` resolves it. ``close()`` drops this
        # reference but does not evict the cache.
        self._model: _ASRModel | None = None
        # Lazy httpx client for the sidecar runtimes. Reused across calls
        # on the same instance so the TCP connection stays warm.
        self._sidecar_client: httpx.AsyncClient | None = None

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
                    name="runtime",
                    label="Runtime",
                    type=FieldType.SELECT,
                    default=DEFAULT_RUNTIME,
                    options=(
                        FieldOption(
                            value=RUNTIME_IN_CONTAINER,
                            label="In-container (PyTorch / NeMo, CPU)",
                        ),
                        FieldOption(
                            value=RUNTIME_MLX_SIDECAR,
                            label="MLX sidecar (Apple Silicon, Metal)",
                        ),
                        FieldOption(
                            value=RUNTIME_COREML_SIDECAR,
                            label="CoreML sidecar (Apple Neural Engine)",
                        ),
                    ),
                    help_text=(
                        "Which Parakeet runtime to use. In-container is "
                        "the simplest (no sidecar to manage) but slowest. "
                        "MLX uses Apple's Metal GPU via the parakeet-mlx "
                        "sidecar (~2-3× faster). CoreML uses the Neural "
                        "Engine via the FluidAudio Swift sidecar (matches "
                        "VoiceInk's speed). Sidecars must be started "
                        "separately — see ./scripts/start-parakeet-sidecar.sh."
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="sidecar_url",
                    label="Sidecar URL",
                    type=FieldType.URL,
                    default=DEFAULT_SIDECAR_URL,
                    placeholder=DEFAULT_SIDECAR_URL,
                    help_text=(
                        "Base URL of the running sidecar. Default port "
                        "is 8765 for MLX, 8766 for CoreML. Use "
                        "http://host.docker.internal:PORT from inside "
                        "the api container; the sidecar runs natively "
                        "on the macOS host. Ignored when runtime is "
                        "in-container."
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="model_id",
                    label="Model",
                    type=FieldType.SELECT,
                    default=DEFAULT_MODEL_ID,
                    help_text=(
                        "Pick a Parakeet checkpoint. The 0.6 B TDT v3 model "
                        "is the recommended default for new deployments. "
                        "Sidecar runtimes always use v3 regardless of this "
                        "setting."
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
                        "~600 MB download survives container rebuilds. "
                        "In-container runtime only."
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
                    help_text=(
                        "In-container runtime only. MPS/CUDA require the "
                        "api to run outside Docker — inside the container "
                        "they silently fall back to CPU. Pick a sidecar "
                        "runtime above for real Apple Silicon acceleration."
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
                        "(fastest). Higher values trade latency for accuracy. "
                        "In-container runtime only."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
            ),
            tips=(
                ProviderTip(
                    topic="Sidecars beat the in-container path on Apple Silicon",
                    body=(
                        "Inside the arm64 Linux api container, PyTorch "
                        "has no MPS / CoreML / ANE access — the model "
                        "runs on CPU regardless of what you pick. For "
                        "real Apple Silicon acceleration, pick the MLX "
                        "or CoreML sidecar runtime and start it with "
                        "./scripts/start-parakeet-sidecar.sh."
                    ),
                ),
                ProviderTip(
                    topic="Process-wide model cache",
                    body=(
                        "The in-container runtime caches the loaded "
                        "model at process scope keyed by "
                        "(model_id, model_dir, device, beam_size, "
                        "language). Only the first /stt_test pays the "
                        "multi-second ASRModel.from_pretrained cost; "
                        "subsequent calls reuse the loaded weights."
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

    @property
    def runtime(self) -> str:
        return self._runtime

    @property
    def sidecar_url(self) -> str:
        return self._sidecar_url

    @property
    def _cache_key(self) -> CacheKey:
        return (
            self._model_id,
            self._model_dir,
            self._device,
            self._beam_size,
            self._language,
        )

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

        Dispatches to the in-container NeMo path or one of the two
        sidecar HTTP paths based on ``self._runtime``.
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
        pcm_bytes = bytes(buffer)
        audio_ms = int(len(pcm_bytes) * 1000 / (PCM_SAMPLE_RATE_HZ * PCM_SAMPLE_WIDTH_BYTES))

        if self._runtime == RUNTIME_IN_CONTAINER:
            async for event in self._transcribe_in_container(pcm_bytes, audio_ms):
                yield event
        else:
            async for event in self._transcribe_via_sidecar(pcm_bytes, audio_ms):
                yield event

    async def _transcribe_in_container(
        self,
        pcm_bytes: bytes,
        audio_ms: int,
    ) -> AsyncIterator[TranscriptEvent]:
        """In-container NeMo path. Caches the loaded model at process scope."""
        waveform = _pcm16_bytes_to_float32(pcm_bytes)
        try:
            model = await self._ensure_model()
            start = time.perf_counter()
            hypotheses = await asyncio.to_thread(
                self._run_transcribe, model, waveform
            )
            forward_ms = int((time.perf_counter() - start) * 1000)
        except STTError:
            raise
        except Exception as exc:
            raise STTError(f"parakeet transcribe failed: {exc}") from exc

        logger.info(
            "parakeet.transcribe: runtime=%s audio_ms=%d forward_ms=%d",
            self._runtime,
            audio_ms,
            forward_ms,
        )

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
                len(pcm_bytes),
            )

    async def _transcribe_via_sidecar(
        self,
        pcm_bytes: bytes,
        audio_ms: int,
    ) -> AsyncIterator[TranscriptEvent]:
        """Sidecar HTTP path. Posts PCM bytes and yields one final event."""
        if not self._sidecar_url:
            raise STTError(
                f"parakeet runtime={self._runtime} requires sidecar_url"
            )
        client = self._sidecar_client_or_open()
        url = f"{self._sidecar_url}/transcribe"
        headers = {
            "Content-Type": "application/octet-stream",
            "X-Audio-Sample-Rate": str(PCM_SAMPLE_RATE_HZ),
            "X-Audio-Channels": "1",
            "X-Audio-Format": "pcm-s16le",
        }
        if self._language:
            headers["X-Language"] = self._language

        start = time.perf_counter()
        try:
            response = await client.post(
                url,
                content=pcm_bytes,
                headers=headers,
                timeout=SIDECAR_HTTP_TIMEOUT_SECONDS,
            )
        except httpx.RequestError as exc:
            raise STTError(
                f"parakeet sidecar {self._runtime} at {self._sidecar_url} "
                f"unreachable: {exc}. Start it with "
                "./scripts/start-parakeet-sidecar.sh"
            ) from exc
        forward_ms = int((time.perf_counter() - start) * 1000)

        if response.status_code != 200:
            raise STTError(
                f"parakeet sidecar {self._runtime} returned "
                f"HTTP {response.status_code}: {response.text[:200]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise STTError(
                f"parakeet sidecar {self._runtime} returned non-JSON body"
            ) from exc

        logger.info(
            "parakeet.transcribe: runtime=%s audio_ms=%d forward_ms=%d",
            self._runtime,
            audio_ms,
            forward_ms,
        )

        text = str(payload.get("text") or "").strip()
        if not text:
            logger.debug(
                "parakeet sidecar %s produced no transcript for %d ms utterance",
                self._runtime,
                audio_ms,
            )
            return
        confidence_raw = payload.get("confidence")
        confidence: float | None
        if confidence_raw is None:
            confidence = None
        else:
            try:
                confidence = max(0.0, min(1.0, float(confidence_raw)))
            except (TypeError, ValueError):
                confidence = None
        yield TranscriptEvent(
            text=text,
            is_final=True,
            timestamp_ms=0,
            confidence=confidence,
        )

    def _sidecar_client_or_open(self) -> httpx.AsyncClient:
        """Return the per-instance httpx client, creating it on first use."""
        if self._sidecar_client is None:
            self._sidecar_client = httpx.AsyncClient(
                timeout=SIDECAR_HTTP_TIMEOUT_SECONDS,
            )
        return self._sidecar_client

    async def close(self) -> None:
        """Release this instance's references.

        Drops the per-instance model handle and closes the per-instance
        httpx client. **Does NOT** evict the process-wide model cache —
        the next ``ParakeetSTT(...)`` for the same config-key will reuse
        the cached weights instead of paying the from-pretrained cost.
        Use :meth:`evict_process_cache` for a hard reset.
        """
        self._model = None
        if self._sidecar_client is not None:
            try:
                await self._sidecar_client.aclose()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
            self._sidecar_client = None

    @classmethod
    def evict_process_cache(cls) -> None:
        """Wipe the process-wide model cache.

        Used by tests and by a future "Reload model" admin action. The
        next transcribe in the in-container runtime will re-run
        ``ASRModel.from_pretrained``. Concurrent transcribes that already
        hold a reference to the old model via ``self._model`` finish on
        the old weights — only fresh ``_ensure_model`` calls see the new
        cache state.
        """
        _evict_process_cache()

    # --- Hooks (overridable in tests) -------------------------------------

    async def _ensure_model(self) -> _ASRModel:
        """Return a loaded ``ASRModel``, using the module-level cache.

        Tests override :meth:`_load_model` on a subclass so the cache
        gets populated with a fake; the in-test :func:`evict_process_cache`
        autouse fixture (see ``tests/providers/test_parakeet_stt.py``)
        wipes the slot between tests so two unrelated tests don't see
        each other's fakes.
        """
        global _LAST
        key = self._cache_key

        # Fast-path: cache hit, no lock needed.
        with _CACHE_LOCK:
            cached = _LAST
        if cached is not None and cached[0] == key:
            self._model = cached[1]
            return self._model

        # Slow-path: per-key load lock coalesces concurrent first-loads
        # for the same key; the global gate ensures only one load runs at
        # a time across keys so we don't briefly hold two 0.6 B models.
        lock = _get_or_make_lock(key)
        async with lock:
            # Re-check now that we hold the lock.
            with _CACHE_LOCK:
                cached = _LAST
            if cached is not None and cached[0] == key:
                self._model = cached[1]
                return self._model
            async with _GLOBAL_LOAD_GATE:
                model = await asyncio.to_thread(self._load_model)
            with _CACHE_LOCK:
                _LAST = (key, model)
            self._model = model
            return self._model

    def _load_model(self) -> _ASRModel:
        """Load and return an :class:`ASRModel` instance.

        Imports the NeMo toolkit lazily so this adapter module can be
        imported in tests / lightweight containers without the optional
        dep installed. Points ``HF_HOME`` at the configured model
        directory before the ``from_pretrained`` call so HuggingFace
        downloads land in the bind-mounted host path rather than
        ``~/.cache``.

        Emits one ``parakeet.load:`` INFO log per successful load with
        the time spent on each segment so future regressions to
        per-request loading show up immediately in ``docker logs``.
        """
        os.environ.setdefault("HF_HOME", self._model_dir)
        os.environ.setdefault("NEMO_CACHE_DIR", self._model_dir)
        load_start = time.perf_counter()
        import_start = load_start
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
        nemo_import_ms = int((time.perf_counter() - import_start) * 1000)
        try:
            asr_model_cls = nemo_asr.models.ASRModel
        except AttributeError as exc:
            raise STTError(
                "nemo.collections.asr.models.ASRModel missing — incompatible NeMo version?"
            ) from exc
        from_pretrained_start = time.perf_counter()
        try:
            model = asr_model_cls.from_pretrained(self._model_id)
        except Exception as exc:
            raise STTError(
                f"failed to load Parakeet model {self._model_id!r} "
                f"from cache {self._model_dir!r}: {exc}"
            ) from exc
        from_pretrained_ms = int((time.perf_counter() - from_pretrained_start) * 1000)
        device_start = time.perf_counter()
        _maybe_move_to_device(model, self._device)
        _maybe_set_beam_size(model, self._beam_size)
        device_move_ms = int((time.perf_counter() - device_start) * 1000)
        total_ms = int((time.perf_counter() - load_start) * 1000)
        logger.info(
            "parakeet.load: model_id=%s device=%s beam=%d "
            "nemo_import_ms=%d from_pretrained_ms=%d device_move_ms=%d total_ms=%d",
            self._model_id,
            self._device,
            self._beam_size,
            nemo_import_ms,
            from_pretrained_ms,
            device_move_ms,
            total_ms,
        )
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
    "ALLOWED_RUNTIMES",
    "DEFAULT_BEAM_SIZE",
    "DEFAULT_DEVICE",
    "DEFAULT_LANGUAGE",
    "DEFAULT_MODEL_DIR",
    "DEFAULT_MODEL_ID",
    "DEFAULT_RUNTIME",
    "DEFAULT_SIDECAR_URL",
    "PROVIDER_NAME",
    "RUNTIME_COREML_SIDECAR",
    "RUNTIME_IN_CONTAINER",
    "RUNTIME_MLX_SIDECAR",
    "SIDECAR_DEFAULT_URLS",
    "SIDECAR_HTTP_TIMEOUT_SECONDS",
    "ParakeetSTT",
    "register",
]
