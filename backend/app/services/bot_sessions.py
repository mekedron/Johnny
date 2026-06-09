"""Bot session lifecycle state transitions persisted to ``bot_sessions``.

The meet-worker container is intentionally SQLAlchemy-free, so it publishes
:class:`~johnny.voice_pipeline.events.SessionStatusChanged` events on the
Redis pub/sub channel (US-020). A subscriber in the API process (wired by
US-029 / US-030 / US-031) calls these helpers to apply the corresponding
update to the ``bot_sessions`` row. Direct calls from a scheduler task
are equally fine — they share the same Session lifetime semantics as
:func:`~app.db.session.session_scope`.

Each helper looks up the row, applies the transition, and flushes the
session so the caller's outer commit makes it durable. Missing rows
raise :class:`BotSessionNotFoundError` so callers can decide whether to
retry, log + move on, or surface to the operator.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.models import BotSession, BotSessionStatus

logger = logging.getLogger(__name__)


class BotSessionNotFoundError(LookupError):
    """No ``bot_sessions`` row matches the given id."""


def _load(session: Session, bot_session_id: int) -> BotSession:
    row = session.get(BotSession, bot_session_id)
    if row is None:
        raise BotSessionNotFoundError(
            f"no bot_sessions row with id={bot_session_id}"
        )
    return row


def mark_session_joining(session: Session, bot_session_id: int) -> BotSession:
    """Transition the session to ``joining`` (meet-worker has launched)."""
    row = _load(session, bot_session_id)
    row.status = BotSessionStatus.JOINING
    session.flush()
    return row


def mark_session_joined(session: Session, bot_session_id: int) -> BotSession:
    """Transition to ``joined`` and stamp ``started_at`` if unset."""
    row = _load(session, bot_session_id)
    row.status = BotSessionStatus.JOINED
    if row.started_at is None:
        row.started_at = datetime.now(UTC)
    # Clear any previous error_reason from a retry path.
    row.error_reason = None
    session.flush()
    return row


def mark_session_failed(
    session: Session,
    bot_session_id: int,
    error_reason: str,
) -> BotSession:
    """Transition to ``failed`` with ``error_reason`` set and ``ended_at`` stamped."""
    row = _load(session, bot_session_id)
    row.status = BotSessionStatus.FAILED
    row.error_reason = error_reason
    if row.ended_at is None:
        row.ended_at = datetime.now(UTC)
    session.flush()
    return row


def mark_session_ended(session: Session, bot_session_id: int) -> BotSession:
    """Transition to ``ended`` (clean shutdown) and stamp ``ended_at``."""
    row = _load(session, bot_session_id)
    row.status = BotSessionStatus.ENDED
    if row.ended_at is None:
        row.ended_at = datetime.now(UTC)
    session.flush()
    return row


def mark_session_waiting_for_relogin(
    session: Session,
    bot_session_id: int,
    error_reason: str,
) -> BotSession:
    """Transition to ``waiting_for_relogin`` — the bot account is signed out.

    A *soft*, recoverable state (Johnny-ebf): the operator is asked to
    re-login the account, so unlike :func:`mark_session_failed` we record the
    human-readable ``error_reason`` but deliberately do NOT stamp ``ended_at``
    (the session has not ended — it is waiting). The scheduler later settles it
    to ``failed`` if the meeting ends or the re-login times out.
    """
    row = _load(session, bot_session_id)
    row.status = BotSessionStatus.WAITING_FOR_RELOGIN
    row.error_reason = error_reason
    session.flush()
    return row


__all__ = [
    "BotSessionNotFoundError",
    "mark_session_ended",
    "mark_session_failed",
    "mark_session_joined",
    "mark_session_joining",
    "mark_session_waiting_for_relogin",
]
