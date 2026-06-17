"""Unit tests for the delegated-task coordinator core (Johnny-trt.18).

:mod:`johnny.agent.tasks` is stdlib-only (every I/O injected), so these run
without the ``agent`` extra: the ordering contract (row persisted *before*
``begin`` returns and before any announcement), the out-of-band lifecycle
(``queued`` → ``running`` → terminal), the stub executor's fail-fast promise,
and teardown (cancelled resolvers settle their rows).
"""

from __future__ import annotations

import asyncio
from typing import Any

from johnny.agent.tasks import (
    ANSWER_TASK_CONTEXT_HEADER,
    ANSWER_TASK_CONTEXT_RULE,
    EXECUTOR_RESULT_STATUSES,
    STATUS_NOTHING_IN_FLIGHT,
    STATUS_RECENT_SETTLE_S,
    TERMINAL_TASK_STATUSES,
    AnswerTaskContext,
    InMemoryTaskSink,
    QueuedTask,
    TaskCoordinator,
    TaskResult,
    TaskSpec,
    WorkstreamOverlayRow,
    executor_error_text,
    stub_executor,
    unsupported_kind_text,
)

# --- helpers -----------------------------------------------------------------


def _spec(**overrides: Any) -> TaskSpec:
    fields: dict[str, Any] = {
        "kind": "web_search",
        "args": {"query": "weather"},
        "ack_text": "let me check on that",
        "turn_id": 4,
        "decision_id": 17,
    }
    fields.update(overrides)
    return TaskSpec(**fields)


class _RecordingSink(InMemoryTaskSink):
    """InMemoryTaskSink that also logs call order into a shared event list."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    async def record_queued(
        self, spec: TaskSpec, *, callback_token: str | None = None
    ) -> int | None:
        task_id = await super().record_queued(spec, callback_token=callback_token)
        self._events.append(f"record_queued:{task_id}")
        return task_id

    async def update_status(self, task_id: int, status: Any, **fields: Any) -> None:
        await super().update_status(task_id, status, **fields)
        self._events.append(f"update:{task_id}:{status}")


class _FailingSink(InMemoryTaskSink):
    async def record_queued(
        self, spec: TaskSpec, *, callback_token: str | None = None
    ) -> int | None:
        raise RuntimeError("db down")


class _NoIdSink(InMemoryTaskSink):
    async def record_queued(
        self, spec: TaskSpec, *, callback_token: str | None = None
    ) -> int | None:
        return None


# --- begin: persistence-first ordering ----------------------------------------


async def test_begin_persists_queued_row_before_returning() -> None:
    sink = InMemoryTaskSink()
    coordinator = TaskCoordinator(sink, executor=stub_executor)

    queued = await coordinator.begin(_spec())

    assert queued is not None
    # No await between begin() returning and this assert: the resolver task is
    # scheduled but has not run, so the row state here is exactly what the
    # gate sees at the moment it speaks the ack.
    record = sink.get(queued.task_id)
    assert record is not None
    assert record.status == "queued"
    assert record.spec.kind == "web_search"
    await coordinator.aclose()


async def test_begin_persists_before_publish_and_wake() -> None:
    events: list[str] = []
    sink = _RecordingSink(events)

    async def publish(queued: QueuedTask) -> None:
        events.append(f"publish:{queued.task_id}")

    async def wake(queued: QueuedTask) -> None:
        events.append(f"wake:{queued.task_id}")

    coordinator = TaskCoordinator(sink, executor=stub_executor, publish_queued=publish, wake=wake)
    queued = await coordinator.begin(_spec())
    assert queued is not None

    # The row write is strictly first; the announcements follow in order.
    assert events[:3] == [
        f"record_queued:{queued.task_id}",
        f"publish:{queued.task_id}",
        f"wake:{queued.task_id}",
    ]
    await coordinator.aclose()


async def test_begin_returns_queued_task_with_sink_id_and_spec() -> None:
    sink = InMemoryTaskSink()
    coordinator = TaskCoordinator(sink, executor=stub_executor)
    spec = _spec()
    queued = await coordinator.begin(spec)
    assert queued is not None
    assert queued.task_id == 1
    assert queued.spec is spec
    await coordinator.aclose()


# --- begin: external_callback locality (US-303, Johnny-d6w.18) -----------------


async def test_begin_external_mints_token_and_spawns_nothing() -> None:
    """An ``external_callback`` spec mints + stores a ``callback_token``, announces
    the origin, and spawns NO resolver/watcher and sends NO wake — it settles only
    via the webhook (the executor must never run it)."""
    sink = InMemoryTaskSink()
    published: list[QueuedTask] = []
    woke: list[QueuedTask] = []

    async def publish(queued: QueuedTask) -> None:
        published.append(queued)

    async def wake(queued: QueuedTask) -> None:
        woke.append(queued)

    coordinator = TaskCoordinator(
        sink, executor=stub_executor, publish_queued=publish, wake=wake
    )
    queued = await coordinator.begin(
        _spec(kind="external.report", source_kind="external_callback")
    )

    assert queued is not None
    # Token minted, returned, and persisted on the durable row.
    assert isinstance(queued.callback_token, str)
    assert len(queued.callback_token) >= 20
    record = sink.get(queued.task_id)
    assert record is not None
    assert record.callback_token == queued.callback_token
    assert record.spec.source_kind == "external_callback"
    # No local execution machinery — and no worker nudge.
    assert not coordinator._resolvers
    assert not coordinator._watchers
    assert woke == []
    # The announce carried the origin so the durable writer + UI stamp it.
    assert published[0].spec.source_kind == "external_callback"
    # Yielding the loop does not settle it — nothing is running it.
    await asyncio.sleep(0)
    settled = sink.get(queued.task_id)
    assert settled is not None and settled.status == "queued"
    await coordinator.aclose()


async def test_begin_delegate_mints_no_callback_token() -> None:
    """The default (delegate) path never mints a token — the column stays NULL."""
    sink = InMemoryTaskSink()
    coordinator = TaskCoordinator(sink, executor=stub_executor)
    queued = await coordinator.begin(_spec())
    assert queued is not None
    assert queued.callback_token is None
    record = sink.get(queued.task_id)
    assert record is not None and record.callback_token is None
    await coordinator.aclose()


def test_external_callback_source_kind_constant_matches_model() -> None:
    """Drift guard: the stdlib-only constant mirrors the SQLAlchemy enum value."""
    from app.db.models import WorkstreamSourceKind
    from johnny.agent.tasks import EXTERNAL_CALLBACK_SOURCE_KIND

    assert (
        EXTERNAL_CALLBACK_SOURCE_KIND == WorkstreamSourceKind.EXTERNAL_CALLBACK.value
    )


# --- begin: persist failure = no promise --------------------------------------


async def test_begin_returns_none_when_sink_raises() -> None:
    published: list[QueuedTask] = []

    async def publish(queued: QueuedTask) -> None:
        published.append(queued)

    coordinator = TaskCoordinator(_FailingSink(), executor=stub_executor, publish_queued=publish)
    assert await coordinator.begin(_spec()) is None
    # Nothing announced, nothing running: no row means no promise.
    assert published == []
    assert len(coordinator._tasks) == 0


async def test_begin_returns_none_when_sink_returns_no_id() -> None:
    coordinator = TaskCoordinator(_NoIdSink(), executor=stub_executor)
    assert await coordinator.begin(_spec()) is None
    assert len(coordinator._tasks) == 0


# --- begin: announcements are best-effort -------------------------------------


async def test_publish_failure_does_not_break_begin_or_run() -> None:
    sink = InMemoryTaskSink()

    async def bad_publish(queued: QueuedTask) -> None:
        raise RuntimeError("bus down")

    coordinator = TaskCoordinator(sink, executor=stub_executor, publish_queued=bad_publish)
    queued = await coordinator.begin(_spec())
    assert queued is not None
    await coordinator.join()
    record = sink.get(queued.task_id)
    assert record is not None and record.status == "failed"


async def test_wake_failure_does_not_break_begin() -> None:
    sink = InMemoryTaskSink()

    async def bad_wake(queued: QueuedTask) -> None:
        raise RuntimeError("redis down")

    coordinator = TaskCoordinator(sink, executor=stub_executor, wake=bad_wake)
    queued = await coordinator.begin(_spec())
    assert queued is not None
    await coordinator.join()
    record = sink.get(queued.task_id)
    assert record is not None and record.status == "failed"


# --- the out-of-band lifecycle -------------------------------------------------


async def test_stub_executor_fails_unsupported_kind_fast_with_spoken_text() -> None:
    events: list[str] = []
    sink = _RecordingSink(events)
    coordinator = TaskCoordinator(sink, executor=stub_executor)

    queued = await coordinator.begin(_spec(kind="book_flight"))
    assert queued is not None
    await coordinator.join()

    record = sink.get(queued.task_id)
    assert record is not None
    assert record.status == "failed"
    # Speech-ready failure text is stored (the ack is not a dead promise).
    assert record.result_text == unsupported_kind_text("book_flight")
    assert record.error is not None and "book_flight" in record.error
    assert record.attempts == 1
    # Full lifecycle in order: queued (insert) -> running -> failed.
    assert events == [
        f"record_queued:{queued.task_id}",
        f"update:{queued.task_id}:running",
        f"update:{queued.task_id}:failed",
    ]


async def test_successful_executor_marks_done_with_result() -> None:
    sink = InMemoryTaskSink()

    async def executor(queued: QueuedTask) -> TaskResult:
        return TaskResult(
            status="done",
            result_text="The weather in Helsinki is sunny.",
            result_json={"temp_c": 21},
        )

    coordinator = TaskCoordinator(sink, executor=executor)
    queued = await coordinator.begin(_spec())
    assert queued is not None
    await coordinator.join()

    record = sink.get(queued.task_id)
    assert record is not None
    assert record.status == "done"
    assert record.result_text == "The weather in Helsinki is sunny."
    assert record.result_json == {"temp_c": 21}
    assert record.error is None
    assert record.attempts == 1


async def test_executor_exception_marks_failed_with_spoken_text() -> None:
    sink = InMemoryTaskSink()

    async def executor(queued: QueuedTask) -> TaskResult:
        raise ValueError("boom")

    coordinator = TaskCoordinator(sink, executor=executor)
    queued = await coordinator.begin(_spec())
    assert queued is not None
    await coordinator.join()

    record = sink.get(queued.task_id)
    assert record is not None
    assert record.status == "failed"
    assert record.result_text == executor_error_text("web_search")
    assert record.error == "executor error: ValueError: boom"


async def test_executor_illegal_status_clamped_to_failed() -> None:
    sink = InMemoryTaskSink()

    async def executor(queued: QueuedTask) -> TaskResult:
        return TaskResult(status="queued")  # type: ignore[arg-type]

    coordinator = TaskCoordinator(sink, executor=executor)
    queued = await coordinator.begin(_spec())
    assert queued is not None
    await coordinator.join()
    record = sink.get(queued.task_id)
    assert record is not None and record.status == "failed"


async def test_update_failure_does_not_crash_resolver() -> None:
    class _FlakySink(InMemoryTaskSink):
        async def update_status(self, task_id: int, status: Any, **fields: Any) -> None:
            raise RuntimeError("db flake")

    coordinator = TaskCoordinator(_FlakySink(), executor=stub_executor)
    queued = await coordinator.begin(_spec())
    assert queued is not None
    await coordinator.join()  # must not raise


# --- the failed-settle report seam (Johnny-trt.53) -------------------------------


class _RecordingReporter:
    """Captures every (QueuedTask, TaskResult) the coordinator reports."""

    def __init__(self) -> None:
        self.reported: list[tuple[QueuedTask, TaskResult]] = []

    async def __call__(self, queued: QueuedTask, result: TaskResult) -> None:
        self.reported.append((queued, result))


async def test_failed_settle_reports_after_row_update() -> None:
    """The stub executor's fast fail reaches the reporter — with the row
    already settled (the walk-back only ever describes durable state)."""
    sink = InMemoryTaskSink()
    reporter = _RecordingReporter()
    row_status_at_report: list[Any] = []

    async def status_capturing_reporter(queued: QueuedTask, result: TaskResult) -> None:
        record = sink.get(queued.task_id)
        row_status_at_report.append(record.status if record is not None else None)
        await reporter(queued, result)

    coordinator = TaskCoordinator(sink, executor=stub_executor)
    coordinator.attach_failure_reporter(status_capturing_reporter)

    queued = await coordinator.begin(_spec(kind="book_flight"))
    assert queued is not None

    await coordinator.join()

    assert len(reporter.reported) == 1
    reported_queued, reported_result = reporter.reported[0]
    assert reported_queued.task_id == queued.task_id
    assert reported_result.status == "failed"
    assert reported_result.result_text == unsupported_kind_text("book_flight")
    # The terminal row update strictly precedes the report — the row already
    # read ``failed`` at the moment the reporter ran.
    assert row_status_at_report == ["failed"]


async def test_executor_exception_reports_failure_with_spoken_text() -> None:
    reporter = _RecordingReporter()

    async def executor(queued: QueuedTask) -> TaskResult:
        raise ValueError("boom")

    coordinator = TaskCoordinator(
        InMemoryTaskSink(), executor=executor, report_failed=reporter
    )
    queued = await coordinator.begin(_spec())
    assert queued is not None
    await coordinator.join()

    assert len(reporter.reported) == 1
    _, result = reporter.reported[0]
    assert result.status == "failed"
    assert result.result_text == executor_error_text("web_search")


async def test_illegal_executor_status_reports_failure() -> None:
    reporter = _RecordingReporter()

    async def executor(queued: QueuedTask) -> TaskResult:
        return TaskResult(status="queued")  # type: ignore[arg-type]

    coordinator = TaskCoordinator(
        InMemoryTaskSink(), executor=executor, report_failed=reporter
    )
    queued = await coordinator.begin(_spec())
    assert queued is not None
    await coordinator.join()
    assert len(reporter.reported) == 1


async def test_done_settle_reports_nothing() -> None:
    """Successful results re-enter via the Phase-5 queue — never this seam."""
    reporter = _RecordingReporter()

    async def executor(queued: QueuedTask) -> TaskResult:
        return TaskResult(status="done", result_text="all good")

    coordinator = TaskCoordinator(
        InMemoryTaskSink(), executor=executor, report_failed=reporter
    )
    assert await coordinator.begin(_spec()) is not None
    await coordinator.join()
    assert reporter.reported == []


async def test_cancelled_settle_reports_nothing() -> None:
    """Session teardown cancels resolvers — nobody is listening, no report."""
    reporter = _RecordingReporter()
    started = asyncio.Event()

    async def hanging_executor(queued: QueuedTask) -> TaskResult:
        started.set()
        await asyncio.sleep(60)
        return TaskResult(status="done")

    coordinator = TaskCoordinator(
        InMemoryTaskSink(), executor=hanging_executor, report_failed=reporter
    )
    assert await coordinator.begin(_spec()) is not None
    await asyncio.wait_for(started.wait(), timeout=2)
    await coordinator.aclose(drain_grace_s=0)
    assert reporter.reported == []


async def test_reporter_raising_is_contained() -> None:
    """A blowing-up reporter never crashes the resolver (the say() seam may
    raise while the session drains) — and the row is already settled."""
    sink = InMemoryTaskSink()

    async def bad_reporter(queued: QueuedTask, result: TaskResult) -> None:
        raise RuntimeError("session draining")

    coordinator = TaskCoordinator(sink, executor=stub_executor, report_failed=bad_reporter)
    queued = await coordinator.begin(_spec())
    assert queued is not None
    await coordinator.join()  # must not raise
    record = sink.get(queued.task_id)
    assert record is not None and record.status == "failed"


async def test_failed_settle_without_reporter_is_noop() -> None:
    """No attached reporter (non-gate consumers, pre-trt.53 shape) — unchanged."""
    coordinator = TaskCoordinator(InMemoryTaskSink(), executor=stub_executor)
    assert await coordinator.begin(_spec()) is not None
    await coordinator.join()  # must not raise


# --- the completed-settle publish seam (Johnny-trt.25) ----------------------------


class _RecordingCompletedPublisher:
    """Captures every (QueuedTask, status, TaskResult) the resolver announces."""

    def __init__(self) -> None:
        self.published: list[tuple[QueuedTask, str, TaskResult]] = []

    async def __call__(self, queued: QueuedTask, status: Any, result: TaskResult) -> None:
        self.published.append((queued, status, result))


async def test_done_settle_publishes_completed_after_row_update() -> None:
    """A successful settle announces TaskCompleted with the row already done."""
    sink = InMemoryTaskSink()
    row_status_at_publish: list[Any] = []

    async def executor(queued: QueuedTask) -> TaskResult:
        return TaskResult(status="done", result_text="3 events this week", result_json={"n": 3})

    publisher = _RecordingCompletedPublisher()

    async def status_capturing_publisher(
        queued: QueuedTask, status: Any, result: TaskResult
    ) -> None:
        record = sink.get(queued.task_id)
        row_status_at_publish.append(record.status if record is not None else None)
        await publisher(queued, status, result)

    coordinator = TaskCoordinator(
        sink, executor=executor, publish_completed=status_capturing_publisher
    )
    queued = await coordinator.begin(_spec())
    assert queued is not None
    await coordinator.join()

    assert len(publisher.published) == 1
    published_queued, status, result = publisher.published[0]
    assert published_queued.task_id == queued.task_id
    assert status == "done"
    assert result.result_text == "3 events this week"
    # The terminal row write strictly precedes the announce (row-before-event).
    assert row_status_at_publish == ["done"]


async def test_failed_settle_publishes_completed_before_failure_report() -> None:
    """An honest failed settle announces completion, then walks the promise back."""
    order: list[str] = []
    publisher = _RecordingCompletedPublisher()

    async def ordering_publisher(queued: QueuedTask, status: Any, result: TaskResult) -> None:
        order.append("publish_completed")
        await publisher(queued, status, result)

    async def reporter(queued: QueuedTask, result: TaskResult) -> None:
        order.append("report_failed")

    coordinator = TaskCoordinator(
        InMemoryTaskSink(),
        executor=stub_executor,
        publish_completed=ordering_publisher,
        report_failed=reporter,
    )
    assert await coordinator.begin(_spec(kind="book_flight")) is not None
    await coordinator.join()

    assert order == ["publish_completed", "report_failed"]
    assert len(publisher.published) == 1
    _, status, result = publisher.published[0]
    assert status == "failed"
    assert result.result_text == unsupported_kind_text("book_flight")


async def test_executor_exception_publishes_completed_failed() -> None:
    publisher = _RecordingCompletedPublisher()

    async def executor(queued: QueuedTask) -> TaskResult:
        raise ValueError("boom")

    coordinator = TaskCoordinator(
        InMemoryTaskSink(), executor=executor, publish_completed=publisher
    )
    assert await coordinator.begin(_spec()) is not None
    await coordinator.join()

    assert len(publisher.published) == 1
    _, status, result = publisher.published[0]
    assert status == "failed"
    assert result.result_text == executor_error_text("web_search")


async def test_illegal_executor_status_publishes_normalized_failed() -> None:
    """The announce carries the clamped row status, never the illegal one."""
    publisher = _RecordingCompletedPublisher()

    async def executor(queued: QueuedTask) -> TaskResult:
        return TaskResult(status="queued")  # type: ignore[arg-type]

    coordinator = TaskCoordinator(
        InMemoryTaskSink(), executor=executor, publish_completed=publisher
    )
    assert await coordinator.begin(_spec()) is not None
    await coordinator.join()
    assert len(publisher.published) == 1
    assert publisher.published[0][1] == "failed"


async def test_cancelled_settle_publishes_nothing() -> None:
    """Teardown cancellation announces no completion — nobody is listening."""
    publisher = _RecordingCompletedPublisher()
    started = asyncio.Event()

    async def hanging_executor(queued: QueuedTask) -> TaskResult:
        started.set()
        await asyncio.sleep(60)
        return TaskResult(status="done")

    coordinator = TaskCoordinator(
        InMemoryTaskSink(), executor=hanging_executor, publish_completed=publisher
    )
    assert await coordinator.begin(_spec()) is not None
    await asyncio.wait_for(started.wait(), timeout=2)
    await coordinator.aclose(drain_grace_s=0)
    assert publisher.published == []


async def test_completed_publisher_raising_is_contained() -> None:
    """A blowing-up announce never crashes the resolver or eats the report."""
    sink = InMemoryTaskSink()
    reporter = _RecordingReporter()

    async def bad_publisher(queued: QueuedTask, status: Any, result: TaskResult) -> None:
        raise RuntimeError("bus down")

    coordinator = TaskCoordinator(
        sink,
        executor=stub_executor,
        publish_completed=bad_publisher,
        report_failed=reporter,
    )
    queued = await coordinator.begin(_spec())
    assert queued is not None
    await coordinator.join()  # must not raise
    record = sink.get(queued.task_id)
    assert record is not None and record.status == "failed"
    # The failure report still ran despite the dead bus.
    assert len(reporter.reported) == 1


async def test_settle_without_completed_publisher_is_noop() -> None:
    """No publisher attached (bare test harnesses) — settles exactly as before."""
    sink = InMemoryTaskSink()

    async def executor(queued: QueuedTask) -> TaskResult:
        return TaskResult(status="done", result_text="ok")

    coordinator = TaskCoordinator(sink, executor=executor)
    queued = await coordinator.begin(_spec())
    assert queued is not None
    await coordinator.join()
    record = sink.get(queued.task_id)
    assert record is not None and record.status == "done"


# --- teardown -------------------------------------------------------------------


async def test_aclose_cancels_in_flight_runner_and_marks_cancelled() -> None:
    sink = InMemoryTaskSink()
    started = asyncio.Event()

    async def hanging_executor(queued: QueuedTask) -> TaskResult:
        started.set()
        await asyncio.sleep(60)
        return TaskResult(status="done")

    coordinator = TaskCoordinator(sink, executor=hanging_executor)
    queued = await coordinator.begin(_spec())
    assert queued is not None
    await asyncio.wait_for(started.wait(), timeout=2)

    await coordinator.aclose(drain_grace_s=0)

    record = sink.get(queued.task_id)
    assert record is not None
    assert record.status == "cancelled"
    assert record.error is not None and "teardown" in record.error
    assert record.result_text is not None and "cancelled" in record.result_text
    assert len(coordinator._tasks) == 0


async def test_aclose_is_idempotent_and_safe_when_idle() -> None:
    coordinator = TaskCoordinator(InMemoryTaskSink(), executor=stub_executor)
    await coordinator.aclose()
    await coordinator.aclose()


async def test_aclose_drain_grace_lets_an_inflight_settle_finish(  # Johnny-trt.57
) -> None:
    """A resolver that completes within the grace settles ``done``, not ``cancelled``.

    The shape of an internal teardown task (meeting.leave / session.end):
    its control call triggers the very teardown that calls ``aclose`` while
    the resolver is still finishing — the bounded drain lets the honest
    terminal land instead of recording a misleading ``cancelled``.
    """
    sink = InMemoryTaskSink()
    started = asyncio.Event()
    release = asyncio.Event()

    async def almost_done_executor(queued: QueuedTask) -> TaskResult:
        started.set()
        await release.wait()
        return TaskResult(status="done", result_text="left the meeting")

    coordinator = TaskCoordinator(sink, executor=almost_done_executor)
    queued = await coordinator.begin(_spec(kind="meeting.leave"))
    assert queued is not None
    await asyncio.wait_for(started.wait(), timeout=2)

    closer = asyncio.ensure_future(coordinator.aclose(drain_grace_s=5.0))
    await asyncio.sleep(0)  # the drain is now waiting on the resolver
    release.set()
    await asyncio.wait_for(closer, timeout=2)

    record = sink.get(queued.task_id)
    assert record is not None
    assert record.status == "done"
    assert record.result_text == "left the meeting"


async def test_aclose_drain_grace_expiry_still_cancels() -> None:
    """A genuinely hung executor is cancelled once the grace expires."""
    sink = InMemoryTaskSink()
    started = asyncio.Event()

    async def hanging_executor(queued: QueuedTask) -> TaskResult:
        started.set()
        await asyncio.sleep(60)
        return TaskResult(status="done")

    coordinator = TaskCoordinator(sink, executor=hanging_executor)
    queued = await coordinator.begin(_spec())
    assert queued is not None
    await asyncio.wait_for(started.wait(), timeout=2)

    await asyncio.wait_for(coordinator.aclose(drain_grace_s=0.05), timeout=2)

    record = sink.get(queued.task_id)
    assert record is not None
    assert record.status == "cancelled"


async def test_join_waits_for_completion_without_cancelling() -> None:
    sink = InMemoryTaskSink()

    async def slow_executor(queued: QueuedTask) -> TaskResult:
        await asyncio.sleep(0.01)
        return TaskResult(status="done", result_text="ok")

    coordinator = TaskCoordinator(sink, executor=slow_executor)
    queued = await coordinator.begin(_spec())
    assert queued is not None
    await coordinator.join()
    record = sink.get(queued.task_id)
    assert record is not None and record.status == "done"


# --- vocabulary ------------------------------------------------------------------


def test_terminal_statuses_vocabulary() -> None:
    assert TERMINAL_TASK_STATUSES == {"done", "failed", "cancelled", "expired"}
    assert EXECUTOR_RESULT_STATUSES == {"done", "failed"}
    assert EXECUTOR_RESULT_STATUSES < TERMINAL_TASK_STATUSES


async def test_in_memory_sink_update_unknown_id_is_noop() -> None:
    sink = InMemoryTaskSink()
    # No record with id 99 — must not raise (mirrors the SQL sink's warn+return).
    await sink.update_status(99, "done")
    assert len(sink.snapshot()) == 0


# --- locality routing + the worker-owned-task watcher (Johnny-trt.24) ----------


def _external_coordinator(
    sink: InMemoryTaskSink,
    *,
    local_kinds: frozenset[str] = frozenset({"local.kind"}),
    **kwargs: Any,
) -> TaskCoordinator:
    """Coordinator routing everything outside ``local_kinds`` to the watcher,
    with test-speed watch cadence."""
    kwargs.setdefault("executor", stub_executor)
    kwargs.setdefault("watch_poll_interval_s", 0.01)
    kwargs.setdefault("watch_timeout_s", 2.0)
    return TaskCoordinator(
        sink, runs_in_session=lambda kind: kind in local_kinds, **kwargs
    )


async def test_runs_in_session_none_keeps_everything_in_process() -> None:
    """Default predicate (None) = the Phase-3 shape: the executor runs."""
    sink = InMemoryTaskSink()
    coordinator = TaskCoordinator(sink, executor=stub_executor)
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None
    await coordinator.join()
    record = sink.get(queued.task_id)
    assert record is not None and record.status == "failed"  # stub ran in-process


async def test_local_kind_routes_to_in_process_resolver() -> None:
    sink = InMemoryTaskSink()
    ran: list[str] = []

    async def executor(task: QueuedTask) -> TaskResult:
        ran.append(task.spec.kind)
        return TaskResult(status="done", result_text="ok")

    coordinator = _external_coordinator(sink, executor=executor)
    queued = await coordinator.begin(_spec(kind="local.kind"))
    assert queued is not None
    await coordinator.join()
    assert ran == ["local.kind"]
    record = sink.get(queued.task_id)
    assert record is not None and record.status == "done"


async def test_external_kind_never_runs_the_session_executor() -> None:
    """Worker-owned kinds stay queued: no resolver, no executor call, no
    running stamp — the worker claims the row, not the session."""
    sink = InMemoryTaskSink()
    ran: list[str] = []

    async def executor(task: QueuedTask) -> TaskResult:
        ran.append(task.spec.kind)
        return TaskResult(status="done")

    coordinator = _external_coordinator(sink, executor=executor)
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None
    await asyncio.sleep(0.05)  # several watch polls
    assert ran == []
    record = sink.get(queued.task_id)
    assert record is not None and record.status == "queued"
    await coordinator.aclose()


async def test_external_failed_settle_fires_failure_report() -> None:
    """The trt.53 bridge: the worker settles failed out of process; the
    watcher reads the row and reports it with the row's speech-ready text."""
    sink = InMemoryTaskSink()
    reported: list[tuple[int, str, str, str]] = []

    async def report(queued: QueuedTask, result: TaskResult) -> None:
        reported.append(
            (queued.task_id, result.status, result.result_text, result.error)
        )

    coordinator = _external_coordinator(sink, report_failed=report)
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None
    # Simulate the worker: running, then a failed settle with spoken copy.
    await sink.update_status(queued.task_id, "running", attempts=1)
    await asyncio.sleep(0.03)
    assert reported == []  # nothing reported while in flight
    await sink.update_status(
        queued.task_id,
        "failed",
        result_text="No Google account is connected.",
        error="gog: not authenticated",
    )
    await coordinator.join()
    assert reported == [
        (
            queued.task_id,
            "failed",
            "No Google account is connected.",
            "gog: not authenticated",
        )
    ]
    # The watcher never writes the row — the worker's words stand.
    record = sink.get(queued.task_id)
    assert record is not None
    assert record.status == "failed"
    assert record.result_text == "No Google account is connected."


async def test_external_failed_settle_without_text_reports_generic_speech() -> None:
    sink = InMemoryTaskSink()
    reported: list[TaskResult] = []

    async def report(queued: QueuedTask, result: TaskResult) -> None:
        reported.append(result)

    coordinator = _external_coordinator(sink, report_failed=report)
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None
    await sink.update_status(queued.task_id, "failed")
    await coordinator.join()
    assert len(reported) == 1
    assert reported[0].result_text == executor_error_text("web_search")


async def test_external_done_settle_reports_nothing() -> None:
    """done is Phase-5 re-entry territory — the watcher exits silently."""
    sink = InMemoryTaskSink()
    reported: list[TaskResult] = []

    async def report(queued: QueuedTask, result: TaskResult) -> None:
        reported.append(result)

    coordinator = _external_coordinator(sink, report_failed=report)
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None
    await sink.update_status(queued.task_id, "done", result_text="3 events this week")
    await coordinator.join()
    assert reported == []


async def test_external_cancelled_or_expired_settle_reports_nothing() -> None:
    sink = InMemoryTaskSink()
    reported: list[TaskResult] = []

    async def report(queued: QueuedTask, result: TaskResult) -> None:
        reported.append(result)

    coordinator = _external_coordinator(sink, report_failed=report)
    first = await coordinator.begin(_spec(kind="web_search"))
    second = await coordinator.begin(_spec(kind="gmail.search"))
    assert first is not None and second is not None
    await sink.update_status(first.task_id, "cancelled")
    await sink.update_status(second.task_id, "expired")
    await coordinator.join()
    assert reported == []


async def test_external_completed_event_not_published_by_session() -> None:
    """The settler announces (trt.25): the worker owns TaskCompleted for
    worker-owned kinds — the session's publish_completed must stay silent."""
    sink = InMemoryTaskSink()
    completed: list[int] = []

    async def publish_completed(queued: QueuedTask, status: Any, result: TaskResult) -> None:
        completed.append(queued.task_id)

    coordinator = _external_coordinator(sink, publish_completed=publish_completed)
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None
    await sink.update_status(queued.task_id, "failed", result_text="nope")
    await coordinator.join()
    assert completed == []


async def test_aclose_cancels_watcher_immediately_without_touching_row() -> None:
    """Watchers are exempt from the trt.57 drain grace (they settle nothing)
    and must never write the worker-owned row on the way out."""
    sink = InMemoryTaskSink()
    coordinator = _external_coordinator(sink)
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None
    loop = asyncio.get_running_loop()
    started = loop.time()
    await coordinator.aclose(drain_grace_s=30.0)
    assert loop.time() - started < 5.0  # no 30s drain spent on the watcher
    record = sink.get(queued.task_id)
    assert record is not None
    assert record.status == "queued"  # untouched — the worker still owns it


async def test_watcher_gives_up_when_sink_cannot_fetch() -> None:
    """A sink without fetch support (default returns None) ends the watch
    after a few polls — logged, contained, row untouched."""

    class _NoFetchSink(InMemoryTaskSink):
        async def fetch_status(self, task_id: int) -> Any:
            return None

    sink = _NoFetchSink()
    reported: list[TaskResult] = []

    async def report(queued: QueuedTask, result: TaskResult) -> None:
        reported.append(result)

    coordinator = _external_coordinator(sink, report_failed=report)
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None
    await coordinator.join()  # watcher exits via the fetch-failure budget
    assert reported == []
    record = sink.get(queued.task_id)
    assert record is not None and record.status == "queued"


async def test_watcher_times_out_quietly_on_a_never_terminal_row() -> None:
    sink = InMemoryTaskSink()
    reported: list[TaskResult] = []

    async def report(queued: QueuedTask, result: TaskResult) -> None:
        reported.append(result)

    coordinator = _external_coordinator(
        sink, report_failed=report, watch_timeout_s=0.05
    )
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None
    await coordinator.join()
    assert reported == []
    record = sink.get(queued.task_id)
    assert record is not None and record.status == "queued"


async def test_watcher_tolerates_transient_fetch_errors() -> None:
    """A couple of raising polls don't end the watch — the budget resets on
    the first successful read and the failed settle still gets reported."""
    sink = InMemoryTaskSink()
    calls = {"n": 0}
    real_fetch = sink.fetch_status

    async def flaky_fetch(task_id: int) -> Any:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("transient")
        return await real_fetch(task_id)

    sink.fetch_status = flaky_fetch  # type: ignore[method-assign]
    reported: list[TaskResult] = []

    async def report(queued: QueuedTask, result: TaskResult) -> None:
        reported.append(result)

    coordinator = _external_coordinator(sink, report_failed=report)
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None
    await sink.update_status(queued.task_id, "failed", result_text="broke")
    await coordinator.join()
    assert len(reported) == 1 and reported[0].result_text == "broke"


async def test_external_kind_still_pings_wake_and_publishes_queued() -> None:
    """Row-before-announce holds for worker-owned kinds too: TaskQueued and
    the wake ping (the worker's nudge) both fire from begin()."""
    events: list[str] = []
    sink = _RecordingSink(events)

    async def publish_queued(queued: QueuedTask) -> None:
        events.append(f"publish:{queued.task_id}")

    async def wake(queued: QueuedTask) -> None:
        events.append(f"wake:{queued.task_id}")

    coordinator = _external_coordinator(
        sink, publish_queued=publish_queued, wake=wake
    )
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None
    assert events == [
        f"record_queued:{queued.task_id}",
        f"publish:{queued.task_id}",
        f"wake:{queued.task_id}",
    ]
    await coordinator.aclose()


async def test_in_memory_sink_fetch_status_reads_current_row() -> None:
    sink = InMemoryTaskSink()
    task_id = await sink.record_queued(_spec())
    assert task_id is not None
    snapshot = await sink.fetch_status(task_id)
    assert snapshot is not None and snapshot.status == "queued"
    await sink.update_status(task_id, "failed", result_text="say this", error="why")
    snapshot = await sink.fetch_status(task_id)
    assert snapshot is not None
    assert snapshot.status == "failed"
    assert snapshot.result_text == "say this"
    assert snapshot.error == "why"
    assert await sink.fetch_status(9999) is None


# --- the in-memory task registry (Johnny-trt.28) -------------------------------


def _registry_coordinator(
    sink: InMemoryTaskSink | None = None,
    *,
    now: list[float] | None = None,
    **kwargs: Any,
) -> tuple[TaskCoordinator, InMemoryTaskSink]:
    """An external-kind coordinator with an injectable monotonic clock.

    ``now`` is a single-element list the test mutates to advance time.
    """
    sink = sink if sink is not None else InMemoryTaskSink()
    clock = now if now is not None else [100.0]
    kwargs.setdefault("monotonic", lambda: clock[0])
    coordinator = _external_coordinator(sink, **kwargs)
    return coordinator, sink


async def test_begin_seeds_registry_entry_with_origin_and_clock() -> None:
    now = [50.0]
    coordinator, _sink = _registry_coordinator(now=now)
    queued = await coordinator.begin(_spec(kind="web_search", ack_text="on it", turn_id=9))
    assert queued is not None
    entry = coordinator.registry_entry(queued.task_id)
    assert entry is not None
    assert entry.kind == "web_search"
    assert entry.origin == "worker"  # not in local_kinds
    assert entry.status == "queued"
    assert entry.queued_at == 50.0
    assert entry.ack_text == "on it"
    assert entry.turn_id == 9
    assert entry.delivered is False
    assert entry.terminal is False
    await coordinator.aclose()


async def test_in_session_resolver_updates_registry_to_done() -> None:
    sink = InMemoryTaskSink()

    async def executor(task: QueuedTask) -> TaskResult:
        return TaskResult(status="done", result_text="3 events this week")

    coordinator, _ = _registry_coordinator(sink, executor=executor)
    queued = await coordinator.begin(_spec(kind="local.kind"))
    assert queued is not None
    entry = coordinator.registry_entry(queued.task_id)
    assert entry is not None and entry.origin == "session"
    await coordinator.join()
    assert entry.status == "done"
    assert entry.result_text == "3 events this week"
    assert entry.settled_at is not None
    await coordinator.aclose()


async def test_note_task_settled_first_observer_wins() -> None:
    coordinator, _ = _registry_coordinator()
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None
    first = coordinator.note_task_settled(
        queued.task_id, status="done", result_text="the answer"
    )
    assert first is not None and first.status == "done"
    # The losing observer gets None and must trigger no side effects.
    second = coordinator.note_task_settled(
        queued.task_id, status="failed", result_text="other", error="boom"
    )
    assert second is None
    entry = coordinator.registry_entry(queued.task_id)
    assert entry is not None
    assert entry.status == "done"
    assert entry.result_text == "the answer"
    await coordinator.aclose()


async def test_note_task_settled_refuses_non_terminal_status() -> None:
    coordinator, _ = _registry_coordinator()
    queued = await coordinator.begin(_spec())
    assert queued is not None
    assert coordinator.note_task_settled(queued.task_id, status="running") is None
    entry = coordinator.registry_entry(queued.task_id)
    assert entry is not None and entry.status == "queued"
    await coordinator.aclose()


async def test_note_task_running_and_settled_seed_unknown_ids() -> None:
    """Frames for tasks this process never began still keep the registry honest."""
    coordinator, _ = _registry_coordinator()
    coordinator.note_task_running(777, kind="calendar.check", turn_id=3)
    entry = coordinator.registry_entry(777)
    assert entry is not None
    assert entry.origin == "worker" and entry.status == "running"
    settled = coordinator.note_task_settled(
        888, status="done", kind="calendar.check", result_text="done text"
    )
    assert settled is not None and settled.task_id == 888
    await coordinator.aclose()


async def test_late_progress_frame_never_reopens_a_settled_task() -> None:
    coordinator, _ = _registry_coordinator()
    queued = await coordinator.begin(_spec())
    assert queued is not None
    assert coordinator.note_task_settled(queued.task_id, status="done") is not None
    coordinator.note_task_running(queued.task_id)
    entry = coordinator.registry_entry(queued.task_id)
    assert entry is not None and entry.status == "done"
    await coordinator.aclose()


async def test_mark_result_delivered_flips_flag_and_handles_unknown() -> None:
    coordinator, _ = _registry_coordinator()
    queued = await coordinator.begin(_spec())
    assert queued is not None
    assert coordinator.mark_result_delivered(queued.task_id) is True
    entry = coordinator.registry_entry(queued.task_id)
    assert entry is not None and entry.delivered is True
    assert coordinator.mark_result_delivered(31337) is False
    await coordinator.aclose()


async def test_registry_snapshot_keeps_begin_order() -> None:
    coordinator, _ = _registry_coordinator()
    first = await coordinator.begin(_spec(kind="web_search"))
    second = await coordinator.begin(_spec(kind="calendar.check"))
    assert first is not None and second is not None
    snapshot = coordinator.registry_snapshot()
    assert [e.task_id for e in snapshot] == [first.task_id, second.task_id]
    await coordinator.aclose()


async def test_attach_remote_listener_suppresses_the_watcher() -> None:
    """With a live push listener, worker-owned begins spawn no poll watcher."""
    sink = InMemoryTaskSink()
    coordinator, _ = _registry_coordinator(sink)
    coordinator.attach_remote_listener()
    assert coordinator.remote_listener_active is True
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None
    assert not coordinator._watchers  # no watcher task spawned
    # join() returns immediately — nothing in flight.
    await asyncio.wait_for(coordinator.join(), timeout=0.5)
    # Detach restores the Phase-4 fallback for future begins.
    coordinator.detach_remote_listener()
    queued2 = await coordinator.begin(_spec(kind="web_search"))
    assert queued2 is not None
    assert len(coordinator._watchers) == 1
    await coordinator.aclose()


async def test_watcher_observed_settle_routes_through_registry_exactly_once() -> None:
    """The watcher's failed report fires only when IT settles the entry."""
    sink = InMemoryTaskSink()
    reporter = _RecordingReporter()
    coordinator, _ = _registry_coordinator(sink, report_failed=reporter)
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None
    # The push listener observes the settle first (simulated).
    assert (
        coordinator.note_task_settled(queued.task_id, status="failed", error="x")
        is not None
    )
    # Now the worker writes the row; the watcher polls it terminal.
    await sink.update_status(queued.task_id, "failed", result_text="sorry", error="x")
    await asyncio.wait_for(coordinator.join(), timeout=2.0)
    # The watcher lost the registry race — no second report.
    assert reporter.reported == []
    await coordinator.aclose()


async def test_watcher_failed_settle_still_reports_when_first() -> None:
    """No listener: the watcher remains the trt.53 correction's settler."""
    sink = InMemoryTaskSink()
    reporter = _RecordingReporter()
    coordinator, _ = _registry_coordinator(sink, report_failed=reporter)
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None
    await sink.update_status(queued.task_id, "failed", result_text="sorry", error="x")
    await asyncio.wait_for(coordinator.join(), timeout=2.0)
    assert len(reporter.reported) == 1
    entry = coordinator.registry_entry(queued.task_id)
    assert entry is not None and entry.status == "failed"
    await coordinator.aclose()


async def test_report_remote_failure_routes_the_attached_seam() -> None:
    coordinator, _ = _registry_coordinator()
    reporter = _RecordingReporter()
    coordinator.attach_failure_reporter(reporter)
    queued = await coordinator.begin(
        _spec(kind="calendar.check", ack_text="on it", turn_id=5)
    )
    assert queued is not None
    entry = coordinator.note_task_settled(
        queued.task_id, status="failed", result_text="couldn't reach the calendar", error="dns"
    )
    assert entry is not None
    await coordinator.report_remote_failure(entry)
    assert len(reporter.reported) == 1
    reported_queued, reported_result = reporter.reported[0]
    assert reported_queued.task_id == queued.task_id
    assert reported_queued.spec.kind == "calendar.check"
    assert reported_queued.spec.turn_id == 5
    assert reported_result.status == "failed"
    assert reported_result.result_text == "couldn't reach the calendar"
    await coordinator.aclose()


async def test_reconcile_in_flight_settles_from_the_sink() -> None:
    """A settle missed by the push channel is recovered from the durable row."""
    sink = InMemoryTaskSink()
    coordinator, _ = _registry_coordinator(sink)
    coordinator.attach_remote_listener()  # no watcher — the listener owns settles
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None
    # The worker settled the row while the subscription was down.
    await sink.update_status(queued.task_id, "done", result_text="found 3 events")
    settled = await coordinator.reconcile_in_flight()
    assert [e.task_id for e in settled] == [queued.task_id]
    assert settled[0].status == "done"
    assert settled[0].result_text == "found 3 events"
    # A second reconcile finds nothing new (first-wins held).
    assert await coordinator.reconcile_in_flight() == []
    await coordinator.aclose()


async def test_reconcile_skips_session_origin_and_non_terminal_rows() -> None:
    sink = InMemoryTaskSink()

    async def executor(task: QueuedTask) -> TaskResult:
        await asyncio.Event().wait()  # in-flight forever (cancelled by aclose)
        return TaskResult(status="done")  # pragma: no cover

    coordinator, _ = _registry_coordinator(sink, executor=executor)
    coordinator.attach_remote_listener()
    local = await coordinator.begin(_spec(kind="local.kind"))
    remote = await coordinator.begin(_spec(kind="web_search"))
    assert local is not None and remote is not None
    # local: row still queued/running (resolver owns it); remote: row queued.
    assert await coordinator.reconcile_in_flight() == []
    await coordinator.aclose(drain_grace_s=0)


# --- the status query render (Johnny-trt.29) ------------------------------------


def _status_coordinator(now: list[float]) -> TaskCoordinator:
    """A listener-attached coordinator (no watchers) with a mutable clock —
    the status render is a pure registry read, so no sink/executor activity
    is wanted in these tests."""
    coordinator, _ = _registry_coordinator(now=now)
    coordinator.attach_remote_listener()
    return coordinator


async def test_status_summary_empty_registry() -> None:
    now = [100.0]
    coordinator = _status_coordinator(now)
    summary = coordinator.status_summary()
    assert summary.text == STATUS_NOTHING_IN_FLIGHT
    assert summary.carried_results == ()
    await coordinator.aclose()


async def test_status_summary_single_in_flight_with_duration() -> None:
    now = [100.0]
    coordinator = _status_coordinator(now)
    queued = await coordinator.begin(_spec(kind="google-calendar"))
    assert queued is not None
    coordinator.note_task_running(queued.task_id)
    now[0] = 121.0  # 21 s since begin
    summary = coordinator.status_summary()
    assert summary.text == (
        "Still working on the google calendar task, about 20 seconds in."
    )
    assert summary.carried_results == ()
    await coordinator.aclose()


async def test_status_summary_duration_phrasing_bands() -> None:
    now = [100.0]
    coordinator = _status_coordinator(now)
    queued = await coordinator.begin(_spec(kind="gmail.search"))
    assert queued is not None
    now[0] = 103.0  # 3 s — below the number-is-noise floor
    assert "just a few seconds in" in coordinator.status_summary().text
    now[0] = 287.0  # 187 s — minutes band
    assert "about 3 minutes in" in coordinator.status_summary().text
    now[0] = 161.0  # 61 s — rounds to the nearest 5
    assert "about 60 seconds in" in coordinator.status_summary().text
    await coordinator.aclose()


async def test_status_summary_multiple_in_flight() -> None:
    now = [100.0]
    coordinator = _status_coordinator(now)
    first = await coordinator.begin(_spec(kind="google-calendar"))
    now[0] = 115.0
    second = await coordinator.begin(_spec(kind="gmail.search"))
    assert first is not None and second is not None
    now[0] = 120.0
    text = coordinator.status_summary().text
    assert "Still working on the google calendar task, about 20 seconds in." in text
    assert "Also still on the gmail search task, just a few seconds in." in text
    await coordinator.aclose()


async def test_status_summary_undelivered_result_carried_verbatim() -> None:
    """The session-4 seam: a done-but-unspoken result is delivered inside the
    status text whatever its age, and returned in carried_results."""
    now = [100.0]
    coordinator = _status_coordinator(now)
    queued = await coordinator.begin(_spec(kind="google-calendar"))
    assert queued is not None
    entry = coordinator.note_task_settled(
        queued.task_id, status="done", result_text="You have 3 events this week."
    )
    assert entry is not None
    # Far past the recent-settle window — undelivered results never go stale.
    now[0] = 100.0 + STATUS_RECENT_SETTLE_S * 10
    summary = coordinator.status_summary()
    assert summary.text == (
        "The google calendar task is done: You have 3 events this week."
    )
    assert summary.carried_results == (entry,)
    await coordinator.aclose()


async def test_status_summary_result_text_gets_sentence_punctuation() -> None:
    now = [100.0]
    coordinator = _status_coordinator(now)
    queued = await coordinator.begin(_spec(kind="google-calendar"))
    assert queued is not None
    coordinator.note_task_settled(
        queued.task_id, status="done", result_text="3 events this week"
    )
    assert coordinator.status_summary().text == (
        "The google calendar task is done: 3 events this week."
    )
    await coordinator.aclose()


async def test_status_summary_recent_failure_spoken_with_result_text() -> None:
    now = [100.0]
    coordinator = _status_coordinator(now)
    queued = await coordinator.begin(_spec(kind="google-calendar"))
    assert queued is not None
    coordinator.note_task_settled(
        queued.task_id,
        status="failed",
        result_text="The calendar account isn't linked.",
    )
    now[0] = 130.0
    summary = coordinator.status_summary()
    assert summary.text == (
        "The google calendar task didn't work out: The calendar account isn't linked."
    )
    assert summary.carried_results == ()  # failures are never queue-carried
    await coordinator.aclose()


async def test_status_summary_failure_without_text_gets_generic_speech() -> None:
    now = [100.0]
    coordinator = _status_coordinator(now)
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None
    coordinator.note_task_settled(queued.task_id, status="failed", error="exit 2")
    text = coordinator.status_summary().text
    assert executor_error_text("web_search") in text
    await coordinator.aclose()


async def test_status_summary_stale_failure_not_mentioned() -> None:
    now = [100.0]
    coordinator = _status_coordinator(now)
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None
    coordinator.note_task_settled(queued.task_id, status="failed", result_text="nope")
    now[0] = 100.0 + STATUS_RECENT_SETTLE_S + 1.0
    assert coordinator.status_summary().text == STATUS_NOTHING_IN_FLIGHT
    await coordinator.aclose()


async def test_status_summary_delivered_result_gets_aware_tail() -> None:
    now = [100.0]
    coordinator = _status_coordinator(now)
    queued = await coordinator.begin(_spec(kind="google-calendar"))
    assert queued is not None
    coordinator.note_task_settled(
        queued.task_id, status="done", result_text="You have 3 events this week."
    )
    coordinator.mark_result_delivered(queued.task_id)
    now[0] = 110.0
    summary = coordinator.status_summary()
    assert summary.text == (
        "Nothing in flight right now — the google calendar task finished "
        "and I already shared the result."
    )
    assert summary.carried_results == ()
    # …and past the window it is no longer brought up at all.
    now[0] = 100.0 + STATUS_RECENT_SETTLE_S + 1.0
    assert coordinator.status_summary().text == STATUS_NOTHING_IN_FLIGHT
    await coordinator.aclose()


async def test_status_summary_blank_result_done_is_not_carried() -> None:
    """A done task with nothing speakable (UI-only) must not promise a result."""
    now = [100.0]
    coordinator = _status_coordinator(now)
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None
    coordinator.note_task_settled(queued.task_id, status="done", result_text="   ")
    summary = coordinator.status_summary()
    assert summary.carried_results == ()
    assert summary.text == (
        "Nothing in flight right now — the web search task finished "
        "and there was nothing to report back."
    )
    await coordinator.aclose()


async def test_status_summary_cancelled_and_expired_never_mentioned() -> None:
    now = [100.0]
    coordinator = _status_coordinator(now)
    first = await coordinator.begin(_spec(kind="web_search"))
    second = await coordinator.begin(_spec(kind="gmail.search"))
    assert first is not None and second is not None
    coordinator.note_task_settled(first.task_id, status="cancelled")
    coordinator.note_task_settled(second.task_id, status="expired")
    assert coordinator.status_summary().text == STATUS_NOTHING_IN_FLIGHT
    await coordinator.aclose()


async def test_status_summary_composes_result_then_active_then_failure() -> None:
    """Most-valuable-first composition: undelivered result, in-flight, failure."""
    now = [100.0]
    coordinator = _status_coordinator(now)
    done = await coordinator.begin(_spec(kind="google-calendar"))
    running = await coordinator.begin(_spec(kind="gmail.search"))
    failed = await coordinator.begin(_spec(kind="web_search"))
    assert done is not None and running is not None and failed is not None
    coordinator.note_task_settled(
        done.task_id, status="done", result_text="You have 3 events this week."
    )
    coordinator.note_task_settled(failed.task_id, status="failed", result_text="No luck.")
    now[0] = 121.0
    summary = coordinator.status_summary()
    assert summary.text == (
        "The google calendar task is done: You have 3 events this week. "
        "Still working on the gmail search task, about 20 seconds in. "
        "The web search task didn't work out: No luck."
    )
    assert [entry.task_id for entry in summary.carried_results] == [done.task_id]
    await coordinator.aclose()


# --- the answer-context render (Johnny-0qw) --------------------------------------


async def test_answer_task_context_empty_registry() -> None:
    coordinator = _status_coordinator([100.0])
    context = coordinator.answer_task_context()
    assert context.empty
    assert context.text == ""
    assert context.undelivered == ()
    assert context.in_flight == ()
    await coordinator.aclose()


async def test_answer_task_context_default_instance_is_empty() -> None:
    """The coordinator-less gate's stand-in renders nothing."""
    context = AnswerTaskContext()
    assert context.empty
    assert context.text == ""


async def test_answer_task_context_undelivered_result_verbatim() -> None:
    """The settle→delivery blind window: the result rides verbatim, framed by
    the header and the no-invention rule."""
    now = [100.0]
    coordinator = _status_coordinator(now)
    queued = await coordinator.begin(_spec(kind="google-calendar"))
    assert queued is not None
    entry = coordinator.note_task_settled(
        queued.task_id, status="done", result_text="You have 3 events this week."
    )
    assert entry is not None
    context = coordinator.answer_task_context()
    assert not context.empty
    assert context.undelivered == (entry,)
    assert context.in_flight == ()
    assert context.text == (
        f"{ANSWER_TASK_CONTEXT_HEADER}\n"
        "The google calendar task has finished. Its actual result: "
        "You have 3 events this week.\n"
        f"{ANSWER_TASK_CONTEXT_RULE}"
    )
    await coordinator.aclose()


async def test_answer_task_context_undelivered_never_goes_stale() -> None:
    """Same no-staleness stance as the status render: the registry copy is the
    only true answer the session holds, however old."""
    now = [100.0]
    coordinator = _status_coordinator(now)
    queued = await coordinator.begin(_spec(kind="google-calendar"))
    assert queued is not None
    coordinator.note_task_settled(queued.task_id, status="done", result_text="3 events")
    now[0] = 100.0 + STATUS_RECENT_SETTLE_S * 10
    context = coordinator.answer_task_context()
    assert len(context.undelivered) == 1
    assert "3 events" in context.text
    await coordinator.aclose()


async def test_answer_task_context_in_flight_named_with_duration() -> None:
    """The ack→settle blind window: a running task is named, timed, and
    explicitly result-less so the model cannot improvise an outcome."""
    now = [100.0]
    coordinator = _status_coordinator(now)
    queued = await coordinator.begin(_spec(kind="gmail.search"))
    assert queued is not None
    coordinator.note_task_running(queued.task_id)
    now[0] = 121.0
    context = coordinator.answer_task_context()
    assert context.undelivered == ()
    assert [entry.task_id for entry in context.in_flight] == [queued.task_id]
    assert (
        "The gmail search task is still running (about 20 seconds in); "
        "its result is not available yet." in context.text
    )
    await coordinator.aclose()


async def test_answer_task_context_queued_counts_as_in_flight() -> None:
    """``queued`` and ``running`` alike — the user does not care about claim
    mechanics (the status-render stance)."""
    now = [100.0]
    coordinator = _status_coordinator(now)
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None
    now[0] = 103.0
    context = coordinator.answer_task_context()
    assert [entry.task_id for entry in context.in_flight] == [queued.task_id]
    assert "just a few seconds in" in context.text
    await coordinator.aclose()


async def test_answer_task_context_excludes_failures_and_delivered() -> None:
    """Failures already spoke their trt.53 correction into the chat history;
    a delivered result rides the history as spoken text — neither is
    re-injected. Cancelled entries are likewise silent."""
    now = [100.0]
    coordinator = _status_coordinator(now)
    failed = await coordinator.begin(_spec(kind="web_search"))
    delivered = await coordinator.begin(_spec(kind="google-calendar"))
    cancelled = await coordinator.begin(_spec(kind="gmail.search"))
    assert failed is not None and delivered is not None and cancelled is not None
    coordinator.note_task_settled(failed.task_id, status="failed", result_text="No luck.")
    coordinator.note_task_settled(
        delivered.task_id, status="done", result_text="You have 3 events this week."
    )
    coordinator.mark_result_delivered(delivered.task_id)
    coordinator.note_task_settled(cancelled.task_id, status="cancelled")
    context = coordinator.answer_task_context()
    assert context.empty
    assert context.text == ""
    await coordinator.aclose()


async def test_answer_task_context_blank_result_done_not_included() -> None:
    """A done task with nothing speakable (blank result_text, the UI-only
    contract) gives the answer model nothing to report."""
    coordinator = _status_coordinator([100.0])
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None
    coordinator.note_task_settled(queued.task_id, status="done", result_text="   ")
    context = coordinator.answer_task_context()
    assert context.empty
    await coordinator.aclose()


async def test_answer_task_context_result_text_gets_sentence_punctuation() -> None:
    coordinator = _status_coordinator([100.0])
    queued = await coordinator.begin(_spec(kind="google-calendar"))
    assert queued is not None
    coordinator.note_task_settled(queued.task_id, status="done", result_text="3 events")
    context = coordinator.answer_task_context()
    assert "Its actual result: 3 events." in context.text
    await coordinator.aclose()


async def test_answer_task_context_composes_finished_before_in_flight() -> None:
    now = [100.0]
    coordinator = _status_coordinator(now)
    running = await coordinator.begin(_spec(kind="gmail.search"))
    done = await coordinator.begin(_spec(kind="google-calendar"))
    assert running is not None and done is not None
    coordinator.note_task_settled(
        done.task_id, status="done", result_text="You have 3 events this week."
    )
    now[0] = 121.0
    context = coordinator.answer_task_context()
    lines = context.text.split("\n")
    assert lines[0] == ANSWER_TASK_CONTEXT_HEADER
    assert lines[1].startswith("The google calendar task has finished.")
    assert lines[2].startswith("The gmail search task is still running")
    assert lines[3] == ANSWER_TASK_CONTEXT_RULE
    assert len(lines) == 4
    await coordinator.aclose()


async def test_answer_task_context_injectable_now() -> None:
    """``now`` overrides the coordinator clock (the status_summary contract)."""
    now = [100.0]
    coordinator = _status_coordinator(now)
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None
    context = coordinator.answer_task_context(now=121.0)
    assert "about 20 seconds in" in context.text
    await coordinator.aclose()


# --- US-203: durable-overlay registry seeding --------------------------------


class _OverlaySink(InMemoryTaskSink):
    """InMemoryTaskSink that also serves a fixed durable workstream overlay."""

    def __init__(self, rows: list[WorkstreamOverlayRow]) -> None:
        super().__init__()
        self._overlay_rows = rows

    async def load_workstream_overlay(self) -> list[WorkstreamOverlayRow]:
        return list(self._overlay_rows)


class _BoomOverlaySink(InMemoryTaskSink):
    async def load_workstream_overlay(self) -> list[WorkstreamOverlayRow]:
        raise RuntimeError("overlay read failed")


def _overlay_row(**overrides: Any) -> WorkstreamOverlayRow:
    fields: dict[str, Any] = {
        "task_id": 1,
        "kind": "google-calendar",
        "status": "running",
        "result_text": "",
        "error": "",
        "delivered": False,
        "age_seconds": 20.0,
        "settled_age_seconds": None,
    }
    fields.update(overrides)
    return WorkstreamOverlayRow(**fields)


async def test_seed_registry_from_overlay_rebuilds_inflight_and_undelivered() -> None:
    """US-203: a fresh (respawned) coordinator rebuilds in-flight + done-but-
    undelivered work from the durable overlay so a status query reflects it
    instead of speaking the empty-registry line — the same source the column
    reads."""
    now = [1000.0]
    sink = _OverlaySink(
        [
            _overlay_row(task_id=7, kind="web_search", status="running", age_seconds=20.0),
            _overlay_row(
                task_id=8,
                kind="google-calendar",
                status="done",
                result_text="42 tons of CO2",
                delivered=False,
                settled_age_seconds=5.0,
            ),
        ]
    )
    coordinator, _ = _registry_coordinator(sink, now=now)

    seeded = await coordinator.seed_registry_from_overlay()

    assert {e.task_id for e in seeded} == {7, 8}
    assert {e.task_id for e in coordinator.registry_snapshot()} == {7, 8}
    summary = coordinator.status_summary()
    assert summary.text != STATUS_NOTHING_IN_FLIGHT
    assert "The google calendar task is done: 42 tons of CO2." in summary.text
    assert "Still working on the web search task, about 20 seconds in." in summary.text
    assert [e.task_id for e in summary.carried_results] == [8]
    await coordinator.aclose()


async def test_seed_registry_from_overlay_preserves_elapsed_minutes_band() -> None:
    """Wall-clock age from the overlay is re-derived against the monotonic clock,
    so the spoken duration survives the respawn (185 s → minutes band)."""
    now = [1000.0]
    sink = _OverlaySink(
        [_overlay_row(task_id=3, kind="web_search", status="running", age_seconds=185.0)]
    )
    coordinator, _ = _registry_coordinator(sink, now=now)
    await coordinator.seed_registry_from_overlay()
    assert "about 3 minutes in" in coordinator.status_summary().text
    await coordinator.aclose()


async def test_seed_registry_from_overlay_suppresses_delivered_and_expired() -> None:
    """Already-delivered and expired results are never re-spoken as undelivered:
    carried_results stays empty and their text is not voiced verbatim."""
    now = [1000.0]
    sink = _OverlaySink(
        [
            _overlay_row(
                task_id=1,
                kind="web_search",
                status="done",
                result_text="delivered already",
                delivered=True,
                settled_age_seconds=5.0,
            ),
            _overlay_row(
                task_id=2,
                kind="google-calendar",
                status="done",
                result_text="aged out",
                delivered=True,
                settled_age_seconds=5.0,
            ),
        ]
    )
    coordinator, _ = _registry_coordinator(sink, now=now)
    await coordinator.seed_registry_from_overlay()
    summary = coordinator.status_summary()
    assert summary.carried_results == ()
    assert "delivered already" not in summary.text
    assert "aged out" not in summary.text
    await coordinator.aclose()


async def test_seed_registry_from_overlay_first_observer_wins() -> None:
    """A live in-process entry is never clobbered by the overlay, so re-seeding
    on every resubscribe is idempotent."""
    now = [1000.0]
    sink = _OverlaySink(
        [_overlay_row(task_id=1, kind="web_search", status="done", result_text="stale")]
    )
    coordinator, _ = _registry_coordinator(sink, now=now)
    coordinator.attach_remote_listener()  # begin() then spawns no poll watcher
    queued = await coordinator.begin(_spec(kind="web_search"))
    assert queued is not None and queued.task_id == 1

    seeded = await coordinator.seed_registry_from_overlay()

    assert seeded == []  # id already present — skipped
    entry = coordinator.registry_entry(1)
    assert entry is not None and entry.status == "queued"  # the live entry stands
    await coordinator.aclose()


async def test_seed_registry_from_overlay_contained_on_sink_failure() -> None:
    """A sink read failure leaves the registry untouched and never raises."""
    now = [1000.0]
    coordinator, _ = _registry_coordinator(_BoomOverlaySink(), now=now)
    seeded = await coordinator.seed_registry_from_overlay()
    assert seeded == []
    assert coordinator.registry_snapshot() == ()
    assert coordinator.status_summary().text == STATUS_NOTHING_IN_FLIGHT
    await coordinator.aclose()


# --- US-302: cancel_task (Johnny-d6w.17) -------------------------------------


async def test_cancel_task_in_session_cuts_execution_and_announces() -> None:
    """An in-session cancel cuts the running resolver, settles the row
    ``cancelled`` (not failed), and announces exactly one ``TaskCancelled``
    carrying the requesting actor (US-302, AC1)."""
    sink = InMemoryTaskSink()
    started = asyncio.Event()
    cancelled: list[tuple[int, str]] = []
    completed: list[Any] = []

    async def executor(queued: QueuedTask) -> TaskResult:
        started.set()
        await asyncio.Event().wait()  # block until cancelled
        return TaskResult(status="done")  # pragma: no cover - unreachable

    async def publish_cancelled(queued: QueuedTask, actor: str) -> None:
        cancelled.append((queued.task_id, actor))

    async def publish_completed(queued: QueuedTask, status: Any, result: TaskResult) -> None:
        completed.append((queued.task_id, status))

    coordinator = TaskCoordinator(
        sink,
        executor=executor,
        publish_cancelled=publish_cancelled,
        publish_completed=publish_completed,
    )
    queued = await coordinator.begin(_spec())
    assert queued is not None
    await started.wait()  # the resolver is mid-executor (running)

    outcome = await coordinator.cancel_task(queued.task_id, actor="voice")
    assert outcome == "cancelling"
    await coordinator.join()  # let the cancelled resolver settle

    record = sink.get(queued.task_id)
    assert record is not None and record.status == "cancelled"
    entry = coordinator.registry_entry(queued.task_id)
    assert entry is not None and entry.status == "cancelled"
    # cancel is not a completion and not a failure
    assert cancelled == [(queued.task_id, "voice")]
    assert completed == []


async def test_cancel_task_worker_owned_signals_worker() -> None:
    """A worker-owned cancel publishes the worker cut-signal (the coordinator
    owns no row to cut directly) and returns ``requested`` (US-302, AC1)."""
    sink = InMemoryTaskSink()
    signals: list[int] = []

    async def request_worker_cancel(task_id: int) -> None:
        signals.append(task_id)

    coordinator = TaskCoordinator(
        sink,
        executor=stub_executor,
        request_worker_cancel=request_worker_cancel,
        runs_in_session=lambda kind: False,  # worker-owned locality
    )
    coordinator.attach_remote_listener()  # no poll watcher in the test
    queued = await coordinator.begin(_spec())
    assert queued is not None

    outcome = await coordinator.cancel_task(queued.task_id, actor="ui")
    assert outcome == "requested"
    assert signals == [queued.task_id]
    entry = coordinator.registry_entry(queued.task_id)
    assert entry is not None and entry.origin == "worker" and not entry.terminal
    await coordinator.aclose()


async def test_cancel_task_idempotent_on_terminal_and_unknown() -> None:
    """Cancelling an unknown id or an already-settled task is a safe no-op
    (US-302): the engine command never double-settles."""
    sink = InMemoryTaskSink()
    coordinator = TaskCoordinator(sink, executor=stub_executor)
    assert await coordinator.cancel_task(999, actor="ui") == "unknown"

    queued = await coordinator.begin(_spec())  # stub_executor fails fast → terminal
    assert queued is not None
    await coordinator.join()
    assert await coordinator.cancel_task(queued.task_id, actor="ui") == "already_settled"


async def test_cancel_task_in_session_without_worker_seam_is_contained() -> None:
    """A worker-owned cancel with no seam wired logs + no-ops, never raising
    (the in-process / harness assembly has no Redis)."""
    sink = InMemoryTaskSink()
    coordinator = TaskCoordinator(
        sink, executor=stub_executor, runs_in_session=lambda kind: False
    )
    coordinator.attach_remote_listener()
    queued = await coordinator.begin(_spec())
    assert queued is not None
    # request_worker_cancel is None → contained no-op, still returns requested
    assert await coordinator.cancel_task(queued.task_id, actor="ui") == "requested"
    await coordinator.aclose()
