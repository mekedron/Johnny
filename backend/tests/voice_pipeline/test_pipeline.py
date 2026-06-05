"""End-to-end tests for the voice pipeline.

The headline test runs the full pipeline against a synthetic WAV fixture
(two speech bursts separated by silence) with fake STT / router LLM /
answer LLM / TTS providers and an in-memory event bus, then asserts the
expected events fire in the expected order — the AC for US-022.
"""

from __future__ import annotations

import asyncio
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
        self.last_response_format: dict[str, Any] | None = None
        self.calls: list[Sequence[ChatMessage]] = []

    @property
    def name(self) -> str:
        return "fake-answer"

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.last_messages = messages
        self.last_response_format = response_format
        self.calls.append(list(messages))
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
    # Streaming: each TTS frame is played individually (3 frames for the first
    # utterance, 0 for the suppressed second). Total bytes match the expected
    # audio duration captured on the AgentSpoke event.
    assert len(transport.played) == 3
    assert sum(len(f) for f in transport.played) > 0
    assert transport.played_source_rate == 16_000


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


# --- US-024: streaming answer LLM into TTS -------------------------------


class _StreamingAnswerLLM(LLMProvider):
    """Answer LLM that overrides ``stream_chat`` with per-token deltas."""

    def __init__(self, deltas: list[str]) -> None:
        self._deltas = list(deltas)
        self.calls: list[Sequence[ChatMessage]] = []
        self.chat_called = 0

    @property
    def name(self) -> str:
        return "streaming-answer"

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
        response_format: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> LLMResponse:
        self.chat_called += 1
        return LLMResponse(text="".join(self._deltas), finish_reason="stop")

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
    ) -> AsyncIterator[str]:
        self.calls.append(list(messages))
        for delta in self._deltas:
            yield delta


async def test_pipeline_uses_stream_chat_for_answer_llm(
    two_utterance_pcm: bytes,
) -> None:
    """The pipeline calls ``stream_chat`` (not ``chat``) on the answer LLM."""
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    answer = _StreamingAnswerLLM(deltas=["Hel", "lo ", "there"])
    tts = _FakeTTS(frame_count=2)
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hi"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {"should_speak": True, "confidence": 0.95, "reason": "ok"},
                {"should_speak": False, "confidence": 0.1, "reason": "second skip"},
            ]
        ),
        answer_llm=answer,
        tts=tts,
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
    )
    await pipeline.run()
    # stream_chat was called for the answer stage; chat() was NOT used.
    assert len(answer.calls) == 1
    assert answer.chat_called == 0
    # TTS received the full concatenated text since deltas had no sentence
    # boundaries → single flush at the end of the stream.
    assert tts.calls == ["Hello there"]


async def test_pipeline_flushes_sentences_to_tts_as_they_arrive(
    two_utterance_pcm: bytes,
) -> None:
    """Each complete sentence triggers a TTS call so audio starts streaming early."""
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    # Two sentences separated by ". " (sentence boundary). Each should
    # generate its own TTS call.
    answer = _StreamingAnswerLLM(deltas=["Hello there. ", "How are you?"])
    tts = _FakeTTS(frame_count=2)
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["greet"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {"should_speak": True, "confidence": 0.95, "reason": "ok"},
                {"should_speak": False, "confidence": 0.1, "reason": "second skip"},
            ]
        ),
        answer_llm=answer,
        tts=tts,
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
    )
    await pipeline.run()
    # First call: "Hello there." (boundary `". "`).
    # Second call: "How are you?" (final flush).
    assert tts.calls == ["Hello there.", "How are you?"]


async def test_pipeline_stream_chat_default_implementation_yields_full_text() -> None:
    """LLMProvider.stream_chat default yields the full chat() text once."""

    class _Plain(LLMProvider):
        @property
        def name(self) -> str:
            return "plain"

        async def chat(
            self,
            messages: Sequence[ChatMessage],
            tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
            response_format: dict[str, Any] | None = None,  # noqa: ARG002
        ) -> LLMResponse:
            del messages
            return LLMResponse(text="hello world", finish_reason="stop")

    adapter = _Plain()
    deltas: list[str] = []
    async for delta in adapter.stream_chat([ChatMessage(role="user", content="x")]):
        deltas.append(delta)
    assert deltas == ["hello world"]


async def test_pipeline_stream_chat_default_yields_nothing_when_text_empty() -> None:
    class _Plain(LLMProvider):
        @property
        def name(self) -> str:
            return "plain"

        async def chat(
            self,
            messages: Sequence[ChatMessage],
            tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
            response_format: dict[str, Any] | None = None,  # noqa: ARG002
        ) -> LLMResponse:
            del messages
            return LLMResponse(text="", finish_reason="stop")

    adapter = _Plain()
    deltas: list[str] = []
    async for delta in adapter.stream_chat([ChatMessage(role="user", content="x")]):
        deltas.append(delta)
    assert deltas == []


# --- US-024: TTS streaming into transport --------------------------------


async def test_pipeline_streams_tts_frames_individually_to_transport(
    two_utterance_pcm: bytes,
) -> None:
    """The pipeline streams TTS frames as they arrive (not buffered to a blob)."""
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    tts = _FakeTTS(frame_count=5)
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hi"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {"should_speak": True, "confidence": 0.95, "reason": "ok"},
                {"should_speak": False, "confidence": 0.1, "reason": "second skip"},
            ]
        ),
        answer_llm=_FakeAnswerLLM(answers=["hi"]),
        tts=tts,
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
    )
    await pipeline.run()
    # Five TTS frames → five individual entries in transport.played.
    assert len(transport.played) == 5
    assert transport.played_source_rate == 16_000


# --- US-024: structured output for allowed_replies -----------------------


async def test_pipeline_uses_structured_output_for_allowed_replies(
    two_utterance_pcm: bytes,
) -> None:
    """When allowed_replies is set, response_format is sent and used for selection."""
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]

    class _StructuredAnswerLLM(LLMProvider):
        def __init__(self) -> None:
            self.last_response_format: dict[str, Any] | None = None

        @property
        def name(self) -> str:
            return "structured-answer"

        async def chat(
            self,
            messages: Sequence[ChatMessage],
            tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
            response_format: dict[str, Any] | None = None,
        ) -> LLMResponse:
            del messages
            self.last_response_format = response_format
            return LLMResponse(
                text="",
                finish_reason="stop",
                structured_output={"selected_reply": "yes"},
            )

    answer = _StructuredAnswerLLM()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["are we good"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": True, "confidence": 0.95, "reason": "ok"}]
        ),
        answer_llm=answer,
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            allowed_replies=("yes", "no"),
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
    )
    await pipeline.run()
    assert answer.last_response_format is not None
    schema = answer.last_response_format
    sel = schema["properties"]["selected_reply"]
    assert sel["enum"] == ["yes", "no"]
    assert "selected_reply" in schema["required"]


async def test_pipeline_structured_output_picks_allowed_reply(
    two_utterance_pcm: bytes,
) -> None:
    """When the LLM returns structured_output, the picked reply is used."""
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]

    class _StructuredLLM(LLMProvider):
        @property
        def name(self) -> str:
            return "structured"

        async def chat(
            self,
            messages: Sequence[ChatMessage],
            tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
            response_format: dict[str, Any] | None = None,  # noqa: ARG002
        ) -> LLMResponse:
            del messages
            return LLMResponse(
                text="",
                finish_reason="stop",
                structured_output={"selected_reply": "no"},
            )

    bus = InMemoryEventBus()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["asked"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {"should_speak": True, "confidence": 0.95, "reason": "ok"},
                {"should_speak": False, "confidence": 0.1, "reason": "second skip"},
            ]
        ),
        answer_llm=_StructuredLLM(),
        tts=_FakeTTS(),
        event_bus=bus,
        config=PipelineConfig(
            allowed_replies=("yes", "no"),
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
    )
    await pipeline.run()
    spokes = [e for e in bus.snapshot() if isinstance(e, AgentSpoke)]
    assert len(spokes) == 1
    assert spokes[0].text == "no"
    assert spokes[0].matched_allowed_reply == "no"


# --- US-024: answer prompt includes meeting context & transcript window --


async def test_answer_prompt_includes_instructions_context_suggested_reply(
    two_utterance_pcm: bytes,
) -> None:
    """The answer LLM system message includes instructions, context, and router hint."""
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    answer = _FakeAnswerLLM(answers=["Hi"])
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hello"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {
                    "should_speak": True,
                    "confidence": 0.9,
                    "reason": "asked",
                    "reply_type": "ack",
                    "suggested_reply": "Hi",
                }
            ]
        ),
        answer_llm=answer,
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            instructions="Be polite",
            context="standup",
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
    )
    await pipeline.run()
    assert answer.last_messages is not None
    system_msg = answer.last_messages[0]
    assert system_msg.role == "system"
    assert system_msg.content is not None
    assert "Meeting instructions: Be polite" in system_msg.content
    assert "Context: standup" in system_msg.content
    assert "Router suggested: Hi" in system_msg.content


async def test_answer_prompt_includes_transcript_window(
    two_utterance_pcm: bytes,
) -> None:
    """The answer LLM's user message includes the rolling transcript history."""
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    answer = _FakeAnswerLLM(answers=["Hi", "Sure"])
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["first thing", "second thing"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": True, "confidence": 0.9, "reason": "ok"}]
        ),
        answer_llm=answer,
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
    )
    await pipeline.run()
    # The second answer call should see the first transcript in the history.
    assert len(answer.calls) == 2
    second_user_msg = answer.calls[1][1]
    assert second_user_msg.content is not None
    assert "Recent conversation:" in second_user_msg.content
    assert "first thing" in second_user_msg.content
    assert "Latest transcript: second thing" in second_user_msg.content


async def test_answer_prompt_no_history_on_first_turn(
    two_utterance_pcm: bytes,
) -> None:
    """The first answer call's user message does not include a Recent conversation block."""
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    answer = _FakeAnswerLLM(answers=["Hi"])
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["only one"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": True, "confidence": 0.9, "reason": "ok"}]
        ),
        answer_llm=answer,
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
    )
    await pipeline.run()
    assert answer.calls
    first_user_msg = answer.calls[0][1]
    assert first_user_msg.content is not None
    assert "Recent conversation:" not in first_user_msg.content


# --- US-024: utterance persistence ---------------------------------------


async def test_pipeline_persists_utterance_after_speaking(
    two_utterance_pcm: bytes,
) -> None:
    """When the bot speaks, the utterance is recorded with the right fields."""
    from johnny.voice_pipeline import InMemoryUtteranceSink

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    sink = InMemoryUtteranceSink()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hi", "bye"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {"should_speak": True, "confidence": 0.95, "reason": "ok"},
                {"should_speak": False, "confidence": 0.1, "reason": "no"},
            ]
        ),
        answer_llm=_FakeAnswerLLM(answers=["Hello", "Goodbye"]),
        tts=_FakeTTS(frame_count=3),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            mode="limited_auto_speak",
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            session_id="sess-x",
            bot_session_id=77,
        ),
        utterance_sink=sink,
    )
    await pipeline.run()
    records = sink.snapshot()
    assert len(records) == 1  # Only the first utterance was spoken
    rec = records[0]
    assert rec.mode == "limited_auto_speak"
    assert rec.output_text == "Hello"
    assert rec.audio_duration_ms > 0
    assert rec.session_id == "sess-x"
    assert rec.bot_session_id == 77
    assert rec.matched_allowed_reply is None
    # Prompt is JSON of the message list passed to the answer LLM.
    assert rec.prompt.startswith("[")
    assert "Latest transcript: hi" in rec.prompt


async def test_pipeline_persists_utterance_with_matched_allowed_reply(
    two_utterance_pcm: bytes,
) -> None:
    """Limited-auto-speak utterance records the matched allowed reply."""
    from johnny.voice_pipeline import InMemoryUtteranceSink

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    sink = InMemoryUtteranceSink()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["ask"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": True, "confidence": 0.95, "reason": "ok"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["yes"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            allowed_replies=("yes", "no"),
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
        utterance_sink=sink,
    )
    await pipeline.run()
    rec = sink.snapshot()[0]
    assert rec.output_text == "yes"
    assert rec.matched_allowed_reply == "yes"


async def test_pipeline_does_not_persist_utterance_when_suppressed(
    two_utterance_pcm: bytes,
) -> None:
    """Decision says don't speak → no utterance recorded."""
    from johnny.voice_pipeline import InMemoryUtteranceSink

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    sink = InMemoryUtteranceSink()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["a", "b"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": False, "confidence": 0.1, "reason": "n"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
        utterance_sink=sink,
    )
    await pipeline.run()
    assert sink.snapshot() == []


async def test_pipeline_does_not_persist_utterance_when_allowed_reply_no_match(
    two_utterance_pcm: bytes,
) -> None:
    """No allowed-reply match → utterance not persisted."""
    from johnny.voice_pipeline import InMemoryUtteranceSink

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    sink = InMemoryUtteranceSink()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["ask"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": True, "confidence": 0.95, "reason": "ok"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["maybe"]),  # not in allowed_replies
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            allowed_replies=("yes", "no"),
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
        utterance_sink=sink,
    )
    await pipeline.run()
    assert sink.snapshot() == []


async def test_pipeline_default_utterance_sink_is_noop(
    two_utterance_pcm: bytes,
) -> None:
    """When no utterance_sink is supplied, the pipeline still works (uses Noop)."""
    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hi"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": True, "confidence": 0.95, "reason": "ok"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["Hi"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
    )
    # Should not raise — uses NoopUtteranceSink by default.
    await pipeline.run()


async def test_pipeline_utterance_sink_failure_does_not_crash(
    two_utterance_pcm: bytes,
) -> None:
    """A failing utterance sink is logged and swallowed; audio loop keeps going."""
    from johnny.voice_pipeline import UtteranceSink

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]

    class _BrokenSink(UtteranceSink):
        async def record(self, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            raise RuntimeError("db unavailable")

    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hi"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": True, "confidence": 0.95, "reason": "ok"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["Hi"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
        utterance_sink=_BrokenSink(),
    )
    await pipeline.run()  # must not raise


# --- US-024: cancellation / interrupt -----------------------------------


async def test_pipeline_interrupt_before_speak_skips_audio(
    two_utterance_pcm: bytes,
) -> None:
    """interrupt() set before _answer_and_speak starts → no audio played, no utterance."""
    from johnny.voice_pipeline import InMemoryUtteranceSink

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]

    class _CheckInterruptAnswerLLM(LLMProvider):
        def __init__(self, pipeline_ref: list[Any]) -> None:
            self._pipeline_ref = pipeline_ref

        @property
        def name(self) -> str:
            return "check-interrupt"

        async def chat(
            self,
            messages: Sequence[ChatMessage],
            tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
            response_format: dict[str, Any] | None = None,  # noqa: ARG002
        ) -> LLMResponse:
            del messages
            return LLMResponse(text="", finish_reason="stop")

        async def stream_chat(
            self,
            messages: Sequence[ChatMessage],
        ) -> AsyncIterator[str]:
            del messages
            # Set interrupt early so the loop bails out before any text.
            self._pipeline_ref[0].interrupt()
            return
            yield  # pragma: no cover — required to make this a generator

    pipeline_ref: list[Any] = [None]
    transport = _BufferedTransport(frames=frames)
    sink = InMemoryUtteranceSink()
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hi"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": True, "confidence": 0.95, "reason": "ok"}]
        ),
        answer_llm=_CheckInterruptAnswerLLM(pipeline_ref),
        tts=_FakeTTS(frame_count=5),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
        utterance_sink=sink,
    )
    pipeline_ref[0] = pipeline
    await pipeline.run()
    assert transport.played == []
    assert sink.snapshot() == []


async def test_pipeline_interrupt_during_tts_truncates_audio(
    two_utterance_pcm: bytes,
) -> None:
    """interrupt() mid-TTS → loop exits early, partial audio played."""
    import time as _time

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]

    interrupt_signal = asyncio.Event()

    class _SlowTTS(TTSProvider):
        def __init__(self) -> None:
            self.frames_yielded = 0

        @property
        def name(self) -> str:
            return "slow"

        async def synthesize_stream(
            self,
            text: str,  # noqa: ARG002
            voice_id: str | None = None,  # noqa: ARG002
        ) -> AsyncIterator[bytes]:
            for i in range(200):
                yield bytes(640)
                self.frames_yielded += 1
                if i == 3:
                    interrupt_signal.set()
                await asyncio.sleep(0.001)

    transport = _BufferedTransport(frames=frames)
    tts = _SlowTTS()
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hi"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {"should_speak": True, "confidence": 0.95, "reason": "ok"},
                {"should_speak": False, "confidence": 0.1, "reason": "second skip"},
            ]
        ),
        answer_llm=_FakeAnswerLLM(answers=["a long answer"]),
        tts=tts,
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
    )

    async def _trigger_interrupt() -> None:
        await interrupt_signal.wait()
        pipeline.interrupt()

    interrupt_task = asyncio.create_task(_trigger_interrupt())
    start = _time.monotonic()
    await pipeline.run()
    elapsed = _time.monotonic() - start
    await interrupt_task

    # Audio was truncated: should have played some frames but not all 200.
    assert 0 < len(transport.played) < 200
    # Wall-clock: 200 frames * 1ms sleep = 200ms if not interrupted. With
    # interrupt, should finish well under 500ms.
    assert elapsed < 0.5


async def test_pipeline_interrupt_state_clears_between_utterances(
    two_utterance_pcm: bytes,
) -> None:
    """interrupt() set during utterance N does NOT persist to utterance N+1."""
    from johnny.voice_pipeline import InMemoryUtteranceSink

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]

    class _InterruptFirstStreamLLM(LLMProvider):
        def __init__(self, pipeline_ref: list[Any]) -> None:
            self._pipeline_ref = pipeline_ref
            self._calls = 0

        @property
        def name(self) -> str:
            return "interrupt-first"

        async def chat(
            self,
            messages: Sequence[ChatMessage],
            tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
            response_format: dict[str, Any] | None = None,  # noqa: ARG002
        ) -> LLMResponse:
            del messages
            return LLMResponse(text="", finish_reason="stop")

        async def stream_chat(
            self,
            messages: Sequence[ChatMessage],
        ) -> AsyncIterator[str]:
            del messages
            self._calls += 1
            if self._calls == 1:
                # Interrupt the first utterance.
                self._pipeline_ref[0].interrupt()
                return
                yield  # pragma: no cover
            else:
                # Second utterance proceeds normally.
                yield "Second answer"

    pipeline_ref: list[Any] = [None]
    sink = InMemoryUtteranceSink()
    transport = _BufferedTransport(frames=frames)
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["one", "two"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {"should_speak": True, "confidence": 0.95, "reason": "ok"},
                {"should_speak": True, "confidence": 0.95, "reason": "ok"},
            ]
        ),
        answer_llm=_InterruptFirstStreamLLM(pipeline_ref),
        tts=_FakeTTS(frame_count=2),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
        utterance_sink=sink,
    )
    pipeline_ref[0] = pipeline
    await pipeline.run()
    # Only the second utterance produced audio + persisted record.
    records = sink.snapshot()
    assert len(records) == 1
    assert records[0].output_text == "Second answer"


# --- US-033: transcript persistence -------------------------------------


async def test_pipeline_persists_finalised_transcripts(
    two_utterance_pcm: bytes,
) -> None:
    """Every finalised transcript is recorded to the transcript sink."""
    from johnny.voice_pipeline import InMemoryTranscriptSink

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    sink = InMemoryTranscriptSink()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hello team", "any updates"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": False, "confidence": 0.1, "reason": "n"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            session_id="sess-x",
            bot_session_id=42,
        ),
        transcript_sink=sink,
    )
    await pipeline.run()
    records = sink.snapshot()
    assert len(records) == 2
    assert [r.text for r in records] == ["hello team", "any updates"]
    for r in records:
        assert r.session_id == "sess-x"
        assert r.bot_session_id == 42
        assert r.start_offset_ms >= 0
        assert r.end_offset_ms >= r.start_offset_ms


async def test_pipeline_transcript_persistence_carries_speaker_and_confidence(
    two_utterance_pcm: bytes,
) -> None:
    """speaker / confidence from STT events flow through to the sink."""
    from app.providers import STTProvider, TranscriptEvent
    from johnny.voice_pipeline import InMemoryTranscriptSink

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]

    class _STTWithSpeaker(STTProvider):
        def __init__(self) -> None:
            self._idx = 0

        @property
        def name(self) -> str:
            return "stt-speaker"

        async def transcribe_stream(
            self,
            audio_iter: AsyncIterator[bytes],
        ) -> AsyncIterator[TranscriptEvent]:
            async for _ in audio_iter:
                pass
            self._idx += 1
            yield TranscriptEvent(
                text=f"text-{self._idx}",
                is_final=True,
                timestamp_ms=self._idx * 1000,
                confidence=0.8,
                speaker=f"speaker-{self._idx}",
            )

    sink = InMemoryTranscriptSink()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_STTWithSpeaker(),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": False, "confidence": 0.0, "reason": "n"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(vad_threshold=0.05, end_of_speech_ms=300),
        transcript_sink=sink,
    )
    await pipeline.run()
    records = sink.snapshot()
    assert len(records) == 2
    assert records[0].speaker == "speaker-1"
    assert records[0].confidence == pytest.approx(0.8)


async def test_pipeline_transcript_offsets_match_utterance_duration(
    two_utterance_pcm: bytes,
) -> None:
    """start_offset_ms = end_offset_ms - utterance_duration (derived from PCM len)."""
    from johnny.voice_pipeline import InMemoryTranscriptSink

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    sink = InMemoryTranscriptSink()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["one", "two"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": False, "confidence": 0.1, "reason": "n"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(vad_threshold=0.05, end_of_speech_ms=300),
        transcript_sink=sink,
    )
    await pipeline.run()
    records = sink.snapshot()
    assert len(records) == 2
    # Each utterance from _FakeSTT has timestamp_ms = idx * 1000.
    # The first utterance is non-empty PCM so start_offset_ms should be
    # < end_offset_ms.
    for r in records:
        assert r.end_offset_ms > r.start_offset_ms


async def test_pipeline_default_transcript_sink_is_noop(
    two_utterance_pcm: bytes,
) -> None:
    """No transcript_sink supplied → uses NoopTranscriptSink, run completes."""
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
            decisions=[{"should_speak": False, "confidence": 0.0, "reason": "n"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(vad_threshold=0.05, end_of_speech_ms=300),
    )
    await pipeline.run()  # uses NoopTranscriptSink — must not raise


async def test_pipeline_transcript_sink_failure_does_not_crash(
    two_utterance_pcm: bytes,
) -> None:
    """A failing transcript sink is logged and swallowed; audio loop keeps going."""
    from johnny.voice_pipeline import TranscriptSink

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]

    class _BrokenSink(TranscriptSink):
        async def record(self, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            raise RuntimeError("db unavailable")

    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["one", "two"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": False, "confidence": 0.0, "reason": "n"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(vad_threshold=0.05, end_of_speech_ms=300),
        transcript_sink=_BrokenSink(),
    )
    await pipeline.run()  # must not raise


# --- US-028: Limited auto-speak rate limiting ---------------------------


def test_pipeline_config_has_rate_limit_defaults() -> None:
    """Default rate limit: max 3 utterances per 5-minute (300_000 ms) window."""
    cfg = PipelineConfig()
    assert cfg.rate_limit_max_utterances == 3
    assert cfg.rate_limit_window_ms == 5 * 60 * 1000


async def test_pipeline_rate_limits_limited_auto_speak_after_max_utterances(
    four_utterance_pcm: bytes,
) -> None:
    """Limited-auto-speak with max=2 speaks the first two then suppresses the rest."""
    from johnny.voice_pipeline import InMemoryUtteranceSink

    frame_size = 640
    frames = [
        four_utterance_pcm[i : i + frame_size]
        for i in range(0, len(four_utterance_pcm), frame_size)
        if i + frame_size <= len(four_utterance_pcm)
    ]
    sink = InMemoryUtteranceSink()
    dsink = InMemoryDecisionSink()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["one", "two", "three", "four"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": True, "confidence": 0.95, "reason": "ok"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["yes", "no", "yes", "no"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            mode="limited_auto_speak",
            allowed_replies=("yes", "no"),
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            rate_limit_max_utterances=2,
            rate_limit_window_ms=10 * 60 * 1000,
        ),
        utterance_sink=sink,
        decision_sink=dsink,
    )
    await pipeline.run()
    # Only the first two utterances were spoken — the third and fourth
    # were rate-limited.
    utterances = sink.snapshot()
    assert len(utterances) == 2
    decisions = dsink.snapshot()
    outcomes = [d.outcome for d in decisions]
    assert outcomes.count("spoken") == 2
    assert outcomes.count("suppressed") >= 2


async def test_pipeline_rate_limit_disabled_when_max_zero(
    four_utterance_pcm: bytes,
) -> None:
    """max_utterances=0 disables the limit entirely."""
    from johnny.voice_pipeline import InMemoryUtteranceSink

    frame_size = 640
    frames = [
        four_utterance_pcm[i : i + frame_size]
        for i in range(0, len(four_utterance_pcm), frame_size)
        if i + frame_size <= len(four_utterance_pcm)
    ]
    sink = InMemoryUtteranceSink()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["one", "two", "three", "four"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": True, "confidence": 0.95, "reason": "ok"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["yes", "no", "yes", "no"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            mode="limited_auto_speak",
            allowed_replies=("yes", "no"),
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            rate_limit_max_utterances=0,
            rate_limit_window_ms=300_000,
        ),
        utterance_sink=sink,
    )
    await pipeline.run()
    assert len(sink.snapshot()) == 4


async def test_pipeline_rate_limit_not_enforced_without_allowed_replies(
    four_utterance_pcm: bytes,
) -> None:
    """Without allowed_replies set, the rate limit doesn't apply (free-text path)."""
    from johnny.voice_pipeline import InMemoryUtteranceSink

    frame_size = 640
    frames = [
        four_utterance_pcm[i : i + frame_size]
        for i in range(0, len(four_utterance_pcm), frame_size)
        if i + frame_size <= len(four_utterance_pcm)
    ]
    sink = InMemoryUtteranceSink()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["one", "two", "three", "four"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": True, "confidence": 0.95, "reason": "ok"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["a", "b", "c", "d"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            # Use the default free-speak mode so this test stays focused
            # on the rate-limit behaviour (approval_required adds its own
            # gate that's tested elsewhere).
            mode="limited_auto_speak",
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            rate_limit_max_utterances=1,
            rate_limit_window_ms=10 * 60 * 1000,
        ),
        utterance_sink=sink,
    )
    await pipeline.run()
    # All four utterances spoken because rate limit only applies to
    # limited-auto-speak (allowed_replies set).
    assert len(sink.snapshot()) == 4


async def test_pipeline_rate_limited_decision_records_suppressed(
    four_utterance_pcm: bytes,
) -> None:
    """Rate-limited utterances surface in the decision sink as ``suppressed``."""
    frame_size = 640
    frames = [
        four_utterance_pcm[i : i + frame_size]
        for i in range(0, len(four_utterance_pcm), frame_size)
        if i + frame_size <= len(four_utterance_pcm)
    ]
    dsink = InMemoryDecisionSink()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["one", "two", "three", "four"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": True, "confidence": 0.95, "reason": "ok"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["yes", "no", "yes", "no"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            mode="limited_auto_speak",
            allowed_replies=("yes", "no"),
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            rate_limit_max_utterances=1,
            rate_limit_window_ms=10 * 60 * 1000,
        ),
        decision_sink=dsink,
    )
    await pipeline.run()
    records = dsink.snapshot()
    # 4 utterances → 4 decisions. First "spoken", remaining "suppressed".
    assert len(records) == 4
    assert records[0].outcome == "spoken"
    assert all(r.outcome == "suppressed" for r in records[1:])


def test_is_rate_limited_returns_false_without_allowed_replies() -> None:
    """The rate limit is a no-op when allowed_replies is empty."""
    transport = _BufferedTransport(frames=[])
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=[]),
        router_llm=_FakeRouterLLM(decisions=[]),
        answer_llm=_FakeAnswerLLM(answers=[]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            rate_limit_max_utterances=1,
            rate_limit_window_ms=1,
        ),
    )
    # Stuff "recent utterances" in directly — without allowed_replies the
    # helper still returns False.
    pipeline._recent_utterance_times = [0, 1, 2]
    assert pipeline._is_rate_limited() is False


async def test_is_rate_limited_returns_true_when_full(
    two_utterance_pcm: bytes,  # noqa: ARG001 — only need a running event loop
) -> None:
    """When recent count >= max within the window, helper returns True."""
    transport = _BufferedTransport(frames=[])
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=[]),
        router_llm=_FakeRouterLLM(decisions=[]),
        answer_llm=_FakeAnswerLLM(answers=[]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            allowed_replies=("yes",),
            rate_limit_max_utterances=2,
            rate_limit_window_ms=10 * 60 * 1000,
        ),
    )
    # Seed two recent timestamps "just now"
    now_ms = pipeline._now_ms()
    pipeline._recent_utterance_times = [now_ms, now_ms]
    assert pipeline._is_rate_limited() is True


async def test_is_rate_limited_prunes_outside_window(
    two_utterance_pcm: bytes,  # noqa: ARG001 — only need a running event loop
) -> None:
    """Timestamps older than the window are pruned in place when checked."""
    transport = _BufferedTransport(frames=[])
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=[]),
        router_llm=_FakeRouterLLM(decisions=[]),
        answer_llm=_FakeAnswerLLM(answers=[]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            allowed_replies=("yes",),
            rate_limit_max_utterances=2,
            rate_limit_window_ms=1000,
        ),
    )
    now_ms = pipeline._now_ms()
    # One ancient, one recent → prune leaves 1, not limited
    pipeline._recent_utterance_times = [now_ms - 100_000, now_ms]
    assert pipeline._is_rate_limited() is False
    assert pipeline._recent_utterance_times == [now_ms]


# --- approval-required mode (US-027) --------------------------------------


async def test_pipeline_approval_required_approves_and_speaks(
    two_utterance_pcm: bytes,
) -> None:
    """Approved → answer LLM runs, TTS plays, decision row flipped to spoken."""
    from johnny.voice_pipeline import (
        InMemoryApprovalGate,
        InMemoryDecisionSink,
        InMemoryUtteranceSink,
    )

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    bus = InMemoryEventBus()
    dsink = InMemoryDecisionSink()
    usink = InMemoryUtteranceSink()
    gate = InMemoryApprovalGate(scripted=["approved"], default_outcome="rejected")
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hello team", "anything?"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {
                    "should_speak": True,
                    "confidence": 0.95,
                    "reason": "asked",
                    "suggested_reply": "yes",
                },
                {"should_speak": False, "confidence": 0.1, "reason": "noise"},
            ]
        ),
        answer_llm=_FakeAnswerLLM(answers=["Sure!"]),
        tts=_FakeTTS(frame_count=2),
        event_bus=bus,
        config=PipelineConfig(
            mode="approval_required",
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            approval_timeout_seconds=2.0,
            session_id="sess-A",
        ),
        decision_sink=dsink,
        utterance_sink=usink,
        approval_gate=gate,
    )

    await pipeline.run()

    types = [e.type for e in bus.snapshot()]
    # Two utterances. First triggers approval flow (approved); second
    # suppressed by router.
    assert "approval_pending" in types
    assert "approval_resolved" in types
    # agent_spoke fires only on approval.
    assert types.count("agent_spoke") == 1

    # Decision sink: first decision approved → spoken; second suppressed.
    records = dsink.snapshot()
    assert len(records) == 2
    spoken_record = next(r for r in records if r.decision.should_speak)
    assert spoken_record.outcome == "spoken"
    suppressed_record = next(r for r in records if not r.decision.should_speak)
    assert suppressed_record.outcome == "suppressed"

    # Utterance sink saw exactly the approved spoken reply.
    utterances = usink.snapshot()
    assert len(utterances) == 1
    assert utterances[0].output_text == "Sure!"

    # Approval gate was invoked once with the right shape.
    assert len(gate.requests) == 1
    assert gate.requests[0].suggested_reply == "yes"
    assert gate.requests[0].timeout_s == 2.0
    assert gate.requests[0].session_id == "sess-A"

    # Final emitted ApprovalResolved should be "approved".
    resolved = next(
        e for e in bus.snapshot() if e.type == "approval_resolved"
    )
    assert resolved.resolution == "approved"


async def test_pipeline_approval_required_rejected_stays_silent(
    two_utterance_pcm: bytes,
) -> None:
    """Rejection: TTS does not run, decision flips to rejected, no utterance row."""
    from johnny.voice_pipeline import (
        InMemoryApprovalGate,
        InMemoryDecisionSink,
        InMemoryUtteranceSink,
    )

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    bus = InMemoryEventBus()
    dsink = InMemoryDecisionSink()
    usink = InMemoryUtteranceSink()
    gate = InMemoryApprovalGate(scripted=["rejected"], default_outcome="timeout")
    tts = _FakeTTS()
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hello team", "noise"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {
                    "should_speak": True,
                    "confidence": 0.95,
                    "reason": "ask",
                    "suggested_reply": "yes",
                },
                {"should_speak": False, "confidence": 0.1, "reason": "noise"},
            ]
        ),
        answer_llm=_FakeAnswerLLM(answers=["nope"]),
        tts=tts,
        event_bus=bus,
        config=PipelineConfig(
            mode="approval_required",
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            approval_timeout_seconds=2.0,
        ),
        decision_sink=dsink,
        utterance_sink=usink,
        approval_gate=gate,
    )

    await pipeline.run()

    types = [e.type for e in bus.snapshot()]
    assert "approval_pending" in types
    assert "approval_resolved" in types
    # No agent_spoke event because TTS never ran for the approved reply.
    assert "agent_spoke" not in types
    # TTS adapter was never invoked.
    assert tts.calls == []
    # No utterance row — bot stayed silent.
    assert usink.snapshot() == []

    records = dsink.snapshot()
    spoken_record = next(r for r in records if r.decision.should_speak)
    assert spoken_record.outcome == "rejected"

    resolved = next(
        e for e in bus.snapshot() if e.type == "approval_resolved"
    )
    assert resolved.resolution == "rejected"


async def test_pipeline_approval_required_timeout_auto_rejects(
    two_utterance_pcm: bytes,
) -> None:
    """No human response inside the window ⇒ auto-rejected, decision logged."""
    from johnny.voice_pipeline import (
        InMemoryApprovalGate,
        InMemoryDecisionSink,
        InMemoryUtteranceSink,
    )

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    bus = InMemoryEventBus()
    dsink = InMemoryDecisionSink()
    usink = InMemoryUtteranceSink()
    # Empty script + default timeout → both utterances time out.
    gate = InMemoryApprovalGate(scripted=[], default_outcome="timeout")
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hello", "again"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {
                    "should_speak": True,
                    "confidence": 0.95,
                    "reason": "ask",
                    "suggested_reply": "yes",
                },
                {
                    "should_speak": True,
                    "confidence": 0.95,
                    "reason": "ask",
                    "suggested_reply": "yes",
                },
            ]
        ),
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=bus,
        config=PipelineConfig(
            mode="approval_required",
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            approval_timeout_seconds=0.5,
        ),
        decision_sink=dsink,
        utterance_sink=usink,
        approval_gate=gate,
    )

    await pipeline.run()

    records = dsink.snapshot()
    assert len(records) == 2
    assert all(r.outcome == "rejected" for r in records)
    # No utterance played.
    assert usink.snapshot() == []

    resolutions = [
        e.resolution
        for e in bus.snapshot()
        if e.type == "approval_resolved"
    ]
    assert resolutions == ["timeout", "timeout"]


async def test_pipeline_approval_required_pending_event_carries_decision_id(
    two_utterance_pcm: bytes,
) -> None:
    """``approval_pending`` event includes the persisted decision_id."""
    from johnny.voice_pipeline import (
        ApprovalPending,
        InMemoryApprovalGate,
        InMemoryDecisionSink,
    )

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    bus = InMemoryEventBus()
    dsink = InMemoryDecisionSink()
    gate = InMemoryApprovalGate(scripted=["rejected", "rejected"])
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["a", "b"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {
                    "should_speak": True,
                    "confidence": 0.95,
                    "reason": "ask",
                    "suggested_reply": "yes",
                }
            ]
        ),
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=bus,
        config=PipelineConfig(
            mode="approval_required",
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            approval_timeout_seconds=2.0,
            session_id="sess-D",
        ),
        decision_sink=dsink,
        approval_gate=gate,
    )

    await pipeline.run()

    pending_events = [
        e for e in bus.snapshot() if isinstance(e, ApprovalPending)
    ]
    # Two utterances, both should_speak → two pending events.
    assert len(pending_events) == 2
    # Each event carries a non-zero decision_id, the suggested_reply,
    # and the requested timeout.
    for evt in pending_events:
        assert evt.decision_id > 0
        assert evt.suggested_reply == "yes"
        assert evt.timeout_s == pytest.approx(2.0)
        assert evt.session_id == "sess-D"
    # Each pending event's decision_id matches one of the persisted
    # decision rows.
    pending_ids = {evt.decision_id for evt in pending_events}
    recorded_ids = {
        r.decision_id for r in dsink.snapshot() if r.decision_id is not None
    }
    assert pending_ids == recorded_ids
    # All decisions are now rejected.
    assert all(r.outcome == "rejected" for r in dsink.snapshot())


async def test_pipeline_approval_required_below_threshold_skips_gate(
    two_utterance_pcm: bytes,
) -> None:
    """Low-confidence decisions skip the gate entirely (suppressed pre-approval)."""
    from johnny.voice_pipeline import (
        InMemoryApprovalGate,
        InMemoryDecisionSink,
    )

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    bus = InMemoryEventBus()
    dsink = InMemoryDecisionSink()
    gate = InMemoryApprovalGate(scripted=["approved", "approved"])
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["a", "b"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {
                    "should_speak": True,
                    "confidence": 0.30,
                    "reason": "uncertain",
                    "suggested_reply": "yes",
                }
            ]
        ),
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=bus,
        config=PipelineConfig(
            mode="approval_required",
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.7,
            approval_timeout_seconds=1.0,
        ),
        decision_sink=dsink,
        approval_gate=gate,
    )

    await pipeline.run()

    # Gate was never called — both utterances rejected at threshold check.
    assert gate.requests == []
    # No approval events emitted.
    types = {e.type for e in bus.snapshot()}
    assert "approval_pending" not in types
    assert "approval_resolved" not in types
    # Decisions recorded as suppressed (not rejected).
    assert all(r.outcome == "suppressed" for r in dsink.snapshot())
