"""Unit tests for the per-model-call sink (Johnny-gal).

Drives :class:`app.services.model_calls.SqlAlchemyModelCallSink` against an
in-memory SQLite ``agent_model_calls`` table and asserts one row per LLM call
with prompt / response / tool-calls / token-usage / timing intact — the
answer-loop itemisation the reasoning timeline renders.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import AgentModelCall
from app.services.model_calls import PROMPT_CAP_CHARS, SqlAlchemyModelCallSink
from johnny.agent.model_call_trace import ModelCallTrace


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    Base.metadata.create_all(bind=eng, tables=[AgentModelCall.__table__])  # type: ignore[list-item]
    return eng


def _trace(**overrides: object) -> ModelCallTrace:
    base: dict[str, object] = {
        "role": "answer",
        "turn_id": 4,
        "step_index": 0,
        "model_provider": "openai-compatible",
        "model_name": "gpt-5.5",
        "prompt": [{"role": "user", "content": "weather in Helsinki?"}],
        "response_text": None,
        "tool_calls": [{"id": "call_1", "name": "list_dir", "arguments": {"path": "/skills"}}],
        "finish_reason": "tool_calls",
        "prompt_tokens": 1748,
        "completion_tokens": 112,
        "total_tokens": 1860,
        "time_to_first_token_ms": None,
        "duration_ms": 6252,
        "started_at": datetime(2026, 6, 14, 9, 24, 10, tzinfo=timezone.utc),
        "finished_at": datetime(2026, 6, 14, 9, 24, 16, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return ModelCallTrace(**base)  # type: ignore[arg-type]


def _rows(engine: sa.Engine) -> list[AgentModelCall]:
    with Session(engine) as sess:
        return list(sess.scalars(sa.select(AgentModelCall).order_by(AgentModelCall.id)))


async def test_records_one_row_with_full_trace(engine: sa.Engine) -> None:
    sink = SqlAlchemyModelCallSink(
        bot_session_id=42, session_factory=lambda: Session(engine)
    )
    await sink.record(_trace())
    rows = _rows(engine)
    assert len(rows) == 1
    row = rows[0]
    assert row.bot_session_id == 42
    assert row.turn_id == 4
    assert row.role == "answer"
    assert row.step_index == 0
    assert row.model_name == "gpt-5.5"
    assert row.finish_reason == "tool_calls"
    # Token usage round-trips (the always-0 bug fix).
    assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (1748, 112, 1860)
    assert row.duration_ms == 6252
    assert row.tool_calls_json[0]["name"] == "list_dir"
    assert row.prompt_json[0]["content"] == "weather in Helsinki?"


async def test_step_index_orders_a_tool_loop(engine: sa.Engine) -> None:
    """The answer loop records step 0 (the tool call) then step 1 (the final
    text) — the itemisation that makes the timeline show every prompt run."""
    sink = SqlAlchemyModelCallSink(
        bot_session_id=42, session_factory=lambda: Session(engine)
    )
    await sink.record(_trace(step_index=0, finish_reason="tool_calls"))
    await sink.record(
        _trace(
            step_index=1,
            finish_reason="stop",
            tool_calls=[],
            response_text="Right now in Helsinki: +12°C.",
        )
    )
    rows = _rows(engine)
    assert [r.step_index for r in rows] == [0, 1]
    assert rows[1].response_text is not None and "Helsinki" in rows[1].response_text
    assert rows[1].tool_calls_json is None  # empty list stored as NULL


async def test_emits_a_live_observed_signal(engine: sa.Engine) -> None:
    """When wired with a publish callback, the sink streams a compact
    ModelCallObserved after the write (Johnny-iy6) so the session view updates
    live — without it the row is still written (signal is best-effort)."""
    seen: list[object] = []

    async def _publish(ev: object) -> None:
        seen.append(ev)

    sink = SqlAlchemyModelCallSink(
        bot_session_id=44,
        publish_observed=_publish,
        session_factory=lambda: Session(engine),
    )
    await sink.record(_trace(step_index=0, finish_reason="tool_calls"))
    assert len(seen) == 1
    ev = seen[0]
    assert ev.type == "model_call_observed"  # type: ignore[attr-defined]
    assert ev.turn_id == 4 and ev.step_index == 0  # type: ignore[attr-defined]
    assert ev.total_tokens == 1860 and ev.tool_call_count == 1  # type: ignore[attr-defined]


async def test_caps_an_oversized_prompt(engine: sa.Engine) -> None:
    sink = SqlAlchemyModelCallSink(
        bot_session_id=42, session_factory=lambda: Session(engine)
    )
    huge = "x" * (PROMPT_CAP_CHARS + 5000)
    await sink.record(_trace(prompt=[{"role": "system", "content": huge}]))
    row = _rows(engine)[0]
    stored = row.prompt_json[0]["content"]
    assert len(stored) <= PROMPT_CAP_CHARS + len("\n…[truncated]")
    assert stored.endswith("…[truncated]")
