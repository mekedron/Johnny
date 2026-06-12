"""Tests for the in-process browser AgentSession runner (Johnny-7g5.1).

Focus: :meth:`BrowserAgentSession.feed_text` — the typed-input path that maps onto
the engine. It must publish the user's text as a transcript, route the turn through
the **router gate** (so INV-1 + decision↔utterance parity apply exactly as for a
voice turn), and call ``generate_reply`` only on a SPEAK verdict. Guarded by
``importorskip`` so the suite collects without the ``agent`` extra.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

pytest.importorskip("livekit.agents")

from livekit.agents.llm import ChatContext  # noqa: E402
from livekit.agents.voice import SpeechHandle  # noqa: E402

from app.providers.base import (  # noqa: E402
    LLMProvider,
    LLMResponse,
)
from johnny.agent.browser_session import BrowserAgentSession  # noqa: E402
from johnny.agent.gate import GateTerminal, TurnLedger  # noqa: E402
from johnny.agent.router_gate import RouterGate, RouterGateConfig  # noqa: E402

# asyncio_mode = "auto" — async tests need no mark.


class _ScriptedRouterLLM(LLMProvider):
    def __init__(self, decision: dict[str, Any]) -> None:
        self._decision = decision

    @property
    def name(self) -> str:
        return "scripted-router"

    async def chat(
        self,
        messages: Any,
        tools: Any = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        import json

        return LLMResponse(
            text=json.dumps(self._decision),
            finish_reason="stop",
            structured_output=self._decision,
        )


class _RecordingEmitter:
    def __init__(self) -> None:
        self.records: list[tuple[str, GateTerminal]] = []

    async def __call__(self, turn_id: str, terminal: GateTerminal) -> None:
        self.records.append((turn_id, terminal))


class _Item:
    def __init__(self, text: str) -> None:
        self.text_content = text


class _FakeSpeechHandle:
    def __init__(self) -> None:
        self.id = "reply-1"
        self.interrupted = False
        self.chat_items = [_Item("Sure, here you go.")]
        self._cbs: list[Any] = []

    def add_done_callback(self, cb: Any) -> None:
        self._cbs.append(cb)

    def fire_done(self) -> None:
        for cb in list(self._cbs):
            cb(self)


class _FakeSession:
    """A stand-in AgentSession exposing only what feed_text touches."""

    def __init__(self, gate: RouterGate) -> None:
        self.history = ChatContext.empty()
        self._activity = object()  # truthy → "running"
        self._gate = gate
        self.generate_reply_calls: list[str] = []
        self.generate_reply_ctxs: list[ChatContext] = []

    def generate_reply(self, *, user_input: str, chat_ctx: ChatContext) -> SpeechHandle:
        self.generate_reply_calls.append(user_input)
        self.generate_reply_ctxs.append(chat_ctx)
        handle = _FakeSpeechHandle()
        # Simulate JohnnyAgent.on_enter's speech_created listener binding the reply.
        self._gate.bind_reply(cast(SpeechHandle, handle))
        handle.fire_done()
        return cast(SpeechHandle, handle)


def _make_session(
    decision: dict[str, Any],
) -> tuple[BrowserAgentSession, _FakeSession, _RecordingEmitter, list[Any]]:
    emitter = _RecordingEmitter()
    ledger = TurnLedger(emitter)
    decisions: list[Any] = []

    async def _record_decision(
        d: Any, turn_id: str, *, transcript_window: Any = None
    ) -> None:
        decisions.append((turn_id, d))

    gate = RouterGate(
        _ScriptedRouterLLM(decision),
        config=RouterGateConfig(mode="autonomous"),
        ledger=ledger,
        record_decision=_record_decision,
    )
    fake_session = _FakeSession(gate)
    transcripts: list[Any] = []

    async def _sink(ev: Any) -> None:
        transcripts.append(ev)

    runtime = cast(Any, type("R", (), {"gate": gate})())
    sess = BrowserAgentSession(
        runtime=runtime,
        session=cast(Any, fake_session),
        transport=cast(Any, None),
        audio_out=cast(Any, None),
        transcript_sink=_sink,
        session_id="7",
    )
    # Stash for assertions.
    sess._test_transcripts = transcripts  # type: ignore[attr-defined]
    sess._test_decisions = decisions  # type: ignore[attr-defined]
    return sess, fake_session, emitter, transcripts


async def test_feed_text_speak_routes_through_gate_to_generate_reply() -> None:
    sess, fake_session, emitter, transcripts = _make_session(
        {"should_speak": True, "confidence": 0.95, "reason": "addressed"}
    )
    ok = await sess.feed_text("Johnny, what's the status?")
    assert ok is True

    # User text published as a transcript (offset, not epoch — never overflows).
    assert len(transcripts) == 1
    assert transcripts[0].text == "Johnny, what's the status?"
    assert transcripts[0].speaker == "user"
    assert transcripts[0].timestamp_ms < 10_000_000  # session-relative ms

    # SPEAK → generate_reply called once with the typed text.
    assert fake_session.generate_reply_calls == ["Johnny, what's the status?"]

    # Drain the reply done-callback task, then assert exactly one replied terminal.
    await asyncio.gather(*sess._runtime.gate._reply_tasks)
    assert len(emitter.records) == 1
    turn_id, terminal = emitter.records[0]
    assert terminal.terminal_state == "replied"
    # decision↔terminal parity: the recorded decision shares the turn id.
    assert sess._test_decisions and sess._test_decisions[0][0] == turn_id  # type: ignore[attr-defined]


async def test_feed_text_router_declines_no_reply_and_one_terminal() -> None:
    sess, fake_session, emitter, transcripts = _make_session(
        {"should_speak": False, "confidence": 0.9, "reason": "not addressed"}
    )
    ok = await sess.feed_text("just thinking out loud")
    assert ok is True

    # Transcript still published, but no reply generated.
    assert len(transcripts) == 1
    assert fake_session.generate_reply_calls == []

    # Exactly one terminal — no_reply(router_declined) — owned by the gate (INV-1).
    assert len(emitter.records) == 1
    _, terminal = emitter.records[0]
    assert terminal.terminal_state == "no_reply"
    assert terminal.no_reply_reason == "router_declined"


async def test_feed_text_blank_is_rejected() -> None:
    sess, fake_session, _, transcripts = _make_session(
        {"should_speak": True, "confidence": 0.95, "reason": "x"}
    )
    assert await sess.feed_text("   ") is False
    assert transcripts == []
    assert fake_session.generate_reply_calls == []


async def test_feed_text_generates_from_a_copy_so_injection_never_persists() -> None:
    """The typed path mirrors the voice path's generation-scoped context
    (Johnny-0qw): the gate's task-grounding system message reaches the ctx
    handed to ``generate_reply`` but never the durable ``session.history`` —
    once the result is delivered, no stale task line can linger in history
    contradicting it."""
    from johnny.agent.tasks import InMemoryTaskSink, TaskCoordinator, stub_executor

    emitter = _RecordingEmitter()
    coordinator = TaskCoordinator(InMemoryTaskSink(), executor=stub_executor)
    gate = RouterGate(
        _ScriptedRouterLLM({"should_speak": True, "confidence": 0.95, "reason": "addressed"}),
        config=RouterGateConfig(mode="autonomous"),
        ledger=TurnLedger(emitter),
        tasks=coordinator,
    )
    # The settle→delivery window: a done result sits undelivered.
    coordinator.note_task_settled(
        7, status="done", kind="google-calendar", result_text="You have 3 events this week."
    )
    fake_session = _FakeSession(gate)
    fake_session.history.add_message(role="user", content="check the calendar please")

    async def _sink(ev: Any) -> None:
        pass

    runtime = cast(Any, type("R", (), {"gate": gate})())
    sess = BrowserAgentSession(
        runtime=runtime,
        session=cast(Any, fake_session),
        transport=cast(Any, None),
        audio_out=cast(Any, None),
        transcript_sink=_sink,
        session_id="7",
    )

    assert await sess.feed_text("so what's in the calendar?") is True

    # The reply generated from the gate's turn context: prior history + the
    # injected grounding message.
    assert len(fake_session.generate_reply_ctxs) == 1
    reply_ctx = fake_session.generate_reply_ctxs[0]
    assert reply_ctx is not fake_session.history
    injected = [
        item.text_content or ""
        for item in reply_ctx.items
        if getattr(item, "role", None) == "system"
    ]
    assert len(injected) == 1
    assert "You have 3 events this week." in injected[0]
    # The durable history holds NO system message — the injection was
    # generation-scoped (the SDK persists the user/assistant messages itself).
    assert [
        item for item in fake_session.history.items if getattr(item, "role", None) == "system"
    ] == []
    await coordinator.aclose()


async def test_warm_up_delegates_to_the_runtime() -> None:
    """Johnny-trt.8: the browser session's warm_up is the runtime's prewarm."""
    calls: list[str] = []

    class _Runtime:
        async def warm_up(self) -> None:
            calls.append("warm_up")

    sess = BrowserAgentSession(
        runtime=cast(Any, _Runtime()),
        session=cast(Any, None),
        transport=cast(Any, None),
        audio_out=cast(Any, None),
        transcript_sink=cast(Any, None),
        session_id="7",
    )
    await sess.warm_up()
    assert calls == ["warm_up"]


# --------------------------------------------------------------------------- #
# Live-caption listener (Johnny-trt.13)                                        #
# --------------------------------------------------------------------------- #


class _IOSlot:
    """Settable ``.audio`` attribute, like ``AgentSession.input`` / ``.output``."""

    def __init__(self) -> None:
        self.audio: Any = None


class _FakeStartableSession:
    """Stand-in AgentSession exposing what :meth:`BrowserAgentSession.start` touches."""

    def __init__(self) -> None:
        self.input = _IOSlot()
        self.output = _IOSlot()
        self.listeners: dict[str, Any] = {}
        self.started = False

    def on(self, name: str, cb: Any) -> None:
        self.listeners[name] = cb

    async def start(self, *, agent: Any) -> None:
        self.started = True


class _FakeTransport:
    def capture_frames(self) -> Any:
        return object()  # never iterated in these tests


async def test_start_registers_the_interim_caption_listener() -> None:
    """Johnny-trt.13: start() hangs the user_input_transcribed listener off the
    session, and a non-final hypothesis flows to the bus as TranscriptInterim."""
    from types import SimpleNamespace

    from johnny.agent.observability import InterimTranscriptForwarder
    from johnny.voice_pipeline.event_bus import InMemoryEventBus

    bus = InMemoryEventBus()
    forwarder = InterimTranscriptForwarder(bus, clock=lambda: 5, session_id="7")
    fake_session = _FakeStartableSession()
    sess = BrowserAgentSession(
        runtime=cast(Any, type("R", (), {"agent": object()})()),
        session=cast(Any, fake_session),
        transport=cast(Any, _FakeTransport()),
        audio_out=cast(Any, object()),
        transcript_sink=cast(Any, None),
        session_id="7",
        interim_forwarder=forwarder,
    )
    await sess.start()
    assert fake_session.started is True

    listener = fake_session.listeners.get("user_input_transcribed")
    assert listener is not None

    # The SDK fires interims (is_final=False) for streaming STT; finals are
    # skipped here (the stt_node gate owns the durable TranscriptFinalized).
    listener(SimpleNamespace(transcript="hello th", is_final=False, speaker_id=None))
    listener(SimpleNamespace(transcript="hello there", is_final=True, speaker_id=None))
    await forwarder.aclose()

    (event,) = bus.snapshot()
    assert event.type == "transcript_interim"
    assert event.text == "hello th"
    assert event.session_id == "7"


async def test_start_without_forwarder_registers_no_listener() -> None:
    """Direct constructions (tests, legacy callers) keep the pre-trt.13 shape."""
    fake_session = _FakeStartableSession()
    sess = BrowserAgentSession(
        runtime=cast(Any, type("R", (), {"agent": object()})()),
        session=cast(Any, fake_session),
        transport=cast(Any, _FakeTransport()),
        audio_out=cast(Any, object()),
        transcript_sink=cast(Any, None),
        session_id="7",
    )
    await sess.start()
    assert fake_session.started is True
    assert fake_session.listeners == {}


def test_browser_vad_loads_with_the_040_silence_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Johnny-trt.5: browser sessions get Silero with min_silence_duration=0.40.

    Browser-session-scoped ONLY — the Meet/room path's ``load_vad()`` default
    is pinned separately (test_session_endpointing.py).
    """
    from johnny.agent import browser_session
    from johnny.agent import session as session_mod

    assert browser_session.BROWSER_VAD_MIN_SILENCE_DURATION_S == 0.40

    calls: list[dict[str, Any]] = []

    def _fake_load(**kwargs: Any) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(session_mod.silero.VAD, "load", _fake_load)
    browser_session.load_browser_vad()
    assert calls == [{"min_silence_duration": 0.40}]


def test_shared_vad_caches_the_browser_vad(monkeypatch: pytest.MonkeyPatch) -> None:
    """_shared_vad loads the browser-tuned model once and reuses the handle."""
    from johnny.agent import browser_session

    created: list[object] = []

    def _fake_browser_load() -> object:
        handle = object()
        created.append(handle)
        return handle

    monkeypatch.setattr(browser_session, "load_browser_vad", _fake_browser_load)
    monkeypatch.setattr(browser_session, "_SHARED_VAD", None)
    first = browser_session._shared_vad()
    second = browser_session._shared_vad()
    assert first is second
    assert created == [first]


def test_browser_endpointing_pins_the_vad_floor_min_delay() -> None:
    """Johnny-trt.6: browser endpointing min_delay == the 0.40 s VAD floor.

    The two values are deliberately equal — min_delay overlaps (not stacks on)
    Silero's min_silence wait, so 0.40 commits the turn the moment the VAD
    floor is crossed with zero engine padding on top. max_delay must stay
    unset: with turn_detection="vad" no semantic model ever escalates to it.
    """
    from johnny.agent import browser_session

    assert browser_session.BROWSER_ENDPOINTING_MIN_DELAY_S == 0.40
    assert (
        browser_session.BROWSER_ENDPOINTING_MIN_DELAY_S
        == browser_session.BROWSER_VAD_MIN_SILENCE_DURATION_S
    )
    assert browser_session.browser_endpointing() == {"min_delay": 0.40}


# Stand-in for the process-shared Silero handle (one floor serves both the
# VAD-only and semantic paths — Johnny-1qr).
_PLAIN_VAD_SENTINEL = object()


def _patch_build_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    """Fake the heavy seams of BrowserAgentSession.build; capture session kwargs."""
    from types import SimpleNamespace

    from johnny.agent import browser_session

    captured: list[dict[str, Any]] = []

    async def _fake_runtime(config: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(
            adapters=SimpleNamespace(stt=object(), llm=object(), tts=None),
            enable_barge_in=True,
            min_interruption_duration_s=None,
            needs_approval_wiring=False,
            approval_gate=None,
        )

    def _fake_session(**kwargs: Any) -> object:
        captured.append(kwargs)
        return object()

    monkeypatch.setattr(browser_session, "build_agent_runtime", _fake_runtime)
    monkeypatch.setattr(browser_session, "build_agent_session", _fake_session)
    monkeypatch.setattr(browser_session, "_shared_vad", lambda: _PLAIN_VAD_SENTINEL)
    return captured


def _job_config(language: str | None = None) -> Any:
    """A minimal duck-typed SessionJobConfig for build() gate tests."""
    from types import SimpleNamespace

    options: dict[str, Any] = {} if language is None else {"language": language}
    return SimpleNamespace(
        bot_session_id=7,
        provider_config={"stt": {"options": options}},
    )


async def test_build_applies_browser_endpointing_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Johnny-trt.6: build() endpoints the session at the browser min_delay."""
    from types import SimpleNamespace

    from johnny.agent.browser_session import BrowserAgentSession

    captured = _patch_build_seams(monkeypatch)
    await BrowserAgentSession.build(
        SimpleNamespace(sample_rate=48000),  # type: ignore[arg-type]
        SimpleNamespace(bot_session_id=7),  # type: ignore[arg-type]
        event_bus=SimpleNamespace(),  # type: ignore[arg-type]
    )
    assert len(captured) == 1
    assert captured[0]["turn_detection"] == "vad"
    assert captured[0]["endpointing"] == {"min_delay": 0.40}


async def test_build_forwards_an_explicit_endpointing_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harness's A/B seam: an explicit endpointing dict wins verbatim."""
    from types import SimpleNamespace

    from johnny.agent.browser_session import BrowserAgentSession

    captured = _patch_build_seams(monkeypatch)
    await BrowserAgentSession.build(
        SimpleNamespace(sample_rate=48000),  # type: ignore[arg-type]
        SimpleNamespace(bot_session_id=7),  # type: ignore[arg-type]
        event_bus=SimpleNamespace(),  # type: ignore[arg-type]
        endpointing={"min_delay": 0.5},
    )
    assert len(captured) == 1
    assert captured[0]["endpointing"] == {"min_delay": 0.5}


# --------------------------------------------------------------------------- #
# In-process semantic turn detector gating (Johnny-1qr)                        #
# --------------------------------------------------------------------------- #


async def test_build_engages_the_semantic_detector_for_an_en_stt_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An English STT language engages the in-process EOU model + max_delay tuning."""
    from types import SimpleNamespace

    from johnny.agent.browser_session import BrowserAgentSession
    from johnny.agent.turn_detector import InProcessEnglishModel

    monkeypatch.delenv("JOHNNY_BROWSER_FORCE_VAD_TURNS", raising=False)
    captured = _patch_build_seams(monkeypatch)
    sess = await BrowserAgentSession.build(
        SimpleNamespace(sample_rate=48000),  # type: ignore[arg-type]
        _job_config(language="en"),
        event_bus=SimpleNamespace(),  # type: ignore[arg-type]
    )
    assert len(captured) == 1
    detector = captured[0]["turn_detection"]
    assert isinstance(detector, InProcessEnglishModel)
    # Same min_delay floor as the VAD-only path; max_delay live for the
    # "model says incomplete" hold (Johnny-1qr — the 0.20 s floor drop was
    # reverted in validation, see browser_session.py).
    assert captured[0]["endpointing"] == {"min_delay": 0.40, "max_delay": 1.5}
    assert captured[0]["vad"] is _PLAIN_VAD_SENTINEL  # one shared 0.40 s Silero
    assert sess.semantic_eou_active
    assert sess.turn_detection_label == "semantic-eou(en)"
    # The session carries the model's executor so warm_up() can pre-load it.
    assert sess._eou_executor is detector.executor  # noqa: SLF001
    assert not detector.executor.initialized  # build must never pay the model load


async def test_build_keeps_vad_for_a_non_english_stt_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fi config keeps the tuned VAD-only path (the en-only model is useless there)."""
    from types import SimpleNamespace

    from johnny.agent.browser_session import BrowserAgentSession

    monkeypatch.delenv("JOHNNY_BROWSER_FORCE_VAD_TURNS", raising=False)
    captured = _patch_build_seams(monkeypatch)
    sess = await BrowserAgentSession.build(
        SimpleNamespace(sample_rate=48000),  # type: ignore[arg-type]
        _job_config(language="fi"),
        event_bus=SimpleNamespace(),  # type: ignore[arg-type]
    )
    assert captured[0]["turn_detection"] == "vad"
    assert captured[0]["endpointing"] == {"min_delay": 0.40}
    assert captured[0]["vad"] is _PLAIN_VAD_SENTINEL
    assert not sess.semantic_eou_active
    assert sess.turn_detection_label == "vad"


async def test_build_kill_switch_forces_vad_even_for_en(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JOHNNY_BROWSER_FORCE_VAD_TURNS=1 pins the trt.6 path regardless of language."""
    from types import SimpleNamespace

    from johnny.agent.browser_session import BrowserAgentSession

    monkeypatch.setenv("JOHNNY_BROWSER_FORCE_VAD_TURNS", "1")
    captured = _patch_build_seams(monkeypatch)
    sess = await BrowserAgentSession.build(
        SimpleNamespace(sample_rate=48000),  # type: ignore[arg-type]
        _job_config(language="en"),
        event_bus=SimpleNamespace(),  # type: ignore[arg-type]
    )
    assert captured[0]["turn_detection"] == "vad"
    assert captured[0]["endpointing"] == {"min_delay": 0.40}
    assert captured[0]["vad"] is _PLAIN_VAD_SENTINEL
    assert not sess.semantic_eou_active


async def test_build_semantic_eou_false_forces_vad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harness baseline arm: semantic_eou=False never engages the detector."""
    from types import SimpleNamespace

    from johnny.agent.browser_session import BrowserAgentSession

    monkeypatch.delenv("JOHNNY_BROWSER_FORCE_VAD_TURNS", raising=False)
    captured = _patch_build_seams(monkeypatch)
    sess = await BrowserAgentSession.build(
        SimpleNamespace(sample_rate=48000),  # type: ignore[arg-type]
        _job_config(language="en"),
        event_bus=SimpleNamespace(),  # type: ignore[arg-type]
        semantic_eou=False,
    )
    assert captured[0]["turn_detection"] == "vad"
    assert captured[0]["endpointing"] == {"min_delay": 0.40}
    assert not sess.semantic_eou_active


async def test_build_semantic_eou_true_raises_when_the_gates_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit-on A/B arm must fail loudly, never silently measure VAD-only."""
    from types import SimpleNamespace

    from johnny.agent.browser_session import BrowserAgentSession

    monkeypatch.delenv("JOHNNY_BROWSER_FORCE_VAD_TURNS", raising=False)
    captured = _patch_build_seams(monkeypatch)
    with pytest.raises(RuntimeError, match="semantic_eou=True"):
        await BrowserAgentSession.build(
            SimpleNamespace(sample_rate=48000),  # type: ignore[arg-type]
            _job_config(language="fi"),
            event_bus=SimpleNamespace(),  # type: ignore[arg-type]
            semantic_eou=True,
        )
    assert captured == []  # failed before any session was assembled


async def test_build_explicit_endpointing_wins_over_the_semantic_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An A/B endpointing override is verbatim even with the detector engaged."""
    from types import SimpleNamespace

    from johnny.agent.browser_session import BrowserAgentSession

    monkeypatch.delenv("JOHNNY_BROWSER_FORCE_VAD_TURNS", raising=False)
    captured = _patch_build_seams(monkeypatch)
    sess = await BrowserAgentSession.build(
        SimpleNamespace(sample_rate=48000),  # type: ignore[arg-type]
        _job_config(language="en"),
        event_bus=SimpleNamespace(),  # type: ignore[arg-type]
        endpointing={"min_delay": 0.5},
    )
    assert captured[0]["endpointing"] == {"min_delay": 0.5}
    assert sess.semantic_eou_active  # the override changes endpointing, not the detector


def test_browser_semantic_endpointing_pins_floor_and_max_delay() -> None:
    """Johnny-1qr: semantic endpointing = the trt.6 floor + a live 1.5 s max_delay.

    min_delay deliberately equals the VAD-only path's (zero engine padding,
    one shared 0.40 s Silero floor — the 0.20 s floor drop was reverted in
    validation per the bead's varied-pause abort criterion); max_delay is
    meaningful here because the engaged turn detector escalates to it on a
    "model says incomplete" verdict.
    """
    from johnny.agent import browser_session

    assert browser_session.BROWSER_SEMANTIC_ENDPOINTING_MAX_DELAY_S == 1.5
    assert browser_session.browser_semantic_endpointing() == {
        "min_delay": browser_session.BROWSER_ENDPOINTING_MIN_DELAY_S,
        "max_delay": 1.5,
    }
    assert browser_session.browser_semantic_endpointing()["min_delay"] == 0.40


async def test_warm_up_also_initializes_the_eou_executor() -> None:
    """Johnny-1qr: warm_up pre-loads the EOU runner alongside the providers."""
    calls: list[str] = []

    class _Runtime:
        async def warm_up(self) -> None:
            calls.append("runtime")

    class _Executor:
        async def warm_up(self) -> None:
            calls.append("eou")

    sess = BrowserAgentSession(
        runtime=cast(Any, _Runtime()),
        session=cast(Any, None),
        transport=cast(Any, None),
        audio_out=cast(Any, None),
        transcript_sink=cast(Any, None),
        session_id="7",
        eou_executor=cast(Any, _Executor()),
    )
    await sess.warm_up()
    assert sorted(calls) == ["eou", "runtime"]


async def test_build_wires_the_interim_caption_forwarder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Johnny-trt.13: build() attaches an InterimTranscriptForwarder on the
    session's event bus so start() can register the live-caption listener."""
    from types import SimpleNamespace

    from johnny.agent.browser_session import BrowserAgentSession
    from johnny.agent.observability import InterimTranscriptForwarder
    from johnny.voice_pipeline.event_bus import InMemoryEventBus

    _patch_build_seams(monkeypatch)
    bus = InMemoryEventBus()
    sess = await BrowserAgentSession.build(
        SimpleNamespace(sample_rate=48000),  # type: ignore[arg-type]
        SimpleNamespace(bot_session_id=7),  # type: ignore[arg-type]
        event_bus=bus,
    )
    forwarder = sess._interim_forwarder  # noqa: SLF001
    assert isinstance(forwarder, InterimTranscriptForwarder)

    forwarder.on_user_input_transcribed(
        SimpleNamespace(transcript="live caption", is_final=False, speaker_id=None)
    )
    await forwarder.aclose()
    (event,) = bus.snapshot()
    assert event.type == "transcript_interim"
    assert event.session_id == "7"
    assert event.timestamp_ms >= 0  # session-relative clock, never epoch-sized
    assert event.timestamp_ms < 10_000_000
