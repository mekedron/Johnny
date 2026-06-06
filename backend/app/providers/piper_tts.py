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

Latency profile: model load happens once per ``synthesize_stream`` call
(piper exits when stdin closes); time-to-first-audio is dominated by
ONNX startup (~200-400 ms for medium voices on CPU). For lower latency
in production, prefer a persistent piper HTTP server fronted by an
adapter that re-uses a long-lived process.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import subprocess
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
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
    get_registry,
)
from app.providers.schema import (
    FieldDef,
    FieldGroup,
    FieldType,
    ProviderSchema,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "piper"
DEFAULT_BINARY = "piper"
DEFAULT_MODEL_DIR = "/var/lib/johnny/piper-models"
DEFAULT_NATIVE_SAMPLE_RATE_HZ = 22_050
DEFAULT_CHUNK_BYTES = 4_096
DEFAULT_WAIT_TIMEOUT_S = 30.0
DEFAULT_TERMINATE_TIMEOUT_S = 2.0

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
                    group=FieldGroup.ADVANCED,
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

    async def synthesize_stream(
        self,
        text: str,
        voice_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        """Synthesize ``text`` to 16 kHz mono S16LE PCM frames.

        Spawns a fresh ``piper`` subprocess per call; the stdin pipe
        carries the input text and stdout streams raw PCM. Output is
        resampled from the model's native rate to 16 kHz so the frames
        slot directly into the meet-worker audio bridge.

        Pre-flight checks run before any subprocess is spawned so the
        user sees "voice not installed" or "piper binary missing" as a
        clean error rather than the previous opaque "exit code 1". When
        piper itself fails (bad onnx, missing onnxruntime, etc.) the
        captured stderr tail is included in :class:`TTSError`.
        """
        voice = voice_id if voice_id not in (None, "") else self._default_voice_id
        if not voice:
            raise TTSError(
                "PiperTTS requires a voice; pass voice_id explicitly or set "
                "voice_id in the provider configuration."
            )
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

    # --- Hooks (overridable in tests) -------------------------------------

    def _resolve_model_path(self, voice: str) -> Path:
        return _resolve_voice_path(self._model_dir, voice)

    def _preflight_checks(self, model_path: Path) -> None:
        """Verify the voice file and piper binary are reachable.

        Surfacing these failures *before* spawning piper means the user
        sees a precise diagnostic ("voice X not installed", "piper not on
        PATH") instead of the opaque "exited with non-zero code 1" they
        used to get when the same problems showed up as subprocess
        crashes with stderr suppressed.
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
        # _resolve_binary raises TTSError with its own clear message when
        # piper is missing from PATH (or the configured absolute path is
        # bogus). Trigger that here so the message arrives before any
        # subprocess noise.
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
    "DEFAULT_BINARY",
    "DEFAULT_CHUNK_BYTES",
    "DEFAULT_MODEL_DIR",
    "DEFAULT_NATIVE_SAMPLE_RATE_HZ",
    "PCM_CHANNELS",
    "PIPER_VOICES_CATALOG_URL",
    "PIPER_VOICES_REPO_BASE",
    "PROVIDER_NAME",
    "PiperTTS",
    "VoiceInfo",
    "download_voice",
    "fetch_voice_catalog",
    "register",
    "voice_is_installed",
]
