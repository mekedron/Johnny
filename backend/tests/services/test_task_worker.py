"""Unit tests for the worker executor pass (Johnny-trt.24).

Claim SQL, the settle fence, and the TTL sweep run against SQLite (the
``FOR UPDATE SKIP LOCKED`` clause is a no-op there; the concurrency
guarantee itself is integration-proven on Postgres —
``tests/integration/test_task_worker.py``). The :class:`TaskWorker` loop
tests run with ``redis_url=None`` (no wake, no events — poll-driven) and an
injected executor, proving claim→run→settle, bounded concurrency, the
execution timeout, and that sweeps keep happening while slow tools run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.db.models import AgentTask, AgentTaskStatus
from app.services.task_worker import (
    ClaimedTask,
    TaskWorker,
    claim_queued_tasks,
    get_task_exec_timeout_seconds,
    settle_claimed_task,
    sweep_stale_tasks,
)
from johnny.agent.internal_tools import INTERNAL_TOOL_KINDS
from johnny.agent.tasks import QueuedTask, TaskResult

# --- fixtures ------------------------------------------------------------------


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    # agent_tasks only — SQLite doesn't enforce the FKs (the task-sink
    # tests' fixture pattern).
    Base.metadata.create_all(bind=eng, tables=[AgentTask.__table__])  # type: ignore[list-item]
    return eng


@pytest.fixture
def session_factory(engine: sa.Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@pytest.fixture
def db(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


_NOW = datetime(2026, 6, 11, 12, 0, 0)  # naive — consistent with SQLite storage


def _insert(
    db: Session,
    *,
    kind: str = "calendar.upcoming_events",
    status: AgentTaskStatus = AgentTaskStatus.QUEUED,
    attempts: int = 0,
    bot_session_id: int = 7,
    turn_id: int | None = 4,
    updated_at: datetime | None = None,
    created_at: datetime | None = None,
    args: dict[str, Any] | None = None,
) -> int:
    row = AgentTask(
        bot_session_id=bot_session_id,
        turn_id=turn_id,
        kind=kind,
        request_json={"kind": kind, "args": args or {}, "ack": "on it"},
        status=status,
        ack_text="on it",
        attempts=attempts,
        created_at=created_at or _NOW,
        updated_at=updated_at or _NOW,
    )
    db.add(row)
    db.commit()
    return int(row.id)


def _row(db: Session, task_id: int) -> AgentTask:
    db.expire_all()
    row = db.get(AgentTask, task_id)
    assert row is not None
    return row


# --- claim_queued_tasks ----------------------------------------------------------


def test_claim_moves_queued_to_running_and_increments_attempts(db: Session) -> None:
    task_id = _insert(db, args={"window": "7d"})
    claimed = claim_queued_tasks(db, limit=5, now=_NOW)
    db.commit()

    assert len(claimed) == 1
    task = claimed[0]
    assert task.task_id == task_id
    assert task.kind == "calendar.upcoming_events"
    assert task.args == {"window": "7d"}
    assert task.ack_text == "on it"
    assert task.turn_id == 4
    assert task.bot_session_id == 7
    assert task.attempts == 1  # claim IS the attempt

    row = _row(db, task_id)
    assert row.status == AgentTaskStatus.RUNNING
    assert row.attempts == 1


def test_claim_skips_internal_kinds_by_default(db: Session) -> None:
    """The Johnny-trt.57 locality guard at the SQL level: internal kinds are
    never claimed — the session owns them."""
    for kind in sorted(INTERNAL_TOOL_KINDS):
        _insert(db, kind=kind)
    skill_id = _insert(db, kind="calendar.upcoming_events")

    claimed = claim_queued_tasks(db, limit=10, now=_NOW)
    db.commit()

    assert [task.task_id for task in claimed] == [skill_id]
    for row in db.scalars(sa.select(AgentTask).where(AgentTask.id != skill_id)):
        assert row.status == AgentTaskStatus.QUEUED


def test_claim_oldest_first_and_respects_limit(db: Session) -> None:
    older = _insert(db, created_at=_NOW - timedelta(seconds=30))
    newer = _insert(db, created_at=_NOW)
    first = claim_queued_tasks(db, limit=1, now=_NOW)
    db.commit()
    assert [task.task_id for task in first] == [older]
    second = claim_queued_tasks(db, limit=1, now=_NOW)
    db.commit()
    assert [task.task_id for task in second] == [newer]
    assert claim_queued_tasks(db, limit=1, now=_NOW) == []


def test_claim_only_kinds_scopes_the_claimer(db: Session) -> None:
    """Test instances on a shared stack claim only their own kinds."""
    mine = _insert(db, kind="session.end")
    _insert(db, kind="calendar.upcoming_events")

    claimed = claim_queued_tasks(
        db,
        limit=10,
        exclude_kinds=frozenset(),
        only_kinds=frozenset({"session.end"}),
        now=_NOW,
    )
    db.commit()
    assert [task.task_id for task in claimed] == [mine]


def test_claim_ignores_non_queued_rows_and_zero_limit(db: Session) -> None:
    _insert(db, status=AgentTaskStatus.RUNNING, attempts=1)
    _insert(db, status=AgentTaskStatus.DONE)
    assert claim_queued_tasks(db, limit=5, now=_NOW) == []
    _insert(db)
    assert claim_queued_tasks(db, limit=0, now=_NOW) == []


def test_claimed_task_as_queued_task_shape(db: Session) -> None:
    task_id = _insert(db, args={"q": "x"})
    claimed = claim_queued_tasks(db, limit=1, now=_NOW)[0]
    db.commit()
    queued: QueuedTask = claimed.as_queued_task()
    assert queued.task_id == task_id
    assert queued.spec.kind == "calendar.upcoming_events"
    assert queued.spec.args == {"q": "x"}
    assert queued.spec.ack_text == "on it"
    assert queued.spec.turn_id == 4


# --- settle_claimed_task ----------------------------------------------------------


def test_settle_writes_terminal_row_when_fence_matches(db: Session) -> None:
    task_id = _insert(db)
    claimed = claim_queued_tasks(db, limit=1, now=_NOW)[0]
    db.commit()

    settled = settle_claimed_task(
        db,
        task_id=task_id,
        claim_attempts=claimed.attempts,
        status="done",
        result_text="You have 3 events this week.",
        result_json={"exit_code": 0},
        now=_NOW,
    )
    db.commit()
    assert settled is True
    row = _row(db, task_id)
    assert row.status == AgentTaskStatus.DONE
    assert row.result_text == "You have 3 events this week."
    assert row.result_json == {"exit_code": 0}
    assert row.error is None


def test_settle_fence_rejects_stale_attempts(db: Session) -> None:
    """A straggling first runner must not clobber a re-claimed row — and the
    caller then publishes nothing (the no-duplicate-events acceptance)."""
    task_id = _insert(db)
    first = claim_queued_tasks(db, limit=1, now=_NOW)[0]
    db.commit()

    # TTL sweep requeues, someone re-claims: attempts moves past the fence.
    sweep_stale_tasks(
        db, ttl_s=0.0, max_attempts=3, now=_NOW + timedelta(seconds=1)
    )
    db.commit()
    second = claim_queued_tasks(db, limit=1, now=_NOW)[0]
    db.commit()
    assert second.attempts == first.attempts + 1

    stale = settle_claimed_task(
        db,
        task_id=task_id,
        claim_attempts=first.attempts,
        status="done",
        result_text="stale result",
        now=_NOW,
    )
    db.commit()
    assert stale is False
    row = _row(db, task_id)
    assert row.status == AgentTaskStatus.RUNNING  # second claim still owns it
    assert row.result_text is None

    fresh = settle_claimed_task(
        db,
        task_id=task_id,
        claim_attempts=second.attempts,
        status="failed",
        result_text="honest failure",
        error="detail",
        now=_NOW,
    )
    db.commit()
    assert fresh is True
    row = _row(db, task_id)
    assert row.status == AgentTaskStatus.FAILED
    assert row.result_text == "honest failure"


def test_settle_rejects_non_running_rows(db: Session) -> None:
    task_id = _insert(db)  # still queued — never claimed
    settled = settle_claimed_task(
        db,
        task_id=task_id,
        claim_attempts=0,
        status="done",
        result_text="x",
        now=_NOW,
    )
    db.commit()
    assert settled is False
    assert _row(db, task_id).status == AgentTaskStatus.QUEUED


# --- sweep_stale_tasks ------------------------------------------------------------


def test_sweep_requeues_stale_running_under_attempts_cap(db: Session) -> None:
    task_id = _insert(
        db,
        status=AgentTaskStatus.RUNNING,
        attempts=1,
        updated_at=_NOW - timedelta(seconds=600),
    )
    result = sweep_stale_tasks(db, ttl_s=300, max_attempts=3, now=_NOW)
    db.commit()
    assert result.requeued == (task_id,)
    assert result.failed == () and result.cancelled == ()
    row = _row(db, task_id)
    assert row.status == AgentTaskStatus.QUEUED
    assert row.attempts == 1  # preserved; the next claim increments


def test_sweep_settles_failed_at_attempts_cap_with_honest_speech(db: Session) -> None:
    task_id = _insert(
        db,
        status=AgentTaskStatus.RUNNING,
        attempts=3,
        updated_at=_NOW - timedelta(seconds=600),
    )
    result = sweep_stale_tasks(db, ttl_s=300, max_attempts=3, now=_NOW)
    db.commit()
    assert result.requeued == () and result.cancelled == ()
    assert len(result.failed) == 1
    failed = result.failed[0]
    assert failed.task_id == task_id
    assert failed.bot_session_id == 7
    assert failed.turn_id == 4
    assert "I couldn't finish the calendar.upcoming_events task" in failed.result_text
    row = _row(db, task_id)
    assert row.status == AgentTaskStatus.FAILED
    assert row.result_text == failed.result_text
    assert "requeue TTL exceeded after 3 attempts" in (row.error or "")


def test_sweep_cancels_stranded_internal_rows(db: Session) -> None:
    """Internal kinds from a crashed session: the worker may never run them,
    so stale queued/running rows settle cancelled (which announces nothing)."""
    queued_id = _insert(
        db, kind="session.end", updated_at=_NOW - timedelta(seconds=600)
    )
    running_id = _insert(
        db,
        kind="meeting.leave",
        status=AgentTaskStatus.RUNNING,
        attempts=1,
        updated_at=_NOW - timedelta(seconds=600),
    )
    result = sweep_stale_tasks(db, ttl_s=300, max_attempts=3, now=_NOW)
    db.commit()
    assert set(result.cancelled) == {queued_id, running_id}
    assert result.failed == () and result.requeued == ()
    for task_id in (queued_id, running_id):
        row = _row(db, task_id)
        assert row.status == AgentTaskStatus.CANCELLED
        assert "didn't finish before its session went away" in (row.result_text or "")


def test_sweep_leaves_fresh_rows_alone(db: Session) -> None:
    _insert(db, status=AgentTaskStatus.RUNNING, attempts=1, updated_at=_NOW)
    _insert(db, kind="session.end", updated_at=_NOW)  # fresh queued internal
    _insert(db, updated_at=_NOW - timedelta(seconds=600))  # stale but QUEUED non-internal
    result = sweep_stale_tasks(db, ttl_s=300, max_attempts=3, now=_NOW)
    db.commit()
    assert result == type(result)()  # nothing swept
    statuses = {row.status for row in db.scalars(sa.select(AgentTask))}
    assert statuses == {AgentTaskStatus.RUNNING, AgentTaskStatus.QUEUED}


def test_sweep_only_kinds_scopes_the_sweeper(db: Session) -> None:
    mine = _insert(
        db,
        kind="session.end",
        status=AgentTaskStatus.RUNNING,
        attempts=1,
        updated_at=_NOW - timedelta(seconds=600),
    )
    other = _insert(
        db,
        status=AgentTaskStatus.RUNNING,
        attempts=1,
        updated_at=_NOW - timedelta(seconds=600),
    )
    result = sweep_stale_tasks(
        db,
        ttl_s=300,
        max_attempts=3,
        only_kinds=frozenset({"session.end"}),
        now=_NOW,
    )
    db.commit()
    assert result.cancelled == (mine,)
    assert _row(db, other).status == AgentTaskStatus.RUNNING


# --- env knobs ---------------------------------------------------------------------


def test_exec_timeout_clamps_under_running_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOHNNY_TASK_RUNNING_TTL_SECONDS", "100")
    monkeypatch.setenv("JOHNNY_TASK_EXEC_TIMEOUT_SECONDS", "500")
    assert get_task_exec_timeout_seconds() == pytest.approx(80.0)
    monkeypatch.setenv("JOHNNY_TASK_EXEC_TIMEOUT_SECONDS", "60")
    assert get_task_exec_timeout_seconds() == pytest.approx(60.0)


# --- the TaskWorker loop (poll-driven, no redis) -------------------------------------


def _worker(
    session_factory: sessionmaker[Session],
    *,
    executor: Any,
    **kwargs: Any,
) -> TaskWorker:
    kwargs.setdefault("poll_interval_s", 0.05)
    kwargs.setdefault("concurrency", 4)
    kwargs.setdefault("running_ttl_s", 300.0)
    kwargs.setdefault("max_attempts", 3)
    kwargs.setdefault("exec_timeout_s", 5.0)
    kwargs.setdefault("sweep_interval_s", 0.1)
    return TaskWorker(
        redis_url=None,
        session_factory=session_factory,
        executor=executor,
        **kwargs,
    )


async def _run_until(
    worker: TaskWorker, predicate: Any, *, timeout: float = 5.0
) -> None:
    """Drive worker.run() until ``predicate()`` holds, then stop it."""
    runner = asyncio.ensure_future(worker.run())
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("worker did not reach the expected state in time")
            await asyncio.sleep(0.02)
    finally:
        worker.request_stop()
        await asyncio.wait_for(runner, timeout=10.0)


async def test_loop_claims_runs_and_settles_done(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    task_id = _insert(db, args={"q": "calendar"})

    async def executor(task: QueuedTask) -> TaskResult:
        assert task.spec.args == {"q": "calendar"}
        return TaskResult(
            status="done", result_text="3 events", result_json={"exit_code": 0}
        )

    worker = _worker(session_factory, executor=executor)
    await _run_until(
        worker, lambda: _row(db, task_id).status == AgentTaskStatus.DONE
    )
    row = _row(db, task_id)
    assert row.result_text == "3 events"
    assert row.attempts == 1


async def test_loop_bounds_concurrency_and_settles_everything(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    ids = [_insert(db) for _ in range(5)]
    state = {"running": 0, "peak": 0}

    async def slow_executor(task: QueuedTask) -> TaskResult:
        state["running"] += 1
        state["peak"] = max(state["peak"], state["running"])
        try:
            await asyncio.sleep(0.1)
        finally:
            state["running"] -= 1
        return TaskResult(status="done", result_text="ok")

    worker = _worker(session_factory, executor=slow_executor, concurrency=2)
    await _run_until(
        worker,
        lambda: all(
            _row(db, task_id).status == AgentTaskStatus.DONE for task_id in ids
        ),
        timeout=10.0,
    )
    assert state["peak"] <= 2  # the semaphore + capacity-bounded claim held


async def test_loop_sweeps_while_a_slow_tool_runs(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    """The deliberately-slow-tool acceptance, in-loop: a stale row gets
    requeued (and then re-run) while a slow task occupies a runner slot."""
    slow_id = _insert(db, kind="slow.kind")
    # Aware UTC like the loop's own stamps — the in-loop sweep compares
    # against an aware cutoff, and SQLite compares the rendered strings.
    stale_id = _insert(
        db,
        kind="stale.kind",
        status=AgentTaskStatus.RUNNING,
        attempts=1,
        updated_at=datetime.now(UTC) - timedelta(seconds=600),
    )
    release = asyncio.Event()

    async def executor(task: QueuedTask) -> TaskResult:
        if task.spec.kind == "slow.kind":
            await asyncio.wait_for(release.wait(), timeout=8.0)
        return TaskResult(status="done", result_text="ok")

    worker = _worker(session_factory, executor=executor, concurrency=2)

    def _stale_recovered() -> bool:
        status = _row(db, stale_id).status
        if status == AgentTaskStatus.DONE and not release.is_set():
            # The stale row was requeued, re-claimed, and re-run to done
            # while slow.kind is still held — now let it finish too.
            release.set()
        return (
            status == AgentTaskStatus.DONE
            and _row(db, slow_id).status == AgentTaskStatus.DONE
        )

    await _run_until(worker, _stale_recovered, timeout=10.0)


async def test_loop_times_out_a_hung_executor(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    task_id = _insert(db)

    async def hung_executor(task: QueuedTask) -> TaskResult:
        await asyncio.sleep(60)
        return TaskResult(status="done")

    worker = _worker(session_factory, executor=hung_executor, exec_timeout_s=0.1)
    await _run_until(
        worker, lambda: _row(db, task_id).status == AgentTaskStatus.FAILED
    )
    row = _row(db, task_id)
    assert "took too long" in (row.result_text or "")
    assert "timeout" in (row.error or "")


async def test_loop_records_failed_for_raising_and_illegal_executors(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    raising_id = _insert(db, kind="raises.kind")
    illegal_id = _insert(db, kind="illegal.kind")

    async def executor(task: QueuedTask) -> TaskResult:
        if task.spec.kind == "raises.kind":
            raise RuntimeError("boom")
        return TaskResult(status="cancelled", result_text="not allowed")  # type: ignore[arg-type]

    worker = _worker(session_factory, executor=executor)
    await _run_until(
        worker,
        lambda: all(
            _row(db, task_id).status == AgentTaskStatus.FAILED
            for task_id in (raising_id, illegal_id)
        ),
    )
    assert "executor error: RuntimeError: boom" in (_row(db, raising_id).error or "")


async def test_loop_never_claims_internal_kinds(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    # Fresh aware stamp: the loop's boot sweep must see this internal row as
    # NOT stale (a stale one would legitimately be cancelled) — what's under
    # test here is only the claim exclusion.
    internal_id = _insert(
        db, kind="session.end", updated_at=datetime.now(UTC)
    )
    skill_id = _insert(db)

    async def executor(task: QueuedTask) -> TaskResult:
        assert task.spec.kind not in INTERNAL_TOOL_KINDS
        return TaskResult(status="done", result_text="ok")

    worker = _worker(session_factory, executor=executor)
    await _run_until(
        worker, lambda: _row(db, skill_id).status == AgentTaskStatus.DONE
    )
    assert _row(db, internal_id).status == AgentTaskStatus.QUEUED


async def test_claimed_task_dataclass_is_event_ready() -> None:
    """ClaimedTask carries everything the dual-channel events need."""
    claimed = ClaimedTask(
        task_id=1,
        bot_session_id=7,
        kind="calendar.upcoming_events",
        args={},
        ack_text="on it",
        turn_id=4,
        decision_id=None,
        attempts=1,
    )
    queued = claimed.as_queued_task()
    assert queued.spec.decision_id is None
    assert queued.spec.turn_id == 4
