"""``JohnnySTT(stt.STT)`` — LiveKit STT plugin over Johnny's ``STTProvider`` (Johnny-c81).

Lets LiveKit's ``AgentSession`` drive every admin-configured Johnny STT
provider (Deepgram, ElevenLabs, faster-whisper, Parakeet, OpenAI Realtime)
unchanged: the adapter subclasses :class:`livekit.agents.stt.STT` and exposes
a streaming :class:`~livekit.agents.stt.RecognizeStream` whose ``_run`` feeds
the ``rtc.AudioFrame``\\ s LiveKit pushes into the provider's
:meth:`~app.providers.base.STTProvider.transcribe_stream` (an
``AsyncIterator[bytes]`` of 16 kHz mono S16LE PCM) and re-emits each
:class:`~app.providers.base.TranscriptEvent` it yields as a LiveKit
:class:`~livekit.agents.stt.SpeechEvent`.

Mapping:

* ``rtc.AudioFrame`` (any sample rate; the base
  :class:`RecognizeStream` auto-resamples to 16 kHz because we pass
  ``sample_rate=PCM_SAMPLE_RATE_HZ``) → raw S16LE PCM ``bytes`` for the
  provider's ``audio_iter``;
* :class:`TranscriptEvent` → :class:`SpeechEvent` —
  ``is_final`` selects ``FINAL_TRANSCRIPT`` vs ``INTERIM_TRANSCRIPT``; the
  text / confidence / speaker / timestamp land on a single
  :class:`SpeechData` alternative. Johnny's ``TranscriptEvent`` carries no
  language, so :class:`SpeechData.language` is filled from the configured /
  per-``stream()`` language (empty = unknown). Speech boundaries are left to
  the session VAD — ``START_OF_SPEECH`` / ``END_OF_SPEECH`` are not emitted
  (the SDK treats them as optional for an STT).

Provider :class:`~app.providers.base.STTError`\\ s map to a retryable LiveKit
``APIConnectionError`` so the base ``RecognizeStream`` / ``recognize`` retry
loop handles them (``STTError`` has no terminal/transient ``category``, unlike
``TTSError`` — Johnny-g2n — so there is no circuit-breaker branch here).

Requires the ``agent`` extra (``livekit-agents``); imported only where that
extra is installed (the api/agent image), never from the import-safe
top-level :mod:`johnny.agent` package.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from livekit import rtc
from livekit.agents import utils
from livekit.agents._exceptions import APIConnectionError
from livekit.agents.language import LanguageCode
from livekit.agents.stt import (
    STT,
    RecognizeStream,
    SpeechData,
    SpeechEvent,
    SpeechEventType,
    STTCapabilities,
)
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
    NotGivenOr,
)
from livekit.agents.utils import AudioBuffer, combine_frames
from livekit.agents.utils.misc import is_given

from app.providers.base import (
    PCM_SAMPLE_RATE_HZ,
    STTError,
    STTProvider,
    TranscriptEvent,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator


def _frame_to_pcm_bytes(frame: rtc.AudioFrame) -> bytes:
    """Pull raw S16LE PCM ``bytes`` out of an ``rtc.AudioFrame``.

    ``frame.data`` is a buffer view that is ``bytes`` / ``memoryview`` on
    current SDKs but may be a numpy view on older ones; both shapes are
    handled (mirrors ``voice_pipeline.livekit_transport._extract_frame_bytes``).
    """
    data: Any = frame.data
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    tobytes = getattr(data, "tobytes", None)
    if callable(tobytes):
        converted = tobytes()
        if isinstance(converted, bytes):
            return converted
    return bytes(data)


def transcript_to_speech_event(
    event: TranscriptEvent,
    *,
    language: str,
    request_id: str,
) -> SpeechEvent:
    """Map a Johnny :class:`TranscriptEvent` onto a LiveKit :class:`SpeechEvent`.

    ``is_final`` chooses the event type; ``timestamp_ms`` (offset since the
    stream start) becomes ``start_time`` / ``end_time`` in seconds; a missing
    ``confidence`` defaults to ``0.0`` (the :class:`SpeechData` default).
    """
    speech_type = (
        SpeechEventType.FINAL_TRANSCRIPT
        if event.is_final
        else SpeechEventType.INTERIM_TRANSCRIPT
    )
    offset_s = event.timestamp_ms / 1000.0
    return SpeechEvent(
        type=speech_type,
        request_id=request_id,
        alternatives=[
            SpeechData(
                language=LanguageCode(language),
                text=event.text,
                start_time=offset_s,
                end_time=offset_s,
                confidence=0.0 if event.confidence is None else event.confidence,
                speaker_id=event.speaker,
            )
        ],
    )


class JohnnySTTStream(RecognizeStream):
    """One streaming recognition session over a Johnny :class:`STTProvider`.

    :meth:`_run` is invoked by the base :class:`RecognizeStream` machinery
    (inside its retry loop). It bridges LiveKit's push model (frames arrive on
    ``self._input_ch`` via :meth:`RecognizeStream.push_frame`) to Johnny's pull
    model (``transcribe_stream`` consumes an ``AsyncIterator[bytes]``): the
    :meth:`_audio_frames` generator drains ``self._input_ch``, yields each
    frame's PCM, and ends when the input channel closes (``end_input()``);
    every :class:`TranscriptEvent` the provider yields is forwarded to
    ``self._event_ch`` as a :class:`SpeechEvent`.

    Both async generators are ``aclose()``-d in a ``finally`` so a barge-in
    cancellation tears down the provider's HTTP / WebSocket promptly
    (mirroring the legacy pipeline and the TTS adapter).
    """

    def __init__(
        self,
        stt: JohnnySTT,
        *,
        provider: STTProvider,
        language: str,
        conn_options: APIConnectOptions,
    ) -> None:
        # sample_rate pins the base resampler to 16 kHz: LiveKit room audio is
        # often 48 kHz, but Johnny providers expect the 16 kHz mono bridge
        # format, so push_frame() resamples before _run() ever sees a frame.
        super().__init__(
            stt=stt, conn_options=conn_options, sample_rate=PCM_SAMPLE_RATE_HZ
        )
        self._provider = provider
        self._language = language
        self._request_id = utils.shortuuid("johnny_stt_")

    async def _audio_frames(self) -> AsyncGenerator[bytes, None]:
        """Drain ``self._input_ch`` into the PCM byte stream the provider pulls.

        Flush sentinels mark LiveKit segment boundaries; Johnny's
        ``transcribe_stream`` consumes a single continuous byte stream and has
        no segment-commit concept, so they are skipped. The generator ends
        when the input channel closes.
        """
        async for item in self._input_ch:
            if isinstance(item, self._FlushSentinel):
                continue
            pcm = _frame_to_pcm_bytes(item)
            if pcm:
                yield pcm

    async def _run(self) -> None:
        audio_iter = self._audio_frames()
        # transcribe_stream is typed AsyncIterator on the ABC; concrete
        # adapters are async generators, so cast to aclose() on teardown.
        events = cast(
            "AsyncGenerator[TranscriptEvent, None]",
            self._provider.transcribe_stream(audio_iter),
        )
        try:
            async for event in events:
                self._event_ch.send_nowait(
                    transcript_to_speech_event(
                        event,
                        language=self._language,
                        request_id=self._request_id,
                    )
                )
        except STTError as exc:
            # No category on STTError -> always retryable; the base
            # RecognizeStream retry loop catches APIError up to max_retry.
            raise APIConnectionError(str(exc)) from exc
        finally:
            with suppress(Exception):
                await events.aclose()
            with suppress(Exception):
                await audio_iter.aclose()


class JohnnySTT(STT[Any]):
    """LiveKit :class:`stt.STT` backed by a Johnny :class:`STTProvider`.

    Constructed by the adapter factory (Johnny-zb3) from the admin-active STT
    provider so ``AgentSession(stt=JohnnySTT(provider))`` runs Johnny's own
    provider stack — registry, schema, and Fernet credential handling all
    untouched. ``language`` is the default :class:`SpeechData.language` stamped
    on transcripts (Johnny's ``TranscriptEvent`` carries none); a per-call
    ``stream(language=...)`` / ``recognize(language=...)`` overrides it.
    ``model`` is an optional label surfaced on metrics / traces.

    ``capabilities.streaming`` is ``True`` (the provider streams interim +
    final transcripts), so ``AgentSession`` drives the :meth:`stream` path;
    :meth:`_recognize_impl` provides the batch fallback by running the buffer
    through ``transcribe_stream`` as a single chunk.
    """

    def __init__(
        self,
        provider: STTProvider,
        *,
        language: str | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(
            capabilities=STTCapabilities(streaming=True, interim_results=True)
        )
        self._provider = provider
        self._language = language
        self._model = model

    @property
    def model(self) -> str:
        return self._model or "unknown"

    @property
    def provider(self) -> str:
        return self._provider.name

    def _resolve_language(self, override: NotGivenOr[str]) -> str:
        if is_given(override) and override:
            return override
        return self._language or ""

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> SpeechEvent:
        """Batch recognition: run the whole buffer through ``transcribe_stream``.

        Streaming STT uses :meth:`stream`; this path exists because the SDK
        marks recognition abstract (and the fallback adapter may call it). The
        buffer is handed to the provider as one chunk and the last final (or,
        failing that, the last interim) transcript is returned as a single
        ``FINAL_TRANSCRIPT``.
        """
        pcm = _frame_to_pcm_bytes(combine_frames(buffer))
        resolved = self._resolve_language(language)
        request_id = utils.shortuuid("johnny_stt_")

        async def _one_chunk() -> AsyncIterator[bytes]:
            if pcm:
                yield pcm

        events = cast(
            "AsyncGenerator[TranscriptEvent, None]",
            self._provider.transcribe_stream(_one_chunk()),
        )
        final_alt: SpeechData | None = None
        last_alt: SpeechData | None = None
        try:
            async for event in events:
                last_alt = transcript_to_speech_event(
                    event, language=resolved, request_id=request_id
                ).alternatives[0]
                if event.is_final:
                    final_alt = last_alt
        except STTError as exc:
            raise APIConnectionError(str(exc)) from exc
        finally:
            with suppress(Exception):
                await events.aclose()
        # Prefer the last final; fall back to the last interim if none arrived.
        best = final_alt if final_alt is not None else last_alt
        return SpeechEvent(
            type=SpeechEventType.FINAL_TRANSCRIPT,
            request_id=request_id,
            alternatives=[best] if best is not None else [],
        )

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> RecognizeStream:
        return JohnnySTTStream(
            self,
            provider=self._provider,
            language=self._resolve_language(language),
            conn_options=conn_options,
        )


__all__ = ["JohnnySTT", "JohnnySTTStream", "transcript_to_speech_event"]
