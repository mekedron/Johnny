"""Integration tests for the GET /calendar/events endpoint (US-007)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
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
from app.services.google_client import GoogleApiClient


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
    sess = Session(engine)
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture
def crypto() -> CredentialCrypto:
    return CredentialCrypto(Fernet.generate_key())


@pytest.fixture
def settings_override() -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        redis_url="redis://localhost:6379/0",
        fernet_key=Fernet.generate_key().decode("ascii"),
        google_client_id="cid",
        google_client_secret="cs",
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
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _make_account(
    session: Session, crypto: CredentialCrypto, *, email: str = "alice@example.com"
) -> GoogleAccount:
    row = GoogleAccount(
        email=email,
        access_token_encrypted=crypto.encrypt("fresh-access"),
        refresh_token_encrypted=crypto.encrypt("refresh-1"),
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session.add(row)
    session.flush()
    return row


def _stub_calendar_response(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Monkeypatch :meth:`GoogleApiClient._client` to inject a mock transport.

    The endpoint instantiates ``GoogleApiClient`` directly; the simplest
    seam is to override the private client factory so every call to
    ``client.request`` is intercepted by our handler.
    """

    real_init = GoogleApiClient.__init__

    def patched_init(self: GoogleApiClient, *args: Any, **kwargs: Any) -> None:
        kwargs["http_client"] = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(GoogleApiClient, "__init__", patched_init)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _near_future(days_from_now: float = 3.0) -> tuple[str, str]:
    """Return ``(start_iso, end_iso)`` for an event inside the 14-day window.

    Centralised so tests don't need to keep recomputing the ``now``
    arithmetic — the default 14-day window applies to both the sync
    and the post-sync list query, so fixture dates must sit inside it.
    """
    base = datetime.now(UTC) + timedelta(days=days_from_now)
    return _iso(base), _iso(base + timedelta(minutes=30))


def _event_payload(
    *,
    external_id: str,
    summary: str = "Standup",
    start: str | None = None,
    end: str | None = None,
    hangout_link: str | None = "https://meet.google.com/aaa",
) -> dict[str, Any]:
    if start is None or end is None:
        s, e = _near_future()
        start = start or s
        end = end or e
    body: dict[str, Any] = {
        "id": external_id,
        "etag": '"etag"',
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        "organizer": {"email": "boss@example.com"},
    }
    if hangout_link is not None:
        body["hangoutLink"] = hangout_link
    return body


# --- happy path -----------------------------------------------------------


def test_list_events_returns_synced_rows(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _make_account(db_session, crypto)
    start_b, end_b = _near_future(days_from_now=5)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    _event_payload(external_id="evt-a"),
                    _event_payload(
                        external_id="evt-b",
                        start=start_b,
                        end=end_b,
                        hangout_link=None,
                    ),
                ]
            },
        )

    _stub_calendar_response(monkeypatch, handler)

    resp = client.get(f"/calendar/events?account_id={account.id}&window_days=14")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["account_id"] == account.id
    assert body["window_days"] == 14
    assert body["created_count"] == 2
    events = body["events"]
    assert {e["external_id"] for e in events} == {"evt-a", "evt-b"}
    a = next(e for e in events if e["external_id"] == "evt-a")
    assert a["has_meet_link"] is True
    assert a["meet_link"] == "https://meet.google.com/aaa"
    assert a["has_meeting_config"] is False
    b = next(e for e in events if e["external_id"] == "evt-b")
    assert b["has_meet_link"] is False


def test_list_events_includes_has_meeting_config_flag(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An event with a meeting_config attached must surface the flag."""
    account = _make_account(db_session, crypto)
    # Pre-seed an event so the meeting_config has a parent row.
    far_future_start = datetime.now(UTC) + timedelta(days=3)
    evt = CalendarEvent(
        account_id=account.id,
        external_id="evt-cfg",
        summary="Configured meeting",
        start_time=far_future_start,
        end_time=far_future_start + timedelta(minutes=30),
        meet_link="https://meet.google.com/cfg",
    )
    db_session.add(evt)
    db_session.flush()
    cfg = MeetingConfig(
        calendar_event_id=evt.id,
        identity_account_id=account.id,
        enabled=True,
    )
    db_session.add(cfg)
    db_session.flush()

    def handler(_: httpx.Request) -> httpx.Response:
        # Return the same event so the sync upserts (unchanged) and the
        # row remains.
        iso = far_future_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        iso_end = (far_future_start + timedelta(minutes=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        return httpx.Response(
            200,
            json={
                "items": [
                    _event_payload(
                        external_id="evt-cfg",
                        summary="Configured meeting",
                        start=iso,
                        end=iso_end,
                        hangout_link="https://meet.google.com/cfg",
                    )
                ]
            },
        )

    _stub_calendar_response(monkeypatch, handler)

    resp = client.get(f"/calendar/events?account_id={account.id}")
    assert resp.status_code == 200, resp.text
    events = resp.json()["events"]
    target = next(e for e in events if e["external_id"] == "evt-cfg")
    assert target["has_meeting_config"] is True
    assert target["has_meet_link"] is True


def test_list_events_rejects_unknown_account(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp = client.get("/calendar/events?account_id=9999")
    assert resp.status_code == 404


def test_list_events_rejects_window_over_max(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
) -> None:
    account = _make_account(db_session, crypto)
    resp = client.get(
        f"/calendar/events?account_id={account.id}&window_days=9999"
    )
    assert resp.status_code == 422


def test_list_events_returns_502_on_google_error(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _make_account(db_session, crypto)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _stub_calendar_response(monkeypatch, handler)
    resp = client.get(f"/calendar/events?account_id={account.id}")
    assert resp.status_code == 502
    assert "calendar fetch failed" in resp.json()["detail"]


def test_list_events_default_window_used_when_omitted(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _make_account(db_session, crypto)
    seen_params: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.append(parse_qs(urlparse(str(request.url)).query))
        return httpx.Response(200, json={"items": []})

    _stub_calendar_response(monkeypatch, handler)
    resp = client.get(f"/calendar/events?account_id={account.id}")
    assert resp.status_code == 200
    assert seen_params
    # The maxResults parameter is always set by the sync helper.
    assert "maxResults" in seen_params[0]


def test_list_events_returns_409_when_refresh_token_undecryptable(
    db_session: Session,
    crypto: CredentialCrypto,
    settings_override: Settings,
) -> None:
    """Stored row from a previous FERNET_KEY surfaces as 409 + structured detail.

    The frontend keys off ``code == 'account_needs_reauth'`` to render the
    Reconnect affordance instead of a generic error banner.
    """
    # Use a *different* crypto to encrypt the account row (simulating a
    # prior FERNET_KEY) than the one injected into the API dependency
    # (the rotated current key).
    legacy_crypto = CredentialCrypto(Fernet.generate_key())
    account = _make_account(
        db_session, legacy_crypto, email="legacy@example.com"
    )
    # Force the access token expired so the client must reach for the
    # (undecryptable) refresh token.
    account.token_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.flush()

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
    try:
        with TestClient(app) as test_client:
            resp = test_client.get(
                f"/calendar/events?account_id={account.id}"
            )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 409, resp.text
    body = resp.json()
    detail = body["detail"]
    assert detail["code"] == "account_needs_reauth"
    assert detail["account_id"] == account.id
    assert detail["email"] == "legacy@example.com"
    assert "Reconnect" in detail["message"]
