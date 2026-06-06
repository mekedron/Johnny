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
from app.db.models import BotSession, BotSessionStatus
from app.services import session_status_subscriber
from app.services.session_status_subscriber import (
    SESSION_STATUS_EVENT_TYPE,
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
    Base.metadata.create_all(bind=eng, tables=[BotSession.__table__])  # type: ignore[list-item]
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
