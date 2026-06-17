"""Unit tests for the inbound session control channel (US-302, Johnny-d6w.17).

The wire contract + the listener's message routing onto
:meth:`TaskCoordinator.cancel_task` — no real Redis (the subscribe loop is the
proven RedisApprovalGate shape, exercised at integration level).
"""

from __future__ import annotations

import json
from typing import Any

from johnny.agent.session_control import (
    SessionControlListener,
    build_cancel_command,
    control_channel,
    publish_cancel,
)


class _FakeRedis:
    def __init__(self, *, result: int = 1) -> None:
        self.published: list[tuple[str, str]] = []
        self.result = result

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return self.result


class _RecordingCoordinator:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def cancel_task(self, task_id: int, *, actor: str) -> str:
        self.calls.append((task_id, actor))
        return "cancelling"


def test_control_channel_and_command_wire_shape() -> None:
    assert control_channel("42") == "johnny.control.42"
    assert json.loads(build_cancel_command(7, actor="ui")) == {
        "action": "cancel",
        "task_id": 7,
        "actor": "ui",
    }


async def test_publish_cancel_targets_the_control_channel() -> None:
    redis = _FakeRedis(result=1)
    subscribers = await publish_cancel(redis, "42", 7, actor="ui")
    assert subscribers == 1
    assert redis.published == [
        (
            "johnny.control.42",
            json.dumps(
                {"action": "cancel", "task_id": 7, "actor": "ui"},
                separators=(",", ":"),
            ),
        )
    ]


def _listener(coord: Any) -> SessionControlListener:
    return SessionControlListener(
        redis_url="redis://unused", session_id="42", coordinator=coord
    )


async def test_listener_routes_cancel_to_coordinator() -> None:
    coord = _RecordingCoordinator()
    listener = _listener(coord)
    await listener._handle(
        json.dumps({"action": "cancel", "task_id": 7, "actor": "voice"}).encode()
    )
    assert coord.calls == [(7, "voice")]


async def test_listener_defaults_unknown_actor_to_ui() -> None:
    coord = _RecordingCoordinator()
    await _listener(coord)._handle(
        json.dumps({"action": "cancel", "task_id": 7, "actor": "bogus"}).encode()
    )
    assert coord.calls == [(7, "ui")]


async def test_listener_ignores_non_cancel_and_malformed() -> None:
    coord = _RecordingCoordinator()
    listener = _listener(coord)
    await listener._handle(json.dumps({"action": "other", "task_id": 7}).encode())
    await listener._handle(json.dumps({"action": "cancel"}).encode())  # no task_id
    await listener._handle(b"not json")
    await listener._handle(json.dumps([1, 2, 3]).encode())  # not a dict
    assert coord.calls == []


async def test_listener_contains_a_raising_coordinator() -> None:
    class _Boom:
        async def cancel_task(self, task_id: int, *, actor: str) -> str:
            raise RuntimeError("coordinator down")

    # never raises out of _handle — a bad cancel can't crash the subscribe loop
    await _listener(_Boom())._handle(
        json.dumps({"action": "cancel", "task_id": 7, "actor": "ui"}).encode()
    )
