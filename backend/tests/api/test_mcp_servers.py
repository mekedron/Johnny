"""Tests for the /mcp-servers API (Johnny-hp1): file-backed, name-keyed.

CRUD over each workspace's ``.johnny/.mcp.json`` (secrets stored plaintext /
``${VAR}`` on disk but masked to key names in responses, 409 on duplicate
name, 422 through the one runtime validator) and the probe endpoint: the happy
path persists the tool cache to ``.mcp-state.json`` + reports qualified catalog
kinds; the sad path records the error and keeps the stale cache so the catalog
renders unavailable-with-reason instead of forgetting the tools (Johnny-trt.55).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import get_session
from app.db import Base
from app.db.models import Workspace
from app.main import app
from johnny.mcp import store
from johnny.mcp.catalog import McpToolInfo
from johnny.mcp.client import McpProbeResult


@pytest.fixture(autouse=True)
def workspaces_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store at a tmp workspaces root; keep the probe's lazy
    container-ensure a no-op in the test env."""
    monkeypatch.setenv("JOHNNY_WORKSPACES_DIR", str(tmp_path))
    monkeypatch.delenv("JOHNNY_USE_DOCKER_LAUNCHER", raising=False)
    return tmp_path


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
    session = sessionmaker(bind=engine)()
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


@pytest.fixture
def workspace(db_session: Session) -> Workspace:
    """The default workspace these servers attach to. Its stdio probe spawns in
    ``johnny-workspace-1`` (the ensure no-ops here — no docker is driven)."""
    row = Workspace(name="Default", slug="default", is_default=True)
    db_session.add(row)
    db_session.commit()
    return row


@pytest.fixture
def base(workspace: Workspace) -> str:
    return f"/workspaces/{workspace.id}/mcp-servers"


def _stdio_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "fixture",
        "transport": "stdio",
        "command": "python3",
        "args": ["/opt/sandbox/mcp_fixture_server.py"],
        "env": {"API_TOKEN": "secret-value"},
    }
    payload.update(overrides)
    return payload


def test_create_masks_secrets_and_lists(
    client: TestClient, base: str, workspace: Workspace
) -> None:
    created = client.post(base, json=_stdio_payload())
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "fixture"
    assert body["workspace_id"] == workspace.id  # owned by the workspace
    assert body["env_keys"] == ["API_TOKEN"]
    assert "secret-value" not in created.text  # the masking contract
    assert body["tools"] is None  # never probed
    assert body["last_probe_ok"] is None
    assert "id" not in body  # identity is `name` now (no surrogate id)

    listed = client.get(base)
    assert listed.status_code == 200
    assert [s["name"] for s in listed.json()["servers"]] == ["fixture"]
    assert "secret-value" not in listed.text
    # The value IS on disk (plaintext) — masking is an API contract only.
    assert store.read_servers_raw("default")["fixture"]["env"] == {
        "API_TOKEN": "secret-value"
    }


def test_unknown_workspace_404(client: TestClient) -> None:
    assert client.get("/workspaces/999/mcp-servers").status_code == 404
    assert (
        client.post("/workspaces/999/mcp-servers", json=_stdio_payload()).status_code
        == 404
    )


def test_servers_are_isolated_per_workspace(
    client: TestClient, db_session: Session, base: str
) -> None:
    """A server on one workspace is invisible — and 404 — through another's URL
    (the per-workspace file boundary, Johnny-hp1)."""
    other = Workspace(name="Finance", slug="finance", is_default=False)
    db_session.add(other)
    db_session.commit()
    other_base = f"/workspaces/{other.id}/mcp-servers"

    assert client.post(base, json=_stdio_payload()).status_code == 201

    assert client.get(other_base).json()["servers"] == []
    assert client.get(f"{other_base}/fixture").status_code == 404
    assert client.patch(f"{other_base}/fixture", json={"enabled": False}).status_code == 404
    assert client.delete(f"{other_base}/fixture").status_code == 404
    assert client.post(f"{other_base}/fixture/probe").status_code == 404

    assert [s["name"] for s in client.get(base).json()["servers"]] == ["fixture"]


def test_same_name_allowed_in_different_workspaces(
    client: TestClient, db_session: Session, base: str
) -> None:
    other = Workspace(name="Finance", slug="finance", is_default=False)
    db_session.add(other)
    db_session.commit()
    assert client.post(base, json=_stdio_payload()).status_code == 201
    assert (
        client.post(
            f"/workspaces/{other.id}/mcp-servers", json=_stdio_payload()
        ).status_code
        == 201
    )


def test_create_validates_through_runtime_config(client: TestClient, base: str) -> None:
    bad_name = client.post(base, json=_stdio_payload(name="has_underscore"))
    assert bad_name.status_code == 422
    assert "underscores" in bad_name.json()["detail"]

    no_url = client.post(base, json={"name": "remote", "transport": "http"})
    assert no_url.status_code == 422
    assert "url" in no_url.json()["detail"]

    mixed = client.post(
        base,
        json={
            "name": "remote",
            "transport": "http",
            "url": "https://x.test/mcp",
            "command": "python3",
        },
    )
    assert mixed.status_code == 422


def test_duplicate_name_conflicts(client: TestClient, base: str) -> None:
    assert client.post(base, json=_stdio_payload()).status_code == 201
    assert client.post(base, json=_stdio_payload()).status_code == 409


def test_patch_updates_and_preserves_untouched_secrets(
    client: TestClient, base: str
) -> None:
    client.post(base, json=_stdio_payload())

    patched = client.patch(
        f"{base}/fixture", json={"enabled": False, "tool_exclude": ["add"]}
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["enabled"] is False
    assert body["tool_exclude"] == ["add"]
    assert body["env_keys"] == ["API_TOKEN"]  # untouched secrets survive
    # The stored value really is preserved on disk (write-only on the API).
    assert store.read_servers_raw("default")["fixture"]["env"] == {
        "API_TOKEN": "secret-value"
    }

    cleared = client.patch(f"{base}/fixture", json={"env": {}, "headers": {}})
    assert cleared.status_code == 200
    assert cleared.json()["env_keys"] == []
    assert "env" not in store.read_servers_raw("default")["fixture"]


def test_patch_rename_carries_identity(client: TestClient, base: str) -> None:
    client.post(base, json=_stdio_payload())
    renamed = client.patch(f"{base}/fixture", json={"name": "renamed"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "renamed"
    assert client.get(f"{base}/fixture").status_code == 404
    assert client.get(f"{base}/renamed").status_code == 200


def test_patch_invalid_shape_rejected(client: TestClient, base: str) -> None:
    client.post(base, json=_stdio_payload())
    bad = client.patch(f"{base}/fixture", json={"command": ""})
    assert bad.status_code == 422
    assert "command" in bad.json()["detail"]


def test_delete_and_404s(client: TestClient, base: str) -> None:
    assert client.get(f"{base}/ghost").status_code == 404
    assert client.delete(f"{base}/ghost").status_code == 404
    assert client.post(f"{base}/ghost/probe").status_code == 404

    client.post(base, json=_stdio_payload())
    assert client.delete(f"{base}/fixture").status_code == 204
    assert client.get(f"{base}/fixture").status_code == 404


def test_probe_happy_path_persists_cache_and_reports_kinds(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, base: str
) -> None:
    client.post(base, json=_stdio_payload(tool_exclude=["always-fail"]))

    async def fake_probe(config: Any, *, sandbox_url: str, http_client: Any = None) -> Any:
        assert config.name == "fixture"
        assert config.env == {"API_TOKEN": "secret-value"}  # probe uses live secrets
        return McpProbeResult(
            ok=True,
            tools=(
                McpToolInfo(name="echo", description="Echo it."),
                McpToolInfo(name="add", description="Add numbers."),
                McpToolInfo(name="always-fail", description="Boom."),
            ),
            server_info="johnny-mcp-fixture 1.0.0",
            duration_ms=12,
        )

    monkeypatch.setattr("johnny.mcp.client.probe_mcp_server", fake_probe)

    probed = client.post(f"{base}/fixture/probe")
    assert probed.status_code == 200, probed.text
    body = probed.json()
    assert body["ok"] is True
    assert body["server_info"] == "johnny-mcp-fixture 1.0.0"
    assert [t["name"] for t in body["tools"]] == ["echo", "add", "always-fail"]
    assert body["tools"][2]["included"] is False  # the excluded tool, flagged
    assert body["catalog_kinds"] == ["mcp__fixture__echo", "mcp__fixture__add"]

    state = store.read_states("default")["fixture"]
    assert state["last_probe_ok"] is True
    assert state["last_probe_error"] == ""
    assert [t["name"] for t in state["tools_cache"]] == ["echo", "add", "always-fail"]

    # The cached view now renders on reads too.
    read = client.get(f"{base}/fixture").json()
    assert read["catalog_kinds"] == ["mcp__fixture__echo", "mcp__fixture__add"]
    assert read["last_probe_ok"] is True


def test_probe_sad_path_records_error_keeps_stale_cache(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, base: str
) -> None:
    client.post(base, json=_stdio_payload())
    # A prior good probe seeded a cache.
    store.write_state(
        "default",
        "fixture",
        ok=True,
        error="",
        tools=[{"name": "echo", "description": "from a better day"}],
    )

    async def failing_probe(
        config: Any, *, sandbox_url: str, http_client: Any = None
    ) -> Any:
        return McpProbeResult(ok=False, error="skills sandbox unreachable", duration_ms=3)

    monkeypatch.setattr("johnny.mcp.client.probe_mcp_server", failing_probe)

    probed = client.post(f"{base}/fixture/probe")
    assert probed.status_code == 200
    body = probed.json()
    assert body["ok"] is False
    assert "unreachable" in body["error"]
    assert body["tools"] == []  # a failed probe reports nothing new

    state = store.read_states("default")["fixture"]
    assert state["last_probe_ok"] is False
    assert "unreachable" in state["last_probe_error"]
    # The stale cache SURVIVES (Johnny-trt.55) instead of vanishing.
    assert [t["name"] for t in state["tools_cache"]] == ["echo"]
