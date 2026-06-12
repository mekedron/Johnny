"""Tests for the /workspaces CRUD API (Johnny-wks.1).

The bead's CRUD matrix: create derives a frozen unique slug, names are
unique (409), rename never touches the slug, the seeded default workspace
is non-deletable (409), and delete is blocked while agents are attached
(409 with the count). ``agent_count`` reports effective attachment —
explicit rows everywhere, plus the NULL-attached agents on the default.
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
from app.db.models import Agent, Workspace
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


# --- delete × container/volume teardown (Johnny-wks.2) -------------------------


class _FakeManager:
    """Stands in for WorkspaceContainerManager in the delete endpoint."""

    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises
        self.calls: list[tuple[int, bool]] = []

    def retire(self, *, workspace_id: int, remove_volume: bool) -> None:
        if self.raises:
            from app.services.workspace_containers import WorkspaceContainerError

            raise WorkspaceContainerError("volume is in use")
        self.calls.append((workspace_id, remove_volume))


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
