"""Tests for app.services.mcp_servers (Johnny-trt.36): rows ↔ configs ↔ probes."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.db.models import McpServer
from app.security.crypto import CredentialCrypto
from app.services.mcp_servers import (
    decrypt_secrets,
    encrypt_secrets,
    load_server_configs,
    load_server_snapshots,
    probe_server_row,
    row_to_config,
    secret_key_names,
)
from johnny.mcp.catalog import McpToolInfo, mcp_catalog_entries
from johnny.mcp.client import McpProbeResult


@pytest.fixture
def db() -> Iterator[Session]:
    engine = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def crypto() -> CredentialCrypto:
    return CredentialCrypto(Fernet.generate_key())


def _row(**overrides: Any) -> McpServer:
    base: dict[str, Any] = {
        "name": "fixture",
        "transport": "stdio",
        "command": "python3",
        "args": ["/opt/sandbox/mcp_fixture_server.py"],
    }
    base.update(overrides)
    return McpServer(**base)


def test_secrets_round_trip_and_masking(crypto: CredentialCrypto) -> None:
    blob = encrypt_secrets(crypto, env={"TOKEN": "t"}, headers={"Authorization": "Bearer x"})
    assert blob is not None and "TOKEN" not in blob  # actually encrypted
    env, headers = decrypt_secrets(crypto, blob)
    assert env == {"TOKEN": "t"}
    assert headers == {"Authorization": "Bearer x"}
    assert secret_key_names(crypto, blob) == (["TOKEN"], ["Authorization"])
    # No secrets → no blob; masking helpers stay calm on None/garbage.
    assert encrypt_secrets(crypto, env={}, headers={}) is None
    assert secret_key_names(crypto, None) == ([], [])
    assert secret_key_names(crypto, "not-a-token") == ([], [])


def test_row_to_config_secretless_vs_decrypted(
    db: Session, crypto: CredentialCrypto
) -> None:
    row = _row(secrets_encrypted=encrypt_secrets(crypto, env={"K": "v"}, headers={}))
    db.add(row)
    db.flush()
    secretless = row_to_config(row, crypto=None)
    assert secretless.env == {}
    full = row_to_config(row, crypto=crypto)
    assert full.env == {"K": "v"}
    assert full.argv == ("python3", "/opt/sandbox/mcp_fixture_server.py")


def test_loaders_skip_corrupt_rows_and_split_disabled(
    db: Session, crypto: CredentialCrypto
) -> None:
    db.add(_row())
    db.add(_row(name="off", enabled=False))
    # A row that fails validation (URL on a stdio transport, hand-edited DB).
    db.add(_row(name="corrupt", url="https://nope.test"))
    db.flush()

    # The worker's read keeps disabled rows (the executor speaks the
    # isn't-enabled distinction) but never a corrupt one.
    configs = load_server_configs(db, crypto)
    assert [(c.name, c.enabled) for c in configs] == [("fixture", True), ("off", False)]

    # Catalog assembly sees enabled servers only.
    snapshots = load_server_snapshots(db)
    assert [s.config.name for s in snapshots] == ["fixture"]


def test_snapshots_feed_catalog_with_probe_state(db: Session) -> None:
    db.add(
        _row(
            tools_cache=[
                {"name": "echo", "description": "Echo."},
                {"name": "add", "description": "Add."},
                {"junk": True},  # tolerated garbage entry
            ],
            tool_exclude=["add"],
            last_probe_ok=False,
            last_probe_error="connect refused",
        )
    )
    db.flush()
    snapshots = load_server_snapshots(db)
    assert len(snapshots) == 1
    entries = mcp_catalog_entries(snapshots)
    assert [e.kind for e in entries] == ["mcp__fixture__echo"]
    assert not entries[0].available  # last probe failed → unavailable + reason
    assert "fixture connector" in entries[0].unavailable_reason


async def test_probe_server_row_persists_success_then_failure(
    db: Session, crypto: CredentialCrypto, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _row()
    db.add(row)
    db.flush()

    async def good_probe(config: Any, *, sandbox_url: str, http_client: Any = None) -> Any:
        return McpProbeResult(
            ok=True, tools=(McpToolInfo(name="echo", description="Echo."),), duration_ms=4
        )

    monkeypatch.setattr("johnny.mcp.client.probe_mcp_server", good_probe)
    result = await probe_server_row(db, row, crypto, sandbox_url="http://sb:8088")
    assert result.ok
    assert row.last_probe_ok is True
    assert row.last_probe_at is not None
    assert row.tools_cache == [{"name": "echo", "description": "Echo."}]

    async def bad_probe(config: Any, *, sandbox_url: str, http_client: Any = None) -> Any:
        return McpProbeResult(ok=False, error="boom", duration_ms=2)

    monkeypatch.setattr("johnny.mcp.client.probe_mcp_server", bad_probe)
    result = await probe_server_row(db, row, crypto, sandbox_url="http://sb:8088")
    assert not result.ok
    assert row.last_probe_ok is False
    assert row.last_probe_error == "boom"
    assert row.tools_cache == [{"name": "echo", "description": "Echo."}]  # stale kept
