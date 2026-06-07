"""Google account endpoints: connect calendars, list, disconnect.

The accounts redesign (Johnny-pia) collapses the previous
calendar/bot distinction to a single row per Google identity with
derived capabilities. The schema dropped ``role`` and
``is_default_user``; a row is a calendar source iff it carries an
encrypted refresh token, and a bot identity iff a Playwright
``storage_state.json`` exists for it on the shared volume.

Endpoints in this module own the **calendar** half of the surface:

* ``POST /auth/google/start`` — build the consent URL and remember the
  ``state`` token in process memory.
* ``POST /auth/google/callback`` / ``GET /auth/google/callback`` —
  programmatic and browser-facing variants of the OAuth code exchange.
  The browser variant posts ``johnny:oauth`` back to the opener so the
  Settings tab can refresh.
* ``GET /auth/google/accounts`` and ``GET /auth/google/accounts/{id}``
  — list / fetch. Response shape folds the bot-session status in so
  the UI doesn't need a second round-trip per account.
* ``DELETE /auth/google/accounts/{id}`` — revoke the refresh token (if
  any) at Google and delete the row. HTTP 409 if meeting configs
  reference the account; pass ``?force=true`` to cascade-delete.
* ``DELETE /auth/google/accounts/{id}/bot-session`` — remove just the
  bot capability (clears the stored ``storage_state.json``). The row
  stays if it still carries calendar capability.

The bot **sign-in** flow (noVNC-based) lives in
:mod:`app.api.bot_signin`.

Tokens are encrypted with the application-wide Fernet key before going
to the database. Decryption only happens inside the shared
:class:`~app.services.google_client.GoogleApiClient` wrapper.
"""

from __future__ import annotations

import html
import logging
import secrets
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.deps import get_crypto, get_session
from app.config import Settings, get_settings
from app.db.models import GoogleAccount, MeetingConfig
from app.security.crypto import CredentialCrypto
from app.services.bot_auth_seed import bot_session_status, delete_bot_session
from app.services.google_client import (
    GoogleApiClientError,
    can_decrypt_refresh_token,
    revoke_account,
    upsert_account_from_tokens,
)
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
    one. No role or default flag — the redesign collapsed both away.
    """

    state: str | None = Field(default=None, max_length=128)


class StartResponse(BaseModel):
    """Consent URL the client should open in a browser."""

    authorize_url: str
    state: str


class CallbackRequest(BaseModel):
    """Code Google passed back via the redirect URI."""

    code: str = Field(min_length=1, max_length=2048)
    state: str = Field(min_length=1, max_length=128)


TokenHealth = Literal["ok", "needs_reauth", "none"]


class BotSessionView(BaseModel):
    """Whether a bot account's Playwright ``storage_state.json`` exists.

    Folded into :class:`AccountRead` so the UI sees a single per-account
    payload. ``connected`` is purely file-presence based — cookies may
    have expired, but the meet-worker is the authoritative source for
    that determination at join time.
    """

    connected: bool
    saved_at: datetime | None = None
    size_bytes: int | None = None


class AccountRead(BaseModel):
    """Public view of a :class:`GoogleAccount` row (no tokens exposed).

    Capabilities are exposed as two boolean-like fields:

    * ``has_calendar`` — a calendar refresh token is present.
      ``token_health`` reflects whether it decrypts.
    * ``bot_session.connected`` — a Playwright ``storage_state.json``
      exists for this account on the shared volume.

    A row can carry one or both capabilities; the UI renders it under
    the matching section(s).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    has_calendar: bool
    token_expires_at: datetime | None
    token_health: TokenHealth
    bot_session: BotSessionView
    created_at: datetime
    updated_at: datetime


def _account_read(row: GoogleAccount, crypto: CredentialCrypto) -> AccountRead:
    """Build :class:`AccountRead` and fill in capability fields."""
    has_calendar = row.refresh_token_encrypted is not None
    if not has_calendar:
        health: TokenHealth = "none"
    elif can_decrypt_refresh_token(account=row, crypto=crypto):
        health = "ok"
    else:
        health = "needs_reauth"
    session_status = bot_session_status(row.id)
    return AccountRead(
        id=row.id,
        email=row.email,
        has_calendar=has_calendar,
        token_expires_at=row.token_expires_at,
        token_health=health,
        bot_session=BotSessionView(
            connected=session_status["connected"],
            saved_at=session_status["saved_at"],
            size_bytes=session_status["size_bytes"],
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# --- In-memory state map ---------------------------------------------------


_pending_states: set[str] = set()


def _remember_state(state: str) -> None:
    _pending_states.add(state)


def _consume_state(state: str) -> bool:
    """Return ``True`` if ``state`` was pending; pop it on the way out."""
    if state in _pending_states:
        _pending_states.discard(state)
        return True
    return False


def _peek_state(state: str) -> bool:
    return state in _pending_states


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


async def _exchange_and_persist(
    *,
    code: str,
    state: str,
    session: Session,
    crypto: CredentialCrypto,
    settings: Settings,
) -> GoogleAccount:
    """Shared core of POST and GET ``/callback``.

    Validates the pending state, exchanges the code for tokens, fetches
    the userinfo, and upserts the row. Raises ``HTTPException`` directly
    so both wrappers map errors to HTTP responses identically.
    """
    client_id, client_secret = _require_oauth_config(settings)
    if not _consume_state(state):
        raise HTTPException(status_code=400, detail="unknown or expired state")

    try:
        tokens = await exchange_code_for_tokens(
            code=code,
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
    return upsert_account_from_tokens(
        session=session,
        crypto=crypto,
        email=userinfo.email,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_at=tokens.expires_at,
    )


def _meeting_config_count(session: Session, account_id: int) -> int:
    """Count meeting configs that name this account as the bot identity."""
    stmt = (
        select(func.count())
        .select_from(MeetingConfig)
        .where(MeetingConfig.identity_account_id == account_id)
    )
    return int(session.scalar(stmt) or 0)


def _get_account_or_404(session: Session, account_id: int) -> GoogleAccount:
    row = session.get(GoogleAccount, account_id)
    if row is None:
        raise HTTPException(status_code=404, detail="account not found")
    return row


# --- Endpoints -------------------------------------------------------------


SessionDep = Annotated[Session, Depends(get_session)]
CryptoDep = Annotated[CredentialCrypto, Depends(get_crypto)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.post("/start", response_model=StartResponse)
def start(payload: StartRequest, settings: SettingsDep) -> StartResponse:
    """Return the URL the user should open to grant consent."""
    client_id, _ = _require_oauth_config(settings)
    state = payload.state or secrets.token_urlsafe(32)
    _remember_state(state)
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
    row = await _exchange_and_persist(
        code=payload.code,
        state=payload.state,
        session=session,
        crypto=crypto,
        settings=settings,
    )
    return _account_read(row, crypto)


@router.get("/callback", response_class=HTMLResponse, include_in_schema=False)
async def callback_redirect(
    session: SessionDep,
    crypto: CryptoDep,
    settings: SettingsDep,
    code: str = Query(min_length=1, max_length=2048),
    state: str = Query(min_length=1, max_length=128),
) -> HTMLResponse:
    """Browser-facing variant of the callback.

    Google's OAuth flow can redirect the user's browser here directly.
    The response is a tiny HTML page that posts ``johnny:oauth`` back to
    the opener so the Settings tab can refresh, then asks the user to
    close the popup. On error, the page renders the detail inline so the
    user is not left staring at a blank screen.
    """
    try:
        row = await _exchange_and_persist(
            code=code,
            state=state,
            session=session,
            crypto=crypto,
            settings=settings,
        )
    except HTTPException as exc:
        return HTMLResponse(
            _render_callback_page(error=str(exc.detail)),
            status_code=exc.status_code,
        )
    return HTMLResponse(_render_callback_page(email=row.email))


def _render_callback_page(*, email: str | None = None, error: str | None = None) -> str:
    """Render the tiny HTML page for the GET callback.

    Posts ``johnny:oauth`` to ``window.opener`` if any so the original
    Settings tab can refresh its account list. Falls back to plain text
    if the user opened the URL directly (no opener).
    """
    if error is not None:
        body = (
            f"<h1>Authentication failed</h1>"
            f"<p>Google said: <code>{html.escape(error)}</code></p>"
            f"<p>Close this window and try again.</p>"
        )
        payload = f'{{"type":"johnny:oauth","ok":false,"error":{__js_string(error)}}}'
    else:
        safe_email = html.escape(email or "")
        body = (
            f"<h1>Connected</h1>"
            f"<p>Signed in as <strong>{safe_email}</strong>.</p>"
            f"<p>You can close this window.</p>"
        )
        payload = f'{{"type":"johnny:oauth","ok":true,"email":{__js_string(email or "")}}}'
    script = (
        "<script>"
        "try {"
        f"  if (window.opener) {{ window.opener.postMessage({payload}, '*'); }}"
        "} catch (e) {}"
        "</script>"
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Johnny — Google sign-in</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:32rem;"
        "margin:4rem auto;padding:0 1rem;color:#111827}"
        "h1{font-size:1.5rem;margin-bottom:1rem}"
        "code{background:#f3f4f6;padding:0.1rem 0.3rem;border-radius:4px}"
        "</style></head><body>"
        f"{body}{script}"
        "</body></html>"
    )


def __js_string(value: str) -> str:
    """Encode a Python string as a JSON-safe JS string literal."""
    import json as _json

    return _json.dumps(value)


@router.get("/accounts", response_model=list[AccountRead])
def list_accounts(session: SessionDep, crypto: CryptoDep) -> list[AccountRead]:
    """List every connected Google identity.

    Ordered by id for stability; the UI is responsible for grouping by
    capability (Calendars vs Meeting bots).
    """
    rows = session.scalars(
        select(GoogleAccount).order_by(GoogleAccount.id.asc())
    ).all()
    return [_account_read(row, crypto) for row in rows]


@router.get("/accounts/{account_id}", response_model=AccountRead)
def get_account(
    account_id: int, session: SessionDep, crypto: CryptoDep
) -> AccountRead:
    """Fetch one account by id."""
    row = _get_account_or_404(session, account_id)
    return _account_read(row, crypto)


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_account(
    account_id: int,
    session: SessionDep,
    crypto: CryptoDep,
    force: Annotated[
        bool,
        Query(description="cascade-delete referencing meeting configs"),
    ] = False,
) -> None:
    """Revoke the calendar refresh token (if any) and delete the local row.

    ``meeting_configs.identity_account_id`` is RESTRICT, so an account
    that any meeting config names as its bot identity cannot be deleted
    without first detaching those rows. Without ``force=true``, returns
    HTTP 409 with ``meeting_config_count`` in the detail so the UI can
    warn the user.

    The Google-side revocation is best-effort: a 4xx/5xx from Google
    still proceeds to delete the local row (logged in
    :func:`revoke_account`). A bot-only row (no refresh token) skips
    the Google round-trip entirely.

    Also clears the bot ``storage_state.json`` if one is on disk so the
    next sign-in for the same email starts clean.
    """
    row = _get_account_or_404(session, account_id)
    referencing = _meeting_config_count(session, account_id)
    if referencing > 0 and not force:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"account is the bot identity for {referencing} meeting config(s); "
                    "pass force=true to cascade-delete"
                ),
                "meeting_config_count": referencing,
            },
        )
    if referencing > 0:
        session.execute(
            delete(MeetingConfig).where(MeetingConfig.identity_account_id == account_id)
        )
    delete_bot_session(account_id)
    try:
        await revoke_account(session=session, account=row, crypto=crypto)
    except GoogleApiClientError as exc:
        # Decrypt failure means the row is unusable anyway — keep
        # deleting but log so the operator notices.
        logger.warning("revoke_account failed for id=%s: %s", account_id, exc)
        session.delete(row)


@router.delete(
    "/accounts/{account_id}/bot-session",
    response_model=AccountRead,
)
def disconnect_bot_session(
    account_id: int, session: SessionDep, crypto: CryptoDep
) -> AccountRead:
    """Remove the saved Playwright storage_state for this account.

    Drops just the bot capability; the row stays if it still carries a
    calendar refresh token. Use ``DELETE /accounts/{id}`` to remove the
    whole identity. A no-op (no storage_state on disk) is not an error.
    """
    row = _get_account_or_404(session, account_id)
    delete_bot_session(account_id)
    return _account_read(row, crypto)


__all__ = [
    "AccountRead",
    "BotSessionView",
    "CallbackRequest",
    "StartRequest",
    "StartResponse",
    "TokenHealth",
    "_account_read",
    "_consume_state",
    "_exchange_and_persist",
    "_peek_state",
    "_pending_states",
    "_remember_state",
    "router",
]
