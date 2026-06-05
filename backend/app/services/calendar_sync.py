"""Google Calendar fetch and upsert (US-007).

Pure async helpers that talk to Google Calendar via the shared
:class:`~app.services.google_client.GoogleApiClient` and upsert the
results into the :class:`~app.db.models.CalendarEvent` table.

The sync is idempotent: re-running it for the same window converges
``calendar_events`` to the latest server-side state. Per-event change
detection produces a list of ``CalendarEventChange`` records so callers
(US-007's polling worker) can emit WebSocket events for downstream UIs.

The functions here intentionally do *not* commit — they leave the open
transaction to the caller (the FastAPI dep-injected session for the
endpoint, or the worker's :func:`session_scope` block). That way one
sync pass is one transaction, easy to roll back on partial failures.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CalendarEvent, GoogleAccount
from app.services.google_client import GoogleApiClient

logger = logging.getLogger(__name__)

CALENDAR_EVENTS_URL = (
    "https://www.googleapis.com/calendar/v3/calendars/primary/events"
)

DEFAULT_WINDOW_DAYS = 14
MAX_WINDOW_DAYS = 60
DEFAULT_PAGE_SIZE = 250


class CalendarSyncError(Exception):
    """Raised when the Google Calendar fetch cannot proceed."""


@dataclass(frozen=True)
class CalendarEventChange:
    """A single calendar_events row that changed during a sync.

    ``kind`` is ``"created"`` when the row was newly inserted,
    ``"updated"`` when an existing row's user-visible fields changed,
    ``"unchanged"`` when no payload-affecting fields differed, or
    ``"deleted"`` when the event was removed remotely.
    """

    kind: str
    event_id: int
    external_id: str


@dataclass(frozen=True)
class CalendarSyncResult:
    """Outcome of one calendar sync pass.

    The ``changes`` list distinguishes between created / updated /
    unchanged / deleted rows so callers can emit one WebSocket event
    per material change without re-querying the DB.
    """

    account_id: int
    changes: list[CalendarEventChange]

    @property
    def created_count(self) -> int:
        return sum(1 for c in self.changes if c.kind == "created")

    @property
    def updated_count(self) -> int:
        return sum(1 for c in self.changes if c.kind == "updated")

    @property
    def deleted_count(self) -> int:
        return sum(1 for c in self.changes if c.kind == "deleted")


# --- Calendar API client ---------------------------------------------------


async def fetch_calendar_events(
    client: GoogleApiClient,
    *,
    time_min: datetime,
    time_max: datetime,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Return every event in the ``[time_min, time_max)`` window.

    Uses ``singleEvents=true`` + ``orderBy=startTime`` so recurring
    events are expanded to individual occurrences (the only sensible
    representation for an attendance-tracking UI). Follows
    ``nextPageToken`` until exhausted.
    """
    if time_min.tzinfo is None:
        time_min = time_min.replace(tzinfo=UTC)
    if time_max.tzinfo is None:
        time_max = time_max.replace(tzinfo=UTC)

    params: dict[str, str | int | float | bool] = {
        "timeMin": _iso8601_utc(time_min),
        "timeMax": _iso8601_utc(time_max),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": page_size,
    }
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        page_params = dict(params)
        if page_token:
            page_params["pageToken"] = page_token
        response = await client.request(
            "GET", CALENDAR_EVENTS_URL, params=page_params
        )
        if not response.is_success:
            raise CalendarSyncError(
                f"calendar fetch failed: HTTP {response.status_code} "
                f"{response.text[:200]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CalendarSyncError("calendar response was not JSON") from exc
        if not isinstance(payload, dict):
            raise CalendarSyncError("calendar response was not a JSON object")
        raw_items = payload.get("items")
        if isinstance(raw_items, list):
            for it in raw_items:
                if isinstance(it, dict):
                    items.append(it)
        token_raw = payload.get("nextPageToken")
        if isinstance(token_raw, str) and token_raw:
            page_token = token_raw
        else:
            break
    return items


# --- Parsing ---------------------------------------------------------------


def _iso8601_utc(value: datetime) -> str:
    """Render ``value`` as the RFC3339 string Google expects."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_event_datetime(payload: Any) -> datetime | None:
    """Parse Google's ``start`` / ``end`` field into a UTC-aware datetime.

    Google emits either ``{"dateTime": "...ISO8601..."}`` for timed events
    or ``{"date": "YYYY-MM-DD"}`` for all-day events. The latter has no
    time-of-day component; we treat it as midnight UTC for sort/poll
    purposes — the meeting-config UI dims all-day events anyway because
    they don't carry a Meet link.
    """
    if not isinstance(payload, dict):
        return None
    raw = payload.get("dateTime")
    if isinstance(raw, str) and raw:
        return _parse_iso_datetime(raw)
    raw = payload.get("date")
    if isinstance(raw, str) and raw:
        try:
            d = datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return None
        return d.replace(tzinfo=UTC)
    return None


def _parse_iso_datetime(value: str) -> datetime | None:
    """Best-effort ISO 8601 → tz-aware UTC parser.

    Google's Calendar API consistently emits offsets like ``+02:00`` or
    ``Z``, so :py:meth:`datetime.fromisoformat` works on Python 3.11+
    after a small normalisation pass.
    """
    candidate = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _extract_meet_link(payload: dict[str, Any]) -> str | None:
    """Find the Google Meet URL on a calendar event.

    Preference: ``hangoutLink`` (legacy field, always set when present) →
    ``conferenceData.entryPoints[].uri`` where ``entryPointType="video"``.
    """
    hangout = payload.get("hangoutLink")
    if isinstance(hangout, str) and hangout:
        return hangout
    conf = payload.get("conferenceData")
    if isinstance(conf, dict):
        entry_points = conf.get("entryPoints")
        if isinstance(entry_points, list):
            for ep in entry_points:
                if not isinstance(ep, dict):
                    continue
                ep_type = ep.get("entryPointType")
                uri = ep.get("uri")
                if ep_type == "video" and isinstance(uri, str) and uri:
                    return uri
    return None


def _extract_organizer(payload: dict[str, Any]) -> str | None:
    org = payload.get("organizer")
    if isinstance(org, dict):
        email = org.get("email")
        if isinstance(email, str) and email:
            return email
    return None


def _extract_attendees(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    raw = payload.get("attendees")
    if not isinstance(raw, list):
        return None
    cleaned: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cleaned.append(
            {
                "email": item.get("email"),
                "display_name": item.get("displayName"),
                "response_status": item.get("responseStatus"),
                "optional": bool(item.get("optional", False)),
                "organizer": bool(item.get("organizer", False)),
                "self": bool(item.get("self", False)),
            }
        )
    return cleaned


@dataclass(frozen=True)
class _ParsedEvent:
    external_id: str
    start_time: datetime
    end_time: datetime
    summary: str | None
    organizer: str | None
    attendees: list[dict[str, Any]] | None
    meet_link: str | None
    etag: str | None
    cancelled: bool


def _parse_event_payload(payload: dict[str, Any]) -> _ParsedEvent | None:
    """Turn a Calendar v3 event JSON into a :class:`_ParsedEvent`.

    Returns ``None`` for events that lack the required ``id`` /
    ``start`` / ``end`` fields — we never want to insert a row that
    can't be referenced. Cancelled events are returned with
    ``cancelled=True`` so the caller can soft-handle them (we currently
    delete the local row).
    """
    external_id = payload.get("id")
    if not isinstance(external_id, str) or not external_id:
        return None
    cancelled = payload.get("status") == "cancelled"
    start = _parse_event_datetime(payload.get("start"))
    end = _parse_event_datetime(payload.get("end"))
    if not cancelled and (start is None or end is None):
        return None
    summary_raw = payload.get("summary")
    summary = str(summary_raw) if isinstance(summary_raw, str) and summary_raw else None
    etag_raw = payload.get("etag")
    etag = str(etag_raw) if isinstance(etag_raw, str) and etag_raw else None
    return _ParsedEvent(
        external_id=external_id,
        start_time=start or datetime.now(UTC),
        end_time=end or datetime.now(UTC),
        summary=summary,
        organizer=_extract_organizer(payload),
        attendees=_extract_attendees(payload),
        meet_link=_extract_meet_link(payload),
        etag=etag,
        cancelled=cancelled,
    )


# --- Upsert ---------------------------------------------------------------


def _attendees_changed(
    a: list[dict[str, Any]] | None, b: list[dict[str, Any]] | None
) -> bool:
    """Compare attendee lists structurally, ignoring server-set order.

    Stored as JSON; order from Google is stable per fetch but might
    differ across fetches if the server-side list changes. Normalise
    both sides to a sorted list of dicts keyed on email.
    """

    def _norm(value: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        if not value:
            return []
        return sorted(
            (dict(v) for v in value),
            key=lambda d: str(d.get("email") or ""),
        )

    return _norm(a) != _norm(b)


def _apply_parsed_event(
    *,
    session: Session,
    account: GoogleAccount,
    parsed: _ParsedEvent,
    synced_at: datetime,
) -> CalendarEventChange:
    """Insert or update a single calendar_events row from a parsed event.

    Returns a change descriptor distinguishing created vs updated vs
    unchanged so the caller can emit precise events. Updates only fire
    when at least one user-visible field actually changed; pure metadata
    refreshes (``last_synced_at``) are silent.
    """
    existing = session.scalar(
        select(CalendarEvent).where(
            CalendarEvent.account_id == account.id,
            CalendarEvent.external_id == parsed.external_id,
        )
    )
    if existing is None:
        row = CalendarEvent(
            account_id=account.id,
            external_id=parsed.external_id,
            summary=parsed.summary,
            organizer=parsed.organizer,
            attendees=parsed.attendees,
            start_time=parsed.start_time,
            end_time=parsed.end_time,
            meet_link=parsed.meet_link,
            etag=parsed.etag,
            last_synced_at=synced_at,
        )
        session.add(row)
        session.flush()
        return CalendarEventChange(
            kind="created", event_id=row.id, external_id=parsed.external_id
        )

    changed = False
    if existing.summary != parsed.summary:
        existing.summary = parsed.summary
        changed = True
    if existing.organizer != parsed.organizer:
        existing.organizer = parsed.organizer
        changed = True
    if _attendees_changed(existing.attendees, parsed.attendees):
        existing.attendees = parsed.attendees
        changed = True
    if _datetimes_differ(existing.start_time, parsed.start_time):
        existing.start_time = parsed.start_time
        changed = True
    if _datetimes_differ(existing.end_time, parsed.end_time):
        existing.end_time = parsed.end_time
        changed = True
    if existing.meet_link != parsed.meet_link:
        existing.meet_link = parsed.meet_link
        changed = True
    if existing.etag != parsed.etag:
        existing.etag = parsed.etag
        # etag alone is not a user-visible change.
    existing.last_synced_at = synced_at
    return CalendarEventChange(
        kind="updated" if changed else "unchanged",
        event_id=existing.id,
        external_id=parsed.external_id,
    )


def _datetimes_differ(a: datetime | None, b: datetime | None) -> bool:
    """Compare datetimes ignoring naive/aware mismatches and tz offsets.

    SQLite strips tzinfo on read; we still want to treat
    ``2026-01-01 10:00:00+00:00`` (fresh from Google) and the bare
    ``2026-01-01 10:00:00`` (re-loaded from SQLite) as the same instant.
    """
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    if a.tzinfo is None:
        a = a.replace(tzinfo=UTC)
    if b.tzinfo is None:
        b = b.replace(tzinfo=UTC)
    return a.astimezone(UTC) != b.astimezone(UTC)


def _delete_cancelled(
    *,
    session: Session,
    account: GoogleAccount,
    external_id: str,
) -> CalendarEventChange | None:
    """Delete the local row for a cancelled event, if any."""
    existing = session.scalar(
        select(CalendarEvent).where(
            CalendarEvent.account_id == account.id,
            CalendarEvent.external_id == external_id,
        )
    )
    if existing is None:
        return None
    event_id = existing.id
    session.delete(existing)
    session.flush()
    return CalendarEventChange(
        kind="deleted", event_id=event_id, external_id=external_id
    )


def _validate_window(window_days: int) -> int:
    if window_days <= 0:
        raise CalendarSyncError("window_days must be positive")
    return min(window_days, MAX_WINDOW_DAYS)


# --- Public sync entrypoint -----------------------------------------------


async def sync_account_events(
    *,
    session: Session,
    client: GoogleApiClient,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> CalendarSyncResult:
    """Fetch all events for ``client.account`` within the window and upsert them.

    ``window_days`` is clamped to :data:`MAX_WINDOW_DAYS` so a misconfigured
    UI or worker can't request a year-long fetch by accident. ``now`` is
    accepted for deterministic testing; production passes ``None`` and
    we use :func:`datetime.now`.
    """
    window = _validate_window(window_days)
    base = now or datetime.now(UTC)
    time_min = base
    time_max = base + timedelta(days=window)
    account = client.account

    raw_events = await fetch_calendar_events(
        client, time_min=time_min, time_max=time_max
    )
    synced_at = base
    changes: list[CalendarEventChange] = []
    for raw in raw_events:
        parsed = _parse_event_payload(raw)
        if parsed is None:
            continue
        if parsed.cancelled:
            deletion = _delete_cancelled(
                session=session, account=account, external_id=parsed.external_id
            )
            if deletion is not None:
                changes.append(deletion)
            continue
        changes.append(
            _apply_parsed_event(
                session=session,
                account=account,
                parsed=parsed,
                synced_at=synced_at,
            )
        )
    logger.info(
        "calendar sync account_id=%s window_days=%d created=%d updated=%d deleted=%d",
        account.id,
        window,
        sum(1 for c in changes if c.kind == "created"),
        sum(1 for c in changes if c.kind == "updated"),
        sum(1 for c in changes if c.kind == "deleted"),
    )
    return CalendarSyncResult(account_id=account.id, changes=changes)


def list_account_events(
    session: Session,
    *,
    account_id: int,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> list[CalendarEvent]:
    """Return calendar events for ``account_id`` ordered by start time.

    Used by the GET endpoint after the sync writes the latest state.
    The window is the same one used for sync so the UI sees a consistent
    view; events whose ``end_time`` is in the past are filtered out
    unless they fall inside the requested window.
    """
    window = _validate_window(window_days)
    base = now or datetime.now(UTC)
    time_min = base
    time_max = base + timedelta(days=window)
    rows: Sequence[CalendarEvent] = session.scalars(
        select(CalendarEvent)
        .where(CalendarEvent.account_id == account_id)
        .where(CalendarEvent.end_time >= time_min)
        .where(CalendarEvent.start_time < time_max)
        .order_by(CalendarEvent.start_time, CalendarEvent.id)
    ).all()
    return list(rows)


__all__ = [
    "CALENDAR_EVENTS_URL",
    "CalendarEventChange",
    "CalendarSyncError",
    "CalendarSyncResult",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_WINDOW_DAYS",
    "MAX_WINDOW_DAYS",
    "_attendees_changed",
    "_extract_meet_link",
    "_parse_event_datetime",
    "_parse_event_payload",
    "fetch_calendar_events",
    "list_account_events",
    "sync_account_events",
]
