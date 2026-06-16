"""Tests for the SQLAlchemy-backed task sink (Johnny-trt.18).

Covers :class:`app.services.agent_tasks.SqlAlchemyTaskSink` row writes /
status flips against SQLite, plus the bead's integration: a stub-kind
delegation driven end-to-end through the real :class:`TaskCoordinator` +
real sink, asserting the row lifecycle ``queued`` → ``failed`` with
speech-ready error text stored.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import (
    AgentTask,
    AgentTaskStatus,
    AgentToolCall,
    AgentWorkstream,
    WorkstreamDeliveryStatus,
    WorkstreamSourceKind,
    WorkstreamStatus,
)
from app.services.agent_tasks import SqlAlchemyTaskSink, SqlAlchemyToolCallTraceSink
from app.services.session_trace import build_session_trace_view
from johnny.agent.tasks import (
    STATUS_NOTHING_IN_FLIGHT,
    TaskCoordinator,
    TaskSpec,
    stub_executor,
    unsupported_kind_text,
)
from johnny.skills.executor import ToolCallTrace
from johnny.voice_pipeline.event_bus import InMemoryEventBus
from johnny.voice_pipeline.events import TaskQueued


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    # agent_tasks + agent_workstreams (US-203 overlay read joins them). FKs point
    # at bot_sessions / agent_decisions but SQLite doesn't enforce FKs by default,
    # so the table-only fixture works (the decision-sink test's pattern).
    Base.metadata.create_all(
        bind=eng,
        tables=[AgentTask.__table__, AgentWorkstream.__table__],  # type: ignore[list-item]
    )
    return eng


@pytest.fixture
def db_session(engine: sa.Engine) -> Iterator[Session]:
    sess = Session(engine)
    try:
        yield sess
    finally:
        sess.close()


def _spec(**overrides: object) -> TaskSpec:
    base: dict[str, object] = {
        "kind": "web_search",
        "args": {"query": "weather in Helsinki"},
        "ack_text": "let me check on that",
        "turn_id": 4,
        "decision_id": 17,
    }
    base.update(overrides)
    return TaskSpec(**base)  # type: ignore[arg-type]


# --- record_queued ------------------------------------------------------------


async def test_record_queued_persists_full_row(db_session: Session) -> None:
    sink = SqlAlchemyTaskSink(db_session, bot_session_id=99)
    task_id = await sink.record_queued(_spec())
    assert task_id is not None

    row = db_session.scalars(sa.select(AgentTask)).one()
    assert row.id == task_id
    assert row.bot_session_id == 99
    assert row.agent_decision_id == 17
    assert row.turn_id == 4
    assert row.kind == "web_search"
    assert row.request_json == {
        "kind": "web_search",
        "args": {"query": "weather in Helsinki"},
        "ack": "let me check on that",
    }
    assert row.status == AgentTaskStatus.QUEUED
    assert row.ack_text == "let me check on that"
    assert row.result_text is None
    assert row.result_json is None
    assert row.error is None
    assert row.attempts == 0
    assert row.callback_token is None
    assert row.created_at is not None
    assert row.updated_at is not None


async def test_record_queued_optional_fields_default_null(db_session: Session) -> None:
    sink = SqlAlchemyTaskSink(db_session, bot_session_id=1)
    await sink.record_queued(_spec(ack_text="", turn_id=None, decision_id=None, args={}))
    row = db_session.scalars(sa.select(AgentTask)).one()
    assert row.agent_decision_id is None
    assert row.turn_id is None
    assert row.ack_text is None  # empty ack stored as NULL, not ""
    assert row.request_json == {"kind": "web_search", "args": {}, "ack": ""}


async def test_record_queued_stamps_reasoning_llm(db_session: Session) -> None:
    """The session's resolved reasoning-LLM identity rides every queued row
    (Johnny-trt.42) — identity only, never credentials. Absent descriptor
    (agents that pin nothing on an LLM-less payload) leaves the legacy shape."""
    descriptor = {
        "provider_id": 6,
        "provider_name": "openai",
        "display_name": "Cloud reasoning",
        "model": "gpt-large",
    }
    sink = SqlAlchemyTaskSink(db_session, bot_session_id=99, reasoning_llm=descriptor)
    await sink.record_queued(_spec())
    row = db_session.scalars(sa.select(AgentTask)).one()
    assert row.request_json["reasoning_llm"] == descriptor
    assert "credentials" not in row.request_json["reasoning_llm"]
    assert row.request_json["kind"] == "web_search"


async def test_record_queued_stamps_workspace(db_session: Session) -> None:
    """The session's frozen workspace identity rides every queued row
    (Johnny-wks.1) so the worker resolver runs the task in the workspace the
    session's catalog promised. No stamp (legacy / default-workspace
    sessions) keeps the pre-workspaces row shape byte-identical."""
    workspace = {"id": 7, "name": "Finance", "slug": "finance", "is_default": False}
    sink = SqlAlchemyTaskSink(db_session, bot_session_id=99, workspace=workspace)
    await sink.record_queued(_spec())
    row = db_session.scalars(sa.select(AgentTask)).one()
    assert row.request_json["workspace"] == workspace

    bare = SqlAlchemyTaskSink(db_session, bot_session_id=99)
    await bare.record_queued(_spec())
    rows = db_session.scalars(sa.select(AgentTask).order_by(AgentTask.id)).all()
    assert "workspace" not in rows[1].request_json


# --- update_status --------------------------------------------------------------


async def test_update_status_flips_through_lifecycle(db_session: Session) -> None:
    sink = SqlAlchemyTaskSink(db_session, bot_session_id=1)
    task_id = await sink.record_queued(_spec())
    assert task_id is not None

    await sink.update_status(task_id, "running", attempts=1)
    row = db_session.get(AgentTask, task_id)
    assert row is not None
    assert row.status == AgentTaskStatus.RUNNING
    assert row.attempts == 1

    await sink.update_status(
        task_id,
        "done",
        result_text="Sunny, 21 degrees.",
        result_json={"temp_c": 21},
    )
    db_session.refresh(row)
    # mypy narrows row.status from the earlier ``== RUNNING`` assert; the DB
    # refresh has changed it, so the comparison is valid at runtime.
    assert row.status == AgentTaskStatus.DONE  # type: ignore[comparison-overlap]
    assert row.result_text == "Sunny, 21 degrees."
    assert row.result_json == {"temp_c": 21}
    assert row.attempts == 1  # untouched by the terminal write
    assert row.error is None


async def test_update_status_none_fields_leave_columns_alone(db_session: Session) -> None:
    sink = SqlAlchemyTaskSink(db_session, bot_session_id=1)
    task_id = await sink.record_queued(_spec())
    assert task_id is not None
    await sink.update_status(task_id, "failed", error="boom", result_text="It broke.")
    await sink.update_status(task_id, "failed")  # no kwargs: only status rewritten
    row = db_session.get(AgentTask, task_id)
    assert row is not None
    assert row.error == "boom"
    assert row.result_text == "It broke."


async def test_update_status_unknown_id_warns_and_noops(db_session: Session) -> None:
    sink = SqlAlchemyTaskSink(db_session, bot_session_id=1)
    await sink.update_status(12345, "done")  # must not raise
    assert db_session.scalars(sa.select(AgentTask)).all() == []


async def test_update_status_junk_status_recorded_failed(db_session: Session) -> None:
    sink = SqlAlchemyTaskSink(db_session, bot_session_id=1)
    task_id = await sink.record_queued(_spec())
    assert task_id is not None
    await sink.update_status(task_id, "exploded", error="kept")  # type: ignore[arg-type]
    row = db_session.get(AgentTask, task_id)
    assert row is not None
    assert row.status == AgentTaskStatus.FAILED
    assert row.error == "kept"


# --- integration: stub-kind delegation end-to-end --------------------------------


async def test_stub_delegation_end_to_end_queued_then_failed(db_session: Session) -> None:
    """The bead's integration test: real coordinator + real sink + stub executor.

    begin() leaves a durable ``queued`` row before it returns (the gate speaks
    the ack only after that), publishes ``TaskQueued`` on the session bus, and
    the stub executor settles the row ``failed`` fast with speech-ready error
    text — the full ``queued`` → ``failed`` lifecycle of an unsupported kind.
    """
    from johnny.agent.task_wiring import build_publish_task_queued

    sink = SqlAlchemyTaskSink(db_session, bot_session_id=55)
    bus = InMemoryEventBus()
    coordinator = TaskCoordinator(
        sink,
        executor=stub_executor,
        publish_queued=build_publish_task_queued(bus, session_id="55", clock=lambda: 1),
    )

    queued = await coordinator.begin(_spec(kind="summarize_meeting"))
    assert queued is not None

    # Row-before-ack: at the moment begin() returned (= before the gate would
    # speak), the row is durable and still queued — the resolver hasn't run.
    row = db_session.get(AgentTask, queued.task_id)
    assert row is not None
    assert row.status == AgentTaskStatus.QUEUED

    # TaskQueued is on the session channel, correlating by task_id.
    events = bus.snapshot()
    assert len(events) == 1
    assert isinstance(events[0], TaskQueued)
    assert events[0].task_id == queued.task_id
    assert events[0].session_id == "55"

    await coordinator.join()

    db_session.refresh(row)
    # mypy narrows row.status from the earlier ``== QUEUED`` assert; the DB
    # refresh has changed it, so the comparison is valid at runtime.
    assert row.status == AgentTaskStatus.FAILED  # type: ignore[comparison-overlap]
    assert row.result_text == unsupported_kind_text("summarize_meeting")
    assert row.error is not None and "summarize_meeting" in row.error
    assert row.attempts == 1
    await coordinator.aclose()


# --- fetch_status (Johnny-trt.24 watcher reads) ---------------------------------


async def test_fetch_status_returns_current_snapshot(db_session: Session) -> None:
    sink = SqlAlchemyTaskSink(db_session, bot_session_id=99)
    task_id = await sink.record_queued(_spec())
    assert task_id is not None

    snapshot = await sink.fetch_status(task_id)
    assert snapshot is not None
    assert snapshot.status == "queued"
    assert snapshot.result_text is None and snapshot.error is None

    await sink.update_status(
        task_id, "failed", result_text="No account linked.", error="gog: unauthed"
    )
    snapshot = await sink.fetch_status(task_id)
    assert snapshot is not None
    assert snapshot.status == "failed"
    assert snapshot.result_text == "No account linked."
    assert snapshot.error == "gog: unauthed"


async def test_fetch_status_missing_row_returns_none(db_session: Session) -> None:
    sink = SqlAlchemyTaskSink(db_session, bot_session_id=99)
    assert await sink.fetch_status(424242) is None


async def test_fetch_status_sees_other_session_commits(engine: sa.Engine) -> None:
    """The watcher's freshness contract: a settle committed through a
    DIFFERENT session (the worker process, in production) is visible to the
    sink's polling reads despite the identity map."""
    reader = Session(engine)
    writer = Session(engine)
    try:
        sink = SqlAlchemyTaskSink(reader, bot_session_id=99)
        task_id = await sink.record_queued(_spec())
        assert task_id is not None
        first = await sink.fetch_status(task_id)
        assert first is not None and first.status == "queued"

        row = writer.get(AgentTask, task_id)
        assert row is not None
        row.status = AgentTaskStatus.FAILED
        row.result_text = "worker says no"
        writer.commit()

        snapshot = await sink.fetch_status(task_id)
        assert snapshot is not None
        assert snapshot.status == "failed"
        assert snapshot.result_text == "worker says no"
    finally:
        reader.close()
        writer.close()


# --- SqlAlchemyToolCallTraceSink: turn-id binding (Johnny-5sm) -----------------


@pytest.fixture
def tool_engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    Base.metadata.create_all(bind=eng, tables=[AgentToolCall.__table__])  # type: ignore[list-item]
    return eng


def _tool_trace(**overrides: object) -> ToolCallTrace:
    base: dict[str, object] = {
        "tool_name": "sandbox.exec",
        "phase": "exec",
        "request": {"argv": ["bash", "/skills/weather/run.sh", "Helsinki"]},
        "ok": True,
        "exit_code": 0,
        "stdout": "Right now in Helsinki: Partly cloudy +12°C",
        "stderr": "",
        "duration_ms": 180,
        "timed_out": False,
        "truncated": False,
        "denied": False,
        "error": "",
    }
    base.update(overrides)
    return ToolCallTrace(**base)  # type: ignore[arg-type]


def _only_row(engine: sa.Engine) -> AgentToolCall:
    with Session(engine) as sess:
        rows = sess.scalars(sa.select(AgentToolCall)).all()
        assert len(rows) == 1
        return rows[0]


async def test_tool_sink_resolver_stamps_the_live_turn(tool_engine: sa.Engine) -> None:
    """The inline native-tool loop (one session-scoped sink) resolves the
    issuing turn per call — the fix for the 'black box' where every inline call
    landed turn_id=NULL and the timeline dropped it."""
    sink = SqlAlchemyToolCallTraceSink(
        bot_session_id=36,
        resolve_turn_id=lambda: 7,
        session_factory=lambda: Session(tool_engine),
    )
    await sink.record(_tool_trace())
    row = _only_row(tool_engine)
    assert row.turn_id == 7
    assert row.bot_session_id == 36
    assert row.request_json["argv"][-1] == "Helsinki"  # args round-trip intact


async def test_tool_sink_resolver_none_falls_back_to_fixed_turn(
    tool_engine: sa.Engine,
) -> None:
    """A resolver that returns None (no active reply) keeps the fixed binding so
    a resolver miss never costs us the row's attribution."""
    sink = SqlAlchemyToolCallTraceSink(
        bot_session_id=36,
        turn_id=3,
        resolve_turn_id=lambda: None,
        session_factory=lambda: Session(tool_engine),
    )
    await sink.record(_tool_trace())
    assert _only_row(tool_engine).turn_id == 3


async def test_tool_sink_resolver_raise_falls_back_and_persists(
    tool_engine: sa.Engine,
) -> None:
    """A raising resolver must not lose the trace — it falls back to the fixed
    binding and still persists the row."""

    def _boom() -> int:
        raise RuntimeError("gate gone")

    sink = SqlAlchemyToolCallTraceSink(
        bot_session_id=36,
        turn_id=5,
        resolve_turn_id=_boom,
        session_factory=lambda: Session(tool_engine),
    )
    await sink.record(_tool_trace())
    assert _only_row(tool_engine).turn_id == 5


async def test_tool_sink_emits_a_live_observed_signal(tool_engine: sa.Engine) -> None:
    """With a publish callback the sink streams a compact ToolCallObserved after
    the write (Johnny-iy6) — the live signal that drives the session view's
    during-turn refresh; best-effort, so the row persists regardless."""
    seen: list[object] = []

    async def _publish(ev: object) -> None:
        seen.append(ev)

    sink = SqlAlchemyToolCallTraceSink(
        bot_session_id=44,
        resolve_turn_id=lambda: 7,
        publish_observed=_publish,
        session_factory=lambda: Session(tool_engine),
    )
    await sink.record(_tool_trace(phase="list"))
    assert _only_row(tool_engine).turn_id == 7
    assert len(seen) == 1
    ev = seen[0]
    assert ev.type == "tool_call_observed"  # type: ignore[attr-defined]
    assert ev.turn_id == 7 and ev.phase == "list" and ev.ok is True  # type: ignore[attr-defined]


async def test_tool_sink_worker_path_keeps_fixed_turn(tool_engine: sa.Engine) -> None:
    """The worker builds one sink per task with a fixed turn_id and NO resolver —
    that path is unchanged."""
    sink = SqlAlchemyToolCallTraceSink(
        bot_session_id=36,
        agent_task_id=11,
        turn_id=4,
        kind="web_search",
        session_factory=lambda: Session(tool_engine),
    )
    await sink.record(_tool_trace())
    row = _only_row(tool_engine)
    assert row.turn_id == 4
    assert row.agent_task_id == 11
    assert row.kind == "web_search"


# --- US-203: durable-overlay read + status/column parity ----------------------


def _delegated_workstream(
    *,
    bot_session_id: int,
    agent_task_id: int,
    kind: str,
    status: WorkstreamStatus,
    delivery_status: WorkstreamDeliveryStatus,
    created_at: datetime,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    result_text: str | None = None,
) -> AgentWorkstream:
    return AgentWorkstream(
        bot_session_id=bot_session_id,
        source_kind=WorkstreamSourceKind.DELEGATE,
        agent_task_id=agent_task_id,
        title=kind,
        status=status,
        delivery_status=delivery_status,
        created_at=created_at,
        started_at=started_at,
        completed_at=completed_at,
        result_text=result_text,
    )


async def test_load_workstream_overlay_maps_delegated_rows(db_session: Session) -> None:
    """US-203: the durable overlay flattens this session's delegated workstreams
    (joined to agent_tasks for the kind), folds delivery state into ``delivered``,
    and computes wall-clock ages; inline (NULL agent_task_id) and other-session
    rows are excluded."""
    now = datetime.now(UTC)
    t1 = AgentTask(
        bot_session_id=42, kind="mcp__demo-http__reverse_text",
        request_json={}, status=AgentTaskStatus.RUNNING,
    )
    t2 = AgentTask(
        bot_session_id=42, kind="google-calendar",
        request_json={}, status=AgentTaskStatus.DONE,
    )
    t_other = AgentTask(
        bot_session_id=99, kind="other", request_json={}, status=AgentTaskStatus.DONE,
    )
    db_session.add_all([t1, t2, t_other])
    db_session.flush()
    t1_id, t2_id = t1.id, t2.id
    db_session.add_all(
        [
            _delegated_workstream(
                bot_session_id=42, agent_task_id=t1.id,
                kind="mcp__demo-http__reverse_text",
                status=WorkstreamStatus.RUNNING,
                delivery_status=WorkstreamDeliveryStatus.NOT_READY,
                created_at=now - timedelta(seconds=30),
                started_at=now - timedelta(seconds=25),
            ),
            _delegated_workstream(
                bot_session_id=42, agent_task_id=t2.id, kind="google-calendar",
                status=WorkstreamStatus.DONE,
                delivery_status=WorkstreamDeliveryStatus.READY,
                created_at=now - timedelta(seconds=12),
                completed_at=now - timedelta(seconds=4),
                result_text="3 events this week",
            ),
            # Inline (NULL agent_task_id) — excluded.
            AgentWorkstream(
                bot_session_id=42,
                source_kind=WorkstreamSourceKind.FOREGROUND_TOOL_LOOP,
                agent_task_id=None, title="inline", status=WorkstreamStatus.DONE,
                delivery_status=WorkstreamDeliveryStatus.READY, created_at=now,
            ),
            # Another session — excluded.
            _delegated_workstream(
                bot_session_id=99, agent_task_id=t_other.id, kind="other",
                status=WorkstreamStatus.DONE,
                delivery_status=WorkstreamDeliveryStatus.READY, created_at=now,
            ),
        ]
    )
    db_session.commit()

    sink = SqlAlchemyTaskSink(db_session, bot_session_id=42)
    overlay = await sink.load_workstream_overlay()

    by_id = {r.task_id: r for r in overlay}
    assert set(by_id) == {t1_id, t2_id}  # inline + other-session excluded
    assert by_id[t1_id].kind == "mcp__demo-http__reverse_text"  # from agent_tasks
    assert by_id[t1_id].status == "running"
    assert by_id[t1_id].settled_age_seconds is None
    assert by_id[t1_id].age_seconds >= 29.0
    assert by_id[t1_id].delivered is False
    assert by_id[t2_id].status == "done"
    assert by_id[t2_id].result_text == "3 events this week"
    assert by_id[t2_id].delivered is False  # delivery_status=ready → still speakable
    assert (by_id[t2_id].settled_age_seconds or 0.0) >= 3.0


async def test_status_overlay_and_column_read_the_same_source(db_session: Session) -> None:
    """US-203 AC#2: the Workstreams column (build_session_trace_view over
    agent_workstreams) and the spoken status (registry seeded from the overlay)
    describe the same workstreams — both read agent_workstreams."""
    now = datetime.now(UTC)
    t1 = AgentTask(
        bot_session_id=7, kind="mcp__demo-http__reverse_text",
        request_json={}, status=AgentTaskStatus.RUNNING,
    )
    t2 = AgentTask(
        bot_session_id=7, kind="google-calendar",
        request_json={}, status=AgentTaskStatus.DONE,
    )
    db_session.add_all([t1, t2])
    db_session.flush()
    t1_id, t2_id = t1.id, t2.id
    db_session.add_all(
        [
            _delegated_workstream(
                bot_session_id=7, agent_task_id=t1.id,
                kind="mcp__demo-http__reverse_text",
                status=WorkstreamStatus.RUNNING,
                delivery_status=WorkstreamDeliveryStatus.NOT_READY,
                created_at=now - timedelta(seconds=10),
                started_at=now - timedelta(seconds=8),
            ),
            _delegated_workstream(
                bot_session_id=7, agent_task_id=t2.id, kind="google-calendar",
                status=WorkstreamStatus.DONE,
                delivery_status=WorkstreamDeliveryStatus.READY,
                created_at=now - timedelta(seconds=6),
                completed_at=now - timedelta(seconds=2),
                result_text="3 events this week",
            ),
        ]
    )
    db_session.commit()

    # Column source: the durable projection the trace API serves.
    ws_rows = db_session.scalars(
        sa.select(AgentWorkstream).where(AgentWorkstream.bot_session_id == 7)
    ).all()
    task_rows = db_session.scalars(
        sa.select(AgentTask).where(AgentTask.bot_session_id == 7)
    ).all()
    view = build_session_trace_view(
        decisions=[], utterances=[], tasks=task_rows, tool_calls=[],
        model_calls=[], workstreams=ws_rows, workstream_events=[],
        conversation_events=[],
    )
    column = {(w.agent_task_id, w.status) for w in view.workstreams}

    # Status source: a fresh coordinator seeded from the same durable overlay.
    clock = [1000.0]
    sink = SqlAlchemyTaskSink(db_session, bot_session_id=7)
    coordinator = TaskCoordinator(
        sink, executor=stub_executor,
        runs_in_session=lambda _kind: False, monotonic=lambda: clock[0],
    )
    await coordinator.seed_registry_from_overlay()
    status = {(e.task_id, e.status) for e in coordinator.registry_snapshot()}

    assert column == status == {(t1_id, "running"), (t2_id, "done")}
    assert coordinator.status_summary().text != STATUS_NOTHING_IN_FLIGHT
    await coordinator.aclose()
