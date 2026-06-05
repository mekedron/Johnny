"""Tests for johnny.voice_pipeline.transport."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Iterable

import pytest

from johnny.voice_pipeline.transport import JohnnyTransport, LocalAudioTransport


def test_cannot_instantiate_johnny_transport_directly() -> None:
    with pytest.raises(TypeError):
        JohnnyTransport()  # type: ignore[abstract]


class _FakeBridge:
    def __init__(self, frames: list[bytes], sample_rate: int = 16_000) -> None:
        self._frames = list(frames)
        self.sample_rate = sample_rate
        self.started = False
        self.stopped = False
        self.played: list[bytes] = []
        self.played_source_rate: int | None = None

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def capture_frames(self) -> AsyncIterator[bytes]:
        for frame in self._frames:
            yield frame

    async def play_frames(
        self,
        frames: Iterable[bytes] | AsyncIterable[bytes],
        source_rate: int | None = None,
    ) -> None:
        self.played_source_rate = source_rate
        if isinstance(frames, AsyncIterable):
            async for f in frames:
                self.played.append(f)
        else:
            for f in frames:
                self.played.append(f)


async def test_local_audio_transport_delegates_lifecycle() -> None:
    bridge = _FakeBridge(frames=[])
    t = LocalAudioTransport(bridge)  # type: ignore[arg-type]
    assert bridge.started is False
    await t.start()
    assert bridge.started is True
    await t.stop()
    assert bridge.stopped is True


async def test_local_audio_transport_capture_yields_frames() -> None:
    bridge = _FakeBridge(frames=[b"a", b"b", b"c"])
    t = LocalAudioTransport(bridge)  # type: ignore[arg-type]
    received: list[bytes] = []
    async for f in t.capture_frames():
        received.append(f)
    assert received == [b"a", b"b", b"c"]


async def test_local_audio_transport_play_pushes_to_bridge() -> None:
    bridge = _FakeBridge(frames=[])
    t = LocalAudioTransport(bridge)  # type: ignore[arg-type]
    await t.play_frames([b"x", b"y"], source_rate=24_000)
    assert bridge.played == [b"x", b"y"]
    assert bridge.played_source_rate == 24_000


async def test_local_audio_transport_exposes_sample_rate() -> None:
    bridge = _FakeBridge(frames=[], sample_rate=8_000)
    t = LocalAudioTransport(bridge)  # type: ignore[arg-type]
    assert t.sample_rate == 8_000


async def test_local_audio_transport_async_context_starts_and_stops() -> None:
    bridge = _FakeBridge(frames=[])
    async with LocalAudioTransport(bridge) as t:  # type: ignore[arg-type]
        assert bridge.started is True
        assert isinstance(t, LocalAudioTransport)
    assert bridge.stopped is True


async def test_local_audio_transport_exposes_bridge() -> None:
    bridge = _FakeBridge(frames=[])
    t = LocalAudioTransport(bridge)  # type: ignore[arg-type]
    assert t.bridge is bridge  # type: ignore[comparison-overlap]


class _CustomTransport(JohnnyTransport):
    """Minimal concrete impl to verify the ABC contract is satisfiable."""

    def __init__(self) -> None:
        self.events: list[str] = []

    @property
    def sample_rate(self) -> int:
        return 16_000

    async def start(self) -> None:
        self.events.append("start")

    async def stop(self) -> None:
        self.events.append("stop")

    async def capture_frames(self) -> AsyncIterator[bytes]:
        for f in [b"hello"]:
            yield f

    async def play_frames(
        self,
        frames: Iterable[bytes] | AsyncIterable[bytes],
        source_rate: int | None = None,
    ) -> None:
        self.events.append("play")


async def test_custom_transport_implements_protocol() -> None:
    t = _CustomTransport()
    async with t:
        captured: list[bytes] = []
        async for f in t.capture_frames():
            captured.append(f)
        await t.play_frames([b"x"])
    assert captured == [b"hello"]
    assert t.events == ["start", "play", "stop"]


async def test_play_frames_accepts_async_iterable() -> None:
    bridge = _FakeBridge(frames=[])
    t = LocalAudioTransport(bridge)  # type: ignore[arg-type]

    async def _async_frames() -> AsyncIterator[bytes]:
        yield b"alpha"
        yield b"beta"

    await t.play_frames(_async_frames())
    assert bridge.played == [b"alpha", b"beta"]


async def test_transport_default_sample_rate_passthrough() -> None:
    bridge = _FakeBridge(frames=[])
    t = LocalAudioTransport(bridge)  # type: ignore[arg-type]
    assert t.sample_rate == 16_000
