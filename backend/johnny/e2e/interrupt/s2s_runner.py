"""Unified-pipeline runner for the interrupt harness (Johnny-ckz.22).

Drives :class:`UnifiedVoicePipeline` against one
:class:`app.providers.s2s_base.S2SProvider` (OpenAI Realtime, Gemini
Live, or future S2S adapters) per scenario. Mirrors
:mod:`real_runner` (split pipeline + real STT/LLM/TTS) and produces
the same :class:`ScenarioResult` shape so the existing
:func:`render_summary` / :func:`write_report` work unmodified.

Surface parity (Johnny-ckz.22 acceptance):

* The unified pipeline class is identical to what runs inside the
  meet-worker via ``johnny.meet_worker.pipeline_runner.build_and_run_pipeline``
  and inside the in-process browser runner via
  ``app.services.browser_pipeline_runner.assemble_browser_pipeline``.
  Both paths construct ``UnifiedVoicePipeline(transport, s2s, event_bus,
  config)`` from the same arguments — the harness uses the same
  constructor with :class:`PacedScriptedTransport` instead of the
  meet-worker bridge / browser transport. The runner annotates each
  result with ``--surface=meet|playground`` so the report shows which
  entry-point shape it exercised.
* Real S2S barge-in semantics differ per provider:
  - OpenAI Realtime → ``session.interrupt()`` sends
    ``response.cancel`` + ``input_audio_buffer.clear``. Confirmed
    against the GA API at fetch date 2026-06-07.
  - Gemini Live → no client-side cancel. ``session.interrupt()`` sends
    an ``activityEnd`` hint; real cancellation happens when a fresh
    user turn arrives (server VAD or explicit ``activityStart``).
  Both flows are exposed via ``S2SScenario.interrupt_kind``:
  ``session_interrupt`` uses ``pipeline.interrupt()``; ``new_user_turn``
  sends a fresh ``send_audio`` + ``commit_user_turn`` mid-response.

The runner reports barge-in latency for the interrupt scenarios: time
from the interrupt trigger to the next ``S2SResponseCompleted`` event
on the event bus. That's the "barge-in cancellation latency" the bead
calls out — pinned to a generous upper bound (3 s) that accommodates
network + in-flight audio without being so wide that a regression
slides through silently.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass

from app.providers.base import ToolDefinition
from app.providers.s2s_base import (
    S2SAudioFrame,
    S2SError,
    S2SEvent,
    S2SProvider,
    S2SResponseCompleted,
    S2SSession,
)
from johnny.e2e.interrupt.audio import (
    FRAME_DURATION_MS,
    cough_frames,
    silence_frame,
    silence_frames,
    speech_frames,
)
from johnny.e2e.interrupt.report import AssertionResult, ScenarioResult
from johnny.e2e.interrupt.s2s_providers import S2SProviderBundle
from johnny.e2e.interrupt.s2s_scenarios import S2SScenario
from johnny.e2e.interrupt.transport import (
    PacedScriptedTransport,
    TaggedFrame,
)
from johnny.voice_pipeline import (
    AgentSpoke,
    InMemoryEventBus,
    InMemoryTranscriptSink,
    InMemoryUtteranceSink,
    TranscriptFinalized,
)
from johnny.voice_pipeline.unified_pipeline import (
    UnifiedPipelineConfig,
    UnifiedVoicePipeline,
)

logger = logging.getLogger(__name__)


Surface = str  # "meet" or "playground" — informational only.

# Frame size used by the interrupt scenarios on the wire side. Real
# adapters resample internally (OpenAI Realtime → 24 kHz, Gemini Live →
# 24 kHz on the output side), so the harness frames are always 16 kHz
# 20 ms PCM and the adapter handles conversion.
_BARGE_IN_TONE_DURATION_MS = 600
"""Length of the fresh-user-turn tone the runner sends when
``interrupt_kind=new_user_turn``. Has to exceed the OpenAI Realtime
100 ms minimum-buffer gate (the API rejects shorter commits with
"buffer too small" — see Johnny-ckz.19 learnings) AND be long enough
for the server VAD to detect activity. 600 ms gives both adapters
comfortable headroom."""


def _expand_scenario_to_frames(
    scenario: S2SScenario,
) -> list[TaggedFrame]:
    """Build the speaker's PCM frame timeline for an S2S scenario."""
    frames: list[TaggedFrame] = []
    for idx, event in enumerate(scenario.timeline):
        tag = event.tag or f"event_{idx}_{event.kind}"
        if event.kind == "speech":
            raw_frames = speech_frames(event.duration_ms)
        elif event.kind == "cough":
            raw_frames = cough_frames(event.duration_ms)
        else:
            raw_frames = silence_frames(event.duration_ms)
        for raw in raw_frames:
            frames.append(TaggedFrame(pcm=raw, event_tag=tag))
    return frames


class _HoldingScriptedTransport(PacedScriptedTransport):
    """:class:`PacedScriptedTransport` that yields silence after the script ends.

    The unified pipeline closes its S2S session as soon as capture EOF
    fires (``_capture_loop`` exits → ``commit_user_turn`` → session
    teardown). That's fine for production where the meet bridge keeps
    capturing until shutdown, but in the harness the scripted script
    has a finite length — without this extension the pipeline would
    close the session within ~1 s of the script ending, far before the
    S2S provider has had time to generate a response (real S2S APIs
    take 1-3 s to produce assistant audio after a turn commits).

    The transport stays alive yielding frames at production cadence
    until the runner flips ``signal_stop()`` — i.e. when the runner
    has observed the artifact-side completion event(s) it needs.

    Two signals govern post-script behaviour:

    * ``signal_silence_after_commit()`` — after the commit driver has
      fired the explicit ``commit_user_turn``, switch holding-mode
      frames to EMPTY bytes (zero-length). The unified pipeline's
      capture loop skips empty frames (``if not frame: continue``),
      which keeps capture alive without re-feeding silence into the
      S2S session. Required because some adapters (Gemini Live) treat
      every post-commit audio chunk as the start of a NEW user turn,
      which interrupts the in-flight assistant response.
    * ``signal_stop()`` — exit the holding mode entirely, allowing
      capture EOF + clean teardown.
    """

    def __init__(
        self,
        script: list[TaggedFrame],
        *,
        frame_duration_ms: int = FRAME_DURATION_MS,
    ) -> None:
        super().__init__(script=script, frame_duration_ms=frame_duration_ms)
        self._hold_stop = asyncio.Event()
        self._post_commit_silence = False

    def signal_stop(self) -> None:
        """Tell the holding capture loop to exit at the next frame tick."""
        self._hold_stop.set()

    def signal_silence_after_commit(self) -> None:
        """Switch post-script frames to empty bytes (pipeline drops them).

        Called by the commit driver right after ``commit_user_turn``
        fires so the holding silence doesn't re-feed audio into the
        S2S session.
        """
        self._post_commit_silence = True

    async def capture_frames(self) -> AsyncIterator[bytes]:
        """Yield scripted frames, then continue yielding until ``signal_stop``.

        Once ``signal_silence_after_commit`` fires, EVERY subsequent
        frame yielded — both the remaining scripted frames AND the
        post-script holding silence — is replaced with empty bytes so
        the unified pipeline's ``if not frame: continue`` skips them.
        This matters when the scenario script has trailing silence
        AFTER the commit driver fires: those silence frames would
        otherwise reach ``session.send_audio`` and (on Gemini Live)
        be interpreted as a new user-turn activity.
        """
        # First reuse the base class's paced script playback. We re-yield
        # empty bytes after the commit driver fires.
        async for frame in super().capture_frames():
            if self._post_commit_silence:
                yield b""
            else:
                yield frame
        # Script exhausted — keep yielding frames at production cadence
        # so the unified pipeline's capture loop doesn't exit yet.
        loop = asyncio.get_running_loop()
        next_emit_at = loop.time() + (FRAME_DURATION_MS / 1000.0)
        while not self._hold_stop.is_set():
            now = loop.time()
            delay = next_emit_at - now
            if delay > 0:
                try:
                    await asyncio.wait_for(
                        self._hold_stop.wait(), timeout=delay
                    )
                    break  # stop signal arrived during the sleep
                except TimeoutError:
                    pass
            if self._post_commit_silence:
                yield b""
            else:
                yield silence_frame()
            next_emit_at += FRAME_DURATION_MS / 1000.0


def _interrupt_tone_pcm() -> bytes:
    """A short tone the harness can splice in to drive a fresh user turn."""
    chunks = speech_frames(_BARGE_IN_TONE_DURATION_MS)
    return b"".join(chunks)


def _collect_agent_spoke(bus: InMemoryEventBus) -> list[AgentSpoke]:
    return [e for e in bus.snapshot() if isinstance(e, AgentSpoke)]


def _collect_transcripts(bus: InMemoryEventBus) -> list[TranscriptFinalized]:
    return [e for e in bus.snapshot() if isinstance(e, TranscriptFinalized)]


@dataclass(slots=True)
class _RunArtifacts:
    """Side-channel data the runner accumulates while a scenario plays.

    ``interrupt_trigger_at`` / ``interrupt_completed_at`` are monotonic
    timestamps used to compute barge-in latency. ``s2s_events_observed``
    is a flat type-name list useful for diagnostics when an assertion
    fails (e.g. "we never saw an S2SAudioFrame — the session may have
    been closed before generation started").
    """

    interrupt_trigger_at: float | None = None
    interrupt_completed_at: float | None = None
    s2s_events_observed: list[str] = None  # type: ignore[assignment]
    first_audio_at: float | None = None
    response_completed_count: int = 0
    response_completed_reasons: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.s2s_events_observed is None:
            self.s2s_events_observed = []
        if self.response_completed_reasons is None:
            self.response_completed_reasons = []


class _InstrumentedSession(S2SSession):
    """Thin proxy that records S2S events as they stream past.

    The unified pipeline already drains :meth:`S2SSession.events` and
    publishes ``TranscriptFinalized`` / ``AgentSpoke`` on the event bus,
    but for interrupt-latency we want the timestamp of the FIRST
    ``S2SAudioFrame`` (so we know when generation actually started) and
    the timestamp of the ``S2SResponseCompleted`` that follows the
    interrupt trigger. We wrap the events iterator at the source so the
    proxy is the only place that sees them once.
    """

    def __init__(
        self, real_session: S2SSession, artifacts: _RunArtifacts
    ) -> None:
        self._real = real_session
        self._artifacts = artifacts

    async def send_audio(self, pcm: bytes) -> None:
        await self._real.send_audio(pcm)

    async def commit_user_turn(self) -> None:
        await self._real.commit_user_turn()

    async def interrupt(self) -> None:
        await self._real.interrupt()

    async def close(self) -> None:
        await self._real.close()

    def events(self) -> AsyncIterator[S2SEvent]:
        return self._wrap_events()

    async def _wrap_events(self) -> AsyncIterator[S2SEvent]:
        async for event in self._real.events():
            type_name = type(event).__name__
            self._artifacts.s2s_events_observed.append(type_name)
            if (
                isinstance(event, S2SAudioFrame)
                and self._artifacts.first_audio_at is None
            ):
                self._artifacts.first_audio_at = time.monotonic()
            if isinstance(event, S2SResponseCompleted):
                self._artifacts.response_completed_count += 1
                self._artifacts.response_completed_reasons.append(
                    event.finish_reason
                )
                if (
                    self._artifacts.interrupt_trigger_at is not None
                    and self._artifacts.interrupt_completed_at is None
                ):
                    self._artifacts.interrupt_completed_at = time.monotonic()
            yield event


class _InstrumentedS2SProvider(S2SProvider):
    """Wraps a real :class:`S2SProvider` so the runner can intercept events.

    Required so :meth:`UnifiedVoicePipeline.run` sees the wrapped
    session's :meth:`events` and the runner captures per-event
    timestamps inline rather than reading them after the fact from the
    event bus (the bus is multiplexed across types and doesn't carry
    monotonic timestamps).
    """

    def __init__(
        self, real_provider: S2SProvider, artifacts: _RunArtifacts
    ) -> None:
        self._real = real_provider
        self._artifacts = artifacts

    @property
    def name(self) -> str:
        return self._real.name

    async def open_session(
        self,
        *,
        instructions: str = "",
        voice_id: str | None = None,
        tools: Sequence[ToolDefinition] = (),
    ) -> S2SSession:
        real_session = await self._real.open_session(
            instructions=instructions,
            voice_id=voice_id,
            tools=tools,
        )
        return _InstrumentedSession(real_session, self._artifacts)

    async def close(self) -> None:
        await self._real.close()


async def _spawn_interrupt_driver(
    *,
    scenario: S2SScenario,
    pipeline: UnifiedVoicePipeline,
    artifacts: _RunArtifacts,
    interrupt_event_tag: str,
    transport: PacedScriptedTransport,
) -> None:
    """Wait until the bot has spoken at least one audio frame, then interrupt.

    The runner spawns this as a background task at scenario start. The
    ``interrupt_event_tag`` is the tag the runner used for the
    ``await_audio_then_interrupt`` silence slot. The driver waits up
    to ``runner_timeout_s - safety`` for the FIRST assistant audio frame,
    falling back to the timeline-reached signal only when the bot is
    apparently mute (which we still want to report as a failure). The
    long wait is necessary because real S2S APIs need ~2-4 s after a
    turn commit to start producing audio — firing the interrupt before
    the bot speaks means we never hear it and the audio-frame assertion
    fails.
    """
    deadline = time.monotonic() + scenario.runner_timeout_s
    # Safety floor: don't sit forever. We wait at most ``audio_wait_s``
    # AFTER the speaker timeline reaches the tagged slot before firing
    # the interrupt with no observed audio. Real S2S APIs take 2-4 s
    # after commit to produce the first audio frame; 6 s gives that a
    # comfortable buffer without wedging on a slow API. Early-cancel
    # (interrupt fires before any audio) is still a valid barge-in
    # shape — the assertion side reports it as informational.
    audio_wait_s = 6.0
    poll_interval_s = 0.05
    while time.monotonic() < deadline:
        if artifacts.first_audio_at is not None:
            # Small additional pause so the cancel lands mid-response
            # rather than at the very first frame — exercises the
            # actual barge-in path rather than the "cancel a fresh
            # response" path, and gives the bot some audio to truncate.
            await asyncio.sleep(0.25)
            break
        slot_reached = transport.capture_log.first_monotonic_for_tag(
            interrupt_event_tag
        )
        if (
            slot_reached is not None
            and time.monotonic() - slot_reached > audio_wait_s
        ):
            logger.warning(
                "interrupt driver: speaker reached %r %.1fs ago without any "
                "assistant audio — triggering anyway",
                interrupt_event_tag,
                audio_wait_s,
            )
            break
        await asyncio.sleep(poll_interval_s)
    if artifacts.interrupt_trigger_at is not None:
        return
    artifacts.interrupt_trigger_at = time.monotonic()
    if scenario.interrupt_kind == "session_interrupt":
        try:
            await pipeline.interrupt()
        except Exception:  # noqa: BLE001 — log + continue
            logger.exception("pipeline.interrupt raised")
    elif scenario.interrupt_kind == "new_user_turn":
        session = pipeline.session
        if session is None:
            logger.warning("interrupt driver: session not open yet")
            return
        try:
            tone = _interrupt_tone_pcm()
            await session.send_audio(tone)
            await session.commit_user_turn()
        except Exception:  # noqa: BLE001 — log + continue
            logger.exception(
                "new-user-turn interrupt raised against session"
            )
    else:  # pragma: no cover - defensive
        raise ValueError(f"unknown interrupt_kind={scenario.interrupt_kind!r}")


def _scenario_budget_s(scenario: S2SScenario, script_len: int) -> float:
    """Pipeline.run hard timeout."""
    script_s = (script_len * FRAME_DURATION_MS) / 1000.0
    return max(scenario.runner_timeout_s, script_s + scenario.drain_extra_s)


def _evaluate(
    scenario: S2SScenario,
    *,
    bus: InMemoryEventBus,
    transcript_sink: InMemoryTranscriptSink,
    artifacts: _RunArtifacts,
) -> list[AssertionResult]:
    """Common assertion battery for every S2S scenario."""
    assertions: list[AssertionResult] = []

    spoke = _collect_agent_spoke(bus)
    transcripts = _collect_transcripts(bus)

    # Audio-frame check. Hard requirement for non-interrupt scenarios
    # (smoke). For interrupt scenarios, "no audio" is acceptable if the
    # cancel landed before generation produced any frames — that's the
    # OpenAI Realtime "early-cancel" path. We accept either; the
    # latency assertion is the load-bearing one for interrupt cases.
    got_audio = artifacts.first_audio_at is not None
    audio_required = not scenario.expect_interrupt
    audio_passed = got_audio if audio_required else True
    if got_audio:
        audio_detail = "first_audio_at=set"
    elif audio_required:
        audio_detail = "no S2SAudioFrame observed during the scenario"
    else:
        audio_detail = (
            "no S2SAudioFrame observed — interrupt likely fired before "
            "generation started (early-cancel path; informational)"
        )
    assertions.append(
        AssertionResult(
            name="received_at_least_one_audio_frame",
            passed=audio_passed,
            detail=audio_detail,
        )
    )

    assertions.append(
        AssertionResult(
            name="received_response_completed",
            passed=artifacts.response_completed_count >= 1,
            detail=(
                f"completed_count={artifacts.response_completed_count} "
                f"reasons={artifacts.response_completed_reasons!r}"
            ),
        )
    )

    if scenario.expect_interrupt:
        triggered = artifacts.interrupt_trigger_at is not None
        completed = artifacts.interrupt_completed_at is not None
        assertions.append(
            AssertionResult(
                name="interrupt_was_triggered",
                passed=triggered,
                detail=(
                    "interrupt fired during the scenario"
                    if triggered
                    else "interrupt driver never fired — bot may never have spoken"
                ),
            )
        )
        if (
            triggered
            and completed
            and artifacts.interrupt_completed_at is not None
            and artifacts.interrupt_trigger_at is not None
        ):
            latency_s = (
                artifacts.interrupt_completed_at - artifacts.interrupt_trigger_at
            )
            within_budget = latency_s <= scenario.interrupt_latency_budget_s
            assertions.append(
                AssertionResult(
                    name="interrupt_to_response_completed_latency",
                    passed=within_budget,
                    detail=(
                        f"latency_ms={latency_s * 1000:.0f} "
                        f"vs budget_ms={scenario.interrupt_latency_budget_s * 1000:.0f}"
                    ),
                )
            )
        else:
            assertions.append(
                AssertionResult(
                    name="interrupt_to_response_completed_latency",
                    passed=False,
                    detail=(
                        "missing trigger or completed event — "
                        f"trigger_at={artifacts.interrupt_trigger_at} "
                        f"completed_at={artifacts.interrupt_completed_at}"
                    ),
                )
            )
        # Wide acceptance: the finish_reason for the LAST completion
        # SHOULD be "interrupted" for OpenAI Realtime (because we sent
        # response.cancel) and may be "interrupted" or "stop" for
        # Gemini Live (depends on whether the server VAD fired before
        # natural completion). We accept either — the latency budget
        # is the real assertion; the reason is informational.
        last_reason = (
            artifacts.response_completed_reasons[-1]
            if artifacts.response_completed_reasons
            else None
        )
        assertions.append(
            AssertionResult(
                name="last_finish_reason_observed",
                passed=last_reason is not None,
                detail=(
                    f"last_reason={last_reason!r} "
                    f"all_reasons={artifacts.response_completed_reasons!r}"
                ),
            )
        )
    else:
        assertions.append(
            AssertionResult(
                name="no_interrupt_was_triggered",
                passed=artifacts.interrupt_trigger_at is None,
                detail=(
                    f"interrupt_trigger_at={artifacts.interrupt_trigger_at}"
                ),
            )
        )

    # Soft check: at least one assistant utterance landed in the bus.
    # When the S2S provider sends an empty/very-short response (the
    # smoke scenario asks for "hello") the transcript may not be
    # emitted as a finalised event — both providers stream tokens but
    # only emit a final marker when their internal sentence/turn
    # closes. Don't fail on this; just record it.
    has_assistant = any(
        isinstance(t, TranscriptFinalized) and t.speaker == "assistant"
        for t in transcripts
    )
    assertions.append(
        AssertionResult(
            name="assistant_transcript_observed",
            passed=True,  # informational only
            detail=(
                "yes" if has_assistant else "no (informational — some S2S "
                "providers don't emit a finalised assistant transcript "
                "for short turns)"
            ),
        )
    )
    _ = spoke  # accessible via artifacts; kept for future assertions
    _ = transcript_sink  # ditto

    return assertions


async def _spawn_commit_driver(
    *,
    scenario: S2SScenario,
    pipeline: UnifiedVoicePipeline,
    transport: _HoldingScriptedTransport,
) -> None:
    """Drive ``commit_user_turn`` once the speaker finishes the prompt.

    The :class:`UnifiedVoicePipeline` only commits when its capture loop
    exits, but the harness's holding transport keeps capture alive so
    the response has time to land. Without an explicit commit, the
    server never knows the turn ended and never starts generating —
    especially in manual-VAD mode where there's no server-side timer.

    The driver watches the transport's capture log for the LAST speech
    event in the scenario timeline. When that event's tag is emitted +
    a short ``commit_grace_s`` (so trailing silence flushes into the
    buffer), it calls ``session.commit_user_turn()``. Idempotent — a
    second commit is a no-op or yields a "buffer too small" warning the
    adapter swallows.
    """
    last_speech_tag = next(
        (e.tag for e in reversed(scenario.timeline) if e.kind == "speech"),
        None,
    )
    if last_speech_tag is None:
        return
    deadline = time.monotonic() + scenario.runner_timeout_s
    commit_grace_s = 0.3
    while time.monotonic() < deadline:
        last_emitted = transport.capture_log.last_monotonic_for_tag(
            last_speech_tag
        )
        if last_emitted is not None:
            elapsed_since_last = time.monotonic() - last_emitted
            if elapsed_since_last >= commit_grace_s:
                break
            await asyncio.sleep(commit_grace_s - elapsed_since_last)
            continue
        await asyncio.sleep(0.05)
    session = pipeline.session
    if session is None:
        logger.warning("commit driver: session not open when commit fired")
        return
    try:
        await session.commit_user_turn()
    except Exception:  # noqa: BLE001 — log + continue
        logger.exception("commit driver: commit_user_turn raised")
    finally:
        # Silence the holding transport's audio so the S2S provider
        # doesn't see post-commit frames as a new user turn — required
        # for Gemini Live, harmless for OpenAI Realtime.
        transport.signal_silence_after_commit()


async def _spawn_shutdown_driver(
    *,
    scenario: S2SScenario,
    pipeline: UnifiedVoicePipeline,
    transport: _HoldingScriptedTransport,
    artifacts: _RunArtifacts,
) -> None:
    """Stop the holding transport once the response is complete.

    The holding transport keeps yielding silence so the pipeline doesn't
    tear down before the S2S response lands. This driver watches the
    artifacts and signals the transport to stop once we've seen the
    end-of-response — for interrupt scenarios that's the completion
    AFTER the interrupt triggered, for non-interrupt scenarios it's any
    completion.

    A grace period after capture stop lets the events loop drain the
    final transcript / audio frames before the unified pipeline closes
    the session.
    """
    deadline = time.monotonic() + scenario.runner_timeout_s
    # For interrupt scenarios, we want a completion that happened
    # AFTER the interrupt fired — natural completions before that
    # don't satisfy the test.
    while time.monotonic() < deadline:
        if scenario.expect_interrupt:
            ready = (
                artifacts.interrupt_trigger_at is not None
                and artifacts.interrupt_completed_at is not None
            )
        else:
            ready = artifacts.response_completed_count >= 1
        if ready:
            break
        await asyncio.sleep(0.1)
    # Small grace so the events loop publishes the trailing
    # transcript / audio frames before the unified pipeline tears down.
    await asyncio.sleep(0.5)
    transport.signal_stop()


async def run_s2s_scenario(
    scenario: S2SScenario,
    bundle: S2SProviderBundle,
    *,
    surface: Surface = "meet",
) -> ScenarioResult:
    """Execute one S2S scenario end-to-end against a real provider."""
    logger.info(
        "s2s scenario start: %s (provider=%s surface=%s)",
        scenario.name,
        bundle.provider_name,
        surface,
    )
    script = _expand_scenario_to_frames(scenario)
    transport = _HoldingScriptedTransport(
        script=script, frame_duration_ms=FRAME_DURATION_MS
    )
    event_bus = InMemoryEventBus()
    transcript_sink = InMemoryTranscriptSink()
    utterance_sink = InMemoryUtteranceSink()
    artifacts = _RunArtifacts()
    pipeline = UnifiedVoicePipeline(
        transport=transport,
        s2s=_InstrumentedS2SProvider(bundle.provider, artifacts),
        event_bus=event_bus,
        config=UnifiedPipelineConfig(
            instructions=scenario.instructions,
            voice_id=bundle.voice_id,
            session_id=f"e2e-s2s-{scenario.name}-{surface}",
        ),
        transcript_sink=transcript_sink,
        utterance_sink=utterance_sink,
    )

    # Find the tag of the slot the runner should bridge to fire the
    # interrupt. The first slot flagged ``await_audio_then_interrupt``
    # wins; scenarios with ``expect_interrupt=False`` skip this entirely.
    interrupt_tag = next(
        (e.tag for e in scenario.timeline if e.await_audio_then_interrupt),
        None,
    )

    start = time.monotonic()
    interrupt_task: asyncio.Task[None] | None = None
    if scenario.expect_interrupt and interrupt_tag is not None:
        interrupt_task = asyncio.create_task(
            _spawn_interrupt_driver(
                scenario=scenario,
                pipeline=pipeline,
                artifacts=artifacts,
                interrupt_event_tag=interrupt_tag,
                transport=transport,
            )
        )
    commit_task = asyncio.create_task(
        _spawn_commit_driver(
            scenario=scenario,
            pipeline=pipeline,
            transport=transport,
        )
    )
    shutdown_task = asyncio.create_task(
        _spawn_shutdown_driver(
            scenario=scenario,
            pipeline=pipeline,
            transport=transport,
            artifacts=artifacts,
        )
    )

    try:
        try:
            await asyncio.wait_for(
                pipeline.run(),
                timeout=_scenario_budget_s(scenario, script_len=len(script)),
            )
        except TimeoutError:
            elapsed = time.monotonic() - start
            transport.signal_stop()
            logger.exception(
                "s2s scenario %s timed out at %.1fs", scenario.name, elapsed
            )
            return ScenarioResult(
                name=scenario.name,
                description=scenario.description,
                duration_s=elapsed,
                error=(
                    f"unified pipeline.run timed out for provider="
                    f"{bundle.provider_name} surface={surface}"
                ),
                transcripts_persisted=[
                    r.text for r in transcript_sink.snapshot()
                ],
                played_frame_count=len(transport.played),
            )
        except S2SError as exc:
            elapsed = time.monotonic() - start
            transport.signal_stop()
            return ScenarioResult(
                name=scenario.name,
                description=scenario.description,
                duration_s=elapsed,
                error=(
                    f"S2SError during pipeline.run: {exc!s} "
                    f"(provider={bundle.provider_name} surface={surface})"
                ),
                transcripts_persisted=[
                    r.text for r in transcript_sink.snapshot()
                ],
                played_frame_count=len(transport.played),
            )
    finally:
        for task in (interrupt_task, commit_task, shutdown_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    duration = time.monotonic() - start
    assertions = _evaluate(
        scenario,
        bus=event_bus,
        transcript_sink=transcript_sink,
        artifacts=artifacts,
    )

    # Stamp the interrupt latency on the result so the JSON report
    # surfaces it; render_summary doesn't include it but operators can
    # grep ``report.json`` for the field.
    interrupt_latency_ms: float | None = None
    if (
        artifacts.interrupt_trigger_at is not None
        and artifacts.interrupt_completed_at is not None
    ):
        interrupt_latency_ms = (
            (
                artifacts.interrupt_completed_at
                - artifacts.interrupt_trigger_at
            )
            * 1000.0
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
            s.audio_duration_ms for s in _collect_agent_spoke(event_bus)
        ],
        played_frame_count=len(transport.played),
        interrupt_to_cut_ms=interrupt_latency_ms,
    )


async def run_s2s_suite(
    scenarios: Iterable[S2SScenario],
    bundle: S2SProviderBundle,
    *,
    surface: Surface = "meet",
) -> list[ScenarioResult]:
    """Run every S2S scenario in sequence; bundle is closed by the caller."""
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        result = await run_s2s_scenario(scenario, bundle, surface=surface)
        verdict = "PASS" if result.passed else "FAIL"
        logger.info(
            "s2s scenario end: %s [%s] (%.2fs)",
            scenario.name,
            verdict,
            result.duration_s,
        )
        results.append(result)
    return results


__all__ = [
    "Surface",
    "run_s2s_scenario",
    "run_s2s_suite",
]
