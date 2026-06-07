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
    PipelineTiming,
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
    # Activity-log timings (Johnny-ckz.7) share the same event bus but
    # are observability; filter them out before asserting the
    # transcript/decision/utterance contract.
    types = [e.type for e in events if e.type != "pipeline_timing"]
    # Transcription and response run as concurrent tasks (Johnny-har),
    # so the two transcripts may both publish before the response loop
    # catches up. The contract is: two transcripts, two router
    # decisions, one agent_spoke (second utterance is suppressed).
    assert sorted(types) == sorted([
        "transcript_finalized",
        "router_decision_made",
        "agent_spoke",
        "transcript_finalized",
        "router_decision_made",
    ])

    transcripts = [e for e in events if isinstance(e, TranscriptFinalized)]
    assert [t.text for t in transcripts] == ["hello team", "any updates"]
    assert all(t.session_id == "test-session" for t in transcripts)

    decisions = [e for e in events if isinstance(e, RouterDecisionMade)]
    assert len(decisions) == 2
    r1, r2 = decisions
    assert r1.should_speak is True
    assert r1.confidence == pytest.approx(0.9)
    assert r1.suggested_reply == "Hi"
    assert r2.should_speak is False

    spoke = [e for e in events if isinstance(e, AgentSpoke)]
    assert len(spoke) == 1
    assert spoke[0].text == "Hi"
    assert spoke[0].audio_duration_ms > 0

    assert stt.calls == 2
    assert tts.calls == ["Hi"]
    # Streaming: each TTS frame is played individually (3 frames for the first
    # utterance, 0 for the suppressed second). Total bytes match the expected
    # audio duration captured on the AgentSpoke event.
    assert len(transport.played) == 3
    assert sum(len(f) for f in transport.played) > 0
    assert transport.played_source_rate == 16_000

    # Activity-log timings (Johnny-ckz.7): every applicable stage in
    # the speaking turn emits a row, the suppressed turn emits stt +
    # router_llm only.
    timings = [e for e in events if isinstance(e, PipelineTiming)]
    stages_by_turn: dict[int, list[str]] = {}
    for t in timings:
        stages_by_turn.setdefault(t.turn_id, []).append(t.stage)
    # Two turns.
    assert set(stages_by_turn.keys()) == {1, 2}
    # First turn: stt, router_llm, answer_llm, tts, end_to_end.
    assert "stt" in stages_by_turn[1]
    assert "router_llm" in stages_by_turn[1]
    assert "answer_llm" in stages_by_turn[1]
    assert "tts" in stages_by_turn[1]
    assert "end_to_end" in stages_by_turn[1]
    # Second turn: suppressed before answer LLM ran, so only stt +
    # router_llm appear. No end_to_end (the bot never spoke).
    assert "stt" in stages_by_turn[2]
    assert "router_llm" in stages_by_turn[2]
    assert "answer_llm" not in stages_by_turn[2]
    assert "tts" not in stages_by_turn[2]
    assert "end_to_end" not in stages_by_turn[2]
    # Every persisted timing carries the session id.
    assert all(t.session_id == "test-session" for t in timings)
    # Durations are non-negative ints.
    assert all(t.duration_ms >= 0 for t in timings)
    # The end-to-end row carries no provider (it's an orchestration
    # measurement, not a single provider call).
    end_to_end = [t for t in timings if t.stage == "end_to_end"]
    assert len(end_to_end) == 1
    assert end_to_end[0].provider_name is None


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
    types = [e.type for e in bus.snapshot() if e.type != "pipeline_timing"]
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


# --- Johnny-ckz.14: STT noise gate before router ---------------------------


@pytest.mark.parametrize(
    "noise_text",
    [
        "you",
        "Uh.",
        "  hm  ",
        "...HMM!",
        "thank you",
        "Thanks for watching!",
        "............",
        "...",
        "?!?!?",
        "  …  ",
        "a",  # below the 2-char floor
    ],
)
async def test_noise_gate_drops_stoplist_and_punctuation_and_short(
    two_utterance_pcm: bytes, noise_text: str
) -> None:
    """STT artifacts ('you', 'uh', '...') are filtered before the router (Johnny-ckz.14).

    The router must never see these — the bug they reproduce is the bot
    'replying' to a ghost turn because Whisper emitted a single token
    during silence. Every flavour (single filler, punctuation-only,
    sub-floor length, multi-word Whisper hallucination) is covered by
    the same gate.
    """
    from johnny.voice_pipeline import TranscriptFiltered

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    bus = InMemoryEventBus()
    router = _FakeRouterLLM(
        decisions=[
            # Decision should never be consumed — router must not be
            # invoked once the gate fires.
            {"should_speak": True, "confidence": 1.0, "reason": "should not run"},
        ]
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=[noise_text, noise_text]),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["should not run"]),
        tts=_FakeTTS(),
        event_bus=bus,
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            session_id="sess-noise",
        ),
    )
    await pipeline.run()

    # No router decision, no agent speak — the gate held the floor.
    assert router.calls == [], f"router invoked for noise text {noise_text!r}"
    event_types = [e.type for e in bus.snapshot() if e.type != "pipeline_timing"]
    assert "router_decision_made" not in event_types
    assert "agent_spoke" not in event_types
    assert "transcript_finalized" not in event_types

    # Both VAD bursts produce a transcript_filtered event so the
    # activity log can render what was dropped.
    filtered = [e for e in bus.snapshot() if isinstance(e, TranscriptFiltered)]
    assert len(filtered) == 2
    assert all(e.session_id == "sess-noise" for e in filtered)
    assert all(e.reason in {"stoplist_match", "punctuation_only", "too_short"} for e in filtered)


async def test_noise_gate_passes_short_legit_replies_unchanged(
    two_utterance_pcm: bytes,
) -> None:
    """Regression check: 'yes' / 'no' / 'okay' are NOT dropped (Johnny-ckz.14 AC #3).

    The bead explicitly forbids over-filtering these single-word turns —
    if a user replies 'yes' to the bot's question, the bot must continue
    the conversation.
    """
    from johnny.voice_pipeline import TranscriptFiltered

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    bus = InMemoryEventBus()
    router = _FakeRouterLLM(
        decisions=[
            {
                "should_speak": True,
                "confidence": 0.9,
                "reason": "direct affirmation",
                "suggested_reply": "great",
            },
            {
                "should_speak": False,
                "confidence": 0.2,
                "reason": "ack",
            },
        ]
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        # Two short real replies — both must drive a router decision.
        stt=_FakeSTT(transcripts=["yes", "okay"]),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["acknowledged"]),
        tts=_FakeTTS(),
        event_bus=bus,
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
    )
    await pipeline.run()

    # Router saw both transcripts.
    assert len(router.calls) == 2
    assert [e.type for e in bus.snapshot()].count("transcript_finalized") == 2
    # No filtered events.
    assert [e for e in bus.snapshot() if isinstance(e, TranscriptFiltered)] == []


async def test_noise_gate_disabled_lets_everything_through(
    two_utterance_pcm: bytes,
) -> None:
    """Setting ``noise_filter_enabled=False`` restores pre-Johnny-ckz.14 behaviour.

    Operators must be able to opt out per-meeting in case a future
    provider's built-in VAD already covers the same ground; turning the
    gate off must NOT also turn off transcript persistence — the router
    just sees every candidate the way it did before the gate landed.
    """
    from johnny.voice_pipeline import TranscriptFiltered

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    bus = InMemoryEventBus()
    router = _FakeRouterLLM(
        decisions=[
            {"should_speak": False, "confidence": 0.0, "reason": "x"},
            {"should_speak": False, "confidence": 0.0, "reason": "x"},
        ]
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        # Tokens that WOULD be filtered when the gate is on.
        stt=_FakeSTT(transcripts=["uh", "you"]),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=bus,
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            noise_filter_enabled=False,
        ),
    )
    await pipeline.run()

    # Gate off → router sees both, no filtered events.
    assert len(router.calls) == 2
    assert [e for e in bus.snapshot() if isinstance(e, TranscriptFiltered)] == []


async def test_noise_gate_drops_low_confidence_transcripts(
    two_utterance_pcm: bytes,
) -> None:
    """Confidence floor catches sub-threshold STT outputs even when the text looks fine.

    Some STT providers emit a confidence score with each final; sub-
    threshold scores correlate with the kinds of hallucinated content
    the text-only gate can miss (model is uncertain → output is
    untrustworthy). Verified against the same surface so a future
    refactor doesn't accidentally drop the confidence check.
    """
    from johnny.voice_pipeline import TranscriptFiltered

    class _LowConfidenceSTT(STTProvider):
        @property
        def name(self) -> str:
            return "low-conf-stt"

        async def transcribe_stream(
            self,
            audio_iter: AsyncIterator[bytes],
        ) -> AsyncIterator[TranscriptEvent]:
            async for _ in audio_iter:
                pass
            # Text alone would pass every other check.
            yield TranscriptEvent(
                text="hello there",
                is_final=True,
                timestamp_ms=1000,
                confidence=0.1,
            )

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    bus = InMemoryEventBus()
    router = _FakeRouterLLM(
        decisions=[{"should_speak": True, "confidence": 0.95, "reason": "x"}]
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_LowConfidenceSTT(),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=bus,
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            noise_filter_min_confidence=0.6,
        ),
    )
    await pipeline.run()

    assert router.calls == []
    filtered = [e for e in bus.snapshot() if isinstance(e, TranscriptFiltered)]
    assert len(filtered) >= 1
    assert all(e.reason == "low_confidence" for e in filtered)
    assert filtered[0].confidence == 0.1


async def test_noise_gate_skips_stt_on_too_short_audio(tmp_path: Path) -> None:
    """A VAD-detected burst shorter than ``noise_filter_min_audio_ms`` never
    reaches STT (Johnny-ckz.14).

    Skipping the STT round-trip is the leverage point: a noisy mic
    triggers many short VAD bursts per minute, and paying the STT cost
    on each one would burn the user's provider budget before the post-
    STT content gate even ran.
    """
    import array
    import math
    import wave

    from johnny.voice_pipeline import TranscriptFiltered

    sample_rate = 16_000
    # 100 ms tone (below the 250 ms default floor), padded with silence.
    n_tone = sample_rate * 100 // 1000
    n_silence = sample_rate * 400 // 1000
    samples = [0] * n_silence
    samples.extend(
        int(12_000 * math.sin(2 * math.pi * 440 * i / sample_rate))
        for i in range(n_tone)
    )
    samples.extend([0] * n_silence)
    wav_path = tmp_path / "short_burst.wav"
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(array.array("h", samples).tobytes())
    with wave.open(str(wav_path), "rb") as wf:
        pcm = wf.readframes(wf.getnframes())

    frame_size = 640  # 20 ms @ 16 kHz
    frames = [
        pcm[i : i + frame_size]
        for i in range(0, len(pcm), frame_size)
        if i + frame_size <= len(pcm)
    ]

    transport = _BufferedTransport(frames=frames)
    stt = _FakeSTT(transcripts=["should-not-be-called"])
    router = _FakeRouterLLM(
        decisions=[{"should_speak": True, "confidence": 1.0, "reason": "x"}]
    )
    bus = InMemoryEventBus()
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=stt,
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=bus,
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=200,
            noise_filter_min_audio_ms=250,
            session_id="sess-short-audio",
        ),
    )
    await pipeline.run()

    # STT was never called for the sub-threshold burst.
    assert stt.calls == 0
    assert router.calls == []
    filtered = [e for e in bus.snapshot() if isinstance(e, TranscriptFiltered)]
    assert len(filtered) >= 1
    assert all(e.reason == "audio_too_short" for e in filtered)
    assert filtered[0].session_id == "sess-short-audio"
    # The audio duration is recorded so the activity log can show it.
    assert filtered[0].audio_duration_ms is not None
    assert filtered[0].audio_duration_ms < 250


async def test_noise_gate_carries_audio_duration_on_post_stt_filter(
    two_utterance_pcm: bytes,
) -> None:
    """Post-STT filtered events still record the VAD-cut audio length.

    Lets the activity log show 'dropped: "uh" (audio 600 ms, conf 0.9)'
    so operators can see whether the gate was triggered by a long burst
    of real speech that STT mis-transcribed, or a brief cough.
    """
    from johnny.voice_pipeline import TranscriptFiltered

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    bus = InMemoryEventBus()
    router = _FakeRouterLLM(
        decisions=[{"should_speak": False, "confidence": 0.0, "reason": "x"}]
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["you", "uh"]),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=bus,
        config=PipelineConfig(
            vad_threshold=0.05, end_of_speech_ms=300
        ),
    )
    await pipeline.run()

    filtered = [e for e in bus.snapshot() if isinstance(e, TranscriptFiltered)]
    assert len(filtered) == 2
    for evt in filtered:
        # Audio duration ≈ 600 ms tone burst.
        assert evt.audio_duration_ms is not None
        assert evt.audio_duration_ms > 400
        assert evt.confidence == 0.9  # carried through from _FakeSTT
        assert evt.reason == "stoplist_match"


def test_default_noise_stoplist_omits_legit_short_replies() -> None:
    """The default stoplist must NOT contain 'yes' / 'no' / 'okay' / 'thanks' /
    'bye' (Johnny-ckz.14 AC #3).

    These short turns are unambiguously real speech the bot must respond
    to. Anyone tempted to add them later for tighter filtering should
    see this test fail and reconsider — the bead lists them as the
    regression-control set.
    """
    from johnny.voice_pipeline import DEFAULT_NOISE_STOPLIST

    for legit in ("yes", "no", "okay", "thanks", "bye", "hello", "hi"):
        assert legit not in DEFAULT_NOISE_STOPLIST


def test_default_noise_stoplist_contains_known_whisper_artifacts() -> None:
    """The default stoplist catches the specific tokens the bead reported.

    The user observed 'you', 'uh', 'hm' (plus dot sequences and Whisper's
    'thank you for watching' tail). The first three are pure stoplist
    matches; dot sequences land via the punctuation-only path; the
    'thanks for watching' family is multi-word so it lives in the
    stoplist explicitly.
    """
    from johnny.voice_pipeline import DEFAULT_NOISE_STOPLIST

    for token in ("you", "uh", "um", "hm", "hmm", "thank you", "thanks for watching"):
        assert token in DEFAULT_NOISE_STOPLIST


def test_pipeline_config_noise_filter_defaults() -> None:
    """Defaults match the documented sane-floor settings.

    Pins the contract so a future refactor of :class:`PipelineConfig`
    can't silently flip the gate off or relax the floors below what
    the bead's acceptance criteria validated against.
    """
    cfg = PipelineConfig()
    assert cfg.noise_filter_enabled is True
    assert cfg.noise_filter_min_chars == 2
    assert cfg.noise_filter_min_audio_ms == 250
    assert cfg.noise_filter_min_confidence == 0.0
    assert cfg.noise_filter_stoplist  # non-empty default


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


def test_default_end_of_speech_ms_is_800() -> None:
    """Default end-of-speech threshold is 800 ms (Johnny-arh).

    600 ms (the legacy default) was shorter than common mid-sentence
    thinking pauses, so the bot would treat the pause as end-of-turn
    and start answering before the user finished. 800 ms covers natural
    hesitations while still feeling responsive at true end-of-turn.
    """
    from johnny.voice_pipeline.pipeline import DEFAULT_END_OF_SPEECH_MS

    assert DEFAULT_END_OF_SPEECH_MS == 800
    assert PipelineConfig().end_of_speech_ms == 800


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
    # Transcription runs concurrently with the response loop
    # (Johnny-har), so the second transcript may already be in history
    # by the time the router prompt is built. We only require that the
    # current transcript is identified and is "hello".
    current = [entry for entry in window if entry.get("is_current")]
    assert len(current) == 1
    assert current[0]["text"] == "hello"

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
    # Johnny-ckz.3 dropped the hard 6-turn cap. The default is now
    # "unbounded" (0) — :meth:`_build_input_window` enforces a token
    # budget instead.
    assert cfg.transcript_window_size == 0
    assert cfg.context_token_budget == 0
    assert cfg.calendar_context == ""
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


async def test_pipeline_interrupt_before_first_sentence_emits_observable_agent_spoke(
    two_utterance_pcm: bytes,
) -> None:
    """Johnny-tjd: cut answer must publish ``AgentSpoke`` for observability.

    When the user barges in mid-LLM-stream — after some tokens have
    accumulated in ``sentence_buffer`` but BEFORE the first sentence
    boundary flushes to TTS — the pipeline used to silently drop the
    cut answer because ``collected`` was empty. The activity log would
    then show only the post-barge-in follow-up utterance and the cut
    answer was invisible, even though the LLM committed to producing it.

    The fix emits ``AgentSpoke`` (and persists the utterance) whenever
    the answer LLM produced text, with ``audio_duration_ms=0`` as the
    signal that no audio reached the transport. Downstream consumers
    (UI, audit log) can filter on ``audio_duration_ms > 0`` if they
    only want the "actually heard" subset.
    """
    from johnny.voice_pipeline import InMemoryUtteranceSink

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]

    class _PreSentenceInterruptLLM(LLMProvider):
        """First call: yield tokens with NO sentence boundary, set interrupt.

        ``"Sure, let me tell you a"`` has no period / question mark /
        exclamation, so the pipeline's sentence-boundary scanner never
        flushes to TTS while the deltas stream in. After the last delta,
        the LLM sets the interrupt event and returns; the pipeline then
        sees interrupt-set on the tail flush and SKIPS calling TTS, so
        ``collected`` stays empty.

        Second call (driven by the second utterance the two-utterance
        fixture produces): yield nothing so no AgentSpoke fires for the
        follow-up. Keeps the test focused on the cut-path observability
        contract rather than what the follow-up turn does.
        """

        def __init__(self, pipeline_ref: list[Any]) -> None:
            self._pipeline_ref = pipeline_ref
            self._calls = 0

        @property
        def name(self) -> str:
            return "pre-sentence-interrupt"

        async def chat(
            self,
            messages: Sequence[ChatMessage],  # noqa: ARG002
            tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
            response_format: dict[str, Any] | None = None,  # noqa: ARG002
        ) -> LLMResponse:
            return LLMResponse(text="", finish_reason="stop")

        async def stream_chat(
            self,
            messages: Sequence[ChatMessage],
        ) -> AsyncIterator[str]:
            del messages
            self._calls += 1
            if self._calls > 1:
                return
                yield  # pragma: no cover — make this a generator
            for delta in ("Sure", ", ", "let me ", "tell you ", "a"):
                yield delta
            self._pipeline_ref[0].interrupt()

    pipeline_ref: list[Any] = [None]
    sink = InMemoryUtteranceSink()
    transport = _BufferedTransport(frames=frames)
    bus = InMemoryEventBus()
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hi"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": True, "confidence": 0.95, "reason": "ok"}]
        ),
        answer_llm=_PreSentenceInterruptLLM(pipeline_ref),
        tts=_FakeTTS(frame_count=5),
        event_bus=bus,
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
        utterance_sink=sink,
    )
    pipeline_ref[0] = pipeline
    await pipeline.run()

    # No audio reached the transport — the interrupt fired before any
    # sentence boundary, so the pipeline never called TTS.
    assert transport.played == []

    # But the cut path IS observable: one AgentSpoke event with the
    # partial text and audio_duration_ms=0. Without this signal the
    # activity log would silently lose the bot's intent.
    spoke = [e for e in bus.snapshot() if isinstance(e, AgentSpoke)]
    assert len(spoke) == 1
    assert spoke[0].text == "Sure, let me tell you a"
    assert spoke[0].audio_duration_ms == 0

    # And the utterance is persisted so the audit-log row in
    # ``agent_utterances`` records what the bot tried to say.
    records = sink.snapshot()
    assert len(records) == 1
    assert records[0].output_text == "Sure, let me tell you a"
    assert records[0].audio_duration_ms == 0


async def test_pipeline_interrupt_calls_transport_cancel_playback() -> None:
    """Johnny-ckz.13: interrupt() must signal the transport to flush
    queued audio. Without this hook, transports with deep buffers (e.g.
    the BrowserAudioTransport's playback queue) keep streaming TTS to the
    client even after the pipeline gives up on the response, so the user
    keeps hearing the bot for hundreds of milliseconds. The base
    JohnnyTransport.cancel_playback is a no-op; only transports that
    actually buffer audio override it."""

    class _CancellableTransport(_BufferedTransport):
        def __init__(self) -> None:
            super().__init__(frames=[])
            self.cancel_calls = 0

        def cancel_playback(self) -> None:
            self.cancel_calls += 1

    transport = _CancellableTransport()
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hi"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": True, "confidence": 0.95, "reason": "ok"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["whatever"]),
        tts=_FakeTTS(frame_count=2),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(),
    )
    pipeline.interrupt()
    assert transport.cancel_calls == 1
    # The pipeline interrupt event is the contract that already-running
    # generators check; cancel_playback is the contract for queue draining.
    # Both fire together.
    assert pipeline._interrupt_event.is_set()


async def test_pipeline_interrupt_survives_transport_cancel_playback_raise() -> None:
    """If a transport's cancel_playback raises, the pipeline still sets
    its interrupt event — the answer loop must still bail out."""

    class _RaisingTransport(_BufferedTransport):
        def __init__(self) -> None:
            super().__init__(frames=[])

        def cancel_playback(self) -> None:
            raise RuntimeError("buggy transport")

    transport = _RaisingTransport()
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hi"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": True, "confidence": 0.95, "reason": "ok"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["whatever"]),
        tts=_FakeTTS(frame_count=2),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(),
    )
    pipeline.interrupt()  # must not raise
    assert pipeline._interrupt_event.is_set()


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

    types = [e.type for e in bus.snapshot() if e.type != "pipeline_timing"]
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

    types = [e.type for e in bus.snapshot() if e.type != "pipeline_timing"]
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
        stt=_FakeSTT(transcripts=["hello team", "any questions"]),
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
    types = {e.type for e in bus.snapshot() if e.type != "pipeline_timing"}
    assert "approval_pending" not in types
    assert "approval_resolved" not in types
    # Decisions recorded as suppressed (not rejected).
    assert all(r.outcome == "suppressed" for r in dsink.snapshot())


# --- US-026: Listen-only and Suggest-only modes ----------------------------


async def test_listen_only_mode_skips_router_and_tts_emits_only_transcripts(
    two_utterance_pcm: bytes,
) -> None:
    """Listen-only must never run the router, never emit decisions / utterances,
    and never play audio — regardless of ``speak=True``."""
    from johnny.voice_pipeline import InMemoryUtteranceSink

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    router = _FakeRouterLLM(
        decisions=[{"should_speak": True, "confidence": 1.0, "reason": "x"}]
    )
    tts = _FakeTTS()
    dsink = InMemoryDecisionSink()
    usink = InMemoryUtteranceSink()
    bus = InMemoryEventBus()
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["one", "two"]),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["resp"]),
        tts=tts,
        event_bus=bus,
        config=PipelineConfig(
            # speak=True deliberately set — mode must override
            speak=True,
            mode="listen_only",
            vad_threshold=0.05,
            end_of_speech_ms=300,
        ),
        decision_sink=dsink,
        utterance_sink=usink,
    )
    await pipeline.run()
    types = [e.type for e in bus.snapshot() if e.type != "pipeline_timing"]
    # Only transcripts: no decisions, no utterances, no agent_spoke,
    # no agent_suggested.
    assert types == ["transcript_finalized", "transcript_finalized"]
    # Router LLM never called.
    assert router.calls == []
    # TTS never invoked, no audio played.
    assert tts.calls == []
    assert transport.played == []
    # No persisted decisions or utterances.
    assert dsink.snapshot() == []
    assert usink.snapshot() == []


async def test_suggest_only_mode_runs_router_emits_agent_suggested_no_tts(
    two_utterance_pcm: bytes,
) -> None:
    """Suggest-only runs the router and emits AgentSuggested when the router
    approves, but never invokes the answer LLM or plays audio."""
    from johnny.voice_pipeline import AgentSuggested, InMemoryUtteranceSink

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
                "reason": "addresses you",
                "reply_type": "answer",
                "suggested_reply": "Sounds good!",
            },
            {
                "should_speak": False,
                "confidence": 0.2,
                "reason": "not about me",
            },
        ]
    )
    answer = _FakeAnswerLLM(answers=["resp"])
    tts = _FakeTTS()
    dsink = InMemoryDecisionSink()
    usink = InMemoryUtteranceSink()
    bus = InMemoryEventBus()
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hello", "world"]),
        router_llm=router,
        answer_llm=answer,
        tts=tts,
        event_bus=bus,
        config=PipelineConfig(
            speak=True,
            mode="suggest_only",
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.7,
        ),
        decision_sink=dsink,
        utterance_sink=usink,
    )
    await pipeline.run()
    events = bus.snapshot()
    # Activity-log timings (Johnny-ckz.7) share the same event bus but
    # are observability; filter them out before asserting the
    # transcript/decision/utterance contract.
    types = [e.type for e in events if e.type != "pipeline_timing"]
    # Transcription runs concurrently with response (Johnny-har), so
    # the cross-utterance event order can interleave. The contract
    # is: two transcripts, two router decisions, one agent_suggested
    # (only the first router decision approves).
    assert sorted(types) == sorted([
        "transcript_finalized",
        "router_decision_made",
        "agent_suggested",
        "transcript_finalized",
        "router_decision_made",
    ])
    # AgentSuggested carries the suggested reply.
    suggested = [e for e in events if e.type == "agent_suggested"]
    assert len(suggested) == 1
    assert isinstance(suggested[0], AgentSuggested)
    assert suggested[0].suggested_reply == "Sounds good!"
    assert suggested[0].reason == "addresses you"
    assert suggested[0].reply_type == "answer"
    # Answer LLM and TTS are NOT invoked.
    assert answer.calls == []
    assert tts.calls == []
    assert transport.played == []
    # No utterances persisted.
    assert usink.snapshot() == []
    # Decision rows persisted: first as 'suggested', second as 'suppressed'.
    records = dsink.snapshot()
    assert len(records) == 2
    assert records[0].outcome == "suggested"
    assert records[1].outcome == "suppressed"


async def test_suggest_only_below_threshold_does_not_emit_agent_suggested(
    two_utterance_pcm: bytes,
) -> None:
    """When router says should_speak=True but confidence < threshold,
    suggest-only suppresses just like any other mode."""
    from johnny.voice_pipeline import InMemoryUtteranceSink

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
                "confidence": 0.3,
                "reason": "weak",
                "suggested_reply": "maybe",
            }
        ]
    )
    answer = _FakeAnswerLLM(answers=["resp"])
    tts = _FakeTTS()
    dsink = InMemoryDecisionSink()
    usink = InMemoryUtteranceSink()
    bus = InMemoryEventBus()
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["one", "two"]),
        router_llm=router,
        answer_llm=answer,
        tts=tts,
        event_bus=bus,
        config=PipelineConfig(
            mode="suggest_only",
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.7,
        ),
        decision_sink=dsink,
        utterance_sink=usink,
    )
    await pipeline.run()
    types = [e.type for e in bus.snapshot() if e.type != "pipeline_timing"]
    # No agent_suggested emitted because confidence was below threshold.
    assert "agent_suggested" not in types
    # All persisted decisions are 'suppressed'.
    assert all(r.outcome == "suppressed" for r in dsink.snapshot())
    # No utterances, no TTS, no audio.
    assert usink.snapshot() == []
    assert tts.calls == []
    assert transport.played == []


async def test_suggest_only_should_speak_false_suppresses_no_suggestion(
    two_utterance_pcm: bytes,
) -> None:
    """When router says should_speak=False in suggest-only mode, no
    AgentSuggested fires — only the decision is recorded."""
    from johnny.voice_pipeline import InMemoryUtteranceSink

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    router = _FakeRouterLLM(
        decisions=[
            {"should_speak": False, "confidence": 0.1, "reason": "noise"}
        ]
    )
    answer = _FakeAnswerLLM(answers=["resp"])
    tts = _FakeTTS()
    dsink = InMemoryDecisionSink()
    usink = InMemoryUtteranceSink()
    bus = InMemoryEventBus()
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["one", "two"]),
        router_llm=router,
        answer_llm=answer,
        tts=tts,
        event_bus=bus,
        config=PipelineConfig(
            mode="suggest_only",
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
        decision_sink=dsink,
        utterance_sink=usink,
    )
    await pipeline.run()
    types = [e.type for e in bus.snapshot() if e.type != "pipeline_timing"]
    assert "agent_suggested" not in types
    assert "agent_spoke" not in types
    assert answer.calls == []
    assert tts.calls == []
    assert usink.snapshot() == []


async def test_listen_only_mode_persists_transcripts_for_audit(
    two_utterance_pcm: bytes,
) -> None:
    """Listen-only persists transcripts — that IS the audit trail for the mode."""
    from johnny.voice_pipeline import InMemoryTranscriptSink

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    tsink = InMemoryTranscriptSink()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hello", "world"]),
        router_llm=_FakeRouterLLM(decisions=[]),
        answer_llm=_FakeAnswerLLM(answers=[]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            mode="listen_only",
            vad_threshold=0.05,
            end_of_speech_ms=300,
        ),
        transcript_sink=tsink,
    )
    await pipeline.run()
    records = tsink.snapshot()
    assert len(records) == 2
    assert records[0].text == "hello"
    assert records[1].text == "world"


async def test_free_auto_speak_speaks_without_approval_or_allowlist(
    two_utterance_pcm: bytes,
) -> None:
    """free_auto_speak streams the answer LLM straight into TTS, skipping
    both the approval round and the allowed-reply matcher (Johnny-vgl).

    Pins the spec so a future refactor of ``_respond_to_transcript``'s mode
    dispatch doesn't silently change which collaborator is invoked.
    """
    from johnny.voice_pipeline import (
        FREE_AUTO_SPEAK_MODE,
        InMemoryUtteranceSink,
        NoopApprovalGate,
    )

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    tts = _FakeTTS(frame_count=3)
    answer = _FakeAnswerLLM(answers=["Nice to meet you all."])
    dsink = InMemoryDecisionSink()
    usink = InMemoryUtteranceSink()
    bus = InMemoryEventBus()
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["are you here", "thanks"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {
                    "should_speak": True,
                    "confidence": 0.95,
                    "reason": "direct ask",
                    "suggested_reply": "Yes",
                },
                {"should_speak": False, "confidence": 0.1, "reason": "noise"},
            ]
        ),
        answer_llm=answer,
        tts=tts,
        event_bus=bus,
        config=PipelineConfig(
            mode=FREE_AUTO_SPEAK_MODE,
            # allowed_replies present to prove the mode bypasses the allowlist.
            allowed_replies=("yes", "no"),
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
        approval_gate=NoopApprovalGate(),
        decision_sink=dsink,
        utterance_sink=usink,
    )
    await pipeline.run()

    types = [e.type for e in bus.snapshot() if e.type != "pipeline_timing"]
    # Speaks (not "suggested") and never published an approval round.
    assert "agent_spoke" in types
    assert "agent_suggested" not in types
    assert "approval_pending" not in types

    spokes = [e for e in bus.snapshot() if isinstance(e, AgentSpoke)]
    assert len(spokes) == 1
    # Free-form output goes through verbatim — not coerced into "yes"/"no".
    assert spokes[0].text == "Nice to meet you all."
    assert spokes[0].matched_allowed_reply is None

    records = dsink.snapshot()
    assert len(records) == 2
    assert records[0].outcome == "spoken"
    assert records[1].outcome == "suppressed"

    utterances = usink.snapshot()
    assert len(utterances) == 1
    assert utterances[0].mode == FREE_AUTO_SPEAK_MODE
    assert utterances[0].output_text == "Nice to meet you all."
    assert utterances[0].matched_allowed_reply is None


async def test_free_auto_speak_router_below_threshold_suppresses(
    two_utterance_pcm: bytes,
) -> None:
    """The router's confidence_threshold still gates free_auto_speak so
    ambient chatter doesn't trigger replies — same gate as every other
    speaking mode (Johnny-vgl: confirm the threshold is honoured)."""
    from johnny.voice_pipeline import FREE_AUTO_SPEAK_MODE

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    tts = _FakeTTS()
    dsink = InMemoryDecisionSink()
    bus = InMemoryEventBus()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["soft chatter"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {"should_speak": True, "confidence": 0.3, "reason": "weak"},
            ]
        ),
        answer_llm=_FakeAnswerLLM(answers=["resp"]),
        tts=tts,
        event_bus=bus,
        config=PipelineConfig(
            mode=FREE_AUTO_SPEAK_MODE,
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.7,
        ),
        decision_sink=dsink,
    )
    await pipeline.run()

    assert "agent_spoke" not in [e.type for e in bus.snapshot()]
    assert tts.calls == []
    assert all(r.outcome == "suppressed" for r in dsink.snapshot())


async def test_mode_constants_match_db_string_values() -> None:
    """Pipeline mode constants must match the DB BotMode enum values so the
    string passed from MeetingConfig.mode wires through without translation."""
    from app.db.models import BotMode
    from johnny.voice_pipeline import (
        APPROVAL_REQUIRED_MODE,
        AUTONOMOUS_MODE,
        FREE_AUTO_SPEAK_MODE,
        FREE_FORM_MODES,
        LIMITED_AUTO_SPEAK_MODE,
        LISTEN_ONLY_MODE,
        NON_SPEAKING_MODES,
        SPEAKING_MODES,
        SUGGEST_ONLY_MODE,
    )

    assert LISTEN_ONLY_MODE == BotMode.LISTEN_ONLY.value
    assert SUGGEST_ONLY_MODE == BotMode.SUGGEST_ONLY.value
    assert APPROVAL_REQUIRED_MODE == BotMode.APPROVAL_REQUIRED.value
    assert LIMITED_AUTO_SPEAK_MODE == BotMode.LIMITED_AUTO_SPEAK.value
    assert FREE_AUTO_SPEAK_MODE == BotMode.FREE_AUTO_SPEAK.value
    assert AUTONOMOUS_MODE == BotMode.AUTONOMOUS.value
    assert NON_SPEAKING_MODES == {LISTEN_ONLY_MODE, SUGGEST_ONLY_MODE}
    assert SPEAKING_MODES == {
        APPROVAL_REQUIRED_MODE,
        LIMITED_AUTO_SPEAK_MODE,
        FREE_AUTO_SPEAK_MODE,
        AUTONOMOUS_MODE,
    }
    assert FREE_FORM_MODES == {FREE_AUTO_SPEAK_MODE, AUTONOMOUS_MODE}
    # Free-form modes must be a subset of speaking modes — if a free-form
    # mode forgets to register itself as speaking, TTS-missing degradation
    # never kicks in for it (the Johnny-vgl silent-failure bug).
    assert FREE_FORM_MODES.issubset(SPEAKING_MODES)
    # Every DB-known mode is classified as either non-speaking or speaking
    # — adding a new BotMode without picking a side will fail this check.
    all_modes = {m.value for m in BotMode}
    assert NON_SPEAKING_MODES | SPEAKING_MODES == all_modes
    assert NON_SPEAKING_MODES.isdisjoint(SPEAKING_MODES)


# --- Johnny-ckz.2: autonomous mode -----------------------------------------


async def test_autonomous_speaks_without_approval_or_allowlist(
    two_utterance_pcm: bytes,
) -> None:
    """Autonomous mode streams the answer LLM straight into TTS, skipping
    both the approval round and the allowed-reply matcher (Johnny-ckz.2).

    Mirrors free_auto_speak's spec; pins that adding AUTONOMOUS to the
    pipeline's free-form set doesn't accidentally route through the
    approval gate or coerce the LLM output into an allowlist phrase.
    """
    from johnny.voice_pipeline import (
        AUTONOMOUS_MODE,
        InMemoryUtteranceSink,
        NoopApprovalGate,
    )

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    tts = _FakeTTS(frame_count=3)
    answer = _FakeAnswerLLM(answers=["Happy to help — here's the plan."])
    dsink = InMemoryDecisionSink()
    usink = InMemoryUtteranceSink()
    bus = InMemoryEventBus()
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["are you here", "thanks"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {
                    "should_speak": True,
                    "confidence": 0.95,
                    "reason": "direct ask",
                    "suggested_reply": "Yes",
                },
                {"should_speak": False, "confidence": 0.1, "reason": "noise"},
            ]
        ),
        answer_llm=answer,
        tts=tts,
        event_bus=bus,
        config=PipelineConfig(
            mode=AUTONOMOUS_MODE,
            # allowed_replies present to prove autonomous bypasses the allowlist.
            allowed_replies=("yes", "no"),
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            instructions="Help the host run the meeting.",
            # Disable the rate limit so this test only exercises the
            # free-form-with-approval-bypass path.
            rate_limit_max_utterances=0,
        ),
        approval_gate=NoopApprovalGate(),
        decision_sink=dsink,
        utterance_sink=usink,
    )
    await pipeline.run()

    types = [e.type for e in bus.snapshot() if e.type != "pipeline_timing"]
    assert "agent_spoke" in types
    assert "agent_suggested" not in types
    assert "approval_pending" not in types

    spokes = [e for e in bus.snapshot() if isinstance(e, AgentSpoke)]
    assert len(spokes) == 1
    assert spokes[0].text == "Happy to help — here's the plan."
    assert spokes[0].matched_allowed_reply is None

    records = dsink.snapshot()
    assert len(records) == 2
    assert records[0].outcome == "spoken"
    assert records[1].outcome == "suppressed"

    utterances = usink.snapshot()
    assert len(utterances) == 1
    assert utterances[0].mode == AUTONOMOUS_MODE
    assert utterances[0].output_text == "Happy to help — here's the plan."
    assert utterances[0].matched_allowed_reply is None


async def test_autonomous_router_below_threshold_suppresses(
    two_utterance_pcm: bytes,
) -> None:
    """The router's confidence_threshold still gates autonomous mode —
    ambient chatter must not trigger a reply even though the mode is
    otherwise free to speak whenever it pleases."""
    from johnny.voice_pipeline import AUTONOMOUS_MODE

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    tts = _FakeTTS()
    dsink = InMemoryDecisionSink()
    bus = InMemoryEventBus()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["soft chatter"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {"should_speak": True, "confidence": 0.3, "reason": "weak"},
            ]
        ),
        answer_llm=_FakeAnswerLLM(answers=["resp"]),
        tts=tts,
        event_bus=bus,
        config=PipelineConfig(
            mode=AUTONOMOUS_MODE,
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.7,
            instructions="Be a good meeting participant.",
        ),
        decision_sink=dsink,
    )
    await pipeline.run()

    assert "agent_spoke" not in [e.type for e in bus.snapshot()]
    assert tts.calls == []
    assert all(r.outcome == "suppressed" for r in dsink.snapshot())


async def test_autonomous_rate_limit_suppresses_without_allowlist(
    two_utterance_pcm: bytes,
) -> None:
    """Autonomous mode enforces the per-session rate limit even when
    ``allowed_replies`` is empty. The first utterance speaks; the second
    is suppressed by the cap (max=1 in this test).

    free_auto_speak only applies the cap when allowed_replies is set —
    autonomous is the production-ready free-form mode where the cap is
    always required to control cost + talking-over-others (Johnny-ckz.2).
    """
    from johnny.voice_pipeline import AUTONOMOUS_MODE, InMemoryUtteranceSink

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    tts = _FakeTTS(frame_count=2)
    dsink = InMemoryDecisionSink()
    usink = InMemoryUtteranceSink()
    bus = InMemoryEventBus()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["question one", "question two"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {
                    "should_speak": True,
                    "confidence": 0.95,
                    "reason": "direct ask 1",
                    "suggested_reply": "ok",
                },
                {
                    "should_speak": True,
                    "confidence": 0.95,
                    "reason": "direct ask 2",
                    "suggested_reply": "ok",
                },
            ]
        ),
        answer_llm=_FakeAnswerLLM(answers=["first", "second"]),
        tts=tts,
        event_bus=bus,
        config=PipelineConfig(
            mode=AUTONOMOUS_MODE,
            allowed_replies=(),
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            instructions="Speak when spoken to.",
            rate_limit_max_utterances=1,
            rate_limit_window_ms=5 * 60 * 1000,
        ),
        decision_sink=dsink,
        utterance_sink=usink,
    )
    await pipeline.run()

    records = dsink.snapshot()
    assert len(records) == 2
    # First utterance was spoken, second hit the rate cap.
    outcomes = [r.outcome for r in records]
    assert outcomes == ["spoken", "suppressed"]

    # Only one utterance reached the sink — the cap suppressed the second.
    utterances = usink.snapshot()
    assert len(utterances) == 1
    assert utterances[0].mode == AUTONOMOUS_MODE


async def test_free_auto_speak_rate_limit_not_applied_without_allowlist(
    two_utterance_pcm: bytes,
) -> None:
    """free_auto_speak deliberately leaves the cap unenforced when no
    allowlist is configured (prototype-friendly), while AUTONOMOUS does
    enforce it. This test pins the difference so a future refactor that
    consolidates the rate-limit gate keeps the two modes distinct."""
    from johnny.voice_pipeline import FREE_AUTO_SPEAK_MODE

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    tts = _FakeTTS(frame_count=2)
    dsink = InMemoryDecisionSink()
    bus = InMemoryEventBus()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["q1", "q2"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {
                    "should_speak": True,
                    "confidence": 0.95,
                    "reason": "ask 1",
                    "suggested_reply": "ok",
                },
                {
                    "should_speak": True,
                    "confidence": 0.95,
                    "reason": "ask 2",
                    "suggested_reply": "ok",
                },
            ]
        ),
        answer_llm=_FakeAnswerLLM(answers=["first", "second"]),
        tts=tts,
        event_bus=bus,
        config=PipelineConfig(
            mode=FREE_AUTO_SPEAK_MODE,
            allowed_replies=(),
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            # Cap of 1 would suppress the second if free-mode applied it.
            rate_limit_max_utterances=1,
            rate_limit_window_ms=5 * 60 * 1000,
        ),
        decision_sink=dsink,
    )
    await pipeline.run()

    records = dsink.snapshot()
    assert len(records) == 2
    assert [r.outcome for r in records] == ["spoken", "spoken"]


# --- Johnny-ckz.3: whole-meeting context handling ------------------------


def _bare_pipeline(
    *,
    router: _FakeRouterLLM | None = None,
    answer: _FakeAnswerLLM | None = None,
    config: PipelineConfig | None = None,
    transcript_history_loader: Any | None = None,
) -> VoicePipeline:
    """Build a minimal pipeline that only exercises prompt-building logic.

    The audio stages (transport / VAD / STT / TTS) are stubbed because
    these tests poke ``_remember_transcript`` / ``_build_input_window``
    / ``_answer_messages`` directly rather than running ``run()``.
    """
    return VoicePipeline(
        transport=_BufferedTransport(frames=[]),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=[]),
        router_llm=router or _FakeRouterLLM(decisions=[]),
        answer_llm=answer or _FakeAnswerLLM(answers=[]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=config or PipelineConfig(),
        transcript_history_loader=transcript_history_loader,
    )


async def test_history_unbounded_by_default_no_token_budget() -> None:
    """Default config keeps every transcript and emits the whole window."""
    pipeline = _bare_pipeline()
    transcripts = [
        TranscriptFinalized(text=f"line {i}", timestamp_ms=i * 100)
        for i in range(20)
    ]
    for t in transcripts:
        pipeline._remember_transcript(t)

    snapshot = await pipeline._build_input_window(transcripts[-1])

    assert len(pipeline._transcript_history) == 20
    assert len(snapshot["transcript_window"]) == 20
    assert snapshot.get("summary") is None
    assert snapshot["transcript_total_count"] == 20


async def test_history_token_budget_triggers_summarisation() -> None:
    """When estimated tokens exceed budget, oldest transcripts are summarised."""

    class _SummaryRouter(_FakeRouterLLM):
        def __init__(self) -> None:
            super().__init__(decisions=[])
            self.summary_calls: list[Sequence[ChatMessage]] = []

        async def chat(
            self,
            messages: Sequence[ChatMessage],
            tools: Sequence[ToolDefinition] | None = None,
            response_format: dict[str, Any] | None = None,
        ) -> LLMResponse:
            # The summariser uses chat() without response_format. The
            # router decision path passes response_format, so we can
            # tell them apart and only intercept the summary call.
            if response_format is None:
                self.summary_calls.append(list(messages))
                return LLMResponse(
                    text="Earlier: customers discussed timelines.",
                    finish_reason="stop",
                )
            return await super().chat(messages, tools, response_format)

    router = _SummaryRouter()
    # Tiny budget guarantees the guard triggers; each entry is ~14
    # chars → 3 tokens.
    cfg = PipelineConfig(context_token_budget=20, summary_recent_keep=2)
    pipeline = _bare_pipeline(router=router, config=cfg)
    transcripts = [
        TranscriptFinalized(
            text=f"transcript chunk number {i} from the meeting body",
            timestamp_ms=i * 100,
        )
        for i in range(10)
    ]
    for t in transcripts:
        pipeline._remember_transcript(t)

    snapshot = await pipeline._build_input_window(transcripts[-1])

    assert len(router.summary_calls) == 1
    assert snapshot.get("summary") is not None
    assert snapshot["summary"]["text"].startswith("Earlier")
    assert snapshot["summary"]["summarised_count"] > 0
    # Recent verbatim slice keeps at least summary_recent_keep entries
    assert len(snapshot["transcript_window"]) >= 2
    # Total count reflects the whole history, not the summary slice
    assert snapshot["transcript_total_count"] == 10


async def test_summary_cache_reused_until_cutoff_advances() -> None:
    """Two successive builds with the same cutoff don't recompute the summary."""

    class _CountingRouter(_FakeRouterLLM):
        def __init__(self) -> None:
            super().__init__(decisions=[])
            self.summary_call_count = 0

        async def chat(
            self,
            messages: Sequence[ChatMessage],
            tools: Sequence[ToolDefinition] | None = None,
            response_format: dict[str, Any] | None = None,
        ) -> LLMResponse:
            if response_format is None:
                self.summary_call_count += 1
                return LLMResponse(
                    text=f"summary call #{self.summary_call_count}",
                    finish_reason="stop",
                )
            return await super().chat(messages, tools, response_format)

    router = _CountingRouter()
    cfg = PipelineConfig(context_token_budget=20, summary_recent_keep=2)
    pipeline = _bare_pipeline(router=router, config=cfg)
    transcripts = [
        TranscriptFinalized(
            text=f"transcript chunk number {i} from the meeting body",
            timestamp_ms=i * 100,
        )
        for i in range(10)
    ]
    for t in transcripts:
        pipeline._remember_transcript(t)

    first = await pipeline._build_input_window(transcripts[-1])
    second = await pipeline._build_input_window(transcripts[-1])

    assert router.summary_call_count == 1
    assert first["summary"]["text"] == second["summary"]["text"]


async def test_summary_fallback_when_llm_raises() -> None:
    """A summariser exception falls back to the concatenated transcripts."""

    class _BrokenRouter(_FakeRouterLLM):
        def __init__(self) -> None:
            super().__init__(decisions=[])

        async def chat(
            self,
            messages: Sequence[ChatMessage],
            tools: Sequence[ToolDefinition] | None = None,
            response_format: dict[str, Any] | None = None,
        ) -> LLMResponse:
            if response_format is None:
                raise RuntimeError("summary down")
            return await super().chat(messages, tools, response_format)

    router = _BrokenRouter()
    cfg = PipelineConfig(context_token_budget=20, summary_recent_keep=2)
    pipeline = _bare_pipeline(router=router, config=cfg)
    transcripts = [
        TranscriptFinalized(
            text=f"transcript chunk number {i} from the meeting body",
            timestamp_ms=i * 100,
        )
        for i in range(10)
    ]
    for t in transcripts:
        pipeline._remember_transcript(t)

    snapshot = await pipeline._build_input_window(transcripts[-1])

    assert snapshot["summary"]["text"]  # non-empty fallback
    # Pipeline still emits a usable window even when the summariser fails
    assert len(snapshot["transcript_window"]) >= 2


async def test_rehydrate_seeds_history_from_loader_on_run() -> None:
    """The pipeline reloads prior transcripts at run() startup."""
    from johnny.voice_pipeline import InMemoryTranscriptHistoryLoader

    prior = [
        TranscriptFinalized(text="prior 1", timestamp_ms=100),
        TranscriptFinalized(text="prior 2", timestamp_ms=200),
    ]
    loader = InMemoryTranscriptHistoryLoader(transcripts=prior)
    pipeline = _bare_pipeline(transcript_history_loader=loader)

    # Drive the rehydration directly; this matches what run() invokes
    # before the utterance loop starts.
    await pipeline._rehydrate_transcript_history()

    assert [t.text for t in pipeline._transcript_history] == ["prior 1", "prior 2"]
    assert loader.calls == [(None, None)]


async def test_rehydrate_swallows_loader_exception() -> None:
    """A loader exception leaves history empty rather than failing startup."""

    class _BrokenLoader:
        async def load(
            self, *, session_id: str | None, bot_session_id: int | None
        ) -> list[TranscriptFinalized]:
            del session_id, bot_session_id
            raise RuntimeError("loader down")

        async def close(self) -> None:  # pragma: no cover
            return None

    pipeline = _bare_pipeline(transcript_history_loader=_BrokenLoader())

    await pipeline._rehydrate_transcript_history()

    assert pipeline._transcript_history == []


def _build_summary_router(summary_text: str) -> _FakeRouterLLM:
    """Helper: a router LLM that intercepts the summary call only."""

    class _SummaryRouter(_FakeRouterLLM):
        def __init__(self) -> None:
            super().__init__(decisions=[])

        async def chat(
            self,
            messages: Sequence[ChatMessage],
            tools: Sequence[ToolDefinition] | None = None,
            response_format: dict[str, Any] | None = None,
        ) -> LLMResponse:
            if response_format is None:
                return LLMResponse(text=summary_text, finish_reason="stop")
            return await super().chat(messages, tools, response_format)

    return _SummaryRouter()


def _budget_breaking_transcripts(
    count: int = 8,
) -> list[TranscriptFinalized]:
    return [
        TranscriptFinalized(
            text=f"transcript chunk number {i} from the meeting body",
            timestamp_ms=i * 100,
        )
        for i in range(count)
    ]


async def test_input_window_snapshot_includes_calendar_context_and_summary() -> None:
    """Audit row stores the same calendar context + summary the LLM saw."""
    cfg = PipelineConfig(
        context="Manual context note",
        calendar_context="Q3 planning sync with launch reviewers.",
        context_token_budget=20,
        summary_recent_keep=2,
    )
    pipeline = _bare_pipeline(
        router=_build_summary_router("Earlier: planning."),
        config=cfg,
    )
    transcripts = _budget_breaking_transcripts()
    for t in transcripts:
        pipeline._remember_transcript(t)

    snapshot = await pipeline._build_input_window(transcripts[-1])

    assert snapshot["context"] == "Manual context note"
    assert snapshot["calendar_context"] == "Q3 planning sync with launch reviewers."
    assert snapshot["summary"]["text"] == "Earlier: planning."


async def test_router_prompt_includes_calendar_context_and_summary() -> None:
    """Calendar description and summary both reach the router system prompt."""
    router = _build_summary_router("Earlier: planning.")
    cfg = PipelineConfig(
        instructions="be brief",
        calendar_context="Project kickoff with the marketing team.",
        context_token_budget=20,
        summary_recent_keep=2,
    )
    pipeline = _bare_pipeline(router=router, config=cfg)
    transcripts = _budget_breaking_transcripts()
    for t in transcripts:
        pipeline._remember_transcript(t)

    snapshot = await pipeline._build_input_window(transcripts[-1])
    messages = pipeline._router_messages(transcripts[-1], snapshot)
    system_content = messages[0].content or ""
    user_content = messages[1].content or ""

    assert "Calendar event description: Project kickoff" in system_content
    assert "Earlier (summary): Earlier: planning." in user_content


async def test_answer_prompt_includes_calendar_context_and_summary() -> None:
    """Answer LLM sees the calendar description and the cached summary line."""
    cfg = PipelineConfig(
        calendar_context="Weekly product standup.",
        context_token_budget=20,
        summary_recent_keep=2,
    )
    pipeline = _bare_pipeline(
        router=_build_summary_router("Earlier: ten minutes of agenda review."),
        config=cfg,
    )
    transcripts = _budget_breaking_transcripts()
    for t in transcripts:
        pipeline._remember_transcript(t)

    # Trigger the summary build so _history_summary gets populated.
    await pipeline._build_input_window(transcripts[-1])

    decision = RouterDecision(
        should_speak=True, confidence=0.9, reason="ack", suggested_reply=None
    )
    messages = pipeline._answer_messages(transcripts[-1], decision)
    system_content = messages[0].content or ""
    user_content = messages[1].content or ""

    assert "Calendar event description: Weekly product standup." in system_content
    assert "Earlier (summary): Earlier: ten minutes of agenda review." in user_content


def test_estimate_tokens_uses_chars_per_token_heuristic() -> None:
    """The internal token estimator returns ``len // 4`` (floored at 1)."""
    from johnny.voice_pipeline.pipeline import _estimate_tokens

    assert _estimate_tokens(None) == 0
    assert _estimate_tokens("") == 0
    assert _estimate_tokens("a") == 1  # floor at 1 for any non-empty string
    assert _estimate_tokens("12345678") == 2  # 8 chars / 4
    assert _estimate_tokens("x" * 400) == 100


async def test_unbounded_history_with_large_window_no_summary() -> None:
    """When budget is large the full history is emitted verbatim."""
    pipeline = _bare_pipeline(
        config=PipelineConfig(context_token_budget=10_000, summary_recent_keep=2)
    )
    transcripts = [
        TranscriptFinalized(text=f"chunk {i}", timestamp_ms=i * 100)
        for i in range(15)
    ]
    for t in transcripts:
        pipeline._remember_transcript(t)

    snapshot = await pipeline._build_input_window(transcripts[-1])

    assert snapshot.get("summary") is None
    assert len(snapshot["transcript_window"]) == 15


async def test_legacy_transcript_window_size_still_caps_history() -> None:
    """``transcript_window_size > 0`` reinstates the legacy hard cap."""
    pipeline = _bare_pipeline(
        config=PipelineConfig(transcript_window_size=4)
    )
    for i in range(10):
        pipeline._remember_transcript(
            TranscriptFinalized(text=f"line {i}", timestamp_ms=i * 100)
        )

    assert len(pipeline._transcript_history) == 4
    assert [t.text for t in pipeline._transcript_history] == [
        "line 6",
        "line 7",
        "line 8",
        "line 9",
    ]


# --- Johnny-har: STT must never pause while bot is busy --------------------


async def test_transcription_keeps_running_while_bot_is_speaking(
    four_utterance_pcm: bytes,
) -> None:
    """All participant utterances reach the transcript sink even when the
    bot's TTS is stalled mid-utterance.

    Reproduces the Johnny-har regression: the pre-fix pipeline serialised
    STT and the bot's answer/TTS in a single ``async for`` loop, so a
    long-running answer kept the VAD generator suspended and any audio
    arriving in that window was silently dropped from the capture queue.

    Setup: a ``_StallingTTS`` that yields one frame and then awaits an
    :class:`asyncio.Event` controlled by the test. The pipeline starts
    responding to the first utterance, gets stuck inside TTS, and the
    test asserts that the remaining three utterances still make it
    through STT → transcript sink before the TTS is released. After
    release, the rate limiter naturally throttles the catch-up replies.
    """
    from johnny.voice_pipeline import InMemoryTranscriptSink

    frame_size = 640
    frames = [
        four_utterance_pcm[i : i + frame_size]
        for i in range(0, len(four_utterance_pcm), frame_size)
        if i + frame_size <= len(four_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)

    release_tts = asyncio.Event()
    tts_entered = asyncio.Event()

    class _StallingTTS(TTSProvider):
        """TTS that emits one frame then awaits a test-controlled event."""

        def __init__(self) -> None:
            self.calls: list[str] = []

        @property
        def name(self) -> str:
            return "stalling-tts"

        async def synthesize_stream(
            self,
            text: str,
            voice_id: str | None = None,  # noqa: ARG002
        ) -> AsyncIterator[bytes]:
            self.calls.append(text)
            yield bytes(320)  # one 10 ms frame so AgentSpoke registers audio
            tts_entered.set()
            await release_tts.wait()

    stt = _FakeSTT(transcripts=["one", "two", "three", "four"])
    router = _FakeRouterLLM(
        decisions=[
            {"should_speak": True, "confidence": 0.95, "reason": "yes"},
        ]
    )
    answer = _FakeAnswerLLM(answers=["reply"])
    tts = _StallingTTS()
    bus = InMemoryEventBus()
    tsink = InMemoryTranscriptSink()

    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=stt,
        router_llm=router,
        answer_llm=answer,
        tts=tts,
        event_bus=bus,
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            session_id="johnny-har",
        ),
        transcript_sink=tsink,
    )

    run_task = asyncio.create_task(pipeline.run())

    # Wait until the bot is wedged inside TTS — proves the response loop
    # is committed to a long-running utterance.
    await asyncio.wait_for(tts_entered.wait(), timeout=2.0)

    # All four transcripts must have reached the sink even though the
    # bot is still stalled. Poll briefly because the transcribe loop
    # runs concurrently with the suspended response loop.
    async def _all_transcripts_persisted() -> None:
        while len(tsink.snapshot()) < 4:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_all_transcripts_persisted(), timeout=2.0)

    persisted = [r.text for r in tsink.snapshot()]
    assert persisted == ["one", "two", "three", "four"]

    # And the same transcripts must have been published on the event
    # bus — UI subscribers can't render gaps they never received.
    bus_transcripts = [
        e.text for e in bus.snapshot() if isinstance(e, TranscriptFinalized)
    ]
    assert bus_transcripts == ["one", "two", "three", "four"]

    # Crucially: the bot is still stuck in TTS at this point. Verify
    # by checking that AgentSpoke has not been published yet — if STT
    # had been gated on the answer pipeline, the transcripts above
    # could never have been persisted while the bot is still wedged.
    spoke = [e for e in bus.snapshot() if isinstance(e, AgentSpoke)]
    assert spoke == [], (
        "AgentSpoke fired before TTS was released — the stalling-TTS "
        "fixture should have kept the response loop wedged"
    )

    # Release the bot and let the pipeline drain.
    release_tts.set()
    await asyncio.wait_for(run_task, timeout=2.0)

    # Bot eventually spoke at least once (for the first transcript) —
    # confirms the response loop did run, it just didn't block STT.
    spoke = [e for e in bus.snapshot() if isinstance(e, AgentSpoke)]
    assert len(spoke) >= 1


# --- Johnny-di9: voice-triggered barge-in ----------------------------------


class _SlowFakeSTT(STTProvider):
    """``_FakeSTT`` with a configurable per-utterance sleep.

    Production STT calls take hundreds of milliseconds; the synchronous
    ``_FakeSTT`` is fast enough that the transcribe loop processes
    every utterance before the response loop has had a chance to pull
    the first one off the queue. For barge-in tests we want the
    response loop to be wedged in TTS *before* later transcripts
    finalise — a small per-utterance sleep gives the scheduler a
    natural interleave point so the timing matches production.
    """

    def __init__(self, transcripts: list[str], sleep_s: float = 0.02) -> None:
        self._transcripts = list(transcripts)
        self._idx = 0
        self.calls = 0
        self._sleep_s = sleep_s

    @property
    def name(self) -> str:
        return "slow-fake-stt"

    async def transcribe_stream(
        self,
        audio_iter: AsyncIterator[bytes],
    ) -> AsyncIterator[TranscriptEvent]:
        async for _ in audio_iter:
            pass
        await asyncio.sleep(self._sleep_s)
        if self._idx >= len(self._transcripts):
            text = "<exhausted>"
        else:
            text = self._transcripts[self._idx]
        self._idx += 1
        self.calls += 1
        yield TranscriptEvent(
            text=text,
            is_final=True,
            timestamp_ms=self.calls * 1000,
            confidence=0.9,
        )


class _SwitchingRouterLLM(LLMProvider):
    """Fake LLM that serves both router decisions and barge-in verdicts.

    The production pipeline reuses ``router_llm`` for the barge-in
    classifier so deployments stay single-knob; the test fake dispatches
    by the requested ``response_format`` so we can assert each path
    independently.
    """

    def __init__(
        self,
        router_decisions: list[dict[str, Any]],
        barge_in_decisions: list[dict[str, Any]] | None = None,
    ) -> None:
        self._router_decisions = list(router_decisions)
        self._barge_in_decisions = list(barge_in_decisions or [])
        self._router_idx = 0
        self._barge_in_idx = 0
        self.router_calls: list[Sequence[ChatMessage]] = []
        self.barge_in_calls: list[Sequence[ChatMessage]] = []
        self.last_messages: Sequence[ChatMessage] | None = None
        self.last_response_format: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return "switching-router"

    def _is_barge_in_format(
        self, response_format: dict[str, Any] | None
    ) -> bool:
        if not response_format:
            return False
        props = response_format.get("properties")
        if not isinstance(props, dict):
            return False
        return "should_interrupt" in props

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.last_messages = messages
        self.last_response_format = response_format

        if self._is_barge_in_format(response_format):
            self.barge_in_calls.append(list(messages))
            if not self._barge_in_decisions:
                payload = {
                    "should_interrupt": False,
                    "category": "noise",
                    "reason": "test default",
                }
            elif self._barge_in_idx >= len(self._barge_in_decisions):
                payload = self._barge_in_decisions[-1]
            else:
                payload = self._barge_in_decisions[self._barge_in_idx]
                self._barge_in_idx += 1
            return LLMResponse(
                text=json.dumps(payload),
                finish_reason="stop",
                structured_output=payload,
            )

        self.router_calls.append(list(messages))
        if self._router_idx >= len(self._router_decisions):
            decision = self._router_decisions[-1]
        else:
            decision = self._router_decisions[self._router_idx]
            self._router_idx += 1
        return LLMResponse(
            text=json.dumps(decision),
            finish_reason="stop",
            structured_output=decision,
        )


async def _wait_until(
    predicate: Any, timeout: float = 2.0, poll: float = 0.005
) -> None:
    """Poll ``predicate`` until it returns truthy or ``timeout`` elapses."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(poll)
    raise TimeoutError(f"predicate did not become truthy within {timeout}s")


def _frames_from_pcm(pcm: bytes, frame_size: int = 640) -> list[bytes]:
    return [
        pcm[i : i + frame_size]
        for i in range(0, len(pcm), frame_size)
        if i + frame_size <= len(pcm)
    ]


async def test_barge_in_stop_fires_interrupt_event(four_utterance_pcm: bytes) -> None:
    """Participant saying 'stop' while the bot is talking calls interrupt().

    Verifies the classifier-→-interrupt() wiring end-to-end: the test
    stalls the bot inside TTS so the response loop stays in-flight while
    a second transcript ('hey Johnny stop') finalises through the
    transcribe loop. The classifier must then run and flip the pipeline's
    ``_interrupt_event``. Actual audio-cut-on-interrupt behaviour is
    already proven by ``test_pipeline_interrupt_during_tts_truncates_audio``.
    """
    from johnny.voice_pipeline import InMemoryTranscriptSink

    transport = _BufferedTransport(frames=_frames_from_pcm(four_utterance_pcm))

    release_tts = asyncio.Event()
    tts_entered = asyncio.Event()

    class _StallingTTS(TTSProvider):
        def __init__(self) -> None:
            self.calls: list[str] = []

        @property
        def name(self) -> str:
            return "stalling-tts"

        async def synthesize_stream(
            self,
            text: str,
            voice_id: str | None = None,  # noqa: ARG002
        ) -> AsyncIterator[bytes]:
            self.calls.append(text)
            yield bytes(320)
            tts_entered.set()
            await release_tts.wait()

    stt = _SlowFakeSTT(transcripts=["hello team", "hey Johnny stop"])
    router = _SwitchingRouterLLM(
        router_decisions=[
            {
                "should_speak": True,
                "confidence": 0.95,
                "reason": "greeting",
                "suggested_reply": "Hi everyone",
            },
            {
                "should_speak": False,
                "confidence": 0.1,
                "reason": "stop command — nothing to say",
            },
        ],
        barge_in_decisions=[
            {
                "should_interrupt": True,
                "category": "stop",
                "reason": "user wants the bot to stop",
            },
        ],
    )
    bus = InMemoryEventBus()
    tsink = InMemoryTranscriptSink()
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=stt,
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["A long winded greeting reply."]),
        tts=_StallingTTS(),
        event_bus=bus,
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            session_id="barge-in-stop",
        ),
        transcript_sink=tsink,
    )

    run_task = asyncio.create_task(pipeline.run())

    # Wait until the bot is wedged inside TTS for the first utterance.
    await asyncio.wait_for(tts_entered.wait(), timeout=2.0)

    # The second transcript ("hey Johnny stop") must reach the
    # classifier and call interrupt() — verify by waiting until the
    # interrupt event flips. The classifier runs as a fire-and-forget
    # task so we poll for the side effect.
    await _wait_until(
        lambda: pipeline._interrupt_event.is_set(), timeout=2.0
    )

    # Release the stalling TTS so the run task can complete. Once
    # released, the next frame yield doesn't happen (the TTS generator
    # exits), so the response loop drains the rest of the queue.
    release_tts.set()
    await asyncio.wait_for(run_task, timeout=2.0)

    # Both transcripts should be persisted regardless — barge-in
    # categories (stop/correct/new_question) AND non-barge-in
    # categories (side_chat/noise) all land in the meeting history.
    persisted = [r.text for r in tsink.snapshot()]
    # The four_utterance_pcm fixture has 4 speech bursts; the
    # _FakeSTT seeds only 2 transcripts before falling back to
    # "<exhausted>". We assert the first two — the documented
    # transcripts — made it through.
    assert persisted[:2] == ["hello team", "hey Johnny stop"]

    # The barge-in classifier must have been called at least once
    # (for the second transcript while the bot was wedged in TTS).
    assert len(router.barge_in_calls) >= 1
    classifier_user_msg = router.barge_in_calls[0][1].content or ""
    assert "hey Johnny stop" in classifier_user_msg


@pytest.mark.parametrize(
    "category,should_interrupt",
    [
        ("stop", True),
        ("correct", True),
        ("new_question", True),
        ("side_chat", False),
        ("noise", False),
    ],
)
async def test_barge_in_category_drives_interrupt_decision(
    two_utterance_pcm: bytes,
    category: str,
    should_interrupt: bool,
) -> None:
    """Each classifier category maps to the documented interrupt behaviour."""
    transport = _BufferedTransport(frames=_frames_from_pcm(two_utterance_pcm))

    release_tts = asyncio.Event()
    tts_entered = asyncio.Event()

    class _StallingTTS(TTSProvider):
        @property
        def name(self) -> str:
            return "stalling-tts"

        async def synthesize_stream(
            self,
            text: str,  # noqa: ARG002
            voice_id: str | None = None,  # noqa: ARG002
        ) -> AsyncIterator[bytes]:
            yield bytes(320)
            tts_entered.set()
            await release_tts.wait()

    router = _SwitchingRouterLLM(
        router_decisions=[
            {"should_speak": True, "confidence": 0.95, "reason": "greet"},
            {"should_speak": False, "confidence": 0.1, "reason": "skip"},
        ],
        barge_in_decisions=[
            {
                "should_interrupt": should_interrupt,
                "category": category,
                "reason": f"test verdict: {category}",
            },
        ],
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_SlowFakeSTT(transcripts=["hi", "follow-up"]),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["a reply"]),
        tts=_StallingTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
    )

    run_task = asyncio.create_task(pipeline.run())
    await asyncio.wait_for(tts_entered.wait(), timeout=2.0)

    # Wait for the classifier task to fire at least once.
    await _wait_until(
        lambda: len(router.barge_in_calls) >= 1, timeout=2.0
    )
    # Give the verdict a beat to propagate through the gen guard +
    # interrupt() call.
    await asyncio.sleep(0.05)
    if should_interrupt:
        await _wait_until(
            lambda: pipeline._interrupt_event.is_set(), timeout=1.0
        )
    assert pipeline._interrupt_event.is_set() is should_interrupt

    release_tts.set()
    await asyncio.wait_for(run_task, timeout=2.0)


async def test_barge_in_classifier_skipped_when_bot_idle(
    two_utterance_pcm: bytes,
) -> None:
    """Transcripts arriving while the bot is idle don't trigger classifier calls."""
    transport = _BufferedTransport(frames=_frames_from_pcm(two_utterance_pcm))
    router = _SwitchingRouterLLM(
        router_decisions=[
            {"should_speak": False, "confidence": 0.1, "reason": "skip"},
        ],
        # Don't seed any barge-in verdicts — if the classifier IS
        # called, the default falls back to no-interrupt but we still
        # want to assert no calls were made.
        barge_in_decisions=[],
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["one", "two"]),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
    )
    await pipeline.run()

    # Bot decided not to speak (should_speak=False) — no answer ever
    # started, so each subsequent transcript saw _response_in_flight
    # cycle (briefly true → false) between transcripts. The router
    # call for transcript 2 happens after transcript 1's response loop
    # completes naturally, so the classifier never sees in_flight=True.
    # The exact race depends on scheduling, but barge_in_calls should
    # remain zero because the response loop never blocks long enough
    # for transcript 2 to arrive while in-flight.
    assert router.barge_in_calls == []


async def test_barge_in_disabled_by_config(
    four_utterance_pcm: bytes,
) -> None:
    """enable_barge_in=False suppresses the classifier even when bot is busy."""
    transport = _BufferedTransport(frames=_frames_from_pcm(four_utterance_pcm))

    release_tts = asyncio.Event()
    tts_entered = asyncio.Event()

    class _StallingTTS(TTSProvider):
        @property
        def name(self) -> str:
            return "stalling-tts"

        async def synthesize_stream(
            self,
            text: str,  # noqa: ARG002
            voice_id: str | None = None,  # noqa: ARG002
        ) -> AsyncIterator[bytes]:
            yield bytes(320)
            tts_entered.set()
            await release_tts.wait()

    router = _SwitchingRouterLLM(
        router_decisions=[
            {"should_speak": True, "confidence": 0.95, "reason": "ok"},
            {"should_speak": False, "confidence": 0.1, "reason": "skip"},
        ],
        barge_in_decisions=[
            {"should_interrupt": True, "category": "stop", "reason": "would interrupt"},
        ],
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_SlowFakeSTT(
            transcripts=["hi", "stop please", "and another", "fourth"]
        ),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["reply"]),
        tts=_StallingTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            enable_barge_in=False,
        ),
    )

    run_task = asyncio.create_task(pipeline.run())
    await asyncio.wait_for(tts_entered.wait(), timeout=2.0)
    # Let transcripts 2-4 arrive while bot is wedged.
    await asyncio.sleep(0.2)

    # Classifier must not be called at all.
    assert router.barge_in_calls == []
    # And interrupt must not have fired.
    assert pipeline._interrupt_event.is_set() is False

    release_tts.set()
    await asyncio.wait_for(run_task, timeout=2.0)


@pytest.mark.parametrize(
    "mode,speak",
    [
        ("listen_only", True),
        ("suggest_only", True),
        ("limited_auto_speak", False),
    ],
)
async def test_barge_in_skipped_in_non_speaking_modes(
    four_utterance_pcm: bytes,
    mode: str,
    speak: bool,
) -> None:
    """Non-speaking modes don't run the classifier — interrupt would no-op."""
    transport = _BufferedTransport(frames=_frames_from_pcm(four_utterance_pcm))
    router = _SwitchingRouterLLM(
        router_decisions=[
            {"should_speak": True, "confidence": 0.95, "reason": "ok"},
        ],
        barge_in_decisions=[
            {"should_interrupt": True, "category": "stop", "reason": "x"},
        ],
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["one", "two", "three", "four"]),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["reply"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            mode=mode,
            speak=speak,
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
    )
    await pipeline.run()

    assert router.barge_in_calls == []
    assert pipeline._interrupt_event.is_set() is False


async def test_barge_in_classifier_prompt_includes_bot_context(
    four_utterance_pcm: bytes,
) -> None:
    """The classifier prompt names the bot's role and offers the last suggested reply."""
    transport = _BufferedTransport(frames=_frames_from_pcm(four_utterance_pcm))

    release_tts = asyncio.Event()
    tts_entered = asyncio.Event()

    class _StallingTTS(TTSProvider):
        @property
        def name(self) -> str:
            return "stalling-tts"

        async def synthesize_stream(
            self,
            text: str,  # noqa: ARG002
            voice_id: str | None = None,  # noqa: ARG002
        ) -> AsyncIterator[bytes]:
            yield bytes(320)
            tts_entered.set()
            await release_tts.wait()

    router = _SwitchingRouterLLM(
        router_decisions=[
            {
                "should_speak": True,
                "confidence": 0.95,
                "reason": "ok",
                "suggested_reply": "Talking about the Q3 roadmap",
            },
        ],
        barge_in_decisions=[
            {"should_interrupt": False, "category": "noise", "reason": "cough"},
        ],
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_SlowFakeSTT(
            transcripts=["topic please", "cough cough", "x", "y"]
        ),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["the long reply"]),
        tts=_StallingTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            instructions="Speak only about engineering",
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
    )

    run_task = asyncio.create_task(pipeline.run())
    await asyncio.wait_for(tts_entered.wait(), timeout=2.0)
    await _wait_until(lambda: len(router.barge_in_calls) >= 1, timeout=2.0)

    classifier_msgs = router.barge_in_calls[0]
    system_msg = classifier_msgs[0]
    user_msg = classifier_msgs[1]
    assert system_msg.role == "system"
    assert system_msg.content is not None
    # The system prompt anchors the bot's role.
    assert "barge-in" in system_msg.content.lower()
    # Categories must be listed so the classifier knows what to return.
    assert "stop" in system_msg.content
    assert "correct" in system_msg.content
    assert "new_question" in system_msg.content
    assert "side_chat" in system_msg.content
    assert "noise" in system_msg.content
    # Meeting instructions flow through so the classifier can tell
    # on-topic from off-topic correction attempts.
    assert "Speak only about engineering" in system_msg.content

    assert user_msg.role == "user"
    assert user_msg.content is not None
    # User message includes the bot's last suggested reply for context.
    assert "Q3 roadmap" in user_msg.content
    # And the actual participant transcript being classified.
    assert "cough cough" in user_msg.content

    release_tts.set()
    await asyncio.wait_for(run_task, timeout=2.0)


async def test_barge_in_classifier_failure_does_not_interrupt(
    four_utterance_pcm: bytes,
) -> None:
    """An exception in the classifier leaves the bot running (safe default)."""
    transport = _BufferedTransport(frames=_frames_from_pcm(four_utterance_pcm))

    release_tts = asyncio.Event()
    tts_entered = asyncio.Event()

    class _StallingTTS(TTSProvider):
        @property
        def name(self) -> str:
            return "stalling-tts"

        async def synthesize_stream(
            self,
            text: str,  # noqa: ARG002
            voice_id: str | None = None,  # noqa: ARG002
        ) -> AsyncIterator[bytes]:
            yield bytes(320)
            tts_entered.set()
            await release_tts.wait()

    class _BrokenClassifierRouter(_SwitchingRouterLLM):
        async def chat(
            self,
            messages: Sequence[ChatMessage],
            tools: Sequence[ToolDefinition] | None = None,
            response_format: dict[str, Any] | None = None,
        ) -> LLMResponse:
            if self._is_barge_in_format(response_format):
                self.barge_in_calls.append(list(messages))
                raise RuntimeError("classifier upstream failure")
            return await super().chat(messages, tools, response_format)

    router = _BrokenClassifierRouter(
        router_decisions=[
            {"should_speak": True, "confidence": 0.95, "reason": "ok"},
        ],
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_SlowFakeSTT(
            transcripts=["hi", "another", "three", "four"]
        ),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["reply"]),
        tts=_StallingTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            # Pin classifier-only behaviour: this test exists to prove
            # that a broken CLASSIFIER doesn't take down the bot. The
            # Johnny-ze3 fast (VAD-driven) path is a *separate* interrupt
            # source that bypasses the classifier entirely, so we disable
            # it here to keep the assertion focused on classifier-fail
            # semantics. (Fast-path coverage lives in
            # test_fast_barge_in_*.)
            barge_in_min_speech_ms=0,
        ),
    )

    run_task = asyncio.create_task(pipeline.run())
    await asyncio.wait_for(tts_entered.wait(), timeout=2.0)
    # Wait for at least one classifier failure to log.
    await _wait_until(lambda: len(router.barge_in_calls) >= 1, timeout=2.0)
    await asyncio.sleep(0.05)

    # Despite the classifier crashing, the bot keeps going.
    assert pipeline._interrupt_event.is_set() is False

    release_tts.set()
    await asyncio.wait_for(run_task, timeout=2.0)


def test_pipeline_config_barge_in_classifier_timeout_default() -> None:
    """Johnny-wyd: classifier wall-clock timeout is bounded by default.

    Acceptance #2 (single-line WARN on timeout, no traceback) only kicks
    in once :meth:`VoicePipeline._classify_barge_in_intent` actually
    bounds the upstream call. Pinning the default here prevents a future
    refactor from silently zeroing the field and re-introducing the
    original behaviour where the classifier waited the full provider
    httpx timeout (60 s default) before failing.
    """
    cfg = PipelineConfig()
    assert cfg.barge_in_classifier_timeout_s == 5.0


async def test_barge_in_classifier_timeout_logs_warning_single_line(
    four_utterance_pcm: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Johnny-wyd: classifier wall-clock timeout logs WARN, not exception().

    The bead's acceptance #2: "Worker log noise drops to a single WARN
    line on classifier-timeout, not a 30-line traceback." This test
    pins both halves of the contract:

    * the classifier-timeout path emits a ``WARNING`` record (not
      ``ERROR``) for the matching message; and
    * the record carries no ``exc_info`` payload, so the structured
      log handler in production won't render a multi-line stack frame.
    """
    import logging

    caplog.set_level(logging.WARNING, logger="johnny.voice_pipeline.pipeline")

    transport = _BufferedTransport(frames=_frames_from_pcm(four_utterance_pcm))

    release_tts = asyncio.Event()
    tts_entered = asyncio.Event()

    class _StallingTTS(TTSProvider):
        @property
        def name(self) -> str:
            return "stalling-tts"

        async def synthesize_stream(
            self,
            text: str,  # noqa: ARG002
            voice_id: str | None = None,  # noqa: ARG002
        ) -> AsyncIterator[bytes]:
            yield bytes(320)
            tts_entered.set()
            await release_tts.wait()

    class _SlowClassifierRouter(_SwitchingRouterLLM):
        """Classifier path blocks far longer than the wall-clock budget.

        Simulates the 35B Qwen model in the bead: the upstream LLM
        does eventually return, but only after the configured wall-
        clock budget — so :meth:`_classify_barge_in_intent` MUST
        raise :class:`TimeoutError` and never see the late response.
        """

        async def chat(
            self,
            messages: Sequence[ChatMessage],
            tools: Sequence[ToolDefinition] | None = None,
            response_format: dict[str, Any] | None = None,
        ) -> LLMResponse:
            if self._is_barge_in_format(response_format):
                self.barge_in_calls.append(list(messages))
                # Far longer than the test's classifier timeout below.
                await asyncio.sleep(2.0)
                payload = {
                    "should_interrupt": True,
                    "category": "stop",
                    "reason": "late verdict — must be discarded",
                }
                return LLMResponse(
                    text=json.dumps(payload),
                    finish_reason="stop",
                    structured_output=payload,
                )
            return await super().chat(messages, tools, response_format)

    router = _SlowClassifierRouter(
        router_decisions=[
            {"should_speak": True, "confidence": 0.95, "reason": "ok"},
        ],
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_SlowFakeSTT(transcripts=["hi", "another", "three", "four"]),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["reply"]),
        tts=_StallingTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            session_id="bin-timeout",
            # Tight timeout so the test resolves quickly. The classifier
            # sleeps 2.0 s above; 0.05 s is well below that so wait_for
            # always fires first.
            barge_in_classifier_timeout_s=0.05,
            # Pin classifier-only behaviour — the fast VAD path is a
            # separate interrupt source. Disabling it keeps the
            # assertion focused on the slow-path timeout semantics.
            barge_in_min_speech_ms=0,
        ),
    )

    run_task = asyncio.create_task(pipeline.run())
    await asyncio.wait_for(tts_entered.wait(), timeout=2.0)
    # Wait for the classifier to enter and time out.
    await _wait_until(lambda: len(router.barge_in_calls) >= 1, timeout=2.0)
    # Give the wait_for + WARN log a moment to fire.
    await asyncio.sleep(0.2)

    # The classifier timed out — interrupt MUST NOT have fired (the
    # late "stop" verdict is discarded).
    assert pipeline._interrupt_event.is_set() is False

    timeout_records = [
        rec
        for rec in caplog.records
        if "classifier timed out" in rec.getMessage()
    ]
    assert timeout_records, (
        "expected a WARN log for the classifier timeout; "
        f"saw {[rec.getMessage() for rec in caplog.records]!r}"
    )
    rec = timeout_records[0]
    assert rec.levelno == logging.WARNING, (
        f"classifier timeout should log at WARNING, got {rec.levelname}"
    )
    # ``logger.warning(...)`` without exc_info=True keeps the record's
    # exc_info empty — that's how we know production won't render a
    # 30-line traceback for this case (the noise the bead names).
    assert rec.exc_info is None, (
        "classifier-timeout log line must not carry a traceback; "
        "use WARN single-line, not exception()"
    )
    assert "bin-timeout" in rec.getMessage(), (
        f"timeout log must name the session for grep; got {rec.getMessage()!r}"
    )

    release_tts.set()
    await asyncio.wait_for(run_task, timeout=2.0)


async def test_barge_in_classifier_timeout_disabled_when_zero(
    four_utterance_pcm: bytes,
) -> None:
    """barge_in_classifier_timeout_s <= 0 keeps the wait_for bound off (Johnny-wyd).

    Operators that want to inherit the provider's own HTTP timeout (or
    a different upstream cap) can opt out. The classifier call then
    runs to completion regardless of how long it takes — useful for
    tests that exercise other paths and for deployments wiring a small
    bespoke model.
    """
    transport = _BufferedTransport(frames=_frames_from_pcm(four_utterance_pcm))

    release_tts = asyncio.Event()
    tts_entered = asyncio.Event()

    class _StallingTTS(TTSProvider):
        @property
        def name(self) -> str:
            return "stalling-tts"

        async def synthesize_stream(
            self,
            text: str,  # noqa: ARG002
            voice_id: str | None = None,  # noqa: ARG002
        ) -> AsyncIterator[bytes]:
            yield bytes(320)
            tts_entered.set()
            await release_tts.wait()

    router = _SwitchingRouterLLM(
        router_decisions=[
            {"should_speak": True, "confidence": 0.95, "reason": "ok"},
        ],
        barge_in_decisions=[
            {
                "should_interrupt": True,
                "category": "stop",
                "reason": "user said stop",
            },
        ],
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_SlowFakeSTT(transcripts=["hi", "another", "three", "four"]),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["reply"]),
        tts=_StallingTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            # Disable the wall-clock bound entirely.
            barge_in_classifier_timeout_s=0.0,
            barge_in_min_speech_ms=0,
        ),
    )

    run_task = asyncio.create_task(pipeline.run())
    await asyncio.wait_for(tts_entered.wait(), timeout=2.0)
    # With the bound disabled, the (instantaneous) classifier still
    # fires its verdict — interrupt MUST flip.
    await _wait_until(
        lambda: pipeline._interrupt_event.is_set(), timeout=2.0
    )

    release_tts.set()
    await asyncio.wait_for(run_task, timeout=2.0)


async def test_barge_in_stale_verdict_does_not_interrupt_next_response(
    two_utterance_pcm: bytes,
) -> None:
    """Classifier verdict arriving after the response generation moved on is dropped.

    A delayed classifier captures gen=N, but by the time its verdict
    returns the response loop is on gen=N+1 (the original response
    completed naturally). The generation guard MUST drop the late
    interrupt — without it, the user's NEW response would be aborted
    by a verdict that was meant for the PREVIOUS one.
    """
    transport = _BufferedTransport(frames=_frames_from_pcm(two_utterance_pcm))

    classifier_gate = asyncio.Event()
    classifier_entered = asyncio.Event()
    captured_gen: list[int] = []

    class _DelayedClassifierRouter(_SwitchingRouterLLM):
        async def chat(
            self,
            messages: Sequence[ChatMessage],
            tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
            response_format: dict[str, Any] | None = None,
        ) -> LLMResponse:
            if self._is_barge_in_format(response_format):
                self.barge_in_calls.append(list(messages))
                captured_gen.append(pipeline._response_generation)
                classifier_entered.set()
                # Hold the verdict until the test opens the gate.
                await classifier_gate.wait()
                payload = {
                    "should_interrupt": True,
                    "category": "stop",
                    "reason": "stale verdict — must not fire",
                }
                return LLMResponse(
                    text=json.dumps(payload),
                    finish_reason="stop",
                    structured_output=payload,
                )
            return await super().chat(messages, tools, response_format)

    release_first_tts = asyncio.Event()
    first_tts_entered = asyncio.Event()
    second_tts_entered = asyncio.Event()
    release_second_tts = asyncio.Event()
    tts_call_count = 0

    class _GatedTTS(TTSProvider):
        @property
        def name(self) -> str:
            return "gated-tts"

        async def synthesize_stream(
            self,
            text: str,  # noqa: ARG002
            voice_id: str | None = None,  # noqa: ARG002
        ) -> AsyncIterator[bytes]:
            nonlocal tts_call_count
            tts_call_count += 1
            if tts_call_count == 1:
                yield bytes(320)
                first_tts_entered.set()
                await release_first_tts.wait()
            else:
                yield bytes(320)
                second_tts_entered.set()
                await release_second_tts.wait()

    router = _DelayedClassifierRouter(
        router_decisions=[
            {"should_speak": True, "confidence": 0.95, "reason": "ok-1"},
            {"should_speak": True, "confidence": 0.95, "reason": "ok-2"},
        ],
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_SlowFakeSTT(transcripts=["first turn", "second turn"]),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["reply"]),
        tts=_GatedTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
    )

    run_task = asyncio.create_task(pipeline.run())

    # Wait for the bot to start its first response.
    await asyncio.wait_for(first_tts_entered.wait(), timeout=2.0)
    # Wait for the classifier (for transcript 2) to ENTER the gate so
    # we know it captured the first response's generation.
    await asyncio.wait_for(classifier_entered.wait(), timeout=2.0)
    assert len(captured_gen) == 1, (
        f"expected exactly one classifier call (for utterance 2), "
        f"got {len(captured_gen)} (gens: {captured_gen})"
    )
    gen_when_classifier_started = captured_gen[0]

    # Release the first TTS so the response loop completes utterance 1.
    # The classifier is still held by the gate.
    release_first_tts.set()
    # Wait for the SECOND response to start.
    await asyncio.wait_for(second_tts_entered.wait(), timeout=2.0)
    assert pipeline._response_generation > gen_when_classifier_started, (
        f"expected generation to advance past {gen_when_classifier_started}, "
        f"but current is {pipeline._response_generation}"
    )

    # Release the classifier. Its captured gen is now stale —
    # gen_when_classifier_started < pipeline._response_generation —
    # so the gen guard MUST drop the interrupt.
    classifier_gate.set()

    # Give the verdict a chance to (incorrectly) fire.
    await asyncio.sleep(0.1)

    assert pipeline._interrupt_event.is_set() is False, (
        "stale classifier verdict aborted a response it was not meant to"
    )

    # Let the second TTS finish so the pipeline can drain.
    release_second_tts.set()
    await asyncio.wait_for(run_task, timeout=3.0)


# --- Johnny-ze3: fast (VAD-driven) barge-in -------------------------------


def _make_test_pipeline(
    *,
    pcm: bytes,
    enable_barge_in: bool = True,
    barge_in_min_speech_ms: int = 160,
    mode: str = "limited_auto_speak",
    speak: bool = True,
    stalling_tts: TTSProvider | None = None,
) -> tuple[VoicePipeline, _BufferedTransport, _SwitchingRouterLLM]:
    """Wire a pipeline for fast-barge-in tests.

    The stalling TTS keeps the bot wedged in flight so that any speech
    onset on a *later* utterance lands while ``_response_in_flight`` is
    True — without that the fast path is never reachable. Tests that need
    custom TTS pass their own; others get a default that yields one frame
    and then waits forever (the caller releases via cancellation).
    """
    transport = _BufferedTransport(frames=_frames_from_pcm(pcm))

    class _DefaultStallingTTS(TTSProvider):
        @property
        def name(self) -> str:
            return "default-stalling"

        async def synthesize_stream(
            self,
            text: str,  # noqa: ARG002
            voice_id: str | None = None,  # noqa: ARG002
        ) -> AsyncIterator[bytes]:
            yield bytes(320)
            await asyncio.Event().wait()

    tts: TTSProvider = stalling_tts if stalling_tts is not None else _DefaultStallingTTS()
    router = _SwitchingRouterLLM(
        router_decisions=[
            {"should_speak": True, "confidence": 0.95, "reason": "ok"},
            {"should_speak": False, "confidence": 0.1, "reason": "skip"},
        ],
        # Default classifier verdict is no-interrupt so any rise in
        # _interrupt_event MUST come from the fast path.
        barge_in_decisions=[
            {"should_interrupt": False, "category": "noise", "reason": "test default"},
        ],
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_SlowFakeSTT(transcripts=["one", "two", "three", "four"]),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["reply"]),
        tts=tts,
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            enable_barge_in=enable_barge_in,
            barge_in_min_speech_ms=barge_in_min_speech_ms,
            mode=mode,
            speak=speak,
        ),
    )
    return pipeline, transport, router


async def test_fast_barge_in_default_threshold_is_160ms() -> None:
    """The config default lines up with the documented ~200 ms latency target."""
    from johnny.voice_pipeline.pipeline import DEFAULT_BARGE_IN_MIN_SPEECH_MS

    assert PipelineConfig().barge_in_min_speech_ms == 160
    assert DEFAULT_BARGE_IN_MIN_SPEECH_MS == 160


async def test_fast_barge_in_threshold_frames_handles_zero_and_division() -> None:
    """Threshold computation: 0 → disabled, otherwise ceil-style frame count."""
    from app.providers import (
        LLMProvider,
        STTProvider,
        TTSProvider,
    )

    class _Stub(LLMProvider, STTProvider, TTSProvider):
        @property
        def name(self) -> str:
            return "stub"

        async def chat(
            self,
            messages: Sequence[ChatMessage],
            tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
            response_format: dict[str, Any] | None = None,  # noqa: ARG002
        ) -> LLMResponse:
            return LLMResponse(text="", finish_reason="stop")

        async def transcribe_stream(
            self, audio_iter: AsyncIterator[bytes]
        ) -> AsyncIterator[TranscriptEvent]:
            async for _ in audio_iter:
                pass
            if False:
                yield  # pragma: no cover

        async def synthesize_stream(
            self,
            text: str,  # noqa: ARG002
            voice_id: str | None = None,  # noqa: ARG002
        ) -> AsyncIterator[bytes]:
            if False:
                yield  # pragma: no cover

    stub = _Stub()
    transport = _BufferedTransport(frames=[])

    for ms, frame_ms, expected_frames in [
        (160, 20, 8),
        (200, 20, 10),
        (60, 20, 3),
        (0, 20, 0),
        (-5, 20, 0),
        # Very small ms relative to frame size: at least 1 frame.
        (10, 20, 1),
    ]:
        pipeline = VoicePipeline(
            transport=transport,
            vad=EnergyVAD(threshold=0.5),
            stt=stub,
            router_llm=stub,
            answer_llm=stub,
            tts=stub,
            event_bus=InMemoryEventBus(),
            config=PipelineConfig(
                barge_in_min_speech_ms=ms,
                frame_duration_ms=frame_ms,
            ),
        )
        assert pipeline._fast_barge_in_threshold_frames() == expected_frames, (
            f"ms={ms} frame_ms={frame_ms}"
        )


async def test_fast_barge_in_should_fire_predicate_matches_classifier_gates(
    two_utterance_pcm: bytes,
) -> None:
    """Fast and slow paths share the same gating conditions.

    Both must be governed by ``enable_barge_in`` + ``_response_in_flight``
    + ``speak`` + ``mode in SPEAKING_MODES``; operators must never wind
    up with one path firing while the other is muted.
    """
    pipeline, _, _ = _make_test_pipeline(pcm=two_utterance_pcm)

    # Default state: barge-in enabled, but bot is not in flight yet.
    assert pipeline._should_fast_barge_in() is False
    assert pipeline._should_classify_barge_in() is False

    # Flip in-flight on; both predicates flip together.
    pipeline._response_in_flight = True
    assert pipeline._should_fast_barge_in() is True
    assert pipeline._should_classify_barge_in() is True


async def test_fast_barge_in_fires_during_bot_response(
    four_utterance_pcm: bytes,
) -> None:
    """Speech onset while the bot is responding sets _interrupt_event.

    Drives the full pipeline with the bot stalled in TTS during the
    first response; the second / third utterance's speech frames must
    trigger the fast path well before the classifier returns. Asserts
    the observability counter ticks and the interrupt fires WITHOUT
    waiting for any classifier verdict.
    """
    tts_entered = asyncio.Event()
    release_tts = asyncio.Event()

    class _StallingTTS(TTSProvider):
        @property
        def name(self) -> str:
            return "stalling"

        async def synthesize_stream(
            self,
            text: str,  # noqa: ARG002
            voice_id: str | None = None,  # noqa: ARG002
        ) -> AsyncIterator[bytes]:
            yield bytes(320)
            tts_entered.set()
            await release_tts.wait()

    pipeline, _, router = _make_test_pipeline(
        pcm=four_utterance_pcm,
        # Classifier verdict is no-interrupt (set by default in
        # _make_test_pipeline). Any rise in _interrupt_event is therefore
        # attributable to the fast path alone.
        stalling_tts=_StallingTTS(),
    )

    run_task = asyncio.create_task(pipeline.run())
    try:
        await asyncio.wait_for(tts_entered.wait(), timeout=2.0)
        await _wait_until(
            lambda: pipeline._fast_barge_in_count >= 1, timeout=2.0
        )
        # Interrupt event MUST be set by the fast path, not by the
        # (no-interrupt) classifier. The classifier may also have run
        # by now but its verdict is no-interrupt.
        assert pipeline._interrupt_event.is_set() is True
        if router.barge_in_calls:
            verdict = router._barge_in_decisions[0]
            assert verdict["should_interrupt"] is False
    finally:
        release_tts.set()
        await asyncio.wait_for(run_task, timeout=3.0)


async def test_fast_barge_in_does_not_fire_when_bot_idle(
    two_utterance_pcm: bytes,
) -> None:
    """No bot response → no interrupt, even when participants are speaking."""
    transport = _BufferedTransport(frames=_frames_from_pcm(two_utterance_pcm))
    router = _SwitchingRouterLLM(
        # Bot decides NOT to speak so _response_in_flight stays brief
        # (just for the router stage) and never reaches TTS.
        router_decisions=[
            {"should_speak": False, "confidence": 0.1, "reason": "skip"},
            {"should_speak": False, "confidence": 0.1, "reason": "skip"},
        ],
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["one", "two"]),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
    )
    await pipeline.run()
    assert pipeline._fast_barge_in_count == 0
    assert pipeline._interrupt_event.is_set() is False


async def test_fast_barge_in_disabled_via_min_speech_ms_zero(
    four_utterance_pcm: bytes,
) -> None:
    """barge_in_min_speech_ms=0 disables the fast path entirely.

    The bot stays wedged in TTS for the duration of the test — any
    interrupt rises must come from the classifier (which we leave
    on no-interrupt to keep the assertion clean).
    """
    tts_entered = asyncio.Event()
    release_tts = asyncio.Event()

    class _StallingTTS(TTSProvider):
        @property
        def name(self) -> str:
            return "stalling"

        async def synthesize_stream(
            self,
            text: str,  # noqa: ARG002
            voice_id: str | None = None,  # noqa: ARG002
        ) -> AsyncIterator[bytes]:
            yield bytes(320)
            tts_entered.set()
            await release_tts.wait()

    pipeline, _, router = _make_test_pipeline(
        pcm=four_utterance_pcm,
        barge_in_min_speech_ms=0,
        stalling_tts=_StallingTTS(),
    )

    run_task = asyncio.create_task(pipeline.run())
    try:
        await asyncio.wait_for(tts_entered.wait(), timeout=2.0)
        # Wait long enough that any fast-path firing would have happened.
        await _wait_until(
            lambda: len(router.barge_in_calls) >= 1, timeout=2.0
        )
        await asyncio.sleep(0.1)
        assert pipeline._fast_barge_in_count == 0
        assert pipeline._interrupt_event.is_set() is False
    finally:
        release_tts.set()
        await asyncio.wait_for(run_task, timeout=3.0)


async def test_fast_barge_in_respects_enable_barge_in_flag(
    four_utterance_pcm: bytes,
) -> None:
    """enable_barge_in=False suppresses BOTH the classifier and the fast path."""
    tts_entered = asyncio.Event()
    release_tts = asyncio.Event()

    class _StallingTTS(TTSProvider):
        @property
        def name(self) -> str:
            return "stalling"

        async def synthesize_stream(
            self,
            text: str,  # noqa: ARG002
            voice_id: str | None = None,  # noqa: ARG002
        ) -> AsyncIterator[bytes]:
            yield bytes(320)
            tts_entered.set()
            await release_tts.wait()

    pipeline, _, router = _make_test_pipeline(
        pcm=four_utterance_pcm,
        enable_barge_in=False,
        stalling_tts=_StallingTTS(),
    )

    run_task = asyncio.create_task(pipeline.run())
    try:
        await asyncio.wait_for(tts_entered.wait(), timeout=2.0)
        # Give the pipeline a chance to (in)correctly fire the fast path.
        await asyncio.sleep(0.3)
        assert pipeline._fast_barge_in_count == 0
        assert pipeline._interrupt_event.is_set() is False
        # Classifier must also be off.
        assert router.barge_in_calls == []
    finally:
        release_tts.set()
        await asyncio.wait_for(run_task, timeout=3.0)


@pytest.mark.parametrize(
    "mode",
    ["listen_only", "suggest_only"],
)
async def test_fast_barge_in_skipped_in_non_speaking_modes(
    four_utterance_pcm: bytes,
    mode: str,
) -> None:
    """Non-speaking modes never call interrupt() because there's nothing to cut."""
    pipeline, _, _ = _make_test_pipeline(
        pcm=four_utterance_pcm,
        mode=mode,
    )
    # No TTS to stall — non-speaking modes don't run TTS. Pipeline runs
    # to completion on its own once the transport's frames are drained.
    await asyncio.wait_for(pipeline.run(), timeout=3.0)
    assert pipeline._fast_barge_in_count == 0
    assert pipeline._interrupt_event.is_set() is False


async def test_fast_barge_in_skipped_when_speak_false(
    four_utterance_pcm: bytes,
) -> None:
    """speak=False is the legacy listen-only equivalent — no interrupt path."""
    pipeline, _, _ = _make_test_pipeline(
        pcm=four_utterance_pcm,
        speak=False,
    )
    await asyncio.wait_for(pipeline.run(), timeout=3.0)
    assert pipeline._fast_barge_in_count == 0
    assert pipeline._interrupt_event.is_set() is False


async def test_fast_barge_in_fires_at_most_once_per_utterance(
    four_utterance_pcm: bytes,
) -> None:
    """A long sustained speech burst only fires the interrupt once.

    Without the per-utterance one-shot flag, every speech frame past the
    threshold would re-fire interrupt(), spamming the observability
    counter and the log.
    """
    tts_entered = asyncio.Event()
    release_tts = asyncio.Event()

    class _StallingTTS(TTSProvider):
        @property
        def name(self) -> str:
            return "stalling"

        async def synthesize_stream(
            self,
            text: str,  # noqa: ARG002
            voice_id: str | None = None,  # noqa: ARG002
        ) -> AsyncIterator[bytes]:
            yield bytes(320)
            tts_entered.set()
            await release_tts.wait()

    pipeline, _, _ = _make_test_pipeline(
        pcm=four_utterance_pcm,
        stalling_tts=_StallingTTS(),
    )

    run_task = asyncio.create_task(pipeline.run())
    try:
        await asyncio.wait_for(tts_entered.wait(), timeout=2.0)
        await _wait_until(
            lambda: pipeline._fast_barge_in_count >= 1, timeout=2.0
        )
        # Even after the fast path has fired and time has passed, the
        # counter should advance no more than once per utterance burst:
        # four bursts in the fixture, so at most four total fires.
        await asyncio.sleep(0.2)
        assert 1 <= pipeline._fast_barge_in_count <= 4
    finally:
        release_tts.set()
        await asyncio.wait_for(run_task, timeout=3.0)


async def test_fast_barge_in_does_not_fire_for_brief_speech_below_threshold(
    tmp_path: Path,
) -> None:
    """A burst shorter than barge_in_min_speech_ms must NOT cut the bot.

    Synthesises a short tone (~80 ms) — half the default threshold — so
    a cough-equivalent is filtered out even when the bot is in flight.
    """
    from tests.voice_pipeline.conftest import (
        _read_wav_pcm,
        _silence_samples,
        _tone_samples,
        _write_wav,
    )

    samples: list[int] = []
    samples.extend(_silence_samples(200))
    samples.extend(_tone_samples(600))  # utterance 1 (long → bot starts responding)
    samples.extend(_silence_samples(800))
    samples.extend(_tone_samples(80))  # brief tone (below 160 ms threshold)
    samples.extend(_silence_samples(800))
    wav_path = tmp_path / "brief_burst.wav"
    _write_wav(wav_path, samples)
    pcm = _read_wav_pcm(wav_path)

    tts_entered = asyncio.Event()
    release_tts = asyncio.Event()

    class _StallingTTS(TTSProvider):
        @property
        def name(self) -> str:
            return "stalling"

        async def synthesize_stream(
            self,
            text: str,  # noqa: ARG002
            voice_id: str | None = None,  # noqa: ARG002
        ) -> AsyncIterator[bytes]:
            yield bytes(320)
            tts_entered.set()
            await release_tts.wait()

    pipeline, _, _ = _make_test_pipeline(
        pcm=pcm,
        stalling_tts=_StallingTTS(),
    )

    run_task = asyncio.create_task(pipeline.run())
    try:
        await asyncio.wait_for(tts_entered.wait(), timeout=2.0)
        # Let all the audio drain through VAD.
        await asyncio.sleep(0.3)
        # The 80 ms burst is below the default 160 ms threshold so the
        # fast path stays at zero.
        assert pipeline._fast_barge_in_count == 0
        assert pipeline._interrupt_event.is_set() is False
    finally:
        release_tts.set()
        await asyncio.wait_for(run_task, timeout=3.0)


async def test_fast_barge_in_log_line_includes_session_id(
    four_utterance_pcm: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Production diagnoses need a single grep-friendly log per fire.

    The log must include the session id and the configured threshold so
    operators can correlate a fast-barge-in with the rest of the session
    timeline without spelunking through multiple lines.
    """
    import logging

    caplog.set_level(logging.INFO, logger="johnny.voice_pipeline.pipeline")

    tts_entered = asyncio.Event()
    release_tts = asyncio.Event()

    class _StallingTTS(TTSProvider):
        @property
        def name(self) -> str:
            return "stalling"

        async def synthesize_stream(
            self,
            text: str,  # noqa: ARG002
            voice_id: str | None = None,  # noqa: ARG002
        ) -> AsyncIterator[bytes]:
            yield bytes(320)
            tts_entered.set()
            await release_tts.wait()

    transport = _BufferedTransport(frames=_frames_from_pcm(four_utterance_pcm))
    router = _SwitchingRouterLLM(
        router_decisions=[
            {"should_speak": True, "confidence": 0.95, "reason": "ok"},
            {"should_speak": False, "confidence": 0.1, "reason": "skip"},
        ],
        barge_in_decisions=[
            {"should_interrupt": False, "category": "noise", "reason": "test"},
        ],
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_SlowFakeSTT(transcripts=["alpha", "bravo", "charlie", "delta"]),
        router_llm=router,
        answer_llm=_FakeAnswerLLM(answers=["reply"]),
        tts=_StallingTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            session_id="session-test-fast-barge",
            # Pre-Johnny-ckz.14 behaviour — this test asserts on the
            # fast barge-in log line, not on the noise gate. Disabling
            # the gate keeps the synthetic single-burst transcripts
            # ('alpha', 'bravo', ...) flowing through the response loop
            # so the bot's TTS actually starts and gets interrupted.
            noise_filter_enabled=False,
        ),
    )

    run_task = asyncio.create_task(pipeline.run())
    try:
        await asyncio.wait_for(tts_entered.wait(), timeout=2.0)
        await _wait_until(
            lambda: pipeline._fast_barge_in_count >= 1, timeout=2.0
        )
    finally:
        release_tts.set()
        await asyncio.wait_for(run_task, timeout=3.0)

    fast_logs = [
        rec.getMessage()
        for rec in caplog.records
        if "fast barge-in fired" in rec.getMessage()
    ]
    assert fast_logs, "expected at least one fast-barge-in log line"
    # Single log per fire: session id and threshold both present so a
    # production grep can lift session timing without joining lines.
    assert "session-test-fast-barge" in fast_logs[0]
    assert "min_speech_ms=160" in fast_logs[0]


# --- _parse_barge_in_response unit tests ---------------------------------


def test_parse_barge_in_response_with_structured_output() -> None:
    from johnny.voice_pipeline.pipeline import _parse_barge_in_response

    resp = LLMResponse(
        text="",
        finish_reason="stop",
        structured_output={
            "should_interrupt": True,
            "category": "stop",
            "reason": "user said stop",
        },
    )
    d = _parse_barge_in_response(resp)
    assert d.should_interrupt is True
    assert d.category == "stop"
    assert d.reason == "user said stop"


def test_parse_barge_in_response_falls_back_to_text_json() -> None:
    from johnny.voice_pipeline.pipeline import _parse_barge_in_response

    resp = LLMResponse(
        text=(
            '{"should_interrupt": false, "category": "noise", '
            '"reason": "cough"}'
        ),
        finish_reason="stop",
    )
    d = _parse_barge_in_response(resp)
    assert d.should_interrupt is False
    assert d.category == "noise"


def test_parse_barge_in_response_no_structured_output_returns_safe_default() -> None:
    from johnny.voice_pipeline.pipeline import _parse_barge_in_response

    resp = LLMResponse(text="garbage not json", finish_reason="stop")
    d = _parse_barge_in_response(resp)
    assert d.should_interrupt is False
    assert d.category == "noise"


def test_parse_barge_in_response_unknown_category_defaults_to_noise() -> None:
    from johnny.voice_pipeline.pipeline import _parse_barge_in_response

    resp = LLMResponse(
        text="",
        finish_reason="stop",
        structured_output={
            "should_interrupt": True,
            "category": "interrupt_immediately",  # not in BARGE_IN_CATEGORIES
            "reason": "x",
        },
    )
    d = _parse_barge_in_response(resp)
    assert d.category == "noise"
    # Unknown category cannot fire an interrupt.
    assert d.should_interrupt is False


def test_parse_barge_in_response_should_interrupt_downgraded_for_noise() -> None:
    """A buggy classifier saying noise+interrupt is downgraded to no-interrupt."""
    from johnny.voice_pipeline.pipeline import _parse_barge_in_response

    resp = LLMResponse(
        text="",
        finish_reason="stop",
        structured_output={
            "should_interrupt": True,
            "category": "noise",
            "reason": "bug",
        },
    )
    d = _parse_barge_in_response(resp)
    assert d.category == "noise"
    assert d.should_interrupt is False


def test_parse_barge_in_response_should_interrupt_downgraded_for_side_chat() -> None:
    from johnny.voice_pipeline.pipeline import _parse_barge_in_response

    resp = LLMResponse(
        text="",
        finish_reason="stop",
        structured_output={
            "should_interrupt": True,
            "category": "side_chat",
            "reason": "bug",
        },
    )
    d = _parse_barge_in_response(resp)
    assert d.should_interrupt is False


def test_barge_in_config_default_enabled() -> None:
    cfg = PipelineConfig()
    assert cfg.enable_barge_in is True


def test_barge_in_config_can_disable() -> None:
    cfg = PipelineConfig(enable_barge_in=False)
    assert cfg.enable_barge_in is False


# --- Johnny-7qp: bot's own utterances reach prompt history ----------------


async def test_bot_utterance_appended_to_history_after_speaking(
    two_utterance_pcm: bytes,
) -> None:
    """After the bot speaks, its text lands in ``_transcript_history`` as a
    ``Bot (you)`` entry so the next router/answer prompt can reference it."""
    from johnny.voice_pipeline import BOT_SPEAKER_LABEL

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hi there", "and another thing"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {"should_speak": True, "confidence": 0.9, "reason": "ok"},
                {"should_speak": False, "confidence": 0.1, "reason": "skip"},
            ]
        ),
        answer_llm=_FakeAnswerLLM(answers=["Hello back to you."]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
    )
    await pipeline.run()

    bot_entries = [
        t for t in pipeline._transcript_history if t.speaker == BOT_SPEAKER_LABEL
    ]
    assert [t.text for t in bot_entries] == ["Hello back to you."]


async def test_remember_bot_utterance_strips_and_skips_empty() -> None:
    """Empty / whitespace-only bot utterances never enter the history."""
    pipeline = _bare_pipeline()

    pipeline._remember_bot_utterance("", 100)
    pipeline._remember_bot_utterance("   ", 200)
    pipeline._remember_bot_utterance("  real text  ", 300)

    assert len(pipeline._transcript_history) == 1
    assert pipeline._transcript_history[0].text == "real text"


async def test_remember_bot_utterance_uses_bot_speaker_label() -> None:
    """Direct call tags the entry with :data:`BOT_SPEAKER_LABEL`."""
    from johnny.voice_pipeline import BOT_SPEAKER_LABEL

    pipeline = _bare_pipeline()
    pipeline._remember_bot_utterance("we're upgrading infrastructure", 12345)

    entry = pipeline._transcript_history[0]
    assert entry.speaker == BOT_SPEAKER_LABEL
    assert entry.timestamp_ms == 12345


async def test_remember_bot_utterance_respects_window_cap() -> None:
    """Bot utterances count toward ``transcript_window_size`` like transcripts."""
    pipeline = _bare_pipeline(config=PipelineConfig(transcript_window_size=2))
    pipeline._remember_transcript(TranscriptFinalized(text="p1", timestamp_ms=10))
    pipeline._remember_bot_utterance("b1", 20)
    pipeline._remember_transcript(TranscriptFinalized(text="p2", timestamp_ms=30))

    # Window of 2 keeps the two most-recent entries; the first transcript is dropped.
    assert [t.text for t in pipeline._transcript_history] == ["b1", "p2"]


async def test_answer_prompt_renders_bot_history_with_label() -> None:
    """The answer LLM's user message shows the bot's prior reply as a Bot line."""
    from johnny.voice_pipeline import BOT_SPEAKER_LABEL

    pipeline = _bare_pipeline()
    pipeline._remember_transcript(
        TranscriptFinalized(text="hey what's the status?", timestamp_ms=10, speaker="alice")
    )
    pipeline._remember_bot_utterance(
        "we're upgrading infrastructure and making servers more reliable", 20
    )
    current = TranscriptFinalized(
        text="wait, what did you just say?", timestamp_ms=30, speaker="alice"
    )
    pipeline._remember_transcript(current)

    decision = RouterDecision(
        should_speak=True, confidence=0.9, reason="ack", suggested_reply=None
    )
    messages = pipeline._answer_messages(current, decision)
    user_content = messages[1].content or ""

    assert "Recent conversation:" in user_content
    assert f"- {BOT_SPEAKER_LABEL}: we're upgrading infrastructure" in user_content
    assert "- alice: hey what's the status?" in user_content
    assert "Latest transcript: wait, what did you just say?" in user_content


async def test_answer_prompt_system_message_explains_bot_label() -> None:
    """The system prompt names the ``Bot (you)`` convention so the LLM treats those
    lines as its own prior speech."""
    from johnny.voice_pipeline import BOT_SPEAKER_LABEL

    pipeline = _bare_pipeline()
    transcript = TranscriptFinalized(text="hi", timestamp_ms=10, speaker="alice")
    pipeline._remember_transcript(transcript)

    decision = RouterDecision(
        should_speak=True, confidence=0.9, reason="ok", suggested_reply=None
    )
    system_content = pipeline._answer_messages(transcript, decision)[0].content or ""

    assert BOT_SPEAKER_LABEL in system_content
    assert "your" in system_content.lower() or "you" in system_content.lower()


async def test_router_prompt_renders_bot_history_with_label() -> None:
    """The router prompt also surfaces bot history under the same label."""
    from johnny.voice_pipeline import BOT_SPEAKER_LABEL

    pipeline = _bare_pipeline()
    pipeline._remember_transcript(
        TranscriptFinalized(text="status?", timestamp_ms=10, speaker="alice")
    )
    pipeline._remember_bot_utterance("infra is being upgraded", 20)
    current = TranscriptFinalized(
        text="what did you just say?", timestamp_ms=30, speaker="alice"
    )
    pipeline._remember_transcript(current)

    snapshot = await pipeline._build_input_window(current)
    messages = pipeline._router_messages(current, snapshot)
    user_content = messages[1].content or ""
    system_content = messages[0].content or ""

    assert f"- {BOT_SPEAKER_LABEL}: infra is being upgraded" in user_content
    assert BOT_SPEAKER_LABEL in system_content


async def test_answer_prompt_lets_bot_recall_prior_utterance_round_trip(
    two_utterance_pcm: bytes,
) -> None:
    """End-to-end repro of Johnny-7qp acceptance: after the bot says X and the
    user asks 'what did you just say?', the answer LLM's prompt contains X.

    Uses ``_SlowFakeSTT`` so the response loop has a natural interleave
    point between transcripts — without it, the buffered transport
    fixture lets the transcribe loop burn through every utterance
    before the bot has spoken a single word, which is unrealistic
    timing (and would put the bot utterance *after* transcript 2 in
    the in-memory order)."""
    from johnny.voice_pipeline import BOT_SPEAKER_LABEL

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    bot_first_reply = (
        "We are upgrading the database servers and making the cluster more reliable."
    )
    answer = _FakeAnswerLLM(answers=[bot_first_reply, "I just said the servers."])
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_SlowFakeSTT(
            transcripts=[
                "what's the infrastructure roadmap?",
                "wait, what did you just say?",
            ]
        ),
        router_llm=_FakeRouterLLM(
            decisions=[
                {"should_speak": True, "confidence": 0.9, "reason": "direct ask"},
                {"should_speak": True, "confidence": 0.9, "reason": "follow-up"},
            ]
        ),
        answer_llm=answer,
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            # Disable barge-in so the classifier doesn't steal our
            # _FakeRouterLLM decisions for its own use.
            enable_barge_in=False,
        ),
    )
    await pipeline.run()

    assert len(answer.calls) == 2
    second_call = answer.calls[1]
    second_user_msg = second_call[1].content or ""
    # The bot's first reply must be present verbatim in the second prompt
    # so the LLM can quote / paraphrase its own prior statement.
    assert bot_first_reply in second_user_msg
    assert f"{BOT_SPEAKER_LABEL}:" in second_user_msg


# --- Johnny-7qp: rehydration of bot utterances --------------------------


async def test_history_unbounded_default_includes_bot_history_in_snapshot() -> None:
    """A snapshot built when history contains bot turns surfaces them in the window."""
    from johnny.voice_pipeline import BOT_SPEAKER_LABEL

    pipeline = _bare_pipeline()
    pipeline._remember_transcript(
        TranscriptFinalized(text="hello", timestamp_ms=10, speaker="alice")
    )
    pipeline._remember_bot_utterance("hi alice", 20)
    current = TranscriptFinalized(text="how are you", timestamp_ms=30, speaker="alice")
    pipeline._remember_transcript(current)

    snapshot = await pipeline._build_input_window(current)
    speakers = [entry.get("speaker") for entry in snapshot["transcript_window"]]
    assert BOT_SPEAKER_LABEL in speakers
    assert snapshot["transcript_total_count"] == 3


# --- Johnny-arh: bot does not speak over user mid-sentence ----------------


def _make_pcm_with_pause(
    pause_ms: int,
    tone_ms: int = 600,
    leading_silence_ms: int = 200,
    trailing_silence_ms: int = 200,
) -> bytes:
    """Synthesise: silence → tone → pause (silence) → tone → silence.

    Used to verify VAD endpointing: with ``end_of_speech_ms > pause_ms``
    the two tones merge into ONE utterance (the pause is below the
    end-of-turn threshold). With ``end_of_speech_ms <= pause_ms`` they
    split into two utterances. 16 kHz mono S16LE matches the pipeline's
    canonical capture format so the transport can feed it straight
    through without a WAV header.
    """
    import array
    import math

    def _tone(duration_ms: int) -> list[int]:
        n = 16_000 * duration_ms // 1000
        return [
            int(12_000 * math.sin(2 * math.pi * 440 * i / 16_000))
            for i in range(n)
        ]

    def _silence(duration_ms: int) -> list[int]:
        return [0] * (16_000 * duration_ms // 1000)

    samples: list[int] = []
    samples.extend(_silence(leading_silence_ms))
    samples.extend(_tone(tone_ms))
    samples.extend(_silence(pause_ms))
    samples.extend(_tone(tone_ms))
    samples.extend(_silence(trailing_silence_ms))
    return array.array("h", samples).tobytes()


async def test_natural_mid_sentence_pause_below_default_does_not_split(
) -> None:
    """A 700 ms thinking pause is absorbed at the new 800 ms default (Johnny-arh).

    Two tones separated by 700 ms of silence MUST merge into a single
    utterance when the default end-of-speech threshold (800 ms) is in
    effect. With the legacy 600 ms threshold the same pause would have
    split into two utterances and the bot would have answered after the
    first tone — the regression Johnny-arh fixes.
    """
    from johnny.voice_pipeline import InMemoryTranscriptSink

    pcm = _make_pcm_with_pause(pause_ms=700)
    frames = _frames_from_pcm(pcm)
    tsink = InMemoryTranscriptSink()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["first half", "second half"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": False, "confidence": 0.1, "reason": "x"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        # No end_of_speech_ms override — exercise the new 800 ms default.
        config=PipelineConfig(vad_threshold=0.05),
        transcript_sink=tsink,
    )
    await pipeline.run()
    records = tsink.snapshot()
    assert len(records) == 1, (
        f"700 ms pause < 800 ms threshold should keep the utterance whole; "
        f"got {len(records)} records: {[r.text for r in records]}"
    )


async def test_natural_mid_sentence_pause_above_threshold_splits() -> None:
    """A 900 ms silence is treated as end-of-turn at the 800 ms threshold.

    Companion to the test above: when the silence DOES exceed the
    threshold, the pipeline must finalise the first utterance so the
    bot can respond. Guards against an over-correction that disables
    endpointing entirely.
    """
    from johnny.voice_pipeline import InMemoryTranscriptSink

    pcm = _make_pcm_with_pause(pause_ms=900)
    frames = _frames_from_pcm(pcm)
    tsink = InMemoryTranscriptSink()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["first half", "second half"]),
        router_llm=_FakeRouterLLM(
            decisions=[{"should_speak": False, "confidence": 0.1, "reason": "x"}]
        ),
        answer_llm=_FakeAnswerLLM(answers=["x"]),
        tts=_FakeTTS(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(vad_threshold=0.05),
        transcript_sink=tsink,
    )
    await pipeline.run()
    records = tsink.snapshot()
    assert len(records) == 2, (
        f"900 ms pause > 800 ms threshold should split into two utterances; "
        f"got {len(records)} records"
    )


async def test_user_resume_during_router_aborts_answer(
    two_utterance_pcm: bytes,
) -> None:
    """Interrupt fired during router LLM call aborts the answer (Johnny-arh).

    Simulates the race that prompted the bead: the bot is responding to
    utterance N (router LLM is still in flight) when the user resumes
    speaking. The fast barge-in fires :meth:`VoicePipeline.interrupt`
    synchronously from the VAD loop, which sets ``_interrupt_event``.
    The previous code immediately cleared the event at the start of
    ``_answer_and_speak`` — so the bot would speak over the user. After
    Johnny-arh the event survives the router stage and the response is
    suppressed: no TTS, no utterance row, decision row marked
    ``suppressed``.
    """
    from johnny.voice_pipeline import InMemoryDecisionSink, InMemoryUtteranceSink

    frames = _frames_from_pcm(two_utterance_pcm)

    pipeline_ref: list[VoicePipeline | None] = [None]
    router_gate = asyncio.Event()
    router_entered = asyncio.Event()

    class _GatedRouterLLM(LLMProvider):
        """Router that waits for ``router_gate`` before returning.

        Simulates a slow LLM call so the test can fire the interrupt
        WHILE the router is still in flight — the exact race condition
        the Johnny-arh fix targets.
        """

        @property
        def name(self) -> str:
            return "gated-router"

        async def chat(
            self,
            messages: Sequence[ChatMessage],  # noqa: ARG002
            tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
            response_format: dict[str, Any] | None = None,  # noqa: ARG002
        ) -> LLMResponse:
            router_entered.set()
            # Fire the interrupt from the same task so it's guaranteed
            # to land before the chat call returns — mirrors what the
            # VAD fast-barge-in does on the transcribe-loop side.
            pipe = pipeline_ref[0]
            assert pipe is not None
            pipe.interrupt()
            await router_gate.wait()
            return LLMResponse(
                text='{"should_speak": true, "confidence": 0.95, "reason": "ok"}',
                finish_reason="stop",
                structured_output={
                    "should_speak": True,
                    "confidence": 0.95,
                    "reason": "ok",
                },
            )

        async def stream_chat(
            self,
            messages: Sequence[ChatMessage],
        ) -> AsyncIterator[str]:
            del messages
            return
            yield  # pragma: no cover — required to be a generator

    transport = _BufferedTransport(frames=frames)
    answer_llm = _FakeAnswerLLM(answers=["bot would have spoken this"])
    tts = _FakeTTS(frame_count=10)
    decision_sink = InMemoryDecisionSink()
    utterance_sink = InMemoryUtteranceSink()
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hello", "world"]),
        router_llm=_GatedRouterLLM(),
        answer_llm=answer_llm,
        tts=tts,
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            # Disable barge-in so the only interrupt source is our
            # gated router — keeps the assertion focused on the
            # router-stage cancellation path.
            enable_barge_in=False,
        ),
        decision_sink=decision_sink,
        utterance_sink=utterance_sink,
    )
    pipeline_ref[0] = pipeline

    run_task = asyncio.create_task(pipeline.run())
    try:
        await asyncio.wait_for(router_entered.wait(), timeout=2.0)
        # Interrupt is set inside the router. Release it so the response
        # loop can complete and the rest of the pipeline drains.
        router_gate.set()
        await asyncio.wait_for(run_task, timeout=2.0)
    finally:
        router_gate.set()
        if not run_task.done():
            run_task.cancel()
            try:
                await run_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # No TTS frames played — the answer stage was suppressed.
    assert tts.calls == [], (
        f"answer_and_speak ran despite interrupt being set: {tts.calls}"
    )
    # No utterance persisted — the bot never spoke.
    assert utterance_sink.snapshot() == []
    # Answer LLM never invoked — saved the cost.
    assert answer_llm.calls == []
    # Decision was persisted as suppressed (audit trail).
    records = decision_sink.snapshot()
    assert len(records) >= 1
    assert records[0].outcome == "suppressed"


async def test_interrupt_cleared_at_response_start_not_in_answer_and_speak(
    two_utterance_pcm: bytes,
) -> None:
    """The interrupt event is cleared once per response, BEFORE the router.

    Pins Johnny-arh's invariant: the only place ``_interrupt_event`` is
    cleared in the response path is the start of
    ``_respond_to_transcript_inner``. Re-introducing a clear inside
    ``_answer_and_speak`` would mask the race — the router stage runs
    without an interrupt check, so a barge-in fired during that window
    must remain set when the answer stage starts.
    """
    from johnny.voice_pipeline import pipeline as pipeline_module

    source = pipeline_module.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")
    # There should be exactly one ``_interrupt_event.clear()`` call in
    # production code — inside ``_respond_to_transcript_inner``.
    clear_count = text.count("self._interrupt_event.clear()")
    assert clear_count == 1, (
        f"expected exactly one _interrupt_event.clear() call in pipeline.py, "
        f"found {clear_count}"
    )

    # And the call must be in `_respond_to_transcript_inner`, not in
    # `_answer_and_speak`. Walk the relevant function bodies and check.
    import ast

    module = ast.parse(text)
    found_in: list[str] = []
    for node in ast.walk(module):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            body_text = ast.get_source_segment(text, node) or ""
            if "self._interrupt_event.clear()" in body_text:
                found_in.append(node.name)
    assert found_in == ["_respond_to_transcript_inner"], (
        f"_interrupt_event.clear() must live in _respond_to_transcript_inner "
        f"only, found in {found_in}"
    )


async def test_user_resume_during_router_does_not_emit_agent_spoke(
    two_utterance_pcm: bytes,
) -> None:
    """No ``AgentSpoke`` event when the response is cancelled mid-router.

    Subscribers (UI, decision audit) rely on ``AgentSpoke`` to render
    "the bot said X" — emitting it for a cancelled response would
    surface a phantom utterance the user never heard.
    """
    from johnny.voice_pipeline.events import AgentSpoke

    frames = _frames_from_pcm(two_utterance_pcm)
    pipeline_ref: list[VoicePipeline | None] = [None]
    router_gate = asyncio.Event()
    router_entered = asyncio.Event()

    class _GatedRouterLLM(LLMProvider):
        @property
        def name(self) -> str:
            return "gated-router"

        async def chat(
            self,
            messages: Sequence[ChatMessage],  # noqa: ARG002
            tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
            response_format: dict[str, Any] | None = None,  # noqa: ARG002
        ) -> LLMResponse:
            router_entered.set()
            pipe = pipeline_ref[0]
            assert pipe is not None
            pipe.interrupt()
            await router_gate.wait()
            return LLMResponse(
                text='{"should_speak": true, "confidence": 0.95, "reason": "ok"}',
                finish_reason="stop",
                structured_output={
                    "should_speak": True,
                    "confidence": 0.95,
                    "reason": "ok",
                },
            )

        async def stream_chat(
            self,
            messages: Sequence[ChatMessage],
        ) -> AsyncIterator[str]:
            del messages
            return
            yield  # pragma: no cover

    bus = InMemoryEventBus()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hello", "world"]),
        router_llm=_GatedRouterLLM(),
        answer_llm=_FakeAnswerLLM(answers=["nope"]),
        tts=_FakeTTS(),
        event_bus=bus,
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            enable_barge_in=False,
        ),
    )
    pipeline_ref[0] = pipeline

    run_task = asyncio.create_task(pipeline.run())
    try:
        await asyncio.wait_for(router_entered.wait(), timeout=2.0)
        router_gate.set()
        await asyncio.wait_for(run_task, timeout=2.0)
    finally:
        router_gate.set()
        if not run_task.done():
            run_task.cancel()

    spoke = [e for e in bus.snapshot() if isinstance(e, AgentSpoke)]
    assert spoke == [], (
        f"AgentSpoke fired for a cancelled response: {[e.text for e in spoke]}"
    )


# --- feed_text (Johnny-ckz.6 / Johnny-ckz.11 text-input fallback) ---------


class _LiveBlockingTransport(JohnnyTransport):
    """Transport whose ``capture_frames`` blocks until ``stop()`` is called.

    Mirrors the production ``BrowserAudioTransport``'s semantics — the
    capture iterator must not return EOF the moment the buffer is
    empty; otherwise the response loop exits before
    :meth:`VoicePipeline.feed_text` can push anything onto it
    (Johnny-ckz.11)."""

    def __init__(self, sample_rate: int = 16_000) -> None:
        self._sample_rate = sample_rate
        self._stop = asyncio.Event()
        self.played: list[bytes] = []
        self.played_source_rate: int | None = None

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self._stop.set()

    async def capture_frames(self) -> AsyncIterator[bytes]:
        # Block until stop(): the transcribe loop sees nothing and
        # eventually exits when we set the stop event.
        await self._stop.wait()
        if False:  # pragma: no cover — yield to satisfy AsyncIterator
            yield b""

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


@pytest.mark.asyncio
async def test_feed_text_drives_router_and_tts() -> None:
    """Typed text injected via feed_text() drives router + answer + TTS just
    like a transcribed utterance — the playground's mic-denied / mic-muted
    fallback (Johnny-ckz.11)."""
    transport = _LiveBlockingTransport()
    bus = InMemoryEventBus()
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=[]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {
                    "should_speak": True,
                    "confidence": 0.95,
                    "reason": "user typed a greeting",
                    "reply_type": "acknowledgement",
                    "suggested_reply": "Hello there",
                }
            ]
        ),
        answer_llm=_FakeAnswerLLM(answers=["Hello there"]),
        tts=_FakeTTS(frame_count=2),
        event_bus=bus,
        config=PipelineConfig(
            session_id="text-input-test",
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            enable_barge_in=False,
        ),
    )

    run_task = asyncio.create_task(pipeline.run())
    try:
        # Wait until the run loop has constructed _response_queue.
        for _ in range(50):
            if pipeline._response_queue is not None:  # noqa: SLF001
                break
            await asyncio.sleep(0.01)
        assert pipeline._response_queue is not None, (  # noqa: SLF001
            "pipeline didn't initialise _response_queue"
        )

        accepted = await pipeline.feed_text("hello bot")
        assert accepted is True

        # Give the response loop time to pick up the queued transcript +
        # run through router/answer/tts.
        for _ in range(100):
            spoke_events = [e for e in bus.snapshot() if isinstance(e, AgentSpoke)]
            if spoke_events:
                break
            await asyncio.sleep(0.02)
    finally:
        await transport.stop()
        try:
            await asyncio.wait_for(run_task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            if not run_task.done():
                run_task.cancel()
                try:
                    await run_task
                except (asyncio.CancelledError, Exception):
                    pass

    events = bus.snapshot()
    transcripts = [e for e in events if isinstance(e, TranscriptFinalized)]
    assert any(
        t.text == "hello bot" and t.speaker == "user" for t in transcripts
    ), f"feed_text didn't publish the transcript event: {[t.text for t in transcripts]}"
    spoke = [e for e in events if isinstance(e, AgentSpoke)]
    assert spoke, "feed_text didn't drive the response loop to produce AgentSpoke"
    assert spoke[0].text == "Hello there"
    # And the TTS frames flowed through the transport (proves the
    # end-to-end audio path the playground depends on).
    assert transport.played, "no PCM frames were played through the transport"


@pytest.mark.asyncio
async def test_feed_text_rejects_empty_input() -> None:
    """Whitespace-only / empty input is rejected so the API can return 4xx."""
    bus = InMemoryEventBus()
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=[]),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=[]),
        router_llm=_FakeRouterLLM(decisions=[]),
        answer_llm=_FakeAnswerLLM(answers=[]),
        tts=_FakeTTS(),
        event_bus=bus,
        config=PipelineConfig(),
    )
    # Pipeline not yet run — feed_text must report not-accepted.
    assert await pipeline.feed_text("") is False
    assert await pipeline.feed_text("   ") is False


# --- Johnny-g2n: TTS failure surfacing + circuit breaker -------------------


class _FailingTTS(TTSProvider):
    """TTS that raises :class:`TTSError` for every synthesize call.

    Used by the Johnny-g2n tests to drive the pipeline's response loop
    through the new TTS-failure event-emitting + circuit-breaker path.
    The constructor takes the failure ``category`` so a single test can
    exercise both terminal (quota / auth) and transient (rate_limited /
    unknown) categories.
    """

    def __init__(self, category: str = "quota_exceeded", message: str = "out of credits") -> None:
        from app.providers import TTSError

        self._category = category
        self._message = message
        self.calls: list[str] = []
        # Cache the exception class so we don't reach back into app.providers
        # for every call.
        self._err_cls = TTSError

    @property
    def name(self) -> str:
        return "failing-tts"

    async def synthesize_stream(
        self,
        text: str,
        voice_id: str | None = None,  # noqa: ARG002
    ) -> AsyncIterator[bytes]:
        self.calls.append(text)
        raise self._err_cls(self._message, category=self._category)  # type: ignore[arg-type]
        # Unreachable yield to satisfy the AsyncIterator signature.
        yield b""  # pragma: no cover


async def test_pipeline_emits_agent_tts_failed_on_quota_exceeded(
    two_utterance_pcm: bytes,
) -> None:
    """A 401-quota error fires AgentTTSFailed and continues the session."""
    from johnny.voice_pipeline.events import AgentTTSFailed

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    transport = _BufferedTransport(frames=frames)
    bus = InMemoryEventBus()
    tts = _FailingTTS(
        category="quota_exceeded",
        message="elevenlabs TTS HTTP 401: exceeds your quota of 10",
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hi", "again"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {"should_speak": True, "confidence": 0.95, "reason": "ok"},
                {"should_speak": True, "confidence": 0.95, "reason": "ok"},
            ]
        ),
        answer_llm=_FakeAnswerLLM(answers=["sure", "again"]),
        tts=tts,
        event_bus=bus,
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
            session_id="sess-quota",
        ),
    )
    await pipeline.run()

    failures = [e for e in bus.snapshot() if isinstance(e, AgentTTSFailed)]
    assert failures, "no AgentTTSFailed event published"
    first = failures[0]
    assert first.category == "quota_exceeded"
    assert first.terminal is True
    assert first.provider_name == "failing-tts"
    assert "exceeds your quota" in first.message
    assert first.session_id == "sess-quota"
    # No audio played — failure was synchronous.
    assert transport.played == []


async def test_pipeline_circuit_breaker_skips_subsequent_tts(
    two_utterance_pcm: bytes,
) -> None:
    """After a terminal TTS failure, the next turn skips answer LLM + TTS."""
    from johnny.voice_pipeline.events import AgentTTSFailed

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    bus = InMemoryEventBus()
    tts = _FailingTTS(category="auth_failed", message="HTTP 401")
    answer = _FakeAnswerLLM(answers=["one", "two"])
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hi", "again"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {"should_speak": True, "confidence": 0.95, "reason": "ok"},
                {"should_speak": True, "confidence": 0.95, "reason": "ok"},
            ]
        ),
        answer_llm=answer,
        tts=tts,
        event_bus=bus,
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
    )
    await pipeline.run()

    # Only ONE TTS call (the one that tripped the breaker) — second turn
    # is short-circuited before the answer LLM runs.
    assert len(tts.calls) == 1
    assert len(answer.calls) == 1
    # One terminal failure event, marked terminal=True.
    failures = [e for e in bus.snapshot() if isinstance(e, AgentTTSFailed)]
    assert len(failures) == 1
    assert failures[0].terminal is True
    # Router decisions still fire for every turn — the activity log
    # reflects the model's intent even when the answer was suppressed.
    decisions = [e for e in bus.snapshot() if isinstance(e, RouterDecisionMade)]
    assert len(decisions) == 2


async def test_pipeline_transient_tts_failure_does_not_trip_breaker(
    two_utterance_pcm: bytes,
) -> None:
    """rate_limited and unknown categories emit the event but keep retrying."""
    from johnny.voice_pipeline.events import AgentTTSFailed

    frame_size = 640
    frames = [
        two_utterance_pcm[i : i + frame_size]
        for i in range(0, len(two_utterance_pcm), frame_size)
        if i + frame_size <= len(two_utterance_pcm)
    ]
    bus = InMemoryEventBus()
    tts = _FailingTTS(category="rate_limited", message="429")
    answer = _FakeAnswerLLM(answers=["one", "two"])
    pipeline = VoicePipeline(
        transport=_BufferedTransport(frames=frames),
        vad=EnergyVAD(threshold=0.05),
        stt=_FakeSTT(transcripts=["hi", "again"]),
        router_llm=_FakeRouterLLM(
            decisions=[
                {"should_speak": True, "confidence": 0.95, "reason": "ok"},
                {"should_speak": True, "confidence": 0.95, "reason": "ok"},
            ]
        ),
        answer_llm=answer,
        tts=tts,
        event_bus=bus,
        config=PipelineConfig(
            vad_threshold=0.05,
            end_of_speech_ms=300,
            confidence_threshold=0.5,
        ),
    )
    await pipeline.run()

    # Both turns ran answer LLM + TTS — the breaker stays open for
    # transient categories so the next attempt is made.
    assert len(answer.calls) == 2
    assert len(tts.calls) == 2
    failures = [e for e in bus.snapshot() if isinstance(e, AgentTTSFailed)]
    assert len(failures) == 2
    assert all(f.terminal is False for f in failures)
    assert all(f.category == "rate_limited" for f in failures)
