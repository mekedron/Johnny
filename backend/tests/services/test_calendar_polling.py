"""Tests for the calendar polling worker (US-007)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.db.models import (
    AccountRole,
    BotMode,
    CalendarEvent,
    GoogleAccount,
    MeetingConfig,
    ProfileTemplate,
)
from app.security.crypto import CredentialCrypto
from app.services.calendar_polling import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    ChangePublisher,
    PollingResult,
    accounts_with_meeting_configs,
    filter_distinct_accounts,
    get_poll_interval_seconds,
    poll_meeting_config_calendars,
)
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
            ProfileTemplate.__table__,  # type: ignore[list-item]
            MeetingConfig.__table__,  # type: ignore[list-item]
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


# --- Fake publisher -------------------------------------------------------


class _FakePublisher(ChangePublisher):
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []
        self.closed = False

    async def publish(self, payload: dict[str, Any]) -> None:
        self.published.append(payload)

    async def close(self) -> None:
        self.closed = True


# --- Setup helpers --------------------------------------------------------


def _make_account_with_config(
    session: Session,
    crypto: CredentialCrypto,
    *,
    email: str = "alice@example.com",
    external_id: str = "evt-1",
    start: datetime | None = None,
) -> tuple[GoogleAccount, CalendarEvent, MeetingConfig]:
    account = GoogleAccount(
        email=email,
        role=AccountRole.USER,
        access_token_encrypted=crypto.encrypt("access"),
        refresh_token_encrypted=crypto.encrypt("refresh"),
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session.add(account)
    session.flush()
    template = ProfileTemplate(
        name=f"tpl-{email}", mode=BotMode.LISTEN_ONLY, allowed_replies=[]
    )
    session.add(template)
    session.flush()
    # Strip microseconds so the second-precision ISO roundtrip in the
    # mock handler doesn't make a "no-op" sync look like an update.
    s = (start or datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)
    event = CalendarEvent(
        account_id=account.id,
        external_id=external_id,
        summary="Standup",
        start_time=s,
        end_time=s + timedelta(minutes=30),
        meet_link="https://meet.google.com/abc",
    )
    session.add(event)
    session.flush()
    cfg = MeetingConfig(
        calendar_event_id=event.id,
        profile_template_id=template.id,
        identity_account_id=account.id,
        mode=BotMode.LISTEN_ONLY,
        enabled=True,
    )
    session.add(cfg)
    session.flush()
    return account, event, cfg


def _stub_google_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Inject a MockTransport into every GoogleApiClient constructed.

    The polling pass creates one client per account on the fly; patching
    ``__init__`` is the simplest seam.
    """
    real_init = GoogleApiClient.__init__

    def patched_init(self: GoogleApiClient, *args: Any, **kwargs: Any) -> None:
        kwargs["http_client"] = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(GoogleApiClient, "__init__", patched_init)


# --- get_poll_interval_seconds --------------------------------------------


def test_get_poll_interval_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOHNNY_CALENDAR_POLL_INTERVAL_SECONDS", raising=False)
    assert get_poll_interval_seconds() == DEFAULT_POLL_INTERVAL_SECONDS


def test_get_poll_interval_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOHNNY_CALENDAR_POLL_INTERVAL_SECONDS", "60")
    assert get_poll_interval_seconds() == 60


def test_get_poll_interval_invalid_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOHNNY_CALENDAR_POLL_INTERVAL_SECONDS", "not-a-number")
    assert get_poll_interval_seconds() == DEFAULT_POLL_INTERVAL_SECONDS


def test_get_poll_interval_clamps_to_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOHNNY_CALENDAR_POLL_INTERVAL_SECONDS", "0")
    assert get_poll_interval_seconds() == 1


def test_default_poll_interval_is_five_minutes() -> None:
    assert DEFAULT_POLL_INTERVAL_SECONDS == 5 * 60


# --- accounts_with_meeting_configs ----------------------------------------


def test_returns_only_accounts_with_configs(
    session: Session, crypto: CredentialCrypto
) -> None:
    a1, _, _ = _make_account_with_config(session, crypto, email="a@x")
    # Account with no configs at all.
    a2 = GoogleAccount(
        email="b@x",
        role=AccountRole.USER,
        access_token_encrypted=crypto.encrypt("a"),
        refresh_token_encrypted=crypto.encrypt("r"),
    )
    session.add(a2)
    session.flush()
    out = accounts_with_meeting_configs(session)
    ids = [acc.id for acc in out]
    assert a1.id in ids
    assert a2.id not in ids


def test_distinct_when_account_has_multiple_configs(
    session: Session, crypto: CredentialCrypto
) -> None:
    account, _, _ = _make_account_with_config(session, crypto)
    # Add a second meeting config under the same account.
    _, _, _ = _make_account_with_config(
        session, crypto, email=account.email + ".x", external_id="evt-2"
    )
    # Force the same account to own another config too.
    template = ProfileTemplate(
        name="another-tpl", mode=BotMode.LISTEN_ONLY, allowed_replies=[]
    )
    session.add(template)
    session.flush()
    s2 = datetime.now(UTC) + timedelta(days=2)
    evt2 = CalendarEvent(
        account_id=account.id,
        external_id="evt-other",
        summary="x",
        start_time=s2,
        end_time=s2 + timedelta(minutes=30),
    )
    session.add(evt2)
    session.flush()
    cfg2 = MeetingConfig(
        calendar_event_id=evt2.id,
        profile_template_id=template.id,
        identity_account_id=account.id,
        mode=BotMode.LISTEN_ONLY,
    )
    session.add(cfg2)
    session.flush()
    out = accounts_with_meeting_configs(session)
    ids = [acc.id for acc in out]
    assert ids.count(account.id) == 1


def test_filter_distinct_accounts_preserves_order(
    session: Session, crypto: CredentialCrypto
) -> None:
    a, _, _ = _make_account_with_config(session, crypto, email="a@x")
    b, _, _ = _make_account_with_config(
        session, crypto, email="b@x", external_id="evt-b"
    )
    out = filter_distinct_accounts([a, b, a])
    assert [x.id for x in out] == [a.id, b.id]


# --- poll_meeting_config_calendars ----------------------------------------


async def test_poll_publishes_change_on_rescheduled_event(
    session: Session,
    crypto: CredentialCrypto,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account, event, _ = _make_account_with_config(session, crypto)
    new_start = (event.start_time + timedelta(hours=2)).astimezone(UTC)

    def handler(_: httpx.Request) -> httpx.Response:
        iso_start = new_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        iso_end = (new_start + timedelta(minutes=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": event.external_id,
                        "etag": '"new-etag"',
                        "summary": event.summary,
                        "organizer": {"email": "boss@example.com"},
                        "start": {"dateTime": iso_start},
                        "end": {"dateTime": iso_end},
                        "hangoutLink": event.meet_link,
                    }
                ]
            },
        )

    _stub_google_client(monkeypatch, handler)

    pub = _FakePublisher()
    result = await poll_meeting_config_calendars(
        session=session, crypto=crypto, settings=settings, publisher=pub
    )

    assert result.polled_account_count == 1
    assert result.updated_count == 1
    assert result.error_count == 0
    assert len(pub.published) == 1
    payload = pub.published[0]
    assert payload["type"] == "calendar_event_changed"
    assert payload["kind"] == "updated"
    assert payload["account_id"] == account.id
    assert payload["external_id"] == event.external_id


async def test_poll_does_not_publish_when_unchanged(
    session: Session,
    crypto: CredentialCrypto,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, event, _ = _make_account_with_config(session, crypto)
    start_iso = event.start_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = event.end_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": event.external_id,
                        "etag": '"etag"',
                        "summary": event.summary,
                        "start": {"dateTime": start_iso},
                        "end": {"dateTime": end_iso},
                        "hangoutLink": event.meet_link,
                    }
                ]
            },
        )

    _stub_google_client(monkeypatch, handler)
    pub = _FakePublisher()
    result = await poll_meeting_config_calendars(
        session=session, crypto=crypto, settings=settings, publisher=pub
    )
    assert result.updated_count == 0
    assert pub.published == []


async def test_poll_publishes_creation_for_new_event(
    session: Session,
    crypto: CredentialCrypto,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account, _, _ = _make_account_with_config(session, crypto)

    def handler(_: httpx.Request) -> httpx.Response:
        iso = (datetime.now(UTC) + timedelta(days=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        iso_end = (datetime.now(UTC) + timedelta(days=3, minutes=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        # Existing event remains the same so it stays "unchanged" — but
        # a fresh event surfaces, generating a "created" publish.
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "evt-new",
                        "etag": '"new"',
                        "summary": "Brand new meeting",
                        "start": {"dateTime": iso},
                        "end": {"dateTime": iso_end},
                        "hangoutLink": "https://meet.google.com/new",
                    }
                ]
            },
        )

    _stub_google_client(monkeypatch, handler)
    pub = _FakePublisher()
    result = await poll_meeting_config_calendars(
        session=session, crypto=crypto, settings=settings, publisher=pub
    )
    # The existing event was removed from the response so it does not
    # generate a deletion (cancellations need status=cancelled), but the
    # new one shows up.
    assert result.created_count == 1
    assert any(p["kind"] == "created" for p in pub.published)
    creation = next(p for p in pub.published if p["kind"] == "created")
    assert creation["account_id"] == account.id
    assert creation["external_id"] == "evt-new"


async def test_poll_continues_after_single_account_failure(
    session: Session,
    crypto: CredentialCrypto,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a1, _, _ = _make_account_with_config(session, crypto, email="a@x")
    a2, evt2, _ = _make_account_with_config(
        session, crypto, email="b@x", external_id="evt-b"
    )
    new_start = (evt2.start_time + timedelta(hours=1)).astimezone(UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        # Distinguish the two accounts by call order: both share the
        # same access token "access" in this test setup, so we rely on
        # the polling pass visiting accounts in id order — the first
        # call belongs to a1, the second to a2.
        if not handler.first_handled:  # type: ignore[attr-defined]
            handler.first_handled = True  # type: ignore[attr-defined]
            return httpx.Response(500, text="boom")
        iso_start = new_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        iso_end = (new_start + timedelta(minutes=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": evt2.external_id,
                        "etag": '"e"',
                        "summary": evt2.summary,
                        "start": {"dateTime": iso_start},
                        "end": {"dateTime": iso_end},
                        "hangoutLink": evt2.meet_link,
                    }
                ]
            },
        )

    handler.first_handled = False  # type: ignore[attr-defined]
    _stub_google_client(monkeypatch, handler)
    pub = _FakePublisher()
    result = await poll_meeting_config_calendars(
        session=session, crypto=crypto, settings=settings, publisher=pub
    )
    assert result.polled_account_count == 2
    assert result.error_count == 1
    # Updated count must come from the successful account.
    assert result.updated_count == 1


async def test_polling_result_is_zero_when_no_accounts(
    session: Session,
    crypto: CredentialCrypto,
    settings: Settings,
) -> None:
    pub = _FakePublisher()
    result = await poll_meeting_config_calendars(
        session=session, crypto=crypto, settings=settings, publisher=pub
    )
    assert isinstance(result, PollingResult)
    assert result.polled_account_count == 0
    assert result.created_count == 0
    assert pub.published == []


async def test_poll_skips_account_with_undecryptable_token_without_counting_error(
    session: Session,
    crypto: CredentialCrypto,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FERNET_KEY rotation must not stall the loop — skip silently.

    The polling pass is fire-and-forget; an account whose tokens are
    unrecoverable should be skipped (the user reconnects via the UI)
    rather than being counted as a transient error that gets retried
    every poll forever.
    """
    # Encrypt the account row with a *different* crypto so the polling
    # session's crypto cannot decrypt it. The legacy crypto isn't used
    # again — the row is left dangling, exactly as it would be after a
    # FERNET_KEY rotation.
    legacy = CredentialCrypto(Fernet.generate_key())
    _make_account_with_config(session, legacy, email="stale@example.com")

    # No HTTP handler — we should never reach the wire.
    def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("HTTP call must not happen for undecryptable account")

    _stub_google_client(monkeypatch, handler)
    pub = _FakePublisher()
    result = await poll_meeting_config_calendars(
        session=session, crypto=crypto, settings=settings, publisher=pub
    )
    assert result.polled_account_count == 1
    assert result.error_count == 0
    assert result.created_count == 0
    assert result.updated_count == 0
    assert pub.published == []
