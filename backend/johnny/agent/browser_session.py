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
* it builds the :class:`~livekit.agents.AgentSession` with **VAD turn detection**
  (not the job-context-bound ``MultilingualModel``) so it runs with no job
  context — matching the legacy browser pipeline's own VAD-based turn-taking.
  Hosting the semantic EOU model in-process instead was investigated and
  wontfixed (Johnny-trt.6): the multilingual ONNX model costs ~884 MB RSS in
  the API process, over the bead's ~500 MB line. The session compensates with
  tuned VAD-only endpointing (:data:`BROWSER_ENDPOINTING_MIN_DELAY_S`);
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

import logging
import time
from typing import TYPE_CHECKING, Any

from livekit.agents.llm import StopResponse
from livekit.agents.llm.chat_context import ChatMessage as LKChatMessage

from johnny.agent.browser_audio_io import BrowserAudioInput, BrowserAudioOutput
from johnny.agent.job_config import SessionJobConfig
from johnny.agent.job_session import AgentRuntime, build_agent_runtime
from johnny.agent.observability import build_transcript_finalized_emitter
from johnny.agent.session import build_agent_session, load_vad
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


# Engine endpointing floor for the browser session (Johnny-trt.6). The bead's
# in-process semantic turn detector was dropped per its own spike criterion —
# the multilingual EOU model costs ~884 MB RSS inside the API process (ONNX is
# 396 MB on disk; the >~500 MB abort line; full numbers in docs/LATENCY.md) —
# so the browser session stays on VAD endpointing and instead retunes the
# engine's ``min_delay`` padding down to the VAD floor. ``min_delay`` is
# anchored at the *last detected speech*, so it overlaps (not stacks on)
# Silero's 0.40 s ``min_silence_duration`` wait: 0.40 commits the turn the
# moment the VAD floor is crossed instead of padding it to LiveKit's 0.5 s
# default. Hesitation tolerance is the VAD floor itself — pauses < 0.40 s
# never fire END_OF_SPEECH (the trt.5 varied-pause envelope, 0.20–0.35 s,
# is untouched). On today's batch-STT path the commit is STT-final-bound
# (~0.52 s with Parakeet's ~123 ms finals) so this is felt-neutral; it
# removes the engine's 0.5 s floor so streaming/cloud STT (Phase 2) commits
# at the VAD floor. ``max_delay`` stays unset: with ``turn_detection="vad"``
# no semantic model ever escalates to it.
BROWSER_ENDPOINTING_MIN_DELAY_S = 0.40


def browser_endpointing() -> EndpointingOptions:
    """Endpointing options for the browser session (Johnny-trt.6 VAD-only retune).

    ``min_delay`` only — missing keys inherit the SDK defaults (see
    :func:`johnny.agent.session.build_turn_handling`). Browser sessions ONLY;
    the Meet/room path passes no ``endpointing`` and keeps LiveKit's 0.5 s /
    3.0 s defaults (multi-party padding rationale, Johnny-arh).
    """
    return {"min_delay": BROWSER_ENDPOINTING_MIN_DELAY_S}


def load_browser_vad() -> VAD:
    """Load Silero tuned for the single-speaker browser playground (Johnny-trt.5).

    One instance serves both jobs a browser session has for it — the batch-STT
    ``StreamAdapter`` segmentation and the session's ``turn_detection="vad"``
    endpointing — because :meth:`BrowserAgentSession.build` threads the same
    handle through ``build_agent_runtime`` and ``build_agent_session``.
    """
    return load_vad(min_silence_duration=BROWSER_VAD_MIN_SILENCE_DURATION_S)


def _shared_vad() -> VAD:
    """Return the process-shared browser Silero VAD, loading it once on first use."""
    global _SHARED_VAD
    if _SHARED_VAD is None:
        _SHARED_VAD = load_browser_vad()
    return _SHARED_VAD


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
    ) -> None:
        self._runtime = runtime
        self._session = session
        self._transport = transport
        self._audio_out = audio_out
        self._transcript_sink = transcript_sink
        self._session_id = session_id
        # Session-start reference for the typed-input transcript offset (so it
        # lands in the INTEGER transcript_chunks.start_offset_ms as an
        # offset-from-start, never a raw epoch-ms value — Johnny-7g5.1).
        self._started_monotonic = time.monotonic()

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
    ) -> BrowserAgentSession:
        """Assemble the runtime + roomless session + audio I/O for one session.

        Mirrors the worker's entrypoint (Johnny-9eh) minus the room: reuses
        :func:`build_agent_runtime` for every shared seam, builds the
        :class:`AgentSession` with VAD turn detection, and — only in
        ``approval_required`` mode with a live gate — wires the approval
        coordinator (it needs the built session for out-of-band
        ``generate_reply``). Raises
        :class:`~johnny.agent.adapters.factory.AgentSessionSetupError` for a
        non-split / under-configured payload, exactly like the worker.

        ``endpointing`` overrides the browser default
        (:func:`browser_endpointing`, ``min_delay`` 0.40 s — Johnny-trt.6);
        the latency harness passes e.g. ``{"min_delay": 0.5}`` to reproduce
        the pre-trt.6 engine padding for A/B runs.
        """
        from app.db.session import SessionLocal

        if vad is None:
            vad = _shared_vad()
        session_id = str(config.bot_session_id)

        runtime = await build_agent_runtime(
            config,
            vad=vad,
            event_bus=event_bus,
            transcript_history_loader=transcript_history_loader,
            db_session_factory=SessionLocal,
            # Epoch-seconds reference so the metrics translator emits
            # session-relative ``started_at_ms`` (the subscriber writes it into
            # the INTEGER ``session_timings.started_at_ms``; a raw epoch-ms
            # offset overflows it on Postgres — Johnny-7g5.1).
            session_started_at=time.time(),
        )

        session = build_agent_session(
            stt=runtime.adapters.stt,
            llm=runtime.adapters.llm,
            tts=runtime.adapters.tts,
            vad=vad,
            enable_barge_in=runtime.enable_barge_in,
            min_interruption_duration_s=runtime.min_interruption_duration_s,
            # VAD endpointing, not a semantic EOU model: the multilingual turn
            # detector resolves its inference executor from get_job_context(),
            # which does not exist in the API process — and hosting it
            # in-process was wontfixed at ~884 MB RSS (Johnny-trt.6 spike;
            # see BROWSER_ENDPOINTING_MIN_DELAY_S above). Matches the legacy
            # browser pipeline (VAD-based turn-taking).
            turn_detection="vad",
            endpointing=endpointing if endpointing is not None else browser_endpointing(),
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
        return cls(
            runtime=runtime,
            session=session,
            transport=transport,
            audio_out=audio_out,
            transcript_sink=transcript_sink,
            session_id=session_id,
        )

    async def warm_up(self) -> None:
        """Pre-load the session providers' lazy heavy state (Johnny-trt.8).

        Delegates to :meth:`~johnny.agent.job_session.AgentRuntime.warm_up`
        (whisper weights, Piper voice ONNX, local-LLM model load — each
        provider's own ``warm_up()`` hook). The browser runner fires this as
        a background task right after :meth:`build`, concurrently with
        :meth:`start` — the session's ready signal never waits on it. Never
        raises; per-provider failures are logged inside.
        """
        await self._runtime.warm_up()

    async def start(self) -> None:
        """Bind the browser audio seams and start the session roomless.

        Setting ``input.audio`` / ``output.audio`` *before* ``start`` makes the
        SDK skip ``RoomIO`` (it only builds one when a ``room`` is given) and
        instead forward our input frames to the activity + drain our output sink.
        """
        self._session.input.audio = BrowserAudioInput(self._transport)
        self._session.output.audio = self._audio_out
        await self._session.start(agent=self._runtime.agent)
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

        new_message = LKChatMessage(role="user", content=[cleaned])
        try:
            await self._runtime.gate.run_turn(self._session.history, new_message)
        except StopResponse:
            # Router declined / suggest-only / listen-only — the gate already
            # emitted this turn's terminal; nothing to speak.
            return True
        except Exception:
            logger.exception(
                "browser agent feed_text: gate.run_turn failed for session=%s",
                self._session_id,
            )
            return True

        # SPEAK: generate the reply. The on_enter speech_created listener routes
        # it to gate.bind_reply, which pops the turn run_turn just recorded.
        try:
            self._session.generate_reply(user_input=cleaned)
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
    "BROWSER_VAD_MIN_SILENCE_DURATION_S",
    "BrowserAgentSession",
    "browser_endpointing",
    "build_browser_agent_session",
    "load_browser_vad",
]


async def build_browser_agent_session(
    transport: BrowserAudioTransport,
    config: SessionJobConfig,
    *,
    event_bus: EventBus,
    vad: VAD | None = None,
    transcript_history_loader: TranscriptHistoryLoader | None = None,
    endpointing: EndpointingOptions | None = None,
) -> BrowserAgentSession:
    """Functional alias for :meth:`BrowserAgentSession.build` (call-site clarity)."""
    return await BrowserAgentSession.build(
        transport,
        config,
        event_bus=event_bus,
        vad=vad,
        transcript_history_loader=transcript_history_loader,
        endpointing=endpointing,
    )
