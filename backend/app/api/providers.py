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
import wave
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_crypto, get_session
from app.db.models import ProviderCredential
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


class PlaySampleRequest(BaseModel):
    """Optional request body for ``POST /providers/{id}/play_sample``.

    The body is optional — calling without a body preserves the historic
    behavior of synthesising with the row's saved config. When provided,
    ``voice_id`` overrides the row's ``options["voice_id"]`` for this
    single synth call only; the database row is *not* mutated. This is
    what the Piper voice browser modal uses to preview an installed
    voice without forcing the user to re-save the provider first.
    """

    model_config = ConfigDict(extra="forbid")

    voice_id: str | None = Field(default=None, max_length=256)


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
    for kind in (ProviderKind.STT, ProviderKind.LLM, ProviderKind.TTS):
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


# --- Endpoints -------------------------------------------------------------


SessionDep = Annotated[Session, Depends(get_session)]
CryptoDep = Annotated[CredentialCrypto, Depends(get_crypto)]


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
    """List every configured provider grouped by kind."""
    rows = session.scalars(
        select(ProviderCredential).order_by(
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
            detail="a provider with this kind/name/display already exists",
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
            detail="conflicts with another provider with the same kind/name/display",
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
    try:
        chunks: list[bytes] = []
        async for frame in instance.synthesize_stream(TTS_SAMPLE_PHRASE):
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

    if not pcm:
        raise HTTPException(
            status_code=502,
            detail="synthesis produced no audio",
        )

    wav_bytes = _pcm_to_wav_bytes(pcm)
    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'inline; filename="sample.wav"',
        },
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


@router.get("/{provider_id}/voices", response_model=VoiceListResponse)
async def list_voices(
    provider_id: int, session: SessionDep
) -> VoiceListResponse:
    """List every Piper voice from the public rhasspy catalog.

    Each voice is annotated with ``installed=True`` when both the
    ``.onnx`` and ``.onnx.json`` files are already in the provider's
    configured ``model_dir`` so the UI can render Install vs.
    Reinstall affordances without an extra round-trip.

    Returns HTTP 400 when the provider row is not a Piper TTS adapter,
    or HTTP 502 when the huggingface catalog cannot be fetched (network
    failure, malformed payload, etc.) — the upstream error message is
    included in ``detail`` so the operator can debug without checking
    server logs.
    """
    row = _require_piper_row(session, provider_id)
    model_dir = _piper_model_dir(row)
    try:
        voices = await piper_fetch_voice_catalog(model_dir)
    except Exception as exc:  # noqa: BLE001 — surface fetch errors as 502
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return VoiceListResponse(
        model_dir=model_dir,
        voices=[VoiceRead(**v.to_dict()) for v in voices],
    )


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
        select(ProviderCredential).order_by(
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
    "TestResult",
    "router",
]
