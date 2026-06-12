"""Workspace Google-account HTTP endpoints (Johnny-wks.4).

The thin HTTP shell over
:class:`app.services.workspace_accounts.WorkspaceGogAuthService`:

* ``GET    /workspaces/{id}/accounts``           — connected accounts + lock state
* ``POST   /workspaces/{id}/accounts/connect``   — start the serialized flow
* ``DELETE /workspaces/{id}/accounts/pending``   — cancel / dismiss this
  workspace's flow record
* ``DELETE /workspaces/{id}/accounts/{email}``   — disconnect an account
* ``GET    /workspaces/accounts/oauth/callback`` — where Google redirects the
  operator's browser (renders a tiny human-readable HTML page; the panel in
  the app polls the GET view for the same outcome)

No secrets cross this surface: account rows are emails + service names, the
consent ``auth_url`` carries only the public client id and PKCE challenge,
and the one-time authorization code in the callback query is relayed
straight into the workspace's sandbox for the exchange (it is never stored
or rendered).

Error mapping (service exceptions → HTTP): busy lock → 409, an
operator-fixable precondition → 422, sandbox unreachable → 503, any other
gog/flow failure → 502 with the honest command tail.
"""

from __future__ import annotations

import html
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.db.models import Workspace
from app.services.workspace_accounts import (
    CALLBACK_PATH,
    DEFAULT_SERVICES,
    AccountsView,
    GogAccount,
    GogAuthBusyError,
    GogAuthError,
    GogPreconditionError,
    GogSandboxUnreachableError,
    PendingAuth,
    WorkspaceRef,
    get_workspace_gog_auth_service,
)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

SessionDep = Annotated[Session, Depends(get_session)]


# --- Schemas ----------------------------------------------------------------


class AccountRead(BaseModel):
    email: str
    client: str
    services: list[str]


class PendingRead(BaseModel):
    workspace_id: int
    workspace_name: str
    email: str
    services: str
    status: str
    auth_url: str
    error: str
    expires_at: float


class AccountsViewRead(BaseModel):
    workspace_id: int
    workspace_name: str
    reachable: bool
    reason: str
    keyring_backend: str
    client_credentials: bool
    accounts: list[AccountRead]
    pending: PendingRead | None = None
    busy: PendingRead | None = None


class ConnectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    services: str = Field(default=DEFAULT_SERVICES, max_length=512)


# --- Helpers -----------------------------------------------------------------


def _workspace_ref_or_404(session: Session, workspace_id: int) -> WorkspaceRef:
    row = session.get(Workspace, workspace_id)
    if row is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return WorkspaceRef(
        id=row.id, name=row.name, slug=row.slug, is_default=row.is_default
    )


def _http_error(exc: GogAuthError) -> HTTPException:
    if isinstance(exc, GogAuthBusyError):
        code = status.HTTP_409_CONFLICT
    elif isinstance(exc, GogPreconditionError):
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
    elif isinstance(exc, GogSandboxUnreachableError):
        code = status.HTTP_503_SERVICE_UNAVAILABLE
    else:
        code = status.HTTP_502_BAD_GATEWAY
    return HTTPException(status_code=code, detail=str(exc))


def _pending_read(record: PendingAuth | None) -> PendingRead | None:
    if record is None:
        return None
    return PendingRead(
        workspace_id=record.workspace_id,
        workspace_name=record.workspace_name,
        email=record.email,
        services=record.services,
        status=record.status,
        auth_url=record.auth_url,
        error=record.error,
        expires_at=record.expires_at,
    )


def _account_read(account: GogAccount) -> AccountRead:
    return AccountRead(
        email=account.email, client=account.client, services=account.services
    )


def _view_read(view: AccountsView) -> AccountsViewRead:
    return AccountsViewRead(
        workspace_id=view.workspace_id,
        workspace_name=view.workspace_name,
        reachable=view.reachable,
        reason=view.reason,
        keyring_backend=view.keyring_backend,
        client_credentials=view.client_credentials,
        accounts=[_account_read(account) for account in view.accounts],
        pending=_pending_read(view.pending),
        busy=_pending_read(view.busy),
    )


def _redirect_uri(request: Request) -> str:
    """The browser-reachable callback URL, derived from how the browser
    addressed THIS request — whatever loopback origin the operator's UI uses
    to reach the api is by construction one their browser can be redirected
    back to (Google allows http only for loopback addresses; remote/LAN
    deployments need a proper hostname — documented in sandbox/README.md).

    ``localhost`` is normalized to ``127.0.0.1``: Google's grant step has
    been observed to fail with a generic "Something went wrong" on
    ``http://localhost`` redirect URIs for installed clients (live repro,
    2026-06-12) while accepting the RFC 8252 literal-loopback form — and the
    redirect target only has to reach this api, not match the browser's
    origin string.
    """
    base = str(request.base_url).rstrip("/")
    base = base.replace("://localhost:", "://127.0.0.1:", 1)
    if base.endswith("://localhost"):
        base = base[: -len("localhost")] + "127.0.0.1"
    return base + CALLBACK_PATH


# --- Endpoints -----------------------------------------------------------------


@router.get("/{workspace_id}/accounts", response_model=AccountsViewRead)
async def get_accounts(workspace_id: int, session: SessionDep) -> AccountsViewRead:
    """Connected accounts for the workspace — the GET is the refresh (a
    non-default workspace's container is lazily ensured, the capabilities-API
    convention). An unreachable sandbox reports ``reachable=false`` with the
    reason instead of failing."""
    workspace = _workspace_ref_or_404(session, workspace_id)
    view = await get_workspace_gog_auth_service().accounts_view(workspace)
    return _view_read(view)


@router.post("/{workspace_id}/accounts/connect", response_model=PendingRead)
async def start_connect(
    workspace_id: int,
    payload: ConnectIn,
    request: Request,
    session: SessionDep,
) -> PendingRead:
    """Start the serialized connect flow; returns the Google consent URL the
    UI opens in a new tab. One flow at a time across ALL workspaces (409
    names the holder)."""
    workspace = _workspace_ref_or_404(session, workspace_id)
    try:
        record = await get_workspace_gog_auth_service().start_connect(
            workspace,
            email=payload.email,
            services=payload.services,
            redirect_uri=_redirect_uri(request),
        )
    except GogAuthError as exc:
        raise _http_error(exc) from exc
    read = _pending_read(record)
    assert read is not None  # start_connect returns a record or raises
    return read


@router.delete(
    "/{workspace_id}/accounts/pending", status_code=status.HTTP_204_NO_CONTENT
)
async def cancel_pending(workspace_id: int, session: SessionDep) -> None:
    """Cancel this workspace's in-flight connect (or dismiss its outcome
    record). Idempotent; another workspace's live flow is refused (409)."""
    _workspace_ref_or_404(session, workspace_id)
    try:
        await get_workspace_gog_auth_service().cancel_pending(workspace_id)
    except GogAuthError as exc:
        raise _http_error(exc) from exc


@router.delete(
    "/{workspace_id}/accounts/{email}", status_code=status.HTTP_204_NO_CONTENT
)
async def disconnect_account(
    workspace_id: int, email: str, session: SessionDep
) -> None:
    """Remove a connected account from the workspace's keyring."""
    workspace = _workspace_ref_or_404(session, workspace_id)
    try:
        await get_workspace_gog_auth_service().disconnect(workspace, email)
    except GogAuthError as exc:
        raise _http_error(exc) from exc


_CALLBACK_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Johnny — Google account</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #0f1115; color: #e7e9ee;
         display: grid; place-items: center; min-height: 100vh; margin: 0; }}
  main {{ max-width: 26rem; padding: 2rem; text-align: center; }}
  h1 {{ font-size: 1.1rem; margin: 0 0 .75rem; }}
  p {{ margin: .4rem 0; color: #aab0bc; line-height: 1.5; }}
  .ok {{ color: #4ade80; }} .bad {{ color: #f87171; }}
</style>
</head>
<body>
<main>
<h1 class="{tone}">{title}</h1>
<p>{message}</p>
<p>You can close this tab and return to Johnny.</p>
</main>
</body>
</html>"""

_CALLBACK_TITLES = {
    "completed": ("Account connected", "ok"),
    "failed": ("Account not connected", "bad"),
    "mismatch": ("Sign-in did not match", "bad"),
    "expired": ("Nothing waiting for this sign-in", "bad"),
}


@router.get("/accounts/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(request: Request) -> HTMLResponse:
    """Google's redirect target. Relays the authorization response into the
    pending workspace's sandbox (remote step 2) and tells the human how it
    went; the app's panel learns the same outcome from its poll."""
    outcome = await get_workspace_gog_auth_service().complete_callback(
        request.url.query
    )
    title, tone = _CALLBACK_TITLES.get(outcome.status, ("Account connect", "bad"))
    page = _CALLBACK_PAGE.format(
        tone=tone, title=html.escape(title), message=html.escape(outcome.message)
    )
    return HTMLResponse(content=page, status_code=200)


__all__ = ["AccountsViewRead", "ConnectIn", "PendingRead", "router"]
