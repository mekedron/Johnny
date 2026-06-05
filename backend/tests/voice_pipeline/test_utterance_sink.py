"""Tests for the in-pipeline :class:`UtteranceSink` abstractions.

These cover only the SQLAlchemy-free side of the contract — the
production :class:`SqlAlchemyUtteranceSink` is tested in
``tests/services/test_agent_utterances.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from johnny.voice_pipeline import (
    InMemoryUtteranceSink,
    NoopUtteranceSink,
    UtteranceRecord,
    UtteranceSink,
)


def _record_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "mode": "limited_auto_speak",
        "prompt": '[{"role":"system","content":"sys"},{"role":"user","content":"hi"}]',
        "output_text": "hello",
        "audio_duration_ms": 320,
        "matched_allowed_reply": None,
        "session_id": "sess-1",
        "bot_session_id": 42,
    }
    base.update(overrides)
    return base


def test_utterance_sink_is_abstract() -> None:
    with pytest.raises(TypeError):
        UtteranceSink()  # type: ignore[abstract]


async def test_in_memory_sink_appends_record() -> None:
    sink = InMemoryUtteranceSink()
    await sink.record(**_record_kwargs())
    snap = sink.snapshot()
    assert len(snap) == 1
    rec = snap[0]
    assert isinstance(rec, UtteranceRecord)
    assert rec.mode == "limited_auto_speak"
    assert rec.prompt.startswith("[")
    assert rec.output_text == "hello"
    assert rec.audio_duration_ms == 320
    assert rec.matched_allowed_reply is None
    assert rec.session_id == "sess-1"
    assert rec.bot_session_id == 42


async def test_in_memory_sink_captures_matched_allowed_reply() -> None:
    sink = InMemoryUtteranceSink()
    await sink.record(**_record_kwargs(matched_allowed_reply="yes"))
    rec = sink.snapshot()[0]
    assert rec.matched_allowed_reply == "yes"


async def test_in_memory_sink_clear() -> None:
    sink = InMemoryUtteranceSink()
    await sink.record(**_record_kwargs())
    await sink.record(**_record_kwargs(output_text="bye"))
    assert len(sink.snapshot()) == 2
    sink.clear()
    assert sink.snapshot() == []


async def test_in_memory_sink_concurrent_records_are_serialised() -> None:
    sink = InMemoryUtteranceSink()
    coros = [
        sink.record(**_record_kwargs(output_text=f"u-{i}"))
        for i in range(10)
    ]
    await asyncio.gather(*coros)
    snap = sink.snapshot()
    assert len(snap) == 10
    texts = {r.output_text for r in snap}
    assert texts == {f"u-{i}" for i in range(10)}


async def test_in_memory_sink_defaults() -> None:
    """matched_allowed_reply, session_id, bot_session_id all default to None."""
    sink = InMemoryUtteranceSink()
    await sink.record(
        mode="suggest_only",
        prompt="p",
        output_text="o",
        audio_duration_ms=100,
    )
    rec = sink.snapshot()[0]
    assert rec.matched_allowed_reply is None
    assert rec.session_id is None
    assert rec.bot_session_id is None


async def test_noop_sink_drops_utterances() -> None:
    sink = NoopUtteranceSink()
    await sink.record(**_record_kwargs())
    # No state to inspect — just confirms the call doesn't raise.


async def test_utterance_sink_close_is_default_noop() -> None:
    sink = InMemoryUtteranceSink()
    await sink.close()  # should not raise


def test_utterance_record_frozen() -> None:
    from dataclasses import FrozenInstanceError

    rec = UtteranceRecord(
        mode="limited_auto_speak",
        prompt="p",
        output_text="o",
        audio_duration_ms=100,
    )
    with pytest.raises(FrozenInstanceError):
        rec.mode = "listen_only"  # type: ignore[misc]
