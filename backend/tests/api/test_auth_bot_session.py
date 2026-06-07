"""Tests for the bot-session storage_state helpers and disconnect endpoint.

After the accounts redesign (Johnny-pia) the file-upload PUT endpoint is
gone — bot identities are signed in via noVNC and the supervisor writes
``storage_state.json`` directly. What remains:

* ``DELETE /auth/google/accounts/{id}/bot-session`` — drops the bot
  capability, leaves the row (and any calendar capability) intact.
* ``app.services.bot_auth_seed`` — file-level helpers used by the
  supervisor and the API. Validated directly here since they no
  longer have an upload endpoint to gate them.
"""

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
    BotSessionError,
    bot_session_path,
    bot_session_status,
    delete_bot_session,
    save_bot_session,
    validate_storage_state,
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


def _add_bot_account(session: Session, *, email: str) -> GoogleAccount:
    row = GoogleAccount(email=email, refresh_token_encrypted=None)
    session.add(row)
    session.flush()
    return row


def _add_calendar_account(
    session: Session, crypto: CredentialCrypto, *, email: str
) -> GoogleAccount:
    row = GoogleAccount(
        email=email,
        access_token_encrypted=crypto.encrypt("a"),
        refresh_token_encrypted=crypto.encrypt("r"),
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
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


# --- validate_storage_state ------------------------------------------------


def test_validate_storage_state_accepts_well_formed() -> None:
    data = validate_storage_state(_valid_state_bytes())
    assert isinstance(data, dict)
    assert isinstance(data["cookies"], list)
    assert len(data["cookies"]) == 1


def test_validate_storage_state_rejects_too_large() -> None:
    oversize = b"x" * (MAX_STORAGE_STATE_BYTES + 1)
    with pytest.raises(BotSessionError, match="too large"):
        validate_storage_state(oversize)


def test_validate_storage_state_rejects_bad_json() -> None:
    with pytest.raises(BotSessionError, match="JSON"):
        validate_storage_state(b"not valid json")


def test_validate_storage_state_rejects_empty_cookies() -> None:
    with pytest.raises(BotSessionError, match="cookies"):
        validate_storage_state(b'{"cookies": []}')


def test_validate_storage_state_requires_cookies_array() -> None:
    with pytest.raises(BotSessionError, match="cookies"):
        validate_storage_state(b'{"origins": []}')


# --- save_bot_session / bot_session_status / delete_bot_session -----------


def test_save_bot_session_writes_atomically(auth_state_root: Path) -> None:
    payload = _valid_state_bytes()
    result = save_bot_session(account_id=7, raw=payload)
    target = bot_session_path(7)
    assert target.exists()
    assert target.read_bytes() == payload
    assert result["connected"] is True
    assert result["size_bytes"] == len(payload)


def test_bot_session_status_reports_disconnected_when_missing(
    auth_state_root: Path,
) -> None:
    status = bot_session_status(99)
    assert status["connected"] is False
    assert status["saved_at"] is None
    assert status["size_bytes"] is None


def test_save_bot_session_overwrites_existing(auth_state_root: Path) -> None:
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
    save_bot_session(account_id=3, raw=first)
    save_bot_session(account_id=3, raw=second)
    assert bot_session_path(3).read_bytes() == second


def test_delete_bot_session_is_idempotent(auth_state_root: Path) -> None:
    # No file yet — delete returns False, no error.
    assert delete_bot_session(account_id=42) is False
    save_bot_session(account_id=42, raw=_valid_state_bytes())
    assert delete_bot_session(account_id=42) is True
    assert not bot_session_path(42).exists()


# --- DELETE /accounts/{id}/bot-session ------------------------------------


def test_delete_endpoint_removes_file_and_returns_account(
    client: TestClient,
    db_session: Session,
    auth_state_root: Path,
) -> None:
    bot = _add_bot_account(db_session, email="bot@example.com")
    db_session.commit()
    bot_auth_seed.save_bot_session(bot.id, _valid_state_bytes())
    assert bot_session_path(bot.id).exists()

    resp = client.delete(f"/auth/google/accounts/{bot.id}/bot-session")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == bot.id
    assert body["bot_session"]["connected"] is False
    assert not bot_session_path(bot.id).exists()


def test_delete_endpoint_idempotent(
    client: TestClient,
    db_session: Session,
    auth_state_root: Path,
) -> None:
    bot = _add_bot_account(db_session, email="bot@example.com")
    db_session.commit()

    # Never saved a file; delete still returns 200 with connected=False.
    resp = client.delete(f"/auth/google/accounts/{bot.id}/bot-session")
    assert resp.status_code == 200
    assert resp.json()["bot_session"]["connected"] is False


def test_delete_endpoint_leaves_calendar_capability(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
    auth_state_root: Path,
) -> None:
    """A row that has both capabilities loses only the bot side."""
    row = _add_calendar_account(db_session, crypto, email="dual@example.com")
    bot_auth_seed.save_bot_session(row.id, _valid_state_bytes())
    db_session.commit()

    resp = client.delete(f"/auth/google/accounts/{row.id}/bot-session")
    assert resp.status_code == 200
    body = resp.json()
    assert body["bot_session"]["connected"] is False
    assert body["has_calendar"] is True
    # Row stays.
    assert db_session.get(GoogleAccount, row.id) is not None


def test_delete_endpoint_404_for_unknown_id(client: TestClient) -> None:
    resp = client.delete("/auth/google/accounts/9999/bot-session")
    assert resp.status_code == 404


# --- PUT /accounts/{id}/bot-session (transitional until noVNC) ------------


def test_put_bot_session_writes_storage_state(
    client: TestClient,
    db_session: Session,
    auth_state_root: Path,
) -> None:
    bot = _add_bot_account(db_session, email="bot@example.com")
    db_session.commit()

    payload = _valid_state_bytes()
    resp = client.put(
        f"/auth/google/accounts/{bot.id}/bot-session",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["bot_session"]["connected"] is True
    assert body["bot_session"]["size_bytes"] == len(payload)
    saved = bot_session_path(bot.id)
    assert saved.exists()
    assert saved.read_bytes() == payload


def test_put_bot_session_rejects_invalid_json(
    client: TestClient,
    db_session: Session,
    auth_state_root: Path,
) -> None:
    bot = _add_bot_account(db_session, email="bot@example.com")
    db_session.commit()
    resp = client.put(
        f"/auth/google/accounts/{bot.id}/bot-session",
        content=b"not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "JSON" in resp.json()["detail"]


def test_put_bot_session_rejects_empty_body(
    client: TestClient,
    db_session: Session,
    auth_state_root: Path,
) -> None:
    bot = _add_bot_account(db_session, email="bot@example.com")
    db_session.commit()
    resp = client.put(
        f"/auth/google/accounts/{bot.id}/bot-session",
        content=b"",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_put_bot_session_404_for_unknown_id(
    client: TestClient, auth_state_root: Path
) -> None:
    resp = client.put(
        "/auth/google/accounts/9999/bot-session",
        content=_valid_state_bytes(),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 404


def test_put_bot_session_works_on_calendar_account(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
    auth_state_root: Path,
) -> None:
    """Any row can host a bot session — the new model has no role gate.

    A row that already has calendar capability can also gain bot
    capability by writing a storage_state.json against its id.
    """
    row = _add_calendar_account(db_session, crypto, email="dual@example.com")
    db_session.commit()
    resp = client.put(
        f"/auth/google/accounts/{row.id}/bot-session",
        content=_valid_state_bytes(),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["bot_session"]["connected"] is True
    assert body["has_calendar"] is True
