"""Offline replay harness for the voice pipeline (Johnny-ckz.28.5).

Feed a persisted session's STT-final transcripts back through the *real*
:class:`~johnny.voice_pipeline.the legacy split pipeline` — against fake STT / fake TTS
adapters and either recorded or live LLM responses — capture every pipeline
event it emits, and assert one of two things:

* ``invariants`` mode — the captured event stream honours the redesign
  invariants from sub-tasks Johnny-ckz.28.2 / .28.3: every transcribed turn
  ends in exactly one terminal state (INV-1, no silent drops), and the
  decisions/utterances cannot diverge in *existence* (INV-2, decision↔utterance
  parity). This is the CI gate.
* ``regression`` mode — the replayed per-turn outcome is diffed row-by-row
  against the outcome the session *originally* recorded, flagging any
  divergence. This is the manual-review "did my refactor change a session that
  used to work?" mode.

Why drive synthetic audio rather than inject text? The silent-drop bug lived in
the concurrency between the transcribe and response loops, and the per-turn
``turn_id`` correlation (which the invariants key on) is only assigned on the
audio path — ``feed_text`` leaves every injected turn at ``turn_id=0``. So the
harness synthesises one VAD-detectable tone burst per recorded transcript
(exactly like ``tests/voice_pipeline/conftest.py``) and lets the real VAD
segment them, reproducing the live turn-by-turn flow. The fake STT returns the
recorded transcript text for each segment; the noise gate is disabled because
the recordings are already *post-gate* finalised transcripts.

This module is the *pure* half (fixture model, event→turn assembly,
invariant checks, regression diff) — it needs no providers. The driving half
lives in :mod:`johnny.smoketest.replay_agent` (the LiveKit-Agents engine).
The retired ``run_replay`` driver for ``unified`` (S2S) fixtures was removed
with the S2S surface in Johnny-trt.43.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from johnny.voice_pipeline import (
    AgentSpoke,
    PipelineEvent,
    RouterDecisionMade,
    TranscriptFiltered,
    TranscriptFinalized,
    TurnTerminal,
    event_to_dict,
)

SPLIT_RUNTIME = "split"


# --- fixture model (pure) ---------------------------------------------------


@dataclass(frozen=True)
class ReplayTurn:
    """One recorded user turn: the transcript plus the LLM outputs it drove."""

    text: str
    confidence: float = 0.9
    speaker: str = "user"
    # Recorded router structured output ``{should_speak, confidence, reason,
    # reply_type, suggested_reply}``. ``simulate`` is an escape hatch for
    # turns the harness must reproduce without a literal LLM response — only
    # ``"timeout"`` is defined (the session-14 router hang).
    router: dict[str, Any] = field(default_factory=dict)
    simulate: str | None = None
    # Recorded answer-LLM text for a speaking turn (``None`` for turns the
    # router declined — the answer stage never runs).
    answer: str | None = None
    # What the session ORIGINALLY persisted for this turn, for regression
    # diffing. ``None`` fields mean "not recorded" (e.g. session-14 turn 4's
    # silent drop recorded no terminal at all).
    recorded: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReplayFixture:
    """A replayable session: metadata + ordered turns."""

    session_id: str
    label: str
    runtime: str  # "split" — the only runtime since Johnny-trt.43
    mode: str = "autonomous"
    confidence_threshold: float = 0.7
    allowed_replies: tuple[str, ...] = ()
    instructions: str = ""
    turns: tuple[ReplayTurn, ...] = ()

    @property
    def turn_count(self) -> int:
        return len(self.turns)


def fixture_from_dict(data: dict[str, Any]) -> ReplayFixture:
    """Parse a fixture dict (the on-disk ``fixture.json`` shape)."""
    turns = tuple(
        ReplayTurn(
            text=str(t["text"]),
            confidence=float(t.get("confidence", 0.9)),
            speaker=str(t.get("speaker", "user")),
            router=dict(t.get("router", {})),
            simulate=t.get("simulate"),
            answer=t.get("answer"),
            recorded=dict(t.get("recorded", {})),
        )
        for t in data.get("turns", [])
    )
    return ReplayFixture(
        session_id=str(data["session_id"]),
        label=str(data.get("label", f"session-{data['session_id']}")),
        runtime=str(data.get("runtime", "split")),
        mode=str(data.get("mode", "autonomous")),
        confidence_threshold=float(data.get("confidence_threshold", 0.7)),
        allowed_replies=tuple(data.get("allowed_replies", []) or []),
        instructions=str(data.get("instructions", "")),
        turns=turns,
    )


def load_fixture(path: Path) -> ReplayFixture:
    """Load a fixture from ``<dir>/fixture.json`` or a JSON file."""
    fixture_path = path / "fixture.json" if path.is_dir() else path
    with fixture_path.open("r", encoding="utf-8") as fh:
        return fixture_from_dict(json.load(fh))


def discover_fixtures(root: Path) -> list[Path]:
    """Return every ``fixture.json`` directory under ``root``, sorted."""
    if not root.exists():
        return []
    return sorted(p.parent for p in root.glob("*/fixture.json"))


# --- captured per-turn record + assembly (pure) -----------------------------


@dataclass
class TurnRecord:
    """The canonical record the harness derives from the replayed events.

    Keyed by the pipeline's per-turn ``turn_id`` (1-based on the audio path).
    Mirrors what the subscriber would persist onto the turn's
    ``agent_decisions`` row, assembled purely from the captured event stream so
    the harness needs no database.
    """

    turn_id: int
    heard_text: str | None = None
    should_speak: bool | None = None
    confidence: float | None = None
    suggested_reply: str | None = None
    spoke_text: str | None = None
    terminal_state: str | None = None
    outcome: str | None = None
    no_reply_reason: str | None = None
    filtered_reason: str | None = None

    @property
    def diverged(self) -> bool:
        """Spoken text differs from the router's recommendation (whitespace-normalised)."""
        if self.suggested_reply is None or self.spoke_text is None:
            return False
        return " ".join(self.suggested_reply.split()) != " ".join(self.spoke_text.split())


def assemble_turns(
    events: Sequence[PipelineEvent], runtime: str = SPLIT_RUNTIME
) -> list[TurnRecord]:
    """Fold the captured event stream into one :class:`TurnRecord` per turn.

    Decisions, terminals and filtered events carry ``turn_id`` directly.
    ``AgentSpoke`` does not (it never has), so — exactly like the durable
    subscriber — each spoken utterance is bound to the most recent
    ``should_speak`` decision seen in emission order.

    Raises :class:`ValueError` for any runtime other than ``split`` — the
    ``unified`` (S2S) assembly was removed with the S2S surface
    (Johnny-trt.43).
    """
    if runtime != SPLIT_RUNTIME:
        raise ValueError(
            f"unknown replay runtime {runtime!r}: only 'split' exists — the "
            "'unified' (S2S) replay was removed in Johnny-trt.43"
        )
    records: dict[int, TurnRecord] = {}

    def rec(turn_id: int) -> TurnRecord:
        return records.setdefault(turn_id, TurnRecord(turn_id=turn_id))

    last_speak_turn: int | None = None
    pending_transcripts: list[str] = []
    for ev in events:
        if isinstance(ev, TranscriptFinalized):
            pending_transcripts.append(ev.text)
        elif isinstance(ev, RouterDecisionMade):
            r = rec(ev.turn_id or 0)
            r.should_speak = ev.should_speak
            r.confidence = ev.confidence
            r.suggested_reply = ev.suggested_reply
            if r.heard_text is None and pending_transcripts:
                r.heard_text = pending_transcripts.pop(0)
            if ev.should_speak:
                last_speak_turn = ev.turn_id or 0
        elif isinstance(ev, AgentSpoke):
            if last_speak_turn is not None:
                rec(last_speak_turn).spoke_text = ev.text
                last_speak_turn = None
        elif isinstance(ev, TurnTerminal):
            r = rec(ev.turn_id)
            r.terminal_state = ev.terminal_state
            r.outcome = ev.outcome
            r.no_reply_reason = ev.no_reply_reason
        elif isinstance(ev, TranscriptFiltered):
            # Filtered turns never reach the router; the subscriber still
            # records them as durable noise no_reply rows. They carry no
            # turn_id, so bucket them under a synthetic negative id by order.
            tid = -(1 + sum(1 for t in records if t < 0))
            fr = rec(tid)
            fr.heard_text = ev.text
            fr.filtered_reason = ev.reason
            fr.terminal_state = "no_reply"
            fr.no_reply_reason = "noise_filtered"
    return [records[k] for k in sorted(records)]


# --- invariant checks (pure) ------------------------------------------------


@dataclass(frozen=True)
class InvariantViolation:
    """One failed invariant for one turn (or the run as a whole)."""

    invariant: str  # "INV-1" | "INV-2"
    turn_id: int | None
    detail: str


def check_invariants(
    events: Sequence[PipelineEvent], runtime: str = SPLIT_RUNTIME
) -> list[InvariantViolation]:
    """Assert the invariants appropriate to ``runtime`` over a captured stream.

    Raises :class:`ValueError` for any runtime other than ``split`` (the
    ``unified``/INV-U checks were removed with the S2S surface, Johnny-trt.43).
    """
    if runtime != SPLIT_RUNTIME:
        raise ValueError(
            f"unknown replay runtime {runtime!r}: only 'split' exists — the "
            "'unified' (S2S) replay was removed in Johnny-trt.43"
        )
    return _check_split_invariants(events)


def _check_split_invariants(
    events: Sequence[PipelineEvent],
) -> list[InvariantViolation]:
    """Assert the .28.2 / .28.3 invariants over a captured event stream.

    INV-1 (terminal-state-per-turn, Johnny-ckz.28.3): every turn that entered
    the response pipeline (got a ``router_decision_made``) emits exactly one
    ``turn_terminal``, and a ``no_reply`` terminal names its suppressor. A turn
    with a decision but no terminal is the silent drop the invariant forbids.

    INV-2 (decision↔utterance parity, Johnny-ckz.28.2): the chat and the
    decisions panel cannot diverge in existence — every ``agent_spoke`` traces
    to a ``should_speak`` decision and a ``replied`` terminal, the counts match,
    and a ``replied`` terminal is always backed by a spoken utterance. (Text
    *rephrase* between recommended and spoken is allowed and expected — the
    answer LLM is a second call — and is reconciled by the subscriber's ORM
    parity guard with override metadata, covered by test_decision_parity.py.)
    """
    violations: list[InvariantViolation] = []

    decisions = [e for e in events if isinstance(e, RouterDecisionMade)]
    terminals = [e for e in events if isinstance(e, TurnTerminal)]
    spokes = [e for e in events if isinstance(e, AgentSpoke)]

    terminals_by_turn: dict[int, list[TurnTerminal]] = {}
    for t in terminals:
        terminals_by_turn.setdefault(t.turn_id, []).append(t)

    # INV-1: each decided turn has exactly one terminal.
    for d in decisions:
        turn_id = d.turn_id or 0
        hits = terminals_by_turn.get(turn_id, [])
        if not hits:
            violations.append(
                InvariantViolation(
                    "INV-1",
                    turn_id,
                    "turn produced a router decision but no terminal state "
                    "(silent drop)",
                )
            )
        elif len(hits) > 1:
            violations.append(
                InvariantViolation(
                    "INV-1",
                    turn_id,
                    f"turn emitted {len(hits)} terminal states "
                    f"(expected exactly 1): "
                    f"{[t.terminal_state for t in hits]}",
                )
            )

    # INV-1: a no_reply terminal must name its reason.
    for t in terminals:
        if t.terminal_state == "no_reply" and not t.no_reply_reason:
            violations.append(
                InvariantViolation(
                    "INV-1",
                    t.turn_id,
                    "no_reply terminal carries no no_reply_reason",
                )
            )

    # INV-2: replied terminals and spoken utterances are in 1:1 correspondence.
    replied = [t for t in terminals if t.terminal_state == "replied"]
    if len(replied) != len(spokes):
        violations.append(
            InvariantViolation(
                "INV-2",
                None,
                f"{len(replied)} replied terminal(s) but {len(spokes)} "
                f"agent_spoke event(s) — chat/decisions existence parity broken",
            )
        )

    # INV-2: every replied terminal's turn had a should_speak decision.
    decided_speak_turns = {d.turn_id or 0 for d in decisions if d.should_speak}
    for t in replied:
        if t.turn_id not in decided_speak_turns:
            violations.append(
                InvariantViolation(
                    "INV-2",
                    t.turn_id,
                    "replied terminal with no should_speak router decision "
                    "(orphan utterance)",
                )
            )

    return violations


# --- regression diff (pure) -------------------------------------------------


@dataclass(frozen=True)
class TurnDiff:
    """One turn's replayed-vs-recorded comparison for regression mode."""

    turn_id: int
    field: str
    recorded: Any
    replayed: Any

    @property
    def changed(self) -> bool:
        return bool(self.recorded != self.replayed)


def diff_against_recorded(
    fixture: ReplayFixture, records: Sequence[TurnRecord]
) -> list[TurnDiff]:
    """Compare each replayed turn's outcome against the fixture's recorded one.

    Aligns by position (turn 1 ↔ first recorded turn, …) over the turns that
    reached the router (negative-id filtered turns are skipped). Only fields the
    fixture actually recorded are compared, so a partial ``recorded`` block
    (e.g. session-14 turn 4 recorded nothing) still diffs cleanly — a recorded
    ``None`` against a replayed terminal IS the divergence worth surfacing.
    """
    diffs: list[TurnDiff] = []
    routed = [r for r in records if r.turn_id > 0]
    for idx, turn in enumerate(fixture.turns):
        if idx >= len(routed):
            break
        rec = routed[idx]
        recorded = turn.recorded
        comparisons = {
            "should_speak": rec.should_speak,
            "terminal_state": rec.terminal_state,
            "outcome": rec.outcome,
            "spoke_text": rec.spoke_text,
        }
        for field_name, replayed in comparisons.items():
            if field_name not in recorded:
                continue
            recorded_val = recorded[field_name]
            if recorded_val != replayed:
                diffs.append(
                    TurnDiff(
                        turn_id=rec.turn_id,
                        field=field_name,
                        recorded=recorded_val,
                        replayed=replayed,
                    )
                )
    return diffs


# --- result record (shared with the replay_agent driver) --------------------


@dataclass
class ReplayResult:
    """Everything one replay produced."""

    fixture: ReplayFixture
    events: list[PipelineEvent]
    records: list[TurnRecord]
    stt_calls: int

    def events_as_dicts(self) -> list[dict[str, Any]]:
        return [event_to_dict(e) for e in self.events]


__all__ = [
    "InvariantViolation",
    "ReplayFixture",
    "ReplayResult",
    "ReplayTurn",
    "TurnDiff",
    "TurnRecord",
    "assemble_turns",
    "check_invariants",
    "diff_against_recorded",
    "discover_fixtures",
    "fixture_from_dict",
    "load_fixture",
]
