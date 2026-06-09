"""``JohnnyTTS(tts.TTS)`` — LiveKit TTS plugin over Johnny's ``TTSProvider`` (Johnny-7a3).

Lets LiveKit's ``AgentSession`` drive every admin-configured Johnny TTS
provider (Cartesia, ElevenLabs, OpenAI, Kokoro, Piper, KittenTTS) unchanged:
the adapter subclasses :class:`livekit.agents.tts.TTS` and forwards each
``synthesize()`` to the provider's
:meth:`~app.providers.base.TTSProvider.synthesize_stream`, pushing the
16 kHz mono S16LE PCM it yields into a LiveKit
:class:`~livekit.agents.tts.AudioEmitter` (``mime_type="audio/pcm"``), which
reframes it into the ``rtc.AudioFrame``\\ s a
:class:`~livekit.agents.tts.ChunkedStream` emits as
:class:`~livekit.agents.tts.SynthesizedAudio`.

**Voice selection.** ``voice`` (from the admin-active provider config via the
adapter factory, Johnny-zb3) is forwarded as ``synthesize_stream``'s
``voice_id``; ``None`` falls through to the provider's own configured default
(also admin config), so the operator's choice is honored either way.

**Circuit-breaker semantics (Johnny-g2n).** Johnny tags a
:class:`~app.providers.base.TTSError` with a ``category``; the terminal
categories ``quota_exceeded`` / ``auth_failed`` mean the failure will not
recover within the session (the operator must top up credits or rotate the
key). LiveKit's :class:`ChunkedStream` retry loop retries *any* ``APIError``
up to ``conn_options.max_retry`` regardless of the error's ``retryable``
flag, so mapping a terminal failure to an ``APIError`` would hammer a dead
provider. Instead a terminal failure emits the LiveKit ``error`` event
(``recoverable=False``, carrying the categorised Johnny ``TTSError`` so a
session-level breaker can read ``.category``) and re-raises the original
non-``APIError`` to bypass the retry loop and surface immediately. Transient
failures (``rate_limited`` / ``unknown``) map to a retryable LiveKit
``APIError`` so the machinery retries within the turn and emits the
``tts.TTSError`` event.

Requires the ``agent`` extra (``livekit-agents``); imported only where that
extra is installed (the api/agent image), never from the import-safe
top-level :mod:`johnny.agent` package.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from livekit.agents import utils
from livekit.agents._exceptions import APIConnectionError, APIStatusError
from livekit.agents.tts import (
    TTS,
    AudioEmitter,
    ChunkedStream,
    TTSCapabilities,
)
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectOptions,
)

from app.providers.base import (
    PCM_CHANNELS,
    PCM_SAMPLE_RATE_HZ,
    TTSError,
    TTSProvider,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

# TTS failure categories that will not recover within a session — quota
# exhausted or a bad / revoked key. Mirrors
# the legacy split engine (Johnny-g2n):
# the session circuit breaker trips on these, and the adapter must NOT let
# LiveKit retry them (re-calling a dead provider just burns error responses).
_TERMINAL_TTS_CATEGORIES: frozenset[str] = frozenset({"quota_exceeded", "auth_failed"})


class JohnnyTTSStream(ChunkedStream):
    """One ``synthesize()`` call streamed off a Johnny :class:`TTSProvider`.

    :meth:`_run` is invoked by the base :class:`ChunkedStream` machinery
    (inside its retry loop); it initialises the :class:`AudioEmitter` for
    raw 16 kHz mono PCM and pushes every frame
    :meth:`~app.providers.base.TTSProvider.synthesize_stream` yields. The
    provider generator is always ``aclose()``-d in a ``finally`` so a
    barge-in cancellation tears down the provider's HTTP / subprocess
    promptly (mirroring the legacy pipeline's ``_tts_frame_iter``).
    """

    def __init__(
        self,
        tts: JohnnyTTS,
        *,
        provider: TTSProvider,
        input_text: str,
        voice_id: str | None,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._provider = provider
        self._voice_id = voice_id

    async def _run(self, output_emitter: AudioEmitter) -> None:
        output_emitter.initialize(
            request_id=utils.shortuuid("johnny_tts_"),
            sample_rate=self._tts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
            stream=False,
        )
        # synthesize_stream is typed AsyncIterator on the ABC; the concrete
        # adapters are async generators, so cast to call aclose() on teardown.
        gen = cast(
            "AsyncGenerator[bytes, None]",
            self._provider.synthesize_stream(self._input_text, self._voice_id),
        )
        try:
            async for frame in gen:
                if frame:
                    output_emitter.push(frame)
            # The base ChunkedStream calls end_input()/join() after _run
            # returns, which flushes the AudioByteStream's held-back tail and
            # marks the final real frame is_final — no explicit flush needed.
        except TTSError as exc:
            self._raise_tts_error(exc)
        finally:
            with suppress(Exception):
                await gen.aclose()

    def _raise_tts_error(self, exc: TTSError) -> None:
        """Map a Johnny :class:`TTSError` onto LiveKit error semantics."""
        category = getattr(exc, "category", "unknown")
        if category in _TERMINAL_TTS_CATEGORIES:
            # Terminal: surface as a non-recoverable error carrying the Johnny
            # TTSError (so .category survives for a session breaker) and
            # re-raise the original non-APIError to bypass LiveKit's retry loop.
            self._emit_error(exc, recoverable=False)
            raise exc
        if category == "rate_limited":
            raise APIStatusError(str(exc), status_code=429, retryable=True) from exc
        raise APIConnectionError(str(exc), retryable=True) from exc


class JohnnyTTS(TTS[Any]):
    """LiveKit :class:`tts.TTS` backed by a Johnny :class:`TTSProvider`.

    Constructed by the adapter factory (Johnny-zb3) from the admin-active
    TTS provider so ``AgentSession(tts=JohnnyTTS(provider, voice=...))`` runs
    Johnny's own provider stack — registry, schema, and Fernet credential
    handling all untouched. ``voice`` overrides the provider-default voice;
    ``model`` is an optional label surfaced on metrics / traces. The provider
    contract pins output to 16 kHz mono S16LE PCM, so ``sample_rate`` defaults
    to that and rarely needs overriding.

    Only the chunked (non-streaming) ``synthesize()`` path is implemented
    (``capabilities.streaming = False``); the Johnny ``synthesize_stream``
    contract is a one-shot text-in, audio-out call, and ``AgentSession``
    drives TTS per sentence over this path.
    """

    def __init__(
        self,
        provider: TTSProvider,
        *,
        voice: str | None = None,
        model: str | None = None,
        sample_rate: int = PCM_SAMPLE_RATE_HZ,
    ) -> None:
        super().__init__(
            capabilities=TTSCapabilities(streaming=False),
            sample_rate=sample_rate,
            num_channels=PCM_CHANNELS,
        )
        self._provider = provider
        self._voice = voice
        self._model = model

    @property
    def model(self) -> str:
        return self._model or "unknown"

    @property
    def provider(self) -> str:
        return self._provider.name

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> ChunkedStream:
        return JohnnyTTSStream(
            self,
            provider=self._provider,
            input_text=text,
            voice_id=self._voice,
            conn_options=conn_options,
        )


__all__ = ["JohnnyTTS", "JohnnyTTSStream"]
