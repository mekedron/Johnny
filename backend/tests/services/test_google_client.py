"""Tests for the shared Google API client wrapper (US-005)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.db.models import CalendarEvent, GoogleAccount
from app.security.crypto import CredentialCrypto
from app.services import google_client as gc
from app.services.google_client import (
    GoogleApiClient,
    GoogleApiClientError,
    TokenUndecryptableError,
    can_decrypt_refresh_token,
    upsert_account_from_tokens,
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
        ],
    )
    return eng


@pytest.fixture
def session(engine: sa.Engine) -> Iterator[Session]:
    sess = Session(engine)
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture
def crypto() -> CredentialCrypto:
    return CredentialCrypto(Fernet.generate_key())


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        redis_url="redis://localhost:6379/0",
        fernet_key=Fernet.generate_key().decode("ascii"),
        google_client_id="cid",
        google_client_secret="cs",
        google_oauth_redirect_uri="http://localhost:8000/cb",
    )


def _make_account(
    session: Session,
    crypto: CredentialCrypto,
    *,
    access_token: str = "old-access",
    refresh_token: str = "refresh-1",
    expires_at: datetime | None = None,
) -> GoogleAccount:
    row = GoogleAccount(
        email="alice@example.com",
        access_token_encrypted=crypto.encrypt(access_token),
        refresh_token_encrypted=crypto.encrypt(refresh_token),
        token_expires_at=expires_at,
    )
    session.add(row)
    session.flush()
    return row


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- upsert_account_from_tokens -------------------------------------------


def test_upsert_inserts_new_row(session: Session, crypto: CredentialCrypto) -> None:
    row = upsert_account_from_tokens(
        session=session,
        crypto=crypto,
        email="bob@example.com",
        access_token="ya29.a",
        refresh_token="1//r",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert row.id is not None
    assert row.email == "bob@example.com"
    assert row.refresh_token_encrypted is not None
    assert crypto.decrypt(row.refresh_token_encrypted) == "1//r"
    assert crypto.decrypt(row.access_token_encrypted or "") == "ya29.a"


def test_upsert_updates_existing_row(session: Session, crypto: CredentialCrypto) -> None:
    upsert_account_from_tokens(
        session=session,
        crypto=crypto,
        email="alice@example.com",
        access_token="old",
        refresh_token="old-r",
        expires_at=datetime.now(UTC),
    )
    new_expires = datetime.now(UTC) + timedelta(hours=2)
    row = upsert_account_from_tokens(
        session=session,
        crypto=crypto,
        email="alice@example.com",
        access_token="new",
        refresh_token="new-r",
        expires_at=new_expires,
    )
    rows = session.scalars(sa.select(GoogleAccount)).all()
    assert len(rows) == 1
    assert row.refresh_token_encrypted is not None
    assert crypto.decrypt(row.refresh_token_encrypted) == "new-r"


def test_upsert_attaches_calendar_to_existing_bot_only_row(
    session: Session, crypto: CredentialCrypto
) -> None:
    """A bot-only row (no refresh token) gets calendar tokens attached
    rather than triggering a duplicate insert."""
    bot_only = GoogleAccount(email="alice@example.com", refresh_token_encrypted=None)
    session.add(bot_only)
    session.flush()
    bot_id = bot_only.id

    row = upsert_account_from_tokens(
        session=session,
        crypto=crypto,
        email="alice@example.com",
        access_token="ya29",
        refresh_token="1//r",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert row.id == bot_id
    assert row.refresh_token_encrypted is not None
    rows = session.scalars(sa.select(GoogleAccount)).all()
    assert len(rows) == 1


# --- GoogleApiClient: token freshness ------------------------------------


async def test_returns_cached_access_token_when_fresh(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    expires = datetime.now(UTC) + timedelta(minutes=10)
    account = _make_account(session, crypto, expires_at=expires)
    client = GoogleApiClient(
        session=session, account=account, crypto=crypto, settings=settings
    )
    token = await client.get_access_token()
    assert token == "old-access"
    await client.aclose()


async def test_refreshes_when_token_expired(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    expired = datetime.now(UTC) - timedelta(minutes=5)
    account = _make_account(session, crypto, expires_at=expired)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "fresh-access",
                "expires_in": 3600,
            },
        )

    client = GoogleApiClient(
        session=session,
        account=account,
        crypto=crypto,
        settings=settings,
        http_client=_mock_client(handler),
    )
    token = await client.get_access_token()
    assert token == "fresh-access"

    # The row was updated and the access token re-encrypted.
    session.refresh(account)
    assert crypto.decrypt(account.access_token_encrypted or "") == "fresh-access"
    assert account.token_expires_at is not None
    # SQLite strips tzinfo; normalise before comparing.
    expires = account.token_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    assert expires > datetime.now(UTC)
    # Refresh token unchanged (Google didn't rotate).
    assert crypto.decrypt(account.refresh_token_encrypted) == "refresh-1"
    await client.aclose()


async def test_refreshes_when_token_close_to_expiry(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    """Tokens within the leeway window are treated as expired."""
    near_expiry = datetime.now(UTC) + timedelta(
        seconds=gc.REFRESH_LEEWAY_S - 1
    )
    account = _make_account(session, crypto, expires_at=near_expiry)

    refreshed = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal refreshed
        refreshed = True
        return httpx.Response(
            200, json={"access_token": "new-access", "expires_in": 3600}
        )

    client = GoogleApiClient(
        session=session,
        account=account,
        crypto=crypto,
        settings=settings,
        http_client=_mock_client(handler),
    )
    await client.get_access_token()
    assert refreshed is True
    await client.aclose()


async def test_refresh_persists_rotated_refresh_token(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    account = _make_account(
        session,
        crypto,
        refresh_token="old-refresh",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "new-a",
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
            },
        )

    client = GoogleApiClient(
        session=session,
        account=account,
        crypto=crypto,
        settings=settings,
        http_client=_mock_client(handler),
    )
    await client.get_access_token()
    session.refresh(account)
    assert crypto.decrypt(account.refresh_token_encrypted) == "rotated-refresh"
    await client.aclose()


async def test_refresh_raises_when_credentials_not_configured(
    session: Session, crypto: CredentialCrypto
) -> None:
    bad = Settings(
        database_url="sqlite:///:memory:",
        redis_url="redis://localhost:6379/0",
        fernet_key=Fernet.generate_key().decode("ascii"),
        google_client_id=None,
        google_client_secret=None,
    )
    account = _make_account(
        session, crypto, expires_at=datetime.now(UTC) - timedelta(hours=1)
    )
    client = GoogleApiClient(session=session, account=account, crypto=crypto, settings=bad)
    with pytest.raises(GoogleApiClientError):
        await client.get_access_token()
    await client.aclose()


# --- GoogleApiClient.request: auth header, 401 retry ----------------------


async def test_request_attaches_authorization_header(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    account = _make_account(
        session, crypto, expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": []})

    client = GoogleApiClient(
        session=session,
        account=account,
        crypto=crypto,
        settings=settings,
        http_client=_mock_client(handler),
    )
    resp = await client.request(
        "GET",
        "https://www.googleapis.com/calendar/v3/calendars/primary/events",
    )
    assert resp.status_code == 200
    assert seen[0].headers["authorization"] == "Bearer old-access"
    await client.aclose()


async def test_request_refreshes_and_retries_on_401(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    """When the API returns 401, refresh once and retry the call."""
    # Token is "fresh" per the clock but Google has invalidated it server-side.
    account = _make_account(
        session, crypto, expires_at=datetime.now(UTC) + timedelta(hours=1)
    )

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        auth = request.headers.get("authorization", "")
        calls.append(f"{request.method} {url} {auth}")
        if "oauth2.googleapis.com/token" in url:
            return httpx.Response(
                200,
                json={"access_token": "refreshed-access", "expires_in": 3600},
            )
        if auth == "Bearer old-access":
            return httpx.Response(401, json={"error": "invalid_credentials"})
        return httpx.Response(200, json={"ok": True})

    client = GoogleApiClient(
        session=session,
        account=account,
        crypto=crypto,
        settings=settings,
        http_client=_mock_client(handler),
    )
    resp = await client.request(
        "GET", "https://www.googleapis.com/calendar/v3/calendars/primary/events"
    )
    assert resp.status_code == 200
    # The handler was hit three times: original 401, refresh, retry.
    assert len(calls) == 3
    assert "oauth2.googleapis.com/token" in calls[1]
    assert "Bearer refreshed-access" in calls[2]
    await client.aclose()


async def test_request_returns_non_401_errors_unchanged(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    account = _make_account(
        session, crypto, expires_at=datetime.now(UTC) + timedelta(hours=1)
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    client = GoogleApiClient(
        session=session,
        account=account,
        crypto=crypto,
        settings=settings,
        http_client=_mock_client(handler),
    )
    resp = await client.request("GET", "https://www.googleapis.com/x")
    assert resp.status_code == 403
    await client.aclose()


async def test_context_manager_closes_owned_client(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    expires = datetime.now(UTC) + timedelta(hours=1)
    account = _make_account(session, crypto, expires_at=expires)
    async with GoogleApiClient(
        session=session, account=account, crypto=crypto, settings=settings
    ) as client:
        assert client.account is account
    # No assertion needed: a leak would surface as a warning from httpx.


# --- revoke_account --------------------------------------------------------


async def test_revoke_account_calls_endpoint_and_deletes_row(
    session: Session, crypto: CredentialCrypto
) -> None:
    account = _make_account(session, crypto)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="")

    await gc.revoke_account(
        session=session,
        account=account,
        crypto=crypto,
        http_client=_mock_client(handler),
    )
    assert "revoke" in str(seen[0].url)
    assert session.scalars(sa.select(GoogleAccount)).all() == []


async def test_revoke_account_still_deletes_row_when_endpoint_fails(
    session: Session, crypto: CredentialCrypto
) -> None:
    account = _make_account(session, crypto)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    # Should not raise — local cleanup must happen even if Google barfs.
    await gc.revoke_account(
        session=session,
        account=account,
        crypto=crypto,
        http_client=_mock_client(handler),
    )
    assert session.scalars(sa.select(GoogleAccount)).all() == []


# --- Undecryptable refresh token paths ------------------------------------


async def test_refresh_raises_typed_error_when_refresh_token_undecryptable(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    """A FERNET_KEY rotation surfaces as :class:`TokenUndecryptableError`."""
    expired = datetime.now(UTC) - timedelta(minutes=1)
    account = _make_account(session, crypto, expires_at=expired)
    # Simulate post-rotation: a fresh CredentialCrypto with a new key
    # cannot decrypt rows written with the old key.
    rotated = CredentialCrypto(Fernet.generate_key())
    client = GoogleApiClient(
        session=session, account=account, crypto=rotated, settings=settings
    )
    with pytest.raises(TokenUndecryptableError) as exc:
        await client.get_access_token()
    assert exc.value.account_id == account.id
    assert exc.value.email == account.email
    # TokenUndecryptableError subclasses GoogleApiClientError so legacy
    # callers that catch the broader type still work.
    assert isinstance(exc.value, GoogleApiClientError)
    await client.aclose()


async def test_revoke_account_raises_typed_error_when_undecryptable(
    session: Session, crypto: CredentialCrypto
) -> None:
    account = _make_account(session, crypto)
    rotated = CredentialCrypto(Fernet.generate_key())
    with pytest.raises(TokenUndecryptableError) as exc:
        await gc.revoke_account(
            session=session,
            account=account,
            crypto=rotated,
        )
    assert exc.value.account_id == account.id


def test_can_decrypt_refresh_token_true_for_valid_row(
    session: Session, crypto: CredentialCrypto
) -> None:
    account = _make_account(session, crypto)
    assert can_decrypt_refresh_token(account=account, crypto=crypto) is True


def test_can_decrypt_refresh_token_false_after_key_rotation(
    session: Session, crypto: CredentialCrypto
) -> None:
    account = _make_account(session, crypto)
    rotated = CredentialCrypto(Fernet.generate_key())
    assert can_decrypt_refresh_token(account=account, crypto=rotated) is False
