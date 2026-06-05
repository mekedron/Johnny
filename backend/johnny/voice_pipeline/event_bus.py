"""Event publication for the voice pipeline.

The pipeline emits :class:`PipelineEvent` instances through an
:class:`EventBus`. Two implementations ship out-of-the-box:

* :class:`InMemoryEventBus` — append events to an in-process list, used by
  unit tests and by the listen-only mode when no UI is subscribed.
* :class:`RedisEventBus` — publish JSON-encoded events to a Redis channel
  so the FastAPI WebSocket endpoint can fan them out to subscribed
  browsers (US-031). Pub/sub semantics mean late subscribers don't see
  past events — durable persistence is handled separately by the API
  layer when it consumes the channel.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from johnny.voice_pipeline.events import PipelineEvent, event_to_dict

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

DEFAULT_CHANNEL_PREFIX = "johnny.session"


class EventBus(ABC):
    """Publishes :class:`PipelineEvent` instances to a transport."""

    @abstractmethod
    async def publish(self, event: PipelineEvent) -> None:
        """Send ``event`` to subscribers (or buffer it for them)."""

    async def close(self) -> None:  # noqa: B027 — intentional default no-op
        """Release any held connections. Default is a no-op."""


class InMemoryEventBus(EventBus):
    """Buffer events on an async-safe list. Intended for tests and dry runs."""

    def __init__(self) -> None:
        self._events: list[PipelineEvent] = []
        self._lock = asyncio.Lock()

    async def publish(self, event: PipelineEvent) -> None:
        async with self._lock:
            self._events.append(event)

    async def events(self) -> list[PipelineEvent]:
        """Return a snapshot of every published event so far."""
        async with self._lock:
            return list(self._events)

    def snapshot(self) -> list[PipelineEvent]:
        """Non-async snapshot for synchronous test assertions."""
        return list(self._events)

    def clear(self) -> None:
        self._events.clear()


class RedisEventBus(EventBus):
    """Publish events as JSON on a Redis pub/sub channel.

    The channel name is ``"{prefix}.{session_id}"`` when a session_id is
    present on the event, falling back to ``"{prefix}.unknown"`` so events
    from ad-hoc runs don't get dropped silently. Subscribers (US-031)
    subscribe with a pattern like ``"johnny.session.*"`` to fan everything
    out, or a specific session channel for a focused view.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        channel_prefix: str = DEFAULT_CHANNEL_PREFIX,
        default_session_id: str = "unknown",
    ) -> None:
        self._redis = redis
        self._channel_prefix = channel_prefix
        self._default_session_id = default_session_id

    def channel_for(self, event: PipelineEvent) -> str:
        session_id = event.session_id or self._default_session_id
        return f"{self._channel_prefix}.{session_id}"

    async def publish(self, event: PipelineEvent) -> None:
        channel = self.channel_for(event)
        payload = json.dumps(event_to_dict(event), separators=(",", ":"))
        try:
            await self._redis.publish(channel, payload)
        except Exception:
            logger.exception("redis publish failed for channel %s", channel)
            raise

    async def close(self) -> None:
        try:
            await self._redis.aclose()
        except Exception:
            logger.exception("redis close failed")


__all__ = [
    "DEFAULT_CHANNEL_PREFIX",
    "EventBus",
    "InMemoryEventBus",
    "RedisEventBus",
]
