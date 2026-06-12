"""Shared speech floor for multi-agent meetings (Johnny-trt.46).

When a meeting runs more than one bot session (one per enabled agent
assignment, Johnny-trt.45), two invariants must hold without any central
coordinator:

* **Never overlap** — at most one agent produces audio at a time. Every
  speak path (reply, ack/status/decline, correction, queued task result)
  acquires the meeting's *speech floor* before its first audio frame and
  releases it when the speech completes or is interrupted.
* **Never loop** — a bot must not answer another bot. Each session marks
  audio heard inside a peer's floor window as *peer speech*: the transcript
  is recorded labeled with the peer agent's name and the turn never opens —
  except a deliberate by-name handoff (Johnny-trt.47: peer speech that
  names *this* agent opens a turn, bounded to one hop per human utterance;
  see :meth:`johnny.agent.session.JohnnyAgent._gate_stt_events`).
* **Answer once** (Johnny-trt.47) — when several agents' routers all decide
  to answer the same participant utterance, :meth:`SpeechFloor.claim_turn`
  arbitrates claim-once per utterance bucket: exactly one wins; the losers
  terminalize ``no_reply(peer_answered)`` *immediately* instead of queueing
  duplicate answers behind the floor.

The floor is a meeting-scoped Redis lock (``SET NX PX`` on
:data:`FLOOR_LOCK_KEY_TEMPLATE`) with a TTL + heartbeat lease: a healthy
holder renews the TTL every :data:`DEFAULT_HEARTBEAT_INTERVAL_S`; a crashed
holder stops renewing and the TTL frees the floor within
:data:`DEFAULT_FLOOR_TTL_MS` — the other agents continue normally. Floor
state is *broadcast* on a meeting-scoped pub/sub channel
(:data:`FLOOR_CHANNEL_TEMPLATE`) so peers can attribute audio to the holder
without diarization (honest scope: floor-window attribution only) and run
the text-match backstop against the holder's published spoken text.

Layering, mirroring :mod:`johnny.agent.interruptions`:

* :class:`PeerFloorState` — the pure, clock-injected observer core (peer
  windows + recent peer texts + suppression accounting). No I/O.
* :class:`FloorBackend` — the small async lock/broadcast surface;
  :class:`RedisFloorBackend` for production, :class:`InMemoryFloorHub` /
  :class:`InMemoryFloorBackend` for tests and the two-fake-sessions
  contention integration suite.
* :class:`SpeechFloor` — one session's facade over both halves: the holder
  side (:meth:`SpeechFloor.acquire` → :class:`FloorLease`) and the observer
  side (:meth:`SpeechFloor.peer_holds_floor`,
  :meth:`SpeechFloor.attribute_peer_final`). Emits the Johnny-trt.49
  conversation-dynamics events (``FloorAcquired`` / ``FloorReleased`` on the
  holder side; ``FloorExpired`` / ``PeerSpeechSuppressed`` from the observer
  sweep) through an injected defensive publisher.

Single-agent sessions (no ``meeting_config_id`` — every playground session)
never construct a :class:`SpeechFloor`: the gate and the deliverer treat the
absent floor as always-open and no floor event is ever emitted, per the
events.py vocabulary contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from johnny.voice_pipeline.events import (
    FloorAcquired,
    FloorExpired,
    FloorReleased,
    PeerSpeechSuppressed,
    PipelineEvent,
    TurnClaimLost,
    TurnClaimWon,
)

logger = logging.getLogger(__name__)

# --- Wire constants --------------------------------------------------------

FLOOR_LOCK_KEY_TEMPLATE = "johnny:floor:lock:meeting:{meeting_id}"
"""Redis key of the meeting's floor lock. Value = the holder's JSON payload."""

FLOOR_CHANNEL_TEMPLATE = "johnny:floor:meeting:{meeting_id}"
"""Pub/sub channel for floor state broadcasts (acquired/heartbeat/released/spoke)."""

CLAIM_KEY_TEMPLATE = "johnny:claim:meeting:{meeting_id}:{bucket}"
"""Redis key of one utterance bucket's turn claim (Johnny-trt.47). Value = the
winner's JSON payload (``{session_id, agent, t_ms}``)."""

DEFAULT_FLOOR_TTL_MS = 10_000
"""Lock lease length. A crashed holder frees the floor within this bound."""

DEFAULT_HEARTBEAT_INTERVAL_S = 3.0
"""Healthy-holder renew cadence (~TTL/3, so two missed beats still hold)."""

DEFAULT_ACQUIRE_TIMEOUT_S = 12.0
"""Default acquire wait. Deliberately > the TTL so a waiter outlives a crashed
holder's lease and proceeds, rather than giving up just before the floor frees."""

DEFAULT_ACQUIRE_POLL_S = 0.15
"""Acquire retry cadence while a peer holds the floor."""

DEFAULT_SUPPRESSION_TAIL_S = 2.0
"""How long past a floor release the peer window keeps suppressing — STT
finals lag the audio they transcribe, so the window must outlive the lock."""

DEFAULT_MAX_HOLD_S = 120.0
"""Heartbeat stops renewing after this long — leak insurance: a lease whose
release path never ran (a bug, a hung speech) frees the floor for the peers
one TTL later instead of holding the meeting hostage."""

DEFAULT_SWEEP_INTERVAL_S = 0.5
"""Observer-side sweep cadence (closes peer windows, emits their events)."""

DEFAULT_CLAIM_WINDOW_MS = 2_000
"""Turn-claim utterance-bucket window (Johnny-trt.47): two sessions' claims
whose end-of-speech anchors differ by no more than this are the *same*
utterance, so only one may answer. Sized to cover per-bot VAD endpoint skew
plus the semantic-EOU hold spread (a 0.40 s floor commit vs. a 1.5 s
``max_delay`` escalation on the peer ≈ 1.1 s worst case) with margin; two
*distinct* utterances closer together than this can mis-arbitrate, which is
the benign direction (one answer where two were possible). Tunable per
assembly (``JOHNNY_TURN_CLAIM_WINDOW_MS`` on the runtime assemblies)."""

DEFAULT_CLAIM_TTL_MS = 60_000
"""Turn-claim key lease. Long enough that a slow contender (cold local STT +
router latency) still *sees* the winner's claim instead of double-answering;
short enough that the keyspace self-cleans. Buckets are absolute-time keyed,
so a stale claim can never collide with a later utterance's bucket."""

PEER_TEXT_RETENTION_S = 60.0
"""How long a peer's published spoken text stays matchable as the backstop."""

PEER_TEXT_MAX = 8
"""Ring-buffer cap of retained peer texts per session."""

_TEXT_MATCH_MIN_CHARS = 12
"""Containment matches below this normalized length are ignored — short
fragments ("ok", "yes") would suppress genuine participant speech."""

# Release reasons the lease publishes / emits (free text on the event by
# contract — events.FloorReleased.reason — kept here as the canonical set).
RELEASE_COMPLETED = "completed"
RELEASE_INTERRUPTED = "interrupted"
RELEASE_TEARDOWN = "teardown"
RELEASE_SAY_FAILED = "say_failed"
RELEASE_SAY_UNAVAILABLE = "say_unavailable"
RELEASE_SUPERSEDED = "superseded"


def normalize_speech_text(text: str) -> str:
    """Normalize text for the peer text-match backstop.

    Lowercase, punctuation stripped, whitespace collapsed — generous enough
    that an STT rendering of the peer's TTS audio still matches the text the
    peer published, strict enough that the comparison stays deterministic.
    """
    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def session_relative_ms(
    session_started_at: float, clock: Callable[[], float] = time.time
) -> Callable[[], int]:
    """Timestamp factory in the conversation-dynamics convention (Johnny-trt.49).

    Session-relative offset when ``session_started_at`` is a real epoch
    reference, raw epoch ms otherwise — byte-for-byte the
    :func:`johnny.agent.observability.build_interruption_emitter` rule, so
    floor rows sort with the interruption rows they interleave with.
    """

    def _ms() -> int:
        now = clock()
        if session_started_at > 0:
            return max(0, round((now - session_started_at) * 1000))
        return round(now * 1000)

    return _ms


# --- Backend surface --------------------------------------------------------


class FloorBackend(Protocol):
    """The lock + broadcast primitives the floor rides on.

    ``payload`` is the holder's exact serialized identity; ``renew`` /
    ``release`` are compare-and-set against it so a session can never extend
    or delete a lock it lost (the Redis Lua discipline).

    The ``claim_*`` trio (Johnny-trt.47) is the per-utterance-bucket
    claim-once keyspace: ``claim_set`` is an atomic get-or-set (returns the
    existing payload when the bucket was already claimed, ``None`` when this
    call claimed it), ``claim_get`` a plain read, ``claim_release`` a
    compare-and-delete (a demoted cross-bucket claimer removes its own entry
    so later contenders see only the real winner).
    """

    async def try_acquire(self, payload: str, ttl_ms: int) -> bool: ...

    async def renew(self, payload: str, ttl_ms: int) -> bool: ...

    async def release(self, payload: str) -> bool: ...

    async def claim_get(self, bucket: int) -> str | None: ...

    async def claim_set(self, bucket: int, payload: str, ttl_ms: int) -> str | None: ...

    async def claim_release(self, bucket: int, payload: str) -> bool: ...

    async def publish(self, message: dict[str, Any]) -> None: ...

    def subscribe(self) -> AsyncIterator[dict[str, Any]]: ...

    async def aclose(self) -> None: ...


_RENEW_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
else
    return 0
end
"""

_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

# Atomic get-or-set for one claim bucket (Johnny-trt.47): the first writer
# wins and gets nil back; every later caller gets the winner's payload.
# Plain Lua instead of SET NX GET so the oldest supported Redis works.
_CLAIM_LUA = """
local existing = redis.call('get', KEYS[1])
if existing then
    return existing
end
redis.call('set', KEYS[1], ARGV[1], 'px', tonumber(ARGV[2]))
return nil
"""


class RedisFloorBackend:
    """Production :class:`FloorBackend` on the meeting's Redis key + channel.

    ``meeting_id`` is the floor's scope token: a real ``meeting_config_id``
    for Meet sessions, or a synthetic string scope (e.g.
    ``browser-group-{id}``, Johnny-trt.48) for surfaces that share a floor
    without a meeting row — the string namespace can never collide with the
    integer meeting keyspace.

    One client per backend (the session owns exactly one). The subscribe
    loop follows the :class:`~johnny.agent.task_wiring.TaskEventListener`
    discipline: a dropped connection logs, backs off, and resubscribes —
    pub/sub has no replay, but the lock itself (not the broadcast) is the
    overlap guarantee, so a missed frame degrades only the peer-window
    *labeling*, never the never-overlap invariant.
    """

    def __init__(
        self,
        *,
        redis_url: str,
        meeting_id: int | str,
        client_factory: Callable[[], Any] | None = None,
        reconnect_backoff_s: float = 2.0,
    ) -> None:
        self._redis_url = redis_url
        self._lock_key = FLOOR_LOCK_KEY_TEMPLATE.format(meeting_id=meeting_id)
        self._channel = FLOOR_CHANNEL_TEMPLATE.format(meeting_id=meeting_id)
        self._meeting_id = meeting_id
        self._client_factory = client_factory
        self._reconnect_backoff_s = reconnect_backoff_s
        self._client: Any | None = None

    def _claim_key(self, bucket: int) -> str:
        return CLAIM_KEY_TEMPLATE.format(meeting_id=self._meeting_id, bucket=bucket)

    def _build_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        from redis.asyncio import Redis

        return Redis.from_url(self._redis_url, decode_responses=False)

    def _command_client(self) -> Any:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    async def try_acquire(self, payload: str, ttl_ms: int) -> bool:
        result = await self._command_client().set(
            self._lock_key, payload, nx=True, px=ttl_ms
        )
        return bool(result)

    async def renew(self, payload: str, ttl_ms: int) -> bool:
        result = await self._command_client().eval(
            _RENEW_LUA, 1, self._lock_key, payload, str(ttl_ms)
        )
        return bool(result)

    async def release(self, payload: str) -> bool:
        result = await self._command_client().eval(
            _RELEASE_LUA, 1, self._lock_key, payload
        )
        return bool(result)

    async def claim_get(self, bucket: int) -> str | None:
        raw = await self._command_client().get(self._claim_key(bucket))
        if raw is None:
            return None
        return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)

    async def claim_set(self, bucket: int, payload: str, ttl_ms: int) -> str | None:
        raw = await self._command_client().eval(
            _CLAIM_LUA, 1, self._claim_key(bucket), payload, str(ttl_ms)
        )
        if raw is None:
            return None
        return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)

    async def claim_release(self, bucket: int, payload: str) -> bool:
        result = await self._command_client().eval(
            _RELEASE_LUA, 1, self._claim_key(bucket), payload
        )
        return bool(result)

    async def publish(self, message: dict[str, Any]) -> None:
        await self._command_client().publish(
            self._channel, json.dumps(message, separators=(",", ":"))
        )

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        """Yield broadcast frames forever, reconnecting on connection loss."""
        while True:
            client: Any | None = None
            try:
                client = self._build_client()
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                await pubsub.subscribe(self._channel)
                logger.info("speech floor: subscribed to %s", self._channel)
                while True:
                    try:
                        message = await pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=1.0
                        )
                    except TimeoutError:
                        continue
                    if message is None or message.get("type") != "message":
                        continue
                    try:
                        frame = json.loads(message.get("data") or b"{}")
                    except (ValueError, TypeError):
                        logger.warning(
                            "speech floor: malformed frame on %s — skipped",
                            self._channel,
                        )
                        continue
                    if isinstance(frame, dict):
                        yield frame
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "speech floor: subscription on %s dropped — peer windows "
                    "degrade until resubscribe (retrying in %.0fs)",
                    self._channel,
                    self._reconnect_backoff_s,
                )
                await asyncio.sleep(self._reconnect_backoff_s)
            finally:
                if client is not None:
                    with contextlib.suppress(Exception):
                        await client.aclose()

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()


class InMemoryFloorHub:
    """Shared in-process lock + broadcast bus for tests (and the contention suite).

    One hub models one meeting's Redis state; each fake session wraps it in
    its own :class:`InMemoryFloorBackend`. ``clock`` is injectable so TTL
    expiry is testable without real sleeps.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock: tuple[str, float] | None = None  # (payload, expires_at)
        self._claims: dict[int, tuple[str, float]] = {}  # bucket -> (payload, expires_at)
        self._queues: list[asyncio.Queue[dict[str, Any]]] = []
        self.published: list[dict[str, Any]] = []

    def _live_payload(self) -> str | None:
        if self._lock is None:
            return None
        payload, expires_at = self._lock
        if self._clock() >= expires_at:
            self._lock = None
            return None
        return payload

    async def try_acquire(self, payload: str, ttl_ms: int) -> bool:
        if self._live_payload() is not None:
            return False
        self._lock = (payload, self._clock() + ttl_ms / 1000.0)
        return True

    async def renew(self, payload: str, ttl_ms: int) -> bool:
        if self._live_payload() != payload:
            return False
        self._lock = (payload, self._clock() + ttl_ms / 1000.0)
        return True

    async def release(self, payload: str) -> bool:
        if self._live_payload() != payload:
            return False
        self._lock = None
        return True

    def _live_claim(self, bucket: int) -> str | None:
        entry = self._claims.get(bucket)
        if entry is None:
            return None
        payload, expires_at = entry
        if self._clock() >= expires_at:
            del self._claims[bucket]
            return None
        return payload

    async def claim_get(self, bucket: int) -> str | None:
        return self._live_claim(bucket)

    async def claim_set(self, bucket: int, payload: str, ttl_ms: int) -> str | None:
        existing = self._live_claim(bucket)
        if existing is not None:
            return existing
        self._claims[bucket] = (payload, self._clock() + ttl_ms / 1000.0)
        return None

    async def claim_release(self, bucket: int, payload: str) -> bool:
        if self._live_claim(bucket) != payload:
            return False
        del self._claims[bucket]
        return True

    def claim_payload(self, bucket: int) -> str | None:
        """Test read: the bucket's live claim payload, honoring expiry."""
        return self._live_claim(bucket)

    async def publish(self, message: dict[str, Any]) -> None:
        self.published.append(message)
        for queue in list(self._queues):
            queue.put_nowait(message)

    def attach_queue(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._queues.append(queue)
        return queue

    def detach_queue(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with contextlib.suppress(ValueError):
            self._queues.remove(queue)

    def holder_payload(self) -> str | None:
        """Test read: the live holder's payload, honoring expiry."""
        return self._live_payload()


class InMemoryFloorBackend:
    """One fake session's :class:`FloorBackend` view over a shared hub.

    The broadcast queue attaches at construction (not at first
    ``subscribe()`` iteration) so frames published between a floor's
    construction and its subscriber task's first tick are buffered, not
    lost — the determinism the contention tests rely on. Real Redis has the
    same gap, covered in production by ``start()`` running at assembly,
    seconds before any speech exists to broadcast.
    """

    def __init__(self, hub: InMemoryFloorHub) -> None:
        self._hub = hub
        self._queue: asyncio.Queue[dict[str, Any]] | None = hub.attach_queue()

    async def try_acquire(self, payload: str, ttl_ms: int) -> bool:
        return await self._hub.try_acquire(payload, ttl_ms)

    async def renew(self, payload: str, ttl_ms: int) -> bool:
        return await self._hub.renew(payload, ttl_ms)

    async def release(self, payload: str) -> bool:
        return await self._hub.release(payload)

    async def claim_get(self, bucket: int) -> str | None:
        return await self._hub.claim_get(bucket)

    async def claim_set(self, bucket: int, payload: str, ttl_ms: int) -> str | None:
        return await self._hub.claim_set(bucket, payload, ttl_ms)

    async def claim_release(self, bucket: int, payload: str) -> bool:
        return await self._hub.claim_release(bucket, payload)

    async def publish(self, message: dict[str, Any]) -> None:
        await self._hub.publish(message)

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        queue = self._queue
        if queue is None:
            queue = self._hub.attach_queue()
            self._queue = queue
        try:
            while True:
                yield await queue.get()
        finally:
            self._hub.detach_queue(queue)
            self._queue = None

    async def aclose(self) -> None:
        if self._queue is not None:
            self._hub.detach_queue(self._queue)
            self._queue = None


# --- Pure observer core ------------------------------------------------------


@dataclass
class PeerWindow:
    """One peer's floor hold as observed locally (receiver-clock timestamps)."""

    session_id: str
    agent: str
    opened_at: float
    deadline: float
    """Lease expiry bound: last acquired/heartbeat + the broadcast TTL."""
    released_at: float | None = None
    expired: bool = False
    """True when the window lapsed without an explicit release (holder crash)."""
    suppressed: int = 0
    text_match_hits: int = 0

    def end(self) -> float | None:
        """When the hold itself stopped (release or lease lapse); None = live."""
        if self.released_at is not None:
            return self.released_at
        if self.expired:
            return self.deadline
        return None


@dataclass(frozen=True, slots=True)
class ClosedPeerWindow:
    """A finalized window the sweep hands back for event emission."""

    agent: str
    opened_at: float
    hold_ms: int
    window_ms: int
    suppressed: int
    text_match_hits: int
    expired: bool


@dataclass(frozen=True, slots=True)
class PeerAttribution:
    """Why one transcript candidate was attributed to a peer agent."""

    agent: str
    via: str  # "window" | "text_match"
    text_matched: bool = False


@dataclass(frozen=True, slots=True)
class TurnClaimOutcome:
    """One :meth:`SpeechFloor.claim_turn` result (Johnny-trt.47).

    ``won=False`` means a peer already claimed this utterance — the caller
    terminalizes ``no_reply(peer_answered)`` instead of speaking. ``bucket``
    is the contended bucket's identifier (the quantized anchor, stringified
    for the event vocabulary); ``winner`` names the claiming agent on a loss
    (best-effort — blank when the winner's payload was unreadable).
    """

    won: bool
    bucket: str
    winner: str = ""


@dataclass(frozen=True, slots=True)
class _ClaimEntry:
    """A parsed claim payload read back from the bucket keyspace."""

    session_id: str
    agent: str
    t_ms: int


def _parse_claim(raw: str | None) -> _ClaimEntry | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return _ClaimEntry(
            session_id=str(data.get("session_id") or ""),
            agent=str(data.get("agent") or ""),
            t_ms=int(data.get("t_ms") or 0),
        )
    except (ValueError, TypeError):
        return None


class PeerFloorState:
    """Pure peer-window + peer-text tracker (clock values injected per call).

    The :class:`SpeechFloor` subscriber feeds broadcast frames in
    (``note_*``); the STT suppression path asks :meth:`attribute`; the sweep
    task collects :meth:`sweep` results for event emission. All timestamps
    are the *receiver's* monotonic clock — no cross-process clock trust.
    """

    def __init__(
        self,
        *,
        tail_s: float = DEFAULT_SUPPRESSION_TAIL_S,
        text_retention_s: float = PEER_TEXT_RETENTION_S,
        text_max: int = PEER_TEXT_MAX,
    ) -> None:
        self._tail_s = tail_s
        self._text_retention_s = text_retention_s
        self._open: dict[str, PeerWindow] = {}
        self._closing: list[PeerWindow] = []
        self._texts: deque[tuple[str, str, float]] = deque(maxlen=text_max)

    # -- frame intake ----------------------------------------------------- #

    def note_acquired(
        self, session_id: str, agent: str, ttl_ms: int, now: float
    ) -> None:
        self._open[session_id] = PeerWindow(
            session_id=session_id,
            agent=agent,
            opened_at=now,
            deadline=now + ttl_ms / 1000.0,
        )

    def note_heartbeat(self, session_id: str, ttl_ms: int, now: float) -> None:
        window = self._open.get(session_id)
        if window is not None:
            window.deadline = now + ttl_ms / 1000.0

    def note_released(self, session_id: str, now: float) -> None:
        window = self._open.pop(session_id, None)
        if window is not None:
            window.released_at = now
            self._closing.append(window)

    def note_spoke(self, agent: str, text: str, now: float) -> None:
        normalized = normalize_speech_text(text)
        if normalized:
            self._texts.append((agent, normalized, now))

    # -- reads ------------------------------------------------------------ #

    def active_peer(self, now: float) -> str | None:
        """The peer holding the floor *right now* (lease still live)."""
        for window in self._open.values():
            if now < window.deadline:
                return window.agent
        return None

    def window_peer(self, now: float) -> str | None:
        """The peer whose suppression window covers ``now`` (hold or tail)."""
        for window in self._open.values():
            if now < window.deadline + self._tail_s:
                return window.agent
        for window in self._closing:
            end = window.end()
            if end is not None and now < end + self._tail_s:
                return window.agent
        return None

    def attribute(self, text: str, now: float) -> PeerAttribution | None:
        """Attribute one kept STT final to a peer, or ``None`` (= real user).

        Window rule first (anything inside a peer's hold + tail is that
        peer's speech — the honest no-diarization scope), then the
        text-match backstop for finals whose STT latency outran the tail.
        Updates the per-window suppression accounting the sweep reports.
        """
        normalized = normalize_speech_text(text)
        window = self._covering_window(now)
        if window is not None:
            window.suppressed += 1
            matched = self._matches_peer_text(normalized, agent=window.agent, now=now)
            if matched:
                window.text_match_hits += 1
            return PeerAttribution(agent=window.agent, via="window", text_matched=matched)
        for agent, peer_text, noted_at in reversed(self._texts):
            if now - noted_at > self._text_retention_s:
                continue
            if self._texts_match(normalized, peer_text):
                # Backstop hit outside any window: account it to the agent's
                # most recent closing window when one is still sweeping so the
                # PeerSpeechSuppressed totals include it; a hit after the
                # window's event already emitted is suppression-only (logged
                # by the caller, no extra event — documented best-effort).
                for window in reversed(self._closing):
                    if window.agent == agent:
                        window.suppressed += 1
                        window.text_match_hits += 1
                        break
                return PeerAttribution(agent=agent, via="text_match", text_matched=True)
        return None

    def sweep(self, now: float) -> list[ClosedPeerWindow]:
        """Finalize windows whose suppression tail has fully lapsed.

        An open window past its deadline lapsed without a release — the
        holder crashed or hung (``expired=True``; the caller emits
        ``FloorExpired``). It still moves through the closing list so the
        tail keeps suppressing the trailing STT of whatever audio it played.
        """
        for session_id, window in list(self._open.items()):
            if now >= window.deadline:
                window.expired = True
                self._closing.append(window)
                del self._open[session_id]

        finalized: list[ClosedPeerWindow] = []
        still_closing: list[PeerWindow] = []
        for window in self._closing:
            end = window.end()
            if end is None or now < end + self._tail_s:
                still_closing.append(window)
                continue
            finalized.append(
                ClosedPeerWindow(
                    agent=window.agent,
                    opened_at=window.opened_at,
                    hold_ms=max(0, round((end - window.opened_at) * 1000)),
                    window_ms=max(0, round((end + self._tail_s - window.opened_at) * 1000)),
                    suppressed=window.suppressed,
                    text_match_hits=window.text_match_hits,
                    expired=window.expired,
                )
            )
        self._closing = still_closing

        while self._texts and now - self._texts[0][2] > self._text_retention_s:
            self._texts.popleft()
        return finalized

    # -- internals ---------------------------------------------------------- #

    def _covering_window(self, now: float) -> PeerWindow | None:
        for window in self._open.values():
            if now < window.deadline + self._tail_s:
                return window
        for window in reversed(self._closing):
            end = window.end()
            if end is not None and now < end + self._tail_s:
                return window
        return None

    def _matches_peer_text(self, normalized: str, *, agent: str, now: float) -> bool:
        for text_agent, peer_text, noted_at in self._texts:
            if text_agent != agent or now - noted_at > self._text_retention_s:
                continue
            if self._texts_match(normalized, peer_text):
                return True
        return False

    @staticmethod
    def _texts_match(candidate: str, peer_text: str) -> bool:
        if not candidate or not peer_text:
            return False
        if candidate == peer_text:
            return True
        if len(candidate) >= _TEXT_MATCH_MIN_CHARS and candidate in peer_text:
            return True
        return len(peer_text) >= _TEXT_MATCH_MIN_CHARS and peer_text in candidate

    @property
    def open_window_count(self) -> int:
        """Test read: live windows being tracked."""
        return len(self._open)


def shield_handle_through_peer_tail(
    handle: Any,
    floor: SpeechFloor | None,
    *,
    poll_s: float = 0.1,
    max_shield_s: float = 15.0,
) -> asyncio.Task[None] | None:
    """Keep a brand-new speech uninterruptible through a floor-handoff tail.

    Johnny-trt.48: the trt.46 peer awareness suppresses peer speech at the
    STT *final* seam, but the SDK's VAD-level interruption fires earlier —
    at a floor handoff the previous holder's trailing audio still has this
    session's ``user_state`` at "speaking", and the SDK reads that as a live
    barge-in and cuts the brand-new speech within milliseconds (surfaced by
    the trt.48 ensemble scenario: the second agent's replies terminalized
    ``no_reply(barge_in)`` with ~10 ms floor holds, every handoff). The
    shield applies the exact discriminator the final seam uses — "a peer's
    floor window covers now" — to the interruption path: the handle is
    marked uninterruptible while the window is closing, and a lift task
    restores interruptibility the moment it closes (bounded by
    ``max_shield_s`` as leak insurance), so genuine user barge-in works for
    the rest of the speech. Explicit stops (the Stop button / client gate)
    force-interrupt and always win.

    Returns the lift task — the caller must keep a strong reference — or
    ``None`` when no shield was needed (no floor, window already closed, or
    the handle was already uninterruptible).
    """
    if floor is None:
        return None
    try:
        if not floor.peer_window_active():
            return None
        if not bool(getattr(handle, "allow_interruptions", True)):
            return None
        handle.allow_interruptions = False
    except Exception:
        logger.exception(
            "speech floor: handoff shield could not arm — speech left interruptible"
        )
        return None
    logger.info(
        "speech floor: handoff shield armed — peer window still closing; "
        "speech starts uninterruptible"
    )

    async def _lift() -> None:
        deadline = time.monotonic() + max_shield_s
        try:
            while time.monotonic() < deadline and floor.peer_window_active():
                await asyncio.sleep(poll_s)
        finally:
            with contextlib.suppress(Exception):
                handle.allow_interruptions = True

    return asyncio.ensure_future(_lift())


# --- The per-session facade --------------------------------------------------


class FloorLease:
    """One acquired hold on the floor; release exactly once (idempotent).

    Reentrant speech (a correction say() queued while a reply still plays)
    nests: inner leases decrement the depth, the outermost release frees the
    Redis lock, publishes ``released``, and emits ``FloorReleased``. Every
    release publishes the speech's spoken text (when given) as the peers'
    text-match backstop feed.
    """

    def __init__(self, floor: SpeechFloor, *, kind: str) -> None:
        self._floor = floor
        self._kind = kind
        self._released = False

    @property
    def kind(self) -> str:
        return self._kind

    async def release(self, *, reason: str, spoken_text: str = "") -> None:
        if self._released:
            return
        self._released = True
        await self._floor._release_one(reason=reason, spoken_text=spoken_text)


class SpeechFloor:
    """One bot session's handle on its meeting's shared speech floor.

    Holder side: :meth:`acquire` waits (bounded) for the meeting lock,
    heartbeats it while held, and returns a :class:`FloorLease` the speak
    path releases on completion/interrupt — ``None`` on timeout means *do
    not speak*. Observer side: the subscriber task tracks peers' broadcast
    floor windows for :meth:`peer_holds_floor` (the queue-delivery
    predicate) and :meth:`attribute_peer_final` (the STT suppression seam);
    the sweep task closes lapsed windows and emits ``FloorExpired`` /
    ``PeerSpeechSuppressed``.

    ``publish_event`` is the trt.49 conversation-dynamics sink (the session
    EventBus' ``publish``); every emit is defensive — a failing bus never
    reaches a speak path. ``timestamp_ms`` stamps events in the
    session-relative convention (:func:`session_relative_ms`).
    """

    def __init__(
        self,
        *,
        backend: FloorBackend,
        session_id: str,
        agent_name: str,
        publish_event: Callable[[PipelineEvent], Awaitable[None]] | None = None,
        timestamp_ms: Callable[[], int] | None = None,
        clock: Callable[[], float] = time.monotonic,
        ttl_ms: int = DEFAULT_FLOOR_TTL_MS,
        heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
        acquire_timeout_s: float = DEFAULT_ACQUIRE_TIMEOUT_S,
        acquire_poll_s: float = DEFAULT_ACQUIRE_POLL_S,
        suppression_tail_s: float = DEFAULT_SUPPRESSION_TAIL_S,
        max_hold_s: float = DEFAULT_MAX_HOLD_S,
        sweep_interval_s: float = DEFAULT_SWEEP_INTERVAL_S,
        claim_window_ms: int = DEFAULT_CLAIM_WINDOW_MS,
        claim_ttl_ms: int = DEFAULT_CLAIM_TTL_MS,
    ) -> None:
        self._backend = backend
        self._session_id = session_id
        self._agent_name = agent_name
        self._publish_event = publish_event
        self._timestamp_ms = timestamp_ms or session_relative_ms(0.0)
        self._clock = clock
        self._ttl_ms = ttl_ms
        self._heartbeat_interval_s = heartbeat_interval_s
        self._acquire_timeout_s = acquire_timeout_s
        self._acquire_poll_s = acquire_poll_s
        self._max_hold_s = max_hold_s
        self._sweep_interval_s = sweep_interval_s
        self._claim_window_ms = max(1, claim_window_ms)
        self._claim_ttl_ms = max(self._claim_window_ms, claim_ttl_ms)
        # The exact lock value this session writes — compare-and-set target
        # for renew/release, and the identity peers see in broadcasts.
        self._payload = json.dumps(
            {"session_id": session_id, "agent": agent_name}, separators=(",", ":")
        )
        self._peers = PeerFloorState(tail_s=suppression_tail_s)
        self._hold_depth = 0
        self._held_since: float | None = None
        self._lock_lost = False
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._subscriber_task: asyncio.Task[None] | None = None
        self._sweep_task: asyncio.Task[None] | None = None
        self._closed = False

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        """Spawn the observer tasks (idempotent; needs a running loop)."""
        if self._subscriber_task is None or self._subscriber_task.done():
            self._subscriber_task = asyncio.ensure_future(self._subscribe_loop())
        if self._sweep_task is None or self._sweep_task.done():
            self._sweep_task = asyncio.ensure_future(self._sweep_loop())

    async def aclose(self) -> None:
        """Release any held floor, stop the tasks, close the backend. Idempotent."""
        if self._closed:
            return
        self._closed = True
        while self._hold_depth > 0:
            await self._release_one(reason=RELEASE_TEARDOWN, spoken_text="")
        for task in (self._heartbeat_task, self._subscriber_task, self._sweep_task):
            if task is None or task.done():
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._heartbeat_task = None
        self._subscriber_task = None
        self._sweep_task = None
        # Final sweep so a window mid-tail still reports its suppressions.
        far_future = self._clock() + self._ttl_ms / 1000.0 + self._max_hold_s
        await self._emit_swept(self._peers.sweep(far_future))
        with contextlib.suppress(Exception):
            await self._backend.aclose()

    # -- holder side --------------------------------------------------------- #

    async def acquire(
        self, kind: str, *, timeout_s: float | None = None
    ) -> FloorLease | None:
        """Wait for the floor; ``None`` after ``timeout_s`` means do not speak.

        Reentrant: while this session already holds the floor, nested speech
        (a queued say() behind a playing reply) gets a nested lease
        immediately — the hold spans outermost-acquire → last-release, with
        one heartbeat. ``FloorAcquired`` (with the measured ``wait_ms``) is
        emitted only for the outermost acquire.
        """
        if self._closed:
            logger.warning(
                "speech floor: acquire(%s) after close for session=%s — refused",
                kind,
                self._session_id,
            )
            return None
        if self._hold_depth > 0:
            self._hold_depth += 1
            return FloorLease(self, kind=kind)
        budget = self._acquire_timeout_s if timeout_s is None else timeout_s
        started = self._clock()
        while True:
            acquired = False
            try:
                acquired = await self._backend.try_acquire(self._payload, self._ttl_ms)
            except Exception:
                logger.exception(
                    "speech floor: try_acquire failed for session=%s — retrying",
                    self._session_id,
                )
            if acquired:
                break
            if self._clock() - started >= budget:
                logger.warning(
                    "speech floor: %s speech for session=%s timed out waiting "
                    "for the floor (%.1fs) — suppressed",
                    kind,
                    self._session_id,
                    budget,
                )
                return None
            await asyncio.sleep(self._acquire_poll_s)
        now = self._clock()
        wait_ms = max(0, round((now - started) * 1000))
        self._hold_depth = 1
        self._held_since = now
        self._lock_lost = False
        self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())
        await self._broadcast(
            {"kind": "acquired", "ttl_ms": self._ttl_ms},
        )
        await self._emit(
            FloorAcquired(
                holder=self._agent_name,
                timestamp_ms=self._timestamp_ms(),
                wait_ms=wait_ms,
                session_id=self._session_id,
            )
        )
        return FloorLease(self, kind=kind)

    async def claim_turn(self, anchor_ms: int) -> TurnClaimOutcome:
        """Claim-once arbitration for one user utterance (Johnny-trt.47).

        ``anchor_ms`` is the utterance's end-of-speech epoch timestamp as this
        session observed it (the VAD listening edge for voice turns, the
        feed entry time for typed turns). Sessions answering the *same*
        utterance anchor within per-bot VAD endpoint skew of each other;
        quantizing the anchor into ``claim_window_ms`` buckets gives the
        shared Redis key, and the **same-utterance test is the anchor
        distance** (``|Δt| ≤ claim_window_ms``), not the bucket identity —
        the ±1-bucket peek below covers anchors that straddle a boundary.

        Exactly-one-winner discipline:

        1. *Peek* the anchor's bucket and both neighbors — a live peer claim
           within the window means the turn is already answered → lost.
        2. *Atomically* claim the anchor's bucket (get-or-set): losing the
           set race to a within-window peer → lost; an out-of-window
           occupant is a different utterance sharing the bucket (sub-window
           utterance gap) → proceed, the benign-duplicate direction.
        3. *Post-set verify* the neighbor buckets: two contenders straddling
           a boundary can both pass 1–2 (each set its own bucket before the
           other's landed), so the loser is decided deterministically by
           ``(t_ms, session_id)`` order — both sides compute the same
           winner; the loser deletes its own entry so later contenders see
           one claim.

        Every backend failure fails *open* (proceed as won, no event):
        worst case both agents answer sequentially — the documented benign
        failure mode — which beats both staying silent.

        Emits ``TurnClaimWon`` / ``TurnClaimLost`` (Johnny-trt.49
        vocabulary) through the defensive event seam. ``contenders`` is
        best-effort: the peers this call actually observed.
        """
        bucket = anchor_ms // self._claim_window_ms
        bucket_label = str(bucket)
        payload = json.dumps(
            {"session_id": self._session_id, "agent": self._agent_name, "t_ms": anchor_ms},
            separators=(",", ":"),
        )

        def _same_utterance(entry: _ClaimEntry) -> bool:
            return abs(entry.t_ms - anchor_ms) <= self._claim_window_ms

        def _outranks_us(entry: _ClaimEntry) -> bool:
            return (entry.t_ms, entry.session_id) < (anchor_ms, self._session_id)

        try:
            # 1. Peek the bucket neighborhood for an existing same-utterance claim.
            for neighbor in (bucket - 1, bucket, bucket + 1):
                entry = _parse_claim(await self._backend.claim_get(neighbor))
                if entry is None or not _same_utterance(entry):
                    continue
                if entry.session_id == self._session_id:
                    # Already ours (defensive — the gate claims once per turn).
                    return TurnClaimOutcome(won=True, bucket=bucket_label)
                return await self._claim_lost(bucket_label, entry)

            # 2. Atomic get-or-set on the anchor's own bucket.
            existing = _parse_claim(
                await self._backend.claim_set(bucket, payload, self._claim_ttl_ms)
            )
            if existing is not None and existing.session_id != self._session_id:
                if _same_utterance(existing):
                    return await self._claim_lost(bucket_label, existing)
                logger.warning(
                    "turn claim: bucket %s already held for a different utterance "
                    "(Δt=%dms > window %dms) — proceeding unarbitrated (session=%s)",
                    bucket_label,
                    abs(existing.t_ms - anchor_ms),
                    self._claim_window_ms,
                    self._session_id,
                )
                return await self._claim_won(bucket_label, contenders=())

            # 3. Post-set verify: a same-utterance peer in a neighbor bucket
            # that ordered before us wins; we demote and clean our entry up.
            observed: list[str] = []
            for neighbor in (bucket - 1, bucket + 1):
                entry = _parse_claim(await self._backend.claim_get(neighbor))
                if entry is None or entry.session_id == self._session_id:
                    continue
                if not _same_utterance(entry):
                    continue
                if _outranks_us(entry):
                    with contextlib.suppress(Exception):
                        await self._backend.claim_release(bucket, payload)
                    return await self._claim_lost(bucket_label, entry)
                observed.append(entry.agent)
            return await self._claim_won(bucket_label, contenders=tuple(observed))
        except Exception:
            logger.exception(
                "turn claim: backend failed for session=%s bucket=%s — "
                "failing open (unarbitrated turn)",
                self._session_id,
                bucket_label,
            )
            return TurnClaimOutcome(won=True, bucket=bucket_label)

    async def _claim_won(
        self, bucket: str, *, contenders: tuple[str, ...]
    ) -> TurnClaimOutcome:
        await self._emit(
            TurnClaimWon(
                bucket=bucket,
                timestamp_ms=self._timestamp_ms(),
                claimant=self._agent_name,
                contenders=contenders,
                session_id=self._session_id,
            )
        )
        return TurnClaimOutcome(won=True, bucket=bucket)

    async def _claim_lost(self, bucket: str, winner: _ClaimEntry) -> TurnClaimOutcome:
        winner_name = winner.agent or f"agent {winner.session_id}"
        logger.info(
            "turn claim: session=%s lost bucket=%s to %s",
            self._session_id,
            bucket,
            winner_name,
        )
        await self._emit(
            TurnClaimLost(
                bucket=bucket,
                timestamp_ms=self._timestamp_ms(),
                claimant=self._agent_name,
                winner=winner_name,
                contenders=(winner_name,),
                session_id=self._session_id,
            )
        )
        return TurnClaimOutcome(won=False, bucket=bucket, winner=winner_name)

    async def _release_one(self, *, reason: str, spoken_text: str) -> None:
        """One lease's release: outermost frees the lock + emits + broadcasts."""
        if self._hold_depth <= 0:
            return
        # The spoken text feeds the peers' backstop on EVERY release — a
        # nested ack's text matters as much as the outer reply's.
        if spoken_text.strip():
            await self._broadcast({"kind": "spoke", "text": spoken_text})
        self._hold_depth -= 1
        if self._hold_depth > 0:
            return
        held_since = self._held_since
        self._held_since = None
        hold_ms = (
            max(0, round((self._clock() - held_since) * 1000))
            if held_since is not None
            else 0
        )
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if not self._lock_lost:
            try:
                await self._backend.release(self._payload)
            except Exception:
                logger.exception(
                    "speech floor: lock release failed for session=%s "
                    "(TTL will free it)",
                    self._session_id,
                )
        await self._broadcast({"kind": "released", "reason": reason})
        await self._emit(
            FloorReleased(
                holder=self._agent_name,
                timestamp_ms=self._timestamp_ms(),
                hold_ms=hold_ms,
                reason=reason,
                session_id=self._session_id,
            )
        )

    async def _heartbeat_loop(self) -> None:
        """Renew the lease while held; stop at max-hold (leak insurance)."""
        while True:
            await asyncio.sleep(self._heartbeat_interval_s)
            held_since = self._held_since
            if held_since is None:
                return
            if self._clock() - held_since > self._max_hold_s:
                logger.error(
                    "speech floor: session=%s held the floor past the %.0fs "
                    "max hold — heartbeat stops; the TTL frees it for peers",
                    self._session_id,
                    self._max_hold_s,
                )
                return
            renewed = False
            try:
                renewed = await self._backend.renew(self._payload, self._ttl_ms)
            except Exception:
                logger.exception(
                    "speech floor: heartbeat renew failed for session=%s — retrying",
                    self._session_id,
                )
                continue
            if not renewed:
                # The lease lapsed under us (event-loop stall past the TTL):
                # a peer may legitimately hold the lock now. Do not cut the
                # in-flight speech for a bookkeeping miss — log loudly, mark
                # the hold lost so release won't DEL a peer's lock, and let
                # the speech end settle the rest.
                self._lock_lost = True
                logger.error(
                    "speech floor: session=%s lost its lease mid-hold "
                    "(heartbeat outran the TTL) — release will not touch the lock",
                    self._session_id,
                )
                return
            await self._broadcast({"kind": "heartbeat", "ttl_ms": self._ttl_ms})

    # -- observer side ------------------------------------------------------- #

    def peer_holds_floor(self) -> bool:
        """A peer's lease is live right now (the delivery-loop predicate)."""
        return self._peers.active_peer(self._clock()) is not None

    def peer_window_active(self) -> bool:
        """A peer's suppression window (hold or tail) covers right now."""
        return self._peers.window_peer(self._clock()) is not None

    def attribute_peer_final(self, text: str) -> PeerAttribution | None:
        """Attribute one kept STT final to a peer agent, or ``None`` (user)."""
        return self._peers.attribute(text, self._clock())

    async def _subscribe_loop(self) -> None:
        try:
            async for frame in self._backend.subscribe():
                self._apply_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover — backend loops are self-healing
            logger.exception(
                "speech floor: subscriber loop died for session=%s — peer "
                "window labeling disabled",
                self._session_id,
            )

    def _apply_frame(self, frame: dict[str, Any]) -> None:
        session_id = str(frame.get("session_id") or "")
        if not session_id or session_id == self._session_id:
            return
        kind = str(frame.get("kind") or "")
        agent = str(frame.get("agent") or "") or f"agent {session_id}"
        now = self._clock()
        if kind == "acquired":
            self._peers.note_acquired(
                session_id, agent, int(frame.get("ttl_ms") or self._ttl_ms), now
            )
        elif kind == "heartbeat":
            self._peers.note_heartbeat(
                session_id, int(frame.get("ttl_ms") or self._ttl_ms), now
            )
        elif kind == "released":
            self._peers.note_released(session_id, now)
        elif kind == "spoke":
            self._peers.note_spoke(agent, str(frame.get("text") or ""), now)

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_interval_s)
            try:
                await self._emit_swept(self._peers.sweep(self._clock()))
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover — sweep must never die
                logger.exception(
                    "speech floor: sweep failed for session=%s — continuing",
                    self._session_id,
                )

    async def _emit_swept(self, closed: list[ClosedPeerWindow]) -> None:
        for window in closed:
            if window.expired:
                await self._emit(
                    FloorExpired(
                        holder=window.agent,
                        timestamp_ms=self._timestamp_ms(),
                        hold_ms=window.hold_ms,
                        session_id=self._session_id,
                    )
                )
            if window.suppressed > 0:
                await self._emit(
                    PeerSpeechSuppressed(
                        peer=window.agent,
                        timestamp_ms=self._timestamp_ms(),
                        window_ms=window.window_ms,
                        text_match_hits=window.text_match_hits,
                        session_id=self._session_id,
                    )
                )

    # -- plumbing -------------------------------------------------------------- #

    async def _broadcast(self, message: dict[str, Any]) -> None:
        frame = {
            "session_id": self._session_id,
            "agent": self._agent_name,
            **message,
        }
        try:
            await self._backend.publish(frame)
        except Exception:
            logger.exception(
                "speech floor: broadcast %r failed for session=%s",
                message.get("kind"),
                self._session_id,
            )

    async def _emit(self, event: PipelineEvent) -> None:
        if self._publish_event is None:
            return
        try:
            await self._publish_event(event)
        except Exception:
            logger.exception(
                "speech floor: event publish failed for session=%s (%s)",
                self._session_id,
                getattr(event, "type", "?"),
            )


__all__ = [
    "CLAIM_KEY_TEMPLATE",
    "DEFAULT_ACQUIRE_TIMEOUT_S",
    "DEFAULT_CLAIM_TTL_MS",
    "DEFAULT_CLAIM_WINDOW_MS",
    "DEFAULT_FLOOR_TTL_MS",
    "DEFAULT_HEARTBEAT_INTERVAL_S",
    "DEFAULT_MAX_HOLD_S",
    "DEFAULT_SUPPRESSION_TAIL_S",
    "FLOOR_CHANNEL_TEMPLATE",
    "FLOOR_LOCK_KEY_TEMPLATE",
    "ClosedPeerWindow",
    "FloorBackend",
    "FloorLease",
    "InMemoryFloorBackend",
    "InMemoryFloorHub",
    "PeerAttribution",
    "PeerFloorState",
    "PeerWindow",
    "RELEASE_COMPLETED",
    "RELEASE_INTERRUPTED",
    "RELEASE_TEARDOWN",
    "RedisFloorBackend",
    "SpeechFloor",
    "TurnClaimOutcome",
    "normalize_speech_text",
    "session_relative_ms",
    "shield_handle_through_peer_tail",
]
