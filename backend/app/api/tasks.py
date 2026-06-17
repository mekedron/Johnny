"""HTTP endpoint for cancelling a running delegated task / workstream (US-302).

``POST /sessions/{bot_session_id}/tasks/{task_id}/cancel`` — the UI Cancel
button on a running workstream. Scoped to the session like the approve/reject
endpoints so the URL is cheap to authorise and shareable.

The endpoint:

1. Validates the ``agent_tasks`` row exists, belongs to the session, and is
   still cancellable (not already terminal).
2. Publishes ``{"action": "cancel", "task_id": N, "actor": "ui"}`` to the
   session's inbound control channel ``johnny.control.{session_id}`` (the
   :mod:`johnny.agent.session_control` precedent established by the approval
   flow). The running meet-worker's :class:`SessionControlListener` drives
   :meth:`~johnny.agent.tasks.TaskCoordinator.cancel_task`, which cuts the
   in-session resolver or — for worker-owned work — signals the worker over
   ``johnny.tasks.cancel`` to cut its in-flight runner.
3. Returns the subscriber count of the control publish so the API can surface
   "that session isn't live" (``0`` listeners). The durable ``cancelled``
   transition + the live ``task_cancelled`` event are emitted by whichever
   locus actually cut the work — the endpoint never fabricates state, exactly
   like the approve/reject dispatch.

The Redis client factory is shared with :mod:`app.api.decisions` so a single
test seam overrides every control-channel publish.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.decisions import RedisClientFactory, get_redis_client_factory
from app.api.deps import get_session
from app.db.models import AgentTask, AgentTaskStatus, BotSession
from johnny.agent.session_control import publish_cancel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions/{bot_session_id}/tasks", tags=["tasks"])

# Statuses a task can no longer be cancelled from — it already settled.
_TERMINAL_TASK_STATUSES = frozenset(
    {
        AgentTaskStatus.DONE,
        AgentTaskStatus.FAILED,
        AgentTaskStatus.CANCELLED,
        AgentTaskStatus.EXPIRED,
    }
)


class TaskCancelResponse(BaseModel):
    """Returned by the cancel endpoint."""

    task_id: int
    bot_session_id: int
    action: str
    prior_status: str
    subscribers: int


def _load_cancellable_task(
    session: Session, bot_session_id: int, task_id: int
) -> AgentTask:
    """Resolve the task row or raise a clean HTTP error."""
    bot_session = session.get(BotSession, bot_session_id)
    if bot_session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="bot_session not found",
        )
    row = session.get(AgentTask, task_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="task not found",
        )
    if row.bot_session_id != bot_session_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="task does not belong to this session",
        )
    if row.status in _TERMINAL_TASK_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "task is no longer running",
                "status": row.status.value,
            },
        )
    return row


SessionDep = Annotated[Session, Depends(get_session)]
RedisFactoryDep = Annotated[RedisClientFactory, Depends(get_redis_client_factory)]


@router.post("/{task_id}/cancel", response_model=TaskCancelResponse)
async def cancel_task(
    bot_session_id: int,
    task_id: int,
    session: SessionDep,
    redis_factory: RedisFactoryDep,
) -> TaskCancelResponse:
    """Cancel a running workstream — cut execution, not just speech (US-302)."""
    row = _load_cancellable_task(session, bot_session_id, task_id)
    prior_status = row.status.value
    client = await redis_factory()
    try:
        subscribers = await publish_cancel(
            client, str(bot_session_id), task_id, actor="ui"
        )
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            logger.exception("cancel dispatch: error closing redis client")
    logger.info(
        "cancel dispatch: task_id=%d session_id=%d prior=%s subscribers=%d",
        task_id,
        bot_session_id,
        prior_status,
        subscribers,
    )
    return TaskCancelResponse(
        task_id=task_id,
        bot_session_id=bot_session_id,
        action="cancel",
        prior_status=prior_status,
        subscribers=subscribers,
    )


__all__ = ["TaskCancelResponse", "router"]
