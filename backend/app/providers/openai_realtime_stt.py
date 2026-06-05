"""OpenAI Realtime API streaming speech-to-text adapter.

Streams 16 kHz mono S16LE PCM into OpenAI's Realtime WebSocket
(``wss://api.openai.com/v1/realtime?intent=transcription``) and yields
TranscriptEvent objects from the
``conversation.item.input_audio_transcription.delta`` / ``.completed``
server events. The pipeline already chops audio into VAD-bounded
utterances before handing it to STT; ``transcribe_stream`` treats each
invocation as one logical utterance — audio is base64-encoded into
``input_audio_buffer.append`` events, ``input_audio_buffer.commit`` is
sent once the iterator is exhausted, and the receive loop drains
events until the transcription completes.

Latency profile: OpenAI Realtime's whisper-1 backend returns deltas
within ~200-500 ms of audio arrival; the final ``.completed`` event
fires shortly after commit. Partial deltas stream with
``is_final=False``; the completed event emits ``is_final=True``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Mapping
from importlib import import_module
from typing import Any, Protocol, runtime_checkable

from app.providers.base import (
    PCM_SAMPLE_WIDTH_BYTES,
    ProviderConfig,
    ProviderKind,
    STTError,
    STTProvider,
    TranscriptEvent,
    get_registry,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "openai-realtime"
DEFAULT_BASE_URL = "wss://api.openai.com/v1/realtime"
DEFAULT_INTENT = "transcription"
DEFAULT_MODEL = "whisper-1"
DEFAULT_LANGUAGE: str | None = None
DEFAULT_BETA_HEADER = "realtime=v1"
# Each audio chunk forwarded to OpenAI is bounded to avoid building
# unbounded base64 payloads; 0.5 s of 16 kHz S16LE PCM is 16 000 bytes.
DEFAULT_MAX_APPEND_BYTES = 16_000


@runtime_checkable
class _WebSocketLike(Protocol):
    """Minimal interface satisfied by ``websockets.asyncio.client.ClientConnection``.

    Tests inject a fake by overriding
    :meth:`OpenAIRealtimeSTT._open_connection` so no socket is opened.
    """

    async def send(self, data: bytes | str) -> None: ...
    async def recv(self) -> bytes | str: ...
    async def close(self) -> None: ...


class OpenAIRealtimeSTT(STTProvider):
    """Streaming STT via OpenAI's Realtime ``?intent=transcription`` socket.

    Required credentials:

    * ``api_key`` — OpenAI secret key. Sent as ``Authorization: Bearer``.

    Configuration ``options`` (any key may be omitted):

    * ``model`` — transcription model. Default ``"whisper-1"``;
      ``gpt-4o-transcribe`` and ``gpt-4o-mini-transcribe`` are also valid.
    * ``language`` — BCP-47 language code (e.g. ``"en"``); default lets
      the model autodetect.
    * ``prompt`` — optional bias prompt forwarded as
      ``transcription_session.update.session.input_audio_transcription.prompt``.
    * ``base_url`` — WebSocket endpoint base. Defaults to OpenAI's public
      ``wss://api.openai.com/v1/realtime``.
    * ``intent`` — query param (default ``"transcription"``). Overrideable
      for proxied deployments that use a non-standard intent path.
    * ``beta_header`` — value sent as ``OpenAI-Beta``. Default
      ``"realtime=v1"``.
    * ``max_append_bytes`` — chunk size used to slice large audio buffers
      into multiple ``input_audio_buffer.append`` events. Must be a
      multiple of 2. Default 16 000 (~0.5 s @ 16 kHz).
    """

    def __init__(self, config: ProviderConfig) -> None:
        if config.kind is not ProviderKind.STT:
            raise ValueError(
                f"OpenAIRealtimeSTT requires ProviderKind.STT; got {config.kind.value}"
            )
        api_key = config.credentials.get("api_key")
        if not api_key:
            raise ValueError("OpenAIRealtimeSTT requires 'api_key' in credentials")
        self._api_key = str(api_key)
        opts = config.options
        self._model = str(opts.get("model") or DEFAULT_MODEL)
        language = opts.get("language")
        self._language: str | None = (
            str(language) if language not in (None, "") else None
        )
        prompt = opts.get("prompt")
        self._prompt: str | None = (
            str(prompt) if prompt not in (None, "") else None
        )
        self._base_url = str(opts.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self._intent = str(opts.get("intent") or DEFAULT_INTENT)
        self._beta_header = str(opts.get("beta_header") or DEFAULT_BETA_HEADER)
        max_append = int(opts.get("max_append_bytes") or DEFAULT_MAX_APPEND_BYTES)
        if max_append <= 0:
            raise ValueError(f"max_append_bytes must be positive; got {max_append}")
        if max_append % PCM_SAMPLE_WIDTH_BYTES:
            raise ValueError(
                f"max_append_bytes must be a multiple of {PCM_SAMPLE_WIDTH_BYTES}"
            )
        self._max_append_bytes = max_append

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def model(self) -> str:
        return self._model

    @property
    def language(self) -> str | None:
        return self._language

    @property
    def prompt(self) -> str | None:
        return self._prompt

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def intent(self) -> str:
        return self._intent

    @property
    def max_append_bytes(self) -> int:
        return self._max_append_bytes

    def _build_url(self) -> str:
        if self._intent:
            return f"{self._base_url}?intent={self._intent}"
        return self._base_url

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "OpenAI-Beta": self._beta_header,
        }

    def _build_session_update(self) -> dict[str, Any]:
        transcription: dict[str, Any] = {"model": self._model}
        if self._language is not None:
            transcription["language"] = self._language
        if self._prompt is not None:
            transcription["prompt"] = self._prompt
        return {
            "type": "transcription_session.update",
            "session": {
                "input_audio_format": "pcm16",
                "input_audio_transcription": transcription,
            },
        }

    async def transcribe_stream(
        self,
        audio_iter: AsyncIterator[bytes],
    ) -> AsyncIterator[TranscriptEvent]:
        """Stream PCM into OpenAI Realtime and yield TranscriptEvents.

        Opens one WebSocket per call. Audio chunks are buffered into
        ``input_audio_buffer.append`` events (sliced to ``max_append_bytes``);
        once ``audio_iter`` is exhausted, ``input_audio_buffer.commit`` is
        sent and the receive loop drains delta + completed events until
        a single ``.completed`` event arrives or the server closes. Empty
        iterators are a no-op — no socket is opened.
        """
        url = self._build_url()
        headers = self._build_headers()
        first_chunk: bytes | None = None
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
            raise STTError(f"openai-realtime WebSocket connect failed: {exc}") from exc

        send_done = asyncio.Event()
        send_error: list[BaseException] = []
        completion = asyncio.Event()

        async def sender() -> None:
            try:
                await ws.send(json.dumps(self._build_session_update()))
                for slice_ in _split_pcm(first_chunk, self._max_append_bytes):
                    await ws.send(_audio_append_message(slice_))
                async for chunk in audio_iter:
                    if not chunk:
                        continue
                    if len(chunk) % PCM_SAMPLE_WIDTH_BYTES:
                        raise STTError(
                            f"audio chunk {len(chunk)} bytes is not aligned to "
                            f"{PCM_SAMPLE_WIDTH_BYTES}-byte S16 samples"
                        )
                    for slice_ in _split_pcm(chunk, self._max_append_bytes):
                        await ws.send(_audio_append_message(slice_))
                await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
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
                if event is None:
                    continue
                yield event
                if event.is_final:
                    completion.set()
                    break
        except STTError:
            raise
        except Exception as exc:
            raise STTError(f"openai-realtime receive failed: {exc}") from exc
        finally:
            send_task.cancel()
            with contextlib.suppress(BaseException):
                await send_task
            with contextlib.suppress(Exception):
                await ws.close()

        if send_error and not completion.is_set():
            first_error = send_error[0]
            if isinstance(first_error, STTError):
                raise first_error
            raise STTError(f"openai-realtime send failed: {first_error}") from first_error

    async def _open_connection(
        self, url: str, headers: Mapping[str, str]
    ) -> _WebSocketLike:
        """Open the Realtime WebSocket. Overridable in tests."""
        try:
            ws_client = import_module("websockets.asyncio.client")
        except ImportError as exc:  # pragma: no cover
            raise STTError(
                "websockets is not installed; install via `pip install websockets`"
            ) from exc
        connection = await ws_client.connect(url, additional_headers=dict(headers))
        return _cast_to_ws_protocol(connection)


def _audio_append_message(pcm: bytes) -> str:
    """Build the JSON text for an ``input_audio_buffer.append`` event."""
    return json.dumps(
        {
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm).decode("ascii"),
        }
    )


def _split_pcm(pcm: bytes, chunk_size: int) -> list[bytes]:
    """Slice ``pcm`` into ``chunk_size``-byte chunks aligned to S16 samples."""
    if chunk_size <= 0 or len(pcm) <= chunk_size:
        return [pcm]
    return [pcm[i : i + chunk_size] for i in range(0, len(pcm), chunk_size)]


def _parse_message(message: bytes | str) -> TranscriptEvent | None:
    """Parse one Realtime event into a TranscriptEvent.

    Returns ``None`` for non-transcription events
    (``transcription_session.created``, ``response.*``, etc.) and for
    transcription deltas that carry empty text.
    """
    try:
        payload = _decode_json(message)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.debug("openai-realtime emitted non-JSON message; ignoring")
        return None
    if not isinstance(payload, Mapping):
        return None
    msg_type = payload.get("type")
    if msg_type == "error":
        error = payload.get("error")
        detail = ""
        if isinstance(error, Mapping):
            detail = str(error.get("message") or error.get("code") or "")
        raise STTError(f"openai-realtime error: {detail or 'unknown'}")
    if msg_type == "conversation.item.input_audio_transcription.delta":
        delta = payload.get("delta", "")
        if not isinstance(delta, str):
            return None
        text = delta.strip()
        if not text:
            return None
        return TranscriptEvent(
            text=text,
            is_final=False,
            timestamp_ms=0,
            confidence=None,
        )
    if msg_type == "conversation.item.input_audio_transcription.completed":
        transcript = payload.get("transcript", "")
        if not isinstance(transcript, str):
            return None
        text = transcript.strip()
        if not text:
            return None
        return TranscriptEvent(
            text=text,
            is_final=True,
            timestamp_ms=0,
            confidence=None,
        )
    return None


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
    """Register :class:`OpenAIRealtimeSTT` under ``(ProviderKind.STT, "openai-realtime")``.

    Idempotent when ``replace=True``. Called at import time from
    :mod:`app.providers` so the global registry contains ``openai-realtime``
    by the time API startup runs.
    """
    get_registry().register(
        ProviderKind.STT, PROVIDER_NAME, OpenAIRealtimeSTT, replace=replace
    )


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_BETA_HEADER",
    "DEFAULT_INTENT",
    "DEFAULT_LANGUAGE",
    "DEFAULT_MAX_APPEND_BYTES",
    "DEFAULT_MODEL",
    "OpenAIRealtimeSTT",
    "PROVIDER_NAME",
    "register",
]
