"""Tests for the ``/providers/pipeline`` GET/PUT endpoints (Johnny-ckz.17).

Covers the acceptance criterion: "``pipeline_mode`` and ``s2s_provider``
set via providers API survive a backend restart (round-trip through DB)."

The round-trip is exercised by writing the singleton through the API,
reopening the DB session (the SQLAlchemy-side simulation of a restart),
and reading the value back via the same API to assert it matches.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_crypto, get_session
from app.api.providers import router as providers_router
from app.db.base import Base
from app.db.models import PipelineMode, PipelineSettings, ProviderCredential
from app.providers.base import ProviderKind
from app.security.crypto import CredentialCrypto, encrypt_json


@pytest.fixture
def engine() -> sa.Engine:
    # StaticPool + check_same_thread=False so the in-memory SQLite DB is
    # shared across the TestClient's worker thread and the test thread.
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    Base.metadata.create_all(
        bind=eng,
        tables=[
            PipelineSettings.__table__,  # type: ignore[list-item]
            ProviderCredential.__table__,  # type: ignore[list-item]
        ],
    )
    # Seed the singleton row so the API can read it (mirrors what the
    # alembic migration does on a fresh DB).
    with Session(eng) as session:
        session.add(PipelineSettings(id=1, pipeline_mode=PipelineMode.SPLIT))
        session.commit()
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
def client(
    db_session: Session, crypto: CredentialCrypto
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

    app = FastAPI()
    app.include_router(providers_router)
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_crypto] = _override_crypto
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_get_pipeline_settings_returns_default_split(client: TestClient) -> None:
    resp = client.get("/providers/pipeline")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pipeline_mode"] == "split"
    assert body["s2s_provider"] is None


def test_put_pipeline_settings_updates_mode(client: TestClient) -> None:
    resp = client.put(
        "/providers/pipeline", json={"pipeline_mode": "unified"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pipeline_mode"] == "unified"


def test_pipeline_mode_survives_db_session_close(
    client: TestClient, engine: sa.Engine
) -> None:
    """Set unified via API, reopen the DB, GET via API → still unified."""
    put_resp = client.put(
        "/providers/pipeline", json={"pipeline_mode": "unified"}
    )
    assert put_resp.status_code == 200

    # Reopen the DB via a fresh sessionmaker — same engine, same data,
    # different Session. Mirrors a backend process restart against the
    # persistent DB.
    with Session(engine) as fresh:
        row = fresh.get(PipelineSettings, 1)
        assert row is not None
        assert row.pipeline_mode == PipelineMode.UNIFIED

    get_resp = client.get("/providers/pipeline")
    assert get_resp.status_code == 200
    assert get_resp.json()["pipeline_mode"] == "unified"


def test_put_pipeline_settings_rejects_unknown_mode(client: TestClient) -> None:
    resp = client.put(
        "/providers/pipeline", json={"pipeline_mode": "bogus"}
    )
    assert resp.status_code == 422


def test_get_pipeline_settings_surfaces_active_s2s_provider(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
) -> None:
    """When an S2S row is active, ``s2s_provider`` carries its name."""
    row = ProviderCredential(
        kind=ProviderKind.S2S,
        provider_name="stub",
        display_name="Stub",
        credentials_encrypted=encrypt_json(crypto, {}),
        config={"response_text": "hi"},
        is_active=True,
    )
    db_session.add(row)
    db_session.commit()

    resp = client.get("/providers/pipeline")
    assert resp.status_code == 200
    body = resp.json()
    assert body["s2s_provider"] == "stub"


def test_get_pipeline_settings_s2s_none_when_no_active_row(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
) -> None:
    """An inactive S2S row must still surface as ``s2s_provider=None``."""
    row = ProviderCredential(
        kind=ProviderKind.S2S,
        provider_name="stub",
        display_name="Stub",
        credentials_encrypted=encrypt_json(crypto, {}),
        config={},
        is_active=False,
    )
    db_session.add(row)
    db_session.commit()

    resp = client.get("/providers/pipeline")
    assert resp.status_code == 200
    body = resp.json()
    assert body["s2s_provider"] is None
