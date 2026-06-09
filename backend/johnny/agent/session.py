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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from livekit.agents import (
    Agent,
    AgentSession,
    InterruptionOptions,
    TurnHandlingOptions,
)
from livekit.agents.llm.chat_context import ChatContext
from livekit.agents.llm.chat_context import ChatMessage as LKChatMessage
from livekit.plugins import silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from johnny.agent.adapters.johnny_llm import chat_ctx_to_messages
from johnny.agent.answer import (
    AnswerConfig,
    coerce_allowed_reply,
    iter_sentences,
    uses_allowlist,
)
from johnny.voice_pipeline.events import TranscriptFinalized
from johnny.voice_pipeline.transcript_history import (
    BOT_SPEAKER_LABEL,
    NoopTranscriptHistoryLoader,
    TranscriptHistoryLoader,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, AsyncIterator, Sequence

    from livekit import rtc

    # STT / LLM / TTS are Generic over their event type (TEvent) and
    # AgentSession over Userdata_T; the harness is event/userdata-agnostic,
    # so it accepts any concrete adapter via the [Any] parametrization. The
    # Phase-1 adapters (johnny.agent.adapters) subclass with concrete events.
    from livekit.agents import FlushSentinel, ModelSettings
    from livekit.agents.llm import LLM, ChatChunk, Tool
    from livekit.agents.stt import STT
    from livekit.agents.tts import TTS
    from livekit.agents.vad import VAD
    from livekit.agents.voice import SpeechCreatedEvent

    from app.providers.base import LLMProvider
    from johnny.agent.barge_in import BargeInClassifier
    from johnny.agent.router_gate import RouterGate

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
    preemptive_generation: bool = False,
    enable_barge_in: bool = True,
    min_interruption_duration_s: float | None = None,
) -> AgentSession[Any]:
    """Construct Johnny's ``AgentSession`` from provider adapter instances.

    ``stt`` / ``llm`` / ``tts`` are Johnny's own LiveKit plugin adapters
    (:mod:`johnny.agent.adapters`, Phase 1), built from the admin-active
    providers by the adapter factory (Johnny-zb3). Turn-taking uses LiveKit's
    multilingual turn detector plus Silero VAD per the locked decision;
    ``vad`` defaults to a freshly loaded Silero model when not supplied.

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
    """
    turn_handling: TurnHandlingOptions = {
        "turn_detection": MultilingualModel(),
        "preemptive_generation": {"enabled": preemptive_generation},
        "interruption": build_interruption_options(
            enable_barge_in=enable_barge_in,
            min_interruption_duration_s=min_interruption_duration_s,
        ),
    }
    return AgentSession(
        stt=stt,
        llm=llm,
        tts=tts,
        vad=vad if vad is not None else load_vad(),
        turn_handling=turn_handling,
    )


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

    Carries the assembled meeting instructions (personality + brief + calendar
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

    async def on_enter(self) -> None:
        """Wire the reply→turn correlation once the agent is active.

        Registers a session ``speech_created`` listener that hands every
        ``generate_reply`` reply to :meth:`RouterGate.bind_reply`, so the
        reply's done-callback emits the turn's terminal (the speak path's INV-1
        record). No-op without a gate.
        """
        gate = self._router_gate
        if gate is None:
            return

        def _on_speech_created(ev: SpeechCreatedEvent) -> None:
            if ev.source == "generate_reply":
                gate.bind_reply(ev.speech_handle)

        self.session.on("speech_created", _on_speech_created)

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

        Port of ``VoicePipeline._answer_and_speak``'s answer-stage branch into the
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
            and uses_allowlist(config.mode, config.allowed_replies)
        ):
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
          reply (parity with ``VoicePipeline._stream_answer_into_tts``);
        * **graceful TTS-missing degrade** — when no TTS provider is available
          (or the session was degraded to ``suggest_only``), the node consumes the
          text so the upstream generation completes cleanly and emits **no audio**,
          instead of the default node's ``RuntimeError`` — the bot keeps thinking
          rather than crashing the turn.
        """
        tts = self._session_tts()
        if tts is None or not self._tts_available:
            async for _ in text:
                pass
            return
        async for sentence in iter_sentences(text):
            stream = tts.synthesize(sentence)
            async with stream:
                async for ev in stream:
                    yield ev.frame

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
    )


__all__ = [
    "DEFAULT_INSTRUCTIONS",
    "AgentInstructionsConfig",
    "AnswerConfig",
    "JohnnyAgent",
    "build_agent_instructions",
    "build_agent_session",
    "build_interruption_options",
    "build_johnny_agent",
    "load_vad",
    "transcripts_to_chat_ctx",
]
