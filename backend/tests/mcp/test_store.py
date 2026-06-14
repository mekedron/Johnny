"""Tests for johnny.mcp.store (Johnny-hp1): the per-workspace .mcp.json store.

The file twin of the old ``app.services.mcp_servers`` row↔config tests: a
workspace's ``<root>/<slug>/.johnny/.mcp.json`` is the source of truth, the
loaders map entries to the same ``McpServerConfig`` / ``McpServerSnapshot``
value objects, ``${VAR}`` env/headers expand from the process env on the
connecting path, and probe verdicts live in a sibling ``.mcp-state.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from johnny.mcp import store
from johnny.mcp.catalog import mcp_catalog_entries
from johnny.mcp.config import McpServerConfig


@pytest.fixture(autouse=True)
def workspaces_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store's path resolver at a tmp workspaces root."""
    monkeypatch.setenv("JOHNNY_WORKSPACES_DIR", str(tmp_path))
    return tmp_path


def _config_path(root: Path, slug: str) -> Path:
    return root / slug / ".johnny" / ".mcp.json"


def _write(root: Path, slug: str, servers: dict) -> None:
    path = _config_path(root, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def test_round_trip_raw(workspaces_root: Path) -> None:
    entry = store.serialize_entry(
        transport="stdio",
        enabled=True,
        command="python3",
        args=["/opt/sandbox/mcp_fixture_server.py"],
        env={"K": "v"},
        url="",
        headers={},
        tool_include=None,
        tool_exclude=[],
        connect_timeout_s=10.0,
        call_timeout_s=60.0,
        idle_ttl_s=300.0,
    )
    store.write_servers_raw("default", {"fixture": entry})
    back = store.read_servers_raw("default")
    assert set(back) == {"fixture"}
    assert back["fixture"]["command"] == "python3"
    assert back["fixture"]["johnny"]["enabled"] is True
    # http transport drops command/args, keeps url/headers.
    http_entry = store.serialize_entry(
        transport="http",
        enabled=True,
        command="",
        args=[],
        env={},
        url="https://mcp.test/sse",
        headers={"Authorization": "Bearer x"},
        tool_include=["a*"],
        tool_exclude=["delete-*"],
        connect_timeout_s=5.0,
        call_timeout_s=30.0,
        idle_ttl_s=120.0,
    )
    assert http_entry["url"] == "https://mcp.test/sse"
    assert "command" not in http_entry
    assert http_entry["headers"] == {"Authorization": "Bearer x"}


def test_entry_to_config_secretless_vs_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TOKEN", "sekret")
    entry = {
        "type": "stdio",
        "command": "python3",
        "args": ["x.py"],
        "env": {"TOKEN": "${MY_TOKEN}", "PLAIN": "literal"},
        "johnny": {"enabled": True},
    }
    secretless = store.entry_to_config("fixture", entry, resolve_secrets=False)
    assert secretless.env == {}
    assert secretless.argv == ("python3", "x.py")
    resolved = store.entry_to_config("fixture", entry, resolve_secrets=True)
    assert resolved.env == {"TOKEN": "sekret", "PLAIN": "literal"}


def test_unset_var_expands_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOPE", raising=False)
    entry = {"command": "c", "env": {"X": "${NOPE}"}, "johnny": {"enabled": True}}
    resolved = store.entry_to_config("s", entry, resolve_secrets=True)
    assert resolved.env == {"X": ""}


def test_load_configs_includes_disabled_skips_invalid_sorted(workspaces_root: Path) -> None:
    _write(
        workspaces_root,
        "default",
        {
            "fixture": {"command": "python3", "args": ["a"], "johnny": {"enabled": True}},
            "off": {"command": "python3", "args": ["b"], "johnny": {"enabled": False}},
            # invalid: stdio with a url set → McpConfigError → skipped
            "corrupt": {"command": "c", "url": "https://nope.test", "johnny": {}},
        },
    )
    configs = store.load_server_configs("default")
    assert [(c.name, c.enabled) for c in configs] == [("fixture", True), ("off", False)]
    # Catalog snapshots: enabled only.
    snaps = store.load_server_snapshots("default")
    assert [s.config.name for s in snaps] == ["fixture"]


def test_none_slug_degrades_to_empty() -> None:
    assert store.load_server_configs(None) == ()
    assert store.load_server_snapshots(None) == ()
    assert store.load_cached_kinds(None) == frozenset()


def test_malformed_file_degrades(workspaces_root: Path) -> None:
    path = _config_path(workspaces_root, "default")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert store.read_servers_raw("default") == {}
    assert store.load_server_configs("default") == ()


def test_probe_state_round_trip_and_catalog(workspaces_root: Path) -> None:
    _write(
        workspaces_root,
        "default",
        {"fixture": {"command": "python3", "args": ["a"], "johnny": {"tool_exclude": ["add"]}}},
    )
    # No probe yet → snapshot has no tools, catalog empty.
    assert mcp_catalog_entries(store.load_server_snapshots("default")) == ()
    # A failed probe keeps the (just-written) cache and marks unavailable.
    store.write_state(
        "default",
        "fixture",
        ok=True,
        error="",
        tools=[{"name": "echo", "description": "Echo."}, {"name": "add", "description": "Add."}],
    )
    store.write_state("default", "fixture", ok=False, error="connect refused", tools=None)
    snaps = store.load_server_snapshots("default")
    assert snaps[0].probe_ok is False
    entries = mcp_catalog_entries(snaps)
    # 'add' is excluded by the filter; 'echo' survives but is unavailable.
    assert [e.kind for e in entries] == ["mcp__fixture__echo"]
    assert not entries[0].available
    # Cached-kinds view (toggle): filters applied, disabled would still count.
    assert store.load_cached_kinds("default") == frozenset({"mcp__fixture__echo"})


def test_rename_and_remove_state(workspaces_root: Path) -> None:
    store.write_state("default", "old", ok=True, error="", tools=[{"name": "t"}])
    store.rename_state("default", "old", "new")
    states = store.read_states("default")
    assert "old" not in states and states["new"]["last_probe_ok"] is True
    store.remove_state("default", "new")
    assert store.read_states("default") == {}


def test_per_workspace_isolation(workspaces_root: Path) -> None:
    _write(workspaces_root, "default", {"shared": {"command": "c", "args": [], "johnny": {}}})
    _write(workspaces_root, "finance", {"ledger": {"command": "c", "args": [], "johnny": {}}})
    assert [c.name for c in store.load_server_configs("default")] == ["shared"]
    assert [c.name for c in store.load_server_configs("finance")] == ["ledger"]
    assert isinstance(store.load_server_configs("default")[0], McpServerConfig)
