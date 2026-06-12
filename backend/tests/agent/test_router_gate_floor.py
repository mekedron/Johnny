"""RouterGate × shared speech floor (Johnny-trt.46).

Every gate speak path must acquire the attached floor before its first
audio frame and release it from its done-callback — completion AND
interrupt — with the spoken text riding the release for the peers'
text-match backstop. A denied acquire suppresses the speech honestly:
turn-bound paths terminalize ``no_reply(floor_unavailable)``; the
out-of-band correction just drops. No floor attached (every single-agent
session) keeps all paths byte-identical — the whole sibling decision suite
runs floorless and pins that half.

Reuses the scripted-LLM / fake-say harness shapes from
``test_router_gate_decision.py``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any, cast

import pytest
from livekit.agents.llm import ChatContext, StopResponse
from livekit.agents.llm.chat_context import ChatMessage as LKChatMessage
from livekit.agents.voice import SpeechHandle

from app.providers.base import ChatMessage, LLMProvider, LLMResponse, ToolDefinition
from johnny.agent.gate import GateTerminal, TurnIndex, TurnLedger
from johnny.agent.router_gate import RouterGate, RouterGateConfig
from johnny.agent.speech_floor import SpeechFloor
from johnny.agent.tasks import (
    InMemoryTaskSink,
    QueuedTask,
    TaskCoordinator,
    TaskResult,
    TaskSpec,
    stub_executor,
)

# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #


class _FakeRouterLLM(LLMProvider):
    def __init__(self, decisions: list[dict[str, Any]] | None = None) -> None:
        self._decisions = list(decisions or [])
        self._idx = 0

    @property
    def name(self) -> str:
        return "fake-router"

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
        response_format: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> LLMResponse:
        del messages
        decision = (
            self._decisions[self._idx]
            if self._idx < len(self._decisions)
            else self._decisions[-1]
        )
        self._idx += 1
        return LLMResponse(
            text=json.dumps(decision), finish_reason="stop", structured_output=decision
        )


class _RecordingEmitter:
    def __init__(self) -> None:
        self.records: list[tuple[str, GateTerminal]] = []

    async def __call__(self, turn_id: str, terminal: GateTerminal) -> None:
        self.records.append((turn_id, terminal))

    @property
    def reasons(self) -> list[str | None]:
        return [t.no_reply_reason for _, t in self.records]


class _FakeSpeechHandle:
    def __init__(
        self,
        *,
        handle_id: str = "item_reply",
        interrupted: bool = False,
        chat_items: list[Any] | None = None,
    ) -> None:
        self.id = handle_id
        self.interrupted = interrupted
        self.chat_items = chat_items if chat_items is not None else []
        self._cbs: list[Any] = []

    def add_done_callback(self, cb: Any) -> None:
        self._cbs.append(cb)

    def fire_done(self) -> None:
        for cb in list(self._cbs):
            cb(self)


class _FakeSay:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.handles: list[_FakeSpeechHandle] = []
        self.raises: BaseException | None = None

    def __call__(self, text: str) -> SpeechHandle:
        if self.raises is not None:
            raise self.raises
        handle = _FakeSpeechHandle(handle_id=f"item_say_{len(self.handles)}")
        self.texts.append(text)
        self.handles.append(handle)
        return cast(SpeechHandle, handle)


class _FakeLease:
    def __init__(self, floor: _FakeFloor, kind: str) -> None:
        self._floor = floor
        self._kind = kind
        self.released = False

    async def release(self, *, reason: str, spoken_text: str = "") -> None:
        if self.released:
            raise AssertionError("lease released twice")
        self.released = True
        self._floor.releases.append((self._kind, reason, spoken_text))


class _FakeFloor:
    """Scripted SpeechFloor stand-in (duck-typed; cast at the attach seam).

    ``grants`` scripts acquire outcomes per call (``True`` → lease,
    ``False`` → timeout/None); exhausted or ``None`` → always grant.
    """

    def __init__(self, grants: list[bool] | None = None) -> None:
        self._grants = grants
        self.acquires: list[str] = []
        self.releases: list[tuple[str, str, str]] = []
        self.leases: list[_FakeLease] = []

    async def acquire(
        self, kind: str, *, timeout_s: float | None = None
    ) -> _FakeLease | None:
        del timeout_s
        self.acquires.append(kind)
        if self._grants:
            granted = self._grants.pop(0)
            if not granted:
                return None
        lease = _FakeLease(self, kind)
        self.leases.append(lease)
        return lease

    def peer_holds_floor(self) -> bool:
        return False


def _attach(gate: RouterGate, floor: _FakeFloor) -> None:
    gate.attach_speech_floor(cast(SpeechFloor, floor))


def _user_msg(text: str) -> LKChatMessage:
    return LKChatMessage(role="user", content=[text])


def _speak_decision(confidence: float = 0.95) -> dict[str, Any]:
    return {"should_speak": True, "confidence": confidence, "reason": "addressed"}


def _delegate_decision() -> dict[str, Any]:
    return {
        "should_speak": True,
        "confidence": 0.95,
        "reason": "complex ask",
        "action": "delegate",
        "task": {"kind": "calendar.check", "ack": "On it — give me a minute."},
    }


def _make_gate(
    decisions: list[dict[str, Any]],
    *,
    floor: _FakeFloor,
    with_tasks: bool = False,
) -> tuple[RouterGate, _RecordingEmitter, _FakeSay]:
    emitter = _RecordingEmitter()
    say = _FakeSay()
    coordinator = (
        TaskCoordinator(InMemoryTaskSink(), executor=stub_executor)
        if with_tasks
        else None
    )
    turn_index = TurnIndex()
    gate = RouterGate(
        _FakeRouterLLM(decisions),
        config=RouterGateConfig(),
        ledger=TurnLedger(emitter),
        tasks=coordinator,
        resolve_turn_id=turn_index.resolve,
    )
    gate.attach_say(say)
    _attach(gate, floor)
    return gate, emitter, say


async def _drain(gate: RouterGate) -> None:
    while gate._reply_tasks:
        await asyncio.gather(*gate._reply_tasks)


# --------------------------------------------------------------------------- #
# The reply path                                                              #
# --------------------------------------------------------------------------- #


async def test_speak_acquires_floor_and_releases_on_completion() -> None:
    floor = _FakeFloor()
    gate, emitter, _ = _make_gate([_speak_decision()], floor=floor)
    msg = _user_msg("Johnny, what's the status?")

    await gate.run_turn(ChatContext.empty(), msg)
    assert floor.acquires == ["reply"]
    assert floor.releases == []  # held until the reply settles

    handle = _FakeSpeechHandle(chat_items=["irrelevant"])
    handle.chat_items = [type("Item", (), {"text_content": "All green."})()]
    gate.bind_reply(cast(SpeechHandle, handle))
    handle.fire_done()
    await _drain(gate)

    assert floor.releases == [("reply", "completed", "All green.")]
    assert emitter.records[0][1].terminal_state == "replied"
    assert gate._floor_leases == {}


async def test_speak_floor_timeout_suppresses_with_floor_unavailable() -> None:
    floor = _FakeFloor(grants=[False])
    gate, emitter, _ = _make_gate([_speak_decision()], floor=floor)
    msg = _user_msg("Johnny, thoughts?")

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), msg)

    assert floor.acquires == ["reply"]
    assert emitter.reasons == ["floor_unavailable"]
    # Nothing pending: the SDK reply that never comes has no turn to bind.
    assert not gate._pending_speak_turns
    assert gate._floor_leases == {}
    assert gate._ledger.open_turns == ()


async def test_interrupted_reply_releases_floor_as_interrupted() -> None:
    floor = _FakeFloor()
    gate, emitter, _ = _make_gate([_speak_decision()], floor=floor)
    msg = _user_msg("Johnny, summarise")

    await gate.run_turn(ChatContext.empty(), msg)
    handle = _FakeSpeechHandle(interrupted=True)
    gate.bind_reply(cast(SpeechHandle, handle))
    handle.fire_done()
    await _drain(gate)

    assert len(floor.releases) == 1
    kind, reason, _text = floor.releases[0]
    assert (kind, reason) == ("reply", "interrupted")
    assert emitter.reasons == ["barge_in"]


async def test_empty_reply_still_releases_floor() -> None:
    floor = _FakeFloor()
    gate, emitter, _ = _make_gate([_speak_decision()], floor=floor)
    await gate.run_turn(ChatContext.empty(), _user_msg("Johnny?"))

    handle = _FakeSpeechHandle(chat_items=[])
    gate.bind_reply(cast(SpeechHandle, handle))
    handle.fire_done()
    await _drain(gate)

    assert floor.releases == [("reply", "completed", "")]
    assert emitter.reasons == ["model_empty_output"]


# --------------------------------------------------------------------------- #
# The say paths (ack / status / decline)                                      #
# --------------------------------------------------------------------------- #


async def test_delegate_ack_acquires_floor_and_releases_with_ack_text() -> None:
    floor = _FakeFloor()
    gate, emitter, say = _make_gate([_delegate_decision()], floor=floor, with_tasks=True)

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), _user_msg("check my calendar"))

    assert floor.acquires == ["ack"]
    assert say.texts == ["On it — give me a minute."]
    say.handles[0].fire_done()
    await _drain(gate)

    assert floor.releases == [("ack", "completed", "On it — give me a minute.")]
    assert emitter.records[-1][1].terminal_state == "replied"


async def test_delegate_ack_floor_timeout_keeps_task_but_suppresses_ack() -> None:
    floor = _FakeFloor(grants=[False])
    gate, emitter, say = _make_gate([_delegate_decision()], floor=floor, with_tasks=True)

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), _user_msg("check my calendar"))

    # The row was queued before the speak attempt (row-before-ack order is
    # untouched); only the audible promise was suppressed, honestly.
    assert say.texts == []
    assert emitter.reasons == ["floor_unavailable"]
    assert floor.releases == []


async def test_interrupted_ack_releases_floor_as_interrupted() -> None:
    floor = _FakeFloor()
    gate, _, say = _make_gate([_delegate_decision()], floor=floor, with_tasks=True)

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), _user_msg("check my calendar"))

    say.handles[0].interrupted = True
    say.handles[0].fire_done()
    await _drain(gate)

    assert len(floor.releases) == 1
    assert floor.releases[0][:2] == ("ack", "interrupted")


async def test_say_raising_releases_floor_say_failed() -> None:
    floor = _FakeFloor()
    gate, emitter, say = _make_gate([_delegate_decision()], floor=floor, with_tasks=True)
    say.raises = RuntimeError("session draining")

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), _user_msg("check my calendar"))
    await _drain(gate)

    assert floor.releases == [("ack", "say_failed", "")]
    assert emitter.reasons == ["stage_error"]


# --------------------------------------------------------------------------- #
# The correction path                                                          #
# --------------------------------------------------------------------------- #


def _queued(kind: str = "calendar.check") -> QueuedTask:
    return QueuedTask(task_id=7, spec=TaskSpec(kind=kind, ack_text="on it"))


async def test_correction_acquires_floor_and_releases_with_text() -> None:
    floor = _FakeFloor()
    gate, _, say = _make_gate([_speak_decision()], floor=floor)

    await gate.report_task_failure(_queued(), TaskResult(status="failed", result_text="no access"))

    assert floor.acquires == ["correction"]
    assert len(say.texts) == 1
    say.handles[0].fire_done()
    await _drain(gate)
    assert len(floor.releases) == 1
    kind, reason, text = floor.releases[0]
    assert (kind, reason) == ("correction", "completed")
    assert text == say.texts[0]


async def test_correction_floor_timeout_drops_the_walkback() -> None:
    floor = _FakeFloor(grants=[False])
    gate, emitter, say = _make_gate([_speak_decision()], floor=floor)

    await gate.report_task_failure(_queued(), TaskResult(status="failed", result_text="no access"))

    assert say.texts == []  # nothing spoken
    assert emitter.records == []  # out-of-band speech: no terminal either
    assert floor.releases == []


# --------------------------------------------------------------------------- #
# Hygiene                                                                      #
# --------------------------------------------------------------------------- #


async def test_aclose_releases_stranded_reply_lease_as_teardown() -> None:
    floor = _FakeFloor()
    gate, _, _ = _make_gate([_speak_decision()], floor=floor)
    await gate.run_turn(ChatContext.empty(), _user_msg("Johnny?"))
    assert len(floor.leases) == 1

    await gate.aclose()

    assert floor.releases == [("reply", "teardown", "")]
    assert gate._floor_leases == {}


async def test_stale_lease_swept_superseded_on_next_speak_turn() -> None:
    floor = _FakeFloor()
    gate, _, _ = _make_gate([_speak_decision(), _speak_decision()], floor=floor)
    # A ghost lease whose turn is neither pending nor the active reply — the
    # defensive case the sweep exists for (a reply that never materialised
    # after its bind was consumed).
    ghost = await floor.acquire("reply")
    assert ghost is not None
    gate._floor_leases["ghost-turn"] = cast(Any, ghost)

    await gate.run_turn(ChatContext.empty(), _user_msg("Johnny, next topic"))
    await _drain(gate)

    assert ("reply", "superseded", "") in floor.releases
    assert "ghost-turn" not in gate._floor_leases


async def test_no_floor_attached_means_no_acquire_anywhere() -> None:
    """Floorless gate (every single-agent session): zero floor interaction."""
    floor = _FakeFloor()  # constructed but never attached
    emitter = _RecordingEmitter()
    say = _FakeSay()
    gate = RouterGate(
        _FakeRouterLLM([_speak_decision()]),
        config=RouterGateConfig(),
        ledger=TurnLedger(emitter),
    )
    gate.attach_say(say)

    await gate.run_turn(ChatContext.empty(), _user_msg("Johnny?"))
    handle = _FakeSpeechHandle(chat_items=["x"])
    gate.bind_reply(cast(SpeechHandle, handle))
    handle.fire_done()
    await _drain(gate)

    assert floor.acquires == []
    assert floor.releases == []
