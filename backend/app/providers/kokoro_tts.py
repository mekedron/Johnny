"""Kokoro local text-to-speech adapter.

Wraps the Kokoro open-weight TTS model (https://github.com/hexgrad/kokoro)
so the voice pipeline can produce speech entirely on-device. Kokoro is an
82 M-parameter Apache-2.0 model that punches well above its size — multi-voice
(American / British English plus several other languages), small enough to run
on CPU, with cleaner prosody than most models its size. It synthesises mono
float audio at its native 24 000 Hz; this adapter converts to 16-bit signed
little-endian PCM and resamples to the canonical 16 kHz mono format used by the
meet-worker audio bridge and the cloud TTS adapters.

Three runtimes are supported, selected via the ``runtime`` option (Settings →
Providers → Kokoro → Runtime), mirroring the Parakeet STT split:

* ``in-container`` (default) — the ``kokoro`` Python package imported lazily
  into the api process. The ``KPipeline`` (which holds the warm KModel) is
  cached at module scope keyed by ``(model_id, lang_code)`` so only the first
  synth per language pays the multi-second model load; warm calls return first
  audio in well under 200 ms. The heavy ``kokoro`` + torch deps are imported
  lazily so this module stays importable where the library is absent.
* ``mlx-sidecar`` — Kokoro running under Apple's MLX (Metal) via the
  ``mlx-audio`` package, in a sidecar process on the macOS host
  (``sidecars/kokoro-mlx/``, default ``http://host.docker.internal:8772``).
  Metal acceleration is unreachable from inside the arm64 Linux api container,
  so a host sidecar is the only native-GPU path — same architectural reason as
  the Parakeet MLX sidecar.
* ``http-sidecar`` — the upstream Kokoro running in a generic Python sidecar on
  the host (``sidecars/kokoro-http/``, default
  ``http://host.docker.internal:8773``). The non-MLX out-of-container path —
  e.g. an x86_64 Linux host with a CUDA GPU.

Wire protocol for both sidecars (identical to ``sidecars/piper-http``):

    POST /synthesize
        Body: JSON {"text": ..., "voice": ..., "speed": ..., "lang_code": ...}
        Response: 200 application/octet-stream
            Body: raw S16LE PCM at the model's native rate (24 000 Hz)
            Header: X-Sample-Rate: 24000
    GET /health -> {"ready": true, "voice": "...", "backend": "mlx"|"kokoro"}

Start either sidecar with ``./scripts/start-kokoro-sidecar.sh {mlx,http}``.

**License**: Kokoro and its weights ship under Apache-2.0 — usable in
commercial products. See https://huggingface.co/hexgrad/Kokoro-82M.
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
# Surface the structured ``kokoro.synth:`` / ``kokoro.load:`` / ``kokoro.sidecar:``
# lines in ``docker logs api`` so a regression to cold per-call loading shows up
# immediately. Mirrors the parakeet_stt / piper_tts handler setup — without this
# the root logger defaults to WARNING and our timing breadcrumbs get dropped.
# Attach a stderr handler only if the logger chain has none of our own so we
# don't shadow the project's logging setup when one is added later.
logger.setLevel(logging.INFO)
if not any(getattr(h, "_johnny_kokoro", False) for h in logger.handlers):
    _h = logging.StreamHandler()
    _h.setLevel(logging.INFO)
    _h.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    _h._johnny_kokoro = True  # type: ignore[attr-defined]
    logger.addHandler(_h)
    logger.propagate = False

PROVIDER_NAME = "kokoro"
DEFAULT_MODEL_ID = "hexgrad/Kokoro-82M"
DEFAULT_MODEL_DIR = "/var/lib/johnny/kokoro-models"
DEFAULT_VOICE_ID = "af_heart"
DEFAULT_SPEED = 1.0
DEFAULT_CHUNK_BYTES = 4_096
# Kokoro always emits 24 kHz mono float audio regardless of voice; the adapter
# converts to S16LE and resamples to the canonical 16 kHz bridge format.
KOKORO_NATIVE_SAMPLE_RATE_HZ = 24_000

# Kokoro encodes language in the first letter of every voice id (``af_*`` /
# ``am_*`` → American English, ``bf_*`` → British, etc.) and the ``KPipeline``
# is constructed per single-letter ``lang_code``. These are the codes Kokoro
# v1.0 ships.
LANG_AMERICAN_ENGLISH = "a"
LANG_BRITISH_ENGLISH = "b"
KOKORO_LANG_CODES = frozenset(
    {
        LANG_AMERICAN_ENGLISH,  # American English
        LANG_BRITISH_ENGLISH,  # British English
        "e",  # Spanish
        "f",  # French
        "h",  # Hindi
        "i",  # Italian
        "j",  # Japanese
        "p",  # Brazilian Portuguese
        "z",  # Mandarin Chinese
    }
)
DEFAULT_LANG_CODE = LANG_AMERICAN_ENGLISH

# Canonical Kokoro v1.0 voice catalog (value, human label). Drives the voice_id
# SELECT in the provider form. The adapter does NOT hard-reject voices outside
# this list (Kokoro may add more, and an operator may point at a custom pack),
# but the dropdown offers the curated set so the common case is one click.
KOKORO_VOICE_CATALOG: tuple[tuple[str, str], ...] = (
    # American English — female
    ("af_heart", "af_heart — American English ♀ (default)"),
    ("af_alloy", "af_alloy — American English ♀"),
    ("af_aoede", "af_aoede — American English ♀"),
    ("af_bella", "af_bella — American English ♀"),
    ("af_jessica", "af_jessica — American English ♀"),
    ("af_kore", "af_kore — American English ♀"),
    ("af_nicole", "af_nicole — American English ♀"),
    ("af_nova", "af_nova — American English ♀"),
    ("af_river", "af_river — American English ♀"),
    ("af_sarah", "af_sarah — American English ♀"),
    ("af_sky", "af_sky — American English ♀"),
    # American English — male
    ("am_adam", "am_adam — American English ♂"),
    ("am_echo", "am_echo — American English ♂"),
    ("am_eric", "am_eric — American English ♂"),
    ("am_fenrir", "am_fenrir — American English ♂"),
    ("am_liam", "am_liam — American English ♂"),
    ("am_michael", "am_michael — American English ♂"),
    ("am_onyx", "am_onyx — American English ♂"),
    ("am_puck", "am_puck — American English ♂"),
    ("am_santa", "am_santa — American English ♂"),
    # British English
    ("bf_alice", "bf_alice — British English ♀"),
    ("bf_emma", "bf_emma — British English ♀"),
    ("bf_isabella", "bf_isabella — British English ♀"),
    ("bf_lily", "bf_lily — British English ♀"),
    ("bm_daniel", "bm_daniel — British English ♂"),
    ("bm_fable", "bm_fable — British English ♂"),
    ("bm_george", "bm_george — British English ♂"),
    ("bm_lewis", "bm_lewis — British English ♂"),
    # Other languages (G2P backends may need espeak-ng on the host)
    ("ef_dora", "ef_dora — Spanish ♀"),
    ("em_alex", "em_alex — Spanish ♂"),
    ("ff_siwis", "ff_siwis — French ♀"),
    ("hf_alpha", "hf_alpha — Hindi ♀"),
    ("hm_omega", "hm_omega — Hindi ♂"),
    ("if_sara", "if_sara — Italian ♀"),
    ("im_nicola", "im_nicola — Italian ♂"),
    ("jf_alpha", "jf_alpha — Japanese ♀"),
    ("jm_kumo", "jm_kumo — Japanese ♂"),
    ("pf_dora", "pf_dora — Brazilian Portuguese ♀"),
    ("pm_alex", "pm_alex — Brazilian Portuguese ♂"),
    ("zf_xiaobei", "zf_xiaobei — Mandarin Chinese ♀"),
    ("zm_yunjian", "zm_yunjian — Mandarin Chinese ♂"),
)

# Runtime selector. ``in-container`` is the default so unconfigured installs
# behave predictably (and need no host sidecar). The two sidecar runtimes talk
# HTTP to a process running natively on the macOS host — the only way to reach
# Metal (MLX) from inside our arm64 Linux container, or to run on a separate
# GPU host.
RUNTIME_IN_CONTAINER = "in-container"
RUNTIME_MLX_SIDECAR = "mlx-sidecar"
RUNTIME_HTTP_SIDECAR = "http-sidecar"
DEFAULT_RUNTIME = RUNTIME_IN_CONTAINER
ALLOWED_RUNTIMES = frozenset(
    {RUNTIME_IN_CONTAINER, RUNTIME_MLX_SIDECAR, RUNTIME_HTTP_SIDECAR}
)
# Default per-runtime sidecar URLs. ``host.docker.internal`` resolves to the
# host loopback from inside Docker Desktop on macOS. Ports 8772 / 8773 are
# chosen so the Kokoro sidecars run alongside the Parakeet (8765 / 8766),
# KittenTTS (8771) and Piper-http (8775) sidecars without a collision.
SIDECAR_DEFAULT_URLS: dict[str, str] = {
    RUNTIME_MLX_SIDECAR: "http://host.docker.internal:8772",
    RUNTIME_HTTP_SIDECAR: "http://host.docker.internal:8773",
}
DEFAULT_SIDECAR_URL = SIDECAR_DEFAULT_URLS[RUNTIME_MLX_SIDECAR]
# 60 s is generous: a warm sidecar synthesises the sample phrase in well under
# a second; the cold first call may spend a few seconds loading the model. Past
# 60 s is a hung sidecar and the user should see a clear error.
SIDECAR_HTTP_TIMEOUT_SECONDS = 60.0


# --- Persistent in-process pipeline cache (runtime=in-container) ----------
#
# Kokoro's ``KPipeline`` holds the warm KModel (~82 M params) and the
# language-specific G2P. Distinct voices that share a language reuse the same
# pipeline (the voice "pack" is applied per call), so we key the cache by
# ``(model_id, lang_code)`` rather than the bead's literal ``(model_id,
# voice_id)`` — keying per voice would reload the whole model for every voice,
# which is wasteful and pointless. Same lock idiom as ``piper_tts._VOICES``: a
# sync lock guards the dicts, a per-key asyncio lock coalesces first-loads, and
# a global gate serialises the heavy load across keys.
KokoroKey = tuple[str, str]


@dataclass
class _PipelineHandle:
    """A cached, warm Kokoro ``KPipeline`` plus the lock that serialises use.

    ``pipeline`` is the loaded library object (its torch model is not safe to
    drive from two threads at once, hence ``lock``). ``requests_served`` and
    ``loaded_at`` feed the ``kokoro.load:`` breadcrumbs.
    """

    pipeline: Any
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    loaded_at: float = field(default_factory=time.perf_counter)
    requests_served: int = 0


_PIPELINES: dict[KokoroKey, _PipelineHandle] = {}
_LOAD_LOCKS: dict[KokoroKey, asyncio.Lock] = {}
_CACHE_LOCK = threading.Lock()
_GLOBAL_LOAD_GATE = asyncio.Lock()


def _get_or_make_lock(key: KokoroKey) -> asyncio.Lock:
    """Return the load-lock for ``key``, creating it on first sight.

    Guarded by :data:`_CACHE_LOCK` so two coroutines don't race on the
    ``setdefault``. The map only grows with distinct (model, language) keys
    (a handful in practice), so the leak is negligible.
    """
    with _CACHE_LOCK:
        lock = _LOAD_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _LOAD_LOCKS[key] = lock
        return lock


def _evict_process_cache() -> None:
    """Drop every cached warm pipeline and per-key load lock.

    Public API is :meth:`KokoroTTS.evict_process_cache`; this is the
    module-level helper it dispatches to so tests / a future admin "reload
    voices" action can wipe state without touching the class.
    """
    with _CACHE_LOCK:
        _PIPELINES.clear()
        _LOAD_LOCKS.clear()


def _resolve_lang_code(language: str | None, voice: str) -> str:
    """Resolve the Kokoro single-letter ``lang_code`` for a synth call.

    An explicit ``language`` option wins when it is a recognised single-letter
    Kokoro code. Otherwise the language is derived from the voice prefix
    (``af_bella`` → ``a``), since every Kokoro voice id encodes its language in
    the first character. Falls back to American English for an unrecognised
    voice so synthesis still attempts rather than hard-failing.
    """
    if language and language in KOKORO_LANG_CODES:
        return language
    prefix = (voice or "")[:1].lower()
    if prefix in KOKORO_LANG_CODES:
        return prefix
    return DEFAULT_LANG_CODE


def _extract_segment_audio(item: Any) -> Any:
    """Pull the audio array out of one Kokoro pipeline yield.

    Kokoro's ``KPipeline`` yields ``(graphemes, phonemes, audio)`` tuples on
    current releases and ``Result`` objects with an ``.audio`` attribute on
    others. Accept both, plus a bare audio array, so a library version bump
    doesn't break synthesis.
    """
    if isinstance(item, (tuple, list)):
        return item[-1] if item else None
    audio = getattr(item, "audio", None)
    if audio is not None:
        return audio
    return item


def _audio_to_pcm16(audio: Any) -> bytes:
    """Convert a Kokoro audio segment to 16-bit signed-LE PCM bytes.

    Kokoro returns float audio in roughly ``[-1, 1]`` as a torch tensor (or
    numpy array). We move it to CPU / numpy when those methods exist, then
    scale to int16. Falls back to a pure-Python conversion when numpy is absent
    so the helper (and the module) stay importable in lightweight test
    environments that feed plain float lists.
    """
    if audio is None:
        return b""
    # torch.Tensor → detach/cpu/numpy when present (no hard torch dependency).
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
    arr = np.asarray(audio, dtype=np.float32)
    clipped = np.clip(arr, -1.0, 1.0)
    pcm: bytes = (clipped * 32767.0).astype("<i2").tobytes()
    return pcm


class KokoroTTS(TTSProvider):
    """Streaming TTS via the local Kokoro 82 M model.

    Configuration ``options`` (any key may be omitted):

    * ``runtime`` — ``in-container`` (default, ``kokoro`` in the api process),
      ``mlx-sidecar`` (Apple MLX on the macOS host), or ``http-sidecar``
      (upstream Kokoro in a host sidecar). The sidecar runtimes require running
      the matching sidecar via ``./scripts/start-kokoro-sidecar.sh``.
    * ``sidecar_url`` — base URL of the sidecar. Used only by the sidecar
      runtimes; default depends on the runtime (8772 MLX, 8773 HTTP).
    * ``voice_id`` — Kokoro voice (e.g. ``af_heart``, ``am_adam``,
      ``bf_emma``). Defaults to ``af_heart``.
    * ``language`` — Kokoro single-letter ``lang_code`` (``a`` American
      English, ``b`` British, ``e`` Spanish, …). Leave blank to derive it from
      the voice prefix.
    * ``model_id`` — HuggingFace repo of the Kokoro weights (default
      ``hexgrad/Kokoro-82M``). In-container only; sidecars use their own
      configured model.
    * ``model_dir`` — cache directory for downloaded weights. In-container
      only. Falls back to ``JOHNNY_KOKORO_MODEL_DIR`` then
      ``/var/lib/johnny/kokoro-models``.
    * ``speed`` — synthesis speed multiplier (0.5–2.0, default 1.0).
    * ``chunk_bytes`` — output streaming chunk size (default 4096). Must be a
      multiple of the 2-byte S16 sample width.
    """

    def __init__(self, config: ProviderConfig) -> None:
        if config.kind is not ProviderKind.TTS:
            raise ValueError(
                f"KokoroTTS requires ProviderKind.TTS; got {config.kind.value}"
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
        # Lazy httpx client for the sidecar runtimes; reused across calls on the
        # same instance so the TCP connection stays warm.
        self._sidecar_client: httpx.AsyncClient | None = None

        voice_id = opts.get("voice_id")
        # Unlike Piper (which requires an installed voice file), Kokoro ships
        # its voices with the model, so fall back to the flagship voice when the
        # operator leaves it blank — the Play sample button works out of the box.
        self._default_voice_id = (
            str(voice_id) if voice_id not in (None, "") else DEFAULT_VOICE_ID
        )
        language = opts.get("language")
        self._language: str | None = (
            str(language).strip() if language not in (None, "") else None
        )
        self._model_id = str(opts.get("model_id") or DEFAULT_MODEL_ID)
        self._model_dir = str(
            opts.get("model_dir")
            or os.environ.get("JOHNNY_KOKORO_MODEL_DIR")
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
            display_name="Kokoro",
            summary=(
                "Local Kokoro 82M TTS. Apache-2.0, multi-voice, CPU-capable, "
                "no audio leaves host."
            ),
            signup_url="https://huggingface.co/hexgrad/Kokoro-82M",
            fields=(
                FieldDef(
                    name="runtime",
                    label="Runtime",
                    type=FieldType.SELECT,
                    default=DEFAULT_RUNTIME,
                    options=(
                        FieldOption(
                            value=RUNTIME_IN_CONTAINER,
                            label="In-container (kokoro in the api process)",
                        ),
                        FieldOption(
                            value=RUNTIME_MLX_SIDECAR,
                            label="MLX sidecar (Apple Silicon, Metal)",
                        ),
                        FieldOption(
                            value=RUNTIME_HTTP_SIDECAR,
                            label="HTTP sidecar (Kokoro on a host / GPU box)",
                        ),
                    ),
                    help_text=(
                        "Which Kokoro runtime to use. In-container runs the "
                        "kokoro library inside the api process and caches the "
                        "warm model — second and later synths return first "
                        "audio in well under 200 ms. MLX runs Kokoro on Apple's "
                        "Metal GPU via the kokoro-mlx sidecar; HTTP sidecar "
                        "runs the upstream model on a separate host. Sidecars "
                        "must be started separately — see "
                        "./scripts/start-kokoro-sidecar.sh."
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
                        "Base URL of the running sidecar. Default port is 8772 "
                        "for MLX, 8773 for HTTP. Use "
                        "http://host.docker.internal:PORT from inside the api "
                        "container; the sidecar runs natively on the macOS "
                        "host. Ignored when runtime is in-container."
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="voice_id",
                    label="Voice",
                    type=FieldType.SELECT,
                    required=True,
                    default=DEFAULT_VOICE_ID,
                    options=tuple(
                        FieldOption(value=value, label=label)
                        for value, label in KOKORO_VOICE_CATALOG
                    ),
                    help_text=(
                        "Kokoro voice. The first two letters encode language + "
                        "gender (af = American female, bm = British male, …). "
                        "Voices ship with the model — no separate install."
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="language",
                    label="Language code",
                    placeholder="auto (from voice)",
                    help_text=(
                        "Kokoro single-letter lang_code: a American English, "
                        "b British, e Spanish, f French, h Hindi, i Italian, "
                        "j Japanese, p Brazilian Portuguese, z Mandarin. Leave "
                        "blank to derive it from the voice prefix. Non-English "
                        "G2P may need espeak-ng installed on the host."
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="model_id",
                    label="Model repo",
                    default=DEFAULT_MODEL_ID,
                    help_text=(
                        "HuggingFace repo of the Kokoro weights. In-container "
                        "runtime only; sidecars use their own configured model."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="model_dir",
                    label="Model cache directory",
                    default=DEFAULT_MODEL_DIR,
                    help_text=(
                        "Where Kokoro / HuggingFace weights are cached on disk. "
                        "In-container runtime only."
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
                        "The first synth per language pays the model load "
                        "(a few seconds); the KPipeline is then cached at "
                        "process scope keyed by (model, language). Click Play "
                        "sample twice — the second click returns first audio in "
                        "well under 200 ms. The cache is shared across provider "
                        "rows, so a saved Kokoro provider is warm for live "
                        "meetings too."
                    ),
                ),
                ProviderTip(
                    topic="MLX sidecar is the Apple Silicon fast path",
                    body=(
                        "Inside the arm64 Linux api container there is no Metal "
                        "access, so in-container Kokoro runs on CPU. For real "
                        "Apple Silicon acceleration pick the MLX sidecar and "
                        "start it with ./scripts/start-kokoro-sidecar.sh mlx — "
                        "it runs Kokoro under mlx-audio on the host GPU and "
                        "typically beats the in-container CPU path on TTFA."
                    ),
                ),
                ProviderTip(
                    topic="Voices ship with the model",
                    body=(
                        "Unlike Piper (one .onnx per voice), every Kokoro voice "
                        "lives in the single 82M checkpoint, so switching voice "
                        "is instant and needs no install. The first two letters "
                        "pick language + gender: af/am American, bf/bm British, "
                        "plus Spanish, French, Hindi, Italian, Japanese, "
                        "Portuguese and Mandarin sets."
                    ),
                ),
                ProviderTip(
                    topic="Non-English needs espeak-ng",
                    body=(
                        "English uses Kokoro's built-in misaki G2P, but the "
                        "other languages fall back to espeak-ng for "
                        "grapheme-to-phoneme. If a non-English voice produces "
                        "silence or errors, install espeak-ng on whichever host "
                        "runs the model (brew install espeak-ng)."
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
    def language(self) -> str | None:
        return self._language

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def chunk_bytes(self) -> int:
        return self._chunk_bytes

    @property
    def native_sample_rate(self) -> int:
        return KOKORO_NATIVE_SAMPLE_RATE_HZ

    @property
    def runtime(self) -> str:
        return self._runtime

    @property
    def sidecar_url(self) -> str:
        return self._sidecar_url

    async def synthesize_stream(
        self,
        text: str,
        voice_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        """Synthesize ``text`` to 16 kHz mono S16LE PCM frames.

        Dispatches on the configured ``runtime``:

        * ``in-container`` (default) — the ``kokoro`` library in this process,
          with the warm ``KPipeline`` held in the module cache so only the
          first synth per language pays the load.
        * ``mlx-sidecar`` / ``http-sidecar`` — POST the text to a Kokoro
          sidecar on the host (both speak the same wire protocol; they differ
          only in where they run and the default port).

        Output is converted from Kokoro's native 24 kHz float to 16 kHz S16LE
        so the frames slot directly into the meet-worker audio bridge. Emits
        one ``kokoro.synth:`` INFO line per call with time-to-first-audio and
        total wall-clock so a regression to cold loading is visible in
        ``docker logs api``.
        """
        voice = voice_id if voice_id not in (None, "") else self._default_voice_id
        if not voice:
            raise TTSError(
                "KokoroTTS requires a voice; pass voice_id explicitly or set "
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
                "kokoro.synth: runtime=%s voice=%s text_chars=%d "
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
        """Runtime A — warm in-process Kokoro ``KPipeline`` from the cache.

        Loads the pipeline once per language (paying the multi-second model
        load), caches it at module scope keyed by ``(model_id, lang_code)``,
        and reuses it on every later call. The pipeline's torch model is not
        safe to drive concurrently, so a per-pipeline :class:`asyncio.Lock`
        serialises use.

        Kokoro's pipeline is a blocking generator that yields one audio segment
        per sentence; we pump it on a worker thread and hand segments to the
        async consumer through a queue as they arrive, so the first sentence's
        audio leaves the box without waiting for the whole utterance.
        """
        lang_code = _resolve_lang_code(self._language, voice)
        handle = await self._ensure_pipeline(lang_code, voice)

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        sentinel = object()
        pipeline = handle.pipeline
        speed = self._speed

        def _produce() -> None:
            try:
                for item in pipeline(text, voice=voice, speed=speed):
                    pcm = _audio_to_pcm16(_extract_segment_audio(item))
                    loop.call_soon_threadsafe(queue.put_nowait, pcm)
            except Exception as exc:  # noqa: BLE001 — surfaced on the async side
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, sentinel)

        async with handle.lock:
            handle.requests_served += 1
            producer = asyncio.ensure_future(asyncio.to_thread(_produce))
            try:
                while True:
                    item = await queue.get()
                    if item is sentinel:
                        break
                    if isinstance(item, Exception):
                        raise TTSError(
                            f"kokoro in-container synth failed: {item}"
                        ) from item
                    out = resample_pcm16(
                        item, KOKORO_NATIVE_SAMPLE_RATE_HZ, PCM_SAMPLE_RATE_HZ
                    )
                    if out:
                        yield out
            finally:
                with contextlib.suppress(Exception):
                    await producer

    async def _synth_http_sidecar(
        self, text: str, voice: str
    ) -> AsyncGenerator[bytes, None]:
        """Runtimes B & C — POST the text to a Kokoro sidecar on the host.

        Both the MLX and HTTP sidecars speak the same wire protocol (only the
        default port and the backend differ), so one method serves both:
        ``POST {sidecar_url}/synthesize`` with JSON
        ``{"text", "voice", "speed", "lang_code"}``; the response body is raw
        S16LE PCM at the model's native rate, advertised via the
        ``X-Sample-Rate`` header. The adapter resamples to 16 kHz. An
        unreachable sidecar raises a :class:`TTSError` naming the start script.
        """
        if not self._sidecar_url:
            raise TTSError(
                f"kokoro runtime={self._runtime} requires sidecar_url"
            )
        client = self._sidecar_client_or_open()
        url = f"{self._sidecar_url}/synthesize"
        lang_code = _resolve_lang_code(self._language, voice)
        start = time.perf_counter()
        try:
            response = await client.post(
                url,
                json={
                    "text": text,
                    "voice": voice,
                    "speed": self._speed,
                    "lang_code": lang_code,
                },
                timeout=SIDECAR_HTTP_TIMEOUT_SECONDS,
            )
        except httpx.RequestError as exc:
            raise TTSError(
                f"kokoro sidecar ({self._runtime}) at {self._sidecar_url} "
                f"unreachable: {exc}. Start it with "
                "./scripts/start-kokoro-sidecar.sh"
            ) from exc
        ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "kokoro.sidecar: action=request runtime=%s url=%s status=%d ms=%d",
            self._runtime,
            url,
            response.status_code,
            ms,
        )
        if response.status_code != 200:
            raise TTSError(
                f"kokoro sidecar ({self._runtime}) returned HTTP "
                f"{response.status_code}: {response.text[:200]}"
            )
        pcm = response.content
        try:
            rate = int(
                response.headers.get("X-Sample-Rate", "")
                or KOKORO_NATIVE_SAMPLE_RATE_HZ
            )
        except ValueError:
            rate = KOKORO_NATIVE_SAMPLE_RATE_HZ
        extra = len(pcm) % PCM_SAMPLE_WIDTH_BYTES
        if extra:
            pcm = pcm[:-extra]
        out = resample_pcm16(pcm, rate, PCM_SAMPLE_RATE_HZ)
        for i in range(0, len(out), self._chunk_bytes):
            frame = out[i : i + self._chunk_bytes]
            if frame:
                yield frame

    # --- Hooks (overridable in tests) -------------------------------------

    async def _ensure_pipeline(
        self, lang_code: str, voice: str
    ) -> _PipelineHandle:
        """Return a warm :class:`_PipelineHandle`, using the module-level cache.

        Fast-path is a lock-free cache hit. On a miss, a per-key load lock
        coalesces concurrent first-loads of the same language and the global
        gate serialises the heavy ``KPipeline`` construction across keys. Tests
        override :meth:`_load_pipeline` to populate the cache with a fake; the
        autouse eviction fixture wipes the cache between tests.
        """
        key: KokoroKey = (self._model_id, lang_code)

        with _CACHE_LOCK:
            handle = _PIPELINES.get(key)
        if handle is not None:
            return handle

        lock = _get_or_make_lock(key)
        async with lock:
            with _CACHE_LOCK:
                handle = _PIPELINES.get(key)
            if handle is not None:
                return handle
            load_start = time.perf_counter()
            async with _GLOBAL_LOAD_GATE:
                loaded = await asyncio.to_thread(self._load_pipeline, lang_code)
            load_ms = int((time.perf_counter() - load_start) * 1000)
            handle = _PipelineHandle(pipeline=loaded)
            with _CACHE_LOCK:
                _PIPELINES[key] = handle
            logger.info(
                "kokoro.load: voice=%s runtime=%s lang=%s model=%s total_ms=%d",
                voice,
                self._runtime,
                lang_code,
                self._model_id,
                load_ms,
            )
            return handle

    def _load_pipeline(self, lang_code: str) -> Any:
        """Load and return a warm Kokoro ``KPipeline``; overridable in tests.

        Imports ``kokoro`` lazily so this module stays importable in
        lightweight test environments / api containers that don't ship the
        (torch-heavy) library. Points ``HF_HOME`` at the configured model
        directory before construction so downloads land in a stable cache.
        """
        os.environ.setdefault("HF_HOME", self._model_dir)
        with contextlib.suppress(OSError):
            Path(self._model_dir).mkdir(parents=True, exist_ok=True)
        try:
            kokoro_mod = import_module("kokoro")
        except ImportError as exc:
            raise TTSError(
                "kokoro library not importable for the in-container runtime: "
                f"{exc}. Install it (pip install kokoro) into the api image, or "
                "switch the runtime to 'mlx-sidecar' / 'http-sidecar' and run "
                "the sidecar on the host."
            ) from exc
        try:
            return kokoro_mod.KPipeline(
                lang_code=lang_code, repo_id=self._model_id
            )
        except Exception as exc:  # noqa: BLE001 — surfaced as a clean TTSError
            raise TTSError(
                f"failed to load kokoro pipeline (lang={lang_code}, "
                f"model={self._model_id!r}): {exc}"
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

        Does **not** evict the process-wide warm-pipeline cache — the next
        ``KokoroTTS(...)`` for the same model + language reuses the warm
        pipeline instead of paying the load cost again. Use
        :meth:`evict_process_cache` for a hard reset.
        """
        if self._sidecar_client is not None:
            with contextlib.suppress(Exception):
                await self._sidecar_client.aclose()
            self._sidecar_client = None

    @classmethod
    def evict_process_cache(cls) -> None:
        """Wipe the process-wide warm-pipeline cache.

        Used by tests and by a future "reload voices" admin action. The next
        in-container synth re-runs the ``KPipeline`` load. In-flight synths that
        already hold a ``_PipelineHandle`` reference finish on the old pipeline.
        """
        _evict_process_cache()


def register(*, replace: bool = False) -> None:
    """Register :class:`KokoroTTS` under ``(ProviderKind.TTS, "kokoro")``.

    Safe to call from :mod:`app.providers` import even when ``kokoro`` is not
    installed — the library is only imported lazily inside
    :meth:`KokoroTTS._load_pipeline`. Misconfigured deployments fail loudly when
    the in-container model is actually needed, not at package import.
    """
    get_registry().register(
        ProviderKind.TTS, PROVIDER_NAME, KokoroTTS, replace=replace
    )


__all__ = [
    "ALLOWED_RUNTIMES",
    "DEFAULT_CHUNK_BYTES",
    "DEFAULT_LANG_CODE",
    "DEFAULT_MODEL_DIR",
    "DEFAULT_MODEL_ID",
    "DEFAULT_RUNTIME",
    "DEFAULT_SIDECAR_URL",
    "DEFAULT_SPEED",
    "DEFAULT_VOICE_ID",
    "KOKORO_LANG_CODES",
    "KOKORO_NATIVE_SAMPLE_RATE_HZ",
    "KOKORO_VOICE_CATALOG",
    "PCM_CHANNELS",
    "PROVIDER_NAME",
    "RUNTIME_HTTP_SIDECAR",
    "RUNTIME_IN_CONTAINER",
    "RUNTIME_MLX_SIDECAR",
    "SIDECAR_DEFAULT_URLS",
    "SIDECAR_HTTP_TIMEOUT_SECONDS",
    "KokoroTTS",
    "register",
]
