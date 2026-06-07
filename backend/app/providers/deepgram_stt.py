"""Deepgram streaming speech-to-text adapter.

Streams 16 kHz mono S16LE PCM into Deepgram's Listen WebSocket API
(``wss://api.deepgram.com/v1/listen``) and yields TranscriptEvent
objects as the server emits Results messages. The pipeline already
chops audio into VAD-bounded utterances before handing it to STT;
``transcribe_stream`` treats each invocation as one logical utterance —
audio is forwarded as chunks arrive, ``{"type": "CloseStream"}`` is sent
once the iterator is exhausted, and the receive loop drains the
remaining results until Deepgram closes the connection.

Latency profile: Deepgram's nova-2 model returns interim transcripts
within ~100-300 ms and finalises around the configured ``endpointing_ms``
silence window (default 300 ms). Partial events stream with
``is_final=False``; finalised utterances emit ``is_final=True``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from importlib import import_module
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlencode

from app.providers.base import (
    PCM_SAMPLE_RATE_HZ,
    PCM_SAMPLE_WIDTH_BYTES,
    ProviderConfig,
    ProviderKind,
    STTError,
    STTProvider,
    TranscriptEvent,
    get_registry,
)
from app.providers.schema import (
    FieldDef,
    FieldGroup,
    FieldOption,
    FieldType,
    ProviderSchema,
    ProviderTip,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "deepgram"
DEFAULT_BASE_URL = "wss://api.deepgram.com/v1/listen"
DEFAULT_MODEL = "nova-2"
DEFAULT_LANGUAGE = "en-US"
DEFAULT_ENDPOINTING_MS = 300
DEFAULT_INTERIM_RESULTS = True
DEFAULT_PUNCTUATE = True
DEFAULT_SMART_FORMAT = True
CLOSE_STREAM_MESSAGE = json.dumps({"type": "CloseStream"})


@runtime_checkable
class _WebSocketLike(Protocol):
    """Minimal interface satisfied by ``websockets.asyncio.client.ClientConnection``.

    Tests inject a fake by overriding :meth:`DeepgramSTT._open_connection`
    so the adapter never touches the network. The protocol intentionally
    omits the iterator surface — the adapter calls ``recv`` in a loop and
    breaks when ``ConnectionClosed`` is raised, matching the real client.
    """

    async def send(self, data: bytes | str) -> None: ...
    async def recv(self) -> bytes | str: ...
    async def close(self) -> None: ...


class DeepgramSTT(STTProvider):
    """Streaming STT via Deepgram's Listen WebSocket endpoint.

    Required credentials:

    * ``api_key`` — Deepgram API key. Sent as ``Authorization: Token``.

    Configuration ``options`` (any key may be omitted):

    * ``model`` — Deepgram model. Default ``"nova-2"``.
    * ``language`` — BCP-47 language code. Default ``"en-US"``.
    * ``base_url`` — WebSocket endpoint URL; defaults to Deepgram's public
      ``wss://api.deepgram.com/v1/listen``.
    * ``endpointing_ms`` — silence duration (ms) that finalises an
      utterance. Default 300.
    * ``interim_results`` — emit partial ``is_final=False`` events.
      Default True.
    * ``punctuate`` — auto-punctuate transcripts. Default True.
    * ``smart_format`` — apply number / date formatting. Default True.
    * ``extra_query`` — dict of arbitrary query params to forward (e.g.
      ``{"diarize": "true"}``).
    """

    def __init__(self, config: ProviderConfig) -> None:
        if config.kind is not ProviderKind.STT:
            raise ValueError(
                f"DeepgramSTT requires ProviderKind.STT; got {config.kind.value}"
            )
        api_key = config.credentials.get("api_key")
        if not api_key:
            raise ValueError("DeepgramSTT requires 'api_key' in credentials")
        self._api_key = str(api_key)
        opts = config.options
        self._model = str(opts.get("model") or DEFAULT_MODEL)
        self._language = str(opts.get("language") or DEFAULT_LANGUAGE)
        self._base_url = str(opts.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        endpointing = opts.get("endpointing_ms")
        self._endpointing_ms = (
            int(endpointing) if endpointing is not None else DEFAULT_ENDPOINTING_MS
        )
        if self._endpointing_ms < 0:
            raise ValueError(
                f"endpointing_ms must be non-negative; got {self._endpointing_ms}"
            )
        self._interim_results = bool(opts.get("interim_results", DEFAULT_INTERIM_RESULTS))
        self._punctuate = bool(opts.get("punctuate", DEFAULT_PUNCTUATE))
        self._smart_format = bool(opts.get("smart_format", DEFAULT_SMART_FORMAT))
        extra_query = opts.get("extra_query")
        self._extra_query: dict[str, str] = {}
        if isinstance(extra_query, Mapping):
            self._extra_query = {str(k): str(v) for k, v in extra_query.items()}

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @classmethod
    def field_schema(cls) -> ProviderSchema:
        return ProviderSchema(
            kind=ProviderKind.STT,
            provider_name=PROVIDER_NAME,
            display_name="Deepgram",
            summary="Lowest streaming latency. Excellent diarization. Pay-as-you-go.",
            signup_url="https://console.deepgram.com/signup",
            fields=(
                FieldDef(
                    name="api_key",
                    label="API key",
                    type=FieldType.PASSWORD,
                    required=True,
                    secret=True,
                    placeholder="dg-...",
                    help_text="Get a key from console.deepgram.com.",
                    signup_url="https://console.deepgram.com/signup",
                    env_key="DEEPGRAM_API_KEY",
                    group=FieldGroup.AUTH,
                ),
                FieldDef(
                    name="model",
                    label="Model",
                    type=FieldType.SELECT,
                    default=DEFAULT_MODEL,
                    help_text="Deepgram speech model.",
                    options=(
                        FieldOption(value="nova-3", label="nova-3 (latest)"),
                        FieldOption(value="nova-2", label="nova-2 (recommended)"),
                        FieldOption(value="enhanced", label="enhanced"),
                        FieldOption(value="base", label="base"),
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="language",
                    label="Language",
                    default=DEFAULT_LANGUAGE,
                    placeholder="en-US",
                    help_text="BCP-47 language tag.",
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="base_url",
                    label="WebSocket endpoint",
                    type=FieldType.URL,
                    default=DEFAULT_BASE_URL,
                    help_text="Override only for self-hosted gateways.",
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="endpointing_ms",
                    label="Endpointing (ms)",
                    type=FieldType.NUMBER,
                    default=DEFAULT_ENDPOINTING_MS,
                    help_text="Silence duration that finalises an utterance.",
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="interim_results",
                    label="Interim results",
                    type=FieldType.CHECKBOX,
                    default=DEFAULT_INTERIM_RESULTS,
                    help_text="Emit partial transcripts as they arrive.",
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="punctuate",
                    label="Punctuate",
                    type=FieldType.CHECKBOX,
                    default=DEFAULT_PUNCTUATE,
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="smart_format",
                    label="Smart format",
                    type=FieldType.CHECKBOX,
                    default=DEFAULT_SMART_FORMAT,
                    help_text="Apply number / date / currency formatting.",
                    group=FieldGroup.ADVANCED,
                ),
            ),
            tips=(
                ProviderTip(
                    topic="Deepgram is the latency king for STT",
                    body=(
                        "Streaming-mode partials arrive in ~80-150 ms "
                        "from EU/US — about 3-5x faster than running "
                        "Whisper locally on CPU. If your latency "
                        "budget is tight and you accept cloud egress, "
                        "this is the pick."
                    ),
                ),
                ProviderTip(
                    topic="nova-3 / nova-2 — pick nova-2 unless you've tested nova-3",
                    body=(
                        "nova-2 is the recommended stable model; "
                        "nova-3 is newer with slightly better "
                        "accuracy on noisy audio but may behave "
                        "differently for your accent/domain. "
                        "Default to nova-2 in production."
                    ),
                ),
                ProviderTip(
                    topic="Endpointing — keep at ~300 ms",
                    body=(
                        "Deepgram's endpointing fires its own "
                        "is_final marker after this many ms of "
                        "silence. The Johnny pipeline overlays its "
                        "own 800 ms VAD endpointing on top, so the "
                        "Deepgram value mostly governs how many "
                        "partials you get. 300 ms is the documented "
                        "sweet spot."
                    ),
                ),
            ),
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def language(self) -> str:
        return self._language

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def endpointing_ms(self) -> int:
        return self._endpointing_ms

    @property
    def interim_results(self) -> bool:
        return self._interim_results

    @property
    def punctuate(self) -> bool:
        return self._punctuate

    @property
    def smart_format(self) -> bool:
        return self._smart_format

    def _build_url(self) -> str:
        params: dict[str, str] = {
            "encoding": "linear16",
            "sample_rate": str(PCM_SAMPLE_RATE_HZ),
            "channels": "1",
            "model": self._model,
            "language": self._language,
            "interim_results": "true" if self._interim_results else "false",
            "punctuate": "true" if self._punctuate else "false",
            "smart_format": "true" if self._smart_format else "false",
            "endpointing": str(self._endpointing_ms),
        }
        params.update(self._extra_query)
        return f"{self._base_url}?{urlencode(params)}"

    def _build_headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self._api_key}"}

    async def transcribe_stream(
        self,
        audio_iter: AsyncIterator[bytes],
    ) -> AsyncIterator[TranscriptEvent]:
        """Stream PCM into Deepgram and yield TranscriptEvents as they arrive.

        Opens one WebSocket per call. Audio chunks are forwarded as binary
        frames; once ``audio_iter`` is exhausted, ``{"type":"CloseStream"}``
        is sent and the receive loop drains the remaining results until
        the server closes the connection. Empty iterators are a no-op —
        no socket is opened.
        """
        url = self._build_url()
        headers = self._build_headers()
        first_chunk: bytes | None = None
        # Probe the iterator for any data before opening the WS; an empty
        # utterance must not even trigger a connection (mirrors
        # FasterWhisperSTT's "no model load when buffer is empty" behaviour).
        async for chunk in audio_iter:
            if chunk:
                if len(chunk) % PCM_SAMPLE_WIDTH_BYTES:
                    raise STTError(
                        f"audio chunk {len(chunk)} bytes is not aligned to "
                        f"{PCM_SAMPLE_WIDTH_BYTES}-byte S16 samples"
                    )
                first_chunk = chunk
                break
        if first_chunk is None:
            return

        try:
            ws = await self._open_connection(url, headers)
        except Exception as exc:
            raise STTError(f"deepgram WebSocket connect failed: {exc}") from exc

        send_done = asyncio.Event()
        send_error: list[BaseException] = []

        async def sender() -> None:
            try:
                await ws.send(first_chunk)
                async for chunk in audio_iter:
                    if not chunk:
                        continue
                    if len(chunk) % PCM_SAMPLE_WIDTH_BYTES:
                        raise STTError(
                            f"audio chunk {len(chunk)} bytes is not aligned to "
                            f"{PCM_SAMPLE_WIDTH_BYTES}-byte S16 samples"
                        )
                    await ws.send(chunk)
                await ws.send(CLOSE_STREAM_MESSAGE)
            except BaseException as exc:
                send_error.append(exc)
                raise
            finally:
                send_done.set()

        send_task = asyncio.create_task(sender())
        try:
            connection_closed_cls = _connection_closed_class()
            while True:
                try:
                    message = await ws.recv()
                except connection_closed_cls:
                    break
                event = _parse_message(message)
                if event is not None:
                    yield event
        except STTError:
            raise
        except Exception as exc:
            raise STTError(f"deepgram receive failed: {exc}") from exc
        finally:
            send_task.cancel()
            with contextlib.suppress(BaseException):
                await send_task
            with contextlib.suppress(Exception):
                await ws.close()

        if send_error:
            first_error = send_error[0]
            if isinstance(first_error, STTError):
                raise first_error
            raise STTError(f"deepgram send failed: {first_error}") from first_error

    async def _open_connection(
        self, url: str, headers: Mapping[str, str]
    ) -> _WebSocketLike:
        """Open a Deepgram Listen WebSocket. Overridable in tests."""
        try:
            ws_client = import_module("websockets.asyncio.client")
        except ImportError as exc:  # pragma: no cover — websockets is in deps
            raise STTError(
                "websockets is not installed; install via `pip install websockets`"
            ) from exc
        connection = await ws_client.connect(url, additional_headers=dict(headers))
        return _cast_to_ws_protocol(connection)


def _parse_message(message: bytes | str) -> TranscriptEvent | None:
    """Parse one Deepgram WebSocket message into a TranscriptEvent.

    Returns ``None`` for non-Results messages (Metadata, SpeechStarted,
    UtteranceEnd) and for Results with empty transcripts.
    """
    try:
        payload = _decode_json(message)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.debug("deepgram emitted non-JSON message; ignoring")
        return None
    if not isinstance(payload, Mapping):
        return None
    if payload.get("type") != "Results":
        return None
    channel = payload.get("channel")
    if not isinstance(channel, Mapping):
        return None
    alternatives = channel.get("alternatives")
    if not isinstance(alternatives, Sequence) or not alternatives:
        return None
    alt = alternatives[0]
    if not isinstance(alt, Mapping):
        return None
    transcript = alt.get("transcript", "")
    if not isinstance(transcript, str):
        return None
    text = transcript.strip()
    if not text:
        return None
    is_final = bool(payload.get("is_final"))
    confidence_raw = alt.get("confidence")
    confidence: float | None = None
    if isinstance(confidence_raw, int | float):
        confidence = max(0.0, min(1.0, float(confidence_raw)))
    start_raw = payload.get("start")
    timestamp_ms = 0
    if isinstance(start_raw, int | float):
        timestamp_ms = max(0, int(float(start_raw) * 1000))
    return TranscriptEvent(
        text=text,
        is_final=is_final,
        timestamp_ms=timestamp_ms,
        confidence=confidence,
    )


def _decode_json(message: bytes | str) -> Any:
    if isinstance(message, bytes):
        return json.loads(message.decode("utf-8"))
    return json.loads(message)


def _connection_closed_class() -> type[BaseException]:
    """Return ``websockets.exceptions.ConnectionClosed``; fallback for tests."""
    try:
        exceptions = import_module("websockets.exceptions")
        cls = exceptions.ConnectionClosed
    except ImportError:  # pragma: no cover
        return ConnectionError
    if isinstance(cls, type) and issubclass(cls, BaseException):
        return cls
    return ConnectionError


def _cast_to_ws_protocol(connection: Any) -> _WebSocketLike:
    """Narrow the websockets client to the adapter's protocol view."""
    return connection  # type: ignore[no-any-return]


def register(*, replace: bool = False) -> None:
    """Register :class:`DeepgramSTT` under ``(ProviderKind.STT, "deepgram")``.

    Idempotent when ``replace=True``. Called at import time from
    :mod:`app.providers` so the global registry contains ``deepgram`` by
    the time API startup runs.
    """
    get_registry().register(
        ProviderKind.STT, PROVIDER_NAME, DeepgramSTT, replace=replace
    )


__all__ = [
    "CLOSE_STREAM_MESSAGE",
    "DEFAULT_BASE_URL",
    "DEFAULT_ENDPOINTING_MS",
    "DEFAULT_INTERIM_RESULTS",
    "DEFAULT_LANGUAGE",
    "DEFAULT_MODEL",
    "DEFAULT_PUNCTUATE",
    "DEFAULT_SMART_FORMAT",
    "DeepgramSTT",
    "PROVIDER_NAME",
    "register",
]
