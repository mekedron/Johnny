"""AgentSession harness + JohnnyAgent (epic Johnny-7g5, Phase 0 → Phase 2).

This module wires Johnny's voice orchestration onto LiveKit Agents'
``AgentSession(stt, llm, tts, vad, turn_detection)``:

* the ``stt`` / ``llm`` / ``tts`` arguments are Johnny's own LiveKit plugin
  adapters (:mod:`johnny.agent.adapters`, Phase 1), built from
  ``load_active_providers()`` by the adapter factory (Johnny-zb3);
* :class:`JohnnyAgent` carries the assembled instructions (character +
  meeting-context + calendar prompt, reusing the legacy answer-stage prompt
  assembly) and rehydrates prior transcript history into the LiveKit
  ``chat_ctx`` on container respawn so memory survives restarts (Johnny-re2,
  parity with the legacy split pipeline);
* the router "should-speak" gate in ``on_user_turn_completed`` runs the
  decision via :class:`~johnny.agent.router_gate.RouterGate` (Johnny-xpa,
  Phase 2) on top of the gate harness (:mod:`johnny.agent.gate`), raising
  ``StopResponse`` to keep Johnny silent and routing every turn's terminal
  through the session :class:`~johnny.agent.gate.TurnLedger`;
* barge-in (Johnny-k8t, Phase 2) splits into the LiveKit-native fast VAD-onset
  interrupt (configured on the session via ``TurnHandlingOptions`` in
  :func:`build_agent_session`) and the slow out-of-band intent classifier
  (:class:`~johnny.agent.barge_in.BargeInClassifier`), which
  :meth:`JohnnyAgent.on_user_turn_completed` fires while the bot is mid-reply.

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
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from livekit.agents import (
    NOT_GIVEN,
    Agent,
    AgentSession,
    EndpointingOptions,
    InterruptionOptions,
    TurnHandlingOptions,
)
from livekit.agents.llm.chat_context import ChatContext
from livekit.agents.llm.chat_context import ChatMessage as LKChatMessage
from livekit.agents.stt import SpeechEvent, SpeechEventType
from livekit.plugins import silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from johnny.agent.adapters.johnny_llm import chat_ctx_to_messages
from johnny.agent.answer import (
    AnswerConfig,
    coerce_allowed_reply,
    iter_sentences,
    uses_allowlist,
)
from johnny.agent.noise_filter import (
    NoiseFilterConfig,
    TranscriptFilteredSink,
    classify_noise,
    classify_transcript_text,
)
from johnny.agent.speech_floor import normalize_speech_text
from johnny.voice_pipeline.events import TranscriptFiltered, TranscriptFinalized
from johnny.voice_pipeline.transcript_history import (
    BOT_SPEAKER_LABEL,
    NoopTranscriptHistoryLoader,
    TranscriptHistoryLoader,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, AsyncIterator, Callable, Sequence

    from livekit import rtc

    # STT / LLM / TTS are Generic over their event type (TEvent) and
    # AgentSession over Userdata_T; the harness is event/userdata-agnostic,
    # so it accepts any concrete adapter via the [Any] parametrization. The
    # Phase-1 adapters (johnny.agent.adapters) subclass with concrete events.
    from livekit.agents import FlushSentinel, ModelSettings
    from livekit.agents.llm import LLM, ChatChunk, Tool
    from livekit.agents.stt import STT, SpeechData
    from livekit.agents.tts import TTS
    from livekit.agents.vad import VAD
    from livekit.agents.voice import SpeechCreatedEvent
    from livekit.agents.voice.events import MetricsCollectedEvent

    from app.providers.base import LLMProvider
    from johnny.agent.barge_in import BargeInClassifier
    from johnny.agent.observability import SpeechInterimSink, TranscriptFinalizedSink
    from johnny.agent.router_gate import RouterGate
    from johnny.agent.speech_floor import PeerAttribution

    MetricsListener = Callable[[MetricsCollectedEvent], None]


class PeerFloorReader(Protocol):
    """The observer reads :class:`JohnnyAgent` needs from the speech floor.

    Structural subset of :class:`johnny.agent.speech_floor.SpeechFloor`
    (Johnny-trt.46) so tests drive the suppression seam with a plain fake.
    """

    def attribute_peer_final(self, text: str) -> PeerAttribution | None: ...

    def peer_window_active(self) -> bool: ...

logger = logging.getLogger(__name__)

# Placeholder instructions for a bare ``JohnnyAgent()`` — used only when
# neither an explicit ``instructions`` string nor a ``prompt_config`` is
# supplied (smoke tests / a console session with no meeting brief). A real
# scheduled session always builds instructions from :class:`AgentInstructionsConfig`.
DEFAULT_INSTRUCTIONS = "You are Johnny, an AI participant in a live voice meeting."

# Generic answer-stage framing, mirrored from
# the legacy split pipeline so a LiveKit-driven Johnny opens with the
# same job description the meet-worker answer LLM had. Deliberately nameless so
# a configured agent character (rendered next) owns the persona without conflict.
_BASE_INSTRUCTIONS = "You are an AI meeting participant. Produce concise, natural spoken replies."

# Note explaining the rehydrated/streamed conversation history, mirroring the
# legacy answer prompt's "Recent conversation" guidance (Johnny-7qp): assistant
# turns are the bot's own prior speech, user turns are participants (optionally
# speaker-prefixed). Lets the model answer "what did you just say?" after a
# respawn from the rehydrated assistant turns.
_HISTORY_NOTE = (
    "Earlier turns in this meeting are provided as conversation history: "
    'assistant turns are your own prior speech (ground any "what did you '
    'say?" answer in their exact words), and user turns are meeting '
    "participants, sometimes prefixed with the speaker's name."
)

# Capability self-awareness guard (Johnny-etu.17). ALWAYS rendered into the
# answer-side system prompt, even when the dynamic capability block below is
# empty — so the small answer model can never be left with nothing to ground a
# "what can you do?" answer on and fill the gap with either a roleplay
# (session-7 cyberpunk "hacking, surveillance") or a deflection (session-1
# "I'm just a bot / not connected"). It also disambiguates a capability ask
# from a tool RUN, so the model describes its tools instead of replaying an
# earlier task's result (the session-6 stale-result repeat). Worded for spoken
# delivery. It pairs with the dynamic capability notes
# (:func:`johnny.agent.task_catalog.render_capability_notes`): this note is the
# behavior guard + honest baseline, the notes are the real tool list when the
# session's catalog has one. Deliberately avoids the optional-section markers
# ("Context:", "Calendar …", "Last session summary:", "Meeting instructions:")
# and the answer-notes token "CANNOT" so the assembly-order/parity guards stay
# byte-stable.
_SELF_AWARENESS_NOTE = (
    "Know what you actually are, and answer honestly when asked about it. You "
    "are a real AI assistant taking part in this live meeting through actual "
    "software, with a real — possibly small — set of tools wired into this "
    "session. You are not a fictional character and not a generic chatbot, "
    "whatever personality or tone of voice you adopt. "
    "When someone asks what you can do, what skills or tools you have, or "
    "whether you can do something, answer it yourself, plainly and out loud. "
    "The capabilities listed below are YOUR OWN skills: name them and say what "
    "each one is for (for example, that you can look up the calendar), and "
    "offer to use them. Naming a tool and its purpose is exactly what is "
    "wanted — it is NOT the same as inventing a result, so the rule against "
    "stating specifics covers only a tool's actual results (events, times, "
    "names, counts), never whether the tool exists. Do not retreat to listing "
    "generic chat abilities when you have real tools listed below, and never "
    "answer by repeating the result of an earlier task. "
    "Never invent or role-play abilities you do not have: you have no powers "
    "beyond your listed tools, so do not present yourself as a hacker, a spy, "
    "a surveillance system, or any other fictional operator. "
    "Never go the other way and deny yourself either — do not say you are "
    "'just a bot', that you are 'not really here', 'not connected', or unable "
    "to do anything; you are a working assistant and at minimum you can hear, "
    "talk, and help think things through. "
    "If no specific tools are listed below, say plainly that you don't have "
    "any special tools set up in this session right now and that you're a "
    "voice meeting assistant who can listen, discuss, and answer questions — "
    "but still never invent tools and never claim you can do nothing at all."
)


# openclaw-style native tool-use guidance (Johnny-3ow). Rendered ONLY when the
# agent carries the sandbox tools (the cutover flag), so a tool-less session's
# prompt stays byte-identical. Teaches the model the generic tool surface
# (exec/read/write/list_dir), the "skills are files you discover and run" model
# (list_dir → read SKILL.md → exec with the user's REAL args — the fix for the
# always-London arg-drop bug), and the etu.7 grounding rules (call the tool,
# answer from its real output, never fabricate a result, report failures).
# Worded for spoken delivery; deliberately avoids the answer-notes token
# "CANNOT" so the byte-stability/ordering guards stay stable.
TOOL_USE_NOTES = (
    "You have real tools wired into this session, and you call them yourself:\n"
    "- exec: run a command inside your own private sandbox container.\n"
    "- read: read a file from that sandbox.\n"
    "- write: create or overwrite a file in that sandbox.\n"
    "- list_dir: list a directory in that sandbox.\n"
    "Your skills live as files under /skills. When a request matches something a "
    "skill can do, discover and run it: call list_dir('/skills') to see what you "
    "have, read the matching /skills/<name>/SKILL.md to learn how it runs, then "
    "run it with exec — passing the person's REAL values (a city, a ticker, a "
    "date) as the command's arguments. Never substitute a placeholder or fall "
    "back to a default: if they asked about Helsinki, pass Helsinki. "
    "When you actually need to do one of these things, call the tool — do not "
    "merely say you will, and do not make up the answer. Then answer the person "
    "from what the tool actually returned, out loud and concise. If a tool fails "
    "or returns nothing useful, say plainly what went wrong and offer to try "
    "again; never invent a result you did not get. These tools are part of your "
    "real capabilities, so when asked what you can do you may name them and "
    "offer to use them, and you can always check /skills first if you are unsure "
    "what is installed.\n"
    "You may also have external MCP connectors wired in (a workflow engine, a "
    "database, an analytics tool, and so on). When a request might need an "
    "outside system — or when asked what connectors or integrations you have — "
    "call list_mcp_servers to see what is available. To use one, call "
    "list_mcp_tools('<server>') to load that connector's tools and their argument "
    "signatures, then call_mcp_tool('<server>', '<tool>', {…}) with the person's "
    "REAL values. Use the EXACT connector name from list_mcp_servers (for example "
    "mcp-metabase-server, not metabase). Actually CALL these tools — never just "
    "describe a call or write it out as text — and after loading a connector's "
    "tools, pick the single best one and call it instead of listing again. Answer "
    "from what the connector actually returns."
)


@dataclass(frozen=True, slots=True)
class AgentInstructionsConfig:
    """Static prompt components assembled into ``Agent.instructions``.

    Mirrors the subset of the legacy pipeline config
    the legacy answer LLM rendered into its system message — the agent's
    character identity layer (Johnny-trt.41), the per-assignment brief
    (``context``, Johnny-trt.45 — the ONE free-text slot an assignment
    carries; everything else comes from the agent profile), the calendar
    background, and the cross-session memory (Johnny-dsy). The agent worker
    fills ``character_prompt`` / ``context`` from the job payload's frozen
    agent snapshot and the calendar fields from the payload's per-field
    mirrors (``JOHNNY_CALENDAR_CONTEXT``, ``JOHNNY_CALENDAR_ATTACHMENTS``,
    ``JOHNNY_PRIOR_SESSION_CONTEXT``). The free-form ``instructions`` slot
    (the old per-meeting/system-prompt override) was retired with the
    Johnny-trt.45 assignment reshape.

    Every field defaults to ``""`` and an empty field renders nothing, so an
    unconfigured session degrades to the base framing alone (regression guard).

    ``capability_notes`` (Johnny-trt.55 + Johnny-etu.7) is the
    capability-grounding block (:func:`johnny.agent.task_catalog.render_capability_notes`),
    filled by the runtime assembly from the session's task catalog: what the
    session CAN do (so the answer model never denies an available capability)
    and what it CANNOT (so it declines an impossible ask with the reason and
    the fix rather than improvising a pretend-check).
    """

    character_prompt: str = ""
    context: str = ""
    calendar_context: str = ""
    calendar_attachments_text: str = ""
    prior_session_context: str = ""
    capability_notes: str = ""
    tool_use_notes: str = ""


def build_agent_instructions(config: AgentInstructionsConfig) -> str:
    """Assemble the persistent system prompt for :class:`JohnnyAgent`.

    Reuses the legacy answer-stage assembly order from
    the legacy split pipeline: base framing → character (FIRST, so
    the model adopts the character before it reads the job) → history note →
    self-awareness guard (Johnny-etu.17 — ALWAYS present: who you really are,
    answer capability asks from your real tools, never roleplay a fake ability
    and never deflect "I'm just a bot"; rendered after the character so it
    outranks roleplay habits) → capability notes (Johnny-trt.55 + Johnny-etu.7
    — what the session CAN and CANNOT do, absent only when it has no user-facing
    capability and no gap, so that block stays byte-identical) →
    tool-use notes (Johnny-3ow — the openclaw skill-discovery + native
    exec/read/write/list_dir guidance, present only when the agent carries the
    sandbox tools) →
    context (the per-assignment brief — the documented Johnny-trt.45 slot)
    → calendar description → calendar attachments → last-session summary.
    The retired "Meeting instructions" line rendered between capability
    notes and context; it was empty for every post-trt.41 session, so its
    removal keeps the assembled prompt byte-identical. Per-turn-only pieces
    from the legacy builder (the router hint, ``allowed_replies``) are NOT
    part of the static instructions — the router gate (Johnny-xpa) and
    per-turn handlers own those.
    """
    system = _BASE_INSTRUCTIONS
    if config.character_prompt:
        system += f"\n\n{config.character_prompt}"
    system += f"\n\n{_HISTORY_NOTE}"
    # Self-awareness guard (Johnny-etu.17): always present so a capability ask
    # is never answered from an empty prompt — the small model's roleplay /
    # deflection / stale-result failure modes. Sits after the character (so the
    # honest-identity rules outrank a roleplay persona) and before the dynamic
    # capability notes, which it explicitly defers to for the real tool list.
    system += f"\n\n{_SELF_AWARENESS_NOTE}"
    if config.capability_notes:
        # Capability grounding (Johnny-trt.55 + Johnny-etu.7): rendered AFTER
        # the character so the can-do / no-pretend-check rules outrank roleplay
        # habits, and before the assignment brief so that can refine rather than
        # be contradicted (the router prompt's catalog-ordering rationale).
        system += f"\n\n{config.capability_notes}"
    if config.tool_use_notes:
        # Native tool-use guidance (Johnny-3ow): after the capability notes
        # (the catalog grounding) and before the assignment brief, so the
        # "call your real tools, pass real args, answer from the result" rules
        # outrank a roleplay persona while the per-assignment context can still
        # refine them. Empty unless the agent carries sandbox tools, so a
        # tool-less session's prompt stays byte-identical.
        system += f"\n\n{config.tool_use_notes}"
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

    Parity with the legacy split pipeline: a container
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


def load_vad(*, min_silence_duration: float | None = None) -> VAD:
    """Load the Silero VAD model (baked into the image at build time).

    Mirrors the starter's ``prewarm`` step. Kept as a module-level function
    so a worker can warm the model once per process and hand the same handle
    to every :func:`build_agent_session` call.

    ``min_silence_duration`` overrides Silero's end-of-speech silence floor
    (Johnny-trt.5); ``None`` loads with Silero's own defaults (0.55 s floor),
    which is what the Meet/room path always does — its padding is multi-party
    turn-taking headroom (Johnny-arh). The browser playground loads its
    tighter floor through
    :func:`johnny.agent.browser_session.load_browser_vad`.
    """
    if min_silence_duration is None:
        return silero.VAD.load()
    return silero.VAD.load(min_silence_duration=min_silence_duration)


def _speech_alt_duration_ms(alt: SpeechData | None) -> int | None:
    """Segment duration (ms) of an STT alternative, or ``None`` when unknown.

    The pre-STT audio floor needs the VAD-cut speech duration; a LiveKit
    ``SpeechData`` carries it as ``end_time - start_time`` (seconds) when the
    provider reports per-segment timing. Johnny's STT adapters stamp
    ``start_time == end_time`` (a ``TranscriptEvent`` carries a single offset), so
    the span is ``0`` → ``None`` → the audio floor is skipped and only the content
    gate applies. A genuinely positive span is rounded to whole milliseconds.
    """
    if alt is None:
        return None
    span_s = alt.end_time - alt.start_time
    if span_s <= 0:
        return None
    return round(span_s * 1000)


MAX_TOOL_STEPS = 8
"""Cap on native tool-loop steps per turn (Johnny-3ow). Above the openclaw
discovery recipe's 3 steps (list_dir → read SKILL.md → exec) with headroom for
a retry, below an unbounded loop a weak model could spin in."""


def build_agent_session(
    *,
    stt: STT[Any],
    llm: LLM[Any],
    tts: TTS[Any] | None = None,
    vad: VAD | None = None,
    preemptive_generation: bool = False,
    enable_barge_in: bool = True,
    min_interruption_duration_s: float | None = None,
    turn_detection: Any = None,
    endpointing: EndpointingOptions | None = None,
) -> AgentSession[Any]:
    """Construct Johnny's ``AgentSession`` from provider adapter instances.

    ``stt`` / ``llm`` / ``tts`` are Johnny's own LiveKit plugin adapters
    (:mod:`johnny.agent.adapters`, Phase 1), built from the admin-active
    providers by the adapter factory (Johnny-zb3). Turn-taking uses LiveKit's
    multilingual turn detector plus Silero VAD per the locked decision;
    ``vad`` defaults to a freshly loaded Silero model when not supplied.

    ``tts`` is optional (Johnny-un2): ``None`` binds no TTS to the session (it is
    passed as ``NOT_GIVEN``, so ``AgentSession`` runs with ``_tts = None``), which
    :meth:`JohnnyAgent.tts_node` degrades on — it consumes the answer text and emits
    no audio instead of raising. The adapter factory returns ``tts=None`` when no TTS
    provider is configured, and the worker degrades the speaking mode to
    ``suggest_only`` (parity with the meet-worker's graceful TTS-missing degrade), so
    a reply is never generated for the silent session in the first place.

    ``turn_detection`` overrides the turn-detection strategy. It defaults to the
    :class:`~livekit.plugins.turn_detector.multilingual.MultilingualModel` (the
    Meet path's locked decision), but that model resolves its inference executor
    from the live LiveKit ``get_job_context()`` and so cannot run outside a
    dispatched job. The in-browser playground (Johnny-7g5.1) runs the session
    *in the API process* with no job context, so it passes either ``"vad"``
    (Silero VAD endpointing, the legacy browser pipeline's own turn-taking) or
    — for English STT configs — the in-process
    :class:`~johnny.agent.turn_detector.InProcessEnglishModel`, whose executor
    needs no job context (Johnny-1qr). Any value LiveKit accepts (a model,
    ``"vad"`` / ``"stt"`` / …) is forwarded verbatim.

    ``preemptive_generation`` defaults to ``False`` because Johnny gates every
    turn through the router "should-speak" decision in
    :meth:`JohnnyAgent.on_user_turn_completed` (Johnny-xpa): preemptive
    generation would (a) burn answer-LLM tokens producing a reply the gate may
    decline, defeating the gate's purpose, and (b) fire ``speech_created``
    *before* the hook runs, breaking the reply→turn correlation
    (:meth:`RouterGate.bind_reply` relies on the reply being created *after* the
    gate records the turn). Leave it ``False`` whenever a :class:`RouterGate`
    is attached.

    ``enable_barge_in`` (default ``True``) configures the **fast VAD-onset
    interrupt as LiveKit-native** (Johnny-k8t): it toggles
    ``TurnHandlingOptions``' ``interruption.enabled``, so confirmed user speech
    stops the bot's TTS the moment it crosses ``min_duration`` — no Johnny code
    on the hot path. Setting it ``False`` disables interruption entirely (the
    operator should also disable the out-of-band classifier,
    :class:`~johnny.agent.barge_in.BargeInClassifier`, to mirror the legacy
    ``enable_barge_in`` flag that gated both paths). ``min_interruption_duration_s``
    overrides the native interrupt threshold; ``None`` leaves LiveKit's own
    default (0.5 s) in place (see
    :data:`~johnny.agent.barge_in.DEFAULT_NATIVE_INTERRUPTION_MIN_DURATION_S` for
    the legacy fast-trigger value).

    ``endpointing`` (Johnny-trt.5) overrides the engine's end-of-turn
    endpointing dict (``TurnHandlingOptions["endpointing"]``, e.g.
    ``{"min_delay": 0.4}``; missing keys inherit the SDK defaults). ``None``
    leaves the key unset entirely, so LiveKit's own defaults apply
    (``min_delay`` 0.5 s / ``max_delay`` 3.0 s) — the Meet/room path
    (:mod:`johnny.agent.worker`) passes nothing and keeps the pre-trt.5
    semantics. Note the ``min_delay`` wait is anchored to the *last detected
    speech*, so it overlaps the VAD's own ``min_silence_duration`` wait
    rather than stacking on top of it: the effective turn-commit floor is
    ``max(vad min_silence, endpointing min_delay)`` after the user stops.
    """
    return AgentSession(
        stt=stt,
        llm=llm,
        tts=tts if tts is not None else NOT_GIVEN,
        vad=vad if vad is not None else load_vad(),
        # Native tool loop depth (Johnny-3ow): the SDK default (3) is too low
        # for the openclaw discovery recipe — list_dir('/skills') → read the
        # SKILL.md → exec the run script is already 3 steps, leaving no room
        # for a retry or a follow-up read. 8 bounds a runaway loop while
        # leaving headroom; tool-less sessions never reach a tool step, so this
        # is inert for them.
        max_tool_steps=MAX_TOOL_STEPS,
        turn_handling=build_turn_handling(
            turn_detection=turn_detection,
            preemptive_generation=preemptive_generation,
            enable_barge_in=enable_barge_in,
            min_interruption_duration_s=min_interruption_duration_s,
            endpointing=endpointing,
        ),
    )


def build_turn_handling(
    *,
    turn_detection: Any = None,
    preemptive_generation: bool = False,
    enable_barge_in: bool = True,
    min_interruption_duration_s: float | None = None,
    endpointing: EndpointingOptions | None = None,
) -> TurnHandlingOptions:
    """Build the session's ``turn_handling`` options dict (Johnny-trt.5).

    Factored out of :func:`build_agent_session` for the same reason as
    :func:`build_interruption_options`: the kwarg→options mapping is
    unit-testable without a job context — pass ``turn_detection="vad"`` in
    tests, because the default constructs the job-context-bound
    :class:`MultilingualModel`. ``endpointing=None`` omits the key so LiveKit
    applies its own defaults (``min_delay`` 0.5 / ``max_delay`` 3.0); a given
    dict is forwarded verbatim.
    """
    turn_handling: TurnHandlingOptions = {
        "turn_detection": turn_detection if turn_detection is not None else MultilingualModel(),
        "preemptive_generation": {"enabled": preemptive_generation},
        "interruption": build_interruption_options(
            enable_barge_in=enable_barge_in,
            min_interruption_duration_s=min_interruption_duration_s,
        ),
    }
    if endpointing is not None:
        turn_handling["endpointing"] = endpointing
    return turn_handling


def build_interruption_options(
    *,
    enable_barge_in: bool = True,
    min_interruption_duration_s: float | None = None,
) -> InterruptionOptions:
    """Build the native interruption (fast VAD-onset barge-in) config (Johnny-k8t).

    ``enable_barge_in`` maps to ``interruption.enabled`` — the LiveKit-native
    fast path that stops the bot's TTS the instant confirmed speech crosses the
    duration threshold. ``min_interruption_duration_s`` overrides that threshold
    (``interruption.min_duration``); ``None`` leaves the key unset so LiveKit
    applies its own default (0.5 s). Factored out of :func:`build_agent_session`
    so the mapping is unit-testable without a job context (constructing the
    turn detector requires one).
    """
    options: InterruptionOptions = {"enabled": enable_barge_in}
    if min_interruption_duration_s is not None:
        options["min_duration"] = min_interruption_duration_s
    return options


class JohnnyAgent(Agent):
    """Johnny's ``livekit.agents.Agent`` — instructions carrier + gate host.

    Carries the assembled meeting instructions (character + brief + calendar
    + cross-session memory, see :func:`build_agent_instructions`) and seeds the
    LiveKit ``chat_ctx`` with rehydrated prior transcripts so a container
    respawn doesn't wipe the bot's memory (Johnny-re2).

    When a :class:`~johnny.agent.router_gate.RouterGate` is attached,
    :meth:`on_user_turn_completed` runs the router "should-speak" decision and
    raises ``StopResponse`` when Johnny should stay silent (Johnny-xpa), and
    :meth:`on_enter` registers a ``speech_created`` listener so each reply's
    completion emits the speak path's terminal. With no gate the agent replies
    to every turn (the bare-construction / smoke-test behaviour).

    When a :class:`~johnny.agent.barge_in.BargeInClassifier` is attached,
    :meth:`on_user_turn_completed` *also* spawns the out-of-band barge-in
    classifier (Johnny-k8t) whenever the bot is mid-reply, so an actionable
    interruption below the native VAD threshold still yields the floor. The fast
    VAD-onset interrupt itself is LiveKit-native (configured on the session, see
    :func:`build_agent_session`), so nothing happens here for it.

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
        router_gate: RouterGate | None = None,
        barge_in: BargeInClassifier | None = None,
        answer_llm: LLMProvider | None = None,
        answer_config: AnswerConfig | None = None,
        tts_available: bool = True,
        noise_filter: NoiseFilterConfig | None = None,
        transcript_filtered_sink: TranscriptFilteredSink | None = None,
        transcript_finalized_sink: TranscriptFinalizedSink | None = None,
        speech_interim_sink: SpeechInterimSink | None = None,
        metrics_listener: MetricsListener | None = None,
        session_id: str | None = None,
        peer_floor: PeerFloorReader | None = None,
        agent_display_name: str = "",
        sandbox_tools: list[Tool] | None = None,
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
        # ``sandbox_tools`` (Johnny-3ow) are the native exec/read/write/list_dir
        # tools the answer LLM calls directly; LiveKit surfaces them to
        # :meth:`llm_node` and runs the tool loop. ``None`` (the default / no
        # cutover flag) keeps the tool-less behaviour byte-identical.
        super().__init__(
            instructions=instructions, chat_ctx=chat_ctx, tools=sandbox_tools or None
        )
        self._router_gate = router_gate
        self._barge_in = barge_in
        # Answer-path config (Johnny-5ag). ``answer_llm`` is the raw answer
        # ``LLMProvider`` used for allowed-reply coercion (a separate structured
        # call, mirroring the legacy ``answer_llm.chat(response_format=...)``);
        # ``answer_config`` carries the mode + allow-list the node overrides read;
        # ``tts_available`` is the graceful-degrade signal (a missing TTS makes
        # ``tts_node`` emit no audio instead of crashing). All optional so a
        # bare/smoke ``JohnnyAgent`` keeps the default reply behaviour.
        self._answer_llm = answer_llm
        self._answer_config = answer_config
        self._tts_available = tts_available
        # Noise gate (Johnny-cmd). ``noise_filter`` carries the stoplist /
        # length / duration / confidence thresholds the ``stt_node`` applies to
        # each final transcript; ``transcript_filtered_sink`` publishes a
        # ``TranscriptFiltered`` for every dropped candidate (``None`` = no
        # observability wiring, e.g. a smoke agent); ``session_id`` stamps that
        # event. ``noise_filter=None`` leaves ``stt_node`` a transparent
        # pass-through (the bare/smoke default), like the answer-path nodes.
        self._noise_filter = noise_filter
        self._transcript_filtered_sink = transcript_filtered_sink
        # Observability parity (Johnny-d5z). ``transcript_finalized_sink`` publishes
        # a ``TranscriptFinalized`` for every STT final the noise gate KEEPS (the
        # durable transcript the subscriber writes to ``transcript_chunks``); the
        # mirror of ``transcript_filtered_sink`` for the non-noise path.
        # ``metrics_listener`` is the session ``metrics_collected`` callback (the
        # :class:`~johnny.agent.observability.MetricsTranslator`) that maps LiveKit
        # provider metrics to ``PipelineTiming`` events. Both ``None`` on a
        # bare/smoke agent (no emission).
        self._transcript_finalized_sink = transcript_finalized_sink
        # Live bot-reply captions (Johnny-trt.39). ``speech_interim_sink``
        # publishes an ephemeral ``AgentSpeechInterim`` per sentence ``tts_node``
        # flushes into TTS — sync + fire-and-forget so the audio hot path never
        # waits on the bus. ``None`` on a bare/smoke agent (no emission).
        self._speech_interim_sink = speech_interim_sink
        self._metrics_listener = metrics_listener
        self._session_id = session_id
        # Peer-speech suppression (Johnny-trt.46). In a multi-agent meeting
        # the co-agents' TTS plays into the same room audio this session
        # transcribes; ``peer_floor`` (the session's SpeechFloor, or any
        # object with its observer reads) attributes STT finals that land
        # inside a peer's broadcast floor window — or match its published
        # spoken text — to that peer. Attributed finals are recorded in the
        # transcript labeled with the peer agent's name and DROPPED from the
        # SDK stream so the turn never begins (the strict v1 loop rule: a bot
        # never responds to peer-bot speech; arbitration is Johnny-trt.47).
        # ``None`` (single-agent / playground) leaves the node untouched.
        self._peer_floor = peer_floor
        # Deliberate by-name handoffs (Johnny-trt.47): peer speech that names
        # THIS agent opens a turn instead of being suppressed — the relaxation
        # of the trt.46 strict never-respond-to-peers rule. The display name
        # is the floor/transcript identity; the budget flag bounds bot-to-bot
        # chains to ONE handoff per human utterance (a kept human final
        # restores it), which keeps "never loops" a deterministic guarantee
        # rather than a prompt hope. The trt.52 name-alias matcher will
        # replace the plain display-name match here.
        self._agent_display_name = agent_display_name.strip()
        self._peer_handoff_spent = False
        # Session-start reference for transcript ``timestamp_ms`` (Johnny-7g5.1).
        # The status subscriber writes ``timestamp_ms`` into
        # ``transcript_chunks.start_offset_ms`` (a 4-byte INTEGER) as an
        # offset-from-start, mirroring the legacy split pipeline
        # (``loop.time() - session_started_at``). Emitting raw epoch-ms here
        # overflows that column on Postgres, so transcript timestamps are
        # session-relative from agent construction.
        self._session_started_at = time.monotonic()

    async def on_enter(self) -> None:
        """Wire the reply→turn correlation + metrics translation once active.

        Registers a session ``metrics_collected`` listener (Johnny-d5z) that maps
        LiveKit provider metrics onto ``PipelineTiming`` events for the activity
        log, and a ``speech_created`` listener that hands every ``generate_reply``
        reply to :meth:`RouterGate.bind_reply` so the reply's done-callback emits
        the turn's terminal (the speak path's INV-1 record). Each is independent —
        the metrics listener runs with no gate; the ``speech_created`` binding
        no-ops without one.

        Also attaches ``session.say`` to the gate (Johnny-trt.17) so its
        delegate/status branches can speak acks out of band: the session only
        exists once the agent is active, and ``say()``'s ``speech_created``
        fires with ``source="say"``, so the ``generate_reply`` FIFO below never
        sees those speeches — the gate owns their terminals via the returned
        ``SpeechHandle`` directly.
        """
        listener = self._metrics_listener
        if listener is not None:
            self.session.on("metrics_collected", listener)

        gate = self._router_gate
        if gate is None:
            return
        gate.attach_say(self.session.say)

        def _on_speech_created(ev: SpeechCreatedEvent) -> None:
            if ev.source == "generate_reply":
                gate.bind_reply(ev.speech_handle)

        self.session.on("speech_created", _on_speech_created)

        # Conversation dynamics (Johnny-trt.49): feed the gate's interruption
        # monitor the VAD-confirmed user speech edges so a cut speech's settle
        # path attributes user-over-bot barge-ins and measures the
        # onset→audio-stop cut latency. Same duck-typed ``new_state`` read as
        # the task deliverer's listener (Johnny-trt.28).
        def _on_user_state_changed(ev: Any) -> None:
            state = str(getattr(ev, "new_state", ""))
            if state == "speaking":
                gate.note_user_speech_onset()
            elif state in ("listening", "away"):
                gate.note_user_speech_ended()

        self.session.on("user_state_changed", _on_user_state_changed)

    async def on_exit(self) -> None:
        """Tear down the gate + approval coordinator at session end (Johnny-qzj).

        Cancels any in-flight approval resolver (settling its parked turn
        ``approval_rejected``) and sweeps the session ledger so every turn carries
        its INV-1 terminal even on a hard teardown. No-op without a gate.
        """
        gate = self._router_gate
        if gate is not None:
            await gate.aclose()

    async def on_user_turn_completed(
        self, turn_ctx: ChatContext, new_message: LKChatMessage
    ) -> None:
        """Run the router should-speak gate before the SDK generates a reply.

        First spawns the out-of-band barge-in classifier (Johnny-k8t) if the bot
        is mid-reply — a fire-and-forget task that may interrupt the current
        reply for an actionable verdict, running concurrently with the gate below
        (it never blocks the turn). Then delegates to :meth:`RouterGate.run_turn`,
        which raises ``StopResponse`` to keep Johnny silent (no-speak /
        low-confidence / rate-limited / gate timeout / barge-in) and returns to
        let the reply proceed. With no gate attached the turn always proceeds
        (default ``Agent`` behaviour).
        """
        self._maybe_spawn_barge_in(new_message)
        if self._router_gate is None:
            return
        await self._router_gate.run_turn(turn_ctx, new_message)

    def _maybe_spawn_barge_in(self, new_message: LKChatMessage) -> None:
        """Spawn the out-of-band barge-in classifier when the bot is mid-reply.

        No-op unless a :class:`~johnny.agent.barge_in.BargeInClassifier` is
        attached and enabled and there is a current, unfinished reply to target.
        The interrupt target is the session's ``current_speech`` (authoritative
        "what is playing now"); its LiveKit turn id is read from the gate's
        tracked active reply when it matches, else the speech id is used as a
        stable, non-counter label. The classifier re-checks ``current_speech`` at
        verdict time so a stale verdict cannot interrupt a newer reply.
        """
        barge_in = self._barge_in
        if barge_in is None or not barge_in.enabled:
            return
        target = self.session.current_speech
        if target is None or target.done():
            return
        text = (new_message.text_content or "").strip()
        if not text:
            return
        turn_id = target.id
        gate = self._router_gate
        if gate is not None:
            active = gate.active_reply
            if active is not None and active[1] is target:
                turn_id = active[0]
        barge_in.spawn(
            text=text,
            speaker=None,
            target=target,
            target_turn_id=turn_id,
            current_speech=lambda: self.session.current_speech,
        )

    def _relative_ms(self) -> int:
        """Session-relative timestamp (ms) for transcript events (Johnny-7g5.1).

        The offset-from-start the status subscriber writes into the INTEGER
        ``transcript_chunks.start_offset_ms`` column (parity with the legacy
        the legacy split pipeline), so a transcript can never overflow it with a
        raw epoch-ms value.
        """
        return max(0, int((time.monotonic() - self._session_started_at) * 1000))

    # ------------------------------------------------------------------ #
    # STT noise gate (Johnny-cmd)                                         #
    # ------------------------------------------------------------------ #

    async def stt_node(
        self,
        audio: AsyncIterable[rtc.AudioFrame],
        model_settings: ModelSettings,
    ) -> AsyncIterator[SpeechEvent | str]:
        """Transcribe, dropping noise candidates before they can open a turn (Johnny-cmd).

        Port of the legacy split pipeline noise gate
        (Johnny-ckz.14) into the LiveKit STT node. The default node
        (:meth:`Agent.default.stt_node`) drives the session STT; this override
        wraps its event stream and, when a :class:`~johnny.agent.noise_filter.NoiseFilterConfig`
        is configured, runs each transcript through the gate
        (:func:`~johnny.agent.noise_filter.classify_noise`): a cough / lip-smack /
        filler / Whisper hallucination is **dropped** so the SDK's audio
        recognition never accumulates it into the user turn and the turn detector
        never fires — no ``on_user_turn_completed``, no router call, no terminal
        (the legacy "the turn never begins" contract).

        A dropped ``FINAL_TRANSCRIPT`` publishes a
        :class:`~johnny.voice_pipeline.events.TranscriptFiltered` (the durable
        record, one per utterance, as the legacy ``_publish_noise_filtered`` did).
        A noise ``INTERIM_TRANSCRIPT`` is suppressed *silently* (no event): the SDK
        promotes a leftover interim to a final at turn-commit
        (``AudioRecognition._commit_user_turn``), so a passed-through "uh" interim
        would re-open the turn the dropped final was meant to stop — but the final
        is authoritative, so dropping the interim only suppresses its live display.
        Every non-transcript event (start/end-of-speech, usage) and, with no filter
        configured, every event passes straight through (the bare/smoke default).
        """
        source = Agent.default.stt_node(self, audio, model_settings)
        async for event in self._gate_stt_events(source):
            yield event

    async def _gate_stt_events(
        self, source: AsyncIterable[SpeechEvent | str]
    ) -> AsyncIterator[SpeechEvent | str]:
        """Apply the noise gate to an STT event stream (the pure wrapper, Johnny-cmd).

        Split out of :meth:`stt_node` so the gate is unit-testable on a crafted
        event stream without a running ``AgentActivity`` (the default STT source
        needs one), mirroring how :func:`~johnny.agent.answer.iter_sentences` is
        tested apart from :meth:`tts_node`. With no filter configured — or for a
        non-:class:`SpeechEvent` item — the item passes through untouched. A noise
        ``FINAL_TRANSCRIPT`` is dropped *and* publishes its
        :class:`~johnny.voice_pipeline.events.TranscriptFiltered`; a noise
        ``INTERIM_TRANSCRIPT`` is dropped silently so it cannot be promoted at
        turn-commit; everything else is yielded.
        """
        config = self._noise_filter
        async for event in source:
            if not isinstance(event, SpeechEvent):
                yield event
                continue
            if event.type is SpeechEventType.FINAL_TRANSCRIPT:
                if config is not None and config.enabled:
                    dropped = self._classify_noise_final(event, config)
                    if dropped is not None:
                        await self._emit_transcript_filtered(dropped)
                        continue
                # Peer-speech suppression (Johnny-trt.46): a kept final that
                # lands inside a co-agent's floor window — or matches its
                # published spoken text — is the PEER's voice, not a
                # participant's. Recorded in the durable transcript labeled
                # with the peer agent's name, then dropped from the SDK
                # stream so the turn never begins (the noise-gate "turn never
                # begins" contract) — UNLESS the peer addressed this agent by
                # name (Johnny-trt.47): a deliberate handoff opens a turn,
                # bounded to one hop per human utterance so bot-to-bot
                # exchange can never loop. The yielded copy is prefixed with
                # the peer's name so the router (and the answer model) see
                # who is asking; the durable transcript keeps the peer label
                # + original text either way.
                peer = self._attribute_peer_final(event)
                if peer is not None:
                    await self._emit_transcript_finalized(event, speaker_override=peer)
                    if self._is_peer_handoff(event):
                        self._peer_handoff_spent = True
                        alt = event.alternatives[0]
                        logger.info(
                            "peer handoff accepted for session=%s: %r hands to %r "
                            "text=%r",
                            self._session_id,
                            peer,
                            self._agent_display_name,
                            alt.text,
                        )
                        alt.text = f"{peer}: {alt.text}"
                        yield event
                    continue
                # Kept final → durable transcript (Johnny-d5z). Emitted whether or
                # not the noise gate is configured, so a session with filtering off
                # still records its transcripts. A human spoke: the peer-handoff
                # budget is restored (Johnny-trt.47).
                self._peer_handoff_spent = False
                await self._emit_transcript_finalized(event)
                yield event
                continue
            if event.type is SpeechEventType.INTERIM_TRANSCRIPT:
                # A peer-window interim is suppressed silently for the same
                # reason a noise interim is (Johnny-cmd): the SDK promotes a
                # leftover interim to a final at turn-commit, which would
                # re-open the turn the dropped final was meant to stop.
                if self._peer_floor is not None and self._peer_floor.peer_window_active():
                    continue
                if config is not None and config.enabled and self._interim_is_noise(event, config):
                    continue
            yield event

    def _attribute_peer_final(self, event: SpeechEvent) -> str | None:
        """The peer agent name a kept STT final attributes to, or ``None``.

        Defensive wrapper over the floor's observer read (Johnny-trt.46): no
        floor → no suppression; an attribution failure keeps the final as
        participant speech (suppressing real users on a bug would be worse
        than one bot-to-bot echo). Logged at ``info`` per suppression so
        cross-talk is visible in production tails — the durable record is the
        floor's ``PeerSpeechSuppressed`` window event.
        """
        floor = self._peer_floor
        if floor is None:
            return None
        alt = event.alternatives[0] if event.alternatives else None
        text = alt.text if alt is not None else ""
        if not text.strip():
            return None
        try:
            attribution = floor.attribute_peer_final(text)
        except Exception:
            logger.exception(
                "peer floor attribution failed for session=%s — keeping the "
                "final as participant speech",
                self._session_id,
            )
            return None
        if attribution is None:
            return None
        logger.info(
            "peer speech suppressed for session=%s: attributed to %r via %s "
            "(text_matched=%s) text=%r",
            self._session_id,
            attribution.agent,
            attribution.via,
            attribution.text_matched,
            text,
        )
        return attribution.agent

    def _is_peer_handoff(self, event: SpeechEvent) -> bool:
        """Whether a peer-attributed final hands the turn to THIS agent (Johnny-trt.47).

        True only when the agent has a display name, the peer's text contains
        it as a whole word (normalized like the floor's text backstop — the
        STT rendering of the peer's TTS may differ in case/punctuation), and
        the one-hop budget is unspent (no handoff since the last kept human
        final). Deliberately conservative: a miss means the strict trt.46
        suppression applies, which is the safe direction.
        """
        if self._peer_handoff_spent or not self._agent_display_name:
            return False
        alt = event.alternatives[0] if event.alternatives else None
        text = alt.text if alt is not None else ""
        if not text.strip():
            return False
        normalized = normalize_speech_text(text)
        name = normalize_speech_text(self._agent_display_name)
        if not normalized or not name:
            return False
        return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", normalized) is not None

    def _interim_is_noise(self, event: SpeechEvent, config: NoiseFilterConfig) -> bool:
        """Whether an interim transcript is noise, by the content gate alone.

        Interims carry no reliable segment duration (the audio floor is a
        final-only, pre-STT concern), so only the text/confidence content gate
        (:func:`~johnny.agent.noise_filter.classify_transcript_text`) applies. A
        suppressed interim emits no event — the legacy gate recorded one
        :class:`~johnny.voice_pipeline.events.TranscriptFiltered` per finalized
        utterance, not per partial fragment.
        """
        alt = event.alternatives[0] if event.alternatives else None
        text = alt.text if alt is not None else ""
        confidence = alt.confidence if alt is not None else None
        return classify_transcript_text(text, confidence, config) is not None

    def _classify_noise_final(
        self, event: SpeechEvent, config: NoiseFilterConfig
    ) -> TranscriptFiltered | None:
        """Build the ``TranscriptFiltered`` for a noise final, or ``None`` to keep it.

        Called only for ``FINAL_TRANSCRIPT`` events (interims go through
        :meth:`_interim_is_noise`). The text / confidence / speaker come off the
        first alternative; the segment duration (``end_time - start_time``) feeds
        the pre-STT audio floor only when the provider actually reports it (Johnny's
        adapters stamp ``start_time == end_time`` → ``None`` → the audio floor is
        skipped, never dropping a final on an unmeasured duration).
        """
        alt = event.alternatives[0] if event.alternatives else None
        text = alt.text if alt is not None else ""
        confidence = alt.confidence if alt is not None else None
        speaker = alt.speaker_id if alt is not None else None
        audio_duration_ms = _speech_alt_duration_ms(alt)
        reason = classify_noise(
            text=text,
            confidence=confidence,
            audio_duration_ms=audio_duration_ms,
            config=config,
        )
        if reason is None:
            return None
        return TranscriptFiltered(
            text=text,
            timestamp_ms=self._relative_ms(),
            reason=reason,
            speaker=speaker,
            confidence=confidence,
            audio_duration_ms=audio_duration_ms,
            session_id=self._session_id,
        )

    async def _emit_transcript_filtered(self, event: TranscriptFiltered) -> None:
        """Publish a dropped-candidate event through the injected sink, defensively.

        Logged at ``info`` so a noisy mic / mis-tuned stoplist is visible in
        production tails without debug, and the sink is wrapped so a publish
        failure (a lost audit row) never crashes the STT node — the same
        swallow-and-continue contract as the legacy ``_publish_noise_filtered``.
        """
        logger.info(
            "noise gate dropped candidate for session=%s reason=%s "
            "audio_ms=%s confidence=%s text=%r",
            self._session_id,
            event.reason,
            event.audio_duration_ms,
            event.confidence,
            event.text,
        )
        sink = self._transcript_filtered_sink
        if sink is None:
            return
        try:
            await sink(event)
        except Exception:
            logger.exception(
                "failed to publish transcript_filtered for session=%s reason=%s",
                self._session_id,
                event.reason,
            )

    async def _emit_transcript_finalized(
        self, event: SpeechEvent, *, speaker_override: str | None = None
    ) -> None:
        """Publish a kept final's :class:`TranscriptFinalized` (Johnny-d5z), defensively.

        Builds the durable transcript event from the first alternative (text,
        speaker, confidence) and the wall-clock stamp, then publishes through the
        injected sink. A final with no alternative or empty text is skipped (no row
        worth writing); a sink failure is swallowed so the STT node cannot crash on
        a lost audit row — the same swallow-and-continue contract as
        :meth:`_emit_transcript_filtered`. ``speaker_override`` (Johnny-trt.46)
        stamps a peer-attributed final with the peer agent's name — the
        transcript labeling half of peer-speech suppression.
        """
        sink = self._transcript_finalized_sink
        if sink is None:
            return
        alt = event.alternatives[0] if event.alternatives else None
        text = alt.text if alt is not None else ""
        if not text.strip():
            return
        speaker = speaker_override
        if speaker is None:
            speaker = alt.speaker_id if alt is not None else None
        finalized = TranscriptFinalized(
            text=text,
            timestamp_ms=self._relative_ms(),
            speaker=speaker,
            confidence=alt.confidence if alt is not None else None,
            session_id=self._session_id,
        )
        try:
            await sink(finalized)
        except Exception:
            logger.exception(
                "failed to publish transcript_finalized for session=%s",
                self._session_id,
            )

    # ------------------------------------------------------------------ #
    # Answer-path nodes (Johnny-5ag)                                      #
    # ------------------------------------------------------------------ #

    async def llm_node(
        self,
        chat_ctx: ChatContext,
        tools: list[Tool],
        model_settings: ModelSettings,
    ) -> AsyncIterator[ChatChunk | str | FlushSentinel]:
        """Generate the answer text, coercing to an allowed reply when configured.

        Port of the legacy split pipeline's answer-stage branch into the
        LiveKit reply pipeline. In a Limited-auto-speak session with an allow-list
        (and any non-free-form mode), the answer is **coerced** to a verbatim
        allowed reply via :func:`~johnny.agent.answer.coerce_allowed_reply`
        (structured ``enum`` + case-insensitive text fallback); the single matched
        reply is yielded as one text chunk so it streams into TTS. When no allowed
        reply matches, the node yields nothing and flags the gate
        (:meth:`RouterGate.note_coercion_no_match`) so the reply's empty completion
        terminalizes ``no_reply(no_allowed_reply_match)`` rather than the generic
        ``model_empty_output``.

        Every other mode (free-form ``autonomous``, or no allow-list configured)
        streams the answer LLM verbatim through the default node, so token-by-token
        output still reaches TTS for low latency.
        """
        config = self._answer_config
        if (
            config is not None
            and self._answer_llm is not None
            and not tools
            and uses_allowlist(config.mode, config.allowed_replies)
        ):
            # ``not tools`` (Johnny-3ow): the allowlist coercion bypasses the
            # LLM and yields a verbatim reply. Inside a NATIVE TOOL LOOP this
            # re-entrant call (after each FunctionCallOutput) would coerce a
            # canned line and silently drop the tool result. When the agent
            # carries sandbox tools, the loop wins — fall through to the
            # default node, which forwards ``tools`` to the provider.
            picked = await coerce_allowed_reply(
                self._answer_llm,
                chat_ctx_to_messages(chat_ctx),
                config.allowed_replies,
            )
            if picked is None:
                if self._router_gate is not None:
                    self._router_gate.note_coercion_no_match()
                return
            yield picked
            return
        async for chunk in Agent.default.llm_node(self, chat_ctx, tools, model_settings):
            yield chunk

    async def tts_node(
        self,
        text: AsyncIterable[str],
        model_settings: ModelSettings,
    ) -> AsyncIterator[rtc.AudioFrame]:
        """Synthesise the answer per sentence, degrading silently with no TTS.

        Two ported behaviours (Johnny-5ag):

        * **per-sentence flush** — :func:`~johnny.agent.answer.iter_sentences`
          buffers the streaming answer text and yields each complete sentence the
          instant a boundary arrives; each is synthesised immediately, so
          time-to-first-audio is bounded by the first sentence, not the whole
          reply (parity with the legacy split pipeline);
        * **graceful TTS-missing degrade** — when no TTS provider is available
          (or the session was degraded to ``suggest_only``), the node consumes the
          text so the upstream generation completes cleanly and emits **no audio**,
          instead of the default node's ``RuntimeError`` — the bot keeps thinking
          rather than crashing the turn.

        Each flushed sentence is also published as an ephemeral
        :class:`~johnny.voice_pipeline.events.AgentSpeechInterim` (Johnny-trt.39)
        through the injected sink — sequence-numbered per reply so the
        playground can grow a provisional bot bubble while the audio plays.
        The degrade path emits none (nothing is spoken there), and the turn's
        terminal ``AgentSpoke`` remains the authoritative text.
        """
        tts = self._session_tts()
        if tts is None or not self._tts_available:
            async for _ in text:
                pass
            return
        sequence = 0
        async for sentence in iter_sentences(text):
            self._emit_speech_interim(sentence, sequence)
            sequence += 1
            stream = tts.synthesize(sentence)
            async with stream:
                async for ev in stream:
                    yield ev.frame

    def _emit_speech_interim(self, sentence: str, sequence: int) -> None:
        """Hand one flushed sentence to the interim sink, defensively (Johnny-trt.39).

        The sink is sync fire-and-forget (the forwarder schedules the actual
        bus publish), but it is still wrapped so a sink failure can never
        break the synthesis loop — a lost caption beats a crashed reply, the
        same swallow-and-continue contract as :meth:`_emit_transcript_filtered`.
        """
        sink = self._speech_interim_sink
        if sink is None:
            return
        try:
            sink(sentence, sequence)
        except Exception:
            logger.exception(
                "failed to emit agent_speech_interim for session=%s sequence=%d",
                self._session_id,
                sequence,
            )

    def _session_tts(self) -> TTS[Any] | None:
        """The session's TTS plugin, or ``None`` when none is bound.

        The default :meth:`tts_node` reaches the TTS through the running
        ``AgentActivity``; reading it through this seam lets :meth:`tts_node`
        degrade gracefully when the agent has no activity yet or no TTS provider
        is configured (instead of raising), and keeps the node unit-testable by
        injecting the activity.
        """
        activity = self._activity
        if activity is None:
            return None
        return activity.tts


async def build_johnny_agent(
    *,
    prompt_config: AgentInstructionsConfig | None = None,
    instructions: str | None = None,
    transcript_history_loader: TranscriptHistoryLoader | None = None,
    session_id: str | None = None,
    bot_session_id: int | None = None,
    router_gate: RouterGate | None = None,
    barge_in: BargeInClassifier | None = None,
    answer_llm: LLMProvider | None = None,
    answer_config: AnswerConfig | None = None,
    tts_available: bool = True,
    noise_filter: NoiseFilterConfig | None = None,
    transcript_filtered_sink: TranscriptFilteredSink | None = None,
    transcript_finalized_sink: TranscriptFinalizedSink | None = None,
    speech_interim_sink: SpeechInterimSink | None = None,
    metrics_listener: MetricsListener | None = None,
    peer_floor: PeerFloorReader | None = None,
    agent_display_name: str = "",
    sandbox_tools: list[Tool] | None = None,
) -> JohnnyAgent:
    """Build a :class:`JohnnyAgent`, rehydrating prior transcripts if available.

    Parity with the legacy split pipeline → ``_rehydrate_transcript_history``: on
    a container respawn the injected loader pulls the durable transcript rows
    for this session (keyed off ``session_id`` / ``bot_session_id``, whichever
    the implementation prefers) and seeds them into the agent's LiveKit chat
    context so memory survives the restart.

    A missing loader defaults to :class:`NoopTranscriptHistoryLoader` (empty
    history — the bot starts fresh, e.g. a console session or a deployment with
    no rehydration endpoint). Loader exceptions are logged and the agent starts
    with empty history — better to lose context than to refuse to start, the
    same swallow-and-continue contract the legacy method holds.

    ``router_gate`` is the optional :class:`~johnny.agent.router_gate.RouterGate`
    the agent runs in ``on_user_turn_completed``; passed straight through to
    :class:`JohnnyAgent` (``None`` → the agent replies to every turn).
    ``barge_in`` is the optional :class:`~johnny.agent.barge_in.BargeInClassifier`
    the agent spawns out-of-band while mid-reply (``None`` → no slow-classifier
    barge-in; the native VAD interrupt is still configured on the session).
    """
    loader = transcript_history_loader or NoopTranscriptHistoryLoader()
    history: list[TranscriptFinalized] = []
    try:
        history = list(await loader.load(session_id=session_id, bot_session_id=bot_session_id))
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
        router_gate=router_gate,
        barge_in=barge_in,
        answer_llm=answer_llm,
        answer_config=answer_config,
        tts_available=tts_available,
        noise_filter=noise_filter,
        transcript_filtered_sink=transcript_filtered_sink,
        transcript_finalized_sink=transcript_finalized_sink,
        speech_interim_sink=speech_interim_sink,
        metrics_listener=metrics_listener,
        session_id=session_id,
        peer_floor=peer_floor,
        agent_display_name=agent_display_name,
        sandbox_tools=sandbox_tools,
    )


__all__ = [
    "DEFAULT_INSTRUCTIONS",
    "AgentInstructionsConfig",
    "AnswerConfig",
    "JohnnyAgent",
    "NoiseFilterConfig",
    "build_agent_instructions",
    "build_agent_session",
    "build_interruption_options",
    "build_johnny_agent",
    "build_turn_handling",
    "load_vad",
    "transcripts_to_chat_ctx",
]
