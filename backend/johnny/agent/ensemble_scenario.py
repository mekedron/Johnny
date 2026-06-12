"""Fixture-driven multi-agent ensemble scenario (Johnny-trt.48).

The repeatable regression for the multi-agent foundation (Johnny-trt.46) and
the turn-arbitration work tuned on top of it (Johnny-trt.47): build N REAL
:class:`~johnny.agent.browser_session.BrowserAgentSession` engines — the
exact objects a playground group runs — wire them through the production
:class:`~johnny.voice_pipeline.group_audio.GroupAudioRouter` and one shared
speech floor, replay a scripted sequence of utterances addressing different
agents, and assert the conversation dynamics programmatically:

* **never overlap** — floor hold intervals (stamped on the receiver clock as
  the events arrive) are pairwise non-overlapping across members, and every
  member's outbound audio frames land inside its own holds;
* **never loop** — a member opens router turns only for the scripted
  utterances; the co-agent's speech (cross-fed through the router's capture
  mixer as real audio) produces peer-labeled transcripts and
  ``PeerSpeechSuppressed`` accounting, never a turn;
* **claims** (Johnny-trt.47 forward seam) — ``TurnClaimWon`` / ``TurnClaimLost``
  events are collected per step; today's strict-v1 build emits none, and the
  per-step ``addressed_to`` field is carried so the arbitration task can flip
  the expectation to "exactly the addressed agent answers" without reshaping
  the fixture.

Following the Phase-0 harness pattern (:mod:`johnny.agent.latency_harness`),
providers are in-process stubs threaded through the real registry → adapter
path — with one twist: the scenario's TTS streams a REAL recorded speech
fixture (a bundled piper utterance) instead of silence, because the peers'
Silero VAD must classify the cross-fed audio as speech for the suppression
seam to be exercised at all (DSP synthetics produce zero VAD events —
Johnny-trt.2). Utterances are typed (``feed_text``, fanned concurrently like
the group text endpoint) so the script is deterministic; the *peer* audio
path — the part multi-agent coordination actually adds — runs on genuine
audio end to end.

The floor backend is the in-memory hub by default (hermetic, CI-friendly:
``tests/integration/test_ensemble_scenario.py``) or real Redis with the
production ``browser-group-*`` scope via ``--redis`` for stack parity runs::

    docker compose exec api python -m johnny.agent.ensemble_scenario
    docker compose exec api python -m johnny.agent.ensemble_scenario --redis \
        --json-out /tmp/ensemble.json

Requires the ``agent`` extra (``livekit-agents``); imported only where that
extra is installed, never from the import-safe top-level :mod:`johnny.agent`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.providers.base import (
    PCM_SAMPLE_RATE_HZ,
    ProviderConfig,
    ProviderKind,
    TTSProvider,
    get_registry,
)
from johnny.agent.job_config import AUTONOMOUS_MODE, SessionJobConfig
from johnny.agent.latency_harness import (
    BUNDLED_FIXTURES,
    register_stub_providers,
    stub_provider_config,
)
from johnny.agent.speech_floor import InMemoryFloorBackend, InMemoryFloorHub
from johnny.voice_pipeline.browser_transport import BrowserAudioTransport
from johnny.voice_pipeline.event_bus import EventBus
from johnny.voice_pipeline.events import (
    AgentSpoke,
    FloorAcquired,
    FloorExpired,
    FloorReleased,
    PeerSpeechSuppressed,
    PipelineEvent,
    RouterDecisionMade,
    TranscriptFinalized,
    TurnClaimLost,
    TurnClaimWon,
    TurnTerminal,
)
from johnny.voice_pipeline.group_audio import GroupAudioRouter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from livekit.agents.vad import VAD

    from johnny.agent.browser_session import BrowserAgentSession

logger = logging.getLogger(__name__)

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

DEFAULT_SCENARIO_PATH = _FIXTURES_DIR / "ensemble_addressing.json"

SCENARIO_TTS_PROVIDER_NAME = "ensemble-scenario-tts"

SCENARIO_TTS_FIXTURE = "short1"
"""Which bundled piper utterance every stub reply 'speaks' (~1.9 s). Real
speech is load-bearing: the peers' VAD must fire on the cross-feed."""


class ScenarioSpeechTTSProvider(TTSProvider):
    """Stub TTS that streams a real recorded speech fixture for ANY text.

    The reply audio's CONTENT is irrelevant to the scenario (the floor and
    the suppression windows don't read it) but its NATURE is not: it must be
    something Silero classifies as speech once the group router cross-feeds
    it into the peers' captures. 100 ms chunks after a small first-byte
    delay, mirroring the harness stub's shape.
    """

    def __init__(self, config: ProviderConfig | None = None) -> None:
        options = dict(config.options) if config is not None else {}
        fixture = str(options.get("audio_fixture") or SCENARIO_TTS_FIXTURE)
        path = BUNDLED_FIXTURES.get(fixture, BUNDLED_FIXTURES[SCENARIO_TTS_FIXTURE])
        self._pcm = path.read_bytes()
        self._first_byte_delay_s = 0.04

    @property
    def name(self) -> str:
        return SCENARIO_TTS_PROVIDER_NAME

    async def synthesize_stream(
        self, text: str, voice_id: str | None = None
    ) -> AsyncIterator[bytes]:
        if self._first_byte_delay_s:
            await asyncio.sleep(self._first_byte_delay_s)
        chunk_bytes = int(PCM_SAMPLE_RATE_HZ * 0.1) * 2  # 100 ms
        for start in range(0, len(self._pcm), chunk_bytes):
            yield self._pcm[start : start + chunk_bytes]


def register_scenario_tts() -> None:
    get_registry().register(
        ProviderKind.TTS,
        SCENARIO_TTS_PROVIDER_NAME,
        ScenarioSpeechTTSProvider,
        replace=True,
    )


class RecordingBus(EventBus):
    """In-memory bus that stamps each event's ARRIVAL on a shared clock.

    Floor events carry per-session-relative ``timestamp_ms`` — useless for
    cross-member interval comparison. Publish happens inline on the acquire/
    release paths, so the arrival stamp (one ``time.monotonic`` clock shared
    by every member's bus) is the honest common timeline.
    """

    def __init__(self) -> None:
        self.records: list[tuple[float, PipelineEvent]] = []

    async def publish(self, event: PipelineEvent) -> None:
        self.records.append((time.monotonic(), event))

    def events(self) -> list[PipelineEvent]:
        return [event for _, event in self.records]

    def of_type(self, cls: type) -> list[tuple[float, Any]]:
        return [(t, e) for t, e in self.records if isinstance(e, cls)]


# --- Scenario shape ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScenarioStep:
    """One scripted utterance said to the whole room."""

    text: str
    addressed_to: str | None = None
    """Display name of the agent this utterance addresses (``None`` = the
    room). Strict-v1 makes no routing decision on it; Johnny-trt.47's
    selectivity tuning asserts against it."""
    settle_timeout_s: float = 60.0


@dataclass(frozen=True, slots=True)
class ScenarioAgent:
    name: str
    context: str = ""


@dataclass(frozen=True, slots=True)
class EnsembleScenario:
    name: str
    agents: tuple[ScenarioAgent, ...]
    steps: tuple[ScenarioStep, ...]


def load_scenario(path: Path) -> EnsembleScenario:
    data = json.loads(path.read_text())
    agents = tuple(
        ScenarioAgent(name=str(a["name"]), context=str(a.get("context") or ""))
        for a in data["agents"]
    )
    steps = tuple(
        ScenarioStep(
            text=str(s["text"]),
            addressed_to=(str(s["addressed_to"]) if s.get("addressed_to") else None),
            settle_timeout_s=float(s.get("settle_timeout_s") or 60.0),
        )
        for s in data["steps"]
    )
    if len(agents) < 2:
        raise ValueError("an ensemble scenario needs at least two agents")
    return EnsembleScenario(name=str(data.get("name") or path.stem), agents=agents, steps=steps)


# --- Results -----------------------------------------------------------------


@dataclass(slots=True)
class MemberRecord:
    """Everything one member did during the run, on the shared clock."""

    name: str
    session_id: str
    floor_holds: list[tuple[float, float]] = field(default_factory=list)
    """(acquired_at, released_at) arrival-stamped intervals."""
    audio_spans: list[tuple[float, float]] = field(default_factory=list)
    """(first_frame, last_frame) per outbound burst, from the router tap."""
    spoke: list[str] = field(default_factory=list)
    decisions: int = 0
    peer_labeled_finals: list[tuple[str, str]] = field(default_factory=list)
    """(speaker_label, text) of finals attributed to a peer."""
    suppressions: list[PeerSpeechSuppressed] = field(default_factory=list)
    claims_won: list[TurnClaimWon] = field(default_factory=list)
    claims_lost: list[TurnClaimLost] = field(default_factory=list)
    floor_expired: int = 0


@dataclass(slots=True)
class EnsembleResult:
    scenario: EnsembleScenario
    members: list[MemberRecord]
    floor_mode: str = "in-memory"
    problems: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.problems


def _intervals_overlap(
    a: tuple[float, float], b: tuple[float, float], tolerance_s: float
) -> bool:
    return a[0] < b[1] - tolerance_s and b[0] < a[1] - tolerance_s


def evaluate_result(result: EnsembleResult, *, overlap_tolerance_s: float = 0.05) -> None:
    """Run the trt.46 invariant checks; append human-readable problems.

    Mechanical restatements of the acceptance phrasing: hold intervals never
    overlap, audio stays inside the speaker's own holds, peer speech opens no
    turns, and a peer's audio was actually heard + labeled (the suppression
    machinery demonstrably ran — a vacuous pass is a failure here).
    """
    members = result.members
    steps = len(result.scenario.steps)

    # 1. Floor holds pairwise non-overlapping across members.
    for i, first in enumerate(members):
        for second in members[i + 1 :]:
            for hold_a in first.floor_holds:
                for hold_b in second.floor_holds:
                    if _intervals_overlap(hold_a, hold_b, overlap_tolerance_s):
                        result.problems.append(
                            f"floor overlap: {first.name} held {hold_a} while "
                            f"{second.name} held {hold_b}"
                        )

    # 2. Outbound audio only while holding the floor (0.25 s slack for the
    # release broadcast landing after the last frame's arrival).
    for member in members:
        for span in member.audio_spans:
            inside = any(
                hold[0] - 0.25 <= span[0] and span[1] <= hold[1] + 0.25
                for hold in member.floor_holds
            )
            if not inside:
                result.problems.append(
                    f"{member.name}: audio span {span} outside every floor hold "
                    f"{member.floor_holds}"
                )

    # 3. Strict v1 loop rule: router turns == scripted utterances, exactly.
    for member in members:
        if member.decisions != steps:
            result.problems.append(
                f"{member.name}: {member.decisions} router turns for {steps} "
                "scripted utterances — peer speech opened a turn (or one was lost)"
            )

    # 4. The cross-feed demonstrably ran: every member whose peer spoke saw
    # at least one peer-labeled final (window attribution at the STT seam).
    for member in members:
        peers_spoke = any(other.spoke for other in members if other is not member)
        if peers_spoke and not member.peer_labeled_finals:
            result.problems.append(
                f"{member.name}: peers spoke but no peer-labeled transcript "
                "arrived — the suppression seam was not exercised"
            )

    # 5. Nobody's lease lapsed (a crash-path event in a healthy run).
    for member in members:
        if member.floor_expired:
            result.problems.append(
                f"{member.name}: {member.floor_expired} FloorExpired event(s) "
                "in a healthy run"
            )

    # 6. Every speak verdict actually played out — the handoff-shield
    # regression pin: without shield_handle_through_peer_tail the second
    # speaker's reply is insta-cut at every floor handoff (the SDK reads the
    # previous holder's trailing audio as a live barge-in), terminalizing
    # no_reply(barge_in) with a ~10 ms hold and zero replies spoken.
    for member in members:
        if len(member.spoke) != steps:
            result.problems.append(
                f"{member.name}: spoke {len(member.spoke)} replies for {steps} "
                "scripted utterances — a reply was cut or suppressed "
                "(floor-handoff shield regression?)"
            )


# --- The runner ----------------------------------------------------------------


async def run_ensemble_scenario(
    scenario: EnsembleScenario,
    *,
    use_redis: bool = False,
    vad: VAD | None = None,
    settle_gap_s: float = 1.0,
    base_session_id: int = 900_000,
) -> EnsembleResult:
    """Build the group, replay the steps, return the recorded dynamics.

    ``use_redis=False`` (default) shares one in-memory floor hub — hermetic,
    no services needed beyond the in-image models. ``use_redis=True`` builds
    the production ``RedisFloorBackend`` path with a unique
    ``browser-group-*`` scope against the stack's Redis.
    """
    from app.config import get_settings
    from johnny.agent.browser_session import BrowserAgentSession, load_browser_vad

    register_stub_providers()
    register_scenario_tts()
    # Synthetic sessions: reply-audio WAVs under the session audio dir would
    # be junk (latency-harness precedent).
    os.environ.pop("JOHNNY_SESSION_AUDIO_DIR", None)

    provider_config = stub_provider_config()
    provider_config["tts"] = {
        "provider_name": SCENARIO_TTS_PROVIDER_NAME,
        "display_name": "Ensemble scenario speech TTS",
        "credentials": {},
        "options": {},
    }

    floor_scope = f"browser-group-ensemble-{uuid.uuid4().hex[:8]}"
    hub = InMemoryFloorHub()
    redis_url: str | None = None
    if use_redis:
        redis_url = get_settings().redis_url

    if vad is None:
        vad = load_browser_vad()

    audio_tap: list[tuple[float, int, int]] = []  # (t, member_index, n_bytes)

    def _tap(member_id: int, frame: bytes) -> None:
        audio_tap.append((time.monotonic(), member_id, len(frame)))

    router = GroupAudioRouter(on_playback_frame=_tap)

    buses: list[RecordingBus] = []
    sessions: list[BrowserAgentSession] = []
    transports: list[BrowserAudioTransport] = []
    member_ids: list[int] = []
    result = EnsembleResult(
        scenario=scenario,
        members=[],
        floor_mode="redis" if use_redis else "in-memory",
    )

    try:
        for index, agent in enumerate(scenario.agents):
            member_id = base_session_id + index
            config = SessionJobConfig(
                bot_session_id=member_id,
                room_name=f"ensemble-{scenario.name}-{index}",
                agent_snapshot={
                    "name": agent.name,
                    "mode": AUTONOMOUS_MODE,
                    "assignment_context": agent.context
                    or "You are in a scripted ensemble regression run.",
                },
                provider_config=provider_config,
                redis_url=redis_url,
            )
            bus = RecordingBus()
            transport = BrowserAudioTransport()
            await transport.start()
            session = await BrowserAgentSession.build(
                transport,
                config,
                event_bus=bus,
                vad=vad,
                task_wiring=False,
                floor_scope=floor_scope,
                floor_backend=None if use_redis else InMemoryFloorBackend(hub),
            )
            buses.append(bus)
            transports.append(transport)
            sessions.append(session)
            member_ids.append(member_id)

        for session in sessions:
            await session.start()
        for member_id, transport in zip(member_ids, transports, strict=True):
            router.add_member(member_id, transport)

        for step_index, step in enumerate(scenario.steps):
            logger.info(
                "step %d/%d: %r (addressed_to=%s)",
                step_index + 1,
                len(scenario.steps),
                step.text,
                step.addressed_to,
            )
            await asyncio.gather(
                *(session.feed_text(step.text) for session in sessions)
            )
            await _wait_for_step_settle(
                buses, expected_terminals=step_index + 1, timeout_s=step.settle_timeout_s
            )
            await asyncio.sleep(settle_gap_s)

        # Let trailing suppression sweeps land (window tail 2 s + sweep 0.5 s).
        await asyncio.sleep(3.0)
    finally:
        for session in sessions:
            try:
                await session.aclose()
            except Exception:  # noqa: BLE001 — teardown best-effort
                logger.exception("scenario: session aclose failed")
        for transport in transports:
            try:
                await transport.stop()
                transport.close_playback()
            except Exception:  # noqa: BLE001 — teardown best-effort
                logger.exception("scenario: transport stop failed")
        router.close()

    result.members = [
        _collect_member(
            scenario.agents[index].name,
            member_ids[index],
            buses[index],
            audio_tap,
        )
        for index in range(len(scenario.agents))
    ]
    evaluate_result(result)
    return result


async def _wait_for_step_settle(
    buses: list[RecordingBus], *, expected_terminals: int, timeout_s: float
) -> None:
    """Wait until every member settled this step's turn and freed the floor."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        terminals_done = all(
            len(bus.of_type(TurnTerminal)) >= expected_terminals for bus in buses
        )
        floor_free = all(
            len(bus.of_type(FloorAcquired)) == len(bus.of_type(FloorReleased))
            for bus in buses
        )
        if terminals_done and floor_free:
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(
        f"step did not settle within {timeout_s:.0f}s: terminals="
        f"{[len(b.of_type(TurnTerminal)) for b in buses]} acquired/released="
        f"{[(len(b.of_type(FloorAcquired)), len(b.of_type(FloorReleased))) for b in buses]}"
    )


def _collect_member(
    name: str,
    member_id: int,
    bus: RecordingBus,
    audio_tap: list[tuple[float, int, int]],
) -> MemberRecord:
    record = MemberRecord(name=name, session_id=str(member_id))

    acquired_at: float | None = None
    for t, event in bus.records:
        if isinstance(event, FloorAcquired):
            acquired_at = t
        elif isinstance(event, FloorReleased) and acquired_at is not None:
            record.floor_holds.append((acquired_at, t))
            acquired_at = None
        elif isinstance(event, FloorExpired):
            record.floor_expired += 1
        elif isinstance(event, AgentSpoke):
            record.spoke.append(event.text)
        elif isinstance(event, RouterDecisionMade):
            record.decisions += 1
        elif isinstance(event, PeerSpeechSuppressed):
            record.suppressions.append(event)
        elif isinstance(event, TurnClaimWon):
            record.claims_won.append(event)
        elif isinstance(event, TurnClaimLost):
            record.claims_lost.append(event)
        elif (
            isinstance(event, TranscriptFinalized)
            and event.speaker is not None
            and event.speaker not in ("user", "speaker")
        ):
            record.peer_labeled_finals.append((event.speaker, event.text))
    if acquired_at is not None:
        # Hold never released — surface as a degenerate interval; the
        # evaluation's audio/overlap checks will flag it.
        record.floor_holds.append((acquired_at, acquired_at))

    # Burst spans from the router tap: frames for this member, split into
    # spans wherever the gap exceeds 1 s (replies are bursts; 1 s cleanly
    # separates two turns without splitting one TTS stream).
    frames = sorted(t for t, mid, _ in audio_tap if mid == member_id)
    span_start: float | None = None
    last: float | None = None
    for t in frames:
        if span_start is None:
            span_start = t
        elif last is not None and t - last > 1.0:
            record.audio_spans.append((span_start, last))
            span_start = t
        last = t
    if span_start is not None and last is not None:
        record.audio_spans.append((span_start, last))
    return record


# --- Reporting -----------------------------------------------------------------


def render_report(result: EnsembleResult) -> str:
    lines = [
        f"Ensemble scenario: {result.scenario.name}",
        f"Floor backend:     {result.floor_mode}",
        f"Steps:             {len(result.scenario.steps)}",
        "",
    ]
    for member in result.members:
        lines.append(f"[{member.name}] session={member.session_id}")
        lines.append(
            f"  floor holds:        {[(round(a, 2), round(b, 2)) for a, b in member.floor_holds]}"
        )
        lines.append(f"  replies spoken:     {len(member.spoke)}")
        lines.append(f"  router turns:       {member.decisions}")
        lines.append(
            f"  peer finals:        {len(member.peer_labeled_finals)} "
            f"{[s for s, _ in member.peer_labeled_finals]}"
        )
        lines.append(
            "  suppressions:       "
            + ", ".join(
                f"{s.peer} (window {s.window_ms} ms, {s.text_match_hits} text hits)"
                for s in member.suppressions
            )
            if member.suppressions
            else "  suppressions:       none reported (sweep may aggregate)"
        )
        lines.append(
            f"  claims won/lost:    {len(member.claims_won)}/{len(member.claims_lost)}"
        )
        lines.append("")
    lines.append("PASS" if result.passed else "FAIL")
    for problem in result.problems:
        lines.append(f"  - {problem}")
    return "\n".join(lines)


def result_to_json(result: EnsembleResult) -> dict[str, Any]:
    return {
        "scenario": result.scenario.name,
        "floor_mode": result.floor_mode,
        "passed": result.passed,
        "problems": list(result.problems),
        "steps": [
            {"text": s.text, "addressed_to": s.addressed_to}
            for s in result.scenario.steps
        ],
        "members": [
            {
                "name": m.name,
                "session_id": m.session_id,
                "floor_holds": m.floor_holds,
                "audio_spans": m.audio_spans,
                "replies": m.spoke,
                "router_turns": m.decisions,
                "peer_labeled_finals": m.peer_labeled_finals,
                "suppressions": [
                    {
                        "peer": s.peer,
                        "window_ms": s.window_ms,
                        "text_match_hits": s.text_match_hits,
                    }
                    for s in m.suppressions
                ],
                "claims_won": len(m.claims_won),
                "claims_lost": len(m.claims_lost),
                "floor_expired": m.floor_expired,
            }
            for m in result.members
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        type=Path,
        default=DEFAULT_SCENARIO_PATH,
        help="scenario fixture JSON (default: the bundled addressing fixture)",
    )
    parser.add_argument(
        "--redis",
        action="store_true",
        help="use the stack's real Redis floor backend (production parity)",
    )
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    scenario = load_scenario(args.scenario)
    result = asyncio.run(run_ensemble_scenario(scenario, use_redis=args.redis))
    print(render_report(result))
    if args.json_out is not None:
        args.json_out.write_text(json.dumps(result_to_json(result), indent=2))
        print(f"JSON written to {args.json_out}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DEFAULT_SCENARIO_PATH",
    "EnsembleResult",
    "EnsembleScenario",
    "MemberRecord",
    "RecordingBus",
    "ScenarioAgent",
    "ScenarioSpeechTTSProvider",
    "ScenarioStep",
    "evaluate_result",
    "load_scenario",
    "render_report",
    "result_to_json",
    "run_ensemble_scenario",
]
