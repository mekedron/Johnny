"""Real-time-paced transport for the interrupt harness (Johnny-2bw).

The unit tests' :class:`_BufferedTransport` yields every frame as fast as the
consumer asks. That's deliberately decoupled from wall-clock time so unit
suites stay fast, but it makes latency-budget assertions impossible — the
fast barge-in trigger can fire well before the response loop has even
scheduled, or hours after; either way the test can't measure the gap.

:class:`PacedScriptedTransport` solves both halves:

* ``capture_frames()`` yields one scripted frame every ``frame_duration_ms``
  with a real ``asyncio.sleep`` between frames. The pipeline's VAD loop now
  sees frames at production cadence; the respond loop has natural interleave
  points to flip ``_response_in_flight=True`` between utterances.
* ``play_frames()`` records each TTS frame with a monotonic timestamp so the
  harness can compute *how long after* speech-onset the bot's TTS was cut.

"Scripted" means the frame list is precomputed from the
:class:`~johnny.e2e.interrupt.scenarios.Scenario` (a sequence of speech /
silence / cough events). Frames are tagged so the runner can correlate
playback timing with the scenario events that produced them.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Iterable
from dataclasses import dataclass, field

from johnny.e2e.interrupt.audio import (
    BYTES_PER_FRAME,
    FRAME_DURATION_MS,
    SAMPLE_RATE_HZ,
)
from johnny.voice_pipeline.transport import JohnnyTransport


@dataclass(frozen=True, slots=True)
class TaggedFrame:
    """One scripted PCM frame plus the event tag that produced it.

    ``event_tag`` is opaque to the transport — the runner uses it to
    correlate playback timing with the scenario events that produced the
    frame (e.g. "speaker turn 1 frame 47").
    """

    pcm: bytes
    event_tag: str


@dataclass(slots=True)
class PlayedFrame:
    """One TTS frame the pipeline pushed back through the transport.

    ``monotonic_at`` is the wall-clock at which the transport received the
    frame; the runner subtracts the speaker's interrupt-onset time to get
    the interrupt-to-cut latency.
    """

    pcm: bytes
    monotonic_at: float


@dataclass(slots=True)
class CaptureLog:
    """Per-frame timestamp log for the capture (speaker→pipeline) side.

    The runner stamps each yielded frame so it can recover the wall-clock
    at which a given scenario event (e.g. the *end* of the interrupt
    utterance) actually reached the pipeline. Without this we couldn't
    compute interrupt-to-cut latency relative to true speaker time.
    """

    frames: list[TaggedFrame] = field(default_factory=list)
    monotonic_at: list[float] = field(default_factory=list)

    def last_monotonic_for_tag(self, tag: str) -> float | None:
        """Return the monotonic time of the LAST frame carrying ``tag``."""
        last: float | None = None
        for i, frame in enumerate(self.frames):
            if frame.event_tag == tag:
                last = self.monotonic_at[i]
        return last

    def first_monotonic_for_tag(self, tag: str) -> float | None:
        """Return the monotonic time of the FIRST frame carrying ``tag``."""
        for i, frame in enumerate(self.frames):
            if frame.event_tag == tag:
                return self.monotonic_at[i]
        return None


class PacedScriptedTransport(JohnnyTransport):
    """Bidirectional transport: scripted speaker in, timestamped TTS out.

    The pacing is *real*: ``asyncio.sleep(frame_duration_ms / 1000)`` between
    each captured frame. With the harness's default 20 ms frames this means
    a 5-second speaker script takes 5 wall-clock seconds — slow by unit-test
    standards but necessary to make latency-budget assertions meaningful.

    The transport is single-shot: each instance plays its script once and
    then returns from ``capture_frames``, triggering the pipeline's
    end-of-stream sentinel and clean shutdown.
    """

    def __init__(
        self,
        script: list[TaggedFrame],
        *,
        frame_duration_ms: int = FRAME_DURATION_MS,
        sample_rate: int = SAMPLE_RATE_HZ,
        time_scale: float = 1.0,
    ) -> None:
        self._script = list(script)
        self._sample_rate = sample_rate
        # Frame duration governs both the structural frame size and the
        # asyncio.sleep between frames. ``time_scale`` lets us speed up
        # the wall-clock for fast smoke runs (1.0 = real time, 0.1 =
        # 10x faster). Latency assertions remain in scaled wall-clock
        # so they stay coherent.
        self._frame_period_s = (frame_duration_ms / 1000.0) * time_scale
        self._capture_log = CaptureLog()
        self._played: list[PlayedFrame] = []
        self._played_source_rate: int | None = None
        self._started = False
        self._stopped = False
        # Used by the runner to align scenario times with monotonic clock.
        self._capture_started_at: float | None = None

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def capture_log(self) -> CaptureLog:
        """Per-frame capture timing — for the runner's latency assertions."""
        return self._capture_log

    @property
    def played(self) -> list[PlayedFrame]:
        """Every TTS frame the pipeline pushed back to us, oldest first."""
        return list(self._played)

    @property
    def capture_started_at(self) -> float | None:
        """Monotonic time at which the first frame was yielded, or ``None``."""
        return self._capture_started_at

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._stopped = True

    async def capture_frames(self) -> AsyncIterator[bytes]:
        """Yield the scripted frames at real time, logging each timestamp."""
        loop = asyncio.get_running_loop()
        next_emit_at = loop.time()
        self._capture_started_at = next_emit_at
        for frame in self._script:
            now = loop.time()
            delay = next_emit_at - now
            if delay > 0:
                await asyncio.sleep(delay)
            stamp = loop.time()
            self._capture_log.frames.append(frame)
            self._capture_log.monotonic_at.append(stamp)
            # Defensive: production frames are exactly BYTES_PER_FRAME.
            # The harness's audio synth always produces clean frames, but
            # padding a short tail lets us reuse this transport for ad-hoc
            # scripts without surprising the VAD.
            if len(frame.pcm) < BYTES_PER_FRAME:
                yield frame.pcm + bytes(BYTES_PER_FRAME - len(frame.pcm))
            else:
                yield frame.pcm
            next_emit_at += self._frame_period_s

    async def play_frames(
        self,
        frames: Iterable[bytes] | AsyncIterable[bytes],
        source_rate: int | None = None,
    ) -> None:
        self._played_source_rate = source_rate
        loop = asyncio.get_running_loop()
        if isinstance(frames, AsyncIterable):
            async for f in frames:
                self._played.append(PlayedFrame(pcm=f, monotonic_at=loop.time()))
        else:
            for f in frames:
                self._played.append(PlayedFrame(pcm=f, monotonic_at=loop.time()))


__all__ = [
    "CaptureLog",
    "PacedScriptedTransport",
    "PlayedFrame",
    "TaggedFrame",
]
