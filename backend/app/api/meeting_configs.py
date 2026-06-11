"""Per-meeting bot configuration HTTP endpoints (US-009).

CRUD for :class:`MeetingConfig` rows, keyed by ``calendar_event_id`` so
the UI can address each row by the event it belongs to without first
fetching the meeting-config id.

* ``GET    /calendar/events/{event_id}/meeting-config`` — fetch the row.
* ``PUT    /calendar/events/{event_id}/meeting-config`` — upsert (create
  on first save, replace fields on subsequent saves).
* ``DELETE /calendar/events/{event_id}/meeting-config`` — drop the row;
  invoked when the user disables "Enable Johnny" in the UI.

The ``mode`` field on the row falls back to the linked profile template's
``mode`` if the payload omits it, but the column itself is NOT NULL so
every saved row has an explicit mode for downstream pipeline code.

Per-meeting overrides (``instructions``, ``context``, ``allowed_replies``,
``confidence_threshold``) are nullable — ``None`` means "use the
template's value at runtime". Empty strings / empty lists are stored
verbatim so a user can deliberately blank out a base instruction.

When mode is ``limited_auto_speak``, the effective allowed-replies list
(meeting override OR template base) must be non-empty; we reject the
PUT with HTTP 422 if it would leave the pipeline with nothing to say.

When mode is ``autonomous``, the effective instructions (meeting
override OR template base) must be non-empty — autonomous mode has no
allowlist or approval round, so the instructions are the only
governance for what the bot says. Save fails with HTTP 422 otherwise.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.db.models import (
    BotDismissActor,
    BotMode,
    CalendarEvent,
    GoogleAccount,
    MeetingConfig,
    Personality,
    ProfileTemplate,
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


class MeetingConfigUpsert(BaseModel):
    """Payload for ``PUT /calendar/events/{event_id}/meeting-config``.

    Used for both create and update — the endpoint upserts in one call.
    ``mode`` is optional; when omitted we copy the linked template's
    ``mode``. ``enabled`` defaults to ``True``; the frontend deletes the
    row instead of toggling enabled=false, but the column is preserved
    for future "snooze" semantics.
    """

    profile_template_id: int = Field(ge=1)
    identity_account_id: int = Field(ge=1)
    personality_id: int | None = Field(default=None, ge=1)
    """Optional personality preset (Johnny-oly) for this meeting. ``None`` =
    inherit the global default personality at session start (PRD §4a). The
    session resolver tolerates a stale id, but the upsert still 422s on an id
    that doesn't reference an existing personality for clearer operator feedback."""
    mode: BotMode | None = None
    instructions: str | None = None
    context: str | None = None
    allowed_replies: list[str] | None = None
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    enabled: bool = True

    @field_validator("allowed_replies")
    @classmethod
    def _strip_blank_replies(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return [v.strip() for v in value if v and v.strip()]


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
    profile_template_id: int
    identity_account_id: int
    personality_id: int | None
    mode: BotMode
    instructions: str | None
    context: str | None
    allowed_replies: list[str] | None
    confidence_threshold: float | None
    enabled: bool
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


def _get_template_or_422(session: Session, template_id: int) -> ProfileTemplate:
    row = session.get(ProfileTemplate, template_id)
    if row is None:
        raise HTTPException(
            status_code=422, detail="profile_template_id does not reference a template"
        )
    return row


def _get_account_or_422(session: Session, account_id: int) -> GoogleAccount:
    row = session.get(GoogleAccount, account_id)
    if row is None:
        raise HTTPException(
            status_code=422, detail="identity_account_id does not reference an account"
        )
    return row


def _validate_personality_or_422(session: Session, personality_id: int | None) -> None:
    """Reject a non-null ``personality_id`` that references no personality.

    ``None`` is always valid (inherit the global default). A set-but-missing id
    is a 422 — friendlier than letting the DB FK raise a generic IntegrityError.
    """
    if personality_id is None:
        return
    if session.get(Personality, personality_id) is None:
        raise HTTPException(
            status_code=422,
            detail="personality_id does not reference a personality",
        )


def _validate_limited_auto_speak(
    mode: BotMode,
    overrides: list[str] | None,
    template: ProfileTemplate,
) -> None:
    """Reject save when limited_auto_speak would leave no replies to pick.

    Effective allowed-replies = meeting override if set, else template
    base. Either branch must be non-empty for limited_auto_speak so the
    pipeline always has at least one safe phrase to choose from.
    """
    if mode is not BotMode.LIMITED_AUTO_SPEAK:
        return
    effective = overrides if overrides is not None else list(template.allowed_replies or [])
    if len(effective) == 0:
        raise HTTPException(
            status_code=422,
            detail=(
                "allowed_replies must be non-empty when mode is 'limited_auto_speak' "
                "(either set per-meeting overrides, or use a template that has them)"
            ),
        )


def _validate_autonomous(
    mode: BotMode,
    instructions_override: str | None,
    template: ProfileTemplate,
) -> None:
    """Reject save when autonomous mode would have no instructions.

    Effective instructions = meeting override (when explicitly set to a
    non-empty string) else template base. Either source must yield a
    non-empty value because autonomous mode has no allowlist or approval
    round; the instructions are the only governance for the bot's
    free-form output.
    """
    if mode is not BotMode.AUTONOMOUS:
        return
    if instructions_override is not None and instructions_override.strip():
        return
    template_instructions = (template.base_instructions or "").strip()
    if template_instructions:
        return
    raise HTTPException(
        status_code=422,
        detail=(
            "instructions must be non-empty when mode is 'autonomous' "
            "(either set per-meeting overrides, or use a template that has "
            "non-empty base_instructions)"
        ),
    )


def _get_config_for_event(
    session: Session, event_id: int
) -> MeetingConfig | None:
    return session.scalar(
        select(MeetingConfig).where(MeetingConfig.calendar_event_id == event_id)
    )


def _read_with_state(session: Session, row: MeetingConfig) -> MeetingConfigRead:
    """Validate the row and stamp the derived ``bot_state`` (Johnny-trt.56)."""
    view = MeetingConfigRead.model_validate(row)
    return view.model_copy(
        update={
            "bot_state": derive_bot_state(
                row, active_session=has_active_session(session, row.id)
            )
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

    Returns 201 only on first-time creation (via the ``Location`` header
    isn't relevant for a single-resource URL), so we keep the success
    code 200 for both create and update. The upsert checks the linked
    template and account exist (HTTP 422 if not) and enforces the
    limited-auto-speak invariant before flushing.
    """
    _get_event_or_404(session, event_id)
    template = _get_template_or_422(session, payload.profile_template_id)
    _get_account_or_422(session, payload.identity_account_id)
    _validate_personality_or_422(session, payload.personality_id)

    mode = payload.mode if payload.mode is not None else template.mode
    _validate_limited_auto_speak(mode, payload.allowed_replies, template)
    _validate_autonomous(mode, payload.instructions, template)

    existing = _get_config_for_event(session, event_id)
    if existing is None:
        row = MeetingConfig(
            calendar_event_id=event_id,
            profile_template_id=payload.profile_template_id,
            identity_account_id=payload.identity_account_id,
            personality_id=payload.personality_id,
            mode=mode,
            instructions=payload.instructions,
            context=payload.context,
            allowed_replies=(
                list(payload.allowed_replies)
                if payload.allowed_replies is not None
                else None
            ),
            confidence_threshold=payload.confidence_threshold,
            enabled=payload.enabled,
        )
        session.add(row)
    else:
        row = existing
        row.profile_template_id = payload.profile_template_id
        row.identity_account_id = payload.identity_account_id
        row.personality_id = payload.personality_id
        row.mode = mode
        row.instructions = payload.instructions
        row.context = payload.context
        row.allowed_replies = (
            list(payload.allowed_replies)
            if payload.allowed_replies is not None
            else None
        )
        row.confidence_threshold = payload.confidence_threshold
        row.enabled = payload.enabled

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
    "MeetingConfigRead",
    "MeetingConfigUpsert",
    "router",
]
