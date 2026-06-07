"""Tests for ``_resolve_account_for_finalize`` (Johnny-al3 AC #4).

The multi-account bot story turns on one invariant: when the noVNC
sign-in finishes and the supervisor reports a scraped email, the API
must dedupe against existing :class:`GoogleAccount` rows by email and
NOT create a duplicate. A second, separate-email sign-in must produce a
NEW bot-only row. A pre-bound ``account_id`` always wins over both.

This locks down the decision tree inside
``app.api.bot_signin._resolve_account_for_finalize`` so the regression
that ships duplicates can't sneak back in.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.api.bot_signin import _resolve_account_for_finalize
from app.db import Base
from app.db.models import GoogleAccount
from app.security.crypto import CredentialCrypto
from app.services.bot_signin import BotSigninSession


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    Base.metadata.create_all(bind=eng, tables=[GoogleAccount.__table__])  # type: ignore[list-item]
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
    # The resolver does not actually invoke the crypto for any of the
    # tested branches, but the signature requires it so we hand it a
    # real instance built from a valid Fernet key.
    return CredentialCrypto(key=base64.urlsafe_b64encode(b"a" * 32))


def _make_signin(
    *,
    account_id: int | None = None,
    signin_id: str = "abc-123",
) -> BotSigninSession:
    now = datetime.now(UTC)
    return BotSigninSession(
        id=signin_id,
        container_name=f"johnny-bot-signin-{signin_id}",
        status="pending",
        created_at=now,
        expires_at=now + timedelta(minutes=10),
        account_id=account_id,
    )


# --- Pre-bound account wins -----------------------------------------------


def test_resolve_uses_prebound_account_id_when_set(
    db_session: Session, crypto: CredentialCrypto
) -> None:
    """``account_id`` on the BotSigninSession beats the scraped email."""
    bound = GoogleAccount(email="bound@example.com", refresh_token_encrypted=None)
    db_session.add(bound)
    db_session.flush()
    other = GoogleAccount(email="other@example.com", refresh_token_encrypted="x")
    db_session.add(other)
    db_session.flush()

    signin = _make_signin(account_id=bound.id)
    resolved = _resolve_account_for_finalize(
        session=db_session,
        crypto=crypto,
        signin=signin,
        scraped_email="other@example.com",  # would match `other`
    )

    assert resolved.id == bound.id


# --- Dedup by scraped email (AC #4) ---------------------------------------


def test_resolve_attaches_to_existing_row_by_scraped_email(
    db_session: Session, crypto: CredentialCrypto
) -> None:
    """Scraped email matching an existing row collapses onto that row.

    The existing row may be calendar-only — the bot capability attaches
    to it without creating a duplicate. This is AC #4 of Johnny-al3.
    """
    calendar_only = GoogleAccount(
        email="alice@example.com",
        refresh_token_encrypted="encrypted-blob",
        access_token_encrypted="x",
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(calendar_only)
    db_session.flush()

    before = db_session.scalar(sa.select(sa.func.count()).select_from(GoogleAccount))

    signin = _make_signin()
    resolved = _resolve_account_for_finalize(
        session=db_session,
        crypto=crypto,
        signin=signin,
        scraped_email="alice@example.com",
    )

    assert resolved.id == calendar_only.id
    assert resolved.refresh_token_encrypted == "encrypted-blob"
    after = db_session.scalar(sa.select(sa.func.count()).select_from(GoogleAccount))
    assert after == before, "must not create a duplicate row"


def test_resolve_matches_case_insensitively(
    db_session: Session, crypto: CredentialCrypto
) -> None:
    """A supervisor that emits a mixed-case email still hits the existing row."""
    existing = GoogleAccount(email="alice@example.com", refresh_token_encrypted=None)
    db_session.add(existing)
    db_session.flush()

    signin = _make_signin()
    resolved = _resolve_account_for_finalize(
        session=db_session,
        crypto=crypto,
        signin=signin,
        scraped_email="Alice@Example.COM",
    )

    assert resolved.id == existing.id


# --- New row creation -----------------------------------------------------


def test_resolve_creates_bot_only_row_when_no_match(
    db_session: Session, crypto: CredentialCrypto
) -> None:
    """A scraped email with no existing row inserts a fresh bot-only row.

    The new row has ``refresh_token_encrypted=None`` so the capability
    model surfaces it as a bot identity but NOT a calendar source.
    """
    signin = _make_signin()
    resolved = _resolve_account_for_finalize(
        session=db_session,
        crypto=crypto,
        signin=signin,
        scraped_email="newbot@example.com",
    )

    assert resolved.id is not None
    assert resolved.email == "newbot@example.com"
    assert resolved.refresh_token_encrypted is None
    assert resolved.access_token_encrypted is None
    assert resolved.token_expires_at is None


def test_two_distinct_emails_produce_two_distinct_rows(
    db_session: Session, crypto: CredentialCrypto
) -> None:
    """Two separate sign-ins with different emails => two rows (AC #2)."""
    first = _resolve_account_for_finalize(
        session=db_session,
        crypto=crypto,
        signin=_make_signin(signin_id="s1"),
        scraped_email="bot1@example.com",
    )
    second = _resolve_account_for_finalize(
        session=db_session,
        crypto=crypto,
        signin=_make_signin(signin_id="s2"),
        scraped_email="bot2@example.com",
    )

    assert first.id != second.id
    assert first.email == "bot1@example.com"
    assert second.email == "bot2@example.com"


# --- Placeholder fallback -------------------------------------------------


def test_resolve_creates_placeholder_row_when_scrape_fails(
    db_session: Session, crypto: CredentialCrypto
) -> None:
    """No scraped email => placeholder address so the user can rename later."""
    signin = _make_signin(signin_id="placeholder-test")
    resolved = _resolve_account_for_finalize(
        session=db_session,
        crypto=crypto,
        signin=signin,
        scraped_email=None,
    )

    assert resolved.email.endswith("@johnny.local")
    assert resolved.refresh_token_encrypted is None


# --- Vanished pre-bound row fallback --------------------------------------


def test_resolve_falls_back_to_email_match_when_prebound_row_vanished(
    db_session: Session, crypto: CredentialCrypto
) -> None:
    """If the pre-bound account_id was deleted, we still try the scraped email."""
    existing = GoogleAccount(email="alice@example.com", refresh_token_encrypted=None)
    db_session.add(existing)
    db_session.flush()

    signin = _make_signin(account_id=99999)  # non-existent id
    resolved = _resolve_account_for_finalize(
        session=db_session,
        crypto=crypto,
        signin=signin,
        scraped_email="alice@example.com",
    )

    assert resolved.id == existing.id
