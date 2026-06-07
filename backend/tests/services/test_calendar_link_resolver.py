"""Tests for the calendar link resolver (Johnny-4da).

The resolver lives in :mod:`app.services.calendar_link_resolver`. These
tests pin the URL detection, etag-based cache reuse, mime-type
dispatching, and graceful 403/5xx degradation. They use
:class:`httpx.MockTransport` to fake Drive / Sheets API responses so
the tests run in any environment without hitting the live Google APIs.
"""

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
from app.db.models import GoogleAccount
from app.security.crypto import CredentialCrypto
from app.services.calendar_link_resolver import (
    DOCS_MIME,
    MAX_ATTACHMENT_CHARS_TOTAL,
    SHEETS_MIME,
    ResolutionOutcome,
    extract_drive_links,
    has_drive_links,
    resolve_event_attachments,
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


def _make_account(session: Session, crypto: CredentialCrypto) -> GoogleAccount:
    account = GoogleAccount(
        email="user@example.com",
        refresh_token_encrypted=crypto.encrypt("rt"),
        access_token_encrypted=crypto.encrypt("at"),
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session.add(account)
    session.flush()
    return account


def _make_client(
    session: Session,
    account: GoogleAccount,
    crypto: CredentialCrypto,
    settings: Settings,
    handler: Callable[[httpx.Request], httpx.Response],
) -> GoogleApiClient:
    transport = httpx.MockTransport(handler)
    return GoogleApiClient(
        session=session,
        account=account,
        crypto=crypto,
        settings=settings,
        http_client=httpx.AsyncClient(transport=transport),
    )


# --- URL detection ---------------------------------------------------------


def test_extract_drive_links_finds_docs_and_sheets() -> None:
    description = (
        "Agenda for the meeting:\n"
        "- Doc: https://docs.google.com/document/d/doc123ABC/edit\n"
        "- Sheet: https://docs.google.com/spreadsheets/d/sheet456DEF/view?gid=0\n"
        "- File: https://drive.google.com/file/d/file789GHI/preview\n"
    )
    links = extract_drive_links(description)
    assert [link.file_id for link in links] == [
        "doc123ABC",
        "sheet456DEF",
        "file789GHI",
    ]


def test_extract_drive_links_dedupes_repeated_file_ids() -> None:
    description = (
        "Same doc twice: "
        "https://docs.google.com/document/d/dupe999XYZ/edit and "
        "https://docs.google.com/document/d/dupe999XYZ/preview"
    )
    links = extract_drive_links(description)
    assert [link.file_id for link in links] == ["dupe999XYZ"]


def test_extract_drive_links_empty_inputs() -> None:
    assert extract_drive_links(None) == []
    assert extract_drive_links("") == []
    assert extract_drive_links("just plain text, no links here") == []


def test_has_drive_links_cheap_check() -> None:
    assert not has_drive_links(None)
    assert not has_drive_links("")
    assert not has_drive_links("see the email I sent earlier")
    assert has_drive_links("read https://docs.google.com/document/d/abcdefgh1234")
    assert has_drive_links(
        "https://docs.google.com/spreadsheets/d/sheet_id_12345"
    )


# --- Resolver: basic Docs export ------------------------------------------


@pytest.mark.asyncio
async def test_resolve_docs_link_appends_body(
    session: Session,
    crypto: CredentialCrypto,
    settings: Settings,
) -> None:
    """A Docs URL is exported to text/plain and surfaces in the outcome."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/drive/v3/files/docABCDEF12":
            assert request.method == "GET"
            return httpx.Response(
                200,
                json={
                    "id": "docABCDEF12",
                    "name": "Quarterly Plan",
                    "mimeType": DOCS_MIME,
                    "modifiedTime": "2026-06-01T10:00:00.000Z",
                },
            )
        if request.url.path == "/drive/v3/files/docABCDEF12/export":
            return httpx.Response(
                200,
                text="Objective: ship Johnny-4da.\nMilestones: design.",
            )
        return httpx.Response(404)

    account = _make_account(session, crypto)
    client = _make_client(session, account, crypto, settings, handler)
    try:
        outcome = await resolve_event_attachments(
            client=client,
            description=(
                "Agenda: https://docs.google.com/document/d/docABCDEF12/edit"
            ),
            cached_etags=None,
        )
    finally:
        await client.aclose()

    assert outcome.links_found == 1
    assert outcome.links_skipped == []
    assert outcome.text is not None
    assert "--- Quarterly Plan ---" in outcome.text
    assert "Objective: ship Johnny-4da." in outcome.text
    assert outcome.etags == {"docABCDEF12": "2026-06-01T10:00:00.000Z"}


@pytest.mark.asyncio
async def test_resolve_cache_skips_body_fetch_when_etag_matches(
    session: Session,
    crypto: CredentialCrypto,
    settings: Settings,
) -> None:
    """When ``modifiedTime`` matches the cache, the body endpoint is NOT hit."""
    body_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/drive/v3/files/docABCDEF12":
            return httpx.Response(
                200,
                json={
                    "id": "docABCDEF12",
                    "name": "Quarterly Plan",
                    "mimeType": DOCS_MIME,
                    "modifiedTime": "2026-06-01T10:00:00.000Z",
                },
            )
        if request.url.path == "/drive/v3/files/docABCDEF12/export":
            body_calls.append(str(request.url))
            return httpx.Response(200, text="Should not be fetched.")
        return httpx.Response(404)

    account = _make_account(session, crypto)
    client = _make_client(session, account, crypto, settings, handler)
    try:
        outcome = await resolve_event_attachments(
            client=client,
            description=(
                "Agenda: https://docs.google.com/document/d/docABCDEF12/edit"
            ),
            cached_etags={"docABCDEF12": "2026-06-01T10:00:00.000Z"},
        )
    finally:
        await client.aclose()

    assert outcome.cache_reused is True
    assert outcome.text is None
    # Body endpoint was never called — the etag short-circuit fired.
    assert body_calls == []
    # But the etag map is still returned so the caller can refresh
    # the row's cache key (even when it didn't change).
    assert outcome.etags == {"docABCDEF12": "2026-06-01T10:00:00.000Z"}


@pytest.mark.asyncio
async def test_resolve_cache_invalidates_when_modified_time_changed(
    session: Session,
    crypto: CredentialCrypto,
    settings: Settings,
) -> None:
    """Body fetch fires when the doc's modifiedTime changed since last sync."""
    body_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal body_calls
        if request.url.path == "/drive/v3/files/docABCDEF12":
            return httpx.Response(
                200,
                json={
                    "id": "docABCDEF12",
                    "name": "Quarterly Plan",
                    "mimeType": DOCS_MIME,
                    "modifiedTime": "2026-06-02T15:00:00.000Z",
                },
            )
        if request.url.path == "/drive/v3/files/docABCDEF12/export":
            body_calls += 1
            return httpx.Response(200, text="Revised plan content.")
        return httpx.Response(404)

    account = _make_account(session, crypto)
    client = _make_client(session, account, crypto, settings, handler)
    try:
        outcome = await resolve_event_attachments(
            client=client,
            description=(
                "Agenda: https://docs.google.com/document/d/docABCDEF12/edit"
            ),
            cached_etags={"docABCDEF12": "2026-06-01T10:00:00.000Z"},
        )
    finally:
        await client.aclose()

    assert body_calls == 1
    assert outcome.text is not None
    assert "Revised plan content." in outcome.text
    assert outcome.etags == {"docABCDEF12": "2026-06-02T15:00:00.000Z"}


# --- Resolver: Sheets with multiple tabs ----------------------------------


@pytest.mark.asyncio
async def test_resolve_sheets_link_renders_all_tabs(
    session: Session,
    crypto: CredentialCrypto,
    settings: Settings,
) -> None:
    """Sheets with multiple tabs — both tabs render in the output."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/drive/v3/files/sheetXYZ4567":
            return httpx.Response(
                200,
                json={
                    "id": "sheetXYZ4567",
                    "name": "Targets",
                    "mimeType": SHEETS_MIME,
                    "modifiedTime": "2026-06-01T10:00:00.000Z",
                },
            )
        if request.url.path == "/v4/spreadsheets/sheetXYZ4567":
            return httpx.Response(
                200,
                json={
                    "properties": {"title": "Targets"},
                    "sheets": [
                        {
                            "properties": {"title": "Q3"},
                            "data": [
                                {
                                    "rowData": [
                                        {
                                            "values": [
                                                {"formattedValue": "Region"},
                                                {"formattedValue": "Target"},
                                            ]
                                        },
                                        {
                                            "values": [
                                                {"formattedValue": "EMEA"},
                                                {"formattedValue": "$1.2M"},
                                            ]
                                        },
                                    ]
                                }
                            ],
                        },
                        {
                            "properties": {"title": "Q4"},
                            "data": [
                                {
                                    "rowData": [
                                        {
                                            "values": [
                                                {"formattedValue": "Region"},
                                                {"formattedValue": "Target"},
                                            ]
                                        },
                                        {
                                            "values": [
                                                {"formattedValue": "APAC"},
                                                {"formattedValue": "$800k"},
                                            ]
                                        },
                                    ]
                                }
                            ],
                        },
                    ],
                },
            )
        return httpx.Response(404)

    account = _make_account(session, crypto)
    client = _make_client(session, account, crypto, settings, handler)
    try:
        outcome = await resolve_event_attachments(
            client=client,
            description=(
                "Targets: https://docs.google.com/spreadsheets/d/sheetXYZ4567/edit"
            ),
        )
    finally:
        await client.aclose()

    assert outcome.text is not None
    assert "### Q3" in outcome.text
    assert "### Q4" in outcome.text
    assert "EMEA" in outcome.text
    assert "APAC" in outcome.text


# --- Permission denied / graceful degradation -----------------------------


@pytest.mark.asyncio
async def test_resolve_permission_denied_logs_and_continues(
    session: Session,
    crypto: CredentialCrypto,
    settings: Settings,
) -> None:
    """403 on metadata → file is skipped, error logged in ``links_skipped``.

    Acceptance: "Drive permission denied → logged + the link continues
    to ride as plain text (no crash)."
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/drive/v3/files/lockedDOC11":
            return httpx.Response(
                403,
                json={
                    "error": {
                        "message": "The user does not have sufficient permissions"
                    }
                },
            )
        # Reachable doc as a sibling — should still resolve.
        if request.url.path == "/drive/v3/files/openDOC22":
            return httpx.Response(
                200,
                json={
                    "id": "openDOC22",
                    "name": "Readable Doc",
                    "mimeType": DOCS_MIME,
                    "modifiedTime": "2026-06-01T10:00:00.000Z",
                },
            )
        if request.url.path == "/drive/v3/files/openDOC22/export":
            return httpx.Response(200, text="readable body content")
        return httpx.Response(404)

    account = _make_account(session, crypto)
    client = _make_client(session, account, crypto, settings, handler)
    try:
        outcome = await resolve_event_attachments(
            client=client,
            description=(
                "Both: "
                "https://docs.google.com/document/d/lockedDOC11/edit and "
                "https://docs.google.com/document/d/openDOC22/edit"
            ),
        )
    finally:
        await client.aclose()

    assert outcome.links_found == 2
    assert any("permission denied" in note for note in outcome.links_skipped)
    assert outcome.text is not None
    # Reachable doc still surfaces.
    assert "readable body content" in outcome.text
    # Etag map records the denied file with a stable sentinel so the next
    # poll cycle's cache-skip can short-circuit instead of re-hitting 403.
    assert outcome.etags["lockedDOC11"] == "permission_denied"
    assert outcome.etags["openDOC22"] == "2026-06-01T10:00:00.000Z"


@pytest.mark.asyncio
async def test_resolve_total_cap_clips_extra_attachments(
    session: Session,
    crypto: CredentialCrypto,
    settings: Settings,
) -> None:
    """When the running total crosses MAX_ATTACHMENT_CHARS_TOTAL the
    remaining attachments are dropped (recorded in links_skipped).

    Prevents a single doc with multiple linked docs from blowing the
    prompt budget.
    """
    huge_body = "A" * (MAX_ATTACHMENT_CHARS_TOTAL + 5_000)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/export"):
            return httpx.Response(200, text=huge_body)
        if path.startswith("/drive/v3/files/"):
            file_id = path.split("/")[-1]
            return httpx.Response(
                200,
                json={
                    "id": file_id,
                    "name": f"Big {file_id}",
                    "mimeType": DOCS_MIME,
                    "modifiedTime": "2026-06-01T10:00:00.000Z",
                },
            )
        return httpx.Response(404)

    account = _make_account(session, crypto)
    client = _make_client(session, account, crypto, settings, handler)
    try:
        outcome = await resolve_event_attachments(
            client=client,
            description=(
                "Big 1: https://docs.google.com/document/d/bigDoc11AAA/edit\n"
                "Big 2: https://docs.google.com/document/d/bigDoc22BBB/edit\n"
                "Big 3: https://docs.google.com/document/d/bigDoc33CCC/edit"
            ),
        )
    finally:
        await client.aclose()

    assert outcome.text is not None
    # Total cap honoured — final length below the global ceiling.
    assert len(outcome.text) <= MAX_ATTACHMENT_CHARS_TOTAL + 2_000
    assert any("total cap" in note for note in outcome.links_skipped)


@pytest.mark.asyncio
async def test_resolve_no_drive_urls_returns_empty(
    session: Session,
    crypto: CredentialCrypto,
    settings: Settings,
) -> None:
    """A description without Drive URLs short-circuits without HTTP calls."""
    hit_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal hit_count
        hit_count += 1
        return httpx.Response(404)

    account = _make_account(session, crypto)
    client = _make_client(session, account, crypto, settings, handler)
    try:
        outcome = await resolve_event_attachments(
            client=client,
            description="Standup notes — see the wiki page (not linked).",
        )
    finally:
        await client.aclose()

    assert outcome == ResolutionOutcome(
        text=None, etags={}, links_found=0, links_skipped=[]
    )
    assert hit_count == 0
