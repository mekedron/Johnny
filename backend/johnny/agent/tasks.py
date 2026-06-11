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
   on session teardown). A ``done``/``failed`` settle is announced the same
   way (a :class:`~johnny.voice_pipeline.events.TaskCompleted` after the
   terminal row write, Johnny-trt.25). Announcement failures are logged,
   never raised — a flaky bus must not break a turn whose row already
   exists.
3. **Execution** is an injected :data:`TaskExecutor` — but only for kinds the
   session itself runs. Since Johnny-trt.24 ownership is split by locality
   through the injected :data:`RunsInSession` predicate: kinds it accepts
   (the trt.57 internal tools in production) get the in-process resolver and
   executor exactly as in Phase 3; every other kind is **worker-owned** — the
   row stays ``queued`` for the Phase-4 worker executor pass
   (:mod:`app.services.task_worker`, woken by the ping from step 2) and the
   coordinator spawns a read-only *watcher* instead of a resolver. The
   watcher polls the row until it settles so a ``failed`` settle still fires
   the Johnny-trt.53 no-dead-promises correction (the gate's spoken
   walk-back) even though another process owns the execution; it never
   writes the row. The Phase-5 task-event listener (Johnny-trt.28) replaces
   the watcher with a push subscription on ``johnny.tasks.<session>``.

Like :mod:`johnny.agent.gate` and :mod:`johnny.agent.approval`, this module is
deliberately ``livekit``-free, ``sqlalchemy``-free and ``redis``-free (stdlib
only): persistence, event publishing, the wake ping, locality, and execution
are all injected, so ``import johnny.agent.tasks`` stays cheap and the unit
tests run without the ``agent`` extra. :mod:`johnny.agent.task_wiring`
supplies the real seams (the SQLAlchemy sink from ``app.services.agent_tasks``,
the EventBus publisher, the Redis wake publisher, the internal-kind locality
predicate).
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

DEFAULT_ACLOSE_DRAIN_GRACE_S = 10.0
"""How long :meth:`TaskCoordinator.aclose` lets in-flight resolvers finish
before cancelling them (Johnny-trt.57). Exists for self-terminating internal
tasks: ``meeting.leave`` / ``session.end`` trigger the very teardown that
calls ``aclose`` while their resolver is still awaiting the control call's
response + the terminal row write — an immediate cancel would record a
misleading ``cancelled`` row for an action that *succeeded*. The grace also
lets any nearly-done in-session task settle honestly instead of being cut at
the finish line; a genuinely hung executor is still cancelled when it
expires. Watchers for worker-owned tasks (Johnny-trt.24) are *not* drained —
they hold no row to settle, so teardown cancels them immediately."""

WATCH_POLL_INTERVAL_S = 1.0
"""How often the worker-owned-task watcher re-reads the row (Johnny-trt.24).
Background tasks are seconds-long; a 1 s cadence keeps the trt.53 spoken
correction conversational without measurable DB load."""

WATCH_TIMEOUT_S = 900.0
"""When the watcher gives up (Johnny-trt.24). Generous on purpose: a
worker-owned task may sit out crash-requeue TTL cycles (Johnny-trt.24's
sweep) before its final settle; past this the correction would be stale
conversationally anyway. The row itself stays worker-owned and durable."""

_WATCH_MAX_FETCH_FAILURES = 5
"""Consecutive ``fetch_status`` failures the watcher tolerates before
concluding the sink cannot serve reads and exiting quietly."""


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


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    """A point-in-time read of one task row (Johnny-trt.24).

    What :meth:`TaskSink.fetch_status` returns to the worker-owned-task
    watcher: just enough to recognise a terminal status and hand the
    speech-ready failure text to the Johnny-trt.53 correction. Deliberately
    not a full row — the watcher must never gain write-shaped state.
    """

    status: TaskStatus
    result_text: str | None = None
    error: str | None = None


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

    async def fetch_status(self, task_id: int) -> TaskSnapshot | None:
        """Read one task's current status (Johnny-trt.24, watcher support).

        Default returns ``None`` — "this sink cannot serve reads" — so
        custom test sinks keep working; the watcher then logs once and
        stops watching (the durable row still tells the truth, there is
        just no in-session correction for it).
        """
        return None

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

    async def fetch_status(self, task_id: int) -> TaskSnapshot | None:
        async with self._lock:
            for record in self._records:
                if record.task_id == task_id:
                    return TaskSnapshot(
                        status=record.status,
                        result_text=record.result_text,
                        error=record.error,
                    )
        return None

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

PublishCompleted = Callable[[QueuedTask, TaskStatus, TaskResult], Awaitable[None]]
"""Publish ``TaskCompleted`` on the session EventBus channel (Johnny-trt.25).

Called by the resolver *after* the terminal row update (the row-before-event
discipline ``begin`` applies to ``TaskQueued``), only for ``done``/``failed``
settles — ``cancelled`` means the session is tearing down (nobody is
listening, and the bus is closing with it). The status argument is the
*normalized* terminal actually written to the row (an executor returning an
illegal status is clamped to ``failed`` before this fires). Best-effort and
contained like the other announce seams."""

WakePing = Callable[[QueuedTask], Awaitable[None]]
"""Nudge the shared ``johnny.tasks.wake`` channel so an external worker
(Phase 4) picks queued work up without polling."""

ReportTaskFailed = Callable[[QueuedTask, TaskResult], Awaitable[None]]
"""Tell the session a task settled ``failed`` — the no-dead-promises seam
(Johnny-trt.53). Called by the resolver *after* the terminal row update, off
the turn loop, so the consumer (the gate's honest spoken correction via
``say()``) only ever reports durable state. Best-effort and contained like
the announce seams; never invoked for ``done`` (Phase-5 re-entry territory)
or ``cancelled`` (the session is tearing down — nobody is listening). For
worker-owned kinds (Johnny-trt.24) the watcher fires it from the polled row
once the worker's terminal write lands — same contract, different settler."""

RunsInSession = Callable[[str], bool]
"""Locality predicate (Johnny-trt.24): does this *kind* execute inside the
session process? ``True`` → the classic resolver runs the injected executor
(production: the trt.57 internal tools — ``meeting.leave``, ``session.end``);
``False`` → the row is left ``queued`` for the worker executor pass, which
claims every non-internal kind, and the coordinator only watches. ``None``
on the coordinator means *everything* runs in-session — the Phase-3 shape
that unit harnesses keep; :func:`johnny.agent.task_wiring.build_task_coordinator`
defaults production assemblies to the internal-kind predicate so the split
cannot be forgotten."""


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
        publish_completed: PublishCompleted | None = None,
        wake: WakePing | None = None,
        report_failed: ReportTaskFailed | None = None,
        runs_in_session: RunsInSession | None = None,
        watch_poll_interval_s: float = WATCH_POLL_INTERVAL_S,
        watch_timeout_s: float = WATCH_TIMEOUT_S,
    ) -> None:
        self._sink = sink
        self._executor = executor
        self._publish_queued = publish_queued
        self._publish_completed = publish_completed
        self._wake = wake
        # The no-dead-promises seam (Johnny-trt.53). Usually attached after
        # construction via :meth:`attach_failure_reporter` (the gate is built
        # *after* the coordinator in the runtime assembly, the attach_say
        # ordering pattern); the constructor arg serves directly-wired tests.
        self._report_failed = report_failed
        # Locality split (Johnny-trt.24). None keeps the Phase-3 behaviour:
        # every kind resolved in-session by the injected executor.
        self._runs_in_session = runs_in_session
        self._watch_poll_interval_s = watch_poll_interval_s
        self._watch_timeout_s = watch_timeout_s
        # Strong refs to in-flight resolver tasks so they aren't GC'd mid-run
        # (and to avoid "task exception never retrieved" warnings); also lets
        # aclose() drain them at teardown.
        self._tasks: set[asyncio.Task[None]] = set()
        # Watchers for worker-owned tasks (Johnny-trt.24) live apart from the
        # resolvers: they own no row, so aclose() must not spend its trt.57
        # drain grace on them — they are cancelled immediately at teardown.
        self._watchers: set[asyncio.Task[None]] = set()

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

        Locality (Johnny-trt.24): kinds the :data:`RunsInSession` predicate
        accepts get the in-process resolver; everything else stays ``queued``
        for the worker executor pass (the wake ping is its nudge) and only a
        read-only watcher is spawned, keeping the trt.53 failure correction
        alive until the Phase-5 listener replaces it.
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

        if self._runs_in_session is None or self._runs_in_session(spec.kind):
            runner = asyncio.ensure_future(self._run(queued))
            self._tasks.add(runner)
            runner.add_done_callback(self._tasks.discard)
        else:
            watcher = asyncio.ensure_future(self._watch(queued))
            self._watchers.add(watcher)
            watcher.add_done_callback(self._watchers.discard)
        return queued

    async def join(self) -> None:
        """Await every in-flight resolver *and watcher* without cancelling
        (tests / drain). A watcher returns once its row is terminal — callers
        joining on a still-running worker-owned task will wait with it."""
        tasks = [
            task
            for task in (*self._tasks, *self._watchers)
            if not task.done()
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def aclose(self, *, drain_grace_s: float = DEFAULT_ACLOSE_DRAIN_GRACE_S) -> None:
        """Drain in-flight resolvers briefly, then cancel the rest (teardown).

        The bounded drain (Johnny-trt.57) lets a resolver already past its
        executor — or one whose internal teardown action triggered this very
        ``aclose`` — finish its terminal row write, so the history shows the
        honest ``done``/``failed`` instead of a teardown-raced ``cancelled``.
        Anything still running after ``drain_grace_s`` is cancelled and marks
        its row ``cancelled`` on the way out (see :meth:`_run`), so a session
        teardown never strands tasks in ``running``. ``drain_grace_s=0``
        restores the immediate-cancel behaviour. Safe to call more than once.

        Watchers for worker-owned tasks (Johnny-trt.24) are cancelled without
        any drain: they hold nothing to settle (the worker owns the row and
        keeps running after the session ends), and waiting on one would just
        delay teardown by the full grace for every in-flight external task.
        """
        pending = [task for task in self._tasks if not task.done()]
        if pending and drain_grace_s > 0:
            await asyncio.wait(pending, timeout=drain_grace_s)
        tasks = list(self._tasks) + list(self._watchers)
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

        Both ``done`` and ``failed`` settles additionally announce a
        :class:`~johnny.voice_pipeline.events.TaskCompleted` through
        :data:`PublishCompleted` after the row update and before any failure
        report (Johnny-trt.25) — the live-UI signal that the durable result
        is queryable. ``cancelled`` announces nothing.
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
            await self._safe_publish_completed(queued, "failed", result)
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
        await self._safe_publish_completed(queued, status, result)
        if status == "failed":
            await self._safe_report_failed(queued, result)

    # ------------------------------------------------------------------ #
    # The worker-owned-task watcher (Johnny-trt.24)                       #
    # ------------------------------------------------------------------ #

    async def _watch(self, queued: QueuedTask) -> None:
        """Follow a worker-owned task to its terminal status — read-only.

        The worker executor pass owns the row (claim, run, settle, announce);
        this watcher exists solely so the session still *speaks* about a
        ``failed`` settle through the trt.53 :data:`ReportTaskFailed` seam —
        polled from the durable row, the only state the worker and the
        session share today. ``done`` / ``cancelled`` / ``expired`` end the
        watch silently (result delivery is the Phase-5 queue's job). The
        watcher NEVER writes the row — including on cancellation at session
        teardown, when the worker simply keeps running without us. Replaced
        by the Johnny-trt.28 push listener on ``johnny.tasks.<session>``.
        """
        deadline = asyncio.get_running_loop().time() + self._watch_timeout_s
        fetch_failures = 0
        while True:
            try:
                snapshot = await self._sink.fetch_status(queued.task_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                snapshot = None
            if snapshot is None:
                fetch_failures += 1
                if fetch_failures >= _WATCH_MAX_FETCH_FAILURES:
                    logger.warning(
                        "tasks.watch: sink cannot read task_id=%s (kind=%s) — "
                        "stopping watch; the row stays worker-owned and durable",
                        queued.task_id,
                        queued.spec.kind,
                    )
                    return
            else:
                fetch_failures = 0
                if snapshot.status == "failed":
                    result = TaskResult(
                        status="failed",
                        result_text=snapshot.result_text
                        or executor_error_text(queued.spec.kind),
                        error=snapshot.error or "",
                    )
                    await self._safe_report_failed(queued, result)
                    return
                if snapshot.status in TERMINAL_TASK_STATUSES:
                    return
            if asyncio.get_running_loop().time() >= deadline:
                logger.warning(
                    "tasks.watch: task_id=%s (kind=%s) not terminal after %.0fs — "
                    "stopping watch; the row stays worker-owned and durable",
                    queued.task_id,
                    queued.spec.kind,
                    self._watch_timeout_s,
                )
                return
            await asyncio.sleep(self._watch_poll_interval_s)

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

    async def _safe_publish_completed(
        self, queued: QueuedTask, status: TaskStatus, result: TaskResult
    ) -> None:
        if self._publish_completed is None:
            return
        try:
            await self._publish_completed(queued, status, result)
        except Exception:
            logger.exception(
                "tasks.run: TaskCompleted publish failed for task_id=%s", queued.task_id
            )

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
    "DEFAULT_ACLOSE_DRAIN_GRACE_S",
    "EXECUTOR_RESULT_STATUSES",
    "TERMINAL_TASK_STATUSES",
    "WATCH_POLL_INTERVAL_S",
    "WATCH_TIMEOUT_S",
    "InMemoryTaskSink",
    "PublishCompleted",
    "PublishQueued",
    "QueuedTask",
    "ReportTaskFailed",
    "RunsInSession",
    "TaskCoordinator",
    "TaskExecutor",
    "TaskRecord",
    "TaskResult",
    "TaskSink",
    "TaskSnapshot",
    "TaskSpec",
    "TaskStatus",
    "WakePing",
    "executor_error_text",
    "stub_executor",
    "unsupported_kind_text",
]
