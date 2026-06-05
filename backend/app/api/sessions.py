"""Bot session HTTP endpoints (US-029).

* ``GET    /sessions/active`` — list every non-terminal bot_session for
  the UI scheduler status panel.
* ``POST   /sessions/start``  — manual "Join now"; takes a calendar
  event id, finds its meeting_config, and invokes
  :func:`start_session_for_meeting` immediately (bypassing the
  start-window check).
* ``POST   /sessions/{id}/stop`` — manual "Leave now"; calls
  :func:`stop_session_by_id`.

Both manual endpoints delegate to the same helpers the scheduler uses,
so the lifecycle / persistence semantics stay consistent regardless of
trigger.

The launcher is held in a small module-level container (overridable
via :func:`set_launcher`) so the API + scheduler share one instance
in production while tests inject a :class:`NoopContainerLauncher` per
test.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.db.models import (
    BotSession,
    BotSessionStatus,
    CalendarEvent,
    MeetingConfig,
)
from app.services.bot_sessions import BotSessionNotFoundError
from app.services.session_scheduler import (
    ContainerLauncher,
    LauncherError,
    NoopContainerLauncher,
    list_active_sessions,
    start_session_for_meeting,
    stop_session_by_id,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])


# --- Module-level launcher --------------------------------------------------

_launcher: ContainerLauncher = NoopContainerLauncher()


def set_launcher(launcher: ContainerLauncher) -> None:
    """Replace the active launcher (used at startup and in tests)."""
    global _launcher
    _launcher = launcher


def get_launcher() -> ContainerLauncher:
    """FastAPI dep — returns the module-level launcher."""
    return _launcher


# --- Pydantic schemas -------------------------------------------------------


class BotSessionRead(BaseModel):
    """Public view of a :class:`BotSession` row."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    meeting_config_id: int
    status: BotSessionStatus
    container_name: str | None
    started_at: datetime | None
    ended_at: datetime | None
    error_reason: str | None
    created_at: datetime
    updated_at: datetime


class StartSessionPayload(BaseModel):
    """Body of ``POST /sessions/start``."""

    event_id: int


class ActiveSessionsResponse(BaseModel):
    """Wrap the list so future fields (counts, server time) have a home."""

    sessions: list[BotSessionRead]


# --- Helpers ---------------------------------------------------------------


def _to_read(row: BotSession) -> BotSessionRead:
    return BotSessionRead.model_validate(row)


def _meeting_for_event_or_404(
    session: Session, event_id: int
) -> MeetingConfig:
    event = session.get(CalendarEvent, event_id)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="calendar event not found",
        )
    meeting = session.scalar(
        select(MeetingConfig).where(MeetingConfig.calendar_event_id == event_id)
    )
    if meeting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="meeting config not set for event",
        )
    return meeting


# --- Endpoints --------------------------------------------------------------


SessionDep = Annotated[Session, Depends(get_session)]
LauncherDep = Annotated[ContainerLauncher, Depends(get_launcher)]


@router.get("/active", response_model=ActiveSessionsResponse)
def list_active(session: SessionDep) -> ActiveSessionsResponse:
    """List every non-terminal bot_session."""
    rows = list_active_sessions(session)
    return ActiveSessionsResponse(sessions=[_to_read(r) for r in rows])


@router.post(
    "/start",
    response_model=BotSessionRead,
    status_code=status.HTTP_201_CREATED,
)
async def start_now(
    payload: Annotated[StartSessionPayload, Body()],
    session: SessionDep,
    launcher: LauncherDep,
) -> BotSessionRead:
    """Manual "Join now": spawn a worker for ``payload.event_id`` immediately.

    Returns 409 if the meeting already has an active session — the UI
    can refresh the active list to show what's already running.
    """
    meeting = _meeting_for_event_or_404(session, payload.event_id)

    existing = session.scalar(
        select(BotSession).where(
            BotSession.meeting_config_id == meeting.id,
            BotSession.status.in_(
                (
                    BotSessionStatus.SCHEDULED,
                    BotSessionStatus.JOINING,
                    BotSessionStatus.JOINED,
                )
            ),
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "meeting already has an active session",
                "bot_session_id": existing.id,
            },
        )

    try:
        row = await start_session_for_meeting(
            session, meeting=meeting, launcher=launcher
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LauncherError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"launcher failed: {exc}",
        ) from exc
    return _to_read(row)


@router.post("/{bot_session_id}/stop", response_model=BotSessionRead)
async def stop_now(
    bot_session_id: int,
    session: SessionDep,
    launcher: LauncherDep,
) -> BotSessionRead:
    """Manual "Leave now": stop the worker for ``bot_session_id``."""
    try:
        row = await stop_session_by_id(
            session, bot_session_id=bot_session_id, launcher=launcher
        )
    except BotSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LauncherError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"launcher failed: {exc}",
        ) from exc
    return _to_read(row)


__all__ = [
    "ActiveSessionsResponse",
    "BotSessionRead",
    "StartSessionPayload",
    "get_launcher",
    "router",
    "set_launcher",
]
