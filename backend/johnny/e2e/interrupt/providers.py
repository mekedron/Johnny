"""Scripted fake providers for the interrupt harness (Johnny-2bw).

The harness needs providers whose **timing** is close enough to production
that interrupt-budget assertions are meaningful (per-utterance STT latency,
per-frame TTS latency, per-delta LLM streaming) but whose **content** is
deterministic so assertions are reproducible run-to-run.

* :class:`ScriptedSlowSTT` — ordered transcript list with a per-utterance
  sleep (default 30 ms) so the response loop has the natural interleave
  point that production STT provides. Same pattern as the
  ``_SlowFakeSTT`` in :mod:`tests.voice_pipeline.test_pipeline`.
* :class:`SwitchingRouterLLM` — dispatches by the requested
  ``response_format`` so we can drive router decisions and barge-in
  classifier verdicts independently, just like production where the same
  LLM serves both.
* :class:`ScriptedAnswerLLM` — streams a long, multi-sentence reply token
  by token with realistic per-delta sleep so the bot's TTS playback fills
  several real seconds — long enough for the speaker to interrupt mid-
  answer.
* :class:`PacedTTS` — yields ``frame_count`` PCM frames with an
  ``asyncio.sleep`` between frames so the bot's playback takes real wall-
  clock time. Without this the entire TTS dumps in one tick and the
  interrupt event never lands during playback.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

from app.providers import (
    ChatMessage,
    LLMProvider,
    LLMResponse,
    STTProvider,
    ToolDefinition,
    TranscriptEvent,
    TTSProvider,
)


class ScriptedSlowSTT(STTProvider):
    """STT that yields a preset transcript per utterance with a sleep.

    The sleep mimics the round-trip latency of a real streaming STT call
    (Deepgram, OpenAI Realtime, ElevenLabs Scribe all sit around 100–300 ms
    for short utterances). The exact value isn't important — what matters
    is that the response loop *can* schedule between consecutive utterances.

    Transcripts that exceed the configured list collapse to a sentinel so
    a misconfigured scenario fails loudly rather than crashing the loop.
    """

    SENTINEL = "<no-scripted-transcript>"

    def __init__(self, transcripts: Sequence[str], sleep_s: float = 0.03) -> None:
        self._transcripts: list[str] = list(transcripts)
        self._idx = 0
        self._sleep_s = sleep_s
        self.calls = 0

    @property
    def name(self) -> str:
        return "scripted-slow-stt"

    async def transcribe_stream(
        self,
        audio_iter: AsyncIterator[bytes],
    ) -> AsyncIterator[TranscriptEvent]:
        async for _ in audio_iter:
            pass
        await asyncio.sleep(self._sleep_s)
        if self._idx >= len(self._transcripts):
            text = self.SENTINEL
        else:
            text = self._transcripts[self._idx]
        self._idx += 1
        self.calls += 1
        yield TranscriptEvent(
            text=text,
            is_final=True,
            timestamp_ms=self.calls * 1000,
            confidence=0.9,
        )


class SwitchingRouterLLM(LLMProvider):
    """LLM that serves router decisions AND barge-in verdicts.

    Production reuses ``router_llm`` for the barge-in classifier (single
    knob to configure); the discriminator is the ``response_format``
    schema — barge-in is the only schema with a ``should_interrupt`` key.
    """

    _DEFAULT_NO_INTERRUPT_VERDICT = {
        "should_interrupt": False,
        "category": "noise",
        "reason": "default no-interrupt verdict",
    }

    def __init__(
        self,
        router_decisions: Sequence[dict[str, Any]],
        barge_in_decisions: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        self._router_decisions: list[dict[str, Any]] = list(router_decisions)
        self._barge_in_decisions: list[dict[str, Any]] = list(barge_in_decisions or [])
        self._router_idx = 0
        self._barge_in_idx = 0
        self.router_calls: list[Sequence[ChatMessage]] = []
        self.barge_in_calls: list[Sequence[ChatMessage]] = []

    @property
    def name(self) -> str:
        return "switching-router"

    @staticmethod
    def _is_barge_in_format(response_format: dict[str, Any] | None) -> bool:
        if not response_format:
            return False
        props = response_format.get("properties")
        if not isinstance(props, dict):
            return False
        return "should_interrupt" in props

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        if self._is_barge_in_format(response_format):
            self.barge_in_calls.append(list(messages))
            if not self._barge_in_decisions:
                payload = dict(self._DEFAULT_NO_INTERRUPT_VERDICT)
            elif self._barge_in_idx >= len(self._barge_in_decisions):
                payload = self._barge_in_decisions[-1]
            else:
                payload = self._barge_in_decisions[self._barge_in_idx]
                self._barge_in_idx += 1
            return LLMResponse(
                text=json.dumps(payload),
                finish_reason="stop",
                structured_output=payload,
            )

        self.router_calls.append(list(messages))
        if not self._router_decisions:
            decision: dict[str, Any] = {
                "should_speak": False,
                "confidence": 0.0,
                "reason": "no router decision scripted",
            }
        elif self._router_idx >= len(self._router_decisions):
            decision = self._router_decisions[-1]
        else:
            decision = self._router_decisions[self._router_idx]
            self._router_idx += 1
        return LLMResponse(
            text=json.dumps(decision),
            finish_reason="stop",
            structured_output=decision,
        )


class ScriptedAnswerLLM(LLMProvider):
    """LLM that streams a preset multi-sentence answer per call.

    Each delta is a short token-like substring; ``delta_sleep_s`` adds a
    small per-delta sleep so the streaming doesn't all land in a single
    event-loop tick. The pipeline's ``_stream_answer_into_tts`` flushes
    per-sentence — multiple sentences means multiple TTS calls, mirroring
    production cadence.
    """

    DEFAULT_ANSWER = (
        "Let me think about that for a moment. "
        "There are several factors worth considering here. "
        "First, the history of the question. "
        "Second, the practical implications. "
        "Third, what we might do next."
    )

    def __init__(
        self,
        answers: Sequence[str] | None = None,
        *,
        delta_sleep_s: float = 0.001,
    ) -> None:
        self._answers: list[str] = list(answers) if answers else [self.DEFAULT_ANSWER]
        self._idx = 0
        self._delta_sleep_s = delta_sleep_s
        self.calls: list[Sequence[ChatMessage]] = []

    @property
    def name(self) -> str:
        return "scripted-answer"

    def _next_text(self) -> str:
        if self._idx >= len(self._answers):
            return self._answers[-1]
        text = self._answers[self._idx]
        self._idx += 1
        return text

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
        response_format: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> LLMResponse:
        self.calls.append(list(messages))
        return LLMResponse(text=self._next_text(), finish_reason="stop")

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
    ) -> AsyncIterator[str]:
        self.calls.append(list(messages))
        text = self._next_text()
        # Tokenise on whitespace so each delta is a short word; the
        # per-sentence flush in the pipeline picks up sentence boundaries
        # naturally as periods accumulate at the end of words.
        words = text.split(" ")
        for i, word in enumerate(words):
            chunk = word if i == 0 else " " + word
            yield chunk
            if self._delta_sleep_s > 0:
                await asyncio.sleep(self._delta_sleep_s)


class PacedTTS(TTSProvider):
    """TTS that yields ``frame_count`` PCM frames with per-frame sleep.

    Without per-frame pacing the whole TTS payload lands in one event-loop
    tick — the interrupt event would never have a chance to land between
    frames. The default produces ~2 seconds of "audio" (100 frames @ 20 ms
    each), enough to span the interrupt onset in every scenario without
    making the harness wait minutes per run.

    Each frame is silent (all zeros). The harness measures audio cut from
    *frame count* + monotonic timestamps via
    :attr:`PacedScriptedTransport.played`, not from audio content.
    """

    def __init__(
        self,
        *,
        frame_count: int = 100,
        frame_size_bytes: int = 320,
        frame_period_s: float = 0.02,
    ) -> None:
        self._frame_count = frame_count
        self._frame_size = frame_size_bytes
        self._frame_period_s = frame_period_s
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return "paced-tts"

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def frame_period_s(self) -> float:
        return self._frame_period_s

    @property
    def total_duration_s(self) -> float:
        """Total wall-clock time required to stream one full TTS call."""
        return self._frame_count * self._frame_period_s

    async def synthesize_stream(
        self,
        text: str,
        voice_id: str | None = None,  # noqa: ARG002
    ) -> AsyncIterator[bytes]:
        self.calls.append(text)
        for _ in range(self._frame_count):
            yield bytes(self._frame_size)
            if self._frame_period_s > 0:
                await asyncio.sleep(self._frame_period_s)


__all__ = [
    "PacedTTS",
    "ScriptedAnswerLLM",
    "ScriptedSlowSTT",
    "SwitchingRouterLLM",
]
