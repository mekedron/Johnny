"""Personality library HTTP endpoints (Johnny-oly.2).

Manages rows in ``personalities``: list / create / clone / read / update /
delete, plus ``set-default`` to atomically promote one personality to the
single default. A personality bundles an LLM-provider override, a
TTS-provider override, and a default decision mode — the session resolver
(Johnny-oly.3) consumes these at session start; this module only owns CRUD.

The single-default invariant is enforced both by a partial unique index on
``(is_default) WHERE is_default`` and by ``set_default_personality`` which
deactivates every sibling before flipping the requested row on, mirroring
``app.api.providers.activate_provider``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.db.models import BotMode, Personality, ProviderCredential
from app.providers.base import ProviderKind

router = APIRouter(prefix="/personalities", tags=["personalities"])


# --- Pydantic schemas ------------------------------------------------------


class PersonalityCreate(BaseModel):
    """Payload for creating a personality. Always created non-default.

    ``metadata`` maps to the model's ``extra_metadata`` attribute (the
    column is literally ``metadata``; the attribute can't be because
    SQLAlchemy reserves it on the declarative ``Base``). ``populate_by_name``
    lets clients send either key.
    """

    model_config = ConfigDict(populate_by_name=True)

    display_name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    llm_provider_id: int | None = None
    tts_provider_id: int | None = None
    default_mode: BotMode | None = None
    extra_metadata: dict[str, Any] = Field(default_factory=dict, alias="metadata")


class PersonalityUpdate(BaseModel):
    """Patch payload — only fields explicitly present are modified.

    Omit a field to leave it untouched; send it as ``null`` to clear it
    (e.g. ``{"default_mode": null}`` resets the personality to inherit the
    session's mode). ``is_default`` is intentionally not patchable — use
    ``POST /personalities/{id}/set-default`` to promote.
    """

    model_config = ConfigDict(populate_by_name=True)

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    llm_provider_id: int | None = None
    tts_provider_id: int | None = None
    default_mode: BotMode | None = None
    extra_metadata: dict[str, Any] | None = Field(default=None, alias="metadata")


class PersonalityRead(BaseModel):
    """Public view of a personality row."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    display_name: str
    description: str | None
    is_default: bool
    llm_provider_id: int | None
    tts_provider_id: int | None
    default_mode: BotMode | None
    extra_metadata: dict[str, Any] = Field(serialization_alias="metadata")
    created_at: datetime
    updated_at: datetime


# --- Helpers ---------------------------------------------------------------


SessionDep = Annotated[Session, Depends(get_session)]


def _get_row_or_404(session: Session, personality_id: int) -> Personality:
    row = session.get(Personality, personality_id)
    if row is None:
        raise HTTPException(status_code=404, detail="personality not found")
    return row


def _validate_provider_fk(
    session: Session,
    provider_id: int,
    expected_kind: ProviderKind,
    *,
    field: str,
) -> None:
    """Reject a provider FK that doesn't exist or is the wrong kind (422).

    A personality may reference an *inactive* provider (the resolver
    decides at session start), so ``is_active`` is deliberately not
    checked here — only existence and kind. A provider that is later
    deleted ``SET NULL``s the FK; one that is later deactivated is handled
    by the resolver's fallback (Johnny-oly.3).
    """
    row = session.get(ProviderCredential, provider_id)
    if row is None:
        raise HTTPException(
            status_code=422,
            detail=f"{field}={provider_id} does not reference an existing provider",
        )
    if row.kind is not expected_kind:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{field}={provider_id} is a {row.kind.value} provider, "
                f"expected {expected_kind.value}"
            ),
        )


def _unique_clone_name(session: Session, original: str) -> str:
    """Return ``"<original> (copy)"``, disambiguated if that already exists."""
    base = f"{original} (copy)"
    candidate = base
    suffix = 2
    existing = set(
        session.scalars(select(Personality.display_name)).all()
    )
    while candidate in existing:
        candidate = f"{original} (copy {suffix})"
        suffix += 1
    return candidate


def _display_name_conflict(display_name: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=(
            f"a personality with display name {display_name!r} already "
            "exists. Pick a different display name."
        ),
    )


# --- Endpoints -------------------------------------------------------------


@router.get("", response_model=list[PersonalityRead])
def list_personalities(session: SessionDep) -> list[PersonalityRead]:
    """List every personality, the default first then alphabetical."""
    rows = session.scalars(
        select(Personality).order_by(
            Personality.is_default.desc(),
            Personality.display_name,
            Personality.id,
        )
    ).all()
    return [PersonalityRead.model_validate(row) for row in rows]


@router.post("", response_model=PersonalityRead, status_code=status.HTTP_201_CREATED)
def create_personality(
    payload: PersonalityCreate,
    session: SessionDep,
) -> PersonalityRead:
    """Create a new personality. Always created non-default.

    Validates that the LLM / TTS provider FKs (when supplied) reference an
    existing provider of the matching kind (422 otherwise) and that the
    display name is unique (409 otherwise).
    """
    if payload.llm_provider_id is not None:
        _validate_provider_fk(
            session, payload.llm_provider_id, ProviderKind.LLM, field="llm_provider_id"
        )
    if payload.tts_provider_id is not None:
        _validate_provider_fk(
            session, payload.tts_provider_id, ProviderKind.TTS, field="tts_provider_id"
        )

    row = Personality(
        display_name=payload.display_name,
        description=payload.description,
        llm_provider_id=payload.llm_provider_id,
        tts_provider_id=payload.tts_provider_id,
        default_mode=payload.default_mode,
        extra_metadata=dict(payload.extra_metadata),
        is_default=False,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _display_name_conflict(payload.display_name) from exc
    session.refresh(row)
    return PersonalityRead.model_validate(row)


@router.post(
    "/{personality_id}/clone",
    response_model=PersonalityRead,
    status_code=status.HTTP_201_CREATED,
)
def clone_personality(personality_id: int, session: SessionDep) -> PersonalityRead:
    """Duplicate a personality as ``"<original> (copy)"``, non-default."""
    src = _get_row_or_404(session, personality_id)
    row = Personality(
        display_name=_unique_clone_name(session, src.display_name),
        description=src.description,
        llm_provider_id=src.llm_provider_id,
        tts_provider_id=src.tts_provider_id,
        default_mode=src.default_mode,
        extra_metadata=dict(src.extra_metadata or {}),
        is_default=False,
    )
    session.add(row)
    session.flush()
    session.refresh(row)
    return PersonalityRead.model_validate(row)


@router.get("/{personality_id}", response_model=PersonalityRead)
def get_personality(personality_id: int, session: SessionDep) -> PersonalityRead:
    """Read a single personality."""
    return PersonalityRead.model_validate(_get_row_or_404(session, personality_id))


@router.patch("/{personality_id}", response_model=PersonalityRead)
def update_personality(
    personality_id: int,
    payload: PersonalityUpdate,
    session: SessionDep,
) -> PersonalityRead:
    """Patch a personality. Omitted fields are unchanged."""
    row = _get_row_or_404(session, personality_id)
    data = payload.model_dump(exclude_unset=True)

    if data.get("llm_provider_id") is not None:
        _validate_provider_fk(
            session, data["llm_provider_id"], ProviderKind.LLM, field="llm_provider_id"
        )
    if data.get("tts_provider_id") is not None:
        _validate_provider_fk(
            session, data["tts_provider_id"], ProviderKind.TTS, field="tts_provider_id"
        )

    for key, value in data.items():
        setattr(row, key, value)

    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _display_name_conflict(row.display_name) from exc
    session.refresh(row)
    return PersonalityRead.model_validate(row)


@router.delete("/{personality_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_personality(personality_id: int, session: SessionDep) -> None:
    """Delete a personality. Refuses the default (promote another first)."""
    row = _get_row_or_404(session, personality_id)
    if row.is_default:
        raise HTTPException(
            status_code=409,
            detail=(
                "cannot delete the default personality; set another "
                "personality as default first"
            ),
        )
    session.delete(row)


@router.post("/{personality_id}/set-default", response_model=PersonalityRead)
def set_default_personality(
    personality_id: int, session: SessionDep
) -> PersonalityRead:
    """Promote this personality to the single default, atomically.

    Deactivates every other row first so the partial unique index on
    ``(is_default) WHERE is_default`` is never violated, then flips this
    row on — mirrors ``providers.activate_provider``.
    """
    row = _get_row_or_404(session, personality_id)
    session.execute(
        update(Personality)
        .where(Personality.id != row.id)
        .values(is_default=False)
    )
    row.is_default = True
    session.flush()
    session.refresh(row)
    return PersonalityRead.model_validate(row)
