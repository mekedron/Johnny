"""Tests for the calendar sync service (US-007)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.db.models import (
    CalendarEvent,
    GoogleAccount,
    MeetingConfig,
    ProfileTemplate,
)
from app.security.crypto import CredentialCrypto
from app.services.calendar_sync import (
    DEFAULT_WINDOW_DAYS,
    MAX_WINDOW_DAYS,
    CalendarSyncError,
    _attendees_changed,
    _extract_meet_link,
    _parse_event_datetime,
    _parse_event_payload,
    list_account_events,
    sync_account_events,
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


def _make_account(
    session: Session,
    crypto: CredentialCrypto,
    *,
    email: str = "alice@example.com",
    expires_at: datetime | None = None,
) -> GoogleAccount:
    row = GoogleAccount(
        email=email,
        access_token_encrypted=crypto.encrypt("fresh-access"),
        refresh_token_encrypted=crypto.encrypt("refresh-1"),
        token_expires_at=expires_at or (datetime.now(UTC) + timedelta(hours=1)),
    )
    session.add(row)
    session.flush()
    return row


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _make_client(
    *, session: Session, account: GoogleAccount, crypto: CredentialCrypto,
    settings: Settings, handler: Callable[[httpx.Request], httpx.Response],
) -> GoogleApiClient:
    return GoogleApiClient(
        session=session,
        account=account,
        crypto=crypto,
        settings=settings,
        http_client=_mock_client(handler),
    )


def _event_payload(
    *,
    external_id: str = "evt-1",
    summary: str | None = "Standup",
    description: str | None = None,
    start: str = "2026-07-01T10:00:00Z",
    end: str = "2026-07-01T10:30:00Z",
    hangout_link: str | None = "https://meet.google.com/aaa-bbb-ccc",
    organizer_email: str | None = "boss@example.com",
    attendees: list[dict[str, Any]] | None = None,
    status: str | None = None,
    conf_data: dict[str, Any] | None = None,
    recurring_event_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": external_id,
        "etag": '"etag-value"',
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if summary is not None:
        body["summary"] = summary
    if description is not None:
        body["description"] = description
    if hangout_link is not None:
        body["hangoutLink"] = hangout_link
    if organizer_email is not None:
        body["organizer"] = {"email": organizer_email}
    if attendees is not None:
        body["attendees"] = attendees
    if status is not None:
        body["status"] = status
    if conf_data is not None:
        body["conferenceData"] = conf_data
    if recurring_event_id is not None:
        body["recurringEventId"] = recurring_event_id
    return body


# --- parsing --------------------------------------------------------------


def test_parse_event_datetime_handles_datetime_field() -> None:
    out = _parse_event_datetime({"dateTime": "2026-07-01T10:00:00Z"})
    assert out is not None
    assert out.tzinfo is not None
    assert out.year == 2026 and out.hour == 10


def test_parse_event_datetime_handles_date() -> None:
    out = _parse_event_datetime({"date": "2026-07-01"})
    assert out is not None
    assert out.tzinfo is not None
    assert out.year == 2026 and out.month == 7 and out.day == 1


def test_parse_event_datetime_returns_none_for_empty() -> None:
    assert _parse_event_datetime(None) is None
    assert _parse_event_datetime({}) is None


def test_extract_meet_link_prefers_hangout_link() -> None:
    payload = _event_payload(hangout_link="https://meet.google.com/abc-def")
    assert _extract_meet_link(payload) == "https://meet.google.com/abc-def"


def test_extract_meet_link_falls_back_to_conference_data() -> None:
    payload = _event_payload(
        hangout_link=None,
        conf_data={
            "entryPoints": [
                {"entryPointType": "phone", "uri": "tel:+1234"},
                {
                    "entryPointType": "video",
                    "uri": "https://meet.google.com/xyz-123",
                },
            ]
        },
    )
    assert _extract_meet_link(payload) == "https://meet.google.com/xyz-123"


def test_extract_meet_link_returns_none_when_absent() -> None:
    payload = _event_payload(hangout_link=None)
    assert _extract_meet_link(payload) is None


def test_parse_event_payload_extracts_required_fields() -> None:
    parsed = _parse_event_payload(_event_payload())
    assert parsed is not None
    assert parsed.external_id == "evt-1"
    assert parsed.summary == "Standup"
    assert parsed.organizer == "boss@example.com"
    assert parsed.meet_link == "https://meet.google.com/aaa-bbb-ccc"
    assert parsed.etag == '"etag-value"'
    assert parsed.cancelled is False
    # Description defaults to None when the calendar event omits it.
    assert parsed.description is None


def test_parse_event_payload_captures_description() -> None:
    """Johnny-ckz.3: description text rides into the parsed event."""
    parsed = _parse_event_payload(
        _event_payload(description="Q3 launch readiness review.")
    )
    assert parsed is not None
    assert parsed.description == "Q3 launch readiness review."


def test_parse_event_payload_treats_empty_description_as_none() -> None:
    parsed = _parse_event_payload(_event_payload(description=""))
    assert parsed is not None
    assert parsed.description is None


def test_parse_event_payload_captures_recurring_event_id() -> None:
    """Johnny-dsy: recurringEventId rides into the parsed event."""
    parsed = _parse_event_payload(
        _event_payload(recurring_event_id="series-weekly-standup")
    )
    assert parsed is not None
    assert parsed.recurring_event_id == "series-weekly-standup"


def test_parse_event_payload_recurring_id_defaults_to_none() -> None:
    """One-off events with no recurringEventId leave the field None."""
    parsed = _parse_event_payload(_event_payload())
    assert parsed is not None
    assert parsed.recurring_event_id is None


def test_parse_event_payload_empty_recurring_id_treated_as_none() -> None:
    """Google never sends "" but guard against malformed input regardless."""
    parsed = _parse_event_payload(_event_payload(recurring_event_id=""))
    assert parsed is not None
    assert parsed.recurring_event_id is None


def test_parse_event_payload_rejects_missing_id() -> None:
    payload = _event_payload()
    payload.pop("id")
    assert _parse_event_payload(payload) is None


def test_parse_event_payload_marks_cancelled_without_times() -> None:
    payload = _event_payload(status="cancelled")
    # Cancellations from Google sometimes omit start/end.
    payload.pop("start", None)
    payload.pop("end", None)
    parsed = _parse_event_payload(payload)
    assert parsed is not None
    assert parsed.cancelled is True


def test_parse_event_payload_extracts_attendees() -> None:
    parsed = _parse_event_payload(
        _event_payload(
            attendees=[
                {
                    "email": "alice@example.com",
                    "displayName": "Alice",
                    "responseStatus": "accepted",
                    "optional": False,
                    "organizer": False,
                    "self": True,
                },
                {"email": "bob@example.com", "responseStatus": "needsAction"},
            ]
        )
    )
    assert parsed is not None
    assert parsed.attendees is not None
    assert parsed.attendees[0]["email"] == "alice@example.com"
    assert parsed.attendees[0]["self"] is True
    assert parsed.attendees[1]["optional"] is False


def test_attendees_changed_ignores_order() -> None:
    a = [{"email": "a@x"}, {"email": "b@x"}]
    b = [{"email": "b@x"}, {"email": "a@x"}]
    assert _attendees_changed(a, b) is False


def test_attendees_changed_detects_response_change() -> None:
    a = [{"email": "a@x", "response_status": "needsAction"}]
    b = [{"email": "a@x", "response_status": "accepted"}]
    assert _attendees_changed(a, b) is True


# --- sync_account_events --------------------------------------------------


async def test_sync_inserts_new_rows(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    account = _make_account(session, crypto)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "items": [
                    _event_payload(external_id="evt-1", summary="Standup"),
                    _event_payload(
                        external_id="evt-2",
                        summary="Client call",
                        start="2026-07-02T15:00:00Z",
                        end="2026-07-02T15:30:00Z",
                    ),
                ]
            },
        )

    client = _make_client(
        session=session, account=account, crypto=crypto,
        settings=settings, handler=handler,
    )
    result = await sync_account_events(session=session, client=client)
    await client.aclose()

    assert result.account_id == account.id
    assert result.created_count == 2
    assert result.updated_count == 0

    rows = session.scalars(sa.select(CalendarEvent).order_by(CalendarEvent.external_id)).all()
    assert [r.external_id for r in rows] == ["evt-1", "evt-2"]
    assert rows[0].summary == "Standup"
    assert rows[0].meet_link == "https://meet.google.com/aaa-bbb-ccc"
    assert rows[0].last_synced_at is not None

    parsed_q = parse_qs(urlparse(str(requests[0].url)).query)
    assert "timeMin" in parsed_q and "timeMax" in parsed_q
    assert parsed_q["singleEvents"] == ["true"]
    assert parsed_q["orderBy"] == ["startTime"]


async def test_sync_updates_changed_rows(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    account = _make_account(session, crypto)
    initial = CalendarEvent(
        account_id=account.id,
        external_id="evt-1",
        summary="Old summary",
        organizer="someone@example.com",
        attendees=[{"email": "a@x"}],
        start_time=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        end_time=datetime(2026, 7, 1, 10, 30, tzinfo=UTC),
        meet_link="https://meet.google.com/old",
        last_synced_at=datetime(2026, 6, 30, tzinfo=UTC),
    )
    session.add(initial)
    session.flush()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    _event_payload(
                        external_id="evt-1",
                        summary="New summary",
                        start="2026-07-01T11:00:00Z",  # rescheduled
                        end="2026-07-01T11:30:00Z",
                    )
                ]
            },
        )

    client = _make_client(
        session=session, account=account, crypto=crypto,
        settings=settings, handler=handler,
    )
    result = await sync_account_events(session=session, client=client)
    await client.aclose()

    assert result.updated_count == 1
    assert result.created_count == 0
    # The sync mutates the in-memory instance and relies on the caller's
    # commit to persist. ``session.refresh()`` would discard pending
    # changes, so flush first and then inspect the live instance.
    session.flush()
    assert initial.summary == "New summary"
    rescheduled = initial.start_time
    if rescheduled.tzinfo is None:
        rescheduled = rescheduled.replace(tzinfo=UTC)
    assert rescheduled.hour == 11


async def test_sync_unchanged_row_reports_no_update(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    account = _make_account(session, crypto)
    initial = CalendarEvent(
        account_id=account.id,
        external_id="evt-1",
        summary="Standup",
        organizer="boss@example.com",
        attendees=None,
        start_time=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        end_time=datetime(2026, 7, 1, 10, 30, tzinfo=UTC),
        meet_link="https://meet.google.com/aaa-bbb-ccc",
        last_synced_at=None,
    )
    session.add(initial)
    session.flush()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [_event_payload()]})

    client = _make_client(
        session=session, account=account, crypto=crypto,
        settings=settings, handler=handler,
    )
    result = await sync_account_events(session=session, client=client)
    await client.aclose()

    assert result.updated_count == 0
    assert result.created_count == 0
    assert all(c.kind == "unchanged" for c in result.changes)


async def test_sync_persists_event_description(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    """Johnny-ckz.3: a new event's description column is populated from Google."""
    account = _make_account(session, crypto)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    _event_payload(
                        external_id="evt-d",
                        description="Q3 launch readiness review.",
                    )
                ]
            },
        )

    client = _make_client(
        session=session, account=account, crypto=crypto,
        settings=settings, handler=handler,
    )
    await sync_account_events(session=session, client=client)
    await client.aclose()

    row = session.scalar(
        sa.select(CalendarEvent).where(CalendarEvent.external_id == "evt-d")
    )
    assert row is not None
    assert row.description == "Q3 launch readiness review."


async def test_sync_persists_recurring_event_id(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    """Johnny-dsy: recurringEventId on inserted rows is captured for series lookup."""
    account = _make_account(session, crypto)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    _event_payload(
                        external_id="evt-r-1",
                        recurring_event_id="series-weekly-x",
                    ),
                    _event_payload(
                        external_id="evt-r-2",
                        start="2026-07-08T10:00:00Z",
                        end="2026-07-08T10:30:00Z",
                        recurring_event_id="series-weekly-x",
                    ),
                    # A one-off event in the same payload must NOT inherit
                    # the recurring id from its siblings.
                    _event_payload(
                        external_id="evt-r-3",
                        start="2026-07-15T09:00:00Z",
                        end="2026-07-15T09:30:00Z",
                    ),
                ]
            },
        )

    client = _make_client(
        session=session, account=account, crypto=crypto,
        settings=settings, handler=handler,
    )
    await sync_account_events(session=session, client=client)
    await client.aclose()

    rows = session.scalars(
        sa.select(CalendarEvent).order_by(CalendarEvent.external_id)
    ).all()
    by_id = {r.external_id: r.recurring_event_id for r in rows}
    assert by_id["evt-r-1"] == "series-weekly-x"
    assert by_id["evt-r-2"] == "series-weekly-x"
    assert by_id["evt-r-3"] is None


async def test_sync_updates_changed_description(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    """A change in the description text counts as an update + invalidates etag."""
    account = _make_account(session, crypto)
    initial = CalendarEvent(
        account_id=account.id,
        external_id="evt-d",
        summary="Standup",
        description="old description",
        start_time=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        end_time=datetime(2026, 7, 1, 10, 30, tzinfo=UTC),
        meet_link="https://meet.google.com/aaa-bbb-ccc",
    )
    session.add(initial)
    session.flush()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    _event_payload(
                        external_id="evt-d",
                        description="new description with agenda",
                    )
                ]
            },
        )

    client = _make_client(
        session=session, account=account, crypto=crypto,
        settings=settings, handler=handler,
    )
    result = await sync_account_events(session=session, client=client)
    await client.aclose()

    assert result.updated_count == 1
    session.flush()
    assert initial.description == "new description with agenda"


async def test_sync_deletes_cancelled_events(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    account = _make_account(session, crypto)
    existing = CalendarEvent(
        account_id=account.id,
        external_id="evt-1",
        summary="Standup",
        start_time=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        end_time=datetime(2026, 7, 1, 10, 30, tzinfo=UTC),
    )
    session.add(existing)
    session.flush()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "evt-1",
                        "status": "cancelled",
                    }
                ]
            },
        )

    client = _make_client(
        session=session, account=account, crypto=crypto,
        settings=settings, handler=handler,
    )
    result = await sync_account_events(session=session, client=client)
    await client.aclose()

    assert result.deleted_count == 1
    assert session.scalar(
        sa.select(sa.func.count()).select_from(CalendarEvent)
    ) == 0


async def test_sync_follows_pagination(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    account = _make_account(session, crypto)
    pages = [
        httpx.Response(
            200,
            json={
                "items": [_event_payload(external_id=f"evt-{i}") for i in range(3)],
                "nextPageToken": "tok-1",
            },
        ),
        httpx.Response(
            200,
            json={
                "items": [_event_payload(external_id=f"evt-{i}") for i in range(3, 5)],
            },
        ),
    ]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = parse_qs(urlparse(str(request.url)).query)
        calls.append(q.get("pageToken", [""])[0])
        return pages.pop(0)

    client = _make_client(
        session=session, account=account, crypto=crypto,
        settings=settings, handler=handler,
    )
    result = await sync_account_events(session=session, client=client)
    await client.aclose()

    assert result.created_count == 5
    assert calls == ["", "tok-1"]


async def test_sync_clamps_window_days(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    account = _make_account(session, crypto)
    seen_params: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.append(parse_qs(urlparse(str(request.url)).query))
        return httpx.Response(200, json={"items": []})

    client = _make_client(
        session=session, account=account, crypto=crypto,
        settings=settings, handler=handler,
    )
    base = datetime(2026, 7, 1, tzinfo=UTC)
    await sync_account_events(
        session=session, client=client, window_days=999, now=base
    )
    await client.aclose()

    # The timeMax should be base + MAX_WINDOW_DAYS, not base + 999.
    time_max = seen_params[0]["timeMax"][0]
    parsed_max = datetime.fromisoformat(time_max.replace("Z", "+00:00"))
    assert (parsed_max - base).days == MAX_WINDOW_DAYS


async def test_sync_raises_on_non_success(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    account = _make_account(session, crypto)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _make_client(
        session=session, account=account, crypto=crypto,
        settings=settings, handler=handler,
    )
    with pytest.raises(CalendarSyncError):
        await sync_account_events(session=session, client=client)
    await client.aclose()


async def test_sync_skips_event_missing_id(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    account = _make_account(session, crypto)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {"summary": "no id", "start": {"dateTime": "2026-07-01T10:00:00Z"}}
                ]
            },
        )

    client = _make_client(
        session=session, account=account, crypto=crypto,
        settings=settings, handler=handler,
    )
    result = await sync_account_events(session=session, client=client)
    await client.aclose()
    assert result.created_count == 0
    assert result.updated_count == 0


# --- sync_account_events: attachment resolution (Johnny-4da) --------------


async def test_sync_resolves_drive_links_in_description(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    """Description with a Docs URL → attachments_text + etags populated on the row."""
    account = _make_account(session, crypto)

    def handler(request: httpx.Request) -> httpx.Response:
        # Calendar list response.
        if request.url.host == "www.googleapis.com" and request.url.path.startswith(
            "/calendar/"
        ):
            return httpx.Response(
                200,
                json={
                    "items": [
                        _event_payload(
                            external_id="evt-1",
                            summary="Quarterly review",
                            description=(
                                "Read the plan: "
                                "https://docs.google.com/document/d/docABCDEF12/edit"
                            ),
                        )
                    ]
                },
            )
        # Drive metadata + export.
        if request.url.path == "/drive/v3/files/docABCDEF12":
            return httpx.Response(
                200,
                json={
                    "id": "docABCDEF12",
                    "name": "Quarterly Plan",
                    "mimeType": "application/vnd.google-apps.document",
                    "modifiedTime": "2026-06-01T10:00:00.000Z",
                },
            )
        if request.url.path == "/drive/v3/files/docABCDEF12/export":
            return httpx.Response(200, text="Plan: ship Johnny-4da.")
        return httpx.Response(404, text=f"unexpected: {request.url}")

    client = _make_client(
        session=session, account=account, crypto=crypto,
        settings=settings, handler=handler,
    )
    result = await sync_account_events(session=session, client=client)
    await client.aclose()
    assert result.created_count == 1
    row = session.scalars(sa.select(CalendarEvent)).one()
    assert row.attachments_text is not None
    assert "Plan: ship Johnny-4da." in row.attachments_text
    assert row.attachments_etags == {"docABCDEF12": "2026-06-01T10:00:00.000Z"}


async def test_sync_skips_body_fetch_on_second_pass_with_matching_etags(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    """Second sync pass with unchanged doc → no body fetch.

    Acceptance: "Fetched content cached + invalidated on Drive etag change."
    """
    account = _make_account(session, crypto)
    body_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal body_calls
        if request.url.host == "www.googleapis.com" and request.url.path.startswith(
            "/calendar/"
        ):
            return httpx.Response(
                200,
                json={
                    "items": [
                        _event_payload(
                            external_id="evt-1",
                            summary="Quarterly review",
                            description=(
                                "Read the plan: "
                                "https://docs.google.com/document/d/docABCDEF12/edit"
                            ),
                        )
                    ]
                },
            )
        if request.url.path == "/drive/v3/files/docABCDEF12":
            return httpx.Response(
                200,
                json={
                    "id": "docABCDEF12",
                    "name": "Quarterly Plan",
                    "mimeType": "application/vnd.google-apps.document",
                    "modifiedTime": "2026-06-01T10:00:00.000Z",
                },
            )
        if request.url.path == "/drive/v3/files/docABCDEF12/export":
            body_calls += 1
            return httpx.Response(200, text="Plan: ship Johnny-4da.")
        return httpx.Response(404, text=f"unexpected: {request.url}")

    client = _make_client(
        session=session, account=account, crypto=crypto,
        settings=settings, handler=handler,
    )
    # First pass: body fetched, cached.
    await sync_account_events(session=session, client=client)
    assert body_calls == 1
    # Second pass: unchanged modifiedTime → no body fetch (cache reuse).
    await sync_account_events(session=session, client=client)
    assert body_calls == 1
    await client.aclose()


async def test_sync_clears_attachments_when_description_drops_urls(
    session: Session, crypto: CredentialCrypto, settings: Settings
) -> None:
    """Host removes the URL from the description → cached body cleared."""
    account = _make_account(session, crypto)
    initial = CalendarEvent(
        account_id=account.id,
        external_id="evt-1",
        summary="Quarterly review",
        description="(initial description with link — overwritten by sync)",
        start_time=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        end_time=datetime(2026, 7, 1, 10, 30, tzinfo=UTC),
        attachments_text="stale cached body",
        attachments_etags={"docABCDEF12": "2026-06-01T10:00:00.000Z"},
    )
    session.add(initial)
    session.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        # New event payload — description no longer has any Drive URL.
        return httpx.Response(
            200,
            json={
                "items": [
                    _event_payload(
                        external_id="evt-1",
                        summary="Quarterly review",
                        description="Just talk through the goals.",
                    )
                ]
            },
        )

    client = _make_client(
        session=session, account=account, crypto=crypto,
        settings=settings, handler=handler,
    )
    await sync_account_events(session=session, client=client)
    await client.aclose()
    session.flush()
    assert initial.attachments_text is None
    assert initial.attachments_etags is None


# --- list_account_events --------------------------------------------------


def test_list_account_events_filters_by_window(
    session: Session, crypto: CredentialCrypto
) -> None:
    account = _make_account(session, crypto)
    now = datetime(2026, 7, 1, tzinfo=UTC)
    in_window = CalendarEvent(
        account_id=account.id,
        external_id="in-window",
        start_time=now + timedelta(days=1),
        end_time=now + timedelta(days=1, hours=1),
    )
    out_of_window = CalendarEvent(
        account_id=account.id,
        external_id="out-of-window",
        start_time=now + timedelta(days=100),
        end_time=now + timedelta(days=100, hours=1),
    )
    past = CalendarEvent(
        account_id=account.id,
        external_id="past",
        start_time=now - timedelta(days=10),
        end_time=now - timedelta(days=10) + timedelta(hours=1),
    )
    session.add_all([in_window, out_of_window, past])
    session.flush()

    rows = list_account_events(
        session, account_id=account.id, window_days=DEFAULT_WINDOW_DAYS, now=now
    )
    assert [r.external_id for r in rows] == ["in-window"]
