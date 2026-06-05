"""Calendar fetch HTTP endpoints (US-007).

* ``GET /calendar/events?account_id=X&window_days=14`` — fetches the
  current event window from Google for the given account, upserts each
  row into ``calendar_events``, and returns the up-to-date list ordered
  by start time. Window is clamped to
  :data:`~app.services.calendar_sync.MAX_WINDOW_DAYS` to keep the
  Google quota in check.

The polling worker (see :mod:`app.services.calendar_polling`) reuses
the same ``sync_account_events`` helper so the on-demand and periodic
sync paths share one code path and stay in lock-step.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import get_crypto, get_session
from app.config import Settings, get_settings
from app.db.models import GoogleAccount
from app.security.crypto import CredentialCrypto
from app.services.calendar_sync import (
    DEFAULT_WINDOW_DAYS,
    MAX_WINDOW_DAYS,
    CalendarSyncError,
    list_account_events,
    sync_account_events,
)
from app.services.google_client import GoogleApiClient, GoogleApiClientError
from app.services.google_oauth import GoogleOAuthError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/calendar", tags=["calendar"])


class CalendarEventRead(BaseModel):
    """Public view of a :class:`~app.db.models.CalendarEvent` row.

    ``has_meeting_config`` lets the UI dim events that already have a
    bot config attached without a second round-trip; ``has_meet_link``
    drives the "join from here" affordance.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    external_id: str
    summary: str | None
    organizer: str | None
    attendees: list[dict[str, Any]] | None
    start_time: datetime
    end_time: datetime
    meet_link: str | None
    has_meeting_config: bool
    has_meet_link: bool
    last_synced_at: datetime | None
    updated_at: datetime


class CalendarSyncSummary(BaseModel):
    """Wrap the sync result + the resulting event list for the UI.

    Used as the response shape so the client gets both the up-to-date
    rows and the counts of what just changed in one call.
    """

    account_id: int
    window_days: int
    created_count: int
    updated_count: int
    deleted_count: int
    events: list[CalendarEventRead] = Field(default_factory=list)


SessionDep = Annotated[Session, Depends(get_session)]
CryptoDep = Annotated[CredentialCrypto, Depends(get_crypto)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get("/events", response_model=CalendarSyncSummary)
async def list_events(
    session: SessionDep,
    crypto: CryptoDep,
    settings: SettingsDep,
    account_id: Annotated[int, Query(ge=1)],
    window_days: Annotated[int, Query(ge=1, le=MAX_WINDOW_DAYS)] = DEFAULT_WINDOW_DAYS,
) -> CalendarSyncSummary:
    """Sync the window with Google then return the local rows."""
    account = session.get(GoogleAccount, account_id)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="account not found"
        )

    client = GoogleApiClient(
        session=session, account=account, crypto=crypto, settings=settings
    )
    try:
        try:
            result = await sync_account_events(
                session=session, client=client, window_days=window_days
            )
        except CalendarSyncError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"calendar fetch failed: {exc}",
            ) from exc
        except GoogleApiClientError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"google api client error: {exc}",
            ) from exc
        except GoogleOAuthError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"google oauth error: {exc}",
            ) from exc
    finally:
        await client.aclose()

    rows = list_account_events(
        session, account_id=account_id, window_days=window_days
    )
    events: list[CalendarEventRead] = []
    for row in rows:
        events.append(
            CalendarEventRead(
                id=row.id,
                account_id=row.account_id,
                external_id=row.external_id,
                summary=row.summary,
                organizer=row.organizer,
                attendees=list(row.attendees) if row.attendees else None,
                start_time=row.start_time,
                end_time=row.end_time,
                meet_link=row.meet_link,
                has_meeting_config=row.meeting_config is not None,
                has_meet_link=bool(row.meet_link),
                last_synced_at=row.last_synced_at,
                updated_at=row.updated_at,
            )
        )
    return CalendarSyncSummary(
        account_id=account_id,
        window_days=window_days,
        created_count=result.created_count,
        updated_count=result.updated_count,
        deleted_count=result.deleted_count,
        events=events,
    )


__all__ = ["CalendarEventRead", "CalendarSyncSummary", "router"]
