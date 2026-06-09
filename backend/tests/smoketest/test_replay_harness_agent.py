"""Gate: the committed split fixtures replayed through the AgentSession engine.

The LiveKit-Agents ``AgentSession`` engine is the split STT→LLM→TTS path
(``JOHNNY_ORCHESTRATOR=agentsession``, the default since Johnny-n22). Every
committed **split** fixture is fed through
:func:`johnny.smoketest.replay_agent.run_agent_replay` (RouterGate + TurnLedger +
observability) and must hold the ``.28.x`` invariants. ``test_replay_harness.py``
gates the unified (S2S) fixtures on ``UnifiedVoicePipeline``.

Jobs:

1. every committed **split** fixture holds INV-1 (one terminal per turn) +
   INV-2 (decision↔utterance parity) under the engine;
2. the flagship session-14 silent drop terminates in a durable
   ``no_reply(stage_error)`` (the router-timeout fix, Johnny-9k2);
3. a unified fixture is rejected (unified/S2S stays on ``UnifiedVoicePipeline``).

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
    """The cutover gate needs at least the two committed split fixtures (3, 14)."""
    dirs = _split_fixture_dirs()
    assert len(dirs) >= 2, f"expected >= 2 split fixtures, found {len(dirs)} under {FIXTURES_DIR}"


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


async def test_unified_fixture_rejected_by_agent_engine() -> None:
    """The agent engine is split-only; a unified fixture must be rejected, not
    silently mis-replayed (unified/S2S stays on the legacy UnifiedVoicePipeline)."""
    unified = FIXTURES_DIR / "unified-demo"
    if not (unified / "fixture.json").exists():
        pytest.skip("no unified fixture committed")
    fixture = load_fixture(unified)
    with pytest.raises(ValueError, match="split-only"):
        await run_agent_replay(fixture)
