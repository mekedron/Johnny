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
    discover_fixtures,
    load_fixture,
)
from johnny.voice_pipeline.events import (
    AgentSpoke,
    RouterDecisionMade,
    TranscriptFinalized,
    TurnTerminal,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "sessions"


def test_fixtures_exist_and_are_split_only() -> None:
    """At least three committed fixtures, all split.

    The ``unified`` (S2S) runtime — and its ``unified-demo`` fixture — was
    removed with the S2S surface (Johnny-trt.43); split fixtures replay on the
    LiveKit-Agents engine, gated by ``test_replay_harness_agent.py``.
    """
    dirs = discover_fixtures(FIXTURES_DIR)
    assert len(dirs) >= 3, f"expected >= 3 fixtures, found {len(dirs)} under {FIXTURES_DIR}"
    runtimes = {load_fixture(d).runtime for d in dirs}
    assert runtimes == {"split"}, f"fixtures must all be split; got {runtimes}"


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


def test_checker_rejects_retired_unified_runtime() -> None:
    """The unified (S2S) checker was removed (Johnny-trt.43) — asking for it
    must fail loudly, not silently pass a stream unchecked."""
    with pytest.raises(ValueError, match="unified"):
        check_invariants([], "unified")
    with pytest.raises(ValueError, match="unified"):
        assemble_turns([], "unified")
