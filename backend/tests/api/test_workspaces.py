"""Tests for the /workspaces CRUD API (Johnny-wks.1).

The bead's CRUD matrix: create derives a frozen unique slug, names are
unique (409), rename never touches the slug, the seeded default workspace
is non-deletable (409), and delete is blocked while agents are attached
(409 with the count). ``agent_count`` reports effective attachment —
explicit rows everywhere, plus the NULL-attached agents on the default.
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
from app.db.models import Agent, McpServer, Workspace
from app.main import app
from app.services.workspaces import seed_default_workspace


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
def default_workspace(db_session: Session) -> Workspace:
    seed_default_workspace(db_session)
    row = db_session.scalar(sa.select(Workspace).where(Workspace.is_default.is_(True)))
    assert row is not None
    return row


def _create(client: TestClient, name: str, **extra: Any) -> dict[str, Any]:
    resp = client.post("/workspaces", json={"name": name, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- seed / list -------------------------------------------------------------


def test_seed_default_workspace_is_idempotent(db_session: Session) -> None:
    first = seed_default_workspace(db_session)
    assert first is not None
    assert first.is_default is True
    assert first.slug == "default"
    assert seed_default_workspace(db_session) is None  # second run: no-op
    count = db_session.scalar(sa.select(sa.func.count()).select_from(Workspace))
    assert count == 1


def test_list_orders_default_first(
    client: TestClient, default_workspace: Workspace
) -> None:
    _create(client, "Zebra")
    _create(client, "Alpha")
    resp = client.get("/workspaces")
    assert resp.status_code == 200
    names = [row["name"] for row in resp.json()]
    assert names == ["Default", "Alpha", "Zebra"]
    assert resp.json()[0]["is_default"] is True


# --- create ------------------------------------------------------------------


def test_create_derives_slug_and_round_trips(client: TestClient) -> None:
    body = _create(client, "Finance Team", description="Books + reports")
    assert body["name"] == "Finance Team"
    assert body["slug"] == "finance-team"
    assert body["description"] == "Books + reports"
    assert body["is_default"] is False
    assert body["agent_count"] == 0

    read = client.get(f"/workspaces/{body['id']}")
    assert read.status_code == 200
    assert read.json() == body


def test_create_duplicate_name_409(client: TestClient) -> None:
    _create(client, "Finance")
    resp = client.post("/workspaces", json={"name": "Finance"})
    assert resp.status_code == 409


def test_create_slug_collision_disambiguates(client: TestClient) -> None:
    first = _create(client, "Team A")
    second = _create(client, "Team-A")  # different name, same slug base
    assert first["slug"] == "team-a"
    assert second["slug"] == "team-a-2"


def test_create_blank_name_422(client: TestClient) -> None:
    resp = client.post("/workspaces", json={"name": "   "})
    assert resp.status_code == 422


def test_create_symbols_only_name_gets_fallback_slug(client: TestClient) -> None:
    body = _create(client, "!!!")
    assert body["slug"] == "workspace"


# --- rename ------------------------------------------------------------------


def test_rename_keeps_the_slug_frozen(client: TestClient) -> None:
    created = _create(client, "Finance")
    resp = client.patch(
        f"/workspaces/{created['id']}",
        json={"name": "Finance & Ops", "description": "renamed"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Finance & Ops"
    assert body["description"] == "renamed"
    assert body["slug"] == "finance"  # storage identity never moves


def test_rename_to_taken_name_409(client: TestClient) -> None:
    _create(client, "Finance")
    other = _create(client, "Ops")
    resp = client.patch(f"/workspaces/{other['id']}", json={"name": "Finance"})
    assert resp.status_code == 409


def test_rename_default_workspace_is_allowed(
    client: TestClient, default_workspace: Workspace
) -> None:
    resp = client.patch(
        f"/workspaces/{default_workspace.id}", json={"name": "Shared"}
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Shared"
    assert resp.json()["is_default"] is True
    assert resp.json()["slug"] == "default"


def test_patch_unknown_field_422(client: TestClient) -> None:
    created = _create(client, "Finance")
    resp = client.patch(f"/workspaces/{created['id']}", json={"slug": "hacked"})
    assert resp.status_code == 422


def test_patch_missing_workspace_404(client: TestClient) -> None:
    assert client.patch("/workspaces/999", json={"name": "x"}).status_code == 404
    assert client.get("/workspaces/999").status_code == 404
    assert client.delete("/workspaces/999").status_code == 404


# --- delete ------------------------------------------------------------------


def test_delete_default_workspace_409(
    client: TestClient, default_workspace: Workspace
) -> None:
    resp = client.delete(f"/workspaces/{default_workspace.id}")
    assert resp.status_code == 409
    assert "default" in resp.json()["detail"].lower()


def test_delete_with_attached_agents_409_then_succeeds_after_detach(
    client: TestClient, db_session: Session
) -> None:
    created = _create(client, "Finance")
    agent = Agent(name="Books", workspace_id=created["id"])
    db_session.add(agent)
    db_session.flush()

    resp = client.delete(f"/workspaces/{created['id']}")
    assert resp.status_code == 409
    assert "1 agent(s)" in resp.json()["detail"]

    agent.workspace_id = None  # reattach to default
    db_session.flush()
    resp = client.delete(f"/workspaces/{created['id']}")
    assert resp.status_code == 204
    assert client.get(f"/workspaces/{created['id']}").status_code == 404


def test_delete_removes_the_workspace_mcp_servers(
    client: TestClient, db_session: Session
) -> None:
    """A workspace owns its MCP servers (Johnny-wks.8): deleting it removes
    them (the FK is ON DELETE CASCADE; the endpoint deletes them explicitly so
    the behavior holds on SQLite too, which does not enforce FKs)."""
    created = _create(client, "Finance")
    db_session.add(
        McpServer(
            workspace_id=created["id"],
            name="ledger",
            transport="stdio",
            command="python3",
        )
    )
    db_session.flush()

    resp = client.delete(f"/workspaces/{created['id']}")
    assert resp.status_code == 204
    # The owned server is gone — no orphan row left behind.
    remaining = db_session.scalars(
        sa.select(McpServer).where(McpServer.workspace_id == created["id"])
    ).all()
    assert remaining == []


# --- delete × container/volume teardown (Johnny-wks.2) -------------------------


@pytest.fixture(autouse=True)
def _scratch_workspaces_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the api-side ``/workspaces`` view at a scratch dir.

    The DELETE endpoint's gog-dir removal (Johnny-wks.4) resolves
    ``JOHNNY_WORKSPACES_DIR`` and rmtrees ``<root>/<slug>/gog`` on the
    explicit ``remove_volume`` opt-in. When this suite runs inside the api
    container that env var points at the REAL host mount — without this
    fixture a ``"Finance"``-slugged delete test once deleted the operator's
    actual ``~/.johnny/workspaces/finance/gog`` keyring mid-suite. Tests
    must never see the real tree.
    """
    root = tmp_path / "workspaces-view"
    monkeypatch.setenv("JOHNNY_WORKSPACES_DIR", str(root))
    return root


class _FakeManager:
    """Stands in for WorkspaceContainerManager in the delete endpoint and
    the wks.5 container-state/start/stop endpoints."""

    def __init__(
        self,
        *,
        raises: bool = False,
        states: dict[int, str] | None = None,
        states_raise: bool = False,
        ensure_ok: bool = True,
    ) -> None:
        self.raises = raises
        self.states = dict(states or {})
        self.states_raise = states_raise
        self.ensure_ok = ensure_ok
        self.calls: list[tuple[int, bool]] = []
        self.ensure_calls: list[tuple[int, str | None]] = []
        self.stop_calls: list[int] = []

    def _error(self, message: str) -> Exception:
        from app.services.workspace_containers import WorkspaceContainerError

        return WorkspaceContainerError(message)

    def retire(self, *, workspace_id: int, remove_volume: bool) -> None:
        if self.raises:
            raise self._error("volume is in use")
        self.calls.append((workspace_id, remove_volume))

    async def ensure_running(
        self, *, workspace_id: int, slug: str | None = None
    ) -> bool:
        self.ensure_calls.append((workspace_id, slug))
        return self.ensure_ok

    def stop_container(self, *, workspace_id: int) -> bool:
        if self.raises:
            raise self._error("container survived the stop")
        self.stop_calls.append(workspace_id)
        return True

    def container_states(self, workspace_ids: list[int]) -> dict[int, str]:
        if self.states_raise:
            raise self._error("daemon unreachable")
        return {
            ws_id: self.states.get(ws_id, "never-started") for ws_id in workspace_ids
        }


@pytest.fixture
def fake_manager(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeManager]:
    """Docker-driving deployment with an injected fake container manager."""
    from typing import cast

    from app.services import workspace_containers

    manager = _FakeManager()
    monkeypatch.setenv("JOHNNY_USE_DOCKER_LAUNCHER", "true")
    workspace_containers.set_workspace_container_manager(
        cast(workspace_containers.WorkspaceContainerManager, manager)
    )
    try:
        yield manager
    finally:
        workspace_containers.set_workspace_container_manager(None)


def test_delete_retires_container_and_skips_volume_by_default(
    client: TestClient, fake_manager: _FakeManager
) -> None:
    created = _create(client, "Finance")
    resp = client.delete(f"/workspaces/{created['id']}")
    assert resp.status_code == 204
    assert fake_manager.calls == [(created["id"], False)]


def test_delete_remove_volume_is_explicit(
    client: TestClient, fake_manager: _FakeManager
) -> None:
    created = _create(client, "Finance")
    resp = client.delete(f"/workspaces/{created['id']}?remove_volume=true")
    assert resp.status_code == 204
    assert fake_manager.calls == [(created["id"], True)]


def test_delete_remove_volume_removes_the_gog_dir(
    client: TestClient,
    fake_manager: _FakeManager,
    _scratch_workspaces_dir: Path,
) -> None:
    """The explicit state-removal choice covers the host-side credentials
    too (Johnny-wks.4) — otherwise a recreated same-slug workspace would
    silently inherit the old one's Google tokens."""
    created = _create(client, "Finance")
    gog_dir = _scratch_workspaces_dir / created["slug"] / "gog" / "data"
    gog_dir.mkdir(parents=True)
    (gog_dir / "keyring-entry").write_text("sealed")
    resp = client.delete(f"/workspaces/{created['id']}?remove_volume=true")
    assert resp.status_code == 204
    assert not (_scratch_workspaces_dir / created["slug"] / "gog").exists()


def test_delete_without_remove_volume_keeps_the_gog_dir(
    client: TestClient,
    fake_manager: _FakeManager,
    _scratch_workspaces_dir: Path,
) -> None:
    created = _create(client, "Finance")
    gog_dir = _scratch_workspaces_dir / created["slug"] / "gog"
    gog_dir.mkdir(parents=True)
    resp = client.delete(f"/workspaces/{created['id']}")
    assert resp.status_code == 204
    assert gog_dir.exists()  # state stays recoverable, like the volume


def test_delete_teardown_failure_409_preserves_the_row(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import cast

    from app.services import workspace_containers

    monkeypatch.setenv("JOHNNY_USE_DOCKER_LAUNCHER", "true")
    workspace_containers.set_workspace_container_manager(
        cast(
            workspace_containers.WorkspaceContainerManager,
            _FakeManager(raises=True),
        )
    )
    try:
        created = _create(client, "Finance")
        resp = client.delete(f"/workspaces/{created['id']}?remove_volume=true")
        assert resp.status_code == 409
        assert "NOT deleted" in resp.json()["detail"]
        assert client.get(f"/workspaces/{created['id']}").status_code == 200
    finally:
        workspace_containers.set_workspace_container_manager(None)


def test_delete_remove_volume_unavailable_without_docker_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Honor-or-refuse: an explicit request this deployment can't carry out
    is a 409, never a silent row delete that strands the volume."""
    monkeypatch.delenv("JOHNNY_USE_DOCKER_LAUNCHER", raising=False)
    created = _create(client, "Finance")
    resp = client.delete(f"/workspaces/{created['id']}?remove_volume=true")
    assert resp.status_code == 409
    assert "unavailable" in resp.json()["detail"]
    assert client.get(f"/workspaces/{created['id']}").status_code == 200
    # Without the flag the plain delete still works in docker-less deployments.
    assert client.delete(f"/workspaces/{created['id']}").status_code == 204


# --- container states + manual start/stop (Johnny-wks.5) -----------------------


def test_container_states_unavailable_without_docker(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JOHNNY_USE_DOCKER_LAUNCHER", raising=False)
    resp = client.get("/workspaces/containers")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert "JOHNNY_USE_DOCKER_LAUNCHER" in body["reason"]
    assert body["states"] == {}


def test_container_states_cover_non_default_workspaces_only(
    client: TestClient,
    fake_manager: _FakeManager,
    default_workspace: Workspace,
) -> None:
    """Per-id states for every non-default workspace; the default is absent
    (its sandbox is the compose service — 'managed', not ours to report)."""
    finance = _create(client, "Finance")
    ops = _create(client, "Ops")
    fake_manager.states = {finance["id"]: "running"}

    body = client.get("/workspaces/containers").json()
    assert body["available"] is True
    states = {int(k): v for k, v in body["states"].items()}
    assert states == {finance["id"]: "running", ops["id"]: "never-started"}
    assert default_workspace.id not in states


def test_container_states_degrade_when_the_daemon_refuses(
    client: TestClient, fake_manager: _FakeManager
) -> None:
    _create(client, "Finance")
    fake_manager.states_raise = True
    body = client.get("/workspaces/containers").json()
    assert body["available"] is False
    assert "daemon unreachable" in body["reason"]


def test_start_container_ensures_with_the_frozen_slug(
    client: TestClient, fake_manager: _FakeManager
) -> None:
    created = _create(client, "Finance Team")
    resp = client.post(f"/workspaces/{created['id']}/container/start")
    assert resp.status_code == 200
    assert resp.json() == {"workspace_id": created["id"], "state": "running"}
    assert fake_manager.ensure_calls == [(created["id"], "finance-team")]


def test_start_container_failure_is_a_502(
    client: TestClient, fake_manager: _FakeManager
) -> None:
    created = _create(client, "Finance")
    fake_manager.ensure_ok = False
    resp = client.post(f"/workspaces/{created['id']}/container/start")
    assert resp.status_code == 502
    assert "failed to start" in resp.json()["detail"]


def test_stop_container_reports_the_post_stop_state(
    client: TestClient, fake_manager: _FakeManager
) -> None:
    created = _create(client, "Finance")
    fake_manager.states = {created["id"]: "stopped"}
    resp = client.post(f"/workspaces/{created['id']}/container/stop")
    assert resp.status_code == 200
    assert resp.json() == {"workspace_id": created["id"], "state": "stopped"}
    assert fake_manager.stop_calls == [created["id"]]


def test_stop_container_survivor_is_a_409(
    client: TestClient, fake_manager: _FakeManager
) -> None:
    created = _create(client, "Finance")
    fake_manager.raises = True
    resp = client.post(f"/workspaces/{created['id']}/container/stop")
    assert resp.status_code == 409
    assert "container stop failed" in resp.json()["detail"]


def test_container_actions_refuse_the_default_workspace(
    client: TestClient, fake_manager: _FakeManager, default_workspace: Workspace
) -> None:
    for action in ("start", "stop"):
        resp = client.post(f"/workspaces/{default_workspace.id}/container/{action}")
        assert resp.status_code == 409
        assert "always-on compose service" in resp.json()["detail"]
    assert fake_manager.ensure_calls == []
    assert fake_manager.stop_calls == []


def test_container_actions_refuse_without_docker(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JOHNNY_USE_DOCKER_LAUNCHER", raising=False)
    created = _create(client, "Finance")
    for action in ("start", "stop"):
        resp = client.post(f"/workspaces/{created['id']}/container/{action}")
        assert resp.status_code == 409
        assert "unavailable" in resp.json()["detail"]


def test_container_actions_unknown_workspace_404(
    client: TestClient, fake_manager: _FakeManager
) -> None:
    assert client.post("/workspaces/9999/container/start").status_code == 404
    assert client.post("/workspaces/9999/container/stop").status_code == 404


# --- storage_dir (the operator-facing state path) -------------------------------


def test_storage_dir_uses_the_host_truth_env(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    default_workspace: Workspace,
) -> None:
    monkeypatch.setenv("JOHNNY_WORKSPACES_HOST_DIR", "/Users/op/.johnny/workspaces/")
    created = _create(client, "Finance Team")
    assert created["storage_dir"] == "/Users/op/.johnny/workspaces/finance-team"
    by_name = {row["name"]: row for row in client.get("/workspaces").json()}
    # The default's state is the sandbox volume, not a per-workspace dir.
    assert by_name["Default"]["storage_dir"] is None


def test_storage_dir_falls_back_to_the_documented_convention(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JOHNNY_WORKSPACES_HOST_DIR", raising=False)
    created = _create(client, "Finance")
    assert created["storage_dir"] == "~/.johnny/workspaces/finance"


# --- agent_count -------------------------------------------------------------


def test_agent_count_explicit_and_default_implicit(
    client: TestClient, db_session: Session, default_workspace: Workspace
) -> None:
    created = _create(client, "Finance")
    db_session.add(Agent(name="Books", workspace_id=created["id"]))
    db_session.add(Agent(name="Johnny"))  # NULL = the default workspace
    db_session.add(Agent(name="Scribe", workspace_id=default_workspace.id))
    db_session.flush()

    by_name = {row["name"]: row for row in client.get("/workspaces").json()}
    assert by_name["Finance"]["agent_count"] == 1
    # The default counts its explicit attachment AND the NULL-attached agent.
    assert by_name["Default"]["agent_count"] == 2
