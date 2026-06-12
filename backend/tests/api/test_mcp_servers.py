"""Tests for the /mcp-servers API (Johnny-trt.36).

CRUD per the provider-settings pattern (encrypted secrets masked to key
names, 409 on duplicate names, 422 through the one runtime validator) and
the probe endpoint: happy path persists the tool cache + reports qualified
catalog kinds; sad path records the error and keeps the stale cache so the
catalog renders unavailable-with-reason instead of forgetting the tools.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

import app.services.mcp_servers as mcp_service
from app.api.deps import get_crypto, get_session
from app.db import Base
from app.db.models import McpServer
from app.main import app
from app.security.crypto import CredentialCrypto
from johnny.mcp.catalog import McpToolInfo
from johnny.mcp.client import McpProbeResult


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
def crypto() -> CredentialCrypto:
    return CredentialCrypto(Fernet.generate_key())


@pytest.fixture
def client(db_session: Session, crypto: CredentialCrypto) -> Iterator[TestClient]:
    def _override() -> Iterator[Session]:
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    app.dependency_overrides[get_session] = _override
    app.dependency_overrides[get_crypto] = lambda: crypto
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_crypto, None)


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


def test_create_masks_secrets_and_lists(client: TestClient) -> None:
    created = client.post("/mcp-servers", json=_stdio_payload())
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "fixture"
    assert body["env_keys"] == ["API_TOKEN"]
    assert "secret-value" not in created.text  # the masking contract
    assert body["tools"] is None  # never probed
    assert body["last_probe_ok"] is None

    listed = client.get("/mcp-servers")
    assert listed.status_code == 200
    assert [s["name"] for s in listed.json()["servers"]] == ["fixture"]
    assert "secret-value" not in listed.text


def test_create_validates_through_runtime_config(client: TestClient) -> None:
    bad_name = client.post("/mcp-servers", json=_stdio_payload(name="has_underscore"))
    assert bad_name.status_code == 422
    assert "underscores" in bad_name.json()["detail"]

    no_url = client.post(
        "/mcp-servers", json={"name": "remote", "transport": "http"}
    )
    assert no_url.status_code == 422
    assert "url" in no_url.json()["detail"]

    mixed = client.post(
        "/mcp-servers",
        json={
            "name": "remote",
            "transport": "http",
            "url": "https://x.test/mcp",
            "command": "python3",
        },
    )
    assert mixed.status_code == 422


def test_duplicate_name_conflicts(client: TestClient) -> None:
    assert client.post("/mcp-servers", json=_stdio_payload()).status_code == 201
    duplicate = client.post("/mcp-servers", json=_stdio_payload())
    assert duplicate.status_code == 409


def test_patch_updates_and_preserves_untouched_secrets(
    client: TestClient, db_session: Session, crypto: CredentialCrypto
) -> None:
    server_id = client.post("/mcp-servers", json=_stdio_payload()).json()["id"]

    patched = client.patch(
        f"/mcp-servers/{server_id}", json={"enabled": False, "tool_exclude": ["add"]}
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["enabled"] is False
    assert body["tool_exclude"] == ["add"]
    assert body["env_keys"] == ["API_TOKEN"]  # untouched secrets survive

    row = db_session.get(McpServer, server_id)
    assert row is not None
    env, _headers = mcp_service.decrypt_secrets(crypto, row.secrets_encrypted)
    assert env == {"API_TOKEN": "secret-value"}

    cleared = client.patch(
        f"/mcp-servers/{server_id}", json={"env": {}, "headers": {}}
    )
    assert cleared.status_code == 200
    assert cleared.json()["env_keys"] == []
    db_session.refresh(row)
    assert row.secrets_encrypted is None


def test_patch_invalid_shape_rejected(client: TestClient) -> None:
    server_id = client.post("/mcp-servers", json=_stdio_payload()).json()["id"]
    bad = client.patch(f"/mcp-servers/{server_id}", json={"command": ""})
    assert bad.status_code == 422
    assert "command" in bad.json()["detail"]


def test_delete_and_404s(client: TestClient) -> None:
    assert client.get("/mcp-servers/999").status_code == 404
    assert client.delete("/mcp-servers/999").status_code == 404
    assert client.post("/mcp-servers/999/probe").status_code == 404

    server_id = client.post("/mcp-servers", json=_stdio_payload()).json()["id"]
    assert client.delete(f"/mcp-servers/{server_id}").status_code == 204
    assert client.get(f"/mcp-servers/{server_id}").status_code == 404


def test_probe_happy_path_persists_cache_and_reports_kinds(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    server_id = client.post(
        "/mcp-servers", json=_stdio_payload(tool_exclude=["always-fail"])
    ).json()["id"]

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

    probed = client.post(f"/mcp-servers/{server_id}/probe")
    assert probed.status_code == 200, probed.text
    body = probed.json()
    assert body["ok"] is True
    assert body["server_info"] == "johnny-mcp-fixture 1.0.0"
    assert [t["name"] for t in body["tools"]] == ["echo", "add", "always-fail"]
    assert body["tools"][2]["included"] is False  # the excluded tool, flagged
    assert body["catalog_kinds"] == ["mcp__fixture__echo", "mcp__fixture__add"]

    row = db_session.get(McpServer, server_id)
    assert row is not None
    assert row.last_probe_ok is True
    assert row.last_probe_error == ""
    assert [t["name"] for t in row.tools_cache] == ["echo", "add", "always-fail"]

    # The cached view now renders on reads too.
    read = client.get(f"/mcp-servers/{server_id}").json()
    assert read["catalog_kinds"] == ["mcp__fixture__echo", "mcp__fixture__add"]
    assert read["last_probe_ok"] is True


def test_probe_sad_path_records_error_keeps_stale_cache(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    server_id = client.post("/mcp-servers", json=_stdio_payload()).json()["id"]
    row = db_session.get(McpServer, server_id)
    assert row is not None
    row.tools_cache = [{"name": "echo", "description": "from a better day"}]
    row.last_probe_ok = True
    db_session.flush()

    async def failing_probe(
        config: Any, *, sandbox_url: str, http_client: Any = None
    ) -> Any:
        return McpProbeResult(ok=False, error="skills sandbox unreachable", duration_ms=3)

    monkeypatch.setattr("johnny.mcp.client.probe_mcp_server", failing_probe)

    probed = client.post(f"/mcp-servers/{server_id}/probe")
    assert probed.status_code == 200
    body = probed.json()
    assert body["ok"] is False
    assert "unreachable" in body["error"]
    assert body["tools"] == []  # a failed probe reports nothing new

    db_session.refresh(row)
    assert row.last_probe_ok is False
    assert "unreachable" in row.last_probe_error
    # The stale cache SURVIVES: catalog renders unavailable-with-reason
    # (Johnny-trt.55) instead of the tools silently vanishing.
    assert [t["name"] for t in row.tools_cache] == ["echo"]
