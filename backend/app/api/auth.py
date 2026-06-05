"""Google OAuth 2.0 desktop-flow endpoints (US-005).

Two endpoints implement the standard authorization-code flow:

* ``POST /auth/google/start`` — build the consent URL using the configured
  ``GOOGLE_CLIENT_ID`` and return it for the client to open in a browser.
  The client picks a per-session ``state`` value and posts it; the server
  echoes it back inside the URL so it round-trips through Google.

* ``POST /auth/google/callback`` — exchange the authorization code Google
  redirected back with for an access + refresh token pair, fetch the
  user's email and profile via the OpenID userinfo endpoint, and persist
  the encrypted tokens to ``google_accounts``.

Tokens are encrypted with the application-wide Fernet key before going to
the database. Decryption only happens inside the shared
:class:`~app.services.google_client.GoogleApiClient` wrapper.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import get_crypto, get_session
from app.config import Settings, get_settings
from app.db.models import AccountRole
from app.security.crypto import CredentialCrypto
from app.services.google_client import upsert_account_from_tokens
from app.services.google_oauth import (
    GoogleOAuthError,
    build_authorize_url,
    exchange_code_for_tokens,
    fetch_userinfo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/google", tags=["auth"])


# --- Pydantic schemas ------------------------------------------------------


class StartRequest(BaseModel):
    """Optional knobs for the consent URL.

    ``state`` is auto-generated server-side if the client doesn't supply
    one. ``role`` and ``is_default_user`` are remembered in the in-memory
    pending-state map keyed by the state token, so the callback knows
    how to label the resulting account row.
    """

    role: AccountRole = AccountRole.USER
    is_default_user: bool = False
    state: str | None = Field(default=None, max_length=128)


class StartResponse(BaseModel):
    """Consent URL the client should open in a browser."""

    authorize_url: str
    state: str


class CallbackRequest(BaseModel):
    """Code Google passed back via the redirect URI."""

    code: str = Field(min_length=1, max_length=2048)
    state: str = Field(min_length=1, max_length=128)


class AccountRead(BaseModel):
    """Public view of a :class:`GoogleAccount` row (no tokens exposed)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    role: AccountRole
    is_default_user: bool
    token_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


# --- In-memory state map ---------------------------------------------------


class _PendingState(BaseModel):
    """The role / default-user choice the client made at /start time.

    Kept in-memory because the OAuth flow is single-user, single-process,
    and the state lifetime is the time it takes the user to click through
    Google's consent screen (seconds to minutes).
    """

    role: AccountRole
    is_default_user: bool


_pending_states: dict[str, _PendingState] = {}


def _remember_state(state: str, payload: _PendingState) -> None:
    _pending_states[state] = payload


def _consume_state(state: str) -> _PendingState | None:
    return _pending_states.pop(state, None)


def _peek_state(state: str) -> _PendingState | None:
    return _pending_states.get(state)


# --- Helpers ---------------------------------------------------------------


def _require_oauth_config(settings: Settings) -> tuple[str, str]:
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            status_code=503,
            detail=(
                "Google OAuth is not configured: set GOOGLE_CLIENT_ID and "
                "GOOGLE_CLIENT_SECRET in the environment"
            ),
        )
    return settings.google_client_id, settings.google_client_secret


# --- Endpoints -------------------------------------------------------------


SessionDep = Annotated[Session, Depends(get_session)]
CryptoDep = Annotated[CredentialCrypto, Depends(get_crypto)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.post("/start", response_model=StartResponse)
def start(payload: StartRequest, settings: SettingsDep) -> StartResponse:
    """Return the URL the user should open to grant consent."""
    client_id, _ = _require_oauth_config(settings)
    state = payload.state or secrets.token_urlsafe(32)
    _remember_state(
        state,
        _PendingState(role=payload.role, is_default_user=payload.is_default_user),
    )
    authorize_url = build_authorize_url(
        client_id=client_id,
        redirect_uri=settings.google_oauth_redirect_uri,
        state=state,
    )
    return StartResponse(authorize_url=authorize_url, state=state)


@router.post(
    "/callback",
    response_model=AccountRead,
    status_code=status.HTTP_201_CREATED,
)
async def callback(
    payload: CallbackRequest,
    session: SessionDep,
    crypto: CryptoDep,
    settings: SettingsDep,
) -> AccountRead:
    """Exchange the code for tokens and persist the encrypted account row."""
    client_id, client_secret = _require_oauth_config(settings)
    pending = _consume_state(payload.state)
    if pending is None:
        raise HTTPException(status_code=400, detail="unknown or expired state")

    try:
        tokens = await exchange_code_for_tokens(
            code=payload.code,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=settings.google_oauth_redirect_uri,
        )
    except GoogleOAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        userinfo = await fetch_userinfo(access_token=tokens.access_token)
    except GoogleOAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    assert tokens.refresh_token is not None  # exchange_code raises otherwise
    row = upsert_account_from_tokens(
        session=session,
        crypto=crypto,
        email=userinfo.email,
        role=pending.role,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_at=tokens.expires_at,
        is_default_user=pending.is_default_user,
    )
    return AccountRead.model_validate(row)


__all__ = [
    "AccountRead",
    "CallbackRequest",
    "StartRequest",
    "StartResponse",
    "_consume_state",
    "_peek_state",
    "_pending_states",
    "_remember_state",
    "router",
]
