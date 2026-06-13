"""Tests for the /capability-policies API (Johnny-trt.38; per-workspace base
in Johnny-wks.9).

CRUD per scope layer (the one-row-per-target provider-settings pattern),
target validation (safe_bins workspace-only, unknown workspaces/agents/sessions
404), the effective view, and THE resolution inspector: tool/bin +
coordinates → allowed/denied + the deciding layer. The base layer is the
WORKSPACE — reads without an explicit workspace_id derive it from the agent
(or the default workspace).
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
from app.db.models import (
    Agent,
    BotMode,
    BotSession,
    BotSessionSource,
    BotSessionStatus,
    Workspace,
)
from app.main import app
from johnny.skills.policy import BASELINE_BINS


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


@pytest.fixture
def workspace(db_session: Session) -> Workspace:
    """The default workspace the base policy layer attaches to (Johnny-wks.9).

    The agent/session fixtures leave ``workspace_id`` NULL, so the read
    surfaces resolve onto this default — exactly what dispatch does.
    """
    row = Workspace(
        name="Default", slug="default", description="seeded", is_default=True
    )
    db_session.add(row)
    # Commit (not just flush) so the row survives a request that rolls back —
    # a 422-first test (safe_bins rejection) would otherwise wipe the fixture.
    db_session.commit()
    return row


def _ws(workspace: Workspace) -> str:
    return f"/capability-policies/workspaces/{workspace.id}"


def _seed_agent(db_session: Session, name: str = "Progress Bot") -> Agent:
    row = Agent(
        name=name,
        character_prompt="calm",
        mode=BotMode.AUTONOMOUS,
        allowed_replies=[],
        confidence_threshold=0.7,
        is_default=False,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _seed_session(db_session: Session) -> BotSession:
    row = BotSession(
        source=BotSessionSource.BROWSER,
        status=BotSessionStatus.JOINED,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _doc(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "tools_allow": [],
        "tools_also_allow": [],
        "tools_deny": [],
        "bins_deny": [],
    }
    doc.update(overrides)
    return doc


# --- CRUD --------------------------------------------------------------------


def test_list_starts_empty_with_the_builtin_baseline(client: TestClient) -> None:
    response = client.get("/capability-policies")
    assert response.status_code == 200
    body = response.json()
    assert body["rows"] == []
    assert body["baseline_safe_bins"] == list(BASELINE_BINS)


def test_workspace_upsert_is_one_row_and_canonicalized(
    client: TestClient, workspace: Workspace
) -> None:
    created = client.put(
        _ws(workspace),
        json=_doc(tools_deny=["gmail.*", "  gmail.*  ", ""]),
    )
    assert created.status_code == 200
    assert created.json()["scope"] == "workspace"
    assert created.json()["workspace_id"] == workspace.id
    # Dedup + strip happened on write (the canonical document shape).
    assert created.json()["document"]["tools_deny"] == ["gmail.*"]

    replaced = client.put(_ws(workspace), json=_doc(tools_deny=["calendar.*"]))
    assert replaced.status_code == 200
    rows = client.get("/capability-policies").json()["rows"]
    assert len(rows) == 1  # upsert, never a second base row for the workspace
    assert rows[0]["document"]["tools_deny"] == ["calendar.*"]


def test_workspace_scope_requires_an_existing_workspace(client: TestClient) -> None:
    assert (
        client.put("/capability-policies/workspaces/999", json=_doc()).status_code
        == 404
    )
    assert (
        client.delete("/capability-policies/workspaces/999").status_code == 404
    )


def test_agent_scope_requires_an_existing_agent(
    client: TestClient, db_session: Session
) -> None:
    assert (
        client.put("/capability-policies/agents/999", json=_doc()).status_code == 404
    )
    agent = _seed_agent(db_session)
    response = client.put(
        f"/capability-policies/agents/{agent.id}",
        json=_doc(tools_allow=["google-calendar", "tasks.*"]),
    )
    assert response.status_code == 200
    assert response.json()["agent_id"] == agent.id


def test_session_scope_requires_an_existing_session(
    client: TestClient, db_session: Session
) -> None:
    assert (
        client.put("/capability-policies/sessions/999", json=_doc()).status_code == 404
    )
    session_row = _seed_session(db_session)
    response = client.put(
        f"/capability-policies/sessions/{session_row.id}",
        json=_doc(tools_deny=["session.end"]),
    )
    assert response.status_code == 200
    assert response.json()["bot_session_id"] == session_row.id


def test_session_mode_scope_validates_the_mode(client: TestClient) -> None:
    ok = client.put("/capability-policies/session-modes/meet", json=_doc())
    assert ok.status_code == 200
    bad = client.put("/capability-policies/session-modes/zoom", json=_doc())
    assert bad.status_code == 422


def test_safe_bins_is_workspace_only(
    client: TestClient, db_session: Session, workspace: Workspace
) -> None:
    agent = _seed_agent(db_session)
    denied = client.put(
        f"/capability-policies/agents/{agent.id}",
        json=_doc(safe_bins=["bash"]),
    )
    assert denied.status_code == 422
    assert "workspace" in denied.json()["detail"]
    allowed = client.put(_ws(workspace), json=_doc(safe_bins=["bash", "cat"]))
    assert allowed.status_code == 200
    assert allowed.json()["document"]["safe_bins"] == ["bash", "cat"]


def test_delete_resets_a_layer(client: TestClient, workspace: Workspace) -> None:
    client.put(_ws(workspace), json=_doc(tools_deny=["x"]))
    first = client.delete(_ws(workspace))
    assert first.status_code == 200 and first.json()["deleted"] is True
    second = client.delete(_ws(workspace))
    assert second.json()["deleted"] is False
    assert client.get("/capability-policies").json()["rows"] == []


def test_unknown_document_keys_are_rejected(
    client: TestClient, workspace: Workspace
) -> None:
    response = client.put(_ws(workspace), json={"tools_block": ["x"]})
    assert response.status_code == 422


# --- effective view ------------------------------------------------------------


def test_effective_view_resolves_layers_and_safe_bins(
    client: TestClient, db_session: Session, workspace: Workspace
) -> None:
    agent = _seed_agent(db_session)
    edited = [b for b in BASELINE_BINS if b != "curl"]
    client.put(_ws(workspace), json=_doc(safe_bins=edited))
    client.put(
        f"/capability-policies/agents/{agent.id}",
        json=_doc(tools_allow=["google-calendar"]),
    )

    # No explicit workspace_id — it's derived from the agent's workspace
    # (the default), exactly as dispatch resolves it.
    response = client.get(
        "/capability-policies/effective",
        params={"agent_id": agent.id, "session_mode": "meet"},
    )
    assert response.status_code == 200
    body = response.json()
    assert [layer["scope"] for layer in body["layers"]] == ["workspace", "agent"]
    assert body["layers"][0]["scope_detail"] == "Default"
    assert body["layers"][1]["scope_detail"] == "Progress Bot"
    assert body["safe_bins"] == edited
    assert body["removed_baseline_bins"] == ["curl"]
    assert body["tools_unrestricted"] is False
    assert body["allow_layer"] == "agent"


# --- THE resolution inspector ---------------------------------------------------


def test_resolve_tool_names_the_deciding_layer(
    client: TestClient, db_session: Session, workspace: Workspace
) -> None:
    agent = _seed_agent(db_session)
    client.put(_ws(workspace), json=_doc(tools_deny=["mcp__shady__*"]))
    client.put(
        f"/capability-policies/agents/{agent.id}",
        json=_doc(tools_allow=["google-calendar", "tasks.*"]),
    )

    denied_workspace = client.post(
        "/capability-policies/resolve",
        json={"tool": "mcp__shady__send", "agent_id": agent.id},
    ).json()
    assert denied_workspace["allowed"] is False
    assert denied_workspace["layer"] == "workspace"
    assert denied_workspace["rule"] == "mcp__shady__*"

    denied_agent = client.post(
        "/capability-policies/resolve",
        json={"tool": "financial-reports", "agent_id": agent.id},
    ).json()
    assert denied_agent["allowed"] is False
    assert denied_agent["layer"] == "agent"
    assert denied_agent["rule"] == "allow-list"
    assert denied_agent["detail"] == "Progress Bot"

    allowed = client.post(
        "/capability-policies/resolve",
        json={"tool": "google-calendar", "agent_id": agent.id},
    ).json()
    assert allowed["allowed"] is True
    assert allowed["layers_consulted"] == ["workspace", "agent"]

    # Without the agent layer the same kind is allowed (no allow-list). The
    # workspace base still applies (resolved from the default workspace).
    no_agent = client.post(
        "/capability-policies/resolve", json={"tool": "financial-reports"}
    ).json()
    assert no_agent["allowed"] is True


def test_resolve_bin_reports_safe_bins_removal(
    client: TestClient, workspace: Workspace
) -> None:
    edited = [b for b in BASELINE_BINS if b != "curl"]
    client.put(_ws(workspace), json=_doc(safe_bins=edited))
    # No coordinates → the default workspace's base layer.
    removed = client.post(
        "/capability-policies/resolve", json={"bin": "curl"}
    ).json()
    assert removed["allowed"] is False
    assert removed["layer"] == "workspace"
    assert removed["rule"] == "removed from safe-bins"
    assert removed["capability_kind"] == "bin"

    kept = client.post("/capability-policies/resolve", json={"bin": "git"}).json()
    assert kept["allowed"] is True


def test_resolve_requires_exactly_one_capability(client: TestClient) -> None:
    both = client.post(
        "/capability-policies/resolve", json={"tool": "x", "bin": "y"}
    )
    neither = client.post("/capability-policies/resolve", json={})
    assert both.status_code == 422
    assert neither.status_code == 422
