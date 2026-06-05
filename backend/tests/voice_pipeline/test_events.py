"""Tests for johnny.voice_pipeline.events."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from johnny.voice_pipeline.events import (
    AgentSpoke,
    RouterDecisionMade,
    TranscriptFinalized,
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
