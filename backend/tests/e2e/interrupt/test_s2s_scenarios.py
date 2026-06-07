"""Tests for the unified-S2S scenario catalog (Johnny-ckz.22)."""

from __future__ import annotations

import pytest

from johnny.e2e.interrupt.s2s_scenarios import (
    S2S_BARGE_IN_VIA_NEW_USER_TURN,
    S2S_BARGE_IN_VIA_SESSION_INTERRUPT,
    S2S_OPEN_AND_RECEIVE_AUDIO,
    S2S_SCENARIOS,
    s2s_scenarios_by_name,
)


def test_s2s_scenarios_catalog_is_non_empty_and_unique() -> None:
    names = [s.name for s in S2S_SCENARIOS]
    assert len(names) == len(set(names)), f"duplicate scenario names: {names}"
    assert len(S2S_SCENARIOS) >= 3


def test_smoke_scenario_does_not_expect_interrupt() -> None:
    assert S2S_OPEN_AND_RECEIVE_AUDIO.expect_interrupt is False


def test_interrupt_scenarios_have_await_audio_flag() -> None:
    for scenario in (
        S2S_BARGE_IN_VIA_SESSION_INTERRUPT,
        S2S_BARGE_IN_VIA_NEW_USER_TURN,
    ):
        assert scenario.expect_interrupt is True
        flags = [e.await_audio_then_interrupt for e in scenario.timeline]
        assert any(flags), (
            f"{scenario.name} marks expect_interrupt=True but no timeline "
            "event carries await_audio_then_interrupt"
        )


def test_interrupt_kinds_are_orthogonal() -> None:
    assert S2S_BARGE_IN_VIA_SESSION_INTERRUPT.interrupt_kind == "session_interrupt"
    assert S2S_BARGE_IN_VIA_NEW_USER_TURN.interrupt_kind == "new_user_turn"


def test_scenarios_by_name_round_trip() -> None:
    chosen = s2s_scenarios_by_name(["s2s_open_and_receive_audio"])
    assert len(chosen) == 1
    assert chosen[0].name == "s2s_open_and_receive_audio"


def test_scenarios_by_name_raises_on_unknown() -> None:
    with pytest.raises(KeyError, match="unknown"):
        s2s_scenarios_by_name(["does_not_exist"])


def test_runner_budget_covers_typical_response_window() -> None:
    """Every S2S scenario allots at least 30 s — real APIs need it."""
    for scenario in S2S_SCENARIOS:
        assert scenario.runner_timeout_s >= 30.0, (
            f"{scenario.name}.runner_timeout_s={scenario.runner_timeout_s} "
            "is too tight for real S2S APIs"
        )
