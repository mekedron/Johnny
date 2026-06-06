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
import re
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, cast

from app.providers import (
    ChatMessage,
    LLMProvider,
    LLMResponse,
    STTProvider,
    TranscriptEvent,
    TTSProvider,
)
from app.providers.base import PCM_SAMPLE_RATE_HZ
from johnny.voice_pipeline.approval import (
    ApprovalGate,
    ApprovalRequest,
    NoopApprovalGate,
)
from johnny.voice_pipeline.decision_sink import (
    DecisionOutcome,
    DecisionSink,
    NoopDecisionSink,
)
from johnny.voice_pipeline.event_bus import EventBus
from johnny.voice_pipeline.events import (
    AgentSpoke,
    AgentSuggested,
    ApprovalPending,
    ApprovalResolved,
    RouterDecisionMade,
    TranscriptFinalized,
)
from johnny.voice_pipeline.transcript_sink import (
    NoopTranscriptSink,
    TranscriptSink,
)
from johnny.voice_pipeline.transport import JohnnyTransport
from johnny.voice_pipeline.utterance_sink import NoopUtteranceSink, UtteranceSink
from johnny.voice_pipeline.vad import DEFAULT_VAD_THRESHOLD, VADAnalyzer

logger = logging.getLogger(__name__)

DEFAULT_MAX_UTTERANCE_MS = 30_000
DEFAULT_END_OF_SPEECH_MS = 600
DEFAULT_FRAME_DURATION_MS = 20
DEFAULT_CONFIDENCE_THRESHOLD = 0.7
DEFAULT_TRANSCRIPT_WINDOW_SIZE = 6
DEFAULT_MODE = "limited_auto_speak"
DEFAULT_RATE_LIMIT_MAX_UTTERANCES = 3
DEFAULT_RATE_LIMIT_WINDOW_MS = 5 * 60 * 1000
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 15.0
"""Auto-reject window for ``approval_required`` mode (US-027).

Configurable per session via :class:`PipelineConfig.approval_timeout_seconds`.
"""

APPROVAL_REQUIRED_MODE = "approval_required"
LISTEN_ONLY_MODE = "listen_only"
SUGGEST_ONLY_MODE = "suggest_only"
LIMITED_AUTO_SPEAK_MODE = "limited_auto_speak"
# Free-speech: chat without an allowed_replies allowlist and without
# the approval round. The router still gates whether the bot speaks
# (via confidence_threshold), so ambient chatter doesn't trigger
# replies, but anything the model wants to say goes through.
FREE_AUTO_SPEAK_MODE = "free_auto_speak"

NON_SPEAKING_MODES: frozenset[str] = frozenset(
    {LISTEN_ONLY_MODE, SUGGEST_ONLY_MODE}
)
"""Modes in which the bot must NOT generate audio.

Enforced server-side in :meth:`VoicePipeline._process_utterance`: even
when ``speak=True`` and the router approves, no answer LLM call and no
TTS frames are produced. Listen-only also skips the router entirely;
suggest-only runs the router so the UI can show the suggested reply,
but the answer stage is replaced by an :class:`AgentSuggested` event.
"""

_SENTENCE_BOUNDARY = re.compile(r"(?:[.!?]+[\"')\]]*\s+)|(?:\n+)")
"""Matches sentence-ending punctuation followed by whitespace, or one+ newlines.

Used to flush complete sentences from the streaming LLM into the TTS as
soon as they arrive so time-to-first-audio is bounded by the first
sentence rather than the full response.
"""


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Per-session configuration for the pipeline.

    Threshold and timing knobs are surfaced separately from the providers
    so a single set of provider instances can serve many meetings with
    different behaviours.

    ``mode`` is the four-state ``BotMode`` value (string) — both
    included in the router prompt so the model can adjust its decision
    AND enforced server-side: ``listen_only`` skips the router stage
    entirely, ``suggest_only`` runs the router but replaces the answer
    stage with an :class:`AgentSuggested` event, ``approval_required``
    drives the approval round before speaking, ``limited_auto_speak``
    answers freely (subject to ``allowed_replies``). The legacy
    ``speak=False`` flag is retained for tests and stays equivalent to
    listen-only for the router-skip semantics.
    """

    instructions: str = ""
    context: str = ""
    allowed_replies: tuple[str, ...] = ()
    speak: bool = True
    mode: str = DEFAULT_MODE
    vad_threshold: float = DEFAULT_VAD_THRESHOLD
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    end_of_speech_ms: int = DEFAULT_END_OF_SPEECH_MS
    max_utterance_ms: int = DEFAULT_MAX_UTTERANCE_MS
    frame_duration_ms: int = DEFAULT_FRAME_DURATION_MS
    transcript_window_size: int = DEFAULT_TRANSCRIPT_WINDOW_SIZE
    rate_limit_max_utterances: int = DEFAULT_RATE_LIMIT_MAX_UTTERANCES
    rate_limit_window_ms: int = DEFAULT_RATE_LIMIT_WINDOW_MS
    approval_timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS
    session_id: str | None = None
    bot_session_id: int | None = None


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
        decision_sink: DecisionSink | None = None,
        utterance_sink: UtteranceSink | None = None,
        transcript_sink: TranscriptSink | None = None,
        approval_gate: ApprovalGate | None = None,
    ) -> None:
        self.transport = transport
        self.vad = vad
        self.stt = stt
        self.router_llm = router_llm
        self.answer_llm = answer_llm
        self.tts = tts
        self.event_bus = event_bus
        self.config = config or PipelineConfig()
        self.decision_sink = decision_sink or NoopDecisionSink()
        self.utterance_sink = utterance_sink or NoopUtteranceSink()
        self.transcript_sink = transcript_sink or NoopTranscriptSink()
        self.approval_gate = approval_gate or NoopApprovalGate()
        self._session_started_at: float = 0.0
        self._utterance_count = 0
        self._transcript_history: list[TranscriptFinalized] = []
        self._last_decision: RouterDecisionMade | None = None
        self._interrupt_event = asyncio.Event()
        self._recent_utterance_times: list[int] = []

    # ------------------------------------------------------------------
    # Public lifecycle

    async def run(self) -> None:
        """Run the pipeline until the transport's capture stream ends."""
        loop = asyncio.get_running_loop()
        self._session_started_at = loop.time()
        async for utterance in self._utterances():
            await self._process_utterance(utterance)

    def interrupt(self) -> None:
        """Stop the current answer stage's LLM stream and TTS playback.

        Sets an :class:`asyncio.Event` checked between LLM deltas and TTS
        frames; the answer loop exits within one frame's worth of time
        (~20 ms typical) plus the time to ``aclose`` the streaming
        generators. Well under the 500 ms budget mandated by US-024's
        acceptance criteria.

        After interrupt fires, the pipeline keeps processing the next
        utterance — interrupt aborts *one* answer, not the whole session.
        """
        self._interrupt_event.set()

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
        self._remember_transcript(transcript)
        await self._persist_transcript(transcript, utterance)

        # Mode-based server-side enforcement (US-026). Listen-only never
        # runs the router so no router_decision / agent_spoke /
        # agent_suggested events can be emitted; suggest-only runs the
        # router for UI suggestions but the answer stage is replaced by
        # an :class:`AgentSuggested` event below.
        if self.config.mode == LISTEN_ONLY_MODE:
            return
        if not self.config.speak:
            return

        input_window = self._build_input_window(transcript)
        decision, raw_response = await self._run_router(transcript, input_window)
        decision_event = RouterDecisionMade(
            should_speak=decision.should_speak,
            confidence=decision.confidence,
            reason=decision.reason,
            reply_type=decision.reply_type,
            suggested_reply=decision.suggested_reply,
            timestamp_ms=self._now_ms(),
            session_id=self.config.session_id,
            input_window=input_window,
            raw_output=_serialize_raw_output(raw_response, decision),
        )
        await self.event_bus.publish(decision_event)
        self._last_decision = decision_event

        if not decision.should_speak:
            await self._persist_decision(decision_event, "suppressed")
            return
        if decision.confidence < self.config.confidence_threshold:
            await self._persist_decision(decision_event, "suppressed")
            return

        if self.config.mode == SUGGEST_ONLY_MODE:
            await self._handle_suggest_only(decision, decision_event)
            return

        if self._is_rate_limited():
            logger.info(
                "limited-auto-speak rate limit hit for session=%s "
                "(max=%d in window=%dms; recent=%d) — suppressing utterance",
                self.config.session_id,
                self.config.rate_limit_max_utterances,
                self.config.rate_limit_window_ms,
                len(self._recent_utterance_times),
            )
            await self._persist_decision(decision_event, "suppressed")
            return

        if self.config.mode == APPROVAL_REQUIRED_MODE:
            await self._handle_approval_required(transcript, decision, decision_event)
            return

        spoke = await self._answer_and_speak(transcript, decision)
        if spoke:
            self._recent_utterance_times.append(self._now_ms())
        await self._persist_decision(
            decision_event,
            "spoken" if spoke else "suppressed",
        )

    async def _handle_suggest_only(
        self,
        decision: RouterDecision,
        decision_event: RouterDecisionMade,
    ) -> None:
        """Persist a ``suggested`` decision and emit :class:`AgentSuggested`.

        Suggest-only mode never invokes the answer LLM and never produces
        TTS frames. The decision row's permanent outcome is ``suggested``
        (distinct from ``suppressed`` so audit queries can separate
        "router approved but mode prevented speaking" from "router said
        no"). The UI consumes :class:`AgentSuggested` to surface the
        suggestion as an in-app notification — no service worker push,
        because suggest-only does not require user action.
        """
        decision_id = await self._persist_decision(decision_event, "suggested")
        await self.event_bus.publish(
            AgentSuggested(
                decision_id=decision_id,
                suggested_reply=(decision.suggested_reply or "").strip(),
                reason=decision.reason,
                reply_type=decision.reply_type,
                timestamp_ms=self._now_ms(),
                session_id=self.config.session_id,
            )
        )

    async def _handle_approval_required(
        self,
        transcript: TranscriptFinalized,
        decision: RouterDecision,
        decision_event: RouterDecisionMade,
    ) -> None:
        """Drive the approval round before letting the answer LLM run.

        Order of operations:

        1. Insert the decision row with ``outcome='pending'`` so the API
           has a stable ``decision_id`` to expose to the UI.
        2. Emit :class:`ApprovalPending` with that id + the suggested
           reply for the live UI and the browser push notification.
        3. Await :meth:`ApprovalGate.request_approval` for up to
           ``approval_timeout_seconds`` seconds.
        4. On ``approved``: run the answer LLM + TTS; on success flip
           the decision row to ``spoken``, on failure flip to ``rejected``.
        5. On ``rejected`` / ``timeout``: flip the decision row to
           ``rejected`` and log a one-line audit hint.
        6. Always publish :class:`ApprovalResolved` so subscribers can
           clear their UI / dismiss the notification.
        """
        suggested = (decision.suggested_reply or "").strip()
        decision_id = await self._persist_decision(decision_event, "pending")
        if decision_id is None:
            logger.warning(
                "approval_required: decision sink returned no id for session=%s — "
                "skipping approval round; treating as rejected",
                self.config.session_id,
            )
            await self.event_bus.publish(
                ApprovalResolved(
                    decision_id=0,
                    resolution="rejected",
                    timestamp_ms=self._now_ms(),
                    session_id=self.config.session_id,
                )
            )
            return

        timeout_s = max(0.1, float(self.config.approval_timeout_seconds))
        pending_event = ApprovalPending(
            decision_id=decision_id,
            suggested_reply=suggested,
            timestamp_ms=self._now_ms(),
            timeout_s=timeout_s,
            reason=decision.reason,
            reply_type=decision.reply_type,
            session_id=self.config.session_id,
        )
        await self.event_bus.publish(pending_event)

        outcome = await self.approval_gate.request_approval(
            ApprovalRequest(
                decision_id=decision_id,
                suggested_reply=suggested,
                timeout_s=timeout_s,
                session_id=self.config.session_id,
            )
        )

        resolution = outcome
        if outcome == "approved":
            spoke = await self._answer_and_speak(transcript, decision)
            if spoke:
                self._recent_utterance_times.append(self._now_ms())
                await self.decision_sink.update_outcome(decision_id, "spoken")
            else:
                await self.decision_sink.update_outcome(decision_id, "rejected")
                resolution = "rejected"
        else:
            if outcome == "timeout":
                logger.info(
                    "approval_required: decision_id=%d timed out after %.1fs — auto-rejected",
                    decision_id,
                    timeout_s,
                )
            await self.decision_sink.update_outcome(decision_id, "rejected")

        await self.event_bus.publish(
            ApprovalResolved(
                decision_id=decision_id,
                resolution=resolution,
                timestamp_ms=self._now_ms(),
                session_id=self.config.session_id,
            )
        )

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

    async def _run_router(
        self,
        transcript: TranscriptFinalized,
        input_window: dict[str, Any],
    ) -> tuple[RouterDecision, LLMResponse]:
        messages = self._router_messages(transcript, input_window)
        response = await self.router_llm.chat(
            messages,
            response_format=_ROUTER_SCHEMA,
        )
        return _parse_router_response(response), response

    async def _answer_and_speak(
        self,
        transcript: TranscriptFinalized,
        decision: RouterDecision,
    ) -> bool:
        """Generate the answer, stream into TTS, play, persist utterance.

        Returns ``True`` only when audio frames were actually streamed to
        the transport. Returns ``False`` when:

        * the answer LLM produced empty text;
        * ``allowed_replies`` is set and no candidate matched;
        * the user interrupted before audio started playing.
        """
        self._interrupt_event.clear()
        messages = self._answer_messages(transcript, decision)
        prompt_text = _serialize_prompt(messages)

        collected: list[bytes]
        # Free-speech mode ignores allowed_replies — the bot is meant
        # to chat naturally so we always stream the LLM's free-text
        # response straight into TTS, regardless of any allowlist that
        # might still be configured on the meeting.
        use_allowlist = (
            bool(self.config.allowed_replies)
            and self.config.mode != FREE_AUTO_SPEAK_MODE
        )
        if use_allowlist:
            picked = await self._select_allowed_reply(messages)
            if picked is None or self._interrupt_event.is_set():
                return False
            text = picked
            collected = await self._play_text_streamed(text)
        else:
            text, collected = await self._stream_answer_into_tts(messages)
            if not text or not collected:
                return False

        if not collected:
            return False

        audio_bytes = b"".join(collected)
        audio_ms = _pcm_duration_ms(len(audio_bytes), PCM_SAMPLE_RATE_HZ)
        matched_allowed_reply = text if use_allowlist else None

        spoke_event = AgentSpoke(
            text=text,
            audio_duration_ms=audio_ms,
            timestamp_ms=self._now_ms(),
            matched_allowed_reply=matched_allowed_reply,
            session_id=self.config.session_id,
        )
        await self.event_bus.publish(spoke_event)
        await self._persist_utterance(
            prompt=prompt_text,
            output_text=text,
            audio_duration_ms=audio_ms,
            matched_allowed_reply=matched_allowed_reply,
        )
        return True

    async def _select_allowed_reply(
        self,
        messages: list[ChatMessage],
    ) -> str | None:
        """In Limited-auto-speak mode, force the LLM to pick from the list.

        Uses a JSON-schema ``response_format`` with an ``enum`` constraint
        on the allowed-reply set. Adapters that honour structured output
        will return the choice on ``LLMResponse.structured_output``;
        adapters that don't fall back to free text, which is then matched
        verbatim against the allowed list (case-insensitive). If no match
        is found, the utterance is suppressed — the bot stays silent
        rather than say something risky.
        """
        schema = {
            "type": "object",
            "properties": {
                "selected_reply": {
                    "type": "string",
                    "enum": list(self.config.allowed_replies),
                },
            },
            "required": ["selected_reply"],
        }
        response = await self.answer_llm.chat(messages, response_format=schema)
        candidate: str | None = None
        if isinstance(response.structured_output, dict):
            picked = response.structured_output.get("selected_reply")
            if isinstance(picked, str):
                candidate = picked
        if candidate is None and response.text:
            candidate = response.text.strip()
        if candidate is None:
            return None
        return _match_allowed_reply(candidate, self.config.allowed_replies)

    async def _stream_answer_into_tts(
        self,
        messages: list[ChatMessage],
    ) -> tuple[str, list[bytes]]:
        """Stream LLM tokens into TTS, flushing per-sentence to the transport.

        The LLM's streaming output is buffered until a sentence boundary
        is reached; each complete sentence is then handed off to TTS,
        whose frames are pushed into the transport as they arrive. This
        minimises time-to-first-audio because TTS starts as soon as the
        first sentence is ready rather than waiting for the whole answer.

        Returns ``(full_text, all_played_frames)``. Either side can be
        empty if the LLM produced no text or if the user interrupted
        before any audio was played.
        """
        full_text_parts: list[str] = []
        all_frames: list[bytes] = []
        sentence_buffer = ""

        agen = cast(
            AsyncGenerator[str, None],
            self.answer_llm.stream_chat(messages),
        )
        try:
            async for delta in agen:
                if self._interrupt_event.is_set():
                    break
                if not delta:
                    continue
                sentence_buffer += delta
                full_text_parts.append(delta)
                while True:
                    match = _SENTENCE_BOUNDARY.search(sentence_buffer)
                    if match is None:
                        break
                    sentence = sentence_buffer[: match.end()].strip()
                    sentence_buffer = sentence_buffer[match.end() :]
                    if sentence:
                        await self._play_text_streamed(
                            sentence, collected=all_frames
                        )
                        if self._interrupt_event.is_set():
                            break
                if self._interrupt_event.is_set():
                    break
            # Final flush — any trailing text without a sentence boundary
            tail = sentence_buffer.strip()
            if tail and not self._interrupt_event.is_set():
                await self._play_text_streamed(tail, collected=all_frames)
        finally:
            with suppress(Exception):
                await agen.aclose()

        return "".join(full_text_parts).strip(), all_frames

    async def _play_text_streamed(
        self,
        text: str,
        collected: list[bytes] | None = None,
    ) -> list[bytes]:
        """Synthesise ``text`` and stream frames into the transport.

        ``collected`` is appended to in-place when provided so the caller
        can accumulate frames across multiple sentences. Returns the same
        list (or a fresh one) so callers that don't pre-allocate can
        still measure total audio.
        """
        target = collected if collected is not None else []
        await self.transport.play_frames(
            self._tts_frame_iter(text, target),
            source_rate=PCM_SAMPLE_RATE_HZ,
        )
        return target

    async def _tts_frame_iter(
        self,
        text: str,
        collected: list[bytes],
    ) -> AsyncIterator[bytes]:
        """Stream TTS frames for ``text``, recording each to ``collected``.

        The interrupt event is checked before each yield so a user
        interrupt aborts within at most one frame's worth of audio (~20 ms
        typical). ``aclose`` is propagated to the upstream TTS generator
        in the ``finally`` block so the adapter can tear down its
        subprocess / HTTP connection cleanly.
        """
        tts_gen = cast(
            AsyncGenerator[bytes, None],
            self.tts.synthesize_stream(text),
        )
        try:
            async for frame in tts_gen:
                if self._interrupt_event.is_set():
                    break
                if not frame:
                    continue
                collected.append(frame)
                yield frame
        finally:
            with suppress(Exception):
                await tts_gen.aclose()

    # ------------------------------------------------------------------
    # Prompt construction

    def _router_messages(
        self,
        transcript: TranscriptFinalized,
        input_window: dict[str, Any],
    ) -> list[ChatMessage]:
        system = (
            "You are the gating router for an AI meeting bot. Decide whether "
            "the bot should speak in response to the latest transcript. "
            "Reply as JSON matching the supplied schema."
        )
        system += f"\n\nMode: {self.config.mode}"
        system += (
            f"\nConfidence threshold for speaking: {self.config.confidence_threshold:.2f}"
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

        user_parts: list[str] = []
        window: list[dict[str, Any]] = input_window.get("transcript_window", [])
        if len(window) > 1:
            history = window[:-1]
            user_parts.append("Recent conversation:")
            for entry in history:
                speaker = entry.get("speaker") or "speaker"
                user_parts.append(f"- {speaker}: {entry.get('text', '')}")
            user_parts.append("")
        last_decision = input_window.get("last_decision")
        if last_decision is not None:
            user_parts.append(
                "Last router decision (do not repeat without new reason): "
                f"{json.dumps(last_decision, separators=(',', ':'))}"
            )
            user_parts.append("")
        user_parts.append(f"Latest transcript: {transcript.text}")
        return [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content="\n".join(user_parts)),
        ]

    def _answer_messages(
        self,
        transcript: TranscriptFinalized,
        decision: RouterDecision,
    ) -> list[ChatMessage]:
        """Build the prompt for the answer LLM.

        The system message carries the static settings (instructions,
        context, router hint, allowed-reply constraint when set). The
        user message embeds the rolling transcript window so the model
        can reference recent turns, with the latest transcript marked
        explicitly so the model knows what it is responding to.
        """
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

        user_parts: list[str] = []
        if len(self._transcript_history) > 1:
            history = self._transcript_history[:-1]
            user_parts.append("Recent conversation:")
            for entry in history:
                speaker = entry.speaker or "speaker"
                user_parts.append(f"- {speaker}: {entry.text}")
            user_parts.append("")
        user_parts.append(f"Latest transcript: {transcript.text}")

        return [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content="\n".join(user_parts)),
        ]

    # ------------------------------------------------------------------
    # Helpers

    def _now_ms(self) -> int:
        loop = asyncio.get_running_loop()
        return int((loop.time() - self._session_started_at) * 1000)

    def _is_rate_limited(self) -> bool:
        """Return True when the limited-auto-speak rate limit is exceeded.

        Only enforced when ``allowed_replies`` is set — that's the
        operational marker for Limited auto-speak mode. Setting either
        ``rate_limit_max_utterances`` or ``rate_limit_window_ms`` to a
        non-positive value disables the limit. The recent-utterance list
        is pruned in place each time this is called.
        """
        if not self.config.allowed_replies:
            return False
        if (
            self.config.rate_limit_max_utterances <= 0
            or self.config.rate_limit_window_ms <= 0
        ):
            return False
        window_start = self._now_ms() - self.config.rate_limit_window_ms
        self._recent_utterance_times = [
            t for t in self._recent_utterance_times if t > window_start
        ]
        return (
            len(self._recent_utterance_times)
            >= self.config.rate_limit_max_utterances
        )

    def _remember_transcript(self, transcript: TranscriptFinalized) -> None:
        """Append ``transcript`` to the rolling history and bound its size."""
        self._transcript_history.append(transcript)
        window_size = max(1, self.config.transcript_window_size)
        if len(self._transcript_history) > window_size:
            del self._transcript_history[: len(self._transcript_history) - window_size]

    def _build_input_window(
        self, transcript: TranscriptFinalized
    ) -> dict[str, Any]:
        """Snapshot every input the router sees, for prompt & persistence."""
        return {
            "transcript_window": [
                {
                    "text": t.text,
                    "speaker": t.speaker,
                    "timestamp_ms": t.timestamp_ms,
                    "confidence": t.confidence,
                    "is_current": t is transcript,
                }
                for t in self._transcript_history
            ],
            "instructions": self.config.instructions,
            "context": self.config.context,
            "allowed_replies": list(self.config.allowed_replies),
            "mode": self.config.mode,
            "confidence_threshold": self.config.confidence_threshold,
            "last_decision": self._last_decision_summary(),
        }

    def _last_decision_summary(self) -> dict[str, Any] | None:
        if self._last_decision is None:
            return None
        return {
            "should_speak": self._last_decision.should_speak,
            "confidence": self._last_decision.confidence,
            "reason": self._last_decision.reason,
            "reply_type": self._last_decision.reply_type,
            "suggested_reply": self._last_decision.suggested_reply,
            "timestamp_ms": self._last_decision.timestamp_ms,
        }

    async def _persist_transcript(
        self,
        transcript: TranscriptFinalized,
        utterance: bytes,
    ) -> None:
        """Persist the finalised transcript to the configured sink.

        ``transcript.timestamp_ms`` is the pipeline-time offset at which the
        STT stage emitted the finalised chunk (treated as ``end_offset_ms``).
        ``start_offset_ms`` is derived by subtracting the utterance's audio
        duration (PCM length / sample rate) — accurate to within the VAD's
        end-of-speech trim, which is fine for transcript audit views.
        """
        utterance_duration_ms = _pcm_duration_ms(len(utterance), PCM_SAMPLE_RATE_HZ)
        end_offset_ms = transcript.timestamp_ms
        start_offset_ms = max(0, end_offset_ms - utterance_duration_ms)
        try:
            await self.transcript_sink.record(
                text=transcript.text,
                start_offset_ms=start_offset_ms,
                end_offset_ms=end_offset_ms,
                speaker=transcript.speaker,
                confidence=transcript.confidence,
                session_id=self.config.session_id,
                bot_session_id=self.config.bot_session_id,
            )
        except Exception:
            logger.exception(
                "transcript sink failed for session=%s",
                self.config.session_id,
            )

    async def _persist_decision(
        self,
        event: RouterDecisionMade,
        outcome: DecisionOutcome,
    ) -> int | None:
        try:
            return await self.decision_sink.record(
                event,
                outcome=outcome,
                bot_session_id=self.config.bot_session_id,
            )
        except Exception:
            logger.exception(
                "decision sink failed for session=%s outcome=%s",
                self.config.session_id,
                outcome,
            )
            return None

    async def _persist_utterance(
        self,
        *,
        prompt: str,
        output_text: str,
        audio_duration_ms: int,
        matched_allowed_reply: str | None,
    ) -> None:
        try:
            await self.utterance_sink.record(
                mode=self.config.mode,
                prompt=prompt,
                output_text=output_text,
                audio_duration_ms=audio_duration_ms,
                matched_allowed_reply=matched_allowed_reply,
                session_id=self.config.session_id,
                bot_session_id=self.config.bot_session_id,
            )
        except Exception:
            logger.exception(
                "utterance sink failed for session=%s",
                self.config.session_id,
            )


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


def _serialize_raw_output(
    response: LLMResponse,
    decision: RouterDecision,
) -> dict[str, Any]:
    """Flatten an :class:`LLMResponse` for storage in ``agent_decisions.raw_output``.

    Both the model's free-text output and the parsed structured payload are
    captured so a post-hoc review can see exactly what the router said.
    """
    return {
        "text": response.text,
        "finish_reason": response.finish_reason,
        "structured": decision.raw,
    }


def _serialize_prompt(messages: Sequence[ChatMessage]) -> str:
    """Serialise the answer-LLM prompt for storage in ``agent_utterances.prompt``.

    JSON-encodes the role+content of every message so the post-hoc audit
    view can render the exact prompt that produced the spoken utterance.
    Tool calls / tool_call_ids are omitted because the answer stage uses
    plain chat messages; if those become relevant later, extend the dict.
    """
    return json.dumps(
        [
            {"role": m.role, "content": m.content or ""}
            for m in messages
        ],
        separators=(",", ":"),
    )


__all__ = [
    "APPROVAL_REQUIRED_MODE",
    "FREE_AUTO_SPEAK_MODE",
    "DEFAULT_APPROVAL_TIMEOUT_SECONDS",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_END_OF_SPEECH_MS",
    "DEFAULT_FRAME_DURATION_MS",
    "DEFAULT_MAX_UTTERANCE_MS",
    "DEFAULT_MODE",
    "DEFAULT_RATE_LIMIT_MAX_UTTERANCES",
    "DEFAULT_RATE_LIMIT_WINDOW_MS",
    "DEFAULT_TRANSCRIPT_WINDOW_SIZE",
    "LIMITED_AUTO_SPEAK_MODE",
    "LISTEN_ONLY_MODE",
    "NON_SPEAKING_MODES",
    "PipelineConfig",
    "RouterDecision",
    "SUGGEST_ONLY_MODE",
    "VoicePipeline",
]
