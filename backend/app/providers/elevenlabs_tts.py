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
    TTSProvider,
    get_registry,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "elevenlabs"
DEFAULT_BASE_URL = "https://api.elevenlabs.io/v1"
DEFAULT_MODEL_ID = "eleven_multilingual_v2"
# pcm_16000 returns 16 kHz mono S16LE PCM — matches the bridge format.
DEFAULT_OUTPUT_FORMAT = "pcm_16000"
DEFAULT_CHUNK_BYTES = 4_096
DEFAULT_TIMEOUT_S = 30.0


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
            raise TTSError(f"elevenlabs TTS request failed: {exc}") from exc

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.aclose()

    async def _raise_for_status(self, response: httpx.Response) -> None:
        """Translate non-2xx responses into :class:`TTSError`."""
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
        raise TTSError(
            f"elevenlabs TTS HTTP {response.status_code}"
            + (f": {detail}" if detail else "")
        )


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
    "register",
]
