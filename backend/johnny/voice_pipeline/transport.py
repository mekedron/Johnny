"""Transport abstraction: where the pipeline gets audio in and pushes audio out.

The pipeline is transport-agnostic. The default :class:`LocalAudioTransport`
bridges to :class:`MeetAudioBridge` (PulseAudio inside the meet-worker
container). The same interface is implemented by US-025's
``LiveKitTransport`` so the pipeline can run inside a LiveKit room when
stronger realtime infra is wanted; the only thing that changes is the
transport instance handed to :class:`VoicePipeline`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterable, AsyncIterator, Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from johnny.meet_worker.audio_bridge import MeetAudioBridge


class JohnnyTransport(ABC):
    """Bidirectional audio transport for the voice pipeline.

    Capture and playback both run as long-lived async coroutines. The
    transport owns the audio devices / sockets; the pipeline owns the
    decision logic.

    All PCM is 16 kHz mono signed-16-bit little-endian unless the
    transport documents otherwise (in which case the pipeline must be
    configured to match).
    """

    @property
    @abstractmethod
    def sample_rate(self) -> int:
        """Sample rate (Hz) of capture and playback streams."""

    @abstractmethod
    async def start(self) -> None:
        """Open devices / connect to the remote endpoint."""

    @abstractmethod
    async def stop(self) -> None:
        """Cleanly tear down the transport. Idempotent."""

    @abstractmethod
    def capture_frames(self) -> AsyncIterator[bytes]:
        """Yield PCM frames from the meeting (browser speaker / room mic)."""

    @abstractmethod
    async def play_frames(
        self,
        frames: Iterable[bytes] | AsyncIterable[bytes],
        source_rate: int | None = None,
    ) -> None:
        """Push PCM frames to the meeting (virtual mic / room speaker)."""

    async def __aenter__(self) -> JohnnyTransport:
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.stop()


class LocalAudioTransport(JohnnyTransport):
    """Bridge :class:`MeetAudioBridge` (PulseAudio) to the pipeline.

    The bridge already exposes ``capture_frames()`` / ``play_frames()`` /
    ``start()`` / ``stop()`` with the same shape, so this class is a thin
    delegating wrapper. Owning the bridge here keeps the pipeline from
    importing meet-worker internals directly.
    """

    def __init__(self, bridge: MeetAudioBridge) -> None:
        self._bridge = bridge

    @property
    def sample_rate(self) -> int:
        return self._bridge.sample_rate

    @property
    def bridge(self) -> MeetAudioBridge:
        return self._bridge

    async def start(self) -> None:
        await self._bridge.start()

    async def stop(self) -> None:
        await self._bridge.stop()

    def capture_frames(self) -> AsyncIterator[bytes]:
        return self._bridge.capture_frames()

    async def play_frames(
        self,
        frames: Iterable[bytes] | AsyncIterable[bytes],
        source_rate: int | None = None,
    ) -> None:
        await self._bridge.play_frames(frames, source_rate=source_rate)


__all__ = [
    "JohnnyTransport",
    "LocalAudioTransport",
]
