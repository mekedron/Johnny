"""Tests for the bot-session storage_state HTTP endpoints (Johnny-4ph)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_crypto, get_session
from app.config import Settings, get_settings
from app.db import Base
from app.db.models import (
    AccountRole,
    CalendarEvent,
    GoogleAccount,
    MeetingConfig,
    ProfileTemplate,
)
from app.main import app
from app.security.crypto import CredentialCrypto
from app.services import bot_auth_seed
from app.services.bot_auth_seed import (
    BOT_AUTH_STATE_ROOT_ENV,
    MAX_STORAGE_STATE_BYTES,
    bot_session_path,
)


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
            ProfileTemplate.__table__,  # type: ignore[list-item]
            MeetingConfig.__table__,  # type: ignore[list-item]
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
    return Settings(
        database_url="sqlite:///:memory:",
        redis_url="redis://localhost:6379/0",
        fernet_key=Fernet.generate_key().decode("ascii"),
        google_client_id="test-client.apps.googleusercontent.com",
        google_client_secret="test-secret",
        google_oauth_redirect_uri="http://localhost:8000/auth/google/callback",
    )


@pytest.fixture
def auth_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv(BOT_AUTH_STATE_ROOT_ENV, str(tmp_path))
    yield tmp_path


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

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_crypto] = lambda: crypto
    app.dependency_overrides[get_settings] = lambda: settings_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _add_account(
    session: Session,
    crypto: CredentialCrypto,
    *,
    email: str,
    role: AccountRole = AccountRole.BOT,
) -> GoogleAccount:
    row = GoogleAccount(
        email=email,
        role=role,
        access_token_encrypted=crypto.encrypt("a"),
        refresh_token_encrypted=crypto.encrypt("r"),
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        is_default_user=False,
    )
    session.add(row)
    session.flush()
    return row


def _valid_state_bytes() -> bytes:
    body = {
        "cookies": [
            {
                "name": "SID",
                "value": "abc",
                "domain": ".google.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "None",
            }
        ],
        "origins": [],
    }
    return json.dumps(body).encode("utf-8")


# --- GET /accounts/{id}/bot-session ---------------------------------------


def test_get_bot_session_reports_not_connected(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
    auth_state_root: Path,
) -> None:
    bot = _add_account(db_session, crypto, email="bot@example.com")
    db_session.commit()

    resp = client.get(f"/auth/google/accounts/{bot.id}/bot-session")
    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is False
    assert body["saved_at"] is None
    assert body["size_bytes"] is None
    assert body["path"].endswith(f"/account-{bot.id}/storage_state.json")


def test_get_bot_session_reports_connected_after_seed(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
    auth_state_root: Path,
) -> None:
    bot = _add_account(db_session, crypto, email="bot@example.com")
    db_session.commit()

    payload = _valid_state_bytes()
    bot_auth_seed.save_bot_session(bot.id, payload)

    resp = client.get(f"/auth/google/accounts/{bot.id}/bot-session")
    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is True
    assert body["size_bytes"] == len(payload)
    assert body["saved_at"] is not None


def test_get_bot_session_rejects_user_account(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
    auth_state_root: Path,
) -> None:
    user = _add_account(
        db_session, crypto, email="alice@example.com", role=AccountRole.USER
    )
    db_session.commit()

    resp = client.get(f"/auth/google/accounts/{user.id}/bot-session")
    assert resp.status_code == 400
    assert "role=bot" in resp.json()["detail"]


def test_get_bot_session_404_for_unknown_account(
    client: TestClient, auth_state_root: Path
) -> None:
    resp = client.get("/auth/google/accounts/9999/bot-session")
    assert resp.status_code == 404


# --- PUT /accounts/{id}/bot-session ---------------------------------------


def test_put_bot_session_persists_storage_state_to_volume(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
    auth_state_root: Path,
) -> None:
    bot = _add_account(db_session, crypto, email="bot@example.com")
    db_session.commit()

    payload = _valid_state_bytes()
    resp = client.put(
        f"/auth/google/accounts/{bot.id}/bot-session",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is True
    assert body["size_bytes"] == len(payload)

    # File landed at the exact path the meet-worker will read.
    saved = bot_session_path(bot.id)
    assert saved.exists()
    assert saved.read_bytes() == payload


def test_put_bot_session_rejects_invalid_json(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
    auth_state_root: Path,
) -> None:
    bot = _add_account(db_session, crypto, email="bot@example.com")
    db_session.commit()

    resp = client.put(
        f"/auth/google/accounts/{bot.id}/bot-session",
        content=b"not valid json at all",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "JSON" in resp.json()["detail"]
    # No file written on failure.
    assert not bot_session_path(bot.id).exists()


def test_put_bot_session_rejects_empty_cookies(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
    auth_state_root: Path,
) -> None:
    bot = _add_account(db_session, crypto, email="bot@example.com")
    db_session.commit()

    resp = client.put(
        f"/auth/google/accounts/{bot.id}/bot-session",
        content=b'{"cookies": []}',
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "cookies" in resp.json()["detail"]


def test_put_bot_session_rejects_empty_body(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
    auth_state_root: Path,
) -> None:
    bot = _add_account(db_session, crypto, email="bot@example.com")
    db_session.commit()

    resp = client.put(
        f"/auth/google/accounts/{bot.id}/bot-session",
        content=b"",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"]


def test_put_bot_session_rejects_user_account(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
    auth_state_root: Path,
) -> None:
    user = _add_account(
        db_session, crypto, email="alice@example.com", role=AccountRole.USER
    )
    db_session.commit()

    resp = client.put(
        f"/auth/google/accounts/{user.id}/bot-session",
        content=_valid_state_bytes(),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    # No file should be created at the bot path.
    assert not bot_session_path(user.id).exists()


def test_put_bot_session_overwrites_existing_file(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
    auth_state_root: Path,
) -> None:
    bot = _add_account(db_session, crypto, email="bot@example.com")
    db_session.commit()

    first = _valid_state_bytes()
    second = json.dumps(
        {
            "cookies": [
                {
                    "name": "HSID",
                    "value": "xyz",
                    "domain": ".google.com",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None",
                }
            ],
            "origins": [],
        }
    ).encode("utf-8")

    client.put(
        f"/auth/google/accounts/{bot.id}/bot-session",
        content=first,
        headers={"Content-Type": "application/json"},
    )
    client.put(
        f"/auth/google/accounts/{bot.id}/bot-session",
        content=second,
        headers={"Content-Type": "application/json"},
    )
    assert bot_session_path(bot.id).read_bytes() == second


def test_put_bot_session_rejects_oversize(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
    auth_state_root: Path,
) -> None:
    bot = _add_account(db_session, crypto, email="bot@example.com")
    db_session.commit()

    oversize = b"x" * (MAX_STORAGE_STATE_BYTES + 1)
    resp = client.put(
        f"/auth/google/accounts/{bot.id}/bot-session",
        content=oversize,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"]


# --- DELETE /accounts/{id}/bot-session ------------------------------------


def test_delete_bot_session_removes_file(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
    auth_state_root: Path,
) -> None:
    bot = _add_account(db_session, crypto, email="bot@example.com")
    db_session.commit()
    bot_auth_seed.save_bot_session(bot.id, _valid_state_bytes())
    assert bot_session_path(bot.id).exists()

    resp = client.delete(f"/auth/google/accounts/{bot.id}/bot-session")
    assert resp.status_code == 200
    assert resp.json()["connected"] is False
    assert not bot_session_path(bot.id).exists()


def test_delete_bot_session_is_idempotent(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
    auth_state_root: Path,
) -> None:
    bot = _add_account(db_session, crypto, email="bot@example.com")
    db_session.commit()

    # Never saved a file; deleting still succeeds with connected=False.
    resp = client.delete(f"/auth/google/accounts/{bot.id}/bot-session")
    assert resp.status_code == 200
    assert resp.json()["connected"] is False


def test_delete_bot_session_rejects_user_account(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
    auth_state_root: Path,
) -> None:
    user = _add_account(
        db_session, crypto, email="alice@example.com", role=AccountRole.USER
    )
    db_session.commit()

    resp = client.delete(f"/auth/google/accounts/{user.id}/bot-session")
    assert resp.status_code == 400
