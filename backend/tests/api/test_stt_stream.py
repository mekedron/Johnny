"""Tests for the streaming STT WebSocket (Johnny-stt.3)."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import stt_stream as stt_stream_module
from app.api.deps import get_crypto, get_session
from app.db import Base
from app.db.models import ProviderCredential
from app.main import app
from app.providers.base import (
    ProviderConfig,
    ProviderKind,
    STTProvider,
    TranscriptEvent,
    get_registry,
)
from app.security.crypto import CredentialCrypto, encrypt_json


class _CountingSTT(STTProvider):
    """Test STT: returns text that encodes the byte length of the input.

    Each call to ``transcribe_stream`` reads the full audio iterator and
    emits one final event whose text is ``f"len={n}"`` where ``n`` is
    the buffer size. That lets the test assert partials are firing
    against a *growing* buffer without needing real audio + a real
    model.
    """

    instances: list[_CountingSTT] = []

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self.call_count = 0
        self.closed = False
        type(self).instances.append(self)

    @property
    def name(self) -> str:
        return "counting-stt"

    async def transcribe_stream(
        self, audio_iter: AsyncIterator[bytes]
    ) -> AsyncIterator[TranscriptEvent]:
        buf = bytearray()
        async for frame in audio_iter:
            buf.extend(frame)
        self.call_count += 1
        if buf:
            yield TranscriptEvent(
                text=f"len={len(buf)}",
                is_final=True,
                timestamp_ms=0,
                confidence=1.0,
            )

    async def close(self) -> None:
        self.closed = True


class _BrokenSTT(STTProvider):
    """STT that always raises during transcription."""

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return "broken-stt"

    async def transcribe_stream(
        self, audio_iter: AsyncIterator[bytes]
    ) -> AsyncIterator[TranscriptEvent]:
        # Drain so the partial loop sees real audio first.
        async for _ in audio_iter:
            pass
        raise RuntimeError("synthetic transcription failure")
        if False:  # pragma: no cover — keeps this an async-generator factory
            yield TranscriptEvent(text="", is_final=True, timestamp_ms=0)


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
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
def client(
    db_session: Session,
    crypto: CredentialCrypto,
    monkeypatch: pytest.MonkeyPatch,
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

    # The WS handler opens its own session via session_scope() — patch
    # it to reuse the test's shared session so the in-memory DB row
    # exists on lookup. Also patch get_crypto so decrypt_json uses the
    # test key.
    from contextlib import contextmanager

    @contextmanager
    def fake_session_scope() -> Iterator[Session]:
        yield db_session

    monkeypatch.setattr(stt_stream_module, "session_scope", fake_session_scope)
    monkeypatch.setattr(stt_stream_module, "get_crypto", lambda: crypto)
    # Tighter cadence so the test doesn't block 400 ms per partial step.
    monkeypatch.setattr(stt_stream_module, "PARTIAL_INTERVAL_SEC", 0.05)
    monkeypatch.setattr(stt_stream_module, "MIN_BYTES_FOR_PARTIAL", 0)

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_crypto] = _override_crypto
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    registry = get_registry()
    saved = dict(registry._factories)  # noqa: SLF001
    registry.clear()
    _CountingSTT.instances.clear()
    try:
        yield
    finally:
        registry.clear()
        for key, factory in saved.items():
            registry.register(key[0], key[1], factory)


def _seed_row(
    db_session: Session,
    crypto: CredentialCrypto,
    *,
    provider_name: str,
    is_active: bool = True,
    kind: ProviderKind = ProviderKind.STT,
) -> ProviderCredential:
    row = ProviderCredential(
        kind=kind,
        provider_name=provider_name,
        display_name=f"{provider_name} test row",
        credentials_encrypted=encrypt_json(crypto, {"api_key": "sk-test"}),
        config={"model": "fake-model"},
        is_active=is_active,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _recv_envelope(ws: Any) -> dict[str, Any]:
    """Receive one JSON text frame, decoded."""
    return ws.receive_json()


def _recv_until(ws: Any, kind: str, *, max_frames: int = 20) -> dict[str, Any]:
    """Receive until an envelope of ``type == kind`` arrives.

    Bound the loop with ``max_frames`` so a regression that prints
    "partial" forever doesn't hang the suite.
    """
    for _ in range(max_frames):
        envelope = _recv_envelope(ws)
        if envelope.get("type") == kind:
            return envelope
    raise AssertionError(
        f"did not see envelope type={kind!r} within {max_frames} frames"
    )


# --- Tests ----------------------------------------------------------------


def test_ws_emits_ready_with_provider_metadata(
    client: TestClient, db_session: Session, crypto: CredentialCrypto
) -> None:
    get_registry().register(ProviderKind.STT, "counting-stt", _CountingSTT)
    row = _seed_row(db_session, crypto, provider_name="counting-stt")
    with client.websocket_connect(
        f"/ws/stt/stream?provider_id={row.id}"
    ) as ws:
        envelope = _recv_envelope(ws)
        ws.send_text(json.dumps({"type": "abort"}))
    assert envelope["type"] == "ready"
    assert envelope["provider"] == "counting-stt"
    assert envelope["display_name"] == "counting-stt test row"
    assert envelope["sample_rate"] == 16_000


def test_ws_falls_back_to_active_row_when_no_provider_id(
    client: TestClient, db_session: Session, crypto: CredentialCrypto
) -> None:
    get_registry().register(ProviderKind.STT, "counting-stt", _CountingSTT)
    # Inactive row first, then an active one — the endpoint should pick
    # the active one.
    _seed_row(
        db_session, crypto, provider_name="counting-stt", is_active=False
    )
    active = ProviderCredential(
        kind=ProviderKind.STT,
        provider_name="counting-stt",
        display_name="active counting row",
        credentials_encrypted=encrypt_json(crypto, {"api_key": "sk-active"}),
        config={"model": "fake-model"},
        is_active=True,
    )
    db_session.add(active)
    db_session.commit()

    with client.websocket_connect("/ws/stt/stream") as ws:
        envelope = _recv_envelope(ws)
        ws.send_text(json.dumps({"type": "abort"}))
    assert envelope["type"] == "ready"
    assert envelope["display_name"] == "active counting row"


def test_ws_404_when_provider_id_missing(
    client: TestClient, db_session: Session, crypto: CredentialCrypto
) -> None:
    get_registry().register(ProviderKind.STT, "counting-stt", _CountingSTT)
    with client.websocket_connect("/ws/stt/stream?provider_id=9999") as ws:
        envelope = _recv_envelope(ws)
    assert envelope["type"] == "error"
    assert "no STT provider" in envelope["message"]


def test_ws_400_when_no_active_provider(
    client: TestClient, db_session: Session, crypto: CredentialCrypto
) -> None:
    get_registry().register(ProviderKind.STT, "counting-stt", _CountingSTT)
    # No row at all → should still respond with a clear error envelope.
    with client.websocket_connect("/ws/stt/stream") as ws:
        envelope = _recv_envelope(ws)
    assert envelope["type"] == "error"
    assert "no active STT provider" in envelope["message"]


def test_ws_rejects_non_stt_provider(
    client: TestClient, db_session: Session, crypto: CredentialCrypto
) -> None:
    get_registry().register(ProviderKind.STT, "counting-stt", _CountingSTT)
    llm_row = ProviderCredential(
        kind=ProviderKind.LLM,
        provider_name="counting-stt",
        display_name="not-an-stt-row",
        credentials_encrypted=encrypt_json(crypto, {"api_key": "x"}),
        config={},
        is_active=True,
    )
    db_session.add(llm_row)
    db_session.commit()
    db_session.refresh(llm_row)
    with client.websocket_connect(
        f"/ws/stt/stream?provider_id={llm_row.id}"
    ) as ws:
        envelope = _recv_envelope(ws)
    assert envelope["type"] == "error"
    assert "not stt" in envelope["message"]


def test_ws_streams_partials_then_final(
    client: TestClient, db_session: Session, crypto: CredentialCrypto
) -> None:
    """Push 3 chunks, expect 3 partials, then send end → final."""
    get_registry().register(ProviderKind.STT, "counting-stt", _CountingSTT)
    row = _seed_row(db_session, crypto, provider_name="counting-stt")
    with client.websocket_connect(
        f"/ws/stt/stream?provider_id={row.id}"
    ) as ws:
        ready = _recv_envelope(ws)
        assert ready["type"] == "ready"

        partials_seen: list[str] = []
        chunk_a = b"\x01\x00" * 100  # 200 bytes
        chunk_b = b"\x02\x00" * 100  # 200 bytes
        chunk_c = b"\x03\x00" * 100  # 200 bytes

        for chunk in (chunk_a, chunk_b, chunk_c):
            ws.send_bytes(chunk)
            envelope = _recv_until(ws, "partial")
            partials_seen.append(envelope["text"])

        # Acceptance criterion: at least N=3 partials emitted.
        assert len(partials_seen) >= 3
        # Each partial encodes a growing buffer length (the test
        # provider's design): 200, 400, 600.
        assert partials_seen == ["len=200", "len=400", "len=600"]

        ws.send_text(json.dumps({"type": "end"}))
        final = _recv_until(ws, "final")
    assert final["type"] == "final"
    assert final["text"] == "len=600"


def test_ws_partials_skip_unchanged_buffer(
    client: TestClient, db_session: Session, crypto: CredentialCrypto
) -> None:
    """If the buffer hasn't grown since last partial, don't re-transcribe."""
    get_registry().register(ProviderKind.STT, "counting-stt", _CountingSTT)
    row = _seed_row(db_session, crypto, provider_name="counting-stt")
    with client.websocket_connect(
        f"/ws/stt/stream?provider_id={row.id}"
    ) as ws:
        _recv_envelope(ws)  # ready
        ws.send_bytes(b"\x00\x10" * 100)
        first_partial = _recv_until(ws, "partial")
        # Sleep > 5 partial cadences without sending bytes — should not
        # produce a new partial frame.
        time.sleep(stt_stream_module.PARTIAL_INTERVAL_SEC * 6)
        ws.send_text(json.dumps({"type": "end"}))
        final = _recv_until(ws, "final")

    instance = _CountingSTT.instances[-1]
    # At least one partial run + final run, plus one or two re-runs if
    # the first partial result was empty (text dedup is on the result,
    # not the buffer-size check). Tolerate 2-3 calls.
    assert instance.call_count <= 3, (
        f"expected <=3 transcribe calls when buffer is static; got "
        f"{instance.call_count}"
    )
    assert first_partial["text"] == "len=200"
    assert final["text"] == "len=200"


def test_ws_abort_skips_final(
    client: TestClient, db_session: Session, crypto: CredentialCrypto
) -> None:
    get_registry().register(ProviderKind.STT, "counting-stt", _CountingSTT)
    row = _seed_row(db_session, crypto, provider_name="counting-stt")
    received: list[dict[str, Any]] = []
    with client.websocket_connect(
        f"/ws/stt/stream?provider_id={row.id}"
    ) as ws:
        received.append(_recv_envelope(ws))  # ready
        ws.send_bytes(b"\x00\x05" * 50)
        ws.send_text(json.dumps({"type": "abort"}))
        # Drain anything in-flight; the next frame must be the
        # server-initiated close. Bound the drain so a regression
        # can't hang the suite.
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect):
            for _ in range(20):
                envelope = ws.receive_json()
                # If a final ever slips through, fail loudly.
                assert envelope.get("type") != "final"
    assert received[0]["type"] == "ready"


def test_ws_surfaces_transcription_failure(
    client: TestClient, db_session: Session, crypto: CredentialCrypto
) -> None:
    get_registry().register(ProviderKind.STT, "broken-stt", _BrokenSTT)
    row = _seed_row(db_session, crypto, provider_name="broken-stt")
    with client.websocket_connect(
        f"/ws/stt/stream?provider_id={row.id}"
    ) as ws:
        _recv_envelope(ws)  # ready
        ws.send_bytes(b"\x01\x00" * 100)
        envelope = _recv_until(ws, "error")
    assert envelope["type"] == "error"
    assert "partial transcribe failed" in envelope["message"]


def test_ws_buffer_cap_enforced(
    client: TestClient,
    db_session: Session,
    crypto: CredentialCrypto,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_registry().register(ProviderKind.STT, "counting-stt", _CountingSTT)
    row = _seed_row(db_session, crypto, provider_name="counting-stt")
    # Drop the cap to 1 KiB so the test doesn't have to push 10 MiB.
    monkeypatch.setattr(stt_stream_module, "MAX_BUFFER_BYTES", 1024)
    with client.websocket_connect(
        f"/ws/stt/stream?provider_id={row.id}"
    ) as ws:
        _recv_envelope(ws)  # ready
        # Send 2 KiB in one chunk to force overflow.
        ws.send_bytes(b"\xff\x00" * 1024)
        envelope = _recv_until(ws, "error")
    assert "exceeded" in envelope["message"]


def test_ws_handles_unregistered_provider(
    client: TestClient, db_session: Session, crypto: CredentialCrypto
) -> None:
    # Row exists but no factory is registered for its provider_name —
    # the endpoint should fail loudly via the error envelope rather
    # than 500.
    row = _seed_row(db_session, crypto, provider_name="counting-stt")
    with client.websocket_connect(
        f"/ws/stt/stream?provider_id={row.id}"
    ) as ws:
        envelope = _recv_envelope(ws)
    assert envelope["type"] == "error"
    assert "no factory registered" in envelope["message"]
