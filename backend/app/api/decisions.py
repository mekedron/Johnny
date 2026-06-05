"""HTTP endpoints for approving / rejecting pending agent decisions (US-027).

Two endpoints, both scoped to a bot session so the URL is shareable in
notifications and the lookup stays cheap:

* ``POST /sessions/{bot_session_id}/decisions/{decision_id}/approve``
* ``POST /sessions/{bot_session_id}/decisions/{decision_id}/reject``

The endpoint:

1. Validates the decision row exists, belongs to the session, and is
   currently ``outcome='pending'``.
2. Publishes ``{"decision_id": <id>, "action": "approve" | "reject"}``
   to the Redis channel ``johnny.approval.{session_id}``. The
   meet-worker's :class:`RedisApprovalGate` is subscribed; it returns
   the corresponding outcome, and the pipeline flips the decision row's
   outcome itself (so the API does NOT need to update ``agent_decisions``
   here — it would race with the pipeline's own write).
3. Returns the decision id + a status flag indicating whether the
   meet-worker actually had a listener for the message (a count of
   ``0`` subscribers signals "approval round already closed").

The Redis client is constructed via a small factory the test fixture
overrides; production uses the configured ``Settings.redis_url``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.config import get_settings
from app.db.models import AgentDecision, BotSession, DecisionOutcome
from app.services.approval import publish_approval

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions/{bot_session_id}/decisions", tags=["decisions"])


# --- Redis client factory indirection -------------------------------------

RedisClientFactory = Callable[[], Awaitable[Any]]


async def _default_redis_client_factory() -> Any:
    """Build a fresh ``redis.asyncio.Redis`` from the configured URL.

    Imports redis lazily so this module remains importable when the
    optional dep is not installed (e.g. in slimmer test environments).
    """
    from redis.asyncio import Redis

    settings = get_settings()
    return Redis.from_url(settings.redis_url, decode_responses=False)


_redis_factory: RedisClientFactory = _default_redis_client_factory


def set_redis_client_factory(factory: RedisClientFactory | None) -> None:
    """Replace the Redis client factory (tests override; production keeps default)."""
    global _redis_factory
    _redis_factory = factory or _default_redis_client_factory


def get_redis_client_factory() -> RedisClientFactory:
    return _redis_factory


# --- Pydantic responses ---------------------------------------------------


class DecisionActionResponse(BaseModel):
    """Returned by both approve and reject endpoints."""

    decision_id: int
    bot_session_id: int
    action: str
    subscribers: int


# --- Helpers --------------------------------------------------------------


def _load_pending(
    session: Session, bot_session_id: int, decision_id: int
) -> AgentDecision:
    """Resolve the decision row or raise a clean HTTP error."""
    bot_session = session.get(BotSession, bot_session_id)
    if bot_session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="bot_session not found",
        )
    row = session.get(AgentDecision, decision_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="decision not found",
        )
    if row.bot_session_id != bot_session_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="decision does not belong to this session",
        )
    if row.outcome != DecisionOutcome.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "decision is no longer pending",
                "outcome": row.outcome.value,
            },
        )
    return row


async def _dispatch(
    redis_factory: RedisClientFactory,
    bot_session_id: int,
    decision_id: int,
    action: str,
) -> int:
    client = await redis_factory()
    try:
        outcome = "approved" if action == "approve" else "rejected"
        subscribers = await publish_approval(
            client, str(bot_session_id), decision_id, outcome  # type: ignore[arg-type]
        )
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            logger.exception("approval dispatch: error closing redis client")
    return subscribers


# --- Endpoints ------------------------------------------------------------


SessionDep = Annotated[Session, Depends(get_session)]
RedisFactoryDep = Annotated[
    RedisClientFactory, Depends(get_redis_client_factory)
]


@router.post(
    "/{decision_id}/approve",
    response_model=DecisionActionResponse,
)
async def approve_decision(
    bot_session_id: int,
    decision_id: int,
    session: SessionDep,
    redis_factory: RedisFactoryDep,
) -> DecisionActionResponse:
    """Approve a pending decision — the meet-worker speaks the suggested reply."""
    _load_pending(session, bot_session_id, decision_id)
    subscribers = await _dispatch(
        redis_factory, bot_session_id, decision_id, "approve"
    )
    return DecisionActionResponse(
        decision_id=decision_id,
        bot_session_id=bot_session_id,
        action="approve",
        subscribers=subscribers,
    )


@router.post(
    "/{decision_id}/reject",
    response_model=DecisionActionResponse,
)
async def reject_decision(
    bot_session_id: int,
    decision_id: int,
    session: SessionDep,
    redis_factory: RedisFactoryDep,
) -> DecisionActionResponse:
    """Reject a pending decision — the meet-worker stays silent."""
    _load_pending(session, bot_session_id, decision_id)
    subscribers = await _dispatch(
        redis_factory, bot_session_id, decision_id, "reject"
    )
    return DecisionActionResponse(
        decision_id=decision_id,
        bot_session_id=bot_session_id,
        action="reject",
        subscribers=subscribers,
    )


__all__ = [
    "DecisionActionResponse",
    "get_redis_client_factory",
    "router",
    "set_redis_client_factory",
]
