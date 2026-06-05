"""OpenAI text-to-speech streaming adapter.

Calls ``POST /v1/audio/speech`` with ``response_format=pcm`` so the API
returns raw 24 kHz mono signed-16-bit LE PCM. The adapter resamples to
16 kHz to match the canonical meet-worker audio bridge format that the
pipeline expects.

Latency profile: time-to-first-audio is dominated by the request-to-server
round-trip plus model warm-up (~200-500 ms for ``tts-1``). The chunked
HTTP body lets us start playback well before the full synthesis is
complete.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.providers._pcm import resample_pcm16
from app.providers.base import (
    PCM_SAMPLE_RATE_HZ,
    PCM_SAMPLE_WIDTH_BYTES,
    ProviderConfig,
    ProviderKind,
    TTSError,
    TTSProvider,
    get_registry,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "openai"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "tts-1"
DEFAULT_VOICE_ID = "alloy"
# OpenAI's PCM response format is documented as 24 kHz mono S16LE.
DEFAULT_NATIVE_SAMPLE_RATE_HZ = 24_000
DEFAULT_CHUNK_BYTES = 4_096
DEFAULT_TIMEOUT_S = 30.0
ALLOWED_VOICES = frozenset(
    {"alloy", "echo", "fable", "onyx", "nova", "shimmer", "ash", "coral", "sage"}
)


class OpenAITTS(TTSProvider):
    """Streaming TTS via OpenAI's ``/audio/speech`` endpoint.

    Required credentials:

    * ``api_key`` — the OpenAI secret key.

    Configuration ``options`` (any key may be omitted):

    * ``voice_id`` — default voice. One of ``alloy``, ``echo``, ``fable``,
      ``onyx``, ``nova``, ``shimmer``, ``ash``, ``coral``, ``sage``.
      Defaults to ``alloy``.
    * ``model`` — TTS model. Defaults to ``tts-1`` (fast, lower fidelity);
      ``tts-1-hd`` and ``gpt-4o-mini-tts`` are also supported by the API.
    * ``base_url`` — API base URL. Defaults to OpenAI's public endpoint;
      override to target Azure OpenAI or a proxy.
    * ``speed`` — playback speed multiplier (0.25 - 4.0). Default 1.0.
    * ``native_sample_rate`` — sample rate of the PCM response. The
      adapter resamples to 16 kHz; default 24 000 matches OpenAI's docs.
    * ``chunk_bytes`` — read chunk size for the streamed body. Must be a
      multiple of the 2-byte S16 sample width. Default 4096.
    * ``timeout_s`` — request timeout in seconds. Default 30.
    """

    def __init__(self, config: ProviderConfig) -> None:
        if config.kind is not ProviderKind.TTS:
            raise ValueError(
                f"OpenAITTS requires ProviderKind.TTS; got {config.kind.value}"
            )
        api_key = config.credentials.get("api_key")
        if not api_key:
            raise ValueError(
                "OpenAITTS requires 'api_key' in credentials"
            )
        self._api_key = str(api_key)
        opts = config.options
        voice_id = opts.get("voice_id") or DEFAULT_VOICE_ID
        self._default_voice_id = str(voice_id)
        self._model = str(opts.get("model") or DEFAULT_MODEL)
        self._base_url = str(opts.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self._speed = float(opts.get("speed") or 1.0)
        native_rate = int(opts.get("native_sample_rate") or DEFAULT_NATIVE_SAMPLE_RATE_HZ)
        if native_rate <= 0:
            raise ValueError(f"native_sample_rate must be positive; got {native_rate}")
        self._native_sample_rate = native_rate
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
    def default_voice_id(self) -> str:
        return self._default_voice_id

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def native_sample_rate(self) -> int:
        return self._native_sample_rate

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

        Sends one chunked HTTP request per call. The response body is
        streamed and yielded back as resampled PCM as it arrives.
        """
        voice = voice_id if voice_id not in (None, "") else self._default_voice_id
        url = f"{self._base_url}/audio/speech"
        body: dict[str, Any] = {
            "model": self._model,
            "input": text,
            "voice": voice,
            "response_format": "pcm",
            "speed": self._speed,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/octet-stream",
        }
        try:
            async with self._client.stream(
                "POST", url, json=body, headers=headers
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
                    if not data:
                        continue
                    out = resample_pcm16(
                        data, self._native_sample_rate, PCM_SAMPLE_RATE_HZ
                    )
                    if out:
                        yield out
                if carry:
                    logger.debug(
                        "discarded %d trailing OpenAI byte(s) (unaligned sample)",
                        len(carry),
                    )
        except httpx.HTTPError as exc:
            if isinstance(exc, TTSError):
                raise
            raise TTSError(f"openai TTS request failed: {exc}") from exc

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.aclose()

    async def _raise_for_status(self, response: httpx.Response) -> None:
        """Translate non-2xx responses into :class:`TTSError`.

        Reads the (possibly small) error body so the message includes the
        provider's diagnostic text; the streaming body has not started
        yielding application data yet.
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
                    err = payload.get("error")
                    if isinstance(err, dict) and "message" in err:
                        detail = str(err["message"])
                    elif "message" in payload:
                        detail = str(payload["message"])
            if not detail:
                with contextlib.suppress(UnicodeDecodeError):
                    detail = body_bytes.decode("utf-8")[:200]
        raise TTSError(
            f"openai TTS HTTP {response.status_code}"
            + (f": {detail}" if detail else "")
        )


def register(*, replace: bool = False) -> None:
    """Register :class:`OpenAITTS` under ``(ProviderKind.TTS, "openai")``.

    Idempotent when ``replace=True``. Called at import time from
    :mod:`app.providers` so the global registry contains ``openai`` by
    the time API startup runs.
    """
    get_registry().register(
        ProviderKind.TTS, PROVIDER_NAME, OpenAITTS, replace=replace
    )


__all__ = [
    "ALLOWED_VOICES",
    "DEFAULT_BASE_URL",
    "DEFAULT_CHUNK_BYTES",
    "DEFAULT_MODEL",
    "DEFAULT_NATIVE_SAMPLE_RATE_HZ",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_VOICE_ID",
    "OpenAITTS",
    "PROVIDER_NAME",
    "register",
]
