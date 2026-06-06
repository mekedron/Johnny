"""Behaviour tests for the real-time-paced scripted transport."""

from __future__ import annotations

import asyncio

from johnny.e2e.interrupt.audio import BYTES_PER_FRAME, silence_frame
from johnny.e2e.interrupt.transport import (
    PacedScriptedTransport,
    TaggedFrame,
)


async def _consume(transport: PacedScriptedTransport) -> list[bytes]:
    captured: list[bytes] = []
    async for frame in transport.capture_frames():
        captured.append(frame)
    return captured


async def test_capture_yields_every_scripted_frame() -> None:
    script = [
        TaggedFrame(pcm=silence_frame(), event_tag="a"),
        TaggedFrame(pcm=silence_frame(), event_tag="b"),
        TaggedFrame(pcm=silence_frame(), event_tag="b"),
    ]
    transport = PacedScriptedTransport(script=script, time_scale=0.0)
    captured = await _consume(transport)
    assert captured == [silence_frame()] * 3
    assert [f.event_tag for f in transport.capture_log.frames] == ["a", "b", "b"]


async def test_capture_pads_short_frames_to_full_frame_size() -> None:
    short = b"\x00" * 10
    transport = PacedScriptedTransport(
        script=[TaggedFrame(pcm=short, event_tag="x")],
        time_scale=0.0,
    )
    captured = await _consume(transport)
    assert len(captured) == 1
    assert len(captured[0]) == BYTES_PER_FRAME


async def test_play_frames_records_iterable_and_async_iterable() -> None:
    transport = PacedScriptedTransport(script=[], time_scale=0.0)
    from collections.abc import AsyncIterator

    # Iterable path.
    await transport.play_frames([b"a", b"b"], source_rate=16_000)

    async def gen() -> AsyncIterator[bytes]:
        yield b"c"
        yield b"d"

    await transport.play_frames(gen(), source_rate=16_000)

    played_pcm = [p.pcm for p in transport.played]
    assert played_pcm == [b"a", b"b", b"c", b"d"]


async def test_play_frames_timestamps_are_monotonic() -> None:
    transport = PacedScriptedTransport(script=[], time_scale=0.0)
    await transport.play_frames([b"a", b"b", b"c"])
    stamps = [p.monotonic_at for p in transport.played]
    assert stamps == sorted(stamps)


async def test_capture_log_tag_lookup_helpers() -> None:
    script = [
        TaggedFrame(pcm=silence_frame(), event_tag="prompt"),
        TaggedFrame(pcm=silence_frame(), event_tag="prompt"),
        TaggedFrame(pcm=silence_frame(), event_tag="interrupt"),
        TaggedFrame(pcm=silence_frame(), event_tag="interrupt"),
    ]
    transport = PacedScriptedTransport(script=script, time_scale=0.0)
    await _consume(transport)

    log = transport.capture_log
    first_interrupt = log.first_monotonic_for_tag("interrupt")
    last_interrupt = log.last_monotonic_for_tag("interrupt")
    assert first_interrupt is not None
    assert last_interrupt is not None
    assert first_interrupt <= last_interrupt
    assert log.first_monotonic_for_tag("nope") is None
    assert log.last_monotonic_for_tag("nope") is None


async def test_real_time_scale_actually_paces_frames() -> None:
    """At time_scale=1.0 a 5-frame script takes ~5 frame periods to drain.

    Uses a very short frame_duration_ms so the assertion stays fast even
    at real-time scale. The check is sub-second, not full production.
    """
    script = [
        TaggedFrame(pcm=silence_frame(), event_tag=f"f{i}") for i in range(5)
    ]
    transport = PacedScriptedTransport(
        script=script,
        frame_duration_ms=10,
        time_scale=1.0,
    )
    start = asyncio.get_running_loop().time()
    await _consume(transport)
    elapsed = asyncio.get_running_loop().time() - start
    # 5 frames * 10ms = 50ms. Allow 2x slack for scheduler jitter.
    assert 0.03 <= elapsed <= 0.5, f"unexpected elapsed: {elapsed:.3f}s"
