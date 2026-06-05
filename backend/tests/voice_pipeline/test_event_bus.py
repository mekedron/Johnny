"""Tests for johnny.voice_pipeline.event_bus."""

from __future__ import annotations

import json
from typing import Any

import pytest

from johnny.voice_pipeline.event_bus import (
    DEFAULT_CHANNEL_PREFIX,
    InMemoryEventBus,
    RedisEventBus,
)
from johnny.voice_pipeline.events import (
    AgentSpoke,
    RouterDecisionMade,
    TranscriptFinalized,
)


async def test_in_memory_bus_collects_events_in_order() -> None:
    bus = InMemoryEventBus()
    e1 = TranscriptFinalized(text="hi", timestamp_ms=10)
    e2 = RouterDecisionMade(should_speak=False, confidence=0.1, reason="nope", timestamp_ms=20)
    e3 = AgentSpoke(text="x", audio_duration_ms=0, timestamp_ms=30)
    await bus.publish(e1)
    await bus.publish(e2)
    await bus.publish(e3)
    events = await bus.events()
    assert events == [e1, e2, e3]


async def test_in_memory_bus_snapshot_and_clear() -> None:
    bus = InMemoryEventBus()
    await bus.publish(TranscriptFinalized(text="hi", timestamp_ms=1))
    snap = bus.snapshot()
    assert len(snap) == 1
    bus.clear()
    assert bus.snapshot() == []


async def test_in_memory_bus_close_is_noop() -> None:
    bus = InMemoryEventBus()
    await bus.close()  # default no-op
    await bus.publish(TranscriptFinalized(text="hi", timestamp_ms=0))
    assert len(bus.snapshot()) == 1


# --- Redis event bus (with a fake Redis client) ---------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self.closed = False

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1

    async def aclose(self) -> None:
        self.closed = True


def _bus_with_fake(prefix: str | None = None) -> tuple[RedisEventBus, _FakeRedis]:
    fake = _FakeRedis()
    bus = RedisEventBus(
        fake,  # type: ignore[arg-type]
        channel_prefix=prefix or DEFAULT_CHANNEL_PREFIX,
    )
    return bus, fake


async def test_redis_bus_publishes_with_session_channel() -> None:
    bus, fake = _bus_with_fake()
    event = TranscriptFinalized(text="hi", timestamp_ms=10, session_id="sess-1")
    await bus.publish(event)
    assert len(fake.published) == 1
    channel, payload = fake.published[0]
    assert channel == "johnny.session.sess-1"
    parsed: dict[str, Any] = json.loads(payload)
    assert parsed["text"] == "hi"
    assert parsed["type"] == "transcript_finalized"
    assert parsed["session_id"] == "sess-1"


async def test_redis_bus_uses_unknown_channel_without_session_id() -> None:
    bus, fake = _bus_with_fake()
    event = AgentSpoke(text="x", audio_duration_ms=0, timestamp_ms=0)
    await bus.publish(event)
    channel, _ = fake.published[0]
    assert channel == "johnny.session.unknown"


async def test_redis_bus_respects_custom_prefix() -> None:
    bus, fake = _bus_with_fake(prefix="myapp.events")
    event = RouterDecisionMade(
        should_speak=True,
        confidence=0.9,
        reason="ok",
        timestamp_ms=1,
        session_id="abc",
    )
    await bus.publish(event)
    channel, _ = fake.published[0]
    assert channel == "myapp.events.abc"


async def test_redis_bus_payload_is_compact_json() -> None:
    bus, fake = _bus_with_fake()
    event = TranscriptFinalized(text="hi", timestamp_ms=0, session_id="s")
    await bus.publish(event)
    _, payload = fake.published[0]
    # compact JSON: no spaces around separators
    assert ", " not in payload
    assert ": " not in payload


async def test_redis_bus_publish_propagates_failures() -> None:
    class _ExplodingRedis(_FakeRedis):
        async def publish(self, channel: str, message: str) -> int:  # noqa: ARG002
            raise RuntimeError("boom")

    bus = RedisEventBus(_ExplodingRedis())  # type: ignore[arg-type]
    event = TranscriptFinalized(text="hi", timestamp_ms=0)
    with pytest.raises(RuntimeError, match="boom"):
        await bus.publish(event)


async def test_redis_bus_close_calls_aclose() -> None:
    bus, fake = _bus_with_fake()
    await bus.close()
    assert fake.closed is True


async def test_redis_bus_close_swallows_aclose_errors() -> None:
    class _BadCloseRedis(_FakeRedis):
        async def aclose(self) -> None:
            raise RuntimeError("bye")

    bus = RedisEventBus(_BadCloseRedis())  # type: ignore[arg-type]
    # Should not raise — close is best-effort.
    await bus.close()
