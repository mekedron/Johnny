"""Provider configuration HTTP endpoints (US-018).

Manages rows in ``provider_credentials``: list / create / update / delete,
mark one provider per kind as active, and run a one-shot smoke call against
the registered factory to verify credentials work end-to-end.

The active-per-kind invariant is enforced both by a partial unique index
on ``(kind) WHERE is_active`` and by ``activate_provider`` which deactivates
any sibling rows before flipping the requested row on.
"""

from __future__ import annotations

import io
import json
import time
import wave
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_crypto, get_session
from app.db.models import ProviderCredential
from app.providers.anthropic_llm import (
    DEFAULT_BASE_URL as ANTHROPIC_DEFAULT_BASE_URL,
)
from app.providers.anthropic_llm import (
    PROVIDER_NAME as ANTHROPIC_PROVIDER_NAME,
)
from app.providers.anthropic_llm import (
    fetch_model_catalog as anthropic_fetch_model_catalog,
)
from app.providers.audio_assert import (
    AudioMetrics,
    check_audible,
    measure_pcm16,
)
from app.providers.base import (
    PCM_CHANNELS,
    PCM_SAMPLE_RATE_HZ,
    PCM_SAMPLE_WIDTH_BYTES,
    ChatMessage,
    LLMProvider,
    ProviderConfig,
    ProviderKind,
    STTProvider,
    TTSProvider,
    UnknownProviderError,
    get_registry,
)
from app.providers.cartesia_tts import (
    PROVIDER_NAME as CARTESIA_PROVIDER_NAME,
)
from app.providers.cartesia_tts import (
    fetch_voice_catalog as cartesia_fetch_voice_catalog,
)
from app.providers.gemini_llm import (
    DEFAULT_BASE_URL as GEMINI_DEFAULT_BASE_URL,
)
from app.providers.gemini_llm import (
    PROVIDER_NAME as GEMINI_PROVIDER_NAME,
)
from app.providers.gemini_llm import (
    fetch_model_catalog as gemini_fetch_model_catalog,
)
from app.providers.openai_compatible_llm import (
    PROVIDER_NAME as OPENAI_COMPATIBLE_PROVIDER_NAME,
)
from app.providers.openai_compatible_llm import (
    fetch_model_catalog as openai_compatible_fetch_model_catalog,
)
from app.providers.openai_llm import (
    DEFAULT_BASE_URL as OPENAI_DEFAULT_BASE_URL,
)
from app.providers.openai_llm import (
    PROVIDER_NAME as OPENAI_PROVIDER_NAME,
)
from app.providers.openai_llm import (
    fetch_model_catalog as openai_fetch_model_catalog,
)
from app.providers.piper_tts import (
    DEFAULT_MODEL_DIR as PIPER_DEFAULT_MODEL_DIR,
)
from app.providers.piper_tts import (
    PROVIDER_NAME as PIPER_PROVIDER_NAME,
)
from app.providers.piper_tts import (
    download_voice as piper_download_voice,
)
from app.providers.piper_tts import (
    fetch_voice_catalog as piper_fetch_voice_catalog,
)
from app.providers.piper_tts import (
    remove_voice as piper_remove_voice,
)
from app.providers.piper_tts import (
    voice_info_to_meta as piper_voice_info_to_meta,
)
from app.providers.schema import ProviderSchema
from app.providers.schema_validation import (
    FieldValidationError,
    split_values,
    validate_payload,
)
from app.security.crypto import CredentialCrypto, CryptoError, decrypt_json, encrypt_json
from app.services.providers_seed import SUPPORTED_FILE_VERSION

router = APIRouter(prefix="/providers", tags=["providers"])


# --- Pydantic schemas ------------------------------------------------------


class ProviderCreate(BaseModel):
    """Payload for creating a provider credential row.

    Two equivalent shapes are accepted:

    * **Structured form (preferred)**: send a flat ``values`` dict —
      the backend splits it into encrypted credentials and plain
      options based on the provider's field schema.
    * **Legacy buckets**: send ``credentials`` and ``options`` directly.
      Used by tests and older clients.

    If both are supplied, ``credentials`` / ``options`` win for the keys
    they declare and ``values`` fills in the rest.
    """

    kind: ProviderKind
    provider_name: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    credentials: dict[str, str] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    values: dict[str, Any] | None = None


class ProviderUpdate(BaseModel):
    """Patch payload — fields left as ``None`` are not modified."""

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    credentials: dict[str, str] | None = None
    options: dict[str, Any] | None = None
    values: dict[str, Any] | None = None


class ProviderRead(BaseModel):
    """Public view of a provider credential row. Secrets are never returned."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: ProviderKind
    provider_name: str
    display_name: str
    options: dict[str, Any]
    is_active: bool
    credential_keys: list[str]
    created_at: datetime
    updated_at: datetime


class ProviderListResponse(BaseModel):
    """All providers grouped by kind for the configuration UI."""

    stt: list[ProviderRead]
    llm: list[ProviderRead]
    tts: list[ProviderRead]


class TestResult(BaseModel):
    """Outcome of a provider smoke test."""

    ok: bool
    message: str
    detail: str | None = None


class SttTestResult(BaseModel):
    """Outcome of an STT mic-recording test (Johnny-ckz.15.2).

    Mirrors the shape of :class:`TestResult` but adds the user-visible
    transcript + latency + cost the catalog UI renders next to its Test
    button. ``cost_usd`` is ``None`` for local providers (no per-second
    cost to report) and for cloud providers without a published rate.
    """

    ok: bool
    transcript: str
    latency_ms: int
    cost_usd: float | None = None
    audio_ms: int
    message: str | None = None
    detail: str | None = None


class SttCatalogEntry(BaseModel):
    """One entry in the STT catalog response (Johnny-ckz.15.2).

    Carries the schema fields the catalog UI shows at a glance (display
    name, summary, type, streaming flag, model count) so it can render
    cards without computing those metadata bits from raw schemas.
    """

    provider_name: str
    display_name: str
    summary: str
    signup_url: str | None = None
    # "local" — runs on-device, no per-minute cost; "cloud" — audio leaves host.
    provider_type: str
    streaming: bool
    model_count: int
    models: list[str]
    field_schema: dict[str, Any]


class SttCatalogResponse(BaseModel):
    """All registered STT providers with catalog metadata."""

    providers: list[SttCatalogEntry]


class SchemaListResponse(BaseModel):
    """All registered provider schemas grouped by kind."""

    stt: list[dict[str, Any]]
    llm: list[dict[str, Any]]
    tts: list[dict[str, Any]]


class VoiceRead(BaseModel):
    """One Piper voice entry from huggingface.co/rhasspy/piper-voices.

    ``installed`` indicates whether both the ``.onnx`` and ``.onnx.json``
    files for this voice are present in the provider's configured
    ``model_dir`` — the UI uses this to grey out the Install button.
    """

    key: str
    name: str
    language_code: str
    language_name: str
    quality: str
    installed: bool


class VoiceListResponse(BaseModel):
    """Response payload for ``GET /providers/{id}/voices``."""

    model_dir: str
    voices: list[VoiceRead]


class VoiceInstallResponse(BaseModel):
    """Response payload for ``POST /providers/{id}/voices/{key}/install``."""

    key: str
    installed: bool
    onnx_bytes: int
    onnx_json_bytes: int
    already_present: bool


class VoiceRemoveResponse(BaseModel):
    """Response payload for ``DELETE /providers/{id}/voices/{key}``."""

    key: str
    installed: bool
    onnx_removed: bool
    onnx_json_removed: bool


class CartesiaVoiceRead(BaseModel):
    """One Cartesia voice entry returned by ``GET /voices``.

    ``id`` is the UUID to paste into a CartesiaTTS provider's
    ``voice_id`` field. ``language``, ``gender`` and ``description``
    come straight from Cartesia so the picker can group / filter.
    """

    id: str
    name: str
    description: str
    language: str
    gender: str
    is_public: bool


class CartesiaVoiceListResponse(BaseModel):
    """Response payload for ``GET /providers/{id}/cartesia/voices``."""

    voices: list[CartesiaVoiceRead]


class VoiceCatalogVoice(BaseModel):
    """One voice in the unified cross-provider catalog (Johnny-1ge.8).

    Mirrors :class:`app.providers.base.VoiceMeta`. Powers the shared
    ``/providers`` voice picker so Piper, Kokoro, OpenAI (and any future
    TTS adapter) present an identical filterable list with per-voice
    preview, regardless of whether the voice is local or cloud.
    """

    id: str
    label: str
    language: str | None = None
    sample_rate: int | None = None
    gender: str | None = None
    preview_url: str | None = None
    installed: bool = True
    size_bytes: int | None = None
    tier: str | None = None


class VoiceCatalogResponse(BaseModel):
    """Response payload for the unified voice-catalog endpoints (Johnny-1ge.8).

    Returned by ``POST /providers/preview/voices`` and by
    ``GET /providers/{id}/voices`` for every TTS provider *except* Piper,
    whose dedicated install-aware response shape is preserved for the
    existing voice browser.
    """

    voices: list[VoiceCatalogVoice]


class LlmModelRead(BaseModel):
    """One model entry returned by ``GET /providers/{id}/llm_models`` (Johnny-9eq).

    ``id`` is the canonical model identifier the chat adapter expects.
    ``label`` is the UI string the dropdown should display — usually the
    same as ``id``, but adapters may decorate it (e.g. Anthropic's
    ``display_name`` is friendlier than the raw model id). ``description``
    is an optional one-line summary when the provider's catalog supplies
    one — currently only Gemini.
    """

    id: str
    label: str
    description: str | None = None


class LlmModelListResponse(BaseModel):
    """Response payload for ``GET /providers/{id}/llm_models`` and
    ``POST /providers/preview/llm_models`` (Johnny-9eq).
    """

    models: list[LlmModelRead]


class PlaySampleRequest(BaseModel):
    """Optional request body for ``POST /providers/{id}/play_sample``.

    The body is optional — calling without a body preserves the historic
    behavior of synthesising with the row's saved config. When provided,
    ``voice_id`` overrides the row's ``options["voice_id"]`` for this
    single synth call only; the database row is *not* mutated. This is
    what the Piper voice browser modal uses to preview an installed
    voice without forcing the user to re-save the provider first.

    ``runtime`` likewise overrides ``options["runtime"]`` for the single
    call, so ``johnny-tts-smoke`` (Johnny-1ge.7) can exercise every runtime
    a provider supports against the same saved row without re-saving it.
    """

    model_config = ConfigDict(extra="forbid")

    voice_id: str | None = Field(default=None, max_length=256)
    runtime: str | None = Field(default=None, max_length=64)


# --- Helpers ---------------------------------------------------------------


def _credential_keys(crypto: CredentialCrypto, blob: str) -> list[str]:
    """Decrypt and return the sorted credential keys (no values)."""
    try:
        creds = decrypt_json(crypto, blob)
    except (CryptoError, ValueError, json.JSONDecodeError):
        return []
    return sorted(creds.keys())


def _row_to_read(crypto: CredentialCrypto, row: ProviderCredential) -> ProviderRead:
    return ProviderRead(
        id=row.id,
        kind=row.kind,
        provider_name=row.provider_name,
        display_name=row.display_name,
        options=dict(row.config or {}),
        is_active=row.is_active,
        credential_keys=_credential_keys(crypto, row.credentials_encrypted),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _get_row_or_404(session: Session, provider_id: int) -> ProviderCredential:
    row = session.get(ProviderCredential, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")
    return row


def _schema_for(kind: ProviderKind, provider_name: str) -> ProviderSchema | None:
    """Look up a provider's field schema via the registry.

    Returns ``None`` when no factory is registered for ``(kind, name)``
    or when the factory's class did not declare a ``field_schema()``.
    Callers fall back to legacy free-text validation in that case.
    """
    registry = get_registry()
    if not registry.has(kind, provider_name):
        return None
    factory = registry.get(kind, provider_name)
    field_schema = getattr(factory, "field_schema", None)
    if not callable(field_schema):
        return None
    try:
        schema = field_schema()
    except NotImplementedError:
        return None
    if isinstance(schema, ProviderSchema):
        return schema
    return None


def _all_schemas() -> dict[ProviderKind, list[ProviderSchema]]:
    """Collect schemas for every registered provider, grouped by kind."""
    registry = get_registry()
    out: dict[ProviderKind, list[ProviderSchema]] = {
        ProviderKind.STT: [],
        ProviderKind.LLM: [],
        ProviderKind.TTS: [],
    }
    for kind in (
        ProviderKind.STT,
        ProviderKind.LLM,
        ProviderKind.TTS,
    ):
        for provider_name in registry.names(kind):
            schema = _schema_for(kind, provider_name)
            if schema is not None:
                out[kind].append(schema)
    return out


def _merge_values_and_buckets(
    schema: ProviderSchema,
    values: dict[str, Any] | None,
    credentials: dict[str, str] | None,
    options: dict[str, Any] | None,
) -> dict[str, Any]:
    """Combine the structured ``values`` dict with legacy bucket payloads.

    ``credentials`` and ``options`` win for the keys they explicitly
    declare, since they are the historically authoritative shape. The
    ``values`` dict fills in any field the buckets omitted.
    """
    merged: dict[str, Any] = dict(values or {})
    if credentials:
        merged.update(credentials)
    if options:
        merged.update(options)
    # Drop any key not in the schema so unknown fields can't slip through.
    return {k: v for k, v in merged.items() if schema.field(k) is not None}


def _raise_validation_errors(errors: list[FieldValidationError]) -> None:
    """Surface field-level errors as FastAPI's standard 422 envelope."""
    raise HTTPException(
        status_code=422,
        detail=[err.to_dict() for err in errors],
    )


# --- STT catalog metadata --------------------------------------------------

# Each entry maps a registered STT provider_name to its catalog metadata.
# ``type`` is "local" when the adapter runs the model on-device (no network
# I/O, no per-minute cost) and "cloud" when audio leaves the host. ``streaming``
# means partial transcripts arrive before the user finishes speaking — only
# matters for the live-chat surface in Johnny-ckz.15.7, but the catalog UI
# surfaces it so users can pick the right provider for their use case.
# ``cost_per_minute_usd`` is a published per-minute rate used to render an
# estimate next to the Test transcript; ``None`` means "we don't have a rate
# we trust" (the UI just hides the cost line for that provider).
STT_CATALOG_METADATA: dict[str, dict[str, Any]] = {
    "faster-whisper": {
        "type": "local",
        "streaming": False,
        "cost_per_minute_usd": 0.0,
    },
    "parakeet": {
        # NVIDIA NeMo Parakeet. Runs entirely on-device — no audio leaves
        # the host. Johnny-stt.3 wired the streaming-into-chat consumer
        # via ``/ws/stt/stream`` — the endpoint re-runs ``transcribe``
        # over the growing buffer to deliver partials at >=2 Hz, so
        # every STT adapter (Parakeet included) is streaming-from-the-
        # user-perspective regardless of whether the underlying model
        # is batch- or streaming-native.
        "type": "local",
        "streaming": True,
        "cost_per_minute_usd": 0.0,
    },
    "deepgram": {
        "type": "cloud",
        "streaming": True,
        # https://deepgram.com/pricing — Nova-2 streaming, list pay-as-you-go.
        "cost_per_minute_usd": 0.0043,
    },
    "elevenlabs": {
        "type": "cloud",
        "streaming": False,
        # https://elevenlabs.io/pricing — Scribe v2 batch list rate.
        "cost_per_minute_usd": 0.0067,
    },
    "openai-realtime": {
        "type": "cloud",
        "streaming": True,
        # OpenAI Realtime API whisper-1 input audio rate, approximated to
        # the per-minute equivalent so the catalog can show *something*.
        "cost_per_minute_usd": 0.006,
    },
}


def _stt_catalog_entry(schema: ProviderSchema) -> SttCatalogEntry:
    """Enrich an :class:`ProviderSchema` with catalog metadata for the UI."""
    meta = STT_CATALOG_METADATA.get(
        schema.provider_name,
        {"type": "cloud", "streaming": False, "cost_per_minute_usd": None},
    )
    model_field = schema.field("model") or schema.field("model_id") or schema.field("model_size")
    if model_field is not None and model_field.options:
        models = [opt.value for opt in model_field.options]
    else:
        models = []
    return SttCatalogEntry(
        provider_name=schema.provider_name,
        display_name=schema.display_name,
        summary=schema.summary,
        signup_url=schema.signup_url,
        provider_type=str(meta.get("type", "cloud")),
        streaming=bool(meta.get("streaming", False)),
        model_count=len(models),
        models=models,
        field_schema=schema.to_dict(),
    )


# --- Endpoints -------------------------------------------------------------


SessionDep = Annotated[Session, Depends(get_session)]
CryptoDep = Annotated[CredentialCrypto, Depends(get_crypto)]


@router.get("/stt_catalog", response_model=SttCatalogResponse)
def list_stt_catalog() -> SttCatalogResponse:
    """Return every registered STT provider enriched with catalog metadata.

    The ``/providers`` STT tab calls this on mount to render one card
    per installed STT provider (Johnny-ckz.15.2 → unified under
    Johnny-stt.5). Each entry carries the same ``schema`` payload as
    ``GET /providers/schemas`` so the UI can
    render the per-provider config form inline, plus ``type``,
    ``streaming``, and ``model_count`` so users can pick the right
    provider without round-tripping to docs.
    """
    schemas = _all_schemas()[ProviderKind.STT]
    return SttCatalogResponse(
        providers=[_stt_catalog_entry(s) for s in schemas],
    )


@router.get("/schemas", response_model=SchemaListResponse)
def list_schemas() -> SchemaListResponse:
    """Return field schemas for every registered provider, grouped by kind.

    The SvelteKit /providers UI fetches this once on mount and uses it
    to render a structured form per provider — replacing the previous
    free-text Credentials/Options textareas. The wizard and the
    server-side validator share the same source of truth.
    """
    schemas = _all_schemas()
    return SchemaListResponse(
        stt=[s.to_dict() for s in schemas[ProviderKind.STT]],
        llm=[s.to_dict() for s in schemas[ProviderKind.LLM]],
        tts=[s.to_dict() for s in schemas[ProviderKind.TTS]],
    )


@router.get("", response_model=ProviderListResponse)
def list_providers(session: SessionDep, crypto: CryptoDep) -> ProviderListResponse:
    """List every configured provider grouped by kind.

    Scoped to the live :class:`ProviderKind` values: historical
    ``kind='s2s'`` rows (tombstoned by Johnny-trt.43, deactivated in
    migration 0026) stay in the table for the record but are never
    loaded — coercing them onto the three-kind enum would crash.
    """
    rows = session.scalars(
        select(ProviderCredential)
        .where(ProviderCredential.kind.in_(list(ProviderKind)))
        .order_by(
            ProviderCredential.kind,
            ProviderCredential.display_name,
            ProviderCredential.id,
        )
    ).all()
    grouped: dict[ProviderKind, list[ProviderRead]] = {
        ProviderKind.STT: [],
        ProviderKind.LLM: [],
        ProviderKind.TTS: [],
    }
    for row in rows:
        grouped[row.kind].append(_row_to_read(crypto, row))
    return ProviderListResponse(
        stt=grouped[ProviderKind.STT],
        llm=grouped[ProviderKind.LLM],
        tts=grouped[ProviderKind.TTS],
    )


@router.post("", response_model=ProviderRead, status_code=status.HTTP_201_CREATED)
def create_provider(
    payload: ProviderCreate,
    session: SessionDep,
    crypto: CryptoDep,
) -> ProviderRead:
    """Create a new provider credential row. Always created inactive.

    Validates ``values`` / ``credentials`` / ``options`` against the
    provider's declared field schema (when one exists) and surfaces
    field-level errors as HTTP 422. Falls back to legacy unvalidated
    behavior when the registered factory does not declare a schema.
    """
    schema = _schema_for(payload.kind, payload.provider_name)
    if schema is not None:
        merged = _merge_values_and_buckets(
            schema,
            payload.values,
            dict(payload.credentials),
            dict(payload.options),
        )
        errors = validate_payload(schema, merged)
        if errors:
            _raise_validation_errors(errors)
        credentials, options = split_values(schema, merged)
    else:
        credentials = dict(payload.credentials)
        options = dict(payload.options)

    row = ProviderCredential(
        kind=payload.kind,
        provider_name=payload.provider_name,
        display_name=payload.display_name,
        credentials_encrypted=encrypt_json(crypto, credentials),
        config=options,
        is_active=False,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                f"a {payload.kind.value} provider with display name "
                f"{payload.display_name!r} already exists. "
                "Pick a different display name — multiple instances of the "
                "same provider are allowed as long as their display names "
                "are unique."
            ),
        ) from exc
    session.refresh(row)
    return _row_to_read(crypto, row)


@router.patch("/{provider_id}", response_model=ProviderRead)
def update_provider(
    provider_id: int,
    payload: ProviderUpdate,
    session: SessionDep,
    crypto: CryptoDep,
) -> ProviderRead:
    """Patch a provider credential row. Omitted fields are unchanged.

    When ``values`` is supplied alongside a schema-aware adapter, the
    payload is validated and split into credentials / options buckets
    the same way ``POST`` does. Legacy callers may still send the
    bucket fields directly.
    """
    row = _get_row_or_404(session, provider_id)

    if payload.display_name is not None:
        row.display_name = payload.display_name

    schema = _schema_for(row.kind, row.provider_name)
    if schema is not None and payload.values is not None:
        existing_creds: dict[str, str] = {}
        try:
            existing_creds = decrypt_json(crypto, row.credentials_encrypted) or {}
        except (CryptoError, ValueError, json.JSONDecodeError):
            existing_creds = {}
        baseline: dict[str, Any] = {}
        baseline.update(existing_creds)
        baseline.update(row.config or {})
        baseline.update(payload.values)
        if payload.credentials is not None:
            baseline.update(payload.credentials)
        if payload.options is not None:
            baseline.update(payload.options)
        merged = {
            k: v for k, v in baseline.items() if schema.field(k) is not None
        }
        errors = validate_payload(schema, merged)
        if errors:
            _raise_validation_errors(errors)
        credentials, options = split_values(schema, merged)
        row.credentials_encrypted = encrypt_json(crypto, credentials)
        row.config = options
    else:
        if payload.credentials is not None:
            row.credentials_encrypted = encrypt_json(crypto, dict(payload.credentials))
        if payload.options is not None:
            row.config = dict(payload.options)

    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                f"another {row.kind.value} provider already has display "
                f"name {row.display_name!r}. Pick a different display name "
                "— multiple instances of the same provider are allowed as "
                "long as their display names are unique."
            ),
        ) from exc
    session.refresh(row)
    return _row_to_read(crypto, row)


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_provider(provider_id: int, session: SessionDep) -> None:
    """Delete a provider credential row."""
    row = _get_row_or_404(session, provider_id)
    session.delete(row)


@router.post("/{provider_id}/activate", response_model=ProviderRead)
def activate_provider(
    provider_id: int,
    session: SessionDep,
    crypto: CryptoDep,
) -> ProviderRead:
    """Mark this provider as the active one for its kind.

    Atomically deactivates every other row of the same kind first so the
    partial unique index on ``(kind) WHERE is_active`` is never violated.
    """
    row = _get_row_or_404(session, provider_id)
    session.execute(
        update(ProviderCredential)
        .where(ProviderCredential.kind == row.kind)
        .where(ProviderCredential.id != row.id)
        .values(is_active=False)
    )
    row.is_active = True
    session.flush()
    session.refresh(row)
    return _row_to_read(crypto, row)


@router.post("/{provider_id}/deactivate", response_model=ProviderRead)
def deactivate_provider(
    provider_id: int,
    session: SessionDep,
    crypto: CryptoDep,
) -> ProviderRead:
    """Unset the active flag on this provider."""
    row = _get_row_or_404(session, provider_id)
    row.is_active = False
    session.flush()
    session.refresh(row)
    return _row_to_read(crypto, row)


# --- preview-without-save endpoints (Johnny-fe.10) -------------------------
#
# Defined BEFORE the parametric /{provider_id}/... routes so FastAPI's
# in-order matcher routes /preview/* paths here rather than treating
# "preview" as a provider_id. Same applies to /catalog/piper/voices below.


class ProviderPreviewPayload(BaseModel):
    """Request body for the /providers/preview/* endpoints.

    ``kind`` + ``provider_name`` jointly identify the registry factory the
    handler instantiates. ``values`` is the same flat dict the structured
    create / update payloads carry — the handler splits it into encrypted
    credentials and plain options based on the provider's field schema.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ProviderKind
    provider_name: str = Field(min_length=1, max_length=64)
    display_name: str = Field(default="Preview", min_length=1, max_length=128)
    values: dict[str, Any] = Field(default_factory=dict)


def _instantiate_preview(
    payload: ProviderPreviewPayload,
    *,
    relax_voice_catalog: bool = False,
) -> STTProvider | LLMProvider | TTSProvider:
    """Build a transient provider instance from a preview payload.

    ``relax_voice_catalog`` suppresses the ``required`` check on
    ``voice_catalog`` fields so the voice-catalog endpoint can list a
    provider's voices before the operator has picked one — otherwise an
    empty (still-unchosen) ``voice_id`` 422s before ``list_voices()`` ever
    runs, leaving the picker empty.
    """
    schema = _schema_for(payload.kind, payload.provider_name)
    if schema is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no registered provider for {payload.kind.value}:{payload.provider_name}"
            ),
        )
    ignore_required = (
        {f.name for f in schema.fields if f.voice_catalog}
        if relax_voice_catalog
        else frozenset()
    )
    errors = validate_payload(schema, payload.values, ignore_required=ignore_required)
    if errors:
        _raise_validation_errors(errors)
    credentials, options = split_values(schema, payload.values)
    config = ProviderConfig(
        kind=payload.kind,
        provider_name=payload.provider_name,
        display_name=payload.display_name,
        credentials=credentials,
        options=options,
    )
    registry = get_registry()
    try:
        return registry.instantiate(config)
    except UnknownProviderError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"provider factory missing: {exc}",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400,
            detail=f"provider construction failed: {exc}",
        ) from exc


async def _voice_catalog_response(instance: TTSProvider) -> VoiceCatalogResponse:
    """Build the unified voice-catalog payload from a TTS instance.

    Calls the provider's ``list_voices()`` (Johnny-1ge.8) and maps each
    :class:`app.providers.base.VoiceMeta` into the wire model. Providers
    that don't enumerate a catalog return an empty list, which the picker
    treats as "fall back to the schema's voice_id options".
    """
    voices = await instance.list_voices()
    return VoiceCatalogResponse(
        voices=[VoiceCatalogVoice(**v.to_dict()) for v in voices]
    )


@router.post("/preview/test", response_model=TestResult)
async def preview_test(payload: ProviderPreviewPayload) -> TestResult:
    """Run a smoke test against the supplied values without saving anything."""
    instance = _instantiate_preview(payload)
    try:
        return await _smoke_test(instance, payload.kind)
    except Exception as exc:  # noqa: BLE001
        return TestResult(ok=False, message="smoke call failed", detail=str(exc))
    finally:
        try:
            await instance.close()
        except Exception:  # noqa: BLE001, S110
            pass


@router.post(
    "/preview/play_sample",
    responses={
        200: {"content": {"audio/wav": {}}},
        400: {"description": "Invalid configuration"},
        404: {"description": "Provider factory missing"},
        502: {"description": "Synthesis failed"},
    },
)
async def preview_play_sample(payload: ProviderPreviewPayload) -> Response:
    """TTS preview-without-save: synthesise the demo phrase, return WAV."""
    if payload.kind is not ProviderKind.TTS:
        raise HTTPException(
            status_code=400,
            detail=f"preview/play_sample only supports TTS, not {payload.kind.value}",
        )
    instance = _instantiate_preview(payload)
    assert isinstance(instance, TTSProvider)
    start = time.perf_counter()
    ttfa_ms = -1
    try:
        chunks: list[bytes] = []
        async for frame in instance.synthesize_stream(TTS_SAMPLE_PHRASE):
            if ttfa_ms < 0:
                ttfa_ms = int((time.perf_counter() - start) * 1000)
            chunks.append(frame)
        pcm = b"".join(chunks)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"synthesis failed: {exc}",
        ) from exc
    finally:
        try:
            await instance.close()
        except Exception:  # noqa: BLE001, S110
            pass
    total_ms = int((time.perf_counter() - start) * 1000)
    if not pcm:
        raise HTTPException(status_code=502, detail="synthesis produced no audio")
    metrics = measure_pcm16(pcm)
    reasons = check_audible(metrics, TTS_SAMPLE_PHRASE)
    wav_bytes = _pcm_to_wav_bytes(pcm)
    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers=_tts_sample_headers(
            instance, ttfa_ms, total_ms, "preview.wav", metrics, reasons
        ),
    )


@router.post("/preview/voices", response_model=VoiceCatalogResponse)
async def preview_voices(
    payload: ProviderPreviewPayload,
) -> VoiceCatalogResponse:
    """Unified voice catalog for an unsaved TTS row (Johnny-1ge.8).

    Lets the voice picker populate before the provider is persisted, the
    same way ``preview/llm_models`` refreshes the model dropdown. Builds a
    transient instance from ``values`` and returns its ``list_voices()``.

    Defined BEFORE ``/{provider_id}/voices`` so the FastAPI matcher routes
    ``/providers/preview/voices`` here instead of parsing ``preview`` as a
    provider id. Validation failures (e.g. a cloud provider needs its API
    key before its catalog can be built) surface as the usual 422 so the
    picker can fall back to the schema's static options.
    """
    if payload.kind is not ProviderKind.TTS:
        raise HTTPException(
            status_code=400,
            detail=f"preview/voices only supports TTS, not {payload.kind.value}",
        )
    instance = _instantiate_preview(payload, relax_voice_catalog=True)
    assert isinstance(instance, TTSProvider)
    try:
        return await _voice_catalog_response(instance)
    except Exception as exc:  # noqa: BLE001 — surface catalog errors as 502
        raise HTTPException(
            status_code=502,
            detail=f"failed to list voices: {exc}",
        ) from exc
    finally:
        try:
            await instance.close()
        except Exception:  # noqa: BLE001, S110
            pass


@router.post("/preview/stt_test", response_model=SttTestResult)
async def preview_stt_test(
    request: Request,
    kind: ProviderKind,
    provider_name: str,
    display_name: str = "Preview",
    values_json: str = "{}",
) -> SttTestResult:
    """STT preview-without-save: send mic PCM body + config query, get transcript."""
    if kind is not ProviderKind.STT:
        raise HTTPException(
            status_code=400,
            detail=f"preview/stt_test only supports STT, not {kind.value}",
        )
    try:
        values = json.loads(values_json) if values_json else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail=f"values_json is not valid JSON: {exc}"
        ) from exc
    if not isinstance(values, dict):
        raise HTTPException(
            status_code=400, detail="values_json must encode a JSON object"
        )
    payload = ProviderPreviewPayload(
        kind=kind,
        provider_name=provider_name,
        display_name=display_name,
        values=values,
    )
    audio = await request.body()
    if len(audio) > STT_TEST_MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"audio body {len(audio)} bytes exceeds limit "
                f"{STT_TEST_MAX_AUDIO_BYTES}"
            ),
        )
    pcm = _wav_to_pcm_or_raw(audio)
    if not pcm:
        raise HTTPException(status_code=400, detail="audio body is empty")
    sample_bytes = PCM_SAMPLE_RATE_HZ * PCM_SAMPLE_WIDTH_BYTES
    audio_ms = int(len(pcm) * 1000 / sample_bytes) if sample_bytes else 0

    instance = _instantiate_preview(payload)
    assert isinstance(instance, STTProvider)

    async def _one_chunk() -> AsyncIterator[bytes]:
        yield pcm

    transcript_pieces: list[str] = []
    started = time.monotonic()
    try:
        async for event in instance.transcribe_stream(_one_chunk()):
            if not event.is_final:
                continue
            text = (event.text or "").strip()
            if text:
                transcript_pieces.append(text)
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.monotonic() - started) * 1000)
        return SttTestResult(
            ok=False,
            transcript="",
            latency_ms=latency_ms,
            cost_usd=_estimate_stt_cost(provider_name, audio_ms),
            audio_ms=audio_ms,
            message="transcription failed",
            detail=str(exc),
        )
    finally:
        try:
            await instance.close()
        except Exception:  # noqa: BLE001, S110
            pass

    latency_ms = int((time.monotonic() - started) * 1000)
    transcript = " ".join(transcript_pieces).strip()
    cost_usd = _estimate_stt_cost(provider_name, audio_ms)
    if not transcript:
        return SttTestResult(
            ok=False,
            transcript="",
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            audio_ms=audio_ms,
            message="provider returned no transcript",
            detail="The microphone audio may be silent or below the provider's noise floor.",
        )
    return SttTestResult(
        ok=True,
        transcript=transcript,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        audio_ms=audio_ms,
        message=f"Transcribed {audio_ms} ms in {latency_ms} ms",
    )


# --- catalog-level Piper voices (Johnny-fe.10) -----------------------------


@router.get("/catalog/piper/voices", response_model=VoiceListResponse)
async def list_catalog_piper_voices() -> VoiceListResponse:
    """List Piper voices using the default model_dir, no saved row required."""
    try:
        voices = await piper_fetch_voice_catalog(PIPER_DEFAULT_MODEL_DIR)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return VoiceListResponse(
        model_dir=PIPER_DEFAULT_MODEL_DIR,
        voices=[VoiceRead(**v.to_dict()) for v in voices],
    )


@router.post(
    "/catalog/piper/voices/{voice_key}/install",
    response_model=VoiceInstallResponse,
)
async def install_catalog_piper_voice(voice_key: str) -> VoiceInstallResponse:
    """Download a Piper voice to the default model_dir, no saved row required."""
    try:
        result = await piper_download_voice(voice_key, PIPER_DEFAULT_MODEL_DIR)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return VoiceInstallResponse(**result)


@router.delete(
    "/catalog/piper/voices/{voice_key}",
    response_model=VoiceRemoveResponse,
)
def remove_catalog_piper_voice(voice_key: str) -> VoiceRemoveResponse:
    """Delete a Piper voice from the default model_dir, no saved row required."""
    try:
        result = piper_remove_voice(voice_key, PIPER_DEFAULT_MODEL_DIR)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"failed to remove voice {voice_key!r}: {exc}",
        ) from exc
    return VoiceRemoveResponse(**result)


# --- LLM model catalog (Johnny-9eq) ---------------------------------------
#
# Each LLM provider exposes a module-level ``fetch_model_catalog`` that
# hits its catalog endpoint and returns a list of ``LLMModelInfo``. The
# two endpoints below — ``GET /providers/{id}/llm_models`` and
# ``POST /providers/preview/llm_models`` — dispatch to the right
# provider's fetcher so the SvelteKit modal can populate the model
# dropdown dynamically. Replaces the prior hand-curated
# :class:`FieldOption` list that went stale every time a provider
# shipped a new model.


def _missing_llm_credentials_message(provider_name: str) -> str:
    """Compose the "enter an API key first" prompt the UI surfaces."""
    if provider_name == OPENAI_COMPATIBLE_PROVIDER_NAME:
        return (
            "enter the base URL for your OpenAI-compatible endpoint "
            "before loading models"
        )
    return f"enter an {provider_name} API key before loading models"


async def _fetch_llm_models(
    provider_name: str,
    *,
    api_key: str | None,
    base_url: str | None,
    anthropic_version: str | None = None,
) -> list[LlmModelRead]:
    """Dispatch ``fetch_model_catalog`` to the right LLM adapter.

    Returns ``LlmModelRead`` pydantic rows so the endpoint can pass them
    straight into ``LlmModelListResponse``. Raises HTTP 400 when the
    provider doesn't yet have the credentials needed to call its catalog
    (so the UI can prompt for them), and HTTP 502 when the upstream call
    fails — the upstream diagnostic is forwarded so the operator can
    debug without checking server logs.
    """
    name = provider_name
    if name == OPENAI_PROVIDER_NAME:
        if not api_key:
            raise HTTPException(
                status_code=400,
                detail=_missing_llm_credentials_message(name),
            )
        try:
            entries = await openai_fetch_model_catalog(
                api_key,
                base_url=base_url or OPENAI_DEFAULT_BASE_URL,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    elif name == ANTHROPIC_PROVIDER_NAME:
        if not api_key:
            raise HTTPException(
                status_code=400,
                detail=_missing_llm_credentials_message(name),
            )
        kwargs: dict[str, Any] = {
            "base_url": base_url or ANTHROPIC_DEFAULT_BASE_URL,
        }
        if anthropic_version:
            kwargs["anthropic_version"] = anthropic_version
        try:
            entries = await anthropic_fetch_model_catalog(api_key, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    elif name == GEMINI_PROVIDER_NAME:
        if not api_key:
            raise HTTPException(
                status_code=400,
                detail=_missing_llm_credentials_message(name),
            )
        try:
            entries = await gemini_fetch_model_catalog(
                api_key,
                base_url=base_url or GEMINI_DEFAULT_BASE_URL,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    elif name == OPENAI_COMPATIBLE_PROVIDER_NAME:
        if not base_url:
            raise HTTPException(
                status_code=400,
                detail=_missing_llm_credentials_message(name),
            )
        try:
            entries = await openai_compatible_fetch_model_catalog(
                base_url, api_key=api_key
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    else:
        raise HTTPException(
            status_code=400,
            detail=(
                f"dynamic model catalog is not implemented for llm:{name}. "
                "Edit the row manually with the field schema's default model "
                "id, or pick a different provider."
            ),
        )
    return [
        LlmModelRead(
            id=info.id, label=info.label, description=info.description
        )
        for info in entries
    ]


@router.post(
    "/preview/llm_models",
    response_model=LlmModelListResponse,
)
async def preview_llm_models(
    payload: ProviderPreviewPayload,
) -> LlmModelListResponse:
    """LLM model catalog preview-without-save (Johnny-9eq).

    Takes the same ``values`` dict the create / update endpoints accept
    and pulls the api_key + base_url + anthropic_version out of it
    without persisting anything. Lets the modal refresh the model
    dropdown the moment the operator pastes a fresh API key — no need
    to save a half-configured row first.

    Defined BEFORE ``/{provider_id}/llm_models`` so the FastAPI matcher
    routes ``/providers/preview/llm_models`` here instead of trying to
    parse ``preview`` as a provider id.
    """
    if payload.kind is not ProviderKind.LLM:
        raise HTTPException(
            status_code=400,
            detail=(
                f"preview/llm_models only supports LLM, not {payload.kind.value}"
            ),
        )
    values = payload.values or {}
    api_key_raw = values.get("api_key")
    api_key = str(api_key_raw).strip() if isinstance(api_key_raw, str) else ""
    base_url_raw = values.get("base_url")
    base_url = (
        str(base_url_raw).strip() if isinstance(base_url_raw, str) else ""
    )
    anthropic_version_raw = values.get("anthropic_version")
    anthropic_version = (
        str(anthropic_version_raw).strip()
        if isinstance(anthropic_version_raw, str)
        else ""
    )
    models = await _fetch_llm_models(
        payload.provider_name,
        api_key=api_key or None,
        base_url=base_url or None,
        anthropic_version=anthropic_version or None,
    )
    return LlmModelListResponse(models=models)


@router.get(
    "/{provider_id}/llm_models",
    response_model=LlmModelListResponse,
)
async def list_llm_models(
    provider_id: int, session: SessionDep, crypto: CryptoDep
) -> LlmModelListResponse:
    """Fetch the live model list for a saved LLM provider row (Johnny-9eq).

    Decrypts the row's saved API key + options, calls the provider's
    ``fetch_model_catalog``, and returns the result. Only valid for
    ``kind=llm`` rows; STT / TTS rows return 400. Auth or transport
    failures forward as 502 with the upstream diagnostic.
    """
    row = _get_row_or_404(session, provider_id)
    if row.kind is not ProviderKind.LLM:
        raise HTTPException(
            status_code=400,
            detail=(
                "llm_models is only available for llm providers "
                f"(got {row.kind.value}:{row.provider_name})"
            ),
        )
    try:
        creds = decrypt_json(crypto, row.credentials_encrypted)
    except (CryptoError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"failed to decrypt credentials: {exc}",
        ) from exc
    options = dict(row.config or {})
    api_key = (creds.get("api_key") or "").strip() if creds else ""
    base_url = str(options.get("base_url") or "").strip()
    anthropic_version = (
        str(options.get("anthropic_version") or "").strip()
        if row.provider_name == ANTHROPIC_PROVIDER_NAME
        else None
    )
    models = await _fetch_llm_models(
        row.provider_name,
        api_key=api_key or None,
        base_url=base_url or None,
        anthropic_version=anthropic_version or None,
    )
    return LlmModelListResponse(models=models)


@router.post("/{provider_id}/test", response_model=TestResult)
async def test_provider(
    provider_id: int,
    session: SessionDep,
    crypto: CryptoDep,
) -> TestResult:
    """Run a one-line smoke call against the provider to verify wiring.

    The test depends on the provider's :class:`ProviderKind`:

    * **STT** — feed 50 ms of silence and consume the transcript stream.
    * **LLM** — send a single ``"say hi"`` user message.
    * **TTS** — synthesize the word ``"hi"`` and count the audio bytes.

    Returns ``TestResult(ok=True, ...)`` on success or
    ``TestResult(ok=False, detail=<error>)`` on any failure — never raises.
    """
    row = _get_row_or_404(session, provider_id)

    registry = get_registry()
    if not registry.has(row.kind, row.provider_name):
        return TestResult(
            ok=False,
            message=f"no factory registered for {row.kind.value}:{row.provider_name}",
        )

    try:
        creds = decrypt_json(crypto, row.credentials_encrypted)
    except (CryptoError, ValueError, json.JSONDecodeError) as exc:
        return TestResult(
            ok=False,
            message="failed to decrypt credentials",
            detail=str(exc),
        )

    config = ProviderConfig(
        kind=row.kind,
        provider_name=row.provider_name,
        display_name=row.display_name,
        credentials=creds,
        options=dict(row.config or {}),
    )

    try:
        instance = registry.instantiate(config)
    except UnknownProviderError as exc:
        return TestResult(ok=False, message="provider factory missing", detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface any factory error to the UI
        return TestResult(ok=False, message="provider construction failed", detail=str(exc))

    try:
        return await _smoke_test(instance, row.kind)
    except Exception as exc:  # noqa: BLE001 — surface any smoke-call error to the UI
        return TestResult(ok=False, message="smoke call failed", detail=str(exc))
    finally:
        try:
            await instance.close()
        except Exception:  # noqa: BLE001, S110 — cleanup best-effort
            pass


# --- stt mic test endpoint -------------------------------------------------

# Hard cap on the audio body the UI can send to the catalog Test button. 5 s of
# 16 kHz mono S16LE PCM is 160_000 bytes; a ceiling of ~1 MiB (~32 s) is
# generous enough that a user can hold the button for a while without
# hitting the cap, while keeping a misbehaving client from spending a
# provider's quota on a 10-minute upload.
STT_TEST_MAX_AUDIO_BYTES = 1024 * 1024


def _wav_to_pcm_or_raw(audio: bytes) -> bytes:
    """Return raw 16 kHz S16LE PCM bytes from a WAV blob, or pass through.

    The catalog UI captures audio through an AudioWorklet that already
    produces 16 kHz mono S16LE samples, so the simplest wire format is
    raw PCM. Some clients (cURL, Postman) find it easier to send a WAV
    blob; this helper strips the RIFF header so the STT adapter always
    sees raw samples. Bodies without a RIFF prefix are assumed to
    already be raw PCM and returned unchanged.
    """
    if len(audio) >= 12 and audio[:4] == b"RIFF" and audio[8:12] == b"WAVE":
        try:
            with wave.open(io.BytesIO(audio), "rb") as wf:
                pcm = wf.readframes(wf.getnframes())
                return pcm
        except wave.Error:
            return audio
    return audio


def _estimate_stt_cost(provider_name: str, audio_ms: int) -> float | None:
    """Return the catalog-rate cost in USD for ``audio_ms`` of audio, or None.

    Local providers report $0 (so the UI shows a tidy "$0.00" line
    rather than nothing). Cloud providers we have a published rate for
    report the rate × duration; providers without one return ``None``
    so the UI hides the cost line for that adapter.
    """
    meta = STT_CATALOG_METADATA.get(provider_name)
    if meta is None:
        return None
    rate = meta.get("cost_per_minute_usd")
    if rate is None:
        return None
    minutes = audio_ms / 1000.0 / 60.0
    return round(float(rate) * minutes, 6)


@router.post("/{provider_id}/stt_test", response_model=SttTestResult)
async def stt_test(
    provider_id: int,
    request: Request,
    session: SessionDep,
    crypto: CryptoDep,
) -> SttTestResult:
    """Transcribe a short mic recording and report latency + cost.

    The ``/providers`` STT tab captures ~5 s of 16 kHz mono S16LE
    PCM from the user's microphone, posts it here as the raw request
    body, and renders the returned transcript next to the Test button.
    The endpoint is the user-facing companion to :func:`test_provider`:
    that one feeds silence to verify wiring; this one feeds real audio
    so the user can judge quality and pronunciation handling.

    Accepts either raw PCM bytes or a WAV blob (the RIFF header is
    stripped before forwarding to the adapter). Rejects non-STT
    providers with 400 so the UI doesn't accidentally fire this at an
    LLM or TTS row, and 413 when the body exceeds
    :data:`STT_TEST_MAX_AUDIO_BYTES` to bound provider spend.
    """
    row = _get_row_or_404(session, provider_id)
    if row.kind is not ProviderKind.STT:
        raise HTTPException(
            status_code=400,
            detail=f"stt_test only supports STT providers, not {row.kind.value}",
        )

    audio = await request.body()
    if len(audio) > STT_TEST_MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"audio body {len(audio)} bytes exceeds limit "
                f"{STT_TEST_MAX_AUDIO_BYTES}"
            ),
        )
    pcm = _wav_to_pcm_or_raw(audio)
    if not pcm:
        raise HTTPException(
            status_code=400,
            detail="audio body is empty",
        )

    sample_bytes = PCM_SAMPLE_RATE_HZ * PCM_SAMPLE_WIDTH_BYTES
    audio_ms = int(len(pcm) * 1000 / sample_bytes) if sample_bytes else 0

    registry = get_registry()
    if not registry.has(row.kind, row.provider_name):
        return SttTestResult(
            ok=False,
            transcript="",
            latency_ms=0,
            cost_usd=None,
            audio_ms=audio_ms,
            message=f"no factory registered for stt:{row.provider_name}",
        )

    try:
        creds = decrypt_json(crypto, row.credentials_encrypted)
    except (CryptoError, ValueError, json.JSONDecodeError) as exc:
        return SttTestResult(
            ok=False,
            transcript="",
            latency_ms=0,
            cost_usd=None,
            audio_ms=audio_ms,
            message="failed to decrypt credentials",
            detail=str(exc),
        )

    config = ProviderConfig(
        kind=row.kind,
        provider_name=row.provider_name,
        display_name=row.display_name,
        credentials=creds,
        options=dict(row.config or {}),
    )

    try:
        instance = registry.instantiate(config)
    except UnknownProviderError as exc:
        return SttTestResult(
            ok=False,
            transcript="",
            latency_ms=0,
            cost_usd=None,
            audio_ms=audio_ms,
            message="provider factory missing",
            detail=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 — surface factory errors to the UI
        return SttTestResult(
            ok=False,
            transcript="",
            latency_ms=0,
            cost_usd=None,
            audio_ms=audio_ms,
            message="provider construction failed",
            detail=str(exc),
        )

    assert isinstance(instance, STTProvider)

    async def _one_chunk() -> AsyncIterator[bytes]:
        # Feed the full utterance as one chunk — the pipeline normally
        # streams 20 ms frames, but the catalog test only cares about
        # the final transcript so we let the adapter buffer the whole
        # body itself.
        yield pcm

    transcript_pieces: list[str] = []
    started = time.monotonic()
    try:
        async for event in instance.transcribe_stream(_one_chunk()):
            # The catalog UI only renders the final concatenated text;
            # partial deltas would just flash on screen and disappear.
            if not event.is_final:
                continue
            text = (event.text or "").strip()
            if text:
                transcript_pieces.append(text)
    except Exception as exc:  # noqa: BLE001 — surface transcription errors
        latency_ms = int((time.monotonic() - started) * 1000)
        return SttTestResult(
            ok=False,
            transcript="",
            latency_ms=latency_ms,
            cost_usd=_estimate_stt_cost(row.provider_name, audio_ms),
            audio_ms=audio_ms,
            message="transcription failed",
            detail=str(exc),
        )
    finally:
        try:
            await instance.close()
        except Exception:  # noqa: BLE001, S110 — cleanup best-effort
            pass

    latency_ms = int((time.monotonic() - started) * 1000)
    transcript = " ".join(transcript_pieces).strip()
    cost_usd = _estimate_stt_cost(row.provider_name, audio_ms)

    if not transcript:
        # Provider succeeded but produced no usable text — still a useful
        # signal (mic muted, silence, noise gate). Report ok=False so the
        # UI shows an empty-transcript warning rather than a green checkmark.
        return SttTestResult(
            ok=False,
            transcript="",
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            audio_ms=audio_ms,
            message="provider returned no transcript",
            detail="The microphone audio may be silent or below the provider's noise floor.",
        )

    return SttTestResult(
        ok=True,
        transcript=transcript,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        audio_ms=audio_ms,
        message=f"Transcribed {audio_ms} ms in {latency_ms} ms",
    )


# --- play sample endpoint --------------------------------------------------

# A short, neutral phrase the user hears when previewing a TTS voice. Picked
# to exercise common phonemes (sibilants, vowels, "th") so the user can judge
# pronunciation, cadence, and gender at a glance without committing to a full
# meeting. Kept short to bound synthesis cost.
TTS_SAMPLE_PHRASE = (
    "Hi there! This is a quick voice sample so you can hear how I sound."
)


def _pcm_to_wav_bytes(pcm: bytes) -> bytes:
    """Wrap raw 16 kHz mono S16LE PCM in a RIFF/WAV container.

    All TTS adapters yield this canonical format (see
    :data:`PCM_SAMPLE_RATE_HZ`) so the browser can play the response with
    a plain ``<audio>`` tag — no decoder shim needed.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(PCM_CHANNELS)
        wf.setsampwidth(PCM_SAMPLE_WIDTH_BYTES)
        wf.setframerate(PCM_SAMPLE_RATE_HZ)
        wf.writeframes(pcm)
    return buf.getvalue()


def _tts_sample_headers(
    instance: TTSProvider,
    ttfa_ms: int,
    total_ms: int,
    filename: str,
    metrics: AudioMetrics,
    audible_reasons: list[str],
) -> dict[str, str]:
    """Response headers for a TTS sample, stamping runtime + timing + audio.

    ``X-TTS-Runtime`` lets the /providers UI show which runtime served the
    audio (the Piper runtime picker, Johnny-1ge.1). Providers without a
    ``runtime`` attribute (cloud TTS) get an empty value.

    ``X-TTS-Audio-Bytes`` / ``X-TTS-Audio-Ms`` / ``X-TTS-Peak`` carry the
    "is there audible speech?" measurements (Johnny-1ge.7), and
    ``X-TTS-Audible`` is the verdict (``1`` audible, ``0`` silent/short) with
    ``X-TTS-Audible-Reason`` spelling out *why* when ``0``. The frontend reads
    these to warn on a silent sample; ``johnny-tts-smoke`` reads them to render
    PASS/FAIL per (provider × runtime). All listed in the CORS
    ``expose_headers`` so the browser can read them.
    """
    return {
        "Cache-Control": "no-store",
        "Content-Disposition": f'inline; filename="{filename}"',
        "X-TTS-Runtime": str(getattr(instance, "runtime", "") or ""),
        "X-TTS-TTFA-Ms": str(max(ttfa_ms, 0)),
        "X-TTS-Total-Ms": str(total_ms),
        "X-TTS-Audio-Bytes": str(metrics.audio_bytes),
        "X-TTS-Audio-Ms": str(metrics.audio_ms),
        "X-TTS-Peak": f"{metrics.peak_amplitude:.4f}",
        "X-TTS-Audible": "0" if audible_reasons else "1",
        "X-TTS-Audible-Reason": "; ".join(audible_reasons),
    }


@router.post(
    "/{provider_id}/play_sample",
    responses={
        200: {"content": {"audio/wav": {}}},
        400: {"description": "Provider is not a TTS provider"},
        404: {"description": "Provider not found"},
        502: {"description": "Synthesis failed"},
    },
)
async def play_sample(
    provider_id: int,
    session: SessionDep,
    crypto: CryptoDep,
    overrides: PlaySampleRequest | None = None,
) -> Response:
    """Synthesize a short demo phrase via this provider and return WAV audio.

    Only valid for TTS providers — STT/LLM rows return 400. Unlike
    :func:`test_provider`, which is a config-validity smoke test, this is
    a voice-quality preview the user explicitly clicks to hear before
    committing the provider to a live meeting. The response body is a
    self-contained RIFF/WAV at 16 kHz mono S16LE so the browser can play
    it inline with a plain ``Audio`` element.

    Optional request body:

    * ``voice_id`` — override ``options["voice_id"]`` for this single
      synth call only. The saved row is **not** mutated. The Piper voice
      browser modal uses this to preview a freshly-installed voice
      without forcing the user to save the provider first.
    """
    row = _get_row_or_404(session, provider_id)

    if row.kind is not ProviderKind.TTS:
        raise HTTPException(
            status_code=400,
            detail=f"play_sample only supports TTS providers, not {row.kind.value}",
        )

    registry = get_registry()
    if not registry.has(row.kind, row.provider_name):
        raise HTTPException(
            status_code=502,
            detail=f"no factory registered for tts:{row.provider_name}",
        )

    try:
        creds = decrypt_json(crypto, row.credentials_encrypted)
    except (CryptoError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"failed to decrypt credentials: {exc}",
        ) from exc

    options = dict(row.config or {})
    if overrides is not None and overrides.voice_id is not None:
        options["voice_id"] = overrides.voice_id
    if overrides is not None and overrides.runtime is not None:
        options["runtime"] = overrides.runtime

    config = ProviderConfig(
        kind=row.kind,
        provider_name=row.provider_name,
        display_name=row.display_name,
        credentials=creds,
        options=options,
    )

    try:
        instance = registry.instantiate(config)
    except UnknownProviderError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"provider factory missing: {exc}",
        ) from exc
    except Exception as exc:  # noqa: BLE001 — surface any factory error
        raise HTTPException(
            status_code=502,
            detail=f"provider construction failed: {exc}",
        ) from exc

    assert isinstance(instance, TTSProvider)
    start = time.perf_counter()
    ttfa_ms = -1
    try:
        chunks: list[bytes] = []
        async for frame in instance.synthesize_stream(TTS_SAMPLE_PHRASE):
            if ttfa_ms < 0:
                ttfa_ms = int((time.perf_counter() - start) * 1000)
            chunks.append(frame)
        pcm = b"".join(chunks)
    except Exception as exc:  # noqa: BLE001 — surface any synth error
        raise HTTPException(
            status_code=502,
            detail=f"synthesis failed: {exc}",
        ) from exc
    finally:
        try:
            await instance.close()
        except Exception:  # noqa: BLE001, S110 — cleanup best-effort
            pass
    total_ms = int((time.perf_counter() - start) * 1000)

    if not pcm:
        raise HTTPException(
            status_code=502,
            detail="synthesis produced no audio",
        )

    metrics = measure_pcm16(pcm)
    reasons = check_audible(metrics, TTS_SAMPLE_PHRASE)
    wav_bytes = _pcm_to_wav_bytes(pcm)
    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers=_tts_sample_headers(
            instance, ttfa_ms, total_ms, "sample.wav", metrics, reasons
        ),
    )


# --- piper voice catalog endpoints -----------------------------------------


def _piper_model_dir(row: ProviderCredential) -> str:
    """Return the model_dir for a Piper provider row, falling back to default."""
    config = row.config or {}
    raw = config.get("model_dir")
    if isinstance(raw, str) and raw.strip():
        return raw
    return PIPER_DEFAULT_MODEL_DIR


def _require_piper_row(session: Session, provider_id: int) -> ProviderCredential:
    """Look up a provider row and assert it is the Local Piper adapter.

    The voices endpoints are Piper-specific (the rhasspy/piper-voices
    catalog only describes Piper models), so we reject any other kind
    of row with a 400. 404 still applies when the id is unknown.
    """
    row = _get_row_or_404(session, provider_id)
    if row.kind is not ProviderKind.TTS or row.provider_name != PIPER_PROVIDER_NAME:
        raise HTTPException(
            status_code=400,
            detail=(
                "voice catalog is only available for tts:piper providers "
                f"(got {row.kind.value}:{row.provider_name})"
            ),
        )
    return row


@router.get("/{provider_id}/voices")
async def list_voices(
    provider_id: int, session: SessionDep, crypto: CryptoDep
) -> VoiceCatalogResponse:
    """List a TTS provider's voice catalog (Johnny-fe.10 + Johnny-1ge.8/.9).

    Returns the unified ``{voices:[VoiceMeta]}`` shape for every TTS
    provider so the shared picker renders them identically:

    * **Piper** — built from the rhasspy index; each voice carries
      ``installed=True`` when both the ``.onnx`` and ``.onnx.json`` files
      are present in the row's ``model_dir`` so the picker can render
      Install vs. Remove without an extra round-trip (Johnny-1ge.9).
    * **Every other TTS provider** — the unified ``{voices:[VoiceMeta]}``
      catalog (Johnny-1ge.8) built from the adapter's ``list_voices()``,
      so the shared picker renders Kokoro / OpenAI / future providers with
      the same language / gender / sample-rate metadata and per-voice
      preview.

    Returns HTTP 400 when the row is not a TTS provider, or HTTP 502 when
    the catalog can't be built (network failure for Piper / cloud, decrypt
    error, missing factory). The upstream message rides in ``detail`` so
    the operator can debug from the browser.
    """
    row = _get_row_or_404(session, provider_id)
    if row.kind is not ProviderKind.TTS:
        raise HTTPException(
            status_code=400,
            detail=(
                "voice catalog is only available for TTS providers "
                f"(got {row.kind.value}:{row.provider_name})"
            ),
        )

    if row.provider_name == PIPER_PROVIDER_NAME:
        # Piper converged onto the unified picker (Johnny-1ge.9): return the
        # shared VoiceMeta shape (carrying per-voice ``installed`` so the
        # picker can render Install / Remove) instead of the legacy
        # install-aware ``{model_dir, voices:[{key …}]}`` browser shape.
        model_dir = _piper_model_dir(row)
        try:
            voices = await piper_fetch_voice_catalog(model_dir)
        except Exception as exc:  # noqa: BLE001 — surface fetch errors as 502
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return VoiceCatalogResponse(
            voices=[
                VoiceCatalogVoice(**piper_voice_info_to_meta(v).to_dict())
                for v in voices
            ],
        )

    registry = get_registry()
    if not registry.has(row.kind, row.provider_name):
        raise HTTPException(
            status_code=502,
            detail=f"no factory registered for tts:{row.provider_name}",
        )
    try:
        creds = decrypt_json(crypto, row.credentials_encrypted)
    except (CryptoError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"failed to decrypt credentials: {exc}",
        ) from exc
    config = ProviderConfig(
        kind=row.kind,
        provider_name=row.provider_name,
        display_name=row.display_name,
        credentials=creds,
        options=dict(row.config or {}),
    )
    try:
        instance = registry.instantiate(config)
    except Exception as exc:  # noqa: BLE001 — factory error → 502
        raise HTTPException(
            status_code=502,
            detail=f"provider construction failed: {exc}",
        ) from exc
    assert isinstance(instance, TTSProvider)
    try:
        return await _voice_catalog_response(instance)
    except Exception as exc:  # noqa: BLE001 — surface catalog errors as 502
        raise HTTPException(
            status_code=502,
            detail=f"failed to list voices: {exc}",
        ) from exc
    finally:
        try:
            await instance.close()
        except Exception:  # noqa: BLE001, S110
            pass


@router.post(
    "/{provider_id}/voices/{voice_key}/install",
    response_model=VoiceInstallResponse,
)
async def install_voice(
    provider_id: int,
    voice_key: str,
    session: SessionDep,
) -> VoiceInstallResponse:
    """Download a Piper voice into this provider's ``model_dir``.

    The call is idempotent: a voice already present on disk returns
    ``installed=True, already_present=True`` without re-downloading.
    Partial files from a previous interrupted install are overwritten,
    so re-clicking Install is the right recovery action.

    Returns HTTP 400 when the row isn't a Piper TTS adapter or the voice
    key isn't in the catalog. Returns HTTP 502 when the download
    transport fails — the error message includes the upstream cause.
    """
    row = _require_piper_row(session, provider_id)
    model_dir = _piper_model_dir(row)
    try:
        result = await piper_download_voice(voice_key, model_dir)
    except Exception as exc:  # noqa: BLE001 — surface download errors
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return VoiceInstallResponse(**result)


def _require_cartesia_row(
    session: Session, provider_id: int
) -> ProviderCredential:
    """Look up a provider row and assert it is the Cartesia TTS adapter.

    The voices endpoint hits Cartesia's ``GET /voices`` with the row's
    saved API key — only meaningful for a Cartesia row. Reject anything
    else with a 400 so the caller sees a precise diagnostic instead of an
    auth failure against the wrong provider.
    """
    row = _get_row_or_404(session, provider_id)
    if (
        row.kind is not ProviderKind.TTS
        or row.provider_name != CARTESIA_PROVIDER_NAME
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "cartesia voice catalog is only available for tts:cartesia "
                f"providers (got {row.kind.value}:{row.provider_name})"
            ),
        )
    return row


@router.get(
    "/{provider_id}/cartesia/voices",
    response_model=CartesiaVoiceListResponse,
)
async def list_cartesia_voices(
    provider_id: int, session: SessionDep, crypto: CryptoDep
) -> CartesiaVoiceListResponse:
    """List every Cartesia voice via the row's saved API key.

    Returns HTTP 400 when the provider row is not a Cartesia TTS adapter
    or 502 when the upstream call fails (network / auth / malformed
    payload). The error detail forwards the Cartesia diagnostic so an
    operator can debug from the browser without checking server logs.
    """
    row = _require_cartesia_row(session, provider_id)
    try:
        creds = decrypt_json(crypto, row.credentials_encrypted)
    except (CryptoError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"failed to decrypt credentials: {exc}",
        ) from exc
    api_key = creds.get("api_key") or ""
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="cartesia provider row has no api_key in credentials",
        )
    options = dict(row.config or {})
    base_url = str(options.get("base_url") or "").strip()
    api_version = str(options.get("api_version") or "").strip()
    fetch_kwargs: dict[str, Any] = {}
    if base_url:
        fetch_kwargs["base_url"] = base_url
    if api_version:
        fetch_kwargs["api_version"] = api_version
    try:
        voices = await cartesia_fetch_voice_catalog(api_key, **fetch_kwargs)
    except Exception as exc:  # noqa: BLE001 — surface fetch errors as 502
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return CartesiaVoiceListResponse(
        voices=[CartesiaVoiceRead(**v.to_dict()) for v in voices],
    )


@router.delete(
    "/{provider_id}/voices/{voice_key}",
    response_model=VoiceRemoveResponse,
)
def remove_voice_endpoint(
    provider_id: int,
    voice_key: str,
    session: SessionDep,
) -> VoiceRemoveResponse:
    """Delete a Piper voice from this provider's ``model_dir``.

    Removes both ``<voice_key>.onnx`` and ``<voice_key>.onnx.json`` from
    the filesystem. Useful from the voice browser modal so the user can
    reclaim disk space without dropping into a shell. The provider row
    itself is *not* mutated: if the deleted voice happens to be the row's
    currently-saved ``voice_id``, the row keeps that string and the next
    synth call will raise the same "voice not found" error it would have
    raised after any manual deletion — surfacing the inconsistency
    quickly rather than silently switching voices.

    Returns HTTP 400 when the row isn't a Piper TTS adapter, HTTP 404
    when neither voice file is present (idempotency caller can detect),
    and HTTP 502 on filesystem permission errors.
    """
    row = _require_piper_row(session, provider_id)
    model_dir = _piper_model_dir(row)
    try:
        result = piper_remove_voice(voice_key, model_dir)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"failed to remove voice {voice_key!r}: {exc}",
        ) from exc
    return VoiceRemoveResponse(**result)


# --- parakeet runtime package install -------------------------------------


@router.get("/{provider_id}/package")
def get_provider_package(
    provider_id: int,
    session: SessionDep,
) -> dict[str, object]:
    """Return runtime-package status for providers that ship a heavy
    dependency stack outside the image (currently only Parakeet).

    For everything else the response is ``{"applicable": false}`` so the
    UI can skip rendering an Install button — the catalog Test button
    works directly against deps that are already baked into the api
    image.
    """
    from app.services.parakeet_packages import package_status

    row = _get_row_or_404(session, provider_id)
    if row.provider_name != "parakeet":
        return {"applicable": False}
    return {"applicable": True, **package_status()}


@router.post("/{provider_id}/package/install")
def install_provider_package(
    provider_id: int,
    session: SessionDep,
) -> StreamingResponse:
    """Run ``uv pip install`` for the Parakeet package stack at runtime.

    The install lands in a host bind-mounted directory (set by the
    ``JOHNNY_PARAKEET_PACKAGES_DIR`` env, defaulting to
    ``/var/lib/johnny/parakeet-packages``) so it persists across
    container restarts and image rebuilds — same pattern as the Piper
    voice catalog (``POST /providers/{id}/voices/{voice}/install``).

    The body is a ``text/plain`` stream of pip's combined stdout/stderr
    so the frontend can show a live tail instead of staring at a
    spinner for the 5–10 minute first install. Two terminal markers:
    ``[install ok — packages at …]`` on success, ``[install failed —
    exit code …]`` on failure. After success the api process's
    ``sys.path`` is updated in place — no container restart needed.
    """
    from app.services.parakeet_packages import install_packages_stream

    row = _get_row_or_404(session, provider_id)
    if row.provider_name != "parakeet":
        raise HTTPException(
            status_code=400,
            detail=(
                f"runtime package install only supported for "
                f"provider 'parakeet', not {row.provider_name!r}"
            ),
        )
    return StreamingResponse(
        install_packages_stream(),
        media_type="text/plain; charset=utf-8",
    )

# --- export endpoint -------------------------------------------------------


@router.get(
    "/export",
    responses={
        200: {"content": {"application/json": {}}},
    },
)
def export_providers(
    session: SessionDep,
    crypto: CryptoDep,
    with_secrets: bool = False,
) -> Response:
    """Download every configured provider as a single JSON file (Johnny-k3z).

    The response body matches the schema consumed by the startup seeder
    (:mod:`app.services.providers_seed`) so an ``export → file → import``
    roundtrip reproduces the exact provider state. Filename follows the
    ``johnny-providers-YYYY-MM-DD.json`` convention so multiple backups
    sort naturally.

    The ``with_secrets`` query parameter (default ``false``) controls
    whether credentials are exported:

    * ``false`` — credentials become empty dicts. The file is safe to
      share with teammates or check into a repo; re-importing fills in
      the structured options but leaves credentials blank, so the user
      must paste their keys back in via the UI.
    * ``true`` — credentials are decrypted and embedded in plaintext.
      The resulting file IS the secret store and must be handled like
      one. The endpoint refuses to silently include partial secrets:
      a row whose ciphertext can't be decrypted exports as an empty
      credentials dict (corruption is surfaced via the importer's
      validation on re-load, not silently passed through).
    """
    rows = session.scalars(
        select(ProviderCredential)
        # Live kinds only — a tombstoned ``s2s`` row (Johnny-trt.43) would
        # crash the enum coercion at load.
        .where(ProviderCredential.kind.in_(list(ProviderKind)))
        .order_by(
            ProviderCredential.kind,
            ProviderCredential.display_name,
            ProviderCredential.id,
        )
    ).all()

    providers: list[dict[str, Any]] = []
    for row in rows:
        credentials: dict[str, str] = {}
        if with_secrets:
            try:
                credentials = decrypt_json(crypto, row.credentials_encrypted)
            except (CryptoError, ValueError, json.JSONDecodeError):
                # Corrupted ciphertext / key mismatch: leave credentials empty
                # rather than 500ing — the user can still recover the rest of
                # the inventory and re-enter the broken row's secrets by hand.
                credentials = {}
        providers.append(
            {
                "kind": row.kind.value,
                "provider_name": row.provider_name,
                "display_name": row.display_name,
                "credentials": credentials,
                "options": dict(row.config or {}),
                "is_active": row.is_active,
            }
        )

    payload = {
        "version": SUPPORTED_FILE_VERSION,
        "providers": providers,
    }
    body = json.dumps(payload, indent=2, sort_keys=False)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    filename = f"johnny-providers-{today}.json"
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


async def _smoke_test(
    instance: STTProvider | LLMProvider | TTSProvider,
    kind: ProviderKind,
) -> TestResult:
    """Dispatch to the kind-appropriate smoke call."""
    if kind is ProviderKind.STT:
        assert isinstance(instance, STTProvider)
        # 200 ms of silence — long enough to clear OpenAI Realtime's 100 ms
        # minimum buffer-commit threshold while still cheap for the others.
        silence_frame = b"\x00" * (PCM_SAMPLE_RATE_HZ * PCM_SAMPLE_WIDTH_BYTES // 5)

        async def _one_silence() -> AsyncIterator[bytes]:
            yield silence_frame

        events = [ev async for ev in instance.transcribe_stream(_one_silence())]
        return TestResult(
            ok=True,
            message=f"STT smoke OK — {len(events)} transcript event(s)",
        )
    if kind is ProviderKind.LLM:
        assert isinstance(instance, LLMProvider)
        resp = await instance.chat([ChatMessage(role="user", content="say hi")])
        snippet = (resp.text or "").strip().splitlines()[0][:80] if resp.text else ""
        return TestResult(
            ok=True,
            message=f"LLM smoke OK — finish_reason={resp.finish_reason}",
            detail=snippet or None,
        )
    if kind is ProviderKind.TTS:
        assert isinstance(instance, TTSProvider)
        total_bytes = 0
        async for frame in instance.synthesize_stream("hi"):
            total_bytes += len(frame)
        return TestResult(ok=True, message=f"TTS smoke OK — {total_bytes} byte(s)")
    raise AssertionError(f"unreachable: unknown kind {kind!r}")


__all__ = [
    "ProviderCreate",
    "ProviderListResponse",
    "ProviderRead",
    "ProviderUpdate",
    "SchemaListResponse",
    "SttCatalogEntry",
    "SttCatalogResponse",
    "SttTestResult",
    "TestResult",
    "router",
]
