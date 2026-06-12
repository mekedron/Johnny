"""Tests for the /workspaces/*/accounts HTTP surface (Johnny-wks.4).

The router is a thin shell over WorkspaceGogAuthService — these tests pin
the HTTP contract: 404 on unknown workspaces, the service-exception → status
code mapping (busy 409 / precondition 422 / unreachable 503 / other 502),
the redirect_uri derivation from the request's own origin, and the callback
page rendering each outcome as human-readable HTML with nothing secret in
it. The service itself is faked via its injection seam.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import get_session
from app.db import Base
from app.db.models import Workspace
from app.main import app
from app.services.workspace_accounts import (
    AccountsView,
    CallbackOutcome,
    GogAccount,
    GogAuthBusyError,
    GogAuthError,
    GogPreconditionError,
    GogSandboxUnreachableError,
    PendingAuth,
    WorkspaceRef,
    set_workspace_gog_auth_service,
)
from app.services.workspaces import seed_default_workspace


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine)
    session = maker()
    try:
        yield session
    finally:
        session.close()


def _pending(workspace_id: int = 2, status: str = "awaiting_callback") -> PendingAuth:
    return PendingAuth(
        workspace_id=workspace_id,
        workspace_name="Finance",
        workspace_slug="finance",
        workspace_is_default=False,
        email="user@example.com",
        services="user",
        redirect_uri="http://testserver/workspaces/accounts/oauth/callback",
        status=status,
        state="STATE123",
        auth_url="https://accounts.google.com/o/oauth2/auth?state=STATE123",
        expires_at=1_600.0,
    )


@dataclass
class _FakeService:
    """Scripted stand-in recording what the router passes through."""

    view: AccountsView | None = None
    connect_result: PendingAuth | Exception | None = None
    outcome: CallbackOutcome | None = None
    disconnect_error: Exception | None = None
    cancel_error: Exception | None = None
    calls: list[tuple[str, Any]] = field(default_factory=list)

    async def accounts_view(self, workspace: WorkspaceRef) -> AccountsView:
        self.calls.append(("view", workspace))
        assert self.view is not None
        return self.view

    async def start_connect(
        self,
        workspace: WorkspaceRef,
        *,
        email: str,
        services: str,
        redirect_uri: str,
    ) -> PendingAuth:
        self.calls.append(
            ("connect", (workspace, email, services, redirect_uri))
        )
        if isinstance(self.connect_result, Exception):
            raise self.connect_result
        assert self.connect_result is not None
        return self.connect_result

    async def complete_callback(self, raw_query: str) -> CallbackOutcome:
        self.calls.append(("callback", raw_query))
        assert self.outcome is not None
        return self.outcome

    async def cancel_pending(self, workspace_id: int) -> None:
        self.calls.append(("cancel", workspace_id))
        if self.cancel_error is not None:
            raise self.cancel_error

    async def disconnect(self, workspace: WorkspaceRef, email: str) -> None:
        self.calls.append(("disconnect", (workspace, email)))
        if self.disconnect_error is not None:
            raise self.disconnect_error


@pytest.fixture
def fake_service() -> Iterator[_FakeService]:
    service = _FakeService()
    set_workspace_gog_auth_service(service)  # type: ignore[arg-type]
    try:
        yield service
    finally:
        set_workspace_gog_auth_service(None)


@pytest.fixture
def client(db_session: Session, fake_service: _FakeService) -> Iterator[TestClient]:
    def _override() -> Iterator[Session]:
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture
def finance(db_session: Session) -> Workspace:
    seed_default_workspace(db_session)
    row = Workspace(name="Finance", slug="finance", is_default=False)
    db_session.add(row)
    db_session.flush()
    return row


# --- GET view -------------------------------------------------------------


def test_get_accounts_view(
    client: TestClient, fake_service: _FakeService, finance: Workspace
) -> None:
    fake_service.view = AccountsView(
        workspace_id=finance.id,
        workspace_name="Finance",
        reachable=True,
        keyring_backend="file",
        client_credentials=True,
        accounts=[GogAccount(email="a@x.com", services=["calendar"])],
        pending=_pending(finance.id),
    )
    resp = client.get(f"/workspaces/{finance.id}/accounts")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reachable"] is True
    assert body["accounts"] == [
        {"email": "a@x.com", "client": "default", "services": ["calendar"]}
    ]
    assert body["pending"]["email"] == "user@example.com"
    assert body["busy"] is None
    # the workspace ref handed to the service carries the row's identity
    (_, ref), *_ = fake_service.calls
    assert (ref.id, ref.slug, ref.is_default) == (finance.id, "finance", False)


def test_get_accounts_unknown_workspace_404(client: TestClient) -> None:
    assert client.get("/workspaces/999/accounts").status_code == 404


# --- connect -----------------------------------------------------------------


def test_connect_returns_pending_and_derives_redirect_uri(
    client: TestClient, fake_service: _FakeService, finance: Workspace
) -> None:
    fake_service.connect_result = _pending(finance.id)
    resp = client.post(
        f"/workspaces/{finance.id}/accounts/connect",
        json={"email": "user@example.com"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "awaiting_callback"
    assert body["auth_url"].startswith("https://accounts.google.com/")
    (_, (_, email, services, redirect_uri)) = next(
        call for call in fake_service.calls if call[0] == "connect"
    )
    assert email == "user@example.com"
    assert services == "user"
    # derived from how the browser addressed THIS api
    assert redirect_uri == "http://testserver/workspaces/accounts/oauth/callback"


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (GogAuthBusyError(_pending()), 409),
        (GogPreconditionError("set GOG_KEYRING_PASSWORD"), 422),
        (GogSandboxUnreachableError("sandbox down"), 503),
        (GogAuthError("gog exploded"), 502),
    ],
)
def test_connect_error_mapping(
    client: TestClient,
    fake_service: _FakeService,
    finance: Workspace,
    error: Exception,
    expected_status: int,
) -> None:
    fake_service.connect_result = error
    resp = client.post(
        f"/workspaces/{finance.id}/accounts/connect",
        json={"email": "user@example.com"},
    )
    assert resp.status_code == expected_status
    assert str(error).split(" ")[0] in resp.json()["detail"]


def test_connect_unknown_workspace_404(client: TestClient) -> None:
    resp = client.post(
        "/workspaces/999/accounts/connect", json={"email": "user@example.com"}
    )
    assert resp.status_code == 404


def test_connect_rejects_unknown_fields(
    client: TestClient, fake_service: _FakeService, finance: Workspace
) -> None:
    resp = client.post(
        f"/workspaces/{finance.id}/accounts/connect",
        json={"email": "user@example.com", "token": "nope"},
    )
    assert resp.status_code == 422


# --- cancel / disconnect ----------------------------------------------------


def test_cancel_pending_204(
    client: TestClient, fake_service: _FakeService, finance: Workspace
) -> None:
    resp = client.delete(f"/workspaces/{finance.id}/accounts/pending")
    assert resp.status_code == 204
    assert ("cancel", finance.id) in fake_service.calls


def test_cancel_foreign_flow_409(
    client: TestClient, fake_service: _FakeService, finance: Workspace
) -> None:
    fake_service.cancel_error = GogAuthBusyError(_pending(workspace_id=99))
    resp = client.delete(f"/workspaces/{finance.id}/accounts/pending")
    assert resp.status_code == 409


def test_disconnect_204_and_routes_email(
    client: TestClient, fake_service: _FakeService, finance: Workspace
) -> None:
    resp = client.delete(f"/workspaces/{finance.id}/accounts/user%40example.com")
    assert resp.status_code == 204
    (_, (_, email)) = next(
        call for call in fake_service.calls if call[0] == "disconnect"
    )
    assert email == "user@example.com"


def test_disconnect_failure_maps_502(
    client: TestClient, fake_service: _FakeService, finance: Workspace
) -> None:
    fake_service.disconnect_error = GogAuthError("gog auth remove failed: nope")
    resp = client.delete(f"/workspaces/{finance.id}/accounts/user%40example.com")
    assert resp.status_code == 502


# --- callback page -------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        ("completed", "Account connected"),
        ("failed", "Account not connected"),
        ("mismatch", "did not match"),
        ("expired", "Nothing waiting"),
    ],
)
def test_callback_renders_each_outcome(
    client: TestClient, fake_service: _FakeService, status: str, fragment: str
) -> None:
    fake_service.outcome = CallbackOutcome(status=status, message="What happened.")
    resp = client.get(
        "/workspaces/accounts/oauth/callback?state=STATE123&code=secret-code"
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert fragment in resp.text
    assert "What happened." in resp.text
    # the raw query reached the service untouched...
    (_, raw_query) = next(
        call for call in fake_service.calls if call[0] == "callback"
    )
    assert raw_query == "state=STATE123&code=secret-code"
    # ...and the one-time code is never echoed back into the page
    assert "secret-code" not in resp.text


def test_callback_message_is_html_escaped(
    client: TestClient, fake_service: _FakeService
) -> None:
    fake_service.outcome = CallbackOutcome(
        status="failed", message='<script>alert("x")</script>'
    )
    resp = client.get("/workspaces/accounts/oauth/callback?state=s")
    assert "<script>alert" not in resp.text
    assert "&lt;script&gt;" in resp.text
