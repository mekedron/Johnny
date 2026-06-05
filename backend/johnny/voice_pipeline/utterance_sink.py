"""Utterance persistence sinks for the answer stage.

Mirror of :mod:`johnny.voice_pipeline.decision_sink` for the answer-stage
output. The pipeline calls :meth:`UtteranceSink.record` once per spoken
utterance, capturing the prompt sent to the answer LLM, the final text,
the audio duration, and the active bot mode. Production wires the
SQLAlchemy-backed sink (``app.services.agent_utterances``); tests use
:class:`InMemoryUtteranceSink`.

Like :class:`DecisionSink`, the ABC lives in the SQLAlchemy-free
``johnny`` package so the meet-worker image can import it without
pulling in the ORM stack.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UtteranceRecord:
    """One persisted utterance and the context that produced it."""

    mode: str
    prompt: str
    output_text: str
    audio_duration_ms: int
    matched_allowed_reply: str | None = None
    session_id: str | None = None
    bot_session_id: int | None = None


class UtteranceSink(ABC):
    """Persists agent utterances after they are spoken."""

    @abstractmethod
    async def record(
        self,
        *,
        mode: str,
        prompt: str,
        output_text: str,
        audio_duration_ms: int,
        matched_allowed_reply: str | None = None,
        session_id: str | None = None,
        bot_session_id: int | None = None,
    ) -> None:
        """Durably persist the utterance."""

    async def close(self) -> None:  # noqa: B027 — intentional default no-op
        """Release any held connections. Default is a no-op."""


class InMemoryUtteranceSink(UtteranceSink):
    """Append utterances to a list. Intended for tests and dry runs."""

    def __init__(self) -> None:
        self._records: list[UtteranceRecord] = []
        self._lock = asyncio.Lock()

    async def record(
        self,
        *,
        mode: str,
        prompt: str,
        output_text: str,
        audio_duration_ms: int,
        matched_allowed_reply: str | None = None,
        session_id: str | None = None,
        bot_session_id: int | None = None,
    ) -> None:
        async with self._lock:
            self._records.append(
                UtteranceRecord(
                    mode=mode,
                    prompt=prompt,
                    output_text=output_text,
                    audio_duration_ms=audio_duration_ms,
                    matched_allowed_reply=matched_allowed_reply,
                    session_id=session_id,
                    bot_session_id=bot_session_id,
                )
            )

    def snapshot(self) -> list[UtteranceRecord]:
        """Non-async snapshot for synchronous test assertions."""
        return list(self._records)

    def clear(self) -> None:
        self._records.clear()


class NoopUtteranceSink(UtteranceSink):
    """Default sink that drops utterances. Used when no persistence is wired."""

    async def record(
        self,
        *,
        mode: str,
        prompt: str,
        output_text: str,
        audio_duration_ms: int,
        matched_allowed_reply: str | None = None,
        session_id: str | None = None,
        bot_session_id: int | None = None,
    ) -> None:
        del (
            mode,
            prompt,
            output_text,
            audio_duration_ms,
            matched_allowed_reply,
            session_id,
            bot_session_id,
        )


__all__ = [
    "InMemoryUtteranceSink",
    "NoopUtteranceSink",
    "UtteranceRecord",
    "UtteranceSink",
]
