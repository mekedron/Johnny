"""Integration: a scripted barge-in lands as a ``conversation_events`` row (Johnny-trt.49).

The full production chain, with only the SDK's speech handle faked:

    RouterGate (speak turn, reply bound, user onset, interrupted settle)
      → build_interruption_emitter → InMemoryEventBus
      → event_to_dict (the Redis wire shape, JSON round-tripped)
      → apply_conversation_event → conversation_events row

The serialisation hop matters: the subscriber consumes the *decoded JSON*
payload, not the dataclass — round-tripping through ``json`` proves the wire
shape and the column mapping agree end-to-end. The browser-validated live
run (chrome-devtools) covers the same chain with the real SDK + Redis.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any, cast

import pytest
import sqlalchemy as sa
from livekit.agents.llm.chat_context import ChatContext, ChatMessage
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import BotSession, BotSessionStatus, ConversationEvent
from app.providers.base import LLMProvider, LLMResponse
from app.services.session_status_subscriber import apply_conversation_event
from johnny.agent.gate import TurnIndex, TurnLedger
from johnny.agent.observability import build_interruption_emitter
from johnny.agent.router_gate import RouterGate, RouterGateConfig
from johnny.voice_pipeline.event_bus import InMemoryEventBus
from johnny.voice_pipeline.events import event_to_dict

# asyncio_mode = "auto" — async tests need no marker.


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    Base.metadata.create_all(
        bind=eng,
        tables=[
            BotSession.__table__,  # type: ignore[list-item]
            ConversationEvent.__table__,  # type: ignore[list-item]
        ],
    )
    return eng


@pytest.fixture
def db_session(engine: sa.Engine) -> Iterator[Session]:
    sess = Session(engine)
    try:
        yield sess
    finally:
        sess.close()


class _SpeakRouter(LLMProvider):
    """A router LLM that always decides SPEAK."""

    name = "fake-router"

    async def chat(self, messages: Any, response_format: Any = None) -> LLMResponse:
        decision = {"should_speak": True, "confidence": 0.95, "reason": "addressed"}
        return LLMResponse(
            text=json.dumps(decision),
            finish_reason="stop",
            structured_output=decision,
        )


class _FakeSpeechHandle:
    """Duck-typed reply handle; ``fire_done`` runs the registered callbacks."""

    def __init__(self, *, interrupted: bool) -> None:
        self.id = "speech_1"
        self.interrupted = interrupted
        self.chat_items: list[Any] = []
        self._cbs: list[Any] = []

    def add_done_callback(self, cb: Any) -> None:
        self._cbs.append(cb)

    def fire_done(self) -> None:
        for cb in list(self._cbs):
            cb(self)


class _Clock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


async def _noop_terminal(_turn_id: str, _terminal: Any) -> None:
    return None


async def test_scripted_barge_in_persists_a_conversation_event_row(
    db_session: Session,
) -> None:
    session_row = BotSession(status=BotSessionStatus.JOINED)
    db_session.add(session_row)
    db_session.flush()

    bus = InMemoryEventBus()
    turn_index = TurnIndex()
    clock = _Clock(50_000)
    gate = RouterGate(
        _SpeakRouter(),
        config=RouterGateConfig(),
        ledger=TurnLedger(_noop_terminal),
        record_interruption=build_interruption_emitter(
            bus,
            turn_index,
            session_started_at=1_000.0,
            session_id=str(session_row.id),
            clock=lambda: 1_007.5,  # the cut lands 7.5 s into the session
        ),
        clock=clock,
    )

    # A speak turn, its reply bound — then the user talks over it and the
    # SDK settles the handle interrupted 420 ms after the speech onset.
    msg = ChatMessage(role="user", content=["Johnny, walk me through the plan"])
    await gate.run_turn(ChatContext.empty(), msg)
    handle = _FakeSpeechHandle(interrupted=True)
    gate.bind_reply(cast(Any, handle))
    gate.note_user_speech_onset()
    clock.now += 420
    handle.fire_done()
    await asyncio.gather(*gate._reply_tasks)

    # The event crossed the bus; round-trip it through JSON — the exact
    # payload shape the Redis subscriber decodes.
    (event,) = bus.snapshot()
    payload = json.loads(json.dumps(event_to_dict(event)))
    assert payload["type"] == "interruption_recorded"

    applied = apply_conversation_event(db_session, payload)
    assert applied is True

    row = db_session.scalars(sa.select(ConversationEvent)).one()
    assert row.bot_session_id == session_row.id
    assert row.event_type == "interruption_recorded"
    assert row.reason == "user_over_bot"
    assert row.duration_ms == 420  # onset → audio stop
    assert row.timestamp_ms == 7_500  # session-relative audio stop
    assert row.turn_id == turn_index.resolve(msg.id)
    assert row.details == {"speech_kind": "reply", "partial_kept": False}


async def test_scripted_stop_cut_persists_bot_cut_by_stop(
    db_session: Session,
) -> None:
    """The playground Stop path: note_stop_requested before the SDK interrupt
    persists who=bot_cut_by_stop with request→stop latency."""
    session_row = BotSession(status=BotSessionStatus.JOINED)
    db_session.add(session_row)
    db_session.flush()

    bus = InMemoryEventBus()
    clock = _Clock(80_000)
    gate = RouterGate(
        _SpeakRouter(),
        config=RouterGateConfig(),
        ledger=TurnLedger(_noop_terminal),
        record_interruption=build_interruption_emitter(
            bus, TurnIndex(), session_started_at=1_000.0,
            session_id=str(session_row.id), clock=lambda: 1_012.0,
        ),
        clock=clock,
    )

    msg = ChatMessage(role="user", content=["Johnny, talk to me"])
    await gate.run_turn(ChatContext.empty(), msg)
    handle = _FakeSpeechHandle(interrupted=True)
    gate.bind_reply(cast(Any, handle))
    gate.note_stop_requested()  # what BrowserAgentSession.interrupt() does first
    clock.now += 90
    handle.fire_done()
    await asyncio.gather(*gate._reply_tasks)

    (event,) = bus.snapshot()
    payload = json.loads(json.dumps(event_to_dict(event)))
    assert apply_conversation_event(db_session, payload) is True

    row = db_session.scalars(sa.select(ConversationEvent)).one()
    assert row.reason == "bot_cut_by_stop"
    assert row.duration_ms == 90
