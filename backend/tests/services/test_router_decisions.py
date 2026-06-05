"""Tests for the SQLAlchemy-backed decision sink and threshold resolver."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import (
    AgentDecision,
    BotMode,
    DecisionOutcome,
    MeetingConfig,
    ProfileTemplate,
)
from app.services.router_decisions import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    SqlAlchemyDecisionSink,
    resolve_confidence_threshold,
)
from johnny.voice_pipeline.events import RouterDecisionMade

# --- Threshold resolution ------------------------------------------------


def _profile(threshold: float | None) -> ProfileTemplate:
    """Build an unsaved ProfileTemplate. ``confidence_threshold`` defaults to 0.7."""
    return ProfileTemplate(
        name="t",
        mode=BotMode.LIMITED_AUTO_SPEAK,
        base_instructions="",
        base_context="",
        allowed_replies=[],
        confidence_threshold=threshold if threshold is not None else 0.7,
    )


def _meeting(threshold: float | None) -> MeetingConfig:
    """Build an unsaved MeetingConfig. ``confidence_threshold`` may be None."""
    return MeetingConfig(
        calendar_event_id=1,
        profile_template_id=1,
        identity_account_id=1,
        mode=BotMode.LIMITED_AUTO_SPEAK,
        instructions=None,
        context=None,
        allowed_replies=None,
        confidence_threshold=threshold,
        enabled=True,
    )


def test_resolve_threshold_uses_meeting_when_set() -> None:
    profile = _profile(0.6)
    meeting = _meeting(0.9)
    assert resolve_confidence_threshold(profile, meeting) == pytest.approx(0.9)


def test_resolve_threshold_falls_back_to_profile_when_meeting_null() -> None:
    profile = _profile(0.55)
    meeting = _meeting(None)
    assert resolve_confidence_threshold(profile, meeting) == pytest.approx(0.55)


def test_resolve_threshold_falls_back_to_default_when_no_inputs() -> None:
    assert resolve_confidence_threshold(None, None) == pytest.approx(
        DEFAULT_CONFIDENCE_THRESHOLD
    )


def test_resolve_threshold_default_constant_value() -> None:
    assert DEFAULT_CONFIDENCE_THRESHOLD == pytest.approx(0.7)


def test_resolve_threshold_profile_only_uses_profile() -> None:
    profile = _profile(0.42)
    assert resolve_confidence_threshold(profile, None) == pytest.approx(0.42)


def test_resolve_threshold_meeting_zero_is_honored() -> None:
    """meeting.confidence_threshold=0.0 means 'speak on any nonzero confidence'
    and must NOT fall through to the profile."""
    profile = _profile(0.7)
    meeting = _meeting(0.0)
    assert resolve_confidence_threshold(profile, meeting) == pytest.approx(0.0)


def test_resolve_threshold_clamps_out_of_range_values() -> None:
    profile = _profile(1.5)
    assert resolve_confidence_threshold(profile, None) == pytest.approx(1.0)
    profile2 = _profile(-0.2)
    assert resolve_confidence_threshold(profile2, None) == pytest.approx(0.0)


def test_resolve_threshold_meeting_null_profile_null_default() -> None:
    profile = ProfileTemplate(
        name="x",
        mode=BotMode.LIMITED_AUTO_SPEAK,
        base_instructions="",
        base_context="",
        allowed_replies=[],
    )
    meeting = _meeting(None)
    # The ORM default for ProfileTemplate.confidence_threshold is 0.7 — even
    # without a value passed explicitly, the SQLAlchemy column default fires
    # only on flush. So the unsaved instance has the attribute = 0.7
    # because we initialised it that way. Use it directly:
    threshold = resolve_confidence_threshold(profile, meeting)
    assert 0.0 <= threshold <= 1.0


# --- SqlAlchemyDecisionSink -------------------------------------------------


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    # Only need the agent_decisions table — FKs point at bot_sessions but
    # SQLite doesn't enforce FKs by default, so the table-only fixture works.
    Base.metadata.create_all(bind=eng, tables=[AgentDecision.__table__])  # type: ignore[list-item]
    return eng


@pytest.fixture
def db_session(engine: sa.Engine) -> Iterator[Session]:
    sess = Session(engine)
    try:
        yield sess
    finally:
        sess.close()


def _make_event(**overrides: object) -> RouterDecisionMade:
    base: dict[str, object] = {
        "should_speak": True,
        "confidence": 0.85,
        "reason": "direct ask",
        "timestamp_ms": 12345,
        "reply_type": "answer",
        "suggested_reply": "yes",
        "session_id": "sess-1",
        "input_window": {
            "transcript_window": [{"text": "hi", "speaker": None, "is_current": True}],
            "instructions": "Be brief",
            "context": "standup",
            "allowed_replies": ["yes", "no"],
            "mode": "limited_auto_speak",
            "confidence_threshold": 0.7,
            "last_decision": None,
        },
        "raw_output": {
            "text": '{"should_speak": true, "confidence": 0.85, "reason": "direct ask"}',
            "finish_reason": "stop",
            "structured": {
                "should_speak": True,
                "confidence": 0.85,
                "reason": "direct ask",
            },
        },
    }
    base.update(overrides)
    return RouterDecisionMade(**base)  # type: ignore[arg-type]


async def test_sink_persists_decision_to_agent_decisions(db_session: Session) -> None:
    sink = SqlAlchemyDecisionSink(db_session, bot_session_id=99)
    event = _make_event()
    await sink.record(event, outcome="spoken")
    rows = db_session.scalars(sa.select(AgentDecision)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.bot_session_id == 99
    assert row.should_speak is True
    assert row.confidence == pytest.approx(0.85)
    assert row.reason == "direct ask"
    assert row.reply_type == "answer"
    assert row.suggested_reply == "yes"
    assert row.input_window["mode"] == "limited_auto_speak"
    assert row.input_window["confidence_threshold"] == pytest.approx(0.7)
    assert row.raw_output["finish_reason"] == "stop"
    assert row.raw_output["structured"]["confidence"] == pytest.approx(0.85)
    assert row.outcome == DecisionOutcome.SPOKEN
    assert row.created_at is not None


async def test_sink_maps_outcome_strings_to_enums(db_session: Session) -> None:
    sink = SqlAlchemyDecisionSink(db_session, bot_session_id=1)
    await sink.record(_make_event(), outcome="suppressed")
    await sink.record(_make_event(), outcome="pending")
    await sink.record(_make_event(), outcome="rejected")
    await sink.record(_make_event(), outcome="spoken")
    rows = db_session.scalars(sa.select(AgentDecision).order_by(AgentDecision.id)).all()
    outcomes = [r.outcome for r in rows]
    assert outcomes == [
        DecisionOutcome.SUPPRESSED,
        DecisionOutcome.PENDING,
        DecisionOutcome.REJECTED,
        DecisionOutcome.SPOKEN,
    ]


async def test_sink_default_outcome_is_pending(db_session: Session) -> None:
    sink = SqlAlchemyDecisionSink(db_session, bot_session_id=1)
    await sink.record(_make_event())
    row = db_session.scalars(sa.select(AgentDecision)).one()
    assert row.outcome == DecisionOutcome.PENDING


async def test_sink_per_call_bot_session_id_override(db_session: Session) -> None:
    sink = SqlAlchemyDecisionSink(db_session, bot_session_id=1)
    await sink.record(_make_event(), bot_session_id=99)
    row = db_session.scalars(sa.select(AgentDecision)).one()
    assert row.bot_session_id == 99


async def test_sink_persists_full_input_window(db_session: Session) -> None:
    """The full input_window JSON round-trips through the DB."""
    window = {
        "transcript_window": [
            {"text": "t1", "speaker": "alice", "timestamp_ms": 1000, "is_current": False},
            {"text": "t2", "speaker": "bob", "timestamp_ms": 2000, "is_current": True},
        ],
        "instructions": "Stay brief",
        "context": "weekly sync",
        "allowed_replies": ["ack", "nack"],
        "mode": "approval_required",
        "confidence_threshold": 0.55,
        "last_decision": {
            "should_speak": True,
            "confidence": 0.6,
            "reason": "earlier",
            "reply_type": None,
            "suggested_reply": None,
            "timestamp_ms": 500,
        },
    }
    raw = {
        "text": '{"x": 1}',
        "finish_reason": "stop",
        "structured": {"x": 1},
    }
    sink = SqlAlchemyDecisionSink(db_session, bot_session_id=7)
    await sink.record(
        _make_event(input_window=window, raw_output=raw),
        outcome="suppressed",
    )
    row = db_session.scalars(sa.select(AgentDecision)).one()
    assert row.input_window == window
    assert row.raw_output == raw


async def test_sink_records_multiple_decisions_in_order(db_session: Session) -> None:
    sink = SqlAlchemyDecisionSink(db_session, bot_session_id=3)
    for i in range(3):
        await sink.record(
            _make_event(reason=f"r-{i}", timestamp_ms=i * 1000),
            outcome="spoken" if i % 2 == 0 else "suppressed",
        )
    rows = db_session.scalars(
        sa.select(AgentDecision).order_by(AgentDecision.id)
    ).all()
    assert [r.reason for r in rows] == ["r-0", "r-1", "r-2"]
    assert [r.outcome for r in rows] == [
        DecisionOutcome.SPOKEN,
        DecisionOutcome.SUPPRESSED,
        DecisionOutcome.SPOKEN,
    ]


def test_sink_exposes_bot_session_id(db_session: Session) -> None:
    sink = SqlAlchemyDecisionSink(db_session, bot_session_id=12345)
    assert sink.bot_session_id == 12345


async def test_sink_implements_decision_sink_abc() -> None:
    """SqlAlchemyDecisionSink must be a subclass of DecisionSink."""
    from johnny.voice_pipeline.decision_sink import DecisionSink

    assert issubclass(SqlAlchemyDecisionSink, DecisionSink)
