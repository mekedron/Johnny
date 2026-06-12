"""Unit tests for the multi-agent playground audio router (Johnny-trt.48).

Pure-asyncio coverage of the group glue: capture mixing (mic fan-out + peer
cross-feed at the tick clock), playback merge, interrupt purging, member
lifecycle (one member ends, the group keeps flowing), and close semantics.
A fast tick (2 ms) keeps the clocked assertions sub-second.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from johnny.voice_pipeline.browser_transport import BrowserAudioTransport
from johnny.voice_pipeline.group_audio import GroupAudioRouter, mix_pcm16

FAST_TICK_S = 0.002


def _pcm(value: int, samples: int) -> bytes:
    return int(value).to_bytes(2, "little", signed=True) * samples


def _samples(frame: bytes) -> list[int]:
    return np.frombuffer(frame, dtype="<i2").tolist()


async def _drain_captures_until(
    transport: BrowserAudioTransport,
    predicate,
    timeout: float = 1.0,
) -> bytes:
    """Pull capture frames until one matches ``predicate`` (skips silence)."""

    async def _scan() -> bytes:
        iterator = transport.capture_frames()
        async for frame in iterator:
            if predicate(frame):
                return frame
        raise AssertionError("capture stream ended before a matching frame")

    return await asyncio.wait_for(_scan(), timeout)


# --- mix_pcm16 ----------------------------------------------------------------


def test_mix_sums_and_pads() -> None:
    out = mix_pcm16([_pcm(100, 2), _pcm(25, 1)], 8)
    assert _samples(out) == [125, 100, 0, 0]


def test_mix_saturates() -> None:
    out = mix_pcm16([_pcm(30000, 2), _pcm(30000, 2)], 4)
    assert _samples(out) == [32767, 32767]
    out = mix_pcm16([_pcm(-30000, 1), _pcm(-30000, 1)], 2)
    assert _samples(out) == [-32768]


def test_mix_empty_is_silence() -> None:
    assert mix_pcm16([], 6) == b"\x00" * 6


# --- capture mixing -------------------------------------------------------------


async def test_mic_frames_fan_out_to_every_member() -> None:
    router = GroupAudioRouter(mix_tick_s=FAST_TICK_S)
    a, b = BrowserAudioTransport(), BrowserAudioTransport()
    router.add_member(1, a)
    router.add_member(2, b)
    try:
        marker = _pcm(777, 64)
        router.push_capture_frame(marker)
        for transport in (a, b):
            frame = await _drain_captures_until(
                transport, lambda f: 777 in _samples(f)
            )
            assert _samples(frame)[0] == 777
    finally:
        router.close()


async def test_peer_audio_reaches_other_members_but_not_self() -> None:
    router = GroupAudioRouter(mix_tick_s=FAST_TICK_S)
    a, b = BrowserAudioTransport(), BrowserAudioTransport()
    router.add_member(1, a)
    router.add_member(2, b)
    try:
        # Member 1 "speaks": its transport's playback queue is the source the
        # router drains, merges, and cross-feeds.
        await a.play_frames([_pcm(555, 2048)])
        heard = await _drain_captures_until(b, lambda f: 555 in _samples(f))
        assert 555 in _samples(heard)

        # Member 1 must never hear itself: give the mixer time to tick a few
        # frames, then scan everything queued so far for the marker.
        await asyncio.sleep(FAST_TICK_S * 10)
        own_frames: list[bytes] = []
        while not a._capture_q.empty():  # noqa: SLF001 — test introspection
            item = a._capture_q.get_nowait()  # noqa: SLF001
            if item:
                own_frames.append(item)
        assert all(555 not in _samples(f) for f in own_frames)
    finally:
        router.close()


async def test_mic_and_peer_audio_are_sample_added() -> None:
    router = GroupAudioRouter(mix_tick_s=FAST_TICK_S)
    a, b = BrowserAudioTransport(), BrowserAudioTransport()
    router.add_member(1, a)
    router.add_member(2, b)
    try:
        # Pre-load both sources before the next tick consumes them: the mic
        # and member 1's speech must land in ONE mixed frame for member 2.
        await a.play_frames([_pcm(100, 4096)])
        router.push_capture_frame(_pcm(11, 4096))
        mixed = await _drain_captures_until(b, lambda f: 111 in _samples(f))
        assert 111 in _samples(mixed)
    finally:
        router.close()


# --- playback merge ---------------------------------------------------------------


async def test_playback_merges_member_streams_in_arrival_order() -> None:
    taps: list[tuple[int, bytes]] = []
    router = GroupAudioRouter(
        mix_tick_s=FAST_TICK_S, on_playback_frame=lambda m, f: taps.append((m, f))
    )
    a, b = BrowserAudioTransport(), BrowserAudioTransport()
    router.add_member(1, a)
    router.add_member(2, b)
    try:
        await a.play_frames([b"AA"])
        out = router.drain_playback_frames()
        assert await asyncio.wait_for(out.__anext__(), 1.0) == b"AA"
        await b.play_frames([b"BB"])
        assert await asyncio.wait_for(out.__anext__(), 1.0) == b"BB"
        assert taps == [(1, b"AA"), (2, b"BB")]
    finally:
        router.close()


async def test_member_interrupt_purges_its_queued_audio_and_tags_control() -> None:
    router = GroupAudioRouter(mix_tick_s=FAST_TICK_S)
    a, b = BrowserAudioTransport(), BrowserAudioTransport()
    router.add_member(1, a)
    router.add_member(2, b)
    try:
        await a.play_frames([b"CUT1", b"CUT2"])
        await b.play_frames([b"KEEP"])
        # Wait until the drains have merged all three frames.
        deadline = asyncio.get_event_loop().time() + 1.0
        while router._out_q.qsize() < 3:  # noqa: SLF001 — test introspection
            assert asyncio.get_event_loop().time() < deadline
            await asyncio.sleep(0.005)

        a.cancel_playback()  # the member-side stop/barge-in path
        ctrl = router.drain_control_messages()
        msg = await asyncio.wait_for(ctrl.__anext__(), 1.0)
        assert msg["type"] == "interrupt"
        assert msg["member"] == 1

        out = router.drain_playback_frames()
        frame = await asyncio.wait_for(out.__anext__(), 1.0)
        assert frame == b"KEEP"
    finally:
        router.close()


# --- lifecycle ----------------------------------------------------------------------


async def test_remove_member_keeps_group_flowing() -> None:
    router = GroupAudioRouter(mix_tick_s=FAST_TICK_S)
    a, b = BrowserAudioTransport(), BrowserAudioTransport()
    router.add_member(1, a)
    router.add_member(2, b)
    try:
        router.remove_member(1)
        assert router.member_ids == [2]
        # Mic still reaches the survivor...
        router.push_capture_frame(_pcm(333, 64))
        frame = await _drain_captures_until(b, lambda f: 333 in _samples(f))
        assert 333 in _samples(frame)
        # ...and the survivor's playback still merges out.
        await b.play_frames([b"STILL"])
        out = router.drain_playback_frames()
        assert await asyncio.wait_for(out.__anext__(), 1.0) == b"STILL"
    finally:
        router.close()


async def test_close_ends_both_outbound_iterators() -> None:
    router = GroupAudioRouter(mix_tick_s=FAST_TICK_S)
    a = BrowserAudioTransport()
    router.add_member(1, a)
    router.notify_ended("group ended")
    router.close()
    ctrl = router.drain_control_messages()
    msg = await asyncio.wait_for(ctrl.__anext__(), 1.0)
    assert msg == {"type": "ended", "reason": "group ended"}
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(ctrl.__anext__(), 1.0)
    out = router.drain_playback_frames()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(out.__anext__(), 1.0)
    # Idempotent + inert after close.
    router.close()
    router.push_capture_frame(b"xx")
    assert router.member_count == 0


async def test_mic_buffer_is_bounded() -> None:
    router = GroupAudioRouter(mix_tick_s=FAST_TICK_S)
    try:
        big = b"\x01\x00" * 16_000  # 1 s of audio per push
        for _ in range(5):
            router.push_capture_frame(big)
        assert len(router._mic_buf) <= 32_000  # noqa: SLF001 — 1 s cap
    finally:
        router.close()
