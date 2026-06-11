"""Unit tests for the transport-independent reasoning core (Johnny-n22).

Direct coverage of the decision parsers, prompt builder, mode sets and tuning
constants relocated out of the retired split orchestrator into
:mod:`johnny.voice_pipeline.reasoning`. The LiveKit-Agents engine reuses these
verbatim (see tests under ``tests/agent/``), so these tests pin the shared
contract in isolation.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.providers import LLMResponse
from johnny.voice_pipeline.reasoning import (
    _BARGE_IN_SCHEMA,
    _ROUTER_SCHEMA,
    AUTONOMOUS_MODE,
    BARGE_IN_CATEGORIES,
    DEFAULT_NOISE_STOPLIST,
    FREE_FORM_MODES,
    INTERRUPTING_BARGE_IN_CATEGORIES,
    LISTEN_ONLY_MODE,
    NON_SPEAKING_MODES,
    ROUTER_ACTIONS,
    SPEAKING_MODES,
    SUGGEST_ONLY_MODE,
    BargeInDecision,
    RouterDecision,
    TaskRequest,
    _match_allowed_reply,
    _parse_barge_in_response,
    _parse_router_response,
    build_barge_in_messages,
)


def _router_response(structured: dict[str, Any] | None, text: str = "") -> LLMResponse:
    return LLMResponse(text=text, finish_reason="stop", structured_output=structured)


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
    # Old-format output (no ``action`` key): derived, never delegating.
    assert decision.action == "speak"
    assert decision.task_request is None


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
    assert decision.action == "silent"
    assert decision.task_request is None


# --- Phase-3 action/task parser matrix (Johnny-trt.16) -----------------------


@pytest.mark.parametrize(
    "structured",
    [
        # Full old-format payload (the delegation-calendar fixture shape).
        {
            "should_speak": True,
            "confidence": 0.93,
            "reason": "direct ask",
            "reply_type": "string",
            "suggested_reply": "Sure.",
        },
        # Minimal old-format payload (fixture turn 7 omits the optional fields).
        {"should_speak": True, "confidence": 0.86, "reason": "status query"},
        # Old-format decline.
        {
            "should_speak": False,
            "confidence": 0.85,
            "reason": "small talk",
            "reply_type": "null",
            "suggested_reply": None,
        },
    ],
)
def test_parse_router_response_old_format_parses_identically(
    structured: dict[str, Any],
) -> None:
    """The replay-parity contract at unit level: an output with no ``action``
    key produces exactly the legacy field values, with the new fields derived
    (action from should_speak) / inert (task_request None)."""
    decision = _parse_router_response(_router_response(dict(structured)))
    assert decision.should_speak is structured["should_speak"]
    assert decision.confidence == structured["confidence"]
    assert decision.reason == structured["reason"]
    expected_reply_type = structured.get("reply_type")
    assert decision.reply_type == (
        str(expected_reply_type) if expected_reply_type is not None else None
    )
    expected_suggested = structured.get("suggested_reply")
    assert decision.suggested_reply == (
        str(expected_suggested) if expected_suggested is not None else None
    )
    assert decision.raw == structured
    assert decision.action == ("speak" if structured["should_speak"] else "silent")
    assert decision.task_request is None


def test_parse_router_response_valid_delegate() -> None:
    decision = _parse_router_response(
        _router_response(
            {
                "should_speak": True,
                "confidence": 0.9,
                "reason": "complex calendar ask",
                "action": "delegate",
                "task": {
                    "kind": "calendar.upcoming_events",
                    "args": {"days": 7},
                    "ack": "Let me check the calendar.",
                },
            }
        )
    )
    assert decision.action == "delegate"
    assert decision.should_speak is True
    assert decision.task_request == TaskRequest(
        kind="calendar.upcoming_events",
        args={"days": 7},
        ack="Let me check the calendar.",
    )


def test_parse_router_response_delegate_minimal_task_defaults() -> None:
    decision = _parse_router_response(
        _router_response(
            {
                "should_speak": True,
                "confidence": 0.9,
                "reason": "ask",
                "action": "delegate",
                "task": {"kind": "email.check_inbox"},
            }
        )
    )
    assert decision.task_request == TaskRequest(kind="email.check_inbox", args={}, ack="")


def test_parse_router_response_status_action() -> None:
    decision = _parse_router_response(
        _router_response(
            {
                "should_speak": True,
                "confidence": 0.88,
                "reason": "progress query",
                "action": "status",
            }
        )
    )
    assert decision.action == "status"
    assert decision.should_speak is True
    assert decision.task_request is None


def test_parse_router_response_explicit_action_overrides_contradictory_bool() -> None:
    # action is authoritative: silent wins over should_speak=true …
    silent = _parse_router_response(
        _router_response(
            {"should_speak": True, "confidence": 0.9, "reason": "x", "action": "silent"}
        )
    )
    assert silent.action == "silent"
    assert silent.should_speak is False
    # … and a non-silent action wins over should_speak=false / absent.
    speak = _parse_router_response(
        _router_response(
            {"should_speak": False, "confidence": 0.9, "reason": "x", "action": "speak"}
        )
    )
    assert speak.action == "speak"
    assert speak.should_speak is True


def test_parse_router_response_action_is_normalised() -> None:
    decision = _parse_router_response(
        _router_response(
            {
                "should_speak": True,
                "confidence": 0.9,
                "reason": "x",
                "action": "  Delegate ",
                "task": {"kind": "k"},
            }
        )
    )
    assert decision.action == "delegate"
    assert decision.task_request == TaskRequest(kind="k")


def test_parse_router_response_unknown_action_degrades_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING", logger="johnny.voice_pipeline.reasoning"):
        decision = _parse_router_response(
            _router_response(
                {"should_speak": True, "confidence": 0.9, "reason": "x", "action": "dance"}
            )
        )
    assert decision.action == "speak"
    assert decision.should_speak is True
    assert decision.task_request is None
    assert any("unknown action" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "task",
    [
        None,  # delegate with the task object missing entirely
        "calendar",  # not an object
        ["calendar"],  # not an object
        {},  # kind missing
        {"kind": ""},  # kind empty
        {"kind": "   "},  # kind whitespace-only
        {"kind": 7},  # kind not a string
        {"kind": "k", "args": ["x"]},  # args not an object
        {"kind": "k", "args": "x"},  # args not an object
        {"kind": "k", "ack": {"text": "hi"}},  # ack not a string
    ],
)
def test_parse_router_response_malformed_task_degrades_to_speak(
    task: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """AC: a malformed task object degrades to plain speak — never raises."""
    structured: dict[str, Any] = {
        "should_speak": True,
        "confidence": 0.9,
        "reason": "x",
        "action": "delegate",
    }
    if task is not None:
        structured["task"] = task
    with caplog.at_level("WARNING", logger="johnny.voice_pipeline.reasoning"):
        decision = _parse_router_response(_router_response(structured))
    assert decision.action == "speak"
    assert decision.should_speak is True
    assert decision.task_request is None
    assert any("malformed task" in r.message for r in caplog.records)


def test_parse_router_response_malformed_task_with_no_speak_degrades_to_silent() -> None:
    decision = _parse_router_response(
        _router_response(
            {
                "should_speak": False,
                "confidence": 0.9,
                "reason": "x",
                "action": "delegate",
                "task": "junk",
            }
        )
    )
    assert decision.action == "silent"
    assert decision.should_speak is False
    assert decision.task_request is None


def test_parse_router_response_task_ignored_for_non_delegate_actions() -> None:
    """task_request is non-None iff action == 'delegate' — a stray valid task
    on a speak/status verdict is dropped (it survives in ``raw`` for audit)."""
    for action in ("speak", "status", "silent"):
        decision = _parse_router_response(
            _router_response(
                {
                    "should_speak": True,
                    "confidence": 0.9,
                    "reason": "x",
                    "action": action,
                    "task": {"kind": "k"},
                }
            )
        )
        assert decision.action == action
        assert decision.task_request is None


def test_parse_router_response_never_raises_on_bizarre_action_values() -> None:
    """The hook must not crash on arbitrary model JSON (AC)."""
    for action in (3, 1.5, True, ["speak"], {"action": "speak"}):
        decision = _parse_router_response(
            _router_response(
                {"should_speak": True, "confidence": 0.9, "reason": "x", "action": action}
            )
        )
        assert decision.action == "speak"
        assert decision.task_request is None
    # bool True str()s to "true" — not a valid action — and None means absent.
    absent = _parse_router_response(
        _router_response({"should_speak": False, "confidence": 0.9, "reason": "x", "action": None})
    )
    assert absent.action == "silent"


def test_router_decision_direct_construction_derives_action() -> None:
    """Existing construction sites (approval wiring, tests) predate the field:
    the empty-string default derives the action from should_speak."""
    assert RouterDecision(should_speak=True, confidence=1.0, reason="r").action == "speak"
    assert RouterDecision(should_speak=False, confidence=0.0, reason="r").action == "silent"
    explicit = RouterDecision(
        should_speak=True,
        confidence=1.0,
        reason="r",
        action="status",
    )
    assert explicit.action == "status"


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
    # Phase 3 (Johnny-trt.16) added ``action`` to the core contract; the
    # legacy trio stays required so old consumers/prompts keep working.
    assert set(_ROUTER_SCHEMA["required"]) == {
        "should_speak",
        "confidence",
        "reason",
        "action",
    }


def test_router_schema_action_and_task_shape() -> None:
    """The Phase-3 schema extension is additive and closed (Johnny-trt.16):
    a tight action enum plus a nullable task object — one LLM call, no
    second hop."""
    assert ROUTER_ACTIONS == ("silent", "speak", "delegate", "status")
    action = _ROUTER_SCHEMA["properties"]["action"]
    assert action["enum"] == list(ROUTER_ACTIONS)
    task = _ROUTER_SCHEMA["properties"]["task"]
    assert task["type"] == ["object", "null"]
    assert set(task["properties"]) == {"kind", "args", "ack"}
    assert task["required"] == ["kind"]
    # The legacy properties are untouched (additive extension).
    for legacy in ("should_speak", "confidence", "reason", "reply_type", "suggested_reply"):
        assert legacy in _ROUTER_SCHEMA["properties"]
