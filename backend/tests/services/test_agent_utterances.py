"""Tests for the SQLAlchemy-backed utterance sink."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import AgentUtterance, BotMode
from app.services.agent_utterances import (
    SqlAlchemyUtteranceSink,
    _coerce_mode,
)


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    # Only create the agent_utterances table; FKs point at bot_sessions but
    # SQLite doesn't enforce FK constraints unless PRAGMA foreign_keys=ON,
    # so the table-only fixture works.
    Base.metadata.create_all(bind=eng, tables=[AgentUtterance.__table__])  # type: ignore[list-item]
    return eng


@pytest.fixture
def db_session(engine: sa.Engine) -> Iterator[Session]:
    sess = Session(engine)
    try:
        yield sess
    finally:
        sess.close()


def _kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "mode": "limited_auto_speak",
        "prompt": '[{"role":"system","content":"sys"},{"role":"user","content":"hi"}]',
        "output_text": "Hello there",
        "audio_duration_ms": 1200,
        "matched_allowed_reply": None,
        "session_id": "sess-1",
        "bot_session_id": None,
    }
    base.update(overrides)
    return base


async def test_sink_persists_utterance_to_agent_utterances(db_session: Session) -> None:
    sink = SqlAlchemyUtteranceSink(db_session, bot_session_id=99)
    await sink.record(**_kwargs())
    rows = db_session.scalars(sa.select(AgentUtterance)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.bot_session_id == 99
    assert row.mode == BotMode.LIMITED_AUTO_SPEAK
    assert row.prompt.startswith("[")
    assert row.output_text == "Hello there"
    assert row.audio_duration_ms == 1200
    assert row.matched_allowed_reply is None
    assert row.created_at is not None


async def test_sink_persists_matched_allowed_reply(db_session: Session) -> None:
    sink = SqlAlchemyUtteranceSink(db_session, bot_session_id=99)
    await sink.record(**_kwargs(matched_allowed_reply="yes"))
    row = db_session.scalars(sa.select(AgentUtterance)).one()
    assert row.matched_allowed_reply == "yes"


async def test_sink_per_call_bot_session_id_override(db_session: Session) -> None:
    sink = SqlAlchemyUtteranceSink(db_session, bot_session_id=1)
    await sink.record(**_kwargs(bot_session_id=42))
    row = db_session.scalars(sa.select(AgentUtterance)).one()
    assert row.bot_session_id == 42


async def test_sink_falls_back_to_constructor_session_id_when_call_omits(
    db_session: Session,
) -> None:
    sink = SqlAlchemyUtteranceSink(db_session, bot_session_id=7)
    await sink.record(**_kwargs(bot_session_id=None))
    row = db_session.scalars(sa.select(AgentUtterance)).one()
    assert row.bot_session_id == 7


async def test_sink_coerces_known_modes(db_session: Session) -> None:
    sink = SqlAlchemyUtteranceSink(db_session, bot_session_id=1)
    for mode in [
        "listen_only",
        "suggest_only",
        "approval_required",
        "limited_auto_speak",
    ]:
        await sink.record(**_kwargs(mode=mode))
    rows = db_session.scalars(
        sa.select(AgentUtterance).order_by(AgentUtterance.id)
    ).all()
    assert [r.mode for r in rows] == [
        BotMode.LISTEN_ONLY,
        BotMode.SUGGEST_ONLY,
        BotMode.APPROVAL_REQUIRED,
        BotMode.LIMITED_AUTO_SPEAK,
    ]


async def test_sink_records_multiple_utterances_in_order(db_session: Session) -> None:
    sink = SqlAlchemyUtteranceSink(db_session, bot_session_id=3)
    for i in range(3):
        await sink.record(
            **_kwargs(output_text=f"u-{i}", audio_duration_ms=100 * i)
        )
    rows = db_session.scalars(
        sa.select(AgentUtterance).order_by(AgentUtterance.id)
    ).all()
    assert [r.output_text for r in rows] == ["u-0", "u-1", "u-2"]
    assert [r.audio_duration_ms for r in rows] == [0, 100, 200]


def test_sink_exposes_bot_session_id(db_session: Session) -> None:
    sink = SqlAlchemyUtteranceSink(db_session, bot_session_id=12345)
    assert sink.bot_session_id == 12345


async def test_sink_implements_utterance_sink_abc() -> None:
    """SqlAlchemyUtteranceSink must be a subclass of UtteranceSink."""
    from johnny.voice_pipeline.utterance_sink import UtteranceSink

    assert issubclass(SqlAlchemyUtteranceSink, UtteranceSink)


def test_coerce_mode_known_values() -> None:
    assert _coerce_mode("listen_only") == BotMode.LISTEN_ONLY
    assert _coerce_mode("suggest_only") == BotMode.SUGGEST_ONLY
    assert _coerce_mode("approval_required") == BotMode.APPROVAL_REQUIRED
    assert _coerce_mode("limited_auto_speak") == BotMode.LIMITED_AUTO_SPEAK


def test_coerce_mode_unknown_falls_back_to_limited_auto_speak() -> None:
    assert _coerce_mode("not_a_mode") == BotMode.LIMITED_AUTO_SPEAK
    assert _coerce_mode("") == BotMode.LIMITED_AUTO_SPEAK
