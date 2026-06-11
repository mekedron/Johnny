"""Unit tests for the Phase-5 speech-queue pure core (Johnny-trt.27).

:mod:`johnny.agent.speech_queue` is stdlib-only and fully synchronous (no
livekit, no asyncio, every timestamp injected), so these tests run without the
``agent`` extra and pin behavior as a deterministic function of the call
sequence: priority ordering + FIFO within a class, lazy expiry with exactly-once
``on_dropped``, the silence-grace state machine with speech-onset reset,
requeue-once interruption semantics, and the single-settle (exactly-once
callback) chokepoint the ack's ledger terminal will ride on (trt.28).
"""

from __future__ import annotations

import pytest

from johnny.agent.speech_queue import (
    ACK_DEFAULT_TTL_S,
    DEFAULT_SILENCE_GRACE_S,
    DEFAULT_TTLS,
    DROP_QUEUE_CLOSED,
    NOTICE_DEFAULT_TTL_S,
    RESULT_DEFAULT_TTL_S,
    STATUS_DEFAULT_TTL_S,
    ItemState,
    SpeechItem,
    SpeechPriority,
    SpeechQueue,
    expiry_drop_reason,
    interruption_drop_reason,
)

# A timestamp far enough past construction that the default grace (1.2 s) has
# held, yet inside even the shortest TTL (ACK 5 s) for items enqueued around
# t=1 — pops at READY deliver unless a test says otherwise.
READY = 2.0


class Recorder:
    """Collects callback firings so tests can assert exactly-once."""

    def __init__(self) -> None:
        self.spoken: list[SpeechItem] = []
        self.dropped: list[tuple[SpeechItem, str]] = []

    def on_spoken(self, item: SpeechItem) -> None:
        self.spoken.append(item)

    def on_dropped(self, item: SpeechItem, reason: str) -> None:
        self.dropped.append((item, reason))


def make_queue(now: float = 0.0, **kwargs: object) -> SpeechQueue:
    return SpeechQueue(now, **kwargs)  # type: ignore[arg-type]


# --- ordering: priorities + FIFO ----------------------------------------------


def test_priority_order_across_classes() -> None:
    q = make_queue()
    q.enqueue("notice", SpeechPriority.NOTICE, now=1.0)
    q.enqueue("result", SpeechPriority.RESULT_UNSOLICITED, now=1.1)
    q.enqueue("ack", SpeechPriority.ACK, now=1.2)
    q.enqueue("status", SpeechPriority.STATUS_REQUESTED, now=1.3)

    spoken_order: list[str] = []
    while (item := q.pop_ready(READY)) is not None:
        spoken_order.append(item.text)
        assert q.mark_spoken(item, READY)
    assert spoken_order == ["ack", "status", "result", "notice"]


def test_fifo_within_class() -> None:
    q = make_queue()
    for n in range(3):
        q.enqueue(f"result-{n}", SpeechPriority.RESULT_UNSOLICITED, now=1.0 + n * 0.1)

    order: list[str] = []
    while (item := q.pop_ready(READY)) is not None:
        order.append(item.text)
        q.mark_spoken(item, READY)
    assert order == ["result-0", "result-1", "result-2"]


def test_later_ack_preempts_earlier_result() -> None:
    q = make_queue()
    q.enqueue("result", SpeechPriority.RESULT_UNSOLICITED, now=1.0)
    q.enqueue("ack", SpeechPriority.ACK, now=2.0)

    first = q.pop_ready(READY)
    assert first is not None and first.text == "ack"


def test_items_snapshot_in_delivery_order_excludes_in_flight() -> None:
    q = make_queue()
    q.enqueue("notice", SpeechPriority.NOTICE, now=1.0)
    ack = q.enqueue("ack", SpeechPriority.ACK, now=1.1)
    q.enqueue("result", SpeechPriority.RESULT_UNSOLICITED, now=1.2)

    assert [i.text for i in q.items()] == ["ack", "result", "notice"]
    assert len(q) == 3

    popped = q.pop_ready(READY)
    assert popped is ack
    assert [i.text for i in q.items()] == ["result", "notice"]
    assert len(q) == 2


# --- silence-grace gating state machine ----------------------------------------


def test_not_ready_before_grace_elapses() -> None:
    q = make_queue(now=0.0)
    q.enqueue("ack", SpeechPriority.ACK, now=0.0)
    assert q.pop_ready(DEFAULT_SILENCE_GRACE_S - 0.01) is None
    assert q.pop_ready(DEFAULT_SILENCE_GRACE_S) is not None


def test_grace_measured_from_silence_onset_not_enqueue() -> None:
    # Silence has held since construction; a late-arriving item delivers at once.
    q = make_queue(now=0.0)
    q.enqueue("result", SpeechPriority.RESULT_UNSOLICITED, now=50.0)
    assert q.pop_ready(50.0) is not None


def test_speech_onset_blocks_and_resets_grace() -> None:
    q = make_queue(now=0.0)
    q.enqueue("result", SpeechPriority.RESULT_UNSOLICITED, now=0.0, ttl_s=1000.0)

    q.note_speech_onset()
    assert q.speaking
    assert q.silence_since is None
    assert q.pop_ready(100.0) is None  # speaking: blocked no matter how late

    q.note_silence_onset(100.0)
    assert not q.speaking
    assert q.silence_since == 100.0
    assert q.pop_ready(100.0 + DEFAULT_SILENCE_GRACE_S - 0.01) is None
    assert q.pop_ready(100.0 + DEFAULT_SILENCE_GRACE_S + 0.05) is not None


def test_duplicate_silence_onset_keeps_original_anchor() -> None:
    q = make_queue(now=0.0)
    q.note_speech_onset()
    q.note_silence_onset(10.0)
    q.note_silence_onset(11.0)  # re-delivered event must not extend the wait
    assert q.silence_since == 10.0
    q.enqueue("ack", SpeechPriority.ACK, now=10.0)
    # Epsilon dodges float noise at the exact boundary (11.2 - 10.0 < 1.2);
    # 10 + grace + 0.05 is still < 11 + grace, so a moved anchor would fail.
    assert q.pop_ready(10.0 + DEFAULT_SILENCE_GRACE_S + 0.05) is not None


def test_custom_grace() -> None:
    q = make_queue(now=0.0, grace_s=0.5)
    q.enqueue("ack", SpeechPriority.ACK, now=0.0)
    assert q.pop_ready(0.49) is None
    assert q.pop_ready(0.5) is not None


def test_silence_held_helper() -> None:
    q = make_queue(now=0.0)
    assert not q.silence_held(1.0)
    assert q.silence_held(1.2)
    q.note_speech_onset()
    assert not q.silence_held(99.0)


# --- one mouth: single in-flight -----------------------------------------------


def test_single_in_flight_serializes_delivery() -> None:
    q = make_queue()
    q.enqueue("a", SpeechPriority.ACK, now=1.0)
    q.enqueue("b", SpeechPriority.ACK, now=1.1)

    first = q.pop_ready(READY)
    assert first is not None
    assert q.in_flight is first
    assert q.pop_ready(READY) is None  # blocked until the first settles

    q.mark_spoken(first, READY)
    assert q.in_flight is None
    second = q.pop_ready(READY)
    assert second is not None and second.text == "b"


# --- expiry ---------------------------------------------------------------------


def test_ack_expires_at_default_ttl() -> None:
    rec = Recorder()
    q = make_queue(now=0.0)
    item = q.enqueue("ack", SpeechPriority.ACK, now=0.0, on_dropped=rec.on_dropped)

    assert q.pop_ready(ACK_DEFAULT_TTL_S) is None
    assert item.state is ItemState.DROPPED
    assert rec.dropped == [(item, "undelivered for 5s")]


def test_result_expiry_reason_matches_documented_copy() -> None:
    rec = Recorder()
    q = make_queue(now=0.0)
    item = q.enqueue(
        "result", SpeechPriority.RESULT_UNSOLICITED, now=0.0, on_dropped=rec.on_dropped
    )

    dropped = q.sweep_expired(RESULT_DEFAULT_TTL_S)
    assert dropped == [item]
    assert item.drop_reason == "undelivered for 120s"
    assert rec.dropped == [(item, "undelivered for 120s")]


def test_expiry_fires_on_dropped_exactly_once_across_sweeps() -> None:
    rec = Recorder()
    q = make_queue(now=0.0)
    q.enqueue("ack", SpeechPriority.ACK, now=0.0, on_dropped=rec.on_dropped)

    assert len(q.sweep_expired(6.0)) == 1
    assert q.sweep_expired(7.0) == []
    assert q.pop_ready(8.0) is None
    assert len(rec.dropped) == 1


def test_item_not_expired_just_before_deadline() -> None:
    q = make_queue(now=0.0)
    q.enqueue("ack", SpeechPriority.ACK, now=0.0)
    assert q.sweep_expired(ACK_DEFAULT_TTL_S - 0.01) == []
    assert q.pop_ready(ACK_DEFAULT_TTL_S - 0.01) is not None


def test_per_item_ttl_override() -> None:
    rec = Recorder()
    q = make_queue(now=0.0)
    q.enqueue("ack", SpeechPriority.ACK, now=0.0, ttl_s=60.0, on_dropped=rec.on_dropped)
    assert q.sweep_expired(59.0) == []
    assert len(q.sweep_expired(60.0)) == 1
    assert rec.dropped[0][1] == "undelivered for 60s"


def test_in_flight_item_never_expires() -> None:
    rec = Recorder()
    q = make_queue(now=0.0)
    item = q.enqueue(
        "ack", SpeechPriority.ACK, now=0.0, on_spoken=rec.on_spoken, on_dropped=rec.on_dropped
    )
    assert q.pop_ready(2.0) is item

    assert q.sweep_expired(100.0) == []  # committed to the mouth: not swept
    assert q.mark_spoken(item, 100.0)
    assert rec.spoken == [item]
    assert rec.dropped == []


def test_default_ttl_table_pins_plan_values() -> None:
    assert DEFAULT_TTLS[SpeechPriority.ACK] == ACK_DEFAULT_TTL_S == 5.0
    assert DEFAULT_TTLS[SpeechPriority.RESULT_UNSOLICITED] == RESULT_DEFAULT_TTL_S == 120.0
    assert DEFAULT_TTLS[SpeechPriority.STATUS_REQUESTED] == STATUS_DEFAULT_TTL_S == 20.0
    assert DEFAULT_TTLS[SpeechPriority.NOTICE] == NOTICE_DEFAULT_TTL_S == 60.0


def test_ttl_defaults_constructor_override() -> None:
    q = make_queue(now=0.0, ttl_defaults={SpeechPriority.ACK: 2.0})
    q.enqueue("ack", SpeechPriority.ACK, now=0.0)
    assert len(q.sweep_expired(2.0)) == 1


# --- requeue-once interruption semantics ----------------------------------------


def test_interrupted_item_requeues_then_second_interrupt_drops() -> None:
    rec = Recorder()
    q = make_queue(now=0.0)
    item = q.enqueue(
        "result",
        SpeechPriority.RESULT_UNSOLICITED,
        now=0.0,
        on_spoken=rec.on_spoken,
        on_dropped=rec.on_dropped,
    )

    assert q.pop_ready(READY) is item
    assert q.mark_interrupted(item, READY)
    # Local read: asserting item.state directly would let mypy narrow it to
    # Literal[QUEUED] and flag the DROPPED check below as non-overlapping.
    state_after_first_interrupt = item.state
    assert state_after_first_interrupt is ItemState.QUEUED
    assert item.interruptions == 1

    assert q.pop_ready(READY + 5) is item
    assert q.mark_interrupted(item, READY + 5)
    assert item.state is ItemState.DROPPED
    assert item.drop_reason == "interrupted twice"
    assert rec.dropped == [(item, "interrupted twice")]
    assert rec.spoken == []
    assert q.in_flight is None


def test_requeued_item_keeps_seq_position_ahead_of_later_arrivals() -> None:
    q = make_queue()
    first = q.enqueue("first", SpeechPriority.RESULT_UNSOLICITED, now=1.0)
    assert q.pop_ready(READY) is first
    later = q.enqueue("later", SpeechPriority.RESULT_UNSOLICITED, now=READY)

    assert q.mark_interrupted(first, READY)
    assert [i.text for i in q.items()] == ["first", "later"]
    assert q.pop_ready(READY) is first
    q.mark_spoken(first, READY)
    assert q.pop_ready(READY) is later


def test_interrupt_past_deadline_drops_with_expiry_reason() -> None:
    rec = Recorder()
    q = make_queue(now=0.0)
    item = q.enqueue("ack", SpeechPriority.ACK, now=0.0, on_dropped=rec.on_dropped)
    assert q.pop_ready(2.0) is item

    # Interrupted after its original deadline passed mid-delivery: settles now
    # (the ledger terminal must not wait for the next sweep), reason = expiry.
    assert q.mark_interrupted(item, 6.0)
    assert item.state is ItemState.DROPPED
    assert rec.dropped == [(item, expiry_drop_reason(ACK_DEFAULT_TTL_S))]


def test_max_requeues_zero_drops_on_first_interruption() -> None:
    rec = Recorder()
    q = make_queue(now=0.0, max_requeues=0)
    item = q.enqueue(
        "result", SpeechPriority.RESULT_UNSOLICITED, now=0.0, on_dropped=rec.on_dropped
    )
    assert q.pop_ready(READY) is item
    assert q.mark_interrupted(item, READY)
    assert item.state is ItemState.DROPPED
    assert rec.dropped == [(item, "interrupted once")]


def test_mark_interrupted_rejects_non_in_flight_items() -> None:
    q = make_queue()
    queued = q.enqueue("queued", SpeechPriority.ACK, now=1.0)
    assert not q.mark_interrupted(queued, READY)
    assert queued.state is ItemState.QUEUED

    assert q.pop_ready(READY) is queued
    q.mark_spoken(queued, READY)
    assert not q.mark_interrupted(queued, READY)  # already terminal


def test_interruption_drop_reason_wording() -> None:
    assert interruption_drop_reason(1) == "interrupted once"
    assert interruption_drop_reason(2) == "interrupted twice"
    assert interruption_drop_reason(3) == "interrupted 3 times"


# --- callbacks: exactly-once, exception-safe ------------------------------------


def test_mark_spoken_fires_on_spoken_exactly_once() -> None:
    rec = Recorder()
    q = make_queue()
    item = q.enqueue("ack", SpeechPriority.ACK, now=1.0, on_spoken=rec.on_spoken)

    assert q.pop_ready(READY) is item
    assert q.mark_spoken(item, READY)
    assert not q.mark_spoken(item, READY)  # second settle refused
    assert rec.spoken == [item]


def test_settled_item_never_fires_the_other_callback() -> None:
    rec = Recorder()
    q = make_queue()
    item = q.enqueue(
        "ack", SpeechPriority.ACK, now=1.0, on_spoken=rec.on_spoken, on_dropped=rec.on_dropped
    )
    assert q.drop(item, "operator cancel")
    assert not q.mark_spoken(item, READY)
    assert rec.spoken == []
    assert rec.dropped == [(item, "operator cancel")]


def test_raising_on_spoken_is_swallowed_and_item_still_settles() -> None:
    def boom(_item: SpeechItem) -> None:
        raise RuntimeError("hook broke")

    q = make_queue()
    item = q.enqueue("ack", SpeechPriority.ACK, now=1.0, on_spoken=boom)
    assert q.pop_ready(READY) is item
    assert q.mark_spoken(item, READY)  # no raise
    assert item.state is ItemState.SPOKEN
    # Queue still functional afterwards.
    nxt = q.enqueue("next", SpeechPriority.ACK, now=READY)
    assert q.pop_ready(READY + 2) is nxt


def test_raising_on_dropped_is_swallowed_during_sweep() -> None:
    def boom(_item: SpeechItem, _reason: str) -> None:
        raise RuntimeError("hook broke")

    q = make_queue(now=0.0)
    item = q.enqueue("ack", SpeechPriority.ACK, now=0.0, on_dropped=boom)
    assert q.sweep_expired(10.0) == [item]
    assert item.state is ItemState.DROPPED


# --- out-of-band consumption seam (trt.28 race / trt.29) -------------------------


def test_mark_spoken_consumes_still_queued_item() -> None:
    rec = Recorder()
    q = make_queue()
    item = q.enqueue(
        "result",
        SpeechPriority.RESULT_UNSOLICITED,
        now=1.0,
        on_spoken=rec.on_spoken,
        task_id=42,
        kind="calendar.upcoming_events",
    )

    # A direct answer consumed the result content: settle as delivered without
    # ever popping — it must never also play aloud.
    assert q.mark_spoken(item, 2.0)
    assert item.state is ItemState.SPOKEN
    assert rec.spoken == [item]
    assert len(q) == 0
    assert q.pop_ready(READY) is None


def test_drop_with_fire_callback_false_settles_silently() -> None:
    rec = Recorder()
    q = make_queue()
    item = q.enqueue(
        "result", SpeechPriority.RESULT_UNSOLICITED, now=1.0, on_dropped=rec.on_dropped
    )

    assert q.drop(item, "taken over", fire_callback=False)
    assert item.state is ItemState.DROPPED
    assert item.drop_reason == "taken over"
    assert rec.dropped == []
    assert not q.mark_spoken(item, READY)  # terminal stays exactly-once


def test_drop_unknown_or_settled_item_returns_false() -> None:
    q = make_queue()
    item = q.enqueue("ack", SpeechPriority.ACK, now=1.0)
    assert q.drop(item)
    assert not q.drop(item)


# --- close / teardown -------------------------------------------------------------


def test_close_drops_everything_once_in_delivery_order() -> None:
    rec = Recorder()
    q = make_queue()
    notice = q.enqueue("notice", SpeechPriority.NOTICE, now=1.0, on_dropped=rec.on_dropped)
    first_ack = q.enqueue("ack-1", SpeechPriority.ACK, now=1.1, on_dropped=rec.on_dropped)
    second_ack = q.enqueue("ack-2", SpeechPriority.ACK, now=1.2, on_dropped=rec.on_dropped)
    assert q.pop_ready(READY) is first_ack  # now in flight

    dropped = q.close()

    assert q.closed
    # Queued items in delivery order, then the in-flight one.
    assert dropped == [second_ack, notice, first_ack]
    assert [r for (_, r) in rec.dropped] == [DROP_QUEUE_CLOSED] * 3
    assert {i.state for i in (notice, first_ack, second_ack)} == {ItemState.DROPPED}
    assert len(q) == 0 and q.in_flight is None


def test_close_is_idempotent() -> None:
    rec = Recorder()
    q = make_queue()
    q.enqueue("ack", SpeechPriority.ACK, now=1.0, on_dropped=rec.on_dropped)
    assert len(q.close()) == 1
    assert q.close() == []
    assert len(rec.dropped) == 1


def test_enqueue_after_close_drops_immediately() -> None:
    rec = Recorder()
    q = make_queue()
    q.close()
    item = q.enqueue("ack", SpeechPriority.ACK, now=1.0, on_dropped=rec.on_dropped)

    assert item.state is ItemState.DROPPED
    assert rec.dropped == [(item, DROP_QUEUE_CLOSED)]
    assert q.pop_ready(READY) is None


def test_pop_ready_none_after_close() -> None:
    q = make_queue()
    q.enqueue("ack", SpeechPriority.ACK, now=1.0)
    q.close()
    assert q.pop_ready(READY) is None


# --- input validation -------------------------------------------------------------


def test_enqueue_rejects_blank_text() -> None:
    q = make_queue()
    with pytest.raises(ValueError, match="non-empty"):
        q.enqueue("   ", SpeechPriority.ACK, now=1.0)


def test_enqueue_rejects_nonpositive_ttl() -> None:
    q = make_queue()
    with pytest.raises(ValueError, match="ttl_s"):
        q.enqueue("ack", SpeechPriority.ACK, now=1.0, ttl_s=0.0)


def test_constructor_rejects_negative_knobs() -> None:
    with pytest.raises(ValueError, match="grace_s"):
        SpeechQueue(0.0, grace_s=-1.0)
    with pytest.raises(ValueError, match="max_requeues"):
        SpeechQueue(0.0, max_requeues=-1)


# --- correlation fields the wiring builds events/terminals from -------------------


def test_item_carries_correlation_fields() -> None:
    q = make_queue()
    item = q.enqueue(
        "result",
        SpeechPriority.RESULT_UNSOLICITED,
        now=1.0,
        task_id=7,
        kind="calendar.upcoming_events",
        turn_id="item_abc123",
    )
    assert (item.task_id, item.kind, item.turn_id) == (7, "calendar.upcoming_events", "item_abc123")
    assert item.enqueued_at == 1.0
    assert item.expires_at == 1.0 + RESULT_DEFAULT_TTL_S
