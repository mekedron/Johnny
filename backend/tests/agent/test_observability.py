"""Event/observability parity tests for the LiveKit agent path (Johnny-d5z).

Three layers, matching the bead acceptance:

* **Pure builders** — each ``build_*`` seam publishes the right
  :class:`~johnny.voice_pipeline.events.PipelineEvent` shape onto an
  ``EventBus``, with the LiveKit ``str`` turn id translated to the durable
  ``int`` via the shared :class:`~johnny.agent.gate.TurnIndex` so a turn's
  decision / terminal / timing all carry one identical id.
* **Metrics translation** — ``metric_to_timing`` maps LiveKit provider metrics
  onto ``PipelineTiming`` (and drops the non-stage ones), and
  :class:`~johnny.agent.observability.MetricsTranslator` bridges the sync
  ``metrics_collected`` callback to the async bus.
* **Replay parity (the strong proof)** — emitted events are serialised
  (:func:`event_to_dict`) and fed through the *real* subscriber
  (``app.services.session_status_subscriber``) against an in-memory DB, asserting
  the decision↔utterance↔terminal rows bind by the int turn id exactly as the
  legacy pipeline's events did.

Guarded by ``importorskip`` so the suite still collects without the ``agent``
extra (the observability module itself is ``livekit``-free, but the wider agent
suite gates on it).
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("livekit.agents")

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import Base  # noqa: E402
from app.db.models import (  # noqa: E402
    AgentDecision,
    AgentUtterance,
    BotSession,
    BotSessionStatus,
    CalendarEvent,
    DecisionOutcome,
    GoogleAccount,
    MeetingConfig,
    NoReplyReason,
    ProfileTemplate,
    SessionTiming,
    TerminalState,
    TranscriptChunk,
)
from app.services.session_status_subscriber import (  # noqa: E402
    apply_agent_spoke_event,
    apply_pipeline_timing_event,
    apply_router_decision_event,
    apply_transcript_event,
    apply_turn_terminal_event,
)
from johnny.agent.gate import GateTerminal, TurnIndex  # noqa: E402
from johnny.agent.observability import (  # noqa: E402
    MetricsTranslator,
    build_decision_emitter,
    build_observability,
    build_session_terminal_emitter,
    build_spoke_emitter,
    build_suggested_emitter,
    build_transcript_finalized_emitter,
    metric_to_timing,
    terminal_outcome,
)
from johnny.voice_pipeline.event_bus import InMemoryEventBus  # noqa: E402
from johnny.voice_pipeline.events import (  # noqa: E402
    TranscriptFinalized,
    event_to_dict,
)
from johnny.voice_pipeline.pipeline import RouterDecision  # noqa: E402

# asyncio_mode = "auto" — async tests need no marker.


# --------------------------------------------------------------------------- #
# Fakes / helpers                                                             #
# --------------------------------------------------------------------------- #


class _FlakyBus(InMemoryEventBus):
    """An ``EventBus`` whose ``publish`` always raises — for the defensive paths."""

    async def publish(self, event: Any) -> None:  # noqa: ARG002
        raise RuntimeError("bus down")


def _decision(
    *,
    should_speak: bool = True,
    confidence: float = 0.9,
    reason: str = "addressed",
    reply_type: str | None = None,
    suggested_reply: str | None = None,
    raw: dict[str, Any] | None = None,
) -> RouterDecision:
    return RouterDecision(
        should_speak=should_speak,
        confidence=confidence,
        reason=reason,
        reply_type=reply_type,
        suggested_reply=suggested_reply,
        raw=raw or {},
    )


def _stt_metric(**kw: Any) -> SimpleNamespace:
    base = {
        "type": "stt_metrics",
        "label": "johnny.STT",
        "timestamp": 0.0,
        "duration": 0.12,
        "audio_duration": 1.5,
        "streamed": True,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _llm_metric(**kw: Any) -> SimpleNamespace:
    base = {
        "type": "llm_metrics",
        "label": "johnny.LLM",
        "timestamp": 0.0,
        "duration": 0.8,
        "ttft": 0.25,
        "cancelled": False,
        "completion_tokens": 12,
        "prompt_tokens": 100,
        "total_tokens": 112,
        "speech_id": "speech_1",
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _tts_metric(**kw: Any) -> SimpleNamespace:
    base = {
        "type": "tts_metrics",
        "label": "johnny.TTS",
        "timestamp": 0.0,
        "duration": 0.6,
        "ttfb": 0.1,
        "audio_duration": 2.0,
        "cancelled": False,
        "characters_count": 42,
        "speech_id": "speech_1",
    }
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# terminal_outcome mapping                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("state", "reason", "expected"),
    [
        ("replied", None, "spoken"),
        ("pending_approval", None, "pending"),
        ("no_reply", "suggest_only", "suggested"),
        ("no_reply", "approval_rejected", "rejected"),
        ("no_reply", "router_declined", "suppressed"),
        ("no_reply", "low_confidence", "suppressed"),
        ("no_reply", "barge_in", "suppressed"),
        ("no_reply", "model_empty_output", "suppressed"),
        ("no_reply", "stage_error", "suppressed"),
        ("no_reply", "listen_only", "suppressed"),
    ],
)
def test_terminal_outcome_mapping(state: Any, reason: Any, expected: str) -> None:
    assert terminal_outcome(state, reason) == expected


# --------------------------------------------------------------------------- #
# build_session_terminal_emitter                                              #
# --------------------------------------------------------------------------- #


async def test_terminal_emitter_publishes_turn_terminal_with_int_turn_id() -> None:
    bus = InMemoryEventBus()
    index = TurnIndex()
    emit = build_session_terminal_emitter(bus, index, session_id="7", clock=lambda: 111)

    await emit(
        "item_abc",
        GateTerminal(
            terminal_state="no_reply",
            no_reply_reason="router_declined",
            detail="side chatter",
        ),
    )

    (event,) = bus.snapshot()
    assert event.type == "turn_terminal"
    assert event.turn_id == 1  # str → int via the shared index
    assert event.terminal_state == "no_reply"
    assert event.outcome == "suppressed"
    assert event.no_reply_reason == "router_declined"
    assert event.detail == "side chatter"
    assert event.session_id == "7"
    assert event.timestamp_ms == 111


async def test_terminal_emitter_replied_maps_outcome_spoken() -> None:
    bus = InMemoryEventBus()
    emit = build_session_terminal_emitter(bus, TurnIndex(), session_id="1")
    await emit(
        "item_x",
        GateTerminal(terminal_state="replied", no_reply_reason=None, detail="bot spoke"),
    )
    (event,) = bus.snapshot()
    assert event.terminal_state == "replied"
    assert event.outcome == "spoken"
    assert event.no_reply_reason is None


async def test_terminal_emitter_swallows_bus_failure() -> None:
    """A failing bus must not crash the terminal path (lost row, not a crash)."""
    emit = build_session_terminal_emitter(_FlakyBus(), TurnIndex(), session_id="1")
    # Must not raise.
    await emit(
        "item_x",
        GateTerminal(terminal_state="no_reply", no_reply_reason="stage_error", detail=""),
    )


# --------------------------------------------------------------------------- #
# build_decision_emitter                                                      #
# --------------------------------------------------------------------------- #


async def test_decision_emitter_publishes_router_decision_made() -> None:
    bus = InMemoryEventBus()
    index = TurnIndex()
    record = build_decision_emitter(
        bus,
        index,
        mode="limited_auto_speak",
        approval_timeout_seconds=15.0,
        session_id="9",
        clock=lambda: 222,
    )

    await record(
        _decision(
            should_speak=True,
            confidence=0.83,
            reason="directly asked",
            reply_type="answer",
            suggested_reply="affirmative",
            raw={"finish_reason": "stop"},
        ),
        "item_q",
    )

    (event,) = bus.snapshot()
    assert event.type == "router_decision_made"
    assert event.should_speak is True
    assert event.confidence == 0.83
    assert event.reason == "directly asked"
    assert event.reply_type == "answer"
    assert event.suggested_reply == "affirmative"
    assert event.turn_id == 1
    assert event.input_window == {
        "mode": "limited_auto_speak",
        "approval_timeout_seconds": 15.0,
    }
    assert event.raw_output == {"finish_reason": "stop"}
    assert event.session_id == "9"
    assert event.timestamp_ms == 222


async def test_decision_and_terminal_share_int_turn_id() -> None:
    """The parity lynchpin: one str turn id → one int across decision + terminal."""
    bus = InMemoryEventBus()
    index = TurnIndex()
    record = build_decision_emitter(bus, index, mode="autonomous", session_id="1")
    emit = build_session_terminal_emitter(bus, index, session_id="1")

    await record(_decision(), "item_same")
    await emit(
        "item_same",
        GateTerminal(terminal_state="replied", no_reply_reason=None, detail=""),
    )

    decision_ev, terminal_ev = bus.snapshot()
    assert decision_ev.turn_id == terminal_ev.turn_id == 1


# --------------------------------------------------------------------------- #
# build_suggested_emitter                                                     #
# --------------------------------------------------------------------------- #


async def test_suggested_emitter_publishes_agent_suggested() -> None:
    bus = InMemoryEventBus()
    record = build_suggested_emitter(bus, session_id="3", clock=lambda: 5)
    await record(
        _decision(reason="user asked for options", suggested_reply="  Option A  "),
        "item_s",
    )
    (event,) = bus.snapshot()
    assert event.type == "agent_suggested"
    assert event.suggested_reply == "Option A"  # stripped
    assert event.decision_id is None  # async-persisted row, no sync id
    assert event.reason == "user asked for options"
    assert event.session_id == "3"
    assert event.timestamp_ms == 5


# --------------------------------------------------------------------------- #
# build_spoke_emitter                                                         #
# --------------------------------------------------------------------------- #


async def test_spoke_emitter_publishes_agent_spoke() -> None:
    bus = InMemoryEventBus()
    record = build_spoke_emitter(bus, mode="autonomous", session_id="4", clock=lambda: 9)
    await record("Here is the summary.")
    (event,) = bus.snapshot()
    assert event.type == "agent_spoke"
    assert event.text == "Here is the summary."
    assert event.matched_allowed_reply is None  # free-form mode
    assert event.audio_duration_ms == 0
    assert event.prompt == ""
    assert event.session_id == "4"


async def test_spoke_emitter_matches_allowed_reply_case_insensitive() -> None:
    bus = InMemoryEventBus()
    record = build_spoke_emitter(
        bus,
        mode="limited_auto_speak",
        allowed_replies=("Yes", "No", "Maybe"),
        session_id="4",
    )
    await record("yes")
    (event,) = bus.snapshot()
    assert event.matched_allowed_reply == "Yes"  # canonical casing


async def test_spoke_emitter_no_match_leaves_matched_none() -> None:
    bus = InMemoryEventBus()
    record = build_spoke_emitter(
        bus, mode="limited_auto_speak", allowed_replies=("Yes", "No"), session_id="4"
    )
    await record("absolutely")
    (event,) = bus.snapshot()
    assert event.matched_allowed_reply is None


async def test_spoke_emitter_autonomous_never_matches_allowlist() -> None:
    """Free-form modes ignore the allow-list (parity with ``_answer_and_speak``)."""
    bus = InMemoryEventBus()
    record = build_spoke_emitter(bus, mode="autonomous", allowed_replies=("Yes",), session_id="4")
    await record("Yes")
    (event,) = bus.snapshot()
    assert event.matched_allowed_reply is None


# --------------------------------------------------------------------------- #
# build_transcript_finalized_emitter                                          #
# --------------------------------------------------------------------------- #


async def test_transcript_finalized_emitter_publishes() -> None:
    bus = InMemoryEventBus()
    sink = build_transcript_finalized_emitter(bus, session_id="2")
    event = TranscriptFinalized(
        text="hello there", timestamp_ms=10, speaker="alice", confidence=0.9, session_id="2"
    )
    await sink(event)
    (published,) = bus.snapshot()
    assert published is event


async def test_transcript_finalized_emitter_swallows_failure() -> None:
    sink = build_transcript_finalized_emitter(_FlakyBus(), session_id="2")
    await sink(TranscriptFinalized(text="x", timestamp_ms=0, session_id="2"))  # no raise


# --------------------------------------------------------------------------- #
# metric_to_timing (pure translation)                                         #
# --------------------------------------------------------------------------- #


def test_metric_to_timing_stt() -> None:
    timing = metric_to_timing(
        _stt_metric(duration=0.12, audio_duration=1.5),
        turn_id=3,
        started_at_ms=100,
        session_id="1",
    )
    assert timing is not None
    assert timing.stage == "stt"
    assert timing.turn_id == 3
    assert timing.started_at_ms == 100
    assert timing.duration_ms == 120
    assert timing.provider_name == "johnny.STT"
    assert timing.details["audio_duration_ms"] == 1500
    assert timing.details["streamed"] is True


def test_metric_to_timing_llm_maps_to_answer_llm() -> None:
    timing = metric_to_timing(_llm_metric(duration=0.8, ttft=0.25), turn_id=4, started_at_ms=0)
    assert timing is not None
    assert timing.stage == "answer_llm"
    assert timing.duration_ms == 800
    assert timing.details["time_to_first_token_ms"] == 250
    assert timing.details["completion_tokens"] == 12
    assert timing.details["total_tokens"] == 112


def test_metric_to_timing_tts() -> None:
    timing = metric_to_timing(
        _tts_metric(duration=0.6, ttfb=0.1, audio_duration=2.0),
        turn_id=4,
        started_at_ms=0,
    )
    assert timing is not None
    assert timing.stage == "tts"
    assert timing.duration_ms == 600
    assert timing.details["time_to_first_audio_ms"] == 100
    assert timing.details["audio_duration_ms"] == 2000
    assert timing.details["characters_count"] == 42


@pytest.mark.parametrize("metric_type", ["eou_metrics", "vad_metrics", "weird", None])
def test_metric_to_timing_drops_non_stage_metrics(metric_type: Any) -> None:
    assert (
        metric_to_timing(
            SimpleNamespace(type=metric_type, duration=0.1, timestamp=0.0),
            turn_id=1,
            started_at_ms=0,
        )
        is None
    )


def test_metric_to_timing_clamps_negative_turn_id() -> None:
    timing = metric_to_timing(_stt_metric(), turn_id=-5, started_at_ms=-10)
    assert timing is not None
    assert timing.turn_id == 0
    assert timing.started_at_ms == 0


# --------------------------------------------------------------------------- #
# MetricsTranslator (sync callback → async publish)                           #
# --------------------------------------------------------------------------- #


async def test_metrics_translator_publishes_timing() -> None:
    bus = InMemoryEventBus()
    seen: list[str | None] = []

    def resolve(speech_id: str | None) -> int:
        seen.append(speech_id)
        return 7 if speech_id else 0

    translator = MetricsTranslator(bus, resolve_turn_id=resolve, session_id="1")
    translator.on_metrics_collected(SimpleNamespace(metrics=_llm_metric(speech_id="speech_9")))
    await translator.aclose()

    (event,) = bus.snapshot()
    assert event.type == "pipeline_timing"
    assert event.stage == "answer_llm"
    assert event.turn_id == 7
    assert seen == ["speech_9"]


async def test_metrics_translator_stt_resolves_with_none_speech_id() -> None:
    bus = InMemoryEventBus()
    calls: list[str | None] = []

    def resolve(speech_id: str | None) -> int:
        calls.append(speech_id)
        return 2

    translator = MetricsTranslator(bus, resolve_turn_id=resolve, session_id="1")
    translator.on_metrics_collected(SimpleNamespace(metrics=_stt_metric()))
    await translator.aclose()

    assert calls == [None]  # STT carries no speech_id
    (event,) = bus.snapshot()
    assert event.stage == "stt"
    assert event.turn_id == 2


async def test_metrics_translator_offsets_started_at_from_session_start() -> None:
    bus = InMemoryEventBus()
    translator = MetricsTranslator(
        bus,
        resolve_turn_id=lambda _sid: 1,
        session_started_at=100.0,
        session_id="1",
    )
    translator.on_metrics_collected(SimpleNamespace(metrics=_stt_metric(timestamp=102.5)))
    await translator.aclose()
    (event,) = bus.snapshot()
    assert event.started_at_ms == 2500  # (102.5 - 100.0) * 1000


async def test_metrics_translator_drops_non_stage_metric() -> None:
    bus = InMemoryEventBus()
    translator = MetricsTranslator(bus, resolve_turn_id=lambda _s: 1, session_id="1")
    translator.on_metrics_collected(
        SimpleNamespace(metrics=SimpleNamespace(type="eou_metrics", timestamp=0.0))
    )
    await translator.aclose()
    assert bus.snapshot() == []


# --------------------------------------------------------------------------- #
# build_observability factory                                                 #
# --------------------------------------------------------------------------- #


async def test_build_observability_wires_every_seam() -> None:
    bus = InMemoryEventBus()
    index = TurnIndex()
    obs = build_observability(
        bus,
        index,
        mode="limited_auto_speak",
        allowed_replies=("Yes",),
        approval_timeout_seconds=20.0,
        resolve_turn_id=lambda _s: 5,
        session_id="1",
    )
    # Each seam is present and emits onto the same bus + index.
    await obs.record_decision(_decision(suggested_reply="Yes"), "item_t")
    await obs.session_terminal_emitter(
        "item_t", GateTerminal(terminal_state="replied", no_reply_reason=None, detail="")
    )
    await obs.record_spoke("Yes")
    obs.metrics_translator.on_metrics_collected(SimpleNamespace(metrics=_tts_metric()))
    await obs.metrics_translator.aclose()

    types = [e.type for e in bus.snapshot()]
    assert types == [
        "router_decision_made",
        "turn_terminal",
        "agent_spoke",
        "pipeline_timing",
    ]
    decision_ev, terminal_ev = bus.snapshot()[0], bus.snapshot()[1]
    assert decision_ev.turn_id == terminal_ev.turn_id == 1  # shared index


# --------------------------------------------------------------------------- #
# Replay parity: emitted events → real subscriber → DB rows                   #
# --------------------------------------------------------------------------- #


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
            GoogleAccount.__table__,  # type: ignore[list-item]
            CalendarEvent.__table__,  # type: ignore[list-item]
            ProfileTemplate.__table__,  # type: ignore[list-item]
            MeetingConfig.__table__,  # type: ignore[list-item]
            BotSession.__table__,  # type: ignore[list-item]
            AgentDecision.__table__,  # type: ignore[list-item]
            AgentUtterance.__table__,  # type: ignore[list-item]
            SessionTiming.__table__,  # type: ignore[list-item]
            TranscriptChunk.__table__,  # type: ignore[list-item]
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


def _seed(db_session: Session) -> BotSession:
    row = BotSession(meeting_config_id=1, status=BotSessionStatus.JOINED)
    db_session.add(row)
    db_session.flush()
    db_session.commit()
    return row


async def _drive(bus: InMemoryEventBus, db_session: Session) -> None:
    """Feed every emitted event through the real subscriber, in emit order.

    Event types the subscriber does not persist (``agent_suggested`` is live-UI
    only) are skipped, exactly as ``_apply_in_transaction`` passes them through.
    """
    dispatch = {
        "transcript_finalized": apply_transcript_event,
        "router_decision_made": lambda db, p: apply_router_decision_event(db, p)[0],
        "agent_spoke": apply_agent_spoke_event,
        "pipeline_timing": apply_pipeline_timing_event,
        "turn_terminal": apply_turn_terminal_event,
    }
    for event in bus.snapshot():
        payload = event_to_dict(event)
        handler = dispatch.get(payload["type"])
        if handler is not None:
            handler(db_session, payload)
    db_session.flush()


async def test_replay_speak_turn_decision_utterance_terminal_parity(
    db_session: Session,
) -> None:
    """A full speak turn: one decision row, linked utterance, terminal stamped on it."""
    bot = _seed(db_session)
    bus = InMemoryEventBus()
    index = TurnIndex()
    record = build_decision_emitter(bus, index, mode="limited_auto_speak", session_id=str(bot.id))
    spoke = build_spoke_emitter(bus, mode="limited_auto_speak", session_id=str(bot.id))
    terminal = build_session_terminal_emitter(bus, index, session_id=str(bot.id))

    await record(_decision(should_speak=True, suggested_reply="affirmative"), "item_1")
    await spoke("affirmative")
    await terminal(
        "item_1",
        GateTerminal(terminal_state="replied", no_reply_reason=None, detail="bot spoke"),
    )
    await _drive(bus, db_session)

    decision = db_session.scalars(sa.select(AgentDecision)).one()
    utterance = db_session.scalars(sa.select(AgentUtterance)).one()
    assert decision.turn_id == 1
    assert decision.outcome == DecisionOutcome.SPOKEN
    assert decision.terminal_state == TerminalState.REPLIED
    assert decision.final_text == "affirmative"
    # decision↔utterance parity — the utterance links to the one decision row.
    assert utterance.agent_decision_id == decision.id
    assert utterance.output_text == "affirmative"


async def test_replay_declined_turn_binds_terminal_to_decision(
    db_session: Session,
) -> None:
    bot = _seed(db_session)
    bus = InMemoryEventBus()
    index = TurnIndex()
    record = build_decision_emitter(bus, index, mode="limited_auto_speak", session_id=str(bot.id))
    terminal = build_session_terminal_emitter(bus, index, session_id=str(bot.id))

    await record(_decision(should_speak=False, reason="side chatter"), "item_2")
    await terminal(
        "item_2",
        GateTerminal(
            terminal_state="no_reply",
            no_reply_reason="router_declined",
            detail="side chatter",
        ),
    )
    await _drive(bus, db_session)

    # Exactly one decision row (terminal stamped it, did NOT create an orphan).
    decision = db_session.scalars(sa.select(AgentDecision)).one()
    assert decision.turn_id == 1
    assert decision.should_speak is False
    assert decision.outcome == DecisionOutcome.SUPPRESSED
    assert decision.terminal_state == TerminalState.NO_REPLY
    assert decision.no_reply_reason == NoReplyReason.ROUTER_DECLINED


async def test_replay_suggest_only_turn_outcome_suggested(
    db_session: Session,
) -> None:
    bot = _seed(db_session)
    bus = InMemoryEventBus()
    index = TurnIndex()
    record = build_decision_emitter(bus, index, mode="suggest_only", session_id=str(bot.id))
    suggested = build_suggested_emitter(bus, session_id=str(bot.id))
    terminal = build_session_terminal_emitter(bus, index, session_id=str(bot.id))

    decision = _decision(should_speak=True, suggested_reply="Consider option B")
    await record(decision, "item_3")
    await suggested(decision, "item_3")
    await terminal(
        "item_3",
        GateTerminal(
            terminal_state="no_reply",
            no_reply_reason="suggest_only",
            detail="suggest-only mode: nothing spoken",
        ),
    )
    await _drive(bus, db_session)

    row = db_session.scalars(sa.select(AgentDecision)).one()
    assert row.outcome == DecisionOutcome.SUGGESTED  # not suppressed
    assert row.terminal_state == TerminalState.NO_REPLY
    assert row.no_reply_reason == NoReplyReason.SUGGEST_ONLY
    # No utterance was spoken.
    assert db_session.scalars(sa.select(AgentUtterance)).all() == []


async def test_replay_autospeak_optimistic_spoken_demoted_by_terminal(
    db_session: Session,
) -> None:
    """Auto-speak writes ``spoken`` at router time; a no_reply terminal demotes it."""
    bot = _seed(db_session)
    bus = InMemoryEventBus()
    index = TurnIndex()
    record = build_decision_emitter(bus, index, mode="autonomous", session_id=str(bot.id))
    terminal = build_session_terminal_emitter(bus, index, session_id=str(bot.id))

    await record(_decision(should_speak=True), "item_4")
    await terminal(
        "item_4",
        GateTerminal(
            terminal_state="no_reply",
            no_reply_reason="model_empty_output",
            detail="reply produced no assistant output",
        ),
    )
    await _drive(bus, db_session)

    row = db_session.scalars(sa.select(AgentDecision)).one()
    assert row.terminal_state == TerminalState.NO_REPLY
    assert row.no_reply_reason == NoReplyReason.MODEL_EMPTY_OUTPUT
    assert row.outcome == DecisionOutcome.SUPPRESSED  # demoted from optimistic spoken


async def test_replay_transcript_finalized_persists_chunk(
    db_session: Session,
) -> None:
    bot = _seed(db_session)
    bus = InMemoryEventBus()
    sink = build_transcript_finalized_emitter(bus, session_id=str(bot.id))
    await sink(
        TranscriptFinalized(
            text="what is the plan",
            timestamp_ms=1234,
            speaker="bob",
            confidence=0.95,
            session_id=str(bot.id),
        )
    )
    await _drive(bus, db_session)

    chunk = db_session.scalars(sa.select(TranscriptChunk)).one()
    assert chunk.text == "what is the plan"
    assert chunk.speaker == "bob"
    assert chunk.bot_session_id == bot.id


async def test_replay_pipeline_timing_persists_session_timing(
    db_session: Session,
) -> None:
    bot = _seed(db_session)
    bus = InMemoryEventBus()
    translator = MetricsTranslator(bus, resolve_turn_id=lambda _s: 6, session_id=str(bot.id))
    translator.on_metrics_collected(SimpleNamespace(metrics=_tts_metric(duration=0.6)))
    await translator.aclose()
    await _drive(bus, db_session)

    timing = db_session.scalars(sa.select(SessionTiming)).one()
    assert timing.stage == "tts"
    assert timing.turn_id == 6
    assert timing.duration_ms == 600
    assert timing.details["time_to_first_audio_ms"] == 100
