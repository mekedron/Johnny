"""Per-meeting bot configuration HTTP endpoints (US-009, reshaped Johnny-trt.41).

CRUD for :class:`MeetingConfig` rows, keyed by ``calendar_event_id`` so
the UI can address each row by the event it belongs to without first
fetching the meeting-config id.

* ``GET    /calendar/events/{event_id}/meeting-config`` — fetch the row.
* ``PUT    /calendar/events/{event_id}/meeting-config`` — upsert (create
  on first save, replace fields on subsequent saves).
* ``DELETE /calendar/events/{event_id}/meeting-config`` — drop the row;
  invoked when the user disables "Enable Johnny" in the UI.

The Johnny-trt.41 agents rebuild removed the per-meeting override soup
(template/personality FKs, mode, instructions, context, allowed_replies,
confidence_threshold). A meeting config is now just: which identity
account joins, whether the bot is enabled, the occurrence-scoped dismissal
state (Johnny-trt.56), and the list of :class:`MeetingAgent` assignments —
each binding an agent (the behavior owner) to this meeting with an
optional per-assignment ``context`` brief. A meeting with no assignments
falls back to the default agent at dispatch (see
:mod:`app.services.agents`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.db.models import (
    Agent,
    BotDismissActor,
    CalendarEvent,
    GoogleAccount,
    MeetingAgent,
    MeetingConfig,
)
from app.services.meeting_lifecycle import (
    MeetingBotState,
    derive_bot_state,
    dismiss_bot_for_meeting,
    has_active_session,
    undismiss_bot_for_meeting,
)

router = APIRouter(prefix="/calendar/events", tags=["meeting-configs"])


# --- Pydantic schemas ------------------------------------------------------


class MeetingAgentAssignment(BaseModel):
    """One agent assignment inside the upsert payload.

    ``identity_account_id`` (Johnny-trt.45) is the per-assignment join
    identity — a Google account cannot join one Meet twice, so co-attending
    agents need distinct accounts to appear as distinct participants.
    ``None`` falls back to the meeting-level identity account at dispatch.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: int = Field(ge=1)
    identity_account_id: int | None = Field(default=None, ge=1)
    context: str | None = None
    enabled: bool = True
    position: int = Field(default=0, ge=0)


class MeetingConfigUpsert(BaseModel):
    """Payload for ``PUT /calendar/events/{event_id}/meeting-config``.

    Used for both create and update — the endpoint upserts in one call.
    ``agents`` is the full desired assignment list: omitted (``None``)
    leaves existing assignments untouched; an explicit list REPLACES them
    (an empty list clears all assignments, falling the meeting back to the
    default agent). ``enabled`` defaults to ``True``; the frontend deletes
    the row instead of toggling enabled=false, but the column is preserved
    for future "snooze" semantics.
    """

    model_config = ConfigDict(extra="forbid")

    identity_account_id: int = Field(ge=1)
    enabled: bool = True
    agents: list[MeetingAgentAssignment] | None = None


class MeetingAgentRead(BaseModel):
    """Public view of one :class:`MeetingAgent` assignment row."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    agent_id: int
    agent_name: str | None = None
    identity_account_id: int | None = None
    context: str | None
    enabled: bool
    position: int


class MeetingConfigRead(BaseModel):
    """Public view of a :class:`MeetingConfig` row.

    ``bot_state`` is DERIVED per request (Johnny-trt.56): ``active`` when a
    non-terminal bot_session exists, else ``dismissed`` while an in-force
    dismissal covers the current occurrence, else ``ended`` once the
    occurrence is over, else ``scheduled``. The three ``bot_dismissed_*``
    fields mirror the columns so the UI can show who ended it and when.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    calendar_event_id: int
    identity_account_id: int
    enabled: bool
    agents: list[MeetingAgentRead] = Field(default_factory=list)
    bot_state: MeetingBotState = MeetingBotState.SCHEDULED
    bot_dismissed_at: datetime | None = None
    bot_dismissed_by: BotDismissActor | None = None
    bot_dismissed_until: datetime | None = None
    created_at: datetime
    updated_at: datetime


class BotDismissPayload(BaseModel):
    """Body of ``POST .../meeting-config/bot-dismissal``.

    ``dismissed_by`` defaults to ``ui`` — the HTTP surface is the operator's;
    the voice tool (Johnny-trt.57) calls the service function directly with
    ``voice``.
    """

    dismissed_by: BotDismissActor = BotDismissActor.UI


# --- Helpers ---------------------------------------------------------------


def _get_event_or_404(session: Session, event_id: int) -> CalendarEvent:
    row = session.get(CalendarEvent, event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="calendar event not found")
    return row


def _get_account_or_422(session: Session, account_id: int) -> GoogleAccount:
    row = session.get(GoogleAccount, account_id)
    if row is None:
        raise HTTPException(
            status_code=422, detail="identity_account_id does not reference an account"
        )
    return row


def _validate_assignments_or_422(
    session: Session, assignments: list[MeetingAgentAssignment]
) -> None:
    """Reject assignments referencing missing agents/accounts or repeats (422).

    Also enforces the per-meeting co-agent cap (Johnny-trt.46) at assignment
    time — the operator hears "too many agents" while editing, with the cap
    in the message, instead of discovering a silently-truncated launch. Only
    *enabled* assignments count: a long disabled bench is fine.
    """
    from app.services.session_scheduler import MAX_AGENTS_PER_MEETING

    enabled_count = sum(1 for a in assignments if a.enabled)
    if enabled_count > MAX_AGENTS_PER_MEETING:
        raise HTTPException(
            status_code=422,
            detail=(
                f"too many agents: {enabled_count} enabled assignments exceed "
                f"the per-meeting cap of {MAX_AGENTS_PER_MEETING} — disable or "
                "remove some agents"
            ),
        )
    seen: set[int] = set()
    for assignment in assignments:
        if assignment.agent_id in seen:
            raise HTTPException(
                status_code=422,
                detail=f"agent_id={assignment.agent_id} is assigned more than once",
            )
        seen.add(assignment.agent_id)
        if session.get(Agent, assignment.agent_id) is None:
            raise HTTPException(
                status_code=422,
                detail=f"agent_id={assignment.agent_id} does not reference an agent",
            )
        if (
            assignment.identity_account_id is not None
            and session.get(GoogleAccount, assignment.identity_account_id) is None
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"identity_account_id={assignment.identity_account_id} "
                    "does not reference an account"
                ),
            )


def _replace_assignments(
    session: Session,
    row: MeetingConfig,
    assignments: list[MeetingAgentAssignment],
) -> None:
    """Replace the meeting's assignment list with the payload's.

    The deletes are flushed BEFORE the replacements are added: re-saving a
    list that keeps an agent (the UI always sends the full desired list,
    Johnny-trt.45) re-inserts the same ``(meeting_config_id, agent_id)``
    pair, and SQLAlchemy's unit of work orders INSERTs ahead of DELETEs
    within one flush — colliding with ``uq_meeting_agents_config_agent``.
    """
    for existing in list(row.agent_assignments):
        session.delete(existing)
    session.flush()
    row.agent_assignments = [
        MeetingAgent(
            agent_id=assignment.agent_id,
            identity_account_id=assignment.identity_account_id,
            context=assignment.context,
            enabled=assignment.enabled,
            position=assignment.position,
        )
        for assignment in assignments
    ]


def _get_config_for_event(
    session: Session, event_id: int
) -> MeetingConfig | None:
    return session.scalar(
        select(MeetingConfig).where(MeetingConfig.calendar_event_id == event_id)
    )


def _read_with_state(session: Session, row: MeetingConfig) -> MeetingConfigRead:
    """Validate the row and stamp the derived ``bot_state`` (Johnny-trt.56)."""
    view = MeetingConfigRead.model_validate(row)
    agents = [
        MeetingAgentRead(
            id=assignment.id,
            agent_id=assignment.agent_id,
            agent_name=assignment.agent.name if assignment.agent else None,
            identity_account_id=assignment.identity_account_id,
            context=assignment.context,
            enabled=assignment.enabled,
            position=assignment.position,
        )
        for assignment in row.agent_assignments
    ]
    return view.model_copy(
        update={
            "agents": agents,
            "bot_state": derive_bot_state(
                row, active_session=has_active_session(session, row.id)
            ),
        }
    )


# --- Endpoints -------------------------------------------------------------


SessionDep = Annotated[Session, Depends(get_session)]


@router.get(
    "/{event_id}/meeting-config",
    response_model=MeetingConfigRead,
)
def get_meeting_config(event_id: int, session: SessionDep) -> MeetingConfigRead:
    """Return the meeting config for ``event_id`` or 404 if none."""
    _get_event_or_404(session, event_id)
    row = _get_config_for_event(session, event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="meeting config not set")
    return _read_with_state(session, row)


@router.put(
    "/{event_id}/meeting-config",
    response_model=MeetingConfigRead,
)
def upsert_meeting_config(
    event_id: int,
    payload: MeetingConfigUpsert,
    session: SessionDep,
) -> MeetingConfigRead:
    """Create or replace the meeting config for an event.

    Success code is 200 for both create and update (single-resource URL).
    The upsert checks the identity account exists and every assigned agent
    exists (HTTP 422 if not) before flushing.
    """
    _get_event_or_404(session, event_id)
    _get_account_or_422(session, payload.identity_account_id)
    if payload.agents is not None:
        _validate_assignments_or_422(session, payload.agents)

    existing = _get_config_for_event(session, event_id)
    if existing is None:
        row = MeetingConfig(
            calendar_event_id=event_id,
            identity_account_id=payload.identity_account_id,
            enabled=payload.enabled,
        )
        session.add(row)
        session.flush()
    else:
        row = existing
        row.identity_account_id = payload.identity_account_id
        row.enabled = payload.enabled

    if payload.agents is not None:
        _replace_assignments(session, row, payload.agents)

    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=422,
            detail="meeting config save failed",
        ) from exc
    session.refresh(row)
    return _read_with_state(session, row)


@router.delete(
    "/{event_id}/meeting-config",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_meeting_config(event_id: int, session: SessionDep) -> None:
    """Drop the meeting config for an event.

    Idempotent: returns 204 even if no row existed, so the UI doesn't
    have to branch on "was Johnny enabled?" when the user toggles off.
    """
    _get_event_or_404(session, event_id)
    row = _get_config_for_event(session, event_id)
    if row is None:
        return
    session.delete(row)


# --- Bot dismissal (Johnny-trt.56) ------------------------------------------


def _get_config_or_404(session: Session, event_id: int) -> MeetingConfig:
    _get_event_or_404(session, event_id)
    row = _get_config_for_event(session, event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="meeting config not set")
    return row


@router.post(
    "/{event_id}/meeting-config/bot-dismissal",
    response_model=MeetingConfigRead,
)
async def dismiss_bot(
    event_id: int,
    session: SessionDep,
    payload: Annotated[BotDismissPayload | None, Body()] = None,
) -> MeetingConfigRead:
    """End the bot's participation in this occurrence ("End for this meeting").

    Distinct from the meeting ``enabled`` toggle: dismissal is scoped to the
    current occurrence window (recurring meetings rejoin next occurrence by
    design — see :mod:`app.services.meeting_lifecycle`). Stops any active
    session for the meeting and keeps the scheduler from re-dispatching
    until the dismissal lapses or is removed. Idempotent — re-dismissing
    refreshes the stamp.
    """
    from app.api.sessions import get_launcher

    row = _get_config_or_404(session, event_id)
    result = await dismiss_bot_for_meeting(
        session,
        meeting=row,
        actor=(payload.dismissed_by if payload is not None else BotDismissActor.UI),
        launcher=get_launcher(),
    )
    # A stop failure doesn't undo the dismissal (the durable state is the
    # point) but the operator should hear about a container that may still
    # be alive — surface it as a 502 only when NOTHING could be stopped.
    if result.stop_errors and not result.stopped_session_ids:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "bot dismissed, but stopping the active session failed: "
                + "; ".join(result.stop_errors)
            ),
        )
    return _read_with_state(session, row)


@router.delete(
    "/{event_id}/meeting-config/bot-dismissal",
    response_model=MeetingConfigRead,
)
async def undismiss_bot(event_id: int, session: SessionDep) -> MeetingConfigRead:
    """Remove a dismissal so the bot may rejoin this occurrence.

    Idempotent — un-dismissing a meeting that isn't dismissed is a no-op.
    The scheduler picks the meeting up again on its next poll while the
    occurrence window is still open.
    """
    row = _get_config_or_404(session, event_id)
    await undismiss_bot_for_meeting(session, meeting=row)
    return _read_with_state(session, row)


__all__ = [
    "BotDismissPayload",
    "MeetingAgentAssignment",
    "MeetingAgentRead",
    "MeetingConfigRead",
    "MeetingConfigUpsert",
    "router",
]
