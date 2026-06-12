"""The trt.48 ensemble scenario as a repeatable regression (Johnny-trt.48).

Runs the bundled addressing fixture through the REAL multi-agent playground
machinery — two :class:`BrowserAgentSession` engines, the
:class:`GroupAudioRouter` capture mixer / playback merge, one shared speech
floor — and asserts the trt.46 invariants programmatically: floor holds never
overlap, audio stays inside the speaker's own holds, the co-agent's speech is
peer-labeled and opens no turn, and every reply survives the floor handoff
(the trt.48 shield). Hermetic: stub providers, in-memory floor hub, no DB, no
Redis — only the in-image Silero model.

Slow by nature (real engines + real-time audio pacing, ~25 s); it lives in
the integration suite next to its sibling
:mod:`tests.integration.test_speech_floor_contention`.
"""

from __future__ import annotations

from johnny.agent.ensemble_scenario import (
    DEFAULT_SCENARIO_PATH,
    load_scenario,
    render_report,
    run_ensemble_scenario,
)


async def test_addressing_scenario_holds_the_trt46_invariants() -> None:
    scenario = load_scenario(DEFAULT_SCENARIO_PATH)
    result = await run_ensemble_scenario(scenario)
    assert result.passed, render_report(result)

    steps = len(scenario.steps)
    by_name = {member.name: member for member in result.members}
    assert set(by_name) == {"Alex", "Echo"}

    for member in result.members:
        # Always-speak stub router + the handoff shield: every scripted ask
        # produces one completed reply per member, serialized by the floor.
        assert len(member.spoke) == steps, render_report(result)
        assert len(member.floor_holds) == steps, render_report(result)
        # Strict v1 loop rule: the co-agent's audio opened no router turn.
        assert member.decisions == steps, render_report(result)
        # The cross-feed demonstrably ran: the peer's speech arrived, was
        # attributed via the floor window, and labeled with the peer's name.
        peer = "Echo" if member.name == "Alex" else "Alex"
        assert member.peer_labeled_finals, render_report(result)
        assert all(label == peer for label, _ in member.peer_labeled_finals), (
            render_report(result)
        )
        # The suppression sweep reported the windows (one per peer reply).
        assert member.suppressions, render_report(result)
        assert all(s.peer == peer for s in member.suppressions), render_report(result)
        # Claims are the trt.47 seam — strict v1 emits none.
        assert not member.claims_won and not member.claims_lost
