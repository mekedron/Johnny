"""ElevenLabs text-to-speech streaming adapter.

Calls ``POST /v1/text-to-speech/{voice_id}/stream`` with
``output_format=pcm_16000`` so the response is already 16 kHz mono S16LE
PCM — no resampling required to slot into the meet-worker audio bridge.

Latency profile: ElevenLabs' "Flash v2.5" models advertise <200 ms
time-to-first-byte for short prompts. The chunked HTTP body streams as
the synthesis runs so playback can start before completion.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.providers.base import (
    PCM_SAMPLE_WIDTH_BYTES,
    ProviderConfig,
    ProviderKind,
    TTSError,
    TTSErrorCategory,
    TTSProvider,
    VoiceMeta,
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

PROVIDER_NAME = "elevenlabs"
DEFAULT_BASE_URL = "https://api.elevenlabs.io/v1"
DEFAULT_MODEL_ID = "eleven_multilingual_v2"
# pcm_16000 returns 16 kHz mono S16LE PCM — matches the bridge format.
DEFAULT_OUTPUT_FORMAT = "pcm_16000"
DEFAULT_CHUNK_BYTES = 4_096
DEFAULT_TIMEOUT_S = 30.0


def _voice_meta_from_entry(entry: Any) -> VoiceMeta | None:
    """Map one ``GET /v1/voices`` entry to the unified :class:`VoiceMeta`.

    Returns ``None`` for malformed entries so the catalog never carries a
    half-populated row. ElevenLabs serves at the requested output rate
    (``pcm_16000``), so ``sample_rate`` is left unset; ``language`` is the
    best-effort accent label, ``tier`` the voice category (premade /
    cloned / professional). Cloud voices are always ``installed=True``.
    """
    if not isinstance(entry, dict):
        return None
    voice_id = entry.get("voice_id")
    name = entry.get("name")
    if not isinstance(voice_id, str) or not voice_id:
        return None
    labels = entry.get("labels")
    labels = labels if isinstance(labels, dict) else {}
    accent = labels.get("language") or labels.get("accent")
    gender = labels.get("gender")
    category = entry.get("category")
    preview = entry.get("preview_url")
    return VoiceMeta(
        id=voice_id,
        label=str(name or voice_id),
        language=str(accent).title() if accent else None,
        sample_rate=None,
        gender=str(gender).lower() if gender else None,
        preview_url=str(preview) if isinstance(preview, str) and preview else None,
        installed=True,
        tier=str(category) if category else None,
    )


async def fetch_voice_catalog(
    api_key: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = 15.0,
) -> list[VoiceMeta]:
    """Return every voice on the account via ``GET /v1/voices``.

    Maps each entry to the shared :class:`VoiceMeta` so the unified voice
    picker renders ElevenLabs identically to the local providers. Raises
    :class:`TTSError` with the API's diagnostic on transport failure so the
    endpoint can surface it as-is (and the picker falls back to free-text).
    """
    if not api_key:
        raise TTSError("elevenlabs voice catalog requires an api_key")
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout_s)
    headers = {"xi-api-key": api_key, "Accept": "application/json"}
    url = f"{base_url.rstrip('/')}/voices"
    try:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise TTSError(
                f"failed to fetch elevenlabs voice catalog: {exc}"
            ) from exc
        except ValueError as exc:
            raise TTSError(
                f"elevenlabs voice catalog is not valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise TTSError("elevenlabs voice catalog payload is not a JSON object")
        raw = payload.get("voices")
        if not isinstance(raw, list):
            raise TTSError("elevenlabs voice catalog payload missing 'voices' array")
        voices = [m for m in (_voice_meta_from_entry(e) for e in raw) if m is not None]
        voices.sort(key=lambda v: ((v.language or "").lower(), v.label.lower()))
        return voices
    finally:
        if owns_client:
            await client.aclose()


class ElevenLabsTTS(TTSProvider):
    """Streaming TTS via ElevenLabs' ``/text-to-speech/{voice_id}/stream`` endpoint.

    Required credentials:

    * ``api_key`` — the ElevenLabs API key (sent as ``xi-api-key``).

    Configuration ``options`` (any key may be omitted):

    * ``voice_id`` — default voice ID (e.g. ``"21m00Tcm4TlvDq8ikWAM"``).
      Required either here or per-call as ``synthesize_stream(voice_id=...)``.
    * ``model_id`` — ElevenLabs model. Defaults to ``eleven_multilingual_v2``;
      ``eleven_flash_v2_5`` gives lower latency for English.
    * ``base_url`` — API base URL. Defaults to ElevenLabs' public endpoint.
    * ``output_format`` — must be a ``pcm_16000`` value to match the
      bridge format. Override only if you know what you're doing.
    * ``voice_settings`` — dict of voice control fields (``stability``,
      ``similarity_boost``, ``style``, ``use_speaker_boost``). Forwarded
      verbatim to the API.
    * ``chunk_bytes`` — read chunk size for the streamed body. Must be a
      multiple of the 2-byte S16 sample width. Default 4096.
    * ``timeout_s`` — request timeout in seconds. Default 30.
    """

    def __init__(self, config: ProviderConfig) -> None:
        if config.kind is not ProviderKind.TTS:
            raise ValueError(
                f"ElevenLabsTTS requires ProviderKind.TTS; got {config.kind.value}"
            )
        api_key = config.credentials.get("api_key")
        if not api_key:
            raise ValueError("ElevenLabsTTS requires 'api_key' in credentials")
        self._api_key = str(api_key)
        opts = config.options
        voice_id = opts.get("voice_id")
        self._default_voice_id: str | None = (
            str(voice_id) if voice_id not in (None, "") else None
        )
        self._model_id = str(opts.get("model_id") or DEFAULT_MODEL_ID)
        self._base_url = str(opts.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self._output_format = str(opts.get("output_format") or DEFAULT_OUTPUT_FORMAT)
        voice_settings = opts.get("voice_settings")
        self._voice_settings: dict[str, Any] | None = (
            dict(voice_settings) if isinstance(voice_settings, dict) else None
        )
        chunk_bytes = int(opts.get("chunk_bytes") or DEFAULT_CHUNK_BYTES)
        if chunk_bytes <= 0:
            raise ValueError(f"chunk_bytes must be positive; got {chunk_bytes}")
        if chunk_bytes % PCM_SAMPLE_WIDTH_BYTES:
            raise ValueError(
                f"chunk_bytes must be a multiple of {PCM_SAMPLE_WIDTH_BYTES}"
            )
        self._chunk_bytes = chunk_bytes
        self._timeout_s = float(opts.get("timeout_s") or DEFAULT_TIMEOUT_S)
        self._client = self._create_client()

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @classmethod
    def field_schema(cls) -> ProviderSchema:
        return ProviderSchema(
            kind=ProviderKind.TTS,
            provider_name=PROVIDER_NAME,
            display_name="ElevenLabs",
            summary="Highest-quality cloud voices with fast streaming.",
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
                    name="voice_id",
                    label="Voice ID",
                    required=True,
                    voice_catalog=True,
                    placeholder="EXAVITQu4vr4xnSDxMaL",
                    help_text=(
                        "Browse your account's voices with the picker below, or "
                        "paste any ID from elevenlabs.io/app/voice-library."
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="model_id",
                    label="Model",
                    type=FieldType.SELECT,
                    default=DEFAULT_MODEL_ID,
                    options=(
                        FieldOption(
                            value="eleven_multilingual_v2", label="eleven_multilingual_v2"
                        ),
                        FieldOption(
                            value="eleven_flash_v2_5",
                            label="eleven_flash_v2_5 (low latency EN)",
                        ),
                        FieldOption(value="eleven_turbo_v2_5", label="eleven_turbo_v2_5"),
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="output_format",
                    label="Output format",
                    default=DEFAULT_OUTPUT_FORMAT,
                    help_text="Must be a pcm_16000 variant for the audio bridge.",
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="base_url",
                    label="API base URL",
                    type=FieldType.URL,
                    default=DEFAULT_BASE_URL,
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="chunk_bytes",
                    label="Read chunk bytes",
                    type=FieldType.NUMBER,
                    default=DEFAULT_CHUNK_BYTES,
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
                    topic="Flash v2.5 is the latency winner for English",
                    body=(
                        "eleven_flash_v2_5 starts streaming in ~150-"
                        "300 ms from EU/US — the fastest cloud TTS "
                        "available. English-only and slightly less "
                        "expressive than Multilingual v2, but for "
                        "live meetings the latency win is decisive. "
                        "Pick Multilingual v2 only when you need a "
                        "non-English voice."
                    ),
                ),
                ProviderTip(
                    topic="Output format must be pcm_16000",
                    body=(
                        "The audio bridge expects 16 kHz raw PCM. "
                        "Defaults already do this; if you set "
                        "something else (mp3 / opus / pcm_22050) "
                        "the bot's voice will sound chipmunk-fast "
                        "or fail entirely. Keep pcm_16000."
                    ),
                ),
                ProviderTip(
                    topic="Voice library — pick voices with low 'stability' for warmth",
                    body=(
                        "ElevenLabs voice stability settings are "
                        "baked into the voice in the library. Voices "
                        "tagged 'natural' or 'conversational' work "
                        "better for meetings than 'narration' "
                        "voices which can sound stilted in short "
                        "back-and-forth."
                    ),
                ),
                ProviderTip(
                    topic="Costs scale with characters synthesized",
                    body=(
                        "ElevenLabs bills per character. A typical "
                        "bot turn (~80 chars) costs roughly $0.001 "
                        "on Flash, $0.003 on Multilingual v2. A "
                        "long meeting can run to dollars; watch your "
                        "monthly cap."
                    ),
                ),
            ),
        )

    @property
    def default_voice_id(self) -> str | None:
        return self._default_voice_id

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def output_format(self) -> str:
        return self._output_format

    @property
    def chunk_bytes(self) -> int:
        return self._chunk_bytes

    async def list_voices(self) -> tuple[VoiceMeta, ...]:
        """Return the account's voice catalog (Johnny-1ge.9).

        Hits ``GET /v1/voices`` with the configured key and reuses the
        adapter's own HTTP client so the unified picker shows the same
        language / gender / preview metadata it shows for the local
        providers. A keyless add-modal can't reach this (``__init__``
        requires the key) and the picker falls back to free-text entry.
        """
        return tuple(
            await fetch_voice_catalog(
                self._api_key, base_url=self._base_url, client=self._client
            )
        )

    def _create_client(self) -> httpx.AsyncClient:
        """Build the underlying HTTP client. Overridable in tests."""
        return httpx.AsyncClient(timeout=self._timeout_s)

    async def synthesize_stream(
        self,
        text: str,
        voice_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        """Synthesize ``text`` to 16 kHz mono S16LE PCM frames.

        The voice ID is embedded in the URL path; pass ``voice_id`` to
        override the configured default. Raises :class:`TTSError` if no
        voice is available.
        """
        voice = voice_id if voice_id not in (None, "") else self._default_voice_id
        if not voice:
            raise TTSError(
                "ElevenLabsTTS requires a voice_id; pass it per-call or set "
                "voice_id in the provider configuration."
            )
        url = f"{self._base_url}/text-to-speech/{voice}/stream"
        params = {"output_format": self._output_format}
        body: dict[str, Any] = {
            "text": text,
            "model_id": self._model_id,
        }
        if self._voice_settings is not None:
            body["voice_settings"] = dict(self._voice_settings)
        headers = {
            "xi-api-key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/octet-stream",
        }
        try:
            async with self._client.stream(
                "POST", url, json=body, headers=headers, params=params
            ) as response:
                await self._raise_for_status(response)
                carry = b""
                async for chunk in response.aiter_bytes(self._chunk_bytes):
                    if not chunk:
                        continue
                    data = carry + chunk
                    extra = len(data) % PCM_SAMPLE_WIDTH_BYTES
                    if extra:
                        carry = data[-extra:]
                        data = data[:-extra]
                    else:
                        carry = b""
                    if data:
                        yield data
                if carry:
                    logger.debug(
                        "discarded %d trailing ElevenLabs byte(s) (unaligned sample)",
                        len(carry),
                    )
        except httpx.HTTPError as exc:
            if isinstance(exc, TTSError):
                raise
            raise TTSError(
                f"elevenlabs TTS request failed: {exc}",
                category="unknown",
            ) from exc

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.aclose()

    async def _raise_for_status(self, response: httpx.Response) -> None:
        """Translate non-2xx responses into :class:`TTSError`.

        Categorises HTTP failures (Johnny-g2n) so the pipeline can branch
        on the failure type rather than parsing the message:

        * ``401`` + ``quota`` in the detail (ElevenLabs' "exceeds your
          quota of N, M credits required" shape) → ``quota_exceeded``.
        * Any other ``401`` → ``auth_failed`` (bad / revoked key).
        * ``402`` / ``403`` → ``quota_exceeded`` (paywall / billing).
        * ``429`` → ``rate_limited``.
        * Everything else → ``unknown``.
        """
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
        category = _categorise_http_failure(response.status_code, detail)
        raise TTSError(
            f"elevenlabs TTS HTTP {response.status_code}"
            + (f": {detail}" if detail else ""),
            category=category,
        )


def _categorise_http_failure(status_code: int, detail: str) -> TTSErrorCategory:
    """Map an ElevenLabs HTTP failure to a :data:`TTSErrorCategory`.

    Quota / billing errors land at 401 with a "quota" substring in the
    ``detail`` payload ("exceeds your quota of N, M credits required"),
    or at 402/403 when the account is paywalled. Auth failures (bad
    key, revoked key) land at 401 without a quota mention.
    """
    detail_lc = (detail or "").lower()
    if status_code == 401:
        if "quota" in detail_lc or "credits" in detail_lc:
            return "quota_exceeded"
        return "auth_failed"
    if status_code in (402, 403):
        return "quota_exceeded"
    if status_code == 429:
        return "rate_limited"
    return "unknown"


def register(*, replace: bool = False) -> None:
    """Register :class:`ElevenLabsTTS` under ``(ProviderKind.TTS, "elevenlabs")``.

    Idempotent when ``replace=True``. Called at import time from
    :mod:`app.providers` so the global registry contains ``elevenlabs``
    by the time API startup runs.
    """
    get_registry().register(
        ProviderKind.TTS, PROVIDER_NAME, ElevenLabsTTS, replace=replace
    )


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_CHUNK_BYTES",
    "DEFAULT_MODEL_ID",
    "DEFAULT_OUTPUT_FORMAT",
    "DEFAULT_TIMEOUT_S",
    "ElevenLabsTTS",
    "PROVIDER_NAME",
    "fetch_voice_catalog",
    "register",
]
