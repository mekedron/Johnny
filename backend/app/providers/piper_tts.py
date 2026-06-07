"""Piper local text-to-speech adapter.

Wraps the Piper TTS binary (https://github.com/rhasspy/piper) so the
voice pipeline can produce speech entirely on-device, with no audio
leaving the host. Piper synthesises text to raw 16-bit signed
little-endian PCM at the voice model's native sample rate (commonly
22 050 Hz for "medium" voices, 16 000 Hz for "low" voices). This adapter
resamples the output to the canonical 16 kHz mono S16LE format used by
the meet-worker audio bridge and the cloud TTS adapters.

Voice model files (``.onnx`` + ``.onnx.json`` sidecars) live in a
configurable directory that is mounted as a Docker volume in production
so the models persist across container rebuilds. The default location
is ``/var/lib/johnny/piper-models``; override via the ``model_dir``
provider option or the ``JOHNNY_PIPER_MODEL_DIR`` environment variable.

Voices can be installed via the helper functions
:func:`fetch_voice_catalog` and :func:`download_voice` which pull from
huggingface.co/rhasspy/piper-voices — used by the
``POST /providers/{id}/voices/{voice}/install`` endpoint so an operator
never has to touch a terminal to wire up a voice.

Latency profile depends on the ``runtime`` option (Settings → Providers →
Local Piper → Runtime):

* ``subprocess`` (default) — a fresh ``piper`` CLI process per call; time-to-
  first-audio is dominated by ONNX startup (~200-400 ms for medium voices on
  CPU) and is paid every turn. Safe single-step-debug baseline.
* ``persistent-subprocess`` — a warm in-process ``PiperVoice`` cached at module
  scope; the ~700 ms load is paid once and warm calls return first audio in
  ~40-60 ms. (piper-tts 1.x dropped the old ``--json-input`` streaming CLI, so
  the warm path is in-process rather than a long-lived child process.) This is
  the real meeting-latency win.
* ``http-sidecar`` — POSTs text to a piper sidecar on the macOS host
  (``sidecars/piper-http/``, started via ``scripts/start-piper-sidecar.sh``)
  for process isolation or future macOS-native voices.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import IO, Any, Protocol

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
# Surface the structured ``piper.synth:`` / ``piper.worker:`` / ``piper.sidecar:``
# lines in ``docker logs api`` so a regression to cold per-call loading shows up
# immediately. Mirrors the parakeet_stt handler setup — without this the root
# logger defaults to WARNING and our timing breadcrumbs get dropped. Attach a
# stderr handler only if the logger chain has none of our own so we don't shadow
# the project's logging setup when one is added later.
logger.setLevel(logging.INFO)
if not any(getattr(h, "_johnny_piper", False) for h in logger.handlers):
    _h = logging.StreamHandler()
    _h.setLevel(logging.INFO)
    _h.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    _h._johnny_piper = True  # type: ignore[attr-defined]
    logger.addHandler(_h)
    logger.propagate = False

PROVIDER_NAME = "piper"
DEFAULT_BINARY = "piper"
DEFAULT_MODEL_DIR = "/var/lib/johnny/piper-models"
DEFAULT_NATIVE_SAMPLE_RATE_HZ = 22_050
DEFAULT_CHUNK_BYTES = 4_096
DEFAULT_WAIT_TIMEOUT_S = 30.0
DEFAULT_TERMINATE_TIMEOUT_S = 2.0

# Runtime selector. ``subprocess`` is the historical behaviour (a fresh piper
# CLI process per call) and remains the default so unconfigured installs keep
# working bit-for-bit. ``persistent-subprocess`` keeps a warm in-process
# ``PiperVoice`` (ONNX session) cached at module scope — piper-tts 1.x dropped
# the old ``--json-input`` long-running-CLI protocol, so the warm path is an
# in-process voice cache rather than a literal child process (same net effect:
# the ~700 ms ONNX cold-start is paid once, not per turn). ``http-sidecar``
# POSTs text to a piper sidecar on the macOS host, mirroring the Parakeet
# sidecars.
RUNTIME_SUBPROCESS = "subprocess"
RUNTIME_PERSISTENT = "persistent-subprocess"
RUNTIME_HTTP_SIDECAR = "http-sidecar"
DEFAULT_RUNTIME = RUNTIME_SUBPROCESS
ALLOWED_RUNTIMES = frozenset(
    {RUNTIME_SUBPROCESS, RUNTIME_PERSISTENT, RUNTIME_HTTP_SIDECAR}
)
# Default sidecar URL. ``host.docker.internal`` resolves to the host's loopback
# from inside Docker Desktop on macOS; port 8775 is chosen so the piper sidecar
# can run alongside the Parakeet sidecars (8765 / 8766) without a collision.
DEFAULT_SIDECAR_URL = "http://host.docker.internal:8775"
# 60 s is generous: a warm sidecar synthesises the sample phrase in well under a
# second; the cold first call may spend a few seconds loading the voice. Past
# 60 s is a hung sidecar and the user should see a clear error.
SIDECAR_HTTP_TIMEOUT_SECONDS = 60.0

# Cap captured stderr so a runaway error log can't blow up memory. 4 KB is
# more than enough to surface a single piper-phonemize / onnxruntime traceback
# in the UI without producing an unreadable wall of text.
STDERR_BUFFER_LIMIT_BYTES = 4_096

# rhasspy/piper-voices repo on HuggingFace publishes voices.json as a
# manifest of every supported voice, with relative paths to the .onnx +
# .onnx.json sidecar files. These constants point at the resolve/main
# URLs so the API can both list and download voices without hardcoding
# the repo layout in multiple places.
PIPER_VOICES_REPO_BASE = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main"
)
PIPER_VOICES_CATALOG_URL = f"{PIPER_VOICES_REPO_BASE}/voices.json"


# --- Persistent in-process voice cache (runtime=persistent-subprocess) ----
#
# piper-tts 1.x (the Python rewrite installed via the ``local-tts`` extra) has
# no ``--json-input`` streaming-CLI mode and no ``--http`` server, so the
# "persistent" runtime can't be a long-lived child process fed line-by-line.
# Instead we load the library's ``PiperVoice`` (which holds the warm ONNX
# session) once and cache it at module scope keyed by ``(model_path,
# native_sample_rate)`` — the same idiom as ``parakeet_stt._LAST`` but a dict
# rather than a single slot, because piper voices are ~60 MB each (vs Parakeet's
# 0.6 B) so switching voices needn't evict the previous one.
VoiceKey = tuple[str, int]


@dataclass
class _VoiceHandle:
    """A cached, warm ``PiperVoice`` plus the lock that serialises its use.

    ``voice`` is the loaded library object (its ONNX session is not safe to
    drive from two threads at once, hence ``lock``). ``requests_served`` and
    ``loaded_at`` feed the ``piper.worker:`` breadcrumbs.
    """

    voice: _PiperVoice
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    loaded_at: float = field(default_factory=time.perf_counter)
    requests_served: int = 0


# The warm-voice cache and its guards. ``_CACHE_LOCK`` (sync) protects the dicts;
# ``_LOAD_LOCKS`` coalesces concurrent first-loads of the same key; the global
# gate serialises the heavy ``PiperVoice.load`` across keys so a voice switch
# can't briefly hold two loads at once.
_VOICES: dict[VoiceKey, _VoiceHandle] = {}
_LOAD_LOCKS: dict[VoiceKey, asyncio.Lock] = {}
_CACHE_LOCK = threading.Lock()
_GLOBAL_LOAD_GATE = asyncio.Lock()


class _AudioChunk(Protocol):
    """The subset of piper's ``AudioChunk`` the adapter reads."""

    audio_int16_bytes: bytes
    sample_rate: int


class _PiperVoice(Protocol):
    """The subset of piper's ``PiperVoice`` the persistent runtime depends on."""

    def synthesize(self, text: str) -> Any: ...


def _get_or_make_lock(key: VoiceKey) -> asyncio.Lock:
    """Return the load-lock for ``key``, creating it on first sight.

    Guarded by :data:`_CACHE_LOCK` so two coroutines don't race on the
    ``setdefault``. The map only grows with distinct voice keys (a handful of
    provider rows in practice), so the leak is negligible.
    """
    with _CACHE_LOCK:
        lock = _LOAD_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _LOAD_LOCKS[key] = lock
        return lock


def _evict_process_cache() -> None:
    """Drop every cached warm voice and per-key load lock.

    Public API is :meth:`PiperTTS.evict_process_cache`; this is the
    module-level helper it dispatches to so tests / a future admin "reload
    voices" action can wipe state without touching the class.
    """
    with _CACHE_LOCK:
        _VOICES.clear()
        _LOAD_LOCKS.clear()


class _Process(Protocol):
    """The subset of :class:`subprocess.Popen` PiperTTS depends on.

    Mirrors the Protocol used by ``MeetAudioBridge`` so subprocess-driven
    adapters share the same testing pattern (inject a ``BytesIO``-backed
    fake by overriding the spawn hook). ``stderr`` is included so the
    adapter can drain piper's diagnostic output and surface it to the
    user when synthesis fails.
    """

    stdout: IO[bytes] | None
    stdin: IO[bytes] | None
    stderr: IO[bytes] | None

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


_resample_pcm16 = resample_pcm16


def _resolve_voice_path(model_dir: str, voice: str) -> Path:
    """Return the on-disk path to the model for ``voice``.

    Absolute paths are returned verbatim. Bare names like
    ``"en_US-amy-medium"`` resolve to ``<model_dir>/<voice>.onnx``; an
    explicit ``.onnx`` extension is preserved if given.
    """
    candidate = Path(voice)
    if candidate.is_absolute():
        return candidate
    path = Path(model_dir) / voice
    if path.suffix.lower() != ".onnx":
        path = path.with_suffix(".onnx")
    return path


class _StderrBuffer:
    """Bounded, thread-safe accumulator for a subprocess's stderr stream.

    Piper writes startup diagnostics — missing onnxruntime, missing
    espeak-ng phoneme data, malformed .onnx file — to stderr. We drain
    it in a background thread so the OS pipe never blocks the writer,
    and cap retained bytes so a verbose traceback can't blow up the
    process. The retained tail (last ``STDERR_BUFFER_LIMIT_BYTES``) is
    what the user sees in ``TestResult.detail`` when synthesis fails.
    """

    def __init__(self, limit: int = STDERR_BUFFER_LIMIT_BYTES) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._chunks: list[bytes] = []
        self._size = 0
        self._thread: threading.Thread | None = None

    def start_draining(self, stream: IO[bytes]) -> None:
        """Spawn a daemon thread that reads ``stream`` until EOF."""
        thread = threading.Thread(
            target=self._drain, args=(stream,), daemon=True, name="piper-stderr"
        )
        self._thread = thread
        thread.start()

    def _drain(self, stream: IO[bytes]) -> None:
        try:
            while True:
                chunk = stream.read(1024)
                if not chunk:
                    return
                with self._lock:
                    self._chunks.append(chunk)
                    self._size += len(chunk)
                    # Coalesce + trim to the tail when we exceed the cap so
                    # the most recent (most useful) lines are what we keep.
                    if self._size > self._limit * 2:
                        joined = b"".join(self._chunks)[-self._limit:]
                        self._chunks = [joined]
                        self._size = len(joined)
        except (OSError, ValueError):
            # Stream closed mid-read — common during cleanup; not an error.
            return

    def text(self) -> str:
        """Return the captured stderr as decoded text (tail-bounded)."""
        with self._lock:
            joined = b"".join(self._chunks)
        if len(joined) > self._limit:
            joined = joined[-self._limit:]
        return joined.decode("utf-8", errors="replace").strip()

    def join(self, timeout: float = 1.0) -> None:
        """Wait briefly for the drainer thread to finish."""
        if self._thread is not None:
            self._thread.join(timeout=timeout)


@dataclass(frozen=True, slots=True)
class VoiceInfo:
    """Public-facing voice descriptor returned by the API.

    ``key`` is the piper voice identifier (``en_US-amy-medium``); the
    same string the adapter uses as ``voice_id``. ``language_code``,
    ``language_name``, and ``quality`` come straight from the rhasspy
    voices.json. ``installed`` flips to ``True`` once both ``.onnx``
    and ``.onnx.json`` are present in the local ``model_dir``.
    """

    key: str
    name: str
    language_code: str
    language_name: str
    quality: str
    installed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "language_code": self.language_code,
            "language_name": self.language_name,
            "quality": self.quality,
            "installed": self.installed,
        }


# Native output rate per Piper quality tier (rhasspy convention): the low /
# x-low models render at 16 kHz, medium / high at 22.05 kHz. Used only to
# annotate the unified picker (Johnny-1ge.8) — synthesis still resamples to the
# 16 kHz bridge format regardless.
_PIPER_QUALITY_SAMPLE_RATE: dict[str, int] = {
    "x-low": 16_000,
    "low": 16_000,
    "medium": 22_050,
    "high": 22_050,
}


def _voice_info_to_meta(info: VoiceInfo) -> VoiceMeta:
    """Map a Piper :class:`VoiceInfo` to the unified :class:`VoiceMeta`."""
    parts = [p for p in (info.language_name, info.quality) if p]
    suffix = f" — {' · '.join(parts)}" if parts else ""
    return VoiceMeta(
        id=info.key,
        label=f"{info.name}{suffix}",
        language=info.language_name or info.language_code or None,
        sample_rate=_PIPER_QUALITY_SAMPLE_RATE.get(info.quality),
        gender=None,
        installed=info.installed,
    )


def voice_is_installed(model_dir: str, voice_key: str) -> bool:
    """Return ``True`` when both ``.onnx`` and ``.onnx.json`` are on disk."""
    base = Path(model_dir)
    onnx = base / f"{voice_key}.onnx"
    onnx_json = base / f"{voice_key}.onnx.json"
    return onnx.exists() and onnx_json.exists()


def _coerce_catalog(payload: dict[str, Any], model_dir: str) -> list[VoiceInfo]:
    """Turn the raw rhasspy voices.json payload into typed ``VoiceInfo``s.

    The on-disk schema is:
        {"<voice-key>": {"name": str, "language": {"code": ..., "name_english": ...},
                          "quality": "low"|"medium"|"high"|"x-low", ...}}

    We flatten that into a stable list sorted by language then voice key
    so the UI can render it without sorting client-side.
    """
    out: list[VoiceInfo] = []
    for key, entry in payload.items():
        if not isinstance(entry, dict):
            continue
        language = entry.get("language") or {}
        if not isinstance(language, dict):
            language = {}
        info = VoiceInfo(
            key=str(key),
            name=str(entry.get("name") or key),
            language_code=str(language.get("code") or language.get("family") or ""),
            language_name=str(
                language.get("name_english") or language.get("name_native") or ""
            ),
            quality=str(entry.get("quality") or ""),
            installed=voice_is_installed(model_dir, str(key)),
        )
        out.append(info)
    out.sort(key=lambda v: (v.language_code, v.key))
    return out


async def fetch_voice_catalog(
    model_dir: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = 15.0,
) -> list[VoiceInfo]:
    """Return every voice published on huggingface.co/rhasspy/piper-voices.

    Marks each entry with ``installed=True`` when the corresponding
    ``.onnx`` and ``.onnx.json`` exist in ``model_dir`` so the UI can
    grey out already-installed voices.

    Raises :class:`TTSError` with a human-readable message on transport
    failure so the endpoint can surface it as-is.
    """
    owns_client = client is None
    if client is None:
        # follow_redirects: HuggingFace's resolve endpoint 307s to a
        # CDN-cached blob URL. Without this the catalog fetch raises and
        # the voice browser is dead-on-arrival.
        client = httpx.AsyncClient(timeout=timeout_s, follow_redirects=True)
    try:
        try:
            response = await client.get(PIPER_VOICES_CATALOG_URL)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise TTSError(
                f"failed to fetch piper voice catalog: {exc}"
            ) from exc
        except ValueError as exc:
            raise TTSError(
                f"piper voice catalog is not valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise TTSError("piper voice catalog payload is not a JSON object")
        return _coerce_catalog(payload, model_dir)
    finally:
        if owns_client:
            await client.aclose()


def _find_voice_files(entry: dict[str, Any], voice_key: str) -> tuple[str, str]:
    """Locate the ``.onnx`` and ``.onnx.json`` relative paths for a voice.

    Returns the two paths relative to the rhasspy repo root, ready to
    be joined onto :data:`PIPER_VOICES_REPO_BASE`. Raises
    :class:`TTSError` when either file is missing from the manifest.
    """
    files = entry.get("files") or {}
    if not isinstance(files, dict):
        raise TTSError(
            f"voice {voice_key!r} catalog entry has no 'files' map"
        )
    onnx_path: str | None = None
    json_path: str | None = None
    for path in files:
        path_str = str(path)
        if path_str.endswith(f"{voice_key}.onnx"):
            onnx_path = path_str
        elif path_str.endswith(f"{voice_key}.onnx.json"):
            json_path = path_str
    if onnx_path is None or json_path is None:
        raise TTSError(
            f"voice {voice_key!r} is missing .onnx or .onnx.json in the catalog"
        )
    return onnx_path, json_path


async def _download_to_file(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
) -> int:
    """Stream ``url`` to ``dest`` atomically (write to .part, then rename).

    Returns the number of bytes written. The temporary ``.part`` file is
    removed on failure so a partial download never gets confused for a
    completed one.
    """
    tmp = dest.with_suffix(dest.suffix + ".part")
    total = 0
    try:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with tmp.open("wb") as fh:
                async for chunk in response.aiter_bytes():
                    fh.write(chunk)
                    total += len(chunk)
        tmp.replace(dest)
        return total
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


async def download_voice(
    voice_key: str,
    model_dir: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = 300.0,
) -> dict[str, Any]:
    """Download both files of ``voice_key`` into ``model_dir``.

    Idempotent: a fully-installed voice short-circuits and returns
    ``installed=True`` without re-downloading. Partially-downloaded files
    are overwritten so a previous interrupted install can be recovered
    by re-clicking Install.
    """
    dest_dir = Path(model_dir)
    onnx_dest = dest_dir / f"{voice_key}.onnx"
    json_dest = dest_dir / f"{voice_key}.onnx.json"
    if onnx_dest.exists() and json_dest.exists():
        return {
            "key": voice_key,
            "installed": True,
            "onnx_bytes": onnx_dest.stat().st_size,
            "onnx_json_bytes": json_dest.stat().st_size,
            "already_present": True,
        }

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout_s, follow_redirects=True)
    try:
        try:
            catalog_response = await client.get(PIPER_VOICES_CATALOG_URL)
            catalog_response.raise_for_status()
            catalog = catalog_response.json()
        except httpx.HTTPError as exc:
            raise TTSError(
                f"failed to fetch piper voice catalog: {exc}"
            ) from exc
        except ValueError as exc:
            raise TTSError(
                f"piper voice catalog is not valid JSON: {exc}"
            ) from exc
        entry = catalog.get(voice_key) if isinstance(catalog, dict) else None
        if not isinstance(entry, dict):
            raise TTSError(
                f"voice {voice_key!r} not found in piper-voices catalog"
            )
        onnx_rel, json_rel = _find_voice_files(entry, voice_key)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise TTSError(
                f"cannot create model_dir {model_dir!r}: {exc}"
            ) from exc
        try:
            onnx_bytes = await _download_to_file(
                client, f"{PIPER_VOICES_REPO_BASE}/{onnx_rel}", onnx_dest
            )
            json_bytes = await _download_to_file(
                client, f"{PIPER_VOICES_REPO_BASE}/{json_rel}", json_dest
            )
        except httpx.HTTPError as exc:
            raise TTSError(
                f"failed to download voice {voice_key!r}: {exc}"
            ) from exc
        except OSError as exc:
            raise TTSError(
                f"failed to write voice files into {model_dir!r}: {exc}"
            ) from exc
        return {
            "key": voice_key,
            "installed": True,
            "onnx_bytes": onnx_bytes,
            "onnx_json_bytes": json_bytes,
            "already_present": False,
        }
    finally:
        if owns_client:
            await client.aclose()


def remove_voice(voice_key: str, model_dir: str) -> dict[str, Any]:
    """Delete the ``.onnx`` and ``.onnx.json`` files for ``voice_key``.

    Returns a dict describing what happened so the UI can update in
    place without a follow-up catalog round-trip::

        {"key": "...", "installed": False,
         "onnx_removed": bool, "onnx_json_removed": bool}

    Raises :class:`FileNotFoundError` when neither file exists — the
    caller should surface that as a 404 to the user. Permission errors
    propagate as :class:`OSError`; the caller surfaces them as 502.
    Partial removals (one file present, one missing) succeed: the
    presence flag flips to False either way because :func:`is_installed`
    requires both files.
    """
    base = Path(model_dir)
    onnx = base / f"{voice_key}.onnx"
    onnx_json = base / f"{voice_key}.onnx.json"
    onnx_existed = onnx.exists()
    json_existed = onnx_json.exists()
    if not onnx_existed and not json_existed:
        raise FileNotFoundError(
            f"voice {voice_key!r} is not installed in {model_dir!r}"
        )
    if onnx_existed:
        onnx.unlink()
    if json_existed:
        onnx_json.unlink()
    return {
        "key": voice_key,
        "installed": False,
        "onnx_removed": onnx_existed,
        "onnx_json_removed": json_existed,
    }


class PiperTTS(TTSProvider):
    """Streaming TTS via the local Piper binary.

    Configuration ``options`` (any key may be omitted):

    * ``voice_id`` — default voice (model name resolved against
      ``model_dir`` or an absolute path to a ``.onnx`` file).
    * ``model_dir`` — directory holding ``.onnx`` model files. Falls
      back to the ``JOHNNY_PIPER_MODEL_DIR`` env var, then
      ``/var/lib/johnny/piper-models`` (mounted as a Docker volume in
      production).
    * ``binary`` — path to the ``piper`` executable. Falls back to the
      ``JOHNNY_PIPER_BINARY`` env var, then ``piper`` on ``PATH``.
    * ``native_sample_rate`` — model's native output rate in Hz so this
      adapter knows how to resample to 16 kHz. Default 22 050 — correct
      for most "medium" Piper voices; "low" voices output 16 000 Hz.
    * ``chunk_bytes`` — output streaming chunk size (default 4096).
      Must be a multiple of the 2-byte S16 sample width.
    """

    def __init__(self, config: ProviderConfig) -> None:
        if config.kind is not ProviderKind.TTS:
            raise ValueError(
                f"PiperTTS requires ProviderKind.TTS; got {config.kind.value}"
            )
        opts = config.options
        runtime = str(opts.get("runtime") or DEFAULT_RUNTIME)
        if runtime not in ALLOWED_RUNTIMES:
            raise ValueError(
                f"runtime {runtime!r} must be one of {sorted(ALLOWED_RUNTIMES)}"
            )
        self._runtime = runtime
        sidecar_url_opt = opts.get("sidecar_url")
        if sidecar_url_opt:
            self._sidecar_url = str(sidecar_url_opt).rstrip("/")
        elif runtime == RUNTIME_HTTP_SIDECAR:
            self._sidecar_url = DEFAULT_SIDECAR_URL
        else:
            self._sidecar_url = ""
        # Lazy httpx client for the sidecar runtime; reused across calls on the
        # same instance so the TCP connection stays warm.
        self._sidecar_client: httpx.AsyncClient | None = None
        voice_id = opts.get("voice_id")
        self._default_voice_id: str | None = (
            str(voice_id) if voice_id not in (None, "") else None
        )
        self._model_dir = str(
            opts.get("model_dir")
            or os.environ.get("JOHNNY_PIPER_MODEL_DIR")
            or DEFAULT_MODEL_DIR
        )
        self._binary = str(
            opts.get("binary")
            or os.environ.get("JOHNNY_PIPER_BINARY")
            or DEFAULT_BINARY
        )
        native_rate = int(opts.get("native_sample_rate") or DEFAULT_NATIVE_SAMPLE_RATE_HZ)
        if native_rate <= 0:
            raise ValueError(f"native_sample_rate must be positive; got {native_rate}")
        self._native_sample_rate = native_rate
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
            display_name="Local Piper",
            summary="Local Piper TTS. ~60 MB voices, CPU-only, no audio leaves host.",
            signup_url=None,
            fields=(
                FieldDef(
                    name="runtime",
                    label="Runtime",
                    type=FieldType.SELECT,
                    default=DEFAULT_RUNTIME,
                    options=(
                        FieldOption(
                            value=RUNTIME_SUBPROCESS,
                            label="Subprocess (fresh piper per call, safe default)",
                        ),
                        FieldOption(
                            value=RUNTIME_PERSISTENT,
                            label="Persistent (warm in-process voice, fastest)",
                        ),
                        FieldOption(
                            value=RUNTIME_HTTP_SIDECAR,
                            label="HTTP sidecar (piper on the macOS host)",
                        ),
                    ),
                    help_text=(
                        "How piper synthesises. Subprocess spawns a fresh "
                        "piper process per call (~200-400 ms cold every "
                        "turn) — the safe single-step debug default. "
                        "Persistent keeps the voice's ONNX session warm "
                        "in-process so repeat calls return first audio in "
                        "~40-60 ms (this is the real meeting-latency win). "
                        "HTTP sidecar POSTs text to a piper server on the "
                        "macOS host — start it with "
                        "./scripts/start-piper-sidecar.sh."
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
                        "Base URL of the running piper sidecar. Use "
                        "http://host.docker.internal:8775 from inside the "
                        "api container; the sidecar runs natively on the "
                        "macOS host. Ignored unless runtime is HTTP sidecar."
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="voice_id",
                    label="Voice ID",
                    required=True,
                    placeholder="en_US-amy-medium",
                    help_text=(
                        "Piper voice identifier — use the voice browser "
                        "below to install one without a terminal."
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="model_dir",
                    label="Voice directory",
                    default=DEFAULT_MODEL_DIR,
                    help_text="Where the .onnx + .onnx.json files live.",
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="binary",
                    label="Piper binary",
                    default=DEFAULT_BINARY,
                    help_text="Path to the piper executable.",
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="native_sample_rate",
                    label="Native sample rate (Hz)",
                    type=FieldType.NUMBER,
                    default=DEFAULT_NATIVE_SAMPLE_RATE_HZ,
                    help_text="Sample rate of the voice file; the adapter resamples to 16 kHz.",
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="chunk_bytes",
                    label="Read chunk bytes",
                    type=FieldType.NUMBER,
                    default=DEFAULT_CHUNK_BYTES,
                    help_text=(
                        "How many bytes the adapter waits to accumulate "
                        "before yielding a frame downstream. Smaller = "
                        "first audio leaves the box sooner, at the cost "
                        "of more syscalls."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
            ),
            tips=(
                ProviderTip(
                    topic="Voice tier — low vs medium vs high",
                    body=(
                        "Low voices output 16 kHz and synthesise fastest "
                        "(~150 ms time-to-first-audio on a modern CPU). "
                        "Medium voices (22.05 kHz) are noticeably more "
                        "natural and add roughly 50-100 ms. High voices "
                        "are a further +200 ms with marginal quality "
                        "gain for meeting use. Default to medium unless "
                        "you are demoing on a slow box — then low."
                    ),
                ),
                ProviderTip(
                    topic="Native sample rate must match the voice file",
                    body=(
                        "Set this to 22050 for medium / high voices and "
                        "16000 for low voices. The adapter resamples "
                        "everything to 16 kHz internally, but if the "
                        "rate is wrong the audio will sound chipmunk-fast "
                        "or sluggish before resampling adds artefacts. "
                        "Use the voice browser below — it sets the rate "
                        "for you when you install a voice."
                    ),
                ),
                ProviderTip(
                    topic="Runtime: pick Persistent for real meetings",
                    body=(
                        "The Runtime selector at the top trades latency "
                        "for isolation. Subprocess spawns a fresh piper "
                        "per call — ~200-400 ms of ONNX cold-start every "
                        "turn — and is the safe single-step-debug default. "
                        "Persistent loads the voice once and keeps the "
                        "ONNX session warm in-process, so the second and "
                        "later calls return first audio in ~40-60 ms on "
                        "CPU; this is the path to sub-100 ms TTFA in live "
                        "meetings. HTTP sidecar runs piper on the macOS "
                        "host (start with ./scripts/start-piper-sidecar.sh) "
                        "for process isolation or future macOS-native "
                        "voices. piper-tts 1.x has no long-running CLI or "
                        "HTTP mode of its own, so Persistent is in-process "
                        "and the sidecar is a thin piper-library server."
                    ),
                ),
                ProviderTip(
                    topic="chunk_bytes shapes head-of-line delay",
                    body=(
                        "At 22050 Hz / 16-bit the default 4096 bytes is "
                        "~93 ms of audio — the first frame can't leave "
                        "until that much has buffered. Drop to 1024 if "
                        "you want first audio out the door inside 25 ms "
                        "at the cost of more reads. Must be a multiple "
                        "of 2."
                    ),
                ),
                ProviderTip(
                    topic="No audio leaves the host",
                    body=(
                        "Piper runs entirely on-device — no API key, no "
                        "egress, fine on a flight. The trade-off is CPU "
                        "load: on a busy host you may see synthesis "
                        "stall behind whatever else is competing for "
                        "cores."
                    ),
                ),
            ),
        )

    @property
    def default_voice_id(self) -> str | None:
        return self._default_voice_id

    @property
    def model_dir(self) -> str:
        return self._model_dir

    @property
    def binary(self) -> str:
        return self._binary

    @property
    def native_sample_rate(self) -> int:
        return self._native_sample_rate

    @property
    def chunk_bytes(self) -> int:
        return self._chunk_bytes

    @property
    def runtime(self) -> str:
        return self._runtime

    @property
    def sidecar_url(self) -> str:
        return self._sidecar_url

    async def list_voices(self) -> tuple[VoiceMeta, ...]:
        """Return the rhasspy Piper catalog as unified voices (Johnny-1ge.8).

        Fetches the same huggingface index as :func:`fetch_voice_catalog`
        and maps each :class:`VoiceInfo` to the shared :class:`VoiceMeta`
        shape, carrying through the on-disk ``installed`` flag so the picker
        can distinguish ready-to-use voices from ones that download on first
        use. The rich Piper voice-browser (install / remove / preview) still
        drives off the dedicated ``/{id}/voices`` Piper response; this method
        is the provider-agnostic view the unified picker consumes.
        """
        infos = await fetch_voice_catalog(self._model_dir)
        return tuple(_voice_info_to_meta(info) for info in infos)

    async def synthesize_stream(
        self,
        text: str,
        voice_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        """Synthesize ``text`` to 16 kHz mono S16LE PCM frames.

        Dispatches on the configured ``runtime``:

        * ``subprocess`` (default) — a fresh ``piper`` CLI process per call.
          Unchanged historical behaviour; pays ONNX cold-start every turn.
        * ``persistent-subprocess`` — a warm in-process ``PiperVoice`` held in
          the module cache and reused across calls, so only the first pays the
          ~700 ms load (piper-tts 1.x has no streaming-CLI mode, so the warm
          path is in-process rather than a literal long-running child).
        * ``http-sidecar`` — POST the text to a piper sidecar on the host.

        Output is resampled from the voice's native rate to 16 kHz so the
        frames slot directly into the meet-worker audio bridge. Emits one
        ``piper.synth:`` INFO line per call with the time-to-first-audio and
        total wall-clock so a regression to cold loading is visible in
        ``docker logs api``.
        """
        voice = voice_id if voice_id not in (None, "") else self._default_voice_id
        if not voice:
            raise TTSError(
                "PiperTTS requires a voice; pass voice_id explicitly or set "
                "voice_id in the provider configuration."
            )
        stream: AsyncGenerator[bytes, None]
        if self._runtime == RUNTIME_PERSISTENT:
            stream = self._synth_persistent(text, voice)
        elif self._runtime == RUNTIME_HTTP_SIDECAR:
            stream = self._synth_http_sidecar(text, voice)
        else:
            stream = self._synth_subprocess(text, voice)

        start = time.perf_counter()
        ttfa_ms = -1
        try:
            # aclosing() guarantees the inner runtime generator's finally
            # (subprocess cleanup, sidecar drain) runs if the consumer breaks
            # out early and closes this outer generator.
            async with contextlib.aclosing(stream) as inner:
                async for frame in inner:
                    if ttfa_ms < 0:
                        ttfa_ms = int((time.perf_counter() - start) * 1000)
                    yield frame
        finally:
            total_ms = int((time.perf_counter() - start) * 1000)
            logger.info(
                "piper.synth: runtime=%s voice=%s text_chars=%d "
                "ttfa_ms=%d total_ms=%d",
                self._runtime,
                voice,
                len(text),
                ttfa_ms,
                total_ms,
            )

    async def _synth_subprocess(
        self, text: str, voice: str
    ) -> AsyncGenerator[bytes, None]:
        """Runtime A — a fresh ``piper`` CLI subprocess per call.

        The stdin pipe carries the input text and stdout streams raw PCM.
        Pre-flight checks run before any subprocess is spawned so the user
        sees "voice not installed" or "piper binary missing" as a clean error
        rather than the previous opaque "exit code 1". When piper itself fails
        (bad onnx, missing onnxruntime, etc.) the captured stderr tail is
        included in :class:`TTSError`. Behaviour is unchanged from before the
        runtime split.
        """
        model_path = self._resolve_model_path(voice)
        self._preflight_checks(model_path)
        proc = await asyncio.to_thread(self._spawn_process, model_path)
        stderr_buf = _StderrBuffer()
        if proc.stderr is not None:
            stderr_buf.start_draining(proc.stderr)
        try:
            if proc.stdin is None:
                raise TTSError("piper subprocess has no stdin pipe")
            if proc.stdout is None:
                raise TTSError("piper subprocess has no stdout pipe")
            await self._write_text(proc.stdin, text)
            async for frame in self._stream_audio(proc.stdout):
                yield frame
            await self._wait_for_exit(proc, stderr_buf)
        finally:
            await self._cleanup(proc)

    async def _synth_persistent(
        self, text: str, voice: str
    ) -> AsyncGenerator[bytes, None]:
        """Runtime B — warm in-process ``PiperVoice`` from the module cache.

        Loads the voice once (paying the ~700 ms ONNX cold-start), caches it at
        module scope keyed by ``(model_path, native_sample_rate)``, and reuses
        it on every later call so warm time-to-first-audio drops to ~40-60 ms.
        The cached voice's ONNX session is not safe to drive concurrently, so a
        per-voice :class:`asyncio.Lock` serialises use.

        The library's ``synthesize`` is a blocking generator; we pump it on a
        worker thread and hand chunks to the async consumer through a queue as
        they arrive, so the first sentence's audio leaves the box without
        waiting for the whole utterance to finish.
        """
        model_path = self._resolve_model_path(voice)
        self._preflight_model_files(model_path)
        handle = await self._ensure_voice(model_path, voice)

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        sentinel = object()

        def _produce() -> None:
            try:
                for chunk in handle.voice.synthesize(text):
                    pcm = bytes(chunk.audio_int16_bytes)
                    rate = int(getattr(chunk, "sample_rate", 0)) or self._native_sample_rate
                    loop.call_soon_threadsafe(queue.put_nowait, (pcm, rate))
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
                            f"piper persistent synth failed: {item}"
                        ) from item
                    pcm, rate = item
                    out = resample_pcm16(pcm, rate, PCM_SAMPLE_RATE_HZ)
                    if out:
                        yield out
            finally:
                with contextlib.suppress(Exception):
                    await producer

    async def _synth_http_sidecar(
        self, text: str, voice: str
    ) -> AsyncGenerator[bytes, None]:
        """Runtime C — POST the text to a piper sidecar on the macOS host.

        Wire protocol: ``POST {sidecar_url}/synthesize`` with JSON
        ``{"text": ..., "voice": ...}``; the response body is raw S16LE PCM at
        the voice's native rate, advertised via the ``X-Sample-Rate`` header.
        The adapter resamples to 16 kHz, same as the other runtimes. An
        unreachable sidecar raises a :class:`TTSError` naming the start script.
        """
        if not self._sidecar_url:
            raise TTSError(
                f"piper runtime={self._runtime} requires sidecar_url"
            )
        client = self._sidecar_client_or_open()
        url = f"{self._sidecar_url}/synthesize"
        start = time.perf_counter()
        try:
            response = await client.post(
                url,
                json={"text": text, "voice": voice},
                timeout=SIDECAR_HTTP_TIMEOUT_SECONDS,
            )
        except httpx.RequestError as exc:
            raise TTSError(
                f"piper sidecar at {self._sidecar_url} unreachable: {exc}. "
                "Start it with ./scripts/start-piper-sidecar.sh"
            ) from exc
        ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "piper.sidecar: action=request url=%s status=%d ms=%d",
            url,
            response.status_code,
            ms,
        )
        if response.status_code != 200:
            raise TTSError(
                f"piper sidecar returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        pcm = response.content
        try:
            rate = int(response.headers.get("X-Sample-Rate", "") or self._native_sample_rate)
        except ValueError:
            rate = self._native_sample_rate
        extra = len(pcm) % PCM_SAMPLE_WIDTH_BYTES
        if extra:
            pcm = pcm[:-extra]
        out = resample_pcm16(pcm, rate, PCM_SAMPLE_RATE_HZ)
        for i in range(0, len(out), self._chunk_bytes):
            frame = out[i : i + self._chunk_bytes]
            if frame:
                yield frame

    # --- Hooks (overridable in tests) -------------------------------------

    def _resolve_model_path(self, voice: str) -> Path:
        return _resolve_voice_path(self._model_dir, voice)

    def _preflight_model_files(self, model_path: Path) -> None:
        """Verify the voice ``.onnx`` + ``.onnx.json`` files are on disk.

        Surfacing a missing-voice failure *before* loading or spawning piper
        means the user sees a precise "voice X not installed" diagnostic
        instead of an opaque ONNX / subprocess crash. Shared by the subprocess
        and persistent runtimes (the persistent path needs the files but not
        the ``piper`` binary).
        """
        if not model_path.exists():
            sidecar = model_path.with_suffix(model_path.suffix + ".json")
            hint = (
                "click Install on the voice browser above, or POST to "
                "/providers/{id}/voices/{name}/install"
            )
            raise TTSError(
                f"piper voice model not found at {model_path}. "
                f"Expected both {model_path.name} and {sidecar.name} in the "
                f"model directory. To install: {hint}."
            )
        sidecar = model_path.with_suffix(model_path.suffix + ".json")
        if not sidecar.exists():
            raise TTSError(
                f"piper voice sidecar not found at {sidecar}. "
                f"Both <voice>.onnx and <voice>.onnx.json are required."
            )

    def _preflight_checks(self, model_path: Path) -> None:
        """Verify the voice files and the piper binary are reachable.

        Used by the ``subprocess`` runtime, which needs the ``piper``
        executable in addition to the voice files. ``_resolve_binary`` raises
        a clear TTSError when piper is missing from PATH (or the configured
        absolute path is bogus); trigger it here so the message arrives before
        any subprocess noise.
        """
        self._preflight_model_files(model_path)
        self._resolve_binary()

    def _spawn_process(self, model_path: Path) -> _Process:
        binary = self._resolve_binary()
        proc: subprocess.Popen[bytes] = subprocess.Popen(
            [
                binary,
                "--model",
                str(model_path),
                "--output_raw",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        return proc

    def _resolve_binary(self) -> str:
        if os.sep in self._binary or self._binary.startswith("/"):
            return self._binary
        path = shutil.which(self._binary)
        if path is None:
            raise TTSError(
                f"piper binary {self._binary!r} not found on PATH; install "
                "piper-tts (e.g. `pip install piper-tts`) or set the 'binary' "
                "option to its full path"
            )
        return path

    async def _ensure_voice(self, model_path: Path, voice: str) -> _VoiceHandle:
        """Return a warm :class:`_VoiceHandle`, using the module-level cache.

        Fast-path is a lock-free cache hit. On a miss, a per-key load lock
        coalesces concurrent first-loads of the same voice and the global gate
        serialises the heavy ``PiperVoice.load`` across keys. Tests override
        :meth:`_load_voice` to populate the cache with a fake; the autouse
        eviction fixture wipes the cache between tests.
        """
        key: VoiceKey = (str(model_path), self._native_sample_rate)

        with _CACHE_LOCK:
            handle = _VOICES.get(key)
        if handle is not None:
            return handle

        lock = _get_or_make_lock(key)
        async with lock:
            with _CACHE_LOCK:
                handle = _VOICES.get(key)
            if handle is not None:
                return handle
            load_start = time.perf_counter()
            async with _GLOBAL_LOAD_GATE:
                loaded = await asyncio.to_thread(self._load_voice, model_path)
            load_ms = int((time.perf_counter() - load_start) * 1000)
            handle = _VoiceHandle(voice=loaded)
            with _CACHE_LOCK:
                _VOICES[key] = handle
            logger.info(
                "piper.worker: voice=%s action=spawn reason=cache_miss load_ms=%d",
                voice,
                load_ms,
            )
            return handle

    def _load_voice(self, model_path: Path) -> _PiperVoice:
        """Load and return a warm ``PiperVoice``; overridable in tests.

        Imports piper lazily so this module stays importable in lightweight
        test environments without the ``local-tts`` extra installed. The same
        ``piper-tts`` package that ships the ``piper`` CLI exposes
        ``PiperVoice``, so the persistent runtime needs no new dependency.
        """
        try:
            piper_mod = import_module("piper")
        except ImportError as exc:
            raise TTSError(
                "piper-tts library not importable for the persistent runtime: "
                f"{exc}. Install the 'local-tts' extra, or switch the runtime "
                "to 'subprocess'."
            ) from exc
        try:
            return piper_mod.PiperVoice.load(str(model_path))  # type: ignore[no-any-return]
        except Exception as exc:  # noqa: BLE001 — surfaced as a clean TTSError
            raise TTSError(
                f"failed to load piper voice {model_path}: {exc}"
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

        Does **not** evict the process-wide warm-voice cache — the next
        ``PiperTTS(...)`` for the same voice reuses the warm ONNX session
        instead of paying the load cost again. Use :meth:`evict_process_cache`
        for a hard reset.
        """
        if self._sidecar_client is not None:
            with contextlib.suppress(Exception):
                await self._sidecar_client.aclose()
            self._sidecar_client = None

    @classmethod
    def evict_process_cache(cls) -> None:
        """Wipe the process-wide warm-voice cache.

        Used by tests and by a future "reload voices" admin action. The next
        persistent-runtime synth re-runs ``PiperVoice.load``. In-flight synths
        that already hold a ``_VoiceHandle`` reference finish on the old voice.
        """
        _evict_process_cache()

    # --- Internals --------------------------------------------------------

    async def _write_text(self, stdin: IO[bytes], text: str) -> None:
        payload = text.encode("utf-8")
        if not payload.endswith(b"\n"):
            payload = payload + b"\n"
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, stdin.write, payload)
            await loop.run_in_executor(None, stdin.flush)
        except (BrokenPipeError, OSError) as exc:
            raise TTSError(f"piper stdin write failed: {exc}") from exc
        with contextlib.suppress(OSError):
            await loop.run_in_executor(None, stdin.close)

    async def _stream_audio(self, stdout: IO[bytes]) -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        carry = b""
        while True:
            try:
                chunk = await loop.run_in_executor(
                    None, stdout.read, self._chunk_bytes
                )
            except (OSError, ValueError) as exc:
                raise TTSError(f"piper stdout read failed: {exc}") from exc
            if not chunk:
                break
            data = carry + chunk
            extra = len(data) % PCM_SAMPLE_WIDTH_BYTES
            if extra:
                carry = data[-extra:]
                data = data[:-extra]
            else:
                carry = b""
            if not data:
                continue
            out = resample_pcm16(
                data, self._native_sample_rate, PCM_SAMPLE_RATE_HZ
            )
            if out:
                yield out
        if carry:
            logger.debug(
                "discarded %d trailing piper byte(s) (unaligned sample)", len(carry)
            )

    async def _wait_for_exit(
        self, proc: _Process, stderr_buf: _StderrBuffer
    ) -> None:
        try:
            rc = await asyncio.wait_for(
                asyncio.to_thread(proc.wait), timeout=DEFAULT_WAIT_TIMEOUT_S
            )
        except TimeoutError as exc:
            raise TTSError("piper did not exit within timeout") from exc
        if rc:
            await asyncio.to_thread(stderr_buf.join, 1.0)
            err = stderr_buf.text()
            if err:
                raise TTSError(
                    f"piper exited with non-zero code {rc}: {err}"
                )
            raise TTSError(
                f"piper exited with non-zero code {rc} (no stderr captured)"
            )

    async def _cleanup(self, proc: _Process) -> None:
        if proc.poll() is not None:
            return
        with contextlib.suppress(OSError):
            proc.terminate()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(proc.wait), timeout=DEFAULT_TERMINATE_TIMEOUT_S
            )
        except TimeoutError:
            with contextlib.suppress(OSError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(proc.wait)


def register(*, replace: bool = False) -> None:
    """Register :class:`PiperTTS` under ``(ProviderKind.TTS, "piper")``.

    Idempotent if ``replace=True``. Called at import time from
    :mod:`app.providers` so any code that imports the providers package
    can immediately instantiate ``piper`` via the global registry.
    """
    get_registry().register(
        ProviderKind.TTS, PROVIDER_NAME, PiperTTS, replace=replace
    )


__all__ = [
    "ALLOWED_RUNTIMES",
    "DEFAULT_BINARY",
    "DEFAULT_CHUNK_BYTES",
    "DEFAULT_MODEL_DIR",
    "DEFAULT_NATIVE_SAMPLE_RATE_HZ",
    "DEFAULT_RUNTIME",
    "DEFAULT_SIDECAR_URL",
    "PCM_CHANNELS",
    "PIPER_VOICES_CATALOG_URL",
    "PIPER_VOICES_REPO_BASE",
    "PROVIDER_NAME",
    "RUNTIME_HTTP_SIDECAR",
    "RUNTIME_PERSISTENT",
    "RUNTIME_SUBPROCESS",
    "SIDECAR_HTTP_TIMEOUT_SECONDS",
    "PiperTTS",
    "VoiceInfo",
    "download_voice",
    "fetch_voice_catalog",
    "register",
    "voice_is_installed",
]
