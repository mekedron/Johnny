"""Integration tests for the Google OAuth HTTP endpoints (US-005)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import auth as auth_module
from app.api.deps import get_crypto, get_session
from app.config import Settings, get_settings
from app.db import Base
from app.db.models import AccountRole, CalendarEvent, GoogleAccount
from app.main import app
from app.security.crypto import CredentialCrypto, decrypt_json  # noqa: F401
from app.services.google_oauth import GoogleOAuthError, TokenResponse, UserInfo


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    Base.metadata.create_all(
        bind=eng,
        tables=[
            GoogleAccount.__table__,  # type: ignore[list-item]
            CalendarEvent.__table__,  # type: ignore[list-item]
        ],
    )
    return eng


@pytest.fixture
def db_session(engine: sa.Engine) -> Iterator[Session]:
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def crypto() -> CredentialCrypto:
    return CredentialCrypto(Fernet.generate_key())


@pytest.fixture
def settings_override() -> Settings:
    """Settings with Google OAuth credentials populated for the test run."""
    return Settings(
        database_url="sqlite:///:memory:",
        redis_url="redis://localhost:6379/0",
        fernet_key=Fernet.generate_key().decode("ascii"),
        google_client_id="test-client.apps.googleusercontent.com",
        google_client_secret="test-secret",
        google_oauth_redirect_uri="http://localhost:8000/auth/google/callback",
    )


@pytest.fixture
def client(
    db_session: Session,
    crypto: CredentialCrypto,
    settings_override: Settings,
) -> Iterator[TestClient]:
    def _override_session() -> Iterator[Session]:
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    def _override_crypto() -> CredentialCrypto:
        return crypto

    def _override_settings() -> Settings:
        return settings_override

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_crypto] = _override_crypto
    app.dependency_overrides[get_settings] = _override_settings
    # Endpoints with no DB also pull `settings` via Depends(get_settings).
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def clear_pending_states() -> Iterator[None]:
    """Ensure the in-memory state map is empty around each test."""
    auth_module._pending_states.clear()
    try:
        yield
    finally:
        auth_module._pending_states.clear()


# --- /start ----------------------------------------------------------------


def test_start_returns_consent_url(client: TestClient) -> None:
    resp = client.post("/auth/google/start", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert "authorize_url" in data
    assert "state" in data
    parsed = urlparse(data["authorize_url"])
    qs = parse_qs(parsed.query)
    assert parsed.netloc == "accounts.google.com"
    assert qs["client_id"] == ["test-client.apps.googleusercontent.com"]
    assert qs["redirect_uri"] == ["http://localhost:8000/auth/google/callback"]
    assert qs["state"] == [data["state"]]


def test_start_accepts_client_state(client: TestClient) -> None:
    resp = client.post("/auth/google/start", json={"state": "client-state-123"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "client-state-123"


def test_start_remembers_role_for_callback(client: TestClient) -> None:
    resp = client.post(
        "/auth/google/start",
        json={"role": "bot", "is_default_user": False},
    )
    assert resp.status_code == 200
    state = resp.json()["state"]
    pending = auth_module._peek_state(state)
    assert pending is not None
    assert pending.role.value == "bot"
    assert pending.is_default_user is False


def test_start_returns_503_when_oauth_not_configured(crypto: CredentialCrypto) -> None:
    def _override_settings() -> Settings:
        return Settings(
            database_url="sqlite:///:memory:",
            redis_url="redis://localhost:6379/0",
            fernet_key=Fernet.generate_key().decode("ascii"),
            google_client_id=None,
            google_client_secret=None,
            google_oauth_redirect_uri="http://localhost:8000/cb",
        )

    app.dependency_overrides[get_settings] = _override_settings
    app.dependency_overrides[get_crypto] = lambda: crypto
    try:
        with TestClient(app) as cl:
            resp = cl.post("/auth/google/start", json={})
            assert resp.status_code == 503
            assert "GOOGLE_CLIENT_ID" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_start_rejects_bad_role(client: TestClient) -> None:
    resp = client.post("/auth/google/start", json={"role": "admin"})
    assert resp.status_code == 422


# --- /callback -------------------------------------------------------------


def _fake_token_response(
    *,
    access_token: str = "ya29.access",
    refresh_token: str | None = "1//refresh",
    expires_at: datetime | None = None,
) -> TokenResponse:
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at or datetime.now(UTC) + timedelta(hours=1),
        scope="openid profile",
        id_token=None,
    )


def _fake_userinfo(
    *,
    email: str = "alice@example.com",
    sub: str = "1234567890",
) -> UserInfo:
    return UserInfo(email=email, sub=sub, name="Alice Example")


def test_callback_persists_encrypted_tokens(
    client: TestClient, db_session: Session, crypto: CredentialCrypto
) -> None:
    # Pre-stage a pending state as if /start was called.
    state = "valid-state"
    auth_module._remember_state(
        state, auth_module._PendingState(role=AccountRole.USER, is_default_user=True)
    )

    fake_tokens = _fake_token_response()
    fake_user = _fake_userinfo()
    with (
        patch(
            "app.api.auth.exchange_code_for_tokens",
            new=AsyncMock(return_value=fake_tokens),
        ),
        patch(
            "app.api.auth.fetch_userinfo",
            new=AsyncMock(return_value=fake_user),
        ),
    ):
        resp = client.post(
            "/auth/google/callback",
            json={"code": "code-from-google", "state": state},
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "alice@example.com"
    assert body["role"] == "user"
    assert body["is_default_user"] is True

    # State was consumed (no replay possible).
    assert auth_module._peek_state(state) is None

    # The row exists and tokens are encrypted at rest.
    rows = db_session.scalars(sa.select(GoogleAccount)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.email == "alice@example.com"
    assert row.refresh_token_encrypted != "1//refresh"
    assert row.access_token_encrypted is not None
    assert row.access_token_encrypted != "ya29.access"

    # And round-trip via the same crypto used by the dependency.
    assert crypto.decrypt(row.refresh_token_encrypted) == "1//refresh"
    assert crypto.decrypt(row.access_token_encrypted) == "ya29.access"


def test_callback_rejects_unknown_state(client: TestClient) -> None:
    with (
        patch("app.api.auth.exchange_code_for_tokens", new=AsyncMock()) as ex,
        patch("app.api.auth.fetch_userinfo", new=AsyncMock()) as ui,
    ):
        resp = client.post(
            "/auth/google/callback",
            json={"code": "x", "state": "never-issued"},
        )
    assert resp.status_code == 400
    assert "state" in resp.json()["detail"]
    ex.assert_not_called()
    ui.assert_not_called()


def test_callback_returns_400_on_exchange_failure(client: TestClient) -> None:
    state = "valid-state"
    auth_module._remember_state(
        state, auth_module._PendingState(role=AccountRole.USER, is_default_user=False)
    )
    with patch(
        "app.api.auth.exchange_code_for_tokens",
        new=AsyncMock(side_effect=GoogleOAuthError("invalid_grant")),
    ):
        resp = client.post(
            "/auth/google/callback",
            json={"code": "bad", "state": state},
        )
    assert resp.status_code == 400
    assert "invalid_grant" in resp.json()["detail"]


def test_callback_role_is_bot_when_pending_says_so(
    client: TestClient, db_session: Session
) -> None:
    state = "bot-state"
    auth_module._remember_state(
        state, auth_module._PendingState(role=AccountRole.BOT, is_default_user=False)
    )
    with (
        patch(
            "app.api.auth.exchange_code_for_tokens",
            new=AsyncMock(return_value=_fake_token_response()),
        ),
        patch(
            "app.api.auth.fetch_userinfo",
            new=AsyncMock(return_value=_fake_userinfo(email="johnny-bot@example.com")),
        ),
    ):
        resp = client.post(
            "/auth/google/callback",
            json={"code": "c", "state": state},
        )
    assert resp.status_code == 201
    assert resp.json()["role"] == "bot"
    assert resp.json()["is_default_user"] is False
    row = db_session.scalars(sa.select(GoogleAccount)).one()
    assert row.role.value == "bot"


def test_callback_upserts_same_email(client: TestClient, db_session: Session) -> None:
    """Re-authorising the same email should update tokens in place."""
    # First auth.
    state1 = "s1"
    auth_module._remember_state(
        state1, auth_module._PendingState(role=AccountRole.USER, is_default_user=True)
    )
    with (
        patch(
            "app.api.auth.exchange_code_for_tokens",
            new=AsyncMock(return_value=_fake_token_response(refresh_token="r1")),
        ),
        patch(
            "app.api.auth.fetch_userinfo",
            new=AsyncMock(return_value=_fake_userinfo()),
        ),
    ):
        client.post("/auth/google/callback", json={"code": "c1", "state": state1})

    # Second auth — same email, rotated refresh token.
    state2 = "s2"
    auth_module._remember_state(
        state2, auth_module._PendingState(role=AccountRole.USER, is_default_user=True)
    )
    with (
        patch(
            "app.api.auth.exchange_code_for_tokens",
            new=AsyncMock(return_value=_fake_token_response(refresh_token="r2")),
        ),
        patch(
            "app.api.auth.fetch_userinfo",
            new=AsyncMock(return_value=_fake_userinfo()),
        ),
    ):
        resp = client.post("/auth/google/callback", json={"code": "c2", "state": state2})
    assert resp.status_code == 201

    rows: list[GoogleAccount] = list(db_session.scalars(sa.select(GoogleAccount)).all())
    assert len(rows) == 1


def test_callback_returns_503_when_oauth_not_configured(
    db_session: Session, crypto: CredentialCrypto
) -> None:
    def _override_settings() -> Settings:
        return Settings(
            database_url="sqlite:///:memory:",
            redis_url="redis://localhost:6379/0",
            fernet_key=Fernet.generate_key().decode("ascii"),
            google_client_id=None,
            google_client_secret=None,
            google_oauth_redirect_uri="http://localhost:8000/cb",
        )

    def _override_session() -> Iterator[Session]:
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    app.dependency_overrides[get_settings] = _override_settings
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_crypto] = lambda: crypto
    try:
        with TestClient(app) as cl:
            resp = cl.post(
                "/auth/google/callback",
                json={"code": "x", "state": "y"},
            )
            assert resp.status_code == 503
    finally:
        app.dependency_overrides.clear()


def test_callback_validates_payload(client: TestClient) -> None:
    resp = client.post("/auth/google/callback", json={"code": "", "state": ""})
    assert resp.status_code == 422


# --- Read-side: response model never leaks secrets -------------------------


def test_account_read_excludes_tokens(client: TestClient) -> None:
    state = "secret-state"
    auth_module._remember_state(
        state, auth_module._PendingState(role=AccountRole.USER, is_default_user=False)
    )
    with (
        patch(
            "app.api.auth.exchange_code_for_tokens",
            new=AsyncMock(return_value=_fake_token_response()),
        ),
        patch(
            "app.api.auth.fetch_userinfo",
            new=AsyncMock(return_value=_fake_userinfo()),
        ),
    ):
        resp = client.post("/auth/google/callback", json={"code": "c", "state": state})
    assert resp.status_code == 201
    body: dict[str, Any] = resp.json()
    # Never leak token material in the API response.
    assert "refresh_token" not in body
    assert "access_token" not in body
    assert "refresh_token_encrypted" not in body
    assert "access_token_encrypted" not in body
