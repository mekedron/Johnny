"""OpenAI GPT-Realtime speech-to-speech (S2S) adapter (Johnny-ckz.19).

Opens a single WebSocket against ``wss://api.openai.com/v1/realtime`` to
drive OpenAI's GA Realtime API in bidirectional speech-to-speech mode.
Each :class:`_OpenAIRealtimeS2SSession` is one open socket: PCM in via
``input_audio_buffer.append``, audio + transcripts out via
``response.output_audio.delta`` / ``response.output_audio_transcript.delta``,
function calling via ``response.function_call_arguments.*`` /
``conversation.item.create``.

Wire shape (current at fetch date 2026-06-07, GA reshape May 2026):

* Endpoint:
  ``wss://api.openai.com/v1/realtime?model=<model>`` with
  ``Authorization: Bearer <api_key>`` header.
* The legacy ``OpenAI-Beta: realtime=v1`` header is REMOVED in GA and
  must not be sent. ``beta_header`` option remains for the rare proxied
  deployment that still requires the legacy beta endpoint.
* First client message MUST be a ``session.update`` envelope carrying
  ``type=realtime``, ``output_modalities=["audio"]``, ``instructions``,
  ``audio.input.format``, ``audio.input.turn_detection``,
  ``audio.output.format``, ``audio.output.voice``, and optionally
  ``tools``. Sending the pre-GA flat keys
  (``input_audio_format`` / ``voice`` at session root) silently returns
  "Unknown parameter" errors.
* Subsequent client messages carry PCM via
  ``{"type": "input_audio_buffer.append", "audio": "<base64 pcm16>"}``.
* Server messages carry assistant audio + transcripts under
  ``{"type": "response.output_audio.delta", "delta": "<base64 pcm16>"}``
  and ``{"type": "response.output_audio_transcript.delta", "delta": "..."}``
  with a terminating ``response.done`` for each completed turn.
* Tool calls arrive as ``response.function_call_arguments.done`` events
  carrying ``{name, arguments (string), call_id}``; clients reply via
  ``conversation.item.create`` with a ``function_call_output`` item and
  follow up with ``response.create`` to resume generation.
* Barge-in: server VAD with ``interrupt_response=true`` (default) emits
  ``input_audio_buffer.speech_started`` on a fresh user voice and
  auto-cancels the in-flight response. Manual cancellation via
  ``response.cancel`` is supported too — the adapter's
  :meth:`interrupt` sends both ``response.cancel`` and
  ``input_audio_buffer.clear`` for safety.

Important deltas from the OpenAI Realtime STT adapter
(``app/providers/openai_realtime_stt.py``):

* Different ``session.type`` (``"realtime"`` vs ``"transcription"``) and
  different output modality (``audio`` instead of nothing).
* Server VAD is ON by default (matches production conversation
  expectations); the STT adapter sets ``turn_detection=null`` because
  the pipeline already VAD-bounds each utterance.
* Audio is bidirectional: in AND out at 24 kHz PCM16, downsampled to
  16 kHz at the receive boundary so the rest of the pipeline stays at
  one sample rate.
* Tool calling: declared in ``session.update.tools``; responses arrive
  via ``response.function_call_arguments.done`` and surface as
  :class:`S2SToolCall` events.

This module is SQLAlchemy-free so the meet-worker image can import it
without pulling in the ORM stack.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import uuid
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

PROVIDER_NAME = "openai-realtime"

DEFAULT_BASE_URL = "wss://api.openai.com/v1/realtime"
"""Production WebSocket endpoint for the GA Realtime API."""

DEFAULT_MODEL = "gpt-realtime-2"
"""Stable production-ready Realtime model as of 2026-06-07.

Sourced from OpenAI's Realtime model card: ``gpt-realtime-2`` is the
flagship voice agent (released 2026-05-08, supersedes the August 2025
``gpt-realtime``). Sibling models on the same endpoint:

* ``gpt-realtime-mini`` — cheaper/smaller, slightly lower quality
* ``gpt-realtime-translate`` — translation only, not for general S2S
* ``gpt-realtime-whisper`` — STT only (use ``openai-realtime`` STT
  adapter instead)
* ``gpt-realtime`` (legacy, 2025-08-28 GA) — kept for compatibility
* ``gpt-4o-realtime-preview-2024-12-17`` — deprecated, do not use

The legacy preview model rejects the GA session shape because the
flat ``input_audio_format`` keys are required there; the adapter
targets the GA shape only.
"""

DEFAULT_VOICE = "marin"
"""Default voice. ``marin`` and ``cedar`` are the two new flagship
voices exclusive to the Realtime API (2026 release) and recommended by
OpenAI for new builds. Legacy voices: ``alloy``, ``ash``, ``ballad``,
``coral``, ``echo``, ``sage``, ``shimmer``, ``verse``.
"""

PIPELINE_SAMPLE_RATE_HZ = 16_000
"""The rest of the voice pipeline runs at 16 kHz mono S16LE.

The GA Realtime API requires 24 kHz input — the adapter resamples on
the way out (capture → wire) and on the way in (wire → playback).
"""

WIRE_SAMPLE_RATE_HZ = 24_000
"""GA Realtime API minimum sample rate. The pre-GA preview accepted
16 kHz; the GA reshape rejects rates below 24 kHz with "Invalid
'session.audio.input.format.rate': integer below minimum value."
"""

DEFAULT_MAX_APPEND_BYTES = 24_000
"""Cap each ``input_audio_buffer.append`` chunk at ~0.5 s of 24 kHz
S16LE PCM. The API accepts up to 15 MB per event; bounding the chunk
size keeps base64 overhead and websocket head-of-line blocking small.
"""

DEFAULT_TIMEOUT_S = 60.0
"""WebSocket open + ping timeout in seconds."""

DEFAULT_TURN_DETECTION_TYPE = "server_vad"
"""Default VAD type. ``server_vad`` is the legacy energy-based detector
(very fast, default for production conversations). ``semantic_vad`` is
the newer model-driven detector — recommended by OpenAI for natural
turn-taking but somewhat slower. ``null`` disables VAD entirely.
"""

DEFAULT_VAD_THRESHOLD = 0.5
DEFAULT_VAD_PREFIX_PADDING_MS = 300
DEFAULT_VAD_SILENCE_DURATION_MS = 500
"""Server-VAD defaults from the GA docs. Lower threshold → more sensitive;
higher silence_duration_ms → users get longer pauses without barging in.
"""

# OpenAI's Realtime API went GA in 2025; sending ``OpenAI-Beta: realtime=v1``
# now routes to the deprecated beta endpoint, which the server rejects with
# "The Realtime Beta API is no longer supported." Default to empty so no
# header is sent; ``beta_header`` option remains for proxied/legacy deploys.
DEFAULT_BETA_HEADER = ""

# ---- Voice catalog --------------------------------------------------------
PREBUILT_VOICES: tuple[str, ...] = (
    "marin",
    "cedar",
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "sage",
    "shimmer",
    "verse",
)
"""Voices known to work with ``gpt-realtime-2``. ``marin`` and ``cedar``
are the flagship 2026 voices (highest quality, exclusive to Realtime).
The rest are the legacy 4o-realtime voices kept for back-compat. New
voices added to the API can be passed via the ``voice_id`` option
without changing this list (the API rejects unknown values at
session start, so the adapter doesn't gate locally)."""


VALID_TURN_DETECTION_TYPES: tuple[str, ...] = ("server_vad", "semantic_vad", "none")
"""``none`` is the option-string equivalent of ``null`` turn_detection
(JSON null can't be a UI dropdown value, so the schema uses ``none``)."""


@runtime_checkable
class _WebSocketLike(Protocol):
    """Minimal interface satisfied by ``websockets.asyncio.client.ClientConnection``.

    Tests inject a fake by overriding
    :meth:`OpenAIRealtimeS2S._open_connection` so no real socket is opened.
    """

    async def send(self, data: bytes | str) -> None: ...
    async def recv(self) -> bytes | str: ...
    async def close(self) -> None: ...


# ---- Provider -------------------------------------------------------------


class OpenAIRealtimeS2S(S2SProvider):
    """Speech-to-speech adapter for OpenAI's GA Realtime API.

    Required credentials:

    * ``api_key`` — OpenAI secret key. Sent as ``Authorization: Bearer``.

    Configuration ``options`` (any key may be omitted):

    * ``model`` — Realtime model name. Default ``"gpt-realtime-2"``.
      Other values: ``"gpt-realtime-mini"`` (cheaper), ``"gpt-realtime"``
      (legacy GA), or a dated model id for pinning.
    * ``voice_id`` — voice name. Default ``"marin"``. Any voice name
      OpenAI exposes is acceptable (the API rejects unknown values at
      session start).
    * ``turn_detection`` — VAD type: ``"server_vad"`` (default),
      ``"semantic_vad"``, or ``"none"`` to disable.
    * ``vad_threshold`` — server_vad sensitivity (0.0-1.0). Default 0.5.
    * ``vad_silence_duration_ms`` — ms of trailing silence before VAD
      commits a user turn. Default 500.
    * ``vad_prefix_padding_ms`` — ms of audio before speech onset to
      include in the committed buffer. Default 300.
    * ``interrupt_response`` — when True (default), server VAD cancels
      the in-flight response on fresh user speech (built-in barge-in).
    * ``base_url`` — WebSocket endpoint base. Defaults to OpenAI's
      ``wss://api.openai.com/v1/realtime``.
    * ``beta_header`` — value sent as ``OpenAI-Beta``. Default empty
      (GA API); only set this for proxies that still require the legacy
      ``"realtime=v1"`` value.
    * ``max_append_bytes`` — chunk size used to slice large audio
      buffers. Default 24 000 (~0.5 s @ 24 kHz). Must be even.
    * ``timeout_s`` — websocket open + ping timeout. Default 60 s.
    """

    def __init__(self, config: ProviderConfig) -> None:
        if config.kind is not ProviderKind.S2S:
            raise ValueError(
                f"OpenAIRealtimeS2S requires ProviderKind.S2S; got {config.kind.value}"
            )
        api_key = config.credentials.get("api_key")
        if not api_key:
            raise ValueError("OpenAIRealtimeS2S requires 'api_key' in credentials")
        self._api_key = str(api_key)
        opts = config.options
        self._model = str(opts.get("model") or DEFAULT_MODEL)
        self._voice = str(opts.get("voice_id") or opts.get("voice") or DEFAULT_VOICE)
        self._base_url = str(opts.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        beta_opt = opts.get("beta_header")
        self._beta_header = (
            str(beta_opt) if beta_opt is not None else DEFAULT_BETA_HEADER
        )
        turn_det_raw = opts.get("turn_detection")
        if turn_det_raw in (None, ""):
            self._turn_detection: str = DEFAULT_TURN_DETECTION_TYPE
        else:
            td = str(turn_det_raw)
            if td not in VALID_TURN_DETECTION_TYPES:
                raise ValueError(
                    f"turn_detection must be one of {VALID_TURN_DETECTION_TYPES}; "
                    f"got {td!r}"
                )
            self._turn_detection = td
        try:
            self._vad_threshold = float(
                opts.get("vad_threshold", DEFAULT_VAD_THRESHOLD)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"vad_threshold must be a float; got {opts.get('vad_threshold')!r}"
            ) from exc
        if not 0.0 <= self._vad_threshold <= 1.0:
            raise ValueError(
                f"vad_threshold must be in [0.0, 1.0]; got {self._vad_threshold}"
            )
        try:
            self._vad_prefix_padding_ms = int(
                opts.get("vad_prefix_padding_ms", DEFAULT_VAD_PREFIX_PADDING_MS)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "vad_prefix_padding_ms must be an integer; got "
                f"{opts.get('vad_prefix_padding_ms')!r}"
            ) from exc
        if self._vad_prefix_padding_ms < 0:
            raise ValueError(
                "vad_prefix_padding_ms must be >= 0; got "
                f"{self._vad_prefix_padding_ms}"
            )
        try:
            self._vad_silence_duration_ms = int(
                opts.get("vad_silence_duration_ms", DEFAULT_VAD_SILENCE_DURATION_MS)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "vad_silence_duration_ms must be an integer; got "
                f"{opts.get('vad_silence_duration_ms')!r}"
            ) from exc
        if self._vad_silence_duration_ms < 0:
            raise ValueError(
                "vad_silence_duration_ms must be >= 0; got "
                f"{self._vad_silence_duration_ms}"
            )
        self._interrupt_response = bool(opts.get("interrupt_response", True))
        raw_max_append = opts.get("max_append_bytes")
        if raw_max_append is None or raw_max_append == "":
            raw_max_append = DEFAULT_MAX_APPEND_BYTES
        try:
            max_append = int(raw_max_append)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"max_append_bytes must be an integer; got {raw_max_append!r}"
            ) from exc
        if max_append <= 0:
            raise ValueError(f"max_append_bytes must be positive; got {max_append}")
        if max_append % PCM_SAMPLE_WIDTH_BYTES:
            raise ValueError(
                f"max_append_bytes must be a multiple of {PCM_SAMPLE_WIDTH_BYTES}"
            )
        self._max_append_bytes = max_append
        self._timeout_s = float(opts.get("timeout_s") or DEFAULT_TIMEOUT_S)

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
    def base_url(self) -> str:
        return self._base_url

    @property
    def turn_detection(self) -> str:
        return self._turn_detection

    @property
    def vad_threshold(self) -> float:
        return self._vad_threshold

    @property
    def vad_prefix_padding_ms(self) -> int:
        return self._vad_prefix_padding_ms

    @property
    def vad_silence_duration_ms(self) -> int:
        return self._vad_silence_duration_ms

    @property
    def interrupt_response(self) -> bool:
        return self._interrupt_response

    @property
    def max_append_bytes(self) -> int:
        return self._max_append_bytes

    @classmethod
    def field_schema(cls) -> ProviderSchema:
        return ProviderSchema(
            kind=ProviderKind.S2S,
            provider_name=PROVIDER_NAME,
            display_name="OpenAI GPT-Realtime",
            summary=(
                "Unified speech-to-speech via OpenAI's GA Realtime API. "
                "Native audio I/O at 24 kHz, two flagship voices "
                "(marin/cedar), server-side VAD with built-in barge-in."
            ),
            signup_url="https://platform.openai.com/signup",
            fields=(
                FieldDef(
                    name="api_key",
                    label="API key",
                    type=FieldType.PASSWORD,
                    required=True,
                    secret=True,
                    placeholder="sk-...",
                    help_text="Get a key from platform.openai.com.",
                    signup_url="https://platform.openai.com/signup",
                    env_key="OPENAI_API_KEY",
                    group=FieldGroup.AUTH,
                ),
                FieldDef(
                    name="model",
                    label="Model",
                    type=FieldType.SELECT,
                    default=DEFAULT_MODEL,
                    options=(
                        FieldOption(
                            value="gpt-realtime-2",
                            label="gpt-realtime-2 (flagship, May 2026 GA)",
                        ),
                        FieldOption(
                            value="gpt-realtime-mini",
                            label="gpt-realtime-mini (cheaper)",
                        ),
                        FieldOption(
                            value="gpt-realtime",
                            label="gpt-realtime (Aug 2025 GA, legacy)",
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
                        "marin / cedar are the 2026 flagship voices "
                        "(Realtime-exclusive, highest quality). The "
                        "others are the legacy 4o-realtime catalog."
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="turn_detection",
                    label="Turn detection",
                    type=FieldType.SELECT,
                    default=DEFAULT_TURN_DETECTION_TYPE,
                    options=(
                        FieldOption(
                            value="server_vad",
                            label="server_vad (energy-based, fast)",
                        ),
                        FieldOption(
                            value="semantic_vad",
                            label="semantic_vad (model-driven, natural)",
                        ),
                        FieldOption(
                            value="none",
                            label="none (manual commit, push-to-talk)",
                        ),
                    ),
                    help_text=(
                        "server_vad: classic VAD on audio energy. "
                        "semantic_vad: the model itself decides when "
                        "the user is done. none: caller must call "
                        "commit_user_turn explicitly."
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="vad_threshold",
                    label="VAD threshold",
                    type=FieldType.NUMBER,
                    default=DEFAULT_VAD_THRESHOLD,
                    help_text=(
                        "0.0-1.0. Lower = more sensitive (cuts in faster "
                        "on quiet speech). 0.5 is the docs default."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="vad_silence_duration_ms",
                    label="VAD silence duration (ms)",
                    type=FieldType.NUMBER,
                    default=DEFAULT_VAD_SILENCE_DURATION_MS,
                    help_text=(
                        "How long the server waits after speech before "
                        "committing a turn. Raise for users who pause "
                        "naturally; lower for snappier turn-taking."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="vad_prefix_padding_ms",
                    label="VAD prefix padding (ms)",
                    type=FieldType.NUMBER,
                    default=DEFAULT_VAD_PREFIX_PADDING_MS,
                    help_text=(
                        "How many ms of audio BEFORE the detected "
                        "speech onset are included in the committed "
                        "buffer (captures the start of the utterance)."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="interrupt_response",
                    label="Interrupt response on barge-in",
                    type=FieldType.CHECKBOX,
                    default=True,
                    help_text=(
                        "When true and server VAD is on, fresh user "
                        "speech mid-response auto-cancels the "
                        "assistant's audio (built-in barge-in)."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="base_url",
                    label="WebSocket endpoint",
                    type=FieldType.URL,
                    default=DEFAULT_BASE_URL,
                    help_text=(
                        "Override only for proxied / Azure OpenAI / "
                        "legacy preview deployments."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="max_append_bytes",
                    label="Max audio chunk (bytes)",
                    type=FieldType.NUMBER,
                    default=DEFAULT_MAX_APPEND_BYTES,
                    help_text=(
                        "Cap one input_audio_buffer.append event at "
                        "this many wire-side PCM bytes. 24 000 "
                        "≈ 0.5 s @ 24 kHz S16LE."
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
                    topic="marin / cedar are the 2026 flagship voices",
                    body=(
                        "OpenAI's two newest voices are "
                        "Realtime-exclusive and rated highest quality. "
                        "``marin`` is warm and natural; ``cedar`` is "
                        "slightly deeper with a more authoritative "
                        "feel. Default to marin and let users override."
                    ),
                ),
                ProviderTip(
                    topic="Server VAD owns barge-in detection",
                    body=(
                        "With ``server_vad`` + ``interrupt_response`` "
                        "(both on by default), the server itself emits "
                        "``input_audio_buffer.speech_started`` within "
                        "~200 ms of fresh user voice and auto-cancels "
                        "the in-flight response. The pipeline's local "
                        "VAD path is redundant in unified mode."
                    ),
                ),
                ProviderTip(
                    topic="Wire rate is 24 kHz, pipeline is 16 kHz",
                    body=(
                        "The GA Realtime API rejects rates below "
                        "24 kHz. The adapter upsamples capture audio "
                        "16 → 24 kHz on the way out and downsamples "
                        "assistant audio 24 → 16 kHz on the way back, "
                        "keeping the rest of the pipeline at one rate."
                    ),
                ),
                ProviderTip(
                    topic="semantic_vad is more natural but slower",
                    body=(
                        "``semantic_vad`` lets the model decide when "
                        "the user is finished (so it tolerates "
                        "natural mid-sentence pauses without "
                        "interrupting). Adds ~100-200 ms vs "
                        "``server_vad`` on the turn boundary; pick "
                        "based on whether speed or politeness matters."
                    ),
                ),
                ProviderTip(
                    topic="Sessions cap at 30 minutes",
                    body=(
                        "OpenAI closes Realtime sessions at 30 min "
                        "with ``error.code=session_expired``. Long "
                        "meetings need a soft reconnect at ~25 min "
                        "(not implemented here yet — file a follow-up "
                        "if you need it)."
                    ),
                ),
                ProviderTip(
                    topic="gpt-realtime-mini for cost-sensitive ops",
                    body=(
                        "``gpt-realtime-mini`` is materially cheaper "
                        "per audio minute with a small drop in "
                        "instruction-following. Pick it for demos and "
                        "internal QA; flagship gpt-realtime-2 for "
                        "customer-facing flows."
                    ),
                ),
            ),
        )

    def _build_url(self) -> str:
        """Compose the WebSocket URL with the model as a query parameter."""
        separator = "&" if "?" in self._base_url else "?"
        return f"{self._base_url}{separator}model={self._model}"

    def _build_headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if self._beta_header:
            headers["OpenAI-Beta"] = self._beta_header
        return headers

    def _build_turn_detection(self) -> dict[str, Any] | None:
        """Build the ``audio.input.turn_detection`` block (or ``None``)."""
        if self._turn_detection == "none":
            return None
        td: dict[str, Any] = {
            "type": self._turn_detection,
            "create_response": True,
            "interrupt_response": self._interrupt_response,
        }
        if self._turn_detection == "server_vad":
            td["threshold"] = self._vad_threshold
            td["prefix_padding_ms"] = self._vad_prefix_padding_ms
            td["silence_duration_ms"] = self._vad_silence_duration_ms
        return td

    def _build_session_update(
        self,
        *,
        instructions: str,
        voice_id: str | None,
        tools: Sequence[ToolDefinition],
    ) -> dict[str, Any]:
        """Build the initial ``session.update`` envelope (post-GA shape).

        The GA reshape (May 2026) nested audio config under
        ``session.audio.input`` / ``session.audio.output`` and renamed
        ``modalities`` to ``output_modalities``. Sending the pre-GA flat
        keys yields "Unknown parameter: 'session.input_audio_format'".
        """
        voice = voice_id or self._voice
        audio_input: dict[str, Any] = {
            "format": {
                "type": "audio/pcm",
                "rate": WIRE_SAMPLE_RATE_HZ,
            },
            "turn_detection": self._build_turn_detection(),
        }
        audio_output: dict[str, Any] = {
            "format": {
                "type": "audio/pcm",
                "rate": WIRE_SAMPLE_RATE_HZ,
            },
            "voice": voice,
        }
        session: dict[str, Any] = {
            "type": "realtime",
            "model": self._model,
            "output_modalities": ["audio"],
            "audio": {
                "input": audio_input,
                "output": audio_output,
            },
        }
        if instructions:
            session["instructions"] = instructions
        if tools:
            session["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                }
                for t in tools
            ]
        return {"type": "session.update", "session": session}

    async def open_session(
        self,
        *,
        instructions: str = "",
        voice_id: str | None = None,
        tools: Sequence[ToolDefinition] = (),
    ) -> S2SSession:
        url = self._build_url()
        headers = self._build_headers()
        try:
            ws = await self._open_connection(url, headers)
        except Exception as exc:
            raise S2SError(
                f"openai-realtime WebSocket connect failed: {exc}"
            ) from exc

        update_payload = self._build_session_update(
            instructions=instructions,
            voice_id=voice_id,
            tools=tools,
        )
        try:
            await ws.send(json.dumps(update_payload))
        except Exception as exc:
            with contextlib.suppress(Exception):
                await ws.close()
            raise S2SError(
                f"openai-realtime session.update send failed: {exc}"
            ) from exc

        session = _OpenAIRealtimeS2SSession(
            ws=ws,
            max_append_bytes=self._max_append_bytes,
            manual_vad=self._turn_detection == "none",
        )
        await session.start()
        return session

    async def _open_connection(
        self, url: str, headers: Mapping[str, str]
    ) -> _WebSocketLike:
        """Open the Realtime WebSocket. Overridable in tests."""
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


class _OpenAIRealtimeS2SSession(S2SSession):
    """One live Realtime WebSocket connection in S2S mode.

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
        max_append_bytes: int,
        manual_vad: bool = False,
    ) -> None:
        self._ws = ws
        self._max_append_bytes = max_append_bytes
        self._manual_vad = manual_vad
        self._queue: asyncio.Queue[S2SEvent | None] = asyncio.Queue()
        self._send_lock = asyncio.Lock()
        self._read_task: asyncio.Task[None] | None = None
        self._closed = False
        self._response_started = False
        # Accumulators for fragmented transcript / function-call events.
        self._assistant_transcript_buf: dict[str, str] = {}
        self._user_transcript_buf: dict[str, str] = {}
        self._function_call_buf: dict[str, dict[str, str]] = {}

    async def start(self) -> None:
        """Spawn the background read task draining the WebSocket."""
        if self._read_task is not None:
            return
        self._read_task = asyncio.create_task(self._read_loop())

    async def send_audio(self, pcm: bytes) -> None:
        if self._closed:
            raise S2SError("send_audio on a closed openai-realtime session")
        if not pcm:
            return
        if len(pcm) % PCM_SAMPLE_WIDTH_BYTES:
            raise S2SError(
                f"audio chunk {len(pcm)} bytes is not aligned to "
                f"{PCM_SAMPLE_WIDTH_BYTES}-byte S16 samples"
            )
        # Upsample 16 kHz pipeline PCM → 24 kHz wire PCM.
        try:
            wire = resample_pcm16(pcm, PIPELINE_SAMPLE_RATE_HZ, WIRE_SAMPLE_RATE_HZ)
        except Exception as exc:
            raise S2SError(
                f"openai-realtime resample {PIPELINE_SAMPLE_RATE_HZ} "
                f"→ {WIRE_SAMPLE_RATE_HZ} failed: {exc}"
            ) from exc
        for slice_ in _split_pcm(wire, self._max_append_bytes):
            payload = {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(slice_).decode("ascii"),
            }
            await self._send_json(payload)

    async def commit_user_turn(self) -> None:
        """Signal end of the user's current turn.

        With server VAD on (default), the server auto-commits when it
        detects silence; this method's ``commit`` event is then a
        safety net (the API accepts a duplicate commit gracefully —
        empty buffers yield a "buffer too small" error, which we map
        to a debug log instead of an exception since the VAD will have
        already kicked off a response).

        With manual VAD (``turn_detection=none``), this method also
        sends ``response.create`` so the model actually replies.
        """
        if self._closed:
            raise S2SError("commit_user_turn on a closed openai-realtime session")
        try:
            await self._send_json({"type": "input_audio_buffer.commit"})
        except S2SError:
            raise
        if self._manual_vad:
            await self._send_json({"type": "response.create"})

    async def events(self) -> AsyncIterator[S2SEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    async def interrupt(self) -> None:
        """Cancel the current assistant response, if any.

        Sends ``response.cancel`` (cancels the in-flight response) and
        ``input_audio_buffer.clear`` (drops any pending uncommitted
        user audio so the next turn starts clean). Failures are
        swallowed (logged) because the pipeline-level interrupt path
        runs from a race-y context where raising would block the
        user's next turn.
        """
        if self._closed:
            return
        try:
            await self._send_json({"type": "response.cancel"})
        except Exception:  # noqa: BLE001 — log + swallow
            logger.exception("openai-realtime response.cancel failed")
        try:
            await self._send_json({"type": "input_audio_buffer.clear"})
        except Exception:  # noqa: BLE001 — log + swallow
            logger.exception("openai-realtime input_audio_buffer.clear failed")

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
        # Stamp an event_id for every outbound message so server errors
        # echo back with the same id for correlation (per GA spec).
        if "event_id" not in payload:
            payload["event_id"] = f"evt_{uuid.uuid4().hex[:24]}"
        try:
            data = json.dumps(payload)
        except (TypeError, ValueError) as exc:
            raise S2SError(
                f"openai-realtime failed to encode message: {exc}"
            ) from exc
        async with self._send_lock:
            try:
                await self._ws.send(data)
            except Exception as exc:
                raise S2SError(
                    f"openai-realtime websocket send failed: {exc}"
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
                        "openai-realtime websocket recv error: %s", exc
                    )
                    break
                for event in self._parse_server_message(message):
                    if isinstance(event, _ResponseEnded):
                        await self._queue.put(
                            S2SResponseCompleted(
                                finish_reason=event.finish_reason,
                            )
                        )
                        self._response_started = False
                        continue
                    if isinstance(event, _ResponseStartedMarker):
                        if not self._response_started:
                            self._response_started = True
                            await self._queue.put(S2SResponseStarted())
                        continue
                    if isinstance(event, S2SAudioFrame | S2STranscript):
                        if not self._response_started and isinstance(
                            event, S2SAudioFrame
                        ):
                            # First audio frame implicitly starts a response
                            # even if response.created wasn't emitted yet.
                            self._response_started = True
                            await self._queue.put(S2SResponseStarted())
                    await self._queue.put(event)
        finally:
            # Ensure events() can return even when the socket dropped
            # without an explicit close from our side.
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(None)

    def _parse_server_message(
        self, message: bytes | str
    ) -> list[S2SEvent | _ResponseEnded | _ResponseStartedMarker]:
        """Parse one Realtime server event into S2S events."""
        try:
            payload = _decode_json(message)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.debug("openai-realtime emitted non-JSON message; ignoring")
            return []
        if not isinstance(payload, Mapping):
            return []
        msg_type = payload.get("type")
        if not isinstance(msg_type, str):
            return []

        out: list[S2SEvent | _ResponseEnded | _ResponseStartedMarker] = []

        if msg_type in {"session.created", "session.updated"}:
            return out  # Acks; nothing to surface.

        if msg_type == "response.created":
            out.append(_ResponseStartedMarker())
            return out

        if msg_type == "response.output_audio.delta":
            audio_frame = _parse_audio_delta(payload)
            if audio_frame is not None:
                out.append(audio_frame)
            return out

        if msg_type == "response.output_audio.done":
            # End of audio stream for this item — not the end of the
            # response (transcripts may still follow). The terminating
            # response.done event drives the actual completion signal.
            return out

        if msg_type == "response.output_audio_transcript.delta":
            text = _parse_string_delta(payload)
            if text:
                item_id = str(payload.get("item_id") or "")
                self._assistant_transcript_buf[item_id] = (
                    self._assistant_transcript_buf.get(item_id, "") + text
                )
                out.append(
                    S2STranscript(text=text, is_final=False, role="assistant")
                )
            return out

        if msg_type == "response.output_audio_transcript.done":
            item_id = str(payload.get("item_id") or "")
            transcript_raw = payload.get("transcript")
            full_text = (
                str(transcript_raw).strip()
                if isinstance(transcript_raw, str) and transcript_raw
                else self._assistant_transcript_buf.get(item_id, "").strip()
            )
            self._assistant_transcript_buf.pop(item_id, None)
            if full_text:
                out.append(
                    S2STranscript(
                        text=full_text, is_final=True, role="assistant"
                    )
                )
            return out

        if (
            msg_type
            == "conversation.item.input_audio_transcription.completed"
        ):
            transcript_raw = payload.get("transcript")
            text = (
                str(transcript_raw).strip()
                if isinstance(transcript_raw, str)
                else ""
            )
            if text:
                out.append(
                    S2STranscript(text=text, is_final=True, role="user")
                )
            return out

        if (
            msg_type
            == "conversation.item.input_audio_transcription.delta"
        ):
            text = _parse_string_delta(payload)
            if text:
                out.append(
                    S2STranscript(text=text, is_final=False, role="user")
                )
            return out

        if msg_type == "response.function_call_arguments.delta":
            call_id = str(payload.get("call_id") or "")
            delta = _parse_string_delta(payload)
            if call_id and delta:
                buf = self._function_call_buf.setdefault(
                    call_id,
                    {
                        "name": str(payload.get("name") or ""),
                        "arguments": "",
                        "id": call_id,
                    },
                )
                buf["arguments"] = buf.get("arguments", "") + delta
                # Capture name if it arrives on a later delta only.
                name_field = payload.get("name")
                if isinstance(name_field, str) and name_field:
                    buf["name"] = name_field
            return out

        if msg_type == "response.function_call_arguments.done":
            tool_event = self._finalise_function_call(payload)
            if tool_event is not None:
                out.append(tool_event)
            return out

        if msg_type == "input_audio_buffer.speech_started":
            # Server VAD detected fresh user speech. The pipeline-level
            # barge-in is owned by the unified pipeline's interrupt()
            # path; surfacing this as a ResponseEnded(interrupted) lets
            # the events loop persist the in-flight assistant audio
            # with the right finish_reason and stop accumulating new
            # frames. The server itself sends a response.done with
            # status=cancelled shortly after, but emitting the marker
            # eagerly avoids holding the queue until then.
            if self._response_started:
                out.append(_ResponseEnded(finish_reason="interrupted"))
            return out

        if msg_type == "response.done":
            response = payload.get("response")
            finish_reason = "stop"
            if isinstance(response, Mapping):
                status_raw = response.get("status")
                status = (
                    str(status_raw).strip().lower()
                    if isinstance(status_raw, str)
                    else ""
                )
                if status == "cancelled":
                    finish_reason = "interrupted"
                elif status == "failed":
                    finish_reason = "error"
                elif status == "incomplete":
                    finish_reason = "incomplete"
            out.append(_ResponseEnded(finish_reason=finish_reason))
            return out

        if msg_type == "error":
            error = payload.get("error")
            message_text = ""
            if isinstance(error, Mapping):
                message_text = str(
                    error.get("message") or error.get("code") or ""
                )
            logger.warning(
                "openai-realtime server reported error: %s", message_text
            )
            if self._response_started:
                out.append(_ResponseEnded(finish_reason="error"))
            return out

        # All other event types (rate-limits, session.expired, etc.) are
        # logged at debug for forward compatibility but not surfaced.
        logger.debug(
            "openai-realtime ignoring untracked event type=%s", msg_type
        )
        return out

    def _finalise_function_call(
        self, payload: Mapping[str, Any]
    ) -> S2SToolCall | None:
        """Build an :class:`S2SToolCall` from a function_call_arguments.done."""
        call_id = str(payload.get("call_id") or "")
        if not call_id:
            return None
        buf = self._function_call_buf.pop(call_id, None) or {}
        name = str(payload.get("name") or buf.get("name") or "")
        if not name:
            logger.warning(
                "openai-realtime dropped function_call without name "
                "for call_id=%s",
                call_id,
            )
            return None
        arguments_raw = payload.get("arguments")
        if not isinstance(arguments_raw, str) or not arguments_raw:
            arguments_raw = buf.get("arguments", "")
        try:
            arguments: dict[str, Any] = (
                json.loads(arguments_raw) if arguments_raw else {}
            )
        except json.JSONDecodeError:
            logger.warning(
                "openai-realtime function_call args not JSON "
                "for call_id=%s; passing raw string under '_raw'",
                call_id,
            )
            arguments = {"_raw": arguments_raw}
        if not isinstance(arguments, Mapping):
            arguments = {"_raw": str(arguments_raw)}
        return S2SToolCall(id=call_id, name=name, arguments=dict(arguments))


# ---- Parser helpers -------------------------------------------------------


class _ResponseEnded:
    """Marker the read loop uses to bubble up turn-end with finish_reason."""

    __slots__ = ("finish_reason",)

    def __init__(self, finish_reason: str) -> None:
        self.finish_reason = finish_reason


class _ResponseStartedMarker:
    """Marker the read loop uses for ``response.created`` (no payload)."""

    __slots__ = ()


def _parse_audio_delta(payload: Mapping[str, Any]) -> S2SAudioFrame | None:
    """Decode + downsample a base64 PCM delta from response.output_audio.delta."""
    delta_raw = payload.get("delta")
    if not isinstance(delta_raw, str) or not delta_raw:
        return None
    try:
        pcm_wire = base64.b64decode(delta_raw)
    except (ValueError, TypeError) as exc:
        logger.warning("openai-realtime failed to b64-decode audio: %s", exc)
        return None
    if not pcm_wire:
        return None
    try:
        pcm = resample_pcm16(
            pcm_wire, WIRE_SAMPLE_RATE_HZ, PIPELINE_SAMPLE_RATE_HZ
        )
    except Exception as exc:  # noqa: BLE001 — degrade gracefully
        logger.warning(
            "openai-realtime failed to resample %d Hz → %d Hz: %s",
            WIRE_SAMPLE_RATE_HZ,
            PIPELINE_SAMPLE_RATE_HZ,
            exc,
        )
        return None
    return S2SAudioFrame(pcm=pcm)


def _parse_string_delta(payload: Mapping[str, Any]) -> str:
    """Extract a non-empty string ``delta`` field from a server event."""
    raw = payload.get("delta")
    if not isinstance(raw, str):
        return ""
    return raw


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
    """Register :class:`OpenAIRealtimeS2S` under ``(ProviderKind.S2S, "openai-realtime")``.

    Idempotent when ``replace=True``. Called at import time from
    :mod:`app.providers` so the global registry contains
    ``openai-realtime`` (S2S) by the time API startup runs.
    """
    get_registry().register(
        ProviderKind.S2S, PROVIDER_NAME, OpenAIRealtimeS2S, replace=replace
    )


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_BETA_HEADER",
    "DEFAULT_MAX_APPEND_BYTES",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_TURN_DETECTION_TYPE",
    "DEFAULT_VAD_PREFIX_PADDING_MS",
    "DEFAULT_VAD_SILENCE_DURATION_MS",
    "DEFAULT_VAD_THRESHOLD",
    "DEFAULT_VOICE",
    "OpenAIRealtimeS2S",
    "PIPELINE_SAMPLE_RATE_HZ",
    "PREBUILT_VOICES",
    "PROVIDER_NAME",
    "VALID_TURN_DETECTION_TYPES",
    "WIRE_SAMPLE_RATE_HZ",
    "register",
]
