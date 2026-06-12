"""Tests for the SQLAlchemy-backed decision sink.

The ``resolve_confidence_threshold`` helper (profile/meeting threshold
fallback) died with the Johnny-trt.41 agents rebuild — the threshold is now
frozen on ``bot_sessions.agent_snapshot`` at dispatch and consumed straight
from the job config; only the decision sink remains in this module.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import AgentDecision, DecisionOutcome
from app.services.router_decisions import SqlAlchemyDecisionSink
from johnny.voice_pipeline.events import RouterDecisionMade

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
    await sink.record(_make_event(), outcome="suggested")
    rows = db_session.scalars(sa.select(AgentDecision).order_by(AgentDecision.id)).all()
    outcomes = [r.outcome for r in rows]
    assert outcomes == [
        DecisionOutcome.SUPPRESSED,
        DecisionOutcome.PENDING,
        DecisionOutcome.REJECTED,
        DecisionOutcome.SPOKEN,
        DecisionOutcome.SUGGESTED,
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


async def test_sink_record_returns_decision_id(db_session: Session) -> None:
    """``record`` returns the persisted row's primary key (US-027)."""
    sink = SqlAlchemyDecisionSink(db_session, bot_session_id=42)
    decision_id = await sink.record(_make_event(), outcome="pending")
    row = db_session.scalars(sa.select(AgentDecision)).one()
    assert decision_id == row.id


async def test_sink_update_outcome_flips_existing_row(db_session: Session) -> None:
    """``update_outcome`` flips an existing row's outcome (US-027 approval flow)."""
    sink = SqlAlchemyDecisionSink(db_session, bot_session_id=1)
    decision_id = await sink.record(_make_event(), outcome="pending")
    assert decision_id is not None
    await sink.update_outcome(decision_id, "spoken")
    row = db_session.scalars(sa.select(AgentDecision)).one()
    assert row.outcome == DecisionOutcome.SPOKEN


async def test_sink_update_outcome_unknown_id_logs_warning(
    db_session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Updating a non-existent decision_id logs a warning and returns silently."""
    import logging

    sink = SqlAlchemyDecisionSink(db_session, bot_session_id=1)
    with caplog.at_level(logging.WARNING, logger="app.services.router_decisions"):
        await sink.update_outcome(9999, "rejected")
    assert any("not found" in rec.message for rec in caplog.records)
