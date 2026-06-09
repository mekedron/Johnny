"""CI gate + teeth tests for the offline replay harness (Johnny-ckz.28.5).

Two jobs:

1. Run every committed fixture under ``tests/fixtures/sessions/`` through the
   real pipeline and assert the .28.x invariants hold. This is what gates merges
   — a refactor that reintroduces a silent drop or breaks decision↔utterance
   parity fails here.
2. Prove the invariant checker has teeth: hand-craft event streams that violate
   each invariant and assert the checker flags them. Without this a checker that
   always returns "no violations" would pass job 1 silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from johnny.smoketest.replay import (
    assemble_turns,
    check_invariants,
    diff_against_recorded,
    discover_fixtures,
    load_fixture,
    run_replay,
)
from johnny.voice_pipeline.events import (
    AgentSpoke,
    RouterDecisionMade,
    TranscriptFinalized,
    TurnTerminal,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "sessions"


def _fixture_ids() -> list[str]:
    return [p.name for p in discover_fixtures(FIXTURES_DIR)]


def test_fixtures_exist_and_cover_both_runtimes() -> None:
    """At least three fixtures, covering split + unified (bead acceptance)."""
    dirs = discover_fixtures(FIXTURES_DIR)
    assert len(dirs) >= 3, f"expected >= 3 fixtures, found {len(dirs)} under {FIXTURES_DIR}"
    runtimes = {load_fixture(d).runtime for d in dirs}
    assert {"split", "unified"} <= runtimes, f"fixtures must cover split + unified; got {runtimes}"


@pytest.mark.parametrize("fixture_dir", discover_fixtures(FIXTURES_DIR), ids=_fixture_ids())
async def test_fixture_holds_invariants(fixture_dir: Path) -> None:
    """Every committed UNIFIED fixture replays cleanly under the invariants gate.

    Split fixtures run on the LiveKit-Agents engine and are gated by
    ``test_replay_harness_agent.py`` — the hand-rolled split in-worker
    orchestrator was retired in Johnny-n22, so ``run_replay`` only drives the
    unified (S2S) path now.
    """
    fixture = load_fixture(fixture_dir)
    if fixture.runtime != "unified":
        pytest.skip("split fixtures run on the agent engine (test_replay_harness_agent)")
    result = await run_replay(fixture)
    violations = check_invariants(result.events, fixture.runtime)
    assert not violations, f"{fixture.label} invariant violations: {violations}"


# --- teeth: the checker must catch real violations --------------------------


def test_checker_flags_silent_drop() -> None:
    """A decided turn with no terminal is an INV-1 violation (the silent drop)."""
    events = [
        TranscriptFinalized(text="hi", timestamp_ms=0, session_id="t"),
        RouterDecisionMade(
            should_speak=False,
            confidence=0.1,
            reason="n/a",
            timestamp_ms=1,
            turn_id=1,
            session_id="t",
        ),
        # No TurnTerminal for turn 1 — the bug.
    ]
    violations = check_invariants(events, "split")
    assert any(v.invariant == "INV-1" and v.turn_id == 1 for v in violations), violations


def test_checker_flags_no_reply_without_reason() -> None:
    """A no_reply terminal must name its suppressor (INV-1)."""
    events = [
        RouterDecisionMade(
            should_speak=False, confidence=0.1, reason="n/a",
            timestamp_ms=1, turn_id=1, session_id="t",
        ),
        TurnTerminal(
            turn_id=1, terminal_state="no_reply", outcome="suppressed",
            no_reply_reason=None, timestamp_ms=2, session_id="t",
        ),
    ]
    violations = check_invariants(events, "split")
    assert any(v.invariant == "INV-1" for v in violations), violations


def test_checker_flags_existence_parity_break() -> None:
    """A replied terminal with no spoken utterance breaks INV-2 parity."""
    events = [
        RouterDecisionMade(
            should_speak=True, confidence=0.9, reason="ok", suggested_reply="hi",
            timestamp_ms=1, turn_id=1, session_id="t",
        ),
        TurnTerminal(
            turn_id=1, terminal_state="replied", outcome="spoken",
            timestamp_ms=2, session_id="t",
        ),
        # No AgentSpoke — chat would be empty while the decisions panel says replied.
    ]
    violations = check_invariants(events, "split")
    assert any(v.invariant == "INV-2" for v in violations), violations


def test_checker_allows_text_divergence() -> None:
    """Recommended≠spoken text is NOT a violation (the answer LLM rephrases)."""
    events = [
        RouterDecisionMade(
            should_speak=True, confidence=0.9, reason="ok",
            suggested_reply="I can help with that.",
            timestamp_ms=1, turn_id=1, session_id="t",
        ),
        AgentSpoke(
            text="Sure — happy to help!",
            audio_duration_ms=100,
            timestamp_ms=2,
            session_id="t",
        ),
        TurnTerminal(
            turn_id=1, terminal_state="replied", outcome="spoken",
            timestamp_ms=3, session_id="t",
        ),
    ]
    assert check_invariants(events, "split") == []
    # ...but the per-turn record still flags the divergence for the UI.
    records = assemble_turns(events, "split")
    assert records[0].diverged is True


def test_unified_checker_flags_dropped_utterance() -> None:
    """A unified assistant transcript with no agent_spoke is an INV-U drop."""
    events = [
        TranscriptFinalized(text="hello", timestamp_ms=0, speaker="user", session_id="u"),
        TranscriptFinalized(text="hi there", timestamp_ms=1, speaker="assistant", session_id="u"),
        # No AgentSpoke for the assistant turn — the model spoke but nothing reached the user.
    ]
    violations = check_invariants(events, "unified")
    assert any(v.invariant == "INV-U" for v in violations), violations
