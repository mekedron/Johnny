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
