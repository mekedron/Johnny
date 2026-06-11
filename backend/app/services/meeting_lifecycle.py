"""Per-meeting bot participation lifecycle (Johnny-trt.56).

Meetings need a real "the bot is finished here" state: ending a session only
removes the active ``bot_sessions`` row, and the scheduler's dispatch
condition (enabled + within join window + no active session) would re-queue
the bot on the very next poll. Dismissal makes "end for this meeting" durable
without introducing a persisted state machine.

Data model — three nullable columns on ``meeting_configs``, always set and
cleared together:

* ``bot_dismissed_at``    — when the dismissal happened (audit / UI).
* ``bot_dismissed_by``    — :class:`~app.db.models.BotDismissActor`
  (``ui`` | ``voice`` | ``schedule``).
* ``bot_dismissed_until`` — the linked event's ``end_time`` as scheduled at
  dismissal time; the occurrence boundary the dismissal is scoped to.

Occurrence-scoping rule (the documented contract)
-------------------------------------------------

A dismissal is **in force** iff::

    bot_dismissed_at IS NOT NULL
    AND calendar_event.start_time <= bot_dismissed_until

i.e. the event's *current* window still overlaps the window that was
dismissed. Consequences:

* **Recurring meetings rejoin at the next occurrence by design** — calendar
  sync uses ``singleEvents=true``, so every occurrence is its own
  ``calendar_events`` row with its own ``meeting_configs`` row; a dismissal
  never outlives the row it was stamped on.
* **A meeting rescheduled past the dismissed window is a new occurrence** —
  if the organizer moves the event so its start falls after the captured
  ``bot_dismissed_until``, the dismissal lapses and the bot is back on
  schedule (no stale "never joins again" surprises on moved one-offs).
* **A mid-meeting extension stays dismissed** — pushing ``end_time`` later
  keeps ``start_time`` inside the dismissed window, so the bot does not
  sneak back in during the extension.
* After the occurrence ends the dismissal is moot — the scheduler's
  ``end_time > now`` dispatch condition already excludes the row, and the
  derived state reads ``ended``.

The coarse ``bot_state`` surfaced to the UI (``scheduled`` | ``active`` |
``dismissed`` | ``ended``) is **derived** by :func:`derive_bot_state` from
these columns + the session table + the occurrence clock; nothing else is
persisted, so there is no scheduler-maintained state to drift.

Voice seam (Johnny-trt.57): the in-meeting ``meeting.leave`` tool calls
:func:`dismiss_bot_for_meeting` with ``actor=BotDismissActor.VOICE`` — same
function, same events, no parallel code path.

FORWARD-COMPAT (Johnny-trt.45): the Phase-6 agents pivot reshapes
``meeting_configs``; the three dismissal columns must be carried through
that rebuild verbatim.
"""

from __future__ import annotations

import enum
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    BotDismissActor,
    BotSession,
    BotSessionStatus,
    MeetingConfig,
)

logger = logging.getLogger(__name__)

# Channel the meeting-level state-change event is always published on —
# the same global channel the calendar polling worker uses, so the layout
# and calendar page (already subscribed to /ws/global) react with a refetch.
GLOBAL_CALENDAR_CHANNEL = "johnny.global.calendar"
# Per-session channel prefix; used when a dismissal stops a live session so
# the open session-detail page sees the state change on its own socket.
SESSION_CHANNEL_PREFIX = "johnny.session."

# Wire ``type`` of the state-change event (passes through the WS unmapped).
BOT_STATE_EVENT_TYPE = "meeting_bot_state_changed"

# Session statuses that count as "the bot is (or is about to be) in the
# meeting". Mirrors session_scheduler._ACTIVE_STATUSES; duplicated here to
# avoid an import cycle (session_scheduler imports nothing from us, and we
# only need the stop helper lazily inside dismiss).
_ACTIVE_STATUSES = (
    BotSessionStatus.SCHEDULED,
    BotSessionStatus.JOINING,
    BotSessionStatus.JOINED,
    BotSessionStatus.WAITING_FOR_RELOGIN,
)


class MeetingBotState(enum.StrEnum):
    """Coarse, derived bot-participation state for one meeting occurrence."""

    SCHEDULED = "scheduled"
    ACTIVE = "active"
    DISMISSED = "dismissed"
    ENDED = "ended"


# --- Predicates / derivation -------------------------------------------------


def _now() -> datetime:
    """Indirection so tests can pin the clock (same shape as session_scheduler)."""
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """Coerce a DB datetime to aware-UTC (SQLite round-trips naive)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def dismissal_in_force(meeting: MeetingConfig, *, now: datetime | None = None) -> bool:
    """Python-side mirror of the scheduler's SQL dismissal filter.

    True while the linked event's current ``start_time`` is still inside the
    dismissed window (``start_time <= bot_dismissed_until``). ``now`` is
    accepted for signature symmetry with the other helpers but unused — the
    predicate is purely about event-window overlap; clock-based exclusion
    after the occurrence ends is the scheduler's existing ``end_time > now``
    condition.
    """
    del now  # occurrence overlap is time-of-event, not time-of-day
    if meeting.bot_dismissed_at is None or meeting.bot_dismissed_until is None:
        return False
    event = meeting.calendar_event
    if event is None:
        # No event to re-scope against: keep the dismissal in force rather
        # than silently un-dismissing a row in a broken FK state.
        return True
    return _as_utc(event.start_time) <= _as_utc(meeting.bot_dismissed_until)


def has_active_session(session: Session, meeting_config_id: int) -> bool:
    """True when the meeting has a bot_session in a non-terminal status."""
    row = session.scalar(
        select(BotSession.id)
        .where(BotSession.meeting_config_id == meeting_config_id)
        .where(BotSession.status.in_(_ACTIVE_STATUSES))
        .limit(1)
    )
    return row is not None


def derive_bot_state(
    meeting: MeetingConfig,
    *,
    active_session: bool,
    now: datetime | None = None,
) -> MeetingBotState:
    """Derive the coarse participation state for one occurrence.

    Precedence: ``active`` (a live session is the ground truth — even a
    just-dismissed meeting whose stop failed is honestly still active) >
    ``dismissed`` (in-force dismissal, occurrence not over) > ``ended``
    (occurrence over) > ``scheduled``. The meeting-level ``enabled`` toggle
    is deliberately NOT folded in — it is an independent axis the UI
    already renders separately.
    """
    moment = now or _now()
    if active_session:
        return MeetingBotState.ACTIVE
    event = meeting.calendar_event
    occurrence_over = event is not None and _as_utc(event.end_time) <= moment
    if dismissal_in_force(meeting) and not occurrence_over:
        return MeetingBotState.DISMISSED
    if occurrence_over:
        return MeetingBotState.ENDED
    return MeetingBotState.SCHEDULED


# --- Event publishing --------------------------------------------------------

# Publisher seam: ``await publisher(channel, payload)``. Tests inject a
# recorder; production uses the one-off Redis publisher below.
BotStatePublisher = Callable[[str, dict[str, Any]], Awaitable[None]]


async def _publish_via_redis(channel: str, payload: dict[str, Any]) -> None:
    """One-off Redis publish (same shape as RedisCalendarPublisher)."""
    from redis.asyncio import Redis

    from app.config import get_settings

    client = Redis.from_url(get_settings().redis_url, decode_responses=False)
    try:
        await client.publish(channel, json.dumps(payload, separators=(",", ":")))
    finally:
        await client.aclose()


def _state_event_payload(
    meeting: MeetingConfig,
    *,
    bot_state: MeetingBotState,
    stopped_session_ids: tuple[int, ...] = (),
) -> dict[str, Any]:
    return {
        "type": BOT_STATE_EVENT_TYPE,
        "meeting_config_id": meeting.id,
        "calendar_event_id": meeting.calendar_event_id,
        "bot_state": bot_state.value,
        "dismissed_at": (
            _as_utc(meeting.bot_dismissed_at).isoformat()
            if meeting.bot_dismissed_at is not None
            else None
        ),
        "dismissed_by": (
            meeting.bot_dismissed_by.value
            if meeting.bot_dismissed_by is not None
            else None
        ),
        "dismissed_until": (
            _as_utc(meeting.bot_dismissed_until).isoformat()
            if meeting.bot_dismissed_until is not None
            else None
        ),
        "stopped_session_ids": list(stopped_session_ids),
        "timestamp_ms": int(time.time() * 1000),
    }


async def _publish_state_change(
    meeting: MeetingConfig,
    *,
    bot_state: MeetingBotState,
    stopped_session_ids: tuple[int, ...],
    publisher: BotStatePublisher | None,
) -> None:
    """Publish the state-change event; never raises into the caller.

    Always lands on the global calendar channel (layout + calendar page
    refetch on any global event); additionally lands on each stopped
    session's own channel so an open session-detail page reacts live.
    """
    publish = publisher or _publish_via_redis
    payload = _state_event_payload(
        meeting, bot_state=bot_state, stopped_session_ids=stopped_session_ids
    )
    channels = [GLOBAL_CALENDAR_CHANNEL]
    channels.extend(
        f"{SESSION_CHANNEL_PREFIX}{sid}" for sid in stopped_session_ids
    )
    for channel in channels:
        try:
            await publish(channel, payload)
        except Exception:
            logger.exception(
                "failed to publish %s on %s (meeting_config=%s)",
                BOT_STATE_EVENT_TYPE,
                channel,
                meeting.id,
            )


# --- Dismiss / un-dismiss ------------------------------------------------------


@dataclass(frozen=True)
class DismissResult:
    """What a dismissal did: the updated meeting + session-stop outcomes."""

    meeting: MeetingConfig
    stopped_session_ids: tuple[int, ...] = ()
    stop_errors: tuple[str, ...] = field(default=())


async def dismiss_bot_for_meeting(
    session: Session,
    *,
    meeting: MeetingConfig,
    actor: BotDismissActor,
    launcher: Any,
    now: datetime | None = None,
    publisher: BotStatePublisher | None = None,
) -> DismissResult:
    """End the bot's participation in this meeting occurrence.

    Stamps the three dismissal columns (idempotently — re-dismissing
    refreshes the stamp), then stops every active session for the meeting
    via :func:`app.services.session_scheduler.stop_session_by_id` (which
    routes browser-source rows to the in-process runner and meet rows to
    the container launcher). A failed stop is recorded in the result but
    never blocks the dismissal — the durable state is the point; a stop
    sweep or manual retry can clean up a stuck container, and the
    scheduler will not re-dispatch meanwhile.

    Publishes ``meeting_bot_state_changed`` after the rows are flushed.
    The caller's transaction commits; per the row-before-ack discipline the
    publish happens after the state mutation so a publish failure can never
    leave announced-but-missing state.
    """
    from app.services.session_scheduler import stop_session_by_id

    moment = now or _now()
    event = meeting.calendar_event
    # Scope boundary: the occurrence's end as scheduled right now. A config
    # in a broken FK state (no event) falls back to the dismissal moment —
    # in-force forever per dismissal_in_force's missing-event branch, which
    # is the conservative choice for a row that cannot dispatch anyway.
    until = _as_utc(event.end_time) if event is not None else moment
    meeting.bot_dismissed_at = moment
    meeting.bot_dismissed_by = actor
    meeting.bot_dismissed_until = until
    session.flush()

    active_rows = list(
        session.scalars(
            select(BotSession)
            .where(BotSession.meeting_config_id == meeting.id)
            .where(BotSession.status.in_(_ACTIVE_STATUSES))
            .order_by(BotSession.id)
        ).all()
    )
    stopped: list[int] = []
    errors: list[str] = []
    for row in active_rows:
        try:
            await stop_session_by_id(
                session, bot_session_id=row.id, launcher=launcher
            )
            stopped.append(row.id)
        except Exception as exc:  # noqa: BLE001 — dismissal must land regardless
            logger.exception(
                "dismiss: stopping bot_session %s for meeting_config %s failed",
                row.id,
                meeting.id,
            )
            errors.append(f"session {row.id}: {exc}")

    logger.info(
        "bot dismissed for meeting_config=%s by=%s until=%s (stopped=%s)",
        meeting.id,
        actor.value,
        until.isoformat(),
        stopped,
    )
    await _publish_state_change(
        meeting,
        bot_state=MeetingBotState.DISMISSED,
        stopped_session_ids=tuple(stopped),
        publisher=publisher,
    )
    return DismissResult(
        meeting=meeting,
        stopped_session_ids=tuple(stopped),
        stop_errors=tuple(errors),
    )


async def undismiss_bot_for_meeting(
    session: Session,
    *,
    meeting: MeetingConfig,
    publisher: BotStatePublisher | None = None,
) -> MeetingConfig:
    """Clear a dismissal so the scheduler may dispatch again this occurrence.

    Idempotent — clearing an un-dismissed meeting is a no-op (no event is
    published for a no-op so the activity log doesn't collect phantom
    transitions). Publishes ``meeting_bot_state_changed`` with the state
    the meeting falls back to (``scheduled`` or ``ended``).
    """
    if (
        meeting.bot_dismissed_at is None
        and meeting.bot_dismissed_by is None
        and meeting.bot_dismissed_until is None
    ):
        return meeting
    meeting.bot_dismissed_at = None
    meeting.bot_dismissed_by = None
    meeting.bot_dismissed_until = None
    session.flush()

    state = derive_bot_state(
        meeting,
        active_session=has_active_session(session, meeting.id),
    )
    logger.info("bot un-dismissed for meeting_config=%s (now %s)", meeting.id, state)
    await _publish_state_change(
        meeting,
        bot_state=state,
        stopped_session_ids=(),
        publisher=publisher,
    )
    return meeting


__all__ = [
    "BOT_STATE_EVENT_TYPE",
    "BotStatePublisher",
    "DismissResult",
    "GLOBAL_CALENDAR_CHANNEL",
    "MeetingBotState",
    "derive_bot_state",
    "dismiss_bot_for_meeting",
    "dismissal_in_force",
    "has_active_session",
    "undismiss_bot_for_meeting",
]
