"""Event/observability parity for the LiveKit agent path (Johnny-d5z — epic Johnny-7g5).

The legacy split pipeline published a fixed set of ``PipelineEvent``\\s to the
Redis :class:`~johnny.voice_pipeline.event_bus.EventBus`; a single subscriber
(``app.services.session_status_subscriber``) consumes that channel and persists
each event to its DB table — ``transcript_finalized`` → ``transcript_chunks``,
``router_decision_made`` → ``agent_decisions``, ``agent_spoke`` →
``agent_utterances`` (+ links the utterance to its decision row),
``pipeline_timing`` → ``session_timings``, and ``turn_terminal`` *stamps* the
turn's ``agent_decisions`` row by ``turn_id``. The meet-worker stays
SQLAlchemy-free; the subscriber owns every write.

This module is the **emit half** for the new :class:`~livekit.agents.AgentSession`
path: it maps the gate decisions + ``AgentSession`` lifecycle onto the *same*
``PipelineEvent`` set so the *same* subscriber persists them with no DB-side
change. It mirrors :mod:`johnny.agent.approval_wiring` — pure builders the agent
worker (Johnny-9eh) calls; the spike modules (:mod:`johnny.agent.gate`,
:mod:`johnny.agent.router_gate`) stay decoupled, taking these as injected
callbacks.

The lynchpin is the **str→int turn id** (:class:`~johnny.agent.gate.TurnIndex`).
The subscriber binds a turn's decision, terminal, and timing rows by an **int**
``turn_id`` (the legacy utterance counter), and coerces a non-int id to ``None``
— which would orphan every terminal from its decision row. So every event this
module emits for a turn carries the *same* int from the shared index, preserving
the decision↔utterance↔terminal parity the serialised legacy pipeline got for
free.

What this module maps (each `→` is "published to the EventBus; subscriber
persists"):

* gate decision (every non-``approval_required`` path) → ``RouterDecisionMade``
  → ``agent_decisions`` row, outcome chosen by the subscriber from the mode in
  ``input_window``. ``approval_required`` keeps its own sink-based pending row
  (Johnny-qzj) — emitting here too would double-write, so the gate skips it.
* reply spoke → ``AgentSpoke`` → ``agent_utterances`` (+ flips the decision row's
  ``final_text`` / outcome, INV-2).
* suggest-only → ``AgentSuggested`` → live UI (the ``suggested`` decision row is
  the ``RouterDecisionMade`` above).
* kept STT final → ``TranscriptFinalized`` → ``transcript_chunks``.
* turn terminal → ``TurnTerminal`` → stamps the decision row (INV-1).
* LiveKit ``MetricsCollectedEvent`` → ``PipelineTiming`` → ``session_timings``.
* gate triage span (Johnny-trt.19) → ``PipelineTiming(stage="router_llm")`` →
  ``session_timings`` — the router LLM is a side call the SDK emits no metric
  for, so the gate publishes its own per-decided-turn timing.

``ApprovalPending`` / ``ApprovalResolved`` are already wired by
:mod:`johnny.agent.approval_wiring`; they are out of scope here.

Imported only by the full-stack worker / tests — it reaches ``livekit`` (the
metrics event types) and ``johnny.voice_pipeline`` (events, event_bus), so it is
never pulled from the import-safe top-level :mod:`johnny.agent` package.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from johnny.agent.gate import (
    GateTerminal,
    GateTerminalState,
    SessionTerminalEmitter,
    TurnIndex,
    TurnNoReplyReason,
)
from johnny.voice_pipeline.audio_recorder import SpokenAudioRecorder
from johnny.voice_pipeline.event_bus import EventBus
from johnny.voice_pipeline.events import (
    AgentSpeechInterim,
    AgentSpoke,
    AgentSuggested,
    InterruptionRecorded,
    InterruptionWho,
    PipelineTiming,
    PipelineTimingStage,
    RouterDecisionMade,
    TranscriptFinalized,
    TranscriptInterim,
    TurnTerminal,
)
from johnny.voice_pipeline.reasoning import FREE_FORM_MODES, RouterDecision

if TYPE_CHECKING:
    from livekit.agents.voice.events import MetricsCollectedEvent

logger = logging.getLogger(__name__)


def _default_clock_ms() -> int:
    """Epoch milliseconds — the timestamp shape every pipeline event carries."""
    return int(time.time() * 1000)


# Callbacks the gate / agent invoke at each emit point. Kept as plain callables
# (not a bundle interface) so the gate stays decoupled and a smoke/bare agent
# omits them entirely (``None`` → no emission), exactly like the approval seams.

class RecordDecision(Protocol):
    """Publish a turn's ``RouterDecisionMade``.

    Args: the parsed decision and the LiveKit ``str`` turn id (translated to
    the durable ``int`` via the shared :class:`~johnny.agent.gate.TurnIndex`).
    ``transcript_window`` (Johnny-trt.54) is the rolling conversation the gate
    decided over — ``{text, speaker, confidence, is_current, timestamp_ms}``
    entries, the trigger transcript marked ``is_current`` — merged into the
    event's ``input_window`` so the decision row records what was heard (the
    session-detail "Heard you" step and the per-session replay both read it;
    without it an agent-path turn has no reconstructable transcript).
    """

    def __call__(
        self,
        decision: RouterDecision,
        turn_id: str,
        *,
        transcript_window: list[dict[str, Any]] | None = None,
    ) -> Awaitable[None]: ...


RecordSuggested = Callable[[RouterDecision, str], Awaitable[None]]
"""Publish a suggest-only turn's ``AgentSuggested``. Args mirror
:class:`RecordDecision`'s positional pair; the ``suggested`` decision row is the
``RouterDecisionMade`` the gate already emitted, so no decision id is needed here."""

SpokenKind = Literal["reply", "ack", "status", "correction", "task_result"]
"""Which speech path produced an utterance (Johnny-trt.54) — see
:class:`~johnny.voice_pipeline.events.AgentSpoke.kind`. ``task_result`` is the
Phase-5 out-of-band result delivery (Johnny-trt.28): bound to no turn, exactly
like ``correction``."""


class RecordSpoke(Protocol):
    """Publish a spoken utterance's ``AgentSpoke``.

    ``text`` is what the bot actually spoke (the reply ``SpeechHandle``'s chat
    items, or the say()-path line verbatim). ``turn_id`` is the LiveKit ``str``
    turn id of the turn that owns the speech — resolved to the durable int by
    the builder so the subscriber stamps the exact decision row (INV-2);
    ``None`` for speech bound to no turn (the trt.53 correction). ``kind``
    labels the speech path (:data:`SpokenKind`); the subscriber refuses to
    stamp any ``final_text`` for ``"correction"``. ``interrupted``
    (Johnny-trt.58) marks a barge-in partial — ``text`` is then the caption
    sentences flushed by cut time, not the full planned line. The builder
    fills the rest (mode, matched-allowed-reply heuristic, session id, reply
    audio).
    """

    def __call__(
        self,
        text: str,
        *,
        turn_id: str | None = None,
        kind: SpokenKind = "reply",
        interrupted: bool = False,
    ) -> Awaitable[None]: ...

class RecordTriageTiming(Protocol):
    """Publish one turn's triage-stage ``PipelineTiming`` (Johnny-trt.19).

    Args: the LiveKit ``str`` turn id, the triage call's epoch-seconds start
    and end (``time.time()``), and the decided action (``silent`` / ``speak``
    / ``delegate`` / ``status`` — carried in ``details`` so the activity log
    can read the verdict off the timing row). The router LLM runs as a *side*
    ``LLMProvider`` call, never through the session ``llm_node``, so LiveKit
    emits no metric for it — without this seam the triage cost is invisible
    in ``session_timings`` (the pre-trt.19 state: the cost could only be
    inferred from the STT-final → answer-LLM-start gap, which does not even
    exist for delegate/status turns that pay no answer hop).

    ``prompt_chars`` (Johnny-trt.55) is the built router prompt's size in
    characters — persisted into ``details`` so task-catalog growth (and the
    unavailable-block render cap) stays measurable per turn; ``None`` (the
    prompt was never measured) is simply omitted from the row.
    """

    def __call__(
        self,
        turn_id: str,
        started_at: float,
        ended_at: float,
        action: str,
        *,
        prompt_chars: int | None = None,
    ) -> Awaitable[None]: ...

class RecordInterruption(Protocol):
    """Publish one cut speech's ``InterruptionRecorded`` (Johnny-trt.49).

    Called by the gate from every ``handle.interrupted`` settle path
    (reply, ack/status say, correction, task result) with the attribution
    its :class:`~johnny.agent.interruptions.InterruptionMonitor` resolved:
    ``who`` cut the speech and the onset→stop ``cut_latency_ms`` (``None``
    when no cause was observed). ``turn_id`` is the LiveKit ``str`` turn id
    for turn-bound speech (resolved to the durable int by the builder, the
    :class:`RecordSpoke` discipline) and ``None`` for out-of-band speech;
    ``speech_kind`` is the :data:`SpokenKind` that was cut; ``partial_kept``
    mirrors whether a trt.58 partial ``AgentSpoke`` survived.
    """

    def __call__(
        self,
        who: InterruptionWho,
        *,
        cut_latency_ms: int | None,
        speech_kind: SpokenKind,
        turn_id: str | None = None,
        partial_kept: bool = False,
    ) -> Awaitable[None]: ...


TranscriptFinalizedSink = Callable[[TranscriptFinalized], Awaitable[None]]
"""Publish a kept STT final's ``TranscriptFinalized`` (mirror of
:data:`~johnny.agent.noise_filter.TranscriptFilteredSink` for the non-noise path)."""

SpeechInterimSink = Callable[[str, int], None]
"""Publish one flushed reply sentence as an ``AgentSpeechInterim`` (Johnny-trt.39).

Args: the sentence text and its 0-based ``sequence`` within the reply. Sync
and fire-and-forget — :meth:`JohnnyAgent.tts_node` calls it on the audio hot
path right before synthesising the sentence, so it must never await or raise
into the node (the forwarder schedules the publish as a task)."""


def terminal_outcome(
    terminal_state: GateTerminalState,
    no_reply_reason: TurnNoReplyReason | None,
) -> str:
    """Map a :class:`~johnny.agent.gate.GateTerminal` to a ``DecisionOutcome`` value.

    The gate harness carries only the coarse ``terminal_state`` + the
    ``no_reply_reason``; the ``agent_decisions.outcome`` column wants the
    fine-grained value the legacy pipeline stamped per terminal branch
    (the legacy split pipeline):

    * ``replied`` → ``spoken``;
    * ``pending_approval`` → ``pending``;
    * ``no_reply(suggest_only)`` → ``suggested`` (router approved, mode silenced it);
    * ``no_reply(approval_rejected)`` → ``rejected``;
    * every other ``no_reply`` (declined / low-confidence / barge-in / rate-limit /
      tts-unavailable / empty / no-match / noise / stage-error / listen-only) →
      ``suppressed``.

    The subscriber stamps this onto the row and demotes an optimistic ``spoken``
    (written at router time for auto-speak modes) when the terminal is a real
    ``no_reply``.
    """
    if terminal_state == "replied":
        return "spoken"
    if terminal_state == "pending_approval":
        return "pending"
    if no_reply_reason == "suggest_only":
        return "suggested"
    if no_reply_reason == "approval_rejected":
        return "rejected"
    return "suppressed"


def build_session_terminal_emitter(
    event_bus: EventBus,
    turn_index: TurnIndex,
    *,
    session_id: str | None = None,
    clock: Callable[[], int] = _default_clock_ms,
) -> SessionTerminalEmitter:
    """Build the ledger's :data:`~johnny.agent.gate.SessionTerminalEmitter` (INV-1).

    The spike Johnny-o3z left the durable terminal wiring as an injected
    ``Callable[[str, GateTerminal], Awaitable[None]]``; this is its production
    body. It translates the LiveKit ``str`` turn id to the durable ``int`` (so
    the subscriber binds the terminal to the turn's decision row) and publishes a
    :class:`~johnny.voice_pipeline.events.TurnTerminal`. Defensive: a failing bus
    is logged but never re-raised, so emitting a terminal can never crash the
    teardown / reply-completion path that calls it (legacy
    ``_emit_turn_terminal`` parity).
    """

    async def _emit(turn_id: str, terminal: GateTerminal) -> None:
        event = TurnTerminal(
            turn_id=turn_index.resolve(turn_id),
            terminal_state=terminal.terminal_state,
            outcome=terminal_outcome(terminal.terminal_state, terminal.no_reply_reason),
            no_reply_reason=terminal.no_reply_reason,
            detail=terminal.detail,
            timestamp_ms=clock(),
            session_id=session_id,
        )
        try:
            await event_bus.publish(event)
        except Exception:
            logger.exception(
                "failed to publish turn_terminal for session=%s turn=%s — "
                "the turn's terminal audit row will be missing",
                session_id,
                event.turn_id,
            )

    return _emit


def build_decision_emitter(
    event_bus: EventBus,
    turn_index: TurnIndex,
    *,
    mode: str,
    approval_timeout_seconds: float | None = None,
    instructions: str = "",
    confidence_threshold: float | None = None,
    allowed_replies: tuple[str, ...] = (),
    session_id: str | None = None,
    clock: Callable[[], int] = _default_clock_ms,
) -> RecordDecision:
    """Build the gate's per-turn ``RouterDecisionMade`` emitter.

    Mirrors the legacy publish in ``_respond_to_transcript_inner`` right after the
    router returns: one event per turn the router decided on, carrying the int
    ``turn_id`` (so the later ``TurnTerminal`` stamps the same row) and an
    ``input_window`` whose ``mode`` lets the subscriber pick the row's outcome
    (``suggested`` / ``spoken`` / ``suppressed``). ``approval_timeout_seconds`` is
    included for shape parity (the subscriber reads it for approval rounds) though
    the gate skips this emitter in ``approval_required`` mode.

    Johnny-trt.54 widened ``input_window`` toward the legacy pipeline's shape so
    the decision row is self-describing again: the static run config
    (``instructions`` / ``confidence_threshold`` / ``allowed_replies``, only when
    set) plus the per-turn ``transcript_window`` the gate passes — the rolling
    conversation with the trigger transcript marked ``is_current``. The
    session-detail timeline's "Heard you" / "Looked at the context" steps and the
    per-session replay fixture (``load_replay_fixture``) all read these keys;
    before this an agent-path session had **zero** reconstructable turns.
    """
    input_window: dict[str, Any] = {"mode": mode}
    if approval_timeout_seconds is not None:
        input_window["approval_timeout_seconds"] = approval_timeout_seconds
    if instructions:
        input_window["instructions"] = instructions
    if confidence_threshold is not None:
        input_window["confidence_threshold"] = confidence_threshold
    if allowed_replies:
        input_window["allowed_replies"] = list(allowed_replies)

    async def _record(
        decision: RouterDecision,
        turn_id: str,
        *,
        transcript_window: list[dict[str, Any]] | None = None,
    ) -> None:
        window = dict(input_window)
        if transcript_window:
            window["transcript_window"] = list(transcript_window)
        event = RouterDecisionMade(
            should_speak=decision.should_speak,
            confidence=decision.confidence,
            reason=decision.reason,
            reply_type=decision.reply_type,
            suggested_reply=decision.suggested_reply,
            timestamp_ms=clock(),
            session_id=session_id,
            input_window=window,
            raw_output=dict(decision.raw),
            turn_id=turn_index.resolve(turn_id),
        )
        try:
            await event_bus.publish(event)
        except Exception:
            logger.exception(
                "failed to publish router_decision_made for session=%s turn=%s",
                session_id,
                turn_id,
            )

    return _record


def build_suggested_emitter(
    event_bus: EventBus,
    *,
    session_id: str | None = None,
    clock: Callable[[], int] = _default_clock_ms,
) -> RecordSuggested:
    """Build the suggest-only ``AgentSuggested`` emitter (Johnny-5ag deferred this here).

    Port of the legacy split pipeline's event: the router approved a
    reply but the meeting is ``suggest_only``, so the suggestion is surfaced to
    the UI and nothing is spoken. ``decision_id`` is ``None`` — unlike the legacy
    (which had a synchronous sink id), the new path's decision row is written
    asynchronously by the subscriber, so the UI correlates the suggestion by
    session + recency, not by id.
    """

    async def _record(decision: RouterDecision, turn_id: str) -> None:
        del turn_id  # carried by the paired RouterDecisionMade / TurnTerminal
        event = AgentSuggested(
            suggested_reply=(decision.suggested_reply or "").strip(),
            timestamp_ms=clock(),
            decision_id=None,
            reason=decision.reason,
            reply_type=decision.reply_type,
            session_id=session_id,
        )
        try:
            await event_bus.publish(event)
        except Exception:
            logger.exception("failed to publish agent_suggested for session=%s", session_id)

    return _record


def build_spoke_emitter(
    event_bus: EventBus,
    *,
    mode: str,
    allowed_replies: tuple[str, ...] = (),
    session_id: str | None = None,
    clock: Callable[[], int] = _default_clock_ms,
    recorder: SpokenAudioRecorder | None = None,
    turn_index: TurnIndex | None = None,
) -> RecordSpoke:
    """Build the ``AgentSpoke`` emitter for every spoken utterance.

    Port of the legacy split pipeline's publish: emitted once when a
    reply completes with assistant output — and, since Johnny-trt.54, once per
    completed say()-path speech too (delegate ack, status stub, the trt.53
    failed-task correction), so *every* spoken utterance lands in
    ``agent_utterances`` and the chat history. Since Johnny-trt.58 an
    interrupted speech that produced captions emits one as well
    (``interrupted=True``, ``text`` = the partial flushed by cut time) so a
    barged reply keeps its partial instead of vanishing. The subscriber inserts the
    ``agent_utterances`` row and writes the spoken text back onto the turn's
    decision row (INV-2) — except for ``kind="correction"``, which is bound to
    no turn and stamps nothing. ``matched_allowed_reply`` is inferred from the
    active mode + allow-list (an exact, case-insensitive match means the answer
    stage spoke a verbatim allow-listed reply, so the subscriber attributes the
    divergence to ``allowlist`` rather than ``answer_llm``); ``prompt`` is empty
    (the per-turn answer prompt is internal to the LiveKit reply pipeline). Both
    are accepted ``None``/``0``/empty by the subscriber and the utterance audit
    view.

    ``turn_index`` is the session's shared str→int index: the gate passes the
    LiveKit ``str`` turn id and the event carries the durable ``int`` (the same
    value the turn's ``RouterDecisionMade`` / ``TurnTerminal`` carry), so the
    subscriber stamps the *exact* decision row instead of a most-recent scan.
    Without it (or for ``turn_id=None`` speech) the event ships ``turn_id=None``
    and the subscriber falls back to the legacy scan.

    ``recorder`` is the session's :class:`SpokenAudioRecorder` (Johnny-od1): the
    TTS adapter fed it every synthesized segment of this reply, and flushing it
    here yields the reply's WAV filename + exact duration for the event
    (``take_reply`` does file I/O, so it runs in a thread). Without a recorder
    — or when it has nothing buffered (recording disabled, no-TTS degrade) —
    ``audio_file`` is ``None`` and ``audio_duration_ms`` stays ``0``, the
    pre-capture shape (the reply ``SpeechHandle`` exposes no synth duration;
    the TTS :class:`PipelineTiming` still carries the LiveKit metric).
    """
    uses_allowlist = bool(allowed_replies) and mode not in FREE_FORM_MODES
    lowered = {r.casefold(): r for r in allowed_replies}

    async def _record(
        text: str,
        *,
        turn_id: str | None = None,
        kind: SpokenKind = "reply",
        interrupted: bool = False,
    ) -> None:
        matched: str | None = None
        if uses_allowlist:
            matched = lowered.get(text.strip().casefold())
        reply_audio = None
        if recorder is not None:
            try:
                reply_audio = await asyncio.to_thread(recorder.take_reply)
            except Exception:
                logger.exception(
                    "failed flushing reply audio for session=%s", session_id
                )
        event = AgentSpoke(
            text=text,
            audio_duration_ms=reply_audio.duration_ms if reply_audio is not None else 0,
            timestamp_ms=clock(),
            matched_allowed_reply=matched,
            session_id=session_id,
            prompt="",
            audio_file=reply_audio.filename if reply_audio is not None else None,
            kind=kind,
            turn_id=(
                turn_index.resolve(turn_id)
                if turn_index is not None and turn_id is not None
                else None
            ),
            interrupted=interrupted,
        )
        try:
            await event_bus.publish(event)
        except Exception:
            logger.exception("failed to publish agent_spoke for session=%s", session_id)

    return _record


def build_triage_timing_emitter(
    event_bus: EventBus,
    turn_index: TurnIndex,
    *,
    provider_name: str | None = None,
    session_started_at: float = 0.0,
    session_id: str | None = None,
) -> RecordTriageTiming:
    """Build the gate's per-turn triage ``PipelineTiming`` emitter (Johnny-trt.19).

    Publishes a ``stage="router_llm"`` timing the subscriber persists to
    ``session_timings`` (the stage is already in its whitelist — the legacy
    pipeline used it for the same call). ``turn_id`` resolves through the
    shared :class:`~johnny.agent.gate.TurnIndex` so the row groups with the
    turn's decision / terminal rows.

    ``started_at_ms`` is the session-relative offset of the call *start* (the
    documented ``PipelineTiming`` field semantic — note this differs from the
    LiveKit-metric rows :class:`MetricsTranslator` emits, whose 1.5.17
    ``timestamp`` is stamped at stage END; consumers of THIS row need no
    ``- duration_ms`` compensation). ``session_started_at`` is the same epoch
    reference the translator gets; ``<= 0`` falls back to raw epoch ms.
    Defensive like every emitter here: a failing bus is logged, never raised
    into the gate.
    """

    async def _record(
        turn_id: str,
        started_at: float,
        ended_at: float,
        action: str,
        *,
        prompt_chars: int | None = None,
    ) -> None:
        if session_started_at > 0:
            started_at_ms = round((started_at - session_started_at) * 1000)
        else:
            started_at_ms = round(started_at * 1000)
        details: dict[str, Any] = {"action": action}
        if prompt_chars is not None:
            # Router prompt size (Johnny-trt.55): catalog growth visible per
            # turn, so the unavailable-block render cap stays enforceable.
            details["prompt_chars"] = prompt_chars
        event = PipelineTiming(
            turn_id=turn_index.resolve(turn_id),
            stage="router_llm",
            started_at_ms=max(0, started_at_ms),
            duration_ms=max(0, round((ended_at - started_at) * 1000)),
            provider_name=provider_name or None,
            details=details,
            session_id=session_id,
        )
        try:
            await event_bus.publish(event)
        except Exception:
            logger.exception(
                "failed to publish triage timing for session=%s turn=%s",
                session_id,
                turn_id,
            )

    return _record


def build_interruption_emitter(
    event_bus: EventBus,
    turn_index: TurnIndex,
    *,
    session_started_at: float = 0.0,
    session_id: str | None = None,
    clock: Callable[[], float] = time.time,
) -> RecordInterruption:
    """Build the gate's cut-speech ``InterruptionRecorded`` emitter (Johnny-trt.49).

    Publishes the conversation-dynamics record the subscriber persists to
    ``conversation_events``. ``timestamp_ms`` is the session-relative offset
    of the audio stop (the ``build_triage_timing_emitter`` convention:
    epoch-seconds ``clock`` minus ``session_started_at``; a ``<= 0``
    reference falls back to raw epoch ms). ``turn_id`` resolves through the
    shared :class:`~johnny.agent.gate.TurnIndex` so the row groups with the
    cut turn's decision / terminal / timing rows in the activity log;
    ``None`` (out-of-band speech) stays ``None``. Defensive like every
    emitter here: a failing bus is logged, never raised into the gate's
    done-callback.
    """

    async def _record(
        who: InterruptionWho,
        *,
        cut_latency_ms: int | None,
        speech_kind: SpokenKind,
        turn_id: str | None = None,
        partial_kept: bool = False,
    ) -> None:
        now = clock()
        if session_started_at > 0:
            timestamp_ms = round((now - session_started_at) * 1000)
        else:
            timestamp_ms = round(now * 1000)
        event = InterruptionRecorded(
            who=who,
            timestamp_ms=max(0, timestamp_ms),
            cut_latency_ms=cut_latency_ms,
            speech_kind=speech_kind,
            turn_id=turn_index.resolve(turn_id) if turn_id is not None else None,
            partial_kept=partial_kept,
            session_id=session_id,
        )
        try:
            await event_bus.publish(event)
        except Exception:
            logger.exception(
                "failed to publish interruption_recorded for session=%s turn=%s",
                session_id,
                turn_id,
            )

    return _record


def build_transcript_finalized_emitter(
    event_bus: EventBus,
    *,
    session_id: str | None = None,
) -> TranscriptFinalizedSink:
    """Build the kept-STT-final ``TranscriptFinalized`` emitter.

    Mirror of :data:`~johnny.agent.noise_filter.TranscriptFilteredSink` for the
    candidates the noise gate *keeps*: the agent's ``stt_node`` publishes one of
    these per final transcript that survived the gate, so the subscriber writes a
    ``transcript_chunks`` row (the durable transcript the history view renders and
    the next session rehydrates). The event is pre-built by the node (it owns the
    timestamp / speaker / confidence); this sink only publishes, defensively.
    """

    async def _record(event: TranscriptFinalized) -> None:
        try:
            await event_bus.publish(event)
        except Exception:
            logger.exception("failed to publish transcript_finalized for session=%s", session_id)

    return _record


class InterimTranscriptForwarder:
    """Bridge ``user_input_transcribed`` events to ``TranscriptInterim`` (Johnny-trt.13).

    ``AgentSession.on("user_input_transcribed", cb)`` fires a *synchronous*
    callback with a ``UserInputTranscribedEvent(transcript, is_final, ...)``
    for every transcript the SDK's audio recognition sees — interims included
    (streaming STT only; the batch ``StreamAdapter`` path emits finals only).
    This forwarder is the live-caption emit seam: each non-final hypothesis is
    published as a :class:`~johnny.voice_pipeline.events.TranscriptInterim`
    so the playground can render it while the user is still speaking. It owns
    the same sync→async bridge as :class:`MetricsTranslator` (fire-and-forget
    publish tasks held by strong refs, drained at :meth:`aclose`).

    Finals are *skipped* — the durable ``TranscriptFinalized`` is emitted by
    the agent's ``stt_node`` gate (:func:`build_transcript_finalized_emitter`),
    and noise-gated interims never reach the SDK at all, so the caption stream
    inherits the noise filter for free. Empty hypotheses and consecutive
    duplicates (some providers re-send an unchanged interim) are dropped to
    keep the wire quiet; the duplicate guard resets on every final so the next
    turn's first caption always goes out even when textually identical.

    ``clock`` returns the session-relative ``timestamp_ms`` (the same shape
    its sibling ``TranscriptFinalized`` carries — never persisted, but kept
    consistent for subscribers that order by it).
    """

    def __init__(
        self,
        event_bus: EventBus,
        *,
        clock: Callable[[], int],
        session_id: str | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._clock = clock
        self._session_id = session_id
        self._last_text: str | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    def on_user_input_transcribed(self, ev: Any) -> None:
        """Sync ``user_input_transcribed`` listener — build + schedule the publish."""
        if bool(getattr(ev, "is_final", False)):
            # The stt_node gate owns the final's durable event; just re-arm the
            # duplicate guard for the next turn's captions.
            self._last_text = None
            return
        text = getattr(ev, "transcript", None)
        if not isinstance(text, str) or not text.strip():
            return
        if text == self._last_text:
            return
        self._last_text = text
        speaker = getattr(ev, "speaker_id", None)
        event = TranscriptInterim(
            text=text,
            timestamp_ms=self._clock(),
            speaker=speaker if isinstance(speaker, str) and speaker else None,
            session_id=self._session_id,
        )
        task = asyncio.ensure_future(self._publish(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _publish(self, event: TranscriptInterim) -> None:
        try:
            await self._event_bus.publish(event)
        except Exception:
            logger.debug(
                "interim transcript emit failed for session=%s",
                self._session_id,
                exc_info=True,
            )

    async def aclose(self) -> None:
        """Await any in-flight publishes so teardown doesn't leak pending tasks."""
        if not self._tasks:
            return
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)


class AgentSpeechInterimForwarder:
    """Bridge per-sentence TTS flushes to ``AgentSpeechInterim`` events (Johnny-trt.39).

    The bot-side mirror of :class:`InterimTranscriptForwarder`:
    :meth:`on_sentence_flushed` is the :data:`SpeechInterimSink` the agent's
    ``tts_node`` calls for each sentence ``iter_sentences`` hands to TTS, so
    the playground can render Johnny's reply text while the audio is still
    being synthesised. Same sync→async bridge (fire-and-forget publish tasks
    held by strong refs, drained at :meth:`aclose`) — the TTS hot path must
    never wait on Redis.

    ``resolve_turn`` returns the durable int turn id of the gated reply now
    being spoken (``None`` for an ungated speech — a ``say()`` / approval
    reply). It is resolved once per reply at ``sequence == 0`` and reused for
    the reply's later sentences: the gate's ``active_reply`` is "most recently
    *bound*" rather than "now playing", so a rapid next turn binding mid-reply
    must not re-attribute the tail sentences of the current one.
    """

    def __init__(
        self,
        event_bus: EventBus,
        *,
        resolve_turn: Callable[[], int | None],
        clock: Callable[[], int] = _default_clock_ms,
        session_id: str | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._resolve_turn = resolve_turn
        self._clock = clock
        self._session_id = session_id
        self._turn_id: int | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    def on_sentence_flushed(self, text: str, sequence: int) -> None:
        """Sync :data:`SpeechInterimSink` — build + schedule the publish."""
        if not text.strip():
            return
        if sequence == 0:
            self._turn_id = self._safe_resolve_turn()
        event = AgentSpeechInterim(
            text=text,
            sequence=sequence,
            timestamp_ms=self._clock(),
            turn_id=self._turn_id,
            session_id=self._session_id,
        )
        task = asyncio.ensure_future(self._publish(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _safe_resolve_turn(self) -> int | None:
        """Resolve the active reply's turn id; an uncorrelated reply is ``None``."""
        try:
            return self._resolve_turn()
        except Exception:
            logger.debug(
                "speech interim turn resolution failed for session=%s",
                self._session_id,
                exc_info=True,
            )
            return None

    async def _publish(self, event: AgentSpeechInterim) -> None:
        try:
            await self._event_bus.publish(event)
        except Exception:
            logger.debug(
                "speech interim emit failed for session=%s",
                self._session_id,
                exc_info=True,
            )

    async def aclose(self) -> None:
        """Await any in-flight publishes so teardown doesn't leak pending tasks."""
        if not self._tasks:
            return
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)


class SpeechCaptionBuffer:
    """The caption sentences of the speech playing now (Johnny-trt.58).

    The text-side analogue of the reply-audio buffer hygiene: ``tts_node``
    flushes one sentence at a time into TTS (the same flushes the
    :class:`AgentSpeechInterimForwarder` publishes as live captions), and this
    buffer accumulates them so the gate's done-callbacks can recover *what was
    delivered so far* when a barge-in cuts the speech. The snapshot is an
    honest **approximation** of what was audibly heard: a sentence is buffered
    when it is flushed to synthesis, which slightly leads playout — the same
    lead the live caption bubble shows, so the kept partial always matches
    what the operator watched stream.

    ``sequence == 0`` marks a fresh speech and replaces any stale buffer
    (mirroring the caption forwarder's reset); :meth:`take` empties on read so
    every done-path consumes its own speech's sentences and a later speech can
    never inherit them. At most one speech synthesizes at a time (LiveKit
    serializes playout), so a plain list — no per-speech keying — is enough;
    the done-callback runs ticks before the next speech's first flush.
    """

    def __init__(self) -> None:
        self._sentences: list[str] = []

    def note(self, text: str, sequence: int) -> None:
        """Record one flushed sentence; ``sequence == 0`` starts a fresh speech."""
        if sequence == 0:
            self._sentences = []
        cleaned = text.strip()
        if cleaned:
            self._sentences.append(cleaned)

    def take(self) -> str:
        """The speech's sentences so far, space-joined — and clear the buffer."""
        joined = " ".join(self._sentences)
        self._sentences = []
        return joined


# --- LiveKit metrics → PipelineTiming translation -------------------------- #

# LiveKit ``MetricsCollectedEvent.metrics.type`` → our ``PipelineTimingStage``.
# Only the three provider stages map cleanly: the router LLM runs as a *side*
# ``LLMProvider`` call (not through the session ``llm_node``), so the sole
# ``llm_metrics`` the SDK emits is the *answer* LLM. ``eou_metrics`` /
# ``vad_metrics`` describe turn detection, not a Johnny pipeline stage, and have
# no faithful ``end_to_end`` mapping (the true user-speech-end→first-audio number
# isn't reconstructable from a single metric), so they are dropped here — the
# subscriber would drop any non-whitelisted stage anyway.
_METRIC_TYPE_TO_STAGE: dict[str, PipelineTimingStage] = {
    "stt_metrics": "stt",
    "llm_metrics": "answer_llm",
    "tts_metrics": "tts",
}


def _ms(seconds: Any) -> int:
    """Whole milliseconds from a float-seconds metric field; ``0`` when absent/bad."""
    try:
        return max(0, round(float(seconds) * 1000))
    except (TypeError, ValueError):
        return 0


def metric_to_timing(
    metric: Any,
    *,
    turn_id: int,
    started_at_ms: int,
    session_id: str | None = None,
) -> PipelineTiming | None:
    """Translate one LiveKit metric to a :class:`PipelineTiming`, or ``None`` to drop.

    Pure (no I/O), so the mapping is unit-testable on a crafted metric without a
    running session — the analogue of :func:`~johnny.agent.answer.iter_sentences`
    vs ``tts_node``. Reads fields by name with :func:`getattr` so it tolerates the
    metric pydantic models without importing them. ``duration`` (and the
    ``ttft`` / ``ttfb`` / ``audio_duration`` sub-timings) are seconds on the
    metric and converted to ms; the ``details`` bag carries the same TTFT /
    first-audio / token extras the legacy ``_emit_timing`` stashed for the
    activity log.
    """
    metric_type = getattr(metric, "type", None)
    if not isinstance(metric_type, str):
        return None
    stage = _METRIC_TYPE_TO_STAGE.get(metric_type)
    if stage is None:
        return None
    provider_name = getattr(metric, "label", None)
    if not isinstance(provider_name, str) or not provider_name:
        provider_name = None
    details: dict[str, Any] = {}
    if stage == "stt":
        details["audio_duration_ms"] = _ms(getattr(metric, "audio_duration", 0))
        details["streamed"] = bool(getattr(metric, "streamed", False))
    elif stage == "answer_llm":
        details["time_to_first_token_ms"] = _ms(getattr(metric, "ttft", 0))
        details["completion_tokens"] = int(getattr(metric, "completion_tokens", 0) or 0)
        details["prompt_tokens"] = int(getattr(metric, "prompt_tokens", 0) or 0)
        details["total_tokens"] = int(getattr(metric, "total_tokens", 0) or 0)
        details["cancelled"] = bool(getattr(metric, "cancelled", False))
    else:  # tts
        details["time_to_first_audio_ms"] = _ms(getattr(metric, "ttfb", 0))
        details["audio_duration_ms"] = _ms(getattr(metric, "audio_duration", 0))
        details["characters_count"] = int(getattr(metric, "characters_count", 0) or 0)
        details["cancelled"] = bool(getattr(metric, "cancelled", False))
    return PipelineTiming(
        turn_id=max(0, turn_id),
        stage=stage,
        started_at_ms=max(0, started_at_ms),
        duration_ms=_ms(getattr(metric, "duration", 0)),
        provider_name=provider_name,
        details=details,
        session_id=session_id,
    )


# Resolve a metric's ``speech_id`` (the reply ``SpeechHandle.id``, present on
# LLM/TTS metrics; absent on STT) to the durable int turn id. The worker wires
# this to the gate's reply→turn binding + the shared TurnIndex.
ResolveTurnId = Callable[[str | None], int]


class MetricsTranslator:
    """Adapt LiveKit's sync ``metrics_collected`` callback to ``PipelineTiming`` emits.

    ``AgentSession.on("metrics_collected", cb)`` fires a *synchronous* callback on
    the event loop; publishing to the (async) bus must be scheduled as a task.
    This translator owns that bridge: :meth:`on_metrics_collected` is the sync
    callback the agent registers; it resolves the turn id, translates the metric
    (:func:`metric_to_timing`), and fire-and-forgets the publish, holding a strong
    ref to each task so it isn't GC'd mid-flight (the gate's ``_reply_tasks``
    pattern). :meth:`aclose` drains any in-flight publishes at teardown.

    ``session_started_at`` is the loop/epoch reference the metric ``timestamp`` is
    offset from to produce the session-relative ``started_at_ms`` the activity log
    renders; ``0`` falls back to the raw metric timestamp.
    """

    def __init__(
        self,
        event_bus: EventBus,
        *,
        resolve_turn_id: ResolveTurnId,
        session_started_at: float = 0.0,
        session_id: str | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._resolve_turn_id = resolve_turn_id
        self._session_started_at = session_started_at
        self._session_id = session_id
        self._tasks: set[asyncio.Task[None]] = set()

    def on_metrics_collected(self, ev: MetricsCollectedEvent) -> None:
        """Sync ``metrics_collected`` listener — translate + schedule the publish."""
        metric = getattr(ev, "metrics", None)
        if metric is None:
            return
        timing = self._translate(metric)
        if timing is None:
            return
        task = asyncio.ensure_future(self._publish(timing))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _translate(self, metric: Any) -> PipelineTiming | None:
        speech_id = getattr(metric, "speech_id", None)
        turn_id = self._resolve_turn_id(speech_id if isinstance(speech_id, str) else None)
        started_at_ms = self._started_at_ms(metric)
        return metric_to_timing(
            metric,
            turn_id=turn_id,
            started_at_ms=started_at_ms,
            session_id=self._session_id,
        )

    def _started_at_ms(self, metric: Any) -> int:
        ts: Any = getattr(metric, "timestamp", None)
        try:
            ts_f = float(ts)
        except (TypeError, ValueError):
            return 0
        if self._session_started_at <= 0:
            return max(0, round(ts_f * 1000))
        return max(0, round((ts_f - self._session_started_at) * 1000))

    async def _publish(self, timing: PipelineTiming) -> None:
        try:
            await self._event_bus.publish(timing)
        except Exception:
            logger.debug(
                "timing emit failed for session=%s stage=%s",
                self._session_id,
                timing.stage,
                exc_info=True,
            )

    async def aclose(self) -> None:
        """Await any in-flight publishes so a teardown doesn't drop the last timings."""
        if not self._tasks:
            return
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)


@dataclass(frozen=True, slots=True)
class Observability:
    """The wired emit seams for one agent session (the factory's return).

    The agent worker (Johnny-9eh) threads these into the gate / agent:
    ``record_decision`` / ``record_spoke`` / ``record_suggested`` onto the
    :class:`~johnny.agent.router_gate.RouterGate`; ``transcript_finalized_sink``
    onto the :class:`~johnny.agent.session.JohnnyAgent`; ``session_terminal_emitter``
    onto the :class:`~johnny.agent.gate.TurnLedger`; and ``metrics_translator``'s
    :meth:`~MetricsTranslator.on_metrics_collected` onto the session's
    ``metrics_collected`` event (and its :meth:`~MetricsTranslator.aclose` at
    teardown).
    """

    session_terminal_emitter: SessionTerminalEmitter
    record_decision: RecordDecision
    record_spoke: RecordSpoke
    record_suggested: RecordSuggested
    transcript_finalized_sink: TranscriptFinalizedSink
    metrics_translator: MetricsTranslator


def build_observability(
    event_bus: EventBus,
    turn_index: TurnIndex,
    *,
    mode: str,
    allowed_replies: tuple[str, ...] = (),
    approval_timeout_seconds: float | None = None,
    instructions: str = "",
    confidence_threshold: float | None = None,
    resolve_turn_id: ResolveTurnId,
    session_started_at: float = 0.0,
    session_id: str | None = None,
    clock: Callable[[], int] = _default_clock_ms,
    recorder: SpokenAudioRecorder | None = None,
) -> Observability:
    """Wire every emit seam against one ``EventBus`` + shared ``TurnIndex``.

    The single entry point the agent worker calls once it has the session's mode,
    allow-list, and a ``resolve_turn_id`` bound to the gate's reply→turn map. The
    gate skips ``record_decision`` in ``approval_required`` mode (Johnny-qzj owns
    that row), so passing the emitter unconditionally here is safe — it is only
    invoked on the non-approval paths.
    """
    return Observability(
        session_terminal_emitter=build_session_terminal_emitter(
            event_bus, turn_index, session_id=session_id, clock=clock
        ),
        record_decision=build_decision_emitter(
            event_bus,
            turn_index,
            mode=mode,
            approval_timeout_seconds=approval_timeout_seconds,
            instructions=instructions,
            confidence_threshold=confidence_threshold,
            allowed_replies=allowed_replies,
            session_id=session_id,
            clock=clock,
        ),
        record_spoke=build_spoke_emitter(
            event_bus,
            mode=mode,
            allowed_replies=allowed_replies,
            session_id=session_id,
            clock=clock,
            recorder=recorder,
            turn_index=turn_index,
        ),
        record_suggested=build_suggested_emitter(event_bus, session_id=session_id, clock=clock),
        transcript_finalized_sink=build_transcript_finalized_emitter(
            event_bus, session_id=session_id
        ),
        metrics_translator=MetricsTranslator(
            event_bus,
            resolve_turn_id=resolve_turn_id,
            session_started_at=session_started_at,
            session_id=session_id,
        ),
    )


__all__ = [
    "AgentSpeechInterimForwarder",
    "InterimTranscriptForwarder",
    "MetricsTranslator",
    "Observability",
    "RecordDecision",
    "RecordInterruption",
    "RecordSpoke",
    "RecordSuggested",
    "RecordTriageTiming",
    "ResolveTurnId",
    "SpeechCaptionBuffer",
    "SpeechInterimSink",
    "SpokenKind",
    "TranscriptFinalizedSink",
    "build_decision_emitter",
    "build_interruption_emitter",
    "build_observability",
    "build_session_terminal_emitter",
    "build_spoke_emitter",
    "build_suggested_emitter",
    "build_transcript_finalized_emitter",
    "build_triage_timing_emitter",
    "metric_to_timing",
    "terminal_outcome",
]
