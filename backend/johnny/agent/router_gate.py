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
from dataclasses import dataclass

from livekit.agents.llm import ChatContext, StopResponse
from livekit.agents.llm.chat_context import ChatMessage as LKChatMessage
from livekit.agents.voice import SpeechHandle

from app.providers.base import ChatMessage, LLMProvider
from johnny.agent.approval import ApprovalCoordinator, ApprovalRound
from johnny.agent.gate import (
    GateAction,
    TerminalTracker,
    TurnLedger,
    run_gate,
)
from johnny.agent.observability import RecordDecision, RecordSpoke, RecordSuggested
from johnny.voice_pipeline import reasoning as _reasoning
from johnny.voice_pipeline.reasoning import (
    APPROVAL_REQUIRED_MODE,
    AUTONOMOUS_MODE,
    DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_MODE,
    DEFAULT_RATE_LIMIT_MAX_UTTERANCES,
    DEFAULT_RATE_LIMIT_WINDOW_MS,
    DEFAULT_ROUTER_LLM_TIMEOUT_S,
    LISTEN_ONLY_MODE,
    SUGGEST_ONLY_MODE,
    RouterDecision,
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
        action, decision = await run_gate(
            lambda: self._decide(turn_ctx, new_message),
            tracker=tracker,
            timeout_s=self._config.router_llm_timeout_s,
            abandon=self._abandon,
        )

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

        # Observability parity (Johnny-d5z): publish this turn's RouterDecisionMade
        # so the subscriber writes its agent_decisions row (outcome derived from the
        # mode in input_window) and the turn's later TurnTerminal stamps that same
        # row by the int turn id. Emitted once, before the branch — exactly like the
        # legacy ``_respond_to_transcript_inner`` published the decision event right
        # after the router returned, then branched. ``approval_required`` persists
        # its own pending row via ``persist_pending_decision`` (Johnny-qzj); emitting
        # here too would double-write, so that mode is skipped.
        if self._record_decision is not None and self._config.mode != APPROVAL_REQUIRED_MODE:
            await self._record_decision(decision, turn_id)

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
            await self._begin_approval(tracker, turn_id, decision)
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

    async def _decide(self, turn_ctx: ChatContext, new_message: LKChatMessage) -> RouterDecision:
        """Call the router LLM and parse its structured decision.

        Passed to :func:`run_gate` as the bounded router call, so a hang /
        cancellation / provider error is handled there (→ ``stage_error`` /
        ``barge_in``) — this method just builds the prompt, requests the
        decision schema, and reuses the legacy parser for verdict parity.
        """
        messages = self._router_messages(turn_ctx, new_message)
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
        """
        # The reply is finished — clear it so a barge-in classifier started for a
        # later turn doesn't capture a dead handle as its interrupt target.
        if self._active_reply is not None and self._active_reply[0] == turn_id:
            self._active_reply = None
        # Consume any coercion-no-match flag for this turn (set by llm_node) so the
        # set stays bounded regardless of which terminal branch fires below.
        coercion_no_match = turn_id in self._coercion_no_match_turns
        self._coercion_no_match_turns.discard(turn_id)
        if handle.interrupted:
            await self._ledger.emit(
                turn_id,
                terminal_state="no_reply",
                no_reply_reason="barge_in",
                detail="reply interrupted before completion",
            )
            return
        if not handle.chat_items:
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
        # the spoken text back onto the turn's decision row, INV-2). The text comes
        # off the reply's chat items — the same items the empty-reply check above
        # read, so it is non-empty here.
        if self._record_spoke is not None:
            await self._record_spoke(_extract_spoken_text(handle))

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
        confidence threshold + meeting/calendar context + allowed replies. User
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


__all__ = [
    "PersistPendingDecision",
    "RouterGate",
    "RouterGateConfig",
]
