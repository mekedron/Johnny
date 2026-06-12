"""Roomless ``AgentSession`` for the in-browser playground (Johnny-7g5.1).

The Meet path dispatches the agent worker into a LiveKit room and starts the
session bound to that room (:mod:`johnny.agent.worker`). The in-browser
playground has no room, no container, no meet-worker: audio flows browser ↔ API
over a WebSocket and the session must run *in the API process itself* (the
legacy in-process the legacy split pipeline it
replaces did the same). This module is the in-process counterpart of the worker:

* it reuses :func:`~johnny.agent.job_session.build_agent_runtime` to assemble
  every Phase-2 seam (adapters, router gate, observability, barge-in, the
  :class:`~johnny.agent.session.JohnnyAgent`) from a
  :class:`~johnny.agent.job_config.SessionJobConfig`;
* it builds the :class:`~livekit.agents.AgentSession` with turn detection that
  needs **no job context**. For an English STT config that is the in-process
  en-only semantic EOU model
  (:class:`~johnny.agent.turn_detector.InProcessEnglishModel` over the
  process-shared :class:`~johnny.agent.turn_detector.InProcessInferenceExecutor`,
  Johnny-1qr) with :func:`browser_semantic_endpointing` — same 0.40 s floor
  as the VAD-only path, but a pause the model judges mid-thought is held to
  ``max_delay`` 1.5 s instead of hard-cutting at the floor (resumed speech
  cancels the commit entirely). Any other language (or the
  ``JOHNNY_BROWSER_FORCE_VAD_TURNS`` kill-switch) keeps plain **VAD turn
  detection** with the tuned VAD-only endpointing
  (:data:`BROWSER_ENDPOINTING_MIN_DELAY_S`, Johnny-trt.6) — the *multilingual*
  in-process model stays wontfixed (~884 MB RSS, over the ~500 MB line; the
  en-only model measured ~+410 MB / 1.7 ms in-image — Johnny-trt.6 spike +
  the Johnny-1qr re-measure);
* it binds the session to the browser via
  :class:`~johnny.agent.browser_audio_io.BrowserAudioInput` /
  :class:`~johnny.agent.browser_audio_io.BrowserAudioOutput` instead of
  ``RoomIO`` and starts it **roomless** (``session.start(agent=...)`` with no
  ``room`` — verified ``livekit-agents==1.5.17``);
* :meth:`BrowserAgentSession.feed_text` maps the playground's typed-input
  endpoint onto the engine: it runs the **router gate** for the typed turn (so
  the same should-speak decision + INV-1 terminal + decision↔utterance parity
  apply as for a voice turn) and, on a SPEAK verdict, calls
  ``session.generate_reply`` — whose ``speech_created`` the gate's existing
  ``bind_reply`` listener correlates back to the gated turn.

Voice turns need no special handling: the browser mic frames drive the STT/VAD
turn detector exactly as room audio does, so ``on_user_turn_completed`` → the
router gate fires naturally.

Requires the ``agent`` extra (``livekit-agents``); imported only by the browser
session runner (:mod:`app.services.browser_pipeline_runner`), never from the
import-safe top-level :mod:`johnny.agent` package.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from livekit.agents.llm import StopResponse
from livekit.agents.llm.chat_context import ChatMessage as LKChatMessage

from johnny.agent.adapters.factory import stt_language_from_provider_config
from johnny.agent.browser_audio_io import BrowserAudioInput, BrowserAudioOutput
from johnny.agent.job_config import SessionJobConfig
from johnny.agent.job_session import AgentRuntime, build_agent_runtime
from johnny.agent.observability import (
    InterimTranscriptForwarder,
    build_transcript_finalized_emitter,
)
from johnny.agent.session import build_agent_session, load_vad
from johnny.agent.turn_detector import (
    InProcessEnglishModel,
    InProcessInferenceExecutor,
    browser_vad_turns_forced,
    is_english_stt_language,
)
from johnny.voice_pipeline.events import TranscriptFinalized

if TYPE_CHECKING:
    from livekit.agents import AgentSession, EndpointingOptions
    from livekit.agents.vad import VAD

    from johnny.agent.observability import TranscriptFinalizedSink
    from johnny.voice_pipeline.browser_transport import BrowserAudioTransport
    from johnny.voice_pipeline.event_bus import EventBus
    from johnny.voice_pipeline.transcript_history import TranscriptHistoryLoader

logger = logging.getLogger(__name__)

# Silero VAD endpointing for the browser session. Loading the model is the slow
# part of session setup, so — like the worker's per-process prewarm — load it
# once and reuse the one handle across every browser session (only one runs at a
# time, Johnny-8zv.2). Reuse matches the worker, which shares one prewarmed VAD
# across all jobs on its process.
_SHARED_VAD: VAD | None = None

# Browser-scoped Silero end-of-speech silence floor (Johnny-trt.5). The
# playground is a single-speaker surface, so the multi-party turn-taking
# padding that justifies Silero's 0.55 s default on the Meet/room path
# (Johnny-arh) doesn't apply; 0.40 s commits the turn ~150 ms earlier on the
# felt path (harness A/B in docs/LATENCY.md) while still riding out natural
# mid-sentence hesitations (~0.2–0.35 s). Browser sessions ONLY — the worker's
# prewarm keeps :func:`~johnny.agent.session.load_vad` defaults.
BROWSER_VAD_MIN_SILENCE_DURATION_S = 0.40


# Engine endpointing floor for the VAD-only browser session (Johnny-trt.6).
# The trt.6 bead's in-process semantic turn detector was dropped per its own
# spike criterion — the *multilingual* EOU model costs ~884 MB RSS inside the
# API process (ONNX is 396 MB on disk; the >~500 MB abort line; full numbers
# in docs/LATENCY.md) — so the VAD-only path retunes the engine's
# ``min_delay`` padding down to the VAD floor instead. ``min_delay`` is
# anchored at the *last detected speech*, so it overlaps (not stacks on)
# Silero's 0.40 s ``min_silence_duration`` wait: 0.40 commits the turn the
# moment the VAD floor is crossed instead of padding it to LiveKit's 0.5 s
# default. Hesitation tolerance is the VAD floor itself — pauses < 0.40 s
# never fire END_OF_SPEECH (the trt.5 varied-pause envelope, 0.20–0.35 s,
# is untouched). ``max_delay`` stays unset: with ``turn_detection="vad"``
# no semantic model ever escalates to it. This remains the path for any
# non-English STT config and for ``JOHNNY_BROWSER_FORCE_VAD_TURNS=1``; an
# English config upgrades to the semantic tuning below (Johnny-1qr).
BROWSER_ENDPOINTING_MIN_DELAY_S = 0.40

# Semantic-EOU browser tuning (Johnny-1qr). With the in-process en-only turn
# detector engaged, the Silero floor and ``min_delay`` stay at the trt.6
# 0.40 s — only ``max_delay`` becomes live: a pause > 0.40 s whose
# accumulated transcript the model judges mid-thought escalates the commit
# to 1.5 s, and any resumed speech cancels it entirely (the SDK's EOU
# bounce task) — pre-1qr such pauses were unconditional hard cuts. A
# "complete" verdict commits at the floor, exactly today's timing, so the
# detector is a pure quality upgrade with no felt-latency cost. Hesitations
# <= 0.35 s never fire END_OF_SPEECH and ride out mechanically, identical
# to the VAD-only path. ``max_delay`` 1.5 s halves the SDK's 3.0 s default
# (a single-speaker playground does not need multi-party patience, but a
# mid-thought pause deserves more than the floor).
#
# Dropping the floor to ~0.2 s for an additional "commit earlier when
# confident" win was BUILT AND REVERTED in validation: at a 0.20 s floor
# the live varied-pause run split the 0.35 s-edge hesitations — the
# streaming Parakeet sidecar finalizes at ~0.36 s of trailing silence and
# hallucinates terminal punctuation at segment edges ("Jenny, can you?"),
# which the EOU model correctly reads as a complete utterance. That hit
# the bead's abort criterion (0.20-0.35 s hesitations must ride out), so
# the floor drop is blocked until the sidecar's edge-punctuation artifact
# is addressed (see docs/LATENCY.md and .validation/Johnny-1qr/). On the
# local stack the drop was worth only ~30 ms anyway (commits become
# final-bound at the sidecar's ~0.36 s endpoint); the stub A/B's −188 ms
# felt p50 at a 0.20 s floor is recorded as the fast-finals upper bound.
BROWSER_SEMANTIC_ENDPOINTING_MAX_DELAY_S = 1.5


def browser_endpointing() -> EndpointingOptions:
    """Endpointing options for the VAD-only browser session (Johnny-trt.6 retune).

    ``min_delay`` only — missing keys inherit the SDK defaults (see
    :func:`johnny.agent.session.build_turn_handling`). Browser sessions ONLY;
    the Meet/room path passes no ``endpointing`` and keeps LiveKit's 0.5 s /
    3.0 s defaults (multi-party padding rationale, Johnny-arh).
    """
    return {"min_delay": BROWSER_ENDPOINTING_MIN_DELAY_S}


def browser_semantic_endpointing() -> EndpointingOptions:
    """Endpointing options when the semantic turn detector is engaged (Johnny-1qr).

    ``min_delay`` stays the trt.6 VAD-floor value (zero engine padding);
    ``max_delay`` is live here — a "model says incomplete" verdict holds the
    commit to it instead of hard-cutting at the floor. Browser sessions ONLY,
    like :func:`browser_endpointing`.
    """
    return {
        "min_delay": BROWSER_ENDPOINTING_MIN_DELAY_S,
        "max_delay": BROWSER_SEMANTIC_ENDPOINTING_MAX_DELAY_S,
    }


def load_browser_vad() -> VAD:
    """Load Silero tuned for the single-speaker browser playground (Johnny-trt.5).

    One instance serves both jobs a browser session has for it — the batch-STT
    ``StreamAdapter`` segmentation and the session's ``turn_detection="vad"``
    endpointing — because :meth:`BrowserAgentSession.build` threads the same
    handle through ``build_agent_runtime`` and ``build_agent_session``.
    """
    return load_vad(min_silence_duration=BROWSER_VAD_MIN_SILENCE_DURATION_S)


def _shared_vad() -> VAD:
    """Return the process-shared browser Silero VAD, loading it once on first use.

    One handle serves the VAD-only AND the semantic-EOU paths (Johnny-1qr):
    both run the same 0.40 s floor, so the semantic path differs only in
    ``turn_detection`` + ``endpointing``, never in the Silero model.
    """
    global _SHARED_VAD
    if _SHARED_VAD is None:
        _SHARED_VAD = load_browser_vad()
    return _SHARED_VAD


def resolve_browser_turn_detector(config: SessionJobConfig) -> InProcessEnglishModel | None:
    """The in-process semantic turn detector for this session, or ``None`` (Johnny-1qr).

    Engages only when every gate passes, otherwise the session keeps the tuned
    VAD-only path (Johnny-trt.6):

    * the ``JOHNNY_BROWSER_FORCE_VAD_TURNS`` kill-switch is unset;
    * the operator-configured STT language normalizes to English — the same
      option keys the STT adapter stamps onto every transcript, so this
      build-time gate cannot disagree with the SDK's per-turn
      ``supports_language`` gate. The en-only revision supports nothing else
      (and the multilingual one has no Finnish either — trt.6 spike), and the
      matching 0.20 s VAD floor must never ship without a model to judge the
      pauses it exposes;
    * the model wrapper constructs (reads ``languages.json`` from the
      image-baked HF cache) — a failure logs and falls back rather than
      blocking the session.

    Construction is cheap; the ~+400 MB runner load happens lazily in the
    process-shared executor (or in :meth:`BrowserAgentSession.warm_up`).
    ``config`` is duck-typed (``provider_config`` read via ``getattr``) so
    harness/test fakes that model only the fields they exercise resolve to
    the VAD-only path instead of crashing.
    """
    if browser_vad_turns_forced():
        logger.info(
            "browser session: JOHNNY_BROWSER_FORCE_VAD_TURNS set — keeping VAD-only turn detection"
        )
        return None
    language = stt_language_from_provider_config(getattr(config, "provider_config", None) or {})
    if not is_english_stt_language(language):
        if language:
            logger.info(
                "browser session: STT language %r is not English — the en-only "
                "semantic turn detector stays off (VAD-only endpointing)",
                language,
            )
        return None
    try:
        return InProcessEnglishModel()
    except Exception:
        logger.exception(
            "browser session: semantic turn detector unavailable — falling back "
            "to VAD-only endpointing"
        )
        return None


class BrowserAgentSession:
    """One in-process ``AgentSession`` bound to a :class:`BrowserAudioTransport`.

    Built by :meth:`build` (assembles the runtime + session + audio I/O) and
    driven by the browser session runner: :meth:`start` connects the audio
    seams and starts the session roomless; :meth:`feed_text` drives a typed
    turn; :meth:`interrupt` cuts the bot off (barge-in / Stop button);
    :meth:`aclose` tears everything down. It exposes the same ``feed_text`` /
    ``interrupt`` surface the playground's text-input + stop endpoints called on
    the legacy split pipeline, so the endpoint wiring is unchanged.
    """

    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        session: AgentSession[Any],
        transport: BrowserAudioTransport,
        audio_out: BrowserAudioOutput,
        transcript_sink: TranscriptFinalizedSink,
        session_id: str,
        interim_forwarder: InterimTranscriptForwarder | None = None,
        eou_executor: InProcessInferenceExecutor | None = None,
    ) -> None:
        self._runtime = runtime
        self._session = session
        self._transport = transport
        self._audio_out = audio_out
        self._transcript_sink = transcript_sink
        self._session_id = session_id
        self._interim_forwarder = interim_forwarder
        # The in-process executor behind the session's semantic turn detector
        # (Johnny-1qr); None on the VAD-only path. Carried so warm_up() can
        # pre-load the EOU model off the first turn's hot path.
        self._eou_executor = eou_executor
        # Session-start reference for the typed-input transcript offset (so it
        # lands in the INTEGER transcript_chunks.start_offset_ms as an
        # offset-from-start, never a raw epoch-ms value — Johnny-7g5.1).
        self._started_monotonic = time.monotonic()

    @property
    def semantic_eou_active(self) -> bool:
        """Whether this session runs the in-process semantic turn detector."""
        return self._eou_executor is not None

    @property
    def turn_detection_label(self) -> str:
        """Display label for the session's turn-detection strategy (harness/logs)."""
        return "semantic-eou(en)" if self.semantic_eou_active else "vad"

    @classmethod
    async def build(
        cls,
        transport: BrowserAudioTransport,
        config: SessionJobConfig,
        *,
        event_bus: EventBus,
        vad: VAD | None = None,
        transcript_history_loader: TranscriptHistoryLoader | None = None,
        endpointing: EndpointingOptions | None = None,
        semantic_eou: bool | None = None,
        task_wiring: bool = True,
    ) -> BrowserAgentSession:
        """Assemble the runtime + roomless session + audio I/O for one session.

        Mirrors the worker's entrypoint (Johnny-9eh) minus the room: reuses
        :func:`build_agent_runtime` for every shared seam, builds the
        :class:`AgentSession` with job-context-free turn detection (semantic
        or VAD — see below), and — only in ``approval_required`` mode with a
        live gate — wires the approval coordinator (it needs the built session
        for out-of-band ``generate_reply``). Raises
        :class:`~johnny.agent.adapters.factory.AgentSessionSetupError` for a
        non-split / under-configured payload, exactly like the worker.

        ``semantic_eou`` selects the turn-detection strategy (Johnny-1qr):
        ``None`` (the default, and what production passes) auto-engages the
        in-process en-only semantic turn detector when
        :func:`resolve_browser_turn_detector`'s gates pass — an English STT
        config with the kill-switch unset — pairing it with
        :func:`browser_semantic_endpointing` (the trt.6 ``min_delay`` floor
        plus a live ``max_delay`` 1.5 s for "model says incomplete" holds);
        any failed gate keeps tuned VAD-only turn detection (Johnny-trt.6).
        ``False`` forces VAD-only (the harness baseline arm). ``True``
        requires the detector and raises ``RuntimeError`` when it cannot
        engage — an A/B arm must never silently measure the wrong path (the
        trt.11 lesson). Both strategies run the same 0.40 s Silero floor.

        ``endpointing`` overrides the engaged path's default
        (:func:`browser_semantic_endpointing` — ``min_delay`` 0.40 s /
        ``max_delay`` 1.5 s — when the detector engages, else
        :func:`browser_endpointing`, ``min_delay`` 0.40 s); the latency
        harness passes e.g. ``{"min_delay": 0.5}`` to reproduce the pre-trt.6
        engine padding for A/B runs.

        ``task_wiring=False`` (Johnny-trt.59, harness-only) drops the DB
        session factory so the assembly builds no task sink → no coordinator
        → no skill registry scan → empty task catalog: the router call is
        byte-identical to the pre-Phase-3 build in both prompt and schema —
        the "pure conversational hot path" A/B arm. Production always wires.
        """
        from app.db.session import SessionLocal

        detector: InProcessEnglishModel | None = None
        if semantic_eou is not False:
            detector = resolve_browser_turn_detector(config)
            if semantic_eou is True and detector is None:
                raise RuntimeError(
                    "semantic_eou=True but the en-only semantic turn detector could "
                    "not engage — it needs an English STT language in the session's "
                    "provider_config, JOHNNY_BROWSER_FORCE_VAD_TURNS unset, and the "
                    "image-baked turn-detector model files"
                )

        if vad is None:
            vad = _shared_vad()
        session_id = str(config.bot_session_id)

        runtime = await build_agent_runtime(
            config,
            vad=vad,
            event_bus=event_bus,
            transcript_history_loader=transcript_history_loader,
            db_session_factory=SessionLocal if task_wiring else None,
            # Epoch-seconds reference so the metrics translator emits
            # session-relative ``started_at_ms`` (the subscriber writes it into
            # the INTEGER ``session_timings.started_at_ms``; a raw epoch-ms
            # offset overflows it on Postgres — Johnny-7g5.1).
            session_started_at=time.time(),
        )

        # Turn detection without a job context: the en-only semantic model
        # over the in-process executor when the Johnny-1qr gates passed, else
        # plain VAD endpointing (the multilingual model stays job-context
        # -bound and was wontfixed in-process at ~884 MB RSS — trt.6 spike).
        # The endpointing default tracks the engaged path; an explicit
        # ``endpointing`` always wins (the harness A/B seam).
        if endpointing is None:
            endpointing = (
                browser_semantic_endpointing() if detector is not None else browser_endpointing()
            )
        session = build_agent_session(
            stt=runtime.adapters.stt,
            llm=runtime.adapters.llm,
            tts=runtime.adapters.tts,
            vad=vad,
            enable_barge_in=runtime.enable_barge_in,
            min_interruption_duration_s=runtime.min_interruption_duration_s,
            turn_detection=detector if detector is not None else "vad",
            endpointing=endpointing,
        )
        logger.info(
            "browser session %s turn detection: %s (endpointing=%s)",
            session_id,
            "semantic-eou(en)" if detector is not None else "vad",
            endpointing,
        )

        if runtime.needs_approval_wiring and runtime.approval_gate is not None:
            from johnny.agent.approval_wiring import build_approval_coordinator

            build_approval_coordinator(
                ledger=runtime.ledger,
                router_gate=runtime.gate,
                session=session,
                approval_gate=runtime.approval_gate,
                event_bus=runtime.event_bus,
                decision_sink=runtime.decision_sink,
                session_id=session_id,
            )

        audio_out = BrowserAudioOutput(transport)
        transcript_sink = build_transcript_finalized_emitter(event_bus, session_id=session_id)
        # Live-caption seam (Johnny-trt.13): non-final hypotheses from streaming
        # STT flow out as TranscriptInterim. Session-relative clock like the
        # finals; registered on the session in :meth:`start`.
        interim_zero = time.monotonic()
        interim_forwarder = InterimTranscriptForwarder(
            event_bus,
            clock=lambda: max(0, int((time.monotonic() - interim_zero) * 1000)),
            session_id=session_id,
        )
        return cls(
            runtime=runtime,
            session=session,
            transport=transport,
            audio_out=audio_out,
            transcript_sink=transcript_sink,
            session_id=session_id,
            interim_forwarder=interim_forwarder,
            eou_executor=detector.executor if detector is not None else None,
        )

    async def warm_up(self) -> None:
        """Pre-load the session providers' lazy heavy state (Johnny-trt.8).

        Delegates to :meth:`~johnny.agent.job_session.AgentRuntime.warm_up`
        (whisper weights, Piper voice ONNX, local-LLM model load — each
        provider's own ``warm_up()`` hook) and, when the semantic turn
        detector is engaged (Johnny-1qr), concurrently pre-loads its EOU
        runner so the first turn's prediction never pays the model load
        inside its 3 s budget. The browser runner fires this as a background
        task right after :meth:`build`, concurrently with :meth:`start` —
        the session's ready signal never waits on it. Never raises;
        per-provider / executor failures are logged inside.
        """
        warm_ups = [self._runtime.warm_up()]
        if self._eou_executor is not None:
            warm_ups.append(self._eou_executor.warm_up())
        await asyncio.gather(*warm_ups)

    async def start(self) -> None:
        """Bind the browser audio seams and start the session roomless.

        Setting ``input.audio`` / ``output.audio`` *before* ``start`` makes the
        SDK skip ``RoomIO`` (it only builds one when a ``room`` is given) and
        instead forward our input frames to the activity + drain our output sink.

        Also hangs the live-caption listener (Johnny-trt.13) off the session:
        every ``user_input_transcribed`` interim the SDK surfaces is forwarded
        to the EventBus as a ``TranscriptInterim`` so the playground can show
        the in-flight hypothesis while the user is still speaking. Browser
        sessions only — the Meet/room path registers no such listener.
        """
        self._session.input.audio = BrowserAudioInput(self._transport)
        self._session.output.audio = self._audio_out
        if self._interim_forwarder is not None:
            self._session.on(
                "user_input_transcribed",
                self._interim_forwarder.on_user_input_transcribed,
            )
        await self._session.start(agent=self._runtime.agent)
        # Phase-5 speech wiring (Johnny-trt.28): same placement as the agent
        # worker — the task-event listener + gated result delivery need the
        # live session, which only exists from here. No-op without a task
        # coordinator; torn down by runtime.aclose inside our aclose.
        from johnny.agent.task_wiring import attach_task_speech_wiring

        attach_task_speech_wiring(self._runtime, self._session)
        logger.info("browser agent session started for session=%s (roomless)", self._session_id)

    async def feed_text(self, text: str) -> bool:
        """Drive a typed-input turn through the router gate, then speak on SPEAK.

        Parity with the legacy split pipeline + the bead's
        ``feed_text → generate_reply`` mapping, while preserving INV-1 +
        decision↔utterance parity: the typed text is published as a
        ``TranscriptFinalized`` (so it lands in the transcript pane + history),
        then run through :meth:`RouterGate.run_turn` as a synthetic user turn.
        The gate emits the turn's ``RouterDecisionMade`` and owns its terminal —
        a no-speak / low-confidence / suggest-only / listen-only verdict raises
        ``StopResponse`` (no reply, terminal already emitted), and a SPEAK
        verdict records the turn for ``bind_reply`` and returns, after which
        ``generate_reply`` produces the reply whose ``speech_created`` the gate's
        listener correlates back to that turn (its completion emits ``AgentSpoke``
        + the speak-path terminal). Returns ``True`` once accepted, ``False``
        before the session is running.
        """
        cleaned = text.strip()
        if not cleaned:
            return False
        if self._session._activity is None:
            # Session not started yet — let the caller persist the chunk instead.
            return False

        await self._emit_user_transcript(cleaned)

        # Generation-scoped context copy (Johnny-0qw): the gate may inject a
        # task-grounding system message into the turn context on a SPEAK
        # verdict (RouterGate._inject_task_context). The voice path gets that
        # scoping for free (the SDK hands on_user_turn_completed a temp
        # mutable copy and generates from it); the typed path must mirror it —
        # run the gate on a copy and generate from that same copy, so the
        # injection reaches exactly this reply and never pollutes the durable
        # context (generate_reply still persists the user message and the
        # assistant reply into the live ctx itself).
        #
        # The copy source must be the AGENT's chat context, not
        # ``session.history`` (Johnny-trt.45 fix of a Johnny-0qw regression):
        # the SDK keeps the agent's static instructions as a system item
        # inside ``agent._chat_ctx`` ONLY (``update_instructions`` at activity
        # start), while ``session.history`` is the session-surface mirror
        # without it — and ``generate_reply(chat_ctx=…)`` adds no instructions
        # of its own (its ``instructions`` param defaults to None). Copying
        # the history therefore generated typed replies with NO system prompt
        # at all: out of character, blind to the per-assignment context. The
        # agent ctx copy is also exactly what the SDK's voice path hands
        # ``on_user_turn_completed``, so the two surfaces now scope turns
        # identically.
        turn_ctx = self._runtime.agent.chat_ctx.copy()
        new_message = LKChatMessage(role="user", content=[cleaned])
        try:
            await self._runtime.gate.run_turn(turn_ctx, new_message)
        except StopResponse:
            # The gate accounted for this turn without an answer-LLM reply:
            # declined / suggest-only / listen-only (terminal already emitted),
            # or a delegate/status verdict (Johnny-trt.17) whose ack the gate
            # scheduled via session.say() — that speech's completion owns the
            # turn's terminal. Either way, nothing to generate here.
            return True
        except Exception:
            logger.exception(
                "browser agent feed_text: gate.run_turn failed for session=%s",
                self._session_id,
            )
            return True

        # SPEAK: generate the reply from the gate's (possibly task-grounded)
        # turn context. The on_enter speech_created listener routes it to
        # gate.bind_reply, which pops the turn run_turn just recorded.
        try:
            self._session.generate_reply(user_input=cleaned, chat_ctx=turn_ctx)
        except Exception:
            logger.exception(
                "browser agent feed_text: generate_reply failed for session=%s",
                self._session_id,
            )
        return True

    def interrupt(self) -> None:
        """Cut the bot off mid-reply (the playground Stop button / barge-in).

        Schedules the session interruption (which clears our audio sink — see
        :meth:`BrowserAudioOutput.clear_buffer`). The endpoint also drains the
        transport playback queue + signals the browser directly, so a stop still
        cuts audio even when nothing is currently generating.
        """
        try:
            self._session.interrupt()
        except Exception:
            logger.exception(
                "browser agent session interrupt failed for session=%s", self._session_id
            )

    async def aclose(self) -> None:
        """Tear down the session then the runtime (best-effort throughout).

        ``session.aclose`` fires ``JohnnyAgent.on_exit`` (the gate's ledger sweep
        + approval-resolver cancellation, INV-1 on hard teardown); the audio sink
        timer is cancelled; ``runtime.aclose`` drains metrics + closes the
        approval gate + the approval DB session (the event bus is caller-owned,
        so it is left open).
        """
        try:
            await self._session.aclose()
        except Exception:
            logger.exception("browser agent session aclose failed for session=%s", self._session_id)
        if self._interim_forwarder is not None:
            try:
                await self._interim_forwarder.aclose()
            except Exception:
                logger.exception(
                    "interim transcript forwarder aclose failed for session=%s", self._session_id
                )
        try:
            await self._audio_out.aclose()
        except Exception:
            logger.exception("browser audio output aclose failed for session=%s", self._session_id)
        try:
            await self._runtime.aclose()
        except Exception:
            logger.exception("browser agent runtime aclose failed for session=%s", self._session_id)

    async def _emit_user_transcript(self, text: str) -> None:
        """Publish the typed text as a user ``TranscriptFinalized`` (defensive)."""
        transcript = TranscriptFinalized(
            text=text,
            timestamp_ms=max(0, int((time.monotonic() - self._started_monotonic) * 1000)),
            speaker="user",
            session_id=self._session_id,
        )
        try:
            await self._transcript_sink(transcript)
        except Exception:
            logger.exception(
                "browser agent feed_text: failed to publish user transcript for session=%s",
                self._session_id,
            )


__all__ = [
    "BROWSER_ENDPOINTING_MIN_DELAY_S",
    "BROWSER_SEMANTIC_ENDPOINTING_MAX_DELAY_S",
    "BROWSER_VAD_MIN_SILENCE_DURATION_S",
    "BrowserAgentSession",
    "browser_endpointing",
    "browser_semantic_endpointing",
    "build_browser_agent_session",
    "load_browser_vad",
    "resolve_browser_turn_detector",
]


async def build_browser_agent_session(
    transport: BrowserAudioTransport,
    config: SessionJobConfig,
    *,
    event_bus: EventBus,
    vad: VAD | None = None,
    transcript_history_loader: TranscriptHistoryLoader | None = None,
    endpointing: EndpointingOptions | None = None,
    semantic_eou: bool | None = None,
) -> BrowserAgentSession:
    """Functional alias for :meth:`BrowserAgentSession.build` (call-site clarity)."""
    return await BrowserAgentSession.build(
        transport,
        config,
        event_bus=event_bus,
        vad=vad,
        transcript_history_loader=transcript_history_loader,
        endpointing=endpointing,
        semantic_eou=semantic_eou,
    )
