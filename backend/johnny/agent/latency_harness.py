"""Scripted voice-turn latency harness with per-stage p50/p95 (Johnny-trt.1).

Manual 20-turn playground runs (Johnny-cxu) produced the Phase-0 latency
baseline but don't scale to per-phase re-measurement. This harness makes the
same measurement repeatable from one in-container command: it drives a real
:class:`~johnny.agent.browser_session.BrowserAgentSession` — the exact engine
the playground runs, every Phase-2 seam included (router gate, observability,
noise gate, answer nodes) — through a fake
:class:`~johnny.voice_pipeline.browser_transport.BrowserAudioTransport` by
pushing fixture speech as real-time-paced 16 kHz / 20 ms PCM frames, then
derives per-stage timings from the very rows the Johnny-ckz.7 instrument
persists: the :class:`~johnny.voice_pipeline.events.PipelineTiming` events the
:class:`~johnny.agent.observability.MetricsTranslator` publishes (the emit half
of ``session_timings`` — this harness collects them from an in-process
:class:`~johnny.voice_pipeline.event_bus.InMemoryEventBus` instead of the Redis
channel, so a run needs no API server and writes no DB rows).

Stage derivations match the Johnny-cxu analyzer (``.validation/Johnny-cxu/``,
docs/LATENCY.md): LiveKit 1.5.17 metrics stamp ``timestamp`` at stage END, so a
stage's start is ``started_at_ms - duration_ms`` and the router cost is the
STT-final → answer-LLM-start gap. Two timings the activity rows can't carry are
measured wall-clock because the harness owns both ends of the pipe: the VAD
end-of-speech commit (``user_state_changed`` speaking→listening edge vs. the
moment the last speech frame was pushed) and the first reply PCM frame reaching
the transport. STT rows are paired by turn *window*, not ``turn_id`` (STT
metrics carry no ``speech_id`` and attach to the previous turn — Johnny-5vb);
the harness runs turns strictly sequentially, so every event that lands between
a turn's first pushed frame and its post-terminal settle belongs to that turn.

Two provider modes:

* ``--providers stub`` (default) — registers in-process stub STT/LLM/TTS
  providers with small fixed delays and threads them through the *real*
  registry → ``build_agent_runtime`` → adapter path. No network, no DB rows,
  CI-friendly: the pytest integration (``tests/agent/test_latency_harness.py``)
  runs this mode.
* ``--providers local`` — loads the admin-configured active providers from the
  DB exactly like a playground session start
  (:func:`app.services.provider_payload.build_provider_payload`), so the
  harness measures the operator's real local stack (e.g. Parakeet sidecar +
  Ollama + Piper). Sanity gate: numbers should land within ~20% of the
  Johnny-cxu manual baseline for the same provider trio.

Cold start: the session is built fresh by every run (its own process via
``docker compose exec``), so turn 1 *is* the cold turn — it is always reported
separately from the warm (2..N) percentiles, which is the split the Phase-1
prewarm work needs.

Speech fixtures: Silero VAD only fires on real speech (DSP synthetics produce
zero events — Johnny-trt.2), so the harness ships the full Johnny-cxu
24-utterance set as piper-synthesized bot-addressed fixtures
(``fixtures/latency_turn_{short,medium,long}{1..8}.pcm``) and cycles them
across turns. Bot-addressed and *distinct* both matter for ``--providers
local``: a real router declines utterances that aren't for the bot AND repeats
of already-answered questions, either of which leaves no reply stages to time
— with 24 distinct texts a default-length local run gets a reply on nearly
every turn (runs past 24 turns recycle and will see repeat declines).

Run it (in-container — Docker-only rule)::

    docker compose exec api python -m johnny.agent.latency_harness --turns 20
    docker compose exec api python -m johnny.agent.latency_harness \
        --turns 24 --providers local --json-out /tmp/latency.json

Requires the ``agent`` extra (``livekit-agents``); like its siblings
(:mod:`johnny.agent.console_smoke`, :mod:`johnny.agent.sdk_surface_smoke`) it
is imported only where that extra is installed, never from the import-safe
top-level :mod:`johnny.agent` package.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.providers.base import (
    PCM_SAMPLE_RATE_HZ,
    ChatMessage,
    LLMProvider,
    LLMResponse,
    ProviderConfig,
    ProviderKind,
    STTProvider,
    ToolDefinition,
    TranscriptEvent,
    TTSProvider,
    get_registry,
)
from johnny.agent.job_config import AUTONOMOUS_MODE, SessionJobConfig
from johnny.voice_pipeline.browser_transport import BrowserAudioTransport
from johnny.voice_pipeline.event_bus import InMemoryEventBus
from johnny.voice_pipeline.events import (
    PipelineTiming,
    RouterDecisionMade,
    TranscriptFinalized,
    TurnTerminal,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from livekit.agents import EndpointingOptions
    from livekit.agents.vad import VAD
    from livekit.agents.voice.events import UserStateChangedEvent

    from johnny.agent.browser_session import BrowserAgentSession

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants

_FRAME_S = 0.02
_FRAME_BYTES = int(PCM_SAMPLE_RATE_HZ * _FRAME_S) * 2  # 20 ms of S16LE mono
_SILENCE_FRAME = b"\x00" * _FRAME_BYTES

STUB_STT_PROVIDER_NAME = "latency-harness-stt"
STUB_LLM_PROVIDER_NAME = "latency-harness-llm"
STUB_TTS_PROVIDER_NAME = "latency-harness-tts"

STUB_TRANSCRIPT_TEXT = "Johnny, can you give us a quick status update?"
STUB_REPLY_TEXT = "Latency harness reply: everything is on track."

# Bundled bot-addressed speech fixtures (16 kHz mono S16LE raw PCM, synthesized
# with the in-image piper CLI — see fixtures/README.md for provenance): the full
# Johnny-cxu 24-utterance set, one fixture per utterance, so a default-length
# local run repeats no question — the real router declines repeats ("already
# answered"), which would thin the replied-turn percentiles.
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
_FIXTURE_BANDS = ("short", "medium", "long")
BUNDLED_FIXTURES: dict[str, Path] = {
    f"{band}{i}": _FIXTURES_DIR / f"latency_turn_{band}{i}.pcm"
    for i in range(1, 9)
    for band in _FIXTURE_BANDS
}
# Aliases for the original three-fixture bundle (kept for CLI/test ergonomics).
BUNDLED_FIXTURES.update({band: BUNDLED_FIXTURES[f"{band}1"] for band in _FIXTURE_BANDS})
# Default cycle: all 24 distinct utterances, short/medium/long interleaved.
DEFAULT_FIXTURE_NAMES: tuple[str, ...] = tuple(
    f"{band}{i}" for i in range(1, 9) for band in _FIXTURE_BANDS
)

_HARNESS_INSTRUCTIONS = (
    "You are Johnny in a latency measurement harness. "
    "Answer every question with one short sentence."
)

# The report's stage metrics, in render order. Keys match TurnTimings fields.
# ``stt_final_after_vad_end_ms`` is the Phase-2 acceptance metric
# (Johnny-trt.12/.15): wall-clock gap between the VAD end-of-speech commit
# and the user transcript's FINAL event — negative when a streaming STT
# finalizes before the VAD floor elapses, ~+forward-time for batch STT.
REPORT_METRICS: tuple[str, ...] = (
    "vad_end_ms",
    "stt_ms",
    "stt_final_after_vad_end_ms",
    "triage_ms",
    "router_ms",
    "llm_ttft_ms",
    "llm_total_ms",
    "sentence_gap_ms",
    "tts_ttfb_ms",
    "first_audio_wall_ms",
    "e2e_vad_commit_ms",
)
# ``triage_ms`` (Johnny-trt.19) is the gate's own ``router_llm`` timing row —
# the direct triage LLM cost, present for every decided turn including
# delegate/status turns that have no answer stage at all. ``router_ms`` stays
# the legacy *derived* STT-final → answer-LLM-start gap (triage + scheduling
# overhead) for phase-over-phase comparability with the Johnny-cxu baseline.


# ---------------------------------------------------------------------------
# Stub providers — registered through the real registry so the harness session
# is assembled by the production build_agent_runtime path, adapters included.


def _opt_ms(options: dict[str, Any], key: str, default: float) -> float:
    """Read a millisecond knob from provider options, tolerating bad values."""
    try:
        return max(0.0, float(options.get(key, default)))
    except (TypeError, ValueError):
        return default


class HarnessStubSTTProvider(STTProvider):
    """Batch-shaped stub STT: drain one VAD segment, emit one fixed final.

    Declares ``batch_only`` so :func:`~johnny.agent.adapters.johnny_stt.build_stt_adapter`
    wraps it in the VAD-segmented :class:`~livekit.agents.stt.StreamAdapter` —
    the exact recognize path the configured local batch providers (Parakeet,
    faster-whisper) run, which is also the only STT path that emits a real
    ``stt_metrics`` duration (the streaming path only reports usage events, so
    it produces no ``session_timings`` stt row at all). Each ``recognize`` call
    hands it one VAD-cut utterance; it drains it, sleeps ``stt_delay_ms`` (the
    simulated transcribe cost), and yields the fixed bot-addressed transcript.
    """

    batch_only = True

    def __init__(self, config: ProviderConfig | None = None) -> None:
        options = dict(config.options) if config is not None else {}
        self._stt_delay_s = _opt_ms(options, "stt_delay_ms", 80.0) / 1000.0
        self._text = str(options.get("transcript_text") or STUB_TRANSCRIPT_TEXT)

    @property
    def name(self) -> str:
        return STUB_STT_PROVIDER_NAME

    async def transcribe_stream(
        self, audio_iter: AsyncIterator[bytes]
    ) -> AsyncIterator[TranscriptEvent]:
        total_bytes = 0
        async for chunk in audio_iter:
            total_bytes += len(chunk)
        if total_bytes <= 0:
            return
        if self._stt_delay_s:
            await asyncio.sleep(self._stt_delay_s)
        yield TranscriptEvent(
            text=self._text,
            is_final=True,
            timestamp_ms=int(total_bytes / 2 / PCM_SAMPLE_RATE_HZ * 1000),
            confidence=1.0,
        )


class HarnessStubLLMProvider(LLMProvider):
    """Stub router/answer LLM with distinct, configurable per-call delays.

    The router gate calls ``chat(..., response_format=<decision schema>)``
    (the no-catalog variant here — harness sessions wire no TaskCoordinator,
    Johnny-trt.59) — answered with an always-SPEAK verdict (confidence 0.95)
    after ``router_delay_ms``. The answer path streams a plain turn through
    ``stream_chat`` — answered with the fixed reply in two deltas, the first
    after ``answer_ttft_ms`` (so the llm metric's ``ttft`` is real, unlike the
    openai-compatible adapter's buffered fallback — Johnny-dny) and the rest
    after ``answer_extra_ms`` more.
    """

    def __init__(self, config: ProviderConfig | None = None) -> None:
        options = dict(config.options) if config is not None else {}
        self._router_delay_s = _opt_ms(options, "router_delay_ms", 60.0) / 1000.0
        self._answer_ttft_s = _opt_ms(options, "answer_ttft_ms", 120.0) / 1000.0
        self._answer_extra_s = _opt_ms(options, "answer_extra_ms", 80.0) / 1000.0
        self._reply = str(options.get("reply_text") or STUB_REPLY_TEXT)

    @property
    def name(self) -> str:
        return STUB_LLM_PROVIDER_NAME

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        if response_format is not None:
            await asyncio.sleep(self._router_delay_s)
            verdict = {
                "should_speak": True,
                "confidence": 0.95,
                "reason": "latency harness stub always speaks",
                "reply_type": "answer",
                "suggested_reply": self._reply,
            }
            return LLMResponse(
                text=json.dumps(verdict),
                finish_reason="stop",
                structured_output=verdict,
            )
        await asyncio.sleep(self._answer_ttft_s + self._answer_extra_s)
        return LLMResponse(text=self._reply, finish_reason="stop")

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
    ) -> AsyncIterator[str]:
        await asyncio.sleep(self._answer_ttft_s)
        head, _, tail = self._reply.partition(" ")
        yield head + " "
        if self._answer_extra_s:
            await asyncio.sleep(self._answer_extra_s)
        yield tail


class HarnessStubTTSProvider(TTSProvider):
    """Stub TTS emitting ``reply_audio_s`` of silence after a first-byte delay.

    The blind-sink playout estimate (``BrowserAudioOutput``) paces the reply's
    terminal off the pushed duration, so ``reply_audio_s`` directly controls
    how long each stub turn spends "speaking" — keep it short for fast runs.
    """

    def __init__(self, config: ProviderConfig | None = None) -> None:
        options = dict(config.options) if config is not None else {}
        self._first_byte_delay_s = _opt_ms(options, "tts_first_byte_delay_ms", 40.0) / 1000.0
        try:
            self._reply_audio_s = max(0.1, float(options.get("reply_audio_s", 1.0)))
        except (TypeError, ValueError):
            self._reply_audio_s = 1.0

    @property
    def name(self) -> str:
        return STUB_TTS_PROVIDER_NAME

    async def synthesize_stream(
        self, text: str, voice_id: str | None = None
    ) -> AsyncIterator[bytes]:
        if self._first_byte_delay_s:
            await asyncio.sleep(self._first_byte_delay_s)
        chunk = b"\x00" * (_FRAME_BYTES * 5)  # 100 ms per chunk
        for _ in range(max(1, round(self._reply_audio_s * 10))):
            yield chunk


def register_stub_providers() -> None:
    """Register the harness stubs in the global provider registry (idempotent)."""
    registry = get_registry()
    registry.register(
        ProviderKind.STT, STUB_STT_PROVIDER_NAME, HarnessStubSTTProvider, replace=True
    )
    registry.register(
        ProviderKind.LLM, STUB_LLM_PROVIDER_NAME, HarnessStubLLMProvider, replace=True
    )
    registry.register(
        ProviderKind.TTS, STUB_TTS_PROVIDER_NAME, HarnessStubTTSProvider, replace=True
    )


def stub_provider_config() -> dict[str, Any]:
    """The ``SessionJobConfig.provider_config`` payload for the stub trio."""
    return {
        "stt": {
            "provider_name": STUB_STT_PROVIDER_NAME,
            "display_name": "Latency harness stub STT",
            "credentials": {},
            "options": {},
        },
        "llm": {
            "provider_name": STUB_LLM_PROVIDER_NAME,
            "display_name": "Latency harness stub LLM",
            "credentials": {},
            "options": {},
        },
        "tts": {
            "provider_name": STUB_TTS_PROVIDER_NAME,
            "display_name": "Latency harness stub TTS",
            "credentials": {},
            "options": {},
        },
    }


def load_local_provider_config() -> dict[str, Any]:
    """Active provider payload from the DB — the playground session-start path."""
    import app.providers  # noqa: F401 — registers the real adapters
    from app.db.session import SessionLocal
    from app.security.crypto import get_crypto
    from app.services.provider_payload import build_provider_payload

    with SessionLocal() as db:
        return build_provider_payload(db, get_crypto())


# ---------------------------------------------------------------------------
# Fake mic + playback monitor — the harness's two ends of the transport pipe.


class FakeMic:
    """Real-time-paced synthetic mic over ``transport.push_capture_frame``.

    Pushes one 20 ms frame per tick against a drift-free deadline (the
    :mod:`johnny.agent.sdk_surface_smoke` pacing pattern): silence whenever no
    utterance is queued, fixture frames otherwise. Real-time pacing is load-
    bearing — Silero VAD, the away timer, and the blind sink's playout estimate
    are all wall-clock, so pushing faster than real time would skew every
    derived number.
    """

    def __init__(self, transport: BrowserAudioTransport) -> None:
        self._transport = transport
        self._pending: asyncio.Queue[tuple[list[bytes], asyncio.Future[tuple[float, float]]]] = (
            asyncio.Queue()
        )
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="latency-harness-mic")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def speak(self, pcm: bytes) -> tuple[float, float]:
        """Queue one utterance; resolves once its last frame was pushed.

        Returns ``(t_first_frame, t_last_frame)`` monotonic marks — the
        utterance's real start and end on the wire, the wall-clock anchors for
        the VAD-end and first-audio measurements.
        """
        frames = _pcm_to_frames(pcm)
        future: asyncio.Future[tuple[float, float]] = asyncio.get_running_loop().create_future()
        await self._pending.put((frames, future))
        return await future

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        current: list[bytes] | None = None
        current_future: asyncio.Future[tuple[float, float]] | None = None
        started_at = 0.0
        index = 0
        while True:
            if current is None and not self._pending.empty():
                current, current_future = self._pending.get_nowait()
                index = 0
                started_at = time.monotonic()
            if current is not None:
                self._transport.push_capture_frame(current[index])
                index += 1
                if index >= len(current):
                    if current_future is not None and not current_future.done():
                        current_future.set_result((started_at, time.monotonic()))
                    current = None
                    current_future = None
            else:
                self._transport.push_capture_frame(_SILENCE_FRAME)
            next_at += _FRAME_S
            delay = next_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)


def _pcm_to_frames(pcm: bytes) -> list[bytes]:
    """Split raw PCM into 20 ms frames, zero-padding the tail frame."""
    frames = [pcm[i : i + _FRAME_BYTES] for i in range(0, len(pcm), _FRAME_BYTES)]
    if frames and len(frames[-1]) < _FRAME_BYTES:
        frames[-1] = frames[-1] + b"\x00" * (_FRAME_BYTES - len(frames[-1]))
    return frames


class PlaybackMonitor:
    """Drain the transport's playback queue, stamping each frame's arrival.

    The first frame arriving after a turn's speech end is the reply's first
    audio on the wire — the "first frame to transport" metric the activity rows
    can't carry. Draining also keeps the queue from growing unboundedly (the
    WebSocket endpoint's job in a real session).
    """

    def __init__(self, transport: BrowserAudioTransport) -> None:
        self._transport = transport
        self.frames: list[tuple[float, int]] = []
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="latency-harness-playback")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def first_arrival_after(self, t: float) -> float | None:
        for arrived_at, _ in self.frames:
            if arrived_at >= t:
                return arrived_at
        return None

    async def _run(self) -> None:
        async for frame in self._transport.drain_playback_frames():
            self.frames.append((time.monotonic(), len(frame)))


# ---------------------------------------------------------------------------
# Per-turn results


@dataclass(slots=True)
class TurnTimings:
    """One turn's derived stage timings (ms). ``None`` = stage not observed."""

    turn: int
    fixture: str
    outcome: str  # "replied" | "no_reply" | "timeout"
    transcript: str = ""
    decision: str = ""
    vad_end_ms: float | None = None
    stt_ms: float | None = None
    stt_final_after_vad_end_ms: float | None = None
    triage_ms: float | None = None
    router_ms: float | None = None
    llm_ttft_ms: float | None = None
    llm_total_ms: float | None = None
    sentence_gap_ms: float | None = None
    tts_ttfb_ms: float | None = None
    first_audio_wall_ms: float | None = None
    e2e_vad_commit_ms: float | None = None
    tts_segments: int = 0

    def metric(self, name: str) -> float | None:
        value = getattr(self, name)
        return float(value) if value is not None else None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "turn": self.turn,
            "fixture": self.fixture,
            "outcome": self.outcome,
            "transcript": self.transcript,
            "decision": self.decision,
            "tts_segments": self.tts_segments,
        }
        for name in REPORT_METRICS:
            value = self.metric(name)
            out[name] = round(value, 1) if value is not None else None
        return out


@dataclass(slots=True)
class HarnessResult:
    """Whole-run outcome: per-turn timings + run metadata for the report."""

    providers_mode: str
    turns_requested: int
    prewarmed: bool = False
    vad_label: str = "browser-default"
    endpointing_label: str = "browser-default"
    turn_detection_label: str = "vad"
    task_wiring: bool = True
    turns: list[TurnTimings] = field(default_factory=list)

    @property
    def completed(self) -> list[TurnTimings]:
        return [t for t in self.turns if t.outcome != "timeout"]

    @property
    def replied(self) -> list[TurnTimings]:
        return [t for t in self.turns if t.outcome == "replied"]

    @property
    def cold_turn(self) -> TurnTimings | None:
        return self.turns[0] if self.turns else None

    @property
    def warm_replied(self) -> list[TurnTimings]:
        return [t for t in self.turns[1:] if t.outcome == "replied"]


def _percentile(values: list[float], p: float) -> float:
    """Linear-interpolated percentile (the Johnny-cxu analyzer's definition)."""
    ordered = sorted(values)
    k = (len(ordered) - 1) * p / 100
    f, c = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


def summarize(turns: Sequence[TurnTimings]) -> dict[str, dict[str, float]]:
    """Per-metric p50/p95/min/max over ``turns`` (metrics missing → skipped)."""
    summary: dict[str, dict[str, float]] = {}
    for name in REPORT_METRICS:
        values = [v for t in turns if (v := t.metric(name)) is not None]
        if not values:
            continue
        summary[name] = {
            "n": float(len(values)),
            "p50": round(_percentile(values, 50), 1),
            "p95": round(_percentile(values, 95), 1),
            "min": round(min(values), 1),
            "max": round(max(values), 1),
        }
    return summary


# ---------------------------------------------------------------------------
# The run loop


async def _wait_for_terminal(
    bus: InMemoryEventBus, start_index: int, *, timeout_s: float
) -> TurnTerminal | None:
    """Poll the bus for the first ``TurnTerminal`` published at/after ``start_index``."""
    deadline = time.monotonic() + timeout_s
    scanned = start_index
    while time.monotonic() < deadline:
        snapshot = bus.snapshot()
        for event in snapshot[scanned:]:
            if isinstance(event, TurnTerminal):
                return event
        scanned = len(snapshot)
        await asyncio.sleep(0.05)
    return None


async def _absorb_stragglers(
    bus: InMemoryEventBus, *, gap_s: float, max_extra_s: float = 15.0
) -> None:
    """Keep the inter-turn gap open until the bus has gone quiet.

    The plain inter-turn sleep doubles as the observation window, so a clean
    run pays nothing extra. If events are still landing during the gap (a
    reply outliving its settle, or an unexpected extra turn — e.g. a fixture
    the VAD split in two), the gap extends until one whole window passes with
    no new events, so stragglers can never leak into the next turn's window.
    """
    observed = len(bus.snapshot())
    await asyncio.sleep(gap_s)
    deadline = time.monotonic() + max_extra_s
    while len(bus.snapshot()) != observed and time.monotonic() < deadline:
        observed = len(bus.snapshot())
        logger.warning(
            "latency harness: events still landing during the inter-turn gap — "
            "extending it (check the fixture is a single VAD utterance)"
        )
        await asyncio.sleep(max(0.5, gap_s))


def _derive_turn(
    *,
    turn_number: int,
    fixture_name: str,
    events: Sequence[Any],
    terminal: TurnTerminal | None,
    speech_end_t: float,
    vad_end_t: float | None,
    first_audio_t: float | None,
    stt_final_t: float | None = None,
) -> TurnTimings:
    """Reduce one turn's event window + wall-clock marks to stage timings.

    Stage math mirrors the Johnny-cxu analyzer: a LiveKit metric's
    ``started_at_ms`` is the stage END (1.5.17 stamps ``timestamp`` on
    completion), so ``start = started_at_ms - duration_ms``;
    ``router_ms`` is the STT-final→answer-LLM-start gap;
    ``e2e_vad_commit_ms`` is STT-start (the VAD commit) → TTS first byte —
    the baseline-comparable derived end-to-end. ``first_audio_wall_ms`` is the
    harness's own wall-clock speech-end → first-transport-frame measurement
    (it additionally contains the VAD min-silence wait).
    """
    timings = TurnTimings(
        turn=turn_number,
        fixture=fixture_name,
        outcome=(terminal.terminal_state if terminal is not None else "timeout"),
    )
    if terminal is not None and terminal.terminal_state == "replied":
        timings.outcome = "replied"
    elif terminal is not None:
        timings.outcome = "no_reply"

    stt_rows: list[PipelineTiming] = []
    triage_rows: list[PipelineTiming] = []
    llm_rows: list[PipelineTiming] = []
    tts_rows: list[PipelineTiming] = []
    for event in events:
        if isinstance(event, TranscriptFinalized) and event.speaker != "agent":
            timings.transcript = event.text
        elif isinstance(event, RouterDecisionMade):
            timings.decision = (
                f"should_speak={event.should_speak} confidence={event.confidence:.2f}"
            )
        elif isinstance(event, PipelineTiming):
            if event.stage == "stt":
                stt_rows.append(event)
            elif event.stage == "router_llm":
                triage_rows.append(event)
            elif event.stage == "answer_llm":
                llm_rows.append(event)
            elif event.stage == "tts":
                tts_rows.append(event)

    if vad_end_t is not None:
        timings.vad_end_ms = (vad_end_t - speech_end_t) * 1000
        if stt_final_t is not None:
            # Negative = the (streaming) STT finalized before the VAD
            # min-silence floor elapsed; the turn commit is then VAD-bound.
            timings.stt_final_after_vad_end_ms = (stt_final_t - vad_end_t) * 1000
    if first_audio_t is not None:
        timings.first_audio_wall_ms = (first_audio_t - speech_end_t) * 1000

    stt = min(stt_rows, key=lambda r: r.started_at_ms) if stt_rows else None
    triage = min(triage_rows, key=lambda r: r.started_at_ms) if triage_rows else None
    llm = min(llm_rows, key=lambda r: r.started_at_ms) if llm_rows else None
    tts = min(tts_rows, key=lambda r: r.started_at_ms) if tts_rows else None
    timings.tts_segments = len(tts_rows)

    if stt is not None:
        timings.stt_ms = float(stt.duration_ms)
    if triage is not None:
        # The gate's own router_llm row (Johnny-trt.19): duration is the direct
        # triage cost; started_at_ms is the call START (gate-emitted rows keep
        # the documented field semantic — no end-stamp compensation needed).
        timings.triage_ms = float(triage.duration_ms)
    if llm is not None:
        timings.llm_total_ms = float(llm.duration_ms)
        ttft = llm.details.get("time_to_first_token_ms")
        if isinstance(ttft, (int, float)):
            timings.llm_ttft_ms = float(ttft)
        if stt is not None:
            llm_start = llm.started_at_ms - llm.duration_ms
            timings.router_ms = float(llm_start - stt.started_at_ms)
    if tts is not None:
        ttfb = tts.details.get("time_to_first_audio_ms")
        if isinstance(ttfb, (int, float)):
            timings.tts_ttfb_ms = float(ttfb)
        tts_start = tts.started_at_ms - tts.duration_ms
        if llm is not None:
            timings.sentence_gap_ms = float(tts_start - (llm.started_at_ms - llm.duration_ms))
        if stt is not None and timings.tts_ttfb_ms is not None:
            stt_start = stt.started_at_ms - stt.duration_ms
            timings.e2e_vad_commit_ms = float(tts_start + timings.tts_ttfb_ms - stt_start)
    return timings


async def run_latency_harness(
    *,
    turns: int = 20,
    providers_mode: str = "stub",
    provider_config: dict[str, Any] | None = None,
    fixture_paths: Sequence[tuple[str, Path]] | None = None,
    turn_timeout_s: float = 120.0,
    inter_turn_silence_s: float = 1.5,
    settle_s: float = 0.4,
    vad: VAD | None = None,
    vad_min_silence_s: float | None = None,
    endpointing_min_delay_s: float | None = None,
    semantic_eou: str = "auto",
    bot_session_id: int = 0,
    prewarm: bool = False,
    task_wiring: bool = True,
) -> HarnessResult:
    """Run ``turns`` scripted voice turns and return per-turn stage timings.

    Builds the real :class:`BrowserAgentSession` (mode ``autonomous`` — every
    router-approved turn auto-speaks) over a fake transport, then for each
    turn: pushes one fixture utterance through the paced mic, waits for the
    turn's INV-1 terminal on the harness-owned in-memory bus, settles briefly
    so trailing ``PipelineTiming`` publishes land, and derives the turn's
    timings from that window. Turns run strictly sequentially — the next
    utterance starts only after the previous reply's terminal — so no turn can
    barge into another (the Johnny-cxu run-B failure mode).

    ``prewarm=True`` (Johnny-trt.8) awaits the session's provider warm-up
    (:meth:`BrowserAgentSession.warm_up` — whisper weights, Piper voice ONNX,
    local-LLM ping) to *completion* before the first utterance. Production
    fires the same warm-up concurrently with session start; the harness
    awaits it so turn 1 measures the warmed steady state rather than a race
    against the loads — compare a ``prewarm=False`` run's cold turn against a
    ``prewarm=True`` run's to size the prewarm win.

    VAD selection (Johnny-trt.5): by default the session resolves the browser
    engine's own Silero model (0.40 s min-silence floor — shared by the
    VAD-only and semantic paths). ``vad_min_silence_s`` loads a Silero with
    a different floor instead (the A/B knob: ``0.55`` reproduces the
    pre-trt.5 default); an explicit ``vad`` instance wins over both.

    Endpointing (Johnny-trt.6): by default the session resolves the browser
    engine's own endpointing (``min_delay`` 0.40 s, plus ``max_delay`` 1.5 s
    when the semantic detector engages). ``endpointing_min_delay_s``
    overrides it — the A/B knob: ``0.5`` reproduces the pre-trt.6 LiveKit
    engine default.

    Semantic turn detection (Johnny-1qr): ``semantic_eou`` is the A/B knob
    over the in-process en-only EOU model. ``"auto"`` (default) lets the
    session decide exactly like production — for the language-less stub trio
    that resolves to VAD-only, for ``--providers local`` it follows the
    operator's STT language. ``"on"`` stamps ``language: "en"`` into the STT
    options (the stub carries none) and *requires* the detector
    (``build(semantic_eou=True)`` raises rather than measuring the wrong
    arm); pair it with ``prewarm=True`` so turn 1 does not pay the ~400 MB
    model load inside its EOU budget. ``"off"`` forces the tuned VAD-only
    path — the baseline arm. Both arms run the same 0.40 s floor, so the
    expected felt delta is ~0 — the knob exists to verify that, and to
    measure floor-drop experiments when combined with ``vad_min_silence_s``
    + ``endpointing_min_delay_s``.

    Task wiring (Johnny-trt.59): ``task_wiring`` is the delegation-capability
    A/B knob. ``True`` (default) assembles like production — task sink +
    coordinator + skill-loader catalog, so every router call carries the
    catalog prompt block and the full Phase-3 action+task schema. ``False``
    drops the DB factory: no catalog, and the router call is byte-identical
    to the Phase-2 build in both prompt and schema (the no-catalog variant)
    — the pure conversational hot-path arm for phase-over-phase baselines.
    The trt.21 capstone runs were unknowingly wired (the stub catalog rode
    along), so its "+568 ms schema cost" was really schema + catalog prompt
    combined — probe decomposition in ``.validation/Johnny-trt.59/``.
    """
    from johnny.agent.browser_session import (
        BROWSER_ENDPOINTING_MIN_DELAY_S,
        BROWSER_SEMANTIC_ENDPOINTING_MAX_DELAY_S,
        BROWSER_VAD_MIN_SILENCE_DURATION_S,
        BrowserAgentSession,
    )
    from johnny.agent.session import load_vad

    if semantic_eou not in ("auto", "on", "off"):
        raise ValueError(f"unknown semantic_eou {semantic_eou!r} (use auto, on or off)")
    if provider_config is None:
        if providers_mode == "stub":
            register_stub_providers()
            provider_config = stub_provider_config()
        elif providers_mode == "local":
            provider_config = load_local_provider_config()
        else:
            raise ValueError(f"unknown providers_mode {providers_mode!r}")
    for kind in ("stt", "llm"):
        if kind not in provider_config:
            raise RuntimeError(
                f"no active {kind.upper()} provider configured — the harness needs "
                "the full STT+LLM+TTS trio (configure providers, or use --providers stub)"
            )
    if semantic_eou == "on":
        # The explicit-on arm needs an English STT language for both the build
        # gate and the per-turn SpeechData stamps; the stub trio carries none.
        # Copy first — the caller's dict must not grow the stamp.
        provider_config = copy.deepcopy(provider_config)
        provider_config["stt"].setdefault("options", {}).setdefault("language", "en")

    # The harness session is synthetic: reply-audio WAVs under
    # JOHNNY_SESSION_AUDIO_DIR/<bot_session_id>/ would be junk, so disable the
    # recorder for this process before the runtime builds it from env.
    os.environ.pop("JOHNNY_SESSION_AUDIO_DIR", None)

    fixtures = _load_fixtures(fixture_paths)
    if vad is not None:
        vad_label = "caller-supplied"
    elif vad_min_silence_s is not None:
        vad = load_vad(min_silence_duration=vad_min_silence_s)
        vad_label = f"min_silence={vad_min_silence_s:g}s"
    else:
        # None → BrowserAgentSession.build resolves the browser default (the
        # one 0.40 s floor shared by the VAD-only and semantic paths).
        vad_label = f"browser-default (min_silence={BROWSER_VAD_MIN_SILENCE_DURATION_S:g}s)"

    endpointing_label: str | None
    if endpointing_min_delay_s is not None:
        endpointing: EndpointingOptions | None = {"min_delay": endpointing_min_delay_s}
        endpointing_label = f"min_delay={endpointing_min_delay_s:g}s"
    else:
        # None → BrowserAgentSession.build resolves the browser default, which
        # depends on whether the semantic detector engages — labeled post-build.
        endpointing = None
        endpointing_label = None

    config = SessionJobConfig(
        bot_session_id=bot_session_id,
        room_name=f"latency-harness-{bot_session_id}",
        # Behavior rides the snapshot since Johnny-trt.45; the harness brief
        # lands in the assignment-context slot (the one free-text slot).
        agent_snapshot={
            "mode": AUTONOMOUS_MODE,
            "assignment_context": _HARNESS_INSTRUCTIONS,
        },
        provider_config=provider_config,
        redis_url=None,  # the harness owns an in-memory bus; nothing reaches Redis/DB
    )

    bus = InMemoryEventBus()
    transport = BrowserAudioTransport()
    await transport.start()

    agent_session: BrowserAgentSession = await BrowserAgentSession.build(
        transport,
        config,
        event_bus=bus,
        vad=vad,
        endpointing=endpointing,
        semantic_eou=None if semantic_eou == "auto" else (semantic_eou == "on"),
        task_wiring=task_wiring,
    )

    # Resolve the endpointing default label now that the build settled the path.
    if endpointing_label is None:
        endpointing_label = (
            "browser-semantic-default "
            f"(min_delay={BROWSER_ENDPOINTING_MIN_DELAY_S:g}s, "
            f"max_delay={BROWSER_SEMANTIC_ENDPOINTING_MAX_DELAY_S:g}s)"
            if agent_session.semantic_eou_active
            else f"browser-default (min_delay={BROWSER_ENDPOINTING_MIN_DELAY_S:g}s)"
        )

    if prewarm:
        prewarm_start = time.perf_counter()
        await agent_session.warm_up()
        logger.info(
            "provider prewarm completed in %d ms (turn 1 measures warmed state)",
            int((time.perf_counter() - prewarm_start) * 1000),
        )

    # VAD end-of-speech commits surface as speaking→listening user-state edges;
    # the harness reads them wall-clock. Private seam (the sibling smokes use
    # the same kind of access); a production consumer would get a passthrough.
    user_states: list[tuple[str, str, float]] = []

    def _on_user_state(ev: UserStateChangedEvent) -> None:
        user_states.append((str(ev.old_state), str(ev.new_state), time.monotonic()))

    agent_session._session.on("user_state_changed", _on_user_state)  # noqa: SLF001

    # Wall-clock stamps of user FINAL transcripts — the seam for the
    # Phase-2 ``stt_final_after_vad_end_ms`` metric (streaming STT emits
    # its final near/before the VAD commit; batch STT only after it).
    user_final_ts: list[float] = []

    def _on_user_transcribed(ev: Any) -> None:
        if getattr(ev, "is_final", False):
            user_final_ts.append(time.monotonic())

    agent_session._session.on("user_input_transcribed", _on_user_transcribed)  # noqa: SLF001

    mic = FakeMic(transport)
    playback = PlaybackMonitor(transport)
    result = HarnessResult(
        providers_mode=providers_mode,
        turns_requested=turns,
        prewarmed=prewarm,
        vad_label=vad_label,
        endpointing_label=endpointing_label,
        turn_detection_label=agent_session.turn_detection_label,
        task_wiring=task_wiring,
    )

    try:
        await agent_session.start()
        mic.start()
        playback.start()

        for turn_number in range(1, turns + 1):
            fixture_name, fixture_pcm = fixtures[(turn_number - 1) % len(fixtures)]
            window_start = len(bus.snapshot())
            _, speech_end_t = await mic.speak(fixture_pcm)
            terminal = await _wait_for_terminal(bus, window_start, timeout_s=turn_timeout_s)
            await asyncio.sleep(settle_s)  # let trailing metric publishes land

            events = bus.snapshot()[window_start:]
            vad_end_t = next(
                (
                    t
                    for old, new, t in user_states
                    if old == "speaking" and new == "listening" and t >= speech_end_t
                ),
                None,
            )
            stt_final_t = next((t for t in user_final_ts if t >= speech_end_t), None)
            timings = _derive_turn(
                turn_number=turn_number,
                fixture_name=fixture_name,
                events=events,
                terminal=terminal,
                speech_end_t=speech_end_t,
                vad_end_t=vad_end_t,
                first_audio_t=playback.first_arrival_after(speech_end_t),
                stt_final_t=stt_final_t,
            )
            result.turns.append(timings)
            logger.info(
                "turn %02d/%d [%s] outcome=%s e2e_wall=%s vad_end=%s stt=%s router=%s "
                "llm=%s tts_ttfb=%s",
                turn_number,
                turns,
                fixture_name,
                timings.outcome,
                _fmt_ms(timings.first_audio_wall_ms),
                _fmt_ms(timings.vad_end_ms),
                _fmt_ms(timings.stt_ms),
                _fmt_ms(timings.router_ms),
                _fmt_ms(timings.llm_total_ms),
                _fmt_ms(timings.tts_ttfb_ms),
            )
            if inter_turn_silence_s and turn_number < turns:
                await _absorb_stragglers(bus, gap_s=inter_turn_silence_s)
    finally:
        await mic.stop()
        await agent_session.aclose()
        await transport.stop()
        transport.close_playback()
        await playback.stop()
    return result


def _load_fixtures(
    fixture_paths: Sequence[tuple[str, Path]] | None,
) -> list[tuple[str, bytes]]:
    """Read the fixture PCMs (default: the bundled 24-utterance cycle)."""
    if fixture_paths is None:
        fixture_paths = [(name, BUNDLED_FIXTURES[name]) for name in DEFAULT_FIXTURE_NAMES]
    loaded: list[tuple[str, bytes]] = []
    for name, path in fixture_paths:
        data = path.read_bytes()
        if not data:
            raise RuntimeError(f"fixture {path} is empty")
        loaded.append((name, data))
    if not loaded:
        raise RuntimeError("no speech fixtures to push")
    return loaded


def _fmt_ms(value: float | None) -> str:
    return f"{value:.0f}ms" if value is not None else "-"


# ---------------------------------------------------------------------------
# Report rendering


def render_report(result: HarnessResult) -> str:
    """Human-readable per-stage report: cold turn, warm percentiles, all-turn table."""
    lines: list[str] = []
    completed = result.completed
    replied = result.replied
    lines.append(
        f"latency harness: providers={result.providers_mode} "
        f"prewarm={'on' if result.prewarmed else 'off'} "
        f"vad={result.vad_label} "
        f"endpointing={result.endpointing_label} "
        f"turn_detection={result.turn_detection_label} "
        f"task_wiring={'on' if result.task_wiring else 'off'} "
        f"turns={len(result.turns)}/{result.turns_requested} "
        f"completed={len(completed)} replied={len(replied)} "
        f"no_reply={len(completed) - len(replied)} "
        f"timeout={len(result.turns) - len(completed)}"
    )

    cold = result.cold_turn
    if cold is not None:
        cold_bits = ", ".join(f"{name}={_fmt_ms(cold.metric(name))}" for name in REPORT_METRICS)
        lines.append(f"\ncold start (turn 1, fresh session, outcome={cold.outcome}):")
        lines.append(f"  {cold_bits}")

    warm = result.warm_replied
    if warm:
        lines.append(f"\nwarm turns (2..N, replied only, n={len(warm)}):")
        lines.append(f"  {'metric':22s} {'p50':>8s} {'p95':>8s} {'min':>8s} {'max':>8s}")
        for name, stats in summarize(warm).items():
            lines.append(
                f"  {name:22s} {stats['p50']:8.0f} {stats['p95']:8.0f} "
                f"{stats['min']:8.0f} {stats['max']:8.0f}"
            )

    all_replied = replied
    if all_replied and len(all_replied) != len(warm):
        lines.append(f"\nall replied turns (n={len(all_replied)}):")
        lines.append(f"  {'metric':22s} {'p50':>8s} {'p95':>8s} {'min':>8s} {'max':>8s}")
        for name, stats in summarize(all_replied).items():
            lines.append(
                f"  {name:22s} {stats['p50']:8.0f} {stats['p95']:8.0f} "
                f"{stats['min']:8.0f} {stats['max']:8.0f}"
            )

    lines.append("\nper-turn detail:")
    for timing in result.turns:
        lines.append(f"  {json.dumps(timing.to_dict())}")
    return "\n".join(lines)


def result_to_json(result: HarnessResult) -> dict[str, Any]:
    """Machine-readable run summary (the ``--json-out`` payload)."""
    cold = result.cold_turn
    return {
        "providers_mode": result.providers_mode,
        "prewarm": result.prewarmed,
        "vad": result.vad_label,
        "endpointing": result.endpointing_label,
        "turn_detection": result.turn_detection_label,
        "task_wiring": result.task_wiring,
        "turns_requested": result.turns_requested,
        "turns_run": len(result.turns),
        "completed": len(result.completed),
        "replied": len(result.replied),
        "cold_turn": cold.to_dict() if cold is not None else None,
        "warm_summary": summarize(result.warm_replied),
        "all_replied_summary": summarize(result.replied),
        "per_turn": [t.to_dict() for t in result.turns],
    }


# ---------------------------------------------------------------------------
# CLI


def _parse_fixture_arg(raw: str) -> list[tuple[str, Path]]:
    """Parse ``--fixtures``: comma list of bundled names and/or PCM file paths."""
    out: list[tuple[str, Path]] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if token in BUNDLED_FIXTURES:
            out.append((token, BUNDLED_FIXTURES[token]))
        else:
            path = Path(token)
            if not path.is_file():
                raise argparse.ArgumentTypeError(
                    f"fixture {token!r} is neither a bundled name "
                    "(short1..long8, or the short/medium/long aliases) nor a file"
                )
            out.append((path.stem, path))
    if not out:
        raise argparse.ArgumentTypeError("no fixtures given")
    return out


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entry: ``python -m johnny.agent.latency_harness``.

    Exit code 0 when every requested turn reached its terminal (replied or a
    router decline) — non-zero when any turn timed out or the run crashed.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--turns", type=int, default=20, help="number of voice turns (default 20)")
    parser.add_argument(
        "--providers",
        choices=("stub", "local"),
        default="stub",
        help="stub = in-process stub providers; local = the DB-configured active providers",
    )
    parser.add_argument(
        "--fixtures",
        type=_parse_fixture_arg,
        default=None,
        metavar="NAMES_OR_PATHS",
        help=(
            "comma list of bundled fixture names (short1..short8, medium1..medium8, "
            "long1..long8 — all 24 cycle by default; short/medium/long alias the "
            "1-files) and/or 16 kHz mono S16LE .pcm paths"
        ),
    )
    parser.add_argument(
        "--prewarm",
        action="store_true",
        help=(
            "await the session's provider warm_up() before turn 1 (Johnny-trt.8) "
            "so the cold turn measures the warmed steady state"
        ),
    )
    parser.add_argument(
        "--vad-min-silence-s",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "load the Silero VAD with this end-of-speech silence floor instead of "
            "the browser session's default (0.40 s; Johnny-trt.5). The A/B knob: "
            "0.55 reproduces the pre-trt.5 Silero default"
        ),
    )
    parser.add_argument(
        "--endpointing-min-delay-s",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "run the session with this engine endpointing min_delay instead of "
            "the browser session's default (0.40 s; Johnny-trt.6). The A/B knob: "
            "0.5 reproduces the pre-trt.6 LiveKit engine default"
        ),
    )
    parser.add_argument(
        "--semantic-eou",
        choices=("auto", "on", "off"),
        default="auto",
        help=(
            "in-process en-only semantic turn detector (Johnny-1qr): auto = let the "
            "session decide from the STT language (the stub trio carries none, so "
            "stub runs stay VAD-only); on = stamp language=en and require the "
            "detector (pair with --prewarm so turn 1 skips the model load); "
            "off = force the tuned VAD-only baseline arm"
        ),
    )
    parser.add_argument(
        "--task-wiring",
        choices=("on", "off"),
        default="on",
        help=(
            "delegation-capability A/B knob (Johnny-trt.59): on (default) = "
            "production-shaped assembly (task sink + coordinator + skill "
            "catalog → catalog prompt block + full action/task schema on "
            "every router call); off = no DB factory → empty catalog → the "
            "router call is byte-identical to the Phase-2 build (prompt AND "
            "schema) — the pure conversational hot-path arm"
        ),
    )
    parser.add_argument(
        "--turn-timeout-s",
        type=float,
        default=120.0,
        help="max seconds to wait for one turn's terminal (default 120)",
    )
    parser.add_argument(
        "--inter-turn-silence-s",
        type=float,
        default=1.5,
        help="silence between a turn's terminal and the next utterance (default 1.5)",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="also write the machine-readable summary JSON to this path",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Quieten the chattiest SDK loggers so the per-turn lines stay readable.
    for noisy in ("livekit", "httpx", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        result = asyncio.run(
            run_latency_harness(
                turns=max(1, args.turns),
                providers_mode=args.providers,
                fixture_paths=args.fixtures,
                turn_timeout_s=args.turn_timeout_s,
                inter_turn_silence_s=args.inter_turn_silence_s,
                prewarm=args.prewarm,
                vad_min_silence_s=args.vad_min_silence_s,
                endpointing_min_delay_s=args.endpointing_min_delay_s,
                semantic_eou=args.semantic_eou,
                task_wiring=args.task_wiring == "on",
            )
        )
    except Exception:
        logger.exception("latency harness FAILED (exception)")
        sys.exit(1)

    print(render_report(result))
    print("\nJSON:", json.dumps(result_to_json(result)))
    if args.json_out is not None:
        args.json_out.write_text(json.dumps(result_to_json(result), indent=2))
        print(f"wrote {args.json_out}")

    if len(result.completed) < len(result.turns) or len(result.turns) < max(1, args.turns):
        logger.error(
            "latency harness FAILED: %d/%d turns reached a terminal",
            len(result.completed),
            len(result.turns),
        )
        sys.exit(1)
    sys.exit(0)


__all__ = [
    "BUNDLED_FIXTURES",
    "DEFAULT_FIXTURE_NAMES",
    "REPORT_METRICS",
    "STUB_LLM_PROVIDER_NAME",
    "STUB_REPLY_TEXT",
    "STUB_STT_PROVIDER_NAME",
    "STUB_TRANSCRIPT_TEXT",
    "STUB_TTS_PROVIDER_NAME",
    "FakeMic",
    "HarnessResult",
    "HarnessStubLLMProvider",
    "HarnessStubSTTProvider",
    "HarnessStubTTSProvider",
    "PlaybackMonitor",
    "TurnTimings",
    "load_local_provider_config",
    "main",
    "register_stub_providers",
    "render_report",
    "result_to_json",
    "run_latency_harness",
    "stub_provider_config",
    "summarize",
]


if __name__ == "__main__":
    main()
