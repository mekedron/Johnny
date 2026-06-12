"""Two sessions contending for one meeting's speech floor (Johnny-trt.46).

The acceptance integration leg: two :class:`SpeechFloor` instances — the
exact objects two co-agent bot sessions would hold — fight over one
meeting-scoped lock and observe each other's broadcasts. The in-memory hub
variant always runs; the Redis variants run against the dev stack's real
Redis (the same backend two real meet-worker sessions would share) and skip
loudly when it is unreachable, mirroring ``test_task_events_ws``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import pytest

from app.config import get_settings
from johnny.agent.speech_floor import (
    InMemoryFloorBackend,
    InMemoryFloorHub,
    RedisFloorBackend,
    SpeechFloor,
)
from johnny.voice_pipeline.events import (
    FloorAcquired,
    FloorExpired,
    FloorReleased,
    PeerSpeechSuppressed,
)

REDIS_URL = get_settings().redis_url


def _redis_reachable() -> bool:
    try:
        import redis

        client = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=0.5)
        try:
            client.ping()
            return True
        finally:
            client.close()
    except Exception:
        return False


class _Recorder:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def __call__(self, event: Any) -> None:
        self.events.append(event)

    def of_type(self, cls: type) -> list[Any]:
        return [e for e in self.events if isinstance(e, cls)]


# --------------------------------------------------------------------------- #
# In-memory contention (always runs)                                          #
# --------------------------------------------------------------------------- #


async def test_two_sessions_never_hold_the_floor_at_once_in_memory() -> None:
    """The never-overlap invariant on a recorded hold timeline.

    Both fake sessions run a speak loop (acquire → "play audio" → release);
    every hold interval is recorded and the assertion is literal interval
    non-overlap — the acceptance phrasing, mechanically.
    """
    hub = InMemoryFloorHub()
    timeline: list[tuple[str, float, float]] = []

    async def _session(name: str, session_id: str, speeches: int) -> None:
        floor = SpeechFloor(
            backend=InMemoryFloorBackend(hub),
            session_id=session_id,
            agent_name=name,
            ttl_ms=500,
            heartbeat_interval_s=0.1,
            acquire_timeout_s=5.0,
            acquire_poll_s=0.005,
        )
        try:
            for _ in range(speeches):
                lease = await floor.acquire("reply")
                assert lease is not None, f"{name} never got the floor"
                started = time.monotonic()
                await asyncio.sleep(0.03)  # the "audio" of one utterance
                timeline.append((name, started, time.monotonic()))
                await lease.release(reason="completed", spoken_text=f"{name} spoke")
                await asyncio.sleep(0.005)  # breathe so the peer can grab it
        finally:
            await floor.aclose()

    await asyncio.gather(
        _session("Johnny", "1", 4),
        _session("Echo B", "2", 4),
    )

    assert len(timeline) == 8
    ordered = sorted(timeline, key=lambda t: t[1])
    for (_, _, prev_end), (_, next_start, _) in zip(ordered, ordered[1:], strict=False):
        assert next_start >= prev_end, f"overlapping bot speech in {ordered}"


# --------------------------------------------------------------------------- #
# Real-Redis contention (dev stack)                                           #
# --------------------------------------------------------------------------- #

redis_required = pytest.mark.skipif(
    not _redis_reachable(),
    reason=(
        f"redis not reachable at {REDIS_URL} — run inside the compose stack "
        "(docker compose exec api pytest …) for the real-backend leg"
    ),
)


def _unique_meeting_id() -> int:
    return uuid.uuid4().int % 1_000_000_000


def _redis_floor(
    *,
    meeting_id: int,
    session_id: str,
    agent: str,
    recorder: _Recorder | None = None,
    **overrides: Any,
) -> SpeechFloor:
    defaults: dict[str, Any] = {
        "ttl_ms": 800,
        "heartbeat_interval_s": 0.2,
        "acquire_timeout_s": 5.0,
        "acquire_poll_s": 0.02,
        "suppression_tail_s": 0.2,
        "sweep_interval_s": 0.05,
    }
    defaults.update(overrides)
    return SpeechFloor(
        backend=RedisFloorBackend(redis_url=REDIS_URL, meeting_id=meeting_id),
        session_id=session_id,
        agent_name=agent,
        publish_event=recorder,
        **defaults,
    )


@redis_required
async def test_redis_floor_serializes_two_contending_sessions() -> None:
    meeting_id = _unique_meeting_id()
    rec_a, rec_b = _Recorder(), _Recorder()
    floor_a = _redis_floor(
        meeting_id=meeting_id, session_id="100", agent="Johnny", recorder=rec_a
    )
    floor_b = _redis_floor(
        meeting_id=meeting_id, session_id="200", agent="Echo B", recorder=rec_b
    )
    try:
        lease_a = await floor_a.acquire("reply")
        assert lease_a is not None

        async def _release_soon() -> None:
            await asyncio.sleep(0.2)
            await lease_a.release(reason="completed", spoken_text="done")

        release = asyncio.ensure_future(_release_soon())
        lease_b = await floor_b.acquire("reply")
        await release
        assert lease_b is not None
        waited = rec_b.of_type(FloorAcquired)[0]
        assert waited.wait_ms >= 100  # genuinely queued behind A on real Redis
        await lease_b.release(reason="completed", spoken_text="")
        assert [e.reason for e in rec_a.of_type(FloorReleased)] == ["completed"]
    finally:
        await floor_a.aclose()
        await floor_b.aclose()


@redis_required
async def test_redis_peer_window_attribution_and_suppression_event() -> None:
    """A speaks over real Redis; B labels A's text and emits the
    PeerSpeechSuppressed window record — the strict loop rule's mechanics."""
    meeting_id = _unique_meeting_id()
    rec_b = _Recorder()
    floor_a = _redis_floor(meeting_id=meeting_id, session_id="100", agent="Johnny")
    floor_b = _redis_floor(
        meeting_id=meeting_id, session_id="200", agent="Echo B", recorder=rec_b
    )
    floor_b.start()
    try:
        await asyncio.sleep(0.3)  # let B's pub/sub subscription settle
        lease = await floor_a.acquire("reply")
        assert lease is not None
        await asyncio.sleep(0.2)
        assert floor_b.peer_holds_floor() is True
        hit = floor_b.attribute_peer_final("the quarterly numbers are ready")
        assert hit is not None and hit.agent == "Johnny"
        await lease.release(
            reason="completed", spoken_text="The quarterly numbers are ready."
        )
        # Inside the post-release tail the spoke broadcast has landed, so a
        # trailing STT final attributes by window AND matches the text.
        await asyncio.sleep(0.05)
        tail_hit = floor_b.attribute_peer_final("the quarterly numbers are ready")
        assert tail_hit is not None and tail_hit.text_matched is True
        # Past release + tail: a late STT final still attributes via the
        # broadcast text backstop.
        await asyncio.sleep(0.6)
        late = floor_b.attribute_peer_final("the quarterly numbers are ready")
        assert late is not None and late.via == "text_match"
        suppressed = rec_b.of_type(PeerSpeechSuppressed)
        assert len(suppressed) == 1
        assert suppressed[0].peer == "Johnny"
        assert suppressed[0].text_match_hits >= 1
    finally:
        await floor_a.aclose()
        await floor_b.aclose()


@redis_required
async def test_redis_crashed_holder_frees_floor_within_ttl() -> None:
    """Floor-holder crash: no release, no heartbeat — the TTL frees the
    floor and the surviving observer emits FloorExpired."""
    meeting_id = _unique_meeting_id()
    rec_b = _Recorder()
    floor_a = _redis_floor(
        meeting_id=meeting_id,
        session_id="100",
        agent="Johnny",
        ttl_ms=400,
        heartbeat_interval_s=60.0,  # never renews — models the crash
    )
    floor_b = _redis_floor(
        meeting_id=meeting_id, session_id="200", agent="Echo B", recorder=rec_b
    )
    floor_b.start()
    try:
        await asyncio.sleep(0.3)
        lease_a = await floor_a.acquire("reply")
        assert lease_a is not None
        started = time.monotonic()
        lease_b = await floor_b.acquire("reply", timeout_s=3.0)
        recovered_after = time.monotonic() - started
        assert lease_b is not None
        assert recovered_after < 1.5  # within the TTL bound, not the timeout
        await lease_b.release(reason="completed", spoken_text="")
        await asyncio.sleep(0.5)  # B's sweep flags A's lapsed window
        expired = rec_b.of_type(FloorExpired)
        assert len(expired) == 1 and expired[0].holder == "Johnny"
    finally:
        await floor_a.aclose()
        await floor_b.aclose()
