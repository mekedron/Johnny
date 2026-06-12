"""The trt.48 ensemble scenario as a repeatable regression (Johnny-trt.48/.47).

Runs the bundled addressing fixture through the REAL multi-agent playground
machinery — two :class:`BrowserAgentSession` engines, the
:class:`GroupAudioRouter` capture mixer / playback merge, one shared speech
floor — and asserts the trt.46 foundation invariants (floor holds never
overlap, audio stays inside the speaker's own holds, the co-agent's speech is
peer-labeled and opens no turn) plus the trt.47 arbitration: each by-name
step is answered by exactly the named agent (router peer selectivity), each
unaddressed step by exactly one turn-claim winner with the loser
terminalizing ``no_reply(peer_answered)``. Hermetic: stub providers,
in-memory floor hub, no DB, no Redis — only the in-image Silero model.

Slow by nature (real engines + real-time audio pacing, ~45 s); it lives in
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


async def test_addressing_scenario_holds_the_arbitration_invariants() -> None:
    scenario = load_scenario(DEFAULT_SCENARIO_PATH)
    result = await run_ensemble_scenario(scenario)
    assert result.passed, render_report(result)

    steps = len(scenario.steps)
    addressed = [s for s in scenario.steps if s.addressed_to is not None]
    unaddressed_count = steps - len(addressed)
    by_name = {member.name: member for member in result.members}
    assert set(by_name) == {"Alex", "Echo"}

    # Exactly one answer per scripted utterance, ensemble-wide (the trt.47
    # claim arbitration replaces trt.48's both-answer worst case).
    total_replies = sum(len(member.spoke) for member in result.members)
    assert total_replies == steps, render_report(result)

    for member in result.members:
        # Every utterance still ran this member's router (a decline is a
        # decision, not a missing turn) — the strict no-peer-turns rule.
        assert member.decisions == steps, render_report(result)
        # Every reply this member spoke was a won claim, and the floor was
        # held exactly once per reply.
        assert len(member.claims_won) == len(member.spoke), render_report(result)
        assert len(member.floor_holds) == len(member.spoke), render_report(result)
        # A lost claim always terminalized honestly.
        assert len(member.claims_lost) == len(member.peer_answered_times), (
            render_report(result)
        )
        # The cross-feed demonstrably ran: the peer's speech arrived, was
        # attributed via the floor window, and labeled with the peer's name.
        peer = "Echo" if member.name == "Alex" else "Alex"
        assert member.peer_labeled_finals, render_report(result)
        assert all(label == peer for label, _ in member.peer_labeled_finals), (
            render_report(result)
        )
        assert member.suppressions, render_report(result)
        assert all(s.peer == peer for s in member.suppressions), render_report(result)

    # By-name selectivity: the named agent answered its own asks (the
    # per-step exactly-the-named-agent check lives in evaluate_result; this
    # is the per-member floor on top).
    for step in addressed:
        named = by_name[step.addressed_to]
        assert len(named.spoke) >= 1, render_report(result)

    # Unaddressed asks were contended: claims were lost somewhere across the
    # ensemble (each unaddressed step has exactly one loser with 2 agents).
    total_lost = sum(len(member.claims_lost) for member in result.members)
    assert total_lost == unaddressed_count, render_report(result)
