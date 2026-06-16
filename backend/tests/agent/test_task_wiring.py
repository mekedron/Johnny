"""Production wiring for delegated tasks (Johnny-trt.18, events Johnny-trt.25).

Covers the real seams :mod:`johnny.agent.task_wiring` supplies to the
stdlib-only coordinator: the ``TaskQueued`` / ``TaskCompleted`` publishes on
the session EventBus channel, the Redis wake ping on ``johnny.tasks.wake``,
the :func:`build_task_coordinator` assembly (stub executor default, no-redis
degrade), and the status-vocabulary drift guards against the DB enum and the
job-session delegation-mode set.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any, get_args

from johnny.agent.speech_queue import ItemState, SpeechPriority, SpeechQueue
from johnny.agent.task_wiring import (
    TASKS_WAKE_CHANNEL,
    RedisTaskWake,
    TaskEventListener,
    TaskSpeechDeliverer,
    TaskSpeechWiring,
    attach_task_speech_wiring,
    build_publish_task_completed,
    build_publish_task_queued,
    build_task_coordinator,
)
from johnny.agent.tasks import (
    InMemoryTaskSink,
    QueuedTask,
    TaskCoordinator,
    TaskRegistryEntry,
    TaskResult,
    TaskSpec,
    TaskStatus,
    WorkstreamOverlayRow,
    stub_executor,
    unsupported_kind_text,
)
from johnny.voice_pipeline.event_bus import InMemoryEventBus
from johnny.voice_pipeline.events import TaskCompleted, TaskQueued, TaskResultExpired

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
    """An in-session kind with no real executor still fails fast in-process.

    Since Johnny-trt.24 the production default routes only internal kinds to
    the in-process resolver — ``session.end`` exercises that leg (the stub
    settles it, proving the default executor wiring)."""
    sink = InMemoryTaskSink()
    bus = InMemoryEventBus()
    coordinator, wake = build_task_coordinator(
        task_sink=sink, event_bus=bus, session_id="7", redis_url=None
    )
    assert wake is None  # no redis -> no wake ping, everything else still works

    queued = await coordinator.begin(TaskSpec(kind="session.end"))
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
    assert record.result_text == unsupported_kind_text("session.end")
    # The settle announced a TaskCompleted on the same bus (Johnny-trt.25),
    # mirroring the row state the resolver had already written.
    events = bus.snapshot()
    assert len(events) == 2 and isinstance(events[1], TaskCompleted)
    assert events[1].task_id == queued.task_id
    assert events[1].status == "failed"
    assert events[1].result_text == unsupported_kind_text("session.end")
    assert events[1].session_id == "7"
    await coordinator.aclose()


async def test_build_task_coordinator_routes_skill_kinds_to_the_worker() -> None:
    """The Johnny-trt.24 default split: a non-internal kind stays queued
    (worker-owned — no in-process execution, no session TaskCompleted), and
    the watcher reports a worker-side failed settle through the trt.53 seam."""
    sink = InMemoryTaskSink()
    bus = InMemoryEventBus()
    coordinator, _wake = build_task_coordinator(
        task_sink=sink, event_bus=bus, session_id="7", redis_url=None
    )
    coordinator._watch_poll_interval_s = 0.01  # test-speed polling
    reported: list[str] = []

    async def report(queued: QueuedTask, result: TaskResult) -> None:
        del queued
        reported.append(result.result_text)

    coordinator.attach_failure_reporter(report)

    queued = await coordinator.begin(TaskSpec(kind="calendar.upcoming_events"))
    assert queued is not None
    await asyncio.sleep(0.05)
    record = sink.get(queued.task_id)
    assert record is not None and record.status == "queued"  # nobody ran it here

    # Simulate the worker pass settling the row failed out of process.
    await sink.update_status(
        queued.task_id, "failed", result_text="The sandbox is unreachable."
    )
    await coordinator.join()
    assert reported == ["The sandbox is unreachable."]
    # Only the TaskQueued event came from the session — TaskCompleted is the
    # worker's announce (trt.25: whoever settles, announces).
    events = bus.snapshot()
    assert len(events) == 1 and isinstance(events[0], TaskQueued)
    await coordinator.aclose()


async def test_build_task_coordinator_accepts_runs_in_session_override() -> None:
    """Harnesses that deliberately run everything in-process keep working."""
    sink = InMemoryTaskSink()
    coordinator, _wake = build_task_coordinator(
        task_sink=sink,
        event_bus=InMemoryEventBus(),
        session_id="7",
        redis_url=None,
        runs_in_session=lambda kind: True,
    )
    queued = await coordinator.begin(TaskSpec(kind="book_flight"))
    assert queued is not None
    await coordinator.join()
    record = sink.get(queued.task_id)
    assert record is not None
    assert record.status == "failed"  # the stub ran in-process
    assert record.result_text == unsupported_kind_text("book_flight")
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
    # web_search is worker-owned (trt.24): no join — the row never settles in
    # this test, so aclose() simply cancels the read-only watcher.
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


# --- Phase-5 speech wiring (Johnny-trt.28) -------------------------------------
#
# TaskEventListener: the per-session push consumer of johnny.tasks.<id> —
# registry updates, exactly-once settle effects, reconcile on (re)subscribe,
# loud degrade on connection loss. TaskSpeechDeliverer: the gating predicate
# matrix and the deliver/interrupt/expiry flows over a real SpeechQueue with a
# fake session/gate. attach_task_speech_wiring: the assembly contract.


async def _wait_until(predicate: Any, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


def _external_coordinator(
    sink: InMemoryTaskSink | None = None, **kwargs: Any
) -> tuple[TaskCoordinator, InMemoryTaskSink]:
    sink = sink if sink is not None else InMemoryTaskSink()
    kwargs.setdefault("executor", stub_executor)
    kwargs.setdefault("runs_in_session", lambda kind: False)
    kwargs.setdefault("watch_poll_interval_s", 0.01)
    kwargs.setdefault("watch_timeout_s", 2.0)
    return TaskCoordinator(sink, **kwargs), sink


def _entry(task_id: int = 42, **overrides: Any) -> TaskRegistryEntry:
    fields: dict[str, Any] = {
        "task_id": task_id,
        "kind": "calendar.check",
        "origin": "worker",
        "queued_at": 0.0,
        "ack_text": "on it",
        "turn_id": 7,
        "status": "done",
        "result_text": "You have 3 events this week.",
    }
    fields.update(overrides)
    status = fields.pop("status")
    result_text = fields.pop("result_text")
    entry = TaskRegistryEntry(**fields)
    entry.status = status
    entry.result_text = result_text
    return entry


# ---- TaskEventListener fakes ---------------------------------------------------


class _FakePubSub:
    def __init__(self, frames: asyncio.Queue[dict[str, Any]]) -> None:
        self._frames = frames
        self.subscribed: list[str] = []

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def get_message(
        self, *, ignore_subscribe_messages: bool = True, timeout: float = 1.0
    ) -> dict[str, Any] | None:
        del ignore_subscribe_messages
        try:
            return await asyncio.wait_for(self._frames.get(), timeout=min(timeout, 0.05))
        except TimeoutError:
            return None


class _FakeListenerClient:
    def __init__(self, frames: asyncio.Queue[dict[str, Any]]) -> None:
        self._frames = frames
        self.closed = False
        self.pubsubs: list[_FakePubSub] = []

    def pubsub(self, ignore_subscribe_messages: bool = True) -> _FakePubSub:
        del ignore_subscribe_messages
        ps = _FakePubSub(self._frames)
        self.pubsubs.append(ps)
        return ps

    async def aclose(self) -> None:
        self.closed = True


class _BoomClient:
    """A client whose subscription drops immediately (connect-level failure)."""

    def __init__(self) -> None:
        self.closed = False

    def pubsub(self, ignore_subscribe_messages: bool = True) -> Any:
        del ignore_subscribe_messages
        raise RuntimeError("redis down")

    async def aclose(self) -> None:
        self.closed = True


def _frame(payload: dict[str, Any]) -> dict[str, Any]:
    return {"type": "message", "data": json.dumps(payload).encode()}


def _completed_payload(task_id: int, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "task_completed",
        "task_id": task_id,
        "kind": "calendar.check",
        "status": "done",
        "timestamp_ms": 1,
        "result_text": "You have 3 events this week.",
        "error": "",
        "turn_id": 7,
        "session_id": "7",
    }
    payload.update(overrides)
    return payload


class _SettleRecorder:
    def __init__(self) -> None:
        self.entries: list[TaskRegistryEntry] = []

    async def __call__(self, entry: TaskRegistryEntry) -> None:
        self.entries.append(entry)


def _listener(
    coordinator: TaskCoordinator,
    on_settled: Any,
    *,
    frames: asyncio.Queue[dict[str, Any]] | None = None,
    factory: Any = None,
) -> tuple[TaskEventListener, asyncio.Queue[dict[str, Any]]]:
    frames = frames if frames is not None else asyncio.Queue()
    clients: list[_FakeListenerClient] = []

    def _default_factory() -> _FakeListenerClient:
        client = _FakeListenerClient(frames)
        clients.append(client)
        return client

    listener = TaskEventListener(
        redis_url="redis://unused",
        session_id="7",
        coordinator=coordinator,
        on_settled=on_settled,
        client_factory=factory if factory is not None else _default_factory,
        reconnect_backoff_s=0.01,
    )
    return listener, frames


async def test_listener_subscribes_attaches_and_settles_done_frames() -> None:
    coordinator, _sink = _external_coordinator()
    queued = await coordinator.begin(TaskSpec(kind="calendar.check", ack_text="on it"))
    assert queued is not None
    recorder = _SettleRecorder()
    listener, frames = _listener(coordinator, recorder)
    listener.start()
    await _wait_until(lambda: coordinator.remote_listener_active)
    await frames.put(_frame(_completed_payload(queued.task_id)))
    await _wait_until(lambda: len(recorder.entries) == 1)
    entry = recorder.entries[0]
    assert entry.task_id == queued.task_id
    assert entry.status == "done"
    assert entry.result_text == "You have 3 events this week."
    registry = coordinator.registry_entry(queued.task_id)
    assert registry is not None and registry.terminal
    # A duplicate frame loses the first-wins race: no second settle effect.
    await frames.put(_frame(_completed_payload(queued.task_id)))
    await asyncio.sleep(0.1)
    assert len(recorder.entries) == 1
    await listener.aclose()
    assert coordinator.remote_listener_active is False
    await coordinator.aclose()


async def test_listener_progress_frame_marks_running_and_malformed_is_survived() -> None:
    coordinator, _sink = _external_coordinator()
    queued = await coordinator.begin(TaskSpec(kind="calendar.check"))
    assert queued is not None
    recorder = _SettleRecorder()
    listener, frames = _listener(coordinator, recorder)
    listener.start()
    await _wait_until(lambda: coordinator.remote_listener_active)
    await frames.put({"type": "message", "data": b"not json"})
    await frames.put(_frame({"type": "task_completed", "task_id": "NaN"}))
    await frames.put(_frame({"type": "something_else", "task_id": 1}))
    await frames.put(_frame(_completed_payload(queued.task_id, status="weird")))
    await frames.put(
        _frame(
            {
                "type": "task_progress",
                "task_id": queued.task_id,
                "kind": "calendar.check",
                "timestamp_ms": 1,
                "progress_text": "",
                "turn_id": 7,
                "session_id": "7",
            }
        )
    )
    await _wait_until(
        lambda: (coordinator.registry_entry(queued.task_id) or _entry()).status == "running"
    )
    assert recorder.entries == []  # nothing settled by garbage
    await listener.aclose()
    await coordinator.aclose()


async def test_listener_failed_frame_hands_failed_entry_to_hook() -> None:
    coordinator, _sink = _external_coordinator()
    queued = await coordinator.begin(TaskSpec(kind="calendar.check"))
    assert queued is not None
    recorder = _SettleRecorder()
    listener, frames = _listener(coordinator, recorder)
    listener.start()
    await _wait_until(lambda: coordinator.remote_listener_active)
    await frames.put(
        _frame(
            _completed_payload(
                queued.task_id,
                status="failed",
                result_text="I couldn't reach the calendar.",
                error="dns",
            )
        )
    )
    await _wait_until(lambda: len(recorder.entries) == 1)
    assert recorder.entries[0].status == "failed"
    assert recorder.entries[0].error == "dns"
    await listener.aclose()
    await coordinator.aclose()


async def test_listener_reconciles_missed_settles_on_subscribe() -> None:
    """A settle published while the subscription was down is recovered.

    Models the real outage: the listener was live when the task began (so no
    poll watcher exists), the connection then dropped, and the worker settled
    the row while nobody was subscribed. The (re)subscribe reconcile re-reads
    the durable row and delivers the missed settle exactly once.
    """
    coordinator, sink = _external_coordinator()
    coordinator.attach_remote_listener()  # task begins under a live listener
    queued = await coordinator.begin(TaskSpec(kind="calendar.check"))
    assert queued is not None
    # The settle lands while no subscription is live (the outage window).
    await sink.update_status(
        queued.task_id, "done", result_text="found it", error=None
    )
    recorder = _SettleRecorder()
    listener, _frames = _listener(coordinator, recorder)
    listener.start()
    await _wait_until(lambda: len(recorder.entries) >= 1)
    assert recorder.entries[0].task_id == queued.task_id
    assert recorder.entries[0].result_text == "found it"
    # Re-running the reconcile finds nothing new (first-wins held).
    assert await coordinator.reconcile_in_flight() == []
    await listener.aclose()
    await coordinator.aclose()


async def test_listener_drop_detaches_then_resubscribe_reattaches() -> None:
    coordinator, _sink = _external_coordinator()
    recorder = _SettleRecorder()
    frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    good = _FakeListenerClient(frames)
    handed: list[Any] = [_BoomClient(), good]

    def factory() -> Any:
        return handed.pop(0) if handed else good

    listener, _ = _listener(coordinator, recorder, frames=frames, factory=factory)
    listener.start()
    # First client boomed → detached; second subscribes → attached again.
    await _wait_until(lambda: coordinator.remote_listener_active)
    assert good.pubsubs and good.pubsubs[0].subscribed == ["johnny.tasks.7"]
    await listener.aclose()
    await coordinator.aclose()


# ---- TaskSpeechDeliverer fakes --------------------------------------------------


class _FakeDeliveryHandle:
    """An awaitable say() handle the test completes (clean or interrupted)."""

    def __init__(self) -> None:
        self._done = asyncio.Event()
        self.interrupted = False
        self._cbs: list[Any] = []

    def add_done_callback(self, cb: Any) -> None:
        self._cbs.append(cb)

    def done(self) -> bool:
        return self._done.is_set()

    def finish(self, *, interrupted: bool = False) -> None:
        self.interrupted = interrupted
        self._done.set()
        for cb in list(self._cbs):
            cb(self)

    def __await__(self) -> Any:
        async def _wait() -> _FakeDeliveryHandle:
            await self._done.wait()
            return self

        return _wait().__await__()


class _FakeGate:
    """Duck-typed RouterGate: an ``idle`` flag + recording speak_task_result."""

    def __init__(self) -> None:
        self.idle = True
        self.say_available = True
        self.spoken: list[str] = []
        self.handles: list[_FakeDeliveryHandle] = []
        self.attached_queues: list[SpeechQueue] = []

    def speak_task_result(self, text: str) -> _FakeDeliveryHandle | None:
        if not self.say_available:
            return None
        handle = _FakeDeliveryHandle()
        self.spoken.append(text)
        self.handles.append(handle)
        return handle

    def attach_speech_queue(self, queue: SpeechQueue, *, clock: Any) -> None:
        """Records the trt.29 consumption-seam attach (real-gate signature)."""
        del clock
        self.attached_queues.append(queue)


class _FakeDeliverySession:
    """Duck-typed AgentSession: current_speech + on/off event registration."""

    def __init__(self) -> None:
        self.current_speech: Any = None
        self.listeners: dict[str, list[Any]] = {}

    def on(self, event: str, cb: Any) -> None:
        self.listeners.setdefault(event, []).append(cb)

    def off(self, event: str, cb: Any) -> None:
        self.listeners.get(event, []).remove(cb)

    def emit_user_state(self, new_state: str) -> None:
        for cb in list(self.listeners.get("user_state_changed", [])):
            cb(SimpleNamespace(new_state=new_state))


def _deliverer(
    *,
    grace_s: float = 0.05,
    tick_s: float = 0.01,
    coordinator: TaskCoordinator | None = None,
    floor: Any = None,
) -> tuple[
    TaskSpeechDeliverer,
    SpeechQueue,
    _FakeGate,
    _FakeDeliverySession,
    InMemoryEventBus,
    TaskCoordinator,
]:
    if coordinator is None:
        coordinator, _ = _external_coordinator()
    queue = SpeechQueue(time.monotonic(), grace_s=grace_s)
    gate = _FakeGate()
    session = _FakeDeliverySession()
    bus = InMemoryEventBus()
    deliverer = TaskSpeechDeliverer(
        session=session,  # type: ignore[arg-type]
        gate=gate,  # type: ignore[arg-type]
        queue=queue,
        coordinator=coordinator,
        event_bus=bus,
        session_id="7",
        clock_ms=lambda: 99,
        tick_s=tick_s,
        floor=floor,
    )
    return deliverer, queue, gate, session, bus, coordinator


def _expired_events(bus: InMemoryEventBus) -> list[TaskResultExpired]:
    return [e for e in bus.snapshot() if isinstance(e, TaskResultExpired)]


# ---- the gating predicate matrix -------------------------------------------------


async def test_delivery_blocked_reason_matrix() -> None:
    deliverer, _queue, gate, session, _bus, coordinator = _deliverer()
    # All clear.
    assert deliverer.delivery_blocked_reason() is None
    # User speaking blocks (event-tracked).
    session.on("user_state_changed", lambda ev: None)  # unrelated listener is fine
    deliverer._on_user_state(SimpleNamespace(new_state="speaking"))
    assert deliverer.delivery_blocked_reason() == "user speaking"
    deliverer._on_user_state(SimpleNamespace(new_state="listening"))
    assert deliverer.delivery_blocked_reason() is None
    deliverer._on_user_state(SimpleNamespace(new_state="away"))
    assert deliverer.delivery_blocked_reason() is None
    # Bot speaking blocks while the handle is live, clears once done.
    handle = _FakeDeliveryHandle()
    session.current_speech = handle
    assert deliverer.delivery_blocked_reason() == "bot speaking"
    handle.finish()
    assert deliverer.delivery_blocked_reason() is None
    session.current_speech = None
    # Gate busy blocks (mid-decision turn / pending reply / parked approval).
    gate.idle = False
    assert deliverer.delivery_blocked_reason() == "gate busy"
    gate.idle = True
    assert deliverer.delivery_blocked_reason() is None
    await coordinator.aclose()


async def test_result_delivered_only_after_silence_grace() -> None:
    deliverer, _queue, gate, session, _bus, coordinator = _deliverer(grace_s=0.08)
    queued = await coordinator.begin(TaskSpec(kind="calendar.check"))
    assert queued is not None
    entry = coordinator.note_task_settled(
        queued.task_id, status="done", result_text="Three events this week."
    )
    assert entry is not None
    deliverer.start()
    try:
        # The user is mid-monologue: nothing may be spoken.
        session.emit_user_state("speaking")
        deliverer.enqueue_result(entry)
        await asyncio.sleep(0.3)
        assert gate.spoken == []
        # Silence: the grace runs, then the result is delivered.
        session.emit_user_state("listening")
        await _wait_until(lambda: gate.spoken == ["Three events this week."])
        gate.handles[0].finish()
        await _wait_until(
            lambda: (coordinator.registry_entry(queued.task_id) or entry).delivered
        )
    finally:
        await deliverer.aclose()
        await coordinator.aclose()


async def test_gate_busy_blocks_delivery_until_idle() -> None:
    deliverer, _queue, gate, _session, _bus, coordinator = _deliverer()
    queued = await coordinator.begin(TaskSpec(kind="calendar.check"))
    assert queued is not None
    entry = coordinator.note_task_settled(
        queued.task_id, status="done", result_text="The result."
    )
    assert entry is not None
    gate.idle = False
    deliverer.start()
    try:
        deliverer.enqueue_result(entry)
        await asyncio.sleep(0.25)
        assert gate.spoken == []
        gate.idle = True
        await _wait_until(lambda: gate.spoken == ["The result."])
        gate.handles[0].finish()
    finally:
        await deliverer.aclose()
        await coordinator.aclose()


async def test_interrupted_result_requeues_once_then_drops_with_expired_event() -> None:
    deliverer, queue, gate, _session, bus, coordinator = _deliverer()
    queued = await coordinator.begin(TaskSpec(kind="calendar.check", turn_id=7))
    assert queued is not None
    entry = coordinator.note_task_settled(
        queued.task_id, status="done", result_text="The result.", turn_id=7
    )
    assert entry is not None
    deliverer.start()
    try:
        item = deliverer.enqueue_result(entry)
        assert item is not None
        # First delivery: barge-in → re-queued at original seat.
        await _wait_until(lambda: len(gate.handles) == 1)
        gate.handles[0].finish(interrupted=True)
        await _wait_until(lambda: item.state is ItemState.QUEUED)
        assert _expired_events(bus) == []
        # Second delivery (after the bot's own falling edge restarts the
        # grace): interrupted again → dropped, TaskResultExpired published.
        await _wait_until(lambda: len(gate.handles) == 2)
        assert gate.spoken == ["The result.", "The result."]
        gate.handles[1].finish(interrupted=True)
        await _wait_until(lambda: item.state is ItemState.DROPPED)
        await _wait_until(lambda: len(_expired_events(bus)) == 1)
        event = _expired_events(bus)[0]
        assert event.task_id == queued.task_id
        assert event.kind == "calendar.check"
        assert event.reason == "interrupted twice"
        assert event.turn_id == 7
        assert event.session_id == "7"
        assert event.timestamp_ms == 99
        # The registry keeps delivered=False — the UI row is the surface.
        registry = coordinator.registry_entry(queued.task_id)
        assert registry is not None and registry.delivered is False
    finally:
        await deliverer.aclose()
        await coordinator.aclose()


async def test_result_expiring_while_blocked_fires_expired_event_promptly() -> None:
    """The per-tick sweep settles a gated-out RESULT mid-monologue, not later."""
    deliverer, _queue, gate, session, bus, coordinator = _deliverer()
    queued = await coordinator.begin(TaskSpec(kind="calendar.check"))
    assert queued is not None
    entry = coordinator.note_task_settled(
        queued.task_id, status="done", result_text="Stale soon."
    )
    assert entry is not None
    deliverer.start()
    try:
        session.emit_user_state("speaking")  # blocked the whole time
        item = deliverer.enqueue_result(entry)
        assert item is not None
        # Tighten the 120 s class TTL to test speed (queue-owned field, but a
        # test may compress time).
        item.expires_at = time.monotonic() + 0.05
        await _wait_until(lambda: item.state is ItemState.DROPPED)
        await _wait_until(lambda: len(_expired_events(bus)) == 1)
        assert _expired_events(bus)[0].reason.startswith("undelivered for")
        assert gate.spoken == []  # it never reached the mouth
    finally:
        await deliverer.aclose()
        await coordinator.aclose()


async def test_say_unavailable_degrades_to_ui_only_with_expired_event() -> None:
    deliverer, _queue, gate, _session, bus, coordinator = _deliverer()
    queued = await coordinator.begin(TaskSpec(kind="calendar.check"))
    assert queued is not None
    entry = coordinator.note_task_settled(
        queued.task_id, status="done", result_text="The result."
    )
    assert entry is not None
    gate.say_available = False
    deliverer.start()
    try:
        item = deliverer.enqueue_result(entry)
        assert item is not None
        await _wait_until(lambda: item.state is ItemState.DROPPED)
        assert item.drop_reason == "say() unavailable"
        await _wait_until(lambda: len(_expired_events(bus)) == 1)
        assert _expired_events(bus)[0].reason == "say() unavailable"
    finally:
        await deliverer.aclose()
        await coordinator.aclose()


async def test_blank_result_text_is_ui_only_and_never_enqueued() -> None:
    deliverer, queue, _gate, _session, _bus, coordinator = _deliverer()
    queued = await coordinator.begin(TaskSpec(kind="calendar.check"))
    assert queued is not None
    entry = coordinator.note_task_settled(queued.task_id, status="done", result_text="  ")
    assert entry is not None
    assert deliverer.enqueue_result(entry) is None
    assert len(queue) == 0
    await coordinator.aclose()


async def test_wiring_aclose_drops_undelivered_without_expired_events() -> None:
    deliverer, queue, _gate, session, bus, coordinator = _deliverer()
    queued = await coordinator.begin(TaskSpec(kind="calendar.check"))
    assert queued is not None
    entry = coordinator.note_task_settled(
        queued.task_id, status="done", result_text="Never spoken."
    )
    assert entry is not None
    deliverer.start()
    session.emit_user_state("speaking")  # keep it queued
    item = deliverer.enqueue_result(entry)
    assert item is not None
    wiring = TaskSpeechWiring(queue=queue, deliverer=deliverer, listener=None)
    await wiring.aclose()
    assert item.state is ItemState.DROPPED
    assert item.drop_reason == "queue closed"
    # Teardown drops publish no TaskResultExpired (the trt.25 contract).
    assert _expired_events(bus) == []
    assert queue.closed
    # Idempotent.
    await wiring.aclose()
    await coordinator.aclose()


# ---- attach_task_speech_wiring ---------------------------------------------------


def _runtime_stub(coordinator: TaskCoordinator | None, *, redis_url: str | None) -> Any:
    return SimpleNamespace(
        task_coordinator=coordinator,
        gate=_FakeGate(),
        event_bus=InMemoryEventBus(),
        session_id="7",
        config=SimpleNamespace(redis_url=redis_url),
        task_speech=None,
    )


async def test_attach_returns_none_without_a_coordinator() -> None:
    runtime = _runtime_stub(None, redis_url="redis://x")
    assert attach_task_speech_wiring(runtime, _FakeDeliverySession()) is None  # type: ignore[arg-type]
    assert runtime.task_speech is None


async def test_attach_without_redis_runs_listenerless_and_stores_wiring() -> None:
    coordinator, _ = _external_coordinator()
    runtime = _runtime_stub(coordinator, redis_url=None)
    session = _FakeDeliverySession()
    wiring = attach_task_speech_wiring(runtime, session)  # type: ignore[arg-type]
    assert wiring is not None
    assert wiring.listener is None
    assert runtime.task_speech is wiring
    assert coordinator.remote_listener_active is False  # watcher fallback intact
    assert session.listeners.get("user_state_changed")  # deliverer registered
    # The trt.29 consumption seam: the gate sees the same queue the deliverer
    # drains, so a status reply can consume a queued RESULT copy.
    assert runtime.gate.attached_queues == [wiring.queue]
    await wiring.aclose()
    assert session.listeners.get("user_state_changed") == []
    await coordinator.aclose()


async def test_attach_tolerates_a_gate_without_the_queue_seam() -> None:
    """The getattr duck-typing discipline: a harness gate modelling only
    idle/speak_task_result must not crash the attach (trt.29 seam optional)."""
    coordinator, _ = _external_coordinator()
    runtime = _runtime_stub(coordinator, redis_url=None)
    runtime.gate = SimpleNamespace(idle=True, speak_task_result=lambda text: None)
    wiring = attach_task_speech_wiring(runtime, _FakeDeliverySession())  # type: ignore[arg-type]
    assert wiring is not None
    await wiring.aclose()
    await coordinator.aclose()


async def test_attach_end_to_end_frame_to_spoken_result() -> None:
    """The acceptance integration: a worker frame becomes a gated spoken result."""
    coordinator, _sink = _external_coordinator()
    queued = await coordinator.begin(TaskSpec(kind="calendar.check", turn_id=4))
    assert queued is not None
    runtime = _runtime_stub(coordinator, redis_url="redis://stack")
    session = _FakeDeliverySession()
    frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    wiring = attach_task_speech_wiring(
        runtime,
        session,  # type: ignore[arg-type]
        grace_s=0.05,
        tick_s=0.01,
        listener_client_factory=lambda: _FakeListenerClient(frames),
    )
    assert wiring is not None and wiring.listener is not None
    try:
        await _wait_until(lambda: coordinator.remote_listener_active)
        await frames.put(_frame(_completed_payload(queued.task_id)))
        gate = runtime.gate
        await _wait_until(lambda: gate.spoken == ["You have 3 events this week."])
        gate.handles[0].finish()
        await _wait_until(
            lambda: (coordinator.registry_entry(queued.task_id) or _entry()).delivered
        )
    finally:
        await wiring.aclose()
        await coordinator.aclose()


async def test_attach_end_to_end_failed_frame_routes_correction_seam() -> None:
    coordinator, _sink = _external_coordinator()
    reported: list[tuple[QueuedTask, TaskResult]] = []

    async def reporter(q: QueuedTask, r: TaskResult) -> None:
        reported.append((q, r))

    coordinator.attach_failure_reporter(reporter)
    queued = await coordinator.begin(TaskSpec(kind="calendar.check"))
    assert queued is not None
    runtime = _runtime_stub(coordinator, redis_url="redis://stack")
    session = _FakeDeliverySession()
    frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    wiring = attach_task_speech_wiring(
        runtime,
        session,  # type: ignore[arg-type]
        listener_client_factory=lambda: _FakeListenerClient(frames),
    )
    assert wiring is not None
    try:
        await _wait_until(lambda: coordinator.remote_listener_active)
        await frames.put(
            _frame(
                _completed_payload(
                    queued.task_id,
                    status="failed",
                    result_text="The calendar tool is unavailable.",
                    error="exit 2",
                )
            )
        )
        await _wait_until(lambda: len(reported) == 1)
        assert reported[0][1].result_text == "The calendar tool is unavailable."
        # Failed settles never enter the speech queue.
        assert runtime.gate.spoken == []
        assert len(wiring.queue) == 0
    finally:
        await wiring.aclose()
        await coordinator.aclose()


# ---- the shared speech floor (Johnny-trt.46) --------------------------------------


class _FloorLeaseFake:
    def __init__(self, floor: _FloorFake, kind: str) -> None:
        self._floor = floor
        self._kind = kind

    async def release(self, *, reason: str, spoken_text: str = "") -> None:
        self._floor.releases.append((self._kind, reason, spoken_text))


class _FloorFake:
    """Duck-typed SpeechFloor for the deliverer: scripted acquire + busy flag."""

    def __init__(self, *, busy: bool = False, grants: list[bool] | None = None) -> None:
        self.busy = busy
        self._grants = grants
        self.acquires: list[tuple[str, float | None]] = []
        self.releases: list[tuple[str, str, str]] = []

    def peer_holds_floor(self) -> bool:
        return self.busy

    async def acquire(
        self, kind: str, *, timeout_s: float | None = None
    ) -> _FloorLeaseFake | None:
        self.acquires.append((kind, timeout_s))
        if self._grants:
            if not self._grants.pop(0):
                return None
        return _FloorLeaseFake(self, kind)


async def test_predicate_blocks_while_peer_holds_floor() -> None:
    floor = _FloorFake(busy=True)
    deliverer, _queue, _gate, _session, _bus, coordinator = _deliverer(floor=floor)
    assert deliverer.delivery_blocked_reason() == "peer agent holds the floor"
    floor.busy = False
    assert deliverer.delivery_blocked_reason() is None
    await coordinator.aclose()


async def test_deliver_wraps_playout_in_floor_lease() -> None:
    floor = _FloorFake()
    deliverer, queue, gate, _session, _bus, coordinator = _deliverer(floor=floor)
    now = time.monotonic()
    item = queue.enqueue("Three events this week.", SpeechPriority.RESULT_UNSOLICITED, now=now)
    popped = queue.pop_ready(now + 1.0)
    assert popped is item

    deliver = asyncio.ensure_future(deliverer._deliver(item))
    await asyncio.sleep(0.02)
    assert floor.acquires == [("task_result", 2.0)]  # FLOOR_DELIVERY_WAIT_S
    assert gate.spoken == ["Three events this week."]
    assert floor.releases == []  # held while the playout is in flight
    gate.handles[0].finish()
    await deliver

    assert floor.releases == [
        ("task_result", "completed", "Three events this week.")
    ]
    assert item.state.value == "spoken"
    await coordinator.aclose()


async def test_deliver_floor_race_restores_item_unblamed() -> None:
    floor = _FloorFake(grants=[False])
    deliverer, queue, gate, _session, bus, coordinator = _deliverer(floor=floor)
    now = time.monotonic()
    item = queue.enqueue("Result text.", SpeechPriority.RESULT_UNSOLICITED, now=now)
    assert queue.pop_ready(now + 1.0) is item

    await deliverer._deliver(item)

    # Nothing spoken, nothing dropped, no interruption blamed — the item sits
    # queued at its original seat for the next tick.
    assert gate.spoken == []
    assert item.state.value == "queued"
    assert item.interruptions == 0
    assert queue.pop_ready(now + 2.0) is item
    assert _expired_events(bus) == []
    await coordinator.aclose()


async def test_deliver_interrupted_releases_floor_interrupted() -> None:
    floor = _FloorFake()
    deliverer, queue, gate, _session, _bus, coordinator = _deliverer(floor=floor)
    now = time.monotonic()
    item = queue.enqueue("Cut me off.", SpeechPriority.RESULT_UNSOLICITED, now=now)
    assert queue.pop_ready(now + 1.0) is item

    deliver = asyncio.ensure_future(deliverer._deliver(item))
    await asyncio.sleep(0.02)
    gate.handles[0].finish(interrupted=True)
    await deliver

    assert floor.releases == [("task_result", "interrupted", "Cut me off.")]
    assert item.state.value == "queued"  # requeued within the budget
    assert item.interruptions == 1
    await coordinator.aclose()


async def test_deliver_say_unavailable_releases_floor() -> None:
    floor = _FloorFake()
    deliverer, queue, gate, _session, _bus, coordinator = _deliverer(floor=floor)
    gate.say_available = False
    now = time.monotonic()
    item = queue.enqueue("Never spoken.", SpeechPriority.RESULT_UNSOLICITED, now=now)
    assert queue.pop_ready(now + 1.0) is item

    await deliverer._deliver(item)

    assert floor.releases == [("task_result", "say_unavailable", "")]
    assert item.state.value == "dropped"
    await coordinator.aclose()


# --- US-203: the listener seeds the registry from the durable overlay ----------


class _OverlaySink(InMemoryTaskSink):
    """InMemoryTaskSink that also serves a fixed durable workstream overlay."""

    def __init__(self, rows: list[WorkstreamOverlayRow]) -> None:
        super().__init__()
        self._overlay_rows = rows

    async def load_workstream_overlay(self) -> list[WorkstreamOverlayRow]:
        return list(self._overlay_rows)


async def test_listener_seeds_registry_from_overlay_on_subscribe() -> None:
    """US-203: the listener rebuilds the registry from the durable overlay at
    subscribe (off the speech path), so a respawned coordinator's status query
    reflects delegated work that outlived the in-memory registry. Seeding alone
    settles/delivers nothing."""
    sink = _OverlaySink(
        [
            WorkstreamOverlayRow(
                task_id=55, kind="google-calendar", status="running", age_seconds=15.0
            )
        ]
    )
    coordinator, _ = _external_coordinator(sink)
    recorder = _SettleRecorder()
    listener, _frames = _listener(coordinator, recorder)
    listener.start()
    await _wait_until(lambda: coordinator.remote_listener_active)
    await _wait_until(lambda: coordinator.registry_entry(55) is not None)
    entry = coordinator.registry_entry(55)
    assert entry is not None
    assert entry.kind == "google-calendar"
    assert entry.status == "running"
    assert recorder.entries == []  # seeding alone settles/delivers nothing
    await listener.aclose()
    await coordinator.aclose()
