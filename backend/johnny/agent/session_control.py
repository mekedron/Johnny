"""Inbound session control channel — UI commands → the running agent (US-302).

The live event stream flows agent → browser (the pipeline publishes to
``johnny.session.{id}`` and the API's WebSocket fans it out). A few operator
actions need the **reverse** direction — a command the browser issues that has
to reach the in-process engine of a *running* session. The approval flow
(:mod:`app.services.approval`) established the pattern: a per-session Redis
pub/sub control channel the API publishes on and the meet-worker subscribes to.

This module is the second such channel, for **workstream cancel**
(Johnny-d6w.17):

* ``johnny.control.{session_id}`` — control channel. Messages look like
  ``{"action": "cancel", "task_id": N, "actor": "ui"}``. The API publishes;
  the meet-worker's :class:`SessionControlListener` subscribes and drives
  :meth:`~johnny.agent.tasks.TaskCoordinator.cancel_task`, which routes the cut
  to the in-session resolver or — for worker-owned work — on to the worker over
  ``johnny.tasks.cancel``. One channel in, all the origin routing reused.

Like :class:`~johnny.agent.task_wiring.RedisTaskWake` the Redis import is lazy
so importing this module stays cheap; the listener is best-effort and
self-healing (a dropped connection backs off and resubscribes), because a
missed cancel only costs liveness — the durable task row is the contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from johnny.agent.tasks import CancelActor, TaskCoordinator

logger = logging.getLogger(__name__)

SESSION_CONTROL_CHANNEL_PREFIX = "johnny.control."
"""Redis pub/sub channel prefix for per-session inbound control commands."""

CONTROL_RECONNECT_BACKOFF_S = 2.0
"""Backoff before the listener resubscribes after a dropped connection."""

_VALID_ACTORS = ("voice", "ui", "system")


def control_channel(session_id: str) -> str:
    """Build the inbound control channel name for one session."""
    return f"{SESSION_CONTROL_CHANNEL_PREFIX}{session_id}"


def build_cancel_command(task_id: int, *, actor: CancelActor) -> str:
    """Serialise a ``cancel`` control message (the wire form both sides share)."""
    return json.dumps(
        {"action": "cancel", "task_id": task_id, "actor": actor},
        separators=(",", ":"),
    )


async def publish_cancel(
    redis_client: Any,
    session_id: str,
    task_id: int,
    *,
    actor: CancelActor = "ui",
) -> int:
    """Push a ``cancel`` command onto a session's control channel (US-302).

    Used by the API ``POST /sessions/{id}/tasks/{task_id}/cancel`` endpoint.
    Returns the number of subscribers Redis delivered to — ``0`` means no
    meet-worker is listening (the session is not running its engine), which the
    caller surfaces as a 409 so the UI can say "that session isn't live".
    """
    channel = control_channel(session_id)
    result = await redis_client.publish(
        channel, build_cancel_command(task_id, actor=actor)
    )
    return int(result)


class SessionControlListener:
    """Subscribe one session's control channel and drive the coordinator.

    One per running session, mirroring :class:`RedisApprovalGate`'s lazy
    connect + resubscribe discipline. :meth:`start` spawns the subscribe loop;
    :meth:`aclose` cancels it and releases the client. Every command is
    contained: a malformed frame is dropped, and a coordinator call that raises
    is logged, never crashing the loop.
    """

    def __init__(
        self,
        *,
        redis_url: str,
        session_id: str,
        coordinator: TaskCoordinator,
    ) -> None:
        self._redis_url = redis_url
        self._session_id = session_id
        self._coordinator = coordinator
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        """Spawn the subscribe loop (idempotent)."""
        if self._task is not None:
            return
        self._task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        from redis.asyncio import Redis

        while not self._stop.is_set():
            client: Any | None = None
            try:
                client = Redis.from_url(self._redis_url, decode_responses=False)
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                await pubsub.subscribe(control_channel(self._session_id))
                logger.info(
                    "session control: subscribed to %s",
                    control_channel(self._session_id),
                )
                while not self._stop.is_set():
                    try:
                        message = await pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=1.0
                        )
                    except TimeoutError:
                        continue
                    if message is None or message.get("type") != "message":
                        continue
                    await self._handle(message.get("data"))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "session control: subscription dropped — reconnecting in %.0fs",
                    CONTROL_RECONNECT_BACKOFF_S,
                )
                await asyncio.sleep(CONTROL_RECONNECT_BACKOFF_S)
            finally:
                if client is not None:
                    try:
                        await client.aclose()
                    except Exception:  # noqa: BLE001
                        pass

    async def _handle(self, data: Any) -> None:
        if isinstance(data, bytes | bytearray):
            try:
                data = data.decode("utf-8")
            except UnicodeDecodeError:
                return
        if not isinstance(data, str):
            return
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            logger.warning("session control: dropping malformed json: %r", data[:200])
            return
        if not isinstance(payload, dict):
            return
        if payload.get("action") != "cancel":
            return  # forward-compat: unknown actions are ignored
        task_id = payload.get("task_id")
        if not isinstance(task_id, int):
            return
        actor_raw = payload.get("actor")
        actor: CancelActor = actor_raw if actor_raw in _VALID_ACTORS else "ui"
        try:
            outcome = await self._coordinator.cancel_task(task_id, actor=actor)
        except Exception:
            logger.exception(
                "session control: cancel_task raised for task_id=%s", task_id
            )
            return
        logger.info(
            "session control: cancel task_id=%s actor=%s -> %s",
            task_id,
            actor,
            outcome,
        )

    async def aclose(self) -> None:
        """Stop the loop and release the client (safe to call more than once)."""
        self._stop.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None


__all__ = [
    "CONTROL_RECONNECT_BACKOFF_S",
    "SESSION_CONTROL_CHANNEL_PREFIX",
    "SessionControlListener",
    "build_cancel_command",
    "control_channel",
    "publish_cancel",
]
