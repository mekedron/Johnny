"""Production wiring for ``approval_required`` mode (Johnny-qzj — build of Johnny-z97).

The spike (:mod:`johnny.agent.approval`) shipped the
:class:`~johnny.agent.approval.ApprovalCoordinator` with every I/O boundary
injected as a callable, so it stays ``livekit``-/``sqlalchemy``-free and unit
tests run without the ``agent`` extra. This module supplies the **real** ones for
the agent worker (Johnny-9eh):

* :func:`build_request_approval` — adapt the Redis-backed
  :class:`~johnny.voice_pipeline.approval.ApprovalGate` (the legacy approval
  source, reused unchanged) to the coordinator's
  :data:`~johnny.agent.approval.RequestApproval` seam;
* :func:`build_generate_reply` — speak the approved reply out of band via
  ``AgentSession.generate_reply`` and map the resulting ``SpeechHandle`` to a
  :class:`~johnny.agent.approval.ReplyOutcome`, registering the handle with the
  gate so the shared ``speech_created`` listener doesn't mis-bind it (§7.3);
* :func:`build_approval_event_hooks` — publish ``ApprovalPending`` /
  ``ApprovalResolved`` on the :class:`~johnny.voice_pipeline.event_bus.EventBus`
  and flip the ``agent_decisions`` row (``pending`` → ``spoken`` / ``rejected``),
  legacy ``_handle_approval_required`` parity;
* :func:`build_persist_pending_decision` — persist the ``pending`` decision row
  the round correlates on (so the live UI can refer to it by ``decision_id``);
* :func:`build_approval_coordinator` — the single factory the worker calls after
  it has the ``AgentSession`` + ``TurnLedger`` + ``RouterGate``: it wires the
  four seams, builds the coordinator, and attaches it to the gate.

Imported only by the full-stack worker / api / tests — it reaches into
``johnny.voice_pipeline`` (events, event_bus, decision_sink, approval) and the
``livekit``-importing :mod:`johnny.agent.router_gate`, so it is never pulled from
the import-safe top-level :mod:`johnny.agent` package.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from johnny.agent.approval import (
    ApprovalCoordinator,
    ApprovalDecision,
    ApprovalRound,
    GenerateReply,
    PendingHook,
    ReplyOutcome,
    RequestApproval,
    ResolvedHook,
)
from johnny.agent.gate import TurnLedger
from johnny.agent.router_gate import PersistPendingDecision, RouterGate
from johnny.voice_pipeline.approval import ApprovalGate, ApprovalRequest
from johnny.voice_pipeline.decision_sink import DecisionOutcome, DecisionSink
from johnny.voice_pipeline.event_bus import EventBus
from johnny.voice_pipeline.events import (
    ApprovalPending,
    ApprovalResolved,
    RouterDecisionMade,
)
from johnny.voice_pipeline.reasoning import RouterDecision

if TYPE_CHECKING:
    from livekit.agents.voice import AgentSession

logger = logging.getLogger(__name__)


def _default_clock_ms() -> int:
    """Epoch milliseconds — the timestamp shape the pipeline events carry."""
    return int(time.time() * 1000)


def build_request_approval(gate: ApprovalGate, *, session_id: str | None = None) -> RequestApproval:
    """Adapt an :class:`ApprovalGate` to the coordinator's approval source.

    Builds an :class:`ApprovalRequest` from the :class:`ApprovalRound` — carrying
    the round's ``timeout_s`` so the configurable ``approval_timeout_seconds`` is
    honoured by the source itself (legacy parity) — and awaits the gate. The
    production gate is ``app.services.approval.RedisApprovalGate``; it returns
    ``timeout`` when no operator answers in time. ``ApprovalOutcome`` ≡
    ``ApprovalDecision`` (both ``Literal["approved", "rejected", "timeout"]``,
    drift-guarded in the spike tests).
    """

    async def _request(approval_round: ApprovalRound) -> ApprovalDecision:
        return await gate.request_approval(
            ApprovalRequest(
                decision_id=approval_round.decision_id,
                suggested_reply=approval_round.suggested_reply,
                timeout_s=approval_round.timeout_s,
                session_id=session_id,
            )
        )

    return _request


def build_generate_reply(session: AgentSession[Any], router_gate: RouterGate) -> GenerateReply:
    """Speak the approved reply out of band via ``AgentSession.generate_reply``.

    Registers the returned ``SpeechHandle`` id with the gate *before* awaiting it
    (Johnny-z97 §7.3) so the shared ``speech_created`` listener
    (:meth:`RouterGate.bind_reply`) recognises and skips it instead of binding it
    to a pending SPEAK turn. The non-empty ``suggested_reply`` steers the reply as
    ``instructions`` (the operator approved that text); awaiting the handle is safe
    here — this runs in the coordinator's out-of-band task, not the turn hook. The
    outcome maps to :class:`ReplyOutcome`: interrupted → ``barge_in``; no chat
    items → ``model_empty_output``; otherwise spoke.
    """

    async def _generate(approval_round: ApprovalRound) -> ReplyOutcome:
        suggested = approval_round.suggested_reply.strip()
        if suggested:
            handle = session.generate_reply(instructions=suggested)
        else:
            handle = session.generate_reply()
        router_gate.register_approval_reply(handle.id)
        await handle
        if handle.interrupted:
            return ReplyOutcome(
                spoke=False,
                no_reply_reason="barge_in",
                detail="approved reply interrupted before completion",
            )
        if not handle.chat_items:
            return ReplyOutcome(
                spoke=False,
                no_reply_reason="model_empty_output",
                detail="approved reply produced no assistant output",
            )
        return ReplyOutcome(spoke=True, detail="approved and spoke")

    return _generate


def build_approval_event_hooks(
    event_bus: EventBus,
    decision_sink: DecisionSink,
    *,
    session_id: str | None = None,
    clock: Callable[[], int] = _default_clock_ms,
) -> tuple[PendingHook, ResolvedHook]:
    """Build the ``ApprovalPending`` / ``ApprovalResolved`` hooks + decision flip.

    ``on_pending`` publishes :class:`ApprovalPending` — the live-UI / browser-push
    signal for the parked state (the ``agent_decisions`` row was already persisted
    ``pending`` by the gate before the turn parked). ``on_resolved`` flips that row
    (``spoken`` when the reply actually spoke, ``rejected`` on reject / timeout /
    approved-but-empty — legacy ``_handle_approval_required`` parity, since the
    coordinator reports the *effective* resolution) and publishes
    :class:`ApprovalResolved` so subscribers clear their UI.
    """

    async def on_pending(approval_round: ApprovalRound) -> None:
        await event_bus.publish(
            ApprovalPending(
                decision_id=approval_round.decision_id,
                suggested_reply=approval_round.suggested_reply,
                timestamp_ms=clock(),
                timeout_s=approval_round.timeout_s,
                reason=approval_round.reason,
                reply_type=approval_round.reply_type,
                session_id=session_id,
            )
        )

    async def on_resolved(approval_round: ApprovalRound, resolution: ApprovalDecision) -> None:
        outcome: DecisionOutcome = "spoken" if resolution == "approved" else "rejected"
        await decision_sink.update_outcome(approval_round.decision_id, outcome)
        await event_bus.publish(
            ApprovalResolved(
                decision_id=approval_round.decision_id,
                resolution=resolution,
                timestamp_ms=clock(),
                session_id=session_id,
            )
        )

    return on_pending, on_resolved


def build_persist_pending_decision(
    decision_sink: DecisionSink,
    *,
    session_id: str | None = None,
    bot_session_id: int | None = None,
    clock: Callable[[], int] = _default_clock_ms,
) -> PersistPendingDecision:
    """Persist the ``pending`` ``agent_decisions`` row for an approval turn.

    Mirrors ``the legacy split pipeline._persist_decision(..., "pending")``: records a
    :class:`RouterDecisionMade` with ``outcome="pending"`` and returns the row id
    the :class:`ApprovalRound` / UI correlate on. Swallows sink failures (returns
    ``None`` → the gate rejects the turn rather than crashing the turn hook). Wired
    onto the gate at construction (``persist_pending_decision=``), separate from the
    coordinator factory because the id must exist *before* the turn is parked.
    """

    async def _persist(decision: RouterDecision, turn_id: str) -> int | None:
        event = RouterDecisionMade(
            should_speak=decision.should_speak,
            confidence=decision.confidence,
            reason=decision.reason,
            timestamp_ms=clock(),
            reply_type=decision.reply_type,
            suggested_reply=decision.suggested_reply,
            session_id=session_id,
        )
        try:
            return await decision_sink.record(
                event, outcome="pending", bot_session_id=bot_session_id
            )
        except Exception:
            logger.exception("approval: pending decision persist failed for turn=%s", turn_id)
            return None

    return _persist


def build_approval_coordinator(
    *,
    ledger: TurnLedger,
    router_gate: RouterGate,
    session: AgentSession[Any],
    approval_gate: ApprovalGate,
    event_bus: EventBus,
    decision_sink: DecisionSink,
    session_id: str | None = None,
    clock: Callable[[], int] = _default_clock_ms,
) -> ApprovalCoordinator:
    """Assemble the production :class:`ApprovalCoordinator` and attach it to the gate.

    The single entry point the agent worker (Johnny-9eh) calls after it has the
    ``AgentSession``, the ``TurnLedger``, and the ``RouterGate``. Wires the Redis
    approval source, the ``generate_reply`` wrapper, and the event hooks, then
    :meth:`RouterGate.attach_approval`-es the coordinator so the gate's approval
    branch can drive it. Returns the coordinator (call
    :meth:`RouterGate.aclose` — or :meth:`ApprovalCoordinator.aclose` — to drain
    in-flight resolvers at teardown).

    The gate's ``persist_pending_decision`` is wired separately at gate
    construction (see :func:`build_persist_pending_decision`): the ``decision_id``
    must exist *before* the round is parked, so it cannot be deferred to here.
    """
    on_pending, on_resolved = build_approval_event_hooks(
        event_bus, decision_sink, session_id=session_id, clock=clock
    )
    coordinator = ApprovalCoordinator(
        ledger,
        request_approval=build_request_approval(approval_gate, session_id=session_id),
        generate_reply=build_generate_reply(session, router_gate),
        on_pending=on_pending,
        on_resolved=on_resolved,
    )
    router_gate.attach_approval(coordinator)
    return coordinator


__all__ = [
    "build_approval_coordinator",
    "build_approval_event_hooks",
    "build_generate_reply",
    "build_persist_pending_decision",
    "build_request_approval",
]
