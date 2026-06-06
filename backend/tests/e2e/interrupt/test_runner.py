"""Runner-against-real-pipeline integration tests for the interrupt harness.

These tests prove the harness wires up correctly: the real
:class:`VoicePipeline` executes against the scripted transport / providers
and produces the events / sinks the runner assertions need. They use
short scenarios (small ``tts_frame_count``, brief speaker timeline) so the
suite stays under ~10 s wall-clock even at real frame pacing.
"""

from __future__ import annotations

from johnny.e2e.interrupt.audio import BYTES_PER_FRAME, FRAME_DURATION_MS
from johnny.e2e.interrupt.runner import (
    expand_scenario_to_frames,
    run_scenario,
    speech_transcripts,
)
from johnny.e2e.interrupt.scenarios import (
    COUGH_DOES_NOT_INTERRUPT,
    STOP_INTERRUPTS_LONG_ANSWER,
    STT_KEEPS_RUNNING_DURING_BOT_SPEECH,
    Scenario,
    SpeakerEvent,
)


def _short_stop_scenario() -> Scenario:
    """Compact stop scenario (~3.4 s wall-clock) for the runner test."""
    return Scenario(
        name="test_short_stop",
        description="compact reproducer for the stop-interrupt happy path",
        timeline=(
            SpeakerEvent(kind="silence", duration_ms=200, tag="lead_in"),
            SpeakerEvent(
                kind="speech",
                duration_ms=400,
                transcript="prompt",
                tag="prompt",
            ),
            SpeakerEvent(kind="silence", duration_ms=600, tag="gap"),
            SpeakerEvent(
                kind="speech",
                duration_ms=400,
                transcript="stop",
                tag="interrupt",
            ),
            SpeakerEvent(kind="silence", duration_ms=800, tag="cooldown"),
        ),
        router_decisions=(
            {"should_speak": True, "confidence": 0.9, "reason": "prompt"},
            {"should_speak": False, "confidence": 0.1, "reason": "stop"},
        ),
        barge_in_decisions=(
            {
                "should_interrupt": True,
                "category": "stop",
                "reason": "user says stop",
            },
        ),
        # Multi-sentence so the per-sentence TTS flush gives the
        # interrupt event multiple windows to land — closer to the
        # production shape that the bug reproduces.
        answer_text=(
            "Sure, let me tell you a long story. "
            "First, there was a project. "
            "Second, it had many users. "
            "Third, everything was fine. "
            "Fourth, then it was not."
        ),
        expect_interrupt=True,
        expect_followup_utterance=False,
        expect_transcripts=("prompt", "stop"),
        tts_frame_count=80,
        drain_extra_s=0.5,
        # Generous budget because the LLM router stage adds ~0 ms (no
        # network) but the per-utterance STT sleep (30 ms) + the loop
        # interleave still adds up.
        interrupt_latency_budget_s=1.0,
        transcript_landing_budget_s=2.0,
    )


def test_expand_scenario_to_frames_alternates_speech_and_silence() -> None:
    scenario = _short_stop_scenario()
    frames = expand_scenario_to_frames(scenario)
    tags = [f.event_tag for f in frames]
    # First frames must be lead_in silence, then prompt, etc., in order.
    assert tags[0] == "lead_in"
    assert "prompt" in tags
    assert "interrupt" in tags
    assert tags[-1] == "cooldown"
    # Every frame must be a valid PCM byte string.
    assert all(len(f.pcm) == BYTES_PER_FRAME for f in frames)


def test_speech_transcripts_orders_by_timeline_speech_events() -> None:
    transcripts = speech_transcripts(_short_stop_scenario())
    assert transcripts == ["prompt", "stop"]


async def test_runner_stop_scenario_cuts_bot_and_persists_transcripts() -> None:
    """End-to-end: the real pipeline runs against the harness and ALL
    assertions pass for the stop-interrupt happy path."""
    result = await run_scenario(_short_stop_scenario())
    assert result.error is None, f"runner crashed: {result.error}"

    # The compact stop scenario should pass cleanly.
    failed = [a for a in result.assertions if not a.passed]
    assert not failed, (
        "expected all assertions to pass on the compact stop scenario; "
        f"failed: {[(a.name, a.detail) for a in failed]}"
    )

    # Both expected transcripts must be in the sink.
    persisted = result.transcripts_persisted
    assert "prompt" in persisted
    assert "stop" in persisted

    # At least one AgentSpoke must have fired (the bot started its
    # answer); its duration must be SHORTER than the full TTS.
    assert result.agent_spoke_durations_ms, "bot never spoke"
    full_tts_ms = 80 * FRAME_DURATION_MS  # tts_frame_count=80
    assert result.agent_spoke_durations_ms[0] < full_tts_ms

    # Fast barge-in MUST have fired at least once during the run. Note
    # that ``interrupt_event_set`` at end-of-run is NOT a reliable
    # indicator: ``_respond_to_transcript_inner`` clears the event at
    # the start of every new response (Johnny-arh fix), so by the time
    # the run completes the flag is back to False.
    assert result.fast_barge_in_count >= 1


async def test_runner_cough_scenario_completes_full_tts() -> None:
    """Cough below threshold MUST NOT trigger fast barge-in."""
    # Use the canonical cough scenario but with a smaller TTS so the
    # test is fast. Use a one-sentence answer because the pipeline's
    # per-sentence TTS flush would otherwise multiply playback time and
    # bust the soft timeout.
    scenario = Scenario(
        name="test_cough_short",
        description="cough does not interrupt — short variant for tests",
        timeline=(
            SpeakerEvent(kind="silence", duration_ms=200, tag="lead_in"),
            SpeakerEvent(
                kind="speech",
                duration_ms=400,
                transcript="prompt",
                tag="prompt",
            ),
            SpeakerEvent(kind="silence", duration_ms=600, tag="gap"),
            SpeakerEvent(kind="cough", duration_ms=60, tag="cough"),
            SpeakerEvent(kind="silence", duration_ms=2000, tag="cooldown"),
        ),
        # Two decisions: prompt → speak, cough-utterance → don't speak.
        # Without the second one the SwitchingRouterLLM reuses the last
        # decision (should_speak=True) and the bot starts a redundant
        # second TTS cycle that has nothing to do with the test.
        router_decisions=(
            {"should_speak": True, "confidence": 0.9, "reason": "prompt"},
            {"should_speak": False, "confidence": 0.1, "reason": "cough"},
        ),
        # Classifier may run on the cough utterance while the bot is
        # still streaming the first answer; we want a deterministic
        # no-interrupt verdict.
        barge_in_decisions=(
            {
                "should_interrupt": False,
                "category": "noise",
                "reason": "cough",
            },
        ),
        # Single-sentence answer avoids per-sentence TTS multiplier.
        answer_text="A short complete reply.",
        expect_interrupt=False,
        expect_followup_utterance=False,
        expect_transcripts=("prompt",),
        tts_frame_count=40,  # ~0.8 s
        drain_extra_s=1.0,
    )

    result = await run_scenario(scenario)
    assert result.error is None
    failed = [a for a in result.assertions if not a.passed]
    assert not failed, (
        "expected no failed assertions on cough scenario; "
        f"failed: {[(a.name, a.detail) for a in failed]}"
    )
    # Fast barge-in MUST NOT have fired.
    assert result.fast_barge_in_count == 0
    assert result.interrupt_event_set is False


async def test_runner_stt_keeps_running_persists_side_chat() -> None:
    """Johnny-har regression check: side-chat lands in transcript sink."""
    scenario = Scenario(
        name="test_stt_keeps_running",
        description="side chat reaches the sink even mid-bot",
        timeline=(
            SpeakerEvent(kind="silence", duration_ms=200, tag="lead_in"),
            SpeakerEvent(
                kind="speech",
                duration_ms=400,
                transcript="prompt",
                tag="prompt",
            ),
            SpeakerEvent(kind="silence", duration_ms=600, tag="gap"),
            SpeakerEvent(
                kind="speech",
                duration_ms=300,
                transcript="aside",
                tag="side_chat",
            ),
            SpeakerEvent(kind="silence", duration_ms=1500, tag="cooldown"),
        ),
        router_decisions=(
            {"should_speak": True, "confidence": 0.9, "reason": "prompt"},
            {"should_speak": False, "confidence": 0.1, "reason": "aside"},
        ),
        barge_in_decisions=(
            {
                "should_interrupt": False,
                "category": "side_chat",
                "reason": "not addressed to the bot",
            },
        ),
        # Single-sentence answer to avoid the per-sentence TTS multiplier.
        answer_text="A short complete reply.",
        expect_interrupt=False,
        expect_followup_utterance=False,
        expect_transcripts=("prompt", "aside"),
        tts_frame_count=60,
        drain_extra_s=1.0,
        interrupt_latency_budget_s=10.0,
    )

    result = await run_scenario(scenario)
    assert result.error is None
    failed = [a for a in result.assertions if not a.passed]
    assert not failed, (
        "side-chat scenario expected to pass; "
        f"failed: {[(a.name, a.detail) for a in failed]}"
    )
    assert "prompt" in result.transcripts_persisted
    assert "aside" in result.transcripts_persisted


def test_predefined_scenarios_are_importable() -> None:
    """The bead's full catalog is statically constructible (no runtime errors)."""
    assert STOP_INTERRUPTS_LONG_ANSWER.expect_interrupt is True
    assert COUGH_DOES_NOT_INTERRUPT.expect_interrupt is False
    assert STT_KEEPS_RUNNING_DURING_BOT_SPEECH.expect_interrupt is False
