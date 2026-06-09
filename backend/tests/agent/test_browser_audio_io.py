"""Unit tests for the browser LiveKit audio I/O adapters (Johnny-7g5.1).

:class:`BrowserAudioInput` turns the transport's inbound PCM into ``rtc.AudioFrame``\\s
the session STT consumes; :class:`BrowserAudioOutput` queues the bot's TTS PCM onto
the transport and estimates playout (the browser reports none) so the reply
``SpeechHandle`` completes. Guarded by ``importorskip`` so the suite collects without
the ``agent`` extra.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("livekit.agents")

from livekit import rtc  # noqa: E402

from johnny.agent.browser_audio_io import BrowserAudioInput, BrowserAudioOutput  # noqa: E402
from johnny.voice_pipeline import BrowserAudioTransport  # noqa: E402

# asyncio_mode = "auto" — async tests need no mark.

_RATE = 16_000
_FRAME = b"\x01\x00" * 320  # 20 ms @ 16 kHz mono s16 (640 bytes, 320 samples)


# --- BrowserAudioInput ------------------------------------------------------


async def test_input_yields_frames_then_stops_on_close() -> None:
    transport = BrowserAudioTransport(sample_rate=_RATE)
    audio_in = BrowserAudioInput(transport)

    transport.push_capture_frame(_FRAME)
    frame = await audio_in.__anext__()
    assert isinstance(frame, rtc.AudioFrame)
    assert frame.sample_rate == _RATE
    assert frame.num_channels == 1
    assert frame.samples_per_channel == 320

    await transport.stop()  # EOF sentinel
    with pytest.raises(StopAsyncIteration):
        await audio_in.__anext__()


async def test_input_skips_empty_chunks() -> None:
    transport = BrowserAudioTransport(sample_rate=_RATE)
    audio_in = BrowserAudioInput(transport)
    # An empty push is ignored by the transport; a real frame still arrives.
    transport.push_capture_frame(b"")
    transport.push_capture_frame(_FRAME)
    frame = await asyncio.wait_for(audio_in.__anext__(), timeout=1.0)
    assert frame.samples_per_channel == 320


# --- BrowserAudioOutput -----------------------------------------------------


async def _drain(transport: BrowserAudioTransport, out: list[bytes]) -> None:
    async for f in transport.drain_playback_frames():
        out.append(f)


async def test_output_capture_queues_frame_and_fires_playout() -> None:
    transport = BrowserAudioTransport(sample_rate=_RATE)
    out = BrowserAudioOutput(transport)
    assert out.sample_rate == _RATE

    played: list[bytes] = []
    drain_task = asyncio.create_task(_drain(transport, played))

    frame = rtc.AudioFrame(data=_FRAME, sample_rate=_RATE, num_channels=1, samples_per_channel=320)
    await out.capture_frame(frame)
    out.flush()
    # flush schedules on_playback_finished after the audio's real-time duration
    # (~20 ms); wait_for_playout returns once it fires.
    ev = await asyncio.wait_for(out.wait_for_playout(), timeout=2.0)
    assert ev.interrupted is False
    assert ev.playback_position == pytest.approx(0.02, abs=0.05)

    transport.close_playback()
    await asyncio.wait_for(drain_task, timeout=1.0)
    assert played == [_FRAME]


async def test_output_clear_buffer_cancels_playback_and_interrupts() -> None:
    transport = BrowserAudioTransport(sample_rate=_RATE)
    out = BrowserAudioOutput(transport)

    # A long segment so the playout timer is still pending when we interrupt.
    big = b"\x01\x00" * (_RATE * 2)  # ~2 s of audio
    frame = rtc.AudioFrame(
        data=big, sample_rate=_RATE, num_channels=1, samples_per_channel=_RATE * 2
    )
    await out.capture_frame(frame)
    out.flush()

    assert transport.interrupt_seq == 0
    out.clear_buffer()
    # cancel_playback drained the queue + enqueued one browser interrupt message.
    assert transport.interrupt_seq == 1

    ev = await asyncio.wait_for(out.wait_for_playout(), timeout=2.0)
    assert ev.interrupted is True

    await out.aclose()
