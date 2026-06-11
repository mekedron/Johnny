"""Integration: task events published on Redis arrive on /ws/sessions/{id}.

The Johnny-trt.25 acceptance gate for the fan-out leg: a task lifecycle
event published through the REAL producer path
(:class:`johnny.voice_pipeline.event_bus.RedisEventBus` →
``johnny.session.<id>`` on a real Redis) must surface as a frame on the
real ``/ws/sessions/{session_id}`` endpoint with the default (Redis-backed)
event stream factory — no in-memory doubles anywhere on the path.

Needs the compose stack's Redis; the intended runner is::

    docker compose exec api pytest tests/integration/test_task_events_ws.py

When Redis is unreachable (host-side run, CI without the stack) the module
skips loudly rather than failing — the dev-stack run is the acceptance
gate, not an everywhere-green unit suite.

Redis pub/sub has no replay, so the test cannot know when the endpoint's
SUBSCRIBE lands — it republishes the identical event every ~200 ms until
the first frame arrives (bounded). Duplicate frames are harmless: the
receiver stops at the first one and the WS teardown discards the rest.

No database access: the WS endpoint subscribes by channel name without
validating the session id, so a synthetic id keeps the dev DB untouched.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Any

import pytest

from app.config import get_settings

REDIS_URL = get_settings().redis_url

# Bounded wait for the subscribe-then-deliver round trip. Generous because
# the TestClient portal, the endpoint's lazy Redis connect, and the publish
# loop all race; observed end-to-end latency in-container is well under 1 s.
RECEIVE_DEADLINE_S = 15.0
REPUBLISH_INTERVAL_S = 0.2


def _redis_reachable() -> bool:
    try:
        import redis

        client = redis.Redis.from_url(
            REDIS_URL, socket_connect_timeout=2.0, socket_timeout=2.0
        )
        try:
            return bool(client.ping())
        finally:
            client.close()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _redis_reachable(),
    reason=(
        f"redis not reachable at {REDIS_URL} — run inside the compose "
        "stack: docker compose exec api pytest "
        "tests/integration/test_task_events_ws.py"
    ),
)


def _publish_once(event: Any) -> None:
    """Publish ``event`` through the production RedisEventBus, then close."""

    async def _go() -> None:
        from redis.asyncio import Redis

        from johnny.voice_pipeline.event_bus import RedisEventBus

        bus = RedisEventBus(Redis.from_url(REDIS_URL))
        try:
            await bus.publish(event)
        finally:
            await bus.close()

    asyncio.run(_go())


def _receive_first_frame(events: list[Any]) -> dict[str, Any]:
    """Drive the real endpoint and return the first frame it forwards.

    ``events`` are republished round-robin until a frame lands (pub/sub
    subscribe race, see module docstring). Raises on deadline so a broken
    fan-out fails the test instead of hanging it.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    session_id = events[0].session_id
    frames: list[dict[str, Any]] = []

    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/sessions/{session_id}") as ws:

            def _recv() -> None:
                try:
                    frames.append(ws.receive_json())
                except Exception:
                    # WS closed by the deadline path below — nothing to record.
                    pass

            receiver = threading.Thread(target=_recv, daemon=True)
            receiver.start()
            deadline = time.monotonic() + RECEIVE_DEADLINE_S
            i = 0
            while receiver.is_alive() and time.monotonic() < deadline:
                _publish_once(events[i % len(events)])
                i += 1
                receiver.join(timeout=REPUBLISH_INTERVAL_S)
        # Context exit closes the WS, unblocking a still-waiting receiver.
        receiver.join(timeout=5.0)

    assert frames, (
        f"no WS frame arrived on /ws/sessions/{session_id} within "
        f"{RECEIVE_DEADLINE_S}s ({i} publishes attempted)"
    )
    return frames[0]


def test_task_completed_published_on_redis_arrives_on_session_ws() -> None:
    from johnny.voice_pipeline.events import TaskCompleted

    session_id = f"trt25-it-{uuid.uuid4().hex[:8]}"
    event = TaskCompleted(
        task_id=4242,
        kind="calendar.upcoming_events",
        status="done",
        timestamp_ms=1_000,
        result_text="You have 3 events this week.",
        error="",
        turn_id=7,
        session_id=session_id,
    )

    frame = _receive_first_frame([event])

    assert frame["type"] == "task_completed"
    assert frame["task_id"] == 4242
    assert frame["kind"] == "calendar.upcoming_events"
    assert frame["status"] == "done"
    assert frame["result_text"] == "You have 3 events this week."
    assert frame["turn_id"] == 7
    assert frame["session_id"] == session_id
    assert frame["seq"] >= 1


def test_task_queued_and_progress_and_expired_arrive_on_session_ws() -> None:
    """The remaining three types ride the same path — one frame proves each
    serialises through the real bus and back out of the real endpoint."""
    from johnny.voice_pipeline.events import (
        TaskProgress,
        TaskQueued,
        TaskResultExpired,
    )

    for event, expected in (
        (
            TaskQueued(
                task_id=1,
                kind="calendar.upcoming_events",
                timestamp_ms=10,
                ack_text="on it",
                session_id=f"trt25-it-{uuid.uuid4().hex[:8]}",
            ),
            {"type": "task_queued", "ack_text": "on it"},
        ),
        (
            TaskProgress(
                task_id=2,
                kind="calendar.upcoming_events",
                timestamp_ms=20,
                progress_text="searching",
                session_id=f"trt25-it-{uuid.uuid4().hex[:8]}",
            ),
            {"type": "task_progress", "progress_text": "searching"},
        ),
        (
            TaskResultExpired(
                task_id=3,
                kind="calendar.upcoming_events",
                timestamp_ms=30,
                reason="undelivered for 120s",
                session_id=f"trt25-it-{uuid.uuid4().hex[:8]}",
            ),
            {"type": "task_result_expired", "reason": "undelivered for 120s"},
        ),
    ):
        frame = _receive_first_frame([event])
        for key, value in expected.items():
            assert frame[key] == value, event.type
        assert frame["session_id"] == event.session_id
