"""Google Gemini Live API speech-to-speech (S2S) adapter (Johnny-ckz.20).

Opens a single WebSocket against ``wss://generativelanguage.googleapis.com``
to drive Gemini Live's ``BidiGenerateContent`` bidirectional protocol.
Each :class:`GeminiLiveSession` is one open socket: PCM in via
``realtimeInput``, audio + transcripts out via ``serverContent``,
function calling via ``toolCall`` / ``toolResponse``.

Wire shape (current at fetch date 2026-06-07, spec last updated 2026-06-01):

* Endpoint:
  ``wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=<api_key>``
* First client message MUST be a ``setup`` envelope carrying model,
  generation config (including response_modalities=["AUDIO"]),
  speech_config / voice_config, system_instruction, tools, and
  optionally ``input_audio_transcription`` / ``output_audio_transcription``.
* Subsequent client messages carry PCM via
  ``{"realtimeInput": {"audio": {"data": "<base64>", "mimeType": "audio/pcm;rate=16000"}}}``.
* Server messages carry assistant audio + transcripts under
  ``{"serverContent": {...}}`` with:
  - ``modelTurn.parts[].inlineData.data`` — base64 PCM @ 24 kHz S16LE
  - ``inputTranscription.text`` — user-side STT delta/final
  - ``outputTranscription.text`` — assistant text aligned with audio
  - ``interrupted: true`` — VAD detected user speech, current response cut
  - ``turnComplete: true`` — assistant turn natural completion
* Tool calls under ``{"toolCall": {"functionCalls": [...]}}``; clients
  reply via ``{"toolResponse": {"functionResponses": [...]}}``.

Important deltas from OpenAI Realtime:

* No explicit ``response.cancel`` — Gemini Live's VAD owns barge-in
  detection. The adapter calls ``interrupt()`` for client-side cancellation
  which sends an ``activityEnd`` signal (so we don't wait for VAD).
* Audio output is 24 kHz, not the pipeline's 16 kHz — the adapter
  resamples at the boundary so the rest of the pipeline stays 16 kHz.
* Authentication is a ``?key=<api_key>`` query parameter (matching the
  REST Generative Language API auth used by :mod:`app.providers.gemini_llm`)
  rather than a header. Ephemeral access tokens via
  ``access_token=<token>`` are supported by Google but not configured here.

This module is SQLAlchemy-free so the meet-worker image can import it
without pulling in the ORM stack.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from importlib import import_module
from typing import Any, Protocol, runtime_checkable

from app.providers._pcm import resample_pcm16
from app.providers.base import (
    PCM_SAMPLE_WIDTH_BYTES,
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
    S2SToolCall,
    S2STranscript,
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

PROVIDER_NAME = "gemini-live"

DEFAULT_BASE_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
"""Production WebSocket endpoint for Gemini Live BidiGenerateContent."""

DEFAULT_MODEL = "gemini-2.5-flash-native-audio-latest"
"""Stable production-ready Live model as of 2026-06-07.

Sourced from a live ``GET /v1beta/models?key=...`` call: the
generativelanguage API surfaces only four models with
``bidiGenerateContent`` in ``supportedGenerationMethods`` —
``gemini-2.5-flash-native-audio-latest`` (stable),
``gemini-2.5-flash-native-audio-preview-09-2025`` (legacy preview, slated
for removal 2026-03-19), ``gemini-2.5-flash-native-audio-preview-12-2025``
(later preview), and ``gemini-3.1-flash-live-preview`` (newest gen). The
public Google marketing pages list a ``gemini-live-2.5-flash-native-audio``
alias too, but the live API only recognises the ``gemini-2.5-...``
form, so that is what we use.
"""

DEFAULT_VOICE = "Kore"
"""Default prebuilt voice. The Live API ships ~30 voices; ``Kore`` is the
canonical example in Google's documentation and a safe neutral pick."""

DEFAULT_LANGUAGE: str | None = None
"""Language code (BCP-47, e.g. ``"en-US"``); ``None`` lets the model auto-detect."""

PIPELINE_SAMPLE_RATE_HZ = 16_000
"""The rest of the voice pipeline runs at 16 kHz mono S16LE.

The Live API accepts 16 kHz input natively, so no resampling on the way
in. Audio output is at 24 kHz and must be downsampled at this boundary
so transport playback stays consistent across split / unified modes.
"""

WIRE_OUTPUT_SAMPLE_RATE_HZ = 24_000
"""Gemini Live always emits 24 kHz S16LE PCM, regardless of input rate."""

DEFAULT_MAX_AUDIO_PAYLOAD_BYTES = 32_000
"""Cap inbound base64-encoded chunks so a single websocket frame stays
small even when the capture loop hands the adapter a long buffer.

32 000 raw bytes ≈ 1 s of 16 kHz S16LE PCM. The Live API documents no
upper bound on a single ``realtimeInput.audio`` event but limiting
chunk size keeps base64 overhead and head-of-line blocking bounded.
"""

DEFAULT_TIMEOUT_S = 60.0
"""WebSocket open + ping timeout in seconds."""

# ---- Voice catalog --------------------------------------------------------
# Sourced from Google's "Configure language and voice" docs (last verified
# 2026-06-07). Voices listed alphabetically; the catalog grows over time
# but the documented stable set is around 30 names. Users can supply any
# other voice name via ``voice_id`` if they want a newer voice — the
# adapter does not gate on this list.
PREBUILT_VOICES: tuple[str, ...] = (
    "Aoede",
    "Charon",
    "Fenrir",
    "Kore",
    "Leda",
    "Orus",
    "Puck",
    "Zephyr",
)
"""Documented prebuilt voice names for ``speechConfig.voiceConfig``.

This is the trimmed list used to populate the UI dropdown — Google
documents ~30 voices total but most are language-specific variants of
these eight that the model rarely picks on its own without explicit
language config. The ``voice_id`` field still accepts arbitrary strings
so an operator can override with any newer voice.
"""


@runtime_checkable
class _WebSocketLike(Protocol):
    """Minimal interface satisfied by ``websockets.asyncio.client.ClientConnection``.

    Tests inject a fake by overriding
    :meth:`GeminiLiveS2S._open_connection` so no real socket is opened.
    """

    async def send(self, data: bytes | str) -> None: ...
    async def recv(self) -> bytes | str: ...
    async def close(self) -> None: ...


# ---- Provider -------------------------------------------------------------


class GeminiLiveS2S(S2SProvider):
    """Speech-to-speech adapter for Google's Gemini Live (BidiGenerateContent).

    Required credentials:

    * ``api_key`` — the Gemini API key. Forwarded as the ``?key=``
      query parameter on the WebSocket URL (matching the auth pattern
      used by :mod:`app.providers.gemini_llm`).

    Configuration ``options`` (any key may be omitted):

    * ``model`` — Live API model name. Default
      ``"gemini-live-2.5-flash-native-audio"``. Other values:
      ``"gemini-3.1-flash-live-preview"`` for the very newest gen.
      Adapters that need to talk to a regional/Vertex deployment can
      override ``base_url`` and any compatible model id.
    * ``voice_id`` — prebuilt voice name. Default ``"Kore"``. Any
      voice name documented by Google is acceptable (the API rejects
      unknown values at session start).
    * ``language`` — BCP-47 language code biasing both STT and the
      voice locale. ``None`` (default) lets the model auto-detect.
    * ``base_url`` — WebSocket endpoint override (Vertex / proxied
      deployments). Defaults to Google's public endpoint.
    * ``max_audio_payload_bytes`` — slice large PCM buffers into
      sub-chunks of this size before sending. Default 32 000 (~1 s
      of 16 kHz audio).
    * ``timeout_s`` — websocket open + ping timeout. Default 60 s.
    * ``enable_input_transcription`` — when true (default), enables
      ``inputAudioTranscription`` so the unified pipeline can emit
      user transcripts to the event bus / sink.
    * ``enable_output_transcription`` — when true (default), enables
      ``outputAudioTranscription`` so the assistant transcript is
      published alongside the audio.
    * ``disable_server_vad`` — when true, disables the server-side
      automatic activity detection and requires explicit
      ``activityStart`` / ``activityEnd`` markers around each user
      turn. Default ``False`` (server VAD handles turn boundaries —
      matches the production pipeline's behavior). Set to True for
      tests and headless wire round-trips that need deterministic
      turn commit without real speech audio.
    """

    def __init__(self, config: ProviderConfig) -> None:
        if config.kind is not ProviderKind.S2S:
            raise ValueError(
                f"GeminiLiveS2S requires ProviderKind.S2S; got {config.kind.value}"
            )
        api_key = config.credentials.get("api_key")
        if not api_key:
            raise ValueError("GeminiLiveS2S requires 'api_key' in credentials")
        self._api_key = str(api_key)
        opts = config.options
        self._model = str(opts.get("model") or DEFAULT_MODEL)
        self._voice = str(opts.get("voice_id") or opts.get("voice") or DEFAULT_VOICE)
        language = opts.get("language")
        self._language: str | None = (
            str(language) if language not in (None, "") else None
        )
        self._base_url = str(opts.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        raw_max_payload = opts.get("max_audio_payload_bytes")
        if raw_max_payload is None or raw_max_payload == "":
            raw_max_payload = DEFAULT_MAX_AUDIO_PAYLOAD_BYTES
        try:
            max_payload = int(raw_max_payload)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"max_audio_payload_bytes must be an integer; got {raw_max_payload!r}"
            ) from exc
        if max_payload <= 0:
            raise ValueError(
                f"max_audio_payload_bytes must be positive; got {max_payload}"
            )
        if max_payload % PCM_SAMPLE_WIDTH_BYTES:
            raise ValueError(
                "max_audio_payload_bytes must be a multiple of "
                f"{PCM_SAMPLE_WIDTH_BYTES}"
            )
        self._max_audio_payload_bytes = max_payload
        self._timeout_s = float(opts.get("timeout_s") or DEFAULT_TIMEOUT_S)
        self._enable_input_transcription = bool(
            opts.get("enable_input_transcription", True)
        )
        self._enable_output_transcription = bool(
            opts.get("enable_output_transcription", True)
        )
        self._disable_server_vad = bool(opts.get("disable_server_vad", False))

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def model(self) -> str:
        return self._model

    @property
    def voice(self) -> str:
        return self._voice

    @property
    def language(self) -> str | None:
        return self._language

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def max_audio_payload_bytes(self) -> int:
        return self._max_audio_payload_bytes

    @property
    def enable_input_transcription(self) -> bool:
        return self._enable_input_transcription

    @property
    def enable_output_transcription(self) -> bool:
        return self._enable_output_transcription

    @property
    def disable_server_vad(self) -> bool:
        return self._disable_server_vad

    @classmethod
    def field_schema(cls) -> ProviderSchema:
        return ProviderSchema(
            kind=ProviderKind.S2S,
            provider_name=PROVIDER_NAME,
            display_name="Google Gemini Live",
            summary=(
                "Unified speech-to-speech via Gemini Live (BidiGenerateContent). "
                "Native audio I/O, 30+ voices, integrated VAD + barge-in."
            ),
            signup_url="https://aistudio.google.com/app/apikey",
            fields=(
                FieldDef(
                    name="api_key",
                    label="API key",
                    type=FieldType.PASSWORD,
                    required=True,
                    secret=True,
                    placeholder="AIza...",
                    help_text="Get a key from aistudio.google.com.",
                    signup_url="https://aistudio.google.com/app/apikey",
                    env_key="GOOGLE_API_KEY",
                    group=FieldGroup.AUTH,
                ),
                FieldDef(
                    name="model",
                    label="Model",
                    type=FieldType.SELECT,
                    default=DEFAULT_MODEL,
                    options=(
                        FieldOption(
                            value="gemini-2.5-flash-native-audio-latest",
                            label="gemini-2.5-flash-native-audio-latest (stable)",
                        ),
                        FieldOption(
                            value="gemini-3.1-flash-live-preview",
                            label="gemini-3.1-flash-live-preview (newest)",
                        ),
                        FieldOption(
                            value="gemini-2.5-flash-native-audio-preview-12-2025",
                            label="gemini-2.5-flash-native-audio-preview-12-2025 (preview)",
                        ),
                        FieldOption(
                            value="gemini-2.5-flash-native-audio-preview-09-2025",
                            label=(
                                "gemini-2.5-flash-native-audio-preview-09-2025 "
                                "(legacy, removed 2026-03-19)"
                            ),
                        ),
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="voice_id",
                    label="Voice",
                    type=FieldType.SELECT,
                    default=DEFAULT_VOICE,
                    options=tuple(
                        FieldOption(value=name, label=name) for name in PREBUILT_VOICES
                    ),
                    help_text=(
                        "Gemini ships ~30 prebuilt voices; the dropdown lists the "
                        "neutral-default eight. Type a custom voice name into a "
                        "newer adapter if you need one outside this list."
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="language",
                    label="Language (BCP-47)",
                    placeholder="en-US",
                    help_text="Leave blank to auto-detect from input audio.",
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="enable_input_transcription",
                    label="Stream user-side transcript",
                    type=FieldType.CHECKBOX,
                    default=True,
                    help_text=(
                        "Enables ``inputAudioTranscription`` so the user-side "
                        "transcript reaches the event bus + transcript sink "
                        "(matches split-pipeline visibility)."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="enable_output_transcription",
                    label="Stream assistant transcript",
                    type=FieldType.CHECKBOX,
                    default=True,
                    help_text=(
                        "Enables ``outputAudioTranscription`` so the assistant "
                        "text aligned with the spoken audio is captured for the "
                        "AgentSpoke event + activity log."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="base_url",
                    label="WebSocket endpoint",
                    type=FieldType.URL,
                    default=DEFAULT_BASE_URL,
                    help_text=(
                        "Override only for Vertex AI / proxied deployments — "
                        "the generative language endpoint is the right pick "
                        "for direct API-key auth."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="max_audio_payload_bytes",
                    label="Max audio payload (bytes)",
                    type=FieldType.NUMBER,
                    default=DEFAULT_MAX_AUDIO_PAYLOAD_BYTES,
                    help_text=(
                        "Cap one ``realtimeInput.audio`` event at this many "
                        "PCM bytes; large capture buffers are sliced. 32 000 "
                        "≈ 1 s of 16 kHz S16LE."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="timeout_s",
                    label="Request timeout (s)",
                    type=FieldType.NUMBER,
                    default=DEFAULT_TIMEOUT_S,
                    group=FieldGroup.ADVANCED,
                ),
            ),
            tips=(
                ProviderTip(
                    topic="Native-audio model owns its own VAD",
                    body=(
                        "Gemini Live runs VAD server-side and fires "
                        "``interrupted: true`` as soon as it detects user "
                        "speech mid-response — usually within ~200 ms of "
                        "speech onset. The pipeline's own fast-VAD barge-in "
                        "is redundant in this mode; the slow classifier path "
                        "is skipped entirely."
                    ),
                ),
                ProviderTip(
                    topic="Output is 24 kHz, pipeline runs at 16 kHz",
                    body=(
                        "The adapter downsamples assistant audio from 24 kHz "
                        "to 16 kHz at the receive boundary so the rest of "
                        "the pipeline (transport, recordings) stays at one "
                        "sample rate. Adds ~0 ms latency — resampling is in "
                        "a tight numpy loop."
                    ),
                ),
                ProviderTip(
                    topic="Voice picks are interchangeable mid-session",
                    body=(
                        "Voice is locked once the session opens — flipping "
                        "the dropdown takes effect from the NEXT session. "
                        "``Kore`` and ``Aoede`` are warm/professional; "
                        "``Charon`` and ``Fenrir`` are deeper / more "
                        "dramatic; ``Puck`` is youthful + playful."
                    ),
                ),
                ProviderTip(
                    topic="Auto-detect language unless you have a strong reason",
                    body=(
                        "Leaving Language blank lets the model pick the "
                        "right voice locale per turn — useful for mixed-"
                        "language meetings. Set ``en-US`` only if the model "
                        "is misdetecting (e.g. accented speech in a "
                        "non-English browser locale)."
                    ),
                ),
                ProviderTip(
                    topic="Live API has its own rate limits per project",
                    body=(
                        "Free-tier Gemini API keys get a few concurrent "
                        "Live sessions per minute; paid tiers scale up. If "
                        "open_session returns ``429`` or ``RESOURCE_EXHAUSTED``, "
                        "the adapter raises ``S2SError`` and the pipeline "
                        "falls back to a clean failure rather than a hang."
                    ),
                ),
            ),
        )

    def _build_url(self) -> str:
        """Compose the WebSocket URL with the API key as a query parameter."""
        separator = "&" if "?" in self._base_url else "?"
        return f"{self._base_url}{separator}key={self._api_key}"

    def _build_setup_payload(
        self,
        *,
        instructions: str,
        voice_id: str | None,
        tools: Sequence[ToolDefinition],
    ) -> dict[str, Any]:
        """Build the initial ``setup`` envelope sent immediately after connect.

        The Live API rejects any other message until setup is acknowledged
        via ``setupComplete`` — see ``_GeminiLiveSession._read_loop``.
        """
        voice_name = voice_id or self._voice
        speech_config: dict[str, Any] = {
            "voice_config": {
                "prebuilt_voice_config": {"voice_name": voice_name},
            },
        }
        if self._language:
            speech_config["language_code"] = self._language
        generation_config: dict[str, Any] = {
            "response_modalities": ["AUDIO"],
            "speech_config": speech_config,
        }
        setup: dict[str, Any] = {
            "model": (
                self._model
                if self._model.startswith("models/")
                else f"models/{self._model}"
            ),
            "generation_config": generation_config,
        }
        if instructions:
            setup["system_instruction"] = {"parts": [{"text": instructions}]}
        if self._enable_input_transcription:
            setup["input_audio_transcription"] = {}
        if self._enable_output_transcription:
            setup["output_audio_transcription"] = {}
        if self._disable_server_vad:
            setup["realtime_input_config"] = {
                "automatic_activity_detection": {"disabled": True},
            }
        if tools:
            setup["tools"] = [
                {
                    "function_declarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.parameters,
                        }
                        for t in tools
                    ]
                }
            ]
        return {"setup": setup}

    async def open_session(
        self,
        *,
        instructions: str = "",
        voice_id: str | None = None,
        tools: Sequence[ToolDefinition] = (),
    ) -> S2SSession:
        url = self._build_url()
        try:
            ws = await self._open_connection(url, {})
        except Exception as exc:
            raise S2SError(f"gemini-live WebSocket connect failed: {exc}") from exc

        setup_payload = self._build_setup_payload(
            instructions=instructions,
            voice_id=voice_id,
            tools=tools,
        )
        try:
            await ws.send(json.dumps(setup_payload))
        except Exception as exc:
            with contextlib.suppress(Exception):
                await ws.close()
            raise S2SError(
                f"gemini-live setup send failed: {exc}"
            ) from exc

        session = _GeminiLiveSession(
            ws=ws,
            max_audio_payload_bytes=self._max_audio_payload_bytes,
            manual_vad=self._disable_server_vad,
        )
        await session.start()
        return session

    async def _open_connection(
        self, url: str, headers: Mapping[str, str]
    ) -> _WebSocketLike:
        """Open the Live API WebSocket. Overridable in tests."""
        try:
            ws_client = import_module("websockets.asyncio.client")
        except ImportError as exc:  # pragma: no cover
            raise S2SError(
                "websockets is not installed; install via `pip install websockets`"
            ) from exc
        kwargs: dict[str, Any] = {"open_timeout": self._timeout_s}
        if headers:
            kwargs["additional_headers"] = dict(headers)
        connection = await ws_client.connect(url, **kwargs)
        return _cast_to_ws_protocol(connection)


# ---- Session --------------------------------------------------------------


class _GeminiLiveSession(S2SSession):
    """One live BidiGenerateContent connection.

    The session owns a background read task that drains the socket and
    routes parsed server events to an internal :class:`asyncio.Queue`,
    plus a send-side serializer so concurrent ``send_audio`` /
    ``commit_user_turn`` / ``interrupt`` calls don't interleave bytes
    on the same wire.
    """

    def __init__(
        self,
        *,
        ws: _WebSocketLike,
        max_audio_payload_bytes: int,
        manual_vad: bool = False,
    ) -> None:
        self._ws = ws
        self._max_audio_payload_bytes = max_audio_payload_bytes
        self._manual_vad = manual_vad
        self._queue: asyncio.Queue[S2SEvent | None] = asyncio.Queue()
        self._send_lock = asyncio.Lock()
        self._read_task: asyncio.Task[None] | None = None
        self._closed = False
        self._response_started = False
        self._setup_complete = asyncio.Event()
        self._activity_open = False

    async def start(self) -> None:
        """Spawn the background read task draining the WebSocket."""
        if self._read_task is not None:
            return
        self._read_task = asyncio.create_task(self._read_loop())

    async def send_audio(self, pcm: bytes) -> None:
        if self._closed:
            raise S2SError("send_audio on a closed gemini-live session")
        if not pcm:
            return
        if len(pcm) % PCM_SAMPLE_WIDTH_BYTES:
            raise S2SError(
                f"audio chunk {len(pcm)} bytes is not aligned to "
                f"{PCM_SAMPLE_WIDTH_BYTES}-byte S16 samples"
            )
        # In manual-VAD mode the server requires an explicit
        # ``activityStart`` before the first audio chunk of a turn.
        if self._manual_vad and not self._activity_open:
            await self._send_json({"realtimeInput": {"activityStart": {}}})
            self._activity_open = True
        for slice_ in _split_pcm(pcm, self._max_audio_payload_bytes):
            payload = {
                "realtimeInput": {
                    "audio": {
                        "data": base64.b64encode(slice_).decode("ascii"),
                        "mimeType": (
                            f"audio/pcm;rate={PIPELINE_SAMPLE_RATE_HZ}"
                        ),
                    }
                }
            }
            await self._send_json(payload)

    async def commit_user_turn(self) -> None:
        """Signal end of the user's current turn.

        With server VAD (default): sends ``realtimeInput.audioStreamEnd``
        so the server knows no more audio is coming on this stream. The
        VAD will usually have already detected the speech boundary and
        kicked off a response; this is the explicit fallback for the
        no-VAD case (silence-only buffers, ungated test inputs).

        With manual VAD (``disable_server_vad=True``): sends
        ``realtimeInput.activityEnd`` so the server commits the current
        turn and begins generation. Matches the OpenAI Realtime
        ``input_audio_buffer.commit`` semantics.
        """
        if self._closed:
            raise S2SError("commit_user_turn on a closed gemini-live session")
        if self._manual_vad:
            await self._send_json({"realtimeInput": {"activityEnd": {}}})
            self._activity_open = False
        else:
            await self._send_json({"realtimeInput": {"audioStreamEnd": True}})

    async def events(self) -> AsyncIterator[S2SEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    async def interrupt(self) -> None:
        """Tell the server the user wants to interrupt the current response.

        Gemini Live treats ``activityEnd`` as the explicit "stop talking"
        signal — see the API docs on manual VAD. Failures are swallowed
        (logged) because the pipeline-level interrupt path runs from a
        race-y context where raising would block the user's next turn.
        """
        if self._closed:
            return
        try:
            await self._send_json({"realtimeInput": {"activityEnd": {}}})
        except Exception:  # noqa: BLE001 — log + swallow
            logger.exception("gemini-live interrupt failed")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._ws.close()
        # Signal events() to exit.
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)
        # Drain the read task — it should exit immediately on
        # ConnectionClosed once the socket is closed.
        if self._read_task is not None and not self._read_task.done():
            try:
                await asyncio.wait_for(self._read_task, timeout=1.0)
            except (TimeoutError, Exception):
                self._read_task.cancel()
                with contextlib.suppress(BaseException):
                    await self._read_task

    # ------------------------------------------------------------------
    # Internals

    async def _send_json(self, payload: dict[str, Any]) -> None:
        """Serialise + send under the write lock, mapping errors to S2SError."""
        try:
            data = json.dumps(payload)
        except (TypeError, ValueError) as exc:
            raise S2SError(
                f"gemini-live failed to encode message: {exc}"
            ) from exc
        async with self._send_lock:
            try:
                await self._ws.send(data)
            except Exception as exc:
                raise S2SError(
                    f"gemini-live websocket send failed: {exc}"
                ) from exc

    async def _read_loop(self) -> None:
        """Drain the socket forever, parsing server events onto the queue."""
        connection_closed_cls = _connection_closed_class()
        try:
            while True:
                try:
                    message = await self._ws.recv()
                except connection_closed_cls:
                    break
                except Exception as exc:
                    logger.warning(
                        "gemini-live websocket recv error: %s", exc
                    )
                    break
                for event in _parse_server_message(message):
                    if isinstance(event, _ResponseEnded):
                        # _ResponseEnded is an internal marker — it
                        # carries the interrupted/turnComplete signal
                        # the pipeline expects on its event stream.
                        await self._queue.put(
                            S2SResponseCompleted(
                                finish_reason=event.finish_reason,
                            )
                        )
                        self._response_started = False
                        continue
                    if isinstance(event, S2SAudioFrame | S2STranscript):
                        if not self._response_started:
                            self._response_started = True
                            await self._queue.put(S2SResponseStarted())
                    await self._queue.put(event)
        finally:
            # Ensure events() can return even when the socket dropped
            # without an explicit close from our side.
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(None)


# ---- Parsing --------------------------------------------------------------


class _ResponseEnded:
    """Marker the read loop uses to bubble up turn-end with finish_reason."""

    __slots__ = ("finish_reason",)

    def __init__(self, finish_reason: str) -> None:
        self.finish_reason = finish_reason


def _parse_server_message(
    message: bytes | str,
) -> list[S2SEvent | _ResponseEnded]:
    """Parse one ``BidiGenerateContentServerMessage`` into S2S events.

    A single server message may carry several logical events (e.g. a
    model_turn with multiple parts each containing inline_data, plus an
    inputTranscription + outputTranscription). The order matches the
    wire so consumers see audio + transcripts in the same order Gemini
    emitted them.
    """
    try:
        payload = _decode_json(message)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.debug("gemini-live emitted non-JSON message; ignoring")
        return []
    if not isinstance(payload, Mapping):
        return []

    events: list[S2SEvent | _ResponseEnded] = []

    if "setupComplete" in payload:
        # Acknowledged session setup — nothing to surface to consumers.
        return events

    server_content = payload.get("serverContent")
    if isinstance(server_content, Mapping):
        events.extend(_parse_server_content(server_content))

    tool_call = payload.get("toolCall")
    if isinstance(tool_call, Mapping):
        for call in _parse_tool_calls(tool_call):
            events.append(call)

    if "error" in payload:
        # The server reports errors out-of-band; raise via the read loop
        # by translating to a synthetic _ResponseEnded so the pipeline
        # marks the turn done with an "error" finish_reason. We don't
        # raise from inside the parser to avoid drop-cancelling the
        # read task on a non-fatal transient.
        err = payload["error"]
        message_text = ""
        if isinstance(err, Mapping):
            message_text = str(err.get("message") or err.get("code") or "")
        logger.warning("gemini-live server reported error: %s", message_text)
        events.append(_ResponseEnded(finish_reason="error"))

    return events


def _parse_server_content(
    server_content: Mapping[str, Any],
) -> list[S2SEvent | _ResponseEnded]:
    out: list[S2SEvent | _ResponseEnded] = []

    input_transcription = server_content.get("inputTranscription")
    if isinstance(input_transcription, Mapping):
        text_raw = input_transcription.get("text")
        if isinstance(text_raw, str) and text_raw.strip():
            out.append(
                S2STranscript(
                    text=text_raw.strip(),
                    is_final=True,
                    role="user",
                )
            )

    output_transcription = server_content.get("outputTranscription")
    if isinstance(output_transcription, Mapping):
        text_raw = output_transcription.get("text")
        if isinstance(text_raw, str) and text_raw.strip():
            out.append(
                S2STranscript(
                    text=text_raw.strip(),
                    is_final=True,
                    role="assistant",
                )
            )

    model_turn = server_content.get("modelTurn")
    if isinstance(model_turn, Mapping):
        parts = model_turn.get("parts")
        if isinstance(parts, list):
            for part in parts:
                if not isinstance(part, Mapping):
                    continue
                inline_data = part.get("inlineData") or part.get("inline_data")
                if isinstance(inline_data, Mapping):
                    audio_event = _parse_inline_data(inline_data)
                    if audio_event is not None:
                        out.append(audio_event)
                text_value = part.get("text")
                if isinstance(text_value, str) and text_value.strip():
                    out.append(
                        S2STranscript(
                            text=text_value.strip(),
                            is_final=True,
                            role="assistant",
                        )
                    )

    interrupted = server_content.get("interrupted")
    if interrupted is True:
        out.append(_ResponseEnded(finish_reason="interrupted"))
        return out

    if server_content.get("turnComplete") is True:
        out.append(_ResponseEnded(finish_reason="stop"))

    return out


def _parse_inline_data(inline_data: Mapping[str, Any]) -> S2SAudioFrame | None:
    """Decode + downsample a base64 PCM part from ``modelTurn.parts[]``."""
    data_raw = inline_data.get("data")
    if not isinstance(data_raw, str) or not data_raw:
        return None
    mime_type = inline_data.get("mimeType") or inline_data.get("mime_type") or ""
    try:
        pcm_wire = base64.b64decode(data_raw)
    except (ValueError, TypeError) as exc:
        logger.warning("gemini-live failed to b64-decode audio: %s", exc)
        return None
    if not pcm_wire:
        return None
    wire_rate = _wire_rate_from_mime(str(mime_type))
    if wire_rate != PIPELINE_SAMPLE_RATE_HZ:
        try:
            pcm = resample_pcm16(pcm_wire, wire_rate, PIPELINE_SAMPLE_RATE_HZ)
        except Exception as exc:  # noqa: BLE001 — degrade gracefully
            logger.warning(
                "gemini-live failed to resample %d Hz → %d Hz: %s",
                wire_rate,
                PIPELINE_SAMPLE_RATE_HZ,
                exc,
            )
            return None
    else:
        pcm = pcm_wire
    return S2SAudioFrame(pcm=pcm)


def _wire_rate_from_mime(mime_type: str) -> int:
    """Parse the ``rate=NNNN`` field out of an ``audio/pcm;rate=24000`` string."""
    if not mime_type:
        return WIRE_OUTPUT_SAMPLE_RATE_HZ
    for part in mime_type.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        if key.strip().lower() in {"rate", "samplerate", "sample_rate"}:
            try:
                rate = int(value.strip())
            except ValueError:
                continue
            if rate > 0:
                return rate
    return WIRE_OUTPUT_SAMPLE_RATE_HZ


def _parse_tool_calls(tool_call: Mapping[str, Any]) -> list[S2SToolCall]:
    out: list[S2SToolCall] = []
    function_calls = tool_call.get("functionCalls")
    if not isinstance(function_calls, list):
        return out
    for idx, call in enumerate(function_calls):
        if not isinstance(call, Mapping):
            continue
        name = call.get("name")
        if not isinstance(name, str) or not name:
            continue
        args = call.get("args")
        arguments: dict[str, Any]
        if args is None:
            arguments = {}
        elif isinstance(args, Mapping):
            arguments = dict(args)
        else:
            logger.warning(
                "gemini-live skipping tool call with non-dict args; got %s",
                type(args).__name__,
            )
            continue
        call_id_raw = call.get("id")
        call_id = (
            str(call_id_raw) if isinstance(call_id_raw, str) and call_id_raw
            else f"call_{idx}"
        )
        out.append(S2SToolCall(id=call_id, name=name, arguments=arguments))
    return out


def _decode_json(message: bytes | str) -> Any:
    if isinstance(message, bytes):
        return json.loads(message.decode("utf-8"))
    return json.loads(message)


def _split_pcm(pcm: bytes, chunk_size: int) -> list[bytes]:
    """Slice ``pcm`` into ``chunk_size``-byte chunks aligned to S16 samples."""
    if chunk_size <= 0 or len(pcm) <= chunk_size:
        return [pcm]
    return [pcm[i : i + chunk_size] for i in range(0, len(pcm), chunk_size)]


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
    """Register :class:`GeminiLiveS2S` under ``(ProviderKind.S2S, "gemini-live")``.

    Idempotent when ``replace=True``. Called at import time from
    :mod:`app.providers` so the global registry contains ``gemini-live``
    by the time API startup runs.
    """
    get_registry().register(
        ProviderKind.S2S, PROVIDER_NAME, GeminiLiveS2S, replace=replace
    )


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_LANGUAGE",
    "DEFAULT_MAX_AUDIO_PAYLOAD_BYTES",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_VOICE",
    "GeminiLiveS2S",
    "PIPELINE_SAMPLE_RATE_HZ",
    "PREBUILT_VOICES",
    "PROVIDER_NAME",
    "WIRE_OUTPUT_SAMPLE_RATE_HZ",
    "register",
]
