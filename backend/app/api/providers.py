"""Provider configuration HTTP endpoints (US-018).

Manages rows in ``provider_credentials``: list / create / update / delete,
mark one provider per kind as active, and run a one-shot smoke call against
the registered factory to verify credentials work end-to-end.

The active-per-kind invariant is enforced both by a partial unique index
on ``(kind) WHERE is_active`` and by ``activate_provider`` which deactivates
any sibling rows before flipping the requested row on.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_crypto, get_session
from app.db.models import ProviderCredential
from app.providers.base import (
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
from app.security.crypto import CredentialCrypto, CryptoError, decrypt_json, encrypt_json

router = APIRouter(prefix="/providers", tags=["providers"])


# --- Pydantic schemas ------------------------------------------------------


class ProviderCreate(BaseModel):
    """Payload for creating a provider credential row."""

    kind: ProviderKind
    provider_name: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    credentials: dict[str, str] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


class ProviderUpdate(BaseModel):
    """Patch payload — fields left as ``None`` are not modified."""

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    credentials: dict[str, str] | None = None
    options: dict[str, Any] | None = None


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


# --- Endpoints -------------------------------------------------------------


SessionDep = Annotated[Session, Depends(get_session)]
CryptoDep = Annotated[CredentialCrypto, Depends(get_crypto)]


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
    """Create a new provider credential row. Always created inactive."""
    row = ProviderCredential(
        kind=payload.kind,
        provider_name=payload.provider_name,
        display_name=payload.display_name,
        credentials_encrypted=encrypt_json(crypto, dict(payload.credentials)),
        config=dict(payload.options),
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
    """Patch a provider credential row. Omitted fields are unchanged."""
    row = _get_row_or_404(session, provider_id)
    if payload.display_name is not None:
        row.display_name = payload.display_name
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


async def _smoke_test(
    instance: STTProvider | LLMProvider | TTSProvider,
    kind: ProviderKind,
) -> TestResult:
    """Dispatch to the kind-appropriate smoke call."""
    if kind is ProviderKind.STT:
        assert isinstance(instance, STTProvider)
        silence_frame = b"\x00" * (PCM_SAMPLE_RATE_HZ * PCM_SAMPLE_WIDTH_BYTES // 20)

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
    "TestResult",
    "router",
]
