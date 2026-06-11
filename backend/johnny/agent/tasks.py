"""Async-task coordination for ``delegate`` router verdicts (Johnny-trt.18, Phase 3).

A Phase-3 ``delegate`` verdict (Johnny-trt.16) means the bot speaks a short ack
("let me check on that") and runs the actual work *off* the turn loop. The ack
is a promise, and this module is what keeps it honest:

1. **In the gate** (Johnny-trt.17), the delegate branch ``await``\\s
   :meth:`TaskCoordinator.begin` and only speaks the ack when it returns a
   :class:`QueuedTask` — so the durable ``agent_tasks`` row exists **before**
   any ack audio plays (the status query and the live UI correlate on it). A
   persist failure returns ``None`` and the gate terminalizes
   ``no_reply(stage_error)`` instead of promising work nothing recorded.
2. **Out of band**, ``begin`` announces the task (a
   :class:`~johnny.voice_pipeline.events.TaskQueued` event on the session
   channel plus a wake ping on the shared ``johnny.tasks.wake`` channel for
   future external workers) and spawns a resolver task that drives the row's
   lifecycle: ``queued`` → ``running`` → ``done`` / ``failed`` (``cancelled``
   on session teardown). Announcement failures are logged, never raised — a
   flaky bus must not break a turn whose row already exists.
3. **Execution** is an injected :data:`TaskExecutor`. Phase 3 ships only
   :func:`stub_executor`, which fails every kind *fast* with a speech-ready
   error stored on the row — an ack must never be a dead promise, so until
   real executors land (Phase 4) the bot can immediately report "that didn't
   work" instead of leaving tasks queued forever.

Like :mod:`johnny.agent.gate` and :mod:`johnny.agent.approval`, this module is
deliberately ``livekit``-free, ``sqlalchemy``-free and ``redis``-free (stdlib
only): persistence, event publishing, the wake ping, and execution are all
injected, so ``import johnny.agent.tasks`` stays cheap and the unit tests run
without the ``agent`` extra. :mod:`johnny.agent.task_wiring` supplies the real
seams (the SQLAlchemy sink from ``app.services.agent_tasks``, the EventBus
publisher, the Redis wake publisher).
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

logger = logging.getLogger(__name__)

TaskStatus = Literal["queued", "running", "done", "failed", "cancelled", "expired"]
"""Lifecycle states of one delegated task. Mirror of
:class:`app.db.models.AgentTaskStatus` (kept stdlib-only here; a drift-guard
test asserts equality). ``expired`` is reserved for a future staleness sweep —
nothing in this module emits it yet."""

TERMINAL_TASK_STATUSES: frozenset[str] = frozenset({"done", "failed", "cancelled", "expired"})
"""Statuses a task never leaves. Everything else is in flight."""

EXECUTOR_RESULT_STATUSES: frozenset[str] = frozenset({"done", "failed"})
"""The only statuses an executor may settle a task to. ``cancelled`` is the
coordinator's (teardown), ``expired`` the future sweep's."""


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """Everything one delegated task needs at queue time — the unit
    :meth:`TaskCoordinator.begin` takes.

    ``kind`` / ``args`` / ``ack_text`` mirror the router's validated
    :class:`~johnny.voice_pipeline.reasoning.TaskRequest` (the gate maps the
    fields over — this module deliberately does not import the reasoning
    side). ``turn_id`` is the delegating turn's durable int id (the same
    value its ``TurnTerminal`` carries); ``decision_id`` is the turn's
    ``agent_decisions`` row id when one was persisted synchronously. Both are
    ``None`` for tasks queued outside a gated turn.
    """

    kind: str
    args: dict[str, Any] = field(default_factory=dict)
    ack_text: str = ""
    turn_id: int | None = None
    decision_id: int | None = None


@dataclass(frozen=True, slots=True)
class QueuedTask:
    """A successfully persisted task — what :meth:`TaskCoordinator.begin` returns.

    ``task_id`` is the durable ``agent_tasks`` row id (the correlation key for
    the status query, the ``TaskQueued`` event, and the wake ping).
    """

    task_id: int
    spec: TaskSpec


@dataclass(frozen=True, slots=True)
class TaskResult:
    """What an executor settled a task to.

    ``result_text`` is **speech-ready** — the phrase a later ``status`` turn
    (or a proactive report, Phase 5) reads out loud, for successes and
    failures alike. ``error`` is the diagnostic detail for the operator /
    logs; never spoken. ``result_json`` carries structured output for
    machine consumers.
    """

    status: Literal["done", "failed"]
    result_text: str = ""
    result_json: dict[str, Any] | None = None
    error: str = ""


class TaskSink(ABC):
    """Durable persistence for delegated tasks (the ``agent_tasks`` table).

    Production wires :class:`app.services.agent_tasks.SqlAlchemyTaskSink`;
    tests use :class:`InMemoryTaskSink`. Mirrors the
    :class:`~johnny.voice_pipeline.decision_sink.DecisionSink` split so the
    coordinator stays SQLAlchemy-free.
    """

    @abstractmethod
    async def record_queued(self, spec: TaskSpec) -> int | None:
        """Insert the ``queued`` row and return its primary key.

        Returns ``None`` when the sink cannot produce a durable id (a noop
        sink); the coordinator treats that the same as a raise — no row, no
        promise, no ack.
        """

    @abstractmethod
    async def update_status(
        self,
        task_id: int,
        status: TaskStatus,
        *,
        result_text: str | None = None,
        result_json: dict[str, Any] | None = None,
        error: str | None = None,
        attempts: int | None = None,
    ) -> None:
        """Move an existing row to ``status``, updating only the fields given.

        ``None`` keyword values mean "leave the column alone", so a
        ``running`` stamp does not blank a prior error and a terminal stamp
        can set text/error/attempts in one write.
        """

    async def close(self) -> None:  # noqa: B027 — intentional default no-op
        """Release any held connections. Default is a no-op."""


@dataclass(slots=True)
class TaskRecord:
    """One in-memory task row (test double of an ``agent_tasks`` row)."""

    task_id: int
    spec: TaskSpec
    status: TaskStatus = "queued"
    result_text: str | None = None
    result_json: dict[str, Any] | None = None
    error: str | None = None
    attempts: int = 0


class InMemoryTaskSink(TaskSink):
    """Append/update task records in a list. Intended for tests and dry runs."""

    def __init__(self) -> None:
        self._records: list[TaskRecord] = []
        self._lock = asyncio.Lock()
        self._next_id = 1

    async def record_queued(self, spec: TaskSpec) -> int | None:
        async with self._lock:
            task_id = self._next_id
            self._next_id += 1
            self._records.append(TaskRecord(task_id=task_id, spec=spec))
        return task_id

    async def update_status(
        self,
        task_id: int,
        status: TaskStatus,
        *,
        result_text: str | None = None,
        result_json: dict[str, Any] | None = None,
        error: str | None = None,
        attempts: int | None = None,
    ) -> None:
        async with self._lock:
            for record in self._records:
                if record.task_id != task_id:
                    continue
                record.status = status
                if result_text is not None:
                    record.result_text = result_text
                if result_json is not None:
                    record.result_json = dict(result_json)
                if error is not None:
                    record.error = error
                if attempts is not None:
                    record.attempts = attempts
                return

    def snapshot(self) -> list[TaskRecord]:
        """Non-async snapshot for synchronous test assertions."""
        return [replace(record) for record in self._records]

    def get(self, task_id: int) -> TaskRecord | None:
        """Look one record up by id (non-async test helper)."""
        for record in self._records:
            if record.task_id == task_id:
                return replace(record)
        return None


# Injected dependencies (stdlib-only seams; johnny.agent.task_wiring supplies
# the real ones).
TaskExecutor = Callable[[QueuedTask], Awaitable[TaskResult]]
"""Run one task to completion and report how it settled. Phase 3 ships only
:func:`stub_executor`; Phase 4 plugs real skill/tool executors in here."""

PublishQueued = Callable[[QueuedTask], Awaitable[None]]
"""Publish ``TaskQueued`` on the session EventBus channel (live-UI surface)."""

WakePing = Callable[[QueuedTask], Awaitable[None]]
"""Nudge the shared ``johnny.tasks.wake`` channel so an external worker
(Phase 4) picks queued work up without polling."""

ReportTaskFailed = Callable[[QueuedTask, TaskResult], Awaitable[None]]
"""Tell the session a task settled ``failed`` — the no-dead-promises seam
(Johnny-trt.53). Called by the resolver *after* the terminal row update, off
the turn loop, so the consumer (the gate's honest spoken correction via
``say()``) only ever reports durable state. Best-effort and contained like
the announce seams; never invoked for ``done`` (Phase-5 re-entry territory)
or ``cancelled`` (the session is tearing down — nobody is listening)."""


def unsupported_kind_text(kind: str) -> str:
    """Speech-ready failure phrase for a task kind nothing can run yet.

    Composes under the gate's correction prefix (Johnny-trt.53:
    ``"Actually — I can't do that yet: <this>"``), so it carries no
    "I couldn't do that" framing of its own.
    """
    return f"I don't know how to run {kind} tasks yet."


def executor_error_text(kind: str) -> str:
    """Speech-ready failure phrase for an executor that crashed."""
    return f"Something went wrong while I was working on that {kind} task."


async def stub_executor(task: QueuedTask) -> TaskResult:
    """Fail every kind fast — Phase 3 has no real executors yet (Johnny-trt.18).

    The ack must never be a dead promise: until Phase 4 lands real executors,
    every delegated task settles ``failed`` immediately with a speech-ready
    error stored, so a ``status`` ask gets "that didn't work" instead of an
    eternally pending row.
    """
    kind = task.spec.kind
    return TaskResult(
        status="failed",
        result_text=unsupported_kind_text(kind),
        error=f"unsupported task kind: {kind!r} (no executor registered)",
    )


class TaskCoordinator:
    """Drives delegated tasks out of band so the gate's ack is never a lie.

    Construct one per session with the injected sink / executor / announce
    seams. The gate's delegate branch (Johnny-trt.17) ``await``\\s
    :meth:`begin` — the queued row is durable when it returns — and speaks the
    ack only on success; the spawned resolver task carries the row to its
    terminal status off the turn loop (the
    :class:`~johnny.agent.approval.ApprovalCoordinator` discipline: strong
    task refs, :meth:`aclose` drain, every failure contained).
    """

    def __init__(
        self,
        sink: TaskSink,
        *,
        executor: TaskExecutor,
        publish_queued: PublishQueued | None = None,
        wake: WakePing | None = None,
        report_failed: ReportTaskFailed | None = None,
    ) -> None:
        self._sink = sink
        self._executor = executor
        self._publish_queued = publish_queued
        self._wake = wake
        # The no-dead-promises seam (Johnny-trt.53). Usually attached after
        # construction via :meth:`attach_failure_reporter` (the gate is built
        # *after* the coordinator in the runtime assembly, the attach_say
        # ordering pattern); the constructor arg serves directly-wired tests.
        self._report_failed = report_failed
        # Strong refs to in-flight resolver tasks so they aren't GC'd mid-run
        # (and to avoid "task exception never retrieved" warnings); also lets
        # aclose() drain them at teardown.
        self._tasks: set[asyncio.Task[None]] = set()

    def attach_failure_reporter(self, report: ReportTaskFailed) -> None:
        """Attach the failed-task report seam after construction (Johnny-trt.53).

        Called by :class:`~johnny.agent.router_gate.RouterGate` when it is
        constructed with this coordinator (the gate owns ``say()``, so it owns
        the spoken correction) — the coordinator exists first in the runtime
        assembly, so the seam cannot be a constructor argument there. Until
        attached, failed settles are recorded but reported nowhere.
        """
        self._report_failed = report

    # ------------------------------------------------------------------ #
    # The entry point (called from the gate's delegate branch)            #
    # ------------------------------------------------------------------ #

    async def begin(self, spec: TaskSpec) -> QueuedTask | None:
        """Persist the ``queued`` row, announce it, and spawn the resolver.

        The one ordering guarantee callers rely on: the row **exists before
        this returns** (and therefore before any ack is spoken — the gate
        awaits ``begin`` first). Returns ``None`` when persistence failed or
        produced no id, in which case nothing was announced, nothing runs,
        and the caller must not speak an ack (terminalize
        ``no_reply(stage_error)`` instead).

        The ``TaskQueued`` publish and the wake ping are best-effort: the
        durable row is the contract, a flaky bus only costs liveness.
        """
        try:
            task_id = await self._sink.record_queued(spec)
        except Exception:
            logger.exception(
                "tasks.begin: failed to persist queued row for kind=%s — not starting",
                spec.kind,
            )
            return None
        if task_id is None:
            logger.error(
                "tasks.begin: sink returned no row id for kind=%s — not starting",
                spec.kind,
            )
            return None

        queued = QueuedTask(task_id=task_id, spec=spec)
        await self._safe_publish_queued(queued)
        await self._safe_wake(queued)

        runner = asyncio.ensure_future(self._run(queued))
        self._tasks.add(runner)
        runner.add_done_callback(self._tasks.discard)
        return queued

    async def join(self) -> None:
        """Await every in-flight resolver without cancelling (tests / drain)."""
        tasks = [task for task in self._tasks if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def aclose(self) -> None:
        """Cancel and drain any in-flight resolvers (best-effort teardown).

        A cancelled resolver marks its row ``cancelled`` on the way out (see
        :meth:`_run`), so a session teardown never strands tasks in
        ``running``. Safe to call more than once.
        """
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - defensive
                logger.exception("task resolver failed during aclose")

    # ------------------------------------------------------------------ #
    # The out-of-band resolver                                            #
    # ------------------------------------------------------------------ #

    async def _run(self, queued: QueuedTask) -> None:
        """Carry one queued task to its terminal status, off the turn loop.

        Every ``failed`` settle — executor exception, illegal executor status,
        or an honest ``failed`` result — is reported through the attached
        :data:`ReportTaskFailed` seam *after* the row update (Johnny-trt.53:
        the spoken correction only ever describes durable state, the
        row-before-ack discipline applied to the walk-back). ``cancelled``
        (session teardown) and ``done`` (Phase-5 re-entry) report nothing.
        """
        await self._safe_update(queued.task_id, "running", attempts=1)
        try:
            result = await self._executor(queued)
        except asyncio.CancelledError:
            # Session teardown (aclose). Settle the row so it is not left
            # dangling in running, then never swallow the cancellation.
            await self._safe_update(
                queued.task_id,
                "cancelled",
                result_text=f"The {queued.spec.kind} task was cancelled when the session ended.",
                error="cancelled before completion (session teardown)",
            )
            raise
        except Exception as exc:
            logger.exception(
                "tasks.run: executor errored for task_id=%s kind=%s",
                queued.task_id,
                queued.spec.kind,
            )
            result = TaskResult(
                status="failed",
                result_text=executor_error_text(queued.spec.kind),
                error=f"executor error: {type(exc).__name__}: {exc}",
            )
            await self._safe_update(
                queued.task_id,
                "failed",
                result_text=result.result_text,
                error=result.error,
            )
            await self._safe_report_failed(queued, result)
            return

        status: TaskStatus
        if result.status in EXECUTOR_RESULT_STATUSES:
            status = result.status
        else:  # defensive: an executor may only settle done/failed
            logger.error(
                "tasks.run: executor returned illegal status %r for task_id=%s — recording failed",
                result.status,
                queued.task_id,
            )
            status = "failed"
        await self._safe_update(
            queued.task_id,
            status,
            result_text=result.result_text or None,
            result_json=result.result_json,
            error=result.error or None,
        )
        if status == "failed":
            await self._safe_report_failed(queued, result)

    # ------------------------------------------------------------------ #
    # Contained I/O (a failing seam never crashes the round)              #
    # ------------------------------------------------------------------ #

    async def _safe_update(self, task_id: int, status: TaskStatus, **fields: Any) -> None:
        try:
            await self._sink.update_status(task_id, status, **fields)
        except Exception:
            logger.exception("tasks.run: failed to update task_id=%s to status=%s", task_id, status)

    async def _safe_publish_queued(self, queued: QueuedTask) -> None:
        if self._publish_queued is None:
            return
        try:
            await self._publish_queued(queued)
        except Exception:
            logger.exception(
                "tasks.begin: TaskQueued publish failed for task_id=%s", queued.task_id
            )

    async def _safe_wake(self, queued: QueuedTask) -> None:
        if self._wake is None:
            return
        try:
            await self._wake(queued)
        except Exception:
            logger.exception("tasks.begin: wake ping failed for task_id=%s", queued.task_id)

    async def _safe_report_failed(self, queued: QueuedTask, result: TaskResult) -> None:
        if self._report_failed is None:
            return
        try:
            await self._report_failed(queued, result)
        except Exception:
            logger.exception(
                "tasks.run: failure report raised for task_id=%s kind=%s",
                queued.task_id,
                queued.spec.kind,
            )


__all__ = [
    "EXECUTOR_RESULT_STATUSES",
    "TERMINAL_TASK_STATUSES",
    "InMemoryTaskSink",
    "PublishQueued",
    "QueuedTask",
    "ReportTaskFailed",
    "TaskCoordinator",
    "TaskExecutor",
    "TaskRecord",
    "TaskResult",
    "TaskSink",
    "TaskSpec",
    "TaskStatus",
    "WakePing",
    "executor_error_text",
    "stub_executor",
    "unsupported_kind_text",
]
