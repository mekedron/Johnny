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
    EXECUTOR_RESULT_STATUSES,
    TERMINAL_TASK_STATUSES,
    InMemoryTaskSink,
    QueuedTask,
    TaskCoordinator,
    TaskResult,
    TaskSpec,
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

    async def record_queued(self, spec: TaskSpec) -> int | None:
        task_id = await super().record_queued(spec)
        self._events.append(f"record_queued:{task_id}")
        return task_id

    async def update_status(self, task_id: int, status: Any, **fields: Any) -> None:
        await super().update_status(task_id, status, **fields)
        self._events.append(f"update:{task_id}:{status}")


class _FailingSink(InMemoryTaskSink):
    async def record_queued(self, spec: TaskSpec) -> int | None:
        raise RuntimeError("db down")


class _NoIdSink(InMemoryTaskSink):
    async def record_queued(self, spec: TaskSpec) -> int | None:
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
