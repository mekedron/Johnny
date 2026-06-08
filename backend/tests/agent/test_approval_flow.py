"""Out-of-band approval-required flow tests (spike Johnny-z97, Phase 2).

Proves the design that lets ``approval_required`` mode survive the LiveKit-Agents
port: the ~15 s human-in-the-loop wait runs **off** the ``on_user_turn_completed``
turn loop, so later turns never head-of-line-block while an approval is pending, and
every approval turn still resolves to **exactly one** final terminal (INV-1).

Coverage maps to the spike acceptance:

* :meth:`ApprovalCoordinator.begin` is synchronous/non-blocking — it parks the turn
  and spawns a resolver, then returns;
* with the SDK's await-chained turn hooks faithfully simulated, later turns resolve
  while an approval is parked (the no-stall proof);
* approve → ``session.generate_reply`` runs out of band → terminal ``replied``;
* reject / timeout → terminal ``no_reply(approval_rejected)``, the bot stays silent;
* approved-but-empty / approved-but-interrupted / approved-but-errored map to the
  right ``no_reply`` reasons and report the round rejected;
* the :class:`TurnLedger` park/resolve mechanics (park is non-final, resolve is
  first-wins-once, ``emit`` cannot clobber a parked turn, ``close`` force-resolves a
  stranded parked turn) hold under concurrency.

``johnny.agent.approval`` and ``johnny.agent.gate`` are ``livekit``-free, so these
tests collect and run without the ``agent`` extra.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable

import pytest

from johnny.agent.approval import (
    ApprovalCoordinator,
    ApprovalDecision,
    ApprovalRound,
    ReplyOutcome,
)
from johnny.agent.gate import GateTerminal, TurnLedger

# --------------------------------------------------------------------------- #
# Test doubles                                                                #
# --------------------------------------------------------------------------- #


class RecordingSessionEmitter:
    """A :data:`SessionTerminalEmitter` recording every published terminal.

    ``yield_first=True`` awaits ``asyncio.sleep(0)`` before recording so gathered
    emits genuinely interleave at the suspension point — the ledger's claim happens
    *before* this await, so a racing second publish must still be dropped.
    """

    def __init__(self, *, yield_first: bool = False) -> None:
        self.records: list[tuple[str, GateTerminal]] = []
        self._yield_first = yield_first

    async def __call__(self, turn_id: str, terminal: GateTerminal) -> None:
        if self._yield_first:
            await asyncio.sleep(0)
        self.records.append((turn_id, terminal))

    @property
    def turn_ids(self) -> list[str]:
        return [tid for tid, _ in self.records]

    def terminal(self, turn_id: str) -> GateTerminal:
        for tid, term in self.records:
            if tid == turn_id:
                return term
        raise KeyError(turn_id)


class ScriptedApproval:
    """Injectable :data:`RequestApproval`: per-turn immediate or blocking outcomes."""

    def __init__(self) -> None:
        self._immediate: dict[str, ApprovalDecision] = {}
        self._futures: dict[str, asyncio.Future[ApprovalDecision]] = {}
        self.calls: list[str] = []

    def immediate(self, turn_id: str, decision: ApprovalDecision) -> None:
        self._immediate[turn_id] = decision

    def arm(self, turn_id: str) -> asyncio.Future[ApprovalDecision]:
        """Block this turn's wait until the returned future is set (or never)."""
        fut: asyncio.Future[ApprovalDecision] = asyncio.get_running_loop().create_future()
        self._futures[turn_id] = fut
        return fut

    async def __call__(self, round: ApprovalRound) -> ApprovalDecision:
        self.calls.append(round.turn_id)
        if round.turn_id in self._immediate:
            return self._immediate[round.turn_id]
        fut = self._futures.get(round.turn_id)
        if fut is not None:
            return await fut
        return "timeout"


class FakeReply:
    """Injectable :data:`GenerateReply`: returns a scripted outcome or raises."""

    def __init__(
        self,
        outcome: ReplyOutcome | None = None,
        *,
        raises: BaseException | None = None,
        block: asyncio.Future[None] | None = None,
    ) -> None:
        self.outcome = outcome if outcome is not None else ReplyOutcome(spoke=True)
        self.raises = raises
        self.block = block
        self.calls: list[str] = []

    async def __call__(self, round: ApprovalRound) -> ReplyOutcome:
        self.calls.append(round.turn_id)
        if self.block is not None:
            await self.block
        if self.raises is not None:
            raise self.raises
        return self.outcome


class EventSink:
    """Records the ApprovalPending / ApprovalResolved hook calls."""

    def __init__(self) -> None:
        self.pending: list[str] = []
        self.resolved: list[tuple[str, ApprovalDecision]] = []

    async def on_pending(self, round: ApprovalRound) -> None:
        self.pending.append(round.turn_id)

    async def on_resolved(self, round: ApprovalRound, resolution: ApprovalDecision) -> None:
        self.resolved.append((round.turn_id, resolution))


def _round(
    turn_id: str,
    *,
    decision_id: int = 1,
    suggested_reply: str = "the suggested answer",
    timeout_s: float = 30.0,
    reason: str = "",
    reply_type: str | None = None,
) -> ApprovalRound:
    return ApprovalRound(
        turn_id=turn_id,
        decision_id=decision_id,
        suggested_reply=suggested_reply,
        timeout_s=timeout_s,
        reason=reason,
        reply_type=reply_type,
    )


async def _run_round(coord: ApprovalCoordinator, round: ApprovalRound) -> None:
    """begin() + await the spawned resolver (asserting the turn was parkable)."""
    task = coord.begin(round)
    assert task is not None
    await task


def _coordinator(
    ledger: TurnLedger,
    approval: ScriptedApproval,
    reply: FakeReply,
    events: EventSink,
    *,
    timeout_grace_s: float = 5.0,
) -> ApprovalCoordinator:
    return ApprovalCoordinator(
        ledger,
        request_approval=approval,
        generate_reply=reply,
        on_pending=events.on_pending,
        on_resolved=events.on_resolved,
        timeout_grace_s=timeout_grace_s,
    )


async def _await_chained_hooks(
    hook_bodies: list[Callable[[], Awaitable[None]]],
) -> None:
    """Faithfully simulate the SDK's await-chained ``_user_turn_completed_task``.

    livekit-agents serialises end-of-turn hooks: turn N+1's task ``await``\\s turn
    N's before running ("We never cancel user code"). We model that exactly —
    each hook task awaits the previous one — so a hook body that blocked would
    stall the whole chain. The chain advancing *is* the no-stall proof.
    """
    prev: asyncio.Task[None] | None = None
    tasks: list[asyncio.Task[None]] = []
    for body in hook_bodies:

        async def run(
            body: Callable[[], Awaitable[None]] = body,
            prev: asyncio.Task[None] | None = prev,
        ) -> None:
            if prev is not None:
                await prev
            await body()

        task = asyncio.ensure_future(run())
        tasks.append(task)
        prev = task
    await asyncio.gather(*tasks)


async def _drain(loops: int = 5) -> None:
    for _ in range(loops):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------- #
# begin() is non-blocking + the head-of-line proof                            #
# --------------------------------------------------------------------------- #


async def test_begin_parks_synchronously_and_returns_a_task() -> None:
    emitter = RecordingSessionEmitter()
    ledger = TurnLedger(emitter)
    approval = ScriptedApproval()
    approval.arm("turn_a")  # block forever — the resolver must not finish in begin()
    events = EventSink()
    coord = _coordinator(ledger, approval, FakeReply(), events)

    task = coord.begin(_round("turn_a"))

    # Parked synchronously, no terminal published, resolver still in flight.
    assert task is not None and not task.done()
    assert ledger.parked_turns == ("turn_a",)
    assert ledger.open_turns == ()  # a parked turn is NOT an open/unaccounted turn
    assert emitter.records == []
    task.cancel()


async def test_later_turns_do_not_block_while_approval_is_pending() -> None:
    """The core acceptance: an in-flight approval never head-of-line-stalls later turns."""
    emitter = RecordingSessionEmitter()
    ledger = TurnLedger(emitter)
    approval = ScriptedApproval()
    reply = FakeReply(ReplyOutcome(spoke=True))
    events = EventSink()
    coord = _coordinator(ledger, approval, reply, events)

    gate = approval.arm("turn_a")  # turn_a's human wait blocks until we release it
    holder: dict[str, asyncio.Task[None] | None] = {}

    async def hook_a() -> None:
        # approval turn: park + spawn resolver, then "raise StopResponse" (return).
        holder["a"] = coord.begin(_round("turn_a", decision_id=10))

    async def hook_b() -> None:  # a later, declined turn
        ledger.open("turn_b")
        await ledger.emit("turn_b", terminal_state="no_reply", no_reply_reason="router_declined")

    async def hook_c() -> None:  # another later turn that speaks
        ledger.open("turn_c")
        await ledger.emit("turn_c", terminal_state="replied")

    await _await_chained_hooks([hook_a, hook_b, hook_c])
    await _drain()  # let the spawned resolver reach its block point

    # While turn_a is parked, the await-chained later turns DID resolve.
    parked = ledger.terminal_for("turn_a")
    assert parked is not None and parked.terminal_state == "pending_approval"
    assert "turn_a" not in emitter.turn_ids  # no final terminal yet
    assert set(emitter.turn_ids) == {"turn_b", "turn_c"}
    assert events.pending == ["turn_a"]
    assert reply.calls == []  # the reply has not been generated yet

    # The human approves out of band — the parked turn now resolves.
    gate.set_result("approved")
    resolver = holder["a"]
    assert resolver is not None
    await resolver

    assert reply.calls == ["turn_a"]
    assert emitter.terminal("turn_a").terminal_state == "replied"
    assert events.resolved == [("turn_a", "approved")]
    # Exactly one terminal per turn id.
    assert Counter(emitter.turn_ids) == Counter({"turn_a": 1, "turn_b": 1, "turn_c": 1})


# --------------------------------------------------------------------------- #
# approve / reject / timeout (the three required outcomes)                     #
# --------------------------------------------------------------------------- #


async def test_approve_speaks_and_resolves_replied() -> None:
    emitter = RecordingSessionEmitter()
    ledger = TurnLedger(emitter)
    approval = ScriptedApproval()
    approval.immediate("turn_a", "approved")
    reply = FakeReply(ReplyOutcome(spoke=True))
    events = EventSink()
    coord = _coordinator(ledger, approval, reply, events)

    await _run_round(coord, _round("turn_a"))

    assert reply.calls == ["turn_a"]
    assert emitter.terminal("turn_a").terminal_state == "replied"
    assert events.pending == ["turn_a"]
    assert events.resolved == [("turn_a", "approved")]
    assert ledger.parked_turns == ()


async def test_reject_stays_silent_and_resolves_approval_rejected() -> None:
    emitter = RecordingSessionEmitter()
    ledger = TurnLedger(emitter)
    approval = ScriptedApproval()
    approval.immediate("turn_a", "rejected")
    reply = FakeReply()
    events = EventSink()
    coord = _coordinator(ledger, approval, reply, events)

    await _run_round(coord, _round("turn_a"))

    term = emitter.terminal("turn_a")
    assert term.terminal_state == "no_reply"
    assert term.no_reply_reason == "approval_rejected"
    assert reply.calls == []  # rejected → the answer LLM never runs
    assert events.resolved == [("turn_a", "rejected")]


async def test_timeout_from_source_resolves_approval_rejected() -> None:
    emitter = RecordingSessionEmitter()
    ledger = TurnLedger(emitter)
    approval = ScriptedApproval()
    approval.immediate("turn_a", "timeout")  # the source's own timeout fired
    reply = FakeReply()
    events = EventSink()
    coord = _coordinator(ledger, approval, reply, events)

    await _run_round(coord, _round("turn_a"))

    term = emitter.terminal("turn_a")
    assert term.terminal_state == "no_reply"
    assert term.no_reply_reason == "approval_rejected"
    assert "timed out" in term.detail
    assert reply.calls == []
    # ApprovalResolved keeps timeout distinct from rejected for the audit.
    assert events.resolved == [("turn_a", "timeout")]


async def test_timeout_from_defensive_bound_when_source_hangs() -> None:
    """A source that never returns is bounded by the coordinator's own wait_for."""
    emitter = RecordingSessionEmitter()
    ledger = TurnLedger(emitter)
    approval = ScriptedApproval()
    approval.arm("turn_a")  # never resolved → the source hangs
    reply = FakeReply()
    events = EventSink()
    # tiny timeout + tiny grace so the defensive bound fires fast
    coord = _coordinator(ledger, approval, reply, events, timeout_grace_s=0.05)

    task = coord.begin(_round("turn_a", timeout_s=0.05))
    # A concurrent later turn resolves immediately while turn_a is parked.
    ledger.open("turn_b")
    await ledger.emit("turn_b", terminal_state="replied")
    assert "turn_b" in emitter.turn_ids
    assert task is not None
    await task

    term = emitter.terminal("turn_a")
    assert term.terminal_state == "no_reply"
    assert term.no_reply_reason == "approval_rejected"
    assert events.resolved == [("turn_a", "timeout")]


# --------------------------------------------------------------------------- #
# approved-but-no-speech variants                                             #
# --------------------------------------------------------------------------- #


async def test_approved_but_empty_reply_resolves_model_empty_output() -> None:
    emitter = RecordingSessionEmitter()
    ledger = TurnLedger(emitter)
    approval = ScriptedApproval()
    approval.immediate("turn_a", "approved")
    reply = FakeReply(ReplyOutcome(spoke=False, no_reply_reason="model_empty_output"))
    events = EventSink()
    coord = _coordinator(ledger, approval, reply, events)

    await _run_round(coord, _round("turn_a"))

    term = emitter.terminal("turn_a")
    assert term.terminal_state == "no_reply"
    assert term.no_reply_reason == "model_empty_output"
    # Legacy parity: an approved-but-empty answer reports the round rejected.
    assert events.resolved == [("turn_a", "rejected")]


async def test_approved_reply_interrupted_resolves_barge_in() -> None:
    emitter = RecordingSessionEmitter()
    ledger = TurnLedger(emitter)
    approval = ScriptedApproval()
    approval.immediate("turn_a", "approved")
    reply = FakeReply(ReplyOutcome(spoke=False, no_reply_reason="barge_in"))
    events = EventSink()
    coord = _coordinator(ledger, approval, reply, events)

    await _run_round(coord, _round("turn_a"))

    term = emitter.terminal("turn_a")
    assert term.no_reply_reason == "barge_in"
    assert events.resolved == [("turn_a", "rejected")]


async def test_approved_reply_error_resolves_stage_error() -> None:
    emitter = RecordingSessionEmitter()
    ledger = TurnLedger(emitter)
    approval = ScriptedApproval()
    approval.immediate("turn_a", "approved")
    reply = FakeReply(raises=RuntimeError("tts blew up"))
    events = EventSink()
    coord = _coordinator(ledger, approval, reply, events)

    await _run_round(coord, _round("turn_a"))

    term = emitter.terminal("turn_a")
    assert term.terminal_state == "no_reply"
    assert term.no_reply_reason == "stage_error"
    assert "tts blew up" in term.detail
    assert events.resolved == [("turn_a", "rejected")]


async def test_approval_source_error_resolves_stage_error() -> None:
    emitter = RecordingSessionEmitter()
    ledger = TurnLedger(emitter)

    async def boom(round: ApprovalRound) -> ApprovalDecision:
        raise RuntimeError("redis gone")

    reply = FakeReply()
    events = EventSink()
    coord = ApprovalCoordinator(
        ledger,
        request_approval=boom,
        generate_reply=reply,
        on_pending=events.on_pending,
        on_resolved=events.on_resolved,
    )

    await _run_round(coord, _round("turn_a"))

    term = emitter.terminal("turn_a")
    assert term.terminal_state == "no_reply"
    assert term.no_reply_reason == "stage_error"
    assert reply.calls == []
    assert events.resolved == [("turn_a", "rejected")]


# --------------------------------------------------------------------------- #
# Teardown / cancellation                                                     #
# --------------------------------------------------------------------------- #


async def test_aclose_cancels_pending_round_and_resolves_rejected() -> None:
    emitter = RecordingSessionEmitter()
    ledger = TurnLedger(emitter)
    approval = ScriptedApproval()
    approval.arm("turn_a")  # block — the human never answers
    events = EventSink()
    coord = _coordinator(ledger, approval, FakeReply(), events)

    coord.begin(_round("turn_a"))
    await _drain()
    assert ledger.parked_turns == ("turn_a",)

    await coord.aclose()  # session teardown cancels the in-flight resolver

    term = emitter.terminal("turn_a")
    assert term.terminal_state == "no_reply"
    assert term.no_reply_reason == "approval_rejected"
    assert "cancelled" in term.detail
    assert ledger.parked_turns == ()


async def test_cancel_during_approved_reply_resolves_barge_in() -> None:
    emitter = RecordingSessionEmitter()
    ledger = TurnLedger(emitter)
    approval = ScriptedApproval()
    approval.immediate("turn_a", "approved")
    block: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    reply = FakeReply(block=block)  # the reply is mid-generation when we tear down
    events = EventSink()
    coord = _coordinator(ledger, approval, reply, events)

    coord.begin(_round("turn_a"))
    await _drain()
    assert reply.calls == ["turn_a"]  # resolver reached generate_reply and is blocked

    await coord.aclose()  # cancels mid-reply

    term = emitter.terminal("turn_a")
    assert term.terminal_state == "no_reply"
    assert term.no_reply_reason == "barge_in"


# --------------------------------------------------------------------------- #
# Concurrency: many approvals + declines, each exactly one terminal           #
# --------------------------------------------------------------------------- #


async def test_concurrent_approvals_and_declines_each_one_terminal() -> None:
    emitter = RecordingSessionEmitter(yield_first=True)
    ledger = TurnLedger(emitter)
    approval = ScriptedApproval()
    reply = FakeReply(ReplyOutcome(spoke=True))
    events = EventSink()
    coord = _coordinator(ledger, approval, reply, events)

    # Three approval turns with mixed outcomes, interleaved with three plain turns.
    approval.immediate("ap_approved", "approved")
    approval.immediate("ap_rejected", "rejected")
    approval.immediate("ap_timeout", "timeout")

    tasks: list[asyncio.Task[None]] = []
    for tid in ("ap_approved", "ap_rejected", "ap_timeout"):
        t = coord.begin(_round(tid))
        assert t is not None
        tasks.append(t)

    async def declined(tid: str) -> None:
        ledger.open(tid)
        await ledger.emit(tid, terminal_state="no_reply", no_reply_reason="router_declined")

    await asyncio.gather(
        declined("plain_1"),
        declined("plain_2"),
        declined("plain_3"),
        *tasks,
    )

    counts = Counter(emitter.turn_ids)
    assert all(c == 1 for c in counts.values()), counts
    assert set(counts) == {
        "ap_approved",
        "ap_rejected",
        "ap_timeout",
        "plain_1",
        "plain_2",
        "plain_3",
    }
    assert emitter.terminal("ap_approved").terminal_state == "replied"
    assert emitter.terminal("ap_rejected").no_reply_reason == "approval_rejected"
    assert emitter.terminal("ap_timeout").no_reply_reason == "approval_rejected"
    assert ledger.parked_turns == ()


# --------------------------------------------------------------------------- #
# TurnLedger park / resolve mechanics                                         #
# --------------------------------------------------------------------------- #


async def test_park_records_no_terminal_and_excludes_from_open() -> None:
    emitter = RecordingSessionEmitter()
    ledger = TurnLedger(emitter)

    ledger.open("item_a")
    assert ledger.open_turns == ("item_a",)
    assert ledger.park("item_a") is True

    assert emitter.records == []  # parking publishes no TurnTerminal
    assert ledger.open_turns == ()  # parked, so not an unaccounted open turn
    assert ledger.parked_turns == ("item_a",)
    marker = ledger.terminal_for("item_a")
    assert marker is not None and marker.terminal_state == "pending_approval"


async def test_park_then_resolve_is_exactly_one_terminal() -> None:
    emitter = RecordingSessionEmitter()
    ledger = TurnLedger(emitter)

    ledger.park("item_a")
    ok = await ledger.resolve("item_a", terminal_state="replied", detail="spoke")

    assert ok is True
    assert ledger.parked_turns == ()
    assert emitter.turn_ids == ["item_a"]
    assert emitter.terminal("item_a").terminal_state == "replied"


async def test_emit_cannot_clobber_a_parked_turn() -> None:
    """A stray reply done-callback for a parked turn must be dropped, not published."""
    emitter = RecordingSessionEmitter()
    ledger = TurnLedger(emitter)

    ledger.park("item_a")
    dropped = await ledger.emit("item_a", terminal_state="replied")

    assert dropped is False
    assert emitter.records == []
    marker = ledger.terminal_for("item_a")
    assert marker is not None and marker.terminal_state == "pending_approval"


async def test_resolve_on_unparked_turn_is_dropped() -> None:
    emitter = RecordingSessionEmitter()
    ledger = TurnLedger(emitter)

    # open-but-not-parked
    ledger.open("item_open")
    assert await ledger.resolve("item_open", terminal_state="replied") is False
    # already-terminal
    await ledger.emit("item_done", terminal_state="replied")
    assert await ledger.resolve("item_done", terminal_state="replied") is False
    # unknown
    assert await ledger.resolve("item_unknown", terminal_state="replied") is False

    # Only the one legitimate emit published.
    assert emitter.turn_ids == ["item_done"]


async def test_second_resolve_is_dropped() -> None:
    emitter = RecordingSessionEmitter()
    ledger = TurnLedger(emitter)

    ledger.park("item_a")
    first = await ledger.resolve(
        "item_a", terminal_state="no_reply", no_reply_reason="approval_rejected"
    )
    second = await ledger.resolve("item_a", terminal_state="replied")

    assert first is True
    assert second is False
    assert emitter.turn_ids == ["item_a"]
    assert emitter.terminal("item_a").no_reply_reason == "approval_rejected"


async def test_concurrent_resolve_publishes_once() -> None:
    """A human approve racing the timeout sweep → exactly one publish (first-wins)."""
    emitter = RecordingSessionEmitter(yield_first=True)
    ledger = TurnLedger(emitter)

    ledger.park("item_a")
    results = await asyncio.gather(
        ledger.resolve("item_a", terminal_state="replied"),
        ledger.resolve("item_a", terminal_state="no_reply", no_reply_reason="approval_rejected"),
    )

    assert results.count(True) == 1
    assert results.count(False) == 1
    assert len(emitter.records) == 1


async def test_double_begin_same_turn_returns_none_second_time() -> None:
    emitter = RecordingSessionEmitter()
    ledger = TurnLedger(emitter)
    approval = ScriptedApproval()
    approval.arm("turn_a")
    coord = _coordinator(ledger, approval, FakeReply(), EventSink())

    first = coord.begin(_round("turn_a"))
    second = coord.begin(_round("turn_a"))  # already parked

    assert first is not None
    assert second is None  # not parkable twice
    assert ledger.parked_turns == ("turn_a",)
    first.cancel()


# --------------------------------------------------------------------------- #
# close() force-resolves a stranded parked turn                               #
# --------------------------------------------------------------------------- #


async def test_close_force_resolves_a_stranded_parked_turn() -> None:
    emitter = RecordingSessionEmitter()
    ledger = TurnLedger(emitter)

    ledger.park("item_parked")  # an approval round that never settled
    await ledger.close()

    term = emitter.terminal("item_parked")
    assert term.terminal_state == "no_reply"
    assert term.no_reply_reason == "approval_rejected"
    assert "session closed" in term.detail
    assert ledger.parked_turns == ()


async def test_close_mixes_open_sweep_and_parked_resolve() -> None:
    emitter = RecordingSessionEmitter()
    ledger = TurnLedger(emitter)

    ledger.open("item_open")  # gate ran, reply lost → stage_error sweep
    ledger.park("item_parked")  # approval never settled → approval_rejected
    await ledger.emit("item_done", terminal_state="replied")  # already accounted
    await ledger.close()

    assert Counter(emitter.turn_ids) == Counter({"item_open": 1, "item_parked": 1, "item_done": 1})
    assert emitter.terminal("item_open").no_reply_reason == "stage_error"
    assert emitter.terminal("item_parked").no_reply_reason == "approval_rejected"


async def test_strict_close_raises_on_stranded_parked_turn() -> None:
    emitter = RecordingSessionEmitter()
    ledger = TurnLedger(emitter, strict=True)

    ledger.park("item_parked")
    with pytest.raises(AssertionError) as excinfo:
        await ledger.close()

    # The fallback terminal is still emitted before the assertion fires.
    assert emitter.terminal("item_parked").no_reply_reason == "approval_rejected"
    assert "item_parked" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Drift guard — ApprovalDecision must mirror the canonical literals           #
# --------------------------------------------------------------------------- #


def test_approval_decision_mirrors_canonical() -> None:
    from typing import get_args

    from johnny.agent.approval import ApprovalDecision
    from johnny.voice_pipeline.approval import ApprovalOutcome
    from johnny.voice_pipeline.events import ApprovalResolution

    assert set(get_args(ApprovalDecision)) == set(get_args(ApprovalResolution))
    assert set(get_args(ApprovalDecision)) == set(get_args(ApprovalOutcome))
