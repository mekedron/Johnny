"""Cartesia text-to-speech streaming adapter (Sonic 3 / Sonic 3.5).

Calls ``POST /tts/bytes`` with ``output_format`` set to
``container=raw, encoding=pcm_s16le, sample_rate=16000`` so the response
body is already 16 kHz mono S16LE PCM — no resampling or container
unwrapping required to slot into the meet-worker audio bridge.

The HTTP ``/tts/bytes`` endpoint is the lowest-friction streaming path
Cartesia exposes: it transmits the synthesised PCM as a raw chunked
HTTP body (no SSE framing, no WebSocket framing). The WebSocket
(``wss://api.cartesia.ai/tts/websocket``) and SSE (``/tts/sse``)
endpoints exist for cross-context continuity and word-level
timestamps, but the split pipeline already finalises one transcript per
LLM round-trip so chunked HTTP is plenty.

Spec version pinned: ``Cartesia-Version: 2026-03-01`` (current at fetch
date 2026-06-07).

Latency profile: Cartesia advertises Sonic 3.5 at sub-200 ms
time-to-first-audio on warm connections from US/EU regions. The
streaming HTTP body delivers PCM as soon as the model emits it.

See ``fetch_voice_catalog`` for the runtime voice list (Cartesia
publishes voices via ``GET /voices``); a small curated set of stable
public voices is exposed via :data:`KNOWN_SONIC_VOICES` for the field
schema dropdown so the operator can pick a default without an API
round-trip.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from app.providers.base import (
    PCM_SAMPLE_RATE_HZ,
    PCM_SAMPLE_WIDTH_BYTES,
    ProviderConfig,
    ProviderKind,
    TTSError,
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

PROVIDER_NAME = "cartesia"
DEFAULT_BASE_URL = "https://api.cartesia.ai"
# Pin to a known-good API version — Cartesia bumps the header date when
# they ship breaking changes, so a fixed version keeps existing
# deployments deterministic. Bump in lockstep with the adapter when a
# field shape we depend on changes.
DEFAULT_API_VERSION = "2026-03-01"
DEFAULT_MODEL_ID = "sonic-3.5"
# Sonic 3.5 ships as a public sample voice on every Cartesia account; this
# is the default voice surfaced in the documentation's quick-start. Operators
# typically swap to a project-specific voice via the catalog; we keep this
# as a stable fallback so a fresh install can produce audio without a
# voices.list() round-trip.
DEFAULT_VOICE_ID = "694f9389-aac1-45b6-b726-9d9369183238"
DEFAULT_LANGUAGE = "en"
DEFAULT_CHUNK_BYTES = 4_096
DEFAULT_TIMEOUT_S = 30.0

# Sample rates the API will accept for raw pcm_s16le output. The bridge
# expects 16 kHz; an operator who picks a non-default rate gets either
# chipmunk audio or a 400 — we keep this list available so the schema
# can refuse anything obviously wrong before the request is sent.
ALLOWED_SAMPLE_RATES = frozenset({8_000, 16_000, 22_050, 24_000, 44_100, 48_000})

ALLOWED_MODEL_IDS = frozenset({"sonic-3.5", "sonic-3", "sonic-2", "sonic-latest"})

# A small curated set of stable Sonic public voices for the dropdown
# default. The runtime voice catalog (fetch_voice_catalog) returns the
# full live list so this only seeds the picker. UUIDs are public voices
# published on Cartesia's voice library at docs.cartesia.ai.
KNOWN_SONIC_VOICES: tuple[tuple[str, str], ...] = (
    ("694f9389-aac1-45b6-b726-9d9369183238", "Sonic Sample (default)"),
    ("a0e99841-438c-4a64-b679-ae501e7d6091", "Barbershop Man"),
    ("e07c00bc-4134-4eae-9ea4-1a55fb45746b", "Newslady"),
)


@dataclass(frozen=True, slots=True)
class CartesiaVoiceInfo:
    """One voice entry from ``GET /voices``.

    ``id`` is the UUID the adapter passes to ``synthesize_stream`` as
    ``voice_id``. ``name`` is the human-friendly label the catalog UI
    renders. ``language`` and ``gender`` come straight from the API so
    the picker can group / filter.
    """

    id: str
    name: str
    description: str
    language: str
    gender: str
    is_public: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "language": self.language,
            "gender": self.gender,
            "is_public": self.is_public,
        }


def _coerce_voice(entry: Any) -> CartesiaVoiceInfo | None:
    """Turn one raw entry from the voices payload into a typed value.

    Returns ``None`` when the entry is missing required fields so the
    catalog UI never has to defensively check each row.
    """
    if not isinstance(entry, dict):
        return None
    voice_id = entry.get("id")
    name = entry.get("name")
    if not isinstance(voice_id, str) or not isinstance(name, str):
        return None
    return CartesiaVoiceInfo(
        id=voice_id,
        name=name,
        description=str(entry.get("description") or ""),
        language=str(entry.get("language") or ""),
        gender=str(entry.get("gender") or ""),
        is_public=bool(entry.get("is_public", False)),
    )


async def fetch_voice_catalog(
    api_key: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    api_version: str = DEFAULT_API_VERSION,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = 15.0,
    page_size: int = 100,
    max_pages: int = 20,
) -> list[CartesiaVoiceInfo]:
    """Return every voice published on ``GET /voices``.

    Follows pagination until ``has_more`` flips to False or ``max_pages``
    is reached (a guardrail against an unexpectedly huge catalog
    pegging memory). Raises :class:`TTSError` with the API's diagnostic
    text on transport failure so the endpoint can surface it as-is.
    """
    if not api_key:
        raise TTSError("cartesia voice catalog requires an api_key")
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout_s)
    headers = {
        "X-API-Key": api_key,
        "Cartesia-Version": api_version,
        "Accept": "application/json",
    }
    url = f"{base_url.rstrip('/')}/voices"
    voices: list[CartesiaVoiceInfo] = []
    starting_after: str | None = None
    try:
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": page_size}
            if starting_after:
                params["starting_after"] = starting_after
            try:
                response = await client.get(url, headers=headers, params=params)
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPError as exc:
                raise TTSError(
                    f"failed to fetch cartesia voice catalog: {exc}"
                ) from exc
            except ValueError as exc:
                raise TTSError(
                    f"cartesia voice catalog is not valid JSON: {exc}"
                ) from exc
            if not isinstance(payload, dict):
                raise TTSError("cartesia voice catalog payload is not a JSON object")
            data = payload.get("data")
            if not isinstance(data, list):
                raise TTSError(
                    "cartesia voice catalog payload missing 'data' array"
                )
            for entry in data:
                info = _coerce_voice(entry)
                if info is not None:
                    voices.append(info)
            if not payload.get("has_more"):
                break
            next_id = payload.get("next_page") or (
                voices[-1].id if voices else None
            )
            if not isinstance(next_id, str):
                break
            starting_after = next_id
        voices.sort(key=lambda v: (v.language, v.name))
        return voices
    finally:
        if owns_client:
            await client.aclose()


class CartesiaTTS(TTSProvider):
    """Streaming TTS via Cartesia's ``/tts/bytes`` endpoint.

    Required credentials:

    * ``api_key`` — the Cartesia API key (sent as ``X-API-Key``).

    Configuration ``options`` (any key may be omitted):

    * ``voice_id`` — default voice UUID. Pick one from the live
      ``/voices`` catalog. Defaults to the public Sonic sample voice
      :data:`DEFAULT_VOICE_ID` so a fresh install can synthesise audio
      end-to-end without first browsing voices.
    * ``model_id`` — sonic model. Defaults to ``sonic-3.5`` (newest);
      ``sonic-3``, ``sonic-2`` and ``sonic-latest`` are also accepted.
    * ``base_url`` — API base URL. Defaults to Cartesia's public
      endpoint; override to target a proxy.
    * ``api_version`` — Cartesia-Version header value. Pinned to
      :data:`DEFAULT_API_VERSION`. Bump only when the spec the adapter
      depends on actually changes.
    * ``language`` — BCP-47 language tag passed to the API. Defaults to
      ``en``. Leave at default unless the voice is multilingual and
      the operator wants to force a specific language.
    * ``sample_rate`` — output sample rate (Hz). Defaults to 16 000 to
      match the meet-worker bridge format. Override only if you know
      what you're doing.
    * ``chunk_bytes`` — read chunk size for the streamed body. Must be a
      multiple of the 2-byte S16 sample width. Default 4096.
    * ``timeout_s`` — request timeout in seconds. Default 30.
    """

    def __init__(self, config: ProviderConfig) -> None:
        if config.kind is not ProviderKind.TTS:
            raise ValueError(
                f"CartesiaTTS requires ProviderKind.TTS; got {config.kind.value}"
            )
        api_key = config.credentials.get("api_key")
        if not api_key:
            raise ValueError("CartesiaTTS requires 'api_key' in credentials")
        self._api_key = str(api_key)
        opts = config.options
        voice_id = opts.get("voice_id") or DEFAULT_VOICE_ID
        self._default_voice_id = str(voice_id)
        self._model_id = str(opts.get("model_id") or DEFAULT_MODEL_ID)
        self._base_url = str(opts.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self._api_version = str(opts.get("api_version") or DEFAULT_API_VERSION)
        self._language = str(opts.get("language") or DEFAULT_LANGUAGE)
        sample_rate = int(opts.get("sample_rate") or PCM_SAMPLE_RATE_HZ)
        if sample_rate not in ALLOWED_SAMPLE_RATES:
            raise ValueError(
                f"sample_rate {sample_rate} is not one of "
                f"{sorted(ALLOWED_SAMPLE_RATES)}"
            )
        self._sample_rate = sample_rate
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
            display_name="Cartesia Sonic",
            summary=(
                "Sonic 3 / 3.5 streaming TTS — sub-200 ms time-to-first-audio."
            ),
            signup_url="https://play.cartesia.ai/sign-up",
            fields=(
                FieldDef(
                    name="api_key",
                    label="API key",
                    type=FieldType.PASSWORD,
                    required=True,
                    secret=True,
                    placeholder="sk_car_...",
                    help_text="Get a key from play.cartesia.ai → API keys.",
                    signup_url="https://play.cartesia.ai/sign-up",
                    env_key="CARTESIA_API_KEY",
                    group=FieldGroup.AUTH,
                ),
                FieldDef(
                    name="voice_id",
                    label="Voice ID",
                    required=True,
                    voice_catalog=True,
                    default=DEFAULT_VOICE_ID,
                    placeholder=DEFAULT_VOICE_ID,
                    help_text=(
                        "Cartesia voice UUID. Browse the live catalog with the "
                        "voice picker below, or pick one from play.cartesia.ai/voices."
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="model_id",
                    label="Model",
                    type=FieldType.SELECT,
                    default=DEFAULT_MODEL_ID,
                    options=(
                        FieldOption(value="sonic-3.5", label="sonic-3.5 (newest)"),
                        FieldOption(value="sonic-3", label="sonic-3"),
                        FieldOption(value="sonic-2", label="sonic-2"),
                        FieldOption(
                            value="sonic-latest", label="sonic-latest (auto-track)"
                        ),
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="language",
                    label="Language",
                    default=DEFAULT_LANGUAGE,
                    help_text="BCP-47 tag (e.g. en, es, fr). Defaults to en.",
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="sample_rate",
                    label="Sample rate (Hz)",
                    type=FieldType.NUMBER,
                    default=PCM_SAMPLE_RATE_HZ,
                    help_text=(
                        "Must be 16000 to match the audio bridge. Override only "
                        "if you know what you're doing."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="api_version",
                    label="Cartesia-Version",
                    default=DEFAULT_API_VERSION,
                    help_text=(
                        "Pinned spec date. Bump only when the API contract the "
                        "adapter depends on actually changes."
                    ),
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
                    topic="Sonic 3.5 is the latency leader",
                    body=(
                        "sonic-3.5 streams first audio in ~120-200 ms from "
                        "US/EU regions — the fastest cloud TTS available "
                        "today. It carries the laughter / emotion features "
                        "documented at cartesia.ai/sonic. Drop to sonic-2 "
                        "only if you observe quality regressions on a "
                        "specific voice."
                    ),
                ),
                ProviderTip(
                    topic="Use raw pcm_s16le @ 16 kHz",
                    body=(
                        "The adapter pins output_format to container=raw, "
                        "encoding=pcm_s16le, sample_rate=16000 so the body "
                        "lands directly in the bridge with zero transcoding. "
                        "Changing sample_rate forces a resample upstream and "
                        "adds CPU + jitter — keep at 16000."
                    ),
                ),
                ProviderTip(
                    topic="Voice IDs are UUIDs from /voices",
                    body=(
                        "Use the voice picker below to browse the live "
                        "catalog and click Use to set this provider's "
                        "voice_id. Operators sometimes paste a *name* "
                        "(e.g. 'Newslady') by mistake — that returns a 400. "
                        "Always copy the UUID."
                    ),
                ),
                ProviderTip(
                    topic="Costs scale with characters",
                    body=(
                        "Cartesia bills per character synthesised. A "
                        "typical bot turn (~80 chars) is on the order of "
                        "$0.001 on Sonic 3.5. Watch your monthly cap on the "
                        "Cartesia dashboard if you run long meetings."
                    ),
                ),
                ProviderTip(
                    topic="Pin Cartesia-Version",
                    body=(
                        "The adapter sends Cartesia-Version: 2026-03-01. "
                        "Don't bump this header on a whim — the API ships "
                        "breaking changes between dated versions. Update the "
                        "adapter test fixtures in lockstep when you bump."
                    ),
                ),
            ),
        )

    @property
    def default_voice_id(self) -> str:
        return self._default_voice_id

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def api_version(self) -> str:
        return self._api_version

    @property
    def language(self) -> str:
        return self._language

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def chunk_bytes(self) -> int:
        return self._chunk_bytes

    async def list_voices(self) -> tuple[VoiceMeta, ...]:
        """Return the account's voice catalog (Johnny-1ge.9).

        Reuses :func:`fetch_voice_catalog` (the same ``GET /voices`` call
        the dedicated browser used) and maps each entry to the shared
        :class:`VoiceMeta` so the unified picker renders Cartesia with the
        same language / gender filters as the local providers. ``__init__``
        requires the key, so a keyless add-modal falls back to free-text.
        """
        infos = await fetch_voice_catalog(
            self._api_key,
            base_url=self._base_url,
            api_version=self._api_version,
            client=self._client,
        )
        return tuple(
            VoiceMeta(
                id=info.id,
                label=info.name,
                language=info.language or None,
                sample_rate=None,
                gender=info.gender.lower() if info.gender else None,
                installed=True,
            )
            for info in infos
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

        The voice ID is sent in the request body (``voice.id`` UUID);
        pass ``voice_id`` per-call to override the configured default.
        Raises :class:`TTSError` on transport, auth, or synthesis
        failure with the Cartesia diagnostic text included verbatim.
        """
        voice = voice_id if voice_id not in (None, "") else self._default_voice_id
        if not voice:
            raise TTSError(
                "CartesiaTTS requires a voice_id; pass it per-call or set "
                "voice_id in the provider configuration."
            )
        url = f"{self._base_url}/tts/bytes"
        body: dict[str, Any] = {
            "model_id": self._model_id,
            "transcript": text,
            "voice": {"mode": "id", "id": voice},
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": self._sample_rate,
            },
            "language": self._language,
        }
        headers = {
            "X-API-Key": self._api_key,
            "Cartesia-Version": self._api_version,
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
                    if data:
                        yield data
                if carry:
                    logger.debug(
                        "discarded %d trailing cartesia byte(s) (unaligned sample)",
                        len(carry),
                    )
        except httpx.HTTPError as exc:
            if isinstance(exc, TTSError):
                raise
            raise TTSError(f"cartesia TTS request failed: {exc}") from exc

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.aclose()

    async def _raise_for_status(self, response: httpx.Response) -> None:
        """Translate non-2xx responses into :class:`TTSError`.

        Cartesia error bodies follow ``{"error": {"message": "..."}}`` on
        the HTTP endpoints; we also fall back to ``error`` as a string
        and to the raw body text when neither shape matches.
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
                    elif isinstance(err, str):
                        detail = err
                    elif "message" in payload:
                        detail = str(payload["message"])
                    elif "title" in payload:
                        detail = str(payload["title"])
            if not detail:
                with contextlib.suppress(UnicodeDecodeError):
                    detail = body_bytes.decode("utf-8")[:200]
        raise TTSError(
            f"cartesia TTS HTTP {response.status_code}"
            + (f": {detail}" if detail else "")
        )


def register(*, replace: bool = False) -> None:
    """Register :class:`CartesiaTTS` under ``(ProviderKind.TTS, "cartesia")``.

    Idempotent when ``replace=True``. Called at import time from
    :mod:`app.providers` so the global registry contains ``cartesia``
    by the time API startup runs.
    """
    get_registry().register(
        ProviderKind.TTS, PROVIDER_NAME, CartesiaTTS, replace=replace
    )


__all__ = [
    "ALLOWED_MODEL_IDS",
    "ALLOWED_SAMPLE_RATES",
    "CartesiaTTS",
    "CartesiaVoiceInfo",
    "DEFAULT_API_VERSION",
    "DEFAULT_BASE_URL",
    "DEFAULT_CHUNK_BYTES",
    "DEFAULT_LANGUAGE",
    "DEFAULT_MODEL_ID",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_VOICE_ID",
    "KNOWN_SONIC_VOICES",
    "PROVIDER_NAME",
    "fetch_voice_catalog",
    "register",
]
