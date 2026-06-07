"""Speech-to-speech (S2S) provider ABC and value objects (Johnny-ckz.17).

A unified S2S provider — OpenAI GPT-Realtime, Gemini Live, or similar —
collapses STT + LLM + TTS into a single bidirectional session. Audio in /
audio out, transcript metadata streamed alongside. Interrupt is a single
session-level operation rather than three separate stage interrupts.

This module declares the abstract contract every concrete S2S adapter
must implement. The voice pipeline orchestrator (see
``johnny.voice_pipeline.unified_pipeline``) depends only on these
abstractions and the global :class:`ProviderRegistry` from
:mod:`app.providers.base` — concrete adapters (OpenAI Realtime, Gemini
Live) live in their own modules and register themselves at import time
under ``(ProviderKind.S2S, <provider_name>)``.

Audio frames carried by :class:`S2SAudioFrame` are 16 kHz mono signed-
16-bit little-endian PCM, matching the meet-worker audio bridge format
and the existing STT/TTS contracts. Concrete adapters that need a
different wire-side sample rate (OpenAI Realtime requires 24 kHz)
resample at the adapter boundary so the pipeline always speaks 16 kHz.

This module is SQLAlchemy-free so the meet-worker image (which only
ships the ``johnny`` package + a minimal copy of provider ABCs) can
import it without pulling in the ORM stack.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.providers.base import (
    ProviderError,
    ToolDefinition,
    _ProviderBase,
)

S2SRole = Literal["user", "assistant"]
"""Speaker label on a transcript event from an S2S session.

``user`` is what the participant said (the S2S provider's STT output).
``assistant`` is what the model replied (text aligned with the audio
frames it generates). The bot's own audio output is emitted as
:class:`S2SAudioFrame` events; the matching transcript text is emitted
as :class:`S2STranscript` events with ``role='assistant'``.
"""


# --- Errors ---------------------------------------------------------------


class S2SError(ProviderError):
    """Raised when an S2S adapter fails (auth, transport, decode, etc.).

    Mirrors the per-stage error classes on the split pipeline
    (``STTError`` / ``LLMError`` / ``TTSError``) so the pipeline's
    error handling can catch a single class for the whole stack instead
    of three.
    """


# --- Value objects ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class S2SAudioFrame:
    """One PCM audio chunk emitted by the S2S provider's assistant output.

    ``pcm`` is 16 kHz mono signed-16-bit LE bytes. ``timestamp_ms`` is
    the offset since the session began, not wall-clock time.
    """

    pcm: bytes
    timestamp_ms: int = 0


@dataclass(frozen=True, slots=True)
class S2STranscript:
    """A unit of S2S transcript output (user or assistant), partial or final.

    ``role`` distinguishes participant speech (the provider's STT
    output) from the assistant's reply text. ``is_final`` is True for
    a completed turn; partial deltas stream with ``is_final=False``.
    """

    text: str
    is_final: bool
    role: S2SRole
    timestamp_ms: int = 0
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class S2SResponseStarted:
    """Signal that the provider has begun a new assistant response.

    The pipeline uses this to mark the start of bot-speaking state so
    its barge-in logic knows when interrupts apply.
    """

    timestamp_ms: int = 0


@dataclass(frozen=True, slots=True)
class S2SResponseCompleted:
    """Signal that the provider has finished the current assistant response.

    Emitted once the final audio frame for a response has been sent.
    Carries an optional ``finish_reason`` (``"stop"`` / ``"interrupted"``
    / ``"error"``) so the pipeline can distinguish natural completion
    from a barge-in.
    """

    finish_reason: str = "stop"
    timestamp_ms: int = 0


@dataclass(frozen=True, slots=True)
class S2SToolCall:
    """A tool/function invocation requested by the S2S provider.

    Mirrors :class:`app.providers.base.ToolCall` so callers can reuse
    the same handler shape between split-mode LLM tool calls and
    unified-mode S2S tool calls.
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


S2SEvent = (
    S2SAudioFrame
    | S2STranscript
    | S2SResponseStarted
    | S2SResponseCompleted
    | S2SToolCall
)
"""Runtime union of every event the S2S session can emit."""


# --- ABCs ------------------------------------------------------------------


class S2SSession:
    """An open bidirectional session with one S2S provider.

    A session is opened by :meth:`S2SProvider.open_session`; the pipeline
    feeds participant audio through :meth:`send_audio` and consumes
    assistant audio + transcripts through :meth:`events`. Sessions are
    single-use — close them on shutdown via :meth:`close`.

    Concrete adapters subclass this and implement every method.
    """

    @abstractmethod
    async def send_audio(self, pcm: bytes) -> None:
        """Forward one chunk of 16 kHz mono S16LE PCM to the provider.

        Empty / zero-length chunks are silently dropped (no-op).
        Implementations raise :class:`S2SError` on transport / encoding
        failure so the caller can decide whether to retry or fall back.
        """
        raise NotImplementedError

    @abstractmethod
    async def commit_user_turn(self) -> None:
        """Mark the user's current turn as complete.

        Used by adapters that rely on caller-driven VAD (e.g. OpenAI
        Realtime with server VAD disabled). Adapters that handle VAD
        internally may treat this as a hint (or a no-op). After commit,
        the provider is expected to begin assistant response generation
        through :meth:`events`.
        """
        raise NotImplementedError

    @abstractmethod
    def events(self) -> AsyncIterator[S2SEvent]:
        """Stream provider-side events: audio frames, transcripts, signals.

        Returns an async iterator that yields :class:`S2SEvent` values
        for the lifetime of the session. The pipeline consumes this in
        a dedicated reader task and dispatches each event type to the
        right downstream sink (transport.play_frames for audio, event
        bus for transcripts, etc.).
        """
        raise NotImplementedError

    @abstractmethod
    async def interrupt(self) -> None:
        """Stop the current assistant response, if any.

        Maps to OpenAI Realtime's ``response.cancel`` and Gemini Live's
        BidiGenerateContent client-side cancellation. After calling
        ``interrupt``, the session remains open and ready to accept new
        audio for the next user turn. A pending :class:`S2SAudioFrame`
        stream may still emit a few frames already in flight; consumers
        should drain them and treat as no-ops.
        """
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        """Tear down the connection and release any held resources.

        Idempotent: calling close twice must not raise. After close
        every other method is undefined behaviour.
        """
        raise NotImplementedError


class S2SProvider(_ProviderBase):
    """Unified speech-to-speech provider contract (Johnny-ckz.17).

    A concrete adapter (OpenAI GPT-Realtime, Gemini Live, etc.) extends
    :class:`S2SProvider` and implements :meth:`open_session` returning
    an :class:`S2SSession`. Lifecycle: ``open_session()`` opens a fresh
    bidirectional channel; the pipeline drives it for one meeting; the
    pipeline calls ``session.close()`` on teardown. The provider itself
    is long-lived across sessions so adapters can reuse expensive setup
    (HTTP clients, websocket pools).
    """

    @abstractmethod
    async def open_session(
        self,
        *,
        instructions: str = "",
        voice_id: str | None = None,
        tools: Sequence[ToolDefinition] = (),
    ) -> S2SSession:
        """Open a new bidirectional session with the provider.

        ``instructions`` is the system prompt / role description handed
        to the model. ``voice_id`` selects the assistant voice when the
        provider exposes a voice catalog. ``tools`` enables function
        calling — adapters that don't support tools may ignore the arg
        but must not raise.
        """
        raise NotImplementedError


__all__ = [
    "S2SAudioFrame",
    "S2SError",
    "S2SEvent",
    "S2SProvider",
    "S2SResponseCompleted",
    "S2SResponseStarted",
    "S2SRole",
    "S2SSession",
    "S2STranscript",
    "S2SToolCall",
]
