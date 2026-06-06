"""Tests for app.services.session_status_subscriber.

Covers the pure ``apply_status_event`` reducer (status mapping, payload
validation) and the end-to-end loop with a fake message stream.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import (
    AgentDecision,
    BotSession,
    BotSessionStatus,
    DecisionOutcome,
)
from app.services import session_status_subscriber
from app.services.session_status_subscriber import (
    ROUTER_DECISION_EVENT_TYPE,
    SESSION_STATUS_EVENT_TYPE,
    _PendingApprovalEvent,
    apply_router_decision_event,
    apply_status_event,
    run_subscriber,
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
            BotSession.__table__,  # type: ignore[list-item]
            AgentDecision.__table__,  # type: ignore[list-item]
        ],
    )
    return eng


@pytest.fixture
def db_session(engine: sa.Engine) -> Iterator[Session]:
    sess = Session(engine)
    try:
        yield sess
    finally:
        sess.close()


def _seed(
    db_session: Session,
    *,
    status: BotSessionStatus = BotSessionStatus.JOINING,
) -> BotSession:
    row = BotSession(meeting_config_id=1, status=status)
    db_session.add(row)
    db_session.flush()
    return row


def _payload(
    *,
    session_id: Any,
    status: str,
    error_reason: str | None = None,
    event_type: str = SESSION_STATUS_EVENT_TYPE,
) -> dict[str, Any]:
    return {
        "type": event_type,
        "session_id": session_id,
        "status": status,
        "error_reason": error_reason,
        "timestamp_ms": 0,
    }


def test_apply_status_event_joined_sets_status_and_clears_error(
    db_session: Session,
) -> None:
    row = _seed(db_session)
    row.error_reason = "earlier flake"
    db_session.flush()

    applied = apply_status_event(
        db_session, _payload(session_id=row.id, status="joined")
    )

    assert applied is True
    db_session.refresh(row)
    assert row.status == BotSessionStatus.JOINED
    assert row.started_at is not None
    assert row.error_reason is None


def test_apply_status_event_failed_records_reason(db_session: Session) -> None:
    row = _seed(db_session)
    applied = apply_status_event(
        db_session,
        _payload(
            session_id=row.id,
            status="failed",
            error_reason="sign_in_required: cookies missing",
        ),
    )
    assert applied is True
    db_session.refresh(row)
    assert row.status == BotSessionStatus.FAILED
    assert row.error_reason == "sign_in_required: cookies missing"
    assert row.ended_at is not None


def test_apply_status_event_failed_falls_back_to_default_reason(
    db_session: Session,
) -> None:
    row = _seed(db_session)
    applied = apply_status_event(
        db_session,
        _payload(session_id=row.id, status="failed", error_reason=None),
    )
    assert applied is True
    db_session.refresh(row)
    assert row.status == BotSessionStatus.FAILED
    assert row.error_reason is not None
    assert "meet-worker failure" in row.error_reason


def test_apply_status_event_ended_sets_status(db_session: Session) -> None:
    row = _seed(db_session, status=BotSessionStatus.JOINED)
    applied = apply_status_event(
        db_session, _payload(session_id=row.id, status="ended")
    )
    assert applied is True
    db_session.refresh(row)
    assert row.status == BotSessionStatus.ENDED


def test_apply_status_event_scheduled_is_noop(db_session: Session) -> None:
    row = _seed(db_session, status=BotSessionStatus.JOINING)
    applied = apply_status_event(
        db_session, _payload(session_id=row.id, status="scheduled")
    )
    assert applied is False
    db_session.refresh(row)
    assert row.status == BotSessionStatus.JOINING


def test_apply_status_event_drops_wrong_event_type(db_session: Session) -> None:
    row = _seed(db_session)
    applied = apply_status_event(
        db_session,
        _payload(
            session_id=row.id, status="joined", event_type="transcript_finalized"
        ),
    )
    assert applied is False
    db_session.refresh(row)
    assert row.status == BotSessionStatus.JOINING


def test_apply_status_event_drops_missing_session_id(db_session: Session) -> None:
    payload = _payload(session_id=None, status="joined")
    applied = apply_status_event(db_session, payload)
    assert applied is False


def test_apply_status_event_drops_non_int_session_id(db_session: Session) -> None:
    payload = _payload(session_id="not-an-int", status="joined")
    applied = apply_status_event(db_session, payload)
    assert applied is False


def test_apply_status_event_drops_missing_status(db_session: Session) -> None:
    row = _seed(db_session)
    payload = _payload(session_id=row.id, status="")
    payload["status"] = None  # force the type check to fail
    applied = apply_status_event(db_session, payload)
    assert applied is False


def test_apply_status_event_accepts_string_session_id(db_session: Session) -> None:
    row = _seed(db_session)
    applied = apply_status_event(
        db_session, _payload(session_id=str(row.id), status="joined")
    )
    assert applied is True
    db_session.refresh(row)
    assert row.status == BotSessionStatus.JOINED


def test_apply_status_event_ignores_unknown_status(db_session: Session) -> None:
    row = _seed(db_session)
    applied = apply_status_event(
        db_session, _payload(session_id=row.id, status="dreaming")
    )
    assert applied is False
    db_session.refresh(row)
    assert row.status == BotSessionStatus.JOINING


# --- apply_router_decision_event approval_pending wiring -----------------


def _router_decision_payload(
    *,
    session_id: int,
    mode: str,
    should_speak: bool = True,
    suggested_reply: str = "yes",
    reason: str = "ask",
    reply_type: str | None = "answer",
    approval_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    input_window: dict[str, Any] = {"mode": mode}
    if approval_timeout_seconds is not None:
        input_window["approval_timeout_seconds"] = approval_timeout_seconds
    return {
        "type": ROUTER_DECISION_EVENT_TYPE,
        "session_id": session_id,
        "should_speak": should_speak,
        "confidence": 0.9,
        "reason": reason,
        "reply_type": reply_type,
        "suggested_reply": suggested_reply,
        "input_window": input_window,
        "raw_output": {},
        "timestamp_ms": 0,
    }


def test_apply_router_decision_event_returns_pending_event_for_approval_required(
    db_session: Session,
) -> None:
    """Approval-required + should_speak surfaces a follow-up event (Johnny-hn6).

    Before the fix the subscriber inserted the PENDING row but never
    advertised it on the WS channel, so UIs had to refresh to discover
    new approval cards.
    """
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    applied, pending = apply_router_decision_event(
        db_session,
        _router_decision_payload(
            session_id=bot_session.id,
            mode="approval_required",
            suggested_reply="how are you?",
            reason="user-asked",
            reply_type="answer",
            approval_timeout_seconds=12.0,
        ),
    )
    assert applied is True
    assert pending is not None
    assert pending.session_id == bot_session.id
    assert pending.suggested_reply == "how are you?"
    assert pending.reason == "user-asked"
    assert pending.reply_type == "answer"
    assert pending.timeout_s == 12.0
    row = db_session.get(AgentDecision, pending.decision_id)
    assert row is not None
    assert row.outcome == DecisionOutcome.PENDING


def test_apply_router_decision_event_pending_event_uses_default_timeout(
    db_session: Session,
) -> None:
    """Missing ``approval_timeout_seconds`` falls back to the documented default."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    applied, pending = apply_router_decision_event(
        db_session,
        _router_decision_payload(
            session_id=bot_session.id, mode="approval_required"
        ),
    )
    assert applied is True
    assert pending is not None
    assert pending.timeout_s == session_status_subscriber.DEFAULT_APPROVAL_TIMEOUT_S


def test_apply_router_decision_event_returns_no_pending_event_for_suggest_only(
    db_session: Session,
) -> None:
    """Non-approval modes must not emit the approval_pending follow-up."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    applied, pending = apply_router_decision_event(
        db_session,
        _router_decision_payload(
            session_id=bot_session.id, mode="suggest_only"
        ),
    )
    assert applied is True
    assert pending is None


def test_apply_router_decision_event_returns_no_pending_event_when_not_speak(
    db_session: Session,
) -> None:
    """should_speak=False is SUPPRESSED — never a pending approval card."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    applied, pending = apply_router_decision_event(
        db_session,
        _router_decision_payload(
            session_id=bot_session.id,
            mode="approval_required",
            should_speak=False,
        ),
    )
    assert applied is True
    assert pending is None


# --- run_subscriber loop end-to-end --------------------------------------


@pytest.mark.asyncio
async def test_run_subscriber_persists_payloads_via_factory(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _seed(db_session)
    # Commit so a fresh session_scope() opened by the subscriber sees the row.
    db_session.commit()

    # Patch the subscriber's session_scope to use the test engine so the
    # subscriber's writes land in the same in-memory DB the test inspects.
    engine = db_session.bind
    assert engine is not None

    from contextlib import contextmanager

    @contextmanager
    def fake_scope() -> Iterator[Session]:
        sess = Session(engine)
        try:
            yield sess
            sess.commit()
        except BaseException:
            sess.rollback()
            raise
        finally:
            sess.close()

    monkeypatch.setattr(
        session_status_subscriber, "session_scope", fake_scope
    )

    payloads = [
        _payload(session_id=row.id, status="joined"),
        _payload(
            session_id=row.id,
            status="failed",
            error_reason="join_timeout: preview UI never settled",
        ),
    ]

    async def factory(_url: str) -> AsyncIterator[dict[str, Any]]:
        for p in payloads:
            yield p

    await run_subscriber("redis://ignored", message_stream_factory=factory)

    # Re-read via a fresh session so we don't see stale identity-map state.
    refreshed_session = Session(engine)
    try:
        refreshed = refreshed_session.get(BotSession, row.id)
        assert refreshed is not None
        # Final payload wins — the row should be failed with the reason.
        assert refreshed.status == BotSessionStatus.FAILED
        assert refreshed.error_reason is not None
        assert "join_timeout" in refreshed.error_reason
    finally:
        refreshed_session.close()


@pytest.mark.asyncio
async def test_run_subscriber_emits_approval_pending_for_pending_row(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a router_decision_made payload fires the WS event (Johnny-hn6).

    The subscriber persists the row first, then invokes the pending
    publisher with the new id. UIs receive ``approval_pending`` on the
    session WS channel within a single subscriber loop iteration — no
    refresh required.
    """
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    engine = db_session.bind
    assert engine is not None

    from contextlib import contextmanager

    @contextmanager
    def fake_scope() -> Iterator[Session]:
        sess = Session(engine)
        try:
            yield sess
            sess.commit()
        except BaseException:
            sess.rollback()
            raise
        finally:
            sess.close()

    monkeypatch.setattr(
        session_status_subscriber, "session_scope", fake_scope
    )

    payloads = [
        _router_decision_payload(
            session_id=bot_session.id,
            mode="approval_required",
            suggested_reply="sure",
            reason="ask",
            reply_type="answer",
            approval_timeout_seconds=20.0,
        ),
    ]

    async def factory(_url: str) -> AsyncIterator[dict[str, Any]]:
        for p in payloads:
            yield p

    captured: list[_PendingApprovalEvent] = []

    async def fake_publisher(event: _PendingApprovalEvent) -> None:
        captured.append(event)

    async def fake_publisher_factory(_url: str) -> Any:
        return fake_publisher

    await run_subscriber(
        "redis://ignored",
        message_stream_factory=factory,
        pending_publisher_factory=fake_publisher_factory,
    )

    assert len(captured) == 1
    event = captured[0]
    assert event.session_id == bot_session.id
    assert event.suggested_reply == "sure"
    assert event.reason == "ask"
    assert event.reply_type == "answer"
    assert event.timeout_s == 20.0
    # The decision row that ID points at must actually exist.
    refreshed = Session(engine)
    try:
        row = refreshed.get(AgentDecision, event.decision_id)
        assert row is not None
        assert row.outcome == DecisionOutcome.PENDING
    finally:
        refreshed.close()


@pytest.mark.asyncio
async def test_run_subscriber_does_not_emit_pending_for_non_approval_mode(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """suggest_only / auto-speak modes never go through the pending publisher."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    engine = db_session.bind
    assert engine is not None

    from contextlib import contextmanager

    @contextmanager
    def fake_scope() -> Iterator[Session]:
        sess = Session(engine)
        try:
            yield sess
            sess.commit()
        except BaseException:
            sess.rollback()
            raise
        finally:
            sess.close()

    monkeypatch.setattr(
        session_status_subscriber, "session_scope", fake_scope
    )

    payloads = [
        _router_decision_payload(
            session_id=bot_session.id, mode="suggest_only"
        ),
        _router_decision_payload(
            session_id=bot_session.id, mode="free_auto_speak"
        ),
    ]

    async def factory(_url: str) -> AsyncIterator[dict[str, Any]]:
        for p in payloads:
            yield p

    captured: list[_PendingApprovalEvent] = []

    async def fake_publisher(event: _PendingApprovalEvent) -> None:
        captured.append(event)

    async def fake_publisher_factory(_url: str) -> Any:
        return fake_publisher

    await run_subscriber(
        "redis://ignored",
        message_stream_factory=factory,
        pending_publisher_factory=fake_publisher_factory,
    )

    assert captured == []
