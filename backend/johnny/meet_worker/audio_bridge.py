"""Audio bridge between PulseAudio (Google Meet) and the asyncio voice pipeline.

The meet-worker container exposes two PulseAudio devices set up by
``meet-worker-entrypoint.sh``:

* ``<sink_name>``           — null sink the browser plays into; its
  ``.monitor`` is the capture stream.
* ``<source_name>_loopback`` — null sink whose monitor is remapped to
  ``<source_name>`` (the virtual microphone the browser sees). We play
  audio into the loopback sink and the browser hears it as mic input.

This module wraps ``parec`` (capture) and ``pacat`` (playback) so the
rest of the pipeline only sees asyncio primitives: an async generator
of fixed-size PCM frames for capture, and a coroutine that accepts an
(async) iterable of PCM frames for playback. Audio is normalised to
16 kHz mono S16LE — the canonical format for Silero VAD and the STT
adapters in US-022.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import logging
import os
import subprocess
from collections.abc import AsyncIterable, AsyncIterator, Iterable
from typing import IO, Protocol

TARGET_SAMPLE_RATE = 16_000
TARGET_CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2  # s16le
DEFAULT_FRAME_DURATION_MS = 20
DEFAULT_SINK_NAME = "johnny_speaker"
DEFAULT_SOURCE_NAME = "johnny_mic"
DEFAULT_QUEUE_MAX_FRAMES = 100

logger = logging.getLogger(__name__)


def frame_byte_size(
    sample_rate: int = TARGET_SAMPLE_RATE,
    frame_duration_ms: int = DEFAULT_FRAME_DURATION_MS,
    channels: int = TARGET_CHANNELS,
) -> int:
    """Bytes per audio frame for S16LE PCM at the given parameters."""
    if sample_rate <= 0 or frame_duration_ms <= 0 or channels <= 0:
        raise ValueError(
            "sample_rate, frame_duration_ms, and channels must all be positive"
        )
    samples = sample_rate * frame_duration_ms // 1000
    return samples * channels * SAMPLE_WIDTH_BYTES


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample 16-bit signed little-endian mono PCM via linear interpolation.

    Pure-stdlib (uses :mod:`array`) so the meet-worker image needs no
    extra Python deps. Adequate for sub-second chunks at common voice
    rates (8k/16k/22.05k/24k/48k); not anti-aliased, so aggressive
    downsampling will introduce some high-frequency artifacts.
    """
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError(
            f"sample rates must be positive: src={src_rate} dst={dst_rate}"
        )
    if len(pcm) % SAMPLE_WIDTH_BYTES:
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


class _Process(Protocol):
    """The subset of :class:`subprocess.Popen` the bridge depends on."""

    stdout: IO[bytes] | None
    stdin: IO[bytes] | None

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


class MeetAudioBridge:
    """Bridge meeting audio between PulseAudio and an asyncio pipeline.

    Spawns ``parec`` to capture from ``<sink_name>.monitor`` (the
    browser's speakers) and ``pacat`` to feed ``<source_name>_loopback``
    (the null-sink whose monitor is remapped into the browser's virtual
    microphone — see ``meet-worker-entrypoint.sh``).

    The capture side reads fixed-size frames into a bounded asyncio
    queue; when the queue fills, the oldest frame is dropped so the
    consumer always sees recent audio. The playback side resamples on
    demand if the producer's sample rate differs from the bridge's
    configured rate, and silently drops frames if the underlying pipe
    closes (treated as session-ended).
    """

    def __init__(
        self,
        sink_name: str | None = None,
        source_name: str | None = None,
        sample_rate: int = TARGET_SAMPLE_RATE,
        frame_duration_ms: int = DEFAULT_FRAME_DURATION_MS,
        queue_max_frames: int = DEFAULT_QUEUE_MAX_FRAMES,
    ) -> None:
        if sample_rate <= 0 or frame_duration_ms <= 0 or queue_max_frames <= 0:
            raise ValueError(
                "sample_rate, frame_duration_ms, and queue_max_frames must all be positive"
            )
        self.sink_name = sink_name or os.environ.get(
            "JOHNNY_SINK_NAME", DEFAULT_SINK_NAME
        )
        self.source_name = source_name or os.environ.get(
            "JOHNNY_SOURCE_NAME", DEFAULT_SOURCE_NAME
        )
        self.sample_rate = sample_rate
        self.frame_duration_ms = frame_duration_ms
        self.samples_per_frame = sample_rate * frame_duration_ms // 1000
        self.bytes_per_frame = frame_byte_size(
            sample_rate=sample_rate,
            frame_duration_ms=frame_duration_ms,
            channels=TARGET_CHANNELS,
        )
        self.queue_max_frames = queue_max_frames

        self._capture_proc: _Process | None = None
        self._playback_proc: _Process | None = None
        # +1 reserves a slot for the None EOS sentinel even when full.
        self._capture_queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=queue_max_frames + 1
        )
        self._capture_task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        """Spawn the capture and playback subprocesses and start pumping."""
        if self._running:
            return
        self._capture_proc = self._spawn_capture_process()
        self._playback_proc = self._spawn_playback_process()
        self._capture_task = asyncio.create_task(self._pump_capture())
        self._running = True

    def _spawn_capture_process(self) -> _Process:
        proc: subprocess.Popen[bytes] = subprocess.Popen(
            [
                "parec",
                f"--device={self.sink_name}.monitor",
                f"--rate={self.sample_rate}",
                f"--channels={TARGET_CHANNELS}",
                "--format=s16le",
                "--raw",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        return proc

    def _spawn_playback_process(self) -> _Process:
        proc: subprocess.Popen[bytes] = subprocess.Popen(
            [
                "pacat",
                "--playback",
                f"--device={self.source_name}_loopback",
                f"--rate={self.sample_rate}",
                f"--channels={TARGET_CHANNELS}",
                "--format=s16le",
                "--raw",
            ],
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        return proc

    async def _pump_capture(self) -> None:
        proc = self._capture_proc
        assert proc is not None
        stdout = proc.stdout
        if stdout is None:
            with contextlib.suppress(asyncio.QueueFull):
                self._capture_queue.put_nowait(None)
            return
        loop = asyncio.get_running_loop()
        try:
            while True:
                frame = await self._read_full_frame(loop, stdout)
                if frame is None:
                    break
                # Drop oldest user-visible frames so we stay at the cap.
                while self._capture_queue.qsize() >= self.queue_max_frames:
                    try:
                        self._capture_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                with contextlib.suppress(asyncio.QueueFull):
                    self._capture_queue.put_nowait(frame)
        finally:
            with contextlib.suppress(asyncio.QueueFull):
                self._capture_queue.put_nowait(None)

    async def _read_full_frame(
        self, loop: asyncio.AbstractEventLoop, stdout: IO[bytes]
    ) -> bytes | None:
        """Return the next ``bytes_per_frame``-sized chunk or ``None`` on EOF."""
        buf = bytearray()
        while len(buf) < self.bytes_per_frame:
            needed = self.bytes_per_frame - len(buf)
            try:
                chunk = await loop.run_in_executor(None, stdout.read, needed)
            except (OSError, ValueError) as exc:
                logger.warning("capture read failed: %s", exc)
                return None
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    async def capture_frames(self) -> AsyncIterator[bytes]:
        """Yield 16 kHz mono S16LE PCM frames captured from the meeting.

        The iterator exits when the capture subprocess reaches EOF (the
        meeting ended) or :meth:`stop` is called.
        """
        while True:
            frame = await self._capture_queue.get()
            if frame is None:
                return
            yield frame

    async def play_frames(
        self,
        frames: Iterable[bytes] | AsyncIterable[bytes],
        source_rate: int | None = None,
    ) -> None:
        """Write PCM frames to the virtual microphone.

        ``frames`` may be a sync or async iterable of S16LE PCM chunks.
        If ``source_rate`` differs from :attr:`sample_rate`, each chunk
        is resampled before being written. Broken-pipe errors on the
        playback subprocess are swallowed (session treated as ended).
        """
        if isinstance(frames, AsyncIterable):
            async for frame in frames:
                if not await self._write_frame(frame, source_rate):
                    return
            return
        for frame in frames:
            if not await self._write_frame(frame, source_rate):
                return

    async def _write_frame(self, frame: bytes, source_rate: int | None) -> bool:
        proc = self._playback_proc
        if proc is None or proc.stdin is None:
            return False
        data = frame
        if source_rate is not None and source_rate != self.sample_rate:
            data = resample_pcm16(data, source_rate, self.sample_rate)
        if not data:
            return True
        loop = asyncio.get_running_loop()
        stdin = proc.stdin
        try:
            await loop.run_in_executor(None, stdin.write, data)
            await loop.run_in_executor(None, stdin.flush)
        except (BrokenPipeError, OSError) as exc:
            logger.warning("playback write failed: %s", exc)
            return False
        return True

    async def stop(self) -> None:
        """Cancel the pump and terminate both subprocesses."""
        if not self._running:
            return
        self._running = False

        if self._capture_task is not None:
            self._capture_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._capture_task
            self._capture_task = None

        # Close playback stdin so pacat sees end-of-stream before TERM.
        if self._playback_proc is not None and self._playback_proc.stdin is not None:
            with contextlib.suppress(OSError):
                self._playback_proc.stdin.close()

        await self._terminate_process(self._capture_proc)
        await self._terminate_process(self._playback_proc)
        self._capture_proc = None
        self._playback_proc = None

    async def _terminate_process(self, proc: _Process | None) -> None:
        if proc is None or proc.poll() is not None:
            return
        with contextlib.suppress(OSError):
            proc.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=2.0)
        except TimeoutError:
            with contextlib.suppress(OSError):
                proc.kill()

    async def __aenter__(self) -> MeetAudioBridge:
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.stop()
