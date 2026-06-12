"""Gate: the committed split fixtures replayed through the AgentSession engine.

The LiveKit-Agents ``AgentSession`` engine is the split STT→LLM→TTS path
(``JOHNNY_ORCHESTRATOR=agentsession``, the default since Johnny-n22). Every
committed fixture is fed through
:func:`johnny.smoketest.replay_agent.run_agent_replay` (RouterGate + TurnLedger +
observability) and must hold the ``.28.x`` invariants. (The unified/S2S replay
engine and its fixture were removed with the S2S surface, Johnny-trt.43.)

Jobs:

1. every committed fixture holds INV-1 (one terminal per turn) +
   INV-2 (decision↔utterance parity) under the engine;
2. the flagship session-14 silent drop terminates in a durable
   ``no_reply(stage_error)`` (the router-timeout fix, Johnny-9k2);
3. a fixture declaring a non-split runtime is rejected, not mis-replayed.

Guarded by ``importorskip`` so the suite still collects without the ``agent``
extra (``livekit-agents``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("livekit.agents")

from johnny.smoketest.replay import (  # noqa: E402
    check_invariants,
    diff_against_recorded,
    discover_fixtures,
    load_fixture,
)
from johnny.smoketest.replay_agent import run_agent_replay  # noqa: E402
from johnny.voice_pipeline.events import TurnTerminal  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "sessions"

# pytest is configured with ``asyncio_mode = "auto"`` — async tests need no mark.


def _split_fixture_dirs() -> list[Path]:
    """Every committed fixture whose runtime is ``split`` (the agent engine's scope)."""
    return [d for d in discover_fixtures(FIXTURES_DIR) if load_fixture(d).runtime == "split"]


def _split_ids() -> list[str]:
    return [d.name for d in _split_fixture_dirs()]


def test_split_fixtures_exist() -> None:
    """The gate needs the two cutover fixtures (3, 14) plus the Phase-0 parity
    baseline pair (delegation-calendar, delegation-smalltalk; Johnny-trt.3)."""
    dirs = _split_fixture_dirs()
    assert len(dirs) >= 4, f"expected >= 4 split fixtures, found {len(dirs)} under {FIXTURES_DIR}"


@pytest.mark.parametrize("fixture_dir", _split_fixture_dirs(), ids=_split_ids())
async def test_split_fixture_holds_invariants_on_agent_engine(fixture_dir: Path) -> None:
    """Every committed split fixture replays cleanly under the new engine's INV gate."""
    fixture = load_fixture(fixture_dir)
    result = await run_agent_replay(fixture)
    # The gate-level replay reports turn_count as stt_calls (no STT segmentation),
    # so the legacy split segmentation guard is a no-op pass here.
    assert result.stt_calls == fixture.turn_count
    violations = check_invariants(result.events, fixture.runtime)
    assert not violations, f"{fixture.label} invariant violations on agent engine: {violations}"


async def test_session_14_silent_drop_terminates_on_agent_engine() -> None:
    """Flagship: session-14 turn 4 (the silent drop) ends in a durable
    no_reply(stage_error) on the new engine — the gate's ported router timeout
    (Johnny-9k2) turns the hang into a terminal instead of vanishing."""
    fixture = load_fixture(FIXTURES_DIR / "14")
    result = await run_agent_replay(fixture)
    terminals = [e for e in result.events if isinstance(e, TurnTerminal)]
    # Four transcribed turns, four terminals — none dropped.
    assert len(terminals) == fixture.turn_count
    turn4 = [t for t in terminals if t.turn_id == 4]
    assert turn4, "turn 4 produced no terminal — the silent drop is back on the agent engine"
    assert turn4[0].terminal_state == "no_reply"
    assert turn4[0].no_reply_reason == "stage_error"
    # Regression diff surfaces the fix: recorded None → replayed no_reply.
    diffs = diff_against_recorded(fixture, result.records)
    assert any(
        d.turn_id == 4 and d.field == "terminal_state" and d.replayed == "no_reply" for d in diffs
    ), f"expected turn-4 terminal_state divergence on the agent engine, got {diffs}"


# --- Phase-0 verdict-parity baseline for the Phase-3 router extension --------

# Delegation-/status-shaped fixtures (Johnny-trt.3): the utterance shapes the
# Phase-3 triage refactor (Johnny-trt.16) will start routing as delegate/status
# actions — "can you check our calendar for upcoming meetings", "are you still
# working on that?" — plus the negative shapes (the same phrasing addressed to a
# human, plain small talk, a low-confidence retracted ask). Their ``recorded``
# blocks pin the CURRENT engine's verdicts with old-format router payloads (no
# ``action``/``task`` fields); the schema extension must replay them with zero
# divergence — any diff here is the verdict drift the epic's parity invariant
# forbids. See tests/fixtures/sessions/README.md.
PARITY_BASELINE_IDS = ("delegation-calendar", "delegation-smalltalk")


def test_parity_baseline_fixtures_committed() -> None:
    """Renaming or gutting a parity fixture must fail loudly, not skip silently."""
    for fixture_id in PARITY_BASELINE_IDS:
        path = FIXTURES_DIR / fixture_id
        assert (path / "fixture.json").exists(), (
            f"Phase-3 parity-baseline fixture {fixture_id!r} missing under {FIXTURES_DIR}"
        )
        fixture = load_fixture(path)
        assert fixture.runtime == "split", f"{fixture_id} must be split-runtime"
        assert all("should_speak" in t.router for t in fixture.turns), (
            f"{fixture_id}: every turn must carry an old-format recorded router verdict"
        )
        # diff_against_recorded only compares keys PRESENT in the recorded block,
        # so a missing/typo'd key would silently exempt that field from the drift
        # guard — require the full compared set on every turn.
        for i, turn in enumerate(fixture.turns, start=1):
            missing = {"should_speak", "terminal_state", "outcome", "spoke_text"} - set(
                turn.recorded
            )
            assert not missing, (
                f"{fixture_id} turn {i}: recorded block missing {sorted(missing)} — "
                "the parity baseline must pin all diffed fields"
            )


@pytest.mark.parametrize("fixture_id", PARITY_BASELINE_IDS)
async def test_delegation_baseline_zero_verdict_drift(fixture_id: str) -> None:
    """The hard Phase-3 regression gate: replaying a delegation fixture must
    reproduce its recorded speak/no-speak verdicts EXACTLY — unlike the
    session-14 fixture (whose divergence is the fix showing up), zero diffs."""
    fixture = load_fixture(FIXTURES_DIR / fixture_id)
    result = await run_agent_replay(fixture)
    violations = check_invariants(result.events, fixture.runtime)
    assert not violations, f"{fixture.label} invariant violations: {violations}"
    diffs = diff_against_recorded(fixture, result.records)
    assert not diffs, (
        f"{fixture.label}: replayed verdicts drifted from the Phase-0 parity baseline: "
        f"{[(d.turn_id, d.field, d.recorded, d.replayed) for d in diffs]}"
    )


async def test_delegation_smalltalk_pins_suppression_reasons() -> None:
    """The two distinct no-speak paths stay distinct: a delegation-shaped ask
    addressed to a human is suppressed by the ROUTER (router_declined), while a
    retracted ask the router approves at 0.55 is suppressed by the gate's 0.7
    confidence threshold (low_confidence). Phase 3 must not blur them."""
    fixture = load_fixture(FIXTURES_DIR / "delegation-smalltalk")
    result = await run_agent_replay(fixture)
    terminals = {t.turn_id: t for t in result.events if isinstance(t, TurnTerminal)}
    assert terminals[3].no_reply_reason == "router_declined", terminals[3]
    assert terminals[8].no_reply_reason == "low_confidence", terminals[8]


async def test_non_split_fixture_rejected_by_agent_engine() -> None:
    """The agent engine is split-only; a fixture declaring the retired
    ``unified`` runtime must be rejected, not silently mis-replayed."""
    from johnny.smoketest.replay import fixture_from_dict

    fixture = fixture_from_dict(
        {"session_id": "u", "label": "retired-unified", "runtime": "unified"}
    )
    with pytest.raises(ValueError, match="split-only"):
        await run_agent_replay(fixture)
