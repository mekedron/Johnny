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

import asyncio
import html
import logging
import secrets
import time
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.deps import get_crypto, get_session
from app.config import Settings, get_settings
from app.db.models import GoogleAccount, MeetingConfig
from app.security.crypto import CredentialCrypto
from app.services.bot_auth_seed import (
    MAX_STORAGE_STATE_BYTES,
    BotSessionError,
    bot_session_path,
    bot_session_status,
    delete_bot_session,
    save_bot_session,
    validate_storage_state,
)
from app.services.bot_session_probe import (
    BotSessionProbeUnavailableError,
    probe_bot_session,
)
from app.services.google_client import (
    GoogleApiClient,
    GoogleApiClientError,
    TokenUndecryptableError,
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


class CapabilityCheck(BaseModel):
    """Result of an explicit, live check against one capability.

    Surfaced by ``POST /accounts/{id}/verify`` so the UI can offer a
    real "Check connection" button distinct from the cheap ciphertext-
    decryption health hint in :class:`AccountRead`.
    """

    ok: bool
    message: str
    latency_ms: int | None = None
    detail: dict[str, object] | None = None


class VerifyResponse(BaseModel):
    """Combined verify result; per-capability fields are ``None`` when
    the row doesn't carry that capability."""

    checked_at: datetime
    calendar: CapabilityCheck | None = None
    bot_session: CapabilityCheck | None = None


async def _verify_calendar(
    *,
    session: Session,
    account: GoogleAccount,
    crypto: CredentialCrypto,
    settings: Settings,
) -> CapabilityCheck:
    """Round-trip to Google's userinfo endpoint with a fresh access token.

    Forces a refresh via the wrapper if the cached token is stale, then
    calls ``/oauth2/v2/userinfo`` and reports the outcome with latency.
    Catches token-decrypt errors and Google-side failures separately
    so the UI can offer a helpful message.
    """
    start = time.monotonic()
    try:
        async with GoogleApiClient(
            session=session, account=account, crypto=crypto, settings=settings
        ) as client:
            response = await client.request(
                "GET", "https://www.googleapis.com/oauth2/v2/userinfo"
            )
    except TokenUndecryptableError as exc:
        latency = int((time.monotonic() - start) * 1000)
        return CapabilityCheck(
            ok=False,
            latency_ms=latency,
            message="Stored token cannot be decrypted — reconnect to refresh it.",
            detail={"error_type": type(exc).__name__},
        )
    except GoogleApiClientError as exc:
        latency = int((time.monotonic() - start) * 1000)
        return CapabilityCheck(
            ok=False,
            latency_ms=latency,
            message=f"Google refresh failed: {exc}",
            detail={"error_type": type(exc).__name__},
        )
    except Exception as exc:  # noqa: BLE001 — surface as a failure, not a 500
        latency = int((time.monotonic() - start) * 1000)
        return CapabilityCheck(
            ok=False,
            latency_ms=latency,
            message=f"Verification call failed: {exc}",
            detail={"error_type": type(exc).__name__},
        )

    latency = int((time.monotonic() - start) * 1000)
    if response.status_code == 200:
        try:
            data = response.json()
            email = str(data.get("email") or account.email)
        except Exception:  # noqa: BLE001 — parse failure is non-fatal
            email = account.email
        return CapabilityCheck(
            ok=True,
            latency_ms=latency,
            message=f"Authenticated to Google as {email}.",
            detail={"email": email},
        )
    return CapabilityCheck(
        ok=False,
        latency_ms=latency,
        message=f"Google returned HTTP {response.status_code} from /userinfo.",
        detail={"status_code": response.status_code},
    )


def _cookie_expiry_summary(
    cookies: list[object],
) -> tuple[str | None, float | None, bool]:
    """Summarise persistent-cookie expiry.

    Returns ``(soonest_iso, days_until_expiry, expired)``. Session
    cookies (``expires <= 0``) are ignored — Google's session cookies
    carry no expiry, so counting them as "expired" would mislead. With
    no persistent cookies at all, returns ``(None, None, False)``.
    """
    persistent: list[float] = []
    for cookie in cookies:
        expires = cookie.get("expires") if isinstance(cookie, dict) else None
        if isinstance(expires, int | float) and expires > 0:
            persistent.append(float(expires))
    if not persistent:
        return None, None, False
    soonest = min(persistent)
    soonest_iso = datetime.fromtimestamp(soonest, tz=UTC).isoformat()
    now_ts = time.time()
    if soonest <= now_ts:
        return soonest_iso, None, True
    days_left = round((soonest - now_ts) / 86_400, 1)
    return soonest_iso, days_left, False


async def _verify_bot_session(account: GoogleAccount) -> CapabilityCheck:
    """Confirm the bot's storage_state is a live, signed-in Google session.

    Fast-fails on the cheap, deterministic problems first (no file, bad
    JSON, bad shape, all persistent cookies already expired) so the common
    failures don't pay the probe cost. Otherwise it loads the cookies into
    a real headless Chromium — the SAME mechanism the meet-worker uses at
    join time (:func:`app.services.bot_session_probe.probe_bot_session`) —
    and reports whether Google still recognises the session, and as whom.

    A hand-crafted / stale / revoked storage_state therefore reports
    ``ok=False`` (Google bounces it to the sign-in page); a live session
    reports ``ok=True`` with the signed-in email; cookies for a different
    Google account report ``ok=False`` with both emails. The soonest
    cookie-expiry is preserved in ``detail`` either way.
    """
    account_id = account.id
    path = bot_session_path(account_id)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return CapabilityCheck(
            ok=False,
            message="No storage_state.json on disk for this account.",
        )
    except OSError as exc:
        return CapabilityCheck(
            ok=False,
            message=f"Could not read storage_state.json: {exc}",
        )

    try:
        data = validate_storage_state(raw)
    except BotSessionError as exc:
        return CapabilityCheck(
            ok=False,
            message=f"storage_state.json is invalid: {exc}",
        )

    cookies = data.get("cookies", []) or []
    soonest_iso, days_left, expired = _cookie_expiry_summary(cookies)
    expiry_detail: dict[str, object] = {
        "cookie_count": len(cookies),
        "soonest_expiry": soonest_iso,
    }
    if days_left is not None:
        expiry_detail["days_until_expiry"] = days_left

    # Fast-fail: persistent cookies already expired — skip the probe cost.
    if expired:
        return CapabilityCheck(
            ok=False,
            message=(
                f"{len(cookies)} cookies present, but the soonest expiry "
                f"({soonest_iso}) is already in the past — re-sign-in required."
            ),
            detail={**expiry_detail, "expired": True},
        )

    # Live round-trip: load the cookies into Chromium and ask Google. The
    # probe is blocking (Docker SDK), so run it off the event loop.
    start = time.monotonic()
    try:
        result = await asyncio.to_thread(probe_bot_session, account_id)
    except BotSessionProbeUnavailableError as exc:
        latency = int((time.monotonic() - start) * 1000)
        return CapabilityCheck(
            ok=False,
            latency_ms=latency,
            message=(
                f"Cookies are present, but the live Google check could not run: {exc}"
            ),
            detail={**expiry_detail, "probe_error": str(exc)},
        )
    latency = int((time.monotonic() - start) * 1000)

    if not result.signed_in:
        if result.error:
            message = (
                f"Cookies are present, but the live Google check failed: {result.error}"
            )
        else:
            message = (
                "Cookies are present, but Google returned the sign-in page — "
                "the session is no longer valid. Re-sign-in this bot."
            )
        return CapabilityCheck(
            ok=False,
            latency_ms=latency,
            message=message,
            detail={
                **expiry_detail,
                "signed_in": False,
                "final_url": result.final_url,
                "probe_error": result.error,
            },
        )

    scraped = (result.email or "").strip()
    expected = (account.email or "").strip()
    is_placeholder = expected.startswith("unknown-") and expected.endswith(
        "@johnny.local"
    )
    if (
        scraped
        and expected
        and not is_placeholder
        and scraped.casefold() != expected.casefold()
    ):
        return CapabilityCheck(
            ok=False,
            latency_ms=latency,
            message=(
                f"Signed in to Google as {scraped}, which does NOT match this "
                f"account's expected email {expected}."
            ),
            detail={
                **expiry_detail,
                "signed_in": True,
                "signed_in_email": scraped,
                "expected_email": expected,
            },
        )

    if scraped:
        message = f"Signed in to Google as {scraped}."
    else:
        message = (
            "Signed in to Google (the session is live; could not read the "
            "account email to confirm identity)."
        )
    return CapabilityCheck(
        ok=True,
        latency_ms=latency,
        message=message,
        detail={
            **expiry_detail,
            "signed_in": True,
            "signed_in_email": scraped or None,
        },
    )


@router.post(
    "/accounts/{account_id}/verify",
    response_model=VerifyResponse,
)
async def verify_account(
    account_id: int,
    session: SessionDep,
    crypto: CryptoDep,
    settings: SettingsDep,
) -> VerifyResponse:
    """Run live checks against whichever capabilities the row carries.

    Calendar capability → real HTTP round-trip to Google /userinfo,
    forcing a refresh if needed. Bot capability → load the stored
    cookies into a real headless Chromium (the meet-worker's mechanism)
    and confirm Google still recognises the session, and as whom. Either
    field is ``None`` in the response if the row doesn't carry that
    capability.
    """
    row = _get_account_or_404(session, account_id)
    calendar_check: CapabilityCheck | None = None
    if row.refresh_token_encrypted is not None:
        calendar_check = await _verify_calendar(
            session=session, account=row, crypto=crypto, settings=settings
        )
    bot_check: CapabilityCheck | None = None
    if bot_session_status(account_id)["connected"]:
        bot_check = await _verify_bot_session(row)
    return VerifyResponse(
        checked_at=datetime.now(UTC),
        calendar=calendar_check,
        bot_session=bot_check,
    )


@router.put(
    "/accounts/{account_id}/bot-session",
    response_model=AccountRead,
)
async def upload_bot_session(
    account_id: int,
    request: Request,
    session: SessionDep,
    crypto: CryptoDep,
) -> AccountRead:
    """Persist an uploaded ``storage_state.json`` against an existing row.

    First-class alternative to the noVNC sign-in flow (Johnny-ckz.23):
    the user runs ``python -m johnny.tools.seed_auth_state`` on the host
    to generate the file, then uploads it here. Both paths land in the
    SAME on-disk location, so the meet-worker and playground are
    indistinguishable from there on.

    Takes the raw JSON body so the frontend can post the file's bytes
    without a multipart dep. Validation goes through the shared
    :func:`~app.services.bot_auth_seed.validate_storage_state` helper,
    so corrupt / empty / oversized files surface as 400 instead of
    silently writing a broken session.
    """
    row = _get_account_or_404(session, account_id)
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
        save_bot_session(account_id, raw)
    except BotSessionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"failed to write storage_state to volume: {exc}",
        ) from exc
    return _account_read(row, crypto)


@router.post(
    "/accounts/bot/upload",
    response_model=AccountRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_bot_session_new(
    request: Request,
    session: SessionDep,
    crypto: CryptoDep,
    email: Annotated[
        str,
        Query(
            min_length=3,
            max_length=320,
            description=(
                "Email the storage_state was signed in as. Match-or-create: "
                "an existing row with this email gains the bot capability; "
                "otherwise a new bot-only row is created."
            ),
        ),
    ],
) -> AccountRead:
    """Create-or-attach a bot identity from an uploaded storage_state.

    Twin to ``PUT /accounts/{id}/bot-session`` for the "Add another
    meeting bot" → "Upload" flow. The picker UI doesn't know the row
    id yet — it asks the user for the bot's email + file, then this
    endpoint matches an existing row by email (so a calendar-only row
    gains the bot capability without forking the identity) or creates
    a new bot-only row.

    The chosen / created row is returned so the picker can persist the
    user's choice keyed by account id (last-method-per-account memory).
    """
    normalized_email = email.strip().lower()
    if not normalized_email or "@" not in normalized_email:
        raise HTTPException(status_code=400, detail="email is malformed")

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
        validate_storage_state(raw)
    except BotSessionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    existing = session.scalar(
        select(GoogleAccount).where(GoogleAccount.email == normalized_email)
    )
    if existing is None:
        row = GoogleAccount(
            email=normalized_email,
            refresh_token_encrypted=None,
            access_token_encrypted=None,
            token_expires_at=None,
        )
        session.add(row)
        session.flush()
    else:
        row = existing

    try:
        save_bot_session(row.id, raw)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"failed to write storage_state to volume: {exc}",
        ) from exc

    return _account_read(row, crypto)


__all__ = [
    "AccountRead",
    "BotSessionView",
    "CallbackRequest",
    "CapabilityCheck",
    "StartRequest",
    "StartResponse",
    "TokenHealth",
    "VerifyResponse",
    "_account_read",
    "_consume_state",
    "_exchange_and_persist",
    "_peek_state",
    "_pending_states",
    "_remember_state",
    "router",
]
