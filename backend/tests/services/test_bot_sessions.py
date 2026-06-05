"""Tests for app.services.bot_sessions."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import overload

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import BotSession, BotSessionStatus
from app.services.bot_sessions import (
    BotSessionNotFoundError,
    mark_session_ended,
    mark_session_failed,
    mark_session_joined,
    mark_session_joining,
)


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    # Only need the bot_sessions table — FKs point at meeting_configs but
    # SQLite doesn't enforce FKs by default, so the single-table fixture works.
    Base.metadata.create_all(bind=eng, tables=[BotSession.__table__])  # type: ignore[list-item]
    return eng


@pytest.fixture
def db_session(engine: sa.Engine) -> Iterator[Session]:
    sess = Session(engine)
    try:
        yield sess
    finally:
        sess.close()


def _make_session(
    db_session: Session,
    *,
    status: BotSessionStatus = BotSessionStatus.SCHEDULED,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    error_reason: str | None = None,
) -> BotSession:
    row = BotSession(
        meeting_config_id=1,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        error_reason=error_reason,
    )
    db_session.add(row)
    db_session.flush()
    return row


@overload
def _aware(dt: datetime) -> datetime: ...
@overload
def _aware(dt: None) -> None: ...
def _aware(dt: datetime | None) -> datetime | None:
    """Coerce naive datetimes (sqlite round-trip) back to UTC for comparison."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# --- mark_session_joining ------------------------------------------------


def test_mark_session_joining_updates_status(db_session: Session) -> None:
    row = _make_session(db_session)
    returned = mark_session_joining(db_session, row.id)
    db_session.refresh(row)
    assert row.status == BotSessionStatus.JOINING
    assert returned is row


def test_mark_session_joining_raises_for_unknown_id(db_session: Session) -> None:
    with pytest.raises(BotSessionNotFoundError):
        mark_session_joining(db_session, 9999)


# --- mark_session_joined --------------------------------------------------


def test_mark_session_joined_updates_status(db_session: Session) -> None:
    row = _make_session(db_session, status=BotSessionStatus.JOINING)
    before = datetime.now(UTC)
    mark_session_joined(db_session, row.id)
    db_session.refresh(row)
    assert row.status == BotSessionStatus.JOINED
    assert row.started_at is not None
    assert _aware(row.started_at) >= before - timedelta(seconds=1)


def test_mark_session_joined_clears_existing_error_reason(db_session: Session) -> None:
    """A retry that succeeds should not leave an old error in place."""
    row = _make_session(
        db_session,
        status=BotSessionStatus.FAILED,
        error_reason="previous attempt timed out",
    )
    mark_session_joined(db_session, row.id)
    db_session.refresh(row)
    assert row.status == BotSessionStatus.JOINED
    assert row.error_reason is None


def test_mark_session_joined_preserves_existing_started_at(db_session: Session) -> None:
    original = datetime.now(UTC) - timedelta(minutes=5)
    row = _make_session(db_session, started_at=original)
    mark_session_joined(db_session, row.id)
    db_session.refresh(row)
    assert row.started_at is not None
    delta = abs((_aware(row.started_at) - original).total_seconds())
    assert delta < 1.0


def test_mark_session_joined_raises_for_unknown_id(db_session: Session) -> None:
    with pytest.raises(BotSessionNotFoundError):
        mark_session_joined(db_session, 9999)


# --- mark_session_failed -------------------------------------------------


def test_mark_session_failed_sets_status_and_reason(db_session: Session) -> None:
    row = _make_session(db_session, status=BotSessionStatus.JOINING)
    before = datetime.now(UTC)
    mark_session_failed(db_session, row.id, "access denied by host")
    db_session.refresh(row)
    assert row.status == BotSessionStatus.FAILED
    assert row.error_reason == "access denied by host"
    assert row.ended_at is not None
    assert _aware(row.ended_at) >= before - timedelta(seconds=1)


def test_mark_session_failed_preserves_existing_ended_at(db_session: Session) -> None:
    original = datetime.now(UTC) - timedelta(minutes=10)
    row = _make_session(db_session, ended_at=original)
    mark_session_failed(db_session, row.id, "later failure")
    db_session.refresh(row)
    assert row.ended_at is not None
    delta = abs((_aware(row.ended_at) - original).total_seconds())
    assert delta < 1.0


def test_mark_session_failed_raises_for_unknown_id(db_session: Session) -> None:
    with pytest.raises(BotSessionNotFoundError):
        mark_session_failed(db_session, 9999, "x")


# --- mark_session_ended --------------------------------------------------


def test_mark_session_ended_updates_status(db_session: Session) -> None:
    row = _make_session(db_session, status=BotSessionStatus.JOINED)
    before = datetime.now(UTC)
    mark_session_ended(db_session, row.id)
    db_session.refresh(row)
    assert row.status == BotSessionStatus.ENDED
    assert row.ended_at is not None
    assert _aware(row.ended_at) >= before - timedelta(seconds=1)


def test_mark_session_ended_preserves_existing_ended_at(db_session: Session) -> None:
    original = datetime.now(UTC) - timedelta(hours=1)
    row = _make_session(db_session, ended_at=original)
    mark_session_ended(db_session, row.id)
    db_session.refresh(row)
    assert row.ended_at is not None
    delta = abs((_aware(row.ended_at) - original).total_seconds())
    assert delta < 1.0


def test_mark_session_ended_raises_for_unknown_id(db_session: Session) -> None:
    with pytest.raises(BotSessionNotFoundError):
        mark_session_ended(db_session, 9999)
