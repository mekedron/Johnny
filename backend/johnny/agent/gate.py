"""Bounded router-gate execution: timeout + barge-in cancel + INV-1 terminal.

Spike **Johnny-9k2** (epic Johnny-7g5, Phase 2). De-risks the *blocking*
``Agent.on_user_turn_completed`` "should-speak" gate for the LiveKit-Agents
migration. The gate *body* — building the router messages, calling Johnny's
router ``LLMProvider``, parsing the decision, raising ``StopResponse`` — is
Johnny-xpa. This module is the harness Johnny-xpa wraps its router call in so a
hung or barged-in gate (a) never stalls later turns and (b) always leaves
*exactly one* terminal ``no_reply``/``stage_error`` audit row.

Why a harness is needed — verified against ``livekit-agents==1.5.17``
(``voice/agent_activity.py``):

* ``AgentActivity._user_turn_completed_task`` ``await``\\s
  ``on_user_turn_completed`` *before* it schedules any reply, so the hook
  blocks the whole response pipeline until it returns.
* The SDK **never cancels the hook** — its own comment: *"We never cancel user
  code as this is very confusing. So we wait for the old execution of
  on_user_turn_completed to finish."* A newer turn's task literally
  ``await old_task``\\s the older hook. So a hook with **no internal bound
  stalls EVERY subsequent turn** — this is the Session-14 ~60 s hang the
  legacy ``asyncio.wait_for`` bound (``voice_pipeline.pipeline._run_router``,
  ``DEFAULT_ROUTER_LLM_TIMEOUT_S``) was added to kill. We port that bound here.
* The SDK **swallows** ``StopResponse`` *and any* ``Exception`` raised by the
  hook (``except StopResponse: return`` / ``except Exception: ...; return``)
  **without writing any audit row**. So ``StopResponse`` alone loses the turn's
  terminal — a timed-out / declined / barged-in gate must emit its own terminal
  *before* it returns or raises. That is :class:`TerminalTracker`'s job
  (the INV-1 "exactly one terminal per turn" guard, ported from
  ``voice_pipeline.pipeline._emit_turn_terminal`` + its belt-and-suspenders).

How Johnny-xpa composes this in the real, livekit-importing hook::

    from livekit.agents.llm import StopResponse

    async def on_user_turn_completed(self, turn_ctx, new_message):
        tracker = TerminalTracker(self._emit_turn_terminal, turn_id=self._turn_id())
        action, decision = await run_gate(
            lambda: self._router.decide(turn_ctx, new_message),
            tracker=tracker,
            timeout_s=self._router_timeout_s,
            abandon=self._barge_in_event,        # set by the fast-VAD path / Johnny-k8t
        )
        if action is GateAction.STAY_SILENT:
            raise StopResponse()                 # terminal already emitted by run_gate
        if not decision.should_speak:
            await tracker.emit(terminal_state="no_reply",
                               no_reply_reason="router_declined", detail=decision.reason)
            raise StopResponse()
        # SPEAK: emit NO terminal here — the reply-completion path owns the
        # turn's terminal (spoken / interrupted-mid-reply). Returning normally
        # lets the SDK generate the reply.

:class:`TerminalTracker` enforces INV-1 within *one* gate invocation. The
**session-level** authority is :class:`TurnLedger` (spike **Johnny-o3z**): the
legacy ``voice_pipeline.pipeline`` could key INV-1 on a single
``_turn_terminal_emitted`` bool because ``_respond_to_transcript`` was
serialised, but under ``AgentSession`` the gate and the reply ``SpeechHandle``
done-callback are temporally disjoint *and turns overlap* (turn N's reply
done-callback races turn N+1's gate). So the flag must be **per-turn-id**, not
per-session. :class:`TurnLedger` is one ``dict[turn_id, GateTerminal | None]``
per session, keyed by the user ``ChatMessage.id`` (``item_<shortuuid>`` — the
only id available *at gate entry* and preserved through LiveKit's
``_generate_reply``). Both emitters (the gate, via :meth:`TurnLedger.gate_tracker`,
and the reply done-callback, via :meth:`TurnLedger.emit`) route through its
atomic first-wins chokepoint; :meth:`TurnLedger.close` sweeps any turn that was
opened but never terminalized (the zero-emission fallback). See
``.validation/Johnny-o3z/decision.md`` for the full path enumeration.

This module is deliberately ``livekit``-free and ``sqlalchemy``-free (stdlib
only) so ``import johnny.agent.gate`` stays cheap and safe, mirroring the
adapter package's import-safety discipline. The real ``TurnTerminal`` →
``EventBus`` → ``agent_decisions`` wiring is injected as a
:data:`TerminalEmitter` / :data:`SessionTerminalEmitter` callback
(event/observability parity is Johnny-d5z).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Ported verbatim from voice_pipeline.pipeline.DEFAULT_ROUTER_LLM_TIMEOUT_S.
# Session 14 turn 4 hung ~60 s with no bound; 30 s is generous for a sensibly
# sized local model under load yet kills the dead-minute stall. ``<= 0`` (or a
# ``None`` timeout) disables the wall-clock bound but keeps the abandon race.
DEFAULT_ROUTER_GATE_TIMEOUT_S = 30.0

# Local mirrors of voice_pipeline.events.{TerminalState,NoReplyReason}. Kept
# stdlib-only on purpose (see module docstring); a drift-guard test asserts
# these stay a subset of the canonical literals so the audit shape never skews.
GateTerminalState = Literal["no_reply", "replied", "pending_approval"]
"""The coarse bucket a turn resolves to (mirror of events.TerminalState)."""

GateNoReplyReason = Literal[
    "router_declined",
    "low_confidence",
    "barge_in",
    "stage_error",
]
"""The ``no_reply`` sub-reasons the *gate harness itself* produces (subset of
:data:`TurnNoReplyReason`): ``router_declined``/``low_confidence`` (decision
paths, emitted by the caller), ``barge_in`` (abandoned / cancelled mid-gate),
``stage_error`` (router timeout, router raised, or an unaccounted exit)."""

TurnNoReplyReason = Literal[
    "router_declined",
    "low_confidence",
    "barge_in",
    "rate_limited",
    "tts_unavailable",
    "suggest_only",
    "approval_rejected",
    "model_empty_output",
    "no_allowed_reply_match",
    "noise_filtered",
    "stage_error",
    "listen_only",
]
"""The full ``no_reply`` vocabulary the **session ledger** records — every reason
any phase of a turn can resolve to, not just the gate-reachable ones. The gate
emits the :data:`GateNoReplyReason` subset; the reply-completion path adds
``model_empty_output`` (reply produced no audio) and the caller's mode handlers
(Johnny-xpa) add ``rate_limited`` / ``tts_unavailable`` / ``suggest_only`` /
``approval_rejected`` / etc. A full mirror of ``events.NoReplyReason`` (kept
stdlib-only; a drift-guard test asserts equality so the audit shape never skews)."""


@dataclass(frozen=True, slots=True)
class GateTerminal:
    """The single terminal an :class:`TerminalTracker` emits for one turn.

    The :data:`TerminalEmitter` maps this onto the durable
    ``voice_pipeline.events.TurnTerminal`` → ``EventBus`` → ``agent_decisions``
    row (Johnny-d5z). Carrying just the three audit fields keeps the harness
    decoupled from the event/DB layer.
    """

    terminal_state: GateTerminalState
    no_reply_reason: TurnNoReplyReason | None
    detail: str


# Injected by the caller: persist/publish the turn's terminal audit row.
TerminalEmitter = Callable[[GateTerminal], Awaitable[None]]

# Session-level variant injected into :class:`TurnLedger`: persist/publish the
# durable terminal for a specific LiveKit turn id. Johnny-d5z maps
# ``(turn_id, GateTerminal)`` onto a ``voice_pipeline.events.TurnTerminal`` →
# ``EventBus`` → ``agent_decisions`` row (the turn id binds it to the turn's
# canonical decision record).
SessionTerminalEmitter = Callable[[str, GateTerminal], Awaitable[None]]


class GateAction(Enum):
    """What the caller should do once :func:`run_gate` returns."""

    SPEAK = "speak"
    """Router approved within the bound and was not abandoned. The caller
    interprets the decision (``should_speak`` / confidence) and, if it really
    speaks, lets the SDK generate the reply. **No terminal is emitted yet** —
    the reply-completion path owns the turn's terminal."""

    STAY_SILENT = "stay_silent"
    """The gate terminated itself (timeout / barge-in / router error). A
    terminal row was already emitted; the caller must ``raise StopResponse``."""


class RouterStatus(Enum):
    """Outcome of the bounded router call in :func:`run_router_call`."""

    OK = "ok"
    TIMED_OUT = "timed_out"
    ABANDONED = "abandoned"


class TerminalTracker:
    """Enforce INV-1 — *exactly one* terminal per turn — across every gate exit.

    Ported from ``voice_pipeline.pipeline``'s ``_turn_terminal_emitted`` flag +
    ``_emit_turn_terminal`` chokepoint + ``_handle_unaccounted_turn``
    belt-and-suspenders. The first :meth:`emit` wins and marks the turn
    accounted-for; a second is logged and dropped (never two rows).
    :meth:`ensure_terminal` is the fallback that guarantees a silent exit still
    leaves a row.
    """

    def __init__(
        self,
        emit: TerminalEmitter,
        *,
        turn_id: str | int,
        strict: bool = False,
    ) -> None:
        self._emit = emit
        # The LiveKit turn id is the user ChatMessage.id (``item_<shortuuid>``,
        # a str); the legacy pipeline / unit fixtures use the int utterance
        # counter. Only ever rendered into logs / the strict assertion, never
        # used arithmetically, so both are accepted (Johnny-o3z).
        self.turn_id: str | int = turn_id
        # When True, an unaccounted gate exit (no terminal emitted) raises
        # AssertionError after the fallback fires — mirrors STRICT_TURN_TERMINAL
        # so the gap surfaces in dev/test instead of shipping a silent drop.
        self._strict = strict
        self.emitted = False
        self.terminal: GateTerminal | None = None

    async def emit(
        self,
        *,
        terminal_state: GateTerminalState,
        no_reply_reason: TurnNoReplyReason | None = None,
        detail: str = "",
    ) -> bool:
        """Emit the turn's single terminal. Returns ``False`` on a 2nd call.

        Defensive like the legacy chokepoint: a failing emitter is logged at
        ``error`` (a dropped terminal means a lost audit row) but never
        re-raised, so the terminal path cannot itself crash the gate.
        """
        if self.emitted:
            logger.error(
                "INV-1 violation: turn=%s already terminal %r; ignoring 2nd %s/%s",
                self.turn_id,
                self.terminal,
                terminal_state,
                no_reply_reason,
            )
            return False
        self.emitted = True
        terminal = GateTerminal(
            terminal_state=terminal_state,
            no_reply_reason=no_reply_reason,
            detail=detail,
        )
        self.terminal = terminal
        logger.info(
            "agent.turn.terminal: turn=%s state=%s reason=%s detail=%r",
            self.turn_id,
            terminal_state,
            no_reply_reason,
            detail,
        )
        try:
            await self._emit(terminal)
        except Exception:
            logger.exception(
                "failed to emit terminal for turn=%s — the turn's audit row will be missing",
                self.turn_id,
            )
        return True

    async def ensure_terminal(self, *, exc: BaseException | None = None) -> None:
        """Belt-and-suspenders: emit a fallback terminal if none was emitted.

        Called from the gate's outer ``except``/``finally`` so an exit nobody
        accounted for (an unexpected raise, or task cancellation at teardown)
        still leaves a row. ``CancelledError`` maps to ``barge_in`` (the user
        resumed / the session is tearing the turn down); any other exception
        maps to ``stage_error``; a clean-but-terminal-less return also maps to
        ``stage_error`` (the legacy "unaccounted turn"). Best-effort during
        cancellation — the emitter may not get to run if the loop is unwinding.
        """
        if self.emitted:
            return
        if isinstance(exc, asyncio.CancelledError):
            reason: GateNoReplyReason = "barge_in"
            detail = "gate cancelled before a terminal was emitted"
        elif exc is not None:
            reason = "stage_error"
            detail = f"unaccounted gate exit: {type(exc).__name__}: {exc}"
        else:
            reason = "stage_error"
            detail = "gate returned without a terminal"
        await self.emit(terminal_state="no_reply", no_reply_reason=reason, detail=detail)
        if self._strict and not isinstance(exc, asyncio.CancelledError):
            raise AssertionError(
                f"turn {self.turn_id} exited the gate without a terminal: {detail}"
            )


class TurnLedger:
    """Session-scoped INV-1 authority — *exactly one* terminal per LiveKit turn id.

    Spike **Johnny-o3z**. The legacy ``voice_pipeline.pipeline`` enforced INV-1
    with one session-scalar ``_turn_terminal_emitted`` bool, which is correct
    **only because** ``_respond_to_transcript`` is serialised (one turn in
    flight at a time). Under ``AgentSession`` that assumption is gone:

    * our terminal-emitting code runs in two *temporally disjoint* places — the
      ``on_user_turn_completed`` gate, and (only on the speak path) the reply
      :class:`SpeechHandle`'s done-callback, fired *later* by the SDK;
    * turns **overlap** — turn N's reply done-callback races turn N+1's gate, so
      a single shared flag would clobber.

    So INV-1 moves to a per-turn-id ledger. One :class:`TurnLedger` per session
    holds ``dict[turn_id, GateTerminal | None]`` keyed by the user
    ``ChatMessage.id`` (``item_<shortuuid>`` — the only id available *at gate
    entry* and preserved through LiveKit's ``_generate_reply``). Both emitters
    route through :meth:`emit` (the gate via :meth:`gate_tracker`; the reply
    done-callback directly), whose first-wins claim is **atomic** — the slot is
    taken *before* any ``await`` so two concurrent emits for one turn id can
    never both publish. :meth:`close` is the belt-and-suspenders sweep: a turn
    ``open``-ed but never terminalized (a lost reply handle, a hard teardown)
    gets a fallback ``no_reply(stage_error)`` so it can never vanish.

    LiveKit emits no Johnny terminal of its own (``speech_created`` /
    ``conversation_item_added`` / ``metrics_collected`` are observability only),
    so reconciliation is entirely between *our* two emitters — there is no
    SDK-side terminal to double with.

    **Approval-required mode (spike Johnny-z97)** adds a third, *non-final* slot
    state between open and terminal: a turn the gate hands to the out-of-band
    approval round is :meth:`park`-ed (a ``pending_approval`` marker) rather than
    terminalized, because the ~15 s human wait cannot block the gate. A parked
    turn is excluded from :attr:`open_turns` (so the sweep does not call it an
    unaccounted drop) and is settled to its single final terminal by
    :meth:`resolve` — the only call that may overwrite the marker, and only once.
    Three states, three transitions: ``open → emit`` (normal), ``open → park →
    resolve`` (approval), ``open/park → close`` (sweep). INV-1 still holds as
    *exactly one final terminal per turn id*; ``pending_approval`` is a transient
    parked marker, never the turn's durable terminal.

    Stdlib-only, like the rest of this module: the durable ``TurnTerminal`` →
    ``EventBus`` → ``agent_decisions`` wiring is injected as a
    :data:`SessionTerminalEmitter` (Johnny-d5z).
    """

    def __init__(self, emit: SessionTerminalEmitter, *, strict: bool = False) -> None:
        self._emit = emit
        # Strict mirrors STRICT_TURN_TERMINAL: an unaccounted turn at session
        # close raises so the gap surfaces in dev/test instead of shipping.
        self._strict = strict
        # None = opened but not yet terminal; a GateTerminal = accounted-for.
        self._turns: dict[str, GateTerminal | None] = {}

    def open(self, turn_id: str) -> None:
        """Register a turn the moment we first own it (gate entry). Idempotent.

        Only registered turns are chased by :meth:`close`. Paths LiveKit
        short-circuits *before* the gate (``skip_reply``, too-short transcript,
        scheduling paused, no LLM, realtime server-side turn detection) are
        deliberately never opened — they are not turns we own, exactly like the
        legacy ``LISTEN_ONLY`` / noise-gate paths that emit no terminal.
        """
        self._turns.setdefault(turn_id, None)

    @property
    def open_turns(self) -> tuple[str, ...]:
        """Turn ids registered via :meth:`open` that have not yet terminalized.

        Excludes **parked** turns (see :meth:`park`): a turn awaiting human
        approval is accounted-for (it has a ``pending_approval`` marker), so the
        :meth:`close` *open*-sweep must not treat it as an unaccounted drop.
        """
        return tuple(tid for tid, term in self._turns.items() if term is None)

    @property
    def parked_turns(self) -> tuple[str, ...]:
        """Turn ids parked via :meth:`park` and not yet :meth:`resolve`-d.

        A parked turn holds a non-final ``pending_approval`` marker — the
        out-of-band approval round (spike Johnny-z97) owns its single final
        terminal, emitted by :meth:`resolve`. :meth:`close` force-resolves any
        still-parked turn so an abandoned approval can never leave a turn open.
        """
        return tuple(
            tid
            for tid, term in self._turns.items()
            if term is not None and term.terminal_state == "pending_approval"
        )

    def terminal_for(self, turn_id: str) -> GateTerminal | None:
        """The recorded terminal for ``turn_id``.

        ``None`` if open or unknown; the ``pending_approval`` marker if parked
        (a *non-final* state — :meth:`resolve` replaces it with the real
        terminal); the final :class:`GateTerminal` once resolved.
        """
        return self._turns.get(turn_id)

    async def emit(
        self,
        turn_id: str,
        *,
        terminal_state: GateTerminalState,
        no_reply_reason: TurnNoReplyReason | None = None,
        detail: str = "",
    ) -> bool:
        """Emit ``turn_id``'s single terminal. First wins; a 2nd returns ``False``.

        The one session-wide chokepoint. Called by the reply done-callback
        (``replied`` / ``barge_in`` / ``stage_error`` / ``model_empty_output``)
        and by the gate (via :meth:`gate_tracker`). A second call for the same
        turn id — a done-callback that fires twice, a sweep racing a late reply,
        the gate and reply both firing — is logged and dropped.
        """
        terminal = GateTerminal(
            terminal_state=terminal_state,
            no_reply_reason=no_reply_reason,
            detail=detail,
        )
        return await self._publish(turn_id, terminal)

    async def _publish(self, turn_id: str, terminal: GateTerminal) -> bool:
        # Atomic check-and-set: claim the slot BEFORE the first await so two
        # concurrent emits for the same turn id can never both publish (the
        # event loop is single-threaded; with no await between the get and the
        # set, no other coroutine interleaves). Mirrors the legacy
        # ``self._turn_terminal_emitted = True`` placed before the bus await.
        if self._turns.get(turn_id) is not None:
            logger.error(
                "INV-1 violation: turn=%s already terminal %r; ignoring 2nd %s/%s",
                turn_id,
                self._turns[turn_id],
                terminal.terminal_state,
                terminal.no_reply_reason,
            )
            return False
        self._turns[turn_id] = terminal
        logger.info(
            "agent.turn.terminal: turn=%s state=%s reason=%s detail=%r",
            turn_id,
            terminal.terminal_state,
            terminal.no_reply_reason,
            terminal.detail,
        )
        try:
            await self._emit(turn_id, terminal)
        except Exception:
            logger.exception(
                "failed to emit terminal for turn=%s — the turn's audit row will be missing",
                turn_id,
            )
        return True

    def park(self, turn_id: str, *, detail: str = "") -> bool:
        """Mark a turn as awaiting out-of-band human approval (spike Johnny-z97).

        The ``approval_required`` mode cannot block the ``on_user_turn_completed``
        gate for the ~15 s human wait — the SDK await-chains each turn's hook, so a
        blocking gate would head-of-line-stall every later turn. Instead the gate
        **parks** the turn (records a *non-final* ``pending_approval`` marker) and
        raises ``StopResponse`` immediately, and the out-of-band
        :class:`~johnny.agent.approval.ApprovalCoordinator` later calls
        :meth:`resolve` with the single final terminal.

        Parking is *not* a terminal: it claims the slot so :meth:`emit` (a stray
        reply done-callback, the close open-sweep) cannot clobber it, while leaving
        it :meth:`resolve`-able exactly once. Records **no** ``TurnTerminal`` — the
        live UI learns the pending state from the separate ``ApprovalPending``
        event; the turn's one durable terminal lands at resolution (legacy parity:
        ``voice_pipeline.pipeline._handle_approval_required`` emits its single
        terminal *after* the approval resolves, never a ``pending_approval`` row).

        First-wins like :meth:`emit`: returns ``False`` if the turn is already
        parked or already terminal (synchronous; no ``await``, so atomic).
        """
        current = self._turns.get(turn_id)
        if current is not None:
            logger.error(
                "approval.park: turn=%s already %s %r; ignoring park",
                turn_id,
                "parked" if current.terminal_state == "pending_approval" else "terminal",
                current,
            )
            return False
        self._turns[turn_id] = GateTerminal(
            terminal_state="pending_approval",
            no_reply_reason=None,
            detail=detail,
        )
        logger.info("approval.park: turn=%s awaiting approval detail=%r", turn_id, detail)
        return True

    async def resolve(
        self,
        turn_id: str,
        *,
        terminal_state: GateTerminalState,
        no_reply_reason: TurnNoReplyReason | None = None,
        detail: str = "",
    ) -> bool:
        """Settle a :meth:`park`-ed turn with its single final terminal (Johnny-z97).

        The terminal transition for an approval turn: ``replied`` (approved and
        spoke), ``no_reply(approval_rejected)`` (rejected / timed out / session
        closed), ``no_reply(model_empty_output)`` (approved but the reply produced
        nothing), or ``no_reply(stage_error)`` (the approval round itself errored).
        This is the *only* call that may overwrite a ``pending_approval`` marker,
        and it does so exactly once: the final terminal is written **before** the
        first ``await`` (atomic claim), so two concurrent resolves (a human approve
        racing the timeout, or the close sweep racing a late resolution) reconcile
        to one publish — the later one sees a now-final slot and drops.

        Returns ``False`` (and emits nothing) if ``turn_id`` is **not parked** — an
        open, already-final, or unknown turn. The approval path must :meth:`park`
        before it resolves; a non-parked resolve is a misuse and is dropped rather
        than inventing a second terminal for a turn the normal :meth:`emit` path
        already owns.
        """
        current = self._turns.get(turn_id)
        if current is None or current.terminal_state != "pending_approval":
            logger.error(
                "approval.resolve: turn=%s is not parked (slot=%r); dropping %s/%s",
                turn_id,
                current,
                terminal_state,
                no_reply_reason,
            )
            return False
        terminal = GateTerminal(
            terminal_state=terminal_state,
            no_reply_reason=no_reply_reason,
            detail=detail,
        )
        # Atomic claim: replace the park marker with the FINAL terminal before any
        # await, so a racing second resolve sees a non-``pending_approval`` slot
        # and drops (mirrors _publish's claim-before-await first-wins).
        self._turns[turn_id] = terminal
        logger.info(
            "agent.turn.terminal: turn=%s state=%s reason=%s detail=%r (approval)",
            turn_id,
            terminal_state,
            no_reply_reason,
            detail,
        )
        try:
            await self._emit(turn_id, terminal)
        except Exception:
            logger.exception(
                "failed to emit terminal for turn=%s — the turn's audit row will be missing",
                turn_id,
            )
        return True

    def gate_tracker(self, turn_id: str) -> TerminalTracker:
        """A per-turn :class:`TerminalTracker` whose emit routes into this ledger.

        Lets Johnny-xpa compose :func:`run_gate` unchanged while the session-wide
        first-wins authority is the ledger — so the gate's ``no_reply`` and the
        reply done-callback's ``replied`` for the *same* turn id reconcile to a
        single terminal. Opens the turn as a side effect (gate entry is the
        moment we first own it). The tracker's own per-call flag is now redundant
        local defense; :meth:`_publish` is the authoritative chokepoint.
        """
        self.open(turn_id)

        async def _route(terminal: GateTerminal) -> None:
            await self._publish(turn_id, terminal)

        return TerminalTracker(_route, turn_id=turn_id)

    async def close(self) -> None:
        """Sweep every still-open or still-parked turn at session close.

        Belt-and-suspenders for the two ways a turn can outlive the session
        without a final terminal:

        * **open** (gate ran, reply handle lost) → fallback
          ``no_reply(stage_error)`` (the legacy unaccounted-turn drop).
        * **parked** (an approval round that never resolved — the coordinator's
          resolver task was cancelled at teardown, or the human never answered and
          the timeout was lost) → :meth:`resolve` to ``no_reply(approval_rejected)``
          so the parked turn settles instead of silently vanishing.

        Both lists are snapshotted before any ``await`` so a resolver racing the
        sweep reconciles via first-wins (the loser drops). In strict mode, raise
        afterwards if either list was non-empty so the gap is caught at source.
        Idempotent: turns already terminal are skipped.
        """
        stranded_open = self.open_turns
        stranded_parked = self.parked_turns
        for turn_id in stranded_open:
            logger.error(
                "agent.turn.terminal: UNACCOUNTED turn=%s at session close — "
                "emitting fallback no_reply terminal",
                turn_id,
            )
            await self.emit(
                turn_id,
                terminal_state="no_reply",
                no_reply_reason="stage_error",
                detail="session closed with the turn unaccounted for",
            )
        for turn_id in stranded_parked:
            logger.error(
                "agent.turn.terminal: PARKED turn=%s at session close — "
                "resolving approval_rejected (approval never settled)",
                turn_id,
            )
            await self.resolve(
                turn_id,
                terminal_state="no_reply",
                no_reply_reason="approval_rejected",
                detail="session closed with the approval round unresolved",
            )
        stranded = stranded_open + stranded_parked
        if self._strict and stranded:
            raise AssertionError(
                f"session closed with {len(stranded)} unaccounted turn(s): {', '.join(stranded)}"
            )


class TurnIndex:
    """Per-session map from the LiveKit turn id (``str``) to a stable ``int`` (Johnny-d5z).

    The LiveKit turn id is the user ``ChatMessage.id`` (``item_<shortuuid>``, a
    ``str``; spike Johnny-o3z), but the durable observability schema — the
    ``RouterDecisionMade`` / ``TurnTerminal`` / ``PipelineTiming`` events, the
    ``agent_decisions.turn_id`` / ``session_timings.turn_id`` columns, and the
    subscriber that binds a terminal to its decision row
    (``app.services.session_status_subscriber``) — is keyed by an **int** turn
    id (the legacy per-session utterance counter). The subscriber coerces a
    non-int ``turn_id`` to ``None``, which would orphan every terminal from its
    decision row and silently break decision↔terminal↔timing parity.

    This index closes that impedance gap: :meth:`resolve` assigns each distinct
    ``str`` turn id a monotonically increasing ``int`` on first sight and returns
    the same ``int`` for every later lookup, so all of a turn's events
    (``RouterDecisionMade`` at the gate, ``TurnTerminal`` at resolution,
    ``PipelineTiming`` from metrics) carry one identical ``int`` and the
    subscriber binds them to a single ``agent_decisions`` row — exactly the
    parity the serialised legacy pipeline got for free from its ``int``
    ``_utterance_count``.

    Stdlib-only and ``livekit``-free, like the rest of this module. One instance
    per session, shared by the gate (decision emit), the
    :data:`SessionTerminalEmitter` (terminal emit), and the metrics translator
    (timing emit), so they agree on the mapping. Single-threaded-loop safe: the
    assign is a plain dict write with no ``await``.
    """

    def __init__(self) -> None:
        self._ids: dict[str, int] = {}
        self._next = 1
        self._last = 0

    def resolve(self, turn_id: str) -> int:
        """Return ``turn_id``'s stable ``int``, assigning a fresh one on first sight.

        Idempotent: the same ``str`` always maps to the same ``int``. Updates the
        :meth:`last` high-water mark so timing events with no turn correlation
        (STT metrics carry no ``speech_id``) can attribute to the most recent
        turn, mirroring the legacy fallback to ``_utterance_count``.
        """
        existing = self._ids.get(turn_id)
        if existing is not None:
            self._last = existing
            return existing
        assigned = self._next
        self._next += 1
        self._ids[turn_id] = assigned
        self._last = assigned
        return assigned

    def get(self, turn_id: str) -> int | None:
        """The ``int`` for ``turn_id`` if already assigned, else ``None`` (no assign)."""
        return self._ids.get(turn_id)

    def last(self) -> int:
        """The most recently resolved ``int`` turn id (``0`` before any resolve).

        The attribution fallback for timing events that carry no turn
        correlation — the analogue of the legacy ``_emit_timing`` falling back to
        the latest ``_utterance_count`` when no response-loop turn id is set.
        """
        return self._last


async def _discard(task: asyncio.Task[Any]) -> None:
    """Cancel a child task we no longer need and await its teardown cleanly.

    Suppresses the *child's* cancellation/failure but re-raises if the
    ``CancelledError`` is *ours* (the outer task was cancelled while we waited),
    so external teardown is never swallowed.
    """
    if task.done():
        # Consume a stored exception so asyncio doesn't log "never retrieved".
        if not task.cancelled() and task.exception() is not None:
            pass
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        if not task.cancelled():
            raise  # the cancellation was delivered to *us*, not the child
    except Exception:
        pass  # the child failed during teardown — irrelevant now


async def run_router_call[T](
    router_call: Callable[[], Coroutine[Any, Any, T]],
    *,
    timeout_s: float | None = DEFAULT_ROUTER_GATE_TIMEOUT_S,
    abandon: asyncio.Event | None = None,
) -> tuple[RouterStatus, T | None]:
    """Run the router call bounded by ``timeout_s`` and racing ``abandon``.

    Ports ``voice_pipeline.pipeline._run_router``'s ``asyncio.wait_for`` bound
    and adds a cooperative barge-in race: ``abandon`` is an
    :class:`asyncio.Event` the fast-VAD interrupt path (Johnny-k8t) sets when
    the user resumes speaking mid-gate. Because the SDK never cancels the hook,
    this race is how a barge-in tears down the in-flight router *promptly*
    instead of making the next turn wait out the full timeout.

    Returns ``(OK, decision)`` when the router wins; ``(ABANDONED, None)`` when
    ``abandon`` wins; ``(TIMED_OUT, None)`` when neither resolves in time. In
    the ABANDONED/TIMED_OUT cases the in-flight router task is cancelled and
    awaited so the provider HTTP/subprocess is torn down cleanly. A router
    exception propagates (the caller maps it to ``stage_error``).

    ``timeout_s <= 0`` (or ``None``) disables the wall-clock bound but keeps the
    abandon race.
    """
    router_task: asyncio.Task[T] = asyncio.ensure_future(router_call())
    abandon_task: asyncio.Task[bool] | None = (
        asyncio.ensure_future(abandon.wait()) if abandon is not None else None
    )
    waiters: set[asyncio.Task[Any]] = {router_task}
    if abandon_task is not None:
        waiters.add(abandon_task)
    timeout = timeout_s if timeout_s and timeout_s > 0 else None

    try:
        done, _pending = await asyncio.wait(
            waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
    except asyncio.CancelledError:
        # External teardown: tear down both children, then propagate so the
        # caller's ensure_terminal() fallback can fire.
        await _discard(router_task)
        if abandon_task is not None:
            await _discard(abandon_task)
        raise

    if router_task in done:
        if abandon_task is not None:
            await _discard(abandon_task)
        return RouterStatus.OK, router_task.result()

    # Timeout or barge-in: the router is still in flight — cancel it cleanly.
    await _discard(router_task)
    if abandon_task is not None and abandon_task in done:
        return RouterStatus.ABANDONED, None
    if abandon_task is not None:
        await _discard(abandon_task)
    return RouterStatus.TIMED_OUT, None


async def run_gate[T](
    router_call: Callable[[], Coroutine[Any, Any, T]],
    *,
    tracker: TerminalTracker,
    timeout_s: float | None = DEFAULT_ROUTER_GATE_TIMEOUT_S,
    abandon: asyncio.Event | None = None,
) -> tuple[GateAction, T | None]:
    """Run the should-speak gate's bounded router call with INV-1 terminals.

    Composes :func:`run_router_call` with ``tracker`` so every *silent* exit
    leaves exactly one terminal:

    * **timeout** → ``no_reply(stage_error)`` → ``STAY_SILENT``
    * **barge-in** (``abandon`` set) → ``no_reply(barge_in)`` → ``STAY_SILENT``
    * **router raised** → ``no_reply(stage_error)`` → ``STAY_SILENT``
    * **cancelled** (outer task teardown) → best-effort ``no_reply(barge_in)``
      via :meth:`TerminalTracker.ensure_terminal`, then ``CancelledError`` is
      re-raised (cooperative cancellation is never swallowed)
    * **router approved** → ``SPEAK`` with the decision and **no terminal**
      (the caller owns the decline / speak terminals)

    The caller maps ``STAY_SILENT`` → ``raise StopResponse`` in the real hook.
    """
    try:
        status, decision = await run_router_call(router_call, timeout_s=timeout_s, abandon=abandon)
    except asyncio.CancelledError as exc:
        # Outer task cancelled (hard teardown). Emit a best-effort terminal,
        # then never swallow the cancellation.
        await tracker.ensure_terminal(exc=exc)
        raise
    except Exception as exc:
        # The router itself raised within the bound (provider error). Mirror
        # the legacy stage_error terminal; stay silent rather than letting the
        # SDK swallow it audit-less.
        await tracker.emit(
            terminal_state="no_reply",
            no_reply_reason="stage_error",
            detail=f"{type(exc).__name__}: {exc}",
        )
        return GateAction.STAY_SILENT, None

    if status is RouterStatus.TIMED_OUT:
        bound = "disabled" if not timeout_s or timeout_s <= 0 else f"{timeout_s:.1f}s"
        await tracker.emit(
            terminal_state="no_reply",
            no_reply_reason="stage_error",
            detail=f"router exceeded the {bound} gate bound",
        )
        return GateAction.STAY_SILENT, None

    if status is RouterStatus.ABANDONED:
        await tracker.emit(
            terminal_state="no_reply",
            no_reply_reason="barge_in",
            detail="user resumed speaking before the gate returned",
        )
        return GateAction.STAY_SILENT, None

    return GateAction.SPEAK, decision


__all__ = [
    "DEFAULT_ROUTER_GATE_TIMEOUT_S",
    "GateAction",
    "GateNoReplyReason",
    "GateTerminal",
    "GateTerminalState",
    "RouterStatus",
    "SessionTerminalEmitter",
    "TerminalEmitter",
    "TerminalTracker",
    "TurnIndex",
    "TurnLedger",
    "TurnNoReplyReason",
    "run_gate",
    "run_router_call",
]
