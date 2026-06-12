"""Tests for the /agents CRUD API (Johnny-trt.41).

Validation parity with the retired templates/personalities rules:
limited_auto_speak needs allowed_replies, autonomous needs a character
prompt, provider FKs are kind-validated per role slot, names are unique,
and exactly one default exists at any time (set-default atomicity).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import get_session
from app.db import Base
from app.db.models import Agent, BotMode, ProviderCredential, ProviderKind
from app.main import app


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    return eng


@pytest.fixture
def db_session(engine: sa.Engine) -> Iterator[Session]:
    maker = sessionmaker(bind=engine)
    session = maker()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    def _override() -> Iterator[Session]:
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_session, None)


def _seed_provider(
    db_session: Session, *, kind: ProviderKind, name: str = "p"
) -> ProviderCredential:
    row = ProviderCredential(
        kind=kind,
        provider_name=name,
        display_name=f"{name} ({kind.value})",
        credentials_encrypted="enc",
        config={},
        is_active=False,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _create_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": "Echo"}
    payload.update(overrides)
    return payload


# --- create ----------------------------------------------------------------


def test_create_minimal_agent_defaults(client: TestClient) -> None:
    resp = client.post("/agents", json=_create_payload())
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Echo"
    assert body["mode"] == "listen_only"
    assert body["allowed_replies"] == []
    assert body["confidence_threshold"] == 0.7
    assert body["is_default"] is False
    assert body["character_prompt"] == ""
    assert body["tts_options"] == {}
    for field in (
        "router_llm_provider_id",
        "answer_llm_provider_id",
        "reasoning_llm_provider_id",
        "tts_provider_id",
        "tts_voice_id",
        "avatar",
        "description",
    ):
        assert body[field] is None


def test_create_duplicate_name_409(client: TestClient) -> None:
    assert client.post("/agents", json=_create_payload()).status_code == 201
    resp = client.post("/agents", json=_create_payload())
    assert resp.status_code == 409


def test_create_limited_auto_speak_requires_replies(client: TestClient) -> None:
    resp = client.post(
        "/agents", json=_create_payload(mode=BotMode.LIMITED_AUTO_SPEAK.value)
    )
    assert resp.status_code == 422
    ok = client.post(
        "/agents",
        json=_create_payload(
            mode=BotMode.LIMITED_AUTO_SPEAK.value, allowed_replies=["Yes.", "No."]
        ),
    )
    assert ok.status_code == 201


def test_create_blank_replies_are_stripped_and_fail_allowlist_mode(
    client: TestClient,
) -> None:
    resp = client.post(
        "/agents",
        json=_create_payload(
            mode=BotMode.LIMITED_AUTO_SPEAK.value, allowed_replies=["  ", ""]
        ),
    )
    assert resp.status_code == 422


def test_create_autonomous_requires_character_prompt(client: TestClient) -> None:
    resp = client.post(
        "/agents", json=_create_payload(mode=BotMode.AUTONOMOUS.value)
    )
    assert resp.status_code == 422
    ok = client.post(
        "/agents",
        json=_create_payload(
            mode=BotMode.AUTONOMOUS.value, character_prompt="Be sharp."
        ),
    )
    assert ok.status_code == 201


def test_create_voice_requires_tts_provider(
    client: TestClient, db_session: Session
) -> None:
    resp = client.post("/agents", json=_create_payload(tts_voice_id="voice-1"))
    assert resp.status_code == 422
    tts = _seed_provider(db_session, kind=ProviderKind.TTS)
    ok = client.post(
        "/agents",
        json=_create_payload(tts_voice_id="voice-1", tts_provider_id=tts.id),
    )
    assert ok.status_code == 201
    assert ok.json()["tts_voice_id"] == "voice-1"


@pytest.mark.parametrize(
    "field",
    [
        "router_llm_provider_id",
        "answer_llm_provider_id",
        "reasoning_llm_provider_id",
    ],
)
def test_llm_role_slots_reject_wrong_kind_and_missing(
    client: TestClient, db_session: Session, field: str
) -> None:
    missing = client.post("/agents", json=_create_payload(**{field: 999}))
    assert missing.status_code == 422

    tts = _seed_provider(db_session, kind=ProviderKind.TTS, name="tts")
    wrong_kind = client.post("/agents", json=_create_payload(**{field: tts.id}))
    assert wrong_kind.status_code == 422
    assert "expected llm" in wrong_kind.json()["detail"]

    llm = _seed_provider(db_session, kind=ProviderKind.LLM, name="llm")
    ok = client.post("/agents", json=_create_payload(**{field: llm.id}))
    assert ok.status_code == 201
    assert ok.json()[field] == llm.id


def test_tts_provider_rejects_llm_kind(
    client: TestClient, db_session: Session
) -> None:
    llm = _seed_provider(db_session, kind=ProviderKind.LLM, name="llm")
    resp = client.post("/agents", json=_create_payload(tts_provider_id=llm.id))
    assert resp.status_code == 422
    assert "expected tts" in resp.json()["detail"]


# --- list / get --------------------------------------------------------------


def test_list_orders_default_first_then_alpha(
    client: TestClient, db_session: Session
) -> None:
    db_session.add(Agent(name="Zed", is_default=True))
    db_session.add(Agent(name="Alpha"))
    db_session.add(Agent(name="Mike"))
    db_session.flush()
    resp = client.get("/agents")
    assert resp.status_code == 200
    names = [a["name"] for a in resp.json()]
    assert names == ["Zed", "Alpha", "Mike"]


def test_get_404(client: TestClient) -> None:
    assert client.get("/agents/12345").status_code == 404


# --- patch -------------------------------------------------------------------


def test_patch_updates_fields(client: TestClient) -> None:
    created = client.post("/agents", json=_create_payload()).json()
    resp = client.patch(
        f"/agents/{created['id']}",
        json={"avatar": "🤖", "confidence_threshold": 0.9},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["avatar"] == "🤖"
    assert body["confidence_threshold"] == 0.9
    assert body["name"] == "Echo"


def test_patch_mode_flip_validates_effective_state(client: TestClient) -> None:
    created = client.post("/agents", json=_create_payload()).json()
    # Flipping to limited_auto_speak without replies anywhere → 422 even
    # though the patch itself never mentions allowed_replies.
    resp = client.patch(
        f"/agents/{created['id']}", json={"mode": BotMode.LIMITED_AUTO_SPEAK.value}
    )
    assert resp.status_code == 422
    # Same flip with replies in the same patch → OK.
    ok = client.patch(
        f"/agents/{created['id']}",
        json={
            "mode": BotMode.LIMITED_AUTO_SPEAK.value,
            "allowed_replies": ["Understood."],
        },
    )
    assert ok.status_code == 200


def test_patch_clearing_replies_under_allowlist_mode_422(
    client: TestClient,
) -> None:
    created = client.post(
        "/agents",
        json=_create_payload(
            mode=BotMode.LIMITED_AUTO_SPEAK.value, allowed_replies=["Yes."]
        ),
    ).json()
    resp = client.patch(f"/agents/{created['id']}", json={"allowed_replies": []})
    assert resp.status_code == 422


def test_patch_autonomous_prompt_clear_422(client: TestClient) -> None:
    created = client.post(
        "/agents",
        json=_create_payload(
            mode=BotMode.AUTONOMOUS.value, character_prompt="Be sharp."
        ),
    ).json()
    resp = client.patch(f"/agents/{created['id']}", json={"character_prompt": ""})
    assert resp.status_code == 422


def test_patch_duplicate_name_409(client: TestClient) -> None:
    client.post("/agents", json=_create_payload(name="One"))
    two = client.post("/agents", json=_create_payload(name="Two")).json()
    resp = client.patch(f"/agents/{two['id']}", json={"name": "One"})
    assert resp.status_code == 409


# --- clone -------------------------------------------------------------------


def test_clone_copies_fields_non_default(
    client: TestClient, db_session: Session
) -> None:
    llm = _seed_provider(db_session, kind=ProviderKind.LLM, name="llm")
    created = client.post(
        "/agents",
        json=_create_payload(
            character_prompt="Be sharp.",
            mode=BotMode.AUTONOMOUS.value,
            answer_llm_provider_id=llm.id,
            avatar="🦾",
        ),
    ).json()
    client.post(f"/agents/{created['id']}/set-default")

    resp = client.post(f"/agents/{created['id']}/clone")
    assert resp.status_code == 201
    clone = resp.json()
    assert clone["name"] == "Echo (copy)"
    assert clone["is_default"] is False
    assert clone["character_prompt"] == "Be sharp."
    assert clone["answer_llm_provider_id"] == llm.id
    assert clone["avatar"] == "🦾"

    second = client.post(f"/agents/{created['id']}/clone").json()
    assert second["name"] == "Echo (copy 2)"


# --- delete / set-default -----------------------------------------------------


def test_delete_refuses_default(client: TestClient) -> None:
    created = client.post("/agents", json=_create_payload()).json()
    client.post(f"/agents/{created['id']}/set-default")
    resp = client.delete(f"/agents/{created['id']}")
    assert resp.status_code == 409


def test_delete_non_default_204(client: TestClient) -> None:
    created = client.post("/agents", json=_create_payload()).json()
    assert client.delete(f"/agents/{created['id']}").status_code == 204
    assert client.get(f"/agents/{created['id']}").status_code == 404


def test_set_default_is_atomic_single_default(
    client: TestClient, db_session: Session
) -> None:
    a = client.post("/agents", json=_create_payload(name="A")).json()
    b = client.post("/agents", json=_create_payload(name="B")).json()

    assert client.post(f"/agents/{a['id']}/set-default").json()["is_default"] is True
    assert client.post(f"/agents/{b['id']}/set-default").json()["is_default"] is True

    defaults = [row for row in client.get("/agents").json() if row["is_default"]]
    assert [row["id"] for row in defaults] == [b["id"]]
