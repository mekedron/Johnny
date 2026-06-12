"""Unit tests for the shared speech floor (Johnny-trt.46).

Covers the acceptance unit matrix: lock acquire / heartbeat / TTL /
release-on-interrupt semantics, the peer suppression window, the strict
loop-rule attribution, and the text-match backstop. The pure observer core
(:class:`PeerFloorState`) is driven on an injected clock; the async facade
runs over :class:`InMemoryFloorHub` with millisecond-scale leases so the
real-time legs stay fast.
"""

from __future__ import annotations

import asyncio
from typing import Any

from johnny.agent.speech_floor import (
    InMemoryFloorBackend,
    InMemoryFloorHub,
    PeerFloorState,
    SpeechFloor,
    normalize_speech_text,
)
from johnny.voice_pipeline.events import (
    FloorAcquired,
    FloorExpired,
    FloorReleased,
    PeerSpeechSuppressed,
)

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


class _EventRecorder:
    """Collects every conversation-dynamics event a floor emits."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def __call__(self, event: Any) -> None:
        self.events.append(event)

    def of_type(self, cls: type) -> list[Any]:
        return [e for e in self.events if isinstance(e, cls)]


def _floor(
    hub: InMemoryFloorHub,
    *,
    session_id: str,
    agent: str,
    recorder: _EventRecorder | None = None,
    **overrides: Any,
) -> SpeechFloor:
    defaults: dict[str, Any] = {
        "ttl_ms": 150,
        "heartbeat_interval_s": 0.03,
        "acquire_timeout_s": 1.0,
        "acquire_poll_s": 0.01,
        "suppression_tail_s": 0.08,
        "sweep_interval_s": 0.02,
    }
    defaults.update(overrides)
    return SpeechFloor(
        backend=InMemoryFloorBackend(hub),
        session_id=session_id,
        agent_name=agent,
        publish_event=recorder,
        **defaults,
    )


# --------------------------------------------------------------------------- #
# normalize_speech_text                                                       #
# --------------------------------------------------------------------------- #


def test_normalize_lowercases_strips_punctuation_collapses_whitespace() -> None:
    assert (
        normalize_speech_text("  The QUARTERLY numbers,   are ready! ")
        == "the quarterly numbers are ready"
    )


def test_normalize_empty_and_punctuation_only() -> None:
    assert normalize_speech_text("") == ""
    assert normalize_speech_text("?!...") == ""


# --------------------------------------------------------------------------- #
# PeerFloorState — the pure observer core                                     #
# --------------------------------------------------------------------------- #


def test_window_opens_on_acquired_and_lapses_at_deadline() -> None:
    state = PeerFloorState(tail_s=2.0)
    state.note_acquired("7", "Echo B", ttl_ms=10_000, now=100.0)
    assert state.active_peer(105.0) == "Echo B"
    assert state.window_peer(105.0) == "Echo B"
    # Past the lease deadline the peer no longer HOLDS the floor…
    assert state.active_peer(110.5) is None
    # …but the suppression window still covers the deadline+tail gap.
    assert state.window_peer(111.5) == "Echo B"
    assert state.window_peer(113.0) is None


def test_heartbeat_extends_the_deadline() -> None:
    state = PeerFloorState(tail_s=1.0)
    state.note_acquired("7", "Echo B", ttl_ms=1_000, now=100.0)
    state.note_heartbeat("7", ttl_ms=1_000, now=100.8)
    assert state.active_peer(101.5) == "Echo B"  # would have lapsed at 101.0
    state.note_heartbeat("7", ttl_ms=1_000, now=101.6)
    assert state.active_peer(102.4) == "Echo B"


def test_release_closes_hold_but_tail_keeps_suppressing() -> None:
    state = PeerFloorState(tail_s=2.0)
    state.note_acquired("7", "Echo B", ttl_ms=10_000, now=100.0)
    state.note_released("7", now=103.0)
    assert state.active_peer(103.1) is None
    assert state.window_peer(104.9) == "Echo B"  # released_at + tail
    assert state.window_peer(105.1) is None


def test_attribute_inside_window_counts_and_names_the_peer() -> None:
    state = PeerFloorState(tail_s=2.0)
    state.note_acquired("7", "Echo B", ttl_ms=10_000, now=100.0)
    hit = state.attribute("so the deploy finished an hour ago", now=101.0)
    assert hit is not None
    assert hit.agent == "Echo B"
    assert hit.via == "window"
    assert hit.text_matched is False
    swept = state.sweep(200.0)
    assert len(swept) == 1
    assert swept[0].suppressed == 1
    assert swept[0].text_match_hits == 0
    assert swept[0].expired is True  # never released — lease lapsed


def test_attribute_inside_window_with_matching_text_counts_hit() -> None:
    state = PeerFloorState(tail_s=2.0)
    state.note_acquired("7", "Echo B", ttl_ms=10_000, now=100.0)
    state.note_spoke("Echo B", "The deploy finished an hour ago.", now=102.0)
    state.note_released("7", now=102.5)
    hit = state.attribute("the deploy finished an hour ago", now=103.0)
    assert hit is not None and hit.via == "window" and hit.text_matched is True
    swept = state.sweep(200.0)
    assert swept[0].text_match_hits == 1
    assert swept[0].expired is False


def test_text_match_backstop_catches_late_final_outside_window() -> None:
    state = PeerFloorState(tail_s=0.5)
    state.note_acquired("7", "Echo B", ttl_ms=10_000, now=100.0)
    state.note_spoke("Echo B", "Quarterly revenue grew eleven percent.", now=101.0)
    state.note_released("7", now=101.0)
    # Way past the tail — only the published text can attribute it now.
    hit = state.attribute("quarterly revenue grew eleven percent", now=105.0)
    assert hit is not None
    assert hit.via == "text_match"
    assert hit.agent == "Echo B"
    # The backstop hit landed on the closing window's accounting.
    swept = state.sweep(200.0)
    assert swept[0].suppressed == 1
    assert swept[0].text_match_hits == 1


def test_attribute_returns_none_for_real_user_speech() -> None:
    state = PeerFloorState(tail_s=0.5)
    state.note_acquired("7", "Echo B", ttl_ms=1_000, now=100.0)
    state.note_spoke("Echo B", "The deploy finished an hour ago.", now=100.5)
    state.note_released("7", now=100.5)
    assert state.attribute("what is on the agenda today", now=200.0) is None


def test_short_fragment_never_matches_by_containment() -> None:
    state = PeerFloorState(tail_s=0.1)
    state.note_acquired("7", "Echo B", ttl_ms=100, now=100.0)
    state.note_spoke("Echo B", "ok sure I will check on that right away", now=100.0)
    state.note_released("7", now=100.0)
    # "ok" is contained in the peer text but far below the length floor.
    assert state.attribute("ok", now=101.0) is None


def test_exact_match_works_below_containment_floor() -> None:
    state = PeerFloorState(tail_s=0.1)
    state.note_acquired("7", "Echo B", ttl_ms=100, now=100.0)
    state.note_spoke("Echo B", "On it.", now=100.0)
    state.note_released("7", now=100.0)
    hit = state.attribute("on it", now=101.0)
    assert hit is not None and hit.via == "text_match"


def test_text_retention_expires_old_peer_texts() -> None:
    state = PeerFloorState(tail_s=0.1, text_retention_s=10.0)
    state.note_acquired("7", "Echo B", ttl_ms=100, now=100.0)
    state.note_spoke("Echo B", "Quarterly revenue grew eleven percent.", now=100.0)
    state.note_released("7", now=100.0)
    assert state.attribute("quarterly revenue grew eleven percent", now=120.0) is None


def test_sweep_flags_expired_window_and_prunes_state() -> None:
    state = PeerFloorState(tail_s=0.5)
    state.note_acquired("7", "Echo B", ttl_ms=1_000, now=100.0)
    # Nothing finalized while the tail is still open.
    assert state.sweep(101.2) == []
    swept = state.sweep(102.0)  # deadline 101.0 + tail 0.5 < 102.0
    assert len(swept) == 1
    assert swept[0].expired is True
    assert swept[0].agent == "Echo B"
    assert state.open_window_count == 0
    assert state.sweep(103.0) == []  # finalized exactly once


# --------------------------------------------------------------------------- #
# InMemoryFloorHub — lock semantics                                           #
# --------------------------------------------------------------------------- #


async def test_hub_lock_is_exclusive_and_compare_and_set() -> None:
    now = 100.0
    hub = InMemoryFloorHub(clock=lambda: now)
    assert await hub.try_acquire("a", 1_000) is True
    assert await hub.try_acquire("b", 1_000) is False
    assert await hub.renew("b", 1_000) is False
    assert await hub.release("b") is False
    assert await hub.renew("a", 1_000) is True
    assert await hub.release("a") is True
    assert hub.holder_payload() is None


async def test_hub_lock_expires_by_ttl() -> None:
    now = 100.0
    hub = InMemoryFloorHub(clock=lambda: now)
    assert await hub.try_acquire("a", 1_000) is True
    now = 100.9
    assert await hub.try_acquire("b", 1_000) is False
    now = 101.1  # a's lease lapsed
    assert await hub.try_acquire("b", 1_000) is True
    assert await hub.renew("a", 1_000) is False  # a lost the lock for good


# --------------------------------------------------------------------------- #
# SpeechFloor — holder side                                                   #
# --------------------------------------------------------------------------- #


async def test_acquire_free_floor_emits_acquired_and_broadcasts() -> None:
    hub = InMemoryFloorHub()
    recorder = _EventRecorder()
    floor = _floor(hub, session_id="1", agent="Johnny", recorder=recorder)
    lease = await floor.acquire("reply")
    assert lease is not None
    assert hub.holder_payload() is not None
    acquired = recorder.of_type(FloorAcquired)
    assert len(acquired) == 1
    assert acquired[0].holder == "Johnny"
    assert acquired[0].session_id == "1"
    assert acquired[0].wait_ms < 500
    assert [m["kind"] for m in hub.published] == ["acquired"]
    await floor.aclose()


async def test_release_frees_lock_broadcasts_spoke_and_emits_released() -> None:
    hub = InMemoryFloorHub()
    recorder = _EventRecorder()
    floor = _floor(hub, session_id="1", agent="Johnny", recorder=recorder)
    lease = await floor.acquire("reply")
    assert lease is not None
    await lease.release(reason="completed", spoken_text="The deploy is green.")
    assert hub.holder_payload() is None
    kinds = [m["kind"] for m in hub.published]
    assert kinds == ["acquired", "spoke", "released"]
    released = recorder.of_type(FloorReleased)
    assert len(released) == 1
    assert released[0].reason == "completed"
    assert released[0].holder == "Johnny"
    # Idempotent: a second release of the same lease is a no-op.
    await lease.release(reason="completed", spoken_text="again")
    assert [m["kind"] for m in hub.published] == kinds
    await floor.aclose()


async def test_reentrant_acquire_nests_and_outermost_release_frees() -> None:
    hub = InMemoryFloorHub()
    recorder = _EventRecorder()
    floor = _floor(hub, session_id="1", agent="Johnny", recorder=recorder)
    outer = await floor.acquire("reply")
    inner = await floor.acquire("correction")  # instant — own hold
    assert outer is not None and inner is not None
    assert len(recorder.of_type(FloorAcquired)) == 1  # outermost only
    await inner.release(reason="completed", spoken_text="inner text")
    assert hub.holder_payload() is not None  # still held by the outer lease
    assert recorder.of_type(FloorReleased) == []
    await outer.release(reason="completed", spoken_text="outer text")
    assert hub.holder_payload() is None
    assert len(recorder.of_type(FloorReleased)) == 1
    # Both texts were broadcast for the peers' backstop.
    spoke = [m["text"] for m in hub.published if m["kind"] == "spoke"]
    assert spoke == ["inner text", "outer text"]
    await floor.aclose()


async def test_contender_waits_until_release_and_measures_wait() -> None:
    hub = InMemoryFloorHub()
    recorder_b = _EventRecorder()
    floor_a = _floor(hub, session_id="1", agent="Johnny")
    floor_b = _floor(hub, session_id="2", agent="Echo B", recorder=recorder_b)
    lease_a = await floor_a.acquire("reply")
    assert lease_a is not None

    async def _release_soon() -> None:
        await asyncio.sleep(0.08)
        await lease_a.release(reason="completed", spoken_text="")

    release_task = asyncio.ensure_future(_release_soon())
    lease_b = await floor_b.acquire("reply")
    await release_task
    assert lease_b is not None
    acquired_b = recorder_b.of_type(FloorAcquired)
    assert len(acquired_b) == 1
    assert acquired_b[0].wait_ms >= 50  # actually waited for A's release
    await lease_b.release(reason="completed", spoken_text="")
    await floor_a.aclose()
    await floor_b.aclose()


async def test_acquire_times_out_while_peer_holds() -> None:
    hub = InMemoryFloorHub()
    floor_a = _floor(hub, session_id="1", agent="Johnny")
    floor_b = _floor(hub, session_id="2", agent="Echo B")
    lease_a = await floor_a.acquire("reply")
    assert lease_a is not None
    assert await floor_b.acquire("reply", timeout_s=0.06) is None
    await floor_a.aclose()
    await floor_b.aclose()


async def test_crashed_holder_frees_floor_within_ttl() -> None:
    """Floor-holder crash → TTL frees the floor; the other agent continues."""
    hub = InMemoryFloorHub()
    # ttl 80ms, heartbeat far beyond it — the holder never renews, then
    # "crashes" (no release ever runs).
    floor_a = _floor(
        hub, session_id="1", agent="Johnny", ttl_ms=80, heartbeat_interval_s=60.0
    )
    floor_b = _floor(hub, session_id="2", agent="Echo B")
    lease_a = await floor_a.acquire("reply")
    assert lease_a is not None
    lease_b = await floor_b.acquire("reply", timeout_s=1.0)
    assert lease_b is not None  # acquired after A's lease lapsed, within the wait
    await lease_b.release(reason="completed", spoken_text="")
    # NOT releasing/aclosing A's lease first — that is the crash being modeled;
    # aclose only cancels its observer tasks (release is a no-op: lock lost).
    await floor_a.aclose()
    await floor_b.aclose()


async def test_heartbeat_keeps_lease_alive_past_ttl() -> None:
    hub = InMemoryFloorHub()
    floor_a = _floor(
        hub, session_id="1", agent="Johnny", ttl_ms=100, heartbeat_interval_s=0.02
    )
    floor_b = _floor(hub, session_id="2", agent="Echo B")
    lease_a = await floor_a.acquire("reply")
    assert lease_a is not None
    await asyncio.sleep(0.25)  # well past the 100ms TTL — heartbeat must renew
    assert await floor_b.acquire("reply", timeout_s=0.03) is None
    await lease_a.release(reason="completed", spoken_text="")
    await floor_a.aclose()
    await floor_b.aclose()


async def test_aclose_releases_a_held_lease() -> None:
    hub = InMemoryFloorHub()
    recorder = _EventRecorder()
    floor = _floor(hub, session_id="1", agent="Johnny", recorder=recorder)
    lease = await floor.acquire("reply")
    assert lease is not None
    await floor.aclose()
    assert hub.holder_payload() is None
    released = recorder.of_type(FloorReleased)
    assert len(released) == 1
    assert released[0].reason == "teardown"
    # And the floor refuses new work after close.
    assert await floor.acquire("reply") is None


# --------------------------------------------------------------------------- #
# SpeechFloor — observer side (two sessions over one hub)                     #
# --------------------------------------------------------------------------- #


async def test_peer_windows_track_broadcasts_and_suppress() -> None:
    hub = InMemoryFloorHub()
    recorder_b = _EventRecorder()
    floor_a = _floor(hub, session_id="1", agent="Johnny")
    floor_b = _floor(hub, session_id="2", agent="Echo B", recorder=recorder_b)
    floor_b.start()
    try:
        lease = await floor_a.acquire("reply")
        assert lease is not None
        await asyncio.sleep(0.03)  # let B's subscriber consume the frame
        assert floor_b.peer_holds_floor() is True
        assert floor_b.peer_window_active() is True
        hit = floor_b.attribute_peer_final("we shipped the fix this morning")
        assert hit is not None and hit.agent == "Johnny"
        await lease.release(
            reason="completed", spoken_text="We shipped the fix this morning."
        )
        await asyncio.sleep(0.03)
        assert floor_b.peer_holds_floor() is False
        assert floor_b.peer_window_active() is True  # tail still open
        # The released window + suppression sweep emits on B's bus.
        await asyncio.sleep(0.15)  # tail 0.08 + sweep 0.02 cadence
        suppressed = recorder_b.of_type(PeerSpeechSuppressed)
        assert len(suppressed) == 1
        assert suppressed[0].peer == "Johnny"
        assert suppressed[0].session_id == "2"
        assert suppressed[0].window_ms > 0
    finally:
        await floor_a.aclose()
        await floor_b.aclose()


async def test_own_frames_are_ignored_by_the_observer() -> None:
    hub = InMemoryFloorHub()
    floor = _floor(hub, session_id="1", agent="Johnny")
    floor.start()
    try:
        lease = await floor.acquire("reply")
        assert lease is not None
        await asyncio.sleep(0.05)
        assert floor.peer_holds_floor() is False  # own hold is not a peer window
        await lease.release(reason="completed", spoken_text="")
    finally:
        await floor.aclose()


async def test_observer_emits_floor_expired_for_crashed_peer() -> None:
    hub = InMemoryFloorHub()
    recorder_b = _EventRecorder()
    floor_a = _floor(
        hub, session_id="1", agent="Johnny", ttl_ms=80, heartbeat_interval_s=60.0
    )
    floor_b = _floor(hub, session_id="2", agent="Echo B", recorder=recorder_b)
    floor_b.start()
    try:
        lease = await floor_a.acquire("reply")
        assert lease is not None
        # Crash: no release, no heartbeat. B's sweep flags the lapsed window.
        await asyncio.sleep(0.3)  # ttl 80ms + tail 80ms + sweep cadence
        expired = recorder_b.of_type(FloorExpired)
        assert len(expired) == 1
        assert expired[0].holder == "Johnny"
        assert recorder_b.of_type(PeerSpeechSuppressed) == []  # nothing suppressed
    finally:
        await floor_a.aclose()
        await floor_b.aclose()


async def test_text_backstop_via_broadcast_spoke_frames() -> None:
    hub = InMemoryFloorHub()
    floor_a = _floor(hub, session_id="1", agent="Johnny")
    floor_b = _floor(hub, session_id="2", agent="Echo B", suppression_tail_s=0.01)
    floor_b.start()
    try:
        lease = await floor_a.acquire("reply")
        assert lease is not None
        await lease.release(
            reason="completed", spoken_text="Quarterly revenue grew eleven percent."
        )
        await asyncio.sleep(0.1)  # window + microscopic tail fully lapsed
        assert floor_b.peer_window_active() is False
        hit = floor_b.attribute_peer_final("quarterly revenue grew eleven percent")
        assert hit is not None
        assert hit.via == "text_match"
        assert hit.agent == "Johnny"
    finally:
        await floor_a.aclose()
        await floor_b.aclose()
