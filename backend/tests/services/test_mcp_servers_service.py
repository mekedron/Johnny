"""Tests for app.services.mcp_servers (Johnny-trt.36): rows ↔ configs ↔ probes.

Per-workspace scoping (Johnny-wks.8): every server is owned by a workspace,
and the loaders take a ``workspace_id`` — an agent's MCP set is exactly its
workspace's servers. :func:`resolve_mcp_workspace_id` is the seam the worker /
session / catalog share to map a workspace stamp onto the concrete id.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.db.models import McpServer, Workspace
from app.security.crypto import CredentialCrypto
from app.services.mcp_servers import (
    decrypt_secrets,
    encrypt_secrets,
    load_server_configs,
    load_server_snapshots,
    probe_server_row,
    resolve_mcp_workspace_id,
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


def _seed_workspaces(db: Session) -> tuple[int, int]:
    """A seeded default + one non-default workspace; returns ``(default, other)``."""
    default = Workspace(name="Default", slug="default", is_default=True)
    other = Workspace(name="Finance", slug="finance", is_default=False)
    db.add_all([default, other])
    db.flush()
    return int(default.id), int(other.id)


def _row(workspace_id: int, **overrides: Any) -> McpServer:
    base: dict[str, Any] = {
        "workspace_id": workspace_id,
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
    default_id, _ = _seed_workspaces(db)
    row = _row(
        default_id, secrets_encrypted=encrypt_secrets(crypto, env={"K": "v"}, headers={})
    )
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
    default_id, _ = _seed_workspaces(db)
    db.add(_row(default_id))
    db.add(_row(default_id, name="off", enabled=False))
    # A row that fails validation (URL on a stdio transport, hand-edited DB).
    db.add(_row(default_id, name="corrupt", url="https://nope.test"))
    db.flush()

    # The worker's read keeps disabled rows (the executor speaks the
    # isn't-enabled distinction) but never a corrupt one.
    configs = load_server_configs(db, crypto, workspace_id=default_id)
    assert [(c.name, c.enabled) for c in configs] == [("fixture", True), ("off", False)]

    # Catalog assembly sees enabled servers only.
    snapshots = load_server_snapshots(db, workspace_id=default_id)
    assert [s.config.name for s in snapshots] == ["fixture"]


def test_per_workspace_isolation(db: Session, crypto: CredentialCrypto) -> None:
    """Two workspaces, two MCP sets — each loader returns only its own (wks.8)."""
    default_id, finance_id = _seed_workspaces(db)
    db.add(_row(default_id, name="shared-tools"))
    db.add(_row(finance_id, name="ledger", enabled=True))
    db.add(_row(finance_id, name="ledger-off", enabled=False))
    db.flush()

    assert [c.name for c in load_server_configs(db, crypto, workspace_id=default_id)] == [
        "shared-tools"
    ]
    # The finance worker read keeps the disabled row; assembly drops it.
    assert sorted(
        c.name for c in load_server_configs(db, crypto, workspace_id=finance_id)
    ) == ["ledger", "ledger-off"]
    assert [s.config.name for s in load_server_snapshots(db, workspace_id=finance_id)] == [
        "ledger"
    ]
    # A name shared across workspaces is legal — uniqueness is per-workspace.
    db.add(_row(finance_id, name="shared-tools"))
    db.flush()
    assert sorted(
        c.name for c in load_server_configs(db, crypto, workspace_id=finance_id)
    ) == ["ledger", "ledger-off", "shared-tools"]

    # A None workspace id (unseeded schema degrade) loads nothing, never raises.
    assert load_server_configs(db, crypto, workspace_id=None) == ()
    assert load_server_snapshots(db, workspace_id=None) == ()


def test_resolve_mcp_workspace_id(db: Session) -> None:
    default_id, finance_id = _seed_workspaces(db)
    # A non-default stamp owns its servers by its own id.
    assert (
        resolve_mcp_workspace_id(db, workspace_id=finance_id, is_default=False)
        == finance_id
    )
    # The default stamp — and a legacy no-stamp session — resolve to the
    # seeded default's id (the rows the migration mapped the old global set on).
    assert (
        resolve_mcp_workspace_id(db, workspace_id=default_id, is_default=True)
        == default_id
    )
    assert (
        resolve_mcp_workspace_id(db, workspace_id=None, is_default=True) == default_id
    )


def test_resolve_mcp_workspace_id_unseeded_is_none(db: Session) -> None:
    # No default workspace seeded → None (the promise-nothing degrade).
    assert resolve_mcp_workspace_id(db, workspace_id=None, is_default=True) is None


def test_snapshots_feed_catalog_with_probe_state(db: Session) -> None:
    default_id, _ = _seed_workspaces(db)
    db.add(
        _row(
            default_id,
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
    snapshots = load_server_snapshots(db, workspace_id=default_id)
    assert len(snapshots) == 1
    entries = mcp_catalog_entries(snapshots)
    assert [e.kind for e in entries] == ["mcp__fixture__echo"]
    assert not entries[0].available  # last probe failed → unavailable + reason
    assert "fixture connector" in entries[0].unavailable_reason


async def test_probe_server_row_persists_success_then_failure(
    db: Session, crypto: CredentialCrypto, monkeypatch: pytest.MonkeyPatch
) -> None:
    default_id, _ = _seed_workspaces(db)
    row = _row(default_id)
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
