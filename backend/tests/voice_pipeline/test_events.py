"""Tests for johnny.voice_pipeline.events."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from johnny.voice_pipeline.events import (
    AgentSpeechInterim,
    AgentSpoke,
    AgentSuggested,
    AgentTTSFailed,
    ApprovalPending,
    ApprovalResolved,
    RouterDecisionMade,
    SessionStatusChanged,
    TranscriptFiltered,
    TranscriptFinalized,
    TranscriptInterim,
    event_to_dict,
)


def test_transcript_finalized_defaults() -> None:
    ev = TranscriptFinalized(text="hi", timestamp_ms=100)
    assert ev.text == "hi"
    assert ev.timestamp_ms == 100
    assert ev.speaker is None
    assert ev.confidence is None
    assert ev.session_id is None
    assert ev.type == "transcript_finalized"


def test_transcript_finalized_full() -> None:
    ev = TranscriptFinalized(
        text="hello there",
        timestamp_ms=2_500,
        speaker="alice",
        confidence=0.92,
        session_id="abc",
    )
    assert ev.speaker == "alice"
    assert ev.confidence == pytest.approx(0.92)
    assert ev.session_id == "abc"


def test_transcript_finalized_is_frozen() -> None:
    ev = TranscriptFinalized(text="x", timestamp_ms=0)
    with pytest.raises(FrozenInstanceError):
        ev.text = "mutated"  # type: ignore[misc]


def test_transcript_interim_defaults() -> None:
    ev = TranscriptInterim(text="hello th", timestamp_ms=900)
    assert ev.text == "hello th"
    assert ev.timestamp_ms == 900
    assert ev.speaker is None
    assert ev.session_id is None
    assert ev.type == "transcript_interim"


def test_transcript_interim_is_frozen() -> None:
    ev = TranscriptInterim(text="x", timestamp_ms=0)
    with pytest.raises(FrozenInstanceError):
        ev.text = "mutated"  # type: ignore[misc]


def test_event_to_dict_transcript_interim() -> None:
    ev = TranscriptInterim(text="hello th", timestamp_ms=900, speaker="user", session_id="s")
    assert event_to_dict(ev) == {
        "text": "hello th",
        "timestamp_ms": 900,
        "speaker": "user",
        "session_id": "s",
        "type": "transcript_interim",
    }


def test_agent_speech_interim_defaults() -> None:
    ev = AgentSpeechInterim(text="Sure thing.", sequence=0, timestamp_ms=1_200)
    assert ev.text == "Sure thing."
    assert ev.sequence == 0
    assert ev.timestamp_ms == 1_200
    assert ev.turn_id is None
    assert ev.session_id is None
    assert ev.type == "agent_speech_interim"


def test_agent_speech_interim_is_frozen() -> None:
    ev = AgentSpeechInterim(text="x", sequence=0, timestamp_ms=0)
    with pytest.raises(FrozenInstanceError):
        ev.text = "mutated"  # type: ignore[misc]


def test_event_to_dict_agent_speech_interim() -> None:
    ev = AgentSpeechInterim(
        text="Here is the plan.",
        sequence=2,
        timestamp_ms=3_400,
        turn_id=7,
        session_id="s",
    )
    assert event_to_dict(ev) == {
        "text": "Here is the plan.",
        "sequence": 2,
        "timestamp_ms": 3_400,
        "turn_id": 7,
        "session_id": "s",
        "type": "agent_speech_interim",
    }


def test_router_decision_made_minimum() -> None:
    ev = RouterDecisionMade(
        should_speak=False,
        confidence=0.0,
        reason="silence",
        timestamp_ms=100,
    )
    assert ev.should_speak is False
    assert ev.confidence == 0.0
    assert ev.reason == "silence"
    assert ev.reply_type is None
    assert ev.suggested_reply is None
    assert ev.type == "router_decision_made"


def test_router_decision_made_with_suggestion() -> None:
    ev = RouterDecisionMade(
        should_speak=True,
        confidence=0.81,
        reason="direct question to bot",
        timestamp_ms=4_000,
        reply_type="acknowledgement",
        suggested_reply="On it.",
    )
    assert ev.suggested_reply == "On it."
    assert ev.reply_type == "acknowledgement"


def test_agent_spoke_minimum() -> None:
    ev = AgentSpoke(text="On it.", audio_duration_ms=500, timestamp_ms=5_000)
    assert ev.text == "On it."
    assert ev.audio_duration_ms == 500
    assert ev.matched_allowed_reply is None
    assert ev.type == "agent_spoke"


def test_agent_spoke_with_matched_reply() -> None:
    ev = AgentSpoke(
        text="On it.",
        audio_duration_ms=500,
        timestamp_ms=5_000,
        matched_allowed_reply="On it.",
        session_id="s1",
    )
    assert ev.matched_allowed_reply == "On it."
    assert ev.session_id == "s1"


def test_event_to_dict_transcript() -> None:
    ev = TranscriptFinalized(
        text="hi", timestamp_ms=10, speaker="alice", confidence=0.9, session_id="s"
    )
    d = event_to_dict(ev)
    assert d == {
        "text": "hi",
        "timestamp_ms": 10,
        "speaker": "alice",
        "confidence": 0.9,
        "session_id": "s",
        "type": "transcript_finalized",
    }


def test_event_to_dict_router() -> None:
    ev = RouterDecisionMade(
        should_speak=True,
        confidence=0.7,
        reason="ok",
        timestamp_ms=20,
        reply_type="confirmation",
        suggested_reply="Yes",
        session_id="s",
    )
    d = event_to_dict(ev)
    assert d["should_speak"] is True
    assert d["confidence"] == 0.7
    assert d["type"] == "router_decision_made"


def test_event_to_dict_agent() -> None:
    ev = AgentSpoke(
        text="Yes", audio_duration_ms=300, timestamp_ms=30, matched_allowed_reply="Yes"
    )
    d = event_to_dict(ev)
    assert d["text"] == "Yes"
    assert d["audio_duration_ms"] == 300
    assert d["type"] == "agent_spoke"


def test_session_status_changed_defaults() -> None:
    ev = SessionStatusChanged(status="joining", timestamp_ms=100)
    assert ev.status == "joining"
    assert ev.timestamp_ms == 100
    assert ev.session_id is None
    assert ev.error_reason is None
    assert ev.type == "session_status_changed"


def test_session_status_changed_failed_carries_reason() -> None:
    ev = SessionStatusChanged(
        status="failed",
        timestamp_ms=4_200,
        session_id="sess-1",
        error_reason="Meeting has not started yet",
    )
    assert ev.status == "failed"
    assert ev.session_id == "sess-1"
    assert ev.error_reason == "Meeting has not started yet"


def test_session_status_changed_is_frozen() -> None:
    ev = SessionStatusChanged(status="joined", timestamp_ms=0)
    with pytest.raises(FrozenInstanceError):
        ev.status = "ended"  # type: ignore[misc]


def test_event_to_dict_session_status() -> None:
    ev = SessionStatusChanged(
        status="joined",
        timestamp_ms=15,
        session_id="s",
    )
    d = event_to_dict(ev)
    assert d == {
        "status": "joined",
        "timestamp_ms": 15,
        "session_id": "s",
        "error_reason": None,
        "type": "session_status_changed",
    }


def test_approval_pending_defaults() -> None:
    ev = ApprovalPending(
        decision_id=11,
        suggested_reply="yes",
        timestamp_ms=10,
        timeout_s=15.0,
    )
    assert ev.decision_id == 11
    assert ev.suggested_reply == "yes"
    assert ev.timeout_s == pytest.approx(15.0)
    assert ev.reason == ""
    assert ev.reply_type is None
    assert ev.session_id is None
    assert ev.type == "approval_pending"


def test_approval_pending_is_frozen() -> None:
    ev = ApprovalPending(decision_id=1, suggested_reply="x", timestamp_ms=0, timeout_s=1.0)
    with pytest.raises(FrozenInstanceError):
        ev.decision_id = 2  # type: ignore[misc]


def test_approval_pending_full() -> None:
    ev = ApprovalPending(
        decision_id=22,
        suggested_reply="On it.",
        timestamp_ms=5_000,
        timeout_s=30.0,
        reason="direct ask",
        reply_type="acknowledgement",
        session_id="sess-7",
    )
    assert ev.reason == "direct ask"
    assert ev.reply_type == "acknowledgement"
    assert ev.session_id == "sess-7"


def test_event_to_dict_approval_pending() -> None:
    ev = ApprovalPending(
        decision_id=3, suggested_reply="x", timestamp_ms=1, timeout_s=2.0
    )
    d = event_to_dict(ev)
    assert d["type"] == "approval_pending"
    assert d["decision_id"] == 3
    assert d["timeout_s"] == pytest.approx(2.0)


def test_approval_resolved_defaults() -> None:
    ev = ApprovalResolved(
        decision_id=7,
        resolution="approved",
        timestamp_ms=10,
    )
    assert ev.decision_id == 7
    assert ev.resolution == "approved"
    assert ev.session_id is None
    assert ev.type == "approval_resolved"


def test_approval_resolved_supports_rejected_timeout() -> None:
    from typing import Literal, get_args

    for outcome in get_args(Literal["approved", "rejected", "timeout"]):
        ev = ApprovalResolved(
            decision_id=1,
            resolution=outcome,
            timestamp_ms=0,
        )
        assert ev.resolution == outcome


def test_event_to_dict_approval_resolved() -> None:
    ev = ApprovalResolved(
        decision_id=11, resolution="rejected", timestamp_ms=42, session_id="s"
    )
    d = event_to_dict(ev)
    assert d == {
        "decision_id": 11,
        "resolution": "rejected",
        "timestamp_ms": 42,
        "session_id": "s",
        "type": "approval_resolved",
    }


def test_agent_suggested_defaults() -> None:
    ev = AgentSuggested(suggested_reply="Hello", timestamp_ms=100)
    assert ev.suggested_reply == "Hello"
    assert ev.timestamp_ms == 100
    assert ev.decision_id is None
    assert ev.reason == ""
    assert ev.reply_type is None
    assert ev.session_id is None
    assert ev.type == "agent_suggested"


def test_agent_suggested_full() -> None:
    ev = AgentSuggested(
        suggested_reply="I agree.",
        timestamp_ms=5_000,
        decision_id=42,
        reason="direct ask",
        reply_type="acknowledgement",
        session_id="sess-3",
    )
    assert ev.decision_id == 42
    assert ev.reason == "direct ask"
    assert ev.reply_type == "acknowledgement"
    assert ev.session_id == "sess-3"


def test_agent_suggested_is_frozen() -> None:
    ev = AgentSuggested(suggested_reply="x", timestamp_ms=0)
    with pytest.raises(FrozenInstanceError):
        ev.suggested_reply = "y"  # type: ignore[misc]


def test_event_to_dict_agent_suggested() -> None:
    ev = AgentSuggested(
        suggested_reply="Hello",
        timestamp_ms=10,
        decision_id=7,
        reason="addresses you",
        reply_type="answer",
        session_id="s",
    )
    d = event_to_dict(ev)
    assert d == {
        "suggested_reply": "Hello",
        "timestamp_ms": 10,
        "decision_id": 7,
        "reason": "addresses you",
        "reply_type": "answer",
        "session_id": "s",
        "type": "agent_suggested",
    }


# --- TranscriptFiltered (Johnny-ckz.14) -----------------------------------


def test_transcript_filtered_defaults() -> None:
    ev = TranscriptFiltered(text="uh", timestamp_ms=42, reason="stoplist_match")
    assert ev.text == "uh"
    assert ev.timestamp_ms == 42
    assert ev.reason == "stoplist_match"
    assert ev.speaker is None
    assert ev.confidence is None
    assert ev.audio_duration_ms is None
    assert ev.session_id is None
    assert ev.type == "transcript_filtered"


def test_transcript_filtered_full() -> None:
    ev = TranscriptFiltered(
        text="............",
        timestamp_ms=10,
        reason="punctuation_only",
        speaker="participant-2",
        confidence=0.4,
        audio_duration_ms=620,
        session_id="sess-9",
    )
    assert ev.reason == "punctuation_only"
    assert ev.audio_duration_ms == 620
    assert ev.speaker == "participant-2"


def test_transcript_filtered_is_frozen() -> None:
    ev = TranscriptFiltered(text="x", timestamp_ms=0, reason="empty")
    with pytest.raises(FrozenInstanceError):
        ev.text = "y"  # type: ignore[misc]


def test_event_to_dict_transcript_filtered() -> None:
    ev = TranscriptFiltered(
        text="uh",
        timestamp_ms=5_000,
        reason="stoplist_match",
        speaker="alice",
        confidence=0.55,
        audio_duration_ms=300,
        session_id="sess-1",
    )
    d = event_to_dict(ev)
    assert d == {
        "text": "uh",
        "timestamp_ms": 5_000,
        "reason": "stoplist_match",
        "speaker": "alice",
        "confidence": 0.55,
        "audio_duration_ms": 300,
        "session_id": "sess-1",
        "type": "transcript_filtered",
    }


# --- AgentTTSFailed (Johnny-g2n) ------------------------------------------


def test_agent_tts_failed_defaults() -> None:
    ev = AgentTTSFailed(
        provider_name="elevenlabs",
        category="quota_exceeded",
        message="elevenlabs TTS HTTP 401: exceeds your quota",
        timestamp_ms=42,
    )
    assert ev.provider_name == "elevenlabs"
    assert ev.category == "quota_exceeded"
    assert ev.terminal is False
    assert ev.session_id is None
    assert ev.type == "agent_tts_failed"


def test_agent_tts_failed_terminal_full() -> None:
    ev = AgentTTSFailed(
        provider_name="elevenlabs",
        category="quota_exceeded",
        message="exceeds your quota of 10, 25 credits required",
        timestamp_ms=5_000,
        terminal=True,
        session_id="sess-12",
    )
    assert ev.terminal is True
    assert ev.session_id == "sess-12"


def test_agent_tts_failed_is_frozen() -> None:
    ev = AgentTTSFailed(
        provider_name=None,
        category="unknown",
        message="x",
        timestamp_ms=0,
    )
    with pytest.raises(FrozenInstanceError):
        ev.category = "auth_failed"  # type: ignore[misc]


def test_event_to_dict_agent_tts_failed() -> None:
    ev = AgentTTSFailed(
        provider_name="elevenlabs",
        category="auth_failed",
        message="elevenlabs TTS HTTP 401: invalid_api_key",
        timestamp_ms=1_234,
        terminal=True,
        session_id="sess-7",
    )
    d = event_to_dict(ev)
    assert d == {
        "provider_name": "elevenlabs",
        "category": "auth_failed",
        "message": "elevenlabs TTS HTTP 401: invalid_api_key",
        "terminal": True,
        "timestamp_ms": 1_234,
        "session_id": "sess-7",
        "type": "agent_tts_failed",
    }


def test_agent_tts_failed_category_supports_all_documented_values() -> None:
    """Constructor accepts every documented category — pins the Literal contract."""
    from typing import get_args

    from johnny.voice_pipeline.events import AgentTTSFailedCategory

    categories = get_args(AgentTTSFailedCategory)
    assert set(categories) == {
        "quota_exceeded",
        "auth_failed",
        "rate_limited",
        "unknown",
    }
    for cat in categories:
        ev = AgentTTSFailed(
            provider_name="x",
            category=cat,  # type: ignore[arg-type]
            message="x",
            timestamp_ms=0,
        )
        assert ev.category == cat


def test_transcript_filtered_reason_supports_all_documented_values() -> None:
    """Constructor accepts every documented reason — pins the Literal contract."""
    from typing import Literal, get_args

    from johnny.voice_pipeline.events import TranscriptFilteredReason

    reasons = get_args(TranscriptFilteredReason)
    assert set(reasons) == {
        "audio_too_short",
        "empty",
        "punctuation_only",
        "too_short",
        "stoplist_match",
        "low_confidence",
    }
    for reason in reasons:
        ev = TranscriptFiltered(
            text="x",
            timestamp_ms=0,
            reason=reason,  # type: ignore[arg-type]
        )
        assert ev.reason == reason
    # Also confirm the Literal/typing import path is intact for downstream
    # consumers that branch on the reason value.
    assert Literal[*reasons] is not None
