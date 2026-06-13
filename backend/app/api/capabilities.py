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
the bead) — and since Johnny-wks.3 the sandboxes are WORKSPACES: both reads
take an optional ``workspace_id`` and key the whole view by it. The default
workspace (or no parameter) is the original ``sandbox="global"`` inventory,
byte-identical; a non-default workspace's view scans ITS packages
(``~/.johnny/workspaces/<slug>/skills``) and probes ITS container
(``johnny-workspace-<id>``, lazily ensured first — the GET is the refresh),
reported under ``sandbox="workspace-<id>"``. ``GET /capabilities/tools``
additionally mirrors session assembly's derivation when given ``agent_id``:
no explicit workspace means the AGENT'S attached workspace, the same
resolution dispatch performs — so what this returns for an agent is exactly
what its next session's catalog promises.

``POST /capabilities/skills/install`` is the skill install flow
(Johnny-trt.32's seam, consumed by wks.3 as a WORKSPACE choice): a skill
package lands in the target workspace's volume only — the default target
writes today's shared volume; internal kinds are never installable
(locality guard: they stay session-local).

No restart anywhere by construction: the registry is scanned per request,
MCP state is read from the rows, and policy writes bite the next session
assembly / the worker's next claimed task (the trt.38 freshness model).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.db.models import Agent, Workspace
from app.services.capability_policies import (
    get_policy_row,
    resolve_capability_policy,
    upsert_policy_row,
)
from app.services.mcp_servers import (
    cached_tools,
    list_server_rows,
    load_server_snapshots,
    resolve_mcp_workspace_id,
    row_to_config,
)
from app.services.workspaces import resolve_agent_workspace
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
from johnny.skills.sandbox import (
    SandboxClient,
    sandbox_url_for_workspace,
    skills_dir_from_env,
    workspace_skills_dir,
)

router = APIRouter(prefix="/capabilities", tags=["capabilities"])

SessionDep = Annotated[Session, Depends(get_session)]

SANDBOX_KEY = "global"
"""The default workspace's inventory key — the only sandbox before wks.3."""

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
    # The workspace whose sandbox this inventory describes (Johnny-wks.3).
    # ``None``/empty for the parameterless default view (no row resolved).
    workspace_id: int | None = None
    workspace_slug: str = ""
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
    workspace_id: int | None = None
    workspace_slug: str = ""
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


def _workspace_or_404(db: Session, workspace_id: int | None) -> Workspace | None:
    """The named workspace row, ``None`` for the parameterless default view."""
    if workspace_id is None:
        return None
    row = db.get(Workspace, workspace_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"workspace {workspace_id} not found",
        )
    return row


def _sandbox_key(workspace: Workspace | None) -> str:
    """The inventory key responses carry — ``global`` is the default's name."""
    if workspace is None or workspace.is_default:
        return SANDBOX_KEY
    return f"workspace-{workspace.id}"


def _workspace_skills_root(workspace: Workspace | None) -> str:
    """Where this workspace's packages are discovered/installed (api view)."""
    if workspace is None or workspace.is_default:
        return skills_dir_from_env()
    return workspace_skills_dir(workspace.slug)


async def _load_registry(workspace: Workspace | None = None) -> SkillRegistry:
    """One fresh, fully-probed registry load — assembly's exact view of the
    workspace's sandbox (Johnny-wks.3: discovery AND probes are keyed by
    workspace, mirroring the session/worker resolver seams).

    A non-default workspace's container is lazily ensured first — the GET
    is the refresh, and an inventory probed against a stopped container
    would report could-not-verify for everything. Ensure never raises and
    no-ops where docker isn't driven; an unreachable sandbox still degrades
    to honest unavailable verdicts, never an error response.
    """
    if workspace is None or workspace.is_default:
        client = SandboxClient()
    else:
        from app.services.workspace_containers import (
            ensure_workspace_container_for_stamp,
        )

        await ensure_workspace_container_for_stamp(
            {
                "id": workspace.id,
                "is_default": workspace.is_default,
                "slug": workspace.slug,
            },
            context_label=f"capabilities read (workspace {workspace.id})",
        )
        client = SandboxClient(base_url=sandbox_url_for_workspace(workspace.id))
    try:
        return await load_skill_registry(
            _workspace_skills_root(workspace),
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
async def list_skills(db: SessionDep, workspace_id: int | None = None) -> SkillsOut:
    """Every skill on the workspace's volume with its verdicts — GET is the
    refresh. No ``workspace_id`` (or the default's) keeps the original
    ``global`` view byte-identical."""
    workspace = _workspace_or_404(db, workspace_id)
    registry = await _load_registry(workspace)
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
    return SkillsOut(
        sandbox=_sandbox_key(workspace),
        skills_dir=registry.skills_dir,
        workspace_id=workspace.id if workspace is not None else None,
        workspace_slug=workspace.slug if workspace is not None else "",
        skills=skills,
    )


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
    workspace_id: int | None = None,
) -> CatalogOut:
    """The merged catalog with the policy projected on, at the given coordinates.

    Mirrors session assembly: internal → skills → MCP through
    :func:`merge_task_catalog`, then :func:`apply_policy_to_catalog` with the
    policy resolved for the coordinates (none → the global view).
    ``session_mode=browser`` renders the browser surface (``meeting.leave``
    unavailable); the default inventory view shows the Meet-shaped catalog.

    The skills leg is WORKSPACE-keyed (Johnny-wks.3): an explicit
    ``workspace_id`` names the sandbox; otherwise ``agent_id`` derives it the
    way dispatch does (:func:`resolve_agent_workspace` — the agent's attached
    workspace), so this is exactly the catalog that agent's next session
    renders into its prompt blocks. Internal and MCP kinds are
    workspace-independent by construction — internal kinds run in the live
    agent process and never enter ANY workspace container (the trt.57
    locality guard); MCP stdio servers spawn in the task's resolved sandbox
    at claim time.
    """
    workspace = _workspace_or_404(db, workspace_id)
    if workspace is None and agent_id is not None:
        agent = db.get(Agent, agent_id)
        if agent is not None:
            workspace = resolve_agent_workspace(db, agent)
    registry = await _load_registry(workspace)
    # MCP is workspace-keyed too (Johnny-wks.8): the same workspace the skills
    # leg resolved above owns the MCP set, so this mirrors what that agent's
    # next session renders. No workspace/agent → the default workspace's
    # servers (the parameterless ``global`` view, byte-identical to pre-wks.8).
    mcp_workspace_id = resolve_mcp_workspace_id(
        db,
        workspace_id=workspace.id if workspace is not None else None,
        is_default=workspace.is_default if workspace is not None else True,
    )
    merged = merge_task_catalog(
        internal_catalog_entries(meeting_backed=session_mode != "browser"),
        registry.catalog_entries(),
        mcp_catalog_entries(load_server_snapshots(db, workspace_id=mcp_workspace_id)),
    )
    policy = resolve_capability_policy(
        db,
        agent_id=agent_id,
        session_mode=session_mode,
        bot_session_id=bot_session_id,
    )
    deny_list = set(_global_deny_list(db))
    tools = [_catalog_read(entry, deny_list) for entry in apply_policy_to_catalog(merged, policy)]
    return CatalogOut(
        sandbox=_sandbox_key(workspace),
        workspace_id=workspace.id if workspace is not None else None,
        workspace_slug=workspace.slug if workspace is not None else "",
        tools=tools,
    )


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


def _volume_kinds(root: str) -> set[str]:
    """Parse-only kind scan of one skills volume (no sandbox probes)."""
    kinds: set[str] = set()
    for directory in discover_skill_dirs(root):
        try:
            text = (directory / SKILL_FILE_NAME).read_text(encoding="utf-8")
        except OSError:
            kinds.add(directory.name)
            continue
        kinds.add(parse_skill_markdown(text).name or directory.name)
    return kinds


def _known_kinds(db: Session) -> frozenset[str]:
    """Every kind the toggle may address: internal + volume skills + MCP cache.

    Skills come from a parse-only scan (no sandbox probes — ineligible and
    unavailable skills are still toggleable, the management point) of the
    shared volume AND every workspace's own volume (Johnny-wks.3): a deny on
    a workspace-local kind must be placeable from the global tab. MCP kinds
    come from every row's cached tools with the row's filters applied,
    DISABLED servers included: a deny written while a server is off must be
    placeable, and it keeps holding when the server is re-enabled.
    """
    kinds: set[str] = set(INTERNAL_TOOL_KINDS)
    kinds |= _volume_kinds(skills_dir_from_env())
    for workspace in db.scalars(select(Workspace).where(Workspace.is_default.is_(False))):
        kinds |= _volume_kinds(workspace_skills_dir(workspace.slug))
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


# --- POST /capabilities/skills/install (Johnny-trt.32 seam · Johnny-wks.3) -----

_SKILL_DIR_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
"""Installable skill names double as directory names — keep them filesystem-
and container-label-safe (the registry keys kinds by this name)."""

_INSTALL_MAX_FILES = 64
_INSTALL_MAX_TOTAL_BYTES = 2 * 1024 * 1024
"""Skill packages are SKILL.md + a few scripts — config-sized, never model
artifacts. The caps keep a stray upload from filling the volume."""


class SkillInstallFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=255)
    content: str
    executable: bool = False


class SkillInstallIn(BaseModel):
    """One skill package addressed to one workspace's volume.

    ``workspace_id=None`` targets the DEFAULT workspace — today's shared
    volume, exactly what the pre-workspaces drop-a-folder flow meant (the
    trt.32 target parameter's constant-``global`` reading).
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: int | None = None
    files: list[SkillInstallFile] = Field(min_length=1, max_length=_INSTALL_MAX_FILES)
    overwrite: bool = False


class SkillInstallOut(BaseModel):
    kind: str
    directory: str
    sandbox: str
    workspace_id: int | None = None
    workspace_slug: str = ""
    replaced: bool = False


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _validated_relative_path(raw: str) -> str:
    """A package-relative POSIX path, or 422 — the traversal guard."""
    if "\\" in raw or "\x00" in raw:
        raise _unprocessable(f"invalid path {raw!r}: backslashes are not allowed")
    if raw.startswith("/"):
        raise _unprocessable(f"invalid path {raw!r}: must be package-relative")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise _unprocessable(
            f"invalid path {raw!r}: empty, '.' and '..' segments are not allowed"
        )
    return "/".join(parts)


@router.post(
    "/skills/install",
    response_model=SkillInstallOut,
    status_code=status.HTTP_201_CREATED,
)
def install_skill(payload: SkillInstallIn, db: SessionDep) -> SkillInstallOut:
    """Land one skill package in the TARGET workspace's volume — and only there.

    The Johnny-trt.32 install flow with its target parameter consumed as a
    workspace choice (Johnny-wks.3): the default target writes the shared
    volume every pre-workspaces consumer scans; a non-default target writes
    ``~/.johnny/workspaces/<slug>/skills/<name>``, which ONLY that
    workspace's container mounts and only its sessions' catalogs discover.
    No restart, no reload call: the next ``GET /capabilities/skills`` /
    session assembly / worker claim re-scans the volume (the trt.37
    freshness model — for the worker, the kind-miss refresh covers a kind
    installed after its snapshot was cached).

    Validation is deliberately strict — the operator is right there, so a
    defective package is a 422 naming the defect, not a listed-ineligible
    surprise later. Internal kinds are never installable: they are
    session-local by the trt.57 locality guard, and a skill shadowing one
    could otherwise smuggle that name toward a sandbox.
    """
    workspace = _workspace_or_404(db, payload.workspace_id)

    seen: set[str] = set()
    skill_md: str | None = None
    total_bytes = 0
    for entry in payload.files:
        path = _validated_relative_path(entry.path)
        if path in seen:
            raise _unprocessable(f"duplicate file path {path!r}")
        seen.add(path)
        total_bytes += len(entry.content.encode("utf-8"))
        if path == SKILL_FILE_NAME:
            skill_md = entry.content
    if skill_md is None:
        raise _unprocessable(f"the package must include a root-level {SKILL_FILE_NAME}")
    if total_bytes > _INSTALL_MAX_TOTAL_BYTES:
        raise _unprocessable(
            f"package too large ({total_bytes} bytes; cap "
            f"{_INSTALL_MAX_TOTAL_BYTES}) — skill packages are scripts, not artifacts"
        )

    document = parse_skill_markdown(skill_md)
    if document.problems:
        raise _unprocessable(
            "SKILL.md does not parse cleanly: " + "; ".join(document.problems)
        )
    kind = document.name
    if not kind:
        raise _unprocessable("SKILL.md must declare a frontmatter name")
    if not _SKILL_DIR_NAME_RE.match(kind):
        raise _unprocessable(
            f"skill name {kind!r} is not directory-safe "
            "(letters/digits then letters/digits/._- only, max 64 chars)"
        )
    if kind in INTERNAL_TOOL_KINDS:
        raise _unprocessable(
            f"{kind!r} is an internal session-local kind (locality guard) — "
            "it can never be installed into a sandbox"
        )

    target = Path(_workspace_skills_root(workspace)) / kind
    replaced = target.is_dir()
    if replaced and not payload.overwrite:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"skill {kind!r} already exists in {_sandbox_key(workspace)} — "
                "pass overwrite=true to replace it"
            ),
        )
    try:
        if replaced:
            shutil.rmtree(target)
        for entry in payload.files:
            destination = target / _validated_relative_path(entry.path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(entry.content, encoding="utf-8")
            if entry.executable:
                destination.chmod(0o755)
    except OSError as exc:
        # Half-written packages would scan as defective skills; clear the
        # debris so a retry starts clean.
        shutil.rmtree(target, ignore_errors=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"could not write the skill package to {target}: {exc}",
        ) from exc

    return SkillInstallOut(
        kind=kind,
        directory=str(target),
        sandbox=_sandbox_key(workspace),
        workspace_id=workspace.id if workspace is not None else None,
        workspace_slug=workspace.slug if workspace is not None else "",
        replaced=replaced,
    )


__all__ = ["router"]
