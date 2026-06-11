"""Integration: the worker executor pass against the compose stack (Johnny-trt.24).

Real Postgres (claim atomicity needs ``FOR UPDATE SKIP LOCKED``) and real
Redis (the wake subscription + dual-channel announce). The intended runner::

    docker compose exec api pytest tests/integration/test_task_worker.py

Skips loudly off-stack (the trt.35 pattern) — the dev-stack run is the
acceptance gate.

Shared-stack discipline: the LIVE dev worker runs this same executor pass
against the same ``agent_tasks`` table, claiming every non-internal kind. So
every row these tests insert uses an INTERNAL kind (``session.end``) — which
the live worker's claim excludes by definition — and the test-local
:class:`TaskWorker` instances invert the scoping (``exclude_kinds=∅`` +
``only_kinds={'session.end'}``) so they claim exactly the test rows and can
never touch real queued work. A pre-test guard skips if foreign in-flight
``session.end`` rows exist (a crashed live session's strays could otherwise
be claimed by us, oldest-first).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from app.config import get_settings

REDIS_URL = get_settings().redis_url

TEST_KIND = "session.end"
TEST_ONLY_KINDS = frozenset({TEST_KIND})
NO_EXCLUDES: frozenset[str] = frozenset()

WAKE_DISPATCH_DEADLINE_S = 2.0  # the bead's acceptance bound
EVENT_DEADLINE_S = 10.0


def _stack_reachable() -> str | None:
    try:
        import redis

        client = redis.Redis.from_url(
            REDIS_URL, socket_connect_timeout=2.0, socket_timeout=2.0
        )
        try:
            if not client.ping():
                return "redis did not pong"
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001
        return f"redis not reachable: {exc}"
    try:
        from app.db.session import engine

        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        return f"postgres not reachable: {exc}"
    return None


_SKIP_REASON = _stack_reachable()
pytestmark = pytest.mark.skipif(
    _SKIP_REASON is not None,
    reason=(
        f"{_SKIP_REASON} — run inside the compose stack: "
        "docker compose exec api pytest tests/integration/test_task_worker.py"
    ),
)


@pytest.fixture
def bot_session_id() -> Iterator[int]:
    """A synthetic browser-source bot_sessions row; cleanup cascades tasks."""
    from app.db.models import BotSession, BotSessionSource, BotSessionStatus
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        foreign = db.scalar(
            sa.text(
                "SELECT count(*) FROM agent_tasks "
                "WHERE kind = :kind AND status IN ('queued', 'running')"
            ),
            {"kind": TEST_KIND},
        )
        if foreign:
            pytest.skip(
                f"{foreign} in-flight {TEST_KIND} rows already exist on this "
                "stack — refusing to run kind-scoped claimers next to them"
            )
        row = BotSession(
            source=BotSessionSource.BROWSER,
            status=BotSessionStatus.ENDED,
            bot_name=f"trt24-it-{uuid.uuid4().hex[:8]}",
        )
        db.add(row)
        db.commit()
        session_id = int(row.id)
    finally:
        db.close()

    yield session_id

    db = SessionLocal()
    try:
        db.execute(
            sa.text("DELETE FROM bot_sessions WHERE id = :id"), {"id": session_id}
        )
        db.commit()
    finally:
        db.close()


def _insert_task(bot_session_id: int, **overrides: Any) -> int:
    from app.db.models import AgentTask, AgentTaskStatus
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        row = AgentTask(
            bot_session_id=bot_session_id,
            kind=TEST_KIND,
            request_json={"kind": TEST_KIND, "args": {}, "ack": "on it"},
            status=AgentTaskStatus.QUEUED,
            ack_text="on it",
            turn_id=overrides.pop("turn_id", 4),
            **overrides,
        )
        db.add(row)
        db.commit()
        return int(row.id)
    finally:
        db.close()


def _task_row(task_id: int) -> tuple[str, str | None, int]:
    """(status, result_text, attempts) — fresh session per read."""
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        row = db.execute(
            sa.text(
                "SELECT status, result_text, attempts FROM agent_tasks WHERE id = :id"
            ),
            {"id": task_id},
        ).one()
        return (str(row.status), row.result_text, int(row.attempts))
    finally:
        db.close()


def _backdate(
    task_id: int,
    *,
    seconds: float,
    status: str | None = None,
    attempts: int | None = None,
) -> None:
    from app.db.session import SessionLocal

    sets = ["updated_at = :stamp"]
    params: dict[str, Any] = {
        "id": task_id,
        "stamp": datetime.now(UTC) - timedelta(seconds=seconds),
    }
    if status is not None:
        sets.append("status = :status")
        params["status"] = status
    if attempts is not None:
        sets.append("attempts = :attempts")
        params["attempts"] = attempts
    db = SessionLocal()
    try:
        db.execute(
            sa.text(f"UPDATE agent_tasks SET {', '.join(sets)} WHERE id = :id"),
            params,
        )
        db.commit()
    finally:
        db.close()


# --- 1. wake latency + persisted result + both channels announced -----------------


async def test_wake_dispatch_within_two_seconds_and_dual_channel_events(
    bot_session_id: int,
) -> None:
    """queued → running ≤ 2 s after the wake ping; result persisted; both
    ``johnny.session.<id>`` and ``johnny.tasks.<id>`` receive the events."""
    from redis.asyncio import Redis

    from app.services.task_worker import TaskWorker
    from johnny.agent.task_wiring import RedisTaskWake
    from johnny.agent.tasks import QueuedTask, TaskResult, TaskSpec

    async def executor(task: QueuedTask) -> TaskResult:
        return TaskResult(
            status="done",
            result_text="integration says hi",
            result_json={"exit_code": 0},
        )

    worker = TaskWorker(
        redis_url=REDIS_URL,
        executor=executor,
        poll_interval_s=30.0,  # long on purpose: the WAKE must drive dispatch
        concurrency=2,
        sweep_interval_s=600.0,
        exclude_kinds=NO_EXCLUDES,
        only_kinds=TEST_ONLY_KINDS,
    )

    frames: list[tuple[str, dict[str, Any]]] = []
    subscriber = Redis.from_url(REDIS_URL, decode_responses=True)
    pubsub = subscriber.pubsub(ignore_subscribe_messages=True)
    session_channel = f"johnny.session.{bot_session_id}"
    tasks_channel = f"johnny.tasks.{bot_session_id}"
    await pubsub.subscribe(session_channel, tasks_channel)

    async def _collect() -> None:
        while True:
            try:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=0.5
                )
            except TimeoutError:
                continue
            if message is None or message.get("type") != "message":
                continue
            try:
                frames.append((message["channel"], json.loads(message["data"])))
            except (KeyError, ValueError):
                continue

    collector = asyncio.ensure_future(_collect())
    runner = asyncio.ensure_future(worker.run())
    wake = RedisTaskWake(redis_url=REDIS_URL, session_id=str(bot_session_id))
    try:
        await asyncio.sleep(0.5)  # let the wake subscription land
        task_id = _insert_task(bot_session_id)
        queued = QueuedTask(task_id=task_id, spec=TaskSpec(kind=TEST_KIND))

        dispatch_started = time.monotonic()
        await wake(queued)  # the REAL production wake publisher
        left_queued_at: float | None = None
        while time.monotonic() - dispatch_started < WAKE_DISPATCH_DEADLINE_S:
            status, _, _ = _task_row(task_id)
            if status != "queued":
                left_queued_at = time.monotonic()
                break
            await asyncio.sleep(0.05)
        assert left_queued_at is not None, (
            f"row still queued {WAKE_DISPATCH_DEADLINE_S}s after the wake ping "
            "— the wake subscription did not dispatch it"
        )
        dispatch_latency = left_queued_at - dispatch_started
        assert dispatch_latency <= WAKE_DISPATCH_DEADLINE_S

        # Result persisted on the row (the durable record).
        deadline = time.monotonic() + EVENT_DEADLINE_S
        while time.monotonic() < deadline:
            status, result_text, attempts = _task_row(task_id)
            if status == "done":
                break
            await asyncio.sleep(0.05)
        assert status == "done"
        assert result_text == "integration says hi"
        assert attempts == 1

        # Both channels got TaskProgress (claim) and TaskCompleted (settle).
        def _types(channel: str) -> set[str]:
            return {
                frame["type"]
                for chan, frame in frames
                if chan == channel and frame.get("task_id") == task_id
            }

        deadline = time.monotonic() + EVENT_DEADLINE_S
        while time.monotonic() < deadline:
            if {"task_progress", "task_completed"} <= _types(session_channel) and {
                "task_progress",
                "task_completed",
            } <= _types(tasks_channel):
                break
            await asyncio.sleep(0.1)
        assert {"task_progress", "task_completed"} <= _types(session_channel), frames
        assert {"task_progress", "task_completed"} <= _types(tasks_channel), frames
        completed = next(
            frame
            for chan, frame in frames
            if chan == tasks_channel and frame.get("type") == "task_completed"
        )
        assert completed["status"] == "done"
        assert completed["result_text"] == "integration says hi"
        assert completed["session_id"] == str(bot_session_id)
    finally:
        worker.request_stop()
        await asyncio.wait_for(runner, timeout=15.0)
        collector.cancel()
        try:
            await collector
        except asyncio.CancelledError:
            pass
        await pubsub.aclose()
        await subscriber.aclose()
        await wake.close()


# --- 2. claim race: two concurrent claimers, disjoint results ---------------------


def test_claim_race_two_concurrent_claimers_split_disjointly(
    bot_session_id: int,
) -> None:
    """``FOR UPDATE SKIP LOCKED`` under real concurrency: both claimers ask
    for everything while the other's transaction is still open — every row is
    claimed exactly once."""
    from app.db.session import SessionLocal
    from app.services.task_worker import claim_queued_tasks

    task_ids = {_insert_task(bot_session_id) for _ in range(6)}
    barrier = threading.Barrier(2, timeout=15.0)
    results: dict[str, list[int]] = {}
    errors: list[Exception] = []

    def _claimer(name: str) -> None:
        db = SessionLocal()
        try:
            barrier.wait()  # both enter their claim transactions together
            claimed = claim_queued_tasks(
                db,
                limit=len(task_ids),
                exclude_kinds=NO_EXCLUDES,
                only_kinds=TEST_ONLY_KINDS,
            )
            barrier.wait()  # both hold their locks before either commits
            db.commit()
            results[name] = [task.task_id for task in claimed]
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            db.rollback()
        finally:
            db.close()

    threads = [
        threading.Thread(target=_claimer, args=(name,)) for name in ("a", "b")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30.0)

    assert not errors, errors
    claimed_a, claimed_b = set(results["a"]), set(results["b"])
    assert claimed_a.isdisjoint(claimed_b), (claimed_a, claimed_b)
    assert claimed_a | claimed_b == task_ids  # nothing lost, nothing doubled
    for task_id in task_ids:
        status, _, attempts = _task_row(task_id)
        assert status == "running" and attempts == 1


# --- 3. crash requeue: TTL, attempts increment, fenced stale settle ----------------


def test_crash_requeue_increments_attempts_and_fences_the_stale_runner(
    bot_session_id: int,
) -> None:
    from app.db.session import SessionLocal
    from app.services.task_worker import (
        claim_queued_tasks,
        settle_claimed_task,
        sweep_stale_tasks,
    )

    task_id = _insert_task(bot_session_id)

    def _claim_one() -> Any:
        db = SessionLocal()
        try:
            claimed = claim_queued_tasks(
                db, limit=1, exclude_kinds=NO_EXCLUDES, only_kinds=TEST_ONLY_KINDS
            )
            db.commit()
            assert [task.task_id for task in claimed] == [task_id]
            return claimed[0]
        finally:
            db.close()

    first = _claim_one()
    assert first.attempts == 1

    # The claiming worker "crashes": the row goes stale past the TTL.
    _backdate(task_id, seconds=600)
    db = SessionLocal()
    try:
        # internal_kinds overridden: in production session.end is never
        # claimed at all; here it stands in for a normal worker-owned kind.
        swept = sweep_stale_tasks(
            db,
            ttl_s=300,
            max_attempts=3,
            internal_kinds=NO_EXCLUDES,
            only_kinds=TEST_ONLY_KINDS,
        )
        db.commit()
    finally:
        db.close()
    assert swept.requeued == (task_id,)
    status, _, attempts = _task_row(task_id)
    assert status == "queued" and attempts == 1  # attempts preserved on requeue

    second = _claim_one()
    assert second.attempts == 2  # the rerun increments

    # The first (presumed-dead, actually straggling) runner settles late:
    # the attempts fence rejects it — no clobber, and the caller publishes
    # no event for it (the no-duplicate-completions acceptance).
    db = SessionLocal()
    try:
        stale = settle_claimed_task(
            db,
            task_id=task_id,
            claim_attempts=first.attempts,
            status="done",
            result_text="stale ghost result",
        )
        db.commit()
    finally:
        db.close()
    assert stale is False
    status, result_text, attempts = _task_row(task_id)
    assert status == "running" and attempts == 2 and result_text is None

    # The legitimate second claim settles exactly once.
    db = SessionLocal()
    try:
        fresh = settle_claimed_task(
            db,
            task_id=task_id,
            claim_attempts=second.attempts,
            status="done",
            result_text="real result",
        )
        db.commit()
    finally:
        db.close()
    assert fresh is True
    status, result_text, _ = _task_row(task_id)
    assert status == "done" and result_text == "real result"


def test_attempts_cap_settles_failed_with_honest_speech(bot_session_id: int) -> None:
    from app.db.session import SessionLocal
    from app.services.task_worker import sweep_stale_tasks

    task_id = _insert_task(bot_session_id)
    _backdate(task_id, seconds=600, status="running", attempts=3)

    db = SessionLocal()
    try:
        swept = sweep_stale_tasks(
            db,
            ttl_s=300,
            max_attempts=3,
            internal_kinds=NO_EXCLUDES,
            only_kinds=TEST_ONLY_KINDS,
        )
        db.commit()
    finally:
        db.close()
    assert [task.task_id for task in swept.failed] == [task_id]
    assert swept.failed[0].bot_session_id == bot_session_id
    status, result_text, _ = _task_row(task_id)
    assert status == "failed"
    assert "I couldn't finish the session.end task" in (result_text or "")
