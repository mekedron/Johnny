"""ElevenLabs Scribe speech-to-text adapter.

Calls ``POST /v1/speech-to-text`` (the Scribe batch endpoint) with
``file_format=pcm_s16le_16`` so raw 16 kHz mono S16LE PCM goes over the
wire without WAV-wrapping. The pipeline already chops audio into
VAD-bounded utterances before handing it to STT; ``transcribe_stream``
buffers the iterator into a single utterance, POSTs it, and yields one
final :class:`TranscriptEvent` carrying the returned ``text``.

ElevenLabs Scribe is batch-only — there is no streaming/partial transcript
surface today, so this adapter never emits ``is_final=False`` deltas. The
post-VAD utterance boundary is the only finality signal.

Latency profile: the synchronous endpoint returns once the full clip is
transcribed; for short utterances (~1-3 s) round-trip is typically
~300-800 ms end-to-end.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator

import httpx

from app.providers.base import (
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
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "elevenlabs"
DEFAULT_BASE_URL = "https://api.elevenlabs.io/v1"
DEFAULT_MODEL_ID = "scribe_v2"
# ``pcm_s16le_16`` tells Scribe the body is raw 16 kHz mono S16LE PCM —
# matches the meet-worker audio bridge format, no transcoding needed.
DEFAULT_FILE_FORMAT = "pcm_s16le_16"
DEFAULT_LANGUAGE_CODE: str | None = None
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_TAG_AUDIO_EVENTS = False
DEFAULT_DIARIZE = False


class ElevenLabsSTT(STTProvider):
    """Batch STT via ElevenLabs' Scribe ``/v1/speech-to-text`` endpoint.

    Required credentials:

    * ``api_key`` — the ElevenLabs API key (sent as ``xi-api-key``).

    Configuration ``options`` (any key may be omitted):

    * ``model_id`` — Scribe model. Defaults to ``scribe_v2`` (recommended);
      ``scribe_v1`` is the older variant.
    * ``language_code`` — ISO-639-1/3 language hint (e.g. ``"eng"``).
      Leave blank to auto-detect.
    * ``base_url`` — API base URL. Defaults to ElevenLabs' public endpoint.
    * ``file_format`` — must be ``pcm_s16le_16`` for the raw PCM the bridge
      ships. Override only if you proxy through a transcoder.
    * ``tag_audio_events`` — set true to mark non-speech audio events
      (``[laughter]`` etc.) inline. Default false.
    * ``diarize`` — enable speaker diarization. Default false; the
      voice pipeline already labels speakers from the meet roster.
    * ``timeout_s`` — request timeout in seconds. Default 60.
    """

    def __init__(self, config: ProviderConfig) -> None:
        if config.kind is not ProviderKind.STT:
            raise ValueError(
                f"ElevenLabsSTT requires ProviderKind.STT; got {config.kind.value}"
            )
        api_key = config.credentials.get("api_key")
        if not api_key:
            raise ValueError("ElevenLabsSTT requires 'api_key' in credentials")
        self._api_key = str(api_key)
        opts = config.options
        self._model_id = str(opts.get("model_id") or DEFAULT_MODEL_ID)
        language = opts.get("language_code")
        self._language_code: str | None = (
            str(language) if language not in (None, "") else DEFAULT_LANGUAGE_CODE
        )
        self._base_url = str(opts.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self._file_format = str(opts.get("file_format") or DEFAULT_FILE_FORMAT)
        self._tag_audio_events = bool(
            opts.get("tag_audio_events", DEFAULT_TAG_AUDIO_EVENTS)
        )
        self._diarize = bool(opts.get("diarize", DEFAULT_DIARIZE))
        self._timeout_s = float(opts.get("timeout_s") or DEFAULT_TIMEOUT_S)
        self._client = self._create_client()

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @classmethod
    def field_schema(cls) -> ProviderSchema:
        return ProviderSchema(
            kind=ProviderKind.STT,
            provider_name=PROVIDER_NAME,
            display_name="ElevenLabs",
            summary="ElevenLabs Scribe batch STT. High accuracy; no streaming partials.",
            signup_url="https://elevenlabs.io/sign-up",
            fields=(
                FieldDef(
                    name="api_key",
                    label="API key",
                    type=FieldType.PASSWORD,
                    required=True,
                    secret=True,
                    placeholder="sk_...",
                    help_text="Get a key from elevenlabs.io.",
                    signup_url="https://elevenlabs.io/sign-up",
                    env_key="ELEVENLABS_API_KEY",
                    group=FieldGroup.AUTH,
                ),
                FieldDef(
                    name="model_id",
                    label="Model",
                    type=FieldType.SELECT,
                    default=DEFAULT_MODEL_ID,
                    options=(
                        FieldOption(value="scribe_v2", label="scribe_v2 (recommended)"),
                        FieldOption(value="scribe_v1", label="scribe_v1"),
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="language_code",
                    label="Language",
                    placeholder="eng",
                    help_text="ISO-639-1/3 language code. Leave blank to auto-detect.",
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="diarize",
                    label="Diarize speakers",
                    type=FieldType.CHECKBOX,
                    default=DEFAULT_DIARIZE,
                    help_text="Identify speakers in the transcript.",
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="tag_audio_events",
                    label="Tag audio events",
                    type=FieldType.CHECKBOX,
                    default=DEFAULT_TAG_AUDIO_EVENTS,
                    help_text="Inline tags like [laughter], [applause] in the text.",
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="base_url",
                    label="API base URL",
                    type=FieldType.URL,
                    default=DEFAULT_BASE_URL,
                    help_text="Override only for proxied deployments.",
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="file_format",
                    label="Audio file format",
                    default=DEFAULT_FILE_FORMAT,
                    help_text="Must be pcm_s16le_16 for the meet-worker audio bridge.",
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
        )

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def language_code(self) -> str | None:
        return self._language_code

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def file_format(self) -> str:
        return self._file_format

    @property
    def tag_audio_events(self) -> bool:
        return self._tag_audio_events

    @property
    def diarize(self) -> bool:
        return self._diarize

    def _create_client(self) -> httpx.AsyncClient:
        """Build the underlying HTTP client. Overridable in tests."""
        return httpx.AsyncClient(timeout=self._timeout_s)

    async def transcribe_stream(
        self,
        audio_iter: AsyncIterator[bytes],
    ) -> AsyncIterator[TranscriptEvent]:
        """Buffer the utterance, POST to Scribe, yield one final event.

        ElevenLabs Scribe is batch-only — the entire VAD-bounded utterance
        goes over the wire in one multipart request. Empty iterators are a
        no-op; no HTTP call is made.
        """
        buffer = bytearray()
        async for chunk in audio_iter:
            if not chunk:
                continue
            if len(chunk) % PCM_SAMPLE_WIDTH_BYTES:
                raise STTError(
                    f"audio chunk {len(chunk)} bytes is not aligned to "
                    f"{PCM_SAMPLE_WIDTH_BYTES}-byte S16 samples"
                )
            buffer.extend(chunk)
        if not buffer:
            return

        url = f"{self._base_url}/speech-to-text"
        headers = {
            "xi-api-key": self._api_key,
            "Accept": "application/json",
        }
        data: dict[str, str] = {
            "model_id": self._model_id,
            "file_format": self._file_format,
            "tag_audio_events": "true" if self._tag_audio_events else "false",
            "diarize": "true" if self._diarize else "false",
        }
        if self._language_code is not None:
            data["language_code"] = self._language_code
        files = {
            # ``application/octet-stream`` is correct for raw PCM; the
            # server keys off ``file_format=pcm_s16le_16`` not the MIME.
            "file": ("audio.pcm", bytes(buffer), "application/octet-stream"),
        }

        try:
            response = await self._client.post(
                url, data=data, files=files, headers=headers
            )
        except httpx.HTTPError as exc:
            raise STTError(f"elevenlabs STT request failed: {exc}") from exc

        await self._raise_for_status(response)
        event = _parse_response(response)
        if event is not None:
            yield event

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.aclose()

    async def _raise_for_status(self, response: httpx.Response) -> None:
        """Translate non-2xx responses into :class:`STTError`."""
        if response.is_success:
            return
        try:
            body_bytes = await response.aread()
        except httpx.HTTPError:
            body_bytes = b""
        detail = ""
        if body_bytes:
            with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
                payload = json.loads(body_bytes.decode("utf-8"))
                if isinstance(payload, dict):
                    err = payload.get("detail")
                    if isinstance(err, dict) and "message" in err:
                        detail = str(err["message"])
                    elif isinstance(err, str):
                        detail = err
                    elif "message" in payload:
                        detail = str(payload["message"])
            if not detail:
                with contextlib.suppress(UnicodeDecodeError):
                    detail = body_bytes.decode("utf-8")[:200]
        raise STTError(
            f"elevenlabs STT HTTP {response.status_code}"
            + (f": {detail}" if detail else "")
        )


def _parse_response(response: httpx.Response) -> TranscriptEvent | None:
    """Turn a Scribe success response into a final TranscriptEvent.

    Returns ``None`` for empty / whitespace-only ``text`` so silence-only
    utterances don't surface as empty transcripts.
    """
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        raise STTError("elevenlabs STT returned non-JSON response") from None
    if not isinstance(payload, dict):
        raise STTError(
            f"elevenlabs STT response was not an object (got {type(payload).__name__})"
        )
    text_raw = payload.get("text", "")
    if not isinstance(text_raw, str):
        return None
    text = text_raw.strip()
    if not text:
        return None
    probability = payload.get("language_probability")
    confidence: float | None = None
    if isinstance(probability, int | float):
        confidence = max(0.0, min(1.0, float(probability)))
    return TranscriptEvent(
        text=text,
        is_final=True,
        timestamp_ms=0,
        confidence=confidence,
    )


def register(*, replace: bool = False) -> None:
    """Register :class:`ElevenLabsSTT` under ``(ProviderKind.STT, "elevenlabs")``.

    Idempotent when ``replace=True``. Called at import time from
    :mod:`app.providers` so the global registry contains ``elevenlabs``
    STT by the time API startup runs.
    """
    get_registry().register(
        ProviderKind.STT, PROVIDER_NAME, ElevenLabsSTT, replace=replace
    )


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_DIARIZE",
    "DEFAULT_FILE_FORMAT",
    "DEFAULT_LANGUAGE_CODE",
    "DEFAULT_MODEL_ID",
    "DEFAULT_TAG_AUDIO_EVENTS",
    "DEFAULT_TIMEOUT_S",
    "ElevenLabsSTT",
    "PROVIDER_NAME",
    "register",
]
