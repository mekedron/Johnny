"""Tests for the ``agent_decisions`` parity guard (INV-2, Johnny-ckz.28.2).

The guard is a SQLAlchemy ``before_insert`` / ``before_update`` mapper event,
so it covers *every* path that writes a decision row — the event-sourced
subscriber, the ``SqlAlchemyDecisionSink``, and any
test fixture — without each path re-implementing the check. A ``final_text``
that diverges from ``decision_recommended_text`` is rejected at flush time
unless both ``override_actor`` and ``divergence_reason`` are set, making a
silent decision↔utterance swap impossible to persist.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import (
    AgentDecision,
    AgentUtterance,
    BotSession,
    CalendarEvent,
    DecisionOutcome,
    DecisionParityError,
    GoogleAccount,
    MeetingConfig,
    NoReplyReason,
    SessionTiming,
    TerminalState,
    decision_texts_diverge,
)


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    Base.metadata.create_all(
        bind=eng,
        tables=[
            GoogleAccount.__table__,  # type: ignore[list-item]
            CalendarEvent.__table__,  # type: ignore[list-item]
            MeetingConfig.__table__,  # type: ignore[list-item]
            BotSession.__table__,  # type: ignore[list-item]
            AgentDecision.__table__,  # type: ignore[list-item]
            AgentUtterance.__table__,  # type: ignore[list-item]
            SessionTiming.__table__,  # type: ignore[list-item]
        ],
    )
    return eng


@pytest.fixture
def db(engine: sa.Engine) -> Iterator[Session]:
    sess = Session(engine)
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture
def session_id(db: Session) -> int:
    row = BotSession(meeting_config_id=None)
    db.add(row)
    db.flush()
    return int(row.id)


def _decision(session_id: int, **overrides: Any) -> AgentDecision:
    base: dict[str, Any] = dict(
        bot_session_id=session_id,
        should_speak=True,
        confidence=0.9,
        reason="because",
        input_window={},
        raw_output={},
        outcome=DecisionOutcome.SPOKEN,
    )
    base.update(overrides)
    return AgentDecision(**base)


def test_guard_rejects_silent_divergence(db: Session, session_id: int) -> None:
    db.add(
        _decision(
            session_id,
            decision_recommended_text="say A",
            final_text="say B",
        )
    )
    with pytest.raises(DecisionParityError):
        db.flush()


def test_guard_allows_audited_divergence(db: Session, session_id: int) -> None:
    db.add(
        _decision(
            session_id,
            decision_recommended_text="say A",
            final_text="say B",
            override_actor="answer_llm",
            divergence_reason="answer LLM rephrased",
        )
    )
    db.flush()
    row = db.scalars(sa.select(AgentDecision)).one()
    assert row.final_text == "say B"
    assert row.override_actor == "answer_llm"


def test_guard_allows_matching_text(db: Session, session_id: int) -> None:
    db.add(
        _decision(
            session_id,
            decision_recommended_text="identical",
            final_text="identical",
        )
    )
    db.flush()  # no raise


# --- Terminal-state invariant (INV-1, Johnny-ckz.28.3) -------------------


def test_guard_rejects_no_reply_without_reason(db: Session, session_id: int) -> None:
    """A no_reply terminal must name its suppressor — the guard enforces it."""
    db.add(
        _decision(
            session_id,
            outcome=DecisionOutcome.SUPPRESSED,
            terminal_state=TerminalState.NO_REPLY,
            no_reply_reason=None,
        )
    )
    with pytest.raises(DecisionParityError):
        db.flush()


def test_guard_allows_no_reply_with_reason(db: Session, session_id: int) -> None:
    db.add(
        _decision(
            session_id,
            outcome=DecisionOutcome.SUPPRESSED,
            terminal_state=TerminalState.NO_REPLY,
            no_reply_reason=NoReplyReason.ROUTER_DECLINED,
        )
    )
    db.flush()  # no raise
    row = db.scalars(sa.select(AgentDecision)).one()
    assert row.terminal_state == TerminalState.NO_REPLY


def test_guard_allows_replied_without_no_reply_reason(
    db: Session, session_id: int
) -> None:
    """replied / pending_approval never need a no_reply_reason."""
    db.add(
        _decision(
            session_id,
            outcome=DecisionOutcome.SPOKEN,
            terminal_state=TerminalState.REPLIED,
        )
    )
    db.flush()  # no raise


def test_guard_allows_null_terminal_state(db: Session, session_id: int) -> None:
    """The in-progress window (terminal_state NULL) is allowed."""
    db.add(_decision(session_id, terminal_state=None))
    db.flush()  # no raise


def test_guard_ignores_whitespace_only_difference(
    db: Session, session_id: int
) -> None:
    db.add(
        _decision(
            session_id,
            decision_recommended_text="hello   world",
            final_text="hello world",
        )
    )
    db.flush()  # normalised-equal → not a divergence


def test_guard_allows_null_recommendation(db: Session, session_id: int) -> None:
    db.add(
        _decision(
            session_id,
            decision_recommended_text=None,
            final_text="spoke something",
        )
    )
    db.flush()  # nothing to reconcile against


def test_guard_requires_both_override_fields(db: Session, session_id: int) -> None:
    # actor set, reason missing → still rejected.
    db.add(
        _decision(
            session_id,
            decision_recommended_text="A",
            final_text="B",
            override_actor="answer_llm",
        )
    )
    with pytest.raises(DecisionParityError):
        db.flush()


def test_guard_fires_on_update(db: Session, session_id: int) -> None:
    decision = _decision(
        session_id, decision_recommended_text="A", final_text=None
    )
    db.add(decision)
    db.flush()
    decision.final_text = "B"  # diverge without an audit
    with pytest.raises(DecisionParityError):
        db.flush()


def test_decision_texts_diverge_helper() -> None:
    assert decision_texts_diverge("a", "b") is True
    assert decision_texts_diverge("a", "a") is False
    assert decision_texts_diverge("a  b", "a b") is False
    assert decision_texts_diverge(None, "b") is False
    assert decision_texts_diverge("a", None) is False
    assert decision_texts_diverge(None, None) is False
