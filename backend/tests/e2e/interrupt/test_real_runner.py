"""Unit tests for the real-provider runner's evaluation logic (Johnny-gft).

The full ``run_scenario_real`` requires a real STT/LLM/TTS bundle and is
covered by the integration CLI (``python -m johnny.e2e.interrupt --real``).
These tests target ``_evaluate_real`` in isolation, with synthetic pipeline
+ event-bus + sink inputs, so the assertion-shape contract is locked into
the unit-test suite (not just the integration runs).

The single contract under test here: when the real-LLM round-trip is slow
enough that the user's interrupt arrives BEFORE the answer LLM has
produced any tokens, the pipeline emits NO ``AgentSpoke`` event but the
VAD-driven fast barge-in still fires. Per Johnny-gft we accept that as a
valid 'stop' outcome — the bot stopping before it could start is just as
much a successful stop as the bot being cut mid-sentence.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from johnny.e2e.interrupt.real_runner import _evaluate_real
from johnny.e2e.interrupt.scenarios import (
    CLARIFICATION_REDIRECTS_LONG_ANSWER,
    COUGH_DOES_NOT_INTERRUPT,
    STOP_INTERRUPTS_LONG_ANSWER,
)
from johnny.e2e.interrupt.transport import CaptureLog, TaggedFrame
from johnny.voice_pipeline import (
    AgentSpoke,
    InMemoryEventBus,
    InMemoryTranscriptSink,
    InMemoryUtteranceSink,
    TranscriptFinalized,
)


@dataclass
class _StubPipeline:
    """Minimal pipeline surface ``_evaluate_real`` reads from.

    Mirrors the three attributes the real :class:`VoicePipeline` exposes
    that the evaluation function accesses. Construct with the value of
    ``_fast_barge_in_count`` you want to test.
    """

    _fast_barge_in_count: int
    _session_started_at: float = 0.0
    _interrupt_event: asyncio.Event = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._interrupt_event is None:
            self._interrupt_event = asyncio.Event()


class _StubTransport:
    """Minimal transport surface ``_evaluate_real`` reads from."""

    def __init__(self, *, interrupt_monotonic: float | None = None) -> None:
        self._capture_log = CaptureLog()
        if interrupt_monotonic is not None:
            # Plant a fake interrupt frame so first_monotonic_for_tag(...)
            # returns a value the latency assertion can subtract from.
            self._capture_log.frames.append(
                TaggedFrame(pcm=b"\x00\x00" * 320, event_tag="interrupt")
            )
            self._capture_log.monotonic_at.append(interrupt_monotonic)

    @property
    def capture_log(self) -> CaptureLog:
        return self._capture_log


async def _populate_transcript_sink(
    sink: InMemoryTranscriptSink, texts: list[str]
) -> None:
    for text in texts:
        await sink.record(
            text=text,
            start_offset_ms=0,
            end_offset_ms=500,
        )


async def _publish_transcripts(bus: InMemoryEventBus, texts: list[str]) -> None:
    for text in texts:
        await bus.publish(TranscriptFinalized(text=text, timestamp_ms=0))


# --- the new contract -------------------------------------------------------


async def test_stop_scenario_passes_when_llm_too_slow_to_speak_but_barge_in_fires() -> None:
    """Johnny-gft headline case: LLM produces no tokens before the
    interrupt fires, but the VAD-driven fast barge-in DID detect the
    'stop' utterance. This is a valid 'stop' outcome (the bot stopped
    before it could start) and the evaluation MUST pass."""
    scenario = STOP_INTERRUPTS_LONG_ANSWER

    event_bus = InMemoryEventBus()
    await _publish_transcripts(
        event_bus, ["tell me about yourself", "stop please"]
    )
    transcript_sink = InMemoryTranscriptSink()
    await _populate_transcript_sink(
        transcript_sink, ["tell me about yourself", "stop please"]
    )
    utterance_sink = InMemoryUtteranceSink()
    # No AgentSpoke published — the LLM was too slow.

    pipeline = _StubPipeline(_fast_barge_in_count=1)
    transport = _StubTransport(interrupt_monotonic=10.0)

    assertions = _evaluate_real(
        scenario,
        pipeline=pipeline,  # type: ignore[arg-type]
        event_bus=event_bus,
        transcript_sink=transcript_sink,
        utterance_sink=utterance_sink,
        transport=transport,  # type: ignore[arg-type]
    )

    failed = [a for a in assertions if not a.passed]
    assert not failed, (
        "expected the no-AgentSpoke + fast_barge_in case to pass; failed: "
        f"{[(a.name, a.detail) for a in failed]}"
    )

    # The bead's specific assertion shape: the new ``interrupt_observed_by_pipeline``
    # assertion is what carries the pass.
    by_name = {a.name: a for a in assertions}
    assert "interrupt_observed_by_pipeline" in by_name, (
        f"expected interrupt_observed_by_pipeline assertion; got {sorted(by_name)}"
    )
    assert by_name["interrupt_observed_by_pipeline"].passed is True

    # The latency-budget assertion is intentionally SKIPPED when no
    # AgentSpoke exists to measure against (per Johnny-gft).
    assert "interrupt_to_cut_latency_budget" not in by_name, (
        "latency assertion must be skipped when there's nothing to cut"
    )


async def test_stop_scenario_fails_when_no_spoke_and_no_barge_in() -> None:
    """If the bot didn't speak AND fast barge-in never fired, the system
    silently dropped the user's stop signal — that's the actual bug,
    not just a slow-LLM blip. The assertion MUST fail to surface it."""
    scenario = STOP_INTERRUPTS_LONG_ANSWER

    event_bus = InMemoryEventBus()
    await _publish_transcripts(
        event_bus, ["tell me about yourself", "stop please"]
    )
    transcript_sink = InMemoryTranscriptSink()
    await _populate_transcript_sink(
        transcript_sink, ["tell me about yourself", "stop please"]
    )
    utterance_sink = InMemoryUtteranceSink()

    pipeline = _StubPipeline(_fast_barge_in_count=0)  # ← the bug shape
    transport = _StubTransport(interrupt_monotonic=10.0)

    assertions = _evaluate_real(
        scenario,
        pipeline=pipeline,  # type: ignore[arg-type]
        event_bus=event_bus,
        transcript_sink=transcript_sink,
        utterance_sink=utterance_sink,
        transport=transport,  # type: ignore[arg-type]
    )

    by_name = {a.name: a for a in assertions}
    assert by_name["interrupt_observed_by_pipeline"].passed is False, (
        f"detail={by_name['interrupt_observed_by_pipeline'].detail!r}"
    )


async def test_stop_scenario_passes_when_spoke_cut_short() -> None:
    """The fast-LLM case: bot started speaking, was cut mid-sentence.
    The classic ``first_agent_spoke_truncated`` + ``interrupt_to_cut_latency_budget``
    assertions both apply and both must pass."""
    scenario = STOP_INTERRUPTS_LONG_ANSWER

    event_bus = InMemoryEventBus()
    await _publish_transcripts(
        event_bus, ["tell me about yourself", "stop please"]
    )
    # Bot started speaking, was cut at ~2 s into the planned long answer.
    await event_bus.publish(
        AgentSpoke(
            text="Sure, let me tell you a long story.",
            audio_duration_ms=2000,
            timestamp_ms=10_500,  # 500 ms after the interrupt at t=10s
        )
    )
    transcript_sink = InMemoryTranscriptSink()
    await _populate_transcript_sink(
        transcript_sink, ["tell me about yourself", "stop please"]
    )
    utterance_sink = InMemoryUtteranceSink()

    pipeline = _StubPipeline(
        _fast_barge_in_count=1,
        _session_started_at=0.0,  # so first_spoke_wall_clock = 10.5 s
    )
    transport = _StubTransport(interrupt_monotonic=10.0)

    assertions = _evaluate_real(
        scenario,
        pipeline=pipeline,  # type: ignore[arg-type]
        event_bus=event_bus,
        transcript_sink=transcript_sink,
        utterance_sink=utterance_sink,
        transport=transport,  # type: ignore[arg-type]
    )

    failed = [a for a in assertions if not a.passed]
    assert not failed, (
        f"expected all assertions to pass; failed: {[(a.name, a.detail) for a in failed]}"
    )

    by_name = {a.name: a for a in assertions}
    # The cut-short path uses the original name kept for continuity.
    assert by_name["first_agent_spoke_truncated"].passed is True
    # Latency budget IS measured when there's something to measure.
    assert by_name["interrupt_to_cut_latency_budget"].passed is True


async def test_stop_scenario_fails_when_spoke_was_full_length() -> None:
    """The bot ignored the interrupt and finished its long answer.
    ``first_agent_spoke_truncated`` MUST fail."""
    scenario = STOP_INTERRUPTS_LONG_ANSWER

    event_bus = InMemoryEventBus()
    await _publish_transcripts(
        event_bus, ["tell me about yourself", "stop please"]
    )
    # Bot spoke for 6 s — the full long answer. The cut-short floor is 4500 ms.
    await event_bus.publish(
        AgentSpoke(
            text="A complete six-second monologue.",
            audio_duration_ms=6000,
            timestamp_ms=10_500,
        )
    )
    transcript_sink = InMemoryTranscriptSink()
    await _populate_transcript_sink(
        transcript_sink, ["tell me about yourself", "stop please"]
    )
    utterance_sink = InMemoryUtteranceSink()

    pipeline = _StubPipeline(_fast_barge_in_count=0)
    transport = _StubTransport(interrupt_monotonic=10.0)

    assertions = _evaluate_real(
        scenario,
        pipeline=pipeline,  # type: ignore[arg-type]
        event_bus=event_bus,
        transcript_sink=transcript_sink,
        utterance_sink=utterance_sink,
        transport=transport,  # type: ignore[arg-type]
    )

    by_name = {a.name: a for a in assertions}
    assert by_name["first_agent_spoke_truncated"].passed is False, (
        f"detail={by_name['first_agent_spoke_truncated'].detail!r}"
    )


async def test_cough_scenario_unaffected_by_change() -> None:
    """Sanity: the ``expect_interrupt=False`` branch (cough scenario) is
    not touched by Johnny-gft. Bot completes full TTS, no interrupt fired
    — all assertions still pass."""
    scenario = COUGH_DOES_NOT_INTERRUPT

    event_bus = InMemoryEventBus()
    await _publish_transcripts(event_bus, ["explain the system to me"])
    await event_bus.publish(
        AgentSpoke(
            text="Here is a short complete explanation.",
            audio_duration_ms=1600,
            timestamp_ms=2_000,
        )
    )
    transcript_sink = InMemoryTranscriptSink()
    await _populate_transcript_sink(
        transcript_sink, ["explain the system to me"]
    )
    utterance_sink = InMemoryUtteranceSink()

    pipeline = _StubPipeline(_fast_barge_in_count=0)
    # No interrupt tag needed — cough scenario doesn't measure latency.
    transport = _StubTransport(interrupt_monotonic=None)

    assertions = _evaluate_real(
        scenario,
        pipeline=pipeline,  # type: ignore[arg-type]
        event_bus=event_bus,
        transcript_sink=transcript_sink,
        utterance_sink=utterance_sink,
        transport=transport,  # type: ignore[arg-type]
    )

    failed = [a for a in assertions if not a.passed]
    assert not failed, (
        f"cough scenario expected to pass; failed: {[(a.name, a.detail) for a in failed]}"
    )


async def test_clarification_scenario_followup_keyword_unchanged() -> None:
    """The Johnny-tjd ``followup_keyword`` assertion path still works on top
    of the Johnny-gft change. Bot spoke briefly then redirected; the keyword
    'launch' appears in the persisted utterance corpus."""
    scenario = CLARIFICATION_REDIRECTS_LONG_ANSWER

    event_bus = InMemoryEventBus()
    await _publish_transcripts(
        event_bus,
        [
            "give me a summary of project status",
            "wait, what about the launch date?",
        ],
    )
    # First speech (cut), second speech (redirect addressing launch).
    await event_bus.publish(
        AgentSpoke(
            text="Sure, let me give you a long summary.",
            audio_duration_ms=2000,
            timestamp_ms=10_500,
        )
    )
    await event_bus.publish(
        AgentSpoke(
            text="Regarding the launch date, we are targeting end of quarter.",
            audio_duration_ms=3500,
            timestamp_ms=15_000,
        )
    )
    transcript_sink = InMemoryTranscriptSink()
    await _populate_transcript_sink(
        transcript_sink,
        [
            "give me a summary of project status",
            "wait, what about the launch date?",
        ],
    )
    utterance_sink = InMemoryUtteranceSink()
    # Utterance sink contains the redirect text — that's what the keyword check
    # consults.
    await utterance_sink.record(
        mode="limited_auto_speak",
        prompt="",
        output_text="Sure, let me give you a long summary.",
        audio_duration_ms=2000,
    )
    await utterance_sink.record(
        mode="limited_auto_speak",
        prompt="",
        output_text="Regarding the launch date, we are targeting end of quarter.",
        audio_duration_ms=3500,
    )

    pipeline = _StubPipeline(_fast_barge_in_count=1)
    transport = _StubTransport(interrupt_monotonic=10.0)

    assertions = _evaluate_real(
        scenario,
        pipeline=pipeline,  # type: ignore[arg-type]
        event_bus=event_bus,
        transcript_sink=transcript_sink,
        utterance_sink=utterance_sink,
        transport=transport,  # type: ignore[arg-type]
    )

    by_name = {a.name: a for a in assertions}
    # The keyword check is the new (Johnny-tjd) followup assertion shape.
    assert "bot_addressed_followup_keyword" in by_name
    assert by_name["bot_addressed_followup_keyword"].passed is True


