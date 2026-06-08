"""AgentSession harness + JohnnyAgent (epic Johnny-7g5, Phase 0 → Phase 2).

This module wires Johnny's voice orchestration onto LiveKit Agents'
``AgentSession(stt, llm, tts, vad, turn_detection)``:

* the ``stt`` / ``llm`` / ``tts`` arguments are Johnny's own LiveKit plugin
  adapters (:mod:`johnny.agent.adapters`, Phase 1), built from
  ``load_active_providers()`` by the adapter factory (Johnny-zb3);
* :class:`JohnnyAgent` carries the assembled instructions (personality +
  meeting-context + calendar prompt, reusing the legacy answer-stage prompt
  assembly) and rehydrates prior transcript history into the LiveKit
  ``chat_ctx`` on container respawn so memory survives restarts (Johnny-re2,
  parity with ``VoicePipeline._rehydrate_transcript_history``);
* the router "should-speak" gate in ``on_user_turn_completed`` lands next
  (Johnny-xpa, Phase 2) on top of the gate harness (:mod:`johnny.agent.gate`).

End-of-utterance detection follows the operator's locked decision: LiveKit's
:class:`~livekit.plugins.turn_detector.multilingual.MultilingualModel` plus
Silero VAD (``silero.VAD``). Both model files are baked into the image at
build time (``python -m livekit.agents download-files``; see
``backend/Dockerfile``) so a clean ``./run.sh`` runs offline.

Importing this module REQUIRES the ``agent`` extra (``livekit-agents``); it
is only imported where that extra is installed (the api/agent image), never
from the import-safe top-level :mod:`johnny.agent` package. It also pulls
:mod:`johnny.voice_pipeline` (and therefore ``app.providers``) for the
transcript-history value objects shared with the legacy pipeline — fine here
because this is the integration module that bridges both worlds, and it is
only ever imported in the full-stack worker / api / test contexts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from livekit.agents import Agent, AgentSession
from livekit.agents.llm.chat_context import ChatContext
from livekit.plugins import silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from johnny.voice_pipeline.events import TranscriptFinalized
from johnny.voice_pipeline.transcript_history import (
    BOT_SPEAKER_LABEL,
    NoopTranscriptHistoryLoader,
    TranscriptHistoryLoader,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    # STT / LLM / TTS are Generic over their event type (TEvent) and
    # AgentSession over Userdata_T; the harness is event/userdata-agnostic,
    # so it accepts any concrete adapter via the [Any] parametrization. The
    # Phase-1 adapters (johnny.agent.adapters) subclass with concrete events.
    from livekit.agents.llm import LLM
    from livekit.agents.stt import STT
    from livekit.agents.tts import TTS
    from livekit.agents.vad import VAD

logger = logging.getLogger(__name__)

# Placeholder instructions for a bare ``JohnnyAgent()`` — used only when
# neither an explicit ``instructions`` string nor a ``prompt_config`` is
# supplied (smoke tests / a console session with no meeting brief). A real
# scheduled session always builds instructions from :class:`AgentInstructionsConfig`.
DEFAULT_INSTRUCTIONS = "You are Johnny, an AI participant in a live voice meeting."

# Generic answer-stage framing, mirrored from
# ``VoicePipeline._answer_messages`` so a LiveKit-driven Johnny opens with the
# same job description the meet-worker answer LLM had. Deliberately nameless so
# a configured personality (rendered next) owns the persona without conflict.
_BASE_INSTRUCTIONS = (
    "You are an AI meeting participant. Produce concise, natural spoken replies."
)

# Note explaining the rehydrated/streamed conversation history, mirroring the
# legacy answer prompt's "Recent conversation" guidance (Johnny-7qp): assistant
# turns are the bot's own prior speech, user turns are participants (optionally
# speaker-prefixed). Lets the model answer "what did you just say?" after a
# respawn from the rehydrated assistant turns.
_HISTORY_NOTE = (
    "Earlier turns in this meeting are provided as conversation history: "
    "assistant turns are your own prior speech (ground any \"what did you "
    "say?\" answer in their exact words), and user turns are meeting "
    "participants, sometimes prefixed with the speaker's name."
)


@dataclass(frozen=True, slots=True)
class AgentInstructionsConfig:
    """Static prompt components assembled into ``Agent.instructions``.

    Mirrors the subset of :class:`~johnny.voice_pipeline.pipeline.PipelineConfig`
    the legacy answer LLM rendered into its system message — the Johnny-oly.8
    personality identity layer, the meeting brief, the calendar background, and
    the cross-session memory (Johnny-dsy). The agent worker fills these from the
    same launcher env vars the meet-worker reads (``JOHNNY_PERSONALITY_PROMPT``,
    ``JOHNNY_INSTRUCTIONS``, ``JOHNNY_CONTEXT``, ``JOHNNY_CALENDAR_CONTEXT``,
    ``JOHNNY_CALENDAR_ATTACHMENTS``, ``JOHNNY_PRIOR_SESSION_CONTEXT``).

    Every field defaults to ``""`` and an empty field renders nothing, so an
    unconfigured session degrades to the base framing alone (regression guard).
    """

    instructions: str = ""
    personality_prompt: str = ""
    context: str = ""
    calendar_context: str = ""
    calendar_attachments_text: str = ""
    prior_session_context: str = ""


def build_agent_instructions(config: AgentInstructionsConfig) -> str:
    """Assemble the persistent system prompt for :class:`JohnnyAgent`.

    Reuses the legacy answer-stage assembly order from
    ``VoicePipeline._answer_messages``: base framing → personality (FIRST, so
    the model adopts the character before it reads the job) → history note →
    meeting instructions → context → calendar description → calendar
    attachments → last-session summary. Per-turn-only pieces from the legacy
    builder (the router hint, ``allowed_replies``) are NOT part of the static
    instructions — the router gate (Johnny-xpa) and per-turn handlers own those.
    """
    system = _BASE_INSTRUCTIONS
    if config.personality_prompt:
        system += f"\n\n{config.personality_prompt}"
    system += f"\n\n{_HISTORY_NOTE}"
    if config.instructions:
        system += f"\n\nMeeting instructions: {config.instructions}"
    if config.context:
        system += f"\n\nContext: {config.context}"
    if config.calendar_context:
        system += f"\n\nCalendar event description: {config.calendar_context}"
    if config.calendar_attachments_text:
        system += (
            "\n\nCalendar attachments (linked documents from the event "
            f"description):\n{config.calendar_attachments_text}"
        )
    if config.prior_session_context:
        system += f"\n\nLast session summary: {config.prior_session_context}"
    return system


def transcripts_to_chat_ctx(history: Sequence[TranscriptFinalized]) -> ChatContext:
    """Map persisted transcripts into a LiveKit :class:`ChatContext`.

    Parity with ``VoicePipeline._rehydrate_transcript_history``: a container
    respawn mid-session would otherwise start the LiveKit chat context empty,
    so the bot would forget everything spoken before the restart. Each prior
    transcript becomes one chat message:

    * the bot's own utterances (``speaker == BOT_SPEAKER_LABEL``, Johnny-7qp) →
      ``role="assistant"`` so the model recognises them as its prior speech and
      can answer "what did you just say?" verbatim;
    * a participant utterance with a known speaker → ``role="user"`` prefixed
      ``"{speaker}: {text}"`` so multi-party attribution survives the restart
      (the legacy prompt rendered ``"- {speaker}: {text}"``);
    * a participant utterance with no speaker → ``role="user"`` with bare text.

    Items are appended in the loader's chronological order. ``created_at`` is
    left to default (so :meth:`ChatContext.add_message` appends rather than
    doing a sorted insert) — the rehydrated timestamps are heterogeneous
    (session-relative offsets for transcripts vs. epoch ms for bot utterances)
    and must not drive ordering; append order is the conversation order, and
    the default ``created_at`` (≈ now) keeps subsequent live turns sorting
    after the rehydrated history. Empty-text transcripts are skipped.
    """
    ctx = ChatContext.empty()
    for chunk in history:
        text = (chunk.text or "").strip()
        if not text:
            continue
        if chunk.speaker == BOT_SPEAKER_LABEL:
            ctx.add_message(role="assistant", content=text)
        elif chunk.speaker:
            ctx.add_message(role="user", content=f"{chunk.speaker}: {text}")
        else:
            ctx.add_message(role="user", content=text)
    return ctx


def load_vad() -> VAD:
    """Load the Silero VAD model (baked into the image at build time).

    Mirrors the starter's ``prewarm`` step. Kept as a module-level function
    so a worker can warm the model once per process and hand the same handle
    to every :func:`build_agent_session` call.
    """
    return silero.VAD.load()


def build_agent_session(
    *,
    stt: STT[Any],
    llm: LLM[Any],
    tts: TTS[Any],
    vad: VAD | None = None,
    preemptive_generation: bool = True,
) -> AgentSession[Any]:
    """Construct Johnny's ``AgentSession`` from provider adapter instances.

    ``stt`` / ``llm`` / ``tts`` are Johnny's own LiveKit plugin adapters
    (:mod:`johnny.agent.adapters`, Phase 1), built from the admin-active
    providers by the adapter factory (Johnny-zb3). Turn-taking uses LiveKit's
    multilingual turn detector plus Silero VAD per the locked decision;
    ``vad`` defaults to a freshly loaded Silero model when not supplied.
    """
    return AgentSession(
        stt=stt,
        llm=llm,
        tts=tts,
        vad=vad if vad is not None else load_vad(),
        turn_detection=MultilingualModel(),
        preemptive_generation=preemptive_generation,
    )


class JohnnyAgent(Agent):
    """Johnny's ``livekit.agents.Agent`` — instructions carrier + gate host.

    Carries the assembled meeting instructions (personality + brief + calendar
    + cross-session memory, see :func:`build_agent_instructions`) and seeds the
    LiveKit ``chat_ctx`` with rehydrated prior transcripts so a container
    respawn doesn't wipe the bot's memory (Johnny-re2). A later phase overrides
    ``on_user_turn_completed`` to run the router "should-speak" gate that raises
    ``StopResponse`` when Johnny should stay silent (Johnny-xpa).

    Construction is synchronous and takes a *pre-loaded* ``chat_history`` list;
    the async loader-driven path (parity with the legacy pipeline's injected
    :class:`TranscriptHistoryLoader`) is :func:`build_johnny_agent`.
    """

    def __init__(
        self,
        *,
        instructions: str | None = None,
        prompt_config: AgentInstructionsConfig | None = None,
        chat_history: Sequence[TranscriptFinalized] | None = None,
    ) -> None:
        if instructions is None:
            instructions = (
                build_agent_instructions(prompt_config)
                if prompt_config is not None
                else DEFAULT_INSTRUCTIONS
            )
        # Only build a ChatContext when there is history to seed; ``None`` lets
        # the base Agent start from ``ChatContext.empty()``.
        chat_ctx = transcripts_to_chat_ctx(chat_history) if chat_history else None
        super().__init__(instructions=instructions, chat_ctx=chat_ctx)


async def build_johnny_agent(
    *,
    prompt_config: AgentInstructionsConfig | None = None,
    instructions: str | None = None,
    transcript_history_loader: TranscriptHistoryLoader | None = None,
    session_id: str | None = None,
    bot_session_id: int | None = None,
) -> JohnnyAgent:
    """Build a :class:`JohnnyAgent`, rehydrating prior transcripts if available.

    Parity with ``VoicePipeline.run()`` → ``_rehydrate_transcript_history``: on
    a container respawn the injected loader pulls the durable transcript rows
    for this session (keyed off ``session_id`` / ``bot_session_id``, whichever
    the implementation prefers) and seeds them into the agent's LiveKit chat
    context so memory survives the restart.

    A missing loader defaults to :class:`NoopTranscriptHistoryLoader` (empty
    history — the bot starts fresh, e.g. a console session or a deployment with
    no rehydration endpoint). Loader exceptions are logged and the agent starts
    with empty history — better to lose context than to refuse to start, the
    same swallow-and-continue contract the legacy method holds.
    """
    loader = transcript_history_loader or NoopTranscriptHistoryLoader()
    history: list[TranscriptFinalized] = []
    try:
        history = list(
            await loader.load(session_id=session_id, bot_session_id=bot_session_id)
        )
    except Exception:
        logger.exception(
            "transcript history loader failed for session=%s — "
            "starting JohnnyAgent with empty history",
            session_id,
        )
    if history:
        logger.info(
            "rehydrated %d transcript chunks into JohnnyAgent for session=%s",
            len(history),
            session_id,
        )
    return JohnnyAgent(
        instructions=instructions,
        prompt_config=prompt_config,
        chat_history=history or None,
    )


__all__ = [
    "DEFAULT_INSTRUCTIONS",
    "AgentInstructionsConfig",
    "JohnnyAgent",
    "build_agent_instructions",
    "build_agent_session",
    "build_johnny_agent",
    "load_vad",
    "transcripts_to_chat_ctx",
]
