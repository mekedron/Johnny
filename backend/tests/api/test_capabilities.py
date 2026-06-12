"""Tests for the /capabilities inventory API (Johnny-trt.37).

The Skills read (fresh registry scan + per-kind policy verdicts), the
merged Tools catalog (internal → skills → MCP with the policy projected
on), and the per-kind toggle that writes the global layer's ``tools_deny``
— including the honest-enable case where a glob keeps the kind denied.
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
from app.db.models import McpServer
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
    _write_skill(tmp_path, "echoer", name="echoer")
    _write_skill(
        tmp_path,
        "calmail",
        name="calmail",
        metadata='{"openclaw": {"requires": {"bins": ["himalaya"]}}}',
    )
    monkeypatch.setenv("JOHNNY_SKILLS_DIR", str(tmp_path))
    monkeypatch.setattr(capabilities_api, "SandboxClient", FakeSandboxClient)
    return tmp_path


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


def test_skills_reflect_policy_disable(client: TestClient, skills_volume: Path) -> None:
    toggled = client.post("/capabilities/tools/toggle", json={"kind": "echoer", "enabled": False})
    assert toggled.status_code == 200, toggled.text
    assert toggled.json() == {
        "kind": "echoer",
        "enabled": False,
        "layer": "global",
        "rule": "echoer",
        "detail": "",
    }

    body = client.get("/capabilities/skills").json()
    echoer = _skill(body, "echoer")
    assert echoer["enabled"] is False
    assert echoer["policy_layer"] == "global"
    assert echoer["policy_rule"] == "echoer"
    assert echoer["toggle_managed"] is True
    # Eligibility is orthogonal to the policy switch.
    assert echoer["eligible"] is True


# --- GET /capabilities/tools ----------------------------------------------------


def _mcp_row(**overrides: Any) -> McpServer:
    fields: dict[str, Any] = {
        "name": "fixture",
        "transport": "stdio",
        "enabled": True,
        "command": "python3",
        "args": ["/opt/sandbox/mcp_fixture_server.py"],
        "url": "",
        "tools_cache": [
            {"name": "echo", "description": "Echo a message back."},
            {"name": "always-fail", "description": "Always errors."},
        ],
        "last_probe_ok": True,
        "last_probe_error": "",
    }
    fields.update(overrides)
    return McpServer(**fields)


def test_tools_merges_three_sources_with_policy(
    client: TestClient, skills_volume: Path, db_session: Session
) -> None:
    db_session.add(_mcp_row(tool_exclude=["always-fail"]))
    db_session.commit()

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


def test_tools_deny_hides_kind_from_catalog(client: TestClient, skills_volume: Path) -> None:
    toggled = client.post("/capabilities/tools/toggle", json={"kind": "echoer", "enabled": False})
    assert toggled.status_code == 200

    body = client.get("/capabilities/tools").json()
    echoer = _tool(body, "echoer")
    assert echoer["allowed"] is False
    assert echoer["policy_layer"] == "global"
    assert echoer["policy_rule"] == "echoer"
    assert echoer["toggle_managed"] is True


# --- POST /capabilities/tools/toggle ---------------------------------------------


def test_toggle_unknown_kind_404(client: TestClient, skills_volume: Path) -> None:
    res = client.post("/capabilities/tools/toggle", json={"kind": "no-such-kind", "enabled": False})
    assert res.status_code == 404


def test_toggle_round_trip_restores_enabled(client: TestClient, skills_volume: Path) -> None:
    client.post("/capabilities/tools/toggle", json={"kind": "echoer", "enabled": False})
    back = client.post("/capabilities/tools/toggle", json={"kind": "echoer", "enabled": True})
    assert back.status_code == 200
    assert back.json()["enabled"] is True
    assert _skill(client.get("/capabilities/skills").json(), "echoer")["enabled"] is True


def test_toggle_enable_honest_when_glob_still_denies(
    client: TestClient, skills_volume: Path
) -> None:
    put = client.put("/capability-policies/global", json={"tools_deny": ["ech*"]})
    assert put.status_code == 200, put.text

    res = client.post("/capabilities/tools/toggle", json={"kind": "echoer", "enabled": True})
    assert res.status_code == 200
    body = res.json()
    # The exact-kind entry was never there; the glob still denies — the
    # response says so instead of pretending the switch flipped.
    assert body["enabled"] is False
    assert body["layer"] == "global"
    assert body["rule"] == "ech*"


def test_toggle_preserves_other_document_fields(client: TestClient, skills_volume: Path) -> None:
    put = client.put(
        "/capability-policies/global",
        json={"bins_deny": ["curl"], "tools_deny": ["mcp__shady__*"]},
    )
    assert put.status_code == 200, put.text

    client.post("/capabilities/tools/toggle", json={"kind": "echoer", "enabled": False})
    rows = client.get("/capability-policies").json()["rows"]
    [global_row] = [r for r in rows if r["scope"] == "global"]
    assert global_row["document"]["bins_deny"] == ["curl"]
    assert global_row["document"]["tools_deny"] == ["mcp__shady__*", "echoer"]


def test_toggle_mcp_kind_uses_cached_tools(
    client: TestClient, skills_volume: Path, db_session: Session
) -> None:
    db_session.add(_mcp_row(enabled=False))
    db_session.commit()

    # Disabled server: its cached kinds are still toggle-addressable.
    res = client.post(
        "/capabilities/tools/toggle",
        json={"kind": "mcp__fixture__echo", "enabled": False},
    )
    assert res.status_code == 200
    assert res.json()["enabled"] is False
