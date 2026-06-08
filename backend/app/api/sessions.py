"""Bot session HTTP endpoints (US-029, US-032).

* ``GET    /sessions/active`` — list every non-terminal bot_session for
  the UI scheduler status panel.
* ``GET    /sessions/{id}``    — single-session detail for the live view
  (US-032): includes session metadata plus recent transcripts,
  decisions, and utterances so the UI can render the three panes with
  prior context before the WebSocket starts streaming live events.
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
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.db.models import (
    AgentDecision,
    AgentUtterance,
    BotMode,
    BotSession,
    BotSessionSource,
    BotSessionStatus,
    CalendarEvent,
    DecisionOutcome,
    MeetingConfig,
    SessionTiming,
    TranscriptChunk,
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
    """Public view of a :class:`BotSession` row.

    ``source`` is ``meet`` for legacy / scheduled meet-worker sessions
    and ``browser`` for in-browser playground or rehearsal sessions
    (Johnny-ckz.6). Lets the UI badge them differently in the list.
    ``meeting_config_id`` is nullable because playground sessions have
    no calendar event.

    For browser-source rows ``audio_ws_path`` carries the WebSocket the
    UI must connect to in order to reattach the live audio stream
    (Johnny-ckz.11 — used by the session-detail "Reopen" button when
    the playground tab was closed). ``playground_overrides`` exposes
    the per-session knobs (persona, system prompt, provider overrides)
    so the reopen UI can reflect the session's actual configuration
    without re-asking the user.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    meeting_config_id: int | None
    source: BotSessionSource
    status: BotSessionStatus
    container_name: str | None
    bot_name: str | None = None
    started_at: datetime | None
    ended_at: datetime | None
    error_reason: str | None
    created_at: datetime
    updated_at: datetime
    audio_ws_path: str | None = None
    playground_overrides: dict[str, Any] | None = None


class StartSessionPayload(BaseModel):
    """Body of ``POST /sessions/start``."""

    event_id: int


class ActiveSessionsResponse(BaseModel):
    """Wrap the list so future fields (counts, server time) have a home."""

    sessions: list[BotSessionRead]


class TranscriptChunkRead(BaseModel):
    """Audit-trail view of a finalised transcript chunk."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    bot_session_id: int
    start_offset_ms: int
    end_offset_ms: int
    speaker: str | None
    text: str
    created_at: datetime


class AgentDecisionRead(BaseModel):
    """One router decision row for the decision feed."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    bot_session_id: int
    should_speak: bool
    confidence: float
    reason: str
    reply_type: str | None
    suggested_reply: str | None
    # Canonical per-turn record (INV-2, Johnny-ckz.28.2). ``final_text`` is
    # what the bot actually spoke; ``decision_recommended_text`` is what the
    # decision layer recommended; ``divergence_reason`` / ``override_actor``
    # are set together when the two differ so the panel can render the swap.
    decision_recommended_text: str | None
    final_text: str | None
    divergence_reason: str | None
    override_actor: str | None
    outcome: DecisionOutcome
    created_at: datetime


class AgentUtteranceRead(BaseModel):
    """One spoken utterance for the audit trail."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    bot_session_id: int
    agent_decision_id: int | None
    mode: BotMode
    output_text: str
    audio_duration_ms: int | None
    matched_allowed_reply: str | None
    created_at: datetime


class SessionTimingRead(BaseModel):
    """One persisted activity-log timing row (Johnny-ckz.7).

    Mirrors ``session_timings`` rows so the session detail page can
    render a per-turn activity panel without any server-side
    transformation. ``provider_name`` is denormalised at write time so
    the UI can render "TTS: 1.4s — Local Piper" without joining back
    to ``provider_credentials``.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    bot_session_id: int
    turn_id: int
    stage: str
    started_at_ms: int
    duration_ms: int
    provider_name: str | None
    details: dict[str, Any]
    created_at: datetime


class SessionTimingsResponse(BaseModel):
    """Response shape for ``GET /sessions/{id}/timings``."""

    timings: list[SessionTimingRead]


class SessionDetailResponse(BaseModel):
    """Full detail for a single bot session.

    The three lists carry recent history so the live view has context
    on first paint; new events arrive over the WebSocket and are merged
    client-side.
    """

    session: BotSessionRead
    transcripts: list[TranscriptChunkRead]
    decisions: list[AgentDecisionRead]
    utterances: list[AgentUtteranceRead]
    pending_decisions: list[AgentDecisionRead]


# --- Helpers ---------------------------------------------------------------


def _to_read(row: BotSession) -> BotSessionRead:
    data = BotSessionRead.model_validate(row)
    if row.source == BotSessionSource.BROWSER:
        # Mirror /sessions/browser/start's audio_ws_path so the live UI
        # can reattach to the same WebSocket from the session-detail
        # page (Johnny-ckz.11).
        data = data.model_copy(update={"audio_ws_path": f"/ws/sessions/{row.id}/audio"})
    return data


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


# Default caps for the initial-state lists. The live view subscribes to
# the WebSocket for new events, so the lists are bounded — recent
# context, not a full history dump (the /history route handles that).
DEFAULT_DETAIL_LIMIT = 100
MAX_DETAIL_LIMIT = 500


@router.get("/{bot_session_id}", response_model=SessionDetailResponse)
def get_session_detail(
    bot_session_id: int,
    session: SessionDep,
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_DETAIL_LIMIT),
    ] = DEFAULT_DETAIL_LIMIT,
) -> SessionDetailResponse:
    """Return session metadata plus recent transcript / decision / utterance rows.

    The live view (US-032) calls this on mount to seed the three panes
    with prior context, then subscribes to ``/ws/sessions/{id}`` for
    incremental updates. ``pending_decisions`` is a small projection
    of ``decisions`` containing only the rows still awaiting approval
    — saves the UI from filtering client-side.
    """
    row = session.get(BotSession, bot_session_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="bot_session not found",
        )

    transcripts = list(
        session.scalars(
            select(TranscriptChunk)
            .where(TranscriptChunk.bot_session_id == row.id)
            .order_by(TranscriptChunk.start_offset_ms.asc(), TranscriptChunk.id.asc())
            .limit(limit)
        ).all()
    )
    decisions = list(
        session.scalars(
            select(AgentDecision)
            .where(AgentDecision.bot_session_id == row.id)
            .order_by(AgentDecision.created_at.desc(), AgentDecision.id.desc())
            .limit(limit)
        ).all()
    )
    utterances = list(
        session.scalars(
            select(AgentUtterance)
            .where(AgentUtterance.bot_session_id == row.id)
            .order_by(AgentUtterance.created_at.desc(), AgentUtterance.id.desc())
            .limit(limit)
        ).all()
    )
    pending = [d for d in decisions if d.outcome == DecisionOutcome.PENDING]

    return SessionDetailResponse(
        session=_to_read(row),
        transcripts=[TranscriptChunkRead.model_validate(t) for t in transcripts],
        decisions=[AgentDecisionRead.model_validate(d) for d in decisions],
        utterances=[AgentUtteranceRead.model_validate(u) for u in utterances],
        pending_decisions=[AgentDecisionRead.model_validate(d) for d in pending],
    )


# Cap on per-session timing rows returned in one call. The UI only ever
# renders the latest N turns so an unbounded fetch on a long session
# would burn payload size for no visible benefit.
DEFAULT_TIMINGS_LIMIT = 1000
MAX_TIMINGS_LIMIT = 5000


@router.get("/{bot_session_id}/timings", response_model=SessionTimingsResponse)
def get_session_timings(
    bot_session_id: int,
    session: SessionDep,
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_TIMINGS_LIMIT),
    ] = DEFAULT_TIMINGS_LIMIT,
) -> SessionTimingsResponse:
    """Return the per-turn activity log for one session (Johnny-ckz.7).

    Each row is a single measured stage event (STT, router LLM, answer
    LLM, TTS, end-to-end, interrupt, error). Sorted by ``turn_id`` ASC
    then ``started_at_ms`` ASC so the UI renders turns in chronological
    order with stages-within-turn in their pipeline order.

    Sessions that pre-date the activity log return an empty list (no
    rows; no crash). The endpoint is read-only and intentionally
    permissive on bot_session_id existence — a 404 here would make
    the UI noisier without adding value to the operator.
    """
    row = session.get(BotSession, bot_session_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="bot_session not found",
        )

    timings = list(
        session.scalars(
            select(SessionTiming)
            .where(SessionTiming.bot_session_id == bot_session_id)
            .order_by(
                SessionTiming.turn_id.asc(),
                SessionTiming.started_at_ms.asc(),
                SessionTiming.id.asc(),
            )
            .limit(limit)
        ).all()
    )
    return SessionTimingsResponse(
        timings=[SessionTimingRead.model_validate(t) for t in timings],
    )


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
    "AgentDecisionRead",
    "AgentUtteranceRead",
    "BotSessionRead",
    "SessionDetailResponse",
    "SessionTimingRead",
    "SessionTimingsResponse",
    "StartSessionPayload",
    "TranscriptChunkRead",
    "get_launcher",
    "router",
    "set_launcher",
]
