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

    def generate_reply(self, *, user_input: str) -> SpeechHandle:
        self.generate_reply_calls.append(user_input)
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

    async def _record_decision(d: Any, turn_id: str) -> None:
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
    monkeypatch.setattr(browser_session, "_shared_vad", lambda: object())
    return captured


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
