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

This module is deliberately ``livekit``-free and ``sqlalchemy``-free (stdlib
only) so ``import johnny.agent.gate`` stays cheap and safe, mirroring the
adapter package's import-safety discipline. The real ``TurnTerminal`` →
``EventBus`` → ``agent_decisions`` wiring is injected as a
:data:`TerminalEmitter` callback (event/observability parity is Johnny-d5z).
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
"""The ``no_reply`` sub-reasons reachable from the gate (subset of
events.NoReplyReason): ``router_declined``/``low_confidence`` (decision paths,
emitted by the caller), ``barge_in`` (abandoned / cancelled mid-gate),
``stage_error`` (router timeout, router raised, or an unaccounted exit)."""


@dataclass(frozen=True, slots=True)
class GateTerminal:
    """The single terminal an :class:`TerminalTracker` emits for one turn.

    The :data:`TerminalEmitter` maps this onto the durable
    ``voice_pipeline.events.TurnTerminal`` → ``EventBus`` → ``agent_decisions``
    row (Johnny-d5z). Carrying just the three audit fields keeps the harness
    decoupled from the event/DB layer.
    """

    terminal_state: GateTerminalState
    no_reply_reason: GateNoReplyReason | None
    detail: str


# Injected by the caller: persist/publish the turn's terminal audit row.
TerminalEmitter = Callable[[GateTerminal], Awaitable[None]]


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
        turn_id: int,
        strict: bool = False,
    ) -> None:
        self._emit = emit
        self.turn_id = turn_id
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
        no_reply_reason: GateNoReplyReason | None = None,
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
                "failed to emit terminal for turn=%s — the turn's audit row "
                "will be missing",
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
        await self.emit(
            terminal_state="no_reply", no_reply_reason=reason, detail=detail
        )
        if self._strict and not isinstance(exc, asyncio.CancelledError):
            raise AssertionError(
                f"turn {self.turn_id} exited the gate without a terminal: {detail}"
            )


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
        status, decision = await run_router_call(
            router_call, timeout_s=timeout_s, abandon=abandon
        )
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
    "TerminalEmitter",
    "TerminalTracker",
    "run_gate",
    "run_router_call",
]
