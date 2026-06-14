"""Tests for the /capabilities inventory API (Johnny-trt.37 / wks.3).

The Skills read (fresh registry scan + per-kind policy verdicts), the
merged Tools catalog (internal → skills → MCP with the policy projected
on), the per-kind toggle that writes the workspace base layer's
``tools_deny`` (Johnny-wks.9) — including the honest-enable case where a glob
keeps the kind denied — plus the Johnny-wks.3 per-workspace keying of both
reads and the skill install flow with its workspace target.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

import app.api.capabilities as capabilities_api
from app.api.deps import get_session
from app.db import Base
from app.db.models import Agent, Workspace
from app.main import app

# --- fixtures -----------------------------------------------------------------


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


class FakeSandboxClient:
    """Stands in for the exec-API client: ``himalaya`` absent, everything else there."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def check_bins(self, names: list[str]) -> Mapping[str, bool]:
        return {name: name != "himalaya" for name in names}

    async def check_env(self, names: list[str]) -> Mapping[str, bool]:
        return {name: False for name in names}

    async def exec(self, **kwargs: Any) -> Any:  # pragma: no cover - no fixture declares checks
        raise AssertionError("no fixture skill declares an availability check")

    async def aclose(self) -> None:
        pass


def _write_skill(root: Path, directory: str, *, name: str, metadata: str = "") -> None:
    lines = ["---", f"name: {name}", f"description: A {name} helper."]
    if metadata:
        lines.append(f"metadata: {metadata}")
    lines += ["---", "", f"Run the {name} steps.", "More detail follows."]
    (root / directory).mkdir(parents=True)
    (root / directory / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture
def skills_volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Two skills: baseline-only ``echoer`` and ``calmail`` missing ``himalaya``."""
    shared = tmp_path / "shared"
    shared.mkdir()
    _write_skill(shared, "echoer", name="echoer")
    _write_skill(
        shared,
        "calmail",
        name="calmail",
        metadata='{"openclaw": {"requires": {"bins": ["himalaya"]}}}',
    )
    monkeypatch.setenv("JOHNNY_SKILLS_DIR", str(shared))
    monkeypatch.setenv("JOHNNY_WORKSPACES_DIR", str(tmp_path / "workspaces"))
    # The in-container test env carries the compose launcher opt-in; the
    # workspace views' lazy ensure must stay a no-op here.
    monkeypatch.delenv("JOHNNY_USE_DOCKER_LAUNCHER", raising=False)
    monkeypatch.setattr(capabilities_api, "SandboxClient", FakeSandboxClient)
    return shared


@pytest.fixture
def finance_workspace(
    db_session: Session, tmp_path: Path, skills_volume: Path
) -> Workspace:
    """A non-default workspace whose own volume carries ``ledger`` only."""
    row = Workspace(name="Finance", slug="finance", is_default=False)
    db_session.add(row)
    db_session.commit()
    _write_skill(
        tmp_path / "workspaces" / "finance" / ".johnny" / "skills", "ledger", name="ledger"
    )
    return row


def _skill(body: dict[str, Any], kind: str) -> dict[str, Any]:
    matches = [s for s in body["skills"] if s["kind"] == kind]
    assert matches, f"skill {kind!r} not in {[s['kind'] for s in body['skills']]}"
    return matches[0]


def _tool(body: dict[str, Any], kind: str) -> dict[str, Any]:
    matches = [t for t in body["tools"] if t["kind"] == kind]
    assert matches, f"kind {kind!r} not in {[t['kind'] for t in body['tools']]}"
    return matches[0]


# --- GET /capabilities/skills ---------------------------------------------------


def test_skills_lists_verdicts_and_names_missing_bins(
    client: TestClient, skills_volume: Path
) -> None:
    res = client.get("/capabilities/skills")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["sandbox"] == "global"
    assert body["skills_dir"] == str(skills_volume)

    echoer = _skill(body, "echoer")
    assert echoer["eligible"] is True
    assert echoer["available"] is True
    assert echoer["enabled"] is True
    assert echoer["body_preview"].startswith("Run the echoer steps.")

    calmail = _skill(body, "calmail")
    assert calmail["eligible"] is False
    assert calmail["missing_bins"] == ["himalaya"]
    # The acceptance contract: a missing bin NAMES the binary.
    assert any("himalaya" in reason for reason in calmail["reasons"])


def test_skills_reflect_policy_disable(
    client: TestClient, skills_volume: Path, default_workspace: Workspace
) -> None:
    toggled = client.post("/capabilities/tools/toggle", json={"kind": "echoer", "enabled": False})
    assert toggled.status_code == 200, toggled.text
    assert toggled.json() == {
        "kind": "echoer",
        "enabled": False,
        "layer": "workspace",
        "rule": "echoer",
        "detail": "Default",
    }

    body = client.get("/capabilities/skills").json()
    echoer = _skill(body, "echoer")
    assert echoer["enabled"] is False
    assert echoer["policy_layer"] == "workspace"
    assert echoer["policy_rule"] == "echoer"
    assert echoer["toggle_managed"] is True
    # Eligibility is orthogonal to the policy switch.
    assert echoer["eligible"] is True


# --- GET /capabilities/tools ----------------------------------------------------


@pytest.fixture
def default_workspace(db_session: Session) -> Workspace:
    """The seeded default workspace MCP rows attach to (Johnny-wks.8): the
    parameterless ``/capabilities/tools`` view resolves MCP onto it."""
    row = Workspace(name="Default", slug="default", is_default=True)
    db_session.add(row)
    db_session.commit()
    return row


def _write_mcp_server(
    slug: str,
    *,
    name: str = "fixture",
    enabled: bool = True,
    tool_exclude: list[str] | None = None,
    tools_cache: list[dict[str, str]] | None = None,
    command: str = "python3",
    args: list[str] | None = None,
) -> None:
    """Write one MCP server (+ its probed tool cache) to a workspace's
    ``.johnny/.mcp.json`` — the file twin of the old ``_mcp_row`` DB insert
    (Johnny-hp1). The store reads ``JOHNNY_WORKSPACES_DIR`` (set by the
    ``skills_volume`` fixture), so this lands under the tmp workspaces root."""
    from johnny.mcp import store

    entry = store.serialize_entry(
        transport="stdio",
        enabled=enabled,
        command=command,
        args=args or ["/opt/sandbox/mcp_fixture_server.py"],
        env={},
        url="",
        headers={},
        tool_include=None,
        tool_exclude=tool_exclude or [],
        connect_timeout_s=10.0,
        call_timeout_s=60.0,
        idle_ttl_s=300.0,
    )
    servers = store.read_servers_raw(slug)
    servers[name] = entry
    store.write_servers_raw(slug, servers)
    store.write_state(
        slug,
        name,
        ok=True,
        error="",
        tools=tools_cache
        if tools_cache is not None
        else [
            {"name": "echo", "description": "Echo a message back."},
            {"name": "always-fail", "description": "Always errors."},
        ],
    )


def test_tools_merges_three_sources_with_policy(
    client: TestClient,
    skills_volume: Path,
    db_session: Session,
    default_workspace: Workspace,
) -> None:
    _write_mcp_server(default_workspace.slug, tool_exclude=["always-fail"])

    res = client.get("/capabilities/tools")
    assert res.status_code == 200, res.text
    body = res.json()
    kinds = [t["kind"] for t in body["tools"]]

    # Internal first (the merge order), skills next, MCP last.
    assert kinds[:2] == ["meeting.leave", "session.end"]
    assert "echoer" in kinds
    assert "mcp__fixture__echo" in kinds
    # The excluded tool contributes no catalog kind.
    assert "mcp__fixture__always-fail" not in kinds
    # Ineligible skills stay out of the catalog (the trt.23 contract).
    assert "calmail" not in kinds

    assert _tool(body, "meeting.leave")["source"] == "internal"
    assert _tool(body, "echoer")["source"] == "skill"
    mcp_echo = _tool(body, "mcp__fixture__echo")
    assert mcp_echo["source"] == "mcp"
    assert mcp_echo["one_liner"] == "Echo a message back."
    assert all(t["allowed"] for t in body["tools"])


def test_tools_browser_mode_marks_meeting_leave_unavailable(
    client: TestClient, skills_volume: Path
) -> None:
    body = client.get("/capabilities/tools", params={"session_mode": "browser"}).json()
    leave = _tool(body, "meeting.leave")
    assert leave["available"] is False
    assert leave["unavailable_reason"]
    assert _tool(body, "session.end")["available"] is True


def test_tools_deny_hides_kind_from_catalog(
    client: TestClient, skills_volume: Path, default_workspace: Workspace
) -> None:
    toggled = client.post("/capabilities/tools/toggle", json={"kind": "echoer", "enabled": False})
    assert toggled.status_code == 200

    body = client.get("/capabilities/tools").json()
    echoer = _tool(body, "echoer")
    assert echoer["allowed"] is False
    assert echoer["policy_layer"] == "workspace"
    assert echoer["policy_rule"] == "echoer"
    assert echoer["toggle_managed"] is True


# --- POST /capabilities/tools/toggle ---------------------------------------------


def test_toggle_unknown_kind_404(client: TestClient, skills_volume: Path) -> None:
    res = client.post("/capabilities/tools/toggle", json={"kind": "no-such-kind", "enabled": False})
    assert res.status_code == 404


def test_toggle_round_trip_restores_enabled(
    client: TestClient, skills_volume: Path, default_workspace: Workspace
) -> None:
    client.post("/capabilities/tools/toggle", json={"kind": "echoer", "enabled": False})
    back = client.post("/capabilities/tools/toggle", json={"kind": "echoer", "enabled": True})
    assert back.status_code == 200
    assert back.json()["enabled"] is True
    assert _skill(client.get("/capabilities/skills").json(), "echoer")["enabled"] is True


def test_toggle_enable_honest_when_glob_still_denies(
    client: TestClient, skills_volume: Path, default_workspace: Workspace
) -> None:
    put = client.put(
        f"/capability-policies/workspaces/{default_workspace.id}",
        json={"tools_deny": ["ech*"]},
    )
    assert put.status_code == 200, put.text

    res = client.post("/capabilities/tools/toggle", json={"kind": "echoer", "enabled": True})
    assert res.status_code == 200
    body = res.json()
    # The exact-kind entry was never there; the glob still denies — the
    # response says so instead of pretending the switch flipped.
    assert body["enabled"] is False
    assert body["layer"] == "workspace"
    assert body["rule"] == "ech*"


def test_toggle_preserves_other_document_fields(
    client: TestClient, skills_volume: Path, default_workspace: Workspace
) -> None:
    put = client.put(
        f"/capability-policies/workspaces/{default_workspace.id}",
        json={"bins_deny": ["curl"], "tools_deny": ["mcp__shady__*"]},
    )
    assert put.status_code == 200, put.text

    client.post("/capabilities/tools/toggle", json={"kind": "echoer", "enabled": False})
    rows = client.get("/capability-policies").json()["rows"]
    [workspace_row] = [r for r in rows if r["scope"] == "workspace"]
    assert workspace_row["document"]["bins_deny"] == ["curl"]
    assert workspace_row["document"]["tools_deny"] == ["mcp__shady__*", "echoer"]


def test_toggle_mcp_kind_uses_cached_tools(
    client: TestClient,
    skills_volume: Path,
    db_session: Session,
    default_workspace: Workspace,
) -> None:
    _write_mcp_server(default_workspace.slug, enabled=False)

    # Disabled server: its cached kinds are still toggle-addressable.
    res = client.post(
        "/capabilities/tools/toggle",
        json={"kind": "mcp__fixture__echo", "enabled": False},
    )
    assert res.status_code == 200
    assert res.json()["enabled"] is False


# --- per-workspace keying (Johnny-wks.3) -----------------------------------------


def test_skills_workspace_view_lists_only_its_own_packages(
    client: TestClient, finance_workspace: Workspace
) -> None:
    """The Finance inventory is its OWN volume: ``ledger`` and nothing
    shared; the parameterless view stays the shared volume with no leak."""
    res = client.get("/capabilities/skills", params={"workspace_id": finance_workspace.id})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["sandbox"] == f"workspace-{finance_workspace.id}"
    assert body["workspace_id"] == finance_workspace.id
    assert body["workspace_slug"] == "finance"
    assert body["skills_dir"].endswith("workspaces/finance/.johnny/skills")
    assert [s["kind"] for s in body["skills"]] == ["ledger"]

    default_body = client.get("/capabilities/skills").json()
    assert default_body["sandbox"] == "global"
    assert default_body["workspace_id"] is None
    kinds = [s["kind"] for s in default_body["skills"]]
    assert "ledger" not in kinds
    assert set(kinds) == {"echoer", "calmail"}


def test_skills_unknown_workspace_404(client: TestClient, skills_volume: Path) -> None:
    assert client.get("/capabilities/skills", params={"workspace_id": 999}).status_code == 404


def test_tools_catalog_derives_from_the_agents_workspace(
    client: TestClient, finance_workspace: Workspace, db_session: Session
) -> None:
    """``agent_id`` resolves the agent's attached workspace exactly like
    dispatch does: its catalog promises the workspace-local skill, never the
    shared volume's — and internal kinds stay present (they are
    session-local, not sandbox inventory)."""
    agent = Agent(name="FinanceBot", workspace_id=finance_workspace.id)
    db_session.add(agent)
    db_session.commit()

    body = client.get("/capabilities/tools", params={"agent_id": agent.id}).json()
    kinds = [t["kind"] for t in body["tools"]]
    assert body["sandbox"] == f"workspace-{finance_workspace.id}"
    assert "ledger" in kinds
    assert "echoer" not in kinds
    assert kinds[:2] == ["meeting.leave", "session.end"]  # locality guard intact

    # An explicit workspace_id wins over the agent derivation.
    explicit = client.get(
        "/capabilities/tools",
        params={"agent_id": agent.id, "workspace_id": finance_workspace.id},
    ).json()
    assert [t["kind"] for t in explicit["tools"]] == kinds


def test_tools_mcp_catalog_is_workspace_scoped(
    client: TestClient,
    default_workspace: Workspace,
    finance_workspace: Workspace,
    db_session: Session,
) -> None:
    """An MCP server on one workspace contributes its kind only to that
    workspace's catalog — two workspaces, two MCP sets (Johnny-wks.8)."""
    _write_mcp_server(default_workspace.slug, name="shared", tool_exclude=["always-fail"])
    _write_mcp_server(finance_workspace.slug, name="ledgermcp", tool_exclude=["always-fail"])
    agent = Agent(name="FinanceBot", workspace_id=finance_workspace.id)
    db_session.add(agent)
    db_session.commit()

    finance_kinds = [
        t["kind"]
        for t in client.get(
            "/capabilities/tools", params={"agent_id": agent.id}
        ).json()["tools"]
    ]
    assert "mcp__ledgermcp__echo" in finance_kinds  # finance's own server
    assert "mcp__shared__echo" not in finance_kinds  # the default's never leaks in

    default_kinds = [
        t["kind"] for t in client.get("/capabilities/tools").json()["tools"]
    ]
    assert "mcp__shared__echo" in default_kinds  # default view = default's servers
    assert "mcp__ledgermcp__echo" not in default_kinds


def test_tools_parameterless_view_never_leaks_workspace_skills(
    client: TestClient, finance_workspace: Workspace
) -> None:
    body = client.get("/capabilities/tools").json()
    assert body["sandbox"] == "global"
    kinds = [t["kind"] for t in body["tools"]]
    assert "ledger" not in kinds
    assert "echoer" in kinds


def test_toggle_addresses_workspace_local_kinds(
    client: TestClient, finance_workspace: Workspace
) -> None:
    """A workspace-local kind is deniable on its OWN workspace's base layer
    (Johnny-wks.9): the toggle is workspace-scoped, and ``_known_kinds`` scans
    every workspace's volume so the kind is addressable."""
    res = client.post(
        "/capabilities/tools/toggle",
        json={"kind": "ledger", "enabled": False, "workspace_id": finance_workspace.id},
    )
    assert res.status_code == 200, res.text
    assert res.json()["enabled"] is False
    assert res.json()["layer"] == "workspace"


# --- POST /capabilities/skills/install (Johnny-trt.32 seam · wks.3) ----------------


def _package(name: str, *, extra_meta: str = "") -> list[dict[str, Any]]:
    skill_md = "\n".join(
        [
            "---",
            f"name: {name}",
            f"description: A {name} helper.",
            *( [f"metadata: {extra_meta}"] if extra_meta else [] ),
            "---",
            "",
            f"Run the {name} steps.",
        ]
    )
    return [
        {"path": "SKILL.md", "content": skill_md},
        {"path": "run.sh", "content": "#!/bin/sh\necho ok\n", "executable": True},
        {"path": "lib/helper.py", "content": "print('ok')\n"},
    ]


def test_install_into_a_workspace_lands_only_there(
    client: TestClient, finance_workspace: Workspace, tmp_path: Path, skills_volume: Path
) -> None:
    res = client.post(
        "/capabilities/skills/install",
        json={"workspace_id": finance_workspace.id, "files": _package("reports")},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["kind"] == "reports"
    assert body["sandbox"] == f"workspace-{finance_workspace.id}"
    assert body["replaced"] is False

    target = tmp_path / "workspaces" / "finance" / ".johnny" / "skills" / "reports"
    assert (target / "SKILL.md").is_file()
    assert (target / "lib" / "helper.py").is_file()
    # The executable flag landed on the script.
    assert (target / "run.sh").stat().st_mode & 0o111
    # That volume ONLY: nothing arrived on the shared one.
    assert not (skills_volume / "reports").exists()

    # The workspace's next inventory read discovers it; the default's never does.
    ws_kinds = [
        s["kind"]
        for s in client.get(
            "/capabilities/skills", params={"workspace_id": finance_workspace.id}
        ).json()["skills"]
    ]
    assert "reports" in ws_kinds
    default_kinds = [
        s["kind"] for s in client.get("/capabilities/skills").json()["skills"]
    ]
    assert "reports" not in default_kinds


def test_install_default_target_lands_on_the_shared_volume(
    client: TestClient, skills_volume: Path
) -> None:
    res = client.post("/capabilities/skills/install", json={"files": _package("notes")})
    assert res.status_code == 201, res.text
    assert res.json()["sandbox"] == "global"
    assert (skills_volume / "notes" / "SKILL.md").is_file()


def test_install_conflict_then_overwrite(
    client: TestClient, finance_workspace: Workspace
) -> None:
    first = client.post(
        "/capabilities/skills/install",
        json={"workspace_id": finance_workspace.id, "files": _package("reports")},
    )
    assert first.status_code == 201
    dup = client.post(
        "/capabilities/skills/install",
        json={"workspace_id": finance_workspace.id, "files": _package("reports")},
    )
    assert dup.status_code == 409
    replaced = client.post(
        "/capabilities/skills/install",
        json={
            "workspace_id": finance_workspace.id,
            "files": _package("reports"),
            "overwrite": True,
        },
    )
    assert replaced.status_code == 201
    assert replaced.json()["replaced"] is True


def test_install_rejects_bad_packages(
    client: TestClient, finance_workspace: Workspace, skills_volume: Path
) -> None:
    def _install(files: list[dict[str, Any]], **extra: Any) -> Any:
        return client.post(
            "/capabilities/skills/install", json={"files": files, **extra}
        )

    # Unknown workspace target.
    assert _install(_package("x"), workspace_id=999).status_code == 404
    # Path traversal / absolute paths.
    bad_path = _package("x")
    bad_path[1]["path"] = "../escape.sh"
    assert _install(bad_path).status_code == 422
    absolute = _package("x")
    absolute[1]["path"] = "/etc/owned"
    assert _install(absolute).status_code == 422
    # No SKILL.md at the package root.
    assert _install([{"path": "run.sh", "content": "x"}]).status_code == 422
    # SKILL.md without a frontmatter name.
    unnamed = [{"path": "SKILL.md", "content": "---\ndescription: x\n---\nbody"}]
    assert _install(unnamed).status_code == 422
    # Internal kinds stay session-local (the locality-guard regression).
    internal = [
        {
            "path": "SKILL.md",
            "content": "---\nname: meeting.leave\ndescription: x\n---\nbody",
        }
    ]
    res = _install(internal)
    assert res.status_code == 422
    assert "internal" in res.json()["detail"]
    # Nothing from the rejected installs reached either volume.
    assert not (skills_volume / "x").exists()
