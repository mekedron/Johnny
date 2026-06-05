"""Tests for the in-pipeline :class:`DecisionSink` abstractions.

These cover only the SQLAlchemy-free side of the contract — the
production :class:`SqlAlchemyDecisionSink` is tested in
``tests/services/test_router_decisions.py``.
"""

from __future__ import annotations

import pytest

from johnny.voice_pipeline import (
    DecisionRecord,
    DecisionSink,
    InMemoryDecisionSink,
    NoopDecisionSink,
    RouterDecisionMade,
)


def _make_event(**overrides: object) -> RouterDecisionMade:
    base: dict[str, object] = {
        "should_speak": True,
        "confidence": 0.9,
        "reason": "asked",
        "timestamp_ms": 1234,
        "reply_type": "ack",
        "suggested_reply": "yes",
        "session_id": "sess-1",
        "input_window": {"transcript_window": [{"text": "hi"}]},
        "raw_output": {"text": "{}"},
    }
    base.update(overrides)
    return RouterDecisionMade(**base)  # type: ignore[arg-type]


def test_decision_sink_is_abstract() -> None:
    with pytest.raises(TypeError):
        DecisionSink()  # type: ignore[abstract]


async def test_in_memory_sink_appends_record() -> None:
    sink = InMemoryDecisionSink()
    event = _make_event()
    await sink.record(event, outcome="spoken", bot_session_id=42)
    snap = sink.snapshot()
    assert len(snap) == 1
    rec = snap[0]
    assert isinstance(rec, DecisionRecord)
    assert rec.decision is event
    assert rec.outcome == "spoken"
    assert rec.bot_session_id == 42


async def test_in_memory_sink_defaults_pending_outcome() -> None:
    sink = InMemoryDecisionSink()
    await sink.record(_make_event())
    assert sink.snapshot()[0].outcome == "pending"
    assert sink.snapshot()[0].bot_session_id is None


async def test_in_memory_sink_clear() -> None:
    sink = InMemoryDecisionSink()
    await sink.record(_make_event())
    await sink.record(_make_event())
    assert len(sink.snapshot()) == 2
    sink.clear()
    assert sink.snapshot() == []


async def test_in_memory_sink_concurrent_records_are_serialised() -> None:
    import asyncio

    sink = InMemoryDecisionSink()
    events = [_make_event(reason=f"r-{i}") for i in range(10)]
    await asyncio.gather(*(sink.record(e) for e in events))
    snap = sink.snapshot()
    assert len(snap) == 10
    reasons = {r.decision.reason for r in snap}
    assert reasons == {f"r-{i}" for i in range(10)}


async def test_noop_sink_drops_decisions() -> None:
    sink = NoopDecisionSink()
    await sink.record(_make_event(), outcome="spoken", bot_session_id=7)
    # No state to inspect — just confirms the call doesn't raise.


async def test_decision_sink_close_is_default_noop() -> None:
    sink = InMemoryDecisionSink()
    await sink.close()  # should not raise


def test_decision_record_is_mutable_for_outcome_update() -> None:
    """``DecisionRecord`` allows ``outcome`` updates so the in-memory sink
    can reflect ``update_outcome`` calls. The frozen wrapper used to be
    convenient but blocked the approval-required flow (US-027)."""
    rec = DecisionRecord(decision=_make_event(), outcome="pending", bot_session_id=1)
    rec.outcome = "spoken"
    assert rec.outcome == "spoken"


async def test_in_memory_sink_record_returns_id_and_update_outcome() -> None:
    """``record`` returns a synthetic id; ``update_outcome`` flips by id."""
    sink = InMemoryDecisionSink()
    first = await sink.record(_make_event(reason="a"), outcome="pending")
    second = await sink.record(_make_event(reason="b"), outcome="pending")
    assert first is not None
    assert second is not None
    assert first != second
    await sink.update_outcome(first, "spoken")
    snap = sink.snapshot()
    by_reason = {r.decision.reason: r for r in snap}
    assert by_reason["a"].outcome == "spoken"
    assert by_reason["b"].outcome == "pending"


async def test_in_memory_sink_update_outcome_unknown_id_noop() -> None:
    sink = InMemoryDecisionSink()
    await sink.record(_make_event())
    await sink.update_outcome(9999, "rejected")
    assert sink.snapshot()[0].outcome == "pending"


async def test_noop_sink_record_returns_none() -> None:
    sink = NoopDecisionSink()
    assert await sink.record(_make_event()) is None
    # ``update_outcome`` default no-op should not raise.
    await sink.update_outcome(1, "rejected")
