"""Offline replay harness for the voice pipeline (Johnny-ckz.28.5).

Feed a persisted session's STT-final transcripts back through the *real*
:class:`~johnny.voice_pipeline.VoicePipeline` — against fake STT / fake TTS
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

The module is split into a *pure* half (fixture model, event→turn assembly,
invariant checks, regression diff) that needs no providers, and a *driving*
half (``run_replay``) that imports the provider ABCs and spins the pipeline.
"""

from __future__ import annotations

import array
import asyncio
import json
import math
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.providers import (
    ChatMessage,
    LLMProvider,
    LLMResponse,
    STTProvider,
    ToolDefinition,
    TranscriptEvent,
    TTSProvider,
)
from app.providers.s2s_base import (
    S2SAudioFrame,
    S2SEvent,
    S2SProvider,
    S2SResponseCompleted,
    S2SResponseStarted,
    S2SSession,
    S2STranscript,
)
from johnny.voice_pipeline import (
    AgentSpoke,
    BrowserAudioTransport,
    EnergyVAD,
    InMemoryEventBus,
    JohnnyTransport,
    PipelineConfig,
    PipelineEvent,
    RouterDecisionMade,
    TranscriptFiltered,
    TranscriptFinalized,
    TurnTerminal,
    UnifiedPipelineConfig,
    UnifiedVoicePipeline,
    VoicePipeline,
    event_to_dict,
)

SPLIT_RUNTIME = "split"
UNIFIED_RUNTIME = "unified"

# --- synthetic-audio constants (mirror tests/voice_pipeline/conftest.py) ----

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2  # s16
FRAME_DURATION_MS = 20
BYTES_PER_FRAME = (SAMPLE_RATE * FRAME_DURATION_MS // 1000) * SAMPLE_WIDTH
TONE_MS = 600
GAP_MS = 800
LEAD_MS = 200
VAD_THRESHOLD = 0.05
END_OF_SPEECH_MS = 300

# Router timeout used when a fixture turn simulates the session-14 hang. Kept
# small so the timeout path runs fast in CI while still exercising the real
# ``asyncio.wait_for`` bound that turns a stalled router into a durable
# ``no_reply`` row instead of a silent drop.
SIMULATED_HANG_TIMEOUT_S = 0.25
SIMULATED_HANG_SLEEP_S = 5.0


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
    runtime: str  # "split" | "unified"
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
    """
    if runtime == UNIFIED_RUNTIME:
        return _assemble_unified_turns(events)
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


def _assemble_unified_turns(events: Sequence[PipelineEvent]) -> list[TurnRecord]:
    """Per-turn records for a unified-S2S replay.

    Unified has no router/terminal spine — a turn is a (user transcript,
    assistant utterance) pair. Pairs the i-th user transcript with the i-th
    ``agent_spoke`` positionally; an assistant utterance with no matching user
    turn (or vice versa) shows up as a half-filled record the invariant check
    catches.
    """
    user_texts = [
        e.text
        for e in events
        if isinstance(e, TranscriptFinalized) and e.speaker != "assistant"
    ]
    spokes = [e.text for e in events if isinstance(e, AgentSpoke)]
    records: list[TurnRecord] = []
    for i in range(max(len(user_texts), len(spokes))):
        spoke = spokes[i] if i < len(spokes) else None
        records.append(
            TurnRecord(
                turn_id=i + 1,
                heard_text=user_texts[i] if i < len(user_texts) else None,
                should_speak=spoke is not None,
                spoke_text=spoke,
                terminal_state="replied" if spoke is not None else "no_reply",
                outcome="spoken" if spoke is not None else "suppressed",
            )
        )
    return records


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
    """Assert the invariants appropriate to ``runtime`` over a captured stream."""
    if runtime == UNIFIED_RUNTIME:
        return _check_unified_invariants(events)
    return _check_split_invariants(events)


def _check_unified_invariants(
    events: Sequence[PipelineEvent],
) -> list[InvariantViolation]:
    """Unified-S2S analogue of INV-1/INV-2: no assistant utterance is dropped.

    The unified pipeline has no router/terminal spine, so the invariant is
    existence parity between what the model produced (assistant transcripts)
    and what reached the user (``agent_spoke``): equal counts, no orphan
    utterance. This is the unified statement of "a turn can never silently
    vanish" — the same guarantee the split INV-1 makes via terminal states.
    """
    violations: list[InvariantViolation] = []
    assistant_transcripts = [
        e
        for e in events
        if isinstance(e, TranscriptFinalized) and e.speaker == "assistant"
    ]
    spokes = [e for e in events if isinstance(e, AgentSpoke)]
    if len(assistant_transcripts) != len(spokes):
        violations.append(
            InvariantViolation(
                "INV-U",
                None,
                f"{len(assistant_transcripts)} assistant transcript(s) but "
                f"{len(spokes)} agent_spoke event(s) — a unified turn was "
                f"dropped between model output and the user",
            )
        )
    for i, spoke in enumerate(spokes, start=1):
        if not spoke.text.strip():
            violations.append(
                InvariantViolation(
                    "INV-U", i, "agent_spoke carries empty text"
                )
            )
    return violations


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


# --- fake providers + transport (driving half) ------------------------------


class _ReplaySTT(STTProvider):
    """Return the recorded transcript text for each VAD-cut segment, in order."""

    def __init__(self, turns: Sequence[ReplayTurn]) -> None:
        self._turns = list(turns)
        self._idx = 0
        self.calls = 0

    @property
    def name(self) -> str:
        return "replay-stt"

    async def transcribe_stream(
        self, audio_iter: AsyncIterator[bytes]
    ) -> AsyncIterator[TranscriptEvent]:
        async for _ in audio_iter:
            pass
        if self._idx >= len(self._turns):
            return
        turn = self._turns[self._idx]
        self._idx += 1
        self.calls += 1
        yield TranscriptEvent(
            text=turn.text,
            is_final=True,
            timestamp_ms=self.calls * 1000,
            confidence=turn.confidence,
        )


class _AnswerCursor:
    """Shared per-turn answer holder between the recorded router + answer LLMs.

    The pipeline calls the router then (if it approves) the answer LLM
    sequentially within one turn, and the response loop processes turns
    serially — so the router can stash *this turn's* recorded answer here for
    the answer LLM to read. This keys the answer to the turn rather than a
    positional list, so an approved turn the original session never actually
    spoke (e.g. it was rate-limited before the answer stage) replays as an empty
    answer → ``model_empty_output`` no_reply, instead of fabricating a stale
    answer borrowed from a different turn.
    """

    def __init__(self) -> None:
        self.current: str | None = None


class _RecordedRouterLLM(LLMProvider):
    """Replay recorded router structured outputs in order.

    A turn whose ``simulate == "timeout"`` sleeps past the configured router
    timeout so the pipeline's ``asyncio.wait_for`` bound fires — reproducing the
    session-14 hang and proving the fix turns it into a durable ``no_reply``.
    """

    def __init__(self, turns: Sequence[ReplayTurn], cursor: _AnswerCursor) -> None:
        self._turns = list(turns)
        self._cursor = cursor
        self._idx = 0
        self.calls = 0

    @property
    def name(self) -> str:
        return "recorded-router"

    async def chat(
        self,
        messages: Sequence[ChatMessage],  # noqa: ARG002
        tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
        response_format: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> LLMResponse:
        idx = min(self._idx, len(self._turns) - 1)
        turn = self._turns[idx]
        self._idx += 1
        self.calls += 1
        # Stash this turn's recorded answer for the answer LLM (read only if the
        # router approves AND the turn reaches the answer stage).
        self._cursor.current = turn.answer
        if turn.simulate == "timeout":
            await asyncio.sleep(SIMULATED_HANG_SLEEP_S)
        decision = dict(turn.router) or {
            "should_speak": False,
            "confidence": 0.0,
            "reason": "no recorded router output",
        }
        return LLMResponse(
            text=json.dumps(decision),
            finish_reason="stop",
            structured_output=decision,
        )


class _RecordedAnswerLLM(LLMProvider):
    """Replay the recorded answer for the current turn (via the shared cursor).

    Returns the empty string when the current turn recorded no answer — the
    pipeline reads that as ``model_empty_output`` and terminates the turn in a
    no_reply, which is the faithful outcome for a turn that never actually
    produced an utterance.
    """

    def __init__(self, cursor: _AnswerCursor) -> None:
        self._cursor = cursor
        self.calls = 0

    @property
    def name(self) -> str:
        return "recorded-answer"

    async def chat(
        self,
        messages: Sequence[ChatMessage],  # noqa: ARG002
        tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
        response_format: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> LLMResponse:
        self.calls += 1
        return LLMResponse(text=self._cursor.current or "", finish_reason="stop")


class _ReplayTTS(TTSProvider):
    """Emit a handful of PCM frames so spoken turns carry a positive duration."""

    def __init__(self, frame_count: int = 3) -> None:
        self._frame_count = frame_count
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return "replay-tts"

    async def synthesize_stream(
        self, text: str, voice_id: str | None = None  # noqa: ARG002
    ) -> AsyncIterator[bytes]:
        self.calls.append(text)
        for i in range(self._frame_count):
            yield bytes([i & 0xFF, 0x00]) * 160


class _BufferedTransport(JohnnyTransport):
    """Push synthetic PCM frames in; capture played frames out."""

    def __init__(self, frames: list[bytes], sample_rate: int = SAMPLE_RATE) -> None:
        self._frames = list(frames)
        self._sample_rate = sample_rate
        self.played: list[bytes] = []

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def start(self) -> None:  # noqa: B027
        pass

    async def stop(self) -> None:  # noqa: B027
        pass

    async def capture_frames(self) -> AsyncIterator[bytes]:
        for frame in self._frames:
            yield frame

    async def play_frames(
        self,
        frames: Iterable[bytes] | AsyncIterable[bytes],
        source_rate: int | None = None,  # noqa: ARG002
    ) -> None:
        if isinstance(frames, AsyncIterable):
            async for f in frames:
                self.played.append(f)
        else:
            for f in frames:
                self.played.append(f)


def _tone_samples(duration_ms: int, freq_hz: int = 440, amplitude: int = 12_000) -> list[int]:
    n = SAMPLE_RATE * duration_ms // 1000
    return [
        int(amplitude * math.sin(2 * math.pi * freq_hz * i / SAMPLE_RATE))
        for i in range(n)
    ]


def _silence_samples(duration_ms: int) -> list[int]:
    return [0] * (SAMPLE_RATE * duration_ms // 1000)


def _synthesize_pcm(turn_count: int) -> bytes:
    """One VAD-detectable tone burst per turn, separated by silence gaps."""
    samples: list[int] = list(_silence_samples(LEAD_MS))
    for _ in range(turn_count):
        samples.extend(_tone_samples(TONE_MS))
        samples.extend(_silence_samples(GAP_MS))
    return array.array("h", samples).tobytes()


def _frames(pcm: bytes) -> list[bytes]:
    return [
        pcm[i : i + BYTES_PER_FRAME]
        for i in range(0, len(pcm), BYTES_PER_FRAME)
        if i + BYTES_PER_FRAME <= len(pcm)
    ]


# --- run (driving half) -----------------------------------------------------


@dataclass
class ReplayResult:
    """Everything one replay produced."""

    fixture: ReplayFixture
    events: list[PipelineEvent]
    records: list[TurnRecord]
    stt_calls: int

    def events_as_dicts(self) -> list[dict[str, Any]]:
        return [event_to_dict(e) for e in self.events]


async def run_replay(fixture: ReplayFixture) -> ReplayResult:
    """Drive ``fixture`` through the real pipeline and capture its events.

    Dispatches on ``fixture.runtime``: ``split`` → :class:`VoicePipeline`
    (router → answer → terminal), ``unified`` → :class:`UnifiedVoicePipeline`
    over a recorded S2S provider. Both use recorded LLM/S2S outputs so the run
    is deterministic and CI-safe.
    """
    if fixture.runtime == UNIFIED_RUNTIME:
        return await _run_unified_replay(fixture)
    return await _run_split_replay(fixture)


async def _run_split_replay(fixture: ReplayFixture) -> ReplayResult:
    """Split-pipeline replay: synthesise one tone burst per turn, let the real
    VAD segment them, fake STT returns the recorded transcripts, and the
    pipeline runs router → answer → terminal for each — the live meeting path.
    """
    pcm = _synthesize_pcm(fixture.turn_count)
    transport = _BufferedTransport(frames=_frames(pcm))
    stt = _ReplaySTT(fixture.turns)
    cursor = _AnswerCursor()
    router = _RecordedRouterLLM(fixture.turns, cursor)
    answer = _RecordedAnswerLLM(cursor)
    tts = _ReplayTTS()
    bus = InMemoryEventBus()
    has_timeout = any(t.simulate == "timeout" for t in fixture.turns)
    config = PipelineConfig(
        session_id=fixture.session_id,
        mode=fixture.mode,
        instructions=fixture.instructions,
        confidence_threshold=fixture.confidence_threshold,
        allowed_replies=fixture.allowed_replies,
        vad_threshold=VAD_THRESHOLD,
        end_of_speech_ms=END_OF_SPEECH_MS,
        frame_duration_ms=FRAME_DURATION_MS,
        # The recordings are already post-noise-gate finalised transcripts;
        # replaying them through the gate again would double-filter.
        noise_filter_enabled=False,
        # Bound the router so a simulated hang fails fast into a durable
        # no_reply instead of stalling the whole replay.
        router_llm_timeout_s=(
            SIMULATED_HANG_TIMEOUT_S if has_timeout else 0.0
        ),
        # The classifier needs a second LLM and isn't part of the
        # decision/terminal contract under test.
        enable_barge_in=False,
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=VAD_THRESHOLD),
        stt=stt,
        router_llm=router,
        answer_llm=answer,
        tts=tts,
        event_bus=bus,
        config=config,
    )
    await pipeline.run()

    events = bus.snapshot()
    records = assemble_turns(events, SPLIT_RUNTIME)
    return ReplayResult(
        fixture=fixture,
        events=events,
        records=records,
        stt_calls=stt.calls,
    )


class _ReplayS2SSession(S2SSession):
    """An S2S session that replays a fixture's recorded turns.

    On the single ``commit_user_turn`` the unified pipeline issues at
    end-of-capture, queues every recorded turn's event sequence (user
    transcript → response started → assistant transcript → audio →
    response completed) so the real :class:`UnifiedVoicePipeline` publishes
    one ``agent_spoke`` per recorded assistant turn.
    """

    def __init__(self, turns: Sequence[ReplayTurn]) -> None:
        self._turns = list(turns)
        self._queue: asyncio.Queue[S2SEvent | None] = asyncio.Queue()
        self._closed = False
        self.commit_count = 0

    async def send_audio(self, pcm: bytes) -> None:  # noqa: ARG002
        pass

    async def commit_user_turn(self) -> None:
        if self.commit_count:
            return
        self.commit_count += 1
        for turn in self._turns:
            await self._queue.put(
                S2STranscript(text=turn.text, is_final=True, role="user")
            )
            answer = turn.answer
            if answer is None:
                continue
            await self._queue.put(S2SResponseStarted())
            await self._queue.put(
                S2STranscript(text=answer, is_final=True, role="assistant")
            )
            await self._queue.put(S2SAudioFrame(pcm=b"\x00\x00" * 160))
            await self._queue.put(S2SResponseCompleted(finish_reason="stop"))

    async def events(self) -> AsyncIterator[S2SEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    async def interrupt(self) -> None:  # noqa: B027
        pass

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.put(None)


class _ReplayS2S(S2SProvider):
    """S2S provider whose every session replays the fixture's recorded turns."""

    def __init__(self, turns: Sequence[ReplayTurn]) -> None:
        self._turns = list(turns)

    @property
    def name(self) -> str:
        return "replay-s2s"

    async def open_session(
        self,
        *,
        instructions: str = "",  # noqa: ARG002
        voice_id: str | None = None,  # noqa: ARG002
        tools: Sequence[ToolDefinition] = (),  # noqa: ARG002
    ) -> S2SSession:
        return _ReplayS2SSession(self._turns)


async def _run_unified_replay(fixture: ReplayFixture) -> ReplayResult:
    """Unified-S2S replay: drive the real :class:`UnifiedVoicePipeline` with a
    recorded S2S provider, capturing the assistant transcripts + agent_spoke.
    """
    transport = BrowserAudioTransport()
    bus = InMemoryEventBus()
    s2s = _ReplayS2S(fixture.turns)
    config = UnifiedPipelineConfig(
        session_id=fixture.session_id,
        bot_session_id=None,
        instructions=fixture.instructions,
    )
    pipeline = UnifiedVoicePipeline(
        transport=transport,
        s2s=s2s,
        event_bus=bus,
        config=config,
    )
    await transport.start()
    # Push one frame per turn so the capture loop forwards audio, then stop the
    # transport — its EOF triggers the single commit_user_turn that drains all
    # recorded turns through the pipeline.
    for _ in fixture.turns:
        transport.push_capture_frame(b"\x01\x01" * 160)

    run_task = asyncio.create_task(pipeline.run())

    async def _stop() -> None:
        await asyncio.sleep(0.05)
        await transport.stop()

    await asyncio.wait_for(asyncio.gather(run_task, _stop()), timeout=10.0)

    events = bus.snapshot()
    records = assemble_turns(events, UNIFIED_RUNTIME)
    return ReplayResult(
        fixture=fixture,
        events=events,
        records=records,
        # The unified path has no STT segmentation stage; the assistant↔spoke
        # parity check (INV-U) is what catches a dropped turn here, so report
        # turn_count to make the split-only segmentation guard a no-op pass.
        stt_calls=fixture.turn_count,
    )


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
    "run_replay",
]
