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

Latency profile: model load happens once per ``synthesize_stream`` call
(piper exits when stdin closes); time-to-first-audio is dominated by
ONNX startup (~200-400 ms for medium voices on CPU). For lower latency
in production, prefer a persistent piper HTTP server fronted by an
adapter that re-uses a long-lived process.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import logging
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import IO, Protocol

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

logger = logging.getLogger(__name__)

PROVIDER_NAME = "piper"
DEFAULT_BINARY = "piper"
DEFAULT_MODEL_DIR = "/var/lib/johnny/piper-models"
DEFAULT_NATIVE_SAMPLE_RATE_HZ = 22_050
DEFAULT_CHUNK_BYTES = 4_096
DEFAULT_WAIT_TIMEOUT_S = 30.0
DEFAULT_TERMINATE_TIMEOUT_S = 2.0


class _Process(Protocol):
    """The subset of :class:`subprocess.Popen` PiperTTS depends on.

    Mirrors the Protocol used by ``MeetAudioBridge`` so subprocess-driven
    adapters share the same testing pattern (inject a ``BytesIO``-backed
    fake by overriding the spawn hook).
    """

    stdout: IO[bytes] | None
    stdin: IO[bytes] | None

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


def _resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample 16-bit signed LE mono PCM via linear interpolation.

    Pure-stdlib so the adapter ships without numpy / scipy. Mirrors the
    same algorithm used by :func:`johnny.meet_worker.audio_bridge.resample_pcm16`;
    duplicated here to keep ``app/providers/`` free of meet-worker imports
    (the API container ships providers without the meet-worker package).
    """
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError(
            f"sample rates must be positive: src={src_rate} dst={dst_rate}"
        )
    if len(pcm) % PCM_SAMPLE_WIDTH_BYTES:
        raise ValueError("PCM byte length must be even for 16-bit samples")
    if not pcm or src_rate == dst_rate:
        return pcm

    samples = array.array("h")
    samples.frombytes(pcm)
    src_len = len(samples)
    dst_len = max(1, round(src_len * dst_rate / src_rate))
    out = array.array("h", [0] * dst_len)

    if src_len == 1 or dst_len == 1:
        out[0] = samples[0]
        return out.tobytes()

    scale = (src_len - 1) / (dst_len - 1)
    for i in range(dst_len):
        src_idx = i * scale
        idx0 = int(src_idx)
        idx1 = idx0 + 1 if idx0 + 1 < src_len else idx0
        frac = src_idx - idx0
        value = samples[idx0] * (1.0 - frac) + samples[idx1] * frac
        if value > 32767.0:
            value = 32767.0
        elif value < -32768.0:
            value = -32768.0
        out[i] = int(value)

    return out.tobytes()


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
        """
        voice = voice_id if voice_id not in (None, "") else self._default_voice_id
        if not voice:
            raise TTSError(
                "PiperTTS requires a voice; pass voice_id explicitly or set "
                "voice_id in the provider configuration."
            )
        model_path = self._resolve_model_path(voice)
        proc = await asyncio.to_thread(self._spawn_process, model_path)
        try:
            if proc.stdin is None:
                raise TTSError("piper subprocess has no stdin pipe")
            if proc.stdout is None:
                raise TTSError("piper subprocess has no stdout pipe")
            await self._write_text(proc.stdin, text)
            async for frame in self._stream_audio(proc.stdout):
                yield frame
            await self._wait_for_exit(proc)
        finally:
            await self._cleanup(proc)

    # --- Hooks (overridable in tests) -------------------------------------

    def _resolve_model_path(self, voice: str) -> Path:
        return _resolve_voice_path(self._model_dir, voice)

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
            stderr=subprocess.DEVNULL,
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
                "piper-tts or set the 'binary' option to its full path"
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
            out = _resample_pcm16(
                data, self._native_sample_rate, PCM_SAMPLE_RATE_HZ
            )
            if out:
                yield out
        if carry:
            logger.debug(
                "discarded %d trailing piper byte(s) (unaligned sample)", len(carry)
            )

    async def _wait_for_exit(self, proc: _Process) -> None:
        try:
            rc = await asyncio.wait_for(
                asyncio.to_thread(proc.wait), timeout=DEFAULT_WAIT_TIMEOUT_S
            )
        except TimeoutError as exc:
            raise TTSError("piper did not exit within timeout") from exc
        if rc:
            raise TTSError(f"piper exited with non-zero code {rc}")

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
    "PROVIDER_NAME",
    "PiperTTS",
    "register",
]
