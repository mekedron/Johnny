"""Voice pipeline orchestrator.

Wires the stages together — transport → VAD → STT → router LLM → answer
LLM → TTS → transport — and emits :class:`PipelineEvent` instances to an
:class:`EventBus` as each stage completes. Stage classes are injected so
unit tests can use predictable fakes; production wires the real provider
instances from :func:`app.providers.loader.load_active_providers`.

The orchestrator is intentionally framework-light (pure asyncio) rather
than dragging in pipecat-ai's full FrameProcessor machinery. The mental
model is the same: an utterance is the unit of work, VAD chops the
audio stream into utterances, and each utterance flows through the
pipeline stages independently.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.providers import (
    ChatMessage,
    LLMProvider,
    LLMResponse,
    STTProvider,
    TranscriptEvent,
    TTSProvider,
)
from app.providers.base import PCM_SAMPLE_RATE_HZ
from johnny.voice_pipeline.event_bus import EventBus
from johnny.voice_pipeline.events import (
    AgentSpoke,
    RouterDecisionMade,
    TranscriptFinalized,
)
from johnny.voice_pipeline.transport import JohnnyTransport
from johnny.voice_pipeline.vad import DEFAULT_VAD_THRESHOLD, VADAnalyzer

logger = logging.getLogger(__name__)

DEFAULT_MAX_UTTERANCE_MS = 30_000
DEFAULT_END_OF_SPEECH_MS = 600
DEFAULT_FRAME_DURATION_MS = 20
DEFAULT_CONFIDENCE_THRESHOLD = 0.7


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Per-session configuration for the pipeline.

    Threshold and timing knobs are surfaced separately from the providers
    so a single set of provider instances can serve many meetings with
    different behaviours.
    """

    instructions: str = ""
    context: str = ""
    allowed_replies: tuple[str, ...] = ()
    speak: bool = True
    vad_threshold: float = DEFAULT_VAD_THRESHOLD
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    end_of_speech_ms: int = DEFAULT_END_OF_SPEECH_MS
    max_utterance_ms: int = DEFAULT_MAX_UTTERANCE_MS
    frame_duration_ms: int = DEFAULT_FRAME_DURATION_MS
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class RouterDecision:
    """Parsed output of the router LLM.

    Mirrors :class:`RouterDecisionMade` but kept separate so the pipeline
    can manipulate the decision before emitting (e.g. clamp confidence,
    log raw model output).
    """

    should_speak: bool
    confidence: float
    reason: str
    reply_type: str | None = None
    suggested_reply: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


_ROUTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "should_speak": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reason": {"type": "string"},
        "reply_type": {"type": ["string", "null"]},
        "suggested_reply": {"type": ["string", "null"]},
    },
    "required": ["should_speak", "confidence", "reason"],
}


class VoicePipeline:
    """Orchestrates the VAD → STT → LLM → TTS loop for one session."""

    def __init__(
        self,
        transport: JohnnyTransport,
        vad: VADAnalyzer,
        stt: STTProvider,
        router_llm: LLMProvider,
        answer_llm: LLMProvider,
        tts: TTSProvider,
        event_bus: EventBus,
        config: PipelineConfig | None = None,
    ) -> None:
        self.transport = transport
        self.vad = vad
        self.stt = stt
        self.router_llm = router_llm
        self.answer_llm = answer_llm
        self.tts = tts
        self.event_bus = event_bus
        self.config = config or PipelineConfig()
        self._session_started_at: float = 0.0
        self._utterance_count = 0

    # ------------------------------------------------------------------
    # Public lifecycle

    async def run(self) -> None:
        """Run the pipeline until the transport's capture stream ends."""
        loop = asyncio.get_running_loop()
        self._session_started_at = loop.time()
        async for utterance in self._utterances():
            await self._process_utterance(utterance)

    # ------------------------------------------------------------------
    # Utterance collection (VAD-driven segmentation)

    async def _utterances(self) -> AsyncIterator[bytes]:
        """Yield concatenated speech segments cut by VAD silence detection.

        Drives the transport's ``capture_frames()`` iterator; when VAD
        flips from speech→silence for ``end_of_speech_ms`` consecutive
        frames, the buffered speech is yielded as a single utterance. The
        VAD's :meth:`reset` is called between utterances so stateful
        analysers don't carry context across boundaries.
        """
        frame_ms = self.config.frame_duration_ms
        silence_frames_needed = max(1, self.config.end_of_speech_ms // frame_ms)
        max_frames = max(1, self.config.max_utterance_ms // frame_ms)
        buffer: list[bytes] = []
        silence_count = 0
        in_speech = False

        async for frame in self.transport.capture_frames():
            result = self.vad.analyze(frame)
            if result.is_speech:
                buffer.append(frame)
                silence_count = 0
                in_speech = True
                if len(buffer) >= max_frames:
                    yield b"".join(buffer)
                    buffer.clear()
                    silence_count = 0
                    in_speech = False
                    self.vad.reset()
            elif in_speech:
                buffer.append(frame)
                silence_count += 1
                if silence_count >= silence_frames_needed:
                    yield b"".join(buffer[: len(buffer) - silence_count])
                    buffer.clear()
                    silence_count = 0
                    in_speech = False
                    self.vad.reset()
            # else: pre-speech silence; drop frame

        if buffer and in_speech:
            yield b"".join(buffer[: len(buffer) - silence_count])

    # ------------------------------------------------------------------
    # Per-utterance processing

    async def _process_utterance(self, utterance: bytes) -> None:
        if not utterance:
            return
        self._utterance_count += 1

        transcript = await self._run_stt(utterance)
        if transcript is None:
            return
        await self.event_bus.publish(transcript)

        if not self.config.speak:
            return

        decision = await self._run_router(transcript)
        decision_event = RouterDecisionMade(
            should_speak=decision.should_speak,
            confidence=decision.confidence,
            reason=decision.reason,
            reply_type=decision.reply_type,
            suggested_reply=decision.suggested_reply,
            timestamp_ms=self._now_ms(),
            session_id=self.config.session_id,
        )
        await self.event_bus.publish(decision_event)

        if not decision.should_speak:
            return
        if decision.confidence < self.config.confidence_threshold:
            return

        await self._answer_and_speak(transcript, decision)

    # ------------------------------------------------------------------
    # Stage implementations

    async def _run_stt(self, utterance: bytes) -> TranscriptFinalized | None:
        events: list[TranscriptEvent] = []
        async for event in self.stt.transcribe_stream(_single_chunk(utterance)):
            events.append(event)
        finals = [e for e in events if e.is_final]
        if not finals:
            return None
        text_parts: list[str] = []
        speaker: str | None = None
        confidence: float | None = None
        max_ts = 0
        for e in finals:
            text_parts.append(e.text)
            if e.speaker:
                speaker = e.speaker
            if e.confidence is not None:
                confidence = e.confidence
            if e.timestamp_ms > max_ts:
                max_ts = e.timestamp_ms
        text = " ".join(p.strip() for p in text_parts if p).strip()
        if not text:
            return None
        return TranscriptFinalized(
            text=text,
            timestamp_ms=max_ts or self._now_ms(),
            speaker=speaker,
            confidence=confidence,
            session_id=self.config.session_id,
        )

    async def _run_router(self, transcript: TranscriptFinalized) -> RouterDecision:
        messages = self._router_messages(transcript)
        response = await self.router_llm.chat(
            messages,
            response_format=_ROUTER_SCHEMA,
        )
        return _parse_router_response(response)

    async def _answer_and_speak(
        self,
        transcript: TranscriptFinalized,
        decision: RouterDecision,
    ) -> None:
        messages = self._answer_messages(transcript, decision)
        answer_response = await self.answer_llm.chat(messages)
        raw_text = answer_response.text.strip()
        if not raw_text:
            return
        if self.config.allowed_replies:
            matched = _match_allowed_reply(raw_text, self.config.allowed_replies)
            if matched is None:
                return
            text = matched
        else:
            text = raw_text
        audio_bytes = b""
        async for frame in self.tts.synthesize_stream(text):
            audio_bytes += frame
        if not audio_bytes:
            return
        await self.transport.play_frames(
            [audio_bytes], source_rate=PCM_SAMPLE_RATE_HZ
        )
        spoke = AgentSpoke(
            text=text,
            audio_duration_ms=_pcm_duration_ms(
                len(audio_bytes), PCM_SAMPLE_RATE_HZ
            ),
            timestamp_ms=self._now_ms(),
            matched_allowed_reply=text if self.config.allowed_replies else None,
            session_id=self.config.session_id,
        )
        await self.event_bus.publish(spoke)

    # ------------------------------------------------------------------
    # Prompt construction

    def _router_messages(
        self, transcript: TranscriptFinalized
    ) -> list[ChatMessage]:
        system = (
            "You are the gating router for an AI meeting bot. Decide whether "
            "the bot should speak in response to the latest transcript. "
            "Reply as JSON matching the supplied schema."
        )
        if self.config.instructions:
            system += f"\n\nMeeting instructions: {self.config.instructions}"
        if self.config.context:
            system += f"\n\nContext: {self.config.context}"
        if self.config.allowed_replies:
            system += (
                "\n\nAllowed replies (the answer stage will pick verbatim from "
                f"this list): {list(self.config.allowed_replies)}"
            )
        return [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=transcript.text),
        ]

    def _answer_messages(
        self,
        transcript: TranscriptFinalized,
        decision: RouterDecision,
    ) -> list[ChatMessage]:
        system = (
            "You are an AI meeting participant. Produce a concise spoken "
            "reply to the latest transcript."
        )
        if self.config.instructions:
            system += f"\n\nMeeting instructions: {self.config.instructions}"
        if self.config.context:
            system += f"\n\nContext: {self.config.context}"
        if decision.suggested_reply:
            system += f"\n\nRouter suggested: {decision.suggested_reply}"
        if self.config.allowed_replies:
            system += (
                "\n\nYou MUST pick verbatim from these allowed replies: "
                f"{list(self.config.allowed_replies)}"
            )
        return [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=transcript.text),
        ]

    # ------------------------------------------------------------------
    # Helpers

    def _now_ms(self) -> int:
        loop = asyncio.get_running_loop()
        return int((loop.time() - self._session_started_at) * 1000)


# --- module-level helpers --------------------------------------------------


async def _single_chunk(chunk: bytes) -> AsyncIterator[bytes]:
    yield chunk


def _parse_router_response(response: LLMResponse) -> RouterDecision:
    structured = response.structured_output
    if structured is None and response.text:
        try:
            structured = json.loads(response.text)
        except (ValueError, TypeError):
            structured = None
    if not isinstance(structured, dict):
        return RouterDecision(
            should_speak=False,
            confidence=0.0,
            reason="router returned no structured output",
            raw={"text": response.text},
        )
    should_speak = bool(structured.get("should_speak", False))
    confidence = float(structured.get("confidence", 0.0))
    confidence = max(0.0, min(1.0, confidence))
    reason = str(structured.get("reason", ""))
    reply_type_raw = structured.get("reply_type")
    reply_type = str(reply_type_raw) if reply_type_raw is not None else None
    suggested_raw = structured.get("suggested_reply")
    suggested_reply = str(suggested_raw) if suggested_raw is not None else None
    return RouterDecision(
        should_speak=should_speak,
        confidence=confidence,
        reason=reason,
        reply_type=reply_type,
        suggested_reply=suggested_reply,
        raw=structured,
    )


def _match_allowed_reply(text: str, allowed: tuple[str, ...]) -> str | None:
    """Return the verbatim allowed reply matching ``text`` (case-insensitive).

    The pipeline accepts case-insensitive matches because the LLM may
    normalise casing, but the spoken reply is the canonical form from
    ``allowed`` (preserves any required casing for proper nouns, etc.).
    """
    candidate = text.strip().lower()
    for reply in allowed:
        if reply.strip().lower() == candidate:
            return reply
    return None


def _pcm_duration_ms(byte_count: int, sample_rate: int) -> int:
    samples = byte_count // 2  # s16 mono
    if sample_rate <= 0:
        return 0
    return int(samples * 1000 / sample_rate)


__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_END_OF_SPEECH_MS",
    "DEFAULT_FRAME_DURATION_MS",
    "DEFAULT_MAX_UTTERANCE_MS",
    "PipelineConfig",
    "RouterDecision",
    "VoicePipeline",
]
