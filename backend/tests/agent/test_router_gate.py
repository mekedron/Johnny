"""Unit tests for the bounded router-gate harness (spike Johnny-9k2).

Drives :mod:`johnny.agent.gate` — the timeout + barge-in-cancel + INV-1
terminal harness that Johnny-xpa will wrap its blocking
``on_user_turn_completed`` should-speak gate in. The harness is ``livekit``-free
so these tests collect and run without the ``agent`` extra.

Coverage maps to the spike's acceptance:

* a slow/hanging router → the timeout fires, a terminal ``no_reply(stage_error)``
  is emitted, the in-flight router is cancelled, and the next gate is NOT stalled;
* a barge-in (``abandon``) mid-gate → the in-flight router is cancelled cleanly
  and a terminal ``no_reply(barge_in)`` is emitted;
* outer task cancellation (hard teardown) → a best-effort terminal is emitted
  and ``CancelledError`` is re-raised (never swallowed);
* a router exception → ``no_reply(stage_error)`` terminal, stay silent;
* the approve path → ``SPEAK`` with the decision and NO terminal;
* INV-1 — a second terminal for the same turn is dropped, not duplicated;
* the local reason literals stay a subset of the canonical ``events`` literals.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from johnny.agent.gate import (
    DEFAULT_ROUTER_GATE_TIMEOUT_S,
    GateAction,
    GateTerminal,
    RouterStatus,
    TerminalTracker,
    run_gate,
    run_router_call,
)


class RecordingEmitter:
    """A non-suspending :data:`TerminalEmitter` that records every terminal.

    Non-suspending on purpose: it must run to completion even when invoked
    from a cancelled task (the teardown path), so the terminal is captured
    before ``CancelledError`` is re-raised.
    """

    def __init__(self) -> None:
        self.terminals: list[GateTerminal] = []

    async def __call__(self, terminal: GateTerminal) -> None:
        self.terminals.append(terminal)


class FakeRouter:
    """A controllable stand-in for Johnny's router ``LLMProvider`` call.

    ``block=True`` hangs forever (until cancelled) and records whether it was
    cancelled — the slow-router / barge-in fixture. ``raises`` raises on call;
    otherwise it returns ``result`` immediately.
    """

    def __init__(
        self,
        *,
        result: Any = None,
        raises: BaseException | None = None,
        block: bool = False,
    ) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.calls = 0
        self._result = result
        self._raises = raises
        self._block = block

    async def __call__(self) -> Any:
        self.calls += 1
        self.started.set()
        if self._raises is not None:
            raise self._raises
        if self._block:
            try:
                await asyncio.Event().wait()  # hang until cancelled
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return self._result


def _tracker(emitter: RecordingEmitter, *, strict: bool = False) -> TerminalTracker:
    return TerminalTracker(emitter, turn_id=7, strict=strict)


# --------------------------------------------------------------------------- #
# Timeout (the Session-14 hang)                                               #
# --------------------------------------------------------------------------- #


async def test_timeout_fires_emits_stage_error_and_cancels_router() -> None:
    emitter = RecordingEmitter()
    tracker = _tracker(emitter)
    router = FakeRouter(block=True)

    action, decision = await run_gate(
        router, tracker=tracker, timeout_s=0.05
    )

    assert action is GateAction.STAY_SILENT
    assert decision is None
    assert router.cancelled is True  # in-flight router torn down
    assert len(emitter.terminals) == 1
    term = emitter.terminals[0]
    assert term.terminal_state == "no_reply"
    assert term.no_reply_reason == "stage_error"
    assert "gate bound" in term.detail


async def test_run_router_call_times_out_returns_sentinel() -> None:
    router = FakeRouter(block=True)
    status, decision = await run_router_call(router, timeout_s=0.05)
    assert status is RouterStatus.TIMED_OUT
    assert decision is None
    assert router.cancelled is True


async def test_timeout_does_not_stall_the_next_gate() -> None:
    """A hung gate must not block later turns (the SDK awaits the old hook)."""
    emitter = RecordingEmitter()

    # Turn N: router hangs, gate times out fast.
    hung = FakeRouter(block=True)
    action_n, _ = await run_gate(hung, tracker=_tracker(emitter), timeout_s=0.05)
    assert action_n is GateAction.STAY_SILENT
    assert hung.cancelled is True

    # Turn N+1: a fast router resolves promptly — not stalled by the hung turn.
    decision = object()
    fast = FakeRouter(result=decision)
    action_next, got = await asyncio.wait_for(
        run_gate(fast, tracker=_tracker(emitter), timeout_s=5.0),
        timeout=1.0,
    )
    assert action_next is GateAction.SPEAK
    assert got is decision


async def test_timeout_zero_disables_bound_but_abandon_still_works() -> None:
    emitter = RecordingEmitter()
    tracker = _tracker(emitter)
    router = FakeRouter(block=True)
    abandon = asyncio.Event()

    gate = asyncio.ensure_future(
        run_gate(router, tracker=tracker, timeout_s=0.0, abandon=abandon)
    )
    await router.started.wait()
    abandon.set()
    action, _ = await asyncio.wait_for(gate, timeout=1.0)

    assert action is GateAction.STAY_SILENT
    assert router.cancelled is True
    assert emitter.terminals[0].no_reply_reason == "barge_in"


# --------------------------------------------------------------------------- #
# Barge-in mid-gate (cooperative abandon)                                     #
# --------------------------------------------------------------------------- #


async def test_barge_in_cancels_router_and_emits_barge_in_terminal() -> None:
    emitter = RecordingEmitter()
    tracker = _tracker(emitter)
    router = FakeRouter(block=True)
    abandon = asyncio.Event()

    gate = asyncio.ensure_future(
        run_gate(router, tracker=tracker, timeout_s=5.0, abandon=abandon)
    )
    await router.started.wait()  # router genuinely in flight
    abandon.set()
    action, decision = await asyncio.wait_for(gate, timeout=1.0)

    assert action is GateAction.STAY_SILENT
    assert decision is None
    assert router.cancelled is True  # cancelled cleanly, mid-flight
    assert len(emitter.terminals) == 1
    assert emitter.terminals[0].no_reply_reason == "barge_in"


async def test_router_wins_the_race_when_abandon_never_set() -> None:
    emitter = RecordingEmitter()
    tracker = _tracker(emitter)
    decision = object()
    router = FakeRouter(result=decision)
    abandon = asyncio.Event()  # never set

    action, got = await run_gate(
        router, tracker=tracker, timeout_s=5.0, abandon=abandon
    )

    assert action is GateAction.SPEAK
    assert got is decision
    assert emitter.terminals == []  # no terminal on the speak path


# --------------------------------------------------------------------------- #
# Outer cancellation (hard teardown)                                          #
# --------------------------------------------------------------------------- #


async def test_outer_cancellation_emits_terminal_and_reraises() -> None:
    emitter = RecordingEmitter()
    tracker = _tracker(emitter)
    router = FakeRouter(block=True)

    gate = asyncio.ensure_future(
        run_gate(router, tracker=tracker, timeout_s=5.0)
    )
    await router.started.wait()
    gate.cancel()

    with pytest.raises(asyncio.CancelledError):
        await gate

    assert router.cancelled is True
    assert len(emitter.terminals) == 1
    assert emitter.terminals[0].no_reply_reason == "barge_in"


# --------------------------------------------------------------------------- #
# Router error                                                                #
# --------------------------------------------------------------------------- #


async def test_router_exception_emits_stage_error_and_stays_silent() -> None:
    emitter = RecordingEmitter()
    tracker = _tracker(emitter)
    router = FakeRouter(raises=RuntimeError("provider exploded"))

    action, decision = await run_gate(router, tracker=tracker, timeout_s=5.0)

    assert action is GateAction.STAY_SILENT
    assert decision is None
    assert len(emitter.terminals) == 1
    term = emitter.terminals[0]
    assert term.no_reply_reason == "stage_error"
    assert "RuntimeError: provider exploded" in term.detail


# --------------------------------------------------------------------------- #
# Approve path                                                                #
# --------------------------------------------------------------------------- #


async def test_speak_path_returns_decision_without_terminal() -> None:
    emitter = RecordingEmitter()
    tracker = _tracker(emitter)
    decision = {"should_speak": True, "reason": "user asked a question"}
    router = FakeRouter(result=decision)

    action, got = await run_gate(router, tracker=tracker, timeout_s=5.0)

    assert action is GateAction.SPEAK
    assert got is decision
    assert tracker.emitted is False
    assert emitter.terminals == []


# --------------------------------------------------------------------------- #
# INV-1 — exactly one terminal per turn                                       #
# --------------------------------------------------------------------------- #


async def test_second_terminal_is_dropped() -> None:
    emitter = RecordingEmitter()
    tracker = _tracker(emitter)

    first = await tracker.emit(
        terminal_state="no_reply", no_reply_reason="router_declined", detail="nope"
    )
    second = await tracker.emit(
        terminal_state="no_reply", no_reply_reason="stage_error", detail="late"
    )

    assert first is True
    assert second is False
    assert len(emitter.terminals) == 1
    assert emitter.terminals[0].no_reply_reason == "router_declined"


async def test_ensure_terminal_fills_unaccounted_clean_exit() -> None:
    emitter = RecordingEmitter()
    tracker = _tracker(emitter)

    await tracker.ensure_terminal()  # no prior terminal, no exception

    assert len(emitter.terminals) == 1
    term = emitter.terminals[0]
    assert term.no_reply_reason == "stage_error"
    assert "without a terminal" in term.detail


async def test_ensure_terminal_noop_when_already_emitted() -> None:
    emitter = RecordingEmitter()
    tracker = _tracker(emitter)

    await tracker.emit(
        terminal_state="no_reply", no_reply_reason="router_declined", detail="declined"
    )
    await tracker.ensure_terminal()  # must not add a second

    assert len(emitter.terminals) == 1
    assert emitter.terminals[0].no_reply_reason == "router_declined"


async def test_strict_mode_raises_on_unaccounted_clean_exit() -> None:
    emitter = RecordingEmitter()
    tracker = _tracker(emitter, strict=True)

    with pytest.raises(AssertionError):
        await tracker.ensure_terminal()

    # The fallback terminal is still emitted before the assertion.
    assert len(emitter.terminals) == 1
    assert emitter.terminals[0].no_reply_reason == "stage_error"


async def test_strict_mode_does_not_raise_on_cancellation() -> None:
    """Cancellation is teardown, not a bug — strict mode must not raise."""
    emitter = RecordingEmitter()
    tracker = _tracker(emitter, strict=True)

    await tracker.ensure_terminal(exc=asyncio.CancelledError())

    assert len(emitter.terminals) == 1
    assert emitter.terminals[0].no_reply_reason == "barge_in"


# --------------------------------------------------------------------------- #
# Drift guard — local literals must mirror the canonical event literals       #
# --------------------------------------------------------------------------- #


def test_gate_reason_literals_subset_of_canonical() -> None:
    from typing import get_args

    from johnny.agent.gate import GateNoReplyReason, GateTerminalState
    from johnny.voice_pipeline.events import NoReplyReason, TerminalState

    assert set(get_args(GateNoReplyReason)) <= set(get_args(NoReplyReason))
    assert set(get_args(GateTerminalState)) <= set(get_args(TerminalState))


def test_default_timeout_matches_legacy_router_bound() -> None:
    from johnny.voice_pipeline.pipeline import DEFAULT_ROUTER_LLM_TIMEOUT_S

    assert DEFAULT_ROUTER_GATE_TIMEOUT_S == DEFAULT_ROUTER_LLM_TIMEOUT_S
