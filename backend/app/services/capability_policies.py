"""Capability-policy persistence + resolution (Johnny-trt.38).

The DB half of the layered policy engine: CRUD over
:class:`~app.db.models.CapabilityPolicy` rows (one per scope target, the
provider-settings pattern) and the loader that turns the rows matching a
session's coordinates (agent / surface / session) into
:class:`johnny.skills.capability_policy.CapabilityPolicyLayer` objects for
:func:`johnny.skills.capability_policy.resolve_policy`.

Freshness model (the no-restart acceptance):

* **dispatch surfaces** resolve here once per session start and stamp the
  result into the trt.41 ``agent_snapshot`` (``capability_policy`` key) —
  the agent process never reads these tables;
* **the worker** calls :func:`resolve_policy_for_bot_session` fresh per
  claimed task — a policy edit bites a RUNNING session's next delegation
  without any restart (there is no cache to invalidate, exactly like
  provider settings).

Documents are normalized through
:meth:`~johnny.skills.capability_policy.CapabilityPolicyLayer.from_document`
→ ``to_document`` on write, so stored rows are always the canonical shape
the resolver reads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.db.models import (
    CAPABILITY_POLICY_SCOPES,
    CAPABILITY_POLICY_SESSION_MODES,
    Agent,
    BotSession,
    BotSessionSource,
    CapabilityPolicy,
)
from johnny.skills.capability_policy import (
    POLICY_SCOPE_AGENT,
    POLICY_SCOPE_GLOBAL,
    POLICY_SCOPE_SESSION,
    POLICY_SCOPE_SESSION_MODE,
    CapabilityPolicyLayer,
    ResolvedCapabilityPolicy,
    resolve_policy,
)
from johnny.skills.policy import BASELINE_BINS

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def builtin_baseline_safe_bins() -> tuple[str, ...]:
    """The trt.35 baseline the safe-bins editor resets to (plain data)."""
    return BASELINE_BINS


def _validate_target(
    scope: str,
    *,
    agent_id: int | None,
    session_mode: str | None,
    bot_session_id: int | None,
) -> None:
    """Mirror the 0030 CHECK constraints with actionable errors."""
    if scope not in CAPABILITY_POLICY_SCOPES:
        raise ValueError(f"unknown capability-policy scope {scope!r}")
    expected: dict[str, bool] = {
        "agent_id": scope == POLICY_SCOPE_AGENT,
        "session_mode": scope == POLICY_SCOPE_SESSION_MODE,
        "bot_session_id": scope == POLICY_SCOPE_SESSION,
    }
    actual = {
        "agent_id": agent_id is not None,
        "session_mode": session_mode is not None,
        "bot_session_id": bot_session_id is not None,
    }
    if expected != actual:
        raise ValueError(
            f"scope {scope!r} requires exactly its own target key "
            f"(expected {expected}, got {actual})"
        )
    if session_mode is not None and session_mode not in CAPABILITY_POLICY_SESSION_MODES:
        raise ValueError(
            f"unknown session_mode {session_mode!r} "
            f"(legal: {CAPABILITY_POLICY_SESSION_MODES})"
        )


def get_policy_row(
    db: Session,
    scope: str,
    *,
    agent_id: int | None = None,
    session_mode: str | None = None,
    bot_session_id: int | None = None,
) -> CapabilityPolicy | None:
    """The single row for one scope target, or ``None``."""
    _validate_target(
        scope, agent_id=agent_id, session_mode=session_mode, bot_session_id=bot_session_id
    )
    stmt = select(CapabilityPolicy).where(
        CapabilityPolicy.scope == scope,
        CapabilityPolicy.agent_id == agent_id,
        CapabilityPolicy.session_mode == session_mode,
        CapabilityPolicy.bot_session_id == bot_session_id,
    )
    return db.scalar(stmt)


def list_policy_rows(db: Session) -> list[CapabilityPolicy]:
    """Every stored layer row, global first (the resolution order, then target)."""
    rows = list(db.scalars(select(CapabilityPolicy)))
    order = {scope: index for index, scope in enumerate(CAPABILITY_POLICY_SCOPES)}
    rows.sort(
        key=lambda row: (
            order.get(row.scope, len(order)),
            row.agent_id or 0,
            row.session_mode or "",
            row.bot_session_id or 0,
        )
    )
    return rows


def upsert_policy_row(
    db: Session,
    scope: str,
    document: dict[str, Any],
    *,
    agent_id: int | None = None,
    session_mode: str | None = None,
    bot_session_id: int | None = None,
) -> CapabilityPolicy:
    """Create or replace THE row for one scope target (canonicalized document).

    ``safe_bins`` is global-only by design (the one curated baseline per
    install) — rejected here with :class:`ValueError` so the API surfaces a
    422 instead of the resolver silently ignoring it. The caller commits.
    """
    _validate_target(
        scope, agent_id=agent_id, session_mode=session_mode, bot_session_id=bot_session_id
    )
    layer = CapabilityPolicyLayer.from_document(scope, document)
    if layer.safe_bins is not None and scope != POLICY_SCOPE_GLOBAL:
        raise ValueError("safe_bins is only editable on the global layer")
    canonical = layer.to_document()
    row = get_policy_row(
        db,
        scope,
        agent_id=agent_id,
        session_mode=session_mode,
        bot_session_id=bot_session_id,
    )
    if row is None:
        row = CapabilityPolicy(
            scope=scope,
            agent_id=agent_id,
            session_mode=session_mode,
            bot_session_id=bot_session_id,
            document=canonical,
        )
        db.add(row)
    else:
        row.document = canonical
        row.updated_at = datetime.now(UTC)
    db.flush()
    logger.info(
        "capability policy: upserted scope=%s agent_id=%s session_mode=%s "
        "bot_session_id=%s (%s)",
        scope,
        agent_id,
        session_mode,
        bot_session_id,
        {k: len(v) if isinstance(v, list) else v for k, v in canonical.items()},
    )
    return row


def delete_policy_row(
    db: Session,
    scope: str,
    *,
    agent_id: int | None = None,
    session_mode: str | None = None,
    bot_session_id: int | None = None,
) -> bool:
    """Remove one scope target's row (reset that layer). The caller commits."""
    row = get_policy_row(
        db,
        scope,
        agent_id=agent_id,
        session_mode=session_mode,
        bot_session_id=bot_session_id,
    )
    if row is None:
        return False
    db.delete(row)
    db.flush()
    logger.info(
        "capability policy: deleted scope=%s agent_id=%s session_mode=%s bot_session_id=%s",
        scope,
        agent_id,
        session_mode,
        bot_session_id,
    )
    return True


def _agent_detail(db: Session, agent_id: int | None) -> str:
    if agent_id is None:
        return ""
    name = db.scalar(select(Agent.name).where(Agent.id == agent_id))
    return str(name) if name else f"agent-{agent_id}"


def load_policy_layers(
    db: Session,
    *,
    agent_id: int | None = None,
    session_mode: str | None = None,
    bot_session_id: int | None = None,
) -> tuple[CapabilityPolicyLayer, ...]:
    """The stored layers matching one session's coordinates, resolution-ordered.

    Missing rows are simply absent (an empty layer constrains nothing);
    ``scope_detail`` carries the human-readable target (agent name, mode,
    session id) for decision/event attribution copy.
    """
    layers: list[CapabilityPolicyLayer] = []
    row = get_policy_row(db, POLICY_SCOPE_GLOBAL)
    if row is not None:
        layers.append(
            CapabilityPolicyLayer.from_document(POLICY_SCOPE_GLOBAL, row.document)
        )
    if agent_id is not None:
        row = get_policy_row(db, POLICY_SCOPE_AGENT, agent_id=agent_id)
        if row is not None:
            layers.append(
                CapabilityPolicyLayer.from_document(
                    POLICY_SCOPE_AGENT,
                    row.document,
                    scope_detail=_agent_detail(db, agent_id),
                )
            )
    if session_mode is not None:
        row = get_policy_row(db, POLICY_SCOPE_SESSION_MODE, session_mode=session_mode)
        if row is not None:
            layers.append(
                CapabilityPolicyLayer.from_document(
                    POLICY_SCOPE_SESSION_MODE, row.document, scope_detail=session_mode
                )
            )
    if bot_session_id is not None:
        row = get_policy_row(db, POLICY_SCOPE_SESSION, bot_session_id=bot_session_id)
        if row is not None:
            layers.append(
                CapabilityPolicyLayer.from_document(
                    POLICY_SCOPE_SESSION,
                    row.document,
                    scope_detail=f"session-{bot_session_id}",
                )
            )
    return tuple(layers)


def resolve_capability_policy(
    db: Session,
    *,
    agent_id: int | None = None,
    session_mode: str | None = None,
    bot_session_id: int | None = None,
) -> ResolvedCapabilityPolicy:
    """Resolve the effective policy for one set of session coordinates."""
    return resolve_policy(
        load_policy_layers(
            db,
            agent_id=agent_id,
            session_mode=session_mode,
            bot_session_id=bot_session_id,
        )
    )


@dataclass(frozen=True, slots=True)
class SessionPolicyContext:
    """What the worker needs per claimed task: the policy + event timing.

    ``session_relative_ms`` maps "now" onto the session's
    ``conversation_events.timestamp_ms`` time base (0 when the session never
    started — the event still records, just unanchored).
    """

    policy: ResolvedCapabilityPolicy
    agent_id: int | None
    session_mode: str | None
    session_relative_ms: int


def resolve_policy_for_bot_session(
    db: Session, bot_session_id: int
) -> SessionPolicyContext:
    """The worker's per-claim resolution: session row → coordinates → policy.

    Reads the live ``bot_sessions`` row for the agent / surface coordinates
    (NOT the snapshot — the whole point of the worker-side check is picking
    up edits made after dispatch). An unknown session resolves the global +
    nothing else (defensive: the claim already proved the row existed, so
    this leg is unreachable outside test fixtures).
    """
    row = db.get(BotSession, bot_session_id)
    agent_id: int | None = None
    session_mode: str | None = None
    started_at = None
    if row is not None:
        agent_id = row.agent_id
        source = row.source
        session_mode = source.value if isinstance(source, BotSessionSource) else str(source)
        started_at = row.started_at
    policy = resolve_capability_policy(
        db,
        agent_id=agent_id,
        session_mode=session_mode,
        bot_session_id=bot_session_id,
    )
    relative_ms = 0
    if started_at is not None:
        anchor = started_at if started_at.tzinfo else started_at.replace(tzinfo=UTC)
        relative_ms = max(0, int((datetime.now(UTC) - anchor).total_seconds() * 1000))
    return SessionPolicyContext(
        policy=policy,
        agent_id=agent_id,
        session_mode=session_mode,
        session_relative_ms=relative_ms,
    )


__all__ = [
    "SessionPolicyContext",
    "builtin_baseline_safe_bins",
    "delete_policy_row",
    "get_policy_row",
    "list_policy_rows",
    "load_policy_layers",
    "resolve_capability_policy",
    "resolve_policy_for_bot_session",
    "upsert_policy_row",
]
