"""Tests for the WebSocket endpoints (US-031)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.ws import (
    GLOBAL_CHANNEL_PREFIX,
    GLOBAL_FORWARDED_SESSION_TYPES,
    SESSION_CHANNEL_PREFIX,
    WIRE_TYPE_MAP,
    EventStream,
    InMemoryEventStream,
    RedisEventStream,
    set_event_stream_factory,
    to_wire_type,
)
from app.main import app

# --- Type translation ------------------------------------------------------


def test_to_wire_type_maps_known_internal_names() -> None:
    assert to_wire_type("transcript_finalized") == "transcript_final"
    assert to_wire_type("router_decision_made") == "router_decision"
    assert to_wire_type("session_status_changed") == "session_status_change"


def test_to_wire_type_passes_through_unmapped() -> None:
    # Future event types (calendar_event_changed, transcript_partial,
    # approval_pending, agent_spoke, agent_suggested) pass through unchanged.
    assert to_wire_type("agent_spoke") == "agent_spoke"
    assert to_wire_type("agent_suggested") == "agent_suggested"
    assert to_wire_type("calendar_event_changed") == "calendar_event_changed"
    assert to_wire_type("transcript_partial") == "transcript_partial"
    assert to_wire_type("approval_pending") == "approval_pending"
    assert to_wire_type("custom_event") == "custom_event"


def test_wire_type_map_covers_renamed_pipeline_events() -> None:
    # Guard against accidental removal of any of the AC-listed event types
    # whose internal names differ from the wire names.
    for internal in ("transcript_finalized", "router_decision_made", "session_status_changed"):
        assert internal in WIRE_TYPE_MAP


def test_global_forwarded_types_includes_session_status() -> None:
    assert "session_status_change" in GLOBAL_FORWARDED_SESSION_TYPES


# --- In-memory stream behavior --------------------------------------------


async def test_in_memory_stream_yields_pushed_messages() -> None:
    stream = InMemoryEventStream()
    await stream.push("channel-a", {"type": "x", "v": 1})
    await stream.push("channel-b", {"type": "y", "v": 2})

    received: list[tuple[str, dict[str, Any]]] = []

    async def consume() -> None:
        async for item in stream.messages():
            received.append(item)
            if len(received) == 2:
                await stream.close()

    await asyncio.wait_for(consume(), timeout=1.0)
    assert received == [
        ("channel-a", {"type": "x", "v": 1}),
        ("channel-b", {"type": "y", "v": 2}),
    ]


async def test_in_memory_stream_close_unblocks_consumer() -> None:
    stream = InMemoryEventStream()

    async def consume() -> int:
        count = 0
        async for _ in stream.messages():
            count += 1
        return count

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    await stream.close()
    result = await asyncio.wait_for(task, timeout=1.0)
    assert result == 0


async def test_in_memory_stream_drops_pushes_after_close() -> None:
    stream = InMemoryEventStream()
    await stream.close()
    # Should not raise; should be silently dropped.
    await stream.push("c", {"type": "z"})


# --- Endpoint behavior ----------------------------------------------------


@pytest.fixture
def stream() -> Iterator[InMemoryEventStream]:
    """Inject one shared in-memory stream into the WebSocket endpoint."""
    s = InMemoryEventStream()

    def _factory(_patterns: tuple[str, ...], _channels: tuple[str, ...]) -> EventStream:
        return s

    set_event_stream_factory(_factory)
    try:
        yield s
    finally:
        set_event_stream_factory(None)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _push(stream: InMemoryEventStream, channel: str, payload: dict[str, Any]) -> None:
    """Push from sync test code by running the async push to completion."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(stream.push(channel, payload))
    finally:
        loop.close()


def test_session_ws_streams_envelope_with_seq_and_wire_type(
    client: TestClient, stream: InMemoryEventStream
) -> None:
    with client.websocket_connect("/ws/sessions/sess-1") as ws:
        _push(
            stream,
            f"{SESSION_CHANNEL_PREFIX}sess-1",
            {"type": "transcript_finalized", "text": "hi", "timestamp_ms": 10},
        )
        envelope = ws.receive_json()
        assert envelope["seq"] == 1
        assert envelope["type"] == "transcript_final"
        assert envelope["text"] == "hi"
        assert envelope["timestamp_ms"] == 10


def test_session_ws_increments_seq_per_frame(
    client: TestClient, stream: InMemoryEventStream
) -> None:
    with client.websocket_connect("/ws/sessions/sess-1") as ws:
        _push(
            stream,
            f"{SESSION_CHANNEL_PREFIX}sess-1",
            {
                "type": "router_decision_made",
                "should_speak": False,
                "confidence": 0.1,
                "reason": "no",
            },
        )
        e1 = ws.receive_json()
        _push(
            stream,
            f"{SESSION_CHANNEL_PREFIX}sess-1",
            {
                "type": "agent_spoke",
                "text": "hi",
                "audio_duration_ms": 0,
                "timestamp_ms": 5,
            },
        )
        e2 = ws.receive_json()
        _push(
            stream,
            f"{SESSION_CHANNEL_PREFIX}sess-1",
            {"type": "session_status_changed", "status": "joined", "timestamp_ms": 9},
        )
        e3 = ws.receive_json()
    assert (e1["seq"], e2["seq"], e3["seq"]) == (1, 2, 3)
    assert e1["type"] == "router_decision"
    assert e2["type"] == "agent_spoke"
    assert e3["type"] == "session_status_change"


def test_session_ws_filters_other_sessions(
    client: TestClient, stream: InMemoryEventStream
) -> None:
    with client.websocket_connect("/ws/sessions/sess-1") as ws:
        # Event for a different session — must NOT be forwarded.
        _push(
            stream,
            f"{SESSION_CHANNEL_PREFIX}sess-other",
            {"type": "transcript_finalized", "text": "nope"},
        )
        _push(
            stream,
            f"{SESSION_CHANNEL_PREFIX}sess-1",
            {"type": "transcript_finalized", "text": "yep"},
        )
        envelope = ws.receive_json()
    assert envelope["text"] == "yep"
    assert envelope["seq"] == 1  # the dropped event did NOT advance seq


def test_session_ws_since_seq_resumes_numbering(
    client: TestClient, stream: InMemoryEventStream
) -> None:
    with client.websocket_connect("/ws/sessions/sess-1?since_seq=10") as ws:
        _push(
            stream,
            f"{SESSION_CHANNEL_PREFIX}sess-1",
            {"type": "transcript_finalized", "text": "post-reconnect"},
        )
        envelope = ws.receive_json()
    assert envelope["seq"] == 11


def test_session_ws_rejects_negative_since_seq(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/sessions/sess-1?since_seq=-5") as ws:
            ws.receive_text()


def test_session_ws_handles_partial_transcript_passthrough(
    client: TestClient, stream: InMemoryEventStream
) -> None:
    # Confirms that a future-emitted ``transcript_partial`` event would
    # reach the consumer with its wire type unchanged (no mapping needed).
    with client.websocket_connect("/ws/sessions/sess-1") as ws:
        _push(
            stream,
            f"{SESSION_CHANNEL_PREFIX}sess-1",
            {"type": "transcript_partial", "text": "par", "timestamp_ms": 1},
        )
        envelope = ws.receive_json()
    assert envelope["type"] == "transcript_partial"
    assert envelope["text"] == "par"


def test_session_ws_handles_approval_pending_passthrough(
    client: TestClient, stream: InMemoryEventStream
) -> None:
    with client.websocket_connect("/ws/sessions/sess-1") as ws:
        _push(
            stream,
            f"{SESSION_CHANNEL_PREFIX}sess-1",
            {"type": "approval_pending", "suggested_reply": "yes", "timestamp_ms": 2},
        )
        envelope = ws.receive_json()
    assert envelope["type"] == "approval_pending"
    assert envelope["suggested_reply"] == "yes"


def test_session_ws_handles_agent_suggested_passthrough(
    client: TestClient, stream: InMemoryEventStream
) -> None:
    """``agent_suggested`` is the suggest-only UI notification — the wire
    type passes through unchanged so the per-session feed can render it."""
    with client.websocket_connect("/ws/sessions/sess-1") as ws:
        _push(
            stream,
            f"{SESSION_CHANNEL_PREFIX}sess-1",
            {
                "type": "agent_suggested",
                "suggested_reply": "Hello",
                "decision_id": 7,
                "reason": "addressed bot",
                "timestamp_ms": 3,
            },
        )
        envelope = ws.receive_json()
    assert envelope["type"] == "agent_suggested"
    assert envelope["suggested_reply"] == "Hello"
    assert envelope["decision_id"] == 7


def test_global_ws_forwards_calendar_changes(
    client: TestClient, stream: InMemoryEventStream
) -> None:
    with client.websocket_connect("/ws/global") as ws:
        _push(
            stream,
            f"{GLOBAL_CHANNEL_PREFIX}calendar",
            {
                "type": "calendar_event_changed",
                "kind": "updated",
                "account_id": 1,
                "event_id": 7,
            },
        )
        envelope = ws.receive_json()
    assert envelope["type"] == "calendar_event_changed"
    assert envelope["kind"] == "updated"
    assert envelope["event_id"] == 7
    assert envelope["seq"] == 1


def test_global_ws_forwards_session_status_change_only(
    client: TestClient, stream: InMemoryEventStream
) -> None:
    with client.websocket_connect("/ws/global") as ws:
        # Should NOT be forwarded — transcripts are noisy and belong on
        # the per-session WS.
        _push(
            stream,
            f"{SESSION_CHANNEL_PREFIX}sess-1",
            {"type": "transcript_finalized", "text": "noise"},
        )
        # Should be forwarded.
        _push(
            stream,
            f"{SESSION_CHANNEL_PREFIX}sess-1",
            {"type": "session_status_changed", "status": "joined", "session_id": "sess-1"},
        )
        envelope = ws.receive_json()
    assert envelope["type"] == "session_status_change"
    assert envelope["status"] == "joined"
    assert envelope["session_id"] == "sess-1"
    assert envelope["seq"] == 1


def test_global_ws_skips_session_only_events(
    client: TestClient, stream: InMemoryEventStream
) -> None:
    with client.websocket_connect("/ws/global") as ws:
        _push(
            stream,
            f"{SESSION_CHANNEL_PREFIX}sess-1",
            {
                "type": "router_decision_made",
                "should_speak": True,
                "confidence": 0.9,
                "reason": "ok",
            },
        )
        _push(
            stream,
            f"{SESSION_CHANNEL_PREFIX}sess-1",
            {"type": "session_status_changed", "status": "ended"},
        )
        envelope = ws.receive_json()
    # The router_decision_made was dropped; only the status change came through.
    assert envelope["type"] == "session_status_change"
    assert envelope["seq"] == 1


def test_global_ws_since_seq_resumes_numbering(
    client: TestClient, stream: InMemoryEventStream
) -> None:
    with client.websocket_connect("/ws/global?since_seq=99") as ws:
        _push(
            stream,
            f"{GLOBAL_CHANNEL_PREFIX}calendar",
            {"type": "calendar_event_changed", "kind": "created"},
        )
        envelope = ws.receive_json()
    assert envelope["seq"] == 100


def test_session_ws_close_on_client_disconnect_releases_stream(
    client: TestClient, stream: InMemoryEventStream
) -> None:
    # Mostly a sanity test — opening and immediately closing the WS
    # should not deadlock the server.
    with client.websocket_connect("/ws/sessions/sess-1"):
        pass
    # If the test reaches here, the endpoint cleaned up.


def test_session_ws_envelope_is_strict_json(
    client: TestClient, stream: InMemoryEventStream
) -> None:
    """The envelope is sent via send_text(json.dumps(...)), so it must
    round-trip through json.loads on the receiver."""
    with client.websocket_connect("/ws/sessions/sess-1") as ws:
        _push(
            stream,
            f"{SESSION_CHANNEL_PREFIX}sess-1",
            {
                "type": "agent_spoke",
                "text": "héllo — wörld",
                "audio_duration_ms": 250,
                "timestamp_ms": 12,
            },
        )
        text = ws.receive_text()
        envelope = json.loads(text)
    assert envelope["text"] == "héllo — wörld"
    assert envelope["audio_duration_ms"] == 250


# --- RedisEventStream with a fake redis client -----------------------------


class _StopSentinel:
    """Distinct sentinel — avoids confusing ``None`` (= no message yet)."""


_STOP = _StopSentinel()


class _FakePubSub:
    def __init__(self) -> None:
        self.psubscribed: list[str] = []
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.punsubscribed: list[str] = []
        self.closed = False
        self._messages: asyncio.Queue[dict[str, Any] | _StopSentinel] = (
            asyncio.Queue()
        )

    async def psubscribe(self, *patterns: str) -> None:
        self.psubscribed.extend(patterns)

    async def subscribe(self, *channels: str) -> None:
        self.subscribed.extend(channels)

    async def punsubscribe(self, *patterns: str) -> None:
        self.punsubscribed.extend(patterns)

    async def unsubscribe(self, *channels: str) -> None:
        self.unsubscribed.extend(channels)

    async def get_message(
        self,
        ignore_subscribe_messages: bool = False,  # noqa: ARG002
        timeout: float | None = None,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        item = await self._messages.get()
        if isinstance(item, _StopSentinel):
            raise asyncio.CancelledError("test stream stopped")
        return item

    async def aclose(self) -> None:
        self.closed = True

    # Test helpers
    def push_message(
        self, *, channel: str, data: bytes, kind: str = "pmessage"
    ) -> None:
        self._messages.put_nowait(
            {
                "type": kind,
                "channel": channel.encode("utf-8"),
                "data": data,
            }
        )

    def push_stop(self) -> None:
        self._messages.put_nowait(_STOP)


class _FakeRedisClient:
    def __init__(self, pubsub: _FakePubSub) -> None:
        self._pubsub = pubsub
        self.closed = False

    def pubsub(self, ignore_subscribe_messages: bool = False) -> _FakePubSub:  # noqa: ARG002
        return self._pubsub

    async def aclose(self) -> None:
        self.closed = True


def _patch_redis_module(monkeypatch: pytest.MonkeyPatch, pubsub: _FakePubSub) -> None:
    import sys
    import types

    fake_client = _FakeRedisClient(pubsub)

    class _Redis:
        @classmethod
        def from_url(cls, _url: str, decode_responses: bool = False) -> _FakeRedisClient:  # noqa: ARG003
            return fake_client

    fake_module = types.ModuleType("redis.asyncio")
    fake_module.Redis = _Redis  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redis.asyncio", fake_module)


async def _drain(
    stream: RedisEventStream,
) -> list[tuple[str, dict[str, Any]]]:
    """Iterate ``stream.messages()`` until cancelled by the test sentinel."""
    out: list[tuple[str, dict[str, Any]]] = []
    try:
        async for item in stream.messages():
            out.append(item)
    except asyncio.CancelledError:
        pass
    return out


async def test_redis_stream_subscribes_to_patterns_and_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pubsub = _FakePubSub()
    _patch_redis_module(monkeypatch, pubsub)

    stream = RedisEventStream(
        redis_url="redis://x",
        patterns=("johnny.session.*",),
        channels=("johnny.global.calendar",),
    )

    # Push two messages then close.
    pubsub.push_message(
        channel="johnny.session.sess-1",
        data=json.dumps({"type": "agent_spoke", "text": "hi"}).encode(),
    )
    pubsub.push_message(
        channel="johnny.global.calendar",
        data=json.dumps({"type": "calendar_event_changed", "kind": "created"}).encode(),
        kind="message",
    )
    pubsub.push_stop()

    items = await asyncio.wait_for(_drain(stream), timeout=1.0)
    await stream.close()

    assert pubsub.psubscribed == ["johnny.session.*"]
    assert pubsub.subscribed == ["johnny.global.calendar"]
    assert pubsub.punsubscribed == ["johnny.session.*"]
    assert pubsub.unsubscribed == ["johnny.global.calendar"]
    assert pubsub.closed is True
    assert items[0] == ("johnny.session.sess-1", {"type": "agent_spoke", "text": "hi"})
    assert items[1] == (
        "johnny.global.calendar",
        {"type": "calendar_event_changed", "kind": "created"},
    )


async def test_redis_stream_drops_malformed_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pubsub = _FakePubSub()
    _patch_redis_module(monkeypatch, pubsub)

    stream = RedisEventStream(
        redis_url="redis://x", patterns=("johnny.session.*",)
    )

    pubsub.push_message(channel="johnny.session.sess-1", data=b"not json")
    pubsub.push_message(channel="johnny.session.sess-1", data=b'["array not object"]')
    pubsub.push_message(
        channel="johnny.session.sess-1",
        data=json.dumps({"type": "agent_spoke"}).encode(),
    )
    pubsub.push_stop()

    received = await asyncio.wait_for(_drain(stream), timeout=1.0)
    await stream.close()
    assert len(received) == 1
    assert received[0][1]["type"] == "agent_spoke"


async def test_redis_stream_ignores_subscribe_confirmations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pubsub = _FakePubSub()
    _patch_redis_module(monkeypatch, pubsub)

    stream = RedisEventStream(
        redis_url="redis://x", patterns=("johnny.session.*",)
    )
    # Subscribe-confirmation kinds get filtered.
    pubsub.push_message(channel="psubscribe", data=b"", kind="psubscribe")
    pubsub.push_message(
        channel="johnny.session.sess-1",
        data=json.dumps({"type": "agent_spoke"}).encode(),
    )
    pubsub.push_stop()

    received = await asyncio.wait_for(_drain(stream), timeout=1.0)
    await stream.close()
    assert len(received) == 1


def test_redis_stream_requires_at_least_one_target() -> None:
    with pytest.raises(ValueError):
        RedisEventStream(redis_url="redis://x")
