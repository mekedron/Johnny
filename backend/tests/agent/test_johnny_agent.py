"""Unit tests for JohnnyAgent instructions + transcript rehydration (Johnny-re2).

Covers the two responsibilities Phase 2 adds to
:class:`johnny.agent.session.JohnnyAgent`:

* :func:`~johnny.agent.session.build_agent_instructions` assembles the static
  system prompt from the personality / meeting-context / calendar components,
  reusing the legacy ``VoicePipeline._answer_messages`` ordering;
* :func:`~johnny.agent.session.transcripts_to_chat_ctx` /
  :func:`~johnny.agent.session.build_johnny_agent` rehydrate persisted
  transcripts into the LiveKit ``chat_ctx`` so a container respawn keeps the
  bot's memory (parity with ``VoicePipeline._rehydrate_transcript_history``).

Guarded by ``importorskip`` so the suite still collects where the ``agent``
extra (``livekit-agents``) is absent.
"""

from __future__ import annotations

import pytest

pytest.importorskip("livekit.agents")

from livekit.agents.llm import ChatContext  # noqa: E402
from livekit.agents.llm.chat_context import ChatMessage as LKChatMessage  # noqa: E402

from johnny.agent.session import (  # noqa: E402
    DEFAULT_INSTRUCTIONS,
    AgentInstructionsConfig,
    JohnnyAgent,
    build_agent_instructions,
    build_johnny_agent,
    transcripts_to_chat_ctx,
)
from johnny.voice_pipeline.events import TranscriptFinalized  # noqa: E402
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
    assert text.index("[personality: X]") < text.index(
        "assistant turns are your own prior speech"
    )


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
    agent = await build_johnny_agent(
        prompt_config=AgentInstructionsConfig(instructions="x")
    )
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
