"""Tests for the SQLAlchemy-backed task sink (Johnny-trt.18).

Covers :class:`app.services.agent_tasks.SqlAlchemyTaskSink` row writes /
status flips against SQLite, plus the bead's integration: a stub-kind
delegation driven end-to-end through the real :class:`TaskCoordinator` +
real sink, asserting the row lifecycle ``queued`` → ``failed`` with
speech-ready error text stored.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import AgentTask, AgentTaskStatus
from app.services.agent_tasks import SqlAlchemyTaskSink
from johnny.agent.tasks import (
    TaskCoordinator,
    TaskSpec,
    stub_executor,
    unsupported_kind_text,
)
from johnny.voice_pipeline.event_bus import InMemoryEventBus
from johnny.voice_pipeline.events import TaskQueued


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    # Only need the agent_tasks table — FKs point at bot_sessions /
    # agent_decisions but SQLite doesn't enforce FKs by default, so the
    # table-only fixture works (the decision-sink test's pattern).
    Base.metadata.create_all(bind=eng, tables=[AgentTask.__table__])  # type: ignore[list-item]
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
    assert row.status == AgentTaskStatus.DONE
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
    assert row.status == AgentTaskStatus.FAILED
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
