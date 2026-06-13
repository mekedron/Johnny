"""Per-workspace MCP server management endpoints (Johnny-trt.36 · Johnny-wks.8).

CRUD over ``mcp_servers`` (the provider-settings pattern) scoped to ONE
workspace, plus the probe the management UI's add → probe → enable flow
drives. Every route lives under ``/workspaces/{workspace_id}/mcp-servers``:
an MCP server is OWNED by a workspace, and an agent's MCP toolset is exactly
its workspace's servers (there is no global MCP registry — Johnny-wks.8). A
server reached through the wrong workspace's URL is a 404 (ownership
scoping), never a cross-workspace edit.

* ``POST .../{id}/probe`` — connect with the row's live config (stdio servers
  spawn inside the WORKSPACE'S sandbox container — the default workspace's
  shared skills-sandbox, or a non-default workspace's ``johnny-workspace-<id>``
  lazily ensured first; http servers are dialed directly), initialize,
  ``tools/list``, persist the verdict + tool cache on the row, and report
  every tool with its filter verdict and the qualified catalog kind
  (``mcp__<server>__<tool>``) it will contribute.

Secrets (the env/headers values) are Fernet-encrypted at rest and NEVER
rendered back — responses carry key names only (``env_keys`` /
``header_keys``), the provider-credentials masking model. Catalog liveness
needs no push: assembly reads rows fresh per session (scoped to the agent's
workspace) and the worker reads them fresh per claimed task.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_crypto, get_session
from app.db.models import McpServer, Workspace
from app.security.crypto import CredentialCrypto, CryptoError
from app.services.mcp_servers import (
    cached_tools,
    decrypt_secrets,
    encrypt_secrets,
    get_server_row,
    list_server_rows,
    probe_server_row,
    row_to_config,
    secret_key_names,
)
from johnny.mcp.config import McpConfigError, McpServerConfig, qualified_tool_name
from johnny.skills.sandbox import sandbox_url_for_workspace, sandbox_url_from_env

router = APIRouter(
    prefix="/workspaces/{workspace_id}/mcp-servers", tags=["mcp-servers"]
)

SessionDep = Annotated[Session, Depends(get_session)]
CryptoDep = Annotated[CredentialCrypto, Depends(get_crypto)]


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
    (send ``{}`` to clear; values are write-only and never echoed back)."""

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
    id: int
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
    created_at: datetime
    updated_at: datetime


class McpServerListOut(BaseModel):
    servers: list[McpServerRead]


class McpProbeOut(BaseModel):
    ok: bool
    error: str = ""
    server_info: str = ""
    duration_ms: int = 0
    tools: list[McpToolRead] = Field(default_factory=list)
    catalog_kinds: list[str] = Field(default_factory=list)


def _validate_as_config(row: McpServer) -> None:
    """Run the prospective row through the runtime validator (one truth).

    Secrets are irrelevant to shape validity, so the secretless view
    suffices — the row's encrypted blob is never decrypted here.
    """
    McpServerConfig(
        name=row.name,
        transport=row.transport,
        enabled=bool(row.enabled),
        command=row.command or "",
        args=tuple(str(a) for a in (row.args or [])),
        url=row.url or "",
        tool_include=(
            None if row.tool_include is None else tuple(str(g) for g in row.tool_include)
        ),
        tool_exclude=tuple(str(g) for g in (row.tool_exclude or [])),
        connect_timeout_s=float(row.connect_timeout_s),
        call_timeout_s=float(row.call_timeout_s),
        idle_ttl_s=float(row.idle_ttl_s),
    )


def _tool_reads(row: McpServer) -> list[McpToolRead] | None:
    if row.tools_cache is None:
        return None
    tools = cached_tools(row)
    try:
        config = row_to_config(row, crypto=None)
    except McpConfigError:
        return [McpToolRead(name=t.name, description=t.description) for t in tools]
    kept = set(config.filtered_tool_names([t.name for t in tools]))
    return [
        McpToolRead(
            name=t.name,
            description=t.description,
            included=t.name in kept,
            kind=qualified_tool_name(row.name, t.name) if t.name in kept else "",
        )
        for t in tools
    ]


def _row_to_read(row: McpServer, crypto: CredentialCrypto | None) -> McpServerRead:
    env_keys, header_keys = secret_key_names(crypto, row.secrets_encrypted)
    tools = _tool_reads(row)
    return McpServerRead(
        id=int(row.id),
        workspace_id=int(row.workspace_id),
        name=row.name,
        transport=row.transport,
        enabled=bool(row.enabled),
        command=row.command or "",
        args=[str(a) for a in (row.args or [])],
        url=row.url or "",
        env_keys=env_keys,
        header_keys=header_keys,
        tool_include=(
            None if row.tool_include is None else [str(g) for g in row.tool_include]
        ),
        tool_exclude=[str(g) for g in (row.tool_exclude or [])],
        connect_timeout_s=float(row.connect_timeout_s),
        call_timeout_s=float(row.call_timeout_s),
        idle_ttl_s=float(row.idle_ttl_s),
        tools=tools,
        catalog_kinds=[t.kind for t in (tools or []) if t.kind],
        last_probe_at=row.last_probe_at,
        last_probe_ok=row.last_probe_ok,
        last_probe_error=row.last_probe_error or "",
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _workspace_or_404(db: Session, workspace_id: int) -> Workspace:
    row = db.get(Workspace, workspace_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"workspace {workspace_id} not found",
        )
    return row


def _get_row_or_404(db: Session, workspace_id: int, server_id: int) -> McpServer:
    """The server row, requiring it to belong to ``workspace_id`` (else 404).

    Reaching a server through the wrong workspace's URL is a not-found, never
    a cross-workspace read/edit — the ownership boundary the per-workspace
    relocation establishes (Johnny-wks.8)."""
    row = get_server_row(db, server_id, workspace_id=workspace_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"mcp server {server_id} not found in workspace {workspace_id}",
        )
    return row


def _name_conflict(name: str, workspace_id: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f"an mcp server named {name!r} already exists in workspace "
            f"{workspace_id}"
        ),
    )


async def _probe_sandbox_url(workspace: Workspace) -> str:
    """Where this workspace's stdio probe spawns — its own sandbox container.

    The default workspace uses the shared skills-sandbox (byte-identical to
    pre-wks.8); a non-default workspace's container is lazily ensured first
    (the capabilities-read precedent — ensure never raises, and an
    unreachable sandbox degrades the probe to ``ok=false`` with reason,
    never an error response) then dialed at its canonical endpoint."""
    if workspace.is_default:
        return sandbox_url_from_env()
    from app.services.workspace_containers import (
        ensure_workspace_container_for_stamp,
    )

    await ensure_workspace_container_for_stamp(
        {
            "id": workspace.id,
            "is_default": workspace.is_default,
            "slug": workspace.slug,
        },
        context_label=f"mcp probe (workspace {workspace.id})",
    )
    return sandbox_url_for_workspace(workspace.id)


@router.get("", response_model=McpServerListOut)
def list_mcp_servers(
    workspace_id: int, db: SessionDep, crypto: CryptoDep
) -> McpServerListOut:
    _workspace_or_404(db, workspace_id)
    return McpServerListOut(
        servers=[
            _row_to_read(row, crypto)
            for row in list_server_rows(db, workspace_id=workspace_id)
        ]
    )


@router.post("", response_model=McpServerRead, status_code=status.HTTP_201_CREATED)
def create_mcp_server(
    workspace_id: int, payload: McpServerCreate, db: SessionDep, crypto: CryptoDep
) -> McpServerRead:
    _workspace_or_404(db, workspace_id)
    row = McpServer(
        workspace_id=workspace_id,
        name=payload.name.strip(),
        transport=payload.transport,
        enabled=payload.enabled,
        command=payload.command.strip(),
        args=list(payload.args),
        url=payload.url.strip(),
        secrets_encrypted=encrypt_secrets(
            crypto, env=payload.env, headers=payload.headers
        ),
        tool_include=payload.tool_include,
        tool_exclude=list(payload.tool_exclude),
        connect_timeout_s=payload.connect_timeout_s,
        call_timeout_s=payload.call_timeout_s,
        idle_ttl_s=payload.idle_ttl_s,
    )
    try:
        _validate_as_config(row)
    except McpConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise _name_conflict(row.name, workspace_id) from exc
    db.refresh(row)
    return _row_to_read(row, crypto)


@router.get("/{server_id}", response_model=McpServerRead)
def get_mcp_server(
    workspace_id: int, server_id: int, db: SessionDep, crypto: CryptoDep
) -> McpServerRead:
    return _row_to_read(_get_row_or_404(db, workspace_id, server_id), crypto)


@router.patch("/{server_id}", response_model=McpServerRead)
def update_mcp_server(
    workspace_id: int,
    server_id: int,
    payload: McpServerUpdate,
    db: SessionDep,
    crypto: CryptoDep,
) -> McpServerRead:
    row = _get_row_or_404(db, workspace_id, server_id)
    secrets_touched = payload.env is not None or payload.headers is not None
    env: dict[str, str] = {}
    headers: dict[str, str] = {}
    if secrets_touched:
        try:
            env, headers = decrypt_secrets(crypto, row.secrets_encrypted)
        except (CryptoError, ValueError) as exc:
            # A blob the active key cannot read is replace-only: demand both
            # halves so the rewrite never mixes unreadable leftovers.
            if payload.env is None or payload.headers is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "stored secrets cannot be decrypted with the active key; "
                        "resend both 'env' and 'headers' to replace them"
                    ),
                ) from exc
            env, headers = {}, {}

    if payload.name is not None:
        row.name = payload.name.strip()
    if payload.transport is not None:
        row.transport = payload.transport
    if payload.enabled is not None:
        row.enabled = payload.enabled
    if payload.command is not None:
        row.command = payload.command.strip()
    if payload.args is not None:
        row.args = list(payload.args)
    if payload.url is not None:
        row.url = payload.url.strip()
    if payload.env is not None:
        env = dict(payload.env)
    if payload.headers is not None:
        headers = dict(payload.headers)
    if payload.clear_tool_include:
        row.tool_include = None
    elif payload.tool_include is not None:
        row.tool_include = list(payload.tool_include)
    if payload.tool_exclude is not None:
        row.tool_exclude = list(payload.tool_exclude)
    if payload.connect_timeout_s is not None:
        row.connect_timeout_s = payload.connect_timeout_s
    if payload.call_timeout_s is not None:
        row.call_timeout_s = payload.call_timeout_s
    if payload.idle_ttl_s is not None:
        row.idle_ttl_s = payload.idle_ttl_s

    try:
        _validate_as_config(row)
    except McpConfigError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    if secrets_touched:
        row.secrets_encrypted = encrypt_secrets(crypto, env=env, headers=headers)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise _name_conflict(row.name, workspace_id) from exc
    db.refresh(row)
    return _row_to_read(row, crypto)


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mcp_server(workspace_id: int, server_id: int, db: SessionDep) -> None:
    row = _get_row_or_404(db, workspace_id, server_id)
    db.delete(row)
    db.flush()


@router.post("/{server_id}/probe", response_model=McpProbeOut)
async def probe_mcp_server_endpoint(
    workspace_id: int, server_id: int, db: SessionDep, crypto: CryptoDep
) -> McpProbeOut:
    """Connect + initialize + list tools with the row's LIVE config, in THIS
    workspace's sandbox.

    Persists the verdict on the row (success refreshes ``tools_cache``;
    failure keeps the stale cache and records the error) and reports every
    tool with its include/exclude verdict + the catalog kind it contributes.
    Probing never raises for server-side failures — ``ok=false`` + the
    operator-facing error is the contract the management UI renders inline.
    """
    workspace = _workspace_or_404(db, workspace_id)
    row = _get_row_or_404(db, workspace_id, server_id)
    sandbox_url = await _probe_sandbox_url(workspace)
    try:
        result = await probe_server_row(db, row, crypto, sandbox_url=sandbox_url)
    except (McpConfigError, CryptoError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    tool_reads: list[McpToolRead] = []
    catalog_kinds: list[str] = []
    if result.ok:
        config = row_to_config(row, crypto=None)
        kept = set(config.filtered_tool_names([t.name for t in result.tools]))
        for tool in result.tools:
            included = tool.name in kept
            kind = qualified_tool_name(row.name, tool.name) if included else ""
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
