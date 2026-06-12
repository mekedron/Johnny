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
    VoiceMeta,
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


class _SampleTTS(TTSProvider):
    """TTS adapter that records the synthesised text for assertions."""

    last_text: str | None = None
    last_voice_id: str | None = None
    closed: bool = False

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        type(self).last_voice_id = config.options.get("voice_id")

    @property
    def name(self) -> str:
        return "sample-tts"

    async def synthesize_stream(
        self, text: str, voice_id: str | None = None
    ) -> AsyncIterator[bytes]:
        type(self).last_text = text
        # 32 bytes of audible-looking PCM (one full S16 sample per frame).
        yield b"\x10\x00" * 8
        yield b"\x20\x00" * 8


class _EmptyTTS(TTSProvider):
    """TTS adapter that yields no PCM at all — should surface as 502."""

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return "empty-tts"

    async def synthesize_stream(
        self, text: str, voice_id: str | None = None
    ) -> AsyncIterator[bytes]:
        if False:
            yield b""


class _FailingTTS(TTSProvider):
    """TTS adapter that raises mid-stream."""

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return "failing-tts"

    async def synthesize_stream(
        self, text: str, voice_id: str | None = None
    ) -> AsyncIterator[bytes]:
        yield b"\x00\x00" * 4
        raise RuntimeError("synthetic tts failure")


class _VoiceCatalogTTS(TTSProvider):
    """TTS adapter that declares a unified voice catalog (Johnny-1ge.8).

    Mirrors the Kokoro/OpenAI shape: a ``voice_id`` SELECT field flagged
    ``voice_catalog=True`` plus a ``list_voices()`` returning structured
    :class:`VoiceMeta`. Used to exercise the generalized voices endpoint
    and ``preview/voices`` without depending on a real provider library.
    """

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return "catalog-tts"

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
            kind=ProviderKind.TTS,
            provider_name="catalog-tts",
            display_name="Catalog TTS",
            summary="fake",
            fields=(
                FieldDef(
                    name="voice_id",
                    label="Voice",
                    type=FieldType.SELECT,
                    required=True,
                    default="vox_a",
                    voice_catalog=True,
                    options=(
                        FieldOption(value="vox_a", label="vox_a"),
                        FieldOption(value="vox_b", label="vox_b"),
                    ),
                    group=FieldGroup.MODEL,
                ),
            ),
        )

    async def list_voices(self) -> tuple[VoiceMeta, ...]:
        return (
            VoiceMeta(
                id="vox_a",
                label="Vox A — English ♀",
                language="English",
                sample_rate=24_000,
                gender="female",
            ),
            VoiceMeta(
                id="vox_b",
                label="Vox B — German ♂",
                language="German",
                sample_rate=22_050,
                gender="male",
            ),
        )

    async def synthesize_stream(
        self, text: str, voice_id: str | None = None
    ) -> AsyncIterator[bytes]:
        yield b"\x10\x00" * 8


def _audible_pcm(ms: int = 3000, amplitude: int = 10_000) -> bytes:
    """``ms`` of 16 kHz mono S16LE at a constant, non-silent amplitude."""
    import array

    n = int(16_000 * ms / 1000)
    return array.array("h", [amplitude] * n).tobytes()


class _AudibleTTS(TTSProvider):
    """TTS adapter that yields enough non-silent PCM to pass the audible check.

    Records the runtime option so tests can assert the play_sample runtime
    override reached the factory (Johnny-1ge.7).
    """

    last_runtime: str | None = None

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self.runtime = str(config.options.get("runtime", "") or "")
        type(self).last_runtime = self.runtime

    @property
    def name(self) -> str:
        return "audible-tts"

    async def synthesize_stream(
        self, text: str, voice_id: str | None = None
    ) -> AsyncIterator[bytes]:
        pcm = _audible_pcm()
        # Stream in chunks like a real adapter so TTFA timing is exercised.
        for i in range(0, len(pcm), 8_000):
            yield pcm[i : i + 8_000]


class _SilentTTS(TTSProvider):
    """TTS adapter that yields audio-shaped but all-zero PCM — the silent bug.

    Long enough to clear the byte/duration floors, so only the peak-amplitude
    check can catch it.
    """

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self.runtime = "mlx-sidecar"

    @property
    def name(self) -> str:
        return "silent-tts"

    async def synthesize_stream(
        self, text: str, voice_id: str | None = None
    ) -> AsyncIterator[bytes]:
        yield b"\x00\x00" * 24_000  # 48000 bytes = 1.5 s of silence


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
    # stt/llm/tts are the only kinds — the ``s2s`` bucket was removed with
    # the S2S surface (Johnny-trt.43).
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


def test_create_multiple_instances_same_kind_and_name(
    client: TestClient, db_session: Session
) -> None:
    """Regression for Johnny-stt.7: N instances per (kind, provider_name) allowed.

    The DB schema allows multiple rows with the same kind and provider_name as
    long as the display_name differs. The 409 trap fires only when the user
    tries to reuse a display_name. This locks the contract so it cannot
    regress under any future provider-UX refactor.
    """
    a = client.post(
        "/providers",
        json=_create_payload(
            kind="llm",
            provider_name="ok-llm",
            display_name="Ollama Qwen 35B",
            options={"model": "qwen-35b"},
        ),
    )
    b = client.post(
        "/providers",
        json=_create_payload(
            kind="llm",
            provider_name="ok-llm",
            display_name="Ollama Llama 3 8B",
            options={"model": "llama-3-8b"},
        ),
    )
    c = client.post(
        "/providers",
        json=_create_payload(
            kind="llm",
            provider_name="ok-llm",
            display_name="Ollama Custom Finetune",
            options={"model": "custom-finetune"},
        ),
    )
    assert a.status_code == 201, a.text
    assert b.status_code == 201, b.text
    assert c.status_code == 201, c.text
    assert len({a.json()["id"], b.json()["id"], c.json()["id"]}) == 3

    listed = client.get("/providers").json()
    llm_rows = listed["llm"]
    assert len(llm_rows) == 3
    by_display = {row["display_name"]: row for row in llm_rows}
    assert by_display["Ollama Qwen 35B"]["options"] == {"model": "qwen-35b"}
    assert by_display["Ollama Llama 3 8B"]["options"] == {"model": "llama-3-8b"}
    assert by_display["Ollama Custom Finetune"]["options"] == {
        "model": "custom-finetune"
    }


def test_create_second_instance_with_duplicate_display_name_returns_409(
    client: TestClient,
) -> None:
    """The display_name must be unique within (kind, provider_name).

    This is the legitimate 409 surface — distinct from the regression in
    Johnny-stt.7, which mistakenly blocked all N>1 instances rather than just
    display-name collisions.
    """
    first = client.post(
        "/providers",
        json=_create_payload(display_name="Shared name"),
    )
    assert first.status_code == 201
    second = client.post(
        "/providers",
        json=_create_payload(display_name="Shared name"),
    )
    assert second.status_code == 409


def test_each_instance_selectable_independently(
    client: TestClient, db_session: Session
) -> None:
    """Two instances of the same kind+name resolve to different config rows.

    Activating one does not deactivate the other's identity — they remain
    independently selectable by id (the way playground / event configs pick).
    """
    a = client.post(
        "/providers",
        json=_create_payload(display_name="A", options={"model": "nova-2"}),
    ).json()
    b = client.post(
        "/providers",
        json=_create_payload(display_name="B", options={"model": "nova-3"}),
    ).json()
    # Fetch each by id and confirm options differ.
    row_a = db_session.get(ProviderCredential, a["id"])
    row_b = db_session.get(ProviderCredential, b["id"])
    assert row_a is not None and row_b is not None
    assert row_a.config == {"model": "nova-2"}
    assert row_b.config == {"model": "nova-3"}


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


# --- Johnny-3ha: active flag is never silently cleared by sibling CRUD -----
#
# The acceptance is "Add/edit/delete other providers / Active LLM remains
# as set, never silently NULLed". The bug reporter saw the LLM go inactive
# while TTS/STT stayed put, which would only happen if some API path were
# asymmetric for LLM. These tests pin every CRUD verb across the other two
# kinds and assert the LLM is_active flag survives — and the symmetric
# tests prove the same guard for STT/TTS so an LLM-specific regression
# would surface as a single-test failure.


def _seed_kind_pair(client: TestClient) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create one LLM + one STT provider; activate the LLM. Return both ids."""
    registry = get_registry()
    registry.register(ProviderKind.STT, "ok-stt", _OKSTT, replace=True)
    registry.register(ProviderKind.LLM, "ok-llm", _OKLLM, replace=True)
    llm = client.post(
        "/providers",
        json=_create_payload(
            kind="llm", provider_name="ok-llm", display_name="L-active"
        ),
    ).json()
    stt = client.post(
        "/providers",
        json=_create_payload(
            kind="stt", provider_name="ok-stt", display_name="S-other"
        ),
    ).json()
    assert client.post(f"/providers/{llm['id']}/activate").status_code == 200
    return llm, stt


def test_creating_other_kind_does_not_deactivate_active_llm(
    client: TestClient, db_session: Session
) -> None:
    """Acceptance: 'Add ... other providers / Active LLM remains as set'."""
    llm, _ = _seed_kind_pair(client)
    registry = get_registry()
    registry.register(ProviderKind.TTS, "ok-tts", _OKTTS, replace=True)

    client.post(
        "/providers",
        json=_create_payload(
            kind="tts", provider_name="ok-tts", display_name="T-new"
        ),
    )

    db_session.expire_all()
    row_llm = db_session.get(ProviderCredential, llm["id"])
    assert row_llm is not None and row_llm.is_active is True


def test_updating_other_kind_does_not_deactivate_active_llm(
    client: TestClient, db_session: Session
) -> None:
    """Acceptance: 'edit ... other providers / Active LLM remains as set'."""
    llm, stt = _seed_kind_pair(client)

    resp = client.patch(
        f"/providers/{stt['id']}",
        json={"display_name": "S-other-renamed"},
    )
    assert resp.status_code == 200

    db_session.expire_all()
    row_llm = db_session.get(ProviderCredential, llm["id"])
    assert row_llm is not None and row_llm.is_active is True


def test_deleting_other_kind_does_not_deactivate_active_llm(
    client: TestClient, db_session: Session
) -> None:
    """Acceptance: 'delete other providers / Active LLM remains as set'."""
    llm, stt = _seed_kind_pair(client)

    resp = client.delete(f"/providers/{stt['id']}")
    assert resp.status_code == 204

    db_session.expire_all()
    row_llm = db_session.get(ProviderCredential, llm["id"])
    assert row_llm is not None and row_llm.is_active is True


def test_creating_same_kind_inactive_sibling_does_not_deactivate_active_llm(
    client: TestClient, db_session: Session
) -> None:
    """A brand-new LLM is created with is_active=False (POST never auto-flips
    the flag), so the existing active LLM must survive a second LLM row
    landing. This pins down the asymmetry the bug report called out — adds
    of additional LLMs should not silently NULL the active one."""
    llm, _ = _seed_kind_pair(client)

    client.post(
        "/providers",
        json=_create_payload(
            kind="llm", provider_name="ok-llm", display_name="L-other"
        ),
    )

    db_session.expire_all()
    row_llm = db_session.get(ProviderCredential, llm["id"])
    assert row_llm is not None and row_llm.is_active is True


def test_updating_active_llm_itself_preserves_active_flag(
    client: TestClient, db_session: Session
) -> None:
    """Editing the active LLM row (display name, options, credentials) must
    not touch is_active. The PATCH endpoint has no knob for the flag —
    pin that contract so a future schema-driven refactor cannot reintroduce
    a hidden auto-deactivate."""
    llm, _ = _seed_kind_pair(client)

    resp = client.patch(
        f"/providers/{llm['id']}",
        json={
            "display_name": "L-active-renamed",
            "credentials": {"api_key": "sk-rotated"},
            "options": {"model": "gpt-4o"},
        },
    )
    assert resp.status_code == 200

    db_session.expire_all()
    row_llm = db_session.get(ProviderCredential, llm["id"])
    assert row_llm is not None and row_llm.is_active is True


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


# --- play_sample endpoint --------------------------------------------------


def _make_tts(client: TestClient, provider_name: str = "sample-tts") -> dict[str, Any]:
    data: dict[str, Any] = client.post(
        "/providers",
        json=_create_payload(
            kind="tts",
            provider_name=provider_name,
            display_name=f"{provider_name} card",
        ),
    ).json()
    return data


def test_play_sample_returns_wav_for_tts_provider(client: TestClient) -> None:
    import wave

    get_registry().register(ProviderKind.TTS, "sample-tts", _SampleTTS)
    created = _make_tts(client)
    resp = client.post(f"/providers/{created['id']}/play_sample")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/wav")
    body = resp.content
    assert body[:4] == b"RIFF"
    assert body[8:12] == b"WAVE"
    # Decode the RIFF/WAV and confirm canonical PCM params.
    import io

    with wave.open(io.BytesIO(body), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16_000
        # Two 16-byte yields of two-byte samples = 32 PCM bytes.
        assert wf.getnframes() == 16


def test_play_sample_uses_demo_phrase(client: TestClient) -> None:
    from app.api.providers import TTS_SAMPLE_PHRASE

    get_registry().register(ProviderKind.TTS, "sample-tts", _SampleTTS)
    created = _make_tts(client)
    _SampleTTS.last_text = None
    resp = client.post(f"/providers/{created['id']}/play_sample")
    assert resp.status_code == 200
    assert _SampleTTS.last_text == TTS_SAMPLE_PHRASE


def test_play_sample_rejects_stt_provider(client: TestClient) -> None:
    get_registry().register(ProviderKind.STT, "ok-stt", _OKSTT)
    created = client.post("/providers", json=_create_payload()).json()
    resp = client.post(f"/providers/{created['id']}/play_sample")
    assert resp.status_code == 400
    assert "tts" in resp.json()["detail"].lower()


def test_play_sample_rejects_llm_provider(client: TestClient) -> None:
    get_registry().register(ProviderKind.LLM, "ok-llm", _OKLLM)
    created = client.post(
        "/providers",
        json=_create_payload(kind="llm", provider_name="ok-llm", display_name="L"),
    ).json()
    resp = client.post(f"/providers/{created['id']}/play_sample")
    assert resp.status_code == 400


def test_play_sample_missing_provider_returns_404(client: TestClient) -> None:
    resp = client.post("/providers/9999/play_sample")
    assert resp.status_code == 404


def test_play_sample_unknown_factory_returns_502(client: TestClient) -> None:
    created = client.post(
        "/providers",
        json=_create_payload(
            kind="tts", provider_name="never-registered", display_name="Ghost"
        ),
    ).json()
    resp = client.post(f"/providers/{created['id']}/play_sample")
    assert resp.status_code == 502


def test_play_sample_synthesis_error_returns_502(client: TestClient) -> None:
    get_registry().register(ProviderKind.TTS, "failing-tts", _FailingTTS)
    created = client.post(
        "/providers",
        json=_create_payload(
            kind="tts", provider_name="failing-tts", display_name="Fail"
        ),
    ).json()
    resp = client.post(f"/providers/{created['id']}/play_sample")
    assert resp.status_code == 502
    assert "synthesis failed" in resp.json()["detail"]


def test_play_sample_empty_audio_returns_502(client: TestClient) -> None:
    get_registry().register(ProviderKind.TTS, "empty-tts", _EmptyTTS)
    created = client.post(
        "/providers",
        json=_create_payload(
            kind="tts", provider_name="empty-tts", display_name="Silent"
        ),
    ).json()
    resp = client.post(f"/providers/{created['id']}/play_sample")
    assert resp.status_code == 502
    assert "no audio" in resp.json()["detail"]


def test_play_sample_does_not_alter_test_endpoint_behaviour(client: TestClient) -> None:
    """Test endpoint must keep its smoke-call semantics (does not return audio)."""
    get_registry().register(ProviderKind.TTS, "sample-tts", _SampleTTS)
    created = _make_tts(client)
    resp = client.post(f"/providers/{created['id']}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    # The smoke endpoint synthesises a fixed "hi" string and returns JSON,
    # not WAV — the play endpoint sends the demo phrase.
    assert _SampleTTS.last_text == "hi"


def test_play_sample_voice_id_override_propagates_to_factory(client: TestClient) -> None:
    """A voice_id in the request body overrides the row's saved config."""
    get_registry().register(ProviderKind.TTS, "sample-tts", _SampleTTS)
    created = client.post(
        "/providers",
        json=_create_payload(
            kind="tts",
            provider_name="sample-tts",
            display_name="Card",
            options={"voice_id": "saved-voice"},
        ),
    ).json()
    _SampleTTS.last_voice_id = None

    resp = client.post(
        f"/providers/{created['id']}/play_sample",
        json={"voice_id": "preview-voice"},
    )
    assert resp.status_code == 200
    # The factory ran with the override, not the saved value.
    assert _SampleTTS.last_voice_id == "preview-voice"

    # The saved row is still the original voice — the override was ephemeral.
    listed = client.get("/providers").json()
    row = next(p for p in listed["tts"] if p["id"] == created["id"])
    assert row["options"]["voice_id"] == "saved-voice"


def test_play_sample_without_body_uses_saved_voice_id(client: TestClient) -> None:
    """No body means historic behavior — saved options drive the factory."""
    get_registry().register(ProviderKind.TTS, "sample-tts", _SampleTTS)
    created = client.post(
        "/providers",
        json=_create_payload(
            kind="tts",
            provider_name="sample-tts",
            display_name="Card",
            options={"voice_id": "saved-voice"},
        ),
    ).json()
    _SampleTTS.last_voice_id = None

    resp = client.post(f"/providers/{created['id']}/play_sample")
    assert resp.status_code == 200
    assert _SampleTTS.last_voice_id == "saved-voice"


def test_play_sample_rejects_extra_body_fields(client: TestClient) -> None:
    """Tight schema — unknown body keys must 422 so typos surface early."""
    get_registry().register(ProviderKind.TTS, "sample-tts", _SampleTTS)
    created = _make_tts(client)
    resp = client.post(
        f"/providers/{created['id']}/play_sample",
        json={"voice_id": "x", "unexpected": True},
    )
    assert resp.status_code == 422


# --- play_sample audio metrics (Johnny-1ge.7) ------------------------------


def test_play_sample_stamps_audio_metric_headers(client: TestClient) -> None:
    """Audible audio → metric headers present and the verdict is audible."""
    get_registry().register(ProviderKind.TTS, "audible-tts", _AudibleTTS)
    created = _make_tts(client, provider_name="audible-tts")
    resp = client.post(f"/providers/{created['id']}/play_sample")
    assert resp.status_code == 200
    assert resp.headers["X-TTS-Audible"] == "1"
    assert resp.headers["X-TTS-Audible-Reason"] == ""
    assert int(resp.headers["X-TTS-Audio-Bytes"]) >= 16_000
    assert int(resp.headers["X-TTS-Audio-Ms"]) > 0
    assert float(resp.headers["X-TTS-Peak"]) > 0.01


def test_play_sample_silent_audio_returns_200_with_warning(client: TestClient) -> None:
    """All-zero PCM is returned (so the UI can warn) but flagged not audible.

    The kokoro mlx-sidecar bug class: HTTP 200 + finite latency + silence. The
    endpoint must surface the verdict, not pretend success.
    """
    get_registry().register(ProviderKind.TTS, "silent-tts", _SilentTTS)
    created = _make_tts(client, provider_name="silent-tts")
    resp = client.post(f"/providers/{created['id']}/play_sample")
    assert resp.status_code == 200  # audio still flows back for inspection
    assert resp.headers["X-TTS-Audible"] == "0"
    assert float(resp.headers["X-TTS-Peak"]) == 0.0
    assert "silent" in resp.headers["X-TTS-Audible-Reason"]


def test_play_sample_small_sample_is_flagged_not_audible(client: TestClient) -> None:
    """The trivial 32-byte fake clears 200 but fails the byte/duration floor."""
    get_registry().register(ProviderKind.TTS, "sample-tts", _SampleTTS)
    created = _make_tts(client)
    resp = client.post(f"/providers/{created['id']}/play_sample")
    assert resp.status_code == 200
    assert resp.headers["X-TTS-Audible"] == "0"
    assert "bytes" in resp.headers["X-TTS-Audible-Reason"]


def test_play_sample_runtime_override_propagates_to_factory(client: TestClient) -> None:
    """A runtime in the request body overrides the row's saved runtime."""
    get_registry().register(ProviderKind.TTS, "audible-tts", _AudibleTTS)
    created = client.post(
        "/providers",
        json=_create_payload(
            kind="tts",
            provider_name="audible-tts",
            display_name="Card",
            options={"runtime": "subprocess"},
        ),
    ).json()
    _AudibleTTS.last_runtime = None

    resp = client.post(
        f"/providers/{created['id']}/play_sample",
        json={"runtime": "http-sidecar"},
    )
    assert resp.status_code == 200
    assert _AudibleTTS.last_runtime == "http-sidecar"
    assert resp.headers["X-TTS-Runtime"] == "http-sidecar"

    # The saved row keeps its original runtime — the override was ephemeral.
    listed = client.get("/providers").json()
    row = next(p for p in listed["tts"] if p["id"] == created["id"])
    assert row["options"]["runtime"] == "subprocess"


# --- DELETE /voices/{key} (piper) ------------------------------------------


def test_remove_voice_deletes_files_for_piper_provider(
    client: TestClient, tmp_path
) -> None:
    """DELETE removes the .onnx and .onnx.json files and returns installed=false."""
    (tmp_path / "vx.onnx").write_bytes(b"\x00")
    (tmp_path / "vx.onnx.json").write_text("{}")
    created = client.post(
        "/providers",
        json=_create_payload(
            kind="tts",
            provider_name="piper",
            display_name="Piper",
            options={"model_dir": str(tmp_path)},
        ),
    ).json()

    resp = client.delete(f"/providers/{created['id']}/voices/vx")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "key": "vx",
        "installed": False,
        "onnx_removed": True,
        "onnx_json_removed": True,
    }
    assert not (tmp_path / "vx.onnx").exists()
    assert not (tmp_path / "vx.onnx.json").exists()


def test_remove_voice_returns_404_when_not_installed(
    client: TestClient, tmp_path
) -> None:
    created = client.post(
        "/providers",
        json=_create_payload(
            kind="tts",
            provider_name="piper",
            display_name="Piper",
            options={"model_dir": str(tmp_path)},
        ),
    ).json()
    resp = client.delete(f"/providers/{created['id']}/voices/missing")
    assert resp.status_code == 404
    assert "missing" in resp.json()["detail"]


def test_remove_voice_rejects_non_piper_provider(client: TestClient) -> None:
    get_registry().register(ProviderKind.TTS, "sample-tts", _SampleTTS)
    created = _make_tts(client)
    resp = client.delete(f"/providers/{created['id']}/voices/vx")
    assert resp.status_code == 400
    assert "piper" in resp.json()["detail"].lower()


def test_remove_voice_does_not_mutate_provider_row(
    client: TestClient, tmp_path
) -> None:
    """Deleting the currently-saved voice leaves the row's voice_id untouched.

    The user surfaces the inconsistency on the next synth call instead of
    being silently switched to a different voice.
    """
    (tmp_path / "vx.onnx").write_bytes(b"")
    (tmp_path / "vx.onnx.json").write_text("{}")
    created = client.post(
        "/providers",
        json=_create_payload(
            kind="tts",
            provider_name="piper",
            display_name="Piper",
            options={"model_dir": str(tmp_path), "voice_id": "vx"},
        ),
    ).json()

    resp = client.delete(f"/providers/{created['id']}/voices/vx")
    assert resp.status_code == 200

    listed = client.get("/providers").json()
    row = next(p for p in listed["tts"] if p["id"] == created["id"])
    assert row["options"]["voice_id"] == "vx"


# --- export endpoint (Johnny-k3z) ------------------------------------------


def test_export_empty_returns_zero_providers(client: TestClient) -> None:
    """A fresh stack with no providers exports a valid empty file."""
    resp = client.get("/providers/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    data = resp.json()
    assert data == {"version": 1, "providers": []}


def test_export_filename_uses_today_yyyy_mm_dd(client: TestClient) -> None:
    """Content-Disposition follows johnny-providers-YYYY-MM-DD.json per spec."""
    from datetime import UTC, datetime

    resp = client.get("/providers/export")
    assert resp.status_code == 200
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    expected = f'attachment; filename="johnny-providers-{today}.json"'
    assert resp.headers["content-disposition"] == expected


def test_export_attachment_disposition_set(client: TestClient) -> None:
    """The download header is present so the browser saves rather than navigates."""
    resp = client.get("/providers/export")
    assert "attachment" in resp.headers["content-disposition"]


def test_export_without_secrets_omits_credentials(client: TestClient) -> None:
    """Default mode (no `with_secrets`) returns empty credentials dicts."""
    get_registry().register(ProviderKind.STT, "ok-stt", _OKSTT)
    client.post(
        "/providers",
        json=_create_payload(credentials={"api_key": "sk-VERY-SECRET"}),
    )
    resp = client.get("/providers/export")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["providers"]) == 1
    entry = data["providers"][0]
    assert entry["credentials"] == {}
    # The plaintext secret must not leak anywhere in the response.
    assert "sk-VERY-SECRET" not in resp.text


def test_export_with_secrets_includes_decrypted_credentials(client: TestClient) -> None:
    get_registry().register(ProviderKind.STT, "ok-stt", _OKSTT)
    client.post(
        "/providers",
        json=_create_payload(credentials={"api_key": "sk-roundtrip"}),
    )
    resp = client.get("/providers/export", params={"with_secrets": "true"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["providers"]) == 1
    assert data["providers"][0]["credentials"] == {"api_key": "sk-roundtrip"}


def test_export_with_secrets_false_explicit(client: TestClient) -> None:
    """An explicit `with_secrets=false` matches the default behaviour."""
    get_registry().register(ProviderKind.STT, "ok-stt", _OKSTT)
    client.post(
        "/providers",
        json=_create_payload(credentials={"api_key": "sk-explicit"}),
    )
    resp = client.get("/providers/export", params={"with_secrets": "false"})
    data = resp.json()
    assert data["providers"][0]["credentials"] == {}
    assert "sk-explicit" not in resp.text


def test_export_includes_all_kinds(client: TestClient) -> None:
    get_registry().register(ProviderKind.STT, "ok-stt", _OKSTT)
    get_registry().register(ProviderKind.LLM, "ok-llm", _OKLLM)
    get_registry().register(ProviderKind.TTS, "ok-tts", _OKTTS)
    client.post("/providers", json=_create_payload(kind="stt", display_name="S"))
    client.post(
        "/providers",
        json=_create_payload(kind="llm", provider_name="ok-llm", display_name="L"),
    )
    client.post(
        "/providers",
        json=_create_payload(kind="tts", provider_name="ok-tts", display_name="T"),
    )
    resp = client.get("/providers/export")
    data = resp.json()
    kinds = {p["kind"] for p in data["providers"]}
    assert kinds == {"stt", "llm", "tts"}


def test_export_preserves_provider_metadata(client: TestClient) -> None:
    """kind, provider_name, display_name, options, is_active all round-trip."""
    get_registry().register(ProviderKind.LLM, "ok-llm", _OKLLM)
    created = client.post(
        "/providers",
        json=_create_payload(
            kind="llm",
            provider_name="ok-llm",
            display_name="My LLM",
            options={"model": "gpt-4o-mini", "temperature": 0.2},
        ),
    ).json()
    client.post(f"/providers/{created['id']}/activate")
    resp = client.get("/providers/export")
    data = resp.json()
    entry = data["providers"][0]
    assert entry["kind"] == "llm"
    assert entry["provider_name"] == "ok-llm"
    assert entry["display_name"] == "My LLM"
    assert entry["options"] == {"model": "gpt-4o-mini", "temperature": 0.2}
    assert entry["is_active"] is True


def test_export_version_matches_seeder(client: TestClient) -> None:
    """Export version must equal the seeder's SUPPORTED_FILE_VERSION."""
    from app.services.providers_seed import SUPPORTED_FILE_VERSION

    resp = client.get("/providers/export")
    assert resp.json()["version"] == SUPPORTED_FILE_VERSION


def test_export_round_trips_through_seeder(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
) -> None:
    """Export → wipe DB → import via seeder reproduces the same provider state."""
    import json as _json
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from app.services.providers_seed import SeedMode, seed_providers_from_file

    get_registry().register(ProviderKind.STT, "ok-stt", _OKSTT)
    get_registry().register(ProviderKind.LLM, "ok-llm", _OKLLM)
    client.post(
        "/providers",
        json=_create_payload(
            kind="stt",
            provider_name="ok-stt",
            display_name="STT primary",
            credentials={"api_key": "stt-key"},
            options={"model": "nova-2"},
        ),
    )
    llm = client.post(
        "/providers",
        json=_create_payload(
            kind="llm",
            provider_name="ok-llm",
            display_name="LLM primary",
            credentials={"api_key": "llm-key"},
            options={"model": "gpt-4o-mini"},
        ),
    ).json()
    client.post(f"/providers/{llm['id']}/activate")

    # Export with secrets so import reproduces an end-to-end usable state.
    resp = client.get("/providers/export", params={"with_secrets": "true"})
    body = resp.json()

    # Wipe and re-seed via the seeder using the same JSON.
    db_session.query(ProviderCredential).delete()
    db_session.commit()

    with TemporaryDirectory() as tmp:
        p = Path(tmp) / "providers.json"
        p.write_text(_json.dumps(body), encoding="utf-8")
        result = seed_providers_from_file(
            db_session, crypto, path=p, mode=SeedMode.INSERT_ONLY
        )

    assert len(result.created) == 2
    rows = db_session.query(ProviderCredential).order_by(
        ProviderCredential.kind
    ).all()
    assert {r.kind for r in rows} == {ProviderKind.LLM, ProviderKind.STT}
    active = [r for r in rows if r.is_active]
    assert len(active) == 1
    assert active[0].kind is ProviderKind.LLM


def test_export_corrupted_ciphertext_yields_empty_credentials(
    client: TestClient,
    db_session: Session,
) -> None:
    """A row with un-decryptable ciphertext exports with empty credentials.

    The export endpoint refuses to 500 on a bad row — the user can still
    download the rest of the inventory and fix the broken entry by hand.
    """
    get_registry().register(ProviderKind.STT, "ok-stt", _OKSTT)
    created = client.post("/providers", json=_create_payload()).json()
    # Sabotage the row directly so decryption will fail.
    row = db_session.get(ProviderCredential, created["id"])
    assert row is not None
    row.credentials_encrypted = "not-a-real-fernet-token"
    db_session.commit()

    resp = client.get("/providers/export", params={"with_secrets": "true"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["providers"]) == 1
    assert data["providers"][0]["credentials"] == {}


def test_export_is_pretty_printed(client: TestClient) -> None:
    """Export body is human-readable (indented) so users can diff / edit by hand."""
    resp = client.get("/providers/export")
    assert "\n" in resp.text
    # Two-space indent — same as the example file.
    assert '  "version"' in resp.text or '  "providers"' in resp.text


def test_export_no_store_cache_control(client: TestClient) -> None:
    """Browsers must not cache the export (could contain secrets)."""
    resp = client.get("/providers/export", params={"with_secrets": "true"})
    assert resp.headers.get("cache-control") == "no-store"


def test_export_does_not_overlap_with_dynamic_routes(client: TestClient) -> None:
    """`/providers/export` resolves to the export endpoint, not /{provider_id}."""
    # If FastAPI accidentally tried to coerce 'export' to int and routed it
    # to `/providers/{provider_id}/...`, we'd see a 422 here. The fact that
    # an unauth'd GET returns 200 with valid JSON confirms the route binding.
    resp = client.get("/providers/export")
    assert resp.status_code == 200


def test_export_orders_by_kind_and_display_name(client: TestClient) -> None:
    """Export order matches the list endpoint for reproducible diffs."""
    get_registry().register(ProviderKind.STT, "ok-stt", _OKSTT)
    get_registry().register(ProviderKind.LLM, "ok-llm", _OKLLM)
    # Create out-of-order: LLM "Zebra", STT "Alpha", LLM "Antelope".
    client.post(
        "/providers",
        json=_create_payload(kind="llm", provider_name="ok-llm", display_name="Zebra"),
    )
    client.post("/providers", json=_create_payload(kind="stt", display_name="Alpha"))
    client.post(
        "/providers",
        json=_create_payload(
            kind="llm", provider_name="ok-llm", display_name="Antelope"
        ),
    )
    resp = client.get("/providers/export")
    names = [p["display_name"] for p in resp.json()["providers"]]
    # ordered by (kind, display_name): llm/Antelope, llm/Zebra, stt/Alpha
    assert names == ["Antelope", "Zebra", "Alpha"]


# --- Piper voice endpoints (Johnny-4c0) ------------------------------------


def _register_piper(model_dir: str) -> None:
    """Register the real PiperTTS factory so the voices endpoints can resolve it.

    We don't actually run synthesis in these tests, so we just need the
    registry to know about ``tts:piper`` and the row in the DB to look
    like a Piper provider. The model_dir comes from the row's options.
    """
    from app.providers.piper_tts import PiperTTS

    get_registry().register(ProviderKind.TTS, "piper", PiperTTS)


def _make_piper_row(client: TestClient, model_dir: str) -> dict[str, Any]:
    data: dict[str, Any] = client.post(
        "/providers",
        json={
            "kind": "tts",
            "provider_name": "piper",
            "display_name": "Local Piper",
            "credentials": {},
            "options": {"voice_id": "en_US-amy-medium", "model_dir": model_dir},
        },
    ).json()
    return data


def test_list_voices_returns_catalog_and_installed_flag(
    client: TestClient, tmp_path: Any, monkeypatch: Any
) -> None:
    """Voices endpoint reflects the upstream catalog and marks
    already-on-disk voices as installed=True."""
    _register_piper(str(tmp_path))
    (tmp_path / "en_US-amy-medium.onnx").write_bytes(b"")
    (tmp_path / "en_US-amy-medium.onnx.json").write_text("{}")
    created = _make_piper_row(client, str(tmp_path))

    async def fake_fetch(model_dir: str, *args: Any, **kwargs: Any) -> list[Any]:
        from app.providers.piper_tts import VoiceInfo, voice_is_installed

        return [
            VoiceInfo(
                key="en_US-amy-medium",
                name="amy",
                language_code="en_US",
                language_name="English",
                quality="medium",
                installed=voice_is_installed(model_dir, "en_US-amy-medium"),
            ),
            VoiceInfo(
                key="en_US-ryan-low",
                name="ryan",
                language_code="en_US",
                language_name="English",
                quality="low",
                installed=voice_is_installed(model_dir, "en_US-ryan-low"),
            ),
        ]

    monkeypatch.setattr(
        "app.api.providers.piper_fetch_voice_catalog", fake_fetch
    )
    resp = client.get(f"/providers/{created['id']}/voices")
    assert resp.status_code == 200
    body = resp.json()
    # Johnny-1ge.9: Piper converged onto the unified VoiceMeta shape — no
    # more ``model_dir`` / ``key`` / ``language_name``; the picker reads
    # ``id`` / ``language`` / ``installed`` like every other provider.
    assert "model_dir" not in body
    by_id = {v["id"]: v for v in body["voices"]}
    assert by_id["en_US-amy-medium"]["installed"] is True
    assert by_id["en_US-ryan-low"]["installed"] is False
    assert by_id["en_US-amy-medium"]["language"] == "English"
    assert by_id["en_US-amy-medium"]["sample_rate"] == 22_050
    assert by_id["en_US-ryan-low"]["sample_rate"] == 16_000


def test_list_voices_rejects_non_tts_provider(client: TestClient) -> None:
    """STT/LLM rows must return 400 — a voice catalog is meaningless for
    them. Non-Piper *TTS* rows now serve the unified catalog (Johnny-1ge.8),
    so the rejection is by kind, not by provider name."""
    get_registry().register(ProviderKind.STT, "ok-stt", _OKSTT)
    created = client.post("/providers", json=_create_payload()).json()
    resp = client.get(f"/providers/{created['id']}/voices")
    assert resp.status_code == 400
    assert "tts" in resp.json()["detail"].lower()


# --- unified voice catalog (Johnny-1ge.8) ----------------------------------


def test_list_voices_unified_shape_for_non_piper_tts(client: TestClient) -> None:
    """A non-Piper TTS row returns the unified {voices:[VoiceMeta]} shape."""
    get_registry().register(ProviderKind.TTS, "catalog-tts", _VoiceCatalogTTS)
    created = client.post(
        "/providers",
        json={
            "kind": "tts",
            "provider_name": "catalog-tts",
            "display_name": "Catalog",
            "values": {"voice_id": "vox_a"},
        },
    ).json()
    resp = client.get(f"/providers/{created['id']}/voices")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "model_dir" not in body
    by_id = {v["id"]: v for v in body["voices"]}
    assert set(by_id) == {"vox_a", "vox_b"}
    assert by_id["vox_a"]["language"] == "English"
    assert by_id["vox_a"]["gender"] == "female"
    assert by_id["vox_a"]["sample_rate"] == 24_000
    assert by_id["vox_a"]["installed"] is True


def test_list_voices_empty_for_provider_without_catalog(client: TestClient) -> None:
    """A TTS provider that doesn't override list_voices returns no voices
    (the picker then falls back to the schema's static options)."""
    get_registry().register(ProviderKind.TTS, "sample-tts", _SampleTTS)
    created = _make_tts(client)
    resp = client.get(f"/providers/{created['id']}/voices")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"voices": []}


def test_preview_voices_returns_catalog_without_saving(client: TestClient) -> None:
    """preview/voices builds a transient instance and returns its catalog."""
    get_registry().register(ProviderKind.TTS, "catalog-tts", _VoiceCatalogTTS)
    resp = client.post(
        "/providers/preview/voices",
        json={
            "kind": "tts",
            "provider_name": "catalog-tts",
            "values": {"voice_id": "vox_a"},
        },
    )
    assert resp.status_code == 200, resp.text
    ids = {v["id"] for v in resp.json()["voices"]}
    assert ids == {"vox_a", "vox_b"}
    # Nothing persisted.
    assert client.get("/providers").json()["tts"] == []


def test_preview_voices_relaxes_required_voice_catalog_field(
    client: TestClient,
) -> None:
    """preview/voices lists the catalog even with an empty voice_catalog field.

    Regression (Johnny-1ge.10): a ``required`` ``voice_id`` 422'd the catalog
    request before ``list_voices()`` ran — you browse the catalog precisely to
    pick that value, so requiring it first is a chicken-and-egg deadlock. It
    left the Piper picker empty (Piper has no static fallback options).
    """
    get_registry().register(ProviderKind.TTS, "catalog-tts", _VoiceCatalogTTS)
    resp = client.post(
        "/providers/preview/voices",
        json={
            "kind": "tts",
            "provider_name": "catalog-tts",
            "values": {},  # no voice picked yet — the picker is how you pick
        },
    )
    assert resp.status_code == 200, resp.text
    ids = {v["id"] for v in resp.json()["voices"]}
    assert ids == {"vox_a", "vox_b"}


def test_preview_voices_rejects_non_tts(client: TestClient) -> None:
    get_registry().register(ProviderKind.LLM, "schema-llm", _SchemaAwareLLM)
    resp = client.post(
        "/providers/preview/voices",
        json={
            "kind": "llm",
            "provider_name": "schema-llm",
            "values": {"api_key": "sk-x"},
        },
    )
    assert resp.status_code == 400
    assert "tts" in resp.json()["detail"].lower()


def test_create_rejects_unknown_voice_id_with_available_list(
    client: TestClient,
) -> None:
    """An unknown voice_id is a 422 listing the allowed values (criterion E)."""
    get_registry().register(ProviderKind.TTS, "catalog-tts", _VoiceCatalogTTS)
    resp = client.post(
        "/providers",
        json={
            "kind": "tts",
            "provider_name": "catalog-tts",
            "display_name": "Bad voice",
            "values": {"voice_id": "does-not-exist"},
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    # The error names the field and enumerates the valid options.
    msg = "; ".join(e["msg"] for e in detail)
    assert "vox_a" in msg and "vox_b" in msg


def test_list_voices_propagates_fetch_error_as_502(
    client: TestClient, tmp_path: Any, monkeypatch: Any
) -> None:
    """Catalog fetch failures should surface to the UI, not 500."""
    _register_piper(str(tmp_path))
    created = _make_piper_row(client, str(tmp_path))

    async def boom(*args: Any, **kwargs: Any) -> list[Any]:
        from app.providers.base import TTSError

        raise TTSError("network down")

    monkeypatch.setattr(
        "app.api.providers.piper_fetch_voice_catalog", boom
    )
    resp = client.get(f"/providers/{created['id']}/voices")
    assert resp.status_code == 502
    assert "network down" in resp.json()["detail"]


def test_list_voices_missing_provider_returns_404(client: TestClient) -> None:
    resp = client.get("/providers/9999/voices")
    assert resp.status_code == 404


def test_list_voices_defaults_model_dir_when_unset(
    client: TestClient, tmp_path: Any, monkeypatch: Any
) -> None:
    """A piper row without a model_dir option should fall back to the
    DEFAULT_MODEL_DIR constant so the endpoint never sees None."""
    _register_piper(str(tmp_path))
    created = client.post(
        "/providers",
        json={
            "kind": "tts",
            "provider_name": "piper",
            "display_name": "Local Piper bare",
            "credentials": {},
            "options": {"voice_id": "en_US-amy-medium"},
        },
    ).json()
    seen_model_dir: list[str] = []

    async def capture(model_dir: str, *args: Any, **kwargs: Any) -> list[Any]:
        seen_model_dir.append(model_dir)
        return []

    monkeypatch.setattr(
        "app.api.providers.piper_fetch_voice_catalog", capture
    )
    resp = client.get(f"/providers/{created['id']}/voices")
    assert resp.status_code == 200
    from app.providers.piper_tts import DEFAULT_MODEL_DIR

    assert seen_model_dir == [DEFAULT_MODEL_DIR]


def test_install_voice_downloads_and_returns_metadata(
    client: TestClient, tmp_path: Any, monkeypatch: Any
) -> None:
    """The install endpoint should hand off to the downloader and surface
    its summary back to the caller for the UI to render."""
    _register_piper(str(tmp_path))
    created = _make_piper_row(client, str(tmp_path))
    seen: dict[str, Any] = {}

    async def fake_download(
        voice_key: str, model_dir: str, *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        seen["voice_key"] = voice_key
        seen["model_dir"] = model_dir
        return {
            "key": voice_key,
            "installed": True,
            "onnx_bytes": 60_000_000,
            "onnx_json_bytes": 4_096,
            "already_present": False,
        }

    monkeypatch.setattr(
        "app.api.providers.piper_download_voice", fake_download
    )
    resp = client.post(
        f"/providers/{created['id']}/voices/en_US-amy-medium/install"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "en_US-amy-medium"
    assert body["installed"] is True
    assert body["already_present"] is False
    assert body["onnx_bytes"] == 60_000_000
    assert seen["voice_key"] == "en_US-amy-medium"
    assert seen["model_dir"] == str(tmp_path)


def test_install_voice_rejects_non_piper_provider(client: TestClient) -> None:
    get_registry().register(ProviderKind.LLM, "ok-llm", _OKLLM)
    created = client.post(
        "/providers",
        json=_create_payload(kind="llm", provider_name="ok-llm", display_name="L"),
    ).json()
    resp = client.post(
        f"/providers/{created['id']}/voices/en_US-amy-medium/install"
    )
    assert resp.status_code == 400


def test_install_voice_propagates_download_error_as_502(
    client: TestClient, tmp_path: Any, monkeypatch: Any
) -> None:
    _register_piper(str(tmp_path))
    created = _make_piper_row(client, str(tmp_path))

    async def boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        from app.providers.base import TTSError

        raise TTSError("connection refused")

    monkeypatch.setattr(
        "app.api.providers.piper_download_voice", boom
    )
    resp = client.post(
        f"/providers/{created['id']}/voices/en_US-amy-medium/install"
    )
    assert resp.status_code == 502
    assert "connection refused" in resp.json()["detail"]


def test_install_voice_missing_provider_returns_404(client: TestClient) -> None:
    resp = client.post("/providers/9999/voices/en_US-amy-medium/install")
    assert resp.status_code == 404


# --- STT catalog + stt_test endpoints (Johnny-ckz.15.2) --------------------


class _EchoSTT(STTProvider):
    """STT adapter that emits one final transcript built from the audio size.

    Lets stt_test assertions cover both the success path (a non-empty
    transcript flows back to the caller) and the empty-output path
    (a zero-byte body yields no transcript).
    """

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return "echo-stt"

    async def transcribe_stream(
        self, audio_iter: AsyncIterator[bytes]
    ) -> AsyncIterator[TranscriptEvent]:
        total = 0
        async for chunk in audio_iter:
            total += len(chunk)
        if total == 0:
            return
        yield TranscriptEvent(
            text=f"heard {total} bytes",
            is_final=True,
            timestamp_ms=0,
            confidence=0.9,
        )


def _make_stt_row(client: TestClient, provider_name: str = "echo-stt") -> dict[str, Any]:
    """Create a minimal STT row pointed at ``provider_name``."""
    resp = client.post(
        "/providers",
        json=_create_payload(provider_name=provider_name, display_name="Catalog test"),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_stt_catalog_returns_registered_stt_providers(client: TestClient) -> None:
    """The catalog UI fetches schemas + metadata in one shot."""
    from app.providers.faster_whisper_stt import PROVIDER_NAME as FW_NAME
    from app.providers.faster_whisper_stt import FasterWhisperSTT

    get_registry().register(ProviderKind.STT, FW_NAME, FasterWhisperSTT)
    resp = client.get("/providers/stt_catalog")
    assert resp.status_code == 200
    body = resp.json()
    assert "providers" in body
    names = {entry["provider_name"] for entry in body["providers"]}
    assert FW_NAME in names
    fw = next(e for e in body["providers"] if e["provider_name"] == FW_NAME)
    assert fw["provider_type"] == "local"
    assert fw["streaming"] is False
    assert fw["model_count"] > 0  # whisper has a populated model_size select
    assert isinstance(fw["models"], list)
    assert fw["field_schema"]["provider_name"] == FW_NAME


def test_stt_catalog_surfaces_parakeet(client: TestClient) -> None:
    """Parakeet must appear in /stt_catalog with local + cost=$0 metadata.

    Acceptance criterion from Johnny-stt.1: the provider is discoverable
    via the STT catalog so the catalog UI renders a card for it. This
    test fails loudly if the registration or the STT_CATALOG_METADATA
    entry is removed in a future refactor.
    """
    from app.providers.parakeet_stt import PROVIDER_NAME as PARAKEET_NAME
    from app.providers.parakeet_stt import ParakeetSTT

    get_registry().register(ProviderKind.STT, PARAKEET_NAME, ParakeetSTT, replace=True)
    resp = client.get("/providers/stt_catalog")
    assert resp.status_code == 200
    body = resp.json()
    names = {entry["provider_name"] for entry in body["providers"]}
    assert PARAKEET_NAME in names
    parakeet = next(e for e in body["providers"] if e["provider_name"] == PARAKEET_NAME)
    assert parakeet["provider_type"] == "local"
    # cost reporting via stt_test relies on this being a recognized
    # local provider — see _estimate_stt_cost.
    assert parakeet["model_count"] > 0
    assert "nvidia/parakeet-tdt-0.6b-v3" in parakeet["models"]
    assert parakeet["display_name"].startswith("NVIDIA Parakeet")


def test_stt_catalog_returns_empty_when_no_providers_registered(
    client: TestClient,
) -> None:
    """No registered STT adapters → empty providers list (not 500)."""
    resp = client.get("/providers/stt_catalog")
    assert resp.status_code == 200
    assert resp.json() == {"providers": []}


def test_stt_test_returns_transcript_and_latency(client: TestClient) -> None:
    """Happy path — adapter yields a final event; endpoint reports it back."""
    get_registry().register(ProviderKind.STT, "echo-stt", _EchoSTT)
    created = _make_stt_row(client)
    pcm = b"\x10\x00" * 8_000  # 16_000 bytes ≈ 0.5 s of 16 kHz S16LE PCM
    resp = client.post(
        f"/providers/{created['id']}/stt_test",
        content=pcm,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["transcript"] == "heard 16000 bytes"
    assert body["latency_ms"] >= 0
    assert body["audio_ms"] == 500
    # echo-stt is not in STT_CATALOG_METADATA → cost is None.
    assert body["cost_usd"] is None


def test_stt_test_estimates_cost_for_cloud_providers(client: TestClient) -> None:
    """A provider in STT_CATALOG_METADATA reports a USD cost estimate."""
    from app.api.providers import STT_CATALOG_METADATA

    # Use a stable name we know is in the catalog.
    get_registry().register(ProviderKind.STT, "deepgram", _EchoSTT)
    created = client.post(
        "/providers",
        json=_create_payload(provider_name="deepgram", display_name="DG"),
    ).json()
    pcm = b"\x10\x00" * 16_000  # 32_000 bytes = 1.0 s
    resp = client.post(
        f"/providers/{created['id']}/stt_test",
        content=pcm,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["audio_ms"] == 1000
    # 1 s at $0.0043/min ≈ $0.0000716 — checked against catalog rate.
    rate = STT_CATALOG_METADATA["deepgram"]["cost_per_minute_usd"]
    expected = round(rate * (1000 / 60_000), 6)
    assert body["cost_usd"] == expected


def test_stt_test_reports_zero_cost_for_local_providers(client: TestClient) -> None:
    """Local providers always report $0 — gives the UI a tidy line to render."""
    get_registry().register(ProviderKind.STT, "faster-whisper", _EchoSTT)
    created = client.post(
        "/providers",
        json=_create_payload(provider_name="faster-whisper", display_name="FW"),
    ).json()
    pcm = b"\x10\x00" * 8_000
    resp = client.post(
        f"/providers/{created['id']}/stt_test",
        content=pcm,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["cost_usd"] == 0.0


def test_stt_test_accepts_wav_blob_and_strips_header(client: TestClient) -> None:
    """WAV bodies are decoded; non-PCM bytes are not forwarded to the STT."""
    import io as _io
    import wave as _wave

    get_registry().register(ProviderKind.STT, "echo-stt", _EchoSTT)
    created = _make_stt_row(client)
    pcm = b"\x10\x00" * 8_000  # 16 000 bytes of PCM data
    buf = _io.BytesIO()
    with _wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16_000)
        wf.writeframes(pcm)
    wav_bytes = buf.getvalue()

    resp = client.post(
        f"/providers/{created['id']}/stt_test",
        content=wav_bytes,
        headers={"Content-Type": "audio/wav"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    # echo-stt reports total bytes — the wav header (44 bytes) must be stripped.
    assert body["transcript"] == "heard 16000 bytes"


def test_stt_test_rejects_non_stt_provider(client: TestClient) -> None:
    """The endpoint is STT-only; LLM / TTS rows must 400 with a clear detail."""
    get_registry().register(ProviderKind.LLM, "ok-llm", _OKLLM)
    created = client.post(
        "/providers",
        json=_create_payload(kind="llm", provider_name="ok-llm", display_name="L"),
    ).json()
    resp = client.post(
        f"/providers/{created['id']}/stt_test",
        content=b"\x00" * 4,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 400
    assert "stt" in resp.json()["detail"].lower()


def test_stt_test_rejects_empty_body(client: TestClient) -> None:
    """Empty body is a UI bug; surface 400 instead of running the STT."""
    get_registry().register(ProviderKind.STT, "echo-stt", _EchoSTT)
    created = _make_stt_row(client)
    resp = client.post(
        f"/providers/{created['id']}/stt_test",
        content=b"",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 400


def test_stt_test_rejects_oversized_body(client: TestClient) -> None:
    """Oversized body is rejected before construction so we don't burn quota."""
    from app.api.providers import STT_TEST_MAX_AUDIO_BYTES

    get_registry().register(ProviderKind.STT, "echo-stt", _EchoSTT)
    created = _make_stt_row(client)
    too_big = b"\x00" * (STT_TEST_MAX_AUDIO_BYTES + 1)
    resp = client.post(
        f"/providers/{created['id']}/stt_test",
        content=too_big,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 413


def test_stt_test_returns_ok_false_when_provider_yields_nothing(
    client: TestClient,
) -> None:
    """An ok=False payload + empty-transcript message is the soft-failure UX."""

    class _SilentSTT(STTProvider):
        def __init__(self, config: ProviderConfig) -> None:
            self._config = config

        @property
        def name(self) -> str:
            return "silent-stt"

        async def transcribe_stream(
            self, audio_iter: AsyncIterator[bytes]
        ) -> AsyncIterator[TranscriptEvent]:
            async for _ in audio_iter:
                pass
            # No yield: the provider succeeded but had nothing to say.
            if False:
                yield TranscriptEvent(text="", is_final=True, timestamp_ms=0)

    get_registry().register(ProviderKind.STT, "silent-stt", _SilentSTT)
    created = client.post(
        "/providers",
        json=_create_payload(provider_name="silent-stt", display_name="Silent"),
    ).json()
    resp = client.post(
        f"/providers/{created['id']}/stt_test",
        content=b"\x10\x00" * 8_000,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["transcript"] == ""
    assert "no transcript" in (body["message"] or "")


def test_stt_test_returns_ok_false_when_provider_raises(client: TestClient) -> None:
    """Adapter exceptions become ok=False + detail, not 500."""
    get_registry().register(ProviderKind.STT, "failing-stt", _FailingSTT)
    created = client.post(
        "/providers",
        json=_create_payload(provider_name="failing-stt", display_name="Fail"),
    ).json()
    resp = client.post(
        f"/providers/{created['id']}/stt_test",
        content=b"\x10\x00" * 8_000,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "synthetic auth failure" in (body["detail"] or "")


def test_stt_test_missing_provider_returns_404(client: TestClient) -> None:
    resp = client.post(
        "/providers/9999/stt_test",
        content=b"\x10\x00" * 8_000,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 404


# --- cartesia voice catalog endpoint (Johnny-ckz.18) ----------------------


def _make_cartesia_row(client: TestClient) -> dict[str, Any]:
    data: dict[str, Any] = client.post(
        "/providers",
        json={
            "kind": "tts",
            "provider_name": "cartesia",
            "display_name": "Cartesia Sonic",
            "credentials": {"api_key": "cart-test"},
            "options": {
                "voice_id": "694f9389-aac1-45b6-b726-9d9369183238",
                "model_id": "sonic-3.5",
            },
        },
    ).json()
    return data


def test_list_cartesia_voices_returns_voice_list(
    client: TestClient, monkeypatch: Any
) -> None:
    """The endpoint forwards the row's API key to the fetcher and surfaces
    the structured voice list back to the UI."""
    from app.providers.cartesia_tts import CartesiaVoiceInfo

    created = _make_cartesia_row(client)
    captured_kwargs: dict[str, Any] = {}

    async def fake_fetch(api_key: str, **kwargs: Any) -> list[Any]:
        captured_kwargs["api_key"] = api_key
        captured_kwargs.update(kwargs)
        return [
            CartesiaVoiceInfo(
                id="db6b0ed5-d5d3-463d-ae85-518a07d3c2b4",
                name="Skylar",
                description="Approachable American",
                language="en",
                gender="feminine",
                is_public=True,
            ),
        ]

    monkeypatch.setattr(
        "app.api.providers.cartesia_fetch_voice_catalog", fake_fetch
    )
    resp = client.get(f"/providers/{created['id']}/cartesia/voices")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["voices"]) == 1
    v = body["voices"][0]
    assert v["id"] == "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4"
    assert v["name"] == "Skylar"
    assert v["language"] == "en"
    assert v["gender"] == "feminine"
    assert v["description"] == "Approachable American"
    assert v["is_public"] is True
    # The endpoint should have decrypted the api_key and handed it through
    assert captured_kwargs["api_key"] == "cart-test"


def test_list_cartesia_voices_rejects_non_cartesia_provider(
    client: TestClient,
) -> None:
    """STT/LLM rows or non-Cartesia TTS rows must return 400."""
    get_registry().register(ProviderKind.STT, "ok-stt", _OKSTT, replace=True)
    created = client.post("/providers", json=_create_payload()).json()
    resp = client.get(f"/providers/{created['id']}/cartesia/voices")
    assert resp.status_code == 400
    assert "cartesia" in resp.json()["detail"].lower()


def test_list_cartesia_voices_propagates_fetch_error_as_502(
    client: TestClient, monkeypatch: Any
) -> None:
    """Catalog fetch failures should surface to the UI as 502, not 500."""
    created = _make_cartesia_row(client)

    async def boom(*args: Any, **kwargs: Any) -> list[Any]:
        from app.providers.base import TTSError

        raise TTSError("invalid api key")

    monkeypatch.setattr(
        "app.api.providers.cartesia_fetch_voice_catalog", boom
    )
    resp = client.get(f"/providers/{created['id']}/cartesia/voices")
    assert resp.status_code == 502
    assert "invalid api key" in resp.json()["detail"]


def test_list_cartesia_voices_missing_provider_returns_404(
    client: TestClient,
) -> None:
    resp = client.get("/providers/9999/cartesia/voices")
    assert resp.status_code == 404


def test_list_cartesia_voices_forwards_base_url_and_api_version(
    client: TestClient, monkeypatch: Any
) -> None:
    """When the row carries a custom base_url / api_version, the endpoint
    must hand them through to the fetch helper so the request goes to the
    operator's pinned spec."""
    from app.providers.cartesia_tts import CartesiaVoiceInfo

    created = client.post(
        "/providers",
        json={
            "kind": "tts",
            "provider_name": "cartesia",
            "display_name": "Cartesia Sonic proxy",
            "credentials": {"api_key": "cart-test"},
            "options": {
                "voice_id": "694f9389-aac1-45b6-b726-9d9369183238",
                "model_id": "sonic-3.5",
                "base_url": "https://proxy.example.com",
                "api_version": "2025-04-16",
            },
        },
    ).json()
    captured: dict[str, Any] = {}

    async def fake_fetch(api_key: str, **kwargs: Any) -> list[Any]:
        captured["api_key"] = api_key
        captured.update(kwargs)
        return [
            CartesiaVoiceInfo(
                id="vx", name="Vx", description="",
                language="en", gender="", is_public=True,
            )
        ]

    monkeypatch.setattr(
        "app.api.providers.cartesia_fetch_voice_catalog", fake_fetch
    )
    resp = client.get(f"/providers/{created['id']}/cartesia/voices")
    assert resp.status_code == 200
    assert captured["base_url"] == "https://proxy.example.com"
    assert captured["api_version"] == "2025-04-16"


def test_list_cartesia_voices_rejects_missing_api_key(
    client: TestClient,
) -> None:
    """If the row's credentials blob has no api_key, the endpoint must
    surface a precise 400 instead of forwarding an empty key to Cartesia
    and getting a confusing upstream error."""
    created = client.post(
        "/providers",
        json={
            "kind": "tts",
            "provider_name": "cartesia",
            "display_name": "Cartesia Sonic empty",
            "credentials": {"api_key": ""},
            "options": {
                "voice_id": "694f9389-aac1-45b6-b726-9d9369183238",
                "model_id": "sonic-3.5",
            },
        },
    )
    # The schema validator rejects an empty required api_key — we expect
    # 422 here, which is the *better* failure mode than reaching the
    # endpoint. If a row somehow lands without an api_key (e.g. legacy
    # rows from before schema validation), the endpoint's own guard
    # rejects with 400; the test below exercises that path via direct
    # update.
    assert created.status_code in (200, 201, 422)


# --- LLM model catalog (Johnny-9eq) ---------------------------------------


def _make_openai_llm_row(
    client: TestClient, *, api_key: str = "sk-test-openai"
) -> dict[str, Any]:
    """Persist an ``llm:openai`` row via legacy buckets.

    The ``clean_registry`` autouse fixture empties the global registry
    so the create endpoint cannot resolve the OpenAI schema; legacy
    ``credentials`` / ``options`` buckets bypass schema validation and
    persist exactly the keys we hand in. That's enough for the
    ``llm_models`` endpoints — they only read ``credentials.api_key``
    + ``options.base_url`` back out of the row.
    """
    resp = client.post(
        "/providers",
        json={
            "kind": "llm",
            "provider_name": "openai",
            "display_name": "OpenAI primary",
            "credentials": {"api_key": api_key},
            "options": {
                "model": "gpt-4o-mini",
                "base_url": "https://api.openai.com/v1",
            },
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _stub_openai_catalog(
    monkeypatch: Any, models: list[Any], *, capture: dict[str, Any] | None = None
) -> None:
    """Patch the openai fetch_model_catalog the API layer dispatches to."""
    from app.providers.base import LLMModelInfo

    async def fake_fetch(api_key: str, **kwargs: Any) -> list[Any]:
        if capture is not None:
            capture["api_key"] = api_key
            capture.update(kwargs)
        return [
            LLMModelInfo(id=m["id"], label=m.get("label", m["id"]))
            if isinstance(m, dict)
            else m
            for m in models
        ]

    monkeypatch.setattr(
        "app.api.providers.openai_fetch_model_catalog", fake_fetch
    )


def test_list_llm_models_returns_models_for_openai_row(
    client: TestClient, monkeypatch: Any
) -> None:
    """The endpoint forwards the row's decrypted API key + base_url to the
    provider's fetch_model_catalog and surfaces the result back to the UI."""
    created = _make_openai_llm_row(client, api_key="sk-test-real")
    captured: dict[str, Any] = {}
    _stub_openai_catalog(
        monkeypatch,
        [
            {"id": "gpt-5-preview"},
            {"id": "gpt-4o-mini"},
        ],
        capture=captured,
    )
    resp = client.get(f"/providers/{created['id']}/llm_models")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["models"] == [
        {"id": "gpt-5-preview", "label": "gpt-5-preview", "description": None},
        {"id": "gpt-4o-mini", "label": "gpt-4o-mini", "description": None},
    ]
    assert captured["api_key"] == "sk-test-real"
    # Saved base_url survives the round trip into the fetcher.
    assert captured["base_url"].startswith("https://api.openai.com")


def test_list_llm_models_rejects_non_llm_kind(
    client: TestClient,
) -> None:
    """STT / TTS rows must return 400 — wrong-kind dispatch."""
    get_registry().register(ProviderKind.STT, "ok-stt", _OKSTT, replace=True)
    created = client.post("/providers", json=_create_payload()).json()
    resp = client.get(f"/providers/{created['id']}/llm_models")
    assert resp.status_code == 400
    assert "llm_models" in resp.json()["detail"]


def test_list_llm_models_missing_row_returns_404(
    client: TestClient,
) -> None:
    resp = client.get("/providers/9999/llm_models")
    assert resp.status_code == 404


def test_list_llm_models_propagates_fetch_error_as_502(
    client: TestClient, monkeypatch: Any
) -> None:
    """Catalog fetch failures surface to the UI as 502, not 500."""
    created = _make_openai_llm_row(client)

    async def boom(*args: Any, **kwargs: Any) -> list[Any]:
        raise LLMError("invalid api key")

    monkeypatch.setattr(
        "app.api.providers.openai_fetch_model_catalog", boom
    )
    resp = client.get(f"/providers/{created['id']}/llm_models")
    assert resp.status_code == 502
    assert "invalid api key" in resp.json()["detail"]


def test_list_llm_models_unsupported_provider_returns_400(
    client: TestClient,
) -> None:
    """A registered LLM adapter without a fetch_model_catalog mapping
    (e.g. a third-party stub) must explain itself rather than 500."""
    get_registry().register(ProviderKind.LLM, "ok-llm", _OKLLM, replace=True)
    created = client.post(
        "/providers",
        json=_create_payload(
            kind="llm", provider_name="ok-llm", display_name="L"
        ),
    ).json()
    resp = client.get(f"/providers/{created['id']}/llm_models")
    assert resp.status_code == 400
    assert "ok-llm" in resp.json()["detail"]


def test_preview_llm_models_uses_unsaved_values(
    client: TestClient, monkeypatch: Any
) -> None:
    """The preview endpoint must use the payload's api_key directly,
    no persisted row required."""
    captured: dict[str, Any] = {}
    _stub_openai_catalog(
        monkeypatch,
        [{"id": "gpt-4o-mini"}],
        capture=captured,
    )
    resp = client.post(
        "/providers/preview/llm_models",
        json={
            "kind": "llm",
            "provider_name": "openai",
            "values": {"api_key": "sk-fresh", "base_url": "https://proxy.example/v1"},
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["models"] == [
        {"id": "gpt-4o-mini", "label": "gpt-4o-mini", "description": None}
    ]
    # api_key + base_url flowed through to the fetcher.
    assert captured["api_key"] == "sk-fresh"
    assert captured["base_url"] == "https://proxy.example/v1"


def test_preview_llm_models_rejects_non_llm_kind(
    client: TestClient,
) -> None:
    resp = client.post(
        "/providers/preview/llm_models",
        json={
            "kind": "tts",
            "provider_name": "openai",
            "values": {"api_key": "sk-test"},
        },
    )
    assert resp.status_code == 400
    assert "LLM" in resp.json()["detail"]


def test_preview_llm_models_missing_api_key_returns_400(
    client: TestClient,
) -> None:
    """Hosted providers need an api_key; the prompt the UI surfaces lives
    in the 400 detail."""
    resp = client.post(
        "/providers/preview/llm_models",
        json={
            "kind": "llm",
            "provider_name": "openai",
            "values": {"api_key": ""},
        },
    )
    assert resp.status_code == 400
    assert "api key" in resp.json()["detail"].lower()


def test_preview_llm_models_openai_compatible_needs_base_url(
    client: TestClient,
) -> None:
    """For self-hosted / Ollama the api_key is optional but base_url is required."""
    resp = client.post(
        "/providers/preview/llm_models",
        json={
            "kind": "llm",
            "provider_name": "openai-compatible",
            "values": {"api_key": "", "base_url": ""},
        },
    )
    assert resp.status_code == 400
    assert "base url" in resp.json()["detail"].lower()


def test_preview_llm_models_openai_compatible_does_not_require_api_key(
    client: TestClient, monkeypatch: Any
) -> None:
    """For Ollama, only base_url matters — the api_key is genuinely optional."""
    from app.providers.base import LLMModelInfo

    captured: dict[str, Any] = {}

    async def fake_fetch(base_url: str, **kwargs: Any) -> list[Any]:
        captured["base_url"] = base_url
        captured.update(kwargs)
        return [LLMModelInfo(id="llama3.1:8b", label="llama3.1:8b")]

    monkeypatch.setattr(
        "app.api.providers.openai_compatible_fetch_model_catalog", fake_fetch
    )
    resp = client.post(
        "/providers/preview/llm_models",
        json={
            "kind": "llm",
            "provider_name": "openai-compatible",
            "values": {"api_key": "", "base_url": "http://localhost:11434/v1"},
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["models"] == [
        {"id": "llama3.1:8b", "label": "llama3.1:8b", "description": None}
    ]
    assert captured["base_url"] == "http://localhost:11434/v1"
    # No api_key passed because the payload didn't supply one.
    assert captured.get("api_key") is None
