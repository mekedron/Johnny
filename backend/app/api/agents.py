"""Agent library HTTP endpoints (Johnny-trt.41).

Manages rows in ``agents``: list / create / clone / read / update / delete,
plus ``set-default`` to atomically promote one agent to the single default.
An agent bundles identity (name / avatar / description), the character
prompt, behavior (mode / allowed_replies / confidence_threshold) and the
split-pipeline provider role-slot pins. The session-start surfaces consume
agents via :mod:`app.services.agents`; this module only owns CRUD.

Validation parity with the retired templates/personalities rules:

* ``limited_auto_speak`` requires a non-empty ``allowed_replies`` list (the
  pipeline must always have a safe phrase to pick);
* ``autonomous`` requires a non-empty ``character_prompt`` (free-form
  generation's only governance is the prompt);
* provider FKs are kind-validated — the three LLM role slots must reference
  ``llm`` rows, ``tts_provider_id`` a ``tts`` row (422 otherwise);
* ``tts_voice_id`` requires ``tts_provider_id`` (voice ids are
  provider-specific, a dangling voice pin is meaningless);
* names are unique (409), exactly one default exists at any time (partial
  unique index + the deactivate-siblings-first promote).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.db.models import Agent, BotMode, ProviderCredential
from app.providers.base import ProviderKind

router = APIRouter(prefix="/agents", tags=["agents"])


# (payload field, expected provider kind) for every provider FK an agent
# carries. The three LLM role slots (Johnny-trt.41 note: router = triage,
# answer = conversational replies, reasoning = delegated executor tasks) all
# validate against the ``llm`` kind; resolution order/fallback is trt.42.
_PROVIDER_FK_KINDS: tuple[tuple[str, ProviderKind], ...] = (
    ("router_llm_provider_id", ProviderKind.LLM),
    ("answer_llm_provider_id", ProviderKind.LLM),
    ("reasoning_llm_provider_id", ProviderKind.LLM),
    ("tts_provider_id", ProviderKind.TTS),
)


# --- Pydantic schemas ------------------------------------------------------


class AgentCreate(BaseModel):
    """Payload for creating an agent. Always created non-default."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    avatar: str | None = Field(default=None, max_length=64)
    description: str | None = None
    character_prompt: str = ""
    mode: BotMode = BotMode.LISTEN_ONLY
    allowed_replies: list[str] = Field(default_factory=list)
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    router_llm_provider_id: int | None = None
    answer_llm_provider_id: int | None = None
    reasoning_llm_provider_id: int | None = None
    tts_provider_id: int | None = None
    tts_voice_id: str | None = Field(default=None, max_length=128)
    tts_options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("allowed_replies")
    @classmethod
    def _strip_blank_replies(cls, value: list[str]) -> list[str]:
        return [v.strip() for v in value if v and v.strip()]


class AgentUpdate(BaseModel):
    """Patch payload — only fields explicitly present are modified.

    Omit a field to leave it untouched; send ``null`` to clear a nullable
    one. ``is_default`` is intentionally not patchable — use
    ``POST /agents/{id}/set-default`` to promote.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    avatar: str | None = Field(default=None, max_length=64)
    description: str | None = None
    character_prompt: str | None = None
    mode: BotMode | None = None
    allowed_replies: list[str] | None = None
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    router_llm_provider_id: int | None = None
    answer_llm_provider_id: int | None = None
    reasoning_llm_provider_id: int | None = None
    tts_provider_id: int | None = None
    tts_voice_id: str | None = Field(default=None, max_length=128)
    tts_options: dict[str, Any] | None = None

    @field_validator("allowed_replies")
    @classmethod
    def _strip_blank_replies(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return [v.strip() for v in value if v and v.strip()]


class AgentRead(BaseModel):
    """Public view of an agent row."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    avatar: str | None
    description: str | None
    character_prompt: str
    mode: BotMode
    allowed_replies: list[str]
    confidence_threshold: float
    is_default: bool
    router_llm_provider_id: int | None
    answer_llm_provider_id: int | None
    reasoning_llm_provider_id: int | None
    tts_provider_id: int | None
    tts_voice_id: str | None
    tts_options: dict[str, Any]
    created_at: datetime
    updated_at: datetime


# --- Helpers ---------------------------------------------------------------


SessionDep = Annotated[Session, Depends(get_session)]


def _get_row_or_404(session: Session, agent_id: int) -> Agent:
    row = session.get(Agent, agent_id)
    if row is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return row


def _validate_provider_fk(
    session: Session,
    provider_id: int,
    expected_kind: ProviderKind,
    *,
    field: str,
) -> None:
    """Reject a provider FK that doesn't exist or is the wrong kind (422).

    An agent may reference an *inactive* provider (the trt.42 resolver falls
    back at session start), so ``is_active`` is deliberately not checked —
    only existence and kind.
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


def _validate_provider_fks(session: Session, data: dict[str, Any]) -> None:
    for field, kind in _PROVIDER_FK_KINDS:
        value = data.get(field)
        if value is not None:
            _validate_provider_fk(session, value, kind, field=field)


def _validate_behavior(
    *,
    mode: BotMode,
    allowed_replies: list[str],
    character_prompt: str,
    tts_provider_id: int | None,
    tts_voice_id: str | None,
) -> None:
    """Enforce the cross-field behavior invariants on the EFFECTIVE state.

    Called with the post-create / post-patch values so switching ``mode`` on
    an existing agent revalidates against its (possibly unchanged) replies
    and prompt.
    """
    if mode is BotMode.LIMITED_AUTO_SPEAK and not allowed_replies:
        raise HTTPException(
            status_code=422,
            detail=(
                "allowed_replies must be non-empty when mode is "
                "'limited_auto_speak' — the pipeline needs at least one safe "
                "phrase to choose from"
            ),
        )
    if mode is BotMode.AUTONOMOUS and not character_prompt.strip():
        raise HTTPException(
            status_code=422,
            detail=(
                "character_prompt must be non-empty when mode is 'autonomous' "
                "— free-form generation has no allowlist or approval round, "
                "so the prompt is the only governance for what the agent says"
            ),
        )
    if tts_voice_id is not None and tts_voice_id.strip() and tts_provider_id is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "tts_voice_id requires tts_provider_id — voice ids are "
                "provider-specific"
            ),
        )


def _unique_clone_name(session: Session, original: str) -> str:
    """Return ``"<original> (copy)"``, disambiguated if that already exists."""
    base = f"{original} (copy)"
    candidate = base
    suffix = 2
    existing = set(session.scalars(select(Agent.name)).all())
    while candidate in existing:
        candidate = f"{original} (copy {suffix})"
        suffix += 1
    return candidate


def _name_conflict(name: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=f"an agent named {name!r} already exists. Pick a different name.",
    )


# --- Endpoints -------------------------------------------------------------


@router.get("", response_model=list[AgentRead])
def list_agents(session: SessionDep) -> list[AgentRead]:
    """List every agent, the default first then alphabetical."""
    rows = session.scalars(
        select(Agent).order_by(Agent.is_default.desc(), Agent.name, Agent.id)
    ).all()
    return [AgentRead.model_validate(row) for row in rows]


@router.post("", response_model=AgentRead, status_code=status.HTTP_201_CREATED)
def create_agent(payload: AgentCreate, session: SessionDep) -> AgentRead:
    """Create a new agent. Always created non-default."""
    data = payload.model_dump()
    _validate_provider_fks(session, data)
    _validate_behavior(
        mode=payload.mode,
        allowed_replies=payload.allowed_replies,
        character_prompt=payload.character_prompt,
        tts_provider_id=payload.tts_provider_id,
        tts_voice_id=payload.tts_voice_id,
    )

    row = Agent(**data, is_default=False)
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _name_conflict(payload.name) from exc
    session.refresh(row)
    return AgentRead.model_validate(row)


@router.post(
    "/{agent_id}/clone",
    response_model=AgentRead,
    status_code=status.HTTP_201_CREATED,
)
def clone_agent(agent_id: int, session: SessionDep) -> AgentRead:
    """Duplicate an agent as ``"<original> (copy)"``, non-default."""
    src = _get_row_or_404(session, agent_id)
    row = Agent(
        name=_unique_clone_name(session, src.name),
        avatar=src.avatar,
        description=src.description,
        character_prompt=src.character_prompt,
        mode=src.mode,
        allowed_replies=list(src.allowed_replies or []),
        confidence_threshold=src.confidence_threshold,
        router_llm_provider_id=src.router_llm_provider_id,
        answer_llm_provider_id=src.answer_llm_provider_id,
        reasoning_llm_provider_id=src.reasoning_llm_provider_id,
        tts_provider_id=src.tts_provider_id,
        tts_voice_id=src.tts_voice_id,
        tts_options=dict(src.tts_options or {}),
        is_default=False,
    )
    session.add(row)
    session.flush()
    session.refresh(row)
    return AgentRead.model_validate(row)


@router.get("/{agent_id}", response_model=AgentRead)
def get_agent(agent_id: int, session: SessionDep) -> AgentRead:
    """Read a single agent."""
    return AgentRead.model_validate(_get_row_or_404(session, agent_id))


@router.patch("/{agent_id}", response_model=AgentRead)
def update_agent(
    agent_id: int,
    payload: AgentUpdate,
    session: SessionDep,
) -> AgentRead:
    """Patch an agent. Omitted fields are unchanged.

    Behavior invariants are validated against the EFFECTIVE post-patch
    state, so e.g. flipping ``mode`` to ``limited_auto_speak`` on an agent
    with no allowed replies is rejected even though the patch itself never
    mentions ``allowed_replies``.
    """
    row = _get_row_or_404(session, agent_id)
    data = payload.model_dump(exclude_unset=True)

    # Explicit ``null`` for the NOT NULL columns means "reset to empty" —
    # normalise before validation/assignment so it can't reach the DB as
    # NULL. A ``null`` mode is meaningless and treated as "unchanged".
    if data.get("allowed_replies") is None and "allowed_replies" in data:
        data["allowed_replies"] = []
    if data.get("character_prompt") is None and "character_prompt" in data:
        data["character_prompt"] = ""
    if data.get("tts_options") is None and "tts_options" in data:
        data["tts_options"] = {}
    if data.get("mode") is None:
        data.pop("mode", None)
    if data.get("confidence_threshold") is None:
        data.pop("confidence_threshold", None)
    if data.get("name") is None:
        data.pop("name", None)

    _validate_provider_fks(session, data)

    effective: dict[str, Any] = {
        "mode": row.mode,
        "allowed_replies": list(row.allowed_replies or []),
        "character_prompt": row.character_prompt or "",
        "tts_provider_id": row.tts_provider_id,
        "tts_voice_id": row.tts_voice_id,
        **data,
    }
    _validate_behavior(
        mode=effective["mode"],
        allowed_replies=effective["allowed_replies"],
        character_prompt=effective["character_prompt"] or "",
        tts_provider_id=effective["tts_provider_id"],
        tts_voice_id=effective["tts_voice_id"],
    )

    for key, value in data.items():
        setattr(row, key, value)

    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _name_conflict(row.name) from exc
    session.refresh(row)
    return AgentRead.model_validate(row)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent(agent_id: int, session: SessionDep) -> None:
    """Delete an agent. Refuses the default (promote another first)."""
    row = _get_row_or_404(session, agent_id)
    if row.is_default:
        raise HTTPException(
            status_code=409,
            detail=(
                "cannot delete the default agent; set another agent as "
                "default first"
            ),
        )
    session.delete(row)


@router.post("/{agent_id}/set-default", response_model=AgentRead)
def set_default_agent(agent_id: int, session: SessionDep) -> AgentRead:
    """Promote this agent to the single default, atomically.

    Deactivates every other row first so the partial unique index on
    ``(is_default) WHERE is_default`` is never violated, then flips this
    row on — mirrors ``providers.activate_provider``.
    """
    row = _get_row_or_404(session, agent_id)
    session.execute(
        update(Agent).where(Agent.id != row.id).values(is_default=False)
    )
    row.is_default = True
    session.flush()
    session.refresh(row)
    return AgentRead.model_validate(row)


__all__ = ["AgentCreate", "AgentRead", "AgentUpdate", "router"]
