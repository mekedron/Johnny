"""KittenTTS local text-to-speech adapter.

Wraps the KittenTTS open-weight model (https://github.com/KittenML/KittenTTS)
so the voice pipeline can produce speech entirely on-device. KittenTTS is a
tiny (<25 M-parameter) ONNX model that runs comfortably on CPU — no GPU, no
torch — a complementary option to Piper's larger voices and Kokoro's 82 M
checkpoint when the operator wants the smallest possible memory footprint or a
different voice tone. It synthesises mono float audio at its native 24 000 Hz;
this adapter converts to 16-bit signed little-endian PCM and resamples to the
canonical 16 kHz mono format used by the meet-worker audio bridge.

Two runtimes are supported, selected via the ``runtime`` option (Settings →
Providers → KittenTTS → Runtime), mirroring the Kokoro split:

* ``in-container`` (default) — the ``kittentts`` Python package imported lazily
  into the api process. The loaded model (which bundles every voice) is cached
  at module scope keyed by ``model_id`` so only the first synth per model pays
  the one-off load; warm calls return audio in well under 200 ms. The
  ``kittentts`` + onnxruntime deps are imported lazily so this module stays
  importable where the library is absent.
* ``http-sidecar`` — KittenTTS running in a generic Python sidecar on the host
  (``sidecars/kitten-tts/``, default ``http://host.docker.internal:8771``).
  Use it to isolate synthesis in its own process / host, or to keep the api
  image free of the onnxruntime dependency.

There is **no** ``persistent-subprocess`` runtime: KittenTTS ships no CLI to
drive a long-running child process (the ``in-container`` runtime already keeps
the model warm in-process), so — per the epic's scoping instruction — that
option is folded into ``in-container`` rather than invented. KittenTTS also has
no Apple-Silicon (MLX / CoreML) acceleration path as of the 0.8 release, so
there is no MLX sidecar like Kokoro's; the model is CPU-only everywhere.

Wire protocol for the sidecar (identical to ``sidecars/kokoro-http`` /
``sidecars/piper-http`` minus the ``lang_code`` field — KittenTTS is
English-only):

    POST /synthesize
        Body: JSON {"text": ..., "voice": ..., "speed": ...}
        Response: 200 application/octet-stream
            Body: raw S16LE PCM at the model's native rate (24 000 Hz)
            Header: X-Sample-Rate: 24000
    GET /health -> {"ready": true, "voice": "...", "backend": "kittentts"}

Start the sidecar with ``./scripts/start-kitten-sidecar.sh start``.

**License**: KittenTTS ships under Apache-2.0 — usable in commercial products.
See https://github.com/KittenML/KittenTTS.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import logging
import os
import threading
import time
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any

import httpx

from app.providers._pcm import resample_pcm16
from app.providers.base import (
    PCM_CHANNELS,
    PCM_SAMPLE_RATE_HZ,
    PCM_SAMPLE_WIDTH_BYTES,
    ProviderConfig,
    ProviderKind,
    TTSError,
    TTSProvider,
    VoiceMeta,
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
# Surface the structured ``kitten.synth:`` / ``kitten.load:`` / ``kitten.sidecar:``
# lines in ``docker logs api`` so a regression to cold per-call loading shows up
# immediately. Mirrors the kokoro_tts / parakeet_stt handler setup — without
# this the root logger defaults to WARNING and our timing breadcrumbs get
# dropped. Attach a stderr handler only if the logger chain has none of our own
# so we don't shadow the project's logging setup when one is added later.
logger.setLevel(logging.INFO)
if not any(getattr(h, "_johnny_kitten", False) for h in logger.handlers):
    _h = logging.StreamHandler()
    _h.setLevel(logging.INFO)
    _h.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    _h._johnny_kitten = True  # type: ignore[attr-defined]
    logger.addHandler(_h)
    logger.propagate = False

PROVIDER_NAME = "kittentts"
# The current KittenTTS release (0.8.1 wheel) ships the "mini" 0.8 checkpoint
# with the named voice set below. An operator may point ``model_id`` at a nano
# build (e.g. ``KittenML/kitten-tts-nano-0.2``, whose voices are the
# ``expr-voice-*-{m,f}`` ids) via the advanced field.
DEFAULT_MODEL_ID = "KittenML/kitten-tts-mini-0.8"
DEFAULT_MODEL_DIR = "/var/lib/johnny/kitten-models"
DEFAULT_VOICE_ID = "Bella"
DEFAULT_SPEED = 1.0
DEFAULT_CHUNK_BYTES = 4_096
# KittenTTS always emits 24 kHz mono float audio regardless of voice; the
# adapter converts to S16LE and resamples to the canonical 16 kHz bridge format.
KITTEN_NATIVE_SAMPLE_RATE_HZ = 24_000

# Canonical KittenTTS v0.8 voice catalog: (value, label, gender). Drives the
# voice_id SELECT + the unified picker (Johnny-1ge.8). The eight friendly names
# map onto the model's internal ``expr-voice-2..5-{f,m}`` ids (four female, four
# male) and are interchangeable with them; we expose the friendly names the
# current README documents. Order matches ``model.available_voices``.
KITTEN_VOICE_CATALOG: tuple[tuple[str, str, str], ...] = (
    ("Bella", "Bella — English ♀", "female"),
    ("Jasper", "Jasper — English ♂", "male"),
    ("Luna", "Luna — English ♀", "female"),
    ("Bruno", "Bruno — English ♂", "male"),
    ("Rosie", "Rosie — English ♀", "female"),
    ("Hugo", "Hugo — English ♂", "male"),
    ("Kiki", "Kiki — English ♀", "female"),
    ("Leo", "Leo — English ♂", "male"),
)
KITTEN_LANGUAGE = "English"

# Runtime selector. ``in-container`` is the default so unconfigured installs
# behave predictably (and need no host sidecar). The sidecar runtime talks HTTP
# to a process running natively on the host — useful to keep the onnxruntime
# dependency out of the api image, or to isolate synthesis on another machine.
RUNTIME_IN_CONTAINER = "in-container"
RUNTIME_HTTP_SIDECAR = "http-sidecar"
DEFAULT_RUNTIME = RUNTIME_IN_CONTAINER
ALLOWED_RUNTIMES = frozenset({RUNTIME_IN_CONTAINER, RUNTIME_HTTP_SIDECAR})
# Default per-runtime sidecar URLs. ``host.docker.internal`` resolves to the
# host loopback from inside Docker Desktop on macOS. Port 8771 is chosen so the
# KittenTTS sidecar runs alongside the Parakeet (8765 / 8766), Kokoro
# (8772 / 8773) and Piper-http (8775) sidecars without a collision.
SIDECAR_DEFAULT_URLS: dict[str, str] = {
    RUNTIME_HTTP_SIDECAR: "http://host.docker.internal:8771",
}
DEFAULT_SIDECAR_URL = SIDECAR_DEFAULT_URLS[RUNTIME_HTTP_SIDECAR]
# 60 s is generous: a warm sidecar synthesises the sample phrase in well under
# a second; the cold first call may spend a few seconds loading the model. Past
# 60 s is a hung sidecar and the user should see a clear error.
SIDECAR_HTTP_TIMEOUT_SECONDS = 60.0


# --- Persistent in-process model cache (runtime=in-container) --------------
#
# KittenTTS holds the whole (tiny) model and every voice in a single object, so
# distinct voices reuse the same loaded model (the voice is applied per call).
# We therefore key the cache by ``model_id`` — NOT the bead's literal
# ``(voice_id, sample_rate)``, which would reload the whole model for every
# voice (pointless: all voices share one checkpoint and one 24 kHz rate). Same
# lock idiom as ``kokoro_tts._PIPELINES``: a sync lock guards the dicts, a
# per-key asyncio lock coalesces first-loads, and a global gate serialises the
# heavy load across keys.


@dataclass
class _ModelHandle:
    """A cached, loaded KittenTTS model plus the lock that serialises use.

    ``model`` is the loaded library object; its ONNX session is driven under
    ``lock`` so two coroutines don't run inference on it at once.
    ``requests_served`` and ``loaded_at`` feed the ``kitten.load:`` breadcrumbs.
    """

    model: Any
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    loaded_at: float = field(default_factory=time.perf_counter)
    requests_served: int = 0


_MODELS: dict[str, _ModelHandle] = {}
_LOAD_LOCKS: dict[str, asyncio.Lock] = {}
_CACHE_LOCK = threading.Lock()
_GLOBAL_LOAD_GATE = asyncio.Lock()


def _get_or_make_lock(key: str) -> asyncio.Lock:
    """Return the load-lock for ``key``, creating it on first sight.

    Guarded by :data:`_CACHE_LOCK` so two coroutines don't race on the
    ``setdefault``. The map only grows with distinct model ids (a handful in
    practice), so the leak is negligible.
    """
    with _CACHE_LOCK:
        lock = _LOAD_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _LOAD_LOCKS[key] = lock
        return lock


def _evict_process_cache() -> None:
    """Drop every cached model and per-key load lock.

    Public API is :meth:`KittenTTS.evict_process_cache`; this is the
    module-level helper it dispatches to so tests / a future admin "reload
    voices" action can wipe state without touching the class.
    """
    with _CACHE_LOCK:
        _MODELS.clear()
        _LOAD_LOCKS.clear()


def _audio_to_pcm16(audio: Any) -> bytes:
    """Convert a KittenTTS audio segment to 16-bit signed-LE PCM bytes.

    KittenTTS returns float audio in roughly ``[-1, 1]`` as a numpy array. We
    scale to int16. Falls back to a pure-Python conversion when numpy is absent
    so the helper (and the module) stay importable in lightweight test
    environments that feed plain float lists.
    """
    if audio is None:
        return b""
    # Some libraries hand back torch-ish tensors; move to numpy when possible
    # (no hard torch dependency).
    detach = getattr(audio, "detach", None)
    if callable(detach):
        audio = detach()
    cpu = getattr(audio, "cpu", None)
    if callable(cpu):
        audio = cpu()
    to_numpy = getattr(audio, "numpy", None)
    if callable(to_numpy):
        audio = to_numpy()
    try:
        np = import_module("numpy")
    except ImportError:
        # Clip to [-1, 1] BEFORE scaling so this matches the numpy path
        # byte-for-byte (numpy clips then scales); otherwise extreme-negative
        # samples would diverge (-32767 vs -32768).
        out = array.array("h")
        for sample in audio:
            value = float(sample)
            if value > 1.0:
                value = 1.0
            elif value < -1.0:
                value = -1.0
            out.append(int(value * 32767.0))
        return out.tobytes()
    arr = np.asarray(audio, dtype=np.float32).reshape(-1)
    clipped = np.clip(arr, -1.0, 1.0)
    pcm: bytes = (clipped * 32767.0).astype("<i2").tobytes()
    return pcm


def _voice_meta(value: str, label: str, gender: str) -> VoiceMeta:
    """Build a :class:`VoiceMeta` for one KittenTTS catalog entry (Johnny-1ge.8).

    Every KittenTTS voice ships inside the single tiny checkpoint, so
    ``installed`` is always ``True`` and there is no per-voice download size.
    KittenTTS is English-only at 24 kHz.
    """
    return VoiceMeta(
        id=value,
        label=label,
        language=KITTEN_LANGUAGE,
        sample_rate=KITTEN_NATIVE_SAMPLE_RATE_HZ,
        gender=gender,
        installed=True,
    )


class KittenTTS(TTSProvider):
    """Streaming TTS via the local KittenTTS (<25 M) ONNX model.

    Configuration ``options`` (any key may be omitted):

    * ``runtime`` — ``in-container`` (default, ``kittentts`` in the api
      process) or ``http-sidecar`` (KittenTTS in a host sidecar). The sidecar
      runtime requires running the sidecar via
      ``./scripts/start-kitten-sidecar.sh start``.
    * ``sidecar_url`` — base URL of the sidecar. Used only by the sidecar
      runtime; default ``http://host.docker.internal:8771``.
    * ``voice_id`` — KittenTTS voice (``Bella``, ``Jasper``, ``Luna``, …).
      Defaults to ``Bella``.
    * ``model_id`` — HuggingFace repo of the KittenTTS weights (default
      ``KittenML/kitten-tts-mini-0.8``). In-container only; the sidecar uses
      its own configured model.
    * ``model_dir`` — cache directory for downloaded weights. In-container
      only. Falls back to ``JOHNNY_KITTEN_MODEL_DIR`` then
      ``/var/lib/johnny/kitten-models``.
    * ``speed`` — synthesis speed multiplier (0.5–2.0, default 1.0).
    * ``chunk_bytes`` — output streaming chunk size (default 4096). Must be a
      multiple of the 2-byte S16 sample width.
    """

    def __init__(self, config: ProviderConfig) -> None:
        if config.kind is not ProviderKind.TTS:
            raise ValueError(
                f"KittenTTS requires ProviderKind.TTS; got {config.kind.value}"
            )
        opts = config.options
        runtime = str(opts.get("runtime") or DEFAULT_RUNTIME)
        if runtime not in ALLOWED_RUNTIMES:
            raise ValueError(
                f"runtime {runtime!r} must be one of {sorted(ALLOWED_RUNTIMES)}"
            )
        self._runtime = runtime
        # Sidecar URL: explicit value wins; otherwise pick the per-runtime
        # default. The in-container runtime keeps an empty string — ignored.
        sidecar_url_opt = opts.get("sidecar_url")
        if sidecar_url_opt:
            self._sidecar_url = str(sidecar_url_opt).rstrip("/")
        elif runtime in SIDECAR_DEFAULT_URLS:
            self._sidecar_url = SIDECAR_DEFAULT_URLS[runtime]
        else:
            self._sidecar_url = ""
        # Lazy httpx client for the sidecar runtime; reused across calls on the
        # same instance so the TCP connection stays warm.
        self._sidecar_client: httpx.AsyncClient | None = None

        voice_id = opts.get("voice_id")
        # KittenTTS ships its voices with the model, so fall back to the
        # flagship voice when the operator leaves it blank — the Play sample
        # button works out of the box.
        self._default_voice_id = (
            str(voice_id) if voice_id not in (None, "") else DEFAULT_VOICE_ID
        )
        self._model_id = str(opts.get("model_id") or DEFAULT_MODEL_ID)
        self._model_dir = str(
            opts.get("model_dir")
            or os.environ.get("JOHNNY_KITTEN_MODEL_DIR")
            or DEFAULT_MODEL_DIR
        )
        speed_opt = opts.get("speed")
        if speed_opt is None or speed_opt == "":
            speed = DEFAULT_SPEED
        else:
            speed = float(speed_opt)
        if speed <= 0:
            raise ValueError(f"speed must be positive; got {speed}")
        self._speed = speed
        chunk_bytes = int(opts.get("chunk_bytes") or DEFAULT_CHUNK_BYTES)
        if chunk_bytes <= 0:
            raise ValueError(f"chunk_bytes must be positive; got {chunk_bytes}")
        if chunk_bytes % PCM_SAMPLE_WIDTH_BYTES:
            raise ValueError(
                f"chunk_bytes must be a multiple of {PCM_SAMPLE_WIDTH_BYTES}"
            )
        self._chunk_bytes = chunk_bytes

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @classmethod
    def field_schema(cls) -> ProviderSchema:
        return ProviderSchema(
            kind=ProviderKind.TTS,
            provider_name=PROVIDER_NAME,
            display_name="KittenTTS",
            summary=(
                "Local KittenTTS (<25 MB) ONNX TTS. Apache-2.0, CPU-only, "
                "8 English voices, no audio leaves host."
            ),
            signup_url="https://github.com/KittenML/KittenTTS",
            fields=(
                FieldDef(
                    name="runtime",
                    label="Runtime",
                    type=FieldType.SELECT,
                    default=DEFAULT_RUNTIME,
                    options=(
                        FieldOption(
                            value=RUNTIME_IN_CONTAINER,
                            label="In-container (kittentts in the api process)",
                        ),
                        FieldOption(
                            value=RUNTIME_HTTP_SIDECAR,
                            label="HTTP sidecar (KittenTTS on a host process)",
                        ),
                    ),
                    help_text=(
                        "Which KittenTTS runtime to use. In-container runs the "
                        "kittentts library inside the api process and caches "
                        "the warm model — second and later synths return audio "
                        "in well under 200 ms. HTTP sidecar runs the model in a "
                        "separate host process (keeps onnxruntime out of the "
                        "api image, or isolates synthesis). The sidecar must be "
                        "started separately — see "
                        "./scripts/start-kitten-sidecar.sh. KittenTTS is "
                        "CPU-only; there is no MLX/GPU runtime."
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
                        "Base URL of the running sidecar. Default port is 8771. "
                        "Use http://host.docker.internal:PORT from inside the "
                        "api container; the sidecar runs natively on the host. "
                        "Ignored when runtime is in-container."
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="voice_id",
                    label="Voice",
                    type=FieldType.SELECT,
                    required=True,
                    default=DEFAULT_VOICE_ID,
                    voice_catalog=True,
                    options=tuple(
                        FieldOption(value=value, label=label)
                        for value, label, _ in KITTEN_VOICE_CATALOG
                    ),
                    help_text=(
                        "KittenTTS voice. Eight English voices (four female, "
                        "four male) ship inside the model — no separate "
                        "install. The friendly names map onto the model's "
                        "expr-voice-* ids."
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="model_id",
                    label="Model repo",
                    default=DEFAULT_MODEL_ID,
                    help_text=(
                        "HuggingFace repo of the KittenTTS weights. In-container "
                        "runtime only; the sidecar uses its own configured "
                        "model. The voice list above matches "
                        "kitten-tts-mini-0.8; nano builds use expr-voice-* ids."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="model_dir",
                    label="Model cache directory",
                    default=DEFAULT_MODEL_DIR,
                    help_text=(
                        "Where KittenTTS / HuggingFace weights are cached on "
                        "disk. In-container runtime only."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="speed",
                    label="Speed",
                    type=FieldType.NUMBER,
                    default=DEFAULT_SPEED,
                    help_text=(
                        "Synthesis speed multiplier. 1.0 is natural; 0.5–2.0 is "
                        "the useful range. Higher is faster speech, not lower "
                        "latency."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="chunk_bytes",
                    label="Read chunk bytes",
                    type=FieldType.NUMBER,
                    default=DEFAULT_CHUNK_BYTES,
                    help_text=(
                        "How many bytes the adapter waits to accumulate before "
                        "yielding a frame downstream. Smaller = first audio "
                        "leaves the box sooner, at the cost of more syscalls. "
                        "Must be a multiple of 2."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
            ),
            tips=(
                ProviderTip(
                    topic="In-container is warm after the first synth",
                    body=(
                        "The first synth pays the model load (a second or two); "
                        "the model is then cached at process scope keyed by "
                        "model id. Click Play sample twice — the second click "
                        "returns audio in well under 200 ms. The cache is "
                        "shared across provider rows, so a saved KittenTTS "
                        "provider is warm for live meetings too."
                    ),
                ),
                ProviderTip(
                    topic="Tiny and CPU-only — no GPU needed",
                    body=(
                        "KittenTTS is under 25 MB and runs on ONNX Runtime on "
                        "CPU, so the in-container runtime is the simplest fast "
                        "path — no host sidecar required. There is no MLX/Metal "
                        "or CoreML build, so (unlike Kokoro) there is no GPU "
                        "sidecar; the http-sidecar exists only to isolate "
                        "synthesis in its own process or host."
                    ),
                ),
                ProviderTip(
                    topic="Voices ship with the model",
                    body=(
                        "All eight voices live in the single checkpoint, so "
                        "switching voice is instant and needs no install. Four "
                        "female (Bella, Luna, Rosie, Kiki) and four male "
                        "(Jasper, Bruno, Hugo, Leo), English only."
                    ),
                ),
            ),
        )

    @property
    def default_voice_id(self) -> str | None:
        return self._default_voice_id

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def model_dir(self) -> str:
        return self._model_dir

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def chunk_bytes(self) -> int:
        return self._chunk_bytes

    @property
    def native_sample_rate(self) -> int:
        return KITTEN_NATIVE_SAMPLE_RATE_HZ

    @property
    def runtime(self) -> str:
        return self._runtime

    @property
    def sidecar_url(self) -> str:
        return self._sidecar_url

    async def list_voices(self) -> tuple[VoiceMeta, ...]:
        """Return KittenTTS's canonical voice catalog (Johnny-1ge.8).

        Static — derived from :data:`KITTEN_VOICE_CATALOG`; no model load,
        network call, or runtime dependency, so it works for every runtime
        and before any provider row is saved. Every voice lives in the single
        checkpoint, so each is reported ``installed=True``.
        """
        return tuple(
            _voice_meta(value, label, gender)
            for value, label, gender in KITTEN_VOICE_CATALOG
        )

    async def synthesize_stream(
        self,
        text: str,
        voice_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        """Synthesize ``text`` to 16 kHz mono S16LE PCM frames.

        Dispatches on the configured ``runtime``:

        * ``in-container`` (default) — the ``kittentts`` library in this
          process, with the loaded model held in the module cache so only the
          first synth per model pays the load.
        * ``http-sidecar`` — POST the text to a KittenTTS sidecar on the host.

        Output is converted from KittenTTS's native 24 kHz float to 16 kHz
        S16LE so the frames slot directly into the meet-worker audio bridge.
        Emits one ``kitten.synth:`` INFO line per call with time-to-first-audio
        and total wall-clock so a regression to cold loading is visible in
        ``docker logs api``.
        """
        voice = voice_id if voice_id not in (None, "") else self._default_voice_id
        if not voice:
            raise TTSError(
                "KittenTTS requires a voice; pass voice_id explicitly or set "
                "voice_id in the provider configuration."
            )
        stream: AsyncGenerator[bytes, None]
        if self._runtime == RUNTIME_IN_CONTAINER:
            stream = self._synth_in_container(text, voice)
        else:
            stream = self._synth_http_sidecar(text, voice)

        start = time.perf_counter()
        ttfa_ms = -1
        try:
            # aclosing() guarantees the inner runtime generator's finally
            # (thread drain, sidecar cleanup) runs if the consumer breaks out
            # early and closes this outer generator.
            async with contextlib.aclosing(stream) as inner:
                async for frame in inner:
                    if ttfa_ms < 0:
                        ttfa_ms = int((time.perf_counter() - start) * 1000)
                    yield frame
        finally:
            total_ms = int((time.perf_counter() - start) * 1000)
            logger.info(
                "kitten.synth: runtime=%s voice=%s text_chars=%d "
                "ttfa_ms=%d total_ms=%d",
                self._runtime,
                voice,
                len(text),
                ttfa_ms,
                total_ms,
            )

    async def _synth_in_container(
        self, text: str, voice: str
    ) -> AsyncGenerator[bytes, None]:
        """Runtime A — warm in-process KittenTTS model from the cache.

        Loads the model once per ``model_id`` (paying the one-off load),
        caches it at module scope, and reuses it on every later call. The
        model's ONNX session is driven under a per-model :class:`asyncio.Lock`
        so two coroutines don't run inference on it at once.

        KittenTTS's ``generate()`` returns the whole utterance as one numpy
        float array (it is not a streaming generator), so we run it on a worker
        thread, convert + resample, then yield the result in ``chunk_bytes``
        frames. Time-to-first-audio therefore ~= total synth time — fine for
        such a small, fast model.
        """
        handle = await self._ensure_model(voice)
        model = handle.model
        speed = self._speed

        def _produce() -> bytes:
            audio = model.generate(text, voice=voice, speed=speed)
            return _audio_to_pcm16(audio)

        async with handle.lock:
            handle.requests_served += 1
            try:
                native_pcm = await asyncio.to_thread(_produce)
            except Exception as exc:  # noqa: BLE001 — surfaced as a clean TTSError
                raise TTSError(
                    f"kittentts in-container synth failed: {exc}"
                ) from exc
        out = resample_pcm16(
            native_pcm, KITTEN_NATIVE_SAMPLE_RATE_HZ, PCM_SAMPLE_RATE_HZ
        )
        for i in range(0, len(out), self._chunk_bytes):
            frame = out[i : i + self._chunk_bytes]
            if frame:
                yield frame

    async def _synth_http_sidecar(
        self, text: str, voice: str
    ) -> AsyncGenerator[bytes, None]:
        """Runtime B — POST the text to a KittenTTS sidecar on the host.

        ``POST {sidecar_url}/synthesize`` with JSON ``{"text", "voice",
        "speed"}``; the response body is raw S16LE PCM at the model's native
        rate, advertised via the ``X-Sample-Rate`` header. The adapter
        resamples to 16 kHz. An unreachable sidecar raises a :class:`TTSError`
        naming the start script.
        """
        if not self._sidecar_url:
            raise TTSError(
                f"kittentts runtime={self._runtime} requires sidecar_url"
            )
        client = self._sidecar_client_or_open()
        url = f"{self._sidecar_url}/synthesize"
        start = time.perf_counter()
        try:
            response = await client.post(
                url,
                json={
                    "text": text,
                    "voice": voice,
                    "speed": self._speed,
                },
                timeout=SIDECAR_HTTP_TIMEOUT_SECONDS,
            )
        except httpx.RequestError as exc:
            raise TTSError(
                f"kittentts sidecar ({self._runtime}) at {self._sidecar_url} "
                f"unreachable: {exc}. Start it with "
                "./scripts/start-kitten-sidecar.sh start"
            ) from exc
        ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "kitten.sidecar: action=request runtime=%s url=%s status=%d ms=%d",
            self._runtime,
            url,
            response.status_code,
            ms,
        )
        if response.status_code != 200:
            raise TTSError(
                f"kittentts sidecar ({self._runtime}) returned HTTP "
                f"{response.status_code}: {response.text[:200]}"
            )
        pcm = response.content
        try:
            rate = int(
                response.headers.get("X-Sample-Rate", "")
                or KITTEN_NATIVE_SAMPLE_RATE_HZ
            )
        except ValueError:
            rate = KITTEN_NATIVE_SAMPLE_RATE_HZ
        extra = len(pcm) % PCM_SAMPLE_WIDTH_BYTES
        if extra:
            pcm = pcm[:-extra]
        out = resample_pcm16(pcm, rate, PCM_SAMPLE_RATE_HZ)
        for i in range(0, len(out), self._chunk_bytes):
            frame = out[i : i + self._chunk_bytes]
            if frame:
                yield frame

    # --- Hooks (overridable in tests) -------------------------------------

    async def _ensure_model(self, voice: str) -> _ModelHandle:
        """Return a warm :class:`_ModelHandle`, using the module-level cache.

        Fast-path is a lock-free cache hit. On a miss, a per-key load lock
        coalesces concurrent first-loads of the same model and the global gate
        serialises the heavy load across keys. Tests override
        :meth:`_load_model` to populate the cache with a fake; the autouse
        eviction fixture wipes the cache between tests.
        """
        key = self._model_id

        with _CACHE_LOCK:
            handle = _MODELS.get(key)
        if handle is not None:
            return handle

        lock = _get_or_make_lock(key)
        async with lock:
            with _CACHE_LOCK:
                handle = _MODELS.get(key)
            if handle is not None:
                return handle
            load_start = time.perf_counter()
            async with _GLOBAL_LOAD_GATE:
                loaded = await asyncio.to_thread(self._load_model)
            load_ms = int((time.perf_counter() - load_start) * 1000)
            handle = _ModelHandle(model=loaded)
            with _CACHE_LOCK:
                _MODELS[key] = handle
            logger.info(
                "kitten.load: voice=%s runtime=%s model=%s total_ms=%d",
                voice,
                self._runtime,
                self._model_id,
                load_ms,
            )
            return handle

    def _load_model(self) -> Any:
        """Load and return a KittenTTS model; overridable in tests.

        Imports ``kittentts`` lazily so this module stays importable in
        lightweight test environments / api containers that don't ship the
        library. Points ``HF_HOME`` at the configured model directory before
        construction so downloads land in a stable cache.
        """
        os.environ.setdefault("HF_HOME", self._model_dir)
        with contextlib.suppress(OSError):
            Path(self._model_dir).mkdir(parents=True, exist_ok=True)
        try:
            kitten_mod = import_module("kittentts")
        except ImportError as exc:
            raise TTSError(
                "kittentts library not importable for the in-container "
                f"runtime: {exc}. Install it (pip install the KittenTTS wheel "
                "from https://github.com/KittenML/KittenTTS/releases) into the "
                "api image, or switch the runtime to 'http-sidecar' and run the "
                "sidecar on the host."
            ) from exc
        try:
            return kitten_mod.KittenTTS(self._model_id)
        except Exception as exc:  # noqa: BLE001 — surfaced as a clean TTSError
            raise TTSError(
                f"failed to load kittentts model (model={self._model_id!r}): "
                f"{exc}"
            ) from exc

    def _sidecar_client_or_open(self) -> httpx.AsyncClient:
        """Return the per-instance httpx client, creating it on first use."""
        if self._sidecar_client is None:
            self._sidecar_client = httpx.AsyncClient(
                timeout=SIDECAR_HTTP_TIMEOUT_SECONDS,
            )
        return self._sidecar_client

    async def close(self) -> None:
        """Close the per-instance sidecar HTTP client.

        Does **not** evict the process-wide model cache — the next
        ``KittenTTS(...)`` for the same model reuses the warm model instead of
        paying the load cost again. Use :meth:`evict_process_cache` for a hard
        reset.
        """
        if self._sidecar_client is not None:
            with contextlib.suppress(Exception):
                await self._sidecar_client.aclose()
            self._sidecar_client = None

    @classmethod
    def evict_process_cache(cls) -> None:
        """Wipe the process-wide model cache.

        Used by tests and by a future "reload voices" admin action. The next
        in-container synth re-runs the model load. In-flight synths that already
        hold a ``_ModelHandle`` reference finish on the old model.
        """
        _evict_process_cache()


def register(*, replace: bool = False) -> None:
    """Register :class:`KittenTTS` under ``(ProviderKind.TTS, "kittentts")``.

    Safe to call from :mod:`app.providers` import even when ``kittentts`` is not
    installed — the library is only imported lazily inside
    :meth:`KittenTTS._load_model`. Misconfigured deployments fail loudly when
    the in-container model is actually needed, not at package import.
    """
    get_registry().register(
        ProviderKind.TTS, PROVIDER_NAME, KittenTTS, replace=replace
    )


__all__ = [
    "ALLOWED_RUNTIMES",
    "DEFAULT_CHUNK_BYTES",
    "DEFAULT_MODEL_DIR",
    "DEFAULT_MODEL_ID",
    "DEFAULT_RUNTIME",
    "DEFAULT_SIDECAR_URL",
    "DEFAULT_SPEED",
    "DEFAULT_VOICE_ID",
    "KITTEN_LANGUAGE",
    "KITTEN_NATIVE_SAMPLE_RATE_HZ",
    "KITTEN_VOICE_CATALOG",
    "PCM_CHANNELS",
    "PROVIDER_NAME",
    "RUNTIME_HTTP_SIDECAR",
    "RUNTIME_IN_CONTAINER",
    "SIDECAR_DEFAULT_URLS",
    "SIDECAR_HTTP_TIMEOUT_SECONDS",
    "KittenTTS",
    "register",
]
