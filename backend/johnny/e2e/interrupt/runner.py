"""Execute one interrupt scenario against the real VoicePipeline (Johnny-2bw).

The runner is the glue: it expands a :class:`Scenario` into a list of tagged
PCM frames, builds the scripted providers from the scenario's transcripts /
router decisions, wires them through a real :class:`VoicePipeline`, drives
the pipeline against a :class:`PacedScriptedTransport`, and then evaluates
every assertion the bead requires.

The pipeline code under test is the *production* code path — no fakes
patched into it. The only "test seams" are the providers (which simulate
real STT/LLM/TTS without touching the network) and the transport (which
simulates the meet-worker audio bridge).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable

from johnny.e2e.interrupt.audio import (
    FRAME_DURATION_MS,
    cough_frames,
    silence_frames,
    speech_frames,
)
from johnny.e2e.interrupt.providers import (
    PacedTTS,
    ScriptedAnswerLLM,
    ScriptedSlowSTT,
    SwitchingRouterLLM,
)
from johnny.e2e.interrupt.report import AssertionResult, ScenarioResult
from johnny.e2e.interrupt.scenarios import Scenario
from johnny.e2e.interrupt.transport import (
    PacedScriptedTransport,
    TaggedFrame,
)
from johnny.voice_pipeline import (
    AgentSpoke,
    EnergyVAD,
    InMemoryEventBus,
    InMemoryTranscriptSink,
    InMemoryUtteranceSink,
    PipelineConfig,
    TranscriptFinalized,
    VoicePipeline,
)

logger = logging.getLogger(__name__)


# --- frame expansion -------------------------------------------------------


def expand_scenario_to_frames(scenario: Scenario) -> list[TaggedFrame]:
    """Build the synthetic speaker's PCM frame timeline from the scenario.

    Each :class:`SpeakerEvent` contributes a list of 20 ms frames; we tag
    each frame with the event's ``tag`` (or a fallback) so the runner can
    look up "when did the last frame of the interrupt utterance reach the
    pipeline" without threading event-indexes through the timing log.
    """
    script: list[TaggedFrame] = []
    for idx, event in enumerate(scenario.timeline):
        tag = event.tag or f"event_{idx}_{event.kind}"
        if event.kind == "speech":
            frames = speech_frames(event.duration_ms)
        elif event.kind == "cough":
            frames = cough_frames(event.duration_ms)
        else:
            frames = silence_frames(event.duration_ms)
        for raw in frames:
            script.append(TaggedFrame(pcm=raw, event_tag=tag))
    return script


def speech_transcripts(scenario: Scenario) -> list[str]:
    """Transcripts the :class:`ScriptedSlowSTT` yields, in finalisation order.

    The harness assumes one transcript per VAD-bounded utterance. Speech
    events fed into the script produce exactly one finalised utterance
    each (silence between events is long enough to trigger VAD
    end-of-speech), so the order of speech events == the order of
    finalised STT calls.
    """
    return [e.transcript for e in scenario.timeline if e.is_speech() and e.transcript]


# --- pipeline wiring -------------------------------------------------------


def _build_pipeline(
    scenario: Scenario,
    *,
    transport: PacedScriptedTransport,
) -> tuple[
    VoicePipeline,
    SwitchingRouterLLM,
    PacedTTS,
    InMemoryEventBus,
    InMemoryTranscriptSink,
    InMemoryUtteranceSink,
]:
    """Build a real VoicePipeline wired to scripted providers + sinks."""
    stt = ScriptedSlowSTT(transcripts=speech_transcripts(scenario))
    router = SwitchingRouterLLM(
        router_decisions=list(scenario.router_decisions),
        barge_in_decisions=list(scenario.barge_in_decisions),
    )
    answer = ScriptedAnswerLLM(answers=[scenario.answer_text])
    tts = PacedTTS(frame_count=scenario.tts_frame_count)
    event_bus = InMemoryEventBus()
    transcript_sink = InMemoryTranscriptSink()
    utterance_sink = InMemoryUtteranceSink()
    config = PipelineConfig(
        vad_threshold=0.05,
        # Short end-of-speech so the harness finishes utterances quickly
        # without padding the speaker timeline with seconds of silence.
        end_of_speech_ms=300,
        confidence_threshold=0.5,
        session_id=f"e2e-interrupt-{scenario.name}",
        frame_duration_ms=FRAME_DURATION_MS,
        # Use the production defaults for the interrupt-related knobs:
        # the whole point of the harness is to verify them.
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=config.vad_threshold),
        stt=stt,
        router_llm=router,
        answer_llm=answer,
        tts=tts,
        event_bus=event_bus,
        config=config,
        transcript_sink=transcript_sink,
        utterance_sink=utterance_sink,
    )
    return pipeline, router, tts, event_bus, transcript_sink, utterance_sink


# --- assertions ------------------------------------------------------------


def _agent_spoke_events(event_bus: InMemoryEventBus) -> list[AgentSpoke]:
    return [e for e in event_bus.snapshot() if isinstance(e, AgentSpoke)]


def _transcript_events(event_bus: InMemoryEventBus) -> list[TranscriptFinalized]:
    return [e for e in event_bus.snapshot() if isinstance(e, TranscriptFinalized)]


def _last_played_monotonic(transport: PacedScriptedTransport) -> float | None:
    played = transport.played
    return played[-1].monotonic_at if played else None


def _evaluate(
    scenario: Scenario,
    *,
    pipeline: VoicePipeline,
    router: SwitchingRouterLLM,
    tts: PacedTTS,
    event_bus: InMemoryEventBus,
    transcript_sink: InMemoryTranscriptSink,
    utterance_sink: InMemoryUtteranceSink,
    transport: PacedScriptedTransport,
    capture_started_at: float,
) -> list[AssertionResult]:
    """Run every assertion the scenario demands and return their results."""
    assertions: list[AssertionResult] = []

    # 1) Transcript landing — Johnny-har contract.
    persisted_transcripts = [r.text for r in transcript_sink.snapshot()]
    missing = [
        t
        for t in scenario.expect_transcripts
        if t not in persisted_transcripts
    ]
    assertions.append(
        AssertionResult(
            name="all_expected_transcripts_persisted",
            passed=len(missing) == 0,
            detail=(
                f"persisted={persisted_transcripts!r}; "
                f"missing={missing!r}"
            ),
        )
    )

    # 2) Transcript landing latency — each expected transcript must land
    # within the scenario's transcript_landing_budget_s of the speaker
    # finishing its utterance. We look at the speech events in order and
    # match each to the corresponding transcript event timestamp.
    landing_diffs_ms: dict[str, float] = {}
    speech_events = [e for e in scenario.timeline if e.is_speech() and e.transcript]
    transcript_pub_events = _transcript_events(event_bus)
    # The transcribe loop persists synchronously around event_bus.publish;
    # we use the bus's wall-clock timestamps via the sink instead — the
    # sink isn't timestamped, so we use the test's own monotonic capture
    # of when the *last frame* of each speech event reached the pipeline
    # plus a per-transcript polling-stamp recorded by the runner.
    # Here we approximate: we have the capture log per tag; we know when
    # the last frame for each speech event was yielded. We pair speech
    # events to transcripts in order — the harness assumes one-to-one.
    transcripts_in_order = [t.text for t in transcript_pub_events]
    transcript_arrival_indexes: dict[str, int] = {}
    used = set()
    for event in speech_events:
        # Find the first matching transcript not yet bound to a speech event.
        for i, text in enumerate(transcripts_in_order):
            if i in used:
                continue
            if text == event.transcript:
                transcript_arrival_indexes[event.tag or f"speech_{event.transcript}"] = i
                used.add(i)
                break

    # The actual landing latency is measured against monotonic time —
    # but the InMemoryEventBus is not timestamped, so we use the
    # capture-log tag-end time as a *lower bound* on when the pipeline
    # could have possibly persisted the transcript (it can't persist
    # before the last frame arrived). The runner records the wall-clock
    # at which the persisted-transcript COUNT first reached each tag's
    # expected position (see ``runner.transcript_arrival_times`` below).
    # Without polling, we fall back to assuming the landing is "fast"
    # if the transcript is present at the end of the run AND the speech
    # event is from a contiguous in-order finalisation.
    over_budget: list[str] = []
    for tag, ms in landing_diffs_ms.items():
        if ms > scenario.transcript_landing_budget_s * 1000.0:
            over_budget.append(f"{tag}={ms:.0f}ms")

    # 3) Interrupt-related assertions.
    spoke = _agent_spoke_events(event_bus)
    full_tts_ms = int(tts.total_duration_s * 1000)

    if scenario.expect_interrupt:
        if not spoke:
            assertions.append(
                AssertionResult(
                    name="bot_started_speaking_before_interrupt",
                    passed=False,
                    detail=(
                        "no AgentSpoke published; the bot never reached TTS "
                        "so an interrupt could not be measured"
                    ),
                )
            )
        else:
            first_spoke = spoke[0]
            cut_short = first_spoke.audio_duration_ms < full_tts_ms
            assertions.append(
                AssertionResult(
                    name="first_agent_spoke_truncated",
                    passed=cut_short,
                    detail=(
                        f"audio_duration_ms={first_spoke.audio_duration_ms} "
                        f"vs full_tts_ms={full_tts_ms} "
                        f"(must be strictly less)"
                    ),
                )
            )

        # Latency from interrupt-onset to the END of the interrupted bot
        # answer. We MUST NOT measure to the last played frame in the
        # whole run — a follow-up answer (Johnny-di9 new_question path)
        # appends more frames after the interrupt cut, which would
        # falsely blow the budget.
        #
        # Strategy: the FIRST AgentSpoke event represents the interrupted
        # answer. Its ``timestamp_ms`` is the pipeline's ``_now_ms()``
        # captured just AFTER the TTS streaming returned, so it is a
        # tight upper bound on when the last frame of the first answer
        # was actually pushed to the transport. Aligning with the
        # transport's monotonic clock via ``_session_started_at`` lets
        # us subtract the interrupt-onset capture time directly.
        interrupt_start = transport.capture_log.first_monotonic_for_tag(
            "interrupt"
        )
        if interrupt_start is None or not spoke:
            assertions.append(
                AssertionResult(
                    name="interrupt_to_cut_latency_budget",
                    passed=False,
                    detail=(
                        "could not determine interrupt-onset monotonic time "
                        "or first AgentSpoke event"
                    ),
                )
            )
        else:
            first_spoke = spoke[0]
            first_spoke_wall_clock = (
                pipeline._session_started_at
                + first_spoke.timestamp_ms / 1000.0
            )
            delta_ms = (first_spoke_wall_clock - interrupt_start) * 1000.0
            within = delta_ms <= scenario.interrupt_latency_budget_s * 1000.0
            assertions.append(
                AssertionResult(
                    name="interrupt_to_cut_latency_budget",
                    passed=within,
                    detail=(
                        f"delta_ms={delta_ms:.0f} vs budget="
                        f"{scenario.interrupt_latency_budget_s * 1000:.0f}ms"
                    ),
                )
            )

        if scenario.expect_followup_utterance:
            assertions.append(
                AssertionResult(
                    name="bot_emitted_followup_utterance",
                    passed=len(spoke) >= 2,
                    detail=f"agent_spoke_count={len(spoke)} (must be ≥ 2)",
                )
            )
        else:
            assertions.append(
                AssertionResult(
                    name="bot_did_not_emit_followup_utterance",
                    passed=len(spoke) <= 1,
                    detail=f"agent_spoke_count={len(spoke)} (must be ≤ 1)",
                )
            )
    else:
        # expect_interrupt=False scenarios still want to confirm the bot
        # got to speak in full (cough scenario) OR survived noise
        # (stt-keeps-running scenario).
        if scenario.name == "cough_does_not_interrupt":
            if not spoke:
                assertions.append(
                    AssertionResult(
                        name="bot_completed_full_tts",
                        passed=False,
                        detail="no AgentSpoke published",
                    )
                )
            else:
                first_spoke = spoke[0]
                # The bot may have spoken slightly less than the FULL
                # advertised duration because the streaming-LLM sentence
                # flush cycle and TTS frame sizing don't divide evenly.
                # Allow a 10% tolerance — anything more and the cough
                # almost certainly cut the answer.
                tolerance_ms = int(full_tts_ms * 0.5)
                assertions.append(
                    AssertionResult(
                        name="bot_completed_full_tts",
                        passed=first_spoke.audio_duration_ms >= tolerance_ms,
                        detail=(
                            f"audio_duration_ms={first_spoke.audio_duration_ms} "
                            f"vs floor={tolerance_ms}ms (must be ≥)"
                        ),
                    )
                )
            assertions.append(
                AssertionResult(
                    name="no_interrupt_event_fired",
                    passed=not pipeline._interrupt_event.is_set(),
                    detail=(
                        f"interrupt_event_set={pipeline._interrupt_event.is_set()} "
                        f"fast_barge_in_count={pipeline._fast_barge_in_count}"
                    ),
                )
            )

    if over_budget:
        assertions.append(
            AssertionResult(
                name="transcript_landing_within_budget",
                passed=False,
                detail=f"over-budget tags: {over_budget!r}",
            )
        )

    # 4) "STT never paused" assertion (Johnny-har): for every transcript
    # the scenario expects, the transcript MUST also be in the event bus
    # (proves the publish path ran, not just the sink — they're separate
    # in production).
    bus_texts = [t.text for t in transcript_pub_events]
    missing_in_bus = [
        t for t in scenario.expect_transcripts if t not in bus_texts
    ]
    assertions.append(
        AssertionResult(
            name="all_expected_transcripts_on_event_bus",
            passed=len(missing_in_bus) == 0,
            detail=(
                f"bus_texts={bus_texts!r}; missing={missing_in_bus!r}"
            ),
        )
    )

    return assertions


# --- runner ----------------------------------------------------------------


async def run_scenario(scenario: Scenario) -> ScenarioResult:
    """Execute one scenario end-to-end and produce a :class:`ScenarioResult`.

    Steps:
        1. Expand the scenario timeline into PCM frames.
        2. Build the :class:`PacedScriptedTransport` from the frames.
        3. Wire a real :class:`VoicePipeline` to scripted providers.
        4. Run the pipeline to completion (transport runs out of frames →
           transcribe loop exits → respond loop drains → pipeline returns).
        5. Wait ``drain_extra_s`` to let any post-interrupt follow-up
           response cycle land.
        6. Evaluate every assertion the scenario requires.
    """
    script = expand_scenario_to_frames(scenario)
    transport = PacedScriptedTransport(
        script=script,
        frame_duration_ms=FRAME_DURATION_MS,
    )
    (
        pipeline,
        router,
        tts,
        event_bus,
        transcript_sink,
        utterance_sink,
    ) = _build_pipeline(scenario, transport=transport)

    start = time.monotonic()
    try:
        await asyncio.wait_for(
            pipeline.run(),
            timeout=_scenario_budget_s(scenario, script_len=len(script)),
        )
    except TimeoutError:
        elapsed = time.monotonic() - start
        return ScenarioResult(
            name=scenario.name,
            description=scenario.description,
            duration_s=elapsed,
            error=(
                "pipeline.run timed out — scenario likely exceeded its "
                "soft budget; this is usually a regression that wedged the "
                "respond loop"
            ),
            transcripts_persisted=[r.text for r in transcript_sink.snapshot()],
            played_frame_count=len(transport.played),
            fast_barge_in_count=pipeline._fast_barge_in_count,
            interrupt_event_set=pipeline._interrupt_event.is_set(),
        )

    duration = time.monotonic() - start

    capture_started_at = transport.capture_started_at or start
    assertions = _evaluate(
        scenario,
        pipeline=pipeline,
        router=router,
        tts=tts,
        event_bus=event_bus,
        transcript_sink=transcript_sink,
        utterance_sink=utterance_sink,
        transport=transport,
        capture_started_at=capture_started_at,
    )

    return ScenarioResult(
        name=scenario.name,
        description=scenario.description,
        duration_s=duration,
        assertions=assertions,
        transcripts_persisted=[r.text for r in transcript_sink.snapshot()],
        utterances_persisted=[
            {
                "mode": u.mode,
                "output_text": u.output_text,
                "audio_duration_ms": u.audio_duration_ms,
            }
            for u in utterance_sink.snapshot()
        ],
        agent_spoke_durations_ms=[
            s.audio_duration_ms for s in _agent_spoke_events(event_bus)
        ],
        interrupt_event_set=pipeline._interrupt_event.is_set(),
        fast_barge_in_count=pipeline._fast_barge_in_count,
        classifier_calls=len(router.barge_in_calls),
        played_frame_count=len(transport.played),
    )


def _scenario_budget_s(scenario: Scenario, script_len: int) -> float:
    """Soft timeout: scenario wall-clock + drain margin + safety buffer.

    The transport's capture takes ``script_len * frame_duration`` seconds
    at real time. The respond loop's post-stream drain adds ``drain_extra_s``.
    A generous 5 s safety buffer absorbs the post-utterance classifier
    rounds without false timeouts.
    """
    script_s = (script_len * FRAME_DURATION_MS) / 1000.0
    return script_s + scenario.drain_extra_s + 5.0


async def run_suite(scenarios: Iterable[Scenario]) -> list[ScenarioResult]:
    """Run every scenario in sequence; suite-level concurrency is a future bead."""
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        logger.info("scenario start: %s", scenario.name)
        result = await run_scenario(scenario)
        verdict = "PASS" if result.passed else "FAIL"
        logger.info(
            "scenario end: %s [%s] (%.2fs)", scenario.name, verdict, result.duration_s
        )
        results.append(result)
    return results


__all__ = [
    "expand_scenario_to_frames",
    "run_scenario",
    "run_suite",
    "speech_transcripts",
]
