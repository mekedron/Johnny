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
    AgentSpeechInterimForwarder,
    InterimTranscriptForwarder,
    MetricsTranslator,
    build_decision_emitter,
    build_observability,
    build_session_terminal_emitter,
    build_spoke_emitter,
    build_suggested_emitter,
    build_transcript_finalized_emitter,
    build_triage_timing_emitter,
    metric_to_timing,
    terminal_outcome,
)
from johnny.voice_pipeline.event_bus import InMemoryEventBus  # noqa: E402
from johnny.voice_pipeline.events import (  # noqa: E402
    TranscriptFinalized,
    event_to_dict,
)
from johnny.voice_pipeline.reasoning import RouterDecision  # noqa: E402

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


async def test_spoke_emitter_attaches_reply_audio(tmp_path) -> None:
    """With a recorder, the event carries the flushed WAV name + exact duration (Johnny-od1)."""
    from johnny.voice_pipeline.audio_recorder import SpokenAudioRecorder

    recorder = SpokenAudioRecorder(tmp_path, 4, clock_ms=lambda: 77)
    recorder.feed_segment(b"\x01\x02" * 16_000)  # 1.0 s @ 32 000 B/s
    bus = InMemoryEventBus()
    record = build_spoke_emitter(
        bus, mode="autonomous", session_id="4", clock=lambda: 9, recorder=recorder
    )
    await record("Here is the summary.")
    (event,) = bus.snapshot()
    assert event.audio_file == "utt-77-1.wav"
    assert event.audio_duration_ms == 1000
    assert (tmp_path / "4" / "utt-77-1.wav").is_file()


async def test_spoke_emitter_empty_recorder_keeps_legacy_shape(tmp_path) -> None:
    """A recorder with nothing buffered (no-TTS degrade) yields the pre-capture shape."""
    from johnny.voice_pipeline.audio_recorder import SpokenAudioRecorder

    bus = InMemoryEventBus()
    record = build_spoke_emitter(
        bus,
        mode="autonomous",
        session_id="4",
        recorder=SpokenAudioRecorder(tmp_path, 4),
    )
    await record("text only")
    (event,) = bus.snapshot()
    assert event.audio_file is None
    assert event.audio_duration_ms == 0


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
# build_triage_timing_emitter (Johnny-trt.19)                                 #
# --------------------------------------------------------------------------- #


async def test_triage_timing_emitter_publishes_router_llm_stage() -> None:
    bus = InMemoryEventBus()
    index = TurnIndex()
    emit = build_triage_timing_emitter(
        bus,
        index,
        provider_name="openai_compatible",
        session_started_at=1000.0,
        session_id="7",
    )

    # 2.5 s into the session, a 1.5 s triage call that decided "delegate".
    await emit("item_abc", 1002.5, 1004.0, "delegate")

    (event,) = bus.snapshot()
    assert event.type == "pipeline_timing"
    assert event.stage == "router_llm"
    assert event.turn_id == 1  # str → int via the shared index
    assert event.started_at_ms == 2500  # session-relative call START
    assert event.duration_ms == 1500
    assert event.provider_name == "openai_compatible"
    assert event.details == {"action": "delegate"}
    assert event.session_id == "7"


async def test_triage_timing_emitter_shares_turn_index_with_terminals() -> None:
    """The timing row groups with the same turn's decision/terminal rows."""
    bus = InMemoryEventBus()
    index = TurnIndex()
    terminal_emit = build_session_terminal_emitter(bus, index, session_id="7")
    timing_emit = build_triage_timing_emitter(bus, index, session_id="7")

    await timing_emit("item_t1", 10.0, 10.2, "speak")
    await terminal_emit(
        "item_t1",
        GateTerminal(terminal_state="replied", no_reply_reason=None, detail="bot spoke"),
    )

    timing, terminal = bus.snapshot()
    assert timing.turn_id == terminal.turn_id == 1


async def test_triage_timing_emitter_without_session_start_uses_epoch_ms() -> None:
    """session_started_at <= 0 falls back to raw epoch ms (translator parity)."""
    bus = InMemoryEventBus()
    emit = build_triage_timing_emitter(bus, TurnIndex(), session_id="7")

    await emit("item_x", 12.0, 12.25, "silent")

    (event,) = bus.snapshot()
    assert event.started_at_ms == 12_000
    assert event.duration_ms == 250


async def test_triage_timing_emitter_swallows_bus_failure() -> None:
    """A failing bus loses the timing row, never crashes the gate."""
    emit = build_triage_timing_emitter(_FlakyBus(), TurnIndex(), session_id="7")
    await emit("item_x", 1.0, 2.0, "speak")  # no raise


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
# InterimTranscriptForwarder (live captions, Johnny-trt.13)                    #
# --------------------------------------------------------------------------- #


def _interim_forwarder(
    bus: InMemoryEventBus, *, session_id: str = "1"
) -> InterimTranscriptForwarder:
    return InterimTranscriptForwarder(bus, clock=lambda: 42, session_id=session_id)


def _transcribed(text: str, *, is_final: bool = False, speaker_id: str | None = None) -> Any:
    """Shape of the SDK's ``UserInputTranscribedEvent`` (transcript/is_final/speaker_id)."""
    return SimpleNamespace(transcript=text, is_final=is_final, speaker_id=speaker_id)


async def test_interim_forwarder_publishes_transcript_interim() -> None:
    bus = InMemoryEventBus()
    forwarder = _interim_forwarder(bus)
    forwarder.on_user_input_transcribed(_transcribed("hello th", speaker_id="spk-1"))
    await forwarder.aclose()

    (event,) = bus.snapshot()
    assert event.type == "transcript_interim"
    assert event.text == "hello th"
    assert event.timestamp_ms == 42
    assert event.speaker == "spk-1"
    assert event.session_id == "1"


async def test_interim_forwarder_skips_finals_and_empty_hypotheses() -> None:
    bus = InMemoryEventBus()
    forwarder = _interim_forwarder(bus)
    forwarder.on_user_input_transcribed(_transcribed("the final text", is_final=True))
    forwarder.on_user_input_transcribed(_transcribed(""))
    forwarder.on_user_input_transcribed(_transcribed("   "))
    await forwarder.aclose()
    assert bus.snapshot() == []


async def test_interim_forwarder_drops_consecutive_duplicates() -> None:
    bus = InMemoryEventBus()
    forwarder = _interim_forwarder(bus)
    forwarder.on_user_input_transcribed(_transcribed("hello"))
    forwarder.on_user_input_transcribed(_transcribed("hello"))
    forwarder.on_user_input_transcribed(_transcribed("hello there"))
    await forwarder.aclose()
    assert [e.text for e in bus.snapshot()] == ["hello", "hello there"]


async def test_interim_forwarder_final_resets_the_duplicate_guard() -> None:
    # Two consecutive turns may produce the textually identical hypothesis
    # ("yes" → final → "yes"); the second turn's caption must still go out.
    bus = InMemoryEventBus()
    forwarder = _interim_forwarder(bus)
    forwarder.on_user_input_transcribed(_transcribed("yes"))
    forwarder.on_user_input_transcribed(_transcribed("yes", is_final=True))
    forwarder.on_user_input_transcribed(_transcribed("yes"))
    await forwarder.aclose()
    assert [e.text for e in bus.snapshot()] == ["yes", "yes"]


async def test_interim_forwarder_speaker_defaults_to_none() -> None:
    bus = InMemoryEventBus()
    forwarder = _interim_forwarder(bus)
    forwarder.on_user_input_transcribed(_transcribed("hi", speaker_id=""))
    await forwarder.aclose()
    (event,) = bus.snapshot()
    assert event.speaker is None


async def test_interim_forwarder_swallows_bus_failure() -> None:
    forwarder = InterimTranscriptForwarder(_FlakyBus(), clock=lambda: 0, session_id="1")
    forwarder.on_user_input_transcribed(_transcribed("hello"))
    await forwarder.aclose()  # must not raise


# --------------------------------------------------------------------------- #
# AgentSpeechInterimForwarder (live bot-reply captions, Johnny-trt.39)        #
# --------------------------------------------------------------------------- #


def _speech_forwarder(
    bus: InMemoryEventBus,
    *,
    resolve_turn: Any = lambda: 7,
    session_id: str = "1",
) -> AgentSpeechInterimForwarder:
    return AgentSpeechInterimForwarder(
        bus, resolve_turn=resolve_turn, clock=lambda: 42, session_id=session_id
    )


async def test_speech_forwarder_publishes_agent_speech_interim() -> None:
    bus = InMemoryEventBus()
    forwarder = _speech_forwarder(bus)
    forwarder.on_sentence_flushed("Sure thing.", 0)
    forwarder.on_sentence_flushed("Here is the plan.", 1)
    await forwarder.aclose()

    first, second = bus.snapshot()
    assert first.type == "agent_speech_interim"
    assert (first.text, first.sequence, first.turn_id) == ("Sure thing.", 0, 7)
    assert first.timestamp_ms == 42
    assert first.session_id == "1"
    assert (second.text, second.sequence, second.turn_id) == ("Here is the plan.", 1, 7)


async def test_speech_forwarder_resolves_turn_once_per_reply() -> None:
    # The turn is captured at sequence 0 and reused for the reply's later
    # sentences: a rapid next turn binding mid-reply (active_reply is "most
    # recently bound", not "now playing") must not re-attribute the tail.
    bus = InMemoryEventBus()
    turns = iter([3, 99])
    forwarder = _speech_forwarder(bus, resolve_turn=lambda: next(turns))
    forwarder.on_sentence_flushed("One.", 0)
    forwarder.on_sentence_flushed("Two.", 1)
    forwarder.on_sentence_flushed("Next reply.", 0)  # re-resolves
    await forwarder.aclose()

    assert [(e.sequence, e.turn_id) for e in bus.snapshot()] == [(0, 3), (1, 3), (0, 99)]


async def test_speech_forwarder_uncorrelated_reply_carries_none() -> None:
    bus = InMemoryEventBus()
    forwarder = _speech_forwarder(bus, resolve_turn=lambda: None)
    forwarder.on_sentence_flushed("Heads up.", 0)
    await forwarder.aclose()
    (event,) = bus.snapshot()
    assert event.turn_id is None


async def test_speech_forwarder_skips_blank_sentences() -> None:
    bus = InMemoryEventBus()
    forwarder = _speech_forwarder(bus)
    forwarder.on_sentence_flushed("", 0)
    forwarder.on_sentence_flushed("   ", 1)
    await forwarder.aclose()
    assert bus.snapshot() == []


async def test_speech_forwarder_swallows_resolver_failure() -> None:
    def _boom() -> int:
        raise RuntimeError("gate gone")

    bus = InMemoryEventBus()
    forwarder = _speech_forwarder(bus, resolve_turn=_boom)
    forwarder.on_sentence_flushed("Still spoken.", 0)
    await forwarder.aclose()
    (event,) = bus.snapshot()
    assert event.turn_id is None
    assert event.text == "Still spoken."


async def test_speech_forwarder_swallows_bus_failure() -> None:
    forwarder = AgentSpeechInterimForwarder(
        _FlakyBus(), resolve_turn=lambda: 1, clock=lambda: 0, session_id="1"
    )
    forwarder.on_sentence_flushed("hello", 0)
    await forwarder.aclose()  # must not raise


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
