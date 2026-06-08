"""Wiring tests for ``approval_required`` mode (Johnny-qzj — build of Johnny-z97).

The spike proved the :class:`~johnny.agent.approval.ApprovalCoordinator` and the
parked :class:`~johnny.agent.gate.TurnLedger` state in isolation
(``test_approval_flow.py``). This module proves the **build**: the
:class:`~johnny.agent.router_gate.RouterGate` approval branch + the production
seams in :mod:`johnny.agent.approval_wiring` + the :class:`JohnnyAgent` teardown,
driven end-to-end with the real coordinator / ledger / event bus / decision sink
and only the router LLM + ``AgentSession`` faked.

Coverage maps to the bead acceptance:

* **approve** → reply spoken (terminal ``replied``), ``ApprovalPending`` +
  ``ApprovalResolved(approved)`` emitted, decision row flipped ``pending`` →
  ``spoken``;
* **reject** / **timeout** → terminal ``no_reply(approval_rejected)``,
  ``ApprovalResolved(rejected|timeout)``, row → ``rejected``, no reply spoken;
* approved-but-empty → ``no_reply(model_empty_output)`` reported rejected (legacy
  parity);
* the configurable ``approval_timeout_seconds`` is carried to the approval source;
* the gate parks (does not record a SPEAK turn) and raises ``StopResponse``;
* the approval reply's ``speech_created`` is disambiguated from gated SPEAK
  replies; teardown settles a still-parked round; misconfigurations reject;
* the individual ``approval_wiring`` builders behave.

Guarded by ``importorskip`` so the suite still collects without the ``agent``
extra (``livekit-agents``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any, cast

import pytest

pytest.importorskip("livekit.agents")

from livekit.agents.llm import ChatContext, StopResponse  # noqa: E402
from livekit.agents.llm.chat_context import ChatMessage as LKChatMessage  # noqa: E402
from livekit.agents.voice import SpeechHandle  # noqa: E402

from app.providers.base import (  # noqa: E402
    ChatMessage,
    LLMProvider,
    LLMResponse,
    ToolDefinition,
)
from johnny.agent.approval import (  # noqa: E402
    ApprovalCoordinator,
    ApprovalRound,
)
from johnny.agent.approval_wiring import (  # noqa: E402
    build_approval_coordinator,
    build_approval_event_hooks,
    build_generate_reply,
    build_persist_pending_decision,
    build_request_approval,
)
from johnny.agent.gate import GateTerminal, TurnLedger  # noqa: E402
from johnny.agent.router_gate import RouterGate, RouterGateConfig  # noqa: E402
from johnny.voice_pipeline.approval import (  # noqa: E402
    ApprovalGate,
    ApprovalOutcome,
    ApprovalRequest,
    InMemoryApprovalGate,
)
from johnny.voice_pipeline.decision_sink import InMemoryDecisionSink  # noqa: E402
from johnny.voice_pipeline.event_bus import InMemoryEventBus  # noqa: E402
from johnny.voice_pipeline.events import (  # noqa: E402
    ApprovalPending,
    ApprovalResolved,
)
from johnny.voice_pipeline.pipeline import (  # noqa: E402
    APPROVAL_REQUIRED_MODE,
    RouterDecision,
)

# pytest is configured with ``asyncio_mode = "auto"`` — async tests need no mark.

_FIXED_TS = 1000


# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #


class _FakeRouterLLM(LLMProvider):
    """Scripted router ``LLMProvider`` (same shape as the router-gate tests)."""

    def __init__(self, decisions: list[dict[str, Any]] | None = None) -> None:
        self._decisions = list(decisions or [])
        self._idx = 0

    @property
    def name(self) -> str:
        return "fake-router"

    async def chat(
        self,
        messages: Sequence[ChatMessage],  # noqa: ARG002
        tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
        response_format: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> LLMResponse:
        decision = (
            self._decisions[self._idx] if self._idx < len(self._decisions) else self._decisions[-1]
        )
        self._idx += 1
        return LLMResponse(
            text=json.dumps(decision),
            finish_reason="stop",
            structured_output=decision,
        )


class _RecordingEmitter:
    """A :data:`SessionTerminalEmitter` recording every ``(turn_id, terminal)``."""

    def __init__(self) -> None:
        self.records: list[tuple[str, GateTerminal]] = []

    async def __call__(self, turn_id: str, terminal: GateTerminal) -> None:
        self.records.append((turn_id, terminal))


class _FakeSpeechHandle:
    """A stand-in reply ``SpeechHandle`` (id + interrupted + chat_items + await)."""

    def __init__(
        self,
        *,
        handle_id: str = "approval-reply-1",
        interrupted: bool = False,
        chat_items: list[Any] | None = None,
    ) -> None:
        self.id = handle_id
        self.interrupted = interrupted
        self.chat_items = chat_items if chat_items is not None else ["assistant item"]
        self._cbs: list[Any] = []

    def add_done_callback(self, cb: Any) -> None:
        self._cbs.append(cb)

    def __await__(self) -> Any:
        async def _await_self() -> _FakeSpeechHandle:
            return self

        return _await_self().__await__()


class _FakeSession:
    """A stand-in ``AgentSession`` exposing only ``generate_reply``."""

    def __init__(self, handle: _FakeSpeechHandle | None = None) -> None:
        self._handle = handle or _FakeSpeechHandle()
        self.calls: list[str | None] = []

    def generate_reply(self, *, instructions: str | None = None) -> _FakeSpeechHandle:
        self.calls.append(instructions)
        return self._handle


class _BlockingApprovalGate(ApprovalGate):
    """An approval source that blocks forever (until the resolver is cancelled)."""

    async def request_approval(self, request: ApprovalRequest) -> ApprovalOutcome:
        del request
        await asyncio.Event().wait()
        return "approved"  # pragma: no cover - never reached


class _BoomDecisionSink(InMemoryDecisionSink):
    """Records raise — exercises the persist hook's swallow-and-return-None path."""

    async def record(self, *args: Any, **kwargs: Any) -> int | None:
        raise RuntimeError("decision sink unavailable")


# --------------------------------------------------------------------------- #
# Builders                                                                    #
# --------------------------------------------------------------------------- #


def _approval_config(*, timeout_s: float = 12.0) -> RouterGateConfig:
    return RouterGateConfig(
        mode=APPROVAL_REQUIRED_MODE,
        confidence_threshold=0.5,
        approval_timeout_seconds=timeout_s,
    )


def _wire(
    *,
    approval_gate: ApprovalGate,
    session: _FakeSession | None = None,
    decisions: list[dict[str, Any]] | None = None,
    config: RouterGateConfig | None = None,
    decision_sink: InMemoryDecisionSink | None = None,
) -> tuple[
    RouterGate,
    ApprovalCoordinator,
    _RecordingEmitter,
    InMemoryEventBus,
    InMemoryDecisionSink,
    _FakeSession,
]:
    """Build the full approval wiring around the real coordinator + ledger."""
    emitter = _RecordingEmitter()
    ledger = TurnLedger(emitter)
    bus = InMemoryEventBus()
    sink = decision_sink if decision_sink is not None else InMemoryDecisionSink()
    sess = session or _FakeSession()
    router = _FakeRouterLLM(
        decisions
        or [
            {
                "should_speak": True,
                "confidence": 0.95,
                "reason": "addressed",
                "suggested_reply": "Yes, confirmed.",
            }
        ]
    )
    persist = build_persist_pending_decision(
        sink, session_id="s1", bot_session_id=7, clock=lambda: _FIXED_TS
    )
    gate = RouterGate(
        router,
        config=config or _approval_config(),
        ledger=ledger,
        persist_pending_decision=persist,
    )
    coordinator = build_approval_coordinator(
        ledger=ledger,
        router_gate=gate,
        session=cast(Any, sess),
        approval_gate=approval_gate,
        event_bus=bus,
        decision_sink=sink,
        session_id="s1",
        clock=lambda: _FIXED_TS,
    )
    return gate, coordinator, emitter, bus, sink, sess


def _user_msg(text: str) -> LKChatMessage:
    return LKChatMessage(role="user", content=[text])


async def _drain(coordinator: ApprovalCoordinator) -> None:
    """Await every in-flight resolver task to completion."""
    tasks = list(coordinator._tasks)
    if tasks:
        await asyncio.gather(*tasks)


def _only_terminal(emitter: _RecordingEmitter) -> tuple[str, GateTerminal]:
    assert len(emitter.records) == 1, emitter.records
    return emitter.records[0]


# --------------------------------------------------------------------------- #
# Gate approval branch — park, no SPEAK turn, StopResponse                     #
# --------------------------------------------------------------------------- #


async def test_approval_branch_parks_without_speak_turn_and_raises() -> None:
    gate, coordinator, emitter, bus, sink, _ = _wire(approval_gate=_BlockingApprovalGate())
    msg = _user_msg("Johnny, can you confirm the budget?")

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), msg)

    # Parked (non-final), NOT recorded as a pending SPEAK turn, no terminal yet.
    assert gate._ledger.parked_turns == (msg.id,)
    assert gate._ledger.open_turns == ()
    assert list(gate._pending_speak_turns) == []
    assert emitter.records == []
    # The pending decision row was persisted (the UI correlates on it).
    rows = sink.snapshot()
    assert len(rows) == 1
    assert rows[0].outcome == "pending"

    await gate.aclose()  # drain the blocked resolver
    # Teardown settled the still-parked turn to approval_rejected (INV-1 — the
    # ledger.close() parked-sweep, since the resolver was cancelled before it
    # could settle the round itself).
    _, term = _only_terminal(emitter)
    assert term.terminal_state == "no_reply"
    assert term.no_reply_reason == "approval_rejected"


# --------------------------------------------------------------------------- #
# Approve → spoken + events + row flip                                         #
# --------------------------------------------------------------------------- #


async def test_approve_path_speaks_replied_and_emits_events() -> None:
    session = _FakeSession(_FakeSpeechHandle(chat_items=["assistant reply"]))
    gate, coordinator, emitter, bus, sink, sess = _wire(
        approval_gate=InMemoryApprovalGate(["approved"]),
        session=session,
    )
    msg = _user_msg("Johnny, confirm please")

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), msg)
    await _drain(coordinator)

    # Single final terminal: replied, on the original turn id.
    turn_id, term = _only_terminal(emitter)
    assert turn_id == msg.id
    assert term.terminal_state == "replied"
    assert gate._ledger.parked_turns == ()

    # The approved reply was spoken with the suggested reply as instructions.
    assert sess.calls == ["Yes, confirmed."]

    # ApprovalPending then ApprovalResolved(approved).
    events = bus.snapshot()
    pendings = [e for e in events if isinstance(e, ApprovalPending)]
    resolveds = [e for e in events if isinstance(e, ApprovalResolved)]
    assert len(pendings) == 1
    assert pendings[0].suggested_reply == "Yes, confirmed."
    assert pendings[0].timeout_s == 12.0
    assert len(resolveds) == 1
    assert resolveds[0].resolution == "approved"
    assert pendings[0].decision_id == resolveds[0].decision_id

    # Decision row flipped pending → spoken.
    rows = sink.snapshot()
    assert len(rows) == 1
    assert rows[0].outcome == "spoken"
    assert rows[0].decision_id == resolveds[0].decision_id


# --------------------------------------------------------------------------- #
# Reject / timeout → no_reply(approval_rejected) + events + row flip           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("scripted", "expected_resolution"),
    [("rejected", "rejected"), ("timeout", "timeout")],
)
async def test_reject_or_timeout_suppresses_and_emits_events(
    scripted: ApprovalOutcome, expected_resolution: str
) -> None:
    session = _FakeSession()
    gate, coordinator, emitter, bus, sink, sess = _wire(
        approval_gate=InMemoryApprovalGate([scripted]),
        session=session,
    )
    msg = _user_msg("Johnny, should we ship?")

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), msg)
    await _drain(coordinator)

    # Terminal no_reply(approval_rejected) — same for reject and timeout.
    turn_id, term = _only_terminal(emitter)
    assert turn_id == msg.id
    assert term.terminal_state == "no_reply"
    assert term.no_reply_reason == "approval_rejected"

    # No reply was ever generated.
    assert sess.calls == []

    # ApprovalResolved carries the distinct resolution (reject vs timeout).
    resolveds = [e for e in bus.snapshot() if isinstance(e, ApprovalResolved)]
    assert len(resolveds) == 1
    assert resolveds[0].resolution == expected_resolution

    # Decision row flipped pending → rejected.
    assert sink.snapshot()[0].outcome == "rejected"


async def test_approved_but_empty_reply_is_reported_rejected() -> None:
    # Approved, but the reply produced nothing → model_empty_output, reported
    # rejected for the ApprovalResolved audit (legacy parity).
    session = _FakeSession(_FakeSpeechHandle(chat_items=[]))
    gate, coordinator, emitter, bus, sink, _ = _wire(
        approval_gate=InMemoryApprovalGate(["approved"]),
        session=session,
    )
    msg = _user_msg("Johnny?")

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), msg)
    await _drain(coordinator)

    _, term = _only_terminal(emitter)
    assert term.terminal_state == "no_reply"
    assert term.no_reply_reason == "model_empty_output"

    resolveds = [e for e in bus.snapshot() if isinstance(e, ApprovalResolved)]
    assert resolveds[0].resolution == "rejected"
    assert sink.snapshot()[0].outcome == "rejected"


async def test_configurable_timeout_is_carried_to_the_source() -> None:
    gate_source = InMemoryApprovalGate(["approved"])
    gate, coordinator, *_ = _wire(
        approval_gate=gate_source,
        session=_FakeSession(),
        config=_approval_config(timeout_s=7.5),
    )
    msg = _user_msg("confirm")

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), msg)
    await _drain(coordinator)

    assert gate_source.requests[0].timeout_s == 7.5


# --------------------------------------------------------------------------- #
# speech_created disambiguation                                               #
# --------------------------------------------------------------------------- #


async def test_bind_reply_skips_approval_owned_handle() -> None:
    gate, *_ = _wire(approval_gate=_BlockingApprovalGate())

    approval_handle = _FakeSpeechHandle(handle_id="approval-owned")
    gate.register_approval_reply("approval-owned")
    gate.bind_reply(cast(SpeechHandle, approval_handle))

    # No done-callback bound, and no pending SPEAK turn consumed.
    assert approval_handle._cbs == []
    # Consumed from the set so it does not grow unbounded.
    assert "approval-owned" not in gate._approval_reply_handles


async def test_bind_reply_still_binds_a_normal_speak_reply() -> None:
    gate, *_ = _wire(approval_gate=_BlockingApprovalGate())
    gate._pending_speak_turns.append("item_speak")

    normal = _FakeSpeechHandle(handle_id="normal-reply")
    gate.bind_reply(cast(SpeechHandle, normal))

    # A non-approval reply binds normally (done-callback registered, turn popped).
    assert len(normal._cbs) == 1
    assert list(gate._pending_speak_turns) == []


# --------------------------------------------------------------------------- #
# Teardown + JohnnyAgent.on_exit                                              #
# --------------------------------------------------------------------------- #


async def test_on_exit_settles_a_still_parked_round() -> None:
    from johnny.agent.session import JohnnyAgent

    gate, coordinator, emitter, _, _, _ = _wire(approval_gate=_BlockingApprovalGate())
    msg = _user_msg("waiting on a human")

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), msg)
    assert gate._ledger.parked_turns == (msg.id,)

    agent = JohnnyAgent(router_gate=gate)
    await agent.on_exit()  # session teardown → gate.aclose() → settle + sweep

    _, term = _only_terminal(emitter)
    assert term.terminal_state == "no_reply"
    assert term.no_reply_reason == "approval_rejected"
    assert gate._ledger.parked_turns == ()


# --------------------------------------------------------------------------- #
# Misconfiguration → reject (legacy parity)                                    #
# --------------------------------------------------------------------------- #


async def test_no_coordinator_rejects_the_turn() -> None:
    # approval_required mode but no coordinator attached → reject, terminalize.
    emitter = _RecordingEmitter()
    ledger = TurnLedger(emitter)
    router = _FakeRouterLLM(
        [{"should_speak": True, "confidence": 0.9, "reason": "x", "suggested_reply": "hi"}]
    )
    gate = RouterGate(router, config=_approval_config(), ledger=ledger)
    msg = _user_msg("no coordinator wired")

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), msg)

    _, term = _only_terminal(emitter)
    assert term.terminal_state == "no_reply"
    assert term.no_reply_reason == "approval_rejected"


async def test_no_decision_id_rejects_without_parking() -> None:
    # Decision sink yields no id (its record raises) → reject; never park / begin.
    session = _FakeSession()
    gate, coordinator, emitter, bus, _, sess = _wire(
        approval_gate=InMemoryApprovalGate(["approved"]),
        session=session,
        decision_sink=_BoomDecisionSink(),
    )
    msg = _user_msg("persist will fail")

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), msg)
    await _drain(coordinator)

    _, term = _only_terminal(emitter)
    assert term.terminal_state == "no_reply"
    assert term.no_reply_reason == "approval_rejected"
    assert gate._ledger.parked_turns == ()
    # No approval round started → no ApprovalPending, no reply.
    assert bus.snapshot() == []
    assert sess.calls == []


# --------------------------------------------------------------------------- #
# approval_wiring builders — direct unit coverage                             #
# --------------------------------------------------------------------------- #


def _round(**overrides: Any) -> ApprovalRound:
    base: dict[str, Any] = {
        "turn_id": "item_1",
        "decision_id": 5,
        "suggested_reply": "Sounds good.",
        "timeout_s": 9.0,
        "reason": "addressed",
        "reply_type": "answer",
    }
    base.update(overrides)
    return ApprovalRound(**base)


async def test_build_request_approval_maps_round_to_request() -> None:
    source = InMemoryApprovalGate(["approved"])
    request_approval = build_request_approval(source, session_id="sess-9")

    decision = await request_approval(_round())

    assert decision == "approved"
    assert source.requests == [
        ApprovalRequest(
            decision_id=5,
            suggested_reply="Sounds good.",
            timeout_s=9.0,
            session_id="sess-9",
        )
    ]


async def test_build_generate_reply_registers_handle_and_maps_outcomes() -> None:
    emitter = _RecordingEmitter()
    gate = RouterGate(_FakeRouterLLM(), config=RouterGateConfig(), ledger=TurnLedger(emitter))

    # spoke
    session = _FakeSession(_FakeSpeechHandle(handle_id="h1", chat_items=["x"]))
    generate = build_generate_reply(cast(Any, session), gate)
    outcome = await generate(_round())
    assert outcome.spoke is True
    assert session.calls == ["Sounds good."]
    assert "h1" in gate._approval_reply_handles  # registered for disambiguation

    # interrupted → barge_in
    session = _FakeSession(_FakeSpeechHandle(handle_id="h2", interrupted=True))
    outcome = await build_generate_reply(cast(Any, session), gate)(_round())
    assert outcome.spoke is False
    assert outcome.no_reply_reason == "barge_in"

    # empty chat_items → model_empty_output
    session = _FakeSession(_FakeSpeechHandle(handle_id="h3", chat_items=[]))
    outcome = await build_generate_reply(cast(Any, session), gate)(_round())
    assert outcome.spoke is False
    assert outcome.no_reply_reason == "model_empty_output"

    # empty suggested_reply → generate_reply() called with no instructions
    session = _FakeSession(_FakeSpeechHandle(handle_id="h4", chat_items=["y"]))
    await build_generate_reply(cast(Any, session), gate)(_round(suggested_reply=""))
    assert session.calls == [None]


async def test_build_event_hooks_publish_and_flip_the_row() -> None:
    bus = InMemoryEventBus()
    sink = InMemoryDecisionSink()
    # Seed a pending row so update_outcome has a target.
    from johnny.voice_pipeline.events import RouterDecisionMade

    decision_id = await sink.record(
        RouterDecisionMade(should_speak=True, confidence=0.9, reason="r", timestamp_ms=0),
        outcome="pending",
    )
    assert decision_id is not None
    on_pending, on_resolved = build_approval_event_hooks(
        bus, sink, session_id="s2", clock=lambda: _FIXED_TS
    )
    rnd = _round(decision_id=decision_id)

    await on_pending(rnd)
    await on_resolved(rnd, "approved")

    events = bus.snapshot()
    assert any(isinstance(e, ApprovalPending) and e.decision_id == decision_id for e in events)
    resolved = [e for e in events if isinstance(e, ApprovalResolved)]
    assert resolved[0].resolution == "approved"
    assert sink.snapshot()[0].outcome == "spoken"

    # rejected / timeout both flip the row to rejected.
    await on_resolved(rnd, "rejected")
    assert sink.snapshot()[0].outcome == "rejected"
    await on_resolved(rnd, "timeout")
    assert sink.snapshot()[0].outcome == "rejected"
    assert [e.resolution for e in bus.snapshot() if isinstance(e, ApprovalResolved)] == [
        "approved",
        "rejected",
        "timeout",
    ]


async def test_build_persist_pending_decision_records_pending() -> None:
    sink = InMemoryDecisionSink()
    persist = build_persist_pending_decision(
        sink, session_id="s3", bot_session_id=11, clock=lambda: _FIXED_TS
    )
    decision = RouterDecision(
        should_speak=True,
        confidence=0.8,
        reason="addressed",
        reply_type="answer",
        suggested_reply="Hello.",
    )

    decision_id = await persist(decision, "item_42")

    assert decision_id == 1
    row = sink.snapshot()[0]
    assert row.outcome == "pending"
    assert row.bot_session_id == 11
    assert row.decision.suggested_reply == "Hello."
    assert row.decision.session_id == "s3"


async def test_build_persist_pending_decision_swallows_sink_failure() -> None:
    persist = build_persist_pending_decision(_BoomDecisionSink())
    decision = RouterDecision(should_speak=True, confidence=1.0, reason="r")
    assert await persist(decision, "item_99") is None
