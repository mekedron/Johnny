"""Integration tests for the scripted latency harness (Johnny-trt.1).

Runs the harness's stub-provider mode end-to-end — a real roomless
``BrowserAgentSession`` over a fake ``BrowserAudioTransport``, fixture speech
pushed real-time-paced through the fake mic — and asserts every turn completes
with the full set of per-stage timings the report needs. The run is wall-clock
bound by design (real Silero VAD endpointing + estimated reply playout), so it
runs ONCE per module and the tests share the result.

Like the sibling smokes, the suite needs the ``agent`` extra and the baked
Silero VAD, so it passes inside the api/agent image
(``docker compose exec api pytest tests/agent/test_latency_harness.py``) and
skips where the extra is absent.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("livekit.agents")

from johnny.agent.latency_harness import (  # noqa: E402
    BUNDLED_FIXTURES,
    REPORT_METRICS,
    STUB_REPLY_TEXT,
    STUB_TRANSCRIPT_TEXT,
    HarnessResult,
    render_report,
    result_to_json,
    run_latency_harness,
    summarize,
)

_TURNS = 3


@pytest.fixture(scope="module")
def harness_result() -> HarnessResult:
    """One shared stub-provider run (3 short-fixture turns, ~20 s wall-clock).

    Deliberately a *sync* fixture driving its own ``asyncio.run``: the result is
    plain data with no loop affinity, and a module-scoped async fixture would
    need a module-scoped event loop that the function-scoped tests don't share.
    """

    async def _run() -> HarnessResult:
        from johnny.agent.session import load_vad

        return await run_latency_harness(
            turns=_TURNS,
            providers_mode="stub",
            fixture_paths=[("short", BUNDLED_FIXTURES["short"])],
            turn_timeout_s=30.0,
            inter_turn_silence_s=0.8,
            vad=load_vad(),
        )

    return asyncio.run(_run())


def test_all_turns_complete_and_reply(harness_result: HarnessResult) -> None:
    assert len(harness_result.turns) == _TURNS
    outcomes = [t.outcome for t in harness_result.turns]
    assert outcomes == ["replied"] * _TURNS, outcomes
    # The stub STT transcript flowed through the noise gate and the stub
    # router approved it — the engine ran the same gate path a real turn runs.
    for turn in harness_result.turns:
        assert turn.transcript == STUB_TRANSCRIPT_TEXT
        assert "should_speak=True" in turn.decision


def test_every_expected_stage_timing_emitted(harness_result: HarnessResult) -> None:
    """Each replied turn carries every report metric (the bead's stage list)."""
    for turn in harness_result.turns:
        missing = [name for name in REPORT_METRICS if turn.metric(name) is None]
        assert not missing, f"turn {turn.turn} missing stages: {missing}"
        assert turn.tts_segments >= 1


def test_wall_clock_stages_are_sane(harness_result: HarnessResult) -> None:
    """VAD-end / first-audio wall measurements sit in physically plausible ranges."""
    for turn in harness_result.turns:
        vad_end = turn.metric("vad_end_ms")
        first_audio = turn.metric("first_audio_wall_ms")
        e2e_derived = turn.metric("e2e_vad_commit_ms")
        assert vad_end is not None and 200 <= vad_end <= 3000, vad_end
        # The reply can only reach the transport after the VAD committed the turn.
        assert first_audio is not None and first_audio > vad_end
        # Wall e2e ≈ VAD-commit wait + the derived stage-graph e2e; generous
        # slack for scheduling jitter and the stt-row stamp granularity.
        assert e2e_derived is not None
        assert abs(first_audio - (vad_end + e2e_derived)) < 1500, (
            f"wall={first_audio} vad_end={vad_end} derived={e2e_derived}"
        )


def test_cold_first_turn_reported_separately(harness_result: HarnessResult) -> None:
    """The cold-start split: turn 1 separate, warm percentiles exclude it."""
    cold = harness_result.cold_turn
    assert cold is not None and cold.turn == 1
    warm = harness_result.warm_replied
    assert [t.turn for t in warm] == list(range(2, _TURNS + 1))
    payload = result_to_json(harness_result)
    assert payload["cold_turn"]["turn"] == 1
    assert payload["warm_summary"]  # non-empty per-stage stats
    report = render_report(harness_result)
    assert "cold start (turn 1" in report
    assert "warm turns (2..N" in report


def test_summary_percentiles_are_ordered(harness_result: HarnessResult) -> None:
    stats = summarize(harness_result.replied)
    assert set(stats) == set(REPORT_METRICS)
    for name, row in stats.items():
        assert row["min"] <= row["p50"] <= row["p95"] <= row["max"], (name, row)


def test_stub_reply_spoke_through_tts(harness_result: HarnessResult) -> None:
    """The stub answer text reached TTS: per-turn first audio implies a synth ran."""
    for turn in harness_result.turns:
        ttfb = turn.metric("tts_ttfb_ms")
        assert ttfb is not None and ttfb >= 0
    assert STUB_REPLY_TEXT  # imported constant stays the documented contract


def test_result_json_shape(harness_result: HarnessResult) -> None:
    payload: dict[str, Any] = result_to_json(harness_result)
    assert payload["turns_requested"] == _TURNS
    assert payload["completed"] == _TURNS
    assert payload["replied"] == _TURNS
    assert len(payload["per_turn"]) == _TURNS
    for row in payload["per_turn"]:
        assert set(REPORT_METRICS) <= set(row)
