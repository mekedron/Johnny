"""Unified voice pipeline driven by a single S2S provider (Johnny-ckz.17).

The split pipeline (:class:`johnny.voice_pipeline.pipeline.VoicePipeline`)
runs STT, an LLM router/answer, and TTS as three independent stages.
The unified pipeline collapses all three into one bidirectional session
with an :class:`app.providers.s2s_base.S2SProvider` — OpenAI
GPT-Realtime, Gemini Live, or similar. Audio flows in and out over a
single connection; the provider handles VAD, transcription, generation,
and synthesis internally.

The same transports work in both modes — :class:`JohnnyTransport`
exposes capture/playback methods the unified pipeline drives just like
the split pipeline does. The same :class:`EventBus` carries
:class:`TranscriptFinalized` / :class:`AgentSpoke` /
:class:`PipelineTiming` events so the per-turn activity log
(Johnny-ckz.7) and the live UI work without per-mode changes.

Scope (Johnny-ckz.17):

* Open one S2S session for the meeting lifetime.
* Forward capture-side PCM into ``session.send_audio``.
* Drain ``session.events()`` — assistant audio goes to the transport,
  user/assistant transcripts go to the event bus + transcript sink.
* Plumb interrupts: a transport-level ``cancel_playback`` or an
  explicit ``interrupt()`` call yields the floor to the participant
  via ``session.interrupt()``.
* End-to-end error containment: an S2S transport failure logs and
  exits cleanly rather than crashing the session orchestrator.

Out of scope for this iteration: VAD-driven local barge-in (the S2S
provider's internal VAD handles speech onset cheaper than running
Silero in addition), the noise-stoplist gate (provider-side STT is
authoritative when no Whisper hallucinations are involved), and the
two-stage router (the unified provider answers immediately — there is
no separate "should the bot speak" decision). All of those can be
revisited per real S2S adapter if needed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.providers.s2s_base import (
    S2SAudioFrame,
    S2SError,
    S2SProvider,
    S2SResponseCompleted,
    S2SResponseStarted,
    S2SSession,
    S2STranscript,
)
from johnny.voice_pipeline.event_bus import EventBus
from johnny.voice_pipeline.events import (
    AgentSpoke,
    TranscriptFinalized,
)
from johnny.voice_pipeline.transcript_sink import (
    NoopTranscriptSink,
    TranscriptSink,
)
from johnny.voice_pipeline.transport import JohnnyTransport
from johnny.voice_pipeline.utterance_sink import (
    NoopUtteranceSink,
    UtteranceSink,
)

logger = logging.getLogger(__name__)

DEFAULT_FRAME_DURATION_MS = 20
"""Per-frame duration used when chunking audio for the S2S provider.

Matches the split pipeline's default. The S2S provider may apply its
own framing on the wire (OpenAI Realtime buffers internally; Gemini
Live works with arbitrary-size append events) but a consistent inbound
frame size keeps the timing rows comparable across modes.
"""


@dataclass(frozen=True, slots=True)
class UnifiedPipelineConfig:
    """Per-session configuration for :class:`UnifiedVoicePipeline`.

    Kept structurally similar to :class:`PipelineConfig` so callers can
    pass instructions/context through. The unified pipeline does not
    consume mode/threshold/allowlist knobs because the S2S provider
    handles routing internally.
    """

    instructions: str = ""
    context: str = ""
    calendar_context: str = ""
    voice_id: str | None = None
    frame_duration_ms: int = DEFAULT_FRAME_DURATION_MS
    session_id: str | None = None
    bot_session_id: int | None = None


class UnifiedVoicePipeline:
    """Orchestrates a unified S2S session against a :class:`JohnnyTransport`.

    Lifecycle:

    1. :meth:`run` opens the S2S session via ``provider.open_session``.
    2. Two concurrent tasks drive the connection — a capture task forwards
       transport audio into ``session.send_audio``, and an events task
       drains ``session.events()`` and dispatches audio frames to the
       transport, transcripts to the event bus + sinks.
    3. On capture EOF (transport closes) or explicit
       :meth:`shutdown`, both tasks unwind and the session is closed.

    The pipeline is intentionally framework-light to mirror the split
    pipeline — pure asyncio, no external orchestration framework, so the
    meet-worker container stays minimal.
    """

    def __init__(
        self,
        transport: JohnnyTransport,
        s2s: S2SProvider,
        event_bus: EventBus,
        config: UnifiedPipelineConfig | None = None,
        transcript_sink: TranscriptSink | None = None,
        utterance_sink: UtteranceSink | None = None,
    ) -> None:
        self.transport = transport
        self.s2s = s2s
        self.event_bus = event_bus
        self.config = config or UnifiedPipelineConfig()
        self.transcript_sink = transcript_sink or NoopTranscriptSink()
        self.utterance_sink = utterance_sink or NoopUtteranceSink()
        self._session: S2SSession | None = None
        self._stop_event = asyncio.Event()
        self._assistant_audio_running_text: list[str] = []
        self._assistant_audio_byte_count = 0
        self._session_started_at: float = 0.0

    @property
    def session(self) -> S2SSession | None:
        """The currently open S2S session, or ``None`` before :meth:`run`."""
        return self._session

    # ------------------------------------------------------------------
    # Public lifecycle

    async def run(self) -> None:
        """Open the S2S session and drive it until the transport closes."""
        loop = asyncio.get_running_loop()
        self._session_started_at = loop.time()
        try:
            session = await self.s2s.open_session(
                instructions=self.config.instructions,
                voice_id=self.config.voice_id,
            )
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception(
                "S2S open_session failed for session=%s: %s",
                self.config.session_id,
                exc,
            )
            return
        self._session = session
        logger.info(
            "unified pipeline opened S2S session for session=%s",
            self.config.session_id,
        )

        capture_task = asyncio.create_task(self._capture_loop(session))
        events_task = asyncio.create_task(self._events_loop(session))
        stop_task = asyncio.create_task(self._stop_event.wait())
        try:
            # First wait: capture EOF, events crash, or stop signal.
            done, _ = await asyncio.wait(
                (capture_task, events_task, stop_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                if task is stop_task:
                    continue
                try:
                    task.result()
                except S2SError:
                    logger.exception(
                        "S2S session reported transport error for session=%s",
                        self.config.session_id,
                    )
                except Exception:  # noqa: BLE001 — log + continue teardown
                    logger.exception(
                        "unified pipeline worker crashed for session=%s",
                        self.config.session_id,
                    )
            # Capture has ended (EOF or stop) — its finally already
            # called ``commit_user_turn``, so the provider has queued
            # any in-flight response events. Give the events loop a
            # short drain window to publish them before we close the
            # session, otherwise the AgentSpoke / final transcript can
            # be cancelled mid-flight.
            if not events_task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(events_task), timeout=0.5
                    )
                except asyncio.TimeoutError:
                    pass
                except S2SError:
                    logger.exception(
                        "S2S events loop reported transport error during drain "
                        "for session=%s",
                        self.config.session_id,
                    )
                except Exception:  # noqa: BLE001 — drain is best-effort
                    logger.exception(
                        "events loop crashed during drain for session=%s",
                        self.config.session_id,
                    )
        finally:
            # Close the session so the events queue receives its EOF
            # sentinel and the events loop exits naturally — without
            # this it would block on the next queue.get() forever and
            # we'd have to cancel it.
            try:
                await session.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                logger.exception(
                    "S2S session close raised for session=%s",
                    self.config.session_id,
                )
            # Give the events loop one last chance to exit cleanly
            # after the close sentinel, then cancel any stragglers.
            if not events_task.done():
                try:
                    await asyncio.wait_for(events_task, timeout=1.0)
                except (asyncio.TimeoutError, Exception):
                    pass
            for task in (capture_task, events_task, stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                capture_task, events_task, stop_task, return_exceptions=True
            )

    async def shutdown(self) -> None:
        """Signal :meth:`run` to unwind and close the session.

        Idempotent. Safe to call from a different task than the one
        driving :meth:`run`. Mirrors the meet-worker's stop_event flow.
        """
        self._stop_event.set()

    async def interrupt(self) -> None:
        """Yield the floor to the participant.

        Cuts the current assistant response (if any) at the S2S
        provider AND drops any audio already queued for playback. The
        unified provider may emit a few in-flight frames before
        observing the cancellation; those are drained as no-ops by the
        events loop and discarded by ``transport.cancel_playback``.
        """
        try:
            self.transport.cancel_playback()
        except Exception:  # noqa: BLE001 — never block the interrupt
            logger.exception(
                "transport.cancel_playback raised for session=%s",
                self.config.session_id,
            )
        session = self._session
        if session is None:
            return
        try:
            await session.interrupt()
        except Exception:  # noqa: BLE001 — log + continue
            logger.exception(
                "S2S session.interrupt raised for session=%s",
                self.config.session_id,
            )

    # ------------------------------------------------------------------
    # Background loops

    async def _capture_loop(self, session: S2SSession) -> None:
        """Forward every transport capture frame into ``session.send_audio``.

        Exits when the transport's capture iterator ends (EOF on
        shutdown) or when a send raises :class:`S2SError`. Sends a
        single ``commit_user_turn`` at the end so the provider knows
        the user is done — adapters that ignore explicit commits treat
        it as a hint.
        """
        try:
            async for frame in self.transport.capture_frames():
                if not frame:
                    continue
                await session.send_audio(frame)
        except S2SError:
            raise
        except Exception:  # noqa: BLE001 — log + finish the loop
            logger.exception(
                "capture loop crashed for session=%s",
                self.config.session_id,
            )
        finally:
            # Signal end-of-input. Adapters that handle VAD internally
            # may already have committed; calling twice is harmless.
            try:
                await session.commit_user_turn()
            except Exception:  # noqa: BLE001 — best-effort tail
                logger.exception(
                    "commit_user_turn at end-of-capture raised for session=%s",
                    self.config.session_id,
                )

    async def _events_loop(self, session: S2SSession) -> None:
        """Drain ``session.events()`` and route each event downstream.

        Audio frames → transport playback queue.
        Transcripts → event bus + transcript sink.
        Response started/completed → activity log markers.
        Unknown event types are logged + skipped (forward-compatible).
        """
        try:
            async for event in session.events():
                if isinstance(event, S2SAudioFrame):
                    await self._dispatch_audio_frame(event)
                elif isinstance(event, S2STranscript):
                    await self._dispatch_transcript(event)
                elif isinstance(event, S2SResponseStarted):
                    self._on_response_started(event)
                elif isinstance(event, S2SResponseCompleted):
                    await self._on_response_completed(event)
                else:
                    logger.debug(
                        "unified pipeline ignoring unknown event "
                        "type=%s for session=%s",
                        type(event).__name__,
                        self.config.session_id,
                    )
        except S2SError:
            raise
        except Exception:  # noqa: BLE001 — log + exit loop
            logger.exception(
                "S2S events loop crashed for session=%s",
                self.config.session_id,
            )

    # ------------------------------------------------------------------
    # Per-event handlers

    async def _dispatch_audio_frame(self, event: S2SAudioFrame) -> None:
        """Forward an assistant-audio frame to the transport playback queue."""
        if not event.pcm:
            return
        self._assistant_audio_byte_count += len(event.pcm)
        try:
            await self.transport.play_frames(_single_frame(event.pcm))
        except Exception:  # noqa: BLE001 — log + continue
            logger.exception(
                "transport.play_frames raised for session=%s",
                self.config.session_id,
            )

    async def _dispatch_transcript(self, event: S2STranscript) -> None:
        """Publish a transcript event to the event bus + transcript sink."""
        if not event.is_final:
            # Partial deltas are streamed to event bus only — the sink
            # only takes finals so transcript_chunks stays clean.
            return
        text = event.text.strip()
        if not text:
            return
        timestamp_ms = self._now_ms()
        finalized = TranscriptFinalized(
            text=text,
            timestamp_ms=timestamp_ms,
            speaker=event.role,
            confidence=event.confidence,
            session_id=self.config.session_id,
        )
        try:
            await self.event_bus.publish(finalized)
        except Exception:  # noqa: BLE001 — log + continue
            logger.exception(
                "event_bus.publish raised for session=%s",
                self.config.session_id,
            )
        try:
            await self.transcript_sink.record(
                text=text,
                start_offset_ms=timestamp_ms,
                end_offset_ms=timestamp_ms,
                speaker=event.role,
                confidence=event.confidence,
                session_id=self.config.session_id,
                bot_session_id=self.config.bot_session_id,
            )
        except Exception:  # noqa: BLE001 — log + continue
            logger.exception(
                "transcript_sink.record raised for session=%s",
                self.config.session_id,
            )
        if event.role == "assistant":
            self._assistant_audio_running_text.append(text)

    def _on_response_started(self, event: S2SResponseStarted) -> None:
        """Reset per-response counters when a fresh assistant turn begins."""
        _ = event
        self._assistant_audio_running_text = []
        self._assistant_audio_byte_count = 0

    async def _on_response_completed(
        self, event: S2SResponseCompleted
    ) -> None:
        """Persist the completed assistant turn as an :class:`AgentSpoke`."""
        text = " ".join(self._assistant_audio_running_text).strip()
        if not text:
            return
        # Crude duration estimate: bytes / (sample_rate * 2). Adapters
        # that need a more precise figure can override by emitting their
        # own AgentSpoke via the bus. 16 kHz mono S16LE → 32 000 B/s.
        audio_duration_ms = int(
            self._assistant_audio_byte_count * 1000 / 32_000
        )
        spoke = AgentSpoke(
            text=text,
            audio_duration_ms=audio_duration_ms,
            timestamp_ms=self._now_ms(),
            session_id=self.config.session_id,
        )
        try:
            await self.event_bus.publish(spoke)
        except Exception:  # noqa: BLE001 — log + continue
            logger.exception(
                "event_bus.publish(AgentSpoke) raised for session=%s",
                self.config.session_id,
            )
        try:
            await self.utterance_sink.record(
                mode="unified",
                prompt=self.config.instructions,
                output_text=text,
                audio_duration_ms=audio_duration_ms,
                matched_allowed_reply=None,
                session_id=self.config.session_id,
                bot_session_id=self.config.bot_session_id,
            )
        except Exception:  # noqa: BLE001 — log + continue
            logger.exception(
                "utterance_sink.record raised for session=%s",
                self.config.session_id,
            )
        # Reset so subsequent turns don't accumulate.
        self._assistant_audio_running_text = []
        self._assistant_audio_byte_count = 0
        _ = event

    # ------------------------------------------------------------------
    # Internals

    def _now_ms(self) -> int:
        loop = asyncio.get_event_loop()
        return int((loop.time() - self._session_started_at) * 1000)


async def _single_frame(pcm: bytes):  # type: ignore[no-untyped-def]
    """Yield a single PCM frame for ``transport.play_frames``.

    The transport's ``play_frames`` accepts both sync and async
    iterables. Wrapping the single frame in an async generator means
    we don't have to materialise a list per frame and keeps memory
    bounded for long assistant responses.
    """
    yield pcm


__all__ = [
    "DEFAULT_FRAME_DURATION_MS",
    "UnifiedPipelineConfig",
    "UnifiedVoicePipeline",
]
