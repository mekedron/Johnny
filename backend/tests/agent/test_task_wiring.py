"""Production wiring for delegated tasks (Johnny-trt.18, events Johnny-trt.25).

Covers the real seams :mod:`johnny.agent.task_wiring` supplies to the
stdlib-only coordinator: the ``TaskQueued`` / ``TaskCompleted`` publishes on
the session EventBus channel, the Redis wake ping on ``johnny.tasks.wake``,
the :func:`build_task_coordinator` assembly (stub executor default, no-redis
degrade), and the status-vocabulary drift guards against the DB enum and the
job-session delegation-mode set.
"""

from __future__ import annotations

import json
from typing import Any, get_args

from johnny.agent.task_wiring import (
    TASKS_WAKE_CHANNEL,
    RedisTaskWake,
    build_publish_task_completed,
    build_publish_task_queued,
    build_task_coordinator,
)
from johnny.agent.tasks import (
    InMemoryTaskSink,
    QueuedTask,
    TaskResult,
    TaskSpec,
    TaskStatus,
    unsupported_kind_text,
)
from johnny.voice_pipeline.event_bus import InMemoryEventBus
from johnny.voice_pipeline.events import TaskCompleted, TaskQueued

# --- helpers -----------------------------------------------------------------


def _queued(**spec_overrides: Any) -> QueuedTask:
    fields: dict[str, Any] = {
        "kind": "web_search",
        "args": {"query": "weather"},
        "ack_text": "on it",
        "turn_id": 4,
        "decision_id": 17,
    }
    fields.update(spec_overrides)
    return QueuedTask(task_id=42, spec=TaskSpec(**fields))


class _FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self.closed = False

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1

    async def aclose(self) -> None:
        self.closed = True


# --- build_publish_task_queued -------------------------------------------------


async def test_publish_task_queued_emits_event_on_bus() -> None:
    bus = InMemoryEventBus()
    publish = build_publish_task_queued(bus, session_id="7", clock=lambda: 1234)

    await publish(_queued())

    events = bus.snapshot()
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, TaskQueued)
    assert event.type == "task_queued"
    assert event.task_id == 42
    assert event.kind == "web_search"
    assert event.turn_id == 4
    assert event.decision_id == 17
    assert event.ack_text == "on it"
    assert event.session_id == "7"
    assert event.timestamp_ms == 1234


async def test_publish_task_queued_none_correlation_fields() -> None:
    bus = InMemoryEventBus()
    publish = build_publish_task_queued(bus, clock=lambda: 1)
    await publish(_queued(turn_id=None, decision_id=None, ack_text=""))
    event = bus.snapshot()[0]
    assert isinstance(event, TaskQueued)
    assert event.turn_id is None
    assert event.decision_id is None
    assert event.ack_text == ""
    assert event.session_id is None


# --- build_publish_task_completed (Johnny-trt.25) ------------------------------


async def test_publish_task_completed_emits_event_on_bus() -> None:
    bus = InMemoryEventBus()
    publish = build_publish_task_completed(bus, session_id="7", clock=lambda: 5678)

    await publish(
        _queued(),
        "done",
        TaskResult(status="done", result_text="3 events this week", error=""),
    )

    events = bus.snapshot()
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, TaskCompleted)
    assert event.type == "task_completed"
    assert event.task_id == 42
    assert event.kind == "web_search"
    assert event.status == "done"
    assert event.result_text == "3 events this week"
    assert event.error == ""
    assert event.turn_id == 4
    assert event.session_id == "7"
    assert event.timestamp_ms == 5678


async def test_publish_task_completed_failed_carries_error_detail() -> None:
    bus = InMemoryEventBus()
    publish = build_publish_task_completed(bus, clock=lambda: 1)
    await publish(
        _queued(turn_id=None),
        "failed",
        TaskResult(status="failed", result_text="that didn't work", error="boom"),
    )
    event = bus.snapshot()[0]
    assert isinstance(event, TaskCompleted)
    assert event.status == "failed"
    assert event.result_text == "that didn't work"
    assert event.error == "boom"
    assert event.turn_id is None
    assert event.session_id is None


async def test_publish_task_completed_refuses_non_settle_statuses() -> None:
    """Defensive: the PublishCompleted contract is done/failed only — anything
    else is dropped with a log, never published as a malformed event."""
    bus = InMemoryEventBus()
    publish = build_publish_task_completed(bus, clock=lambda: 1)
    await publish(_queued(), "cancelled", TaskResult(status="failed"))
    assert bus.snapshot() == []


# --- RedisTaskWake ---------------------------------------------------------------


async def test_wake_publishes_json_on_shared_channel() -> None:
    client = _FakeRedis()
    wake = RedisTaskWake(redis_url="redis://unused:6379/0", session_id="7", client=client)

    await wake(_queued())

    assert len(client.published) == 1
    channel, payload = client.published[0]
    assert channel == TASKS_WAKE_CHANNEL == "johnny.tasks.wake"
    assert json.loads(payload) == {"task_id": 42, "kind": "web_search", "session_id": "7"}


async def test_wake_close_releases_client_and_is_safe_when_never_connected() -> None:
    client = _FakeRedis()
    wake = RedisTaskWake(redis_url="redis://unused:6379/0", client=client)
    await wake(_queued())
    await wake.close()
    assert client.closed is True

    # Never connected: close is a no-op, not an error.
    never_used = RedisTaskWake(redis_url="redis://unused:6379/0")
    await never_used.close()


# --- build_task_coordinator -------------------------------------------------------


async def test_build_task_coordinator_defaults_to_stub_executor() -> None:
    sink = InMemoryTaskSink()
    bus = InMemoryEventBus()
    coordinator, wake = build_task_coordinator(
        task_sink=sink, event_bus=bus, session_id="7", redis_url=None
    )
    assert wake is None  # no redis -> no wake ping, everything else still works

    queued = await coordinator.begin(TaskSpec(kind="book_flight"))
    assert queued is not None
    # Row exists (queued) the moment begin returns — before any ack would play.
    record = sink.get(queued.task_id)
    assert record is not None and record.status == "queued"
    # TaskQueued went out on the session bus.
    events = bus.snapshot()
    assert len(events) == 1 and isinstance(events[0], TaskQueued)
    assert events[0].session_id == "7"

    await coordinator.join()
    record = sink.get(queued.task_id)
    assert record is not None
    assert record.status == "failed"
    assert record.result_text == unsupported_kind_text("book_flight")
    # The settle announced a TaskCompleted on the same bus (Johnny-trt.25),
    # mirroring the row state the resolver had already written.
    events = bus.snapshot()
    assert len(events) == 2 and isinstance(events[1], TaskCompleted)
    assert events[1].task_id == queued.task_id
    assert events[1].status == "failed"
    assert events[1].result_text == unsupported_kind_text("book_flight")
    assert events[1].session_id == "7"
    await coordinator.aclose()


async def test_build_task_coordinator_wires_wake_when_redis_url_set() -> None:
    sink = InMemoryTaskSink()
    bus = InMemoryEventBus()
    coordinator, wake = build_task_coordinator(
        task_sink=sink,
        event_bus=bus,
        session_id="7",
        redis_url="redis://unused:6379/0",
    )
    assert isinstance(wake, RedisTaskWake)
    # Inject the fake client so begin()'s ping never dials a real Redis.
    client = _FakeRedis()
    wake._client = client

    queued = await coordinator.begin(TaskSpec(kind="web_search"))
    assert queued is not None
    assert client.published and client.published[0][0] == TASKS_WAKE_CHANNEL
    await coordinator.join()
    await coordinator.aclose()
    await wake.close()
    assert client.closed is True


# --- drift guards -----------------------------------------------------------------


def test_task_status_literal_matches_db_enum() -> None:
    """johnny.agent.tasks.TaskStatus (stdlib mirror) ≡ app.db.models.AgentTaskStatus."""
    from app.db.models import AgentTaskStatus

    literal_values = set(get_args(TaskStatus))
    enum_values = {member.value for member in AgentTaskStatus}
    assert literal_values == enum_values


def test_delegation_capable_modes_match_speaking_modes() -> None:
    """job_session's delegation gate ≡ the canonical SPEAKING_MODES set."""
    import pytest

    pytest.importorskip("livekit.agents")
    from johnny.agent.job_session import DELEGATION_CAPABLE_MODES
    from johnny.voice_pipeline.reasoning import SPEAKING_MODES

    assert DELEGATION_CAPABLE_MODES == SPEAKING_MODES


def test_wake_channel_is_global_not_session_scoped() -> None:
    # One shared channel for all sessions (a Phase-4 worker subscribes once);
    # the payload carries the session id instead.
    assert "{" not in TASKS_WAKE_CHANNEL
    assert TASKS_WAKE_CHANNEL.startswith("johnny.tasks.")
