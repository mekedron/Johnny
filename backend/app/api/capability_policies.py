"""Capability-policy HTTP endpoints (Johnny-trt.38).

CRUD over the four policy scope layers (global / per-agent / per-session-mode
/ per-session override) plus the two read surfaces the trt.37 management UI
is built on:

* ``GET /capability-policies/effective`` — the layers matching a set of
  session coordinates and the resolved summary (effective safe-bins, denies,
  the allow-list in force);
* ``POST /capability-policies/resolve`` — THE effective-policy inspector
  (the trt.38 acceptance API): give it a tool kind or an exec binary plus
  scope coordinates, get back allowed/denied **and the deciding layer**.

Writes take effect without a restart by construction: dispatch surfaces
re-resolve per session start, the worker re-resolves per claimed task
(:mod:`app.services.capability_policies` documents the freshness model).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.db.models import (
    CAPABILITY_POLICY_SESSION_MODES,
    Agent,
    BotSession,
    CapabilityPolicy,
)
from app.services.capability_policies import (
    builtin_baseline_safe_bins,
    delete_policy_row,
    list_policy_rows,
    load_policy_layers,
    upsert_policy_row,
)
from johnny.skills.capability_policy import (
    POLICY_SCOPE_AGENT,
    POLICY_SCOPE_GLOBAL,
    POLICY_SCOPE_SESSION,
    POLICY_SCOPE_SESSION_MODE,
    resolve_policy,
)

router = APIRouter(prefix="/capability-policies", tags=["capability-policies"])

SessionModeLiteral = Literal["meet", "browser"]


class PolicyDocumentIn(BaseModel):
    """One layer's policy document (the wire/UI shape).

    Glob patterns follow :func:`fnmatch.fnmatchcase` (``*``, ``?``,
    ``[seq]``); tool patterns address catalog kinds (skills, internal tools,
    future ``mcp__<server>__<tool>``), bin patterns address ``argv[0]``
    basenames. ``safe_bins`` (global layer only): the edited trt.35
    baseline; ``null``/absent = the built-in baseline (reset-to-default).
    """

    model_config = ConfigDict(extra="forbid")

    tools_allow: list[str] = Field(default_factory=list)
    tools_also_allow: list[str] = Field(default_factory=list)
    tools_deny: list[str] = Field(default_factory=list)
    bins_deny: list[str] = Field(default_factory=list)
    safe_bins: list[str] | None = None


class PolicyRowOut(BaseModel):
    id: int
    scope: str
    agent_id: int | None = None
    session_mode: str | None = None
    bot_session_id: int | None = None
    document: dict[str, Any]


def _row_out(row: CapabilityPolicy) -> PolicyRowOut:
    return PolicyRowOut(
        id=row.id,
        scope=row.scope,
        agent_id=row.agent_id,
        session_mode=row.session_mode,
        bot_session_id=row.bot_session_id,
        document=dict(row.document or {}),
    )


class PolicyListOut(BaseModel):
    """Every stored layer + the built-in baseline the safe-bins editor resets to."""

    baseline_safe_bins: list[str]
    rows: list[PolicyRowOut]


class ResolveIn(BaseModel):
    """The inspector input: exactly one capability + the scope coordinates."""

    model_config = ConfigDict(extra="forbid")

    tool: str | None = None
    bin: str | None = None
    agent_id: int | None = None
    session_mode: SessionModeLiteral | None = None
    bot_session_id: int | None = None

    @model_validator(mode="after")
    def _exactly_one_capability(self) -> ResolveIn:
        if (self.tool is None) == (self.bin is None):
            raise ValueError("provide exactly one of 'tool' or 'bin'")
        return self


class ResolveOut(BaseModel):
    """The inspector verdict: allowed/denied + the deciding layer (trt.38)."""

    capability: str
    capability_kind: Literal["tool", "bin"]
    allowed: bool
    layer: str
    rule: str = ""
    detail: str = ""
    layers_consulted: list[str]


class PolicyLayerOut(BaseModel):
    scope: str
    scope_detail: str = ""
    document: dict[str, Any]


class EffectiveOut(BaseModel):
    """The resolved view backing the trt.37 policy editor."""

    layers: list[PolicyLayerOut]
    safe_bins: list[str]
    removed_baseline_bins: list[str]
    baseline_safe_bins: list[str]
    tools_unrestricted: bool
    allow_layer: str = ""


def _require_agent(db: Session, agent_id: int) -> None:
    if db.get(Agent, agent_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"agent {agent_id} not found"
        )


def _require_session(db: Session, bot_session_id: int) -> None:
    if db.get(BotSession, bot_session_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"session {bot_session_id} not found",
        )


def _upsert(
    db: Session,
    scope: str,
    payload: PolicyDocumentIn,
    *,
    agent_id: int | None = None,
    session_mode: str | None = None,
    bot_session_id: int | None = None,
) -> PolicyRowOut:
    try:
        row = upsert_policy_row(
            db,
            scope,
            payload.model_dump(),
            agent_id=agent_id,
            session_mode=session_mode,
            bot_session_id=bot_session_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return _row_out(row)


def _delete(
    db: Session,
    scope: str,
    *,
    agent_id: int | None = None,
    session_mode: str | None = None,
    bot_session_id: int | None = None,
) -> dict[str, bool]:
    try:
        deleted = delete_policy_row(
            db,
            scope,
            agent_id=agent_id,
            session_mode=session_mode,
            bot_session_id=bot_session_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return {"deleted": deleted}


@router.get("", response_model=PolicyListOut)
def list_policies(db: Annotated[Session, Depends(get_session)]) -> PolicyListOut:
    return PolicyListOut(
        baseline_safe_bins=list(builtin_baseline_safe_bins()),
        rows=[_row_out(row) for row in list_policy_rows(db)],
    )


@router.get("/effective", response_model=EffectiveOut)
def effective_policy(
    db: Annotated[Session, Depends(get_session)],
    agent_id: int | None = None,
    session_mode: SessionModeLiteral | None = None,
    bot_session_id: int | None = None,
) -> EffectiveOut:
    layers = load_policy_layers(
        db,
        agent_id=agent_id,
        session_mode=session_mode,
        bot_session_id=bot_session_id,
    )
    resolved = resolve_policy(layers)
    return EffectiveOut(
        layers=[
            PolicyLayerOut(
                scope=layer.scope,
                scope_detail=layer.scope_detail,
                document=layer.to_document(),
            )
            for layer in layers
        ],
        safe_bins=list(resolved.safe_bins),
        removed_baseline_bins=list(resolved.removed_baseline_bins),
        baseline_safe_bins=list(builtin_baseline_safe_bins()),
        tools_unrestricted=resolved.tools_unrestricted,
        allow_layer=resolved.allow_layer,
    )


@router.post("/resolve", response_model=ResolveOut)
def resolve_capability(
    payload: ResolveIn, db: Annotated[Session, Depends(get_session)]
) -> ResolveOut:
    """THE effective-policy inspector (the trt.38 acceptance API).

    Input: a tool kind or an exec binary + scope coordinates. Output:
    allowed/denied + the deciding layer/rule — exactly what a denial would
    record in its ``policy_denied`` event, so the UI can explain enforcement
    before it happens.
    """
    layers = load_policy_layers(
        db,
        agent_id=payload.agent_id,
        session_mode=payload.session_mode,
        bot_session_id=payload.bot_session_id,
    )
    resolved = resolve_policy(layers)
    if payload.tool is not None:
        capability, kind = payload.tool, "tool"
        decision = resolved.check_tool(payload.tool)
    else:
        assert payload.bin is not None  # the model validator guarantees it
        capability, kind = payload.bin, "bin"
        decision = resolved.check_bin(payload.bin)
    return ResolveOut(
        capability=capability,
        capability_kind=kind,  # type: ignore[arg-type]
        allowed=decision.allowed,
        layer=decision.layer,
        rule=decision.rule,
        detail=decision.detail,
        layers_consulted=[layer.scope for layer in layers],
    )


@router.put("/global", response_model=PolicyRowOut)
def put_global_policy(
    payload: PolicyDocumentIn, db: Annotated[Session, Depends(get_session)]
) -> PolicyRowOut:
    return _upsert(db, POLICY_SCOPE_GLOBAL, payload)


@router.delete("/global")
def delete_global_policy(db: Annotated[Session, Depends(get_session)]) -> dict[str, bool]:
    """Reset the global layer — including safe-bins back to the trt.35 baseline."""
    return _delete(db, POLICY_SCOPE_GLOBAL)


@router.put("/agents/{agent_id}", response_model=PolicyRowOut)
def put_agent_policy(
    agent_id: int,
    payload: PolicyDocumentIn,
    db: Annotated[Session, Depends(get_session)],
) -> PolicyRowOut:
    _require_agent(db, agent_id)
    return _upsert(db, POLICY_SCOPE_AGENT, payload, agent_id=agent_id)


@router.delete("/agents/{agent_id}")
def delete_agent_policy(
    agent_id: int, db: Annotated[Session, Depends(get_session)]
) -> dict[str, bool]:
    return _delete(db, POLICY_SCOPE_AGENT, agent_id=agent_id)


@router.put("/session-modes/{session_mode}", response_model=PolicyRowOut)
def put_session_mode_policy(
    session_mode: SessionModeLiteral,
    payload: PolicyDocumentIn,
    db: Annotated[Session, Depends(get_session)],
) -> PolicyRowOut:
    return _upsert(db, POLICY_SCOPE_SESSION_MODE, payload, session_mode=session_mode)


@router.delete("/session-modes/{session_mode}")
def delete_session_mode_policy(
    session_mode: SessionModeLiteral, db: Annotated[Session, Depends(get_session)]
) -> dict[str, bool]:
    return _delete(db, POLICY_SCOPE_SESSION_MODE, session_mode=session_mode)


@router.put("/sessions/{bot_session_id}", response_model=PolicyRowOut)
def put_session_policy(
    bot_session_id: int,
    payload: PolicyDocumentIn,
    db: Annotated[Session, Depends(get_session)],
) -> PolicyRowOut:
    _require_session(db, bot_session_id)
    return _upsert(db, POLICY_SCOPE_SESSION, payload, bot_session_id=bot_session_id)


@router.delete("/sessions/{bot_session_id}")
def delete_session_policy(
    bot_session_id: int, db: Annotated[Session, Depends(get_session)]
) -> dict[str, bool]:
    return _delete(db, POLICY_SCOPE_SESSION, bot_session_id=bot_session_id)


# Validate the session-mode literals stay in sync with the model vocabulary.
assert set(CAPABILITY_POLICY_SESSION_MODES) == {"meet", "browser"}

__all__ = ["router"]
