"""Tests for app.services.mcp_servers (Johnny-hp1): file-backed CRUD + probe.

The DB→file cutover: servers live in each workspace's ``.johnny/.mcp.json``,
the service is the api's view over that file (list/get/create/update/delete +
probe → ``.mcp-state.json``). ``slug_for_stamp`` / ``resolve_mcp_slug`` are the
seams the worker / session / catalog share to map a workspace onto its file.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.services import mcp_servers as svc
from app.services.workspaces import DEFAULT_WORKSPACE_SLUG, seed_default_workspace
from johnny.mcp.catalog import McpToolInfo
from johnny.mcp.client import McpProbeResult


@pytest.fixture(autouse=True)
def workspaces_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("JOHNNY_WORKSPACES_DIR", str(tmp_path))
    return tmp_path


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


def _create(slug: str, name: str, **overrides: Any) -> Any:
    base: dict[str, Any] = {
        "name": name,
        "transport": "stdio",
        "enabled": True,
        "command": "python3",
        "args": ["/opt/sandbox/mcp_fixture_server.py"],
        "env": {},
        "url": "",
        "headers": {},
        "tool_include": None,
        "tool_exclude": [],
        "connect_timeout_s": 10.0,
        "call_timeout_s": 60.0,
        "idle_ttl_s": 300.0,
    }
    base.update(overrides)
    return svc.create_server(slug, **base)


def test_slug_for_stamp() -> None:
    # Stampless legacy claim → the default workspace's servers.
    assert svc.slug_for_stamp(None, None) == DEFAULT_WORKSPACE_SLUG
    # A stamped workspace (default or named) uses its own slug.
    assert svc.slug_for_stamp(1, "default") == "default"
    assert svc.slug_for_stamp(7, "finance") == "finance"
    # Stamped but slug missing → None (can't locate the file; load nothing).
    assert svc.slug_for_stamp(7, "") is None
    assert svc.slug_for_stamp(7, None) is None


def test_resolve_mcp_slug(db: Session) -> None:
    seed_default_workspace(db)
    db.flush()

    class _Ws:
        slug = "finance"

    # A concrete workspace owns its servers by its own slug (no DB touch).
    assert svc.resolve_mcp_slug(db, _Ws()) == "finance"
    # No workspace (parameterless view) → the seeded default slug.
    assert svc.resolve_mcp_slug(db, None) == DEFAULT_WORKSPACE_SLUG


def test_create_list_get_delete() -> None:
    rec = _create("default", "fixture", env={"TOKEN": "t"})
    assert rec.config.name == "fixture"
    assert rec.env_keys == ["TOKEN"]  # masked: key name only
    assert rec.has_probe_cache is False
    assert [r.config.name for r in svc.list_servers("default")] == ["fixture"]
    got = svc.get_server("default", "fixture")
    assert got.config.argv == ("python3", "/opt/sandbox/mcp_fixture_server.py")
    svc.delete_server("default", "fixture")
    assert svc.list_servers("default") == []
    with pytest.raises(svc.McpServerNotFoundError):
        svc.get_server("default", "fixture")


def test_create_duplicate_is_conflict() -> None:
    _create("default", "dup")
    with pytest.raises(svc.McpServerNameExistsError):
        _create("default", "dup")


def test_create_invalid_is_config_error() -> None:
    from johnny.mcp.config import McpConfigError

    # stdio with no command → invalid shape.
    with pytest.raises(McpConfigError):
        _create("default", "bad", command="")
    # Uppercase name → invalid slug.
    with pytest.raises(McpConfigError):
        _create("default", "BadName")


def test_update_patches_and_preserves_secrets() -> None:
    _create("default", "fixture", env={"TOKEN": "secret"})
    # Patch the command, omit env → stored secret preserved (write-only).
    rec = svc.update_server("default", "fixture", command="python3.11")
    assert rec.config.command == "python3.11"
    assert rec.env_keys == ["TOKEN"]
    # Toggle enabled.
    rec = svc.update_server("default", "fixture", enabled=False)
    assert rec.config.enabled is False
    # Replace env wholesale.
    rec = svc.update_server("default", "fixture", env={"NEW": "v"})
    assert rec.env_keys == ["NEW"]


def test_update_rename_carries_state() -> None:
    _create("default", "old")
    svc.update_server("default", "old", new_name="renamed")
    assert [r.config.name for r in svc.list_servers("default")] == ["renamed"]
    with pytest.raises(svc.McpServerNotFoundError):
        svc.get_server("default", "old")
    # Renaming onto an existing name conflicts.
    _create("default", "other")
    with pytest.raises(svc.McpServerNameExistsError):
        svc.update_server("default", "renamed", new_name="other")


def test_update_missing_is_not_found() -> None:
    with pytest.raises(svc.McpServerNotFoundError):
        svc.update_server("default", "ghost", enabled=False)


async def test_probe_persists_success_then_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create("default", "fixture")

    async def good(config: Any, *, sandbox_url: str, http_client: Any = None) -> Any:
        return McpProbeResult(
            ok=True, tools=(McpToolInfo(name="echo", description="Echo."),), duration_ms=4
        )

    monkeypatch.setattr("johnny.mcp.client.probe_mcp_server", good)
    result = await svc.probe_and_store("default", "fixture", sandbox_url="http://sb:8088")
    assert result.ok
    rec = svc.get_server("default", "fixture")
    assert rec.has_probe_cache is True
    assert rec.last_probe_ok is True
    assert [t.name for t in rec.tools] == ["echo"]

    async def bad(config: Any, *, sandbox_url: str, http_client: Any = None) -> Any:
        return McpProbeResult(ok=False, error="boom", duration_ms=2)

    monkeypatch.setattr("johnny.mcp.client.probe_mcp_server", bad)
    result = await svc.probe_and_store("default", "fixture", sandbox_url="http://sb:8088")
    assert not result.ok
    rec = svc.get_server("default", "fixture")
    assert rec.last_probe_ok is False
    assert rec.last_probe_error == "boom"
    assert [t.name for t in rec.tools] == ["echo"]  # stale cache kept


def test_per_workspace_isolation() -> None:
    _create("default", "shared")
    _create("finance", "ledger")
    assert [r.config.name for r in svc.list_servers("default")] == ["shared"]
    assert [r.config.name for r in svc.list_servers("finance")] == ["ledger"]
    # The same name in two workspaces is legal (per-workspace files).
    _create("finance", "shared")
    assert sorted(r.config.name for r in svc.list_servers("finance")) == ["ledger", "shared"]
