"""Router "should-speak" gate for ``Agent.on_user_turn_completed`` (Johnny-xpa).

This is the Phase-2 port of the legacy split pipeline router decision into
LiveKit Agents' blocking turn hook. When the user finishes speaking, the SDK
``await``\\s :meth:`livekit.agents.Agent.on_user_turn_completed` *before* it
generates any reply (verified ``livekit-agents==1.5.17``); raising
:class:`~livekit.agents.llm.StopResponse` from the hook makes the SDK drop the
turn silently. :class:`RouterGate` runs Johnny's router ``LLMProvider`` inside
that hook and raises ``StopResponse`` when the bot should stay silent.

The decision logic mirrors the legacy split pipeline in
order and outcome (the in-scope subset for this bead — the other modes are
downstream):

* router returns ``should_speak=false`` → ``no_reply(router_declined)``;
* router approves but ``confidence < confidence_threshold`` →
  ``no_reply(low_confidence)``;
* the per-session over-talk cap is hit → ``no_reply(rate_limited)``;
* otherwise **speak** — the hook returns normally and the SDK generates the
  reply. The router prompt build / parse / confidence clamp are *reused verbatim*
  from ``johnny.voice_pipeline.reasoning`` so the verdicts replay identically
  (the replay-harness acceptance).

Phase-3 triage (Johnny-trt.16/.17) extends the approved-and-confident leg in
inline-speaking modes with two more actions before the speak fallthrough:
``delegate`` queues an async task through the session
:class:`~johnny.agent.tasks.TaskCoordinator` (the durable ``agent_tasks`` row
exists before any audio — row-before-ack) and speaks the model-authored ack
via ``session.say()`` whose completion owns the turn's terminal; ``status``
speaks the fixed Phase-3 stub the same way. Neither pays an answer-LLM hop.
An ackless delegate verdict is degraded to a plain SPEAK instead
(Johnny-trt.53, instrumented under :data:`ACK_FALLBACK_KEY`) — a real answer
beats the canned :data:`DEFAULT_DELEGATE_ACK`. Task *results* arrive later as
session-scoped speech (the approval-reply precedent), never as turn
terminals, so INV-1 keeps exactly one terminal per turn; until the Phase-5
re-entry queue exists, a task that settles ``failed`` re-enters immediately
as the honest spoken correction (:meth:`RouterGate.report_task_failure` — no
dead promises).

INV-1 ("exactly one terminal per turn") is enforced by the session-scoped
:class:`~johnny.agent.gate.TurnLedger` (spike Johnny-o3z): :meth:`run_turn`
drives the bounded :func:`~johnny.agent.gate.run_gate` harness (timeout +
barge-in cancel; spike Johnny-9k2) through a per-turn
:class:`~johnny.agent.gate.TerminalTracker` that routes into the ledger, then
emits the decision-path terminal itself. The **speak** path emits no terminal
in the hook — its terminal is owned by the reply: :meth:`bind_reply` correlates
the ``generate_reply`` :class:`~livekit.agents.voice.SpeechHandle` (caught by a
session ``speech_created`` listener wired in :meth:`JohnnyAgent.on_enter`) to
the turn and registers a done-callback that emits ``replied`` /
``model_empty_output`` / ``barge_in`` when the reply completes.

Requires the ``agent`` extra (``livekit-agents``) and pulls
``johnny.voice_pipeline.reasoning``; imported only from
:mod:`johnny.agent.session` (the full-stack integration module), never from the
import-safe top-level :mod:`johnny.agent` package.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from livekit.agents.llm import ChatContext, StopResponse
from livekit.agents.llm.chat_context import ChatMessage as LKChatMessage
from livekit.agents.voice import SpeechHandle

from app.providers.base import ChatMessage, LLMProvider
from johnny.agent.approval import ApprovalCoordinator, ApprovalRound
from johnny.agent.complexity import SHADOW_KEY, score_complexity
from johnny.agent.gate import (
    GateAction,
    TerminalTracker,
    TurnLedger,
    run_gate,
)
from johnny.agent.observability import (
    RecordDecision,
    RecordSpoke,
    RecordSuggested,
    RecordTriageTiming,
    SpeechCaptionBuffer,
    SpokenKind,
)
from johnny.agent.task_catalog import TaskCatalogEntry, render_task_catalog
from johnny.agent.tasks import QueuedTask, TaskCoordinator, TaskResult, TaskSpec
from johnny.voice_pipeline import reasoning as _reasoning
from johnny.voice_pipeline.audio_recorder import SpokenAudioRecorder
from johnny.voice_pipeline.reasoning import (
    APPROVAL_REQUIRED_MODE,
    AUTONOMOUS_MODE,
    DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_MODE,
    DEFAULT_RATE_LIMIT_MAX_UTTERANCES,
    DEFAULT_RATE_LIMIT_WINDOW_MS,
    DEFAULT_ROUTER_LLM_TIMEOUT_S,
    DELEGATE_ACTION,
    LISTEN_ONLY_MODE,
    SPEAK_ACTION,
    STATUS_ACTION,
    SUGGEST_ONLY_MODE,
    RouterDecision,
    TaskRequest,
)
from johnny.voice_pipeline.transcript_history import BOT_SPEAKER_LABEL

logger = logging.getLogger(__name__)

# Reuse the legacy router schema + parser verbatim (both private in the pipeline
# module, accessed module-qualified) so the gate produces byte-for-byte
# identical verdicts on the same model output — the "replay harness reproduces
# the same speak/no-speak verdicts" acceptance. A divergent copy would silently
# change behaviour.
ROUTER_DECISION_SCHEMA = _reasoning._ROUTER_SCHEMA


def _default_clock() -> int:
    """Monotonic wall clock in milliseconds for the rate-limit window."""
    return int(time.monotonic() * 1000)


def _extract_spoken_text(handle: SpeechHandle) -> str:
    """Join the assistant text a completed reply produced, for ``AgentSpoke`` (Johnny-d5z).

    The reply's terminal text lives on the ``SpeechHandle.chat_items`` (the same
    items :meth:`RouterGate._on_reply_done` reads for the empty-reply check), so
    this is only called when there is at least one item. Empty ``text_content`` is
    skipped and multiple chunks are space-joined — a best-effort reconstruction of
    what the bot said for the audit row, with no dependency on the answer pipeline
    internals.
    """
    parts: list[str] = []
    for item in handle.chat_items:
        text = (getattr(item, "text_content", None) or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts)


PersistPendingDecision = Callable[[RouterDecision, str], Awaitable[int | None]]
"""Persist the ``pending`` ``agent_decisions`` row for an approval turn, returning
its id (``None`` on a noop sink / persist failure). Injected by Johnny-qzj's wiring
(:func:`johnny.agent.approval_wiring.build_persist_pending_decision`): takes the
parsed :class:`RouterDecision` and the LiveKit ``turn_id``. The returned
``decision_id`` is what the live UI / browser push correlate on and what the
:class:`~johnny.agent.approval.ApprovalRound` carries to the coordinator — so it
must be persisted *before* the turn is parked (the round needs it)."""

SaySpeech = Callable[[str], SpeechHandle]
"""Speak a fixed line out of band via ``AgentSession.say`` (Johnny-trt.17).

Attached by :meth:`JohnnyAgent.on_enter` through :meth:`RouterGate.attach_say`
(the session only exists once the agent is active, so it cannot be a
constructor argument). ``say()``'s ``speech_created`` fires with
``source="say"``, so the ``generate_reply`` FIFO (:meth:`RouterGate.bind_reply`)
never sees these speeches — the gate attaches the turn's terminal done-callback
to the returned :class:`SpeechHandle` directly."""

DEFAULT_DELEGATE_ACK = "Let me check on that — I'll get back to you."
"""The canned ack — kept ONLY as a defensive last resort (Johnny-trt.53).

THE RULE (chosen over speaking this default, documented in
``docs/ROUTING.md`` §2): a ``delegate`` verdict that carries no usable
``ack`` is **degraded to SPEAK** in :meth:`RouterGate.run_turn` — the answer
pipeline produces a real, contextual reply instead of a hollow canned
promise. That degrade runs before the delegate branch, so this constant is
unreachable through the normal turn flow; it survives solely for hand-built
decisions that bypass :meth:`run_turn`, and any occurrence is logged as a
warning (live use of this string was the trt.53 bug)."""

ACK_FALLBACK_KEY = "ack_fallback"
"""``decision.raw`` key marking an ackless delegate verdict the gate degraded
to SPEAK (Johnny-trt.53). Stashed *before* the ``RouterDecisionMade`` emit so
the marker lands inside ``agent_decisions.raw_output`` (the trt.50
``decision.raw`` ride-along pattern — no event field, no migration). The
per-session fallback-ack rate is rows carrying this key over rows whose
``raw_output->>'action'`` is ``'delegate'``; the delegate rate is those rows
over all decision rows."""

CAPABILITY_GAP_KEY = "capability_gap"
"""``decision.raw`` key marking a delegate verdict that targeted an
*unavailable* catalog kind (Johnny-trt.55) — the defense-in-depth backstop:
the model can never act on a capability the session lacks. Stashed before
the decision emit (the same trt.50 ride-along as :data:`ACK_FALLBACK_KEY`)
with ``{from_action, to_action, kind, reason}``, so the decision row records
the capability-gap reason; the turn then speaks the honest decline
deterministically via say() — never the answer pipeline, which could invent
a pretend-check."""


def capability_decline_speech(kind: str, reason: str) -> str:
    """Compose the spoken decline for an unavailable-capability ask (Johnny-trt.55).

    ``reason`` is the catalog entry's ``unavailable_reason`` — spoken-form
    and actionable by contract (it names what is missing and the fix), so it
    is spoken verbatim. The generic tail covers a blank reason defensively.
    """
    spoken = (reason or "").strip()
    if spoken:
        return spoken
    return f"I can't do that in this session — the {kind} capability isn't available right now."

STATUS_STUB_REPLY = "I don't have any tasks in flight right now."
"""The Phase-3 ``status`` verdict stub — there is no delegated-task registry to
query until the Phase-5 real status query lands, so the gate speaks this fixed
line instead of paying an answer-LLM hop."""

TRANSCRIPT_WINDOW_LIMIT = 12
"""Most recent prior conversation entries carried in the decision event's
``input_window.transcript_window`` (Johnny-trt.54). The router prompt itself
keeps the full rolling context; this only bounds what is *persisted* per
``agent_decisions`` row for the timeline / replay, so a long meeting doesn't
grow every row without bound. The ``is_current`` trigger entry is always
appended on top of the cap."""


def delegate_failure_correction(result_text: str) -> str:
    """Compose the honest spoken walk-back for a fast-failed delegated task.

    The Phase-3 no-dead-promises stopgap (Johnny-trt.53): the ack promised
    work, the stub executor (or a Phase-4 crash) settled the row ``failed``,
    and nothing else would re-enter the conversation until the Phase-5 speech
    queue (Johnny-trt.29) — so the gate says so, out loud, immediately.
    ``result_text`` is the row's speech-ready failure phrase
    (:func:`~johnny.agent.tasks.unsupported_kind_text` /
    :func:`~johnny.agent.tasks.executor_error_text`); a blank one gets a
    generic but still honest tail.
    """
    spoken = (result_text or "").strip() or "that task didn't go through."
    return f"Actually — I can't do that yet: {spoken}"


@dataclass(frozen=True, slots=True)
class RouterGateConfig:
    """The router-decision knobs, mirrored from the ``PipelineConfig`` subset.

    Only the fields the router actually reads are carried here — the answer /
    TTS / approval / noise-filter knobs belong to the (downstream) reply and
    mode handlers. Defaults match ``johnny.voice_pipeline.reasoning`` so an
    unconfigured gate behaves like the legacy default session.
    """

    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    mode: str = DEFAULT_MODE
    personality_prompt: str = ""
    instructions: str = ""
    context: str = ""
    calendar_context: str = ""
    calendar_attachments_text: str = ""
    prior_session_context: str = ""
    allowed_replies: tuple[str, ...] = ()
    rate_limit_max_utterances: int = DEFAULT_RATE_LIMIT_MAX_UTTERANCES
    rate_limit_window_ms: int = DEFAULT_RATE_LIMIT_WINDOW_MS
    router_llm_timeout_s: float = DEFAULT_ROUTER_LLM_TIMEOUT_S
    approval_timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS
    # Delegatable task kinds rendered into the router prompt (Johnny-trt.19).
    # Empty = no catalog block at all (the prompt stays byte-identical to the
    # pre-catalog build). The runtime assembly only fills this when a
    # TaskCoordinator is actually wired, so the router is never taught to
    # delegate work the gate would have to stage_error.
    task_catalog: tuple[TaskCatalogEntry, ...] = ()


class RouterGate:
    """Runs the should-speak router decision inside ``on_user_turn_completed``.

    Construct one per session with the admin-active router ``LLMProvider``, the
    session :class:`~johnny.agent.gate.TurnLedger`, and the resolved
    :class:`RouterGateConfig`. :meth:`run_turn` is called from the hook;
    :meth:`bind_reply` is called from the session ``speech_created`` listener.

    ``abandon`` is the cooperative barge-in event raced inside the gate (set by
    the fast-VAD interrupt path, Johnny-k8t) — left ``None`` until that lands.
    ``clock`` is injectable so rate-limit tests can drive the window
    deterministically.

    ``approval`` is the out-of-band :class:`~johnny.agent.approval.ApprovalCoordinator`
    that carries ``approval_required`` rounds off the turn loop (Johnny-z97/qzj);
    ``persist_pending_decision`` persists the ``pending`` decision row the round
    correlates on. Both default ``None`` (the agent replies/declines inline with
    no approval step); the agent worker wires them via
    :func:`johnny.agent.approval_wiring.build_approval_coordinator`. The coordinator
    holds a back-reference to this gate, so it is attached *after* construction via
    :meth:`attach_approval` to resolve the mutual dependency.
    """

    def __init__(
        self,
        router_llm: LLMProvider,
        *,
        config: RouterGateConfig,
        ledger: TurnLedger,
        approval: ApprovalCoordinator | None = None,
        persist_pending_decision: PersistPendingDecision | None = None,
        record_decision: RecordDecision | None = None,
        record_spoke: RecordSpoke | None = None,
        record_suggested: RecordSuggested | None = None,
        record_triage_timing: RecordTriageTiming | None = None,
        reply_audio: SpokenAudioRecorder | None = None,
        tasks: TaskCoordinator | None = None,
        resolve_turn_id: Callable[[str], int] | None = None,
        abandon: asyncio.Event | None = None,
        clock: Callable[[], int] = _default_clock,
    ) -> None:
        self._router_llm = router_llm
        self._config = config
        self._ledger = ledger
        self._approval = approval
        self._persist_pending_decision = persist_pending_decision
        # Observability emit seams (Johnny-d5z), all optional so a smoke/bare gate
        # emits nothing. ``record_decision`` publishes the turn's RouterDecisionMade
        # (non-approval paths); ``record_spoke`` the speak path's AgentSpoke;
        # ``record_suggested`` the suggest-only AgentSuggested. Built by
        # :func:`johnny.agent.observability.build_observability` against the session
        # EventBus + shared TurnIndex.
        self._record_decision = record_decision
        self._record_spoke = record_spoke
        self._record_suggested = record_suggested
        # The triage-stage PipelineTiming emit (Johnny-trt.19): the router LLM
        # runs as a side call (never through the session llm_node), so LiveKit
        # emits no metric for it — the gate publishes its own ``router_llm``
        # timing per decided turn so session_timings shows the triage cost.
        self._record_triage_timing = record_triage_timing
        # The session's reply-audio recorder (Johnny-od1). The gate only does
        # buffer hygiene: reset at every speech bind so stale segments (an
        # approval reply, a say(), an interrupted reply) never leak into the
        # next reply's file, and discard on the non-spoke terminals. The spoke
        # emitter owns the flush-to-WAV.
        self._reply_audio = reply_audio
        # Delegated-task pieces (Johnny-trt.17/.18). ``tasks`` is the session's
        # TaskCoordinator the delegate branch drives (row-before-ack: ``begin``
        # is awaited and the ack is only spoken on a non-None QueuedTask);
        # ``resolve_turn_id`` maps the LiveKit str turn id to the durable int
        # (the shared TurnIndex) so the agent_tasks row correlates with the
        # turn's decision/terminal rows. Both optional: a gate without them
        # terminalizes delegate verdicts ``no_reply(stage_error)`` instead of
        # promising work nothing can record.
        self._tasks = tasks
        self._resolve_turn_id = resolve_turn_id
        # No dead promises (Johnny-trt.53): the gate owns say(), so it owns the
        # honest spoken walk-back when a delegated task fails fast — attach the
        # coordinator's failure-report seam right here so every assembly that
        # pairs a gate with a coordinator (job_session, the test harnesses)
        # gets the correction wiring for free. 1:1 per session, so the attach
        # cannot clobber another consumer.
        if tasks is not None:
            tasks.attach_failure_reporter(self.report_task_failure)
        # The say() seam for delegate acks / status stubs (Johnny-trt.17),
        # attached by JohnnyAgent.on_enter once the session exists.
        self._say: SaySpeech | None = None
        # The most recent say() SpeechHandle (ack / status / correction),
        # kept so the internal-tool teardown runners (Johnny-trt.57) can wait
        # for the farewell ack to finish playing before disconnecting — see
        # :meth:`wait_recent_say_done`.
        self._last_say_handle: SpeechHandle | None = None
        # Caption sentences of the speech playing now (Johnny-trt.58), fed by
        # the assembly's tts_node sink tee via :meth:`note_speech_caption`.
        # When a barge-in cuts a speech, the done-callback takes this buffer
        # as the partial actually delivered so the text is kept (marked
        # interrupted) instead of vanishing. Always constructed — a gate with
        # no caption wiring just sees an empty buffer and keeps the legacy
        # nothing-recorded behaviour.
        self._captions = SpeechCaptionBuffer()
        self._abandon = abandon
        self._clock = clock
        # SpeechHandle ids the approval coordinator owns (it created them via its
        # out-of-band generate_reply, Johnny-z97 §7.3). The shared speech_created
        # listener routes every generate_reply speech through :meth:`bind_reply`,
        # which early-returns for these instead of mis-binding the approval reply
        # to a pending SPEAK turn.
        self._approval_reply_handles: set[str] = set()
        # Timestamps (ms) of utterances the bot actually spoke, for the
        # per-session over-talk cap. Pruned in place by :meth:`_is_rate_limited`.
        self._recent_utterance_times: list[int] = []
        # Turn ids that decided SPEAK and are awaiting their reply's terminal.
        # The session ``speech_created`` listener pops the oldest to bind the
        # reply SpeechHandle's done-callback (the reply→turn correlation).
        self._pending_speak_turns: deque[str] = deque()
        # Strong refs to in-flight reply-done emit tasks so they aren't GC'd
        # mid-flight (and to avoid "task exception never retrieved" warnings).
        self._reply_tasks: set[asyncio.Task[None]] = set()
        # The reply currently being spoken: ``(turn_id, SpeechHandle)``, set when
        # a reply binds and cleared when it completes. The barge-in classifier
        # (Johnny-k8t) reads this to capture the turn id of the reply it might
        # interrupt — the LiveKit-turn-keyed analogue of the legacy
        # ``_response_generation`` counter.
        self._active_reply: tuple[str, SpeechHandle] | None = None
        # Turn ids whose allowed-reply coercion found no match (Johnny-5ag): the
        # llm_node yields nothing, so the reply completes empty, and
        # :meth:`_on_reply_done` maps that empty reply to
        # ``no_reply(no_allowed_reply_match)`` instead of ``model_empty_output``.
        # Flagged via :meth:`note_coercion_no_match` (keyed off the active reply's
        # turn id) and consumed when that reply's done-callback fires.
        self._coercion_no_match_turns: set[str] = set()
        # Char count of the most recent router prompt (Johnny-trt.55): set by
        # _decide right after the prompt build, read by run_turn's triage
        # timing emit so session_timings shows catalog growth (the render-cap
        # enforcement metric). Turns run serially through the blocking hook,
        # so a single slot cannot race.
        self._last_prompt_chars: int | None = None

    # ------------------------------------------------------------------ #
    # The blocking gate                                                  #
    # ------------------------------------------------------------------ #

    async def run_turn(self, turn_ctx: ChatContext, new_message: LKChatMessage) -> None:
        """Run the should-speak gate for one user turn.

        Returns normally to **speak** (the SDK then generates the reply); raises
        :class:`~livekit.agents.llm.StopResponse` to stay silent. Every silent
        exit leaves exactly one terminal in the ledger (INV-1) — except
        ``listen_only``, which (like the legacy early return) is never opened, so
        it accounts for no turn:

        * ``listen_only`` → silent, **no terminal** (router skipped, turn never opened);
        * gate timeout / barge-in / router error → emitted by :func:`run_gate`;
        * ``should_speak=false`` → ``no_reply(router_declined)``;
        * ``confidence < threshold`` → ``no_reply(low_confidence)``;
        * ``suggest_only`` (after the router approves) → ``no_reply(suggest_only)``;
        * rate-limited → ``no_reply(rate_limited)``.

        Phase-3 triage (Johnny-trt.17): an approved-and-confident turn in an
        inline-speaking mode branches on ``decision.action`` before the SPEAK
        fallthrough. ``delegate`` queues the async task (row-before-ack via
        :meth:`TaskCoordinator.begin`) and speaks the model-authored ack via
        ``say()`` — no answer-LLM hop — with the ack :class:`SpeechHandle`'s
        completion owning the turn's terminal (``replied`` /
        ``no_reply(barge_in)``); a missing coordinator / failed persist /
        unattached ``say`` speaks nothing and terminalizes
        ``no_reply(stage_error)``. A delegate verdict with **no usable ack**
        never reaches that branch: :meth:`_degrade_ackless_delegate`
        (Johnny-trt.53) rewrites it to a plain SPEAK — marked in
        ``decision.raw`` under :data:`ACK_FALLBACK_KEY` before the decision
        emit — because a real answer beats a hollow canned promise. ``status``
        speaks the fixed :data:`STATUS_STUB_REPLY` through the same machinery
        (the real status query is Phase 5). Both raise ``StopResponse`` so the
        SDK generates no reply; both run *after* the mode branches above, so
        ``suggest_only`` / ``approval_required`` / ``listen_only`` sessions and
        the rate limiter treat a delegate/status verdict exactly like a speak
        verdict (unchanged behaviour).

        Shadow complexity pre-score (Johnny-trt.50): before awaiting the
        router LLM the gate runs the pure-python heuristic scorer
        (:func:`~johnny.agent.complexity.score_complexity`) over the latest
        transcript and stashes its 4-key verdict in ``decision.raw`` so the
        ``RouterDecisionMade`` emit persists it inside
        ``agent_decisions.raw_output``. Observability only — no branch reads
        it, and a scorer failure is logged and ignored.

        In ``approval_required`` mode an approved-to-speak turn is **parked** for
        out-of-band human approval (Johnny-z97): the gate hands it to the
        :class:`~johnny.agent.approval.ApprovalCoordinator` and raises
        ``StopResponse`` immediately — never blocking the ~15 s human wait on the
        await-chained turn loop. The coordinator owns that turn's single terminal
        (``replied`` on approve, ``no_reply(approval_rejected)`` on reject/timeout)
        and its ``ApprovalPending`` / ``ApprovalResolved`` events, so the gate
        emits **no** terminal on that path and does **not** record a SPEAK turn.

        The **speak** path emits no terminal here; it records the turn id for
        :meth:`bind_reply` to terminalize on reply completion.
        """
        turn_id = new_message.id
        if self._config.mode == LISTEN_ONLY_MODE:
            # Listen-only never speaks and skips the router entirely — parity with
            # the legacy split pipeline early return. The
            # turn is deliberately NOT opened in the ledger: there is no turn to
            # account for, so INV-1 emits no terminal (exactly like the noise-gate /
            # skip_reply paths documented on :meth:`TurnLedger.open`). Stay silent.
            raise StopResponse()
        tracker = self._ledger.gate_tracker(turn_id)  # opens the turn (INV-1)
        # Shadow complexity pre-score (Johnny-trt.50): pure stdlib, computed
        # synchronously BEFORE the triage-LLM await — where a future behavioral
        # pre-scorer would run — and outside the triage span below so the
        # router_llm timing row stays comparable to the pre-shadow baseline.
        # Observability only: nothing branches on it.
        shadow = self._complexity_shadow(new_message, turn_id)
        self._last_prompt_chars = None  # set by _decide once the prompt is built
        triage_started = time.time()
        action, decision = await run_gate(
            lambda: self._decide(turn_ctx, new_message),
            tracker=tracker,
            timeout_s=self._config.router_llm_timeout_s,
            abandon=self._abandon,
        )
        triage_ended = time.time()

        if action is GateAction.STAY_SILENT:
            # run_gate already emitted the terminal (stage_error / barge_in).
            raise StopResponse()
        if decision is None:
            # Defensive: SPEAK always carries a decision. Account for the turn
            # rather than letting it fall through to the close() sweep.
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="stage_error",
                detail="router returned no decision",
            )
            raise StopResponse()

        # Shadow-verdict persistence (Johnny-trt.50): ride the decision's raw
        # payload so the existing RouterDecisionMade emit below lands it inside
        # ``agent_decisions.raw_output`` next to the router's own ``action`` —
        # no event field, no migration, and the replay diff never reads raw, so
        # parity is untouched by construction. Nothing downstream reads the key
        # back; it exists for the offline heuristic-vs-LLM-action dataset.
        if shadow is not None:
            decision.raw[SHADOW_KEY] = shadow

        # Capability backstop (Johnny-trt.55) FIRST, then the ack rule: a
        # delegate verdict targeting an unavailable catalog kind is degraded
        # to the deterministic spoken decline — before the ackless degrade,
        # because an unavailable ask must never ride the answer pipeline
        # (which could invent a pretend-check). Both helpers stash their raw
        # markers before the emits below, so the timing row carries the
        # *effective* action and the decision row records the gap/fallback.
        decision = self._degrade_unavailable_delegate(decision, turn_id)
        decision = self._degrade_ackless_delegate(decision, turn_id)

        # Triage-stage timing (Johnny-trt.19): one ``router_llm`` row per turn
        # the router actually decided (timed-out / barged / errored gates leave
        # only their terminal — the stage never completed). Spans run_gate, so
        # it is the prompt build + LLM call + parse + harness overhead — the
        # wall cost every verdict pays before anything else can happen. Emitted
        # for every mode (a timing row, not a decision row, so the
        # approval-mode double-write concern below does not apply).
        # ``prompt_chars`` (Johnny-trt.55) is the router prompt size _decide
        # measured — the catalog-growth metric that keeps the render cap
        # enforceable.
        if self._record_triage_timing is not None:
            await self._record_triage_timing(
                turn_id,
                triage_started,
                triage_ended,
                decision.action,
                prompt_chars=self._last_prompt_chars,
            )

        # Observability parity (Johnny-d5z): publish this turn's RouterDecisionMade
        # so the subscriber writes its agent_decisions row (outcome derived from the
        # mode in input_window) and the turn's later TurnTerminal stamps that same
        # row by the int turn id. Emitted once, before the branch — exactly like the
        # legacy ``_respond_to_transcript_inner`` published the decision event right
        # after the router returned, then branched. ``approval_required`` persists
        # its own pending row via ``persist_pending_decision`` (Johnny-qzj); emitting
        # here too would double-write, so that mode is skipped. The transcript
        # window (Johnny-trt.54) rides the event into ``input_window`` so the
        # decision row records what the turn heard — the timeline's "Heard you"
        # step and the per-session replay reconstruct from it.
        if self._record_decision is not None and self._config.mode != APPROVAL_REQUIRED_MODE:
            await self._record_decision(
                decision,
                turn_id,
                transcript_window=self._transcript_window(turn_ctx, new_message),
            )

        if not decision.should_speak:
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="router_declined",
                detail=decision.reason,
            )
            raise StopResponse()

        if decision.confidence < self._config.confidence_threshold:
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="low_confidence",
                detail=(
                    f"confidence {decision.confidence:.2f} < threshold "
                    f"{self._config.confidence_threshold:.2f}"
                ),
            )
            raise StopResponse()

        if self._config.mode == SUGGEST_ONLY_MODE:
            # suggest_only: the router ran (so the UI sees a suggestion) and
            # approved, but the bot speaks nothing. Mirrors the legacy order
            # (``_respond_to_transcript_inner`` checks suggest_only after
            # should-speak/confidence, before rate-limit/approval). The terminal
            # is owned here; the AgentSuggested event that carries the suggested
            # reply to the UI is event/observability parity (Johnny-d5z).
            await self._handle_suggest_only(tracker, decision, turn_id)
            raise StopResponse()

        if self._is_rate_limited():
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="rate_limited",
                detail=(
                    f"rate limit: {self._config.rate_limit_max_utterances} "
                    f"per {self._config.rate_limit_window_ms}ms"
                ),
            )
            raise StopResponse()

        if self._config.mode == APPROVAL_REQUIRED_MODE:
            # approval_required: hold the reply for out-of-band human approval.
            # The coordinator parks the turn (no terminal yet) and owns the rest;
            # the gate raises StopResponse so the SDK generates nothing inline —
            # the approved reply is spoken out of band via generate_reply. The
            # turn is deliberately NOT pushed onto _pending_speak_turns (its reply
            # is coordinator-owned, not a gated-SPEAK reply; Johnny-z97 §7.2).
            # A delegate/status action parks like any approved decision — the
            # Phase-3 triage branches below are inline-speaking-mode only.
            await self._begin_approval(tracker, turn_id, decision)
            raise StopResponse()

        capability_gap = decision.raw.get(CAPABILITY_GAP_KEY)
        if isinstance(capability_gap, dict):
            # The trt.55 backstop's speech leg: the delegate verdict targeted
            # an unavailable kind, so speak the honest decline (the catalog
            # entry's spoken-form reason) through the say() machinery — no
            # answer-LLM hop that could pretend-check, no task row at all.
            await self._handle_capability_decline(tracker, turn_id, capability_gap)
            raise StopResponse()

        if decision.action == DELEGATE_ACTION and decision.task_request is not None:
            # delegate (Johnny-trt.17): queue the async task and speak the short
            # ack — the felt latency of the turn is the triage call plus say()'s
            # first audio, with no answer-LLM hop. The trt.16 parser guarantees
            # task_request is set for a delegate action; the None guard means a
            # hand-built decision that violates the pair degrades to SPEAK below
            # (the parser's own malformed-task degrade) rather than crashing.
            await self._begin_delegated_task(tracker, turn_id, decision.task_request)
            raise StopResponse()

        if decision.action == STATUS_ACTION:
            # status (Johnny-trt.17): no delegated-task registry to query until
            # Phase 5, so speak the fixed stub through the same say() machinery
            # (deterministic, no answer-LLM hop).
            await self._handle_status(tracker, turn_id)
            raise StopResponse()

        # SPEAK: no terminal here — the reply-completion path owns it. Record
        # the turn so the next generate_reply SpeechHandle binds to it.
        self._pending_speak_turns.append(turn_id)
        logger.info(
            "agent.router.gate: turn=%s SPEAK confidence=%.2f reason=%r",
            turn_id,
            decision.confidence,
            decision.reason,
        )

    def _complexity_shadow(
        self, new_message: LKChatMessage, turn_id: str
    ) -> dict[str, object] | None:
        """Compute the turn's shadow complexity verdict (Johnny-trt.50).

        Pure-python heuristic over the latest transcript + the session's task
        catalog (the delegate-prior dimension — empty catalog on
        non-delegation runtimes zeroes it, mirroring the prompt's capability
        gating). Returns the 4-key payload :meth:`run_turn` stashes under
        :data:`~johnny.agent.complexity.SHADOW_KEY` in ``decision.raw``, or
        ``None`` if scoring failed — shadow mode must never break a turn, so
        every exception is swallowed into a log line. The debug line below is
        the bead's "one debug log line" and the only runtime trace besides
        the persisted JSON.
        """
        try:
            verdict = score_complexity(
                (new_message.text_content or "").strip(),
                catalog=self._config.task_catalog,
            )
        except Exception:
            logger.exception(
                "agent.router.gate: complexity shadow scoring failed for turn=%s", turn_id
            )
            return None
        logger.debug(
            "agent.router.gate: turn=%s complexity-shadow tier=%s score=%.3f "
            "confidence=%.2f ambiguous=%s signals=%s",
            turn_id,
            verdict.tier,
            verdict.score,
            verdict.confidence,
            verdict.ambiguous,
            list(verdict.signals[:3]),
        )
        return verdict.shadow_payload()

    def _degrade_ackless_delegate(
        self, decision: RouterDecision, turn_id: str
    ) -> RouterDecision:
        """Rewrite a delegate verdict with no usable ack to a plain SPEAK (Johnny-trt.53).

        THE RULE, chosen over speaking :data:`DEFAULT_DELEGATE_ACK` and
        documented in ``docs/ROUTING.md`` §2: when the router picks
        ``delegate`` but skips the required model-authored ack, the turn falls
        through to the answer pipeline — a real, contextual reply beats a
        hollow canned promise (and in Phase 3 the task could only fail fast in
        the stub executor anyway). Verdicts with a non-blank ack pass through
        untouched.

        Instrumented both ways the bead demands: the
        :data:`ACK_FALLBACK_KEY` marker is stashed in ``decision.raw``
        *before* :meth:`run_turn`'s decision emit (so it persists inside
        ``agent_decisions.raw_output``, where the per-session fallback-ack
        rate is derived from), and a warning names the dropped kind. The
        returned decision keeps ``should_speak=True`` (delegate implied it)
        and clears ``task_request`` so the action/task pair stays consistent.
        """
        task_request = decision.task_request
        if decision.action != DELEGATE_ACTION or task_request is None:
            return decision
        if task_request.ack.strip():
            return decision
        decision.raw[ACK_FALLBACK_KEY] = {
            "from_action": DELEGATE_ACTION,
            "to_action": SPEAK_ACTION,
            "kind": task_request.kind,
            "reason": "delegate verdict carried no ack",
        }
        logger.warning(
            "agent.router.gate: turn=%s delegate verdict for kind=%r carried no "
            "ack — degrading to SPEAK (Johnny-trt.53: a real answer beats a "
            "hollow promise)",
            turn_id,
            task_request.kind,
        )
        return replace(decision, action=SPEAK_ACTION, task_request=None)

    def _degrade_unavailable_delegate(
        self, decision: RouterDecision, turn_id: str
    ) -> RouterDecision:
        """Rewrite a delegate verdict targeting an unavailable kind to the decline (Johnny-trt.55).

        Defense in depth behind the prompt's unavailable block: whatever the
        model says, a capability this session lacks can never be acted on.
        The marker is stashed in ``decision.raw`` *before* :meth:`run_turn`'s
        emits (the trt.50 ride-along), so the decision row records the
        capability-gap reason; the action is rewritten to ``status`` — the
        effective shape of the turn (deterministic say()-path speech, no
        answer hop, no task row) — and ``task_request`` is cleared so nothing
        downstream can queue it. Kinds absent from the catalog entirely are
        left alone: they ride the normal delegate path into the executor's
        fail-fast legs (the trt.57 hallucinated-kind stance).
        """
        task_request = decision.task_request
        if decision.action != DELEGATE_ACTION or task_request is None:
            return decision
        entry = next(
            (e for e in self._config.task_catalog if e.kind == task_request.kind), None
        )
        if entry is None or entry.available:
            return decision
        decision.raw[CAPABILITY_GAP_KEY] = {
            "from_action": DELEGATE_ACTION,
            "to_action": STATUS_ACTION,
            "kind": task_request.kind,
            "reason": entry.unavailable_reason,
        }
        logger.warning(
            "agent.router.gate: turn=%s delegate verdict targets UNAVAILABLE "
            "kind=%r — degrading to the spoken decline (Johnny-trt.55: %s)",
            turn_id,
            task_request.kind,
            entry.unavailable_reason or "no reason recorded",
        )
        return replace(decision, action=STATUS_ACTION, task_request=None)

    async def _handle_capability_decline(
        self, tracker: TerminalTracker, turn_id: str, gap: dict[str, object]
    ) -> None:
        """Speak the honest unavailable-capability decline; its completion owns the terminal.

        The say()-path leg of the trt.55 backstop, shaped like
        :meth:`_handle_status`: deterministic text
        (:func:`capability_decline_speech` over the catalog's spoken-form
        reason — naming what is missing and the fix), spoken via
        :meth:`_say_with_terminal` with ``kind="status"`` (a self-state
        report, the closest trt.54 speech kind). No coordinator involvement —
        nothing is queued, so there is no row and no promise. Without say()
        the turn terminalizes ``no_reply(stage_error)`` like the other
        say()-path verdicts.
        """
        kind = str(gap.get("kind", ""))
        if self._say is None:
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="stage_error",
                detail=(
                    f"capability decline for kind={kind!r} but say() is not "
                    "attached — cannot speak"
                ),
            )
            return
        text = capability_decline_speech(kind, str(gap.get("reason", "")))
        logger.info(
            "agent.router.gate: turn=%s DECLINE unavailable kind=%s reason=%r",
            turn_id,
            kind,
            text,
        )
        await self._say_with_terminal(
            tracker,
            turn_id,
            text,
            kind="status",
            replied_detail=f"declined unavailable capability {kind!r}; spoke the reason",
            interrupted_detail=(
                f"capability decline for {kind!r} interrupted before completion"
            ),
        )

    async def _begin_approval(
        self, tracker: TerminalTracker, turn_id: str, decision: RouterDecision
    ) -> None:
        """Park ``turn_id`` for out-of-band approval, or terminalize on misconfig.

        Happy path: persist the ``pending`` decision row, build the
        :class:`~johnny.agent.approval.ApprovalRound`, and hand it to the
        coordinator's non-blocking :meth:`~johnny.agent.approval.ApprovalCoordinator.begin`
        (which parks the turn + spawns the resolver). The coordinator then owns the
        single final terminal (via ``ledger.resolve``) and the
        ``ApprovalPending`` / ``ApprovalResolved`` events — this method emits
        **no** terminal on the happy path.

        Two misconfigurations terminalize the still-*open* turn directly (legacy
        parity: ``_handle_approval_required`` rejects when it has no usable
        decision id), so it is not left for the :meth:`~johnny.agent.gate.TurnLedger.close`
        sweep:

        * no coordinator wired though the mode is ``approval_required``;
        * persistence returned no id (noop decision sink / persist failure).

        The configurable ``approval_timeout_seconds`` (legacy parity, floored at
        0.1 s) is carried on the round so the injected approval source enforces it.
        """
        if self._approval is None:
            logger.error(
                "approval_required mode but no ApprovalCoordinator wired for turn=%s — rejecting",
                turn_id,
            )
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="approval_rejected",
                detail="approval_required mode but no approval coordinator configured",
            )
            return

        decision_id: int | None = None
        if self._persist_pending_decision is not None:
            decision_id = await self._persist_pending_decision(decision, turn_id)
        if decision_id is None:
            logger.warning(
                "approval_required: no decision id for turn=%s — skipping approval "
                "round, rejecting",
                turn_id,
            )
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="approval_rejected",
                detail="approval gate misconfigured (no decision id)",
            )
            return

        approval_round = ApprovalRound(
            turn_id=turn_id,
            decision_id=decision_id,
            suggested_reply=(decision.suggested_reply or "").strip(),
            timeout_s=max(0.1, float(self._config.approval_timeout_seconds)),
            reason=decision.reason,
            reply_type=decision.reply_type,
        )
        if self._approval.begin(approval_round) is None:
            # park failed — the turn was already parked/terminal (a re-entrant
            # begin for the same turn id). Its existing owner settles it; emitting
            # here would clobber the parked marker or double-terminalize.
            logger.error(
                "approval_required: could not park turn=%s (already accounted) — skipping",
                turn_id,
            )

    async def _handle_suggest_only(
        self, tracker: TerminalTracker, decision: RouterDecision, turn_id: str
    ) -> None:
        """Terminalize a suggest_only turn (Johnny-5ag) — suggestion, no speech.

        Port of the legacy split pipeline's terminal: the router
        approved, so a suggestion exists (``decision.suggested_reply``), but the
        bot speaks nothing into the meeting — from the operator's chat the turn is
        a deliberate ``no_reply(suggest_only)``. The terminal's ``outcome`` maps to
        ``suggested`` (so the decision row reads ``suggested``, not ``suppressed``);
        the :class:`AgentSuggested` event that surfaces the suggestion to the UI is
        published via the injected ``record_suggested`` seam (Johnny-d5z). The
        ``RouterDecisionMade`` for this turn was already emitted in :meth:`run_turn`.
        """
        suggested = (decision.suggested_reply or "").strip()
        await tracker.emit(
            terminal_state="no_reply",
            no_reply_reason="suggest_only",
            detail=f"suggest-only mode: nothing spoken (suggested={suggested!r})",
        )
        if self._record_suggested is not None:
            await self._record_suggested(decision, turn_id)

    # ------------------------------------------------------------------ #
    # Phase-3 triage actions: delegate / status (Johnny-trt.17)           #
    # ------------------------------------------------------------------ #

    async def _begin_delegated_task(
        self, tracker: TerminalTracker, turn_id: str, task_request: TaskRequest
    ) -> None:
        """Queue the delegated task, then speak the ack whose completion owns the terminal.

        The row-before-ack ordering (Johnny-trt.18) is the contract: the durable
        ``agent_tasks`` row exists when :meth:`TaskCoordinator.begin` returns, so
        the ack is only ever spoken for work that is actually recorded. Every
        failure leg speaks **nothing** and terminalizes the still-open turn
        ``no_reply(stage_error)``:

        * no coordinator wired (non-delegation runtime, missing DB factory);
        * ``say()`` not attached (the session never reached ``on_enter``) —
          checked *before* ``begin`` because an unspeakable ack must queue
          nothing (the trt.18 "unspeakable ack ⇒ no queue" rule);
        * ``begin`` returned ``None`` (persist failed / produced no id).

        On success the router's own ack phrase is spoken via
        :meth:`_say_with_terminal` — guaranteed non-blank by :meth:`run_turn`'s
        ackless-delegate degrade (Johnny-trt.53); :data:`DEFAULT_DELEGATE_ACK`
        survives only as an instrumented defensive last resort for hand-built
        decisions that bypass the degrade. The task resolver runs off the turn
        loop; a fast ``failed`` settle re-enters as the spoken correction
        (:meth:`report_task_failure`) and an eventual real result is
        session-scoped speech later (the approval-reply precedent) — never this
        turn's terminal, so INV-1 stays exactly one terminal per turn.
        """
        kind = task_request.kind
        if self._tasks is None:
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="stage_error",
                detail=f"delegate verdict for kind={kind!r} but no task coordinator wired",
            )
            return
        if self._say is None:
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="stage_error",
                detail=(
                    f"delegate verdict for kind={kind!r} but say() is not attached — "
                    "cannot speak the ack, so nothing was queued"
                ),
            )
            return

        ack = task_request.ack.strip()
        if not ack:
            # Unreachable via run_turn (the trt.53 degrade rewrites ackless
            # delegates to SPEAK first) — a hand-built decision bypassed it.
            # Instrumented because every canned-ack utterance is the exact
            # robotic-deflection bug trt.53 fixed.
            logger.warning(
                "agent.router.gate: turn=%s delegate kind=%r reached the branch "
                "with no ack — speaking DEFAULT_DELEGATE_ACK (defensive last "
                "resort; the run_turn degrade should have caught this)",
                turn_id,
                kind,
            )
            ack = DEFAULT_DELEGATE_ACK
        spec = TaskSpec(
            kind=kind,
            args=dict(task_request.args),
            ack_text=ack,
            turn_id=(self._resolve_turn_id(turn_id) if self._resolve_turn_id is not None else None),
            # The non-approval decision row is written asynchronously by the
            # status subscriber, so no synchronous id exists to carry here.
            decision_id=None,
        )
        queued = await self._tasks.begin(spec)
        if queued is None:
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="stage_error",
                detail=f"task persist failed for kind={kind!r} — ack not spoken",
            )
            return

        logger.info(
            "agent.router.gate: turn=%s DELEGATE kind=%s task_id=%s ack=%r",
            turn_id,
            kind,
            queued.task_id,
            ack,
        )
        await self._say_with_terminal(
            tracker,
            turn_id,
            ack,
            kind="ack",
            replied_detail=f"delegated {kind} task #{queued.task_id}; spoke ack",
            interrupted_detail=(
                f"delegate ack interrupted before completion "
                f"(task #{queued.task_id} {kind} continues)"
            ),
        )

    async def _handle_status(self, tracker: TerminalTracker, turn_id: str) -> None:
        """Speak the Phase-3 status stub; its completion owns the turn's terminal.

        No coordinator is needed — with no delegated-task registry to query yet,
        :data:`STATUS_STUB_REPLY` is the honest answer (Phase 5 replaces this
        with the real per-session task lookup). Only ``say()`` is required;
        without it the turn terminalizes ``no_reply(stage_error)`` like the
        delegate failure legs.
        """
        if self._say is None:
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="stage_error",
                detail="status verdict but say() is not attached — cannot speak",
            )
            return
        logger.info("agent.router.gate: turn=%s STATUS (stub reply)", turn_id)
        await self._say_with_terminal(
            tracker,
            turn_id,
            STATUS_STUB_REPLY,
            kind="status",
            replied_detail="status stub spoken (no delegated-task registry until Phase 5)",
            interrupted_detail="status reply interrupted before completion",
        )

    async def report_task_failure(self, queued: QueuedTask, result: TaskResult) -> None:
        """Speak the honest correction for a delegated task that settled ``failed``.

        The Phase-3 no-dead-promises stopgap (Johnny-trt.53), attached to the
        session :class:`TaskCoordinator` at construction and invoked by its
        resolver *after* the ``agent_tasks`` row settled — so the walk-back
        only ever describes durable state. Session-scoped speech per the
        approval-reply precedent: the delegating turn's terminal (the ack)
        already settled INV-1, so this speech owns **no** terminal and binds
        to no turn — ``say()``'s ``speech_created`` fires with
        ``source="say"``, so :meth:`bind_reply` never sees it either.

        It IS recorded (Johnny-trt.54): a done-callback on the say handle
        publishes an ``AgentSpoke(kind="correction", turn_id=None)`` once the
        speech completes uninterrupted, so the walk-back lands in
        ``agent_utterances`` and the chat history exactly as spoken — while
        the ``turn_id=None`` / ``kind`` pair tells the subscriber to stamp
        **no** decision row's ``final_text`` (the delegating turn's canonical
        text stays its ack). An interrupted correction keeps its caption
        partial the same way (Johnny-trt.58, see :meth:`_on_correction_done`).
        Replaced wholesale by the Phase-5 re-entry queue (Johnny-trt.29).

        Never raises into the resolver: no ``say()`` (session never entered /
        already torn down) or a raising ``say()`` (session draining) is
        logged and swallowed — the durable row already tells the truth.
        """
        say = self._say
        if say is None:
            logger.warning(
                "agent.router.gate: task #%s (%s) failed but say() is not "
                "attached — correction not spoken",
                queued.task_id,
                queued.spec.kind,
            )
            return
        text = delegate_failure_correction(result.result_text)
        # No pre-say buffer discard here (unlike _say_with_terminal): the
        # resolver fires while the delegating turn's ack may still be playing,
        # and ``say()`` QUEUES the correction behind it — an eager discard
        # would eat the ack's in-flight segments before its own completion
        # flush. The ack flushes its buffer at done; the correction's segments
        # accumulate after and are flushed by _on_correction_done.
        try:
            handle = say(text)
        except Exception:
            logger.exception(
                "agent.router.gate: say() failed for task #%s (%s) correction — "
                "nothing spoken",
                queued.task_id,
                queued.spec.kind,
            )
            return
        # Corrections count as "the bot is still talking" for the internal
        # teardown wait (Johnny-trt.57) just like acks do.
        self._last_say_handle = handle

        def _on_done(done_handle: SpeechHandle) -> None:
            task = asyncio.ensure_future(self._on_correction_done(done_handle, text))
            self._reply_tasks.add(task)
            task.add_done_callback(self._reply_tasks.discard)

        handle.add_done_callback(_on_done)
        logger.info(
            "agent.router.gate: task #%s (%s) failed fast — spoke correction %r",
            queued.task_id,
            queued.spec.kind,
            text,
        )

    async def _on_correction_done(self, handle: SpeechHandle, text: str) -> None:
        """Record a completed failed-task correction into history (Johnny-trt.54).

        The unbound-speech analogue of :meth:`_on_say_done`: no turn, no
        terminal, no over-talk accounting — just the ``AgentSpoke`` that makes
        the walk-back visible in ``agent_utterances`` and the chat. An
        interrupted correction that streamed captions keeps its partial
        (Johnny-trt.58): ``AgentSpoke(kind="correction", interrupted=True,
        turn_id=None)`` — still stamping no decision row; cut before the first
        flush → audio discarded, nothing recorded (legacy).
        """
        partial = self._captions.take()
        if handle.interrupted:
            if partial and self._record_spoke is not None:
                logger.info(
                    "agent.router.gate: correction interrupted — partial kept %r",
                    partial,
                )
                await self._record_spoke(
                    partial, turn_id=None, kind="correction", interrupted=True
                )
                return
            if self._reply_audio is not None:
                self._reply_audio.discard_reply()
            logger.info(
                "agent.router.gate: correction interrupted before completion "
                "with no caption flushed — not recorded"
            )
            return
        if self._record_spoke is not None:
            await self._record_spoke(text, turn_id=None, kind="correction")

    async def _say_with_terminal(
        self,
        tracker: TerminalTracker,
        turn_id: str,
        text: str,
        *,
        kind: SpokenKind,
        replied_detail: str,
        interrupted_detail: str,
    ) -> None:
        """``say(text)`` and attach the turn's terminal to the speech's completion.

        ``say()``'s ``speech_created`` fires with ``source="say"``, so the
        ``generate_reply`` FIFO (:meth:`bind_reply`) never sees it — the
        done-callback is attached to the returned :class:`SpeechHandle`
        directly, mirroring the reply path's :meth:`_on_reply_done` task
        pattern (strong refs in ``_reply_tasks``). A ``say()`` that raises
        (session draining / no activity) terminalizes the still-open turn
        ``no_reply(stage_error)`` so it is never left for the close sweep.
        ``kind`` labels the speech path on the AgentSpoke (``"ack"`` /
        ``"status"``, Johnny-trt.54).
        """
        say = self._say
        if say is None:  # defensive: both callers check before invoking
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="stage_error",
                detail="say() is not attached — cannot speak",
            )
            return
        # Buffer hygiene, mirroring bind_reply (Johnny-od1): a new speech is
        # starting, so segments left over from a previous speech must not leak
        # into this ack's flushed WAV when the spoke emitter takes it — nor
        # stale captions into its interrupted partial (Johnny-trt.58).
        if self._reply_audio is not None:
            self._reply_audio.discard_reply()
        self._captions.take()
        try:
            handle = say(text)
        except Exception as exc:
            logger.exception(
                "agent.router.gate: say() failed for turn=%s — nothing spoken", turn_id
            )
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="stage_error",
                detail=f"say() failed: {type(exc).__name__}: {exc}",
            )
            return
        # Stashed synchronously, before the loop can run anything else — a
        # delegate turn's task resolver (queued in begin(), scheduled but not
        # yet started) therefore always finds the farewell ack here when it
        # calls wait_recent_say_done() as its first act (Johnny-trt.57).
        self._last_say_handle = handle

        def _on_done(done_handle: SpeechHandle) -> None:
            task = asyncio.ensure_future(
                self._on_say_done(
                    turn_id,
                    done_handle,
                    text,
                    kind=kind,
                    replied_detail=replied_detail,
                    interrupted_detail=interrupted_detail,
                )
            )
            self._reply_tasks.add(task)
            task.add_done_callback(self._reply_tasks.discard)

        handle.add_done_callback(_on_done)

    async def _on_say_done(
        self,
        turn_id: str,
        handle: SpeechHandle,
        text: str,
        *,
        kind: SpokenKind,
        replied_detail: str,
        interrupted_detail: str,
    ) -> None:
        """Emit a say-spoken turn's single terminal once the speech completes.

        The say-path analogue of :meth:`_on_reply_done`: ``interrupted`` →
        ``no_reply(barge_in)``; otherwise ``replied`` (counting toward the
        over-talk cap) followed by the ``AgentSpoke`` carrying the exact
        spoken text plus the turn id and speech kind (INV-2, Johnny-trt.54 —
        the subscriber stamps this exact turn's ``final_text``), in the
        terminal-before-spoke wire order the UI relies on. An interrupted
        ack/status that already streamed captions keeps its partial exactly
        like the reply path (Johnny-trt.58): terminal unchanged, then
        ``AgentSpoke(interrupted=True)`` with the caption text flushed by cut
        time and the buffered audio left for the emitter's flush; cut before
        the first flush → audio discarded, nothing recorded (legacy). No
        empty-output branch — the text was supplied, not model-generated.
        First-wins via the ledger, so a duplicate done-callback can never
        double-emit.
        """
        partial = self._captions.take()
        if handle.interrupted:
            if partial and self._record_spoke is not None:
                if await self._ledger.emit(
                    turn_id,
                    terminal_state="no_reply",
                    no_reply_reason="barge_in",
                    detail=f"{interrupted_detail} (partial kept)",
                ):
                    await self._record_spoke(
                        partial, turn_id=turn_id, kind=kind, interrupted=True
                    )
                return
            if self._reply_audio is not None:
                self._reply_audio.discard_reply()
            await self._ledger.emit(
                turn_id,
                terminal_state="no_reply",
                no_reply_reason="barge_in",
                detail=interrupted_detail,
            )
            return
        if not await self._ledger.emit(turn_id, terminal_state="replied", detail=replied_detail):
            # A duplicate done-callback lost the first-wins race — the winner
            # already counted the utterance and published the AgentSpoke.
            return
        self._recent_utterance_times.append(self._clock())
        if self._record_spoke is not None:
            await self._record_spoke(text, turn_id=turn_id, kind=kind)

    async def _decide(self, turn_ctx: ChatContext, new_message: LKChatMessage) -> RouterDecision:
        """Call the router LLM and parse its structured decision.

        Passed to :func:`run_gate` as the bounded router call, so a hang /
        cancellation / provider error is handled there (→ ``stage_error`` /
        ``barge_in``) — this method just builds the prompt, requests the
        decision schema, and reuses the legacy parser for verdict parity.
        """
        messages = self._router_messages(turn_ctx, new_message)
        # Router prompt size (Johnny-trt.55): the catalog-growth metric the
        # triage timing row persists (details.prompt_chars) — measured here so
        # it reflects exactly what was sent, render caps included.
        self._last_prompt_chars = sum(len(message.content or "") for message in messages)
        response = await self._router_llm.chat(messages, response_format=ROUTER_DECISION_SCHEMA)
        return _reasoning._parse_router_response(response)

    # ------------------------------------------------------------------ #
    # Reply → turn correlation (the speak path's terminal)               #
    # ------------------------------------------------------------------ #

    def bind_reply(self, speech_handle: SpeechHandle) -> None:
        """Bind a ``generate_reply`` reply to the oldest pending SPEAK turn.

        Called by the session ``speech_created`` listener (Johnny-xpa wires it
        in :meth:`JohnnyAgent.on_enter`) for each ``source == "generate_reply"``
        speech. Registers a done-callback that emits the turn's terminal when
        the reply completes. The gated session runs with
        ``preemptive_generation=False`` so this fires *after* :meth:`run_turn`
        pushed the turn id — a simple FIFO correlation (a reply with no pending
        turn, e.g. an explicit ``say()``, is ignored).

        A reply the approval coordinator created out of band (Johnny-z97 §7.3)
        fires this same listener with ``source == "generate_reply"`` but is **not**
        a gated-SPEAK reply: the coordinator registered its handle id via
        :meth:`register_approval_reply` before ``generate_reply`` returned, so it
        is recognised here and skipped (binding it would mis-attribute its
        completion to an unrelated pending SPEAK turn). It is consumed from the set
        on the way out so the set stays bounded.
        """
        # A new speech is starting: drop any reply audio still buffered from a
        # previous speech (Johnny-od1). This fires before the new speech's TTS
        # produces a single segment — including for approval replies and
        # explicit say()s, whose audio is never persisted — so a kept reply's
        # WAV can only ever contain its own segments. The caption buffer gets
        # the same hygiene (Johnny-trt.58): a speech whose owner never took it
        # (an interrupted approval reply) must not leak into this reply's
        # partial.
        if self._reply_audio is not None:
            self._reply_audio.discard_reply()
        self._captions.take()
        if speech_handle.id in self._approval_reply_handles:
            self._approval_reply_handles.discard(speech_handle.id)
            return
        if not self._pending_speak_turns:
            return
        turn_id = self._pending_speak_turns.popleft()
        # Record the reply now playing so the barge-in classifier (Johnny-k8t)
        # can capture (turn_id, handle) as the interrupt target + generation
        # guard key. Cleared when the reply completes (_on_reply_done).
        self._active_reply = (turn_id, speech_handle)

        def _on_done(handle: SpeechHandle) -> None:
            task = asyncio.ensure_future(self._on_reply_done(turn_id, handle))
            self._reply_tasks.add(task)
            task.add_done_callback(self._reply_tasks.discard)

        speech_handle.add_done_callback(_on_done)

    @property
    def active_reply(self) -> tuple[str, SpeechHandle] | None:
        """The reply currently being spoken as ``(turn_id, SpeechHandle)``.

        ``None`` when the bot is idle. The barge-in path (Johnny-k8t) reads this
        to label the interrupt target with its LiveKit turn id; the authoritative
        "is it still playing" check is the session's ``current_speech``.
        """
        return self._active_reply

    def note_coercion_no_match(self) -> None:
        """Flag the active reply's turn as an allowed-reply coercion no-match (Johnny-5ag).

        Called by :meth:`JohnnyAgent.llm_node` when
        :func:`~johnny.agent.answer.coerce_allowed_reply` finds no allowed reply:
        the node yields nothing, so the reply completes with no assistant output.
        Recording the active reply's turn id makes :meth:`_on_reply_done` emit
        ``no_reply(no_allowed_reply_match)`` for that empty reply instead of the
        generic ``model_empty_output`` (parity with the legacy
        ``_answer_and_speak`` → ``no_allowed_reply_match``). The active reply is
        set by :meth:`bind_reply` (fired by the session ``speech_created``
        listener) *before* ``llm_node`` runs, so the turn id is available here;
        a no-op when there is no active reply (a degenerate, unbound coercion).
        """
        if self._active_reply is not None:
            self._coercion_no_match_turns.add(self._active_reply[0])

    async def _on_reply_done(self, turn_id: str, handle: SpeechHandle) -> None:
        """Emit the speak path's single terminal once the reply is done.

        ``interrupted`` → ``barge_in`` (the user cut the bot off mid-reply);
        no chat items produced → ``no_allowed_reply_match`` when allowed-reply
        coercion flagged this turn (Johnny-5ag), else ``model_empty_output``;
        otherwise ``replied`` (and the utterance counts toward the over-talk cap).
        First-wins via the ledger, so a duplicate done-callback can never
        double-emit.

        An interrupted reply that already streamed captions keeps its partial
        (Johnny-trt.58): the terminal stays ``no_reply(barge_in)`` — INV-1
        semantics unchanged — and an ``AgentSpoke(interrupted=True)`` follows
        in the terminal-before-spoke wire order, carrying the caption text
        flushed by cut time so the phrase lands in the chat/history instead of
        vanishing. Its buffered audio is left for the spoke emitter to flush
        (the partial WAV is as real as the partial text). A reply cut before
        any caption flushed produced no audible speech — legacy behaviour:
        audio discarded, nothing recorded.
        """
        # The reply is finished — clear it so a barge-in classifier started for a
        # later turn doesn't capture a dead handle as its interrupt target.
        if self._active_reply is not None and self._active_reply[0] == turn_id:
            self._active_reply = None
        # Consume any coercion-no-match flag for this turn (set by llm_node) so the
        # set stays bounded regardless of which terminal branch fires below.
        coercion_no_match = turn_id in self._coercion_no_match_turns
        self._coercion_no_match_turns.discard(turn_id)
        # Take (and thereby clear) this speech's caption buffer in every branch,
        # so a later speech interrupted before its first flush can never inherit
        # a stale partial from this one.
        partial = self._captions.take()
        if handle.interrupted:
            if partial and self._record_spoke is not None:
                if await self._ledger.emit(
                    turn_id,
                    terminal_state="no_reply",
                    no_reply_reason="barge_in",
                    detail="reply interrupted before completion (partial kept)",
                ):
                    await self._record_spoke(
                        partial, turn_id=turn_id, kind="reply", interrupted=True
                    )
                return
            # No captions (cut before the first sentence flushed, TTS degrade)
            # or no spoke seam: nothing audible to keep — the reply has no
            # chat line to attach audio to, so the buffered segments are
            # dropped, not persisted (Johnny-od1).
            if self._reply_audio is not None:
                self._reply_audio.discard_reply()
            await self._ledger.emit(
                turn_id,
                terminal_state="no_reply",
                no_reply_reason="barge_in",
                detail="reply interrupted before completion",
            )
            return
        if not handle.chat_items:
            if self._reply_audio is not None:
                self._reply_audio.discard_reply()
            await self._ledger.emit(
                turn_id,
                terminal_state="no_reply",
                no_reply_reason=(
                    "no_allowed_reply_match" if coercion_no_match else "model_empty_output"
                ),
                detail=(
                    "allowed-reply coercion found no match"
                    if coercion_no_match
                    else "reply produced no assistant output"
                ),
            )
            return
        self._recent_utterance_times.append(self._clock())
        await self._ledger.emit(turn_id, terminal_state="replied", detail="bot spoke")
        # Observability parity (Johnny-d5z): the bot actually spoke, so publish the
        # AgentSpoke the subscriber turns into the agent_utterances row (and writes
        # the spoken text back onto the turn's decision row, INV-2 — by the exact
        # turn id since Johnny-trt.54). The text comes off the reply's chat items —
        # the same items the empty-reply check above read, so it is non-empty here.
        if self._record_spoke is not None:
            await self._record_spoke(_extract_spoken_text(handle), turn_id=turn_id, kind="reply")

    # ------------------------------------------------------------------ #
    # Approval-required wiring (Johnny-z97 / qzj)                         #
    # ------------------------------------------------------------------ #

    def attach_approval(self, coordinator: ApprovalCoordinator) -> None:
        """Attach the out-of-band approval coordinator after construction.

        The coordinator's ``generate_reply`` wrapper holds a back-reference to
        this gate (to call :meth:`register_approval_reply`), and the gate's
        approval branch needs the coordinator — a mutual reference resolved by
        building the gate first, then the coordinator, then attaching it here
        (see :func:`johnny.agent.approval_wiring.build_approval_coordinator`).
        """
        self._approval = coordinator

    def register_approval_reply(self, handle_id: str) -> None:
        """Mark a ``SpeechHandle`` id as owned by the out-of-band approval reply.

        Called by the approval ``generate_reply`` wrapper (Johnny-z97 §7.3) the
        instant ``session.generate_reply`` returns the handle — *before* the
        ``speech_created`` callback is dispatched on a later loop tick — so
        :meth:`bind_reply` recognises and skips it instead of binding it to a
        pending SPEAK turn.
        """
        self._approval_reply_handles.add(handle_id)

    def attach_say(self, say: SaySpeech) -> None:
        """Attach the ``session.say`` seam for delegate acks / status stubs (Johnny-trt.17).

        Called by :meth:`JohnnyAgent.on_enter` once the agent is active — the
        :class:`~livekit.agents.AgentSession` does not exist when the gate is
        constructed (the :func:`~johnny.agent.job_session.build_agent_runtime`
        assembly order), the same reason :meth:`attach_approval` exists. Until
        attached, delegate/status verdicts terminalize ``no_reply(stage_error)``
        rather than queueing work whose ack cannot be spoken.
        """
        self._say = say

    async def wait_recent_say_done(self, timeout_s: float = 30.0) -> None:
        """Wait for the most recent say() speech to finish playing (Johnny-trt.57).

        The internal-tool teardown runners (``meeting.leave`` /
        ``session.end``) call this before disconnecting, so the farewell —
        the delegate turn's router-authored ack, spoken via
        :meth:`_say_with_terminal` and stashed synchronously before the task
        resolver can run — finishes playing before the plug is pulled. An
        interrupted speech counts as done (``wait_for_playout`` returns on
        interruption); no say yet / a dead handle / a wedged playout all
        degrade to returning (bounded by ``timeout_s``) — a farewell may
        delay a leave, never block it. Never raises.
        """
        handle = self._last_say_handle
        if handle is None:
            return
        try:
            await asyncio.wait_for(handle.wait_for_playout(), timeout=timeout_s)
        except TimeoutError:
            logger.warning(
                "agent.router.gate: wait_recent_say_done timed out after %.0fs — "
                "proceeding",
                timeout_s,
            )
        except Exception:
            logger.exception(
                "agent.router.gate: wait_recent_say_done failed — proceeding"
            )

    def note_speech_caption(self, text: str, sequence: int) -> None:
        """Record one caption sentence of the speech playing now (Johnny-trt.58).

        The assembly tees the agent's ``tts_node`` interim sink here (see
        :func:`~johnny.agent.job_session.build_agent_runtime`), so the gate
        always knows what has been flushed to TTS for the current speech.
        When a barge-in cuts the speech, its done-callback takes the buffer as
        the partial actually delivered — the same sentences the live caption
        bubble showed. Sync and trivially cheap; called on the TTS hot path
        inside the agent's defensive sink wrapper.
        """
        self._captions.note(text, sequence)

    async def aclose(self) -> None:
        """Tear down the gate at session end (Johnny-z97 §7.4).

        Cancels in-flight approval resolvers (each settles its parked turn
        ``approval_rejected`` on the way out) then sweeps the ledger so any turn
        still open or parked gets its fallback terminal — INV-1 holds even on a
        hard teardown. Idempotent; safe to call with no coordinator attached.
        """
        if self._approval is not None:
            await self._approval.aclose()
        await self._ledger.close()

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _is_rate_limited(self) -> bool:
        """Per-session over-talk cap, ported from the legacy split pipeline.

        Enforced only when ``allowed_replies`` is set (the Limited-auto-speak
        marker) or the mode is ``autonomous``; a non-positive cap or window
        disables it. The recent-utterance list is pruned to the window in place.
        """
        cfg = self._config
        if not cfg.allowed_replies and cfg.mode != AUTONOMOUS_MODE:
            return False
        if cfg.rate_limit_max_utterances <= 0 or cfg.rate_limit_window_ms <= 0:
            return False
        window_start = self._clock() - cfg.rate_limit_window_ms
        self._recent_utterance_times = [t for t in self._recent_utterance_times if t > window_start]
        return len(self._recent_utterance_times) >= cfg.rate_limit_max_utterances

    def _router_messages(
        self, turn_ctx: ChatContext, new_message: LKChatMessage
    ) -> list[ChatMessage]:
        """Build the router prompt, mirroring the legacy split pipeline.

        System message: the gating-router framing + personality + mode +
        confidence threshold + task catalog (Johnny-trt.19, only when
        delegation is wired) + meeting/calendar context + allowed replies. User
        message: the rolling conversation (rendered from ``turn_ctx``) plus the
        latest transcript (``new_message``). ``new_message`` is *not* yet in
        ``turn_ctx`` (the SDK copies the chat ctx before appending it), so the
        history needs no de-duplication.
        """
        cfg = self._config
        system = (
            "You are the gating router for an AI meeting bot. Decide whether "
            "the bot should speak in response to the latest transcript. "
            "Reply as JSON matching the supplied schema."
        )
        if cfg.personality_prompt:
            system += f"\n\n{cfg.personality_prompt}"
        system += (
            f"\n\nIn the 'Recent conversation' list below, lines prefixed "
            f"'{BOT_SPEAKER_LABEL}:' are the bot's OWN earlier utterances "
            "(yours). Every other speaker label is a meeting participant. "
            "Use the bot's prior lines to avoid repeating yourself and to "
            "stay coherent with what you already said."
        )
        system += f"\n\nMode: {cfg.mode}"
        system += f"\nConfidence threshold for speaking: {cfg.confidence_threshold:.2f}"
        if cfg.task_catalog:
            # Task catalog (Johnny-trt.19): the delegate-action vocabulary.
            # Rendered before the operator's meeting instructions so those can
            # refine ("never delegate during standup") rather than be
            # contradicted. Empty catalog ⇒ this block is absent and the
            # prompt is byte-identical to the pre-catalog build.
            system += f"\n\n{render_task_catalog(cfg.task_catalog)}"
        if cfg.instructions:
            system += f"\n\nMeeting instructions: {cfg.instructions}"
        if cfg.context:
            system += f"\n\nContext: {cfg.context}"
        if cfg.calendar_context:
            system += f"\n\nCalendar event description: {cfg.calendar_context}"
        if cfg.calendar_attachments_text:
            system += (
                "\n\nCalendar attachments (linked documents from the event "
                f"description):\n{cfg.calendar_attachments_text}"
            )
        if cfg.prior_session_context:
            system += f"\n\nLast session summary: {cfg.prior_session_context}"
        if cfg.allowed_replies:
            system += (
                "\n\nAllowed replies (the answer stage will pick verbatim from "
                f"this list): {list(cfg.allowed_replies)}"
            )

        user_parts: list[str] = []
        history = self._render_history(turn_ctx)
        if history:
            user_parts.append("Recent conversation:")
            user_parts.extend(history)
            user_parts.append("")
        latest = (new_message.text_content or "").strip()
        user_parts.append(f"Latest transcript: {latest}")
        return [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content="\n".join(user_parts)),
        ]

    @staticmethod
    def _render_history(turn_ctx: ChatContext) -> list[str]:
        """Render prior ``turn_ctx`` messages as ``- speaker: text`` lines.

        Assistant items are the bot's own speech → prefixed
        :data:`BOT_SPEAKER_LABEL`; user items render verbatim (rehydrated turns
        already carry a ``"{speaker}: {text}"`` prefix, live turns don't).
        Non-message items (tool calls/outputs, handoffs) and empty text are
        skipped — the router only reasons over conversation.
        """
        lines: list[str] = []
        for item in turn_ctx.items:
            if not isinstance(item, LKChatMessage):
                continue
            if item.role not in ("user", "assistant"):
                continue
            text = (item.text_content or "").strip()
            if not text:
                continue
            if item.role == "assistant":
                lines.append(f"- {BOT_SPEAKER_LABEL}: {text}")
            else:
                lines.append(f"- {text}")
        return lines

    def _transcript_window(
        self, turn_ctx: ChatContext, new_message: LKChatMessage
    ) -> list[dict[str, object]]:
        """The conversation this decision was made over, for ``input_window`` (Johnny-trt.54).

        The decision-event analogue of the legacy pipeline's
        ``transcript_window``: the same ``turn_ctx`` items :meth:`_router_messages`
        renders into the router prompt, as ``{text, speaker, confidence,
        is_current, timestamp_ms}`` entries with the trigger transcript last and
        marked ``is_current`` — the shape the session-detail timeline's "Heard
        you" / "Looked at the context" steps and the per-session replay
        (``_heard_from_input_window``) already consume for legacy rows. Prior
        entries are capped at the most recent :data:`TRANSCRIPT_WINDOW_LIMIT` so
        a long meeting doesn't bloat every ``agent_decisions`` row; ``confidence``
        is ``None`` (the gate has no per-final STT confidence on this path) and
        ``timestamp_ms`` is the emit-time wall clock.
        """
        now_ms = int(time.time() * 1000)
        entries: list[dict[str, object]] = []
        for item in turn_ctx.items:
            if not isinstance(item, LKChatMessage):
                continue
            if item.role not in ("user", "assistant"):
                continue
            text = (item.text_content or "").strip()
            if not text:
                continue
            entries.append(
                {
                    "text": text,
                    "speaker": BOT_SPEAKER_LABEL if item.role == "assistant" else None,
                    "confidence": None,
                    "is_current": False,
                    "timestamp_ms": now_ms,
                }
            )
        entries = entries[-TRANSCRIPT_WINDOW_LIMIT:]
        entries.append(
            {
                "text": (new_message.text_content or "").strip(),
                "speaker": None,
                "confidence": None,
                "is_current": True,
                "timestamp_ms": now_ms,
            }
        )
        return entries


__all__ = [
    "ACK_FALLBACK_KEY",
    "CAPABILITY_GAP_KEY",
    "DEFAULT_DELEGATE_ACK",
    "STATUS_STUB_REPLY",
    "PersistPendingDecision",
    "RouterGate",
    "RouterGateConfig",
    "SaySpeech",
    "capability_decline_speech",
    "delegate_failure_correction",
]
