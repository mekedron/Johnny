"""Real-provider runner for the interrupt harness (Johnny-ckz.4).

The scripted runner in :mod:`johnny.e2e.interrupt.runner` is deterministic
by design: providers return pre-scripted text and frames, so the only
sources of variance are timing-related. The bead Johnny-ckz.4 calls for
running the same scenarios against REAL STT / LLM / TTS so the harness
faithfully reproduces what fails in production. This module provides
that runner.

Key differences from the scripted runner:

* **Speaker audio is real**: each scenario's speech-event transcript is
  rendered through the same real TTS that the bot uses (or a designated
  fallback), pre-cached on disk via :mod:`real_speaker`. The pipeline's
  VAD sees actual speech instead of 440 Hz tones, so the real STT can
  transcribe.
* **STT / LLM / TTS are wired from the JSON**: provider keys from the
  ``providers.json`` file feed instantiated adapters from the production
  registry. No scripted shims.
* **Assertions are fuzzy on text**: real STT will sometimes transcribe
  "stop please" as "Stop, please." or "stop, please" — the assertion
  checks substring / token containment, not exact string match.
* **Bot instructions force a long answer**: the pipeline needs the
  router to allow a verbose answer for the interrupt assertions to be
  meaningful. ``config.instructions`` is set to a system prompt that
  asks for multi-sentence answers.

Latency budgets are widened to accommodate real STT round-trip
(~300–700 ms) vs the scripted STT's 30 ms sleep — the goal is to detect
real-world failures, not chase fast-path timing that depends entirely
on the provider's network latency.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

from johnny.e2e.interrupt.audio import FRAME_DURATION_MS
from johnny.e2e.interrupt.real_providers import RealProviderBundle
from johnny.e2e.interrupt.real_speaker import render_scenario_audio
from johnny.e2e.interrupt.report import AssertionResult, ScenarioResult
from johnny.e2e.interrupt.scenarios import Scenario
from johnny.e2e.interrupt.transport import PacedScriptedTransport
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


DEFAULT_HARNESS_INSTRUCTIONS = (
    "You are a meeting bot helper named Johnny. When a user asks you to "
    "tell them about yourself, give them a thorough multi-sentence "
    "introduction of at least four sentences covering your background, "
    "expertise, what you're working on, and how you can help. Always "
    "answer questions verbosely when asked for explanations or summaries."
)


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


def _has_keyword(text: str, keyword: str) -> bool:
    return _normalise(keyword) in _normalise(text)


def _agent_spoke_events(event_bus: InMemoryEventBus) -> list[AgentSpoke]:
    return [e for e in event_bus.snapshot() if isinstance(e, AgentSpoke)]


def _transcript_events(event_bus: InMemoryEventBus) -> list[TranscriptFinalized]:
    return [e for e in event_bus.snapshot() if isinstance(e, TranscriptFinalized)]


def _expected_keywords(scenario: Scenario) -> list[str]:
    """Pick a representative keyword per expected transcript.

    Real STT will often add punctuation, contract words, or break the
    transcript across two finalisations. The harness asserts "is this
    transcript present in any form?" by checking that at least one
    distinguishing word from the expected text appears across the union
    of persisted transcripts.
    """
    keywords: list[str] = []
    for text in scenario.expect_transcripts:
        tokens = _normalise(text).split()
        # Prefer a content word longer than 3 chars (drops "the", "a"...).
        candidates = [t for t in tokens if len(t) > 3]
        keywords.append(candidates[0] if candidates else tokens[0])
    return keywords


def _build_real_pipeline(
    scenario: Scenario,
    bundle: RealProviderBundle,
    *,
    transport: PacedScriptedTransport,
) -> tuple[VoicePipeline, InMemoryEventBus, InMemoryTranscriptSink, InMemoryUtteranceSink]:
    event_bus = InMemoryEventBus()
    transcript_sink = InMemoryTranscriptSink()
    utterance_sink = InMemoryUtteranceSink()
    config = PipelineConfig(
        instructions=DEFAULT_HARNESS_INSTRUCTIONS,
        # Generous end-of-speech window so VAD doesn't truncate the
        # speaker's rendered TTS prematurely (real TTS has small mid-word
        # gaps that an aggressive 300 ms threshold misclassifies as
        # end-of-utterance).
        end_of_speech_ms=600,
        vad_threshold=0.02,
        confidence_threshold=0.5,
        session_id=f"e2e-interrupt-real-{scenario.name}",
        frame_duration_ms=FRAME_DURATION_MS,
        # Force the verbose answer mode (no allowed_replies).
        mode="limited_auto_speak",
    )
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(threshold=config.vad_threshold),
        stt=bundle.stt,
        router_llm=bundle.llm,
        answer_llm=bundle.llm,
        tts=bundle.tts,
        event_bus=event_bus,
        config=config,
        transcript_sink=transcript_sink,
        utterance_sink=utterance_sink,
    )
    return pipeline, event_bus, transcript_sink, utterance_sink


def _evaluate_real(
    scenario: Scenario,
    *,
    pipeline: VoicePipeline,
    event_bus: InMemoryEventBus,
    transcript_sink: InMemoryTranscriptSink,
    utterance_sink: InMemoryUtteranceSink,
    transport: PacedScriptedTransport,
) -> list[AssertionResult]:
    """Real-provider variants of the scripted assertions (fuzzier on text)."""
    assertions: list[AssertionResult] = []

    persisted_transcripts = [r.text for r in transcript_sink.snapshot()]
    keywords = _expected_keywords(scenario)
    missing_keywords: list[str] = []
    for keyword in keywords:
        found = any(
            _has_keyword(persisted, keyword) for persisted in persisted_transcripts
        )
        if not found:
            missing_keywords.append(keyword)
    assertions.append(
        AssertionResult(
            name="all_expected_transcripts_persisted",
            passed=len(missing_keywords) == 0,
            detail=(
                f"persisted={persisted_transcripts!r}; "
                f"expected_keywords={keywords!r}; "
                f"missing_keywords={missing_keywords!r}"
            ),
        )
    )

    spoke = _agent_spoke_events(event_bus)

    if scenario.expect_interrupt:
        if not spoke:
            assertions.append(
                AssertionResult(
                    name="bot_started_speaking_before_interrupt",
                    passed=False,
                    detail=(
                        "no AgentSpoke published; the bot never reached TTS so "
                        "an interrupt could not be measured. Likely the router "
                        "did not approve speaking, or the LLM/TTS failed."
                    ),
                )
            )
        else:
            first_spoke = spoke[0]
            # Real-provider mode: we cap audio_duration at 4500 ms as the
            # "long answer" floor. Anything shorter and we say "it was
            # cut" (the long answer would have been ≥6 s otherwise).
            cut_short = first_spoke.audio_duration_ms < 4500
            assertions.append(
                AssertionResult(
                    name="first_agent_spoke_truncated",
                    passed=cut_short,
                    detail=(
                        f"audio_duration_ms={first_spoke.audio_duration_ms} "
                        f"vs floor=4500 (must be strictly less)"
                    ),
                )
            )

        interrupt_start = transport.capture_log.first_monotonic_for_tag("interrupt")
        if interrupt_start is None or not spoke:
            assertions.append(
                AssertionResult(
                    name="interrupt_to_cut_latency_budget",
                    passed=False,
                    detail=(
                        "could not determine interrupt-onset monotonic time or "
                        "first AgentSpoke event"
                    ),
                )
            )
        else:
            first_spoke = spoke[0]
            first_spoke_wall_clock = (
                pipeline._session_started_at + first_spoke.timestamp_ms / 1000.0
            )
            delta_ms = (first_spoke_wall_clock - interrupt_start) * 1000.0
            # Real-provider latency budget is wider: 2000 ms accounts for
            # real STT round-trip (~300-700 ms) AND VAD silence-window
            # (600 ms end_of_speech_ms). The fast (VAD-driven) path lands
            # well under this — we observed 1.1 s consistently — but a
            # higher cap absorbs the occasional Deepgram or OpenAI slow
            # turn.
            budget_ms = max(
                scenario.interrupt_latency_budget_s * 1000.0, 2000.0
            )
            within = delta_ms <= budget_ms
            assertions.append(
                AssertionResult(
                    name="interrupt_to_cut_latency_budget",
                    passed=within,
                    detail=f"delta_ms={delta_ms:.0f} vs budget={budget_ms:.0f}ms",
                )
            )

        if scenario.expect_followup_utterance:
            # Semantic check: did the bot actually address the redirect?
            # The user-visible success criterion is "after the barge-in,
            # the bot answered the new question" — not "two AgentSpoke
            # events fired". With scenario.followup_keyword set, we look
            # for that token in the persisted-utterance corpus, which is
            # robust to (a) the cut answer not emitting AgentSpoke when
            # the interrupt lands before any sentence flushes, and
            # (b) real LLMs phrasing the redirect non-deterministically.
            # Fall back to the count check when no keyword is declared
            # (older scenarios).
            persisted_utterance_texts = [
                u.output_text for u in utterance_sink.snapshot()
            ]
            if scenario.followup_keyword:
                keyword = scenario.followup_keyword
                addressed = any(
                    _has_keyword(text, keyword)
                    for text in persisted_utterance_texts
                )
                assertions.append(
                    AssertionResult(
                        name="bot_addressed_followup_keyword",
                        passed=addressed and pipeline._fast_barge_in_count > 0,
                        detail=(
                            f"keyword={keyword!r}; "
                            f"fast_barge_in_count={pipeline._fast_barge_in_count}; "
                            f"persisted_utterances={persisted_utterance_texts!r}"
                        ),
                    )
                )
            else:
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
                # In real mode we just check the bot got to speak for
                # some non-trivial duration (>= 800 ms).
                assertions.append(
                    AssertionResult(
                        name="bot_completed_full_tts",
                        passed=first_spoke.audio_duration_ms >= 800,
                        detail=(
                            f"audio_duration_ms={first_spoke.audio_duration_ms} "
                            f"vs floor=800ms (must be ≥)"
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

    bus_texts = [t.text for t in _transcript_events(event_bus)]
    missing_in_bus_keywords: list[str] = []
    for keyword in keywords:
        found = any(_has_keyword(bus, keyword) for bus in bus_texts)
        if not found:
            missing_in_bus_keywords.append(keyword)
    assertions.append(
        AssertionResult(
            name="all_expected_transcripts_on_event_bus",
            passed=len(missing_in_bus_keywords) == 0,
            detail=(
                f"bus_texts={bus_texts!r}; missing_keywords={missing_in_bus_keywords!r}"
            ),
        )
    )

    return assertions


def _scenario_budget_s(scenario: Scenario, script_len: int) -> float:
    script_s = (script_len * FRAME_DURATION_MS) / 1000.0
    # Real providers add LLM + TTS latency; pad an additional 45 s on
    # top of the scripted budget. The respond loop can stall for ~5 s
    # per LLM call in the worst case, and the clarification scenario has
    # 4-5 sequential LLM calls (router → answer → barge-in → router →
    # answer-followup) plus a TTS run for each answer.
    return script_s + scenario.drain_extra_s + 45.0


REAL_MODE_EXTRA_SILENCE_MS = 8000
"""Extra silence padded after each speech event in real mode.

Real STT+LLM+TTS round-trip is typically 4-6 s — the scripted 1.2 s
``bot_starts_speaking`` silence is far too tight, so the bot never gets
to start TTS before the next speaker event arrives. 8 s leaves comfortable
headroom for an OpenAI gpt-4.1-mini round trip that occasionally spikes
to 3 s under load.
"""


async def run_scenario_real(
    scenario: Scenario,
    bundle: RealProviderBundle,
    *,
    cache_root: Path,
    voice_label: str,
    voice_id: str | None = None,
) -> ScenarioResult:
    """Execute one scenario end-to-end against real STT/LLM/TTS."""
    logger.info("real scenario start: %s", scenario.name)
    script = await render_scenario_audio(
        scenario,
        bundle.tts,
        cache_root=cache_root,
        voice_label=voice_label,
        voice_id=voice_id,
        extra_silence_after_speech_ms=REAL_MODE_EXTRA_SILENCE_MS,
    )
    transport = PacedScriptedTransport(script=script, frame_duration_ms=FRAME_DURATION_MS)
    pipeline, event_bus, transcript_sink, utterance_sink = _build_real_pipeline(
        scenario, bundle, transport=transport
    )

    start = time.monotonic()
    try:
        await asyncio.wait_for(
            pipeline.run(),
            timeout=_scenario_budget_s(scenario, script_len=len(script)),
        )
    except TimeoutError:
        elapsed = time.monotonic() - start
        logger.exception("real scenario %s timed out at %.1fs", scenario.name, elapsed)
        return ScenarioResult(
            name=scenario.name,
            description=scenario.description,
            duration_s=elapsed,
            error=(
                "pipeline.run timed out under real providers — likely an LLM "
                "or TTS latency spike, OR a regression that wedged the respond "
                "loop"
            ),
            transcripts_persisted=[r.text for r in transcript_sink.snapshot()],
            played_frame_count=len(transport.played),
            fast_barge_in_count=pipeline._fast_barge_in_count,
            interrupt_event_set=pipeline._interrupt_event.is_set(),
        )

    duration = time.monotonic() - start
    assertions = _evaluate_real(
        scenario,
        pipeline=pipeline,
        event_bus=event_bus,
        transcript_sink=transcript_sink,
        utterance_sink=utterance_sink,
        transport=transport,
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
        classifier_calls=0,  # real classifier counter not tracked here
        played_frame_count=len(transport.played),
    )


async def run_suite_real(
    scenarios: list[Scenario],
    bundle: RealProviderBundle,
    *,
    cache_root: Path,
    voice_label: str,
    voice_id: str | None = None,
) -> list[ScenarioResult]:
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        result = await run_scenario_real(
            scenario,
            bundle,
            cache_root=cache_root,
            voice_label=voice_label,
            voice_id=voice_id,
        )
        verdict = "PASS" if result.passed else "FAIL"
        logger.info(
            "real scenario end: %s [%s] (%.2fs)",
            scenario.name,
            verdict,
            result.duration_s,
        )
        results.append(result)
    return results


__all__ = [
    "DEFAULT_HARNESS_INSTRUCTIONS",
    "run_scenario_real",
    "run_suite_real",
]
