"""Profile templates HTTP endpoints (US-010).

CRUD for :class:`ProfileTemplate` rows. Templates define reusable bot
behavior (mode, base instructions/context, allowed replies, confidence
threshold) that meeting configs reference via ``profile_template_id``.

Delete semantics: by default a delete fails with HTTP 409 if any
``meeting_configs`` reference the template, returning the dependent
count so the UI can warn the user. Passing ``?force=true`` cascade-
deletes those meeting configs first — the FK is RESTRICT in the schema,
so the only way to drop a referenced row is to remove the references
ourselves.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.db.models import BotMode, MeetingConfig, ProfileTemplate

router = APIRouter(prefix="/templates", tags=["templates"])


# --- Pydantic schemas ------------------------------------------------------


class TemplateBase(BaseModel):
    """Fields shared by create and update payloads."""

    name: str = Field(min_length=1, max_length=128)
    mode: BotMode
    base_instructions: str = Field(default="")
    base_context: str = Field(default="")
    allowed_replies: list[str] = Field(default_factory=list)
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)

    @field_validator("allowed_replies")
    @classmethod
    def _strip_blank_replies(cls, value: list[str]) -> list[str]:
        return [v.strip() for v in value if v and v.strip()]

    @model_validator(mode="after")
    def _validate_limited_auto_speak_has_replies(self) -> TemplateBase:
        if self.mode is BotMode.LIMITED_AUTO_SPEAK and len(self.allowed_replies) == 0:
            raise ValueError(
                "allowed_replies must be a non-empty list when mode is 'limited_auto_speak'"
            )
        return self

    @model_validator(mode="after")
    def _validate_autonomous_has_instructions(self) -> TemplateBase:
        # Autonomous mode runs free-form generation governed only by
        # the template's instructions; blank instructions would leave
        # the bot to invent its own behaviour, which is exactly what we
        # need to prevent at the configuration boundary.
        if self.mode is BotMode.AUTONOMOUS and not self.base_instructions.strip():
            raise ValueError(
                "base_instructions must be non-empty when mode is 'autonomous' "
                "— autonomous mode has no allowlist or approval round, so the "
                "instructions are the only governance for what the bot says"
            )
        return self


class TemplateCreate(TemplateBase):
    """Payload for creating a profile template."""


class TemplateUpdate(BaseModel):
    """Patch payload — fields left as ``None`` are not modified.

    Validation of ``allowed_replies`` vs ``mode`` runs after the patch
    is applied to the row, not on this payload, since either field can
    be omitted.
    """

    name: str | None = Field(default=None, min_length=1, max_length=128)
    mode: BotMode | None = None
    base_instructions: str | None = None
    base_context: str | None = None
    allowed_replies: list[str] | None = None
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("allowed_replies")
    @classmethod
    def _strip_blank_replies(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return [v.strip() for v in value if v and v.strip()]


class TemplateRead(BaseModel):
    """Public view of a profile template row."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    mode: BotMode
    base_instructions: str
    base_context: str
    allowed_replies: list[str]
    confidence_threshold: float
    meeting_config_count: int
    created_at: datetime
    updated_at: datetime


# --- Helpers ---------------------------------------------------------------


def _meeting_config_count(session: Session, template_id: int) -> int:
    """Return the number of meeting_configs that reference ``template_id``."""
    stmt = select(func.count()).select_from(MeetingConfig).where(
        MeetingConfig.profile_template_id == template_id
    )
    return int(session.scalar(stmt) or 0)


def _row_to_read(session: Session, row: ProfileTemplate) -> TemplateRead:
    return TemplateRead(
        id=row.id,
        name=row.name,
        mode=row.mode,
        base_instructions=row.base_instructions,
        base_context=row.base_context,
        allowed_replies=list(row.allowed_replies or []),
        confidence_threshold=row.confidence_threshold,
        meeting_config_count=_meeting_config_count(session, row.id),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _get_row_or_404(session: Session, template_id: int) -> ProfileTemplate:
    row = session.get(ProfileTemplate, template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="template not found")
    return row


# --- Endpoints -------------------------------------------------------------


SessionDep = Annotated[Session, Depends(get_session)]


@router.get("", response_model=list[TemplateRead])
def list_templates(session: SessionDep) -> list[TemplateRead]:
    """List every profile template ordered by name."""
    rows = session.scalars(
        select(ProfileTemplate).order_by(ProfileTemplate.name, ProfileTemplate.id)
    ).all()
    return [_row_to_read(session, row) for row in rows]


@router.post("", response_model=TemplateRead, status_code=status.HTTP_201_CREATED)
def create_template(payload: TemplateCreate, session: SessionDep) -> TemplateRead:
    """Create a new profile template."""
    row = ProfileTemplate(
        name=payload.name.strip(),
        mode=payload.mode,
        base_instructions=payload.base_instructions,
        base_context=payload.base_context,
        allowed_replies=list(payload.allowed_replies),
        confidence_threshold=payload.confidence_threshold,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="a template with this name already exists",
        ) from exc
    session.refresh(row)
    return _row_to_read(session, row)


@router.get("/{template_id}", response_model=TemplateRead)
def get_template(template_id: int, session: SessionDep) -> TemplateRead:
    """Fetch a single template by id."""
    row = _get_row_or_404(session, template_id)
    return _row_to_read(session, row)


@router.patch("/{template_id}", response_model=TemplateRead)
def update_template(
    template_id: int,
    payload: TemplateUpdate,
    session: SessionDep,
) -> TemplateRead:
    """Patch a profile template row. Omitted fields are unchanged."""
    row = _get_row_or_404(session, template_id)

    if payload.name is not None:
        row.name = payload.name.strip()
    if payload.mode is not None:
        row.mode = payload.mode
    if payload.base_instructions is not None:
        row.base_instructions = payload.base_instructions
    if payload.base_context is not None:
        row.base_context = payload.base_context
    if payload.allowed_replies is not None:
        row.allowed_replies = list(payload.allowed_replies)
    if payload.confidence_threshold is not None:
        row.confidence_threshold = payload.confidence_threshold

    if row.mode is BotMode.LIMITED_AUTO_SPEAK and len(row.allowed_replies or []) == 0:
        session.rollback()
        raise HTTPException(
            status_code=422,
            detail=(
                "allowed_replies must be a non-empty list when mode is "
                "'limited_auto_speak'"
            ),
        )
    if row.mode is BotMode.AUTONOMOUS and not (row.base_instructions or "").strip():
        session.rollback()
        raise HTTPException(
            status_code=422,
            detail=(
                "base_instructions must be non-empty when mode is 'autonomous' "
                "— autonomous mode has no allowlist or approval round, so the "
                "instructions are the only governance for what the bot says"
            ),
        )

    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="a template with this name already exists",
        ) from exc
    session.refresh(row)
    return _row_to_read(session, row)


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(
    template_id: int,
    session: SessionDep,
    force: Annotated[bool, Query(description="cascade-delete referencing meeting configs")] = False,  # noqa: E501
) -> None:
    """Delete a profile template.

    Without ``force=true``, returns HTTP 409 if any ``meeting_configs``
    reference the template. The error detail includes ``meeting_config_count``
    so the UI can surface a meaningful warning.

    With ``force=true``, the referencing meeting configs are deleted first
    (cascade detach), then the template itself.
    """
    row = _get_row_or_404(session, template_id)
    referencing = _meeting_config_count(session, template_id)
    if referencing > 0 and not force:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"template is referenced by {referencing} meeting config(s); "
                    "pass force=true to cascade-delete"
                ),
                "meeting_config_count": referencing,
            },
        )
    if referencing > 0:
        session.execute(
            delete(MeetingConfig).where(MeetingConfig.profile_template_id == template_id)
        )
    session.delete(row)


__all__ = [
    "TemplateCreate",
    "TemplateRead",
    "TemplateUpdate",
    "router",
]
