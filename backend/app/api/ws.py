"""WebSocket endpoints for live session/global updates (US-031).

Two endpoints:

* ``WS /ws/sessions/{session_id}`` — streams every pipeline event for a
  given bot session: ``transcript_partial``, ``transcript_final``,
  ``router_decision``, ``approval_pending``, ``approval_resolved``,
  ``agent_spoke``, ``session_status_change``. The voice pipeline
  publishes these to the Redis channel ``johnny.session.{session_id}``
  via :class:`johnny.voice_pipeline.event_bus.RedisEventBus`; the WS
  endpoint subscribes and fans them out to the connected browser.

* ``WS /ws/global`` — streams cross-cutting notifications used by the
  calendar view and the status panel: calendar-event changes (published
  to ``johnny.global.calendar`` by the polling worker) plus per-session
  ``session_status_change`` events (filtered out of every
  ``johnny.session.*`` channel). Lets the UI react to "session
  X transitioned to joined" without subscribing to every session
  individually.

Wire envelope
-------------

Each frame sent over the WebSocket has the shape::

    {"seq": <int>, "type": <wire-type>, ...payload}

``seq`` is monotonically increasing per WebSocket connection — a reconnecting
client passes ``?since_seq=<last>`` and the server skips any frame with
``seq <= since_seq``. Combined with idempotent UI rendering keyed on
``seq``, this delivers the "no duplicate rendered events" requirement
without needing durable persistence.

``type`` mirrors the AC names so the UI branches on a stable string:
the pipeline's internal ``transcript_finalized`` becomes wire
``transcript_final``; ``router_decision_made`` becomes ``router_decision``;
``session_status_changed`` becomes ``session_status_change``. Other
types pass through unchanged.

Event stream abstraction
------------------------

The Redis pub/sub bridge lives behind an :class:`EventStream` ABC so unit
tests can inject an in-memory event source without standing up a Redis
container. :class:`RedisEventStream` is the production implementation;
:class:`InMemoryEventStream` is shared with the test suite via
:func:`set_event_stream_factory`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ws"])

SESSION_CHANNEL_PATTERN = "johnny.session.*"
GLOBAL_CHANNEL_PATTERN = "johnny.global.*"
SESSION_CHANNEL_PREFIX = "johnny.session."
GLOBAL_CHANNEL_PREFIX = "johnny.global."

# Internal-to-wire ``type`` mapping. The pipeline uses past-tense
# descriptive names (``transcript_finalized``); the WebSocket exposes
# the AC's shorter wire names. Unmapped types pass through unchanged so
# new event types added later (e.g. ``calendar_event_changed``,
# ``approval_pending``) just work without touching this dict.
WIRE_TYPE_MAP: dict[str, str] = {
    "transcript_finalized": "transcript_final",
    "transcript_interim": "transcript_partial",
    "agent_speech_interim": "agent_speech_partial",
    "router_decision_made": "router_decision",
    "session_status_changed": "session_status_change",
}

# Types that the global WS forwards from per-session channels. Other
# session-only events (transcripts, decisions, utterances) are not
# pushed to the global feed — the per-session WS is the right place.
GLOBAL_FORWARDED_SESSION_TYPES: frozenset[str] = frozenset(
    {"session_status_change"}
)


def to_wire_type(internal_type: str) -> str:
    """Translate an internal event ``type`` to its WebSocket wire name."""
    return WIRE_TYPE_MAP.get(internal_type, internal_type)


# --- EventStream abstraction ----------------------------------------------


class EventStream(ABC):
    """Async iterable of ``(channel, payload_dict)`` pairs.

    The WebSocket endpoint awaits :meth:`messages`, which yields one
    item per Redis pub/sub message. ``payload_dict`` is the already-
    decoded JSON object the publisher sent. Closing the stream stops
    the iterator and releases connections.
    """

    @abstractmethod
    def messages(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Yield messages until the stream is closed."""

    @abstractmethod
    async def close(self) -> None:
        """Release connections / unsubscribe."""


class InMemoryEventStream(EventStream):
    """Test helper — feed events via :meth:`push` and consume via WS.

    Mirrors the shape :class:`RedisEventStream` exposes so the WebSocket
    endpoint code path is identical between unit tests and production.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = (
            asyncio.Queue()
        )
        self._closed = False

    async def push(self, channel: str, payload: dict[str, Any]) -> None:
        if self._closed:
            return
        await self._queue.put((channel, payload))

    async def messages(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.put(None)


class RedisEventStream(EventStream):
    """Subscribe to one or more Redis pub/sub patterns and yield decoded events.

    ``patterns`` and ``channels`` are passed to ``psubscribe`` and
    ``subscribe`` respectively. The stream decodes the JSON payload of
    each message and yields ``(channel, decoded_dict)``. Subscribe
    confirmation messages from Redis are filtered out by
    ``ignore_subscribe_messages=True``.

    A bounded internal queue keeps backpressure simple: if the consumer
    falls behind, the producer drops old messages — this is the right
    failure mode for live UI updates where stale data is worse than
    missing data.
    """

    def __init__(
        self,
        *,
        redis_url: str,
        patterns: tuple[str, ...] = (),
        channels: tuple[str, ...] = (),
    ) -> None:
        if not patterns and not channels:
            raise ValueError("RedisEventStream requires patterns or channels")
        self._redis_url = redis_url
        self._patterns = patterns
        self._channels = channels
        self._client: Any | None = None
        self._pubsub: Any | None = None

    async def _connect(self) -> Any:
        if self._pubsub is not None:
            return self._pubsub
        from redis.asyncio import Redis

        self._client = Redis.from_url(self._redis_url, decode_responses=False)
        self._pubsub = self._client.pubsub(ignore_subscribe_messages=True)
        if self._patterns:
            await self._pubsub.psubscribe(*self._patterns)
        if self._channels:
            await self._pubsub.subscribe(*self._channels)
        return self._pubsub

    async def messages(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        pubsub = await self._connect()
        try:
            while True:
                # `get_message(timeout=1.0)` returns ``None`` on no message
                # within the timeout, which we treat as a cooperative yield
                # point. Using ``listen()`` blocks indefinitely and tends to
                # surface read-timeout errors on cancellation; ``get_message``
                # keeps cleanup quiet.
                try:
                    raw = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=1.0
                    )
                except TimeoutError:
                    continue
                if raw is None:
                    continue
                kind = raw.get("type")
                if kind not in ("message", "pmessage"):
                    continue
                channel = _decode(raw.get("channel"))
                data = _decode(raw.get("data"))
                if channel is None or data is None:
                    continue
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    logger.warning(
                        "ws: dropping malformed event on channel %s: %r",
                        channel,
                        data[:200] if isinstance(data, str) else data,
                    )
                    continue
                if not isinstance(payload, dict):
                    logger.warning(
                        "ws: dropping non-object payload on %s", channel
                    )
                    continue
                yield channel, payload
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ws: redis event stream crashed")
            raise

    async def close(self) -> None:
        if self._pubsub is not None:
            try:
                if self._patterns:
                    await self._pubsub.punsubscribe(*self._patterns)
                if self._channels:
                    await self._pubsub.unsubscribe(*self._channels)
            except Exception:
                logger.exception("ws: error unsubscribing")
            try:
                aclose = getattr(self._pubsub, "aclose", None)
                if aclose is not None:
                    await aclose()
                else:
                    await self._pubsub.close()
            except Exception:
                logger.exception("ws: error closing pubsub")
            self._pubsub = None
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                logger.exception("ws: error closing redis client")
            self._client = None


def _decode(value: Any) -> str | None:
    """Return ``value`` as a string when it's bytes or already a string."""
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str):
        return value
    return None


# --- Stream factory indirection -------------------------------------------


_EventStreamFactory = Callable[
    [tuple[str, ...], tuple[str, ...]], EventStream
]


def _default_stream_factory(
    patterns: tuple[str, ...], channels: tuple[str, ...]
) -> EventStream:
    settings = get_settings()
    return RedisEventStream(
        redis_url=settings.redis_url,
        patterns=patterns,
        channels=channels,
    )


_stream_factory: _EventStreamFactory = _default_stream_factory


def set_event_stream_factory(factory: _EventStreamFactory | None) -> None:
    """Replace the stream factory (tests inject an in-memory stream).

    Pass ``None`` to restore the default Redis-backed factory.
    """
    global _stream_factory
    _stream_factory = factory or _default_stream_factory


def get_event_stream_factory() -> _EventStreamFactory:
    return _stream_factory


# --- WebSocket envelope ---------------------------------------------------


def _build_envelope(seq: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a publisher payload in the WS frame shape.

    The wire ``type`` is the AC-stable name (``transcript_final``,
    ``router_decision``, ...). The original event payload is flattened
    in alongside ``seq`` so consumers can branch on ``type`` and pull
    fields directly without unpacking a nested object.
    """
    raw_type = str(payload.get("type", "event"))
    out: dict[str, Any] = {"seq": seq, "type": to_wire_type(raw_type)}
    for key, value in payload.items():
        if key == "type":
            continue
        out[key] = value
    return out


# --- Endpoint shared loop --------------------------------------------------


async def _run_ws(
    websocket: WebSocket,
    stream: EventStream,
    *,
    since_seq: int,
    accept: bool,
    should_forward: Callable[[str, dict[str, Any]], bool],
    after_accept: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Drive a single WebSocket connection until it disconnects.

    Shared by both endpoints. Filters events via ``should_forward``,
    skips frames already seen (``seq <= since_seq``), and watches for
    client disconnects via a separate reader task so the publisher loop
    never blocks on a quiet socket.
    """
    if accept:
        await websocket.accept()
    if after_accept is not None:
        await after_accept()

    seq = since_seq
    disconnect_event = asyncio.Event()

    async def watch_disconnect() -> None:
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            disconnect_event.set()
        except Exception:
            disconnect_event.set()

    watcher = asyncio.create_task(watch_disconnect())
    forwarder: asyncio.Task[None] | None = None
    try:
        async def pump() -> None:
            nonlocal seq
            async for channel, payload in stream.messages():
                if disconnect_event.is_set():
                    return
                if not should_forward(channel, payload):
                    continue
                seq += 1
                envelope = _build_envelope(seq, payload)
                try:
                    await websocket.send_text(json.dumps(envelope))
                except (WebSocketDisconnect, RuntimeError):
                    disconnect_event.set()
                    return

        forwarder = asyncio.create_task(pump())
        await asyncio.wait(
            (forwarder, watcher),
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        if forwarder is not None and not forwarder.done():
            forwarder.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await forwarder
        if not watcher.done():
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher
        await stream.close()
        if websocket.client_state is not WebSocketState.DISCONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close()


# --- Per-session WS --------------------------------------------------------


def _session_channel(session_id: str) -> str:
    return f"{SESSION_CHANNEL_PREFIX}{session_id}"


def _session_filter(
    session_id: str,
) -> Callable[[str, dict[str, Any]], bool]:
    target = _session_channel(session_id)

    def _allow(channel: str, _payload: dict[str, Any]) -> bool:
        return channel == target

    return _allow


@router.websocket("/ws/sessions/{session_id}")
async def session_events(
    websocket: WebSocket,
    session_id: str,
    since_seq: int = Query(default=0, ge=0),
) -> None:
    """Stream every event for one bot session."""
    factory = get_event_stream_factory()
    stream = factory((), (_session_channel(session_id),))
    await _run_ws(
        websocket,
        stream,
        since_seq=since_seq,
        accept=True,
        should_forward=_session_filter(session_id),
    )


# --- Global WS -------------------------------------------------------------


def _global_filter(channel: str, payload: dict[str, Any]) -> bool:
    if channel.startswith(GLOBAL_CHANNEL_PREFIX):
        return True
    if channel.startswith(SESSION_CHANNEL_PREFIX):
        wire = to_wire_type(str(payload.get("type", "")))
        return wire in GLOBAL_FORWARDED_SESSION_TYPES
    return False


@router.websocket("/ws/global")
async def global_events(
    websocket: WebSocket,
    since_seq: int = Query(default=0, ge=0),
) -> None:
    """Stream calendar changes + session-lifecycle notifications."""
    factory = get_event_stream_factory()
    stream = factory(
        (GLOBAL_CHANNEL_PATTERN, SESSION_CHANNEL_PATTERN),
        (),
    )
    await _run_ws(
        websocket,
        stream,
        since_seq=since_seq,
        accept=True,
        should_forward=_global_filter,
    )


__all__ = [
    "EventStream",
    "GLOBAL_CHANNEL_PATTERN",
    "GLOBAL_CHANNEL_PREFIX",
    "GLOBAL_FORWARDED_SESSION_TYPES",
    "InMemoryEventStream",
    "RedisEventStream",
    "SESSION_CHANNEL_PATTERN",
    "SESSION_CHANNEL_PREFIX",
    "WIRE_TYPE_MAP",
    "get_event_stream_factory",
    "router",
    "set_event_stream_factory",
    "to_wire_type",
]
