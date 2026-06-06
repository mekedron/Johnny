"""Tests for the provider configuration HTTP API (US-018)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_crypto, get_session
from app.db import Base
from app.db.models import ProviderCredential
from app.main import app
from app.providers.base import (
    ChatMessage,
    LLMError,
    LLMProvider,
    LLMResponse,
    ProviderConfig,
    ProviderKind,
    STTError,
    STTProvider,
    ToolDefinition,
    TranscriptEvent,
    TTSProvider,
    get_registry,
)
from app.security.crypto import CredentialCrypto

# --- Stub providers used to exercise the smoke-test endpoint ---------------


class _OKSTT(STTProvider):
    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self.received_bytes = 0
        self.closed = False

    @property
    def name(self) -> str:
        return "ok-stt"

    async def transcribe_stream(
        self, audio_iter: AsyncIterator[bytes]
    ) -> AsyncIterator[TranscriptEvent]:
        async for frame in audio_iter:
            self.received_bytes += len(frame)
            yield TranscriptEvent(text="", is_final=True, timestamp_ms=0, confidence=0.0)

    async def close(self) -> None:
        self.closed = True


class _FailingSTT(STTProvider):
    def __init__(self, config: ProviderConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return "failing-stt"

    async def transcribe_stream(
        self, audio_iter: AsyncIterator[bytes]
    ) -> AsyncIterator[TranscriptEvent]:
        # Consume so the underlying generator runs, then fail.
        async for _ in audio_iter:
            pass
        raise STTError("synthetic auth failure")
        # Unreachable yield keeps this an async-generator factory.
        yield TranscriptEvent(text="", is_final=True, timestamp_ms=0)


class _OKLLM(LLMProvider):
    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self.last_messages: Sequence[ChatMessage] | None = None

    @property
    def name(self) -> str:
        return "ok-llm"

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.last_messages = messages
        return LLMResponse(text="hi there", finish_reason="stop")


class _FailingLLM(LLMProvider):
    def __init__(self, config: ProviderConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return "failing-llm"

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        raise LLMError("synthetic rate limit")


class _OKTTS(TTSProvider):
    def __init__(self, config: ProviderConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return "ok-tts"

    async def synthesize_stream(
        self, text: str, voice_id: str | None = None
    ) -> AsyncIterator[bytes]:
        yield b"\x00\x00" * 16
        yield b"\x01\x00" * 16


class _CrashingFactory(STTProvider):
    """Used to verify factory exceptions surface as ok=False, not 500s."""

    def __init__(self, config: ProviderConfig) -> None:
        raise RuntimeError("synthetic init failure")

    @property
    def name(self) -> str:
        return "crash"

    async def transcribe_stream(
        self, audio_iter: AsyncIterator[bytes]
    ) -> AsyncIterator[TranscriptEvent]:
        if False:
            yield TranscriptEvent(text="", is_final=True, timestamp_ms=0)


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def engine() -> sa.Engine:
    # StaticPool + check_same_thread=False so the in-memory SQLite DB is shared
    # across the TestClient's worker thread and the test thread.
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    # Only the providers table — the other tables need pgvector / cross-FKs.
    Base.metadata.create_all(bind=eng, tables=[ProviderCredential.__table__])  # type: ignore[list-item]
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
def client(db_session: Session, crypto: CredentialCrypto) -> Iterator[TestClient]:
    def _override_session() -> Iterator[Session]:
        # The fixture session is shared across one request to inspect state
        # after each call. Commit on success; rollback on error. Do not close.
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    def _override_crypto() -> CredentialCrypto:
        return crypto

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_crypto] = _override_crypto
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    """Snapshot and restore the global provider registry around each test."""
    registry = get_registry()
    saved = dict(registry._factories)  # noqa: SLF001 — test-only registry snapshot
    registry.clear()
    try:
        yield
    finally:
        registry.clear()
        for key, factory in saved.items():
            registry.register(key[0], key[1], factory)


# --- Helpers ---------------------------------------------------------------


def _create_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "stt",
        "provider_name": "ok-stt",
        "display_name": "Deepgram primary",
        "credentials": {"api_key": "sk-test"},
        "options": {"model": "nova-2"},
    }
    base.update(overrides)
    return base


# --- list ------------------------------------------------------------------


def test_list_empty_returns_three_buckets(client: TestClient) -> None:
    resp = client.get("/providers")
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"stt": [], "llm": [], "tts": []}


def test_list_groups_by_kind(client: TestClient) -> None:
    client.post("/providers", json=_create_payload(kind="stt", display_name="A"))
    client.post(
        "/providers",
        json=_create_payload(kind="llm", provider_name="ok-llm", display_name="B"),
    )
    client.post(
        "/providers",
        json=_create_payload(kind="tts", provider_name="ok-tts", display_name="C"),
    )
    resp = client.get("/providers")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["stt"]) == 1
    assert len(data["llm"]) == 1
    assert len(data["tts"]) == 1
    assert data["stt"][0]["display_name"] == "A"


# --- create ----------------------------------------------------------------


def test_create_provider_returns_201_and_hides_credentials(
    client: TestClient, db_session: Session, crypto: CredentialCrypto
) -> None:
    resp = client.post("/providers", json=_create_payload())
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] >= 1
    assert data["kind"] == "stt"
    assert data["provider_name"] == "ok-stt"
    assert data["display_name"] == "Deepgram primary"
    assert data["options"] == {"model": "nova-2"}
    assert data["is_active"] is False
    assert data["credential_keys"] == ["api_key"]
    # Secret never appears in the response.
    assert "sk-test" not in resp.text

    # And is encrypted at rest.
    row = db_session.get(ProviderCredential, data["id"])
    assert row is not None
    assert "sk-test" not in row.credentials_encrypted


def test_create_encrypted_credentials_round_trip(
    client: TestClient, db_session: Session, crypto: CredentialCrypto
) -> None:
    resp = client.post("/providers", json=_create_payload())
    row = db_session.get(ProviderCredential, resp.json()["id"])
    assert row is not None
    # Decrypt and verify it's exactly what we sent.
    from app.security.crypto import decrypt_json

    creds = decrypt_json(crypto, row.credentials_encrypted)
    assert creds == {"api_key": "sk-test"}


def test_create_rejects_bad_kind(client: TestClient) -> None:
    resp = client.post("/providers", json=_create_payload(kind="speech"))
    assert resp.status_code == 422


def test_create_rejects_blank_display_name(client: TestClient) -> None:
    resp = client.post("/providers", json=_create_payload(display_name=""))
    assert resp.status_code == 422


def test_create_duplicate_returns_409(client: TestClient) -> None:
    assert client.post("/providers", json=_create_payload()).status_code == 201
    resp = client.post("/providers", json=_create_payload())
    assert resp.status_code == 409


def test_create_does_not_set_active(client: TestClient) -> None:
    resp = client.post("/providers", json=_create_payload())
    assert resp.json()["is_active"] is False


# --- update ----------------------------------------------------------------


def test_update_display_name_only(client: TestClient) -> None:
    created = client.post("/providers", json=_create_payload()).json()
    resp = client.patch(
        f"/providers/{created['id']}",
        json={"display_name": "Renamed"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["display_name"] == "Renamed"
    # Options and credentials are unchanged.
    assert data["options"] == {"model": "nova-2"}
    assert data["credential_keys"] == ["api_key"]


def test_update_credentials_re_encrypts(
    client: TestClient, db_session: Session, crypto: CredentialCrypto
) -> None:
    created = client.post("/providers", json=_create_payload()).json()
    resp = client.patch(
        f"/providers/{created['id']}",
        json={"credentials": {"api_key": "sk-new", "extra": "x"}},
    )
    assert resp.status_code == 200
    assert resp.json()["credential_keys"] == ["api_key", "extra"]

    from app.security.crypto import decrypt_json

    row = db_session.get(ProviderCredential, created["id"])
    assert row is not None
    assert decrypt_json(crypto, row.credentials_encrypted) == {
        "api_key": "sk-new",
        "extra": "x",
    }


def test_update_options_replaces_dict(client: TestClient) -> None:
    created = client.post("/providers", json=_create_payload()).json()
    resp = client.patch(
        f"/providers/{created['id']}", json={"options": {"model": "nova-3"}}
    )
    assert resp.status_code == 200
    assert resp.json()["options"] == {"model": "nova-3"}


def test_update_missing_returns_404(client: TestClient) -> None:
    resp = client.patch("/providers/9999", json={"display_name": "x"})
    assert resp.status_code == 404


# --- delete ----------------------------------------------------------------


def test_delete_removes_row(client: TestClient, db_session: Session) -> None:
    created = client.post("/providers", json=_create_payload()).json()
    resp = client.delete(f"/providers/{created['id']}")
    assert resp.status_code == 204
    assert db_session.get(ProviderCredential, created["id"]) is None


def test_delete_missing_returns_404(client: TestClient) -> None:
    resp = client.delete("/providers/9999")
    assert resp.status_code == 404


# --- activate --------------------------------------------------------------


def test_activate_marks_provider_active(client: TestClient) -> None:
    created = client.post("/providers", json=_create_payload()).json()
    resp = client.post(f"/providers/{created['id']}/activate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True


def test_activate_deactivates_other_of_same_kind(
    client: TestClient, db_session: Session
) -> None:
    a = client.post(
        "/providers",
        json=_create_payload(display_name="A"),
    ).json()
    b = client.post(
        "/providers",
        json=_create_payload(display_name="B"),
    ).json()
    client.post(f"/providers/{a['id']}/activate")
    # Activating B must demote A.
    client.post(f"/providers/{b['id']}/activate")
    db_session.expire_all()
    row_a = db_session.get(ProviderCredential, a["id"])
    row_b = db_session.get(ProviderCredential, b["id"])
    assert row_a is not None
    assert row_b is not None
    assert row_a.is_active is False
    assert row_b.is_active is True


def test_activate_leaves_other_kinds_alone(
    client: TestClient, db_session: Session
) -> None:
    stt = client.post("/providers", json=_create_payload()).json()
    llm = client.post(
        "/providers",
        json=_create_payload(kind="llm", provider_name="ok-llm", display_name="L"),
    ).json()
    client.post(f"/providers/{stt['id']}/activate")
    client.post(f"/providers/{llm['id']}/activate")
    db_session.expire_all()
    row_stt = db_session.get(ProviderCredential, stt["id"])
    row_llm = db_session.get(ProviderCredential, llm["id"])
    assert row_stt is not None
    assert row_llm is not None
    assert row_stt.is_active is True
    assert row_llm.is_active is True


def test_activate_missing_returns_404(client: TestClient) -> None:
    resp = client.post("/providers/9999/activate")
    assert resp.status_code == 404


def test_deactivate_returns_inactive(client: TestClient) -> None:
    created = client.post("/providers", json=_create_payload()).json()
    client.post(f"/providers/{created['id']}/activate")
    resp = client.post(f"/providers/{created['id']}/deactivate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


def test_partial_unique_index_enforced_at_db_level(
    engine: sa.Engine, db_session: Session
) -> None:
    """Direct DB write attempting two active rows of same kind must fail."""
    from app.security.crypto import CredentialCrypto, encrypt_json

    c = CredentialCrypto(Fernet.generate_key())
    blob = encrypt_json(c, {"k": "v"})
    db_session.add(
        ProviderCredential(
            kind=ProviderKind.STT,
            provider_name="ok-stt",
            display_name="A",
            credentials_encrypted=blob,
            config={},
            is_active=True,
        )
    )
    db_session.commit()
    db_session.add(
        ProviderCredential(
            kind=ProviderKind.STT,
            provider_name="ok-stt",
            display_name="B",
            credentials_encrypted=blob,
            config={},
            is_active=True,
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        db_session.commit()


# --- test endpoint ---------------------------------------------------------


def test_smoke_stt_success(client: TestClient) -> None:
    registry = get_registry()
    registry.register(ProviderKind.STT, "ok-stt", _OKSTT)
    created = client.post("/providers", json=_create_payload()).json()
    resp = client.post(f"/providers/{created['id']}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "STT smoke OK" in body["message"]


def test_smoke_llm_success(client: TestClient) -> None:
    registry = get_registry()
    registry.register(ProviderKind.LLM, "ok-llm", _OKLLM)
    created = client.post(
        "/providers",
        json=_create_payload(kind="llm", provider_name="ok-llm", display_name="L"),
    ).json()
    resp = client.post(f"/providers/{created['id']}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "LLM smoke OK" in body["message"]
    assert body["detail"] == "hi there"


def test_smoke_tts_success(client: TestClient) -> None:
    registry = get_registry()
    registry.register(ProviderKind.TTS, "ok-tts", _OKTTS)
    created = client.post(
        "/providers",
        json=_create_payload(kind="tts", provider_name="ok-tts", display_name="T"),
    ).json()
    resp = client.post(f"/providers/{created['id']}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    # 32 bytes per yield, two yields.
    assert "64 byte" in body["message"]


def test_smoke_unknown_provider_returns_ok_false(client: TestClient) -> None:
    created = client.post(
        "/providers",
        json=_create_payload(provider_name="never-registered"),
    ).json()
    resp = client.post(f"/providers/{created['id']}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "never-registered" in body["message"]


def test_smoke_failing_stt_returns_ok_false(client: TestClient) -> None:
    registry = get_registry()
    registry.register(ProviderKind.STT, "failing-stt", _FailingSTT)
    created = client.post(
        "/providers",
        json=_create_payload(provider_name="failing-stt"),
    ).json()
    resp = client.post(f"/providers/{created['id']}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "synthetic auth failure" in (body["detail"] or "")


def test_smoke_failing_llm_returns_ok_false(client: TestClient) -> None:
    registry = get_registry()
    registry.register(ProviderKind.LLM, "failing-llm", _FailingLLM)
    created = client.post(
        "/providers",
        json=_create_payload(
            kind="llm", provider_name="failing-llm", display_name="Fail"
        ),
    ).json()
    resp = client.post(f"/providers/{created['id']}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "synthetic rate limit" in (body["detail"] or "")


def test_smoke_construction_failure_returns_ok_false(client: TestClient) -> None:
    registry = get_registry()
    registry.register(ProviderKind.STT, "crash", _CrashingFactory)
    created = client.post(
        "/providers", json=_create_payload(provider_name="crash")
    ).json()
    resp = client.post(f"/providers/{created['id']}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "synthetic init failure" in (body["detail"] or "")


def test_smoke_missing_returns_404(client: TestClient) -> None:
    resp = client.post("/providers/9999/test")
    assert resp.status_code == 404


# --- schemas endpoint and structured payloads ------------------------------


class _SchemaAwareLLM(LLMProvider):
    """A stub LLM adapter that declares a real field_schema."""

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        if not config.credentials.get("api_key"):
            raise ValueError("missing api_key")

    @property
    def name(self) -> str:
        return "schema-llm"

    @classmethod
    def field_schema(cls):  # type: ignore[no-untyped-def]
        from app.providers.schema import (
            FieldDef,
            FieldGroup,
            FieldOption,
            FieldType,
            ProviderSchema,
        )

        return ProviderSchema(
            kind=ProviderKind.LLM,
            provider_name="schema-llm",
            display_name="Test schema LLM",
            summary="A test adapter that declares a structured schema.",
            signup_url="https://example.com",
            fields=(
                FieldDef(
                    name="api_key",
                    label="API key",
                    type=FieldType.PASSWORD,
                    required=True,
                    secret=True,
                    group=FieldGroup.AUTH,
                ),
                FieldDef(
                    name="model",
                    label="Model",
                    type=FieldType.SELECT,
                    default="gpt-4o-mini",
                    options=(
                        FieldOption(value="gpt-4o-mini", label="gpt-4o-mini"),
                        FieldOption(value="gpt-4o", label="gpt-4o"),
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="temperature",
                    label="Temperature",
                    type=FieldType.NUMBER,
                    default=0.7,
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="base_url",
                    label="Base URL",
                    type=FieldType.URL,
                    group=FieldGroup.ADVANCED,
                ),
            ),
        )

    async def chat(self, messages, tools=None, response_format=None):  # type: ignore[no-untyped-def]
        return LLMResponse(text="hi", finish_reason="stop")


def test_schemas_endpoint_returns_registered_provider_schemas(client: TestClient) -> None:
    get_registry().register(ProviderKind.LLM, "schema-llm", _SchemaAwareLLM)
    resp = client.get("/providers/schemas")
    assert resp.status_code == 200
    body = resp.json()
    assert "stt" in body and "llm" in body and "tts" in body
    llm_schemas = body["llm"]
    by_name = {s["provider_name"]: s for s in llm_schemas}
    assert "schema-llm" in by_name
    schema = by_name["schema-llm"]
    assert schema["display_name"] == "Test schema LLM"
    assert schema["signup_url"] == "https://example.com"
    field_names = [f["name"] for f in schema["fields"]]
    assert "api_key" in field_names
    api_key_field = next(f for f in schema["fields"] if f["name"] == "api_key")
    assert api_key_field["secret"] is True
    assert api_key_field["required"] is True
    assert api_key_field["type"] == "password"


def test_schemas_endpoint_omits_adapters_without_schema(client: TestClient) -> None:
    # _OKLLM is registered without field_schema
    get_registry().register(ProviderKind.LLM, "ok-llm", _OKLLM)
    resp = client.get("/providers/schemas")
    assert resp.status_code == 200
    names = {s["provider_name"] for s in resp.json()["llm"]}
    assert "ok-llm" not in names


def test_create_with_values_splits_into_credentials_and_options(
    client: TestClient, db_session: Session, crypto: CredentialCrypto
) -> None:
    get_registry().register(ProviderKind.LLM, "schema-llm", _SchemaAwareLLM)
    resp = client.post(
        "/providers",
        json={
            "kind": "llm",
            "provider_name": "schema-llm",
            "display_name": "Struct",
            "values": {
                "api_key": "sk-secret",
                "model": "gpt-4o-mini",
                "temperature": "0.9",
                "base_url": "https://api.example.com/v1",
            },
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["credential_keys"] == ["api_key"]
    assert data["options"]["model"] == "gpt-4o-mini"
    assert data["options"]["temperature"] == 0.9
    assert data["options"]["base_url"] == "https://api.example.com/v1"
    # Secret never appears in the response.
    assert "sk-secret" not in resp.text

    # Check it's actually encrypted at rest under the api_key key.
    from app.security.crypto import decrypt_json

    row = db_session.get(ProviderCredential, data["id"])
    assert row is not None
    creds = decrypt_json(crypto, row.credentials_encrypted)
    assert creds == {"api_key": "sk-secret"}


def test_create_with_values_missing_required_returns_422(client: TestClient) -> None:
    get_registry().register(ProviderKind.LLM, "schema-llm", _SchemaAwareLLM)
    resp = client.post(
        "/providers",
        json={
            "kind": "llm",
            "provider_name": "schema-llm",
            "display_name": "Struct",
            "values": {"model": "gpt-4o-mini"},
        },
    )
    assert resp.status_code == 422
    body = resp.json()
    fields = {item["loc"][-1] for item in body["detail"]}
    assert "api_key" in fields


def test_create_with_values_unknown_select_option_returns_422(client: TestClient) -> None:
    get_registry().register(ProviderKind.LLM, "schema-llm", _SchemaAwareLLM)
    resp = client.post(
        "/providers",
        json={
            "kind": "llm",
            "provider_name": "schema-llm",
            "display_name": "Struct",
            "values": {"api_key": "sk-x", "model": "not-a-real-model"},
        },
    )
    assert resp.status_code == 422
    body = resp.json()
    fields = {item["loc"][-1] for item in body["detail"]}
    assert "model" in fields


def test_create_legacy_buckets_still_validated_against_schema(client: TestClient) -> None:
    """Legacy credentials/options shape still pays for schema validation."""
    get_registry().register(ProviderKind.LLM, "schema-llm", _SchemaAwareLLM)
    resp = client.post(
        "/providers",
        json={
            "kind": "llm",
            "provider_name": "schema-llm",
            "display_name": "Legacy",
            "credentials": {},  # missing required api_key
            "options": {"model": "gpt-4o-mini"},
        },
    )
    assert resp.status_code == 422


def test_update_with_values_revalidates_and_resplits(
    client: TestClient, db_session: Session, crypto: CredentialCrypto
) -> None:
    get_registry().register(ProviderKind.LLM, "schema-llm", _SchemaAwareLLM)
    created = client.post(
        "/providers",
        json={
            "kind": "llm",
            "provider_name": "schema-llm",
            "display_name": "Struct",
            "values": {"api_key": "sk-old"},
        },
    ).json()
    resp = client.patch(
        f"/providers/{created['id']}",
        json={"values": {"api_key": "sk-new", "model": "gpt-4o", "temperature": 0.2}},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["options"]["model"] == "gpt-4o"
    assert data["options"]["temperature"] == 0.2

    from app.security.crypto import decrypt_json

    row = db_session.get(ProviderCredential, created["id"])
    assert row is not None
    creds = decrypt_json(crypto, row.credentials_encrypted)
    assert creds == {"api_key": "sk-new"}


def test_create_without_schema_provider_still_works_with_legacy_payload(
    client: TestClient,
) -> None:
    """Adapters without a schema fall back to legacy behavior (no validation)."""
    get_registry().register(ProviderKind.LLM, "ok-llm", _OKLLM)
    resp = client.post(
        "/providers",
        json={
            "kind": "llm",
            "provider_name": "ok-llm",
            "display_name": "Legacy free-text",
            "credentials": {"whatever": "fine"},
            "options": {"anything": "goes"},
        },
    )
    assert resp.status_code == 201
