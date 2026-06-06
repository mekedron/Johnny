"""Google OAuth 2.0 desktop-flow endpoints (US-005, US-006).

US-005 added the initial authorize / callback round-trip; US-006 extends
this module with account-management endpoints (list, patch, disconnect)
plus a GET ``/callback`` variant that Google can redirect a browser to.

* ``POST /auth/google/start`` — build the consent URL using the configured
  ``GOOGLE_CLIENT_ID`` and return it for the client to open in a browser.
  The client picks a per-session ``state`` value (or lets the server pick
  one); the role / default-user tags chosen at start time round-trip
  through Google via the state map.

* ``POST /auth/google/callback`` — programmatic variant: exchange the
  authorization code for tokens and persist the encrypted account row.
  Used by tests and any client that proxies Google's redirect.

* ``GET /auth/google/callback`` — browser-friendly variant of the same
  flow: Google redirects the user here, the server processes the code,
  and a small HTML page tells the user to close the popup. The
  ``window.opener``-aware script posts ``johnny:oauth`` so the original
  Settings tab can refresh without polling.

* ``GET /auth/google/accounts`` — list every connected account (no token
  material is ever returned).

* ``PATCH /auth/google/accounts/{id}`` — change role or promote to the
  default user identity. Promoting one account demotes any other.

* ``DELETE /auth/google/accounts/{id}`` — revoke the refresh token at
  Google's revocation endpoint and remove the row. Returns HTTP 409 if
  any meeting configs reference the account (with ``meeting_config_count``
  in the detail); pass ``?force=true`` to cascade-delete those rows first.

Tokens are encrypted with the application-wide Fernet key before going to
the database. Decryption only happens inside the shared
:class:`~app.services.google_client.GoogleApiClient` wrapper.
"""

from __future__ import annotations

import html
import logging
import secrets
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_crypto, get_session
from app.config import Settings, get_settings
from app.db.models import AccountRole, GoogleAccount, MeetingConfig
from app.security.crypto import CredentialCrypto
from app.services.bot_auth_seed import (
    MAX_STORAGE_STATE_BYTES,
    BotSessionError,
    bot_session_status,
    delete_bot_session,
    save_bot_session,
)
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


TokenHealth = Literal["ok", "needs_reauth"]


class AccountRead(BaseModel):
    """Public view of a :class:`GoogleAccount` row (no tokens exposed).

    ``token_health`` is computed at response time by attempting a no-op
    decrypt of the stored refresh token. ``"needs_reauth"`` means the
    Fernet key has rotated (or the ciphertext is corrupt) and the user
    must re-run the OAuth flow. No Google round-trip is performed.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    role: AccountRole
    is_default_user: bool
    token_expires_at: datetime | None
    token_health: TokenHealth = "ok"
    created_at: datetime
    updated_at: datetime


def _account_read(row: GoogleAccount, crypto: CredentialCrypto) -> AccountRead:
    """Build :class:`AccountRead` and fill in ``token_health``."""
    health: TokenHealth = (
        "ok" if can_decrypt_refresh_token(account=row, crypto=crypto) else "needs_reauth"
    )
    return AccountRead(
        id=row.id,
        email=row.email,
        role=row.role,
        is_default_user=row.is_default_user,
        token_expires_at=row.token_expires_at,
        token_health=health,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class AccountUpdate(BaseModel):
    """Patch payload for promoting an account or changing its role.

    Promoting one account to ``is_default_user=True`` automatically
    demotes any other default-user row so the unique-by-intent invariant
    holds even though the database column is just a plain boolean.
    """

    role: AccountRole | None = None
    is_default_user: bool | None = None


class BotSessionStatusResponse(BaseModel):
    """Whether a bot account's Playwright ``storage_state.json`` exists.

    Returned by the bot-session GET / PUT / DELETE endpoints so the UI
    can render a single status surface (``Connected (saved …)`` vs
    ``Not connected``) regardless of the operation that produced it.

    ``connected`` is purely file-presence based — cookies may have
    expired, but the meet-worker is the authoritative source for that
    determination at join time.
    """

    connected: bool
    saved_at: datetime | None = None
    size_bytes: int | None = None
    path: str


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


async def _exchange_and_persist(
    *,
    code: str,
    state: str,
    session: Session,
    crypto: CredentialCrypto,
    settings: Settings,
) -> GoogleAccount:
    """Shared core of POST and GET ``/callback``.

    Consumes the pending state, exchanges the code for tokens, fetches
    the userinfo, and upserts the row. Raises ``HTTPException`` directly
    so both wrappers map errors to HTTP responses identically.
    """
    client_id, client_secret = _require_oauth_config(settings)
    pending = _consume_state(state)
    if pending is None:
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
        role=pending.role,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_at=tokens.expires_at,
        is_default_user=pending.is_default_user,
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


def _clear_other_default_users(session: Session, keep: GoogleAccount) -> None:
    """Ensure at most one account has ``is_default_user=True``."""
    others = session.scalars(
        select(GoogleAccount).where(
            GoogleAccount.is_default_user.is_(True),
            GoogleAccount.id != keep.id,
        )
    ).all()
    for other in others:
        other.is_default_user = False


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
    """List every connected Google account.

    Ordered by ``is_default_user`` first (so the default user account
    surfaces at the top of the Settings page), then by id for stability.
    """
    rows = session.scalars(
        select(GoogleAccount).order_by(
            GoogleAccount.is_default_user.desc(),
            GoogleAccount.id.asc(),
        )
    ).all()
    return [_account_read(row, crypto) for row in rows]


@router.get("/accounts/{account_id}", response_model=AccountRead)
def get_account(
    account_id: int, session: SessionDep, crypto: CryptoDep
) -> AccountRead:
    """Fetch one account by id."""
    row = _get_account_or_404(session, account_id)
    return _account_read(row, crypto)


@router.patch("/accounts/{account_id}", response_model=AccountRead)
def update_account(
    account_id: int,
    payload: AccountUpdate,
    session: SessionDep,
    crypto: CryptoDep,
) -> AccountRead:
    """Patch role and/or default-user flag for an account.

    Promoting an account to default user automatically demotes any other
    default-user row so at most one account is marked default at a time.
    Demoting is allowed even if it leaves zero default-user accounts; the
    UI is responsible for prompting the user to pick a replacement.
    """
    row = _get_account_or_404(session, account_id)
    if payload.role is not None:
        row.role = payload.role
    if payload.is_default_user is not None:
        row.is_default_user = payload.is_default_user
        if payload.is_default_user:
            _clear_other_default_users(session, row)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="account update failed") from exc
    session.refresh(row)
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
    """Revoke the refresh token at Google and delete the local row.

    ``meeting_configs.identity_account_id`` is RESTRICT, so an account
    that any meeting config names as its bot identity cannot be deleted
    without first detaching those rows. Without ``force=true``, returns
    HTTP 409 with ``meeting_config_count`` in the detail so the UI can
    warn the user.

    The Google-side revocation is best-effort: a 4xx/5xx from Google
    still proceeds to delete the local row (logged in
    :func:`revoke_account`) — the user wants the account gone locally
    even if Google is unreachable.
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
    try:
        await revoke_account(session=session, account=row, crypto=crypto)
    except GoogleApiClientError as exc:
        # Decrypt failure means the row is unusable anyway — keep deleting
        # but log so the operator notices.
        logger.warning("revoke_account failed for id=%s: %s", account_id, exc)
        session.delete(row)


# --- Bot-session storage_state endpoints (Johnny-4ph) ---------------------


def _require_bot_account(session: Session, account_id: int) -> GoogleAccount:
    """Return the account row or 400/404 if it can't host a bot session.

    Only accounts tagged ``role=bot`` carry a Playwright storage_state —
    user accounts authenticate via OAuth tokens, not a saved browser
    session. We surface a 400 instead of silently accepting so a wrong
    account selection is caught at upload time.
    """
    row = _get_account_or_404(session, account_id)
    if row.role is not AccountRole.BOT:
        raise HTTPException(
            status_code=400,
            detail=(
                "bot-session storage_state is only valid for accounts with "
                "role=bot; this account is tagged "
                f"role={row.role.value}"
            ),
        )
    return row


@router.get(
    "/accounts/{account_id}/bot-session",
    response_model=BotSessionStatusResponse,
)
def get_bot_session(account_id: int, session: SessionDep) -> BotSessionStatusResponse:
    """Return whether a Playwright storage_state.json is on disk for this bot.

    The status reflects file presence and mtime — it does NOT round-trip
    to Google or validate the cookies. The meet-worker is the authority
    on whether the session is actually usable; this endpoint just tells
    the UI whether the user has run the helper yet.
    """
    _require_bot_account(session, account_id)
    return BotSessionStatusResponse(**bot_session_status(account_id))


@router.put(
    "/accounts/{account_id}/bot-session",
    response_model=BotSessionStatusResponse,
)
async def upload_bot_session(
    account_id: int,
    request: Request,
    session: SessionDep,
) -> BotSessionStatusResponse:
    """Persist an uploaded ``storage_state.json`` for this bot account.

    Accepts the raw JSON body as ``application/json`` and writes it
    atomically to the shared ``google_auth_state`` volume so the
    meet-worker finds it on its next join attempt. The file format is
    Playwright's standard storage_state — same shape as the file
    :mod:`johnny.tools.seed_auth_state` produces.

    A successful round-trip means the file is on disk and well-formed;
    it does not guarantee the cookies will still be valid when the
    meet-worker actually tries to sign in. Cookies expire; this is the
    same caveat the CLI helper carries.
    """
    _require_bot_account(session, account_id)

    raw = await request.body()
    if not raw:
        raise HTTPException(
            status_code=400,
            detail="empty body — POST the storage_state.json content as the request body",
        )
    if len(raw) > MAX_STORAGE_STATE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"storage_state file is too large: {len(raw)} bytes "
                f"(limit {MAX_STORAGE_STATE_BYTES})"
            ),
        )
    try:
        result = save_bot_session(account_id, raw)
    except BotSessionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        # Disk full, permission denied, etc. — surface as 500 so the UI
        # tells the operator to check the host volume.
        raise HTTPException(
            status_code=500,
            detail=f"failed to write storage_state to volume: {exc}",
        ) from exc
    return BotSessionStatusResponse(**result)


@router.delete(
    "/accounts/{account_id}/bot-session",
    response_model=BotSessionStatusResponse,
)
def delete_bot_session_endpoint(
    account_id: int, session: SessionDep
) -> BotSessionStatusResponse:
    """Remove the saved Playwright storage_state for this bot account.

    Useful when the cookies have expired (the user signs in fresh) or
    when the user is switching the bot identity to a different email.
    Returns the post-delete status — ``connected=False`` either way.
    A no-op (file did not exist) is not an error.
    """
    _require_bot_account(session, account_id)
    delete_bot_session(account_id)
    return BotSessionStatusResponse(**bot_session_status(account_id))


__all__ = [
    "AccountRead",
    "AccountUpdate",
    "BotSessionStatusResponse",
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
