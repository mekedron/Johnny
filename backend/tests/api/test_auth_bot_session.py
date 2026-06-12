"""Tests for the bot-session storage_state HTTP endpoints.

After Johnny-ckz.23 reinstated the CLI+upload path alongside noVNC, the
surface has two upload-side endpoints:

* ``PUT /auth/google/accounts/{id}/bot-session`` — replace / attach a
  storage_state for an existing row (Replace session, Attach to existing).
* ``POST /auth/google/accounts/bot/upload`` — create-or-attach a bot
  identity by email (Add another meeting bot → Upload path).
* ``DELETE /auth/google/accounts/{id}/bot-session`` — drops the bot
  capability, leaves the row (and any calendar capability) intact.
* ``app.services.bot_auth_seed`` — file-level helpers used by the
  supervisor and the API.

Both upload endpoints land in the SAME on-disk location as the noVNC
supervisor, so the meet-worker (live Meet) and the playground use the
same file from there on (failure-domain test below).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

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
from app.services.bot_session_probe import (
    BotSessionProbeResult,
    BotSessionProbeUnavailableError,
)

# The bot leg of POST /accounts/{id}/verify round-trips to Google via a
# headless Playwright probe (Johnny-ckz.24). The tests below inject a fake
# prober (patching ``app.api.auth.probe_bot_session``) so the ok/message
# mapping is exercised deterministically without Docker. The opt-in test
# that actually hits Google lives in tests/services/test_bot_session_probe.py.
_PROBE_TARGET = "app.api.auth.probe_bot_session"


def _signed_in(email: str | None) -> object:
    """A fake prober that reports a live, signed-in session as ``email``."""

    def _probe(_account_id: int, **_kwargs: object) -> BotSessionProbeResult:
        return BotSessionProbeResult(
            signed_in=True, email=email, final_url="https://myaccount.google.com/"
        )

    return _probe


def _not_signed_in(error: str | None = None) -> object:
    """A fake prober that reports Google bounced the cookies to sign-in."""

    def _probe(_account_id: int, **_kwargs: object) -> BotSessionProbeResult:
        return BotSessionProbeResult(
            signed_in=False,
            email=None,
            final_url="https://accounts.google.com/v3/signin/identifier",
            error=error,
        )

    return _probe


def _probe_unavailable(reason: str) -> object:
    """A fake prober that fails to run (infra), raising Unavailable."""

    def _probe(_account_id: int, **_kwargs: object) -> BotSessionProbeResult:
        raise BotSessionProbeUnavailableError(reason)

    return _probe


def _future_state_bytes(days: int = 30) -> bytes:
    """A well-shaped storage_state whose soonest cookie expires in ``days``."""
    expires = datetime.now(UTC) + timedelta(days=days)
    return json.dumps(
        {
            "cookies": [
                {
                    "name": "SID",
                    "value": "abc",
                    "domain": ".google.com",
                    "path": "/",
                    "expires": expires.timestamp(),
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None",
                }
            ],
            "origins": [],
        }
    ).encode("utf-8")


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


# --- POST /accounts/{id}/verify · bot-session leg -------------------------


def test_verify_bot_session_live_signed_in_reports_email(
    client: TestClient,
    db_session: Session,
    auth_state_root: Path,
) -> None:
    """A live session whose scraped email matches the row → ok with the
    signed-in email, and the soonest-expiry detail is preserved."""
    bot = _add_bot_account(db_session, email="bot@example.com")
    db_session.commit()
    bot_auth_seed.save_bot_session(bot.id, _future_state_bytes(days=30))

    with patch(_PROBE_TARGET, _signed_in("bot@example.com")):
        resp = client.post(f"/auth/google/accounts/{bot.id}/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["bot_session"]["ok"] is True
    assert "bot@example.com" in body["bot_session"]["message"]
    # Regression: cookie-expiry detail survives the round-trip rewrite.
    assert body["bot_session"]["detail"]["cookie_count"] == 1
    assert body["bot_session"]["detail"]["soonest_expiry"] is not None
    assert body["bot_session"]["detail"]["days_until_expiry"] > 25


def test_verify_bot_session_fake_cookies_not_signed_in(
    client: TestClient,
    db_session: Session,
    auth_state_root: Path,
) -> None:
    """The user's repro: a well-shaped but fake storage_state must flip to
    ok=False — Google bounces the cookies to the sign-in page."""
    bot = _add_bot_account(db_session, email="bot@example.com")
    db_session.commit()
    # The exact fake shape from the ticket: right shape, far-future expiry,
    # but no real session behind it.
    fake = json.dumps(
        {
            "cookies": [
                {
                    "name": "fake",
                    "value": "fake",
                    "domain": ".google.com",
                    "path": "/",
                    "expires": 9999999999,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ]
        }
    ).encode("utf-8")
    bot_auth_seed.save_bot_session(bot.id, fake)

    with patch(_PROBE_TARGET, _not_signed_in()):
        resp = client.post(f"/auth/google/accounts/{bot.id}/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["bot_session"]["ok"] is False
    assert "sign-in page" in body["bot_session"]["message"].lower()
    assert body["bot_session"]["detail"]["signed_in"] is False


def test_verify_bot_session_mismatched_account(
    client: TestClient,
    db_session: Session,
    auth_state_root: Path,
) -> None:
    """Cookies for a DIFFERENT Google account → ok=False naming both
    emails so the user understands the row is bound to the wrong identity."""
    bot = _add_bot_account(db_session, email="bot@example.com")
    db_session.commit()
    bot_auth_seed.save_bot_session(bot.id, _future_state_bytes())

    with patch(_PROBE_TARGET, _signed_in("someone-else@gmail.com")):
        resp = client.post(f"/auth/google/accounts/{bot.id}/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["bot_session"]["ok"] is False
    message = body["bot_session"]["message"]
    assert "someone-else@gmail.com" in message
    assert "bot@example.com" in message
    assert body["bot_session"]["detail"]["expected_email"] == "bot@example.com"


def test_verify_bot_session_signed_in_no_email_still_ok(
    client: TestClient,
    db_session: Session,
    auth_state_root: Path,
) -> None:
    """A live session whose email could not be scraped is still ok — the
    session works; only identity confirmation is missing."""
    bot = _add_bot_account(db_session, email="bot@example.com")
    db_session.commit()
    bot_auth_seed.save_bot_session(bot.id, _future_state_bytes())

    with patch(_PROBE_TARGET, _signed_in(None)):
        resp = client.post(f"/auth/google/accounts/{bot.id}/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["bot_session"]["ok"] is True
    assert "could not read" in body["bot_session"]["message"].lower()


def test_verify_bot_session_placeholder_email_skips_mismatch(
    client: TestClient,
    db_session: Session,
    auth_state_root: Path,
) -> None:
    """A row whose email is the unknown-*@johnny.local placeholder doesn't
    trigger the mismatch check — a scraped real email is an improvement."""
    bot = _add_bot_account(db_session, email="unknown-deadbeef@johnny.local")
    db_session.commit()
    bot_auth_seed.save_bot_session(bot.id, _future_state_bytes())

    with patch(_PROBE_TARGET, _signed_in("real-bot@gmail.com")):
        resp = client.post(f"/auth/google/accounts/{bot.id}/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["bot_session"]["ok"] is True
    assert "real-bot@gmail.com" in body["bot_session"]["message"]


def test_verify_bot_session_probe_unavailable_is_not_a_pass(
    client: TestClient,
    db_session: Session,
    auth_state_root: Path,
) -> None:
    """If the probe can't run at all, verify must NOT report a pass — a
    silent pass on an unrunnable check is the bug this ticket fixes."""
    bot = _add_bot_account(db_session, email="bot@example.com")
    db_session.commit()
    bot_auth_seed.save_bot_session(bot.id, _future_state_bytes())

    with patch(_PROBE_TARGET, _probe_unavailable("docker daemon down")):
        resp = client.post(f"/auth/google/accounts/{bot.id}/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["bot_session"]["ok"] is False
    assert "could not run" in body["bot_session"]["message"].lower()


def test_verify_bot_session_expired_cookie_fast_fails_without_probe(
    client: TestClient,
    db_session: Session,
    auth_state_root: Path,
) -> None:
    """A storage_state whose soonest persistent cookie is past-due must
    surface ok=False WITHOUT paying the probe cost (no probe call)."""
    bot = _add_bot_account(db_session, email="bot@example.com")
    db_session.commit()
    expires = datetime.now(UTC) - timedelta(days=1)
    payload = json.dumps(
        {
            "cookies": [
                {
                    "name": "SID",
                    "value": "abc",
                    "domain": ".google.com",
                    "path": "/",
                    "expires": expires.timestamp(),
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None",
                }
            ],
            "origins": [],
        }
    ).encode("utf-8")
    bot_auth_seed.save_bot_session(bot.id, payload)

    # The probe must never be reached for an already-expired state.
    def _explode(*_a: object, **_k: object) -> BotSessionProbeResult:
        raise AssertionError("probe must not run for expired cookies")

    with patch(_PROBE_TARGET, _explode):
        resp = client.post(f"/auth/google/accounts/{bot.id}/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["bot_session"]["ok"] is False
    assert "past" in body["bot_session"]["message"].lower()


# --- POST /accounts/bot/upload (create-or-attach) -------------------------


def test_post_bot_upload_creates_new_row(
    client: TestClient,
    db_session: Session,
    auth_state_root: Path,
) -> None:
    """No row with this email yet — endpoint creates a bot-only row and
    writes the storage_state, returns the new AccountRead."""
    payload = _valid_state_bytes()
    resp = client.post(
        "/auth/google/accounts/bot/upload",
        params={"email": "fresh@example.com"},
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "fresh@example.com"
    assert body["has_calendar"] is False
    assert body["bot_session"]["connected"] is True
    assert body["bot_session"]["size_bytes"] == len(payload)
    # File-on-disk parity with the noVNC supervisor path.
    saved = bot_session_path(body["id"])
    assert saved.exists()
    assert saved.read_bytes() == payload


def test_post_bot_upload_attaches_to_existing_calendar_row(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
    auth_state_root: Path,
) -> None:
    """A row that already has calendar capability can also gain bot
    capability via upload — match by email so we don't fork the
    identity."""
    row = _add_calendar_account(db_session, crypto, email="dual@example.com")
    db_session.commit()
    payload = _valid_state_bytes()
    resp = client.post(
        "/auth/google/accounts/bot/upload",
        params={"email": "dual@example.com"},
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == row.id
    assert body["has_calendar"] is True
    assert body["bot_session"]["connected"] is True


def test_post_bot_upload_normalizes_email_case(
    client: TestClient,
    db_session: Session,
    auth_state_root: Path,
) -> None:
    """Mixed-case email matches a row stored lowercase — Google emails
    are case-insensitive so a "Bot@Example.com" upload must land on
    the existing "bot@example.com" row, not fork it."""
    existing = _add_bot_account(db_session, email="bot@example.com")
    db_session.commit()
    resp = client.post(
        "/auth/google/accounts/bot/upload",
        params={"email": "Bot@Example.COM"},
        content=_valid_state_bytes(),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == existing.id


def test_post_bot_upload_rejects_invalid_json(
    client: TestClient,
    db_session: Session,
    auth_state_root: Path,
) -> None:
    resp = client.post(
        "/auth/google/accounts/bot/upload",
        params={"email": "fresh@example.com"},
        content=b"not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "JSON" in resp.json()["detail"]


def test_post_bot_upload_rejects_malformed_email(
    client: TestClient,
    db_session: Session,
    auth_state_root: Path,
) -> None:
    resp = client.post(
        "/auth/google/accounts/bot/upload",
        params={"email": "no-at-sign"},
        content=_valid_state_bytes(),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_post_bot_upload_rejects_empty_body(
    client: TestClient,
    db_session: Session,
    auth_state_root: Path,
) -> None:
    resp = client.post(
        "/auth/google/accounts/bot/upload",
        params={"email": "fresh@example.com"},
        content=b"",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


# --- Failure-domain: upload works independently of noVNC infra -----------


def test_upload_works_when_novnc_launcher_is_down(
    client: TestClient,
    db_session: Session,
    auth_state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the noVNC launcher / docker daemon is broken, the upload path
    MUST still succeed end-to-end. The two sign-in methods are
    independent failure domains; that's the whole reason the CLI/upload
    path stays first-class (Johnny-ckz.23)."""
    from app.api import bot_signin as bot_signin_api

    class _BrokenLauncher:
        def start(self, **_: object) -> str:
            raise bot_signin_api.BotSigninLauncherError("docker daemon down")

        def stop(self, **_: object) -> None:
            raise bot_signin_api.BotSigninLauncherError("docker daemon down")

    bot_signin_api.set_launcher(_BrokenLauncher())  # type: ignore[arg-type]
    try:
        # Confirm the noVNC path is indeed broken under the same client.
        resp_novnc = client.post(
            "/auth/google/accounts/bot/signin/start", json={}
        )
        assert resp_novnc.status_code == 503

        # Upload path is unaffected: row is created, storage_state lands.
        resp_upload = client.post(
            "/auth/google/accounts/bot/upload",
            params={"email": "fallback@example.com"},
            content=_valid_state_bytes(),
            headers={"Content-Type": "application/json"},
        )
        assert resp_upload.status_code == 201
        body = resp_upload.json()
        assert body["bot_session"]["connected"] is True
    finally:
        bot_signin_api.set_launcher(None)


# --- Playground / Meet parity --------------------------------------------


def test_upload_path_matches_meet_worker_resolver(
    auth_state_root: Path,
) -> None:
    """The upload endpoint writes to the same on-disk location the
    meet-worker (and playground, which reuses the same worker) reads
    from, so both paths see whichever storage_state was last written —
    regardless of which sign-in method produced it.

    Asserted directly so a refactor that drifts ``bot_session_path``
    from ``storage_state_path_for_account`` fails loudly here."""
    from johnny.meet_worker.storage_state import storage_state_path_for_account

    saved = save_bot_session(account_id=11, raw=_valid_state_bytes())
    api_path = bot_session_path(11)
    worker_path = storage_state_path_for_account(
        11, env={"JOHNNY_AUTH_STATE_DIR": str(auth_state_root)}
    )
    assert api_path == worker_path
    assert saved["path"] == str(api_path)
    assert worker_path.exists()
