"""Unit tests for the transport-independent reasoning core (Johnny-n22).

Direct coverage of the decision parsers, prompt builder, mode sets and tuning
constants relocated out of the retired split orchestrator into
:mod:`johnny.voice_pipeline.reasoning`. The LiveKit-Agents engine reuses these
verbatim (see tests under ``tests/agent/``), so these tests pin the shared
contract in isolation.
"""

from __future__ import annotations

from app.providers import LLMResponse
from johnny.voice_pipeline.reasoning import (
    AUTONOMOUS_MODE,
    BARGE_IN_CATEGORIES,
    DEFAULT_NOISE_STOPLIST,
    FREE_FORM_MODES,
    INTERRUPTING_BARGE_IN_CATEGORIES,
    LISTEN_ONLY_MODE,
    NON_SPEAKING_MODES,
    SPEAKING_MODES,
    SUGGEST_ONLY_MODE,
    BargeInDecision,
    RouterDecision,
    _BARGE_IN_SCHEMA,
    _match_allowed_reply,
    _parse_barge_in_response,
    _parse_router_response,
    _ROUTER_SCHEMA,
    build_barge_in_messages,
)


def test_parse_router_response_reads_structured_output() -> None:
    resp = LLMResponse(
        text="",
        finish_reason="stop",
        structured_output={
            "should_speak": True,
            "confidence": 0.9,
            "reason": "direct question",
            "reply_type": "answer",
            "suggested_reply": "Sure.",
        },
    )
    decision = _parse_router_response(resp)
    assert isinstance(decision, RouterDecision)
    assert decision.should_speak is True
    assert decision.confidence == 0.9
    assert decision.suggested_reply == "Sure."


def test_parse_router_response_clamps_confidence() -> None:
    resp = LLMResponse(
        text="",
        finish_reason="stop",
        structured_output={"should_speak": True, "confidence": 9.9, "reason": "x"},
    )
    assert _parse_router_response(resp).confidence == 1.0


def test_parse_router_response_falls_back_to_no_speak() -> None:
    resp = LLMResponse(text="not json", finish_reason="stop", structured_output=None)
    decision = _parse_router_response(resp)
    assert decision.should_speak is False
    assert decision.confidence == 0.0


def test_parse_barge_in_downgrades_noise_with_interrupt() -> None:
    # A buggy classifier claiming should_interrupt=True for a non-interrupting
    # category is downgraded to no-interrupt.
    resp = LLMResponse(
        text="",
        finish_reason="stop",
        structured_output={
            "should_interrupt": True,
            "category": "noise",
            "reason": "cough",
        },
    )
    decision = _parse_barge_in_response(resp)
    assert isinstance(decision, BargeInDecision)
    assert decision.category == "noise"
    assert decision.should_interrupt is False


def test_parse_barge_in_honours_interrupting_category() -> None:
    resp = LLMResponse(
        text="",
        finish_reason="stop",
        structured_output={
            "should_interrupt": True,
            "category": "stop",
            "reason": "hey stop",
        },
    )
    assert _parse_barge_in_response(resp).should_interrupt is True


def test_parse_barge_in_defaults_to_safe_no_interrupt() -> None:
    resp = LLMResponse(text="", finish_reason="stop", structured_output=None)
    decision = _parse_barge_in_response(resp)
    assert decision.should_interrupt is False
    assert decision.category == "noise"


def test_build_barge_in_messages_shape() -> None:
    messages = build_barge_in_messages(
        text="hey stop",
        speaker="Alice",
        instructions="be concise",
        suggested_reply="As I was saying...",
    )
    assert [m.role for m in messages] == ["system", "user"]
    assert "be concise" in messages[0].content
    assert "Alice" in messages[1].content
    assert "hey stop" in messages[1].content


def test_match_allowed_reply_is_case_insensitive() -> None:
    allowed = ("Yes", "No")
    assert _match_allowed_reply("yes", allowed) == "Yes"
    assert _match_allowed_reply("NO", allowed) == "No"
    assert _match_allowed_reply("maybe", allowed) is None


def test_mode_sets_are_disjoint_and_complete() -> None:
    assert NON_SPEAKING_MODES.isdisjoint(SPEAKING_MODES)
    assert LISTEN_ONLY_MODE in NON_SPEAKING_MODES
    assert SUGGEST_ONLY_MODE in NON_SPEAKING_MODES
    assert AUTONOMOUS_MODE in SPEAKING_MODES
    assert FREE_FORM_MODES == frozenset({AUTONOMOUS_MODE})


def test_barge_in_category_invariants() -> None:
    assert INTERRUPTING_BARGE_IN_CATEGORIES <= set(BARGE_IN_CATEGORIES)
    assert "noise" not in INTERRUPTING_BARGE_IN_CATEGORIES
    assert _BARGE_IN_SCHEMA["properties"]["category"]["enum"] == list(BARGE_IN_CATEGORIES)


def test_noise_stoplist_excludes_legitimate_short_turns() -> None:
    # The gate must never drop these — they are real one-word replies.
    for keep in ("yes", "no", "okay", "thanks", "bye"):
        assert keep not in DEFAULT_NOISE_STOPLIST
    assert "uh" in DEFAULT_NOISE_STOPLIST


def test_router_schema_requires_core_fields() -> None:
    assert set(_ROUTER_SCHEMA["required"]) == {"should_speak", "confidence", "reason"}
