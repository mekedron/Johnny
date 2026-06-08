"""Property/fuzz + edge tests for the session-level INV-1 ledger (spike Johnny-o3z).

Drives :class:`johnny.agent.gate.TurnLedger` — the session-scoped authority that
guarantees **exactly one terminal per LiveKit turn id** across the two temporally
disjoint, possibly-overlapping emitters (the ``on_user_turn_completed`` gate and the
reply :class:`SpeechHandle` done-callback). The ledger is ``livekit``-free, so these
tests collect and run without the ``agent`` extra.

Coverage maps to the spike acceptance:

* every code path that ends a turn (gate decline / timeout / error / interrupt, the
  speak path's reply completion / interruption / error / empty, and the unaccounted
  fallback) resolves to exactly one terminal;
* **no double-emission** — a second emit for a turn id is dropped, including two
  *concurrent* emits for the same id (the atomic check-and-set);
* **no zero-emission** — a turn opened but never terminalized is swept at ``close``;
* a path LiveKit short-circuits before the gate (never ``open``-ed) gets no terminal;
* ``gate_tracker`` + ``run_gate`` compose, and the gate's terminal reconciles with a
  later (dropped) reply terminal for the same turn id;
* a property/fuzz drive of N overlapping turns with reordered / duplicated / late /
  lost emits asserts the invariant holds for every seed.
"""

from __future__ import annotations

import asyncio
import random
from collections import Counter

import pytest

from johnny.agent.gate import (
    GateAction,
    GateTerminal,
    GateTerminalState,
    TurnLedger,
    TurnNoReplyReason,
    run_gate,
)


class RecordingSessionEmitter:
    """A :data:`SessionTerminalEmitter` recording every ``(turn_id, terminal)``.

    ``yield_first=True`` awaits ``asyncio.sleep(0)`` before recording so that
    gathered emits genuinely interleave at the emitter's suspension point — this
    is what makes the concurrency tests meaningful (the ledger's first-wins claim
    happens *before* this await, so a racing second emit must still be dropped).
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


def _ledger(
    emitter: RecordingSessionEmitter, *, strict: bool = False
) -> TurnLedger:
    return TurnLedger(emitter, strict=strict)


# --------------------------------------------------------------------------- #
# The happy path + read APIs                                                  #
# --------------------------------------------------------------------------- #


async def test_open_then_emit_is_exactly_one_terminal() -> None:
    emitter = RecordingSessionEmitter()
    ledger = _ledger(emitter)

    ledger.open("item_a")
    assert ledger.open_turns == ("item_a",)
    assert ledger.terminal_for("item_a") is None

    ok = await ledger.emit("item_a", terminal_state="replied")

    assert ok is True
    assert not ledger.open_turns  # no longer awaiting
    assert len(emitter.records) == 1
    turn_id, term = emitter.records[0]
    assert turn_id == "item_a"
    assert term.terminal_state == "replied"
    assert ledger.terminal_for("item_a") == term


async def test_emit_for_unopened_turn_still_records() -> None:
    """The reply path may emit for a turn whose open() we never modelled."""
    emitter = RecordingSessionEmitter()
    ledger = _ledger(emitter)

    ok = await ledger.emit(
        "item_x", terminal_state="no_reply", no_reply_reason="router_declined"
    )

    assert ok is True
    assert emitter.turn_ids == ["item_x"]
    assert not ledger.open_turns  # recorded as terminal, not open


# --------------------------------------------------------------------------- #
# No double-emission (first-wins, incl. the concurrent race)                  #
# --------------------------------------------------------------------------- #


async def test_second_emit_same_turn_is_dropped() -> None:
    emitter = RecordingSessionEmitter()
    ledger = _ledger(emitter)

    first = await ledger.emit("item_a", terminal_state="replied")
    second = await ledger.emit(
        "item_a", terminal_state="no_reply", no_reply_reason="barge_in"
    )

    assert first is True
    assert second is False
    assert len(emitter.records) == 1
    assert emitter.records[0][1].terminal_state == "replied"  # first wins


async def test_concurrent_emit_same_turn_publishes_once() -> None:
    """Two gathered emits for one turn id → exactly one publish.

    The reply done-callback (turn N) and the next gate (turn N+1) run on the same
    loop; a reply that both 'completed' and 'was interrupted' could schedule two
    emits for the same id. The atomic claim-before-await makes one of them win.
    """
    emitter = RecordingSessionEmitter(yield_first=True)
    ledger = _ledger(emitter)

    results = await asyncio.gather(
        ledger.emit("item_a", terminal_state="replied"),
        ledger.emit("item_a", terminal_state="no_reply", no_reply_reason="barge_in"),
        ledger.emit("item_a", terminal_state="no_reply", no_reply_reason="stage_error"),
    )

    assert results.count(True) == 1
    assert results.count(False) == 2
    assert len(emitter.records) == 1
    assert emitter.turn_ids == ["item_a"]


async def test_distinct_turns_each_get_their_own_terminal() -> None:
    emitter = RecordingSessionEmitter(yield_first=True)
    ledger = _ledger(emitter)

    await asyncio.gather(
        ledger.emit("item_a", terminal_state="replied"),
        ledger.emit("item_b", terminal_state="no_reply", no_reply_reason="router_declined"),
        ledger.emit("item_c", terminal_state="no_reply", no_reply_reason="barge_in"),
    )

    assert Counter(emitter.turn_ids) == Counter({"item_a": 1, "item_b": 1, "item_c": 1})


# --------------------------------------------------------------------------- #
# No zero-emission (the close() sweep)                                        #
# --------------------------------------------------------------------------- #


async def test_close_sweeps_unaccounted_open_turn() -> None:
    emitter = RecordingSessionEmitter()
    ledger = _ledger(emitter)

    ledger.open("item_lost")  # gate ran, reply handle vanished, nobody emitted
    await ledger.close()

    assert len(emitter.records) == 1
    turn_id, term = emitter.records[0]
    assert turn_id == "item_lost"
    assert term.terminal_state == "no_reply"
    assert term.no_reply_reason == "stage_error"
    assert "unaccounted" in term.detail
    assert not ledger.open_turns


async def test_close_does_not_double_emit_for_accounted_turn() -> None:
    emitter = RecordingSessionEmitter()
    ledger = _ledger(emitter)

    ledger.open("item_done")
    await ledger.emit("item_done", terminal_state="replied")
    await ledger.close()  # already terminal → swept-over

    assert len(emitter.records) == 1
    assert emitter.records[0][1].terminal_state == "replied"


async def test_close_is_idempotent() -> None:
    emitter = RecordingSessionEmitter()
    ledger = _ledger(emitter)

    ledger.open("item_lost")
    await ledger.close()
    await ledger.close()  # second close adds nothing

    assert len(emitter.records) == 1


# --------------------------------------------------------------------------- #
# Short-circuit: a turn LiveKit ends before the gate is never our turn        #
# --------------------------------------------------------------------------- #


async def test_short_circuit_turn_never_opened_gets_no_terminal() -> None:
    """skip_reply / too-short / paused / no-llm: never open()-ed → no terminal."""
    emitter = RecordingSessionEmitter()
    ledger = _ledger(emitter)

    # A real turn we own...
    ledger.open("item_real")
    await ledger.emit("item_real", terminal_state="replied")
    # ...and a short-circuited non-turn we never registered.
    await ledger.close()

    assert emitter.turn_ids == ["item_real"]  # the short-circuit left no trace


# --------------------------------------------------------------------------- #
# Strict mode                                                                 #
# --------------------------------------------------------------------------- #


async def test_strict_close_raises_on_unaccounted_turn() -> None:
    emitter = RecordingSessionEmitter()
    ledger = _ledger(emitter, strict=True)

    ledger.open("item_a")
    ledger.open("item_b")
    await ledger.emit("item_a", terminal_state="replied")

    with pytest.raises(AssertionError) as excinfo:
        await ledger.close()

    # The fallback terminal is still emitted before the assertion fires.
    assert Counter(emitter.turn_ids) == Counter({"item_a": 1, "item_b": 1})
    assert "item_b" in str(excinfo.value)


async def test_strict_close_clean_when_all_accounted() -> None:
    emitter = RecordingSessionEmitter()
    ledger = _ledger(emitter, strict=True)

    ledger.open("item_a")
    await ledger.emit("item_a", terminal_state="replied")
    await ledger.close()  # nothing stranded → no raise

    assert len(emitter.records) == 1


# --------------------------------------------------------------------------- #
# gate_tracker + run_gate compose, and reconcile gate vs reply                #
# --------------------------------------------------------------------------- #


async def _router_returns(decision: object) -> object:
    return decision


async def test_speak_path_defers_terminal_to_reply_then_one_terminal() -> None:
    """SPEAK → gate emits nothing; the reply terminal is the turn's one terminal."""
    emitter = RecordingSessionEmitter()
    ledger = _ledger(emitter)
    decision = object()

    action, got = await run_gate(
        lambda: _router_returns(decision),
        tracker=ledger.gate_tracker("item_a"),
        timeout_s=5.0,
    )

    assert action is GateAction.SPEAK
    assert got is decision
    assert emitter.records == []  # gate emitted no terminal on speak
    assert ledger.open_turns == ("item_a",)  # still awaiting the reply

    # Reply completes later → exactly one terminal for the turn.
    await ledger.emit("item_a", terminal_state="replied")
    assert emitter.turn_ids == ["item_a"]
    assert not ledger.open_turns


async def test_gate_timeout_terminal_drops_a_late_reply_for_same_turn() -> None:
    """The gate's no_reply and a stray later reply reconcile to one terminal."""
    emitter = RecordingSessionEmitter()
    ledger = _ledger(emitter)

    async def _hang() -> object:
        await asyncio.Event().wait()
        return object()  # pragma: no cover

    action, _ = await run_gate(
        _hang, tracker=ledger.gate_tracker("item_a"), timeout_s=0.05
    )

    assert action is GateAction.STAY_SILENT
    gate_term = ledger.terminal_for("item_a")
    assert gate_term is not None
    assert gate_term.no_reply_reason == "stage_error"

    # A reply emit that somehow still fires for this turn is dropped (first-wins).
    dropped = await ledger.emit("item_a", terminal_state="replied")
    assert dropped is False
    assert emitter.turn_ids == ["item_a"]
    assert emitter.records[0][1].no_reply_reason == "stage_error"


# --------------------------------------------------------------------------- #
# Property/fuzz — N overlapping turns, reordered/duplicated/late/lost emits   #
# --------------------------------------------------------------------------- #


# Reasons each phase can produce (all within TurnNoReplyReason). Typed as the
# Literal so rng.choice keeps the precise type through to ledger.emit.
_GATE_NO_SPEAK: tuple[TurnNoReplyReason, ...] = (
    "router_declined",
    "low_confidence",
    "barge_in",
    "stage_error",
)
_REPLY_OUTCOMES: tuple[tuple[GateTerminalState, TurnNoReplyReason | None], ...] = (
    ("replied", None),
    ("no_reply", "barge_in"),  # interrupted mid-reply
    ("no_reply", "stage_error"),  # generation errored
    ("no_reply", "model_empty_output"),  # reply produced no audio
)

# One scheduled emit: (turn_id, terminal_state, no_reply_reason).
_EmitOp = tuple[str, GateTerminalState, "TurnNoReplyReason | None"]


@pytest.mark.parametrize("seed", range(200))
async def test_fuzz_exactly_one_terminal_per_turn(seed: int) -> None:
    """Drive a randomised mix of turns and assert INV-1 holds for every seed.

    Each turn is one of: a short-circuit (never owned → no terminal), a gate
    no-speak, a speak whose reply later resolves (or is duplicated / lost). All
    the resulting emit coroutines are *shuffled and gathered* so reply callbacks
    for earlier turns race the gates of later ones, and duplicates race the
    originals. After close(), every owned turn must have exactly one terminal and
    every short-circuit turn none.
    """
    rng = random.Random(seed)
    emitter = RecordingSessionEmitter(yield_first=True)
    ledger = _ledger(emitter)

    n_turns = rng.randint(1, 12)
    owned: set[str] = set()
    short_circuited: set[str] = set()
    ops: list[_EmitOp] = []  # emit specs to shuffle + gather

    for i in range(n_turns):
        turn_id = f"item_{seed}_{i}"
        roll = rng.random()

        if roll < 0.2:
            # LiveKit short-circuited before the gate — not a turn we own.
            short_circuited.add(turn_id)
            continue

        owned.add(turn_id)
        ledger.open(turn_id)

        if roll < 0.55:
            # Gate no-speak: one emit.
            ops.append((turn_id, "no_reply", rng.choice(_GATE_NO_SPEAK)))
        elif roll < 0.9:
            # Speak path: a reply terminal resolves later (maybe duplicated).
            state, reason = rng.choice(_REPLY_OUTCOMES)
            ops.append((turn_id, state, reason))
            if rng.random() < 0.3:
                # Done-callback fires twice / gate+reply both fire — must drop.
                ops.append((turn_id, "no_reply", "barge_in"))
        else:
            # Speak path whose reply terminal is LOST — close() must sweep it.
            pass

    rng.shuffle(ops)
    await asyncio.gather(
        *(
            ledger.emit(tid, terminal_state=state, no_reply_reason=reason)
            for tid, state, reason in ops
        )
    )
    await ledger.close()

    counts = Counter(emitter.turn_ids)

    # No double-emission: no turn id was published more than once.
    assert all(c == 1 for c in counts.values()), counts
    # No zero-emission: every owned turn resolved to exactly one terminal.
    assert set(counts) == owned, (set(counts) ^ owned)
    # Short-circuit turns left no trace.
    assert short_circuited.isdisjoint(counts)
    # And the ledger agrees nothing is left open.
    assert not ledger.open_turns


# --------------------------------------------------------------------------- #
# Drift guard — the ledger's full reason mirror must equal the canonical one  #
# --------------------------------------------------------------------------- #


def test_turn_no_reply_reason_mirrors_canonical() -> None:
    from typing import get_args

    from johnny.agent.gate import GateTerminalState, TurnNoReplyReason
    from johnny.voice_pipeline.events import NoReplyReason, TerminalState

    # The session ledger records the FULL canonical no_reply vocabulary, so the
    # mirror must equal events.NoReplyReason (not merely be a subset) — a new
    # canonical reason that is not mirrored would be untypable by the ledger.
    assert set(get_args(TurnNoReplyReason)) == set(get_args(NoReplyReason))
    assert set(get_args(GateTerminalState)) <= set(get_args(TerminalState))
