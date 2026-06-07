"""Bot sign-in HTTP + WebSocket router (Johnny-105).

Browser-facing surface for the noVNC-based bot sign-in flow. Four
endpoints:

* ``POST   /auth/google/accounts/bot/signin/start`` — spawn a
  ``johnny-bot-signin`` container and return the connection
  coordinates (proxy WS path, short-lived bearer token, expiry).
* ``GET    /auth/google/accounts/bot/signin/{id}/status`` — polled
  state. Transitions ``pending → signed_in / failed / cancelled /
  expired``; once ``signed_in`` carries the finalized account row.
* ``POST   /auth/google/accounts/bot/signin/{id}/cancel`` — kill the
  container and mark the session ``cancelled``.
* ``WS     /auth/google/accounts/bot/signin/{id}/proxy?token=X`` —
  HMAC-verified WS-to-WS bridge between the user's browser and the
  container's websockify on port 6080 (which itself is a WS-to-TCP
  bridge into x11vnc on 5900).

The companion services that own state + container lifecycle live in
``app.services.bot_signin`` and ``app.services.bot_signin_launcher``.

The ``PATCH``/``rename`` shim
----------------------------

When the supervisor can't scrape an email (the user signed in to a
domain the scrape selector doesn't recognise) the new row is created
with a placeholder address ``unknown-<short>@johnny.local``. The user
renames it inline from the settings page via
``POST /auth/google/accounts/{id}/rename`` (defined here, not in the
sibling auth router, to keep the noVNC surface self-contained).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketState

from app.api.auth import AccountRead, _account_read
from app.api.deps import get_crypto, get_session
from app.config import Settings, get_settings
from app.db.models import GoogleAccount
from app.security.crypto import CredentialCrypto
from app.services.bot_signin import (
    DEFAULT_TTL_SECONDS,
    BotSigninSession,
    cleanup_pending,
    finalize_storage_state,
    load_session,
    mint_proxy_token,
    new_signin_id,
    placeholder_email,
    read_marker,
    save_session,
    verify_proxy_token,
)
from app.services.bot_signin_launcher import (
    BotSigninLauncher,
    BotSigninLauncherError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/google/accounts", tags=["auth"])
ws_router = APIRouter(prefix="/auth/google/accounts", tags=["auth"])


# --- Schemas ---------------------------------------------------------------


class SigninStartRequest(BaseModel):
    """Optional knobs for a new bot sign-in session.

    ``account_id`` pre-binds the resulting storage_state to an existing
    row (used by the settings page when the user clicks "Replace
    session" or "Attach to <existing email>"). Omitting it tells the
    backend to decide at completion time: match the scraped email to
    an existing row, or create a new one if no match.

    ``email_hint`` is just the value Google's AccountChooser pre-types
    in the visible window — does NOT bind the resulting row. The
    supervisor's email scrape is authoritative.
    """

    account_id: int | None = Field(default=None, ge=1)
    email_hint: str | None = Field(default=None, max_length=320)


class SigninStartResponse(BaseModel):
    signin_session_id: str
    proxy_ws_path: str
    token: str
    expires_at: datetime
    container_name: str


class SigninStatusResponse(BaseModel):
    signin_session_id: str
    status: str
    expires_at: datetime
    account: AccountRead | None = None
    error: str | None = None


class AccountRenameRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


# --- Launcher singleton ----------------------------------------------------


_launcher: BotSigninLauncher | None = None


def get_launcher() -> BotSigninLauncher:
    """Lazily build the docker-backed launcher.

    Test harnesses replace this with :func:`set_launcher` to inject a
    fake; production startup does NOT eagerly create the launcher —
    only the first sign-in request pays the docker connection cost.
    """
    global _launcher
    if _launcher is None:
        _launcher = BotSigninLauncher()
    return _launcher


def set_launcher(launcher: BotSigninLauncher | None) -> None:
    """Override the global launcher (tests + the no-docker dev mode)."""
    global _launcher
    _launcher = launcher


# --- Helpers ---------------------------------------------------------------


SessionDep = Annotated[Session, Depends(get_session)]
CryptoDep = Annotated[CredentialCrypto, Depends(get_crypto)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _get_account_or_404(session: Session, account_id: int) -> GoogleAccount:
    row = session.get(GoogleAccount, account_id)
    if row is None:
        raise HTTPException(status_code=404, detail="account not found")
    return row


def _resolve_account_for_finalize(
    *,
    session: Session,
    crypto: CredentialCrypto,
    signin: BotSigninSession,
    scraped_email: str | None,
) -> GoogleAccount:
    """Pick the GoogleAccount row to attach the storage_state to.

    Priority:

    1. ``signin.account_id`` was set at start time → use it.
    2. Scraped email matches an existing row → use it.
    3. Create a new bot-only row using the scraped email, or a
       placeholder if the scrape failed entirely.
    """
    if signin.account_id is not None:
        row = session.get(GoogleAccount, signin.account_id)
        if row is None:
            # The pre-bound account row was deleted while the sign-in
            # was in flight. Fall back to the email-match / create
            # path so the user's effort isn't lost.
            logger.warning(
                "bot-signin %s: pre-bound account_id=%s vanished — "
                "falling back to email match",
                signin.id,
                signin.account_id,
            )
        else:
            return row

    if scraped_email:
        existing = session.scalar(
            select(GoogleAccount).where(
                GoogleAccount.email == scraped_email.lower()
            )
        )
        if existing is not None:
            return existing
        new_row = GoogleAccount(
            email=scraped_email.lower(),
            refresh_token_encrypted=None,
            access_token_encrypted=None,
            token_expires_at=None,
        )
        session.add(new_row)
        session.flush()
        return new_row

    placeholder = placeholder_email(signin.id)
    new_row = GoogleAccount(
        email=placeholder,
        refresh_token_encrypted=None,
        access_token_encrypted=None,
        token_expires_at=None,
    )
    session.add(new_row)
    session.flush()
    return new_row


def _maybe_finalize_signin(
    *,
    session: Session,
    crypto: CredentialCrypto,
    signin: BotSigninSession,
    launcher: BotSigninLauncher,
) -> BotSigninSession:
    """Apply the supervisor's marker if it's written, otherwise no-op.

    Idempotent: once ``status`` is terminal we just return the session
    as-is so repeated /status polls stay cheap.
    """
    if signin.status in {"signed_in", "failed", "cancelled", "expired"}:
        return signin

    now = datetime.now(UTC)
    if now > signin.expires_at:
        try:
            launcher.stop(signin_id=signin.id)
        except BotSigninLauncherError as exc:
            logger.warning(
                "bot-signin %s: stop after expiry failed: %s", signin.id, exc
            )
        signin.status = "expired"
        signin.error = "session expired before sign-in completed"
        save_session(signin)
        cleanup_pending(signin.id)
        return signin

    marker = read_marker(signin.id)
    if marker is None:
        # Supervisor still running (or about to exit before writing the
        # marker). Leave the session pending — the next poll will catch
        # the marker once it lands.
        return signin

    if not marker.get("ok"):
        signin.status = "failed"
        signin.error = str(marker.get("error") or "sign-in failed")
        save_session(signin)
        try:
            launcher.stop(signin_id=signin.id)
        except BotSigninLauncherError as exc:
            logger.warning(
                "bot-signin %s: stop after failure failed: %s", signin.id, exc
            )
        cleanup_pending(signin.id)
        return signin

    scraped_email_raw = marker.get("email")
    scraped_email = (
        str(scraped_email_raw).strip() if scraped_email_raw else None
    ) or None
    account = _resolve_account_for_finalize(
        session=session,
        crypto=crypto,
        signin=signin,
        scraped_email=scraped_email,
    )
    moved = finalize_storage_state(signin.id, account.id)
    if not moved:
        signin.status = "failed"
        signin.error = "storage_state.json missing from supervisor handoff"
        save_session(signin)
        try:
            launcher.stop(signin_id=signin.id)
        except BotSigninLauncherError as exc:
            logger.warning(
                "bot-signin %s: stop after missing storage_state failed: %s",
                signin.id,
                exc,
            )
        cleanup_pending(signin.id)
        return signin

    signin.status = "signed_in"
    signin.finalized_account_id = account.id
    signin.finalized_email = account.email
    save_session(signin)
    try:
        launcher.stop(signin_id=signin.id)
    except BotSigninLauncherError as exc:
        logger.warning(
            "bot-signin %s: stop after signed_in failed: %s", signin.id, exc
        )
    cleanup_pending(signin.id)
    return signin


def _build_status_response(
    *,
    signin: BotSigninSession,
    session: Session,
    crypto: CredentialCrypto,
) -> SigninStatusResponse:
    account: AccountRead | None = None
    if signin.finalized_account_id is not None:
        row = session.get(GoogleAccount, signin.finalized_account_id)
        if row is not None:
            account = _account_read(row, crypto)
    return SigninStatusResponse(
        signin_session_id=signin.id,
        status=signin.status,
        expires_at=signin.expires_at,
        account=account,
        error=signin.error,
    )


# --- HTTP endpoints --------------------------------------------------------


@router.post(
    "/bot/signin/start",
    response_model=SigninStartResponse,
    status_code=status.HTTP_201_CREATED,
)
def start_bot_signin(
    payload: SigninStartRequest,
    session: SessionDep,
    settings: SettingsDep,
) -> SigninStartResponse:
    """Spawn a new bot-signin container and return its connection coords.

    If ``account_id`` is given the row is verified to exist (404
    otherwise) so a stale UI doesn't ship the user into a sign-in that
    will fail to finalize. Failures to start the container surface as
    503 — Docker daemon problems are operational, not 4xx user errors.
    """
    if payload.account_id is not None:
        _get_account_or_404(session, payload.account_id)

    signin_id = new_signin_id()
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=DEFAULT_TTL_SECONDS)

    launcher = get_launcher()
    try:
        container_name = launcher.start(
            signin_id=signin_id,
            email_hint=payload.email_hint,
            timeout_seconds=DEFAULT_TTL_SECONDS,
        )
    except BotSigninLauncherError as exc:
        logger.exception("bot-signin %s: launcher.start failed", signin_id)
        raise HTTPException(
            status_code=503,
            detail=(
                "could not spawn sign-in container — "
                f"is the bot-signin image built? ({exc})"
            ),
        ) from exc

    signin = BotSigninSession(
        id=signin_id,
        container_name=container_name,
        status="pending",
        created_at=now,
        expires_at=expires_at,
        account_id=payload.account_id,
        email_hint=payload.email_hint,
    )
    save_session(signin)
    token = mint_proxy_token(signin_id)
    return SigninStartResponse(
        signin_session_id=signin_id,
        proxy_ws_path=f"/auth/google/accounts/bot/signin/{signin_id}/proxy",
        token=token,
        expires_at=expires_at,
        container_name=container_name,
    )


@router.get(
    "/bot/signin/{signin_id}/status",
    response_model=SigninStatusResponse,
)
def get_bot_signin_status(
    signin_id: str,
    session: SessionDep,
    crypto: CryptoDep,
) -> SigninStatusResponse:
    """Polled state. Drives the noVNC modal's state machine in the UI."""
    signin = load_session(signin_id)
    if signin is None:
        raise HTTPException(
            status_code=404, detail="sign-in session not found or expired"
        )
    launcher = get_launcher()
    signin = _maybe_finalize_signin(
        session=session, crypto=crypto, signin=signin, launcher=launcher
    )
    return _build_status_response(
        signin=signin, session=session, crypto=crypto
    )


@router.post(
    "/bot/signin/{signin_id}/cancel",
    response_model=SigninStatusResponse,
)
def cancel_bot_signin(
    signin_id: str,
    session: SessionDep,
    crypto: CryptoDep,
) -> SigninStatusResponse:
    """User-initiated cancellation. Idempotent."""
    signin = load_session(signin_id)
    if signin is None:
        raise HTTPException(
            status_code=404, detail="sign-in session not found or expired"
        )
    launcher = get_launcher()
    if signin.status == "pending":
        try:
            launcher.stop(signin_id=signin.id)
        except BotSigninLauncherError as exc:
            logger.warning(
                "bot-signin %s: stop on cancel failed: %s", signin.id, exc
            )
        signin.status = "cancelled"
        save_session(signin)
        cleanup_pending(signin.id)
    return _build_status_response(
        signin=signin, session=session, crypto=crypto
    )


@router.post(
    "/{account_id}/rename",
    response_model=AccountRead,
)
def rename_account(
    account_id: int,
    payload: AccountRenameRequest,
    session: SessionDep,
    crypto: CryptoDep,
) -> AccountRead:
    """Rename a Google account row by email.

    The noVNC sign-in flow may finish with a placeholder address when
    the email scrape fails (see
    :func:`app.services.bot_signin.placeholder_email`). The UI offers
    an inline rename so the user can replace the placeholder with the
    real address.

    Rejected if the new email already exists on another row (the
    sign-in path should have collapsed onto that row instead — refuse
    rather than silently dropping the duplicate).
    """
    row = _get_account_or_404(session, account_id)
    new_email = payload.email.strip().lower()
    if not new_email or "@" not in new_email:
        raise HTTPException(status_code=400, detail="email is malformed")
    if new_email == row.email:
        return _account_read(row, crypto)
    collision = session.scalar(
        select(GoogleAccount).where(GoogleAccount.email == new_email)
    )
    if collision is not None and collision.id != row.id:
        raise HTTPException(
            status_code=409,
            detail=(
                f"another account row already uses {new_email!r} "
                "(id={})".format(collision.id)
            ),
        )
    row.email = new_email
    session.flush()
    return _account_read(row, crypto)


# --- WebSocket proxy -------------------------------------------------------


@ws_router.websocket("/bot/signin/{signin_id}/proxy")
async def bot_signin_proxy(
    websocket: WebSocket,
    signin_id: str,
    token: str = Query(default=""),
) -> None:
    """WS-to-WS bridge between the browser and the container's websockify.

    The token gating happens BEFORE ``accept()`` so an invalid token
    closes with 1008 rather than briefly accepting the upgrade. The
    Redis lookup runs after the token check so a leaked token for an
    expired session can't be replayed to discover that fact.
    """
    if not verify_proxy_token(signin_id, token):
        await websocket.close(code=1008)
        return
    signin = load_session(signin_id)
    if signin is None or signin.status != "pending":
        await websocket.close(code=1008)
        return

    target_url = f"ws://{signin.container_name}:6080/"
    try:
        # Imported lazily so the module loads cleanly in environments
        # without the ``websockets`` package (the meet-worker image
        # doesn't carry it; the api/worker images do).
        import websockets  # type: ignore[import-untyped]
    except ImportError:
        logger.exception(
            "bot-signin %s: websockets library missing — cannot proxy",
            signin_id,
        )
        await websocket.close(code=1011)
        return

    await websocket.accept(subprotocol="binary")

    # Brief retry loop: websockify inside the container takes a moment
    # to bind after the entrypoint script kicks it off, so the first
    # connect attempt from the API often races and gets ECONNREFUSED.
    # Five attempts at 0.5 s covers the typical startup window without
    # making a real outage feel slow.
    upstream = None
    last_exc: Exception | None = None
    for _attempt in range(5):
        try:
            upstream = await websockets.connect(
                target_url, subprotocols=["binary"]
            )
            break
        except (OSError, ConnectionRefusedError) as exc:
            last_exc = exc
            await asyncio.sleep(0.5)
        except Exception as exc:  # noqa: BLE001 — any other connect failure aborts
            last_exc = exc
            break
    if upstream is None:
        logger.warning(
            "bot-signin %s: upstream connect failed after retries: %s",
            signin_id,
            last_exc,
        )
        if websocket.client_state is WebSocketState.CONNECTED:
            await websocket.close(code=1011)
        return
    try:
        await _pump(websocket, upstream)
    except Exception as exc:  # noqa: BLE001 — pump failures already logged downstream
        logger.warning(
            "bot-signin %s: pump terminated unexpectedly: %s",
            signin_id,
            exc,
        )
    finally:
        try:
            await upstream.close()
        except Exception:  # noqa: BLE001 — close is best-effort
            pass
        if websocket.client_state is WebSocketState.CONNECTED:
            await websocket.close(code=1000)


async def _pump(client: WebSocket, upstream: object) -> None:
    """Bidirectional binary forwarder between client and upstream WS.

    Cancels whichever direction is still running once either side
    closes so the coroutine returns promptly instead of leaking a
    background task that lives until the next inbound frame.
    """

    async def client_to_upstream() -> None:
        try:
            while True:
                msg = await client.receive()
                kind = msg.get("type")
                if kind == "websocket.disconnect":
                    return
                if "bytes" in msg and msg["bytes"] is not None:
                    await upstream.send(msg["bytes"])  # type: ignore[attr-defined]
                elif "text" in msg and msg["text"] is not None:
                    await upstream.send(msg["text"])  # type: ignore[attr-defined]
        except WebSocketDisconnect:
            return
        except Exception:  # noqa: BLE001 — any error is "stop pumping"
            return

    async def upstream_to_client() -> None:
        try:
            async for msg in upstream:  # type: ignore[attr-defined]
                if isinstance(msg, bytes):
                    if client.client_state is not WebSocketState.CONNECTED:
                        return
                    await client.send_bytes(msg)
                else:
                    if client.client_state is not WebSocketState.CONNECTED:
                        return
                    await client.send_text(msg)
        except Exception:  # noqa: BLE001 — any error is "stop pumping"
            return

    c2u = asyncio.create_task(client_to_upstream())
    u2c = asyncio.create_task(upstream_to_client())
    try:
        done, pending = await asyncio.wait(
            {c2u, u2c}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    finally:
        try:
            await upstream.close()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — close is best-effort
            pass


__all__ = [
    "AccountRenameRequest",
    "SigninStartRequest",
    "SigninStartResponse",
    "SigninStatusResponse",
    "get_launcher",
    "router",
    "set_launcher",
    "ws_router",
]
