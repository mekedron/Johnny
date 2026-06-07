"""Tests for the unified-S2S runner helpers (Johnny-ckz.22).

Live S2S API tests live in ``tests/providers/test_openai_realtime_s2s.py``
and ``tests/providers/test_gemini_live_s2s.py`` (gated on env vars). The
runner-level end-to-end harness is exercised by hand via
``python -m johnny.e2e.interrupt --mode=unified`` rather than as a unit
test — its assertions depend on real-API timing.

This file covers the harness-internal pieces:

* ``_HoldingScriptedTransport`` keeps capture alive after the script,
  switches to empty bytes after ``signal_silence_after_commit``, and
  exits on ``signal_stop``.
* The runner's frame-expansion logic.
"""

from __future__ import annotations

import asyncio

import pytest

from johnny.e2e.interrupt.audio import BYTES_PER_FRAME
from johnny.e2e.interrupt.s2s_runner import (
    _expand_scenario_to_frames,
    _HoldingScriptedTransport,
)
from johnny.e2e.interrupt.s2s_scenarios import (
    S2S_OPEN_AND_RECEIVE_AUDIO,
    S2SScenario,
    S2SSpeakerEvent,
)
from johnny.e2e.interrupt.transport import TaggedFrame


@pytest.mark.asyncio
async def test_holding_transport_yields_script_then_silence() -> None:
    script = [
        TaggedFrame(pcm=b"\x10" * BYTES_PER_FRAME, event_tag="speech"),
        TaggedFrame(pcm=b"\x00" * BYTES_PER_FRAME, event_tag="silence"),
    ]
    transport = _HoldingScriptedTransport(
        script=script, frame_duration_ms=20
    )

    collected: list[bytes] = []

    async def consume() -> None:
        async for frame in transport.capture_frames():
            collected.append(frame)
            if len(collected) >= 5:
                transport.signal_stop()

    await asyncio.wait_for(consume(), timeout=2.0)
    # First two frames are the script; the rest are post-script silence.
    assert collected[0] == script[0].pcm
    assert collected[1] == script[1].pcm
    assert all(len(f) == BYTES_PER_FRAME for f in collected[2:])
    assert all(f == bytes(BYTES_PER_FRAME) for f in collected[2:])


@pytest.mark.asyncio
async def test_holding_transport_silences_after_commit_signal() -> None:
    """signal_silence_after_commit makes every subsequent frame empty bytes."""
    script = [
        TaggedFrame(pcm=b"\x10" * BYTES_PER_FRAME, event_tag="speech"),
        TaggedFrame(pcm=b"\x00" * BYTES_PER_FRAME, event_tag="silence_after"),
    ]
    transport = _HoldingScriptedTransport(
        script=script, frame_duration_ms=20
    )

    collected: list[bytes] = []

    async def consume() -> None:
        idx = 0
        async for frame in transport.capture_frames():
            collected.append(frame)
            idx += 1
            if idx == 1:
                # Fire the commit BEFORE the second scripted frame
                # is delivered; the transport should swap it to b"".
                transport.signal_silence_after_commit()
            if idx >= 4:
                transport.signal_stop()

    await asyncio.wait_for(consume(), timeout=2.0)
    # First frame plays normally.
    assert collected[0] == script[0].pcm
    # All subsequent frames (rest of script + post-script silence)
    # must be empty bytes.
    assert all(f == b"" for f in collected[1:]), collected[1:]


@pytest.mark.asyncio
async def test_holding_transport_signal_stop_is_idempotent() -> None:
    script = [
        TaggedFrame(pcm=b"\x00" * BYTES_PER_FRAME, event_tag="x"),
    ]
    transport = _HoldingScriptedTransport(script=script, frame_duration_ms=20)
    transport.signal_stop()
    transport.signal_stop()  # Must not raise.
    collected: list[bytes] = []
    async for frame in transport.capture_frames():
        collected.append(frame)
    # The single script frame should still emit, but post-script holds
    # are skipped because stop is already set.
    assert collected == [script[0].pcm]


def test_expand_scenario_to_frames_includes_every_event() -> None:
    frames = _expand_scenario_to_frames(S2S_OPEN_AND_RECEIVE_AUDIO)
    tags = {f.event_tag for f in frames}
    expected_tags = {e.tag for e in S2S_OPEN_AND_RECEIVE_AUDIO.timeline}
    assert expected_tags.issubset(tags)
    assert all(len(f.pcm) == BYTES_PER_FRAME for f in frames)


def test_expand_scenario_emits_tagged_frames_for_cough() -> None:
    scenario = S2SScenario(
        name="test_cough_scenario",
        description="cough",
        instructions="ignore",
        timeline=(
            S2SSpeakerEvent(kind="silence", duration_ms=200, tag="lead"),
            S2SSpeakerEvent(kind="cough", duration_ms=80, tag="cough"),
        ),
        expect_interrupt=False,
    )
    frames = _expand_scenario_to_frames(scenario)
    tags = [f.event_tag for f in frames]
    assert "lead" in tags
    assert "cough" in tags
