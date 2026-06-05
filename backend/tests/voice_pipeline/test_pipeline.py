"""End-to-end tests for the voice pipeline.

The headline test runs the full pipeline against a synthetic WAV fixture
(two speech bursts separated by silence) with fake STT / router LLM /
answer LLM / TTS providers and an in-memory event bus, then asserts the
expected events fire in the expected order — the AC for US-022.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Sequence
from pathlib import Path
from typing import Any

import pytest

from app.providers import (
    ChatMessage,
    LLMProvider,
    LLMResponse,
    ProviderConfig,
    ProviderKind,
    STTProvider,
    ToolDefinition,
    TranscriptEvent,
    TTSProvider,
)
from johnny.voice_pipeline import (
    AgentSpoke,
    EnergyVAD,
    InMemoryDecisionSink,
    InMemoryEventBus,
    JohnnyTransport,
    PipelineConfig,
    RouterDecisionMade,
    TranscriptFinalized,
    VoicePipeline,
)
from johnny.voice_pipeline.decision_sink import DecisionSink
from johnny.voice_pipeline.pipeline import (
    RouterDecision,
    _match_allowed_reply,
    _parse_router_response,
    _pcm_duration_ms,
    _serialize_raw_output,
)

# --- fake transport --------------------------------------------------------


class _BufferedTransport(JohnnyTransport):
    """Push PCM frames in, capture played frames out — drives the pipeline."""

    def __init__(self, frames: list[bytes], sample_rate: int = 16_000) -> None:
        self._frames = list(frames)
        self._sample_rate = sample_rate
        self.played: list[bytes] = []
        self.played_source_rate: int | None = None
        self.started = False
        self.stopped = False

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def capture_frames(self) -> AsyncIterator[bytes]:
        for frame in self._frames:
            yield frame

    async def play_frames(
        self,
        frames: Iterable[bytes] | AsyncIterable[bytes],
        source_rate: int | None = None,
    ) -> None:
        self.played_source_rate = source_rate
        if isinstance(frames, AsyncIterable):
            async for f in frames:
                self.played.append(f)
        else:
            for f in frames:
                self.played.append(f)


# --- fake providers --------------------------------------------------------


def _fake_config(kind: ProviderKind, name: str = "fake") -> ProviderConfig:
    return ProviderConfig(kind=kind, provider_name=name, display_name=f"{kind.value}-{name}")


class _FakeSTT(STTProvider):
    def __init__(self, transcripts: list[str]) -> None:
        self._transcripts = list(transcripts)
        self._idx = 0
        self.calls = 0

    @property
    def name(self) -> str:
        return "fake-stt"

    async def transcribe_stream(
        self,
        audio_iter: AsyncIterator[bytes],
    ) -> AsyncIterator[TranscriptEvent]:
        async for _ in audio_iter:
            pass
        if self._idx >= len(self._transcripts):
            text = "<exhausted>"
        else:
            text = self._transcripts[self._idx]
        self._idx += 1
        self.calls += 1
        yield TranscriptEvent(
            text=text, is_final=True, timestamp_ms=self.calls * 1000, confidence=0.9
        )


class _FakeRouterLLM(LLMProvider):
    def __init__(self, decisions: list[dict[str, Any]]) -> None:
        self._decisions = list(decisions)
        self._idx = 0
        self.last_messages: Sequence[ChatMessage] | None = None
        self.last_response_format: dict[str, Any] | None = None
        self.calls: list[Sequence[ChatMessage]] = []

    @property
    def name(self) -> str:
        return "fake-router"

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.last_messages = messages
        self.last_response_format = response_format
        self.calls.append(list(messages))
        if self._idx >= len(self._decisions):
            decision = self._decisions[-1]
        else:
            decision = self._decisions[self._idx]
            self._idx += 1
        return LLMResponse(
            text=json.dumps(decision),
            finish_reason="stop",
            structured_output=decision,
        )


class _FakeAnswerLLM(LLMProvider):
    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self._idx = 0
        self.last_messages: Sequence[ChatMessage] | None = None

    @property
    def name(self) -> str:
        return "fake-answer"

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
        response_format: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> LLMResponse:
        self.last_messages = messages
        if self._idx >= len(self._answers):
            text = self._answers[-1]
        else:
            text = self._answers[self._idx]
            self._idx += 1
        return LLMResponse(text=text, finish_reason="stop")


class _FakeTTS(TTSProvider):
    def __init__(self, frame_count: int = 4) -> None:
        self._frame_count = frame_count
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return "fake-tts"

    async def synthesize_stream(
        self,
        text: str,
        voice_id: str | None = None,  # noqa: ARG002
    ) -> AsyncIterator[bytes]:
        self.calls.append(text)
        for i in range(self._frame_count):
            yield bytes([i & 0xFF, 0x00]) * 160  # 160 samples per "frame"


# --- end-to-end pipeline test ---------------------------------------------


async def test_pipeline_emits_events_in_order_for_two_utterance_wav(
    two_utterance_pcm: bytes, tmp_path: Path
) -> None:
    """The AC test: full pipeline against a fixture WAV, asserting event order."""
    del tmp_path  # not used; fixture file lives under it
    frame_size = (16_000 * 20 // 1000) * 2  # 20 ms @ 16 kHz s16 mono = 640 bytes
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]

    transport = _BufferedTransport(frames=frames)
    vad = EnergyVAD(threshold=0.05)
    stt = _FakeSTT(transcripts=["hello team", "any updates"])
    router = _FakeRouterLLM(
        decisions=[
            {
                "should_speak": True,
                "confidence": 0.9,
                "reason": "direct greeting",
                "reply_type": "acknowledgement",
                "suggested_reply": "Hi",
            },
            {
                "should_speak": False,
                "confidence": 0.4,
                "reason": "not addressed to bot",
            },
        ]
    )
    answer = _FakeAnswerLLM(answers=["Hi"])
    tts = _FakeTTS(frame_count=3)
    bus = InMemoryEventBus()
    cfg = PipelineConfig(
        instructions="Be helpful and brief",
        vad_threshold=0.05,
        end_of_speech_ms=300,
        frame_duration_ms=20,
        session_id="test-session",
        confidence_threshold=0.7,
    )

    pipeline = VoicePipeline(
        transport=transport,
        vad=vad,
        stt=stt,
        router_llm=router,
        answer_llm=answer,
        tts=tts,
        event_bus=bus,
        config=cfg,
    )

    await pipeline.run()

    events = bus.snapshot()
    types = [e.type for e in events]
    # Expected order: T(1), R(1), A(1), T(2), R(2) — second utterance is suppressed
    assert types == [
        "transcript_finalized",
        "router_decision_made",
        "agent_spoke",
        "transcript_finalized",
        "router_decision_made",
    ]

    t1 = events[0]
    assert isinstance(t1, TranscriptFinalized)
    assert t1.text == "hello team"
    assert t1.session_id == "test-session"

    r1 = events[1]
    assert isinstance(r1, RouterDecisionMade)
    assert r1.should_speak is True
    assert r1.confidence == pytest.approx(0.9)
    assert r1.suggested_reply == "Hi"

    a1 = events[2]
    assert isinstance(a1, AgentSpoke)
    assert a1.text == "Hi"
    assert a1.audio_duration_ms > 0

    t2 = events[3]
    assert isinstance(t2, TranscriptFinalized)
    assert t2.text == "any updates"

    r2 = events[4]
    assert isinstance(r2, RouterDecisionMade)
    assert r2.should_speak is False

    assert stt.calls == 2
    assert tts.calls == ["Hi"]
    assert len(transport.played) == 1  # one utterance spoken, one suppressed


# --- pipeline behaviour edge cases ----------------------------------------


async def test_pipeline_speak_false_skips_router_and_tts(
    two_utterance_pcm: bytes,
) -> None:
    """speak=False suppresses both router and TTS: only transcripts emit."""
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    stt = _FakeSTT(transcripts=["one", "two"])
    router = _FakeRouterLLM(decisions=[{"should_speak": True, "confidence": 1.0, "reason": "x"}])
    answer = _FakeAnswerLLM(answers=["resp"])
    tts = _FakeTTS()
    bus = InMemoryEventBus()
    cfg = PipelineConfig(speak=False, vad_threshold=0.05, end_of_speech_ms=300)
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=stt,
        router_llm=router,
        answer_llm=answer,
        tts=tts,
        event_bus=bus,
        config=cfg,
    )
    await pipeline.run()
    types = [e.type for e in bus.snapshot()]
    assert types == ["transcript_finalized", "transcript_finalized"]
    assert tts.calls == []
    assert transport.played == []


async def test_pipeline_below_confidence_threshold_skips_speaking(
    two_utterance_pcm: bytes,
) -> None:
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    bus = InMemoryEventBus()
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["one", "two"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": True, "confidence": 0.3, "reason": "weak"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["resp"]),
        tts=_FakeTTS(),
        event_bus=bus,
        config=PipelineConfig(
            vad_threshold=0.05,
            confidence_threshold=0.7,
            end_of_speech_ms=300,
        ),
    )
    await pipeline.run()
    spoke_events = [e for e in bus.snapshot() if e.type == "agent_spoke"]
    assert spoke_events == []


async def test_pipeline_allowed_replies_enforced_verbatim(
    two_utterance_pcm: bytes,
) -> None:
    """When allowed_replies is set, only verbatim (case-insensitive) matches speak."""
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    bus = InMemoryEventBus()
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hi", "bye"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": True, "confidence": 1.0, "reason": "ok"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["yes", "maybe"]),  # 'maybe' not in allowed
        tts=_FakeTTS(),
        event_bus=bus,
        config=PipelineConfig(
            allowed_replies=("yes", "no"),
            vad_threshold=0.05,
            confidence_threshold=0.5,
            end_of_speech_ms=300,
        ),
    )
    await pipeline.run()
    spokes = [e for e in bus.snapshot() if e.type == "agent_spoke"]
    assert len(spokes) == 1
    assert isinstance(spokes[0], AgentSpoke)
    assert spokes[0].text == "yes"
    assert spokes[0].matched_allowed_reply == "yes"


async def test_pipeline_vad_threshold_passed_through_config() -> None:
    cfg = PipelineConfig(vad_threshold=0.42)
    assert cfg.vad_threshold == 0.42


async def test_pipeline_silence_only_input_emits_nothing() -> None:
    silence = b"\x00" * 640
    transport = _BufferedTransport(frames=[silence] * 20)
    bus = InMemoryEventBus()
    router = _FakeRouterLLM(
        decisions=[{"should_speak": False, "confidence": 0, "reason": "x"}]
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["should-not-call"]),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=bus,
        config=PipelineConfig(vad_threshold=0.05, end_of_speech_ms=200),
    )
    await pipeline.run()
    assert bus.snapshot() == []


# --- helpers ---------------------------------------------------------------


def test_parse_router_response_with_structured_output() -> None:
    resp = LLMResponse(
        text="",
        finish_reason="stop",
        structured_output={
            "should_speak": True,
            "confidence": 0.9,
            "reason": "ok",
            "reply_type": "answer",
            "suggested_reply": "yes",
        },
    )
    d = _parse_router_response(resp)
    assert isinstance(d, RouterDecision)
    assert d.should_speak is True
    assert d.confidence == 0.9
    assert d.reason == "ok"
    assert d.reply_type == "answer"
    assert d.suggested_reply == "yes"


def test_parse_router_response_falls_back_to_text_json() -> None:
    resp = LLMResponse(
        text='{"should_speak": false, "confidence": 0.1, "reason": "noise"}',
        finish_reason="stop",
    )
    d = _parse_router_response(resp)
    assert d.should_speak is False
    assert d.confidence == 0.1
    assert d.reason == "noise"


def test_parse_router_response_no_structured_output_returns_safe_default() -> None:
    resp = LLMResponse(text="not json at all", finish_reason="stop")
    d = _parse_router_response(resp)
    assert d.should_speak is False
    assert d.confidence == 0.0


def test_parse_router_response_clamps_confidence() -> None:
    resp = LLMResponse(
        text="",
        finish_reason="stop",
        structured_output={"should_speak": True, "confidence": 2.5, "reason": "ok"},
    )
    d = _parse_router_response(resp)
    assert d.confidence == 1.0
    resp2 = LLMResponse(
        text="",
        finish_reason="stop",
        structured_output={"should_speak": True, "confidence": -0.5, "reason": "ok"},
    )
    d2 = _parse_router_response(resp2)
    assert d2.confidence == 0.0


def test_match_allowed_reply_exact() -> None:
    assert _match_allowed_reply("Yes", ("Yes", "No")) == "Yes"


def test_match_allowed_reply_case_insensitive() -> None:
    assert _match_allowed_reply("yes", ("Yes", "No")) == "Yes"
    assert _match_allowed_reply("  YES  ", ("Yes", "No")) == "Yes"


def test_match_allowed_reply_no_match() -> None:
    assert _match_allowed_reply("maybe", ("Yes", "No")) is None


def test_pcm_duration_ms() -> None:
    # 1600 samples at 16 kHz = 100 ms; 16-bit = 3200 bytes
    assert _pcm_duration_ms(3200, 16_000) == 100
    assert _pcm_duration_ms(0, 16_000) == 0
    assert _pcm_duration_ms(3200, 0) == 0


def test_pipeline_config_defaults() -> None:
    cfg = PipelineConfig()
    assert cfg.speak is True
    assert cfg.allowed_replies == ()
    assert cfg.vad_threshold > 0.0
    assert cfg.confidence_threshold > 0.0
    assert cfg.session_id is None


def test_pipeline_config_frozen() -> None:
    from dataclasses import FrozenInstanceError

    cfg = PipelineConfig(vad_threshold=0.3)
    with pytest.raises(FrozenInstanceError):
        cfg.vad_threshold = 0.5  # type: ignore[misc]


# --- US-023: rolling transcript window, mode, last decision in prompt ----


async def test_router_prompt_includes_mode_threshold_and_instructions(
    two_utterance_pcm: bytes,
) -> None:
    """Mode + threshold are always in the system prompt so the router can
    adjust behaviour; instructions are appended when configured."""
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    router = _FakeRouterLLM(
        decisions=[{"should_speak": False, "confidence": 0.2, "reason": "skip"}]
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hello team", "anything else"]),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            instructions="Be brief",
            context="standup",
            mode="approval_required",
            confidence_threshold=0.85,
            vad_threshold=0.05,
            end_of_speech_ms=300,
        ),
    )
    await pipeline.run()
    assert router.last_messages is not None
    system_msg = router.last_messages[0]
    assert system_msg.role == "system"
    assert system_msg.content is not None
    assert "Mode: approval_required" in system_msg.content
    assert "Confidence threshold for speaking: 0.85" in system_msg.content
    assert "Meeting instructions: Be brief" in system_msg.content
    assert "Context: standup" in system_msg.content


async def test_router_prompt_includes_rolling_transcript_window(
    two_utterance_pcm: bytes,
) -> None:
    """The second router invocation should see the first transcript in the
    rolling window so it can spot when a topic has already been addressed."""
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    router = _FakeRouterLLM(
        decisions=[
            {"should_speak": False, "confidence": 0.1, "reason": "first"},
            {"should_speak": False, "confidence": 0.1, "reason": "second"},
        ]
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hello team", "anything else"]),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05, end_of_speech_ms=300, transcript_window_size=5
        ),
    )
    await pipeline.run()
    # last_messages is from the SECOND call (router was called twice)
    assert router.last_messages is not None
    user_msg = router.last_messages[1]
    assert user_msg.role == "user"
    assert user_msg.content is not None
    assert "Recent conversation:" in user_msg.content
    assert "hello team" in user_msg.content  # prior transcript in window
    assert "Latest transcript: anything else" in user_msg.content


async def test_router_prompt_includes_last_decision_on_second_turn(
    two_utterance_pcm: bytes,
) -> None:
    """After the first decision, the router prompt for the second utterance
    should embed the prior decision so the model doesn't repeat itself."""
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    router = _FakeRouterLLM(
        decisions=[
            {
                "should_speak": True,
                "confidence": 0.9,
                "reason": "direct ask",
                "reply_type": "answer",
                "suggested_reply": "ok",
            },
            {"should_speak": False, "confidence": 0.1, "reason": "noise"},
        ]
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["one", "two"]),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["ok"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05, end_of_speech_ms=300, confidence_threshold=0.5
        ),
    )
    await pipeline.run()
    assert router.last_messages is not None
    second_user_msg = router.last_messages[1]
    assert second_user_msg.content is not None
    assert "Last router decision" in second_user_msg.content
    assert "direct ask" in second_user_msg.content


async def test_router_prompt_no_last_decision_on_first_turn(
    two_utterance_pcm: bytes,
) -> None:
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    router = _FakeRouterLLM(
        decisions=[{"should_speak": False, "confidence": 0.1, "reason": "skip"}]
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["only one"]),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(vad_threshold=0.05, end_of_speech_ms=300),
    )
    await pipeline.run()
    assert router.calls  # at least one router invocation
    first_user_msg = router.calls[0][1]
    assert first_user_msg.content is not None
    assert "Last router decision" not in first_user_msg.content


# --- US-023: event carries full input_window and raw_output --------------


async def test_router_decision_event_includes_input_window_and_raw_output(
    two_utterance_pcm: bytes,
) -> None:
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    bus = InMemoryEventBus()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hello", "world"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {
                    "should_speak": True,
                    "confidence": 0.95,
                    "reason": "asked",
                    "reply_type": "answer",
                    "suggested_reply": "hi",
                }
            ]
        ),
        answer_llm=_FakeAnswerLLM(answers=["hi"]),
        tts=_FakeTTS(),
        event_bus=bus,
        config=PipelineConfig(
            instructions="Be terse",
            context="meeting",
            mode="limited_auto_speak",
            allowed_replies=("hi", "bye"),
            confidence_threshold=0.5,
            vad_threshold=0.05,
            end_of_speech_ms=300,
        ),
    )
    await pipeline.run()
    decisions = [e for e in bus.snapshot() if e.type == "router_decision_made"]
    assert decisions
    first = decisions[0]
    assert isinstance(first, RouterDecisionMade)
    iw = first.input_window
    assert iw["instructions"] == "Be terse"
    assert iw["context"] == "meeting"
    assert iw["mode"] == "limited_auto_speak"
    assert iw["allowed_replies"] == ["hi", "bye"]
    assert iw["confidence_threshold"] == pytest.approx(0.5)
    assert iw["last_decision"] is None  # first turn
    window: list[dict[str, Any]] = iw["transcript_window"]
    assert len(window) == 1
    assert window[0]["text"] == "hello"
    assert window[0]["is_current"] is True

    raw = first.raw_output
    assert "text" in raw
    assert "structured" in raw
    assert raw["structured"]["reason"] == "asked"


# --- US-023: decision sink persistence ----------------------------------


async def test_pipeline_persists_decisions_with_spoken_outcome(
    two_utterance_pcm: bytes,
) -> None:
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    sink = InMemoryDecisionSink()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["one", "two"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {"should_speak": True, "confidence": 0.95, "reason": "yes"},
                {"should_speak": False, "confidence": 0.1, "reason": "no"},
            ]
        ),
        answer_llm=_FakeAnswerLLM(answers=["hi"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            confidence_threshold=0.5,
            vad_threshold=0.05,
            end_of_speech_ms=300,
            bot_session_id=42,
        ),
        decision_sink=sink,
    )
    await pipeline.run()
    records = sink.snapshot()
    assert len(records) == 2
    assert records[0].outcome == "spoken"
    assert records[0].bot_session_id == 42
    assert records[1].outcome == "suppressed"
    assert records[1].bot_session_id == 42


async def test_pipeline_persists_decision_when_below_threshold(
    two_utterance_pcm: bytes,
) -> None:
    """should_speak=True but confidence < threshold → outcome=suppressed."""
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    sink = InMemoryDecisionSink()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["one", "two"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": True, "confidence": 0.3, "reason": "weak"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            confidence_threshold=0.7,
            vad_threshold=0.05,
            end_of_speech_ms=300,
        ),
        decision_sink=sink,
    )
    await pipeline.run()
    records = sink.snapshot()
    assert len(records) == 2
    assert all(r.outcome == "suppressed" for r in records)


async def test_pipeline_does_not_persist_when_speak_false(
    two_utterance_pcm: bytes,
) -> None:
    """speak=False short-circuits the router entirely; no persistence either."""
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    sink = InMemoryDecisionSink()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["one", "two"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": True, "confidence": 0.95, "reason": "x"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(speak=False, vad_threshold=0.05, end_of_speech_ms=300),
        decision_sink=sink,
    )
    await pipeline.run()
    assert sink.snapshot() == []


async def test_pipeline_default_decision_sink_is_noop(two_utterance_pcm: bytes) -> None:
    """When no decision_sink is supplied, the pipeline still works (uses Noop)."""
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["one", "two"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": False, "confidence": 0.1, "reason": "no"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(vad_threshold=0.05, end_of_speech_ms=300),
    )
    # Should not raise — uses NoopDecisionSink by default.
    await pipeline.run()


async def test_pipeline_decision_sink_failure_does_not_crash(
    two_utterance_pcm: bytes,
) -> None:
    """A failing sink is logged and swallowed; the audio loop keeps going."""
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]

    class _BrokenSink(DecisionSink):
        async def record(self, decision, *, outcome="pending", bot_session_id=None):  # type: ignore[no-untyped-def]
            del decision, outcome, bot_session_id
            raise RuntimeError("db unavailable")

    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["one", "two"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": False, "confidence": 0.1, "reason": "no"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(vad_threshold=0.05, end_of_speech_ms=300),
        decision_sink=_BrokenSink(),
    )
    await pipeline.run()  # must not raise


# --- US-023: transcript window bounding ---------------------------------


async def test_pipeline_transcript_window_bounded() -> None:
    """The rolling transcript window is bounded by ``transcript_window_size``."""
    # Use minimal pipeline directly — just exercise _remember_transcript and
    # _build_input_window without spinning up the full audio loop.
    transport = _BufferedTransport(frames=[])
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=[]),
        router_llm=_FakeRouterLLM(decisions=[]),
        answer_llm=_FakeAnswerLLM(answers=[]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(transcript_window_size=3),
    )
    for i in range(5):
        pipeline._remember_transcript(
            TranscriptFinalized(text=f"t{i}", timestamp_ms=i * 100)
        )
    assert len(pipeline._transcript_history) == 3
    assert [t.text for t in pipeline._transcript_history] == ["t2", "t3", "t4"]


def test_pipeline_config_has_us023_defaults() -> None:
    cfg = PipelineConfig()
    assert cfg.mode == "limited_auto_speak"
    assert cfg.transcript_window_size == 6
    assert cfg.bot_session_id is None


def test_serialize_raw_output_shape() -> None:
    from app.providers import LLMResponse

    response = LLMResponse(
        text="raw text",
        finish_reason="stop",
        structured_output={"should_speak": True, "confidence": 0.9, "reason": "x"},
    )
    decision = _parse_router_response(response)
    raw = _serialize_raw_output(response, decision)
    assert raw["text"] == "raw text"
    assert raw["finish_reason"] == "stop"
    assert raw["structured"]["should_speak"] is True
    assert raw["structured"]["confidence"] == pytest.approx(0.9)


async def test_pipeline_emits_router_event_with_threshold_in_input_window(
    two_utterance_pcm: bytes,
) -> None:
    """The threshold actually used for the gating decision is captured for audit."""
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    bus = InMemoryEventBus()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["one"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": False, "confidence": 0.0, "reason": "n"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=bus,
        config=PipelineConfig(
            confidence_threshold=0.42,
            vad_threshold=0.05,
            end_of_speech_ms=300,
        ),
    )
    await pipeline.run()
    decisions = [e for e in bus.snapshot() if isinstance(e, RouterDecisionMade)]
    assert decisions
    assert decisions[0].input_window["confidence_threshold"] == pytest.approx(0.42)
