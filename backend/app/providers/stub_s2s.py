"""Stub S2S provider for tests and bring-up (Johnny-ckz.17).

An echo-style :class:`S2SProvider` that consumes whatever audio the
pipeline forwards, then on ``commit_user_turn`` emits a deterministic
assistant transcript + PCM frame stream. Useful for:

* Wiring the unified pipeline end-to-end before the real OpenAI Realtime
  / Gemini Live adapters land (separate follow-up tickets).
* Integration tests that exercise the dispatch from
  ``pipeline_mode='unified'`` through ``UnifiedVoicePipeline`` and out
  to the transport without needing live network credentials.
* Manual debugging of the router (split vs unified) by registering the
  stub as the active S2S provider.

The stub is registered under ``(ProviderKind.S2S, "stub")`` and never
opens a network connection. Everything is in-memory so tests can drive
it deterministically.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from app.providers.base import (
    ProviderConfig,
    ProviderKind,
    ToolDefinition,
    get_registry,
)
from app.providers.s2s_base import (
    S2SAudioFrame,
    S2SError,
    S2SEvent,
    S2SProvider,
    S2SResponseCompleted,
    S2SResponseStarted,
    S2SSession,
    S2STranscript,
)
from app.providers.schema import (
    FieldDef,
    FieldGroup,
    FieldType,
    ProviderSchema,
    ProviderTip,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "stub"

DEFAULT_RESPONSE_TEXT = "Hello, this is the stub S2S provider."
"""Assistant transcript emitted on every ``commit_user_turn``."""

DEFAULT_RESPONSE_PCM_MS = 200
"""Duration of synthesised silence PCM the stub plays for each response."""

DEFAULT_FRAME_MS = 20
"""Per-frame duration the stub uses when chunking its synthetic audio."""

_PCM_SAMPLE_RATE = 16_000
_PCM_SAMPLE_WIDTH = 2  # signed 16-bit


def _silence_frame(frame_ms: int) -> bytes:
    """Return a single ``frame_ms``-long PCM frame of zero samples."""
    n_samples = _PCM_SAMPLE_RATE * frame_ms // 1000
    return b"\x00\x00" * n_samples


class StubS2SSession(S2SSession):
    """In-memory :class:`S2SSession` that echoes a fixed response.

    The session collects participant audio in a list (so tests can
    assert what was sent), and on ``commit_user_turn`` queues up the
    response stream: a user transcript echoing the audio byte count,
    a ResponseStarted signal, a configurable response transcript +
    PCM frames, and ResponseCompleted. ``events()`` drains the queue
    and exits when the session is closed.
    """

    def __init__(
        self,
        *,
        response_text: str = DEFAULT_RESPONSE_TEXT,
        response_pcm_ms: int = DEFAULT_RESPONSE_PCM_MS,
        frame_ms: int = DEFAULT_FRAME_MS,
        instructions: str = "",
        voice_id: str | None = None,
    ) -> None:
        self._response_text = response_text
        self._response_pcm_ms = max(0, response_pcm_ms)
        self._frame_ms = max(1, frame_ms)
        self._instructions = instructions
        self._voice_id = voice_id
        self._sent_audio: list[bytes] = []
        self._queue: asyncio.Queue[S2SEvent | None] = asyncio.Queue()
        self._closed = False
        self._commit_count = 0
        self._interrupt_count = 0

    @property
    def sent_audio(self) -> list[bytes]:
        """Return the participant audio chunks observed via send_audio.

        Exposed so integration tests can assert the unified pipeline
        actually piped capture frames through to the provider rather
        than dropping them on the floor.
        """
        return list(self._sent_audio)

    @property
    def commit_count(self) -> int:
        return self._commit_count

    @property
    def interrupt_count(self) -> int:
        return self._interrupt_count

    @property
    def instructions(self) -> str:
        return self._instructions

    @property
    def voice_id(self) -> str | None:
        return self._voice_id

    async def send_audio(self, pcm: bytes) -> None:
        if self._closed:
            raise S2SError("send_audio on a closed StubS2SSession")
        if pcm:
            self._sent_audio.append(bytes(pcm))

    async def commit_user_turn(self) -> None:
        if self._closed:
            raise S2SError("commit_user_turn on a closed StubS2SSession")
        self._commit_count += 1
        total_bytes = sum(len(c) for c in self._sent_audio)
        user_transcript = (
            f"[stub-s2s heard {total_bytes} bytes across "
            f"{len(self._sent_audio)} chunks]"
        )
        await self._queue.put(
            S2STranscript(text=user_transcript, is_final=True, role="user")
        )
        await self._queue.put(S2SResponseStarted())
        await self._queue.put(
            S2STranscript(text=self._response_text, is_final=True, role="assistant")
        )
        if self._response_pcm_ms > 0:
            frames = self._response_pcm_ms // self._frame_ms
            silence = _silence_frame(self._frame_ms)
            for _ in range(max(1, frames)):
                await self._queue.put(S2SAudioFrame(pcm=silence))
        await self._queue.put(S2SResponseCompleted(finish_reason="stop"))
        # Reset the audio buffer for the next turn so subsequent commits
        # report fresh byte counts.
        self._sent_audio.clear()

    async def events(self) -> AsyncIterator[S2SEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    async def interrupt(self) -> None:
        if self._closed:
            return
        self._interrupt_count += 1
        await self._queue.put(
            S2SResponseCompleted(finish_reason="interrupted")
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.put(None)


class StubS2S(S2SProvider):
    """Echo-style S2S adapter for testing the dispatch + pipeline plumbing.

    The provider itself holds no per-session state; every
    :meth:`open_session` returns a fresh :class:`StubS2SSession`
    configured with the values passed at construction time (or their
    schema defaults). Tests can override ``response_text`` to assert
    that the unified pipeline propagates the provider's response into
    the event bus and transport.

    Configuration ``options`` (any key may be omitted):

    * ``response_text`` — assistant transcript emitted on commit.
      Default: ``"Hello, this is the stub S2S provider."``.
    * ``response_pcm_ms`` — total duration of silent PCM emitted per
      response, in ms. Default 200; set to 0 to emit no audio.
    * ``frame_ms`` — per-frame size used to chunk the synthetic PCM.
      Default 20.
    """

    def __init__(self, config: ProviderConfig) -> None:
        if config.kind is not ProviderKind.S2S:
            raise ValueError(
                f"StubS2S requires ProviderKind.S2S; got {config.kind.value}"
            )
        opts = config.options
        self._response_text = str(
            opts.get("response_text") or DEFAULT_RESPONSE_TEXT
        )
        try:
            response_pcm_ms = int(
                opts.get("response_pcm_ms", DEFAULT_RESPONSE_PCM_MS)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"response_pcm_ms must be an integer; got {opts.get('response_pcm_ms')!r}"
            ) from exc
        if response_pcm_ms < 0:
            raise ValueError(
                f"response_pcm_ms must be >= 0; got {response_pcm_ms}"
            )
        self._response_pcm_ms = response_pcm_ms
        try:
            frame_ms = int(opts.get("frame_ms", DEFAULT_FRAME_MS))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"frame_ms must be an integer; got {opts.get('frame_ms')!r}"
            ) from exc
        if frame_ms <= 0:
            raise ValueError(f"frame_ms must be positive; got {frame_ms}")
        self._frame_ms = frame_ms

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def response_text(self) -> str:
        return self._response_text

    @property
    def response_pcm_ms(self) -> int:
        return self._response_pcm_ms

    @property
    def frame_ms(self) -> int:
        return self._frame_ms

    @classmethod
    def field_schema(cls) -> ProviderSchema:
        return ProviderSchema(
            kind=ProviderKind.S2S,
            provider_name=PROVIDER_NAME,
            display_name="Stub S2S (echo)",
            summary=(
                "Test-only speech-to-speech provider that echoes a fixed "
                "response. Use for bring-up of unified mode before a "
                "real S2S adapter is configured."
            ),
            fields=(
                FieldDef(
                    name="response_text",
                    label="Echo response text",
                    type=FieldType.TEXT,
                    default=DEFAULT_RESPONSE_TEXT,
                    help_text=(
                        "Assistant transcript the stub emits on every "
                        "user turn. Useful for asserting end-to-end "
                        "propagation in integration tests."
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="response_pcm_ms",
                    label="Echo PCM duration (ms)",
                    type=FieldType.NUMBER,
                    default=DEFAULT_RESPONSE_PCM_MS,
                    help_text=(
                        "Total silent-PCM duration emitted per response. "
                        "Set to 0 to emit no audio (text-only)."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="frame_ms",
                    label="PCM frame size (ms)",
                    type=FieldType.NUMBER,
                    default=DEFAULT_FRAME_MS,
                    help_text=(
                        "How many ms of PCM go in each emitted audio "
                        "frame. Matches the pipeline frame_duration_ms."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
            ),
            tips=(
                ProviderTip(
                    topic="Stub is for tests and bring-up only",
                    body=(
                        "Switch to a real S2S provider (OpenAI Realtime, "
                        "Gemini Live) for production. The stub never "
                        "transcribes user audio — it just echoes a fixed "
                        "response."
                    ),
                ),
            ),
        )

    async def open_session(
        self,
        *,
        instructions: str = "",
        voice_id: str | None = None,
        tools: Sequence[ToolDefinition] = (),
    ) -> S2SSession:
        # ``tools`` ignored — the stub never invokes tools. Adapters that
        # support tools should wire them through here.
        _ = tools
        return StubS2SSession(
            response_text=self._response_text,
            response_pcm_ms=self._response_pcm_ms,
            frame_ms=self._frame_ms,
            instructions=instructions,
            voice_id=voice_id,
        )


def register(*, replace: bool = False) -> None:
    """Register :class:`StubS2S` under ``(ProviderKind.S2S, "stub")``.

    Called from :mod:`app.providers` so the stub is available out of
    the box for tests and manual debugging.
    """
    get_registry().register(
        ProviderKind.S2S, PROVIDER_NAME, StubS2S, replace=replace
    )


__all__ = [
    "DEFAULT_FRAME_MS",
    "DEFAULT_RESPONSE_PCM_MS",
    "DEFAULT_RESPONSE_TEXT",
    "PROVIDER_NAME",
    "StubS2S",
    "StubS2SSession",
    "register",
]
