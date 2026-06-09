"""LiveKit audio I/O over :class:`BrowserAudioTransport` (Johnny-7g5.1).

The in-browser playground streams raw 16 kHz mono S16LE PCM with the API
process over a WebSocket (:class:`~johnny.voice_pipeline.browser_transport.BrowserAudioTransport`).
The Meet path binds an :class:`~livekit.agents.AgentSession` to a LiveKit room
via ``RoomIO``; the playground has no room, so this module is the roomless
replacement — two adapters that plug the transport directly into the session's
``input.audio`` / ``output.audio`` seams (verified ``livekit-agents==1.5.17``:
``AgentSession.start`` only builds a ``RoomIO`` when a ``room`` is given, and
forwards ``input.audio`` frames to the activity / drains ``output.audio`` when
they are set):

* :class:`BrowserAudioInput` — an :class:`~livekit.agents.voice.io.AudioInput`
  async-iterating ``transport.capture_frames()`` (inbound mic PCM) into
  ``rtc.AudioFrame``\\s the STT/VAD/turn-detector consume.
* :class:`BrowserAudioOutput` — an :class:`~livekit.agents.voice.io.AudioOutput`
  whose ``capture_frame`` queues the bot's TTS PCM onto the transport's playback
  queue (the WebSocket endpoint drains it to the browser).

**Playout timing is estimated.** The browser gives no playback-finished
feedback (unlike a LiveKit ``rtc.AudioSource`` in a room), but the session must
learn when a reply finished playing or the reply ``SpeechHandle`` never
completes and its INV-1 terminal is never emitted (the gate's
``_on_reply_done``). So :class:`BrowserAudioOutput` fires ``on_playback_finished``
after the captured audio's real-time duration elapses — the standard "blind
sink" pattern. A barge-in (``clear_buffer``) cuts that short, draining the
transport's playback queue + signalling the browser interrupt exactly as the
legacy split pipeline did, and reports the already-played position.

Requires the ``agent`` extra (``livekit-agents``) and ``livekit.rtc``; imported
only from the browser session runner (:mod:`johnny.agent.browser_session`),
never from the import-safe top-level :mod:`johnny.agent` package.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from livekit import rtc
from livekit.agents.voice.io import AudioInput, AudioOutput, AudioOutputCapabilities

if TYPE_CHECKING:
    from johnny.voice_pipeline.browser_transport import BrowserAudioTransport

logger = logging.getLogger(__name__)

_BYTES_PER_SAMPLE = 2  # S16LE


class BrowserAudioInput(AudioInput):
    """Feed inbound browser mic PCM into the session as ``rtc.AudioFrame``\\s.

    Wraps the transport's :meth:`~johnny.voice_pipeline.browser_transport.BrowserAudioTransport.capture_frames`
    async iterator (raw PCM ``bytes``) and yields one ``rtc.AudioFrame`` per
    non-empty chunk. The session's ``_forward_audio_task`` iterates this and
    pushes each frame to the running activity (VAD → STT node → turn detector).
    When the transport closes (browser disconnect → EOF sentinel) the underlying
    iterator stops, which ends the forward loop — the roomless analogue of a
    remote track unpublishing.
    """

    def __init__(
        self,
        transport: BrowserAudioTransport,
        *,
        num_channels: int = 1,
    ) -> None:
        super().__init__(label="BrowserAudioInput")
        self._transport = transport
        self._num_channels = max(1, num_channels)
        self._frames = transport.capture_frames()

    async def __anext__(self) -> rtc.AudioFrame:
        # Skip empty / sub-sample chunks rather than yield a zero-length frame
        # (the recogniser treats a 0-sample frame as a glitch); StopAsyncIteration
        # from the transport iterator propagates to end the forward loop.
        while True:
            data = await self._frames.__anext__()
            if not data:
                continue
            samples_per_channel = len(data) // (_BYTES_PER_SAMPLE * self._num_channels)
            if samples_per_channel <= 0:
                continue
            return rtc.AudioFrame(
                data=data,
                sample_rate=self._transport.sample_rate,
                num_channels=self._num_channels,
                samples_per_channel=samples_per_channel,
            )


class BrowserAudioOutput(AudioOutput):
    """Queue the bot's TTS PCM onto the transport for the browser to play.

    The reply-generation forwarding task captures TTS frames here via
    :meth:`capture_frame` (already resampled to :attr:`sample_rate`), then calls
    :meth:`flush` once the segment is complete and :meth:`clear_buffer` only when
    the reply was interrupted (verified ``generation._audio_forwarding_task``).
    This mirrors the room output's contract but estimates playout in real time
    because the browser reports none.
    """

    def __init__(self, transport: BrowserAudioTransport) -> None:
        super().__init__(
            label="BrowserAudioOutput",
            next_in_chain=None,
            sample_rate=transport.sample_rate,
            capabilities=AudioOutputCapabilities(pause=False),
        )
        self._transport = transport
        self._segment_active = False
        self._segment_started_at = 0.0
        self._pushed_duration = 0.0
        self._interrupted_event = asyncio.Event()
        self._flush_task: asyncio.Task[None] | None = None

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        """Queue one TTS frame for the browser; track playout duration."""
        await super().capture_frame(frame)
        # Frames arrive at our declared sample_rate (the forwarding task
        # resamples), so source_rate == transport.sample_rate → no re-resample.
        await self._transport.play_frames([bytes(frame.data)], source_rate=frame.sample_rate)
        if not self._segment_active:
            self._segment_active = True
            self._segment_started_at = time.monotonic()
            self.on_playback_started(created_at=time.time())
        self._pushed_duration += frame.duration

    def flush(self) -> None:
        """Mark the segment complete and start the playout-estimate timer."""
        super().flush()
        if not self._segment_active:
            return
        self._segment_active = False
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
        self._flush_task = asyncio.create_task(self._wait_for_playout())

    def clear_buffer(self) -> None:
        """Barge-in: drop queued audio + signal the browser, cut the estimate short.

        Drains the transport's server-side playback queue and pushes an
        ``interrupt`` control message so the browser stops already-scheduled
        buffers (the legacy ``cancel_playback`` path). Setting the interrupt
        event makes the in-flight :meth:`_wait_for_playout` finish immediately
        with ``interrupted=True`` and the position played so far.
        """
        try:
            self._transport.cancel_playback()
        except Exception:
            logger.exception("browser audio output: cancel_playback failed")
        if self._pushed_duration:
            self._interrupted_event.set()

    async def aclose(self) -> None:
        """Cancel a pending playout timer at teardown (best-effort)."""
        task = self._flush_task
        self._flush_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _wait_for_playout(self) -> None:
        """Fire ``on_playback_finished`` once the audio has (estimated) played out.

        Waits the remaining real-time duration of the captured audio, or returns
        early if :meth:`clear_buffer` set the interrupt event. Exactly one
        ``on_playback_finished`` is emitted per captured segment, which is what
        lets the reply ``SpeechHandle`` complete (and the gate emit its terminal).
        """
        elapsed = time.monotonic() - self._segment_started_at
        remaining = max(0.0, self._pushed_duration - elapsed)
        interrupt_task = asyncio.create_task(self._interrupted_event.wait())
        sleep_task = asyncio.create_task(asyncio.sleep(remaining))
        try:
            await asyncio.wait(
                {sleep_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (sleep_task, interrupt_task):
                if not t.done():
                    t.cancel()
        interrupted = self._interrupted_event.is_set()
        if interrupted:
            played = time.monotonic() - self._segment_started_at
            position = min(played, self._pushed_duration)
        else:
            position = self._pushed_duration
        self._pushed_duration = 0.0
        self._interrupted_event.clear()
        self.on_playback_finished(playback_position=position, interrupted=interrupted)


__all__ = ["BrowserAudioInput", "BrowserAudioOutput"]
