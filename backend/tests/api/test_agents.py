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


# --- workspace attachment (Johnny-wks.1) --------------------------------------


def _seed_workspace(db_session: Session, *, name: str = "Finance") -> Any:
    from app.db.models import Workspace

    row = Workspace(name=name, slug=name.lower(), is_default=False)
    db_session.add(row)
    db_session.flush()
    return row


def test_create_defaults_to_null_workspace(client: TestClient) -> None:
    body = client.post("/agents", json=_create_payload()).json()
    assert body["workspace_id"] is None  # None = the default workspace


def test_workspace_id_round_trips_via_create_patch_and_null(
    client: TestClient, db_session: Session
) -> None:
    ws = _seed_workspace(db_session)
    created = client.post(
        "/agents", json=_create_payload(workspace_id=ws.id)
    ).json()
    assert created["workspace_id"] == ws.id
    assert client.get(f"/agents/{created['id']}").json()["workspace_id"] == ws.id

    # Explicit null reattaches to the default workspace.
    patched = client.patch(
        f"/agents/{created['id']}", json={"workspace_id": None}
    ).json()
    assert patched["workspace_id"] is None

    patched = client.patch(
        f"/agents/{created['id']}", json={"workspace_id": ws.id}
    ).json()
    assert patched["workspace_id"] == ws.id


def test_workspace_id_unknown_422(client: TestClient, db_session: Session) -> None:
    resp = client.post("/agents", json=_create_payload(workspace_id=999))
    assert resp.status_code == 422
    assert "workspace_id=999" in resp.json()["detail"]

    created = client.post("/agents", json=_create_payload()).json()
    resp = client.patch(f"/agents/{created['id']}", json={"workspace_id": 999})
    assert resp.status_code == 422


def test_clone_carries_the_workspace_attachment(
    client: TestClient, db_session: Session
) -> None:
    ws = _seed_workspace(db_session)
    created = client.post(
        "/agents", json=_create_payload(workspace_id=ws.id)
    ).json()
    clone = client.post(f"/agents/{created['id']}/clone").json()
    assert clone["workspace_id"] == ws.id


# --- test_voice endpoint (Johnny-trt.42) -------------------------------------


@pytest.fixture
def crypto() -> Any:
    from cryptography.fernet import Fernet

    from app.security.crypto import CredentialCrypto

    return CredentialCrypto(Fernet.generate_key())


@pytest.fixture
def voice_client(db_session: Session, crypto: Any) -> Iterator[TestClient]:
    """A client with crypto + a clean provider registry for synth tests."""
    from app.api.deps import get_crypto
    from app.providers.base import get_registry

    def _override_session() -> Iterator[Session]:
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    registry = get_registry()
    saved = dict(registry._factories)  # noqa: SLF001 — test-only snapshot
    registry.clear()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_crypto] = lambda: crypto
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_crypto, None)
        registry.clear()
        for key, factory in saved.items():
            registry.register(key[0], key[1], factory)


def _recording_tts_class() -> type:
    """Audible fake TTSProvider that records the options it was built with."""
    from collections.abc import AsyncIterator

    from app.providers.base import ProviderConfig, TTSProvider

    class _RecordingTTS(TTSProvider):
        built_options: dict[str, Any] | None = None

        def __init__(self, config: ProviderConfig) -> None:
            _RECORDING_TTS.built_options = dict(config.options)

        @property
        def name(self) -> str:
            return "recording-tts"

        async def synthesize_stream(
            self, text: str, voice_id: str | None = None
        ) -> AsyncIterator[bytes]:
            import array

            # 3 s of constant-amplitude 16 kHz mono S16LE — clears check_audible.
            yield array.array("h", [10_000] * 48_000).tobytes()

    return _RecordingTTS


def _seed_tts_row(
    db_session: Session,
    crypto: Any,
    *,
    provider_name: str = "fake-tts",
    display_name: str = "Fake TTS",
    options: dict[str, Any] | None = None,
    is_active: bool = False,
) -> ProviderCredential:
    from app.security.crypto import encrypt_json

    row = ProviderCredential(
        kind=ProviderKind.TTS,
        provider_name=provider_name,
        display_name=display_name,
        credentials_encrypted=encrypt_json(crypto, {"api_key": "k"}),
        config=dict(options or {}),
        is_active=is_active,
    )
    db_session.add(row)
    db_session.flush()
    return row


_RECORDING_TTS = _recording_tts_class()


def _register_fake_tts(provider_name: str = "fake-tts") -> None:
    from app.providers.base import get_registry

    get_registry().register(ProviderKind.TTS, provider_name, _RECORDING_TTS)


def test_test_voice_synthesizes_with_pinned_provider_and_voice(
    voice_client: TestClient, db_session: Session, crypto: Any
) -> None:
    _register_fake_tts()
    row = _seed_tts_row(db_session, crypto, options={"model_id": "m1"})
    agent = Agent(
        name="Echo",
        tts_provider_id=row.id,
        tts_voice_id="Rachel",
        tts_options={"stability": 0.4},
    )
    db_session.add(agent)
    db_session.flush()

    resp = voice_client.post(f"/agents/{agent.id}/test_voice")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.content[:4] == b"RIFF"
    assert resp.headers["X-TTS-Audible"] == "1"
    assert resp.headers["X-TTS-Provider"] == "Fake TTS"
    assert resp.headers["X-TTS-Voice"] == "Rachel"
    # The agent's exact combo reached the provider factory: row config +
    # agent tts_options merged, the agent voice last.
    assert _RECORDING_TTS.built_options == {
        "model_id": "m1",
        "stability": 0.4,
        "voice_id": "Rachel",
    }


def test_test_voice_pin_honors_inactive_rows(
    voice_client: TestClient, db_session: Session, crypto: Any
) -> None:
    """One active row per kind — pins must work on inactive rows or per-agent
    voices are impossible. The pin wins over the active row."""
    _register_fake_tts()
    _register_fake_tts("other-tts")
    _seed_tts_row(
        db_session, crypto, provider_name="other-tts",
        display_name="Global active", is_active=True,
    )
    pinned = _seed_tts_row(db_session, crypto, is_active=False)
    agent = Agent(name="Echo", tts_provider_id=pinned.id, tts_voice_id="V2")
    db_session.add(agent)
    db_session.flush()

    resp = voice_client.post(f"/agents/{agent.id}/test_voice")
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-TTS-Provider"] == "Fake TTS"
    assert _RECORDING_TTS.built_options is not None
    assert _RECORDING_TTS.built_options["voice_id"] == "V2"


def test_test_voice_unpinned_agent_uses_global_active_without_voice(
    voice_client: TestClient, db_session: Session, crypto: Any
) -> None:
    _register_fake_tts()
    _seed_tts_row(
        db_session, crypto, options={"voice_id": "row-default"}, is_active=True
    )
    agent = Agent(name="Echo")
    db_session.add(agent)
    db_session.flush()

    resp = voice_client.post(f"/agents/{agent.id}/test_voice")
    assert resp.status_code == 200, resp.text
    # The row's own configured voice plays; nothing agent-side overrides it.
    assert _RECORDING_TTS.built_options == {"voice_id": "row-default"}
    assert resp.headers["X-TTS-Voice"] == "row-default"


def test_test_voice_missing_pin_is_409_not_fallback(
    voice_client: TestClient, db_session: Session, crypto: Any
) -> None:
    """Unlike session start (which falls back so a meeting proceeds), the
    Test endpoint must surface a broken pin to the edit page."""
    _register_fake_tts()
    _seed_tts_row(db_session, crypto, is_active=True)
    agent = Agent(name="Echo", tts_provider_id=9999)
    db_session.add(agent)
    db_session.flush()

    resp = voice_client.post(f"/agents/{agent.id}/test_voice")
    assert resp.status_code == 409
    assert "no longer exists" in resp.json()["detail"]


def test_test_voice_wrong_kind_pin_is_409(
    voice_client: TestClient, db_session: Session, crypto: Any
) -> None:
    llm_row = _seed_provider(db_session, kind=ProviderKind.LLM, name="some-llm")
    agent = Agent(name="Echo", tts_provider_id=llm_row.id)
    db_session.add(agent)
    db_session.flush()

    resp = voice_client.post(f"/agents/{agent.id}/test_voice")
    assert resp.status_code == 409
    assert "kind llm" in resp.json()["detail"]


def test_test_voice_no_provider_anywhere_is_409(
    voice_client: TestClient, db_session: Session
) -> None:
    agent = Agent(name="Echo")
    db_session.add(agent)
    db_session.flush()

    resp = voice_client.post(f"/agents/{agent.id}/test_voice")
    assert resp.status_code == 409
    assert "no global" in resp.json()["detail"]


def test_test_voice_unknown_agent_404(voice_client: TestClient) -> None:
    assert voice_client.post("/agents/424242/test_voice").status_code == 404


# --- meeting_count (Johnny-trt.44) -------------------------------------------


def test_meeting_count_zero_without_assignments(client: TestClient) -> None:
    created = client.post("/agents", json=_create_payload()).json()
    assert created["meeting_count"] == 0
    assert client.get(f"/agents/{created['id']}").json()["meeting_count"] == 0


def test_meeting_count_reflects_meeting_agent_rows(
    client: TestClient, db_session: Session
) -> None:
    # The SQLite test engine doesn't enforce FKs (no PRAGMA foreign_keys),
    # so assignment rows can point at synthetic meeting_config ids — the
    # count only reads meeting_agents.agent_id.
    from app.db.models import MeetingAgent

    a = client.post("/agents", json=_create_payload(name="A")).json()
    b = client.post("/agents", json=_create_payload(name="B")).json()
    db_session.add_all(
        [
            MeetingAgent(meeting_config_id=101, agent_id=a["id"]),
            MeetingAgent(meeting_config_id=102, agent_id=a["id"], enabled=False),
        ]
    )
    db_session.flush()

    by_id = {row["id"]: row for row in client.get("/agents").json()}
    assert by_id[a["id"]]["meeting_count"] == 2
    assert by_id[b["id"]]["meeting_count"] == 0
    assert client.get(f"/agents/{a['id']}").json()["meeting_count"] == 2
    # Patch responses carry the count too (the edit page refreshes from them).
    patched = client.patch(f"/agents/{a['id']}", json={"avatar": "🤖"}).json()
    assert patched["meeting_count"] == 2
