"""Tests for the Redis-backed approval gate + publish helper."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.services.approval import (
    APPROVAL_CHANNEL_PREFIX,
    SESSION_CHANNEL_PREFIX,
    RedisApprovalGate,
    approval_channel,
    publish_account_relogin_event,
    publish_approval,
    publish_approval_pending_event,
    publish_approval_resolved_event,
    session_channel,
)
from johnny.voice_pipeline.approval import ApprovalRequest


def test_approval_channel_includes_session_id() -> None:
    assert approval_channel("sess-42") == f"{APPROVAL_CHANNEL_PREFIX}sess-42"


def test_session_channel_includes_session_id() -> None:
    assert session_channel("sess-42") == f"{SESSION_CHANNEL_PREFIX}sess-42"


# --- _FakeRedisClient / _FakePubSub ---------------------------------------


class _FakePubSub:
    """Minimal pubsub double mirroring the redis-py async API surface."""

    def __init__(self) -> None:
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.closed = False

    async def subscribe(self, *channels: str) -> None:
        self.subscribed.extend(channels)

    async def unsubscribe(self, *channels: str) -> None:
        self.unsubscribed.extend(channels)

    async def get_message(
        self,
        *,
        ignore_subscribe_messages: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any] | None:
        del ignore_subscribe_messages
        try:
            return await asyncio.wait_for(
                self._queue.get(),
                timeout=timeout if timeout is not None else 0.5,
            )
        except TimeoutError:
            return None

    async def aclose(self) -> None:
        self.closed = True

    def push(self, channel: str, payload: dict[str, Any]) -> None:
        self._queue.put_nowait(
            {
                "type": "message",
                "channel": channel.encode("utf-8"),
                "data": json.dumps(payload).encode("utf-8"),
            }
        )

    def push_raw(self, raw: dict[str, Any]) -> None:
        self._queue.put_nowait(raw)


class _FakeRedis:
    """Fake redis.asyncio.Redis client just enough for the gate + publish_approval."""

    def __init__(self, pubsub: _FakePubSub | None = None) -> None:
        self._pubsub = pubsub or _FakePubSub()
        self.published: list[tuple[str, str]] = []
        self.publish_result = 1
        self.aclosed = False

    def pubsub(self, ignore_subscribe_messages: bool = False) -> _FakePubSub:
        del ignore_subscribe_messages
        return self._pubsub

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return self.publish_result

    async def aclose(self) -> None:
        self.aclosed = True


class _TestRedisApprovalGate(RedisApprovalGate):
    """Subclass that overrides ``_connect`` to inject a fake client."""

    def __init__(self, fake: _FakeRedis, *, session_id: str = "sess") -> None:
        super().__init__(redis_url="redis://test", session_id=session_id)
        self._fake = fake

    async def _connect(self) -> Any:
        if self._pubsub is not None:
            return self._pubsub
        self._client = self._fake
        pubsub = self._fake.pubsub()
        await pubsub.subscribe(approval_channel(self._session_id))
        self._pubsub = pubsub
        return pubsub


async def test_redis_gate_returns_approved_on_matching_message() -> None:
    pubsub = _FakePubSub()
    fake = _FakeRedis(pubsub=pubsub)
    gate = _TestRedisApprovalGate(fake, session_id="sess-1")

    task = asyncio.create_task(
        gate.request_approval(
            ApprovalRequest(decision_id=10, suggested_reply="hi", timeout_s=2.0)
        )
    )
    await asyncio.sleep(0.01)
    pubsub.push(
        approval_channel("sess-1"),
        {"decision_id": 10, "action": "approve"},
    )
    outcome = await task
    assert outcome == "approved"
    assert pubsub.subscribed == [approval_channel("sess-1")]


async def test_redis_gate_returns_rejected_on_reject_action() -> None:
    pubsub = _FakePubSub()
    fake = _FakeRedis(pubsub=pubsub)
    gate = _TestRedisApprovalGate(fake, session_id="s")
    task = asyncio.create_task(
        gate.request_approval(
            ApprovalRequest(decision_id=3, suggested_reply="x", timeout_s=2.0)
        )
    )
    await asyncio.sleep(0.01)
    pubsub.push(approval_channel("s"), {"decision_id": 3, "action": "reject"})
    assert await task == "rejected"


async def test_redis_gate_ignores_other_decision_ids() -> None:
    """Messages for unrelated decisions should NOT resolve our wait."""
    pubsub = _FakePubSub()
    fake = _FakeRedis(pubsub=pubsub)
    gate = _TestRedisApprovalGate(fake, session_id="s")
    task = asyncio.create_task(
        gate.request_approval(
            ApprovalRequest(decision_id=99, suggested_reply="x", timeout_s=2.0)
        )
    )
    await asyncio.sleep(0.01)
    # Push a different decision id — should be ignored.
    pubsub.push(approval_channel("s"), {"decision_id": 1, "action": "approve"})
    await asyncio.sleep(0.05)
    assert not task.done()
    pubsub.push(approval_channel("s"), {"decision_id": 99, "action": "approve"})
    assert await task == "approved"


async def test_redis_gate_drops_malformed_json() -> None:
    pubsub = _FakePubSub()
    fake = _FakeRedis(pubsub=pubsub)
    gate = _TestRedisApprovalGate(fake, session_id="s")
    task = asyncio.create_task(
        gate.request_approval(
            ApprovalRequest(decision_id=5, suggested_reply="x", timeout_s=2.0)
        )
    )
    await asyncio.sleep(0.01)
    # Malformed payload — should be dropped, gate keeps waiting.
    pubsub.push_raw(
        {"type": "message", "channel": b"x", "data": b"not-json"}
    )
    await asyncio.sleep(0.05)
    assert not task.done()
    pubsub.push(approval_channel("s"), {"decision_id": 5, "action": "approve"})
    assert await task == "approved"


async def test_redis_gate_times_out() -> None:
    pubsub = _FakePubSub()
    fake = _FakeRedis(pubsub=pubsub)
    gate = _TestRedisApprovalGate(fake, session_id="s")
    outcome = await gate.request_approval(
        ApprovalRequest(decision_id=7, suggested_reply="x", timeout_s=0.05)
    )
    assert outcome == "timeout"


async def test_redis_gate_close_unsubscribes_and_closes_client() -> None:
    pubsub = _FakePubSub()
    fake = _FakeRedis(pubsub=pubsub)
    gate = _TestRedisApprovalGate(fake, session_id="z")
    # Force connection.
    await gate.request_approval(
        ApprovalRequest(decision_id=1, suggested_reply="x", timeout_s=0.05)
    )
    await gate.close()
    assert pubsub.unsubscribed == [approval_channel("z")]
    assert pubsub.closed is True
    assert fake.aclosed is True


async def test_redis_gate_close_when_never_connected_is_noop() -> None:
    pubsub = _FakePubSub()
    fake = _FakeRedis(pubsub=pubsub)
    gate = _TestRedisApprovalGate(fake, session_id="z")
    await gate.close()
    assert pubsub.unsubscribed == []


async def test_redis_gate_ignores_subscribe_messages() -> None:
    pubsub = _FakePubSub()
    fake = _FakeRedis(pubsub=pubsub)
    gate = _TestRedisApprovalGate(fake, session_id="s")
    task = asyncio.create_task(
        gate.request_approval(
            ApprovalRequest(decision_id=12, suggested_reply="x", timeout_s=2.0)
        )
    )
    await asyncio.sleep(0.01)
    pubsub.push_raw({"type": "subscribe", "channel": b"x", "data": 1})
    await asyncio.sleep(0.05)
    assert not task.done()
    pubsub.push(approval_channel("s"), {"decision_id": 12, "action": "reject"})
    assert await task == "rejected"


async def test_redis_gate_warns_on_unknown_action_and_keeps_waiting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    pubsub = _FakePubSub()
    fake = _FakeRedis(pubsub=pubsub)
    gate = _TestRedisApprovalGate(fake, session_id="s")
    task = asyncio.create_task(
        gate.request_approval(
            ApprovalRequest(decision_id=8, suggested_reply="x", timeout_s=2.0)
        )
    )
    await asyncio.sleep(0.01)
    with caplog.at_level(logging.WARNING, logger="app.services.approval"):
        pubsub.push(approval_channel("s"), {"decision_id": 8, "action": "maybe"})
        await asyncio.sleep(0.05)
        assert not task.done()
        pubsub.push(approval_channel("s"), {"decision_id": 8, "action": "approve"})
        assert await task == "approved"
    assert any("unknown action" in rec.message for rec in caplog.records)


# --- publish_approval helper ----------------------------------------------


async def test_publish_approval_sends_payload_and_returns_subscriber_count() -> None:
    fake = _FakeRedis()
    fake.publish_result = 3
    subs = await publish_approval(fake, "sess-7", 42, "approved")  # type: ignore[arg-type]
    assert subs == 3
    assert len(fake.published) == 1
    channel, payload = fake.published[0]
    assert channel == approval_channel("sess-7")
    body = json.loads(payload)
    assert body == {"decision_id": 42, "action": "approve"}


async def test_publish_approval_maps_rejected_action() -> None:
    fake = _FakeRedis()
    await publish_approval(fake, "x", 1, "rejected")  # type: ignore[arg-type]
    body = json.loads(fake.published[0][1])
    assert body["action"] == "reject"


async def test_publish_approval_rejects_unsupported_action() -> None:
    fake = _FakeRedis()
    with pytest.raises(ValueError):
        await publish_approval(fake, "x", 1, "timeout")  # type: ignore[arg-type]


# --- WS fan-out event helpers (Johnny-hn6) --------------------------------


async def test_publish_approval_pending_event_lands_on_session_channel() -> None:
    fake = _FakeRedis()
    fake.publish_result = 2
    subs = await publish_approval_pending_event(
        fake,  # type: ignore[arg-type]
        session_id="7",
        decision_id=42,
        suggested_reply="how are you?",
        reason="user-asked",
        reply_type="answer",
        timeout_s=12.5,
    )
    assert subs == 2
    assert len(fake.published) == 1
    channel, payload = fake.published[0]
    assert channel == session_channel("7")
    body = json.loads(payload)
    assert body["type"] == "approval_pending"
    assert body["decision_id"] == 42
    assert body["suggested_reply"] == "how are you?"
    assert body["reason"] == "user-asked"
    assert body["reply_type"] == "answer"
    assert body["timeout_s"] == 12.5
    assert body["session_id"] == "7"
    assert isinstance(body["timestamp_ms"], int)


async def test_publish_account_relogin_event_lands_on_session_channel() -> None:
    fake = _FakeRedis()
    fake.publish_result = 3
    subs = await publish_account_relogin_event(
        fake,  # type: ignore[arg-type]
        session_id="7",
        account_id=5,
        account_email="bot@example.com",
        meet_link="https://meet.google.com/abc-defg-hij",
        message="Couldn't join — the account bot@example.com is signed out.",
    )
    assert subs == 3
    assert len(fake.published) == 1
    channel, payload = fake.published[0]
    assert channel == session_channel("7")
    body = json.loads(payload)
    assert body["type"] == "account_relogin_needed"
    assert body["account_id"] == 5
    assert body["account_email"] == "bot@example.com"
    assert body["meet_link"] == "https://meet.google.com/abc-defg-hij"
    assert "bot@example.com" in body["message"]
    assert body["session_id"] == "7"
    assert isinstance(body["timestamp_ms"], int)


async def test_publish_approval_resolved_event_lands_on_session_channel() -> None:
    fake = _FakeRedis()
    subs = await publish_approval_resolved_event(
        fake,  # type: ignore[arg-type]
        session_id="9",
        decision_id=11,
        resolution="approved",
    )
    assert subs == 1
    channel, payload = fake.published[0]
    assert channel == session_channel("9")
    body = json.loads(payload)
    assert body == {
        "type": "approval_resolved",
        "decision_id": 11,
        "resolution": "approved",
        "timestamp_ms": body["timestamp_ms"],
        "session_id": "9",
    }
    assert isinstance(body["timestamp_ms"], int)
