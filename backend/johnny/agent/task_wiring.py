"""Production wiring for delegated async tasks (Johnny-trt.18 — Phase 3).

The coordinator core (:mod:`johnny.agent.tasks`) ships with every I/O boundary
injected as a callable, so it stays ``livekit``-/``sqlalchemy``-/``redis``-free
and unit tests run without the ``agent`` extra (the
:mod:`johnny.agent.approval` / :mod:`johnny.agent.approval_wiring` split). This
module supplies the **real** seams for a running session:

* :func:`build_publish_task_queued` / :func:`build_publish_task_completed` —
  publish :class:`~johnny.voice_pipeline.events.TaskQueued` /
  :class:`~johnny.voice_pipeline.events.TaskCompleted` on the session
  :class:`~johnny.voice_pipeline.event_bus.EventBus` channel (the live-UI /
  WS surface; the status subscriber ignores both types — the durable record
  is the ``agent_tasks`` row the coordinator's sink writes before either
  event fires, Johnny-trt.25);
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
  client the runtime must close at teardown;
* the **Phase-5 speech-queue wiring** (Johnny-trt.28, the
  ``ApprovalCoordinator``-wiring pattern): :class:`TaskEventListener` — the
  per-session push consumer of ``johnny.tasks.<bot_session_id>`` that turns
  the worker's ``TaskProgress`` / ``TaskCompleted`` frames into coordinator
  registry updates and exactly-once settle effects; :class:`TaskSpeechDeliverer`
  — the delivery loop that releases queued results only at conversational
  boundaries (``current_speech`` is None ∧ user not speaking ∧
  :attr:`RouterGate.idle` ∧ the queue's ~1.2 s silence grace) and speaks them
  via :meth:`RouterGate.speak_task_result` (``session.say()``, no LLM hop, no
  ``bind_reply`` interaction); and :func:`attach_task_speech_wiring` — the
  one factory both session surfaces (agent worker, browser playground) call
  right after ``session.start``, storing the assembled
  :class:`TaskSpeechWiring` on the runtime for teardown in
  ``AgentRuntime.aclose``.

Imported only by the full-stack worker / api / tests — it reaches into
``johnny.voice_pipeline`` (events, event_bus) and lazily into ``redis``
(``livekit`` only under ``TYPE_CHECKING``), so it is never pulled from the
import-safe top-level :mod:`johnny.agent` package.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from johnny.agent.speech_floor import (
    RELEASE_COMPLETED,
    RELEASE_INTERRUPTED,
    RELEASE_SAY_UNAVAILABLE,
    FloorLease,
    SpeechFloor,
)
from johnny.agent.speech_queue import (
    DEFAULT_SILENCE_GRACE_S,
    DROP_QUEUE_CLOSED,
    SpeechItem,
    SpeechPriority,
    SpeechQueue,
)
from johnny.agent.tasks import (
    CancelActor,
    PublishCancelled,
    PublishCompleted,
    PublishQueued,
    QueuedTask,
    RunsInSession,
    TaskCoordinator,
    TaskExecutor,
    TaskRegistryEntry,
    TaskResult,
    TaskSink,
    TaskStatus,
    stub_executor,
)
from johnny.voice_pipeline.event_bus import (
    DEFAULT_CHANNEL_PREFIX,
    EventBus,
)
from johnny.voice_pipeline.events import (
    TaskCancelled,
    TaskCompleted,
    TaskCompletedStatus,
    TaskProgress,
    TaskQueued,
    TaskResultExpired,
    WorkstreamDeliveredStatus,
    WorkstreamDeliveryChanged,
    event_to_dict,
)

if TYPE_CHECKING:
    from livekit.agents.voice import AgentSession

    from johnny.agent.job_session import AgentRuntime
    from johnny.agent.router_gate import RouterGate
    from johnny.skills.executor import TaskProgressReporter

logger = logging.getLogger(__name__)

FLOOR_DELIVERY_WAIT_S = 2.0
"""Floor-acquire wait for a queued result (Johnny-trt.46). Deliberately short:
the predicate saw the floor open this tick, so a miss is a races-with-peer
edge — the item is restored unblamed and the next tick retries; the long
:data:`~johnny.agent.speech_floor.DEFAULT_ACQUIRE_TIMEOUT_S` wait is for
turn-bound speech that has no retry loop behind it."""

TASKS_WAKE_CHANNEL = "johnny.tasks.wake"
"""Redis pub/sub channel queued-task wake pings go out on.

Shared across sessions (unlike the per-session ``johnny.session.{id}`` /
``johnny.approval.{id}`` channels): the Phase-4 task worker
(:mod:`app.services.task_worker`, Johnny-trt.24) subscribes once and learns
about new work from every session. The payload is a small JSON object —
``{"task_id": N, "kind": ..., "session_id": ...}`` — a *nudge*, not the work
itself; the durable queue is the ``agent_tasks`` table. Pinged for every
queued task, internal kinds included (the worker's claim excludes those by
kind, so the extra nudge is harmless).
"""

TASKS_CANCEL_CHANNEL = "johnny.tasks.cancel"
"""Redis pub/sub channel user-cancel signals for worker-owned tasks go out on
(Johnny-d6w.17, US-302).

Shared across sessions like :data:`TASKS_WAKE_CHANNEL`: the Phase-4 task worker
subscribes once and cuts the in-flight runner for any ``{"task_id": N}`` it
hears, settling the row ``cancelled``. Published by **both** the agent (a voice
``cancel`` verdict over a worker-owned task) and the API (the UI Cancel button),
so a worker-owned task can be stopped from either surface. ``.cancel`` shares
the ``johnny.tasks`` prefix but is a reserved (non-numeric) suffix, never a
session id."""

TASKS_CHANNEL_PREFIX = "johnny.tasks"
"""Channel prefix for the per-session *agent* task channel (Johnny-trt.24):
the worker publishes ``TaskProgress`` / ``TaskCompleted`` on
``johnny.tasks.<bot_session_id>`` in addition to the UI session channel, so
the Phase-5 in-session listener (Johnny-trt.28) can subscribe to exactly its
own tasks. ``.wake`` shares the prefix but is a reserved (non-numeric)
suffix, never a session id."""


def _default_clock_ms() -> int:
    """Epoch milliseconds — the timestamp shape the pipeline events carry."""
    return int(time.time() * 1000)


async def publish_task_completed_frames(
    client: Any,
    *,
    session_id: str,
    task_id: int,
    kind: str,
    status: TaskCompletedStatus,
    result_text: str = "",
    error: str = "",
    turn_id: int | None = None,
    request_id: str | None = None,
    clock: Callable[[], int] = _default_clock_ms,
) -> int:
    """Publish one :class:`TaskCompleted` frame on **both** task surfaces.

    The out-of-process re-entry analogue of the worker's dual ``_publish``
    (:mod:`app.services.task_worker`): the same event, serialized identically
    (:func:`~johnny.voice_pipeline.events.event_to_dict`), goes to —

    * the **UI session channel** ``johnny.session.<id>`` — consumed by the
      always-on single durable writer (:func:`apply_task_event` settles the
      ``agent_workstreams`` envelope ``done``/``failed``, live *or* ended) and
      by the per-session WS fan-out (a connected browser updates in place);
    * the **agent task channel** ``johnny.tasks.<id>`` — the per-session
      :class:`TaskEventListener`, which (only while the session is live) drives
      :class:`TaskSpeechDeliverer` to speak the result as
      ``AgentSpoke(kind="task_result", turn_id=None)`` — never a turn terminal.

    Returns the agent-channel subscriber count: ``> 0`` means a live session
    heard it and will talk the result back; ``0`` means the result was only
    persisted + shown (an ended session — the trt.31 contract). Best-effort,
    like every event publish: the durable ``agent_tasks`` row the webhook
    already settled is the record.
    """
    event = TaskCompleted(
        task_id=task_id,
        kind=kind,
        status=status,
        timestamp_ms=clock(),
        result_text=result_text,
        error=error,
        turn_id=turn_id,
        session_id=session_id,
        request_id=request_id,
    )
    payload = json.dumps(event_to_dict(event), separators=(",", ":"))
    # UI + durable writer first (the persistence guarantee), talk-back second.
    await client.publish(f"{DEFAULT_CHANNEL_PREFIX}.{session_id}", payload)
    talk_back = await client.publish(f"{TASKS_CHANNEL_PREFIX}.{session_id}", payload)
    return int(talk_back)


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
                request_id=queued.spec.request_id,
                source_kind=queued.spec.source_kind,
                session_id=session_id,
            )
        )

    return _publish


class _TaskProgressReporter:
    """A progress reporter bound to one claimed task (US-202, Johnny-d6w.14).

    Structurally satisfies :class:`~johnny.skills.executor.TaskProgressReporter`.
    Each :meth:`report` increments a monotonic ``step`` (starting at 1 — step 0
    is the worker's bare claim signal) and publishes a :class:`TaskProgress`
    through ``publish``.
    """

    def __init__(
        self,
        publish: Callable[[TaskProgress], Awaitable[None]],
        *,
        task_id: int,
        kind: str,
        turn_id: int | None,
        request_id: str | None,
        session_id: str | None,
        clock: Callable[[], int],
    ) -> None:
        self._publish = publish
        self._task_id = task_id
        self._kind = kind
        self._turn_id = turn_id
        self._request_id = request_id
        self._session_id = session_id
        self._clock = clock
        self._step = 0

    async def report(self, text: str, *, phase: str | None = None) -> None:
        self._step += 1
        await self._publish(
            TaskProgress(
                task_id=self._task_id,
                kind=self._kind,
                timestamp_ms=self._clock(),
                progress_text=text,
                turn_id=self._turn_id,
                request_id=self._request_id,
                session_id=self._session_id,
                step=self._step,
                phase=phase,
            )
        )


def make_task_progress_reporter(
    publish: Callable[[TaskProgress], Awaitable[None]],
    *,
    task_id: int,
    kind: str,
    turn_id: int | None,
    request_id: str | None,
    session_id: str | None,
    clock: Callable[[], int] = _default_clock_ms,
) -> TaskProgressReporter:
    """Bind a per-task progress reporter (US-202, Johnny-d6w.14).

    Shared by the live worker (``publish`` fans the frame to the UI + agent
    channels via its ``_publish``) and the scenario harness (``publish`` is the
    in-memory bus), so both drive the identical milestone → ``TaskProgress`` →
    durable-writer path — the harness exercises the production emission seam, not
    a stand-in. ``publish`` is the only side-effect: a publish failure is the
    caller's to swallow (the worker's ``_publish`` already does), so a bus hiccup
    never fails a task.
    """
    return _TaskProgressReporter(
        publish,
        task_id=task_id,
        kind=kind,
        turn_id=turn_id,
        request_id=request_id,
        session_id=session_id,
        clock=clock,
    )


def build_publish_task_completed(
    event_bus: EventBus,
    *,
    session_id: str | None = None,
    clock: Callable[[], int] = _default_clock_ms,
) -> PublishCompleted:
    """Publish :class:`TaskCompleted` for a freshly settled task (Johnny-trt.25).

    The resolver invokes this *after* the terminal ``agent_tasks`` row write
    and only for ``done``/``failed`` settles (see
    :data:`~johnny.agent.tasks.PublishCompleted`), so the event always
    describes durable, queryable state. ``status`` arrives pre-normalized
    ("done"/"failed"); the cast to the event's narrower Literal is safe by
    that contract.
    """

    async def _publish(queued: QueuedTask, status: TaskStatus, result: TaskResult) -> None:
        if status not in ("done", "failed"):  # defensive: contract is executor settles only
            logger.error(
                "task events: refusing TaskCompleted publish with status=%r for task_id=%s",
                status,
                queued.task_id,
            )
            return
        await event_bus.publish(
            TaskCompleted(
                task_id=queued.task_id,
                kind=queued.spec.kind,
                status=status,
                timestamp_ms=clock(),
                result_text=result.result_text,
                error=result.error,
                turn_id=queued.spec.turn_id,
                request_id=queued.spec.request_id,
                session_id=session_id,
            )
        )

    return _publish


def build_publish_task_cancelled(
    event_bus: EventBus,
    *,
    session_id: str | None = None,
    clock: Callable[[], int] = _default_clock_ms,
) -> PublishCancelled:
    """Publish :class:`TaskCancelled` for a user-cancelled task (Johnny-d6w.17).

    The in-session resolver invokes this *after* the terminal ``agent_tasks``
    row write, only for a **user** cancel (see
    :data:`~johnny.agent.tasks.PublishCancelled`), so the event always
    describes durable, queryable ``cancelled`` state. The single durable writer
    persists it onto the ``agent_workstreams`` envelope; the per-session WS fans
    it to the browser so the Workstreams column flips to ``cancelled`` live.
    """

    async def _publish(queued: QueuedTask, actor: CancelActor) -> None:
        await event_bus.publish(
            TaskCancelled(
                task_id=queued.task_id,
                kind=queued.spec.kind,
                timestamp_ms=clock(),
                actor=actor,
                turn_id=queued.spec.turn_id,
                request_id=queued.spec.request_id,
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

    async def request_worker_cancel(self, task_id: int) -> None:
        """Publish a cancel signal for one worker-owned task (Johnny-d6w.17).

        The voice ``cancel`` verdict path: the coordinator owns no worker row,
        so it asks the worker — over the shared :data:`TASKS_CANCEL_CHANNEL` —
        to cut its in-flight runner and settle the row ``cancelled``. Reuses
        this session's wake client (same ``johnny.tasks.*`` namespace, one
        connection per session). Publish-only and best-effort like the ping;
        the coordinator's :meth:`_safe_request_worker_cancel` contains failures.
        """
        client = await self._connect()
        payload = {"task_id": task_id, "session_id": self._session_id}
        await client.publish(
            TASKS_CANCEL_CHANNEL, json.dumps(payload, separators=(",", ":"))
        )

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
    runs_in_session: RunsInSession | None = None,
) -> tuple[TaskCoordinator, RedisTaskWake | None]:
    """Assemble the production :class:`TaskCoordinator` for one session.

    ``executor`` defaults to the Phase-3 :func:`stub_executor` (every kind
    fails fast with speech-ready text). ``redis_url=None`` simply skips the
    wake ping — the in-process executor needs no nudge, and the durable row +
    ``TaskQueued`` event still flow. Returns ``(coordinator, wake)``; the
    caller owns both teardowns (:meth:`TaskCoordinator.aclose` drains in-flight
    resolvers, :meth:`RedisTaskWake.close` releases the Redis client).

    ``runs_in_session`` defaults to the internal-kind predicate
    (Johnny-trt.24): in every production assembly only the trt.57 internal
    tools execute in-process; all other kinds stay ``queued`` for the worker
    executor pass, which claims exactly the non-internal kinds — the split is
    structural, so a session and the worker can never both run one task.
    Pass an explicit predicate (e.g. ``lambda kind: True``) only in harnesses
    that deliberately run everything in-process.
    """
    if runs_in_session is None:
        from johnny.agent.internal_tools import is_internal_kind

        runs_in_session = is_internal_kind
    wake = RedisTaskWake(redis_url=redis_url, session_id=session_id) if redis_url else None
    coordinator = TaskCoordinator(
        task_sink,
        executor=executor if executor is not None else stub_executor,
        publish_queued=build_publish_task_queued(event_bus, session_id=session_id, clock=clock),
        publish_completed=build_publish_task_completed(
            event_bus, session_id=session_id, clock=clock
        ),
        publish_cancelled=build_publish_task_cancelled(
            event_bus, session_id=session_id, clock=clock
        ),
        # The voice ``cancel`` verdict reaches worker-owned tasks over Redis
        # (Johnny-d6w.17); reuse the wake client. ``None`` (no redis_url, e.g.
        # in-process harnesses) makes worker cancel a logged no-op.
        request_worker_cancel=(
            wake.request_worker_cancel if wake is not None else None
        ),
        wake=wake,
        runs_in_session=runs_in_session,
    )
    return coordinator, wake


# --------------------------------------------------------------------------- #
# Phase-5 speech-queue wiring (Johnny-trt.28)                                   #
# --------------------------------------------------------------------------- #

DELIVERY_TICK_S = 0.15
"""Cadence of the delivery loop's gating check. Small enough that "delivery
<= grace + 2 s once silence holds" (the trt.28 acceptance) has comfortable
margin even with say() startup on top; large enough to be invisible next to
the audio pipeline's own latencies."""

LISTENER_RECONNECT_BACKOFF_S = 2.0
"""How long the task-event listener waits before resubscribing after a
dropped Redis connection (the wake-listener discipline)."""


class TaskEventListener:
    """Per-session push consumer of ``johnny.tasks.<bot_session_id>`` (Johnny-trt.28).

    The Phase-4 worker publishes ``TaskProgress`` (claim) and ``TaskCompleted``
    (settle) on this channel in addition to the UI session channel
    (:meth:`app.services.task_worker.TaskWorker._event_buses`); this listener
    turns those frames into :class:`TaskCoordinator` registry updates and
    hands first-observed settles to the injected ``on_settled`` hook (the
    delivery wiring: RESULT enqueue for ``done``, the trt.53 spoken correction
    for ``failed``).

    Lifecycle, the worker wake-listener's proven shape: subscribe →
    :meth:`TaskCoordinator.attach_remote_listener` (begin() then spawns no
    poll watcher) → :meth:`TaskCoordinator.reconcile_in_flight` (pub/sub has
    no replay, so every (re)subscribe re-reads non-terminal entries from the
    durable row — a settle published while the connection was down is
    recovered here) → ``get_message(timeout=1.0)`` loop. A dropped connection
    logs, **detaches** (new begins fall back to the poll watcher, so the
    trt.53 correction survives a Redis-only outage), backs off, and
    resubscribes; the registry's first-observer-wins settle chokepoint makes
    the watcher/listener overlap harmless. Listener loss therefore degrades
    exactly as documented: turns keep working, results stay visible in the UI
    (the worker's session-channel publish is independent), and nothing here
    ever blocks a turn.

    ``client_factory`` is the test seam (a fake Redis client per connect);
    production builds one from ``redis_url`` per (re)connect attempt.
    """

    def __init__(
        self,
        *,
        redis_url: str,
        session_id: str,
        coordinator: TaskCoordinator,
        on_settled: Callable[[TaskRegistryEntry], Awaitable[None]],
        client_factory: Callable[[], Any] | None = None,
        reconnect_backoff_s: float = LISTENER_RECONNECT_BACKOFF_S,
    ) -> None:
        self._redis_url = redis_url
        self._session_id = session_id
        self._channel = f"{TASKS_CHANNEL_PREFIX}.{session_id}"
        self._coordinator = coordinator
        self._on_settled = on_settled
        self._client_factory = client_factory
        self._reconnect_backoff_s = reconnect_backoff_s
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Spawn the subscribe loop (idempotent)."""
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._listen())

    async def aclose(self) -> None:
        """Stop listening and release the subscription. Idempotent."""
        self._coordinator.detach_remote_listener()
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover - defensive
            logger.exception("task listener: close raised for %s", self._channel)

    def _build_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        from redis.asyncio import Redis

        return Redis.from_url(self._redis_url, decode_responses=False)

    async def _listen(self) -> None:
        while True:
            client: Any | None = None
            try:
                client = self._build_client()
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                await pubsub.subscribe(self._channel)
                self._coordinator.attach_remote_listener()
                logger.info("task listener: subscribed to %s", self._channel)
                # Rebuild the registry from the durable overlay (US-203): a
                # respawned / reconnected coordinator starts empty while
                # agent_workstreams still holds this session's delegated work, so
                # a status query would otherwise speak "nothing in flight" while
                # the column shows it. Off the speech path; first-observer-wins so
                # live in-process entries are never clobbered (idempotent re-seed).
                await self._coordinator.seed_registry_from_overlay()
                # Close the no-replay window: deliver anything that settled
                # while no subscription was live (including before the first).
                for entry in await self._coordinator.reconcile_in_flight():
                    await self._safe_on_settled(entry)
                while True:
                    try:
                        message = await pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=1.0
                        )
                    except TimeoutError:
                        continue
                    if message is not None and message.get("type") == "message":
                        await self._handle_frame(message.get("data"))
            except asyncio.CancelledError:
                raise
            except Exception:
                # The documented trt.28 degradation, loudly: until resubscribe
                # the push path is gone — results reach the UI only; new
                # begins fall back to the poll watcher via the detach.
                self._coordinator.detach_remote_listener()
                logger.exception(
                    "task listener: subscription on %s dropped — results degrade "
                    "to UI-only until resubscribe (retrying in %.0fs)",
                    self._channel,
                    self._reconnect_backoff_s,
                )
                await asyncio.sleep(self._reconnect_backoff_s)
            finally:
                if client is not None:
                    try:
                        await client.aclose()
                    except Exception:  # noqa: BLE001 — teardown is best-effort
                        pass

    async def _handle_frame(self, raw: Any) -> None:
        """Apply one channel frame to the registry. Malformed frames log + skip."""
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("frame is not a JSON object")
            frame_type = str(payload.get("type") or "")
            if frame_type not in ("task_progress", "task_completed"):
                return  # additive channel: unknown frame types are not ours to judge
            task_id = int(payload["task_id"])
            kind = str(payload.get("kind") or "")
            raw_turn = payload.get("turn_id")
            turn_id = int(raw_turn) if raw_turn is not None else None
        except asyncio.CancelledError:  # pragma: no cover - defensive
            raise
        except Exception:
            logger.exception("task listener: malformed frame on %s: %r", self._channel, raw)
            return
        if frame_type == "task_progress":
            self._coordinator.note_task_running(task_id, kind=kind, turn_id=turn_id)
            return
        status = str(payload.get("status") or "")
        if status not in ("done", "failed"):
            logger.warning(
                "task listener: task_completed frame with status=%r for task_id=%s — ignoring",
                status,
                task_id,
            )
            return
        entry = self._coordinator.note_task_settled(
            task_id,
            status=status,  # type: ignore[arg-type]  # narrowed by the check above
            kind=kind,
            result_text=str(payload.get("result_text") or ""),
            error=str(payload.get("error") or ""),
            turn_id=turn_id,
        )
        if entry is not None:
            await self._safe_on_settled(entry)

    async def _safe_on_settled(self, entry: TaskRegistryEntry) -> None:
        try:
            await self._on_settled(entry)
        except Exception:
            logger.exception(
                "task listener: on_settled hook failed for task_id=%s (%s)",
                entry.task_id,
                entry.kind,
            )


class TaskSpeechDeliverer:
    """The Phase-5 delivery loop: speak queued results only at turn boundaries.

    Owns one :class:`~johnny.agent.speech_queue.SpeechQueue` feed (the pure
    core stays clock-free; this loop injects every ``now``) and the gating
    predicate the trt.28 plan pins — an item leaves the queue only when ALL of:

    * ``session.current_speech`` is ``None``/done — nothing audibly playing
      (the Phase-0 SDK findings' authoritative "is the bot talking" check);
    * the user is not speaking — tracked from ``user_state_changed``
      (verified roomless on the pinned SDK, :mod:`johnny.agent.sdk_surface_smoke`);
    * the gate is idle (:attr:`RouterGate.idle`) — no turn anywhere between
      gate entry and terminal, so a mid-decision turn or a parked approval
      blocks delivery even while inaudible;
    * silence has held for the queue's ~1.2 s grace, with speech-onset reset
      (the queue's internal state machine, fed by the edges sampled here).

    Delivery is :meth:`RouterGate.speak_task_result` — ``session.say()`` with
    the pre-composed text, no LLM hop, no ``bind_reply`` interaction — and the
    loop reports its own delivery as a speech onset, so back-to-back results
    space out at conversational rhythm. An interrupted delivery re-queues once
    at its original priority seat and drops on the second interruption (the
    queue's budget); every RESULT drop except teardown publishes
    :class:`~johnny.voice_pipeline.events.TaskResultExpired` so the UI shows
    why nothing was spoken. Every tick failure is contained: delivery can
    degrade, turns never block.
    """

    def __init__(
        self,
        *,
        session: AgentSession[Any],
        gate: RouterGate,
        queue: SpeechQueue,
        coordinator: TaskCoordinator,
        event_bus: EventBus,
        session_id: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        clock_ms: Callable[[], int] = _default_clock_ms,
        tick_s: float = DELIVERY_TICK_S,
        floor: SpeechFloor | None = None,
    ) -> None:
        self._session = session
        self._gate = gate
        self._queue = queue
        self._coordinator = coordinator
        self._event_bus = event_bus
        self._session_id = session_id
        self._clock = clock
        self._clock_ms = clock_ms
        self._tick_s = tick_s
        # The meeting's shared speech floor (Johnny-trt.46), ``None`` outside
        # multi-agent meetings. The predicate blocks delivery while a peer
        # holds it; _deliver acquires its own lease around the playout so a
        # queued result can never overlap a co-agent's speech.
        self._floor = floor
        self._user_speaking = False
        self._was_speaking = False
        self._loop_task: asyncio.Task[None] | None = None
        # Strong refs to in-flight event publishes scheduled from sync queue
        # callbacks (the _reply_tasks discipline).
        self._publish_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Register the user-state listener and spawn the tick loop (idempotent)."""
        self._session.on("user_state_changed", self._on_user_state)
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.ensure_future(self._run_loop())

    async def aclose(self) -> None:
        """Stop the loop + listener and drain scheduled publishes. Idempotent.

        Does NOT close the queue — :meth:`TaskSpeechWiring.aclose` owns that
        ordering (loop first, so a teardown-raced pop can't speak; then the
        queue's close settles every undelivered item exactly once).
        """
        task = self._loop_task
        self._loop_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - defensive
                logger.exception("task speech: delivery loop close raised")
        try:
            self._session.off("user_state_changed", self._on_user_state)
        except Exception:  # noqa: BLE001 — a torn-down session may refuse; harmless
            logger.debug("task speech: user_state_changed off() failed", exc_info=True)
        if self._publish_tasks:
            await asyncio.gather(*list(self._publish_tasks), return_exceptions=True)

    # ------------------------------------------------------------------ #
    # Producer side (called by the listener's on_settled hook)            #
    # ------------------------------------------------------------------ #

    def enqueue_result(self, entry: TaskRegistryEntry) -> SpeechItem | None:
        """Queue one ``done`` task's ``result_text`` for spoken delivery.

        Blank ``result_text`` stays UI-only (logged — there is nothing
        speakable; the registry keeps ``delivered=False`` so the trt.29
        status path still knows the result was never voiced). The item's
        ``on_spoken`` marks the registry delivered — including when trt.29
        consumes the queued copy into a direct answer via
        :meth:`SpeechQueue.mark_spoken` — and ``on_dropped`` publishes
        :class:`TaskResultExpired` for every reason except queue teardown
        (the session is going away; the UI row already delivered, trt.25
        contract).
        """
        text = entry.result_text.strip()
        if not text:
            logger.info(
                "task speech: task #%s (%s) settled done with no speech-ready "
                "result_text — UI-only",
                entry.task_id,
                entry.kind,
            )
            return None
        task_id, kind, turn_id = entry.task_id, entry.kind, entry.turn_id

        def _on_spoken(item: SpeechItem) -> None:
            self._coordinator.mark_result_delivered(task_id)
            # Durable delivery (Johnny-d6w.2): the single durable writer stamps
            # agent_workstreams.delivery_status=delivered from this — the durable
            # replacement for the in-memory TaskRegistryEntry.delivered flip above.
            self._publish_delivery_changed(
                task_id=task_id, kind=kind, turn_id=turn_id, delivery_status="delivered"
            )
            logger.info(
                "task speech: result for task #%s (%s) delivered after %.1fs queued",
                task_id,
                kind,
                self._clock() - item.enqueued_at,
            )

        def _on_dropped(item: SpeechItem, reason: str) -> None:
            del item
            if reason == DROP_QUEUE_CLOSED:
                return
            self._publish_result_expired(
                task_id=task_id, kind=kind, turn_id=turn_id, reason=reason
            )

        return self._queue.enqueue(
            text,
            SpeechPriority.RESULT_UNSOLICITED,
            now=self._clock(),
            on_spoken=_on_spoken,
            on_dropped=_on_dropped,
            task_id=task_id,
            kind=kind,
        )

    # ------------------------------------------------------------------ #
    # The gating predicate + tick loop                                    #
    # ------------------------------------------------------------------ #

    def delivery_blocked_reason(self) -> str | None:
        """Why delivery must wait *right now*, or ``None`` when the floor is open.

        The instantaneous half of the trt.28 predicate (the time half — the
        silence grace — lives in :meth:`SpeechQueue.pop_ready`). Exposed for
        the unit matrix; the loop consults it every tick.
        """
        if self._user_speaking:
            return "user speaking"
        speech = getattr(self._session, "current_speech", None)
        if speech is not None and not speech.done():
            return "bot speaking"
        if not self._gate.idle:
            return "gate busy"
        if self._floor is not None and self._floor.peer_holds_floor():
            # Johnny-trt.46: a co-agent is speaking (its floor lease is
            # live) — a queued result waits exactly like it waits for a
            # human participant.
            return "peer agent holds the floor"
        return None

    def _on_user_state(self, ev: Any) -> None:
        """``user_state_changed`` listener: track the user half of the floor.

        ``speaking`` blocks delivery; ``listening`` / ``away`` clear it. Sync
        and trivially cheap (the SDK dispatches listeners inline); the edges
        feed the queue's grace machine from the next tick.
        """
        self._user_speaking = str(getattr(ev, "new_state", "")) == "speaking"

    def _tick_edges(self, now: float) -> None:
        """Feed sampled speech edges into the queue's grace state machine.

        Someone = the user (event-tracked) or the bot (``current_speech``
        sampled). Rising edge → onset (anchor reset); falling edge → silence
        onset (grace starts). Sampling quantizes edges to the tick, which is
        noise next to the 1.2 s grace.
        """
        speech = getattr(self._session, "current_speech", None)
        bot_speaking = speech is not None and not speech.done()
        speaking = self._user_speaking or bot_speaking
        if speaking and not self._was_speaking:
            self._queue.note_speech_onset()
        elif not speaking and self._was_speaking:
            self._queue.note_silence_onset(now)
        self._was_speaking = speaking

    async def _run_loop(self) -> None:
        while True:
            await asyncio.sleep(self._tick_s)
            try:
                now = self._clock()
                self._tick_edges(now)
                # Sweep even while gated so an expiring RESULT fires its
                # TaskResultExpired promptly mid-monologue, not minutes later.
                self._queue.sweep_expired(now)
                if self.delivery_blocked_reason() is not None:
                    continue
                item = self._queue.pop_ready(now)
                if item is None:
                    continue
                await self._deliver(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("task speech: delivery tick failed — continuing")

    async def _deliver(self, item: SpeechItem) -> None:
        """Speak one popped item and settle it by playout outcome.

        With a speech floor attached (Johnny-trt.46) the lease wraps the
        whole playout: acquired with a short wait (the predicate already saw
        the floor open this tick — a miss means a peer raced us, so the item
        is :meth:`SpeechQueue.restore`-d unblamed for the next tick) and
        released after the handle settles, carrying the delivered text as
        the peers' text-match backstop feed.
        """
        floor_lease: FloorLease | None = None
        if self._floor is not None:
            floor_lease = await self._floor.acquire(
                "task_result", timeout_s=FLOOR_DELIVERY_WAIT_S
            )
            if floor_lease is None:
                self._queue.restore(item, self._clock())
                return
        handle = self._gate.speak_task_result(item.text)
        if handle is None:
            # say() unavailable (not attached / raised): the gate already
            # logged it; degrade this item to UI-only rather than retrying
            # into the same wall.
            if floor_lease is not None:
                await self._release_floor_lease(
                    floor_lease, reason=RELEASE_SAY_UNAVAILABLE, spoken_text=""
                )
            self._queue.drop(item, reason="say() unavailable")
            return
        # The bot's own delivery is a speech onset (speech_queue module doc) —
        # reported explicitly because the tick is paused inside this await, so
        # sampling alone would leave the silence anchor stale and chain the
        # next item with no conversational gap.
        self._queue.note_speech_onset()
        self._was_speaking = True
        try:
            await handle
        finally:
            self._queue.note_silence_onset(self._clock())
            self._was_speaking = False
        interrupted = bool(getattr(handle, "interrupted", False))
        if floor_lease is not None:
            await self._release_floor_lease(
                floor_lease,
                reason=RELEASE_INTERRUPTED if interrupted else RELEASE_COMPLETED,
                spoken_text=item.text,
            )
        if interrupted:
            # Requeue-once-then-drop is the queue's budget (trt.28 acceptance);
            # the drop fires on_dropped → TaskResultExpired("interrupted twice").
            # Stamp the durable delivery_status=interrupted (Johnny-d6w.2); a
            # later redelivery moves it to delivered, a second cut to expired.
            self._publish_delivery_changed(
                task_id=item.task_id,
                kind=item.kind,
                turn_id=None,
                delivery_status="interrupted",
            )
            self._queue.mark_interrupted(item, self._clock())
        else:
            self._queue.mark_spoken(item, self._clock())

    async def _release_floor_lease(
        self, lease: FloorLease, *, reason: str, spoken_text: str
    ) -> None:
        """Release the delivery lease defensively — the loop must never die on it."""
        try:
            await lease.release(reason=reason, spoken_text=spoken_text)
        except Exception:
            logger.exception(
                "task speech: floor release (%s) failed — the TTL will free it",
                reason,
            )

    # ------------------------------------------------------------------ #
    # TaskResultExpired publishing (sync-callback → scheduled task)       #
    # ------------------------------------------------------------------ #

    def _publish_result_expired(
        self, *, task_id: int | None, kind: str, turn_id: int | None, reason: str
    ) -> None:
        event = TaskResultExpired(
            task_id=task_id if task_id is not None else 0,
            kind=kind,
            timestamp_ms=self._clock_ms(),
            reason=reason,
            turn_id=turn_id,
            session_id=self._session_id,
        )
        task = asyncio.ensure_future(self._publish_event(event))
        self._publish_tasks.add(task)
        task.add_done_callback(self._publish_tasks.discard)

    def _publish_delivery_changed(
        self,
        *,
        task_id: int | None,
        kind: str,
        turn_id: int | None,
        delivery_status: WorkstreamDeliveredStatus,
    ) -> None:
        """Announce a workstream delivery transition (Johnny-d6w.2, US-002).

        Scheduled from the same sync callbacks as the expiry publish; carries
        the originating ``task_id`` (absent on the ``AgentSpoke`` the delivery
        produces) so the single durable writer can stamp the durable
        ``agent_workstreams.delivery_status``. Best-effort like every other
        event here — the row write, not this fan-out, is the record.
        """
        event = WorkstreamDeliveryChanged(
            task_id=task_id if task_id is not None else 0,
            kind=kind,
            delivery_status=delivery_status,
            timestamp_ms=self._clock_ms(),
            turn_id=turn_id,
            session_id=self._session_id,
        )
        task = asyncio.ensure_future(self._publish_event(event))
        self._publish_tasks.add(task)
        task.add_done_callback(self._publish_tasks.discard)

    async def _publish_event(
        self, event: TaskResultExpired | WorkstreamDeliveryChanged
    ) -> None:
        try:
            await self._event_bus.publish(event)
        except Exception:  # noqa: BLE001 — events are best-effort; the row is the record
            logger.warning(
                "task speech: %s publish failed for task_id=%s",
                event.type,
                event.task_id,
            )


@dataclass(slots=True)
class TaskSpeechWiring:
    """The assembled Phase-5 speech-delivery stack for one session (Johnny-trt.28).

    Carried on :attr:`AgentRuntime.task_speech`; :meth:`AgentRuntime.aclose`
    calls :meth:`aclose` before the coordinator drain so a teardown-raced
    delivery can never speak into a closing session.
    """

    queue: SpeechQueue
    deliverer: TaskSpeechDeliverer
    listener: TaskEventListener | None

    async def aclose(self) -> None:
        """Teardown in dependency order: listener → loop → queue. Idempotent.

        The queue closes last so the stopped loop's in-flight item (if any)
        and every still-queued item settle exactly once — their queue-closed
        drops deliberately publish no ``TaskResultExpired`` (the session is
        going away with its bus; the UI rows already delivered).
        """
        if self.listener is not None:
            try:
                await self.listener.aclose()
            except Exception:  # pragma: no cover - defensive
                logger.exception("task speech wiring: listener close failed")
        try:
            await self.deliverer.aclose()
        except Exception:  # pragma: no cover - defensive
            logger.exception("task speech wiring: deliverer close failed")
        self.queue.close()


def attach_task_speech_wiring(
    runtime: AgentRuntime,
    session: AgentSession[Any],
    *,
    grace_s: float = DEFAULT_SILENCE_GRACE_S,
    tick_s: float = DELIVERY_TICK_S,
    clock: Callable[[], float] = time.monotonic,
    clock_ms: Callable[[], int] = _default_clock_ms,
    listener_client_factory: Callable[[], Any] | None = None,
) -> TaskSpeechWiring | None:
    """Build + start the Phase-5 speech wiring for one live session (Johnny-trt.28).

    The single factory both session surfaces call right after
    ``session.start`` (the agent worker for Meet rooms, the browser session
    for the roomless playground — the ``build_approval_coordinator``
    placement, for the same reason: ``say()`` / ``current_speech`` /
    ``user_state_changed`` need the live ``AgentSession``). Wires the
    :class:`SpeechQueue` (pure core, trt.27), the :class:`TaskSpeechDeliverer`
    (gated delivery loop), and — only with a ``redis_url`` — the
    :class:`TaskEventListener` whose first-observed settles drive the trt.53
    correction (``failed``) and the RESULT enqueue (``done``). Without Redis
    the listener is skipped loudly: worker-owned settles keep the Phase-4
    poll-watcher correction and results stay UI-only.

    Returns ``None`` for runtimes without a task coordinator (non-delegating
    modes — nothing will ever be queued). Otherwise stores the wiring on
    ``runtime.task_speech`` (torn down by :meth:`AgentRuntime.aclose`) and
    returns it.

    ``runtime`` is read via ``getattr`` (the
    :func:`~johnny.agent.browser_session.resolve_browser_turn_detector`
    duck-typing discipline) so harness/test runtimes that model only the
    fields they exercise resolve to the no-task-pieces path instead of
    crashing — a real :class:`AgentRuntime` always carries the attribute.
    """
    coordinator = getattr(runtime, "task_coordinator", None)
    if coordinator is None:
        return None
    queue = SpeechQueue(clock(), grace_s=grace_s)
    deliverer = TaskSpeechDeliverer(
        session=session,
        gate=runtime.gate,
        queue=queue,
        coordinator=coordinator,
        event_bus=runtime.event_bus,
        session_id=runtime.session_id,
        clock=clock,
        clock_ms=clock_ms,
        tick_s=tick_s,
        # The meeting's shared speech floor (Johnny-trt.46); getattr per the
        # duck-typing discipline above — None on single-agent runtimes and
        # harness fakes, which leaves delivery ungated.
        floor=getattr(runtime, "speech_floor", None),
    )
    # The trt.29 consumption seam: the gate's status path reads this queue to
    # consume a RESULT copy whose text it just spoke inside a status reply.
    # getattr per the duck-typing discipline above — a real RouterGate always
    # has it; harness fakes that model only idle/speak_task_result skip it.
    attach_queue = getattr(runtime.gate, "attach_speech_queue", None)
    if attach_queue is not None:
        attach_queue(queue, clock=clock)

    # In-session ``done`` talk-back (Johnny-d6w.24): a mid-loop-promoted
    # continuation runs as an in-session resolver, whose ``done`` settle has no
    # push-listener ``on_settled`` to enqueue its result. Attach the coordinator's
    # delivery seam, scoped to the continuation kind so internal in-session kinds
    # (meeting.leave / session.end) stay silent.
    from johnny.agent.inline_promotion import INLINE_CONTINUATION_KIND

    async def _on_in_session_done(entry: TaskRegistryEntry) -> None:
        if entry.kind == INLINE_CONTINUATION_KIND:
            deliverer.enqueue_result(entry)

    attach_result_deliverer = getattr(coordinator, "attach_result_deliverer", None)
    if attach_result_deliverer is not None:
        attach_result_deliverer(_on_in_session_done)

    async def _on_settled(entry: TaskRegistryEntry) -> None:
        # The exactly-once side effects of a first-observed remote settle:
        # an honest spoken walk-back for failed (Johnny-trt.53 — same seam,
        # different observer), the result queue for done. Other terminals
        # (cancelled/expired via reconcile) update the registry only.
        if entry.status == "failed":
            await coordinator.report_remote_failure(entry)
        elif entry.status == "done":
            deliverer.enqueue_result(entry)

    listener: TaskEventListener | None = None
    redis_url = runtime.config.redis_url
    if redis_url:
        listener = TaskEventListener(
            redis_url=redis_url,
            session_id=runtime.session_id,
            coordinator=coordinator,
            on_settled=_on_settled,
            client_factory=listener_client_factory,
        )
        listener.start()
    else:
        logger.warning(
            "task speech: no redis_url for session %s — task-event listener "
            "disabled; worker-owned settles fall back to the poll watcher and "
            "results stay UI-only",
            runtime.session_id,
        )
    deliverer.start()
    wiring = TaskSpeechWiring(queue=queue, deliverer=deliverer, listener=listener)
    runtime.task_speech = wiring
    logger.info(
        "task speech wiring attached for session %s (listener=%s, grace=%.2fs)",
        runtime.session_id,
        "on" if listener is not None else "off",
        grace_s,
    )
    return wiring


__all__ = [
    "DELIVERY_TICK_S",
    "LISTENER_RECONNECT_BACKOFF_S",
    "TASKS_CANCEL_CHANNEL",
    "TASKS_CHANNEL_PREFIX",
    "TASKS_WAKE_CHANNEL",
    "RedisTaskWake",
    "TaskEventListener",
    "TaskSpeechDeliverer",
    "TaskSpeechWiring",
    "attach_task_speech_wiring",
    "build_publish_task_cancelled",
    "build_publish_task_completed",
    "build_publish_task_queued",
    "build_task_coordinator",
    "publish_task_completed_frames",
]
