"""Production wiring for delegated async tasks (Johnny-trt.18 — Phase 3).

The coordinator core (:mod:`johnny.agent.tasks`) ships with every I/O boundary
injected as a callable, so it stays ``livekit``-/``sqlalchemy``-/``redis``-free
and unit tests run without the ``agent`` extra (the
:mod:`johnny.agent.approval` / :mod:`johnny.agent.approval_wiring` split). This
module supplies the **real** seams for a running session:

* :func:`build_publish_task_queued` — publish
  :class:`~johnny.voice_pipeline.events.TaskQueued` on the session
  :class:`~johnny.voice_pipeline.event_bus.EventBus` channel (the live-UI /
  WS surface; the status subscriber ignores the type — the durable record is
  the ``agent_tasks`` row the sink already wrote);
* :class:`RedisTaskWake` — publish a wake ping on the shared
  :data:`TASKS_WAKE_CHANNEL` Redis channel so a future external task worker
  (Phase 4) picks queued work up without polling. Publish-only and lazily
  connected; harmless today when nothing subscribes;
* :func:`build_task_coordinator` — the single factory
  :func:`~johnny.agent.job_session.build_agent_runtime` calls once it has the
  session's :class:`~johnny.agent.tasks.TaskSink`: wires the announce seams,
  defaults the executor to the Phase-3 :func:`~johnny.agent.tasks.stub_executor`
  (every kind fails fast with speech-ready text — an ack must never be a dead
  promise), and returns the coordinator plus the wake publisher whose Redis
  client the runtime must close at teardown.

Imported only by the full-stack worker / api / tests — it reaches into
``johnny.voice_pipeline`` (events, event_bus) and lazily into ``redis``, so it
is never pulled from the import-safe top-level :mod:`johnny.agent` package.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from johnny.agent.tasks import (
    PublishQueued,
    QueuedTask,
    TaskCoordinator,
    TaskExecutor,
    TaskSink,
    stub_executor,
)
from johnny.voice_pipeline.event_bus import EventBus
from johnny.voice_pipeline.events import TaskQueued

logger = logging.getLogger(__name__)

TASKS_WAKE_CHANNEL = "johnny.tasks.wake"
"""Redis pub/sub channel queued-task wake pings go out on.

Shared across sessions (unlike the per-session ``johnny.session.{id}`` /
``johnny.approval.{id}`` channels): a future Phase-4 task worker subscribes
once and learns about new work from every session. The payload is a small
JSON object — ``{"task_id": N, "kind": ..., "session_id": ...}`` — a *nudge*,
not the work itself; the durable queue is the ``agent_tasks`` table.
"""


def _default_clock_ms() -> int:
    """Epoch milliseconds — the timestamp shape the pipeline events carry."""
    return int(time.time() * 1000)


def build_publish_task_queued(
    event_bus: EventBus,
    *,
    session_id: str | None = None,
    clock: Callable[[], int] = _default_clock_ms,
) -> PublishQueued:
    """Publish :class:`TaskQueued` for a freshly persisted task.

    Mirrors the observability emitters: the event is the live-UI signal that
    the row (already durable — the coordinator persists before announcing)
    exists and can be queried by ``task_id``.
    """

    async def _publish(queued: QueuedTask) -> None:
        await event_bus.publish(
            TaskQueued(
                task_id=queued.task_id,
                kind=queued.spec.kind,
                timestamp_ms=clock(),
                turn_id=queued.spec.turn_id,
                decision_id=queued.spec.decision_id,
                ack_text=queued.spec.ack_text,
                session_id=session_id,
            )
        )

    return _publish


class RedisTaskWake:
    """Publish-only wake pings on :data:`TASKS_WAKE_CHANNEL`.

    Lazily connects on the first ping (the :class:`RedisApprovalGate`
    discipline, minus the subscribe half) and reuses one client for the
    session. The coordinator already contains ping failures, so this only
    has to be honest: connect, publish, close. ``client`` may be injected
    for tests; otherwise it is built from ``redis_url`` on first use.
    """

    def __init__(
        self,
        *,
        redis_url: str,
        session_id: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._redis_url = redis_url
        self._session_id = session_id
        self._client = client

    async def _connect(self) -> Any:
        if self._client is None:
            from redis.asyncio import Redis as RedisClient

            self._client = RedisClient.from_url(self._redis_url, decode_responses=False)
        return self._client

    async def __call__(self, queued: QueuedTask) -> None:
        client = await self._connect()
        payload = {
            "task_id": queued.task_id,
            "kind": queued.spec.kind,
            "session_id": self._session_id,
        }
        await client.publish(TASKS_WAKE_CHANNEL, json.dumps(payload, separators=(",", ":")))

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.aclose()
        except Exception:
            logger.exception("task wake: error closing redis client")
        self._client = None


def build_task_coordinator(
    *,
    task_sink: TaskSink,
    event_bus: EventBus,
    session_id: str | None = None,
    redis_url: str | None = None,
    executor: TaskExecutor | None = None,
    clock: Callable[[], int] = _default_clock_ms,
) -> tuple[TaskCoordinator, RedisTaskWake | None]:
    """Assemble the production :class:`TaskCoordinator` for one session.

    ``executor`` defaults to the Phase-3 :func:`stub_executor` (every kind
    fails fast with speech-ready text). ``redis_url=None`` simply skips the
    wake ping — the in-process executor needs no nudge, and the durable row +
    ``TaskQueued`` event still flow. Returns ``(coordinator, wake)``; the
    caller owns both teardowns (:meth:`TaskCoordinator.aclose` drains in-flight
    resolvers, :meth:`RedisTaskWake.close` releases the Redis client).
    """
    wake = RedisTaskWake(redis_url=redis_url, session_id=session_id) if redis_url else None
    coordinator = TaskCoordinator(
        task_sink,
        executor=executor if executor is not None else stub_executor,
        publish_queued=build_publish_task_queued(event_bus, session_id=session_id, clock=clock),
        wake=wake,
    )
    return coordinator, wake


__all__ = [
    "TASKS_WAKE_CHANNEL",
    "RedisTaskWake",
    "build_publish_task_queued",
    "build_task_coordinator",
]
