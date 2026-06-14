"""Per-workspace MCP server management endpoints (Johnny-trt.36 · Johnny-hp1).

CRUD over each workspace's ``.johnny/.mcp.json`` (the FastMCP ``mcpServers``
file — the operator's source of truth, no DB table) plus the probe the
management UI's add → probe → enable flow drives. Every route lives under
``/workspaces/{workspace_id}/mcp-servers``: an MCP server is OWNED by a
workspace, and an agent's MCP toolset is exactly its workspace's servers. A
server is addressed by its NAME (the per-workspace unique slug that also
prefixes ``mcp__<name>__<tool>``), and one reached through the wrong
workspace's file is a 404.

* ``POST .../{name}/probe`` — connect with the server's live config (stdio
  servers spawn inside the WORKSPACE'S sandbox container — every workspace
  lazily ensured first; http servers are dialed directly), initialize,
  ``tools/list``, persist the verdict + tool cache to ``.mcp-state.json``, and
  report every tool with its filter verdict and the qualified catalog kind
  (``mcp__<server>__<tool>``) it contributes.

Secrets (env/headers values) are stored PLAINTEXT / ``${VAR}`` on disk (under
``~/.johnny`` on the operator's host) but NEVER rendered back — responses carry
key names only (``env_keys`` / ``header_keys``). Catalog liveness needs no
push: assembly reads the file fresh per session (scoped to the agent's
workspace) and the worker reads it fresh per claimed task.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.db.models import Workspace
from app.services.mcp_servers import (
    McpServerNameExistsError,
    McpServerNotFoundError,
    ServerRecord,
    create_server,
    delete_server,
    get_server,
    list_servers,
    probe_and_store,
    update_server,
)
from johnny.mcp.config import McpConfigError, qualified_tool_name
from johnny.skills.sandbox import sandbox_url_for_workspace

router = APIRouter(prefix="/workspaces/{workspace_id}/mcp-servers", tags=["mcp-servers"])

SessionDep = Annotated[Session, Depends(get_session)]


class McpServerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    transport: Literal["stdio", "http"]
    enabled: bool = True
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    tool_include: list[str] | None = None
    tool_exclude: list[str] = Field(default_factory=list)
    connect_timeout_s: float = 10.0
    call_timeout_s: float = 60.0
    idle_ttl_s: float = 300.0


class McpServerUpdate(BaseModel):
    """Patch shape — omitted fields stay; ``env`` / ``headers`` replace whole
    (send ``{}`` to clear; values are write-only and never echoed back).
    ``name`` renames the server (and carries its probe state)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    transport: Literal["stdio", "http"] | None = None
    enabled: bool | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    tool_include: list[str] | None = None
    clear_tool_include: bool = False
    tool_exclude: list[str] | None = None
    connect_timeout_s: float | None = None
    call_timeout_s: float | None = None
    idle_ttl_s: float | None = None


class McpToolRead(BaseModel):
    name: str
    description: str = ""
    included: bool = True
    kind: str = ""


class McpServerRead(BaseModel):
    """One server's masked view. Identity is ``name`` (per-workspace unique);
    there is no surrogate id now that the store is a JSON file."""

    workspace_id: int
    name: str
    transport: str
    enabled: bool
    command: str
    args: list[str]
    url: str
    env_keys: list[str]
    header_keys: list[str]
    tool_include: list[str] | None
    tool_exclude: list[str]
    connect_timeout_s: float
    call_timeout_s: float
    idle_ttl_s: float
    tools: list[McpToolRead] | None
    catalog_kinds: list[str]
    last_probe_at: datetime | None
    last_probe_ok: bool | None
    last_probe_error: str


class McpServerListOut(BaseModel):
    servers: list[McpServerRead]


class McpProbeOut(BaseModel):
    ok: bool
    error: str = ""
    server_info: str = ""
    duration_ms: int = 0
    tools: list[McpToolRead] = Field(default_factory=list)
    catalog_kinds: list[str] = Field(default_factory=list)


def _tool_reads(record: ServerRecord) -> list[McpToolRead] | None:
    if not record.has_probe_cache:
        return None
    config = record.config
    kept = set(config.filtered_tool_names([t.name for t in record.tools]))
    return [
        McpToolRead(
            name=t.name,
            description=t.description,
            included=t.name in kept,
            kind=qualified_tool_name(config.name, t.name) if t.name in kept else "",
        )
        for t in record.tools
    ]


def _to_read(workspace_id: int, record: ServerRecord) -> McpServerRead:
    config = record.config
    tools = _tool_reads(record)
    return McpServerRead(
        workspace_id=workspace_id,
        name=config.name,
        transport=config.transport,
        enabled=config.enabled,
        command=config.command,
        args=list(config.args),
        url=config.url,
        env_keys=record.env_keys,
        header_keys=record.header_keys,
        tool_include=None if config.tool_include is None else list(config.tool_include),
        tool_exclude=list(config.tool_exclude),
        connect_timeout_s=config.connect_timeout_s,
        call_timeout_s=config.call_timeout_s,
        idle_ttl_s=config.idle_ttl_s,
        tools=tools,
        catalog_kinds=[t.kind for t in (tools or []) if t.kind],
        last_probe_at=record.last_probe_at,
        last_probe_ok=record.last_probe_ok,
        last_probe_error=record.last_probe_error,
    )


def _workspace_or_404(db: Session, workspace_id: int) -> Workspace:
    row = db.get(Workspace, workspace_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"workspace {workspace_id} not found",
        )
    return row


def _config_error(exc: McpConfigError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


def _name_conflict(name: str, workspace_id: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"an mcp server named {name!r} already exists in workspace {workspace_id}",
    )


def _not_found(name: str, workspace_id: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"mcp server {name!r} not found in workspace {workspace_id}",
    )


async def _probe_sandbox_url(workspace: Workspace) -> str:
    """Where this workspace's stdio probe spawns — its own sandbox container.

    EVERY workspace's container is lazily ensured first — the DEFAULT (id 1)
    included (Johnny-etu.5). Ensure never raises, and an unreachable sandbox
    degrades the probe to ``ok=false`` with reason, never an error response;
    the container is then dialed at its canonical endpoint."""
    from app.services.workspace_containers import ensure_workspace_container_for_stamp

    await ensure_workspace_container_for_stamp(
        {"id": workspace.id, "is_default": workspace.is_default, "slug": workspace.slug},
        context_label=f"mcp probe (workspace {workspace.id})",
    )
    return sandbox_url_for_workspace(workspace.id)


@router.get("", response_model=McpServerListOut)
def list_mcp_servers(workspace_id: int, db: SessionDep) -> McpServerListOut:
    workspace = _workspace_or_404(db, workspace_id)
    return McpServerListOut(
        servers=[
            _to_read(workspace_id, record) for record in list_servers(workspace.slug)
        ]
    )


@router.post("", response_model=McpServerRead, status_code=status.HTTP_201_CREATED)
def create_mcp_server(
    workspace_id: int, payload: McpServerCreate, db: SessionDep
) -> McpServerRead:
    workspace = _workspace_or_404(db, workspace_id)
    try:
        record = create_server(
            workspace.slug,
            name=payload.name,
            transport=payload.transport,
            enabled=payload.enabled,
            command=payload.command,
            args=payload.args,
            env=payload.env,
            url=payload.url,
            headers=payload.headers,
            tool_include=payload.tool_include,
            tool_exclude=payload.tool_exclude,
            connect_timeout_s=payload.connect_timeout_s,
            call_timeout_s=payload.call_timeout_s,
            idle_ttl_s=payload.idle_ttl_s,
        )
    except McpConfigError as exc:
        raise _config_error(exc) from exc
    except McpServerNameExistsError as exc:
        raise _name_conflict(payload.name.strip(), workspace_id) from exc
    return _to_read(workspace_id, record)


@router.get("/{name}", response_model=McpServerRead)
def get_mcp_server(workspace_id: int, name: str, db: SessionDep) -> McpServerRead:
    workspace = _workspace_or_404(db, workspace_id)
    try:
        record = get_server(workspace.slug, name)
    except McpServerNotFoundError as exc:
        raise _not_found(name, workspace_id) from exc
    except McpConfigError as exc:
        raise _config_error(exc) from exc
    return _to_read(workspace_id, record)


@router.patch("/{name}", response_model=McpServerRead)
def update_mcp_server(
    workspace_id: int, name: str, payload: McpServerUpdate, db: SessionDep
) -> McpServerRead:
    workspace = _workspace_or_404(db, workspace_id)
    try:
        record = update_server(
            workspace.slug,
            name,
            new_name=payload.name,
            transport=payload.transport,
            enabled=payload.enabled,
            command=payload.command,
            args=payload.args,
            env=payload.env,
            url=payload.url,
            headers=payload.headers,
            tool_include=payload.tool_include,
            clear_tool_include=payload.clear_tool_include,
            tool_exclude=payload.tool_exclude,
            connect_timeout_s=payload.connect_timeout_s,
            call_timeout_s=payload.call_timeout_s,
            idle_ttl_s=payload.idle_ttl_s,
        )
    except McpServerNotFoundError as exc:
        raise _not_found(name, workspace_id) from exc
    except McpConfigError as exc:
        raise _config_error(exc) from exc
    except McpServerNameExistsError as exc:
        raise _name_conflict((payload.name or name).strip(), workspace_id) from exc
    return _to_read(workspace_id, record)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mcp_server(workspace_id: int, name: str, db: SessionDep) -> None:
    workspace = _workspace_or_404(db, workspace_id)
    try:
        delete_server(workspace.slug, name)
    except McpServerNotFoundError as exc:
        raise _not_found(name, workspace_id) from exc


@router.post("/{name}/probe", response_model=McpProbeOut)
async def probe_mcp_server_endpoint(
    workspace_id: int, name: str, db: SessionDep
) -> McpProbeOut:
    """Connect + initialize + list tools with the server's LIVE config, in THIS
    workspace's sandbox.

    Persists the verdict to ``.mcp-state.json`` (success refreshes the tool
    cache; failure keeps the stale cache and records the error) and reports
    every tool with its include/exclude verdict + the catalog kind it
    contributes. Probing never raises for server-side failures — ``ok=false`` +
    the operator-facing error is the contract the management UI renders inline.
    """
    workspace = _workspace_or_404(db, workspace_id)
    # Resolve the live config name → server exists check before paying the
    # container ensure / SDK import.
    try:
        record = get_server(workspace.slug, name)
    except McpServerNotFoundError as exc:
        raise _not_found(name, workspace_id) from exc
    except McpConfigError as exc:
        raise _config_error(exc) from exc
    sandbox_url = await _probe_sandbox_url(workspace)
    try:
        result = await probe_and_store(workspace.slug, name, sandbox_url=sandbox_url)
    except McpServerNotFoundError as exc:
        raise _not_found(name, workspace_id) from exc
    except McpConfigError as exc:
        raise _config_error(exc) from exc
    tool_reads: list[McpToolRead] = []
    catalog_kinds: list[str] = []
    if result.ok:
        config = record.config
        kept = set(config.filtered_tool_names([t.name for t in result.tools]))
        for tool in result.tools:
            included = tool.name in kept
            kind = qualified_tool_name(config.name, tool.name) if included else ""
            tool_reads.append(
                McpToolRead(
                    name=tool.name,
                    description=tool.description,
                    included=included,
                    kind=kind,
                )
            )
            if kind:
                catalog_kinds.append(kind)
    return McpProbeOut(
        ok=result.ok,
        error=result.error,
        server_info=result.server_info,
        duration_ms=result.duration_ms,
        tools=tool_reads,
        catalog_kinds=catalog_kinds,
    )


__all__ = ["router"]
