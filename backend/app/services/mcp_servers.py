"""Per-workspace MCP server CRUD — the api's file-backed service (Johnny-hp1).

The source of truth is each workspace's ``.johnny/.mcp.json`` (FastMCP
``mcpServers`` format — see :mod:`johnny.mcp.store`); there is no DB table
anymore. This module is the api's view over that file: list / get / create /
update / delete + the probe the management UI's add → probe → enable flow
drives. It returns :class:`ServerRecord` value objects (a validated
:class:`~johnny.mcp.config.McpServerConfig` + masked secret-key names + probe
state) that :mod:`app.api.mcp_servers` maps to its read schema.

The worker (claim-time configs) and session assembly (catalog snapshots) read
the store DIRECTLY (:func:`johnny.mcp.store.load_server_configs` /
:func:`~johnny.mcp.store.load_server_snapshots`) — they never come through
here, so the hot paths carry no app-layer dependency.

Secrets are PLAINTEXT / ``${VAR}`` on disk (the operator's chosen trade) but
never echoed back: responses carry key names only (``env_keys`` /
``header_keys``), the provider-credentials masking model preserved across the
DB→file cutover.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from johnny.mcp import store
from johnny.mcp.catalog import McpToolInfo
from johnny.mcp.config import McpConfigError, McpServerConfig

logger = logging.getLogger(__name__)


class McpServerNotFoundError(LookupError):
    """No server with that name in the workspace's config file (api → 404)."""


class McpServerNameExistsError(ValueError):
    """A server with that name already exists in the file (api → 409)."""


@dataclass(frozen=True, slots=True)
class ServerRecord:
    """One server as the api renders it: validated config + masked secrets +
    probe state.

    ``config.env`` / ``config.headers`` are EMPTY here (the secretless view);
    only the key names survive, in ``env_keys`` / ``header_keys``.
    ``has_probe_cache`` distinguishes never-probed (api ``tools=null``) from
    probed-zero-tools (``tools=[]``).
    """

    config: McpServerConfig
    env_keys: list[str]
    header_keys: list[str]
    tools: tuple[McpToolInfo, ...]
    has_probe_cache: bool
    last_probe_at: datetime | None
    last_probe_ok: bool | None
    last_probe_error: str


def slug_for_stamp(workspace_id: int | None, slug: str | None) -> str | None:
    """The ``.mcp.json`` slug for a workspace STAMP (worker claim / session config).

    The file twin of the old ``resolve_mcp_workspace_id`` for the hot paths,
    minus the DB hit: a stampless legacy claim (``workspace_id is None``)
    resolves to the default workspace's servers (where the old global set was
    mapped / where n8n is seeded); every stamped workspace — the default
    (slug ``default``) included — uses its OWN slug. A stamped row with no
    usable slug yields ``None`` (the file can't be located; load nothing — the
    skills-dir resolver's ``None`` branch), never a wrong-workspace read.
    """
    if workspace_id is None:
        from app.services.workspaces import DEFAULT_WORKSPACE_SLUG

        return DEFAULT_WORKSPACE_SLUG
    return slug or None


def resolve_mcp_slug(db: Session, workspace: Any | None) -> str | None:
    """The slug whose ``.mcp.json`` applies — the file twin of the old
    ``resolve_mcp_workspace_id``.

    A concrete workspace owns its servers by its own (frozen) slug; ``None``
    (the parameterless capability catalog view) resolves to the default
    workspace's slug — where n8n is seeded — byte-for-byte the behavior the
    DB resolver gave by mapping the default/legacy stamp onto the default
    workspace. ``None`` only on an unseeded schema with no default workspace:
    callers then load no servers (the promise-nothing degrade).
    """
    if workspace is not None:
        return workspace.slug
    from app.services.workspaces import select_default_workspace

    default = select_default_workspace(db)
    return default.slug if default is not None else None


def _probe_at(state: dict[str, Any]) -> datetime | None:
    raw = store.state_probe_at(state)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _validate_intent(name: str, fields: dict[str, Any]) -> None:
    """Run the RAW intended fields through the one validator.

    ``store.serialize_entry`` writes only transport-appropriate keys (it drops
    a command on an http server, a url on a stdio one), so a contradictory
    payload would otherwise be silently sanitized instead of rejected. Validate
    the operator's actual intent first → 422 on http+command, stdio+url, a bad
    name, etc.
    """
    McpServerConfig(
        name=name,
        transport=fields["transport"],
        enabled=fields["enabled"],
        command=fields["command"],
        args=tuple(fields["args"]),
        url=fields["url"],
        tool_include=(
            None if fields["tool_include"] is None else tuple(fields["tool_include"])
        ),
        tool_exclude=tuple(fields["tool_exclude"]),
        connect_timeout_s=fields["connect_timeout_s"],
        call_timeout_s=fields["call_timeout_s"],
        idle_ttl_s=fields["idle_ttl_s"],
    )


def _record(name: str, entry: dict[str, Any], state: dict[str, Any]) -> ServerRecord:
    """Assemble one :class:`ServerRecord`; raises :class:`McpConfigError` if the
    stored entry is not a valid config (list skips it, get/create/update
    surface it)."""
    config = store.entry_to_config(name, entry, resolve_secrets=False)
    env_keys, header_keys = store.entry_secret_keys(entry)
    return ServerRecord(
        config=config,
        env_keys=env_keys,
        header_keys=header_keys,
        tools=store.state_tools(state),
        has_probe_cache=store.state_has_cache(state),
        last_probe_at=_probe_at(state),
        last_probe_ok=store.state_probe_ok(state),
        last_probe_error=str(state.get("last_probe_error") or ""),
    )


def list_servers(slug: str | None) -> list[ServerRecord]:
    """Every renderable server in the workspace (malformed entries skipped)."""
    if not slug:
        return []
    states = store.read_states(slug)
    records: list[ServerRecord] = []
    for name, entry in store.read_servers_raw(slug).items():
        try:
            records.append(_record(name, entry, states.get(name, {})))
        except McpConfigError:
            logger.exception(
                "mcp: skipping unrenderable server %r in workspace %r", name, slug
            )
    return sorted(records, key=lambda r: r.config.name)


def get_server(slug: str, name: str) -> ServerRecord:
    """One server by name; raises :class:`McpServerNotFoundError` if absent."""
    entries = store.read_servers_raw(slug)
    if name not in entries:
        raise McpServerNotFoundError(name)
    return _record(name, entries[name], store.read_states(slug).get(name, {}))


def create_server(
    slug: str,
    *,
    name: str,
    transport: str,
    enabled: bool,
    command: str,
    args: list[str],
    env: dict[str, str],
    url: str,
    headers: dict[str, str],
    tool_include: list[str] | None,
    tool_exclude: list[str],
    connect_timeout_s: float,
    call_timeout_s: float,
    idle_ttl_s: float,
) -> ServerRecord:
    """Add a server to the workspace's config file.

    Validates the shape first (invalid → :class:`McpConfigError`), then guards
    the per-workspace name uniqueness (dup → :class:`McpServerNameExistsError`),
    then writes — so an invalid name is a 422, a duplicate a 409.
    """
    name = name.strip()
    fields = {
        "transport": transport,
        "enabled": enabled,
        "command": command.strip(),
        "args": list(args),
        "env": dict(env),
        "url": url.strip(),
        "headers": dict(headers),
        "tool_include": tool_include,
        "tool_exclude": list(tool_exclude),
        "connect_timeout_s": connect_timeout_s,
        "call_timeout_s": call_timeout_s,
        "idle_ttl_s": idle_ttl_s,
    }
    _validate_intent(name, fields)  # raises McpConfigError before any write
    entry = store.serialize_entry(**fields)
    entries = store.read_servers_raw(slug)
    if name in entries:
        raise McpServerNameExistsError(name)
    entries[name] = entry
    store.write_servers_raw(slug, entries)
    return _record(name, entry, {})


def update_server(
    slug: str,
    name: str,
    *,
    new_name: str | None = None,
    transport: str | None = None,
    enabled: bool | None = None,
    command: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    url: str | None = None,
    headers: dict[str, str] | None = None,
    tool_include: list[str] | None = None,
    clear_tool_include: bool = False,
    tool_exclude: list[str] | None = None,
    connect_timeout_s: float | None = None,
    call_timeout_s: float | None = None,
    idle_ttl_s: float | None = None,
) -> ServerRecord:
    """Patch one server (omitted fields stay).

    ``env`` / ``headers`` replace the whole map when present (send ``{}`` to
    clear; omit to keep what's on disk — the write-only-secrets contract). A
    rename moves the entry key and carries its probe state. Raises
    :class:`McpServerNotFoundError`, :class:`McpConfigError` (422), or
    :class:`McpServerNameExistsError` (409).
    """
    entries = store.read_servers_raw(slug)
    if name not in entries:
        raise McpServerNotFoundError(name)
    fields = store.read_entry_fields(entries[name])
    if transport is not None:
        fields["transport"] = transport
    if enabled is not None:
        fields["enabled"] = enabled
    if command is not None:
        fields["command"] = command.strip()
    if args is not None:
        fields["args"] = list(args)
    if env is not None:
        fields["env"] = dict(env)
    if url is not None:
        fields["url"] = url.strip()
    if headers is not None:
        fields["headers"] = dict(headers)
    if clear_tool_include:
        fields["tool_include"] = None
    elif tool_include is not None:
        fields["tool_include"] = list(tool_include)
    if tool_exclude is not None:
        fields["tool_exclude"] = list(tool_exclude)
    if connect_timeout_s is not None:
        fields["connect_timeout_s"] = connect_timeout_s
    if call_timeout_s is not None:
        fields["call_timeout_s"] = call_timeout_s
    if idle_ttl_s is not None:
        fields["idle_ttl_s"] = idle_ttl_s

    target = new_name.strip() if new_name is not None else name
    _validate_intent(target, fields)  # raises McpConfigError before any write
    new_entry = store.serialize_entry(**fields)
    record = _record(target, new_entry, store.read_states(slug).get(name, {}))
    if target != name and target in entries:
        raise McpServerNameExistsError(target)
    if target != name:
        del entries[name]
    entries[target] = new_entry
    store.write_servers_raw(slug, entries)
    if target != name:
        store.rename_state(slug, name, target)
    return record


def delete_server(slug: str, name: str) -> None:
    """Remove a server (and its probe state); raises :class:`McpServerNotFoundError`."""
    entries = store.read_servers_raw(slug)
    if name not in entries:
        raise McpServerNotFoundError(name)
    del entries[name]
    store.write_servers_raw(slug, entries)
    store.remove_state(slug, name)


async def probe_and_store(slug: str, name: str, *, sandbox_url: str) -> Any:
    """Probe one server's LIVE config and persist the verdict to ``.mcp-state.json``.

    Returns the :class:`johnny.mcp.client.McpProbeResult`. Works for disabled
    servers on purpose (add → probe → enable). Success refreshes the tool
    cache; failure keeps the stale cache and records the error (Johnny-trt.55).
    Raises :class:`McpServerNotFoundError` / :class:`McpConfigError`. The
    ``johnny.mcp.client`` import stays lazy — the api pays the SDK import only
    when an operator actually probes.
    """
    entries = store.read_servers_raw(slug)
    if name not in entries:
        raise McpServerNotFoundError(name)
    config = store.entry_to_config(name, entries[name], resolve_secrets=True)

    from johnny.mcp.client import probe_mcp_server

    result = await probe_mcp_server(config, sandbox_url=sandbox_url)
    tools = (
        [{"name": tool.name, "description": tool.description} for tool in result.tools]
        if result.ok
        else None
    )
    store.write_state(
        slug,
        name,
        ok=result.ok,
        error="" if result.ok else (result.error or "probe failed"),
        tools=tools,
    )
    return result


__all__ = [
    "McpServerNameExistsError",
    "McpServerNotFoundError",
    "ServerRecord",
    "create_server",
    "delete_server",
    "get_server",
    "list_servers",
    "probe_and_store",
    "resolve_mcp_slug",
    "slug_for_stamp",
    "update_server",
]
