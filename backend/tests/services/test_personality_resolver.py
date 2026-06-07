"""Tests for app.services.personality_resolver (Johnny-oly.3).

Covers the two entry points the PRD (§4) specifies:

* :func:`select_personality` — selection precedence (explicit request → the
  meeting's personality → the ``is_default`` row → ``None``).
* :func:`apply_personality` — LLM/TTS override with **loud fallback**: a FK is
  honored only when its row exists, is ``is_active``, and decrypts; otherwise
  the kind keeps the global-active entry and a ``personality.fallback:`` line is
  logged. A ``NULL`` FK inherits silently.

Note on the one-active-per-kind invariant: in v1 a *valid* personality FK can
only point at the single active row for its kind, which is exactly what the
base payload already carries — so for realistic inputs ``apply_personality``'s
payload equals the base. To prove the override *mechanism* (that it resolves
the FK and overwrites the entry, which lights up in v2), a couple of tests feed
a hand-crafted base payload that deliberately differs from the active row.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import (
    BotMode,
    MeetingConfig,
    Personality,
    ProviderCredential,
    ProviderKind,
)
from app.security.crypto import CredentialCrypto, encrypt_json
from app.services.personality_resolver import (
    apply_personality,
    select_personality,
)
from app.services.provider_payload import build_provider_payload


@pytest.fixture
def crypto() -> CredentialCrypto:
    return CredentialCrypto(Fernet.generate_key())


def _make_engine(*, enforce_fk: bool = False) -> sa.Engine:
    engine = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    if enforce_fk:

        @sa.event.listens_for(engine, "connect")
        def _enable_fk(dbapi_conn: object, _record: object) -> None:
            cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    Base.metadata.create_all(
        bind=engine,
        tables=[
            ProviderCredential.__table__,  # type: ignore[list-item]
            Personality.__table__,  # type: ignore[list-item]
        ],
    )
    return engine


@pytest.fixture
def session() -> Iterator[Session]:
    # FK enforcement OFF so a test can craft a dangling FK (the "missing"
    # fallback). The dedicated deleted-provider test builds its own FK-on engine.
    engine = _make_engine(enforce_fk=False)
    sess = Session(engine)
    try:
        yield sess
    finally:
        sess.close()


def _provider(
    session: Session,
    crypto: CredentialCrypto,
    *,
    kind: ProviderKind = ProviderKind.LLM,
    provider_name: str = "stub",
    display_name: str = "row",
    is_active: bool = True,
    credentials: dict[str, str] | None = None,
    options: dict[str, Any] | None = None,
    bad_cipher: bool = False,
) -> ProviderCredential:
    blob = (
        "not-a-valid-fernet-token"
        if bad_cipher
        else encrypt_json(crypto, credentials or {"api_key": "k"})
    )
    row = ProviderCredential(
        kind=kind,
        provider_name=provider_name,
        display_name=display_name,
        credentials_encrypted=blob,
        config=options or {},
        is_active=is_active,
    )
    session.add(row)
    session.flush()
    return row


def _personality(
    session: Session,
    *,
    name: str = "P",
    is_default: bool = False,
    llm: int | None = None,
    tts: int | None = None,
    mode: BotMode | None = None,
) -> Personality:
    row = Personality(
        display_name=name,
        is_default=is_default,
        llm_provider_id=llm,
        tts_provider_id=tts,
        default_mode=mode,
    )
    session.add(row)
    session.flush()
    return row


# --- select_personality precedence -----------------------------------------


def test_select_explicit_request_wins(session: Session) -> None:
    default = _personality(session, name="Default", is_default=True)
    requested = _personality(session, name="Requested")
    meeting_p = _personality(session, name="Meeting")
    meeting = MeetingConfig(personality_id=meeting_p.id)

    got = select_personality(session, requested_id=requested.id, meeting=meeting)
    assert got is not None and got.id == requested.id
    assert default.id != requested.id  # sanity: default was not chosen


def test_select_stale_request_falls_through_to_meeting(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    _personality(session, name="Default", is_default=True)
    meeting_p = _personality(session, name="Meeting")
    meeting = MeetingConfig(personality_id=meeting_p.id)

    with caplog.at_level(logging.WARNING):
        got = select_personality(session, requested_id=999_999, meeting=meeting)
    assert got is not None and got.id == meeting_p.id
    assert "personality.select" in caplog.text


def test_select_meeting_personality_over_default(session: Session) -> None:
    _personality(session, name="Default", is_default=True)
    meeting_p = _personality(session, name="Meeting")
    meeting = MeetingConfig(personality_id=meeting_p.id)

    got = select_personality(session, requested_id=None, meeting=meeting)
    assert got is not None and got.id == meeting_p.id


def test_select_default_when_no_request_or_meeting(session: Session) -> None:
    default = _personality(session, name="Default", is_default=True)
    got = select_personality(session, requested_id=None, meeting=None)
    assert got is not None and got.id == default.id


def test_select_meeting_with_null_personality_uses_default(session: Session) -> None:
    default = _personality(session, name="Default", is_default=True)
    meeting = MeetingConfig(personality_id=None)
    got = select_personality(session, requested_id=None, meeting=meeting)
    assert got is not None and got.id == default.id


def test_select_none_when_no_default_exists(session: Session) -> None:
    # No is_default row at all (e.g. a DB without the bootstrap seed).
    assert select_personality(session, requested_id=None, meeting=None) is None


# --- apply_personality: no-op / inherit paths ------------------------------


def test_apply_none_personality_is_passthrough_copy(
    session: Session, crypto: CredentialCrypto
) -> None:
    _provider(session, crypto, kind=ProviderKind.LLM, provider_name="ga")
    base = build_provider_payload(session, crypto)

    res = apply_personality(session, base, None, crypto=crypto)
    assert res.payload == base
    assert res.payload is not base  # fresh copy — base must not be mutated
    assert res.personality_id is None
    assert res.personality_name is None
    assert res.default_mode is None
    assert res.fallbacks == ()
    assert res.fell_back is False


def test_apply_null_fks_inherit_silently(
    session: Session, crypto: CredentialCrypto, caplog: pytest.LogCaptureFixture
) -> None:
    """Bootstrap-Johnny shape: NULL FKs → byte-identical payload, NO fallback log."""
    _provider(session, crypto, kind=ProviderKind.LLM, provider_name="ga-llm")
    _provider(session, crypto, kind=ProviderKind.TTS, provider_name="ga-tts")
    base = build_provider_payload(session, crypto)
    johnny = _personality(session, name="Johnny", is_default=True, llm=None, tts=None)

    with caplog.at_level(logging.WARNING):
        res = apply_personality(session, base, johnny, crypto=crypto)

    assert res.payload == base  # zero behaviour change
    assert res.personality_id == johnny.id
    assert res.personality_name == "Johnny"
    assert res.fallbacks == ()
    assert "personality.fallback" not in caplog.text  # silent inherit


# --- apply_personality: override mechanism ---------------------------------


def test_apply_active_slots_resolve_to_those_rows(
    session: Session, crypto: CredentialCrypto
) -> None:
    """Happy path: both slots point at active rows → payload carries those rows."""
    llm = _provider(
        session,
        crypto,
        kind=ProviderKind.LLM,
        provider_name="anthropic",
        display_name="Claude",
        credentials={"api_key": "sk-llm"},
        options={"model": "claude"},
    )
    tts = _provider(
        session,
        crypto,
        kind=ProviderKind.TTS,
        provider_name="piper",
        display_name="Piper",
        credentials={"api_key": "sk-tts"},
    )
    base = build_provider_payload(session, crypto)
    p = _personality(session, name="Brainy", llm=llm.id, tts=tts.id)

    res = apply_personality(session, base, p, crypto=crypto)

    assert res.payload["llm"]["provider_name"] == "anthropic"
    assert res.payload["llm"]["credentials"] == {"api_key": "sk-llm"}
    assert res.payload["llm"]["options"] == {"model": "claude"}
    assert res.payload["tts"]["provider_name"] == "piper"
    assert res.personality_id == p.id
    assert res.fallbacks == ()


def test_apply_overwrites_base_entry_with_personality_row(
    session: Session, crypto: CredentialCrypto
) -> None:
    """Prove apply RESOLVES the FK and overwrites — synthetic base differs from
    the active row, which cannot happen in v1 but isolates the merge contract."""
    active = _provider(
        session,
        crypto,
        kind=ProviderKind.LLM,
        provider_name="new-brain",
        display_name="New",
        credentials={"api_key": "sk-new"},
    )
    synthetic_base = {
        "llm": {
            "provider_name": "OLD",
            "display_name": "old",
            "credentials": {"api_key": "sk-old"},
            "options": {},
        }
    }
    p = _personality(session, name="Override", llm=active.id)

    res = apply_personality(session, synthetic_base, p, crypto=crypto)

    assert res.payload["llm"]["provider_name"] == "new-brain"
    assert res.payload["llm"]["credentials"] == {"api_key": "sk-new"}
    # base left untouched
    assert synthetic_base["llm"]["provider_name"] == "OLD"


# --- apply_personality: fallback paths -------------------------------------


def test_apply_deactivated_fk_falls_back_and_warns(
    session: Session, crypto: CredentialCrypto, caplog: pytest.LogCaptureFixture
) -> None:
    ga = _provider(
        session, crypto, kind=ProviderKind.LLM, provider_name="ga", display_name="GA"
    )
    base = build_provider_payload(session, crypto)
    inactive = _provider(
        session,
        crypto,
        kind=ProviderKind.LLM,
        provider_name="dormant",
        display_name="Dormant",
        is_active=False,
    )
    p = _personality(session, name="PinsDormant", llm=inactive.id)

    with caplog.at_level(logging.WARNING):
        res = apply_personality(session, base, p, crypto=crypto)

    # Fell back to the global-active row.
    assert res.payload["llm"]["provider_name"] == "ga"
    assert ga.id != inactive.id
    assert [(f.kind, f.reason) for f in res.fallbacks] == [("llm", "deactivated")]
    assert res.fell_back is True
    assert "personality.fallback:" in caplog.text
    assert "reason=deactivated" in caplog.text


def test_apply_missing_fk_falls_back(
    session: Session, crypto: CredentialCrypto, caplog: pytest.LogCaptureFixture
) -> None:
    _provider(session, crypto, kind=ProviderKind.TTS, provider_name="ga-tts")
    base = build_provider_payload(session, crypto)
    # Dangling FK (FK enforcement is off on this fixture's engine).
    p = _personality(session, name="Dangling", tts=987_654)

    with caplog.at_level(logging.WARNING):
        res = apply_personality(session, base, p, crypto=crypto)

    assert res.payload["tts"]["provider_name"] == "ga-tts"  # inherited
    assert [(f.kind, f.reason) for f in res.fallbacks] == [("tts", "missing")]
    assert "reason=missing" in caplog.text


def test_apply_undecryptable_fk_falls_back(
    session: Session, crypto: CredentialCrypto, caplog: pytest.LogCaptureFixture
) -> None:
    # One active TTS (good) survives; the single active LLM has a rotated key,
    # so build_provider_payload skips it (no llm in base) and the personality's
    # pin at that same row is undecryptable → fallback, llm channel absent.
    _provider(session, crypto, kind=ProviderKind.TTS, provider_name="ga-tts")
    rotted = _provider(
        session,
        crypto,
        kind=ProviderKind.LLM,
        provider_name="rotated-key",
        display_name="Rotated",
        is_active=True,
        bad_cipher=True,  # credentials won't decrypt
    )
    base = build_provider_payload(session, crypto)
    assert "llm" not in base  # the rotated active row was skipped
    p = _personality(session, name="RotatedKey", llm=rotted.id)

    with caplog.at_level(logging.WARNING):
        res = apply_personality(session, base, p, crypto=crypto)

    assert "llm" not in res.payload  # nothing usable to inherit for llm
    assert res.payload["tts"]["provider_name"] == "ga-tts"  # tts unaffected
    assert [(f.kind, f.reason) for f in res.fallbacks] == [("llm", "undecryptable")]
    assert "reason=undecryptable" in caplog.text


def test_apply_deleted_provider_via_set_null_no_exception(
    crypto: CredentialCrypto, caplog: pytest.LogCaptureFixture
) -> None:
    """A provider deleted out from under a personality SET-NULLs the FK; the
    resolver then inherits global active, silently, with no exception."""
    engine = _make_engine(enforce_fk=True)
    with Session(engine) as session:
        ga = _provider(
            session, crypto, kind=ProviderKind.TTS, provider_name="ga-tts"
        )
        pinned = _provider(
            session,
            crypto,
            kind=ProviderKind.TTS,
            provider_name="doomed",
            is_active=False,
        )
        p = _personality(session, name="PinsDoomed", tts=pinned.id)
        session.commit()

        # Operator deletes the pinned provider; ON DELETE SET NULL fires.
        session.delete(pinned)
        session.commit()
        session.refresh(p)
        assert p.tts_provider_id is None  # SET NULL happened

        base = build_provider_payload(session, crypto)
        with caplog.at_level(logging.WARNING):
            res = apply_personality(session, base, p, crypto=crypto)

        assert res.payload["tts"]["provider_name"] == "ga-tts"  # inherited
        assert res.fallbacks == ()  # NULL FK is silent, not a fallback
        assert "personality.fallback" not in caplog.text
        assert ga.id is not None


# --- apply_personality: mode + isolation -----------------------------------


def test_apply_surfaces_default_mode(
    session: Session, crypto: CredentialCrypto
) -> None:
    p = _personality(session, name="Listener", mode=BotMode.LISTEN_ONLY)
    res = apply_personality(session, {}, p, crypto=crypto)
    assert res.default_mode == "listen_only"

    p2 = _personality(session, name="NoMode", mode=None)
    res2 = apply_personality(session, {}, p2, crypto=crypto)
    assert res2.default_mode is None


def test_apply_does_not_mutate_base_and_is_independent_across_calls(
    session: Session, crypto: CredentialCrypto
) -> None:
    """Concurrent session starts with different personalities don't leak state:
    each call returns an independent payload and never mutates the shared base."""
    a = _provider(
        session, crypto, kind=ProviderKind.LLM, provider_name="A", display_name="A"
    )
    base = build_provider_payload(session, crypto)
    base_snapshot = {k: dict(v) for k, v in base.items()}

    p1 = _personality(session, name="One", llm=a.id, mode=BotMode.SUGGEST_ONLY)
    p2 = _personality(session, name="Two", llm=None, mode=BotMode.AUTONOMOUS)

    res1 = apply_personality(session, base, p1, crypto=crypto)
    res2 = apply_personality(session, base, p2, crypto=crypto)

    assert base == base_snapshot  # base never mutated
    assert res1.payload is not res2.payload
    assert res1.personality_name == "One"
    assert res2.personality_name == "Two"
    assert res1.default_mode == "suggest_only"
    assert res2.default_mode == "autonomous"
    # Mutating one result must not bleed into the other or the base.
    res1.payload["llm"]["provider_name"] = "MUTATED"
    assert res2.payload["llm"]["provider_name"] == "A"
    assert base["llm"]["provider_name"] == "A"
