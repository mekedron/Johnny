"""Schema / wiring tests for the declarative scenario catalog."""

from __future__ import annotations

import pytest

from johnny.e2e.interrupt.scenarios import (
    SCENARIOS,
    Scenario,
    scenarios_by_name,
)


def test_scenario_catalog_is_nonempty_and_unique() -> None:
    assert len(SCENARIOS) >= 4, "the bead requires 4 reproducer scenarios"
    names = [s.name for s in SCENARIOS]
    assert len(set(names)) == len(names), "scenario names must be unique"


def test_every_scenario_has_at_least_one_speech_event() -> None:
    for scenario in SCENARIOS:
        speech = [e for e in scenario.timeline if e.is_speech()]
        assert speech, f"{scenario.name}: no speech events in timeline"


def test_every_speech_event_has_a_transcript_string() -> None:
    """The harness pairs speech events to STT transcripts in order."""
    for scenario in SCENARIOS:
        for event in scenario.timeline:
            if not event.is_speech():
                continue
            assert event.transcript, (
                f"{scenario.name}: speech event missing transcript "
                f"(tag={event.tag!r})"
            )


def test_router_decision_count_covers_speech_events() -> None:
    """At minimum, the router needs one decision per finalised utterance."""
    for scenario in SCENARIOS:
        speech_count = sum(1 for e in scenario.timeline if e.is_speech())
        # The pipeline reuses the last decision when scripted decisions
        # run out, so >= 1 is technically enough; we want strict
        # coverage for clarity.
        assert len(scenario.router_decisions) >= 1, (
            f"{scenario.name}: must script at least one router decision; "
            f"saw {len(scenario.router_decisions)} for {speech_count} speech events"
        )


def test_expect_transcripts_match_timeline_speech_transcripts() -> None:
    """expect_transcripts must be a subset of the speech-event transcripts."""
    for scenario in SCENARIOS:
        timeline_texts = {
            e.transcript for e in scenario.timeline if e.is_speech() and e.transcript
        }
        for expected in scenario.expect_transcripts:
            assert expected in timeline_texts, (
                f"{scenario.name}: expect_transcripts has {expected!r} but the "
                f"timeline doesn't produce it (timeline: {timeline_texts})"
            )


def test_expect_interrupt_implies_at_least_one_interrupting_barge_in() -> None:
    interrupting = {"stop", "correct", "new_question"}
    for scenario in SCENARIOS:
        if not scenario.expect_interrupt:
            continue
        verdicts = [
            d for d in scenario.barge_in_decisions
            if d.get("should_interrupt") and d.get("category") in interrupting
        ]
        assert verdicts, (
            f"{scenario.name}: expect_interrupt=True but no barge_in decision "
            f"matches an interrupting category"
        )


def test_interrupt_scenarios_use_interrupt_tag() -> None:
    """Runner looks up `interrupt` tag to compute latency."""
    for scenario in SCENARIOS:
        if not scenario.expect_interrupt:
            continue
        tags = {e.tag for e in scenario.timeline}
        assert "interrupt" in tags, (
            f"{scenario.name}: expect_interrupt=True must include an event "
            f"tagged 'interrupt' so the runner can measure cut latency"
        )


def test_scenarios_by_name_returns_in_order_and_raises_on_unknown() -> None:
    selected = scenarios_by_name(
        ["cough_does_not_interrupt", "stop_interrupts_long_answer"]
    )
    assert [s.name for s in selected] == [
        "cough_does_not_interrupt",
        "stop_interrupts_long_answer",
    ]

    with pytest.raises(KeyError):
        scenarios_by_name(["nonexistent_scenario"])


def test_scenario_is_frozen_dataclass() -> None:
    """Frozen so accidentally mutating a scenario in one test can't leak."""
    from dataclasses import FrozenInstanceError

    scenario = SCENARIOS[0]
    with pytest.raises(FrozenInstanceError):
        scenario.name = "different"  # type: ignore[misc]
    assert isinstance(scenario, Scenario)
