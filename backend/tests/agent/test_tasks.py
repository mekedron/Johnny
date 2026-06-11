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

    await coordinator.aclose()

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
