"""Capability inventory endpoints (Johnny-trt.37).

The read surfaces behind the management UI's Skills and Tools tabs, plus
the per-kind enable/disable toggle:

* ``GET /capabilities/skills`` — a FRESH volume scan + sandbox probe per
  request (the same :func:`johnny.skills.registry.load_skill_registry` call
  session assembly makes, so what the operator sees is exactly what the
  next session loads — the refresh button is just a re-GET). Each skill
  carries its eligibility/availability verdicts AND the global-policy
  verdict for its kind, so the tab can render eligible / missing-bins /
  unavailable / disabled states from one response.
* ``GET /capabilities/tools`` — the merged task catalog (internal → skills
  → MCP, :func:`johnny.agent.internal_tools.merge_task_catalog`) with the
  resolved policy projected on (:func:`apply_policy_to_catalog`), at the
  scope coordinates the query names. This is the inventory mirror of the
  router's task_catalog: a kind hidden here is hidden from the next
  session's prompts, with the deciding layer/rule attached.
* ``POST /capabilities/tools/toggle`` — per-kind enable/disable, expressed
  through the trt.38 policy engine exactly as its module doc prescribes
  (deny the kind on the GLOBAL layer's ``tools_deny``). The write is
  read-modify-write server-side so the UI never round-trips the whole
  policy document for a one-kind flip. Enabling removes the exact kind
  entry; if a glob or another layer still denies it, the response says so
  (the UI explains instead of showing a toggle that lies).

Inventory is a property of a SANDBOX, not of the app (the Phase-7 note on
the bead): responses carry ``sandbox="global"`` — the only sandbox until
Phase 7 — so per-agent personal sandboxes later appear as additional
inventories under the same shapes, not a rebuild.

No restart anywhere by construction: the registry is scanned per request,
MCP state is read from the rows, and policy writes bite the next session
assembly / the worker's next claimed task (the trt.38 freshness model).
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.services.capability_policies import (
    get_policy_row,
    resolve_capability_policy,
    upsert_policy_row,
)
from app.services.mcp_servers import (
    cached_tools,
    list_server_rows,
    load_server_snapshots,
    row_to_config,
)
from johnny.agent.internal_tools import (
    INTERNAL_TOOL_KINDS,
    internal_catalog_entries,
    merge_task_catalog,
)
from johnny.agent.task_catalog import TaskCatalogEntry
from johnny.mcp.catalog import mcp_catalog_entries
from johnny.mcp.config import McpConfigError, is_mcp_kind, qualified_tool_name
from johnny.skills.capability_policy import (
    POLICY_SCOPE_GLOBAL,
    apply_policy_to_catalog,
)
from johnny.skills.frontmatter import parse_skill_markdown
from johnny.skills.registry import (
    SKILL_FILE_NAME,
    SkillRegistry,
    build_sandbox_availability_runner,
    discover_skill_dirs,
    load_skill_registry,
)
from johnny.skills.sandbox import SandboxClient, skills_dir_from_env

router = APIRouter(prefix="/capabilities", tags=["capabilities"])

SessionDep = Annotated[Session, Depends(get_session)]

SANDBOX_KEY = "global"
"""The one inventory key until Phase 7 brings per-agent personal sandboxes."""

BODY_PREVIEW_CAP = 400
"""Chars of SKILL.md body shown in the list — a preview, not the executor
prompt (progressive disclosure stays intact)."""

SessionModeLiteral = Literal["meet", "browser"]


class SkillRead(BaseModel):
    kind: str
    description: str
    directory: str
    eligible: bool
    reasons: list[str]
    missing_bins: list[str]
    available: bool
    unavailable_reason: str
    keywords: list[str]
    body_preview: str
    enabled: bool
    policy_layer: str
    policy_rule: str
    toggle_managed: bool


class SkillsOut(BaseModel):
    sandbox: str
    skills_dir: str
    skills: list[SkillRead]


ToolSource = Literal["internal", "skill", "mcp"]


class CatalogToolRead(BaseModel):
    kind: str
    source: ToolSource
    one_liner: str
    available: bool
    unavailable_reason: str
    allowed: bool
    policy_layer: str
    policy_rule: str
    toggle_managed: bool


class CatalogOut(BaseModel):
    sandbox: str
    tools: list[CatalogToolRead]


class ToolToggleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    enabled: bool


class ToolToggleOut(BaseModel):
    """The post-toggle truth: what the resolver now says about the kind.

    ``enabled`` is the RESOLVED verdict, not an echo of the request — an
    enable that leaves the kind denied elsewhere (a glob, an allow-list)
    comes back ``enabled=false`` with the deciding ``layer``/``rule`` so the
    UI explains instead of flipping a toggle that changes nothing.
    """

    kind: str
    enabled: bool
    layer: str = ""
    rule: str = ""
    detail: str = ""


async def _load_registry() -> SkillRegistry:
    """One fresh, fully-probed registry load — assembly's exact view."""
    client = SandboxClient()
    try:
        return await load_skill_registry(
            skills_dir_from_env(),
            check_bins=client.check_bins,
            check_env=client.check_env,
            run_check=build_sandbox_availability_runner(client),
        )
    finally:
        await client.aclose()


def _global_deny_list(db: Session) -> list[str]:
    """The global layer's ``tools_deny`` as stored (empty when no row)."""
    row = get_policy_row(db, POLICY_SCOPE_GLOBAL)
    if row is None or not isinstance(row.document, dict):
        return []
    raw = row.document.get("tools_deny")
    return [str(item) for item in raw] if isinstance(raw, list) else []


@router.get("/skills", response_model=SkillsOut)
async def list_skills(db: SessionDep) -> SkillsOut:
    """Every skill on the volume with its verdicts — GET is the refresh."""
    registry = await _load_registry()
    policy = resolve_capability_policy(db)
    deny_list = set(_global_deny_list(db))
    skills: list[SkillRead] = []
    for skill in registry.skills:
        decision = policy.check_tool(skill.name)
        body = skill.document.body.strip()
        if len(body) > BODY_PREVIEW_CAP:
            body = body[: BODY_PREVIEW_CAP - 1].rstrip() + "…"
        skills.append(
            SkillRead(
                kind=skill.name,
                description=skill.description,
                directory=skill.directory,
                eligible=skill.eligible,
                reasons=list(skill.reasons),
                missing_bins=list(skill.missing_bins),
                available=skill.available,
                unavailable_reason=skill.unavailable_reason,
                keywords=list(skill.document.keywords),
                body_preview=body,
                enabled=decision.allowed,
                policy_layer="" if decision.allowed else decision.layer,
                policy_rule="" if decision.allowed else decision.rule,
                toggle_managed=skill.name in deny_list,
            )
        )
    return SkillsOut(sandbox=SANDBOX_KEY, skills_dir=registry.skills_dir, skills=skills)


def _entry_source(kind: str) -> ToolSource:
    if kind in INTERNAL_TOOL_KINDS:
        return "internal"
    if is_mcp_kind(kind):
        return "mcp"
    return "skill"


@router.get("/tools", response_model=CatalogOut)
async def list_tools(
    db: SessionDep,
    agent_id: int | None = None,
    session_mode: SessionModeLiteral | None = None,
    bot_session_id: int | None = None,
) -> CatalogOut:
    """The merged catalog with the policy projected on, at the given coordinates.

    Mirrors session assembly: internal → skills → MCP through
    :func:`merge_task_catalog`, then :func:`apply_policy_to_catalog` with the
    policy resolved for the coordinates (none → the global view).
    ``session_mode=browser`` renders the browser surface (``meeting.leave``
    unavailable); the default inventory view shows the Meet-shaped catalog.
    """
    registry = await _load_registry()
    merged = merge_task_catalog(
        internal_catalog_entries(meeting_backed=session_mode != "browser"),
        registry.catalog_entries(),
        mcp_catalog_entries(load_server_snapshots(db)),
    )
    policy = resolve_capability_policy(
        db,
        agent_id=agent_id,
        session_mode=session_mode,
        bot_session_id=bot_session_id,
    )
    deny_list = set(_global_deny_list(db))
    tools = [_catalog_read(entry, deny_list) for entry in apply_policy_to_catalog(merged, policy)]
    return CatalogOut(sandbox=SANDBOX_KEY, tools=tools)


def _catalog_read(entry: TaskCatalogEntry, deny_list: set[str]) -> CatalogToolRead:
    return CatalogToolRead(
        kind=entry.kind,
        source=_entry_source(entry.kind),
        one_liner=entry.one_liner,
        available=entry.available,
        unavailable_reason=entry.unavailable_reason,
        allowed=not entry.hidden,
        policy_layer=entry.policy_layer,
        policy_rule=entry.policy_rule,
        toggle_managed=entry.kind in deny_list,
    )


def _known_kinds(db: Session) -> frozenset[str]:
    """Every kind the toggle may address: internal + volume skills + MCP cache.

    Skills come from a parse-only scan (no sandbox probes — ineligible and
    unavailable skills are still toggleable, the management point). MCP kinds
    come from every row's cached tools with the row's filters applied,
    DISABLED servers included: a deny written while a server is off must be
    placeable, and it keeps holding when the server is re-enabled.
    """
    kinds: set[str] = set(INTERNAL_TOOL_KINDS)
    for directory in discover_skill_dirs(skills_dir_from_env()):
        try:
            text = (directory / SKILL_FILE_NAME).read_text(encoding="utf-8")
        except OSError:
            kinds.add(directory.name)
            continue
        kinds.add(parse_skill_markdown(text).name or directory.name)
    for row in list_server_rows(db):
        tools = cached_tools(row)
        if not tools:
            continue
        try:
            config = row_to_config(row, crypto=None)
        except McpConfigError:
            continue
        for name in config.filtered_tool_names([t.name for t in tools]):
            kinds.add(qualified_tool_name(row.name, name))
    return frozenset(kinds)


@router.post("/tools/toggle", response_model=ToolToggleOut)
def toggle_tool(payload: ToolToggleIn, db: SessionDep) -> ToolToggleOut:
    """Flip one kind on/off via the global layer's ``tools_deny`` (trt.38).

    Body-based (not a path param) so kinds keep their exact spelling —
    ``meeting.leave``, ``mcp__server__tool`` — without URL-encoding rules.
    """
    kind = payload.kind.strip()
    if not kind:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="kind must be non-empty",
        )
    if kind not in _known_kinds(db):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown capability kind {kind!r}",
        )
    row = get_policy_row(db, POLICY_SCOPE_GLOBAL)
    document = dict(row.document) if row is not None and isinstance(row.document, dict) else {}
    raw = document.get("tools_deny")
    deny = [str(item) for item in raw] if isinstance(raw, list) else []
    if payload.enabled:
        deny = [item for item in deny if item != kind]
    elif kind not in deny:
        deny.append(kind)
    document["tools_deny"] = deny
    upsert_policy_row(db, POLICY_SCOPE_GLOBAL, document)

    decision = resolve_capability_policy(db).check_tool(kind)
    return ToolToggleOut(
        kind=kind,
        enabled=decision.allowed,
        layer="" if decision.allowed else decision.layer,
        rule="" if decision.allowed else decision.rule,
        detail="" if decision.allowed else decision.detail,
    )


__all__ = ["router"]
