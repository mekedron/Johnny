"""Out-of-band approval-required orchestration (spike Johnny-z97, Phase 2).

``approval_required`` mode holds the bot's reply until a human approves it. In
the legacy split engine that wait lived *inline* in
``_handle_approval_required``: the serialised response loop emitted
``ApprovalPending``, **blocked** on ``ApprovalGate.request_approval`` for up to
``approval_timeout_seconds`` (~15 s), then ran the answer LLM + TTS on approve or
logged a rejection. Blocking was safe there because exactly one turn was ever in
flight.

That does **not** survive the port to LiveKit Agents. ``Agent.on_user_turn_completed``
is a *blocking* hook the SDK ``await``\\s before generating any reply, and — verified
against ``livekit-agents==1.5.17`` (``voice/agent_activity.py``) — a newer turn's
``_user_turn_completed_task`` literally ``await``\\s the older hook ("We never cancel
user code"). So a gate that blocked ~15 s on a human would **head-of-line-stall every
subsequent turn** for that whole window while VAD keeps firing. Getting this wrong
forces a Phase-2 rewrite, so this module establishes the out-of-band flow the
approval *build* (Johnny-qzj) wires up:

1. **In the gate** (``on_user_turn_completed``), once the router approves and the
   confidence / rate-limit checks pass, the approval branch calls
   :meth:`ApprovalCoordinator.begin` and raises ``StopResponse`` **immediately**.
   ``begin`` :meth:`~johnny.agent.gate.TurnLedger.park`\\s the turn (a non-final
   ``pending_approval`` marker), spawns the resolver task, and returns synchronously —
   so the hook returns at once and later turns never wait on the human.
2. **Out of band** (the resolver task), :meth:`request_approval` is awaited — *this*
   is where the ~15 s wait happens, off the turn loop. On ``approved`` the coordinator
   calls :meth:`generate_reply` (the injected ``session.generate_reply`` wrapper) to
   speak the reply; on ``rejected`` / ``timeout`` it stays silent.
3. **Exactly one final terminal** lands via
   :meth:`~johnny.agent.gate.TurnLedger.resolve` (INV-1, spike Johnny-o3z) — never
   ``pending_approval`` itself (legacy parity: the durable terminal is the
   *resolution*, ``replied`` / ``no_reply(approval_rejected)`` / etc.). The
   ``ApprovalPending`` / ``ApprovalResolved`` events are emitted via injected hooks
   (the live-UI / browser-push surface; observability parity is Johnny-d5z).

Like :mod:`johnny.agent.gate`, this module is deliberately ``livekit``-free and
``sqlalchemy``-free (stdlib only): the human-approval source, the reply generation,
and the event emission are all injected as callables, so ``import
johnny.agent.approval`` stays cheap and the unit tests run without the ``agent``
extra. Johnny-qzj supplies the real wiring:

* :data:`RequestApproval` ← ``app.services.approval.RedisApprovalGate.request_approval``
  (build an ``ApprovalRequest`` from the :class:`ApprovalRound` and await it);
* :data:`GenerateReply` ← a wrapper that calls ``session.generate_reply(...)``,
  awaits the returned ``SpeechHandle``, and maps ``interrupted`` / empty
  ``chat_items`` / spoke → :class:`ReplyOutcome`. The coordinator **owns** that
  handle, so the approval reply must NOT also be bound by the gate's shared
  ``speech_created`` FIFO listener (``JohnnyAgent.on_enter`` → ``RouterGate.bind_reply``)
  — it would mis-attribute the approval reply's completion to an unrelated pending
  SPEAK turn. Recommended fix in qzj: register the approval handle's id in a
  coordinator-owned set the listener early-returns on (the handle is returned
  synchronously by ``generate_reply``, before the ``speech_created`` callback is
  dispatched), and never push the approval turn onto ``_pending_speak_turns``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from johnny.agent.gate import TurnLedger, TurnNoReplyReason

logger = logging.getLogger(__name__)

# Ported from the legacy split engine (US-027).
DEFAULT_APPROVAL_TIMEOUT_S = 15.0

# Defensive outer bound added on top of the approval source's own timeout, so a
# source that hangs (a wedged Redis pubsub) can never park a turn forever. The
# source is expected to return ``timeout`` at ``timeout_s``; this grace only fires
# if it fails to.
DEFAULT_TIMEOUT_GRACE_S = 5.0

ApprovalDecision = Literal["approved", "rejected", "timeout"]
"""The outcome of one approval round. Mirror of
``voice_pipeline.events.ApprovalResolution`` / ``voice_pipeline.approval.ApprovalOutcome``
(kept stdlib-only here; a drift-guard test asserts equality). ``timeout`` is kept
distinct from ``rejected`` for the ``ApprovalResolved`` audit even though both
suppress the bot (the turn's terminal is ``no_reply(approval_rejected)`` either way)."""


@dataclass(frozen=True, slots=True)
class ApprovalRound:
    """Everything one approval round needs — the unit :meth:`ApprovalCoordinator.begin`
    takes.

    ``turn_id`` is the LiveKit turn id (the user ``ChatMessage.id``) the ledger keys
    on. ``decision_id`` is the ``agent_decisions`` row id (persisted ``pending`` by
    the caller) the live UI / push notification correlate on. The rest mirror the
    fields ``voice_pipeline.events.ApprovalPending`` carries.
    """

    turn_id: str
    decision_id: int
    suggested_reply: str
    timeout_s: float = DEFAULT_APPROVAL_TIMEOUT_S
    reason: str = ""
    reply_type: str | None = None


@dataclass(frozen=True, slots=True)
class ReplyOutcome:
    """What the out-of-band ``generate_reply`` produced on the approve path.

    ``spoke=True`` → the reply played audio → terminal ``replied``. ``spoke=False``
    → the reply produced nothing (``model_empty_output``) or was interrupted
    mid-play (``barge_in``); ``no_reply_reason`` carries which, mapped from the
    ``SpeechHandle`` (``interrupted`` / empty ``chat_items``) by the Johnny-qzj
    wrapper.
    """

    spoke: bool
    no_reply_reason: TurnNoReplyReason | None = None
    detail: str = ""


# Injected dependencies (stdlib-only seam; Johnny-qzj supplies the real ones).
RequestApproval = Callable[[ApprovalRound], Awaitable[ApprovalDecision]]
"""Await the human's approve/reject for this round (or ``timeout``). Wraps the
Redis-backed ``ApprovalGate.request_approval`` in production."""

GenerateReply = Callable[[ApprovalRound], Awaitable[ReplyOutcome]]
"""Speak the approved reply out of band and report what happened. Wraps
``session.generate_reply(...)`` + awaiting the resulting ``SpeechHandle``."""

PendingHook = Callable[[ApprovalRound], Awaitable[None]]
"""Emit ``ApprovalPending`` (+ any DB bookkeeping) for the live UI / push."""

ResolvedHook = Callable[[ApprovalRound, ApprovalDecision], Awaitable[None]]
"""Emit ``ApprovalResolved`` (+ flip the decision row) once the round settles."""


class ApprovalCoordinator:
    """Drives ``approval_required`` rounds out of band so the gate never blocks.

    Construct one per session with the session :class:`~johnny.agent.gate.TurnLedger`
    and the injected approval source / reply generator / event hooks. The gate calls
    :meth:`begin` (non-blocking) and raises ``StopResponse``; the coordinator's spawned
    resolver task carries the round to its single final terminal.
    """

    def __init__(
        self,
        ledger: TurnLedger,
        *,
        request_approval: RequestApproval,
        generate_reply: GenerateReply,
        on_pending: PendingHook | None = None,
        on_resolved: ResolvedHook | None = None,
        timeout_grace_s: float = DEFAULT_TIMEOUT_GRACE_S,
    ) -> None:
        self._ledger = ledger
        self._request_approval = request_approval
        self._generate_reply = generate_reply
        self._on_pending = on_pending
        self._on_resolved = on_resolved
        self._timeout_grace_s = timeout_grace_s
        # Strong refs to in-flight resolver tasks so they aren't GC'd mid-flight
        # (and to avoid "task exception never retrieved" warnings); also lets
        # aclose() drain them at teardown.
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ #
    # The non-blocking entry point (called from the gate)                #
    # ------------------------------------------------------------------ #

    def begin(self, round: ApprovalRound) -> asyncio.Task[None] | None:
        """Park the turn and spawn its resolver — **synchronous, non-blocking**.

        This is the whole point of the spike: ``begin`` does no ``await``. It
        :meth:`~johnny.agent.gate.TurnLedger.park`\\s the turn (so the close sweep and
        any stray reply ``emit`` cannot clobber it) and schedules :meth:`_run` on the
        loop, then returns. The caller (the gate) raises ``StopResponse`` right after,
        so the hook returns at once and the SDK's await-chained later turns are never
        held up by this round's human wait.

        Returns the spawned resolver :class:`asyncio.Task` (handy for tests /
        teardown), or ``None`` if the turn could not be parked (already parked or
        already terminal — a double-begin or a turn the gate already accounted for).
        """
        if not self._ledger.park(round.turn_id, detail="awaiting approval"):
            logger.error(
                "approval.begin: turn=%s not parkable — skipping approval round",
                round.turn_id,
            )
            return None
        task = asyncio.ensure_future(self._run(round))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def aclose(self) -> None:
        """Cancel and drain any in-flight resolver tasks (best-effort teardown).

        A cancelled resolver settles its parked turn to ``no_reply(approval_rejected)``
        on the way out (see :meth:`_run`); whatever it misses, the ledger's
        :meth:`~johnny.agent.gate.TurnLedger.close` sweep force-resolves. Safe to call
        more than once.
        """
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - defensive
                logger.exception("approval resolver task failed during aclose")

    # ------------------------------------------------------------------ #
    # The out-of-band resolver                                           #
    # ------------------------------------------------------------------ #

    async def _run(self, round: ApprovalRound) -> None:
        """Carry one parked round to its single final terminal, off the turn loop."""
        await self._safe_pending(round)

        try:
            decision = await self._await_decision(round)
        except asyncio.CancelledError:
            # Teardown (aclose / session close). Settle the parked turn so it is not
            # left dangling, then never swallow the cancellation.
            await self._ledger.resolve(
                round.turn_id,
                terminal_state="no_reply",
                no_reply_reason="approval_rejected",
                detail="approval round cancelled before resolution",
            )
            raise
        except Exception as exc:
            # The approval source itself errored — stay silent with a stage_error
            # terminal, and report the round resolved-as-rejected to subscribers.
            logger.exception(
                "approval.run: source errored for turn=%s — rejecting",
                round.turn_id,
            )
            await self._ledger.resolve(
                round.turn_id,
                terminal_state="no_reply",
                no_reply_reason="stage_error",
                detail=f"approval source error: {type(exc).__name__}: {exc}",
            )
            await self._safe_resolved(round, "rejected")
            return

        if decision == "approved":
            resolution = await self._handle_approved(round)
        else:
            detail = (
                f"approval timed out after {round.timeout_s:.1f}s"
                if decision == "timeout"
                else "approval rejected by operator"
            )
            await self._ledger.resolve(
                round.turn_id,
                terminal_state="no_reply",
                no_reply_reason="approval_rejected",
                detail=detail,
            )
            resolution = decision

        await self._safe_resolved(round, resolution)

    async def _await_decision(self, round: ApprovalRound) -> ApprovalDecision:
        """Await the human decision, bounded defensively.

        The injected source is expected to enforce ``timeout_s`` itself and return
        ``timeout`` (legacy parity); the outer :func:`asyncio.wait_for` only guards a
        source that *fails* to, so a wedged source can never strand the parked turn.
        """
        bound = max(0.1, round.timeout_s) + self._timeout_grace_s
        try:
            return await asyncio.wait_for(self._request_approval(round), timeout=bound)
        except TimeoutError:
            logger.warning(
                "approval.run: source exceeded the %.1fs defensive bound for "
                "turn=%s — treating as timeout",
                bound,
                round.turn_id,
            )
            return "timeout"

    async def _handle_approved(self, round: ApprovalRound) -> ApprovalDecision:
        """Speak the approved reply out of band; resolve the turn's terminal.

        Returns the *effective* resolution for the ``ApprovalResolved`` event:
        ``approved`` when the reply actually spoke, ``rejected`` when the approved
        reply produced nothing / errored (legacy ``_handle_approval_required`` parity:
        an approved-but-empty answer is reported rejected).
        """
        try:
            outcome = await self._generate_reply(round)
        except asyncio.CancelledError:
            await self._ledger.resolve(
                round.turn_id,
                terminal_state="no_reply",
                no_reply_reason="barge_in",
                detail="approved reply cancelled before completion",
            )
            raise
        except Exception as exc:
            logger.exception(
                "approval.run: approved reply errored for turn=%s",
                round.turn_id,
            )
            await self._ledger.resolve(
                round.turn_id,
                terminal_state="no_reply",
                no_reply_reason="stage_error",
                detail=f"approved reply error: {type(exc).__name__}: {exc}",
            )
            return "rejected"

        if outcome.spoke:
            await self._ledger.resolve(
                round.turn_id,
                terminal_state="replied",
                detail=outcome.detail or "approved and spoke",
            )
            return "approved"

        await self._ledger.resolve(
            round.turn_id,
            terminal_state="no_reply",
            no_reply_reason=outcome.no_reply_reason or "model_empty_output",
            detail=outcome.detail or "approved but the reply produced nothing",
        )
        return "rejected"

    # ------------------------------------------------------------------ #
    # Event hooks (best-effort; a failing bus never crashes the round)   #
    # ------------------------------------------------------------------ #

    async def _safe_pending(self, round: ApprovalRound) -> None:
        if self._on_pending is None:
            return
        try:
            await self._on_pending(round)
        except Exception:
            logger.exception("approval.run: on_pending hook failed for turn=%s", round.turn_id)

    async def _safe_resolved(self, round: ApprovalRound, resolution: ApprovalDecision) -> None:
        if self._on_resolved is None:
            return
        try:
            await self._on_resolved(round, resolution)
        except Exception:
            logger.exception("approval.run: on_resolved hook failed for turn=%s", round.turn_id)


__all__ = [
    "DEFAULT_APPROVAL_TIMEOUT_S",
    "DEFAULT_TIMEOUT_GRACE_S",
    "ApprovalCoordinator",
    "ApprovalDecision",
    "ApprovalRound",
    "GenerateReply",
    "PendingHook",
    "ReplyOutcome",
    "RequestApproval",
    "ResolvedHook",
]
