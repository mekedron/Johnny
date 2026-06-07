"""Tests for the ``/personalities`` CRUD + set-default endpoints (Johnny-oly.2).

Covers every endpoint (happy path, validation 422, 404, 409), the
single-default invariant under rapid alternating ``set-default`` calls, the
``ON DELETE SET NULL`` provider-FK behaviour, and the refusal to delete the
default personality.

Mirrors ``test_pipeline_settings.py``: an in-memory SQLite DB shared across
the TestClient thread via ``StaticPool``, the ORM ``get_session`` dependency
overridden onto it. ``PRAGMA foreign_keys=ON`` is enabled so the SET-NULL FK
contract is actually exercised (SQLite leaves FK enforcement off by default).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.personalities import router as personalities_router
from app.db.base import Base
from app.db.models import Personality, ProviderCredential
from app.providers.base import ProviderKind


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )

    @sa.event.listens_for(eng, "connect")
    def _enable_fk(dbapi_conn: object, _record: object) -> None:
        cursor = dbapi_conn.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(
        bind=eng,
        tables=[
            ProviderCredential.__table__,  # type: ignore[list-item]
            Personality.__table__,  # type: ignore[list-item]
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
def client(db_session: Session) -> Iterator[TestClient]:
    def _override_session() -> Iterator[Session]:
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    app = FastAPI()
    app.include_router(personalities_router)
    app.dependency_overrides[get_session] = _override_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _make_provider(
    db_session: Session,
    *,
    kind: ProviderKind,
    display_name: str,
    is_active: bool = False,
) -> int:
    row = ProviderCredential(
        kind=kind,
        provider_name="stub",
        display_name=display_name,
        credentials_encrypted="x",
        config={},
        is_active=is_active,
    )
    db_session.add(row)
    db_session.commit()
    return row.id


def _create(client: TestClient, **body: object) -> dict[str, Any]:
    body.setdefault("display_name", "P")
    resp = client.post("/personalities", json=body)
    assert resp.status_code == 201, resp.text
    data: dict[str, Any] = resp.json()
    return data


def _defaults_count(db_session: Session) -> int:
    return len(
        db_session.scalars(
            sa.select(Personality).where(Personality.is_default.is_(True))
        ).all()
    )


# --- list ------------------------------------------------------------------


def test_list_empty(client: TestClient) -> None:
    resp = client.get("/personalities")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_default_first(client: TestClient) -> None:
    a = _create(client, display_name="Aaa")
    b = _create(client, display_name="Zzz")
    client.post(f"/personalities/{b['id']}/set-default")

    rows = client.get("/personalities").json()
    assert [r["id"] for r in rows] == [b["id"], a["id"]]
    assert rows[0]["is_default"] is True
    assert rows[1]["is_default"] is False


# --- create ----------------------------------------------------------------


def test_create_minimal(client: TestClient) -> None:
    body = _create(client, display_name="Johnny")
    assert body["display_name"] == "Johnny"
    assert body["is_default"] is False
    assert body["description"] is None
    assert body["llm_provider_id"] is None
    assert body["tts_provider_id"] is None
    assert body["default_mode"] is None
    assert body["metadata"] == {}


def test_create_full(client: TestClient, db_session: Session) -> None:
    llm = _make_provider(db_session, kind=ProviderKind.LLM, display_name="GPT")
    tts = _make_provider(db_session, kind=ProviderKind.TTS, display_name="Rachel")
    body = _create(
        client,
        display_name="Pro",
        description="for sales",
        llm_provider_id=llm,
        tts_provider_id=tts,
        default_mode="autonomous",
        metadata={"tts_options": {"speed": 1.1}},
    )
    assert body["description"] == "for sales"
    assert body["llm_provider_id"] == llm
    assert body["tts_provider_id"] == tts
    assert body["default_mode"] == "autonomous"
    assert body["metadata"] == {"tts_options": {"speed": 1.1}}


def test_create_duplicate_name_409(client: TestClient) -> None:
    _create(client, display_name="Dup")
    resp = client.post("/personalities", json={"display_name": "Dup"})
    assert resp.status_code == 409


def test_create_invalid_default_mode_422(client: TestClient) -> None:
    resp = client.post(
        "/personalities", json={"display_name": "X", "default_mode": "bogus"}
    )
    assert resp.status_code == 422


def test_create_blank_name_422(client: TestClient) -> None:
    resp = client.post("/personalities", json={"display_name": ""})
    assert resp.status_code == 422


def test_create_missing_llm_fk_422(client: TestClient) -> None:
    resp = client.post(
        "/personalities", json={"display_name": "X", "llm_provider_id": 999}
    )
    assert resp.status_code == 422
    assert "llm_provider_id" in resp.text


def test_create_wrong_kind_fk_422(client: TestClient, db_session: Session) -> None:
    tts = _make_provider(db_session, kind=ProviderKind.TTS, display_name="Rachel")
    resp = client.post(
        "/personalities", json={"display_name": "X", "llm_provider_id": tts}
    )
    assert resp.status_code == 422
    assert "expected llm" in resp.text


def test_create_inactive_provider_fk_ok(
    client: TestClient, db_session: Session
) -> None:
    """A personality may reference an inactive provider (resolver decides later)."""
    llm = _make_provider(
        db_session, kind=ProviderKind.LLM, display_name="GPT", is_active=False
    )
    body = _create(client, display_name="X", llm_provider_id=llm)
    assert body["llm_provider_id"] == llm


# --- get -------------------------------------------------------------------


def test_get_ok(client: TestClient) -> None:
    created = _create(client, display_name="One")
    resp = client.get(f"/personalities/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "One"


def test_get_404(client: TestClient) -> None:
    assert client.get("/personalities/12345").status_code == 404


# --- patch -----------------------------------------------------------------


def test_patch_partial_leaves_others(client: TestClient) -> None:
    created = _create(
        client, display_name="One", description="orig", default_mode="autonomous"
    )
    resp = client.patch(
        f"/personalities/{created['id']}", json={"description": "updated"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["description"] == "updated"
    assert body["display_name"] == "One"
    assert body["default_mode"] == "autonomous"


def test_patch_clear_default_mode(client: TestClient) -> None:
    created = _create(client, display_name="One", default_mode="autonomous")
    resp = client.patch(
        f"/personalities/{created['id']}", json={"default_mode": None}
    )
    assert resp.status_code == 200
    assert resp.json()["default_mode"] is None


def test_patch_metadata(client: TestClient) -> None:
    created = _create(client, display_name="One")
    resp = client.patch(
        f"/personalities/{created['id']}", json={"metadata": {"k": "v"}}
    )
    assert resp.status_code == 200
    assert resp.json()["metadata"] == {"k": "v"}


def test_patch_duplicate_name_409(client: TestClient) -> None:
    _create(client, display_name="Taken")
    other = _create(client, display_name="Other")
    resp = client.patch(
        f"/personalities/{other['id']}", json={"display_name": "Taken"}
    )
    assert resp.status_code == 409


def test_patch_bad_fk_422(client: TestClient) -> None:
    created = _create(client, display_name="One")
    resp = client.patch(
        f"/personalities/{created['id']}", json={"tts_provider_id": 999}
    )
    assert resp.status_code == 422


def test_patch_404(client: TestClient) -> None:
    assert (
        client.patch("/personalities/999", json={"description": "x"}).status_code
        == 404
    )


# --- clone -----------------------------------------------------------------


def test_clone_copies_fields(client: TestClient, db_session: Session) -> None:
    llm = _make_provider(db_session, kind=ProviderKind.LLM, display_name="GPT")
    src = _create(
        client,
        display_name="Base",
        description="d",
        llm_provider_id=llm,
        default_mode="autonomous",
        metadata={"k": 1},
    )
    resp = client.post(f"/personalities/{src['id']}/clone")
    assert resp.status_code == 201
    body = resp.json()
    assert body["display_name"] == "Base (copy)"
    assert body["id"] != src["id"]
    assert body["is_default"] is False
    assert body["llm_provider_id"] == llm
    assert body["default_mode"] == "autonomous"
    assert body["metadata"] == {"k": 1}


def test_clone_twice_disambiguates(client: TestClient) -> None:
    src = _create(client, display_name="Base")
    first = client.post(f"/personalities/{src['id']}/clone").json()
    second = client.post(f"/personalities/{src['id']}/clone").json()
    assert first["display_name"] == "Base (copy)"
    assert second["display_name"] == "Base (copy 2)"


def test_clone_never_default(client: TestClient) -> None:
    src = _create(client, display_name="Base")
    client.post(f"/personalities/{src['id']}/set-default")
    clone = client.post(f"/personalities/{src['id']}/clone").json()
    assert clone["is_default"] is False


def test_clone_404(client: TestClient) -> None:
    assert client.post("/personalities/999/clone").status_code == 404


# --- delete ----------------------------------------------------------------


def test_delete_ok(client: TestClient) -> None:
    created = _create(client, display_name="Tmp")
    assert client.delete(f"/personalities/{created['id']}").status_code == 204
    assert client.get(f"/personalities/{created['id']}").status_code == 404


def test_delete_404(client: TestClient) -> None:
    assert client.delete("/personalities/999").status_code == 404


def test_delete_default_refused_409(client: TestClient) -> None:
    created = _create(client, display_name="TheDefault")
    client.post(f"/personalities/{created['id']}/set-default")
    resp = client.delete(f"/personalities/{created['id']}")
    assert resp.status_code == 409
    # Still there.
    assert client.get(f"/personalities/{created['id']}").status_code == 200


# --- set-default -----------------------------------------------------------


def test_set_default_flips_previous(
    client: TestClient, db_session: Session
) -> None:
    a = _create(client, display_name="A")
    b = _create(client, display_name="B")

    client.post(f"/personalities/{a['id']}/set-default")
    assert _defaults_count(db_session) == 1

    client.post(f"/personalities/{b['id']}/set-default")
    rows = {r["id"]: r for r in client.get("/personalities").json()}
    assert rows[a["id"]]["is_default"] is False
    assert rows[b["id"]]["is_default"] is True
    assert _defaults_count(db_session) == 1


def test_set_default_atomicity_alternating(
    client: TestClient, db_session: Session
) -> None:
    """Rapid alternating set-default never yields zero or two defaults."""
    a = _create(client, display_name="A")
    b = _create(client, display_name="B")

    for target in (a, b, a, b, a):
        resp = client.post(f"/personalities/{target['id']}/set-default")
        assert resp.status_code == 200
        assert resp.json()["is_default"] is True
        assert _defaults_count(db_session) == 1
        rows = {r["id"]: r for r in client.get("/personalities").json()}
        assert rows[target["id"]]["is_default"] is True


def test_set_default_404(client: TestClient) -> None:
    assert client.post("/personalities/999/set-default").status_code == 404


# --- provider FK ON DELETE SET NULL ---------------------------------------


def test_provider_delete_sets_fk_null(
    client: TestClient, db_session: Session
) -> None:
    """Deleting a referenced provider nulls the FK; the personality survives."""
    llm = _make_provider(db_session, kind=ProviderKind.LLM, display_name="GPT")
    tts = _make_provider(db_session, kind=ProviderKind.TTS, display_name="Rachel")
    created = _create(
        client, display_name="P", llm_provider_id=llm, tts_provider_id=tts
    )

    db_session.execute(
        sa.delete(ProviderCredential).where(ProviderCredential.id == llm)
    )
    db_session.commit()

    body = client.get(f"/personalities/{created['id']}").json()
    assert body["llm_provider_id"] is None
    assert body["tts_provider_id"] == tts


# --- metadata wire name ----------------------------------------------------


def test_metadata_uses_clean_wire_name(client: TestClient) -> None:
    """Input + output both use ``metadata`` (not ``extra_metadata``)."""
    body = _create(client, display_name="P", metadata={"a": 1})
    assert "metadata" in body
    assert "extra_metadata" not in body
    assert body["metadata"] == {"a": 1}
