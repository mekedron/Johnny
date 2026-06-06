"""Browser-backed :class:`JohnnyTransport` (Johnny-ckz.6).

The in-browser voice surface speaks raw 16 kHz mono S16LE PCM with the
API process over a WebSocket. This transport is the pipeline-side
adapter: the WebSocket endpoint pushes inbound frames in via
:meth:`push_capture_frame` and pulls outbound TTS frames out via
:meth:`drain_playback_frames`. The pipeline itself is unchanged — only
the transport instance handed to :class:`VoicePipeline` differs.

Design notes
------------

* **Bidirectional async queues**. Capture is a bounded queue that drops
  oldest frames when full (mirrors :class:`MeetAudioBridge` /
  :class:`LiveKitTransport` — for a live UI, stale audio is worse than
  missing audio).
* **Playback is also a queue**, but unbounded — the WebSocket endpoint
  drains it as fast as the browser can decode. TTS bursts are usually
  short so the unbounded queue is fine.
* **End-of-stream sentinel**. The capture queue accepts ``None`` as the
  end-of-stream marker. When the WebSocket disconnects, the endpoint
  pushes ``None`` and :meth:`capture_frames` stops yielding, which
  cleanly drains the pipeline's transcribe loop.
* **No infrastructure dependencies**. Unlike :class:`LiveKitTransport`,
  this transport has no SDK to import — it's pure asyncio.

The WebSocket wire format is the same raw PCM the pipeline expects, no
JSON wrapping. A single frame is ``frame_duration_ms * sample_rate /
1000 * 2`` bytes (40 bytes at 20 ms / 16 kHz). The browser side
buffers AudioWorklet output into matching chunks before sending.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterable, AsyncIterator, Iterable

from app.providers.base import PCM_SAMPLE_RATE_HZ
from johnny.meet_worker.audio_bridge import resample_pcm16
from johnny.voice_pipeline.transport import JohnnyTransport

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_RATE = PCM_SAMPLE_RATE_HZ
DEFAULT_CAPTURE_QUEUE_MAX_FRAMES = 200
"""Max inbound frames buffered before the oldest is dropped.

At 20 ms/frame that's 4 s of audio — enough to absorb a brief WebSocket
stall without letting the pipeline lag the user. Drops are logged at
DEBUG so we can spot persistent overruns without noisy production logs.
"""


class BrowserAudioTransport(JohnnyTransport):
    """A :class:`JohnnyTransport` that streams raw PCM over a WebSocket.

    Owned by the per-session pipeline runner; the WebSocket endpoint
    holds a reference and feeds it through :meth:`push_capture_frame`
    (inbound mic audio) / :meth:`drain_playback_frames` (outbound TTS
    audio). Both directions cleanly tear down via :meth:`close_capture`
    so the pipeline's run loop exits when the browser disconnects.

    The transport does NOT own the WebSocket — the endpoint does. This
    keeps the pipeline pure async and lets us swap WebSocket frameworks
    (Starlette, FastAPI, custom) without touching the pipeline.
    """

    def __init__(
        self,
        *,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        capture_queue_max: int = DEFAULT_CAPTURE_QUEUE_MAX_FRAMES,
    ) -> None:
        self._sample_rate = sample_rate
        self._capture_queue_max = max(1, capture_queue_max)
        # Use ``Queue[bytes | None]`` so we can signal end-of-stream.
        # ``maxsize=0`` would mean unbounded; we WANT a bound for
        # backpressure / oldest-drop semantics.
        self._capture_q: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=self._capture_queue_max
        )
        self._playback_q: asyncio.Queue[bytes] = asyncio.Queue()
        self._started = False
        self._closed = False
        self._capture_drop_count = 0

    # ------------------------------------------------------------------
    # JohnnyTransport interface

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def start(self) -> None:
        """Mark the transport open. No external resources to allocate."""
        self._started = True

    async def stop(self) -> None:
        """Close the capture stream so the pipeline drains cleanly. Idempotent.

        Sets the ``_closed`` flag and pokes the iterator awake by
        pushing the EOF sentinel. If the queue is full we drain
        existing items first into a holding list and re-queue them so
        no audio is lost just because the stream was asked to stop. The
        consumer will see every still-queued frame and then the EOF.
        """
        if self._closed:
            return
        self._closed = True
        held: list[bytes | None] = []
        while True:
            try:
                held.append(self._capture_q.get_nowait())
            except asyncio.QueueEmpty:
                break
        for item in held:
            try:
                self._capture_q.put_nowait(item)
            except asyncio.QueueFull:  # pragma: no cover — drained above
                break
        # Final EOF sentinel; queue must have room since we drained it,
        # and queue maxsize is at least 1.
        try:
            self._capture_q.put_nowait(None)
        except asyncio.QueueFull:  # pragma: no cover — only fires if
            # consumer raced and refilled the queue before we got the
            # sentinel in. Safe to skip: the next .get() either returns
            # a held item (eventually consumed) or the consumer notices
            # `_closed` via the iterator's secondary check.
            pass

    def capture_frames(self) -> AsyncIterator[bytes]:
        """Yield inbound PCM frames until the stream closes."""
        return self._capture_iter()

    async def _capture_iter(self) -> AsyncIterator[bytes]:
        while True:
            # If the producer already closed and queued nothing else,
            # an awaited get() would block forever — short-circuit when
            # we see the closed flag with an empty queue.
            if self._closed and self._capture_q.empty():
                return
            frame = await self._capture_q.get()
            if frame is None:
                return
            yield frame

    async def play_frames(
        self,
        frames: Iterable[bytes] | AsyncIterable[bytes],
        source_rate: int | None = None,
    ) -> None:
        """Buffer outbound PCM frames for the WebSocket endpoint to drain.

        ``source_rate`` follows the same contract as the other transports:
        when the producer (TTS) emits at a different sample rate than
        the transport, resample to the transport's rate before queuing.
        """
        if isinstance(frames, AsyncIterable):
            async for frame in frames:
                self._enqueue_playback(frame, source_rate)
        else:
            for frame in frames:
                self._enqueue_playback(frame, source_rate)

    def _enqueue_playback(self, frame: bytes, source_rate: int | None) -> None:
        if not frame:
            return
        if source_rate is not None and source_rate != self._sample_rate:
            frame = resample_pcm16(
                frame, src_rate=source_rate, dst_rate=self._sample_rate
            )
            if not frame:
                return
        self._playback_q.put_nowait(frame)

    # ------------------------------------------------------------------
    # WebSocket endpoint interface

    def push_capture_frame(self, frame: bytes) -> None:
        """Hand one inbound PCM frame to the pipeline.

        Drops the oldest queued frame if the queue is full — keeps
        latency steady at the cost of a few-millisecond gap; for a live
        voice UI this is the right tradeoff. Called from the WebSocket
        receive loop; never blocks.
        """
        if self._closed or not frame:
            return
        try:
            self._capture_q.put_nowait(frame)
            return
        except asyncio.QueueFull:
            pass
        # Drop the oldest frame, then push the new one. If the drop
        # races with the consumer we'll just try again next call —
        # don't loop forever here.
        try:
            self._capture_q.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover — defensive
            pass
        try:
            self._capture_q.put_nowait(frame)
            self._capture_drop_count += 1
            logger.debug(
                "browser transport: dropped 1 inbound frame (drops=%d)",
                self._capture_drop_count,
            )
        except asyncio.QueueFull:  # pragma: no cover — defensive
            pass

    async def drain_playback_frames(self) -> AsyncIterator[bytes]:
        """Yield outbound PCM frames until the transport closes.

        The WebSocket endpoint awaits each yield and forwards the frame
        to the browser as a binary message. The iterator exits when
        :meth:`close_playback` is called; the endpoint also doubles up by
        watching its own disconnect event so a slow consumer can't
        wedge the pipeline.
        """
        while True:
            frame = await self._playback_q.get()
            if frame == b"":
                return
            yield frame

    def close_playback(self) -> None:
        """Signal :meth:`drain_playback_frames` to exit. Idempotent."""
        try:
            self._playback_q.put_nowait(b"")
        except asyncio.QueueFull:  # pragma: no cover — unbounded queue
            pass

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def capture_drop_count(self) -> int:
        return self._capture_drop_count


__all__ = [
    "DEFAULT_CAPTURE_QUEUE_MAX_FRAMES",
    "DEFAULT_SAMPLE_RATE",
    "BrowserAudioTransport",
]
