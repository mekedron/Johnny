"""HTTP endpoints for the session's delegated tasks / workstreams.

Two routes, both scoped to the session like the approve/reject endpoints so
the URL is cheap to authorise and shareable:

* ``POST /sessions/{bot_session_id}/tasks/{task_id}/cancel`` (US-302) — the UI
  Cancel button on a running workstream.
* ``POST /sessions/{bot_session_id}/tasks/{task_id}/callback`` (US-303,
  Johnny-d6w.18) — the authenticated webhook an out-of-process workstream POSTs
  to re-enter the session and report its result (see :func:`task_callback`).

The cancel endpoint:

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

import hmac
import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.decisions import RedisClientFactory, get_redis_client_factory
from app.api.deps import get_session
from app.db.models import AgentTask, AgentTaskStatus, BotSession
from johnny.agent.session_control import publish_cancel
from johnny.agent.task_wiring import publish_task_completed_frames

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


class TaskCallbackRequest(BaseModel):
    """Body of the external-workstream webhook callback (US-303).

    ``callback_token`` is the per-task secret minted when the
    ``external_callback`` workstream began (``TaskCoordinator.begin``);
    ``status`` is the terminal the external system settled to. ``result_text``
    is the speech-ready summary a live session talks back; ``result_json`` is
    optional structured output; ``error`` is operator/diagnostic detail (never
    spoken). FastAPI rejects a ``status`` outside ``done``/``failed`` with 422.
    """

    callback_token: str
    status: Literal["done", "failed"]
    result_text: str | None = None
    result_json: dict[str, Any] | None = None
    error: str | None = None


class TaskCallbackResponse(BaseModel):
    """Returned by the callback endpoint."""

    task_id: int
    bot_session_id: int
    action: str
    status: str
    # A live session heard the talk-back frame (``johnny.tasks.<id>`` had a
    # subscriber); ``False`` means the result was only persisted + shown.
    spoken: bool
    # The task was already terminal — a duplicate callback handled as a no-op.
    idempotent: bool


def _load_callback_task(
    session: Session, bot_session_id: int, task_id: int
) -> AgentTask:
    """Resolve the task row or raise a clean HTTP error.

    Unlike :func:`_load_cancellable_task` there is **no** terminal-status guard:
    a duplicate callback over an already-settled task is idempotent (handled in
    the endpoint), not a 409.
    """
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
    return row


@router.post("/{task_id}/callback", response_model=TaskCallbackResponse)
async def task_callback(
    bot_session_id: int,
    task_id: int,
    payload: TaskCallbackRequest,
    session: SessionDep,
    redis_factory: RedisFactoryDep,
) -> TaskCallbackResponse:
    """Accept an external workstream's result and re-enter the session (US-303).

    The out-of-process re-entry path. An ``external_callback`` workstream queued
    with a minted ``callback_token`` (and no resolver/watcher) settles **only**
    here: the external system POSTs its result with the token, and this endpoint

    1. authenticates the token (constant-time) against the row's
       ``callback_token`` — a mismatch or a non-external task (NULL token) is a
       ``403`` with **zero side effects**;
    2. is **idempotent** — a duplicate callback over an already-terminal task
       returns ``200`` with the current state and re-emits nothing;
    3. settles the executor-owned ``agent_tasks`` row (``done``/``failed`` +
       result), then publishes one ``TaskCompleted`` frame on both task surfaces
       (:func:`~johnny.agent.task_wiring.publish_task_completed_frames`): the
       always-on durable writer settles the ``agent_workstreams`` envelope
       (live *or* ended) and the WS updates a connected browser; the live
       in-session listener (if any) speaks the result as
       ``AgentSpoke(kind="task_result", turn_id=None)`` — never a turn terminal
       (INV-1). An ended session persists + shows the result but speaks nothing.
    """
    row = _load_callback_task(session, bot_session_id, task_id)
    # Auth: external_callback workstreams carry a token; everything else (NULL)
    # fails closed. Constant-time compare so a wrong token can't be timed out.
    stored = row.callback_token
    if not stored or not hmac.compare_digest(stored, payload.callback_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid callback token",
        )
    # Idempotency: a settled task is a no-op replay — no re-settle, no re-emit.
    if row.status in _TERMINAL_TASK_STATUSES:
        logger.info(
            "task callback: task_id=%d session_id=%d already %s — idempotent no-op",
            task_id,
            bot_session_id,
            row.status.value,
        )
        return TaskCallbackResponse(
            task_id=task_id,
            bot_session_id=bot_session_id,
            action="callback",
            status=row.status.value,
            spoken=False,
            idempotent=True,
        )
    # Settle the executor-owned row — the webhook IS the external executor's
    # terminal write (the single-durable-writer rule covers agent_workstreams,
    # which the subscriber still owns; agent_tasks stays executor-owned).
    new_status = (
        AgentTaskStatus.DONE if payload.status == "done" else AgentTaskStatus.FAILED
    )
    row.status = new_status
    if payload.result_text is not None:
        row.result_text = payload.result_text
    if payload.result_json is not None:
        row.result_json = dict(payload.result_json)
    if payload.error is not None:
        row.error = payload.error
    session.commit()

    client = await redis_factory()
    try:
        talk_back = await publish_task_completed_frames(
            client,
            session_id=str(bot_session_id),
            task_id=task_id,
            kind=row.kind,
            status=payload.status,
            result_text=payload.result_text or "",
            error=payload.error or "",
            turn_id=row.turn_id,
            request_id=row.request_id,
        )
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            logger.exception("task callback: error closing redis client")
    logger.info(
        "task callback: task_id=%d session_id=%d settled=%s talk_back_subs=%d",
        task_id,
        bot_session_id,
        new_status.value,
        talk_back,
    )
    return TaskCallbackResponse(
        task_id=task_id,
        bot_session_id=bot_session_id,
        action="callback",
        status=new_status.value,
        spoken=talk_back > 0,
        idempotent=False,
    )


__all__ = [
    "TaskCallbackRequest",
    "TaskCallbackResponse",
    "TaskCancelResponse",
    "router",
]
