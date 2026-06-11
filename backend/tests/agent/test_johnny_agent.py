"""Unit tests for JohnnyAgent instructions + transcript rehydration (Johnny-re2).

Covers the two responsibilities Phase 2 adds to
:class:`johnny.agent.session.JohnnyAgent`:

* :func:`~johnny.agent.session.build_agent_instructions` assembles the static
  system prompt from the personality / meeting-context / calendar components,
  reusing the legacy split pipeline ordering;
* :func:`~johnny.agent.session.transcripts_to_chat_ctx` /
  :func:`~johnny.agent.session.build_johnny_agent` rehydrate persisted
  transcripts into the LiveKit ``chat_ctx`` so a container respawn keeps the
  bot's memory (parity with the legacy split pipeline).

Guarded by ``importorskip`` so the suite still collects where the ``agent``
extra (``livekit-agents``) is absent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

import pytest

pytest.importorskip("livekit.agents")

from livekit.agents import ModelSettings  # noqa: E402
from livekit.agents.llm import ChatContext  # noqa: E402
from livekit.agents.llm.chat_context import ChatMessage as LKChatMessage  # noqa: E402
from livekit.agents.stt import SpeechData, SpeechEvent, SpeechEventType  # noqa: E402

from app.providers.base import (  # noqa: E402
    ChatMessage,
    LLMProvider,
    LLMResponse,
    ToolDefinition,
    TTSProvider,
)
from johnny.agent.adapters.johnny_tts import JohnnyTTS  # noqa: E402
from johnny.agent.session import (  # noqa: E402
    DEFAULT_INSTRUCTIONS,
    AgentInstructionsConfig,
    AnswerConfig,
    JohnnyAgent,
    NoiseFilterConfig,
    build_agent_instructions,
    build_johnny_agent,
    transcripts_to_chat_ctx,
)
from johnny.voice_pipeline.events import TranscriptFiltered, TranscriptFinalized  # noqa: E402
from johnny.voice_pipeline.reasoning import (  # noqa: E402
    AUTONOMOUS_MODE,
    LIMITED_AUTO_SPEAK_MODE,
)
from johnny.voice_pipeline.transcript_history import (  # noqa: E402
    BOT_SPEAKER_LABEL,
    InMemoryTranscriptHistoryLoader,
    TranscriptHistoryLoader,
)


def _participant(text: str, *, speaker: str | None = None, ts: int = 0) -> TranscriptFinalized:
    return TranscriptFinalized(text=text, timestamp_ms=ts, speaker=speaker)


def _bot(text: str, *, ts: int = 0) -> TranscriptFinalized:
    return TranscriptFinalized(text=text, timestamp_ms=ts, speaker=BOT_SPEAKER_LABEL)


def _pairs(ctx: ChatContext) -> list[tuple[str, str]]:
    """(role, text) for each message item — narrowed to ChatMessage."""
    out: list[tuple[str, str]] = []
    for item in ctx.items:
        assert isinstance(item, LKChatMessage)
        out.append((item.role, item.text_content or ""))
    return out


# --- build_agent_instructions ---------------------------------------------


def test_empty_config_renders_base_framing_and_history_note_only() -> None:
    text = build_agent_instructions(AgentInstructionsConfig())
    assert text.startswith("You are an AI meeting participant.")
    # The history note is always present (explains assistant=own speech).
    assert "assistant turns are your own prior speech" in text
    # Nothing optional leaked when all fields are empty.
    assert "Meeting instructions:" not in text
    assert "Context:" not in text
    assert "Calendar event description:" not in text
    assert "Calendar attachments" not in text
    assert "Last session summary:" not in text


def test_all_components_render_in_legacy_order() -> None:
    config = AgentInstructionsConfig(
        instructions="Stay on the agenda.",
        personality_prompt="[personality: Pirate]\nArr, ye be a pirate.",
        context="Quarterly planning.",
        calendar_context="Q3 OKR review.",
        calendar_attachments_text="Doc body: roadmap.",
        prior_session_context="Last week we deferred hiring.",
    )
    text = build_agent_instructions(config)

    # Every configured component is present...
    assert "[personality: Pirate]\nArr, ye be a pirate." in text
    assert "Meeting instructions: Stay on the agenda." in text
    assert "Context: Quarterly planning." in text
    assert "Calendar event description: Q3 OKR review." in text
    assert "Calendar attachments (linked documents from the event " in text
    assert "Doc body: roadmap." in text
    assert "Last session summary: Last week we deferred hiring." in text

    # ...and in the legacy answer-stage order: personality FIRST (before the
    # job/brief), then instructions → context → calendar → attachments → prior.
    positions = [
        text.index("[personality: Pirate]"),
        text.index("Meeting instructions:"),
        text.index("Context: Quarterly planning."),
        text.index("Calendar event description:"),
        text.index("Calendar attachments"),
        text.index("Last session summary:"),
    ]
    assert positions == sorted(positions)
    # Personality is rendered ahead of the base "job" tail too: it sits right
    # after the opening framing sentence.
    assert text.index("[personality: Pirate]") < text.index("Meeting instructions:")


def test_personality_renders_before_history_note() -> None:
    text = build_agent_instructions(
        AgentInstructionsConfig(personality_prompt="[personality: X]\nBe X.")
    )
    assert text.index("[personality: X]") < text.index("assistant turns are your own prior speech")


# --- transcripts_to_chat_ctx -----------------------------------------------


def test_bot_utterances_map_to_assistant_role() -> None:
    ctx = transcripts_to_chat_ctx([_bot("I said this earlier.")])
    assert _pairs(ctx) == [("assistant", "I said this earlier.")]


def test_participant_with_speaker_is_prefixed_user_turn() -> None:
    ctx = transcripts_to_chat_ctx([_participant("Hello team.", speaker="Alice")])
    assert _pairs(ctx) == [("user", "Alice: Hello team.")]


def test_participant_without_speaker_is_bare_user_turn() -> None:
    ctx = transcripts_to_chat_ctx([_participant("No speaker known.")])
    assert _pairs(ctx) == [("user", "No speaker known.")]


def test_empty_text_transcripts_are_skipped() -> None:
    ctx = transcripts_to_chat_ctx(
        [
            _participant("   ", speaker="Alice"),
            _bot(""),
            _participant("Real content.", speaker="Bob"),
        ]
    )
    assert _pairs(ctx) == [("user", "Bob: Real content.")]


def test_chronological_order_is_preserved() -> None:
    ctx = transcripts_to_chat_ctx(
        [
            _participant("First.", speaker="Alice", ts=1000),
            _bot("Second.", ts=2000),
            _participant("Third.", speaker="Bob", ts=3000),
        ]
    )
    assert _pairs(ctx) == [
        ("user", "Alice: First."),
        ("assistant", "Second."),
        ("user", "Bob: Third."),
    ]


# --- JohnnyAgent construction ----------------------------------------------


def test_bare_agent_uses_default_instructions_and_empty_history() -> None:
    agent = JohnnyAgent()
    assert agent.instructions == DEFAULT_INSTRUCTIONS
    assert list(agent.chat_ctx.items) == []


def test_prompt_config_builds_instructions() -> None:
    config = AgentInstructionsConfig(instructions="Be brief.")
    agent = JohnnyAgent(prompt_config=config)
    assert agent.instructions == build_agent_instructions(config)
    assert "Meeting instructions: Be brief." in agent.instructions


def test_explicit_instructions_override_prompt_config() -> None:
    agent = JohnnyAgent(
        instructions="VERBATIM",
        prompt_config=AgentInstructionsConfig(instructions="ignored"),
    )
    assert agent.instructions == "VERBATIM"


def test_chat_history_seeds_chat_ctx() -> None:
    agent = JohnnyAgent(
        chat_history=[
            _participant("What's the status?", speaker="Alice"),
            _bot("On track."),
        ]
    )
    assert _pairs(agent.chat_ctx) == [
        ("user", "Alice: What's the status?"),
        ("assistant", "On track."),
    ]


# --- build_johnny_agent (loader-driven rehydration) ------------------------


async def test_build_johnny_agent_rehydrates_from_loader() -> None:
    loader = InMemoryTranscriptHistoryLoader(
        [_participant("Earlier question.", speaker="Bob"), _bot("Earlier answer.")]
    )
    agent = await build_johnny_agent(
        prompt_config=AgentInstructionsConfig(instructions="Help out."),
        transcript_history_loader=loader,
        session_id="sess-1",
        bot_session_id=42,
    )
    # History rehydrated into chat_ctx...
    assert _pairs(agent.chat_ctx) == [
        ("user", "Bob: Earlier question."),
        ("assistant", "Earlier answer."),
    ]
    # ...instructions built from the config...
    assert "Meeting instructions: Help out." in agent.instructions
    # ...and the loader was queried with both ids (parity with the pipeline).
    assert loader.calls == [("sess-1", 42)]


async def test_build_johnny_agent_without_loader_starts_empty() -> None:
    agent = await build_johnny_agent(prompt_config=AgentInstructionsConfig(instructions="x"))
    assert list(agent.chat_ctx.items) == []
    assert "Meeting instructions: x" in agent.instructions


async def test_build_johnny_agent_swallows_loader_failure() -> None:
    class _BoomLoader(TranscriptHistoryLoader):
        async def load(
            self, *, session_id: str | None, bot_session_id: int | None
        ) -> list[TranscriptFinalized]:
            raise RuntimeError("DB unreachable")

    # A loader failure must not refuse to start — agent comes up with empty
    # history (better to lose context than to fail to join).
    agent = await build_johnny_agent(
        transcript_history_loader=_BoomLoader(),
        session_id="sess-2",
    )
    assert list(agent.chat_ctx.items) == []
    assert agent.instructions == DEFAULT_INSTRUCTIONS


async def test_build_johnny_agent_explicit_instructions_win() -> None:
    loader = InMemoryTranscriptHistoryLoader([_bot("hi")])
    agent = await build_johnny_agent(
        instructions="OVERRIDE",
        prompt_config=AgentInstructionsConfig(instructions="ignored"),
        transcript_history_loader=loader,
    )
    assert agent.instructions == "OVERRIDE"
    # Rehydration still happens regardless of how instructions were chosen.
    assert _pairs(agent.chat_ctx) == [("assistant", "hi")]


# --- llm_node: allowed-reply coercion (Johnny-5ag) -------------------------


class _FakeAnswerLLM(LLMProvider):
    """A scripted answer ``LLMProvider`` for the coercion node tests."""

    def __init__(self, *, structured: Any = None, text: str = "") -> None:
        self._structured = structured
        self._text = text
        self.calls: list[Sequence[ChatMessage]] = []

    @property
    def name(self) -> str:
        return "fake-answer"

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
        response_format: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> LLMResponse:
        self.calls.append(list(messages))
        return LLMResponse(
            text=self._text, finish_reason="stop", structured_output=self._structured
        )


class _RecordingGate:
    """Duck-typed stand-in for RouterGate — records coercion-no-match flags."""

    def __init__(self) -> None:
        self.no_match_calls = 0

    def note_coercion_no_match(self) -> None:
        self.no_match_calls += 1


async def _drain(agen: AsyncIterator[Any]) -> list[Any]:
    return [chunk async for chunk in agen]


async def test_llm_node_coerces_to_matched_allowed_reply() -> None:
    answer_llm = _FakeAnswerLLM(structured={"selected_reply": "Yes"})
    agent = JohnnyAgent(
        answer_llm=answer_llm,
        answer_config=AnswerConfig(mode=LIMITED_AUTO_SPEAK_MODE, allowed_replies=("Yes", "No")),
    )

    out = await _drain(agent.llm_node(ChatContext.empty(), [], ModelSettings()))

    # The single matched reply is yielded as one text chunk (streams into TTS).
    assert out == ["Yes"]
    assert len(answer_llm.calls) == 1  # one structured coercion call


async def test_llm_node_no_match_yields_nothing_and_flags_gate() -> None:
    # Off-list structured pick → no match → silent, gate flagged.
    answer_llm = _FakeAnswerLLM(structured={"selected_reply": "Maybe later"})
    gate = _RecordingGate()
    agent = JohnnyAgent(
        answer_llm=answer_llm,
        answer_config=AnswerConfig(mode=LIMITED_AUTO_SPEAK_MODE, allowed_replies=("Yes", "No")),
        router_gate=cast(Any, gate),
    )

    out = await _drain(agent.llm_node(ChatContext.empty(), [], ModelSettings()))

    assert out == []  # nothing spoken
    assert gate.no_match_calls == 1  # gate told to terminalize no_allowed_reply_match


async def test_llm_node_autonomous_bypasses_coercion() -> None:
    # Free-form mode never coerces; it delegates to the default streaming node,
    # which needs a running activity (absent here) — proving the bypass.
    answer_llm = _FakeAnswerLLM(structured={"selected_reply": "Yes"})
    agent = JohnnyAgent(
        answer_llm=answer_llm,
        answer_config=AnswerConfig(mode=AUTONOMOUS_MODE, allowed_replies=("Yes", "No")),
    )

    with pytest.raises(RuntimeError):
        await _drain(agent.llm_node(ChatContext.empty(), [], ModelSettings()))

    assert answer_llm.calls == []  # autonomous bypassed the allow-list coercion


# --- tts_node: per-sentence flush + TTS-missing degrade (Johnny-5ag) --------


class _RecordingTTSProvider(TTSProvider):
    """A TTS provider that records the text of every synthesize call."""

    def __init__(self, frames: list[bytes]) -> None:
        self._frames = list(frames)
        self.texts: list[str] = []

    @property
    def name(self) -> str:
        return "fake-tts"

    async def synthesize_stream(
        self,
        text: str,
        voice_id: str | None = None,  # noqa: ARG002
    ) -> AsyncIterator[bytes]:
        self.texts.append(text)
        for frame in self._frames:
            yield frame


class _FakeActivity:
    """Minimal stand-in for the running AgentActivity (only ``tts`` is read)."""

    def __init__(self, tts: Any) -> None:
        self.tts = tts


async def _astream(*deltas: str) -> AsyncIterator[str]:
    for delta in deltas:
        yield delta


async def test_tts_node_synthesizes_per_sentence() -> None:
    provider = _RecordingTTSProvider([b"\x01\x02" * 1_600])  # 0.1 s per sentence
    agent = JohnnyAgent()
    agent._activity = cast(Any, _FakeActivity(JohnnyTTS(provider)))

    frames = [
        fr
        async for fr in agent.tts_node(_astream("Hello world. ", "How are you?\n"), ModelSettings())
    ]

    # Each complete sentence was synthesised separately (per-sentence flush).
    assert provider.texts == ["Hello world.", "How are you?"]
    # Audio was produced at the provider's 16 kHz mono contract.
    assert frames
    assert all(f.sample_rate == 16_000 and f.num_channels == 1 for f in frames)


async def test_tts_node_degrades_to_no_audio_without_session_tts() -> None:
    agent = JohnnyAgent()
    agent._activity = None  # no activity → no session TTS

    consumed: list[str] = []

    async def _recording_stream() -> AsyncIterator[str]:
        for delta in ("Hello. ", "World."):
            consumed.append(delta)
            yield delta

    frames = [fr async for fr in agent.tts_node(_recording_stream(), ModelSettings())]

    assert frames == []  # degraded: no audio, no crash
    # The text stream was fully consumed so the upstream generation completes.
    assert consumed == ["Hello. ", "World."]


async def test_tts_node_degrades_when_tts_unavailable_flag_set() -> None:
    provider = _RecordingTTSProvider([b"\x00\x00" * 1_600])
    agent = JohnnyAgent(tts_available=False)
    agent._activity = cast(Any, _FakeActivity(JohnnyTTS(provider)))

    frames = [fr async for fr in agent.tts_node(_astream("Hi."), ModelSettings())]

    assert frames == []  # forced degrade even though a TTS exists
    assert provider.texts == []  # synthesize never called


# --- tts_node: per-sentence speech interims (Johnny-trt.39) -----------------


async def test_tts_node_emits_speech_interim_per_sentence() -> None:
    provider = _RecordingTTSProvider([b"\x01\x02" * 1_600])
    flushed: list[tuple[str, int]] = []
    agent = JohnnyAgent(speech_interim_sink=lambda text, seq: flushed.append((text, seq)))
    agent._activity = cast(Any, _FakeActivity(JohnnyTTS(provider)))

    frames = [
        fr
        async for fr in agent.tts_node(_astream("Hello world. ", "How are you?\n"), ModelSettings())
    ]

    # One interim per flushed sentence, sequence-numbered from 0 per reply,
    # emitted for exactly the sentences that reached TTS.
    assert flushed == [("Hello world.", 0), ("How are you?", 1)]
    assert provider.texts == ["Hello world.", "How are you?"]
    assert frames


async def test_tts_node_no_interims_on_tts_degrade() -> None:
    # Nothing is spoken on the degrade path, so no provisional text either —
    # a bubble with no audio behind it would be ghost text by construction.
    flushed: list[tuple[str, int]] = []
    agent = JohnnyAgent(speech_interim_sink=lambda text, seq: flushed.append((text, seq)))
    agent._activity = None

    frames = [fr async for fr in agent.tts_node(_astream("Hello. ", "World."), ModelSettings())]

    assert frames == []
    assert flushed == []


async def test_tts_node_sink_failure_does_not_break_synthesis() -> None:
    provider = _RecordingTTSProvider([b"\x01\x02" * 1_600])

    def _boom(text: str, seq: int) -> None:
        raise RuntimeError("sink down")

    agent = JohnnyAgent(speech_interim_sink=_boom)
    agent._activity = cast(Any, _FakeActivity(JohnnyTTS(provider)))

    frames = [fr async for fr in agent.tts_node(_astream("Hi there. ", "Bye."), ModelSettings())]

    # A lost caption beats a crashed reply: every sentence still synthesised.
    assert provider.texts == ["Hi there.", "Bye."]
    assert frames


# --- stt_node: noise gate (Johnny-cmd) -------------------------------------


class _RecordingSink:
    """Records every TranscriptFiltered the gate publishes."""

    def __init__(self) -> None:
        self.events: list[TranscriptFiltered] = []

    async def __call__(self, event: TranscriptFiltered) -> None:
        self.events.append(event)


def _final(
    text: str,
    *,
    confidence: float = 0.0,
    speaker: str | None = None,
    start: float = 0.0,
    end: float = 0.0,
) -> SpeechEvent:
    return SpeechEvent(
        type=SpeechEventType.FINAL_TRANSCRIPT,
        alternatives=[
            SpeechData(
                language="en",
                text=text,
                start_time=start,
                end_time=end,
                confidence=confidence,
                speaker_id=speaker,
            )
        ],
    )


def _interim(text: str) -> SpeechEvent:
    return SpeechEvent(
        type=SpeechEventType.INTERIM_TRANSCRIPT,
        alternatives=[SpeechData(language="en", text=text)],
    )


async def _source(*events: Any) -> AsyncIterator[Any]:
    for event in events:
        yield event


async def test_stt_gate_drops_noise_final_and_publishes_event() -> None:
    sink = _RecordingSink()
    agent = JohnnyAgent(
        noise_filter=NoiseFilterConfig(),
        transcript_filtered_sink=sink,
        session_id="sess-noise",
    )

    out = await _drain(agent._gate_stt_events(_source(_final("uh"), _final("What's the plan?"))))

    # The filler final is dropped; the real turn passes through.
    assert [e.alternatives[0].text for e in out] == ["What's the plan?"]
    # ...and the drop emitted exactly one TranscriptFiltered with the reason.
    assert len(sink.events) == 1
    dropped = sink.events[0]
    assert dropped.reason == "stoplist_match"
    assert dropped.text == "uh"
    assert dropped.session_id == "sess-noise"


async def test_stt_gate_suppresses_noise_interim_silently() -> None:
    # A noise interim is dropped so the SDK can't promote it to a final at
    # turn-commit (the leftover-interim hole) — but silently, with no event
    # (the legacy gate recorded one TranscriptFiltered per finalized utterance).
    sink = _RecordingSink()
    agent = JohnnyAgent(noise_filter=NoiseFilterConfig(), transcript_filtered_sink=sink)

    out = await _drain(agent._gate_stt_events(_source(_interim("uh"))))

    assert out == []  # suppressed
    assert sink.events == []  # no event for a partial fragment


async def test_stt_gate_passes_real_interim() -> None:
    # A genuine partial transcript flows through for live display.
    agent = JohnnyAgent(noise_filter=NoiseFilterConfig())

    out = await _drain(agent._gate_stt_events(_source(_interim("what's the"))))

    assert [e.alternatives[0].text for e in out] == ["what's the"]


async def test_stt_gate_noise_interim_then_final_yields_nothing() -> None:
    # The end-to-end "cough produces zero turns" shape: a provider that emits an
    # interim then a final for the same filler (Deepgram-style) yields no event
    # the turn detector can accumulate, so no turn is opened. The final still
    # records its TranscriptFiltered.
    sink = _RecordingSink()
    agent = JohnnyAgent(noise_filter=NoiseFilterConfig(), transcript_filtered_sink=sink)

    out = await _drain(agent._gate_stt_events(_source(_interim("uh"), _final("uh"))))

    assert out == []  # nothing reaches the turn detector
    assert [e.reason for e in sink.events] == ["stoplist_match"]  # one event, the final


async def test_stt_gate_audio_too_short_from_segment_timing() -> None:
    # A final whose segment timing reports < min_audio_ms is dropped as
    # audio_too_short even though its text is real speech (pre-STT floor parity).
    sink = _RecordingSink()
    agent = JohnnyAgent(noise_filter=NoiseFilterConfig(), transcript_filtered_sink=sink)

    out = await _drain(agent._gate_stt_events(_source(_final("Hello there.", start=0.0, end=0.12))))

    assert out == []
    assert len(sink.events) == 1
    assert sink.events[0].reason == "audio_too_short"
    assert sink.events[0].audio_duration_ms == 120


async def test_stt_gate_no_config_passes_everything() -> None:
    # No filter configured → transparent pass-through (bare/smoke default).
    agent = JohnnyAgent()  # noise_filter=None

    out = await _drain(agent._gate_stt_events(_source(_final("uh"), _final(""))))

    assert [e.alternatives[0].text for e in out] == ["uh", ""]


async def test_stt_gate_disabled_config_passes_everything() -> None:
    sink = _RecordingSink()
    agent = JohnnyAgent(
        noise_filter=NoiseFilterConfig(enabled=False),
        transcript_filtered_sink=sink,
    )

    out = await _drain(agent._gate_stt_events(_source(_final("uh"))))

    assert [e.alternatives[0].text for e in out] == ["uh"]
    assert sink.events == []


async def test_stt_gate_passes_non_speech_events_through() -> None:
    # A non-SpeechEvent item (the node type also allows str) is never gated.
    agent = JohnnyAgent(noise_filter=NoiseFilterConfig())

    out = await _drain(agent._gate_stt_events(_source("raw-string-item")))

    assert out == ["raw-string-item"]


async def test_stt_gate_swallows_sink_failure() -> None:
    # A publish failure must not crash the STT node — the candidate is still
    # dropped, the session keeps running (legacy swallow-and-continue contract).
    async def _boom(_event: TranscriptFiltered) -> None:
        raise RuntimeError("event bus down")

    agent = JohnnyAgent(noise_filter=NoiseFilterConfig(), transcript_filtered_sink=cast(Any, _boom))

    out = await _drain(agent._gate_stt_events(_source(_final("uh"), _final("Real."))))

    # The noise final is still dropped despite the sink raising; the real one flows.
    assert [e.alternatives[0].text for e in out] == ["Real."]


async def test_stt_gate_drop_without_sink_is_safe() -> None:
    # No sink wired (observability off) → still drops, just doesn't publish.
    agent = JohnnyAgent(noise_filter=NoiseFilterConfig())

    out = await _drain(agent._gate_stt_events(_source(_final("um"), _final("Hi there."))))

    assert [e.alternatives[0].text for e in out] == ["Hi there."]
