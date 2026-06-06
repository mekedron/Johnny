"""Tests for :mod:`johnny.voice_pipeline.browser_transport`.

The transport is small + standalone so unit tests can drive the
queues directly without standing up a WebSocket or pipeline. Covers:

* Inbound capture frames flow through ``push_capture_frame`` and out
  through ``capture_frames()`` in order.
* Bounded capture queue drops oldest frames when full (keeping latest
  audio).
* End-of-stream sentinel exits ``capture_frames()`` cleanly.
* Outbound TTS frames queued via ``play_frames`` are drained by the
  WebSocket-side consumer.
* Resampling is applied when a TTS at a different sample rate plays
  through the transport.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from johnny.voice_pipeline import BrowserAudioTransport
from johnny.voice_pipeline.browser_transport import (
    DEFAULT_CAPTURE_QUEUE_MAX_FRAMES,
    DEFAULT_SAMPLE_RATE,
)


@pytest.mark.asyncio
async def test_default_sample_rate_matches_pipeline_pcm() -> None:
    transport = BrowserAudioTransport()
    assert transport.sample_rate == DEFAULT_SAMPLE_RATE == 16_000


@pytest.mark.asyncio
async def test_push_capture_frame_then_iterate_yields_in_order() -> None:
    transport = BrowserAudioTransport()
    await transport.start()
    transport.push_capture_frame(b"\x01\x02")
    transport.push_capture_frame(b"\x03\x04")
    await transport.stop()  # signals EOF via sentinel

    seen: list[bytes] = []
    async for frame in transport.capture_frames():
        seen.append(frame)
    assert seen == [b"\x01\x02", b"\x03\x04"]


@pytest.mark.asyncio
async def test_capture_queue_drops_oldest_when_full() -> None:
    """Queue is bounded; pushing past capacity drops the OLDEST frame."""
    transport = BrowserAudioTransport(capture_queue_max=3)
    await transport.start()
    transport.push_capture_frame(b"\x01")
    transport.push_capture_frame(b"\x02")
    transport.push_capture_frame(b"\x03")
    # The fourth push should drop \x01 and keep \x02, \x03, \x04.
    transport.push_capture_frame(b"\x04")
    assert transport.capture_drop_count == 1
    await transport.stop()

    seen: list[bytes] = []
    async for frame in transport.capture_frames():
        seen.append(frame)
    # Order of remaining items is preserved (FIFO).
    assert seen == [b"\x02", b"\x03", b"\x04"]


@pytest.mark.asyncio
async def test_stop_is_idempotent() -> None:
    transport = BrowserAudioTransport()
    await transport.start()
    await transport.stop()
    await transport.stop()  # second call is a no-op
    assert transport.is_closed


@pytest.mark.asyncio
async def test_play_frames_sync_iterable_queues_for_drain() -> None:
    transport = BrowserAudioTransport()
    await transport.start()
    await transport.play_frames([b"out1", b"out2", b"out3"])
    transport.close_playback()

    drained: list[bytes] = []
    async for frame in transport.drain_playback_frames():
        drained.append(frame)
    assert drained == [b"out1", b"out2", b"out3"]


@pytest.mark.asyncio
async def test_play_frames_async_iterable() -> None:
    async def src() -> AsyncIterator[bytes]:
        yield b"alpha"
        yield b"beta"

    transport = BrowserAudioTransport()
    await transport.start()
    await transport.play_frames(src())
    transport.close_playback()

    drained: list[bytes] = []
    async for frame in transport.drain_playback_frames():
        drained.append(frame)
    assert drained == [b"alpha", b"beta"]


@pytest.mark.asyncio
async def test_play_frames_resamples_when_source_rate_differs() -> None:
    """If TTS emits at 22.05 kHz, the transport must resample to 16 kHz."""
    transport = BrowserAudioTransport()
    await transport.start()
    # 100 ms of silence at 22050 Hz = 2205 samples × 2 bytes = 4410 bytes.
    frame_22k = b"\x00\x00" * 2205
    await transport.play_frames([frame_22k], source_rate=22_050)
    transport.close_playback()

    drained: list[bytes] = []
    async for frame in transport.drain_playback_frames():
        drained.append(frame)
    # The resampler should produce roughly 100 ms at 16 kHz = 1600 samples
    # × 2 bytes = 3200 bytes. Allow ±10% for boundary rounding.
    assert len(drained) == 1
    resampled_len = len(drained[0])
    expected = 3200
    assert abs(resampled_len - expected) <= expected * 0.1, (
        f"resampled length {resampled_len} differs from expected ~{expected}"
    )


@pytest.mark.asyncio
async def test_push_capture_frame_after_close_is_dropped() -> None:
    transport = BrowserAudioTransport()
    await transport.start()
    await transport.stop()
    transport.push_capture_frame(b"\xFF")  # ignored
    # The only frame the iterator should ever yield is none — the
    # EOF sentinel was pushed by stop().
    seen: list[bytes] = []
    async for frame in transport.capture_frames():
        seen.append(frame)
    assert seen == []


@pytest.mark.asyncio
async def test_empty_play_frames_skipped() -> None:
    transport = BrowserAudioTransport()
    await transport.start()
    await transport.play_frames([b"", b"data", b""])
    transport.close_playback()
    drained = [f async for f in transport.drain_playback_frames()]
    assert drained == [b"data"]


@pytest.mark.asyncio
async def test_default_queue_max_constant_is_reasonable() -> None:
    # Sanity guard: at 20 ms/frame the default cap should be a few seconds
    # of audio — not so small that a brief stall drops everything, not so
    # large that the pipeline lags an entire conversation behind.
    assert 50 <= DEFAULT_CAPTURE_QUEUE_MAX_FRAMES <= 1_000


@pytest.mark.asyncio
async def test_concurrent_push_and_iterate_serializes_correctly() -> None:
    """Producer + consumer can run concurrently without deadlocking."""
    transport = BrowserAudioTransport()
    await transport.start()

    received: list[bytes] = []
    consumer_started = asyncio.Event()

    async def consume() -> None:
        consumer_started.set()
        async for frame in transport.capture_frames():
            received.append(frame)

    consumer = asyncio.create_task(consume())
    await consumer_started.wait()
    for i in range(10):
        transport.push_capture_frame(bytes([i, i, i, i]))
        await asyncio.sleep(0)
    await transport.stop()
    await consumer
    assert received == [bytes([i, i, i, i]) for i in range(10)]
