"""Per-workspace MCP server config — the file store (Johnny-hp1).

MCP servers are persisted as a FastMCP ``mcpServers`` JSON file at
``~/.johnny/workspaces/<slug>/.johnny/.mcp.json`` — the operator's source of
truth (there is no DB table anymore). This module is the only place that file
becomes :class:`johnny.mcp.config.McpServerConfig` value objects, the role the
DB service layer played before the cutover.

File shape — FastMCP-compatible (https://gofastmcp.com/integrations/mcp-json-configuration),
with the standard ``type``/``url``/``headers`` superset for http and a
``johnny`` block carrying the policy knobs FastMCP/Claude ignore::

    {
      "mcpServers": {
        "<name>": {
          "type": "stdio",                       # or "http"
          "command": "node", "args": [...],      # stdio
          "env": {"K": "v" | "${VAR}"},          # stdio
          "url": "https://…", "headers": {...},  # http
          "johnny": {
            "enabled": true,
            "tool_include": null, "tool_exclude": [],
            "connect_timeout_s": 10.0, "call_timeout_s": 60.0, "idle_ttl_s": 300.0
          }
        }
      }
    }

Secrets live PLAINTEXT here, or as ``${VAR}`` placeholders expanded from the
process env at connect time (FastMCP/Claude semantics). The file is under
``~/.johnny`` on the operator's host, like gog credentials and ``.env`` — the
chosen trade for a self-contained, hand-editable config.

Probe verdicts + the cached tool list are DERIVED runtime state, not config:
they live in a sibling ``.mcp-state.json`` so ``.mcp.json`` stays a clean
config file. A failed probe keeps the stale tool cache (Johnny-trt.55).

Stdlib-only (like :mod:`johnny.mcp.config` / :mod:`johnny.mcp.catalog`): the
worker, session assembly, and api all read it without importing the ``mcp``
SDK or any DB layer. Every loader is defensive — a missing file, malformed
JSON, or one unusable entry degrades to "no/empty servers", never an
exception, so a broken file can never crash a claim pass or session assembly.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from johnny.mcp.catalog import McpServerSnapshot, McpToolInfo
from johnny.mcp.config import (
    DEFAULT_CALL_TIMEOUT_S,
    DEFAULT_CONNECT_TIMEOUT_S,
    DEFAULT_IDLE_TTL_S,
    TRANSPORT_HTTP,
    TRANSPORT_STDIO,
    McpConfigError,
    McpServerConfig,
    qualified_tool_name,
)
from johnny.skills.sandbox import workspace_mcp_config_path

logger = logging.getLogger(__name__)

_STATE_FILENAME = ".mcp-state.json"
# ${VAR} expansion only — keep it strict and explicit (no bare $VAR / shell
# semantics) so a literal command containing a $ is never mangled.
_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


# --- paths -------------------------------------------------------------------


def _config_path(slug: str) -> Path:
    return Path(workspace_mcp_config_path(slug))


def _state_path(slug: str) -> Path:
    return _config_path(slug).parent / _STATE_FILENAME


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file + ``os.replace`` (never a
    half-written config another reader could see)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- raw config IO -----------------------------------------------------------


def read_servers_raw(slug: str) -> dict[str, dict[str, Any]]:
    """The ``mcpServers`` mapping as stored (name → entry dict).

    ``{}`` when the file is absent, unreadable, not valid JSON, or has no
    ``mcpServers`` object — every failure mode degrades to "no servers".
    """
    path = _config_path(slug)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        logger.exception("mcp store: cannot read %s", path)
        return {}
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        logger.exception("mcp store: %s is not valid JSON — treating as empty", path)
        return {}
    servers = doc.get("mcpServers") if isinstance(doc, dict) else None
    if not isinstance(servers, dict):
        return {}
    return {
        str(name): entry
        for name, entry in servers.items()
        if isinstance(name, str) and isinstance(entry, dict)
    }


def write_servers_raw(slug: str, entries: dict[str, dict[str, Any]]) -> None:
    """Atomically replace the workspace's ``mcpServers`` mapping (insertion
    order preserved so operator-authored files keep their layout)."""
    _atomic_write(
        _config_path(slug),
        json.dumps({"mcpServers": entries}, indent=2) + "\n",
    )


# --- entry ↔ config ----------------------------------------------------------


def _transport_of(entry: dict[str, Any]) -> str:
    raw = entry.get("type") or entry.get("transport")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    # Infer when unset: a url means http, otherwise stdio (the command form).
    return TRANSPORT_HTTP if entry.get("url") else TRANSPORT_STDIO


def _johnny_block(entry: dict[str, Any]) -> dict[str, Any]:
    block = entry.get("johnny")
    return block if isinstance(block, dict) else {}


def _expand_env(value: str) -> str:
    """Expand ``${VAR}`` from ``os.environ`` (unset → empty string)."""
    return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)


def _str_dict(raw: Any, *, expand: bool) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): (_expand_env(str(v)) if expand else str(v)) for k, v in raw.items()
    }


def _str_tuple(raw: Any, *, expand: bool = False) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple((_expand_env(str(x)) if expand else str(x)) for x in raw)


def _float(raw: Any, default: float) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def entry_to_config(
    name: str, entry: dict[str, Any], *, resolve_secrets: bool
) -> McpServerConfig:
    """One stored entry as the validated runtime value object.

    ``resolve_secrets=True`` expands ``${VAR}`` from ``os.environ`` across
    EVERY operator-facing field — env, headers, AND command/args/url — the
    connecting paths (worker exec, api probe). Some servers carry secrets in
    args (e.g. ``mcp-remote … --header CF-Access-Client-Id:${CF_ACCESS_CLIENT_ID}``),
    not just env. ``False`` is the secretless catalog view (env/headers left
    empty, command/args/url kept literal — they are not executed there). Raises
    :class:`McpConfigError` on an invalid shape — bulk loaders catch and skip,
    the api surfaces it.
    """
    block = _johnny_block(entry)
    include_raw = block.get("tool_include")
    command = str(entry.get("command") or "")
    url = str(entry.get("url") or "")
    return McpServerConfig(
        name=name,
        transport=_transport_of(entry),
        enabled=bool(block.get("enabled", True)),
        command=_expand_env(command) if resolve_secrets else command,
        args=_str_tuple(entry.get("args"), expand=resolve_secrets),
        env=_str_dict(entry.get("env"), expand=True) if resolve_secrets else {},
        url=_expand_env(url) if resolve_secrets else url,
        headers=_str_dict(entry.get("headers"), expand=True) if resolve_secrets else {},
        tool_include=None if include_raw is None else _str_tuple(include_raw),
        tool_exclude=_str_tuple(block.get("tool_exclude")),
        connect_timeout_s=_float(block.get("connect_timeout_s"), DEFAULT_CONNECT_TIMEOUT_S),
        call_timeout_s=_float(block.get("call_timeout_s"), DEFAULT_CALL_TIMEOUT_S),
        idle_ttl_s=_float(block.get("idle_ttl_s"), DEFAULT_IDLE_TTL_S),
    )


def serialize_entry(
    *,
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
) -> dict[str, Any]:
    """Build one server's JSON entry (FastMCP shape + ``johnny`` block).

    Only transport-appropriate keys are written — stdio carries
    command/args/env, http carries url/headers — mirroring the
    :class:`McpServerConfig` validity rules so the file never stores
    contradictory fields. Empty env/headers are omitted to keep the file tidy.
    """
    entry: dict[str, Any] = {"type": transport}
    if transport == TRANSPORT_STDIO:
        entry["command"] = command
        entry["args"] = list(args)
        if env:
            entry["env"] = dict(env)
    else:
        entry["url"] = url
        if headers:
            entry["headers"] = dict(headers)
    entry["johnny"] = {
        "enabled": bool(enabled),
        "tool_include": None if tool_include is None else list(tool_include),
        "tool_exclude": list(tool_exclude),
        "connect_timeout_s": float(connect_timeout_s),
        "call_timeout_s": float(call_timeout_s),
        "idle_ttl_s": float(idle_ttl_s),
    }
    return entry


def read_entry_fields(entry: dict[str, Any]) -> dict[str, Any]:
    """A raw entry's fields as :func:`serialize_entry` kwargs (tolerant, no
    validation).

    The update path merges a patch onto this so a partially hand-edited file
    can still be fixed via the api (forcing the stored entry through the
    validator first would 422 the very edit that repairs it).
    """
    block = _johnny_block(entry)
    include_raw = block.get("tool_include")
    env, headers = entry_env_headers(entry)
    return {
        "transport": _transport_of(entry),
        "enabled": bool(block.get("enabled", True)),
        "command": str(entry.get("command") or ""),
        "args": list(_str_tuple(entry.get("args"))),
        "env": env,
        "url": str(entry.get("url") or ""),
        "headers": headers,
        "tool_include": None if include_raw is None else list(_str_tuple(include_raw)),
        "tool_exclude": list(_str_tuple(block.get("tool_exclude"))),
        "connect_timeout_s": _float(block.get("connect_timeout_s"), DEFAULT_CONNECT_TIMEOUT_S),
        "call_timeout_s": _float(block.get("call_timeout_s"), DEFAULT_CALL_TIMEOUT_S),
        "idle_ttl_s": _float(block.get("idle_ttl_s"), DEFAULT_IDLE_TTL_S),
    }


def entry_secret_keys(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    """``(env_keys, header_keys)`` for masked api responses — names only.

    Values are plaintext on disk but never echoed to the browser (the
    provider-credentials masking model survives the DB→file cutover).
    """
    env = entry.get("env")
    headers = entry.get("headers")
    env_keys = sorted(str(k) for k in env) if isinstance(env, dict) else []
    header_keys = sorted(str(k) for k in headers) if isinstance(headers, dict) else []
    return env_keys, header_keys


def entry_env_headers(entry: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """The stored (unexpanded) env/headers — for the update path's preserve-on-omit
    semantics (an edit that doesn't resend secrets keeps what's on disk)."""
    return _str_dict(entry.get("env"), expand=False), _str_dict(
        entry.get("headers"), expand=False
    )


# --- probe state (sibling .mcp-state.json) -----------------------------------


def read_states(slug: str) -> dict[str, dict[str, Any]]:
    """Every server's probe verdict + tool cache (``{}`` on any read failure)."""
    path = _state_path(slug)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.exception("mcp store: cannot read state %s", path)
        return {}
    if not isinstance(doc, dict):
        return {}
    return {str(k): v for k, v in doc.items() if isinstance(v, dict)}


def write_state(
    slug: str,
    name: str,
    *,
    ok: bool,
    error: str,
    tools: list[dict[str, str]] | None,
) -> None:
    """Persist one server's probe verdict.

    ``tools=None`` keeps the stale cache (a failed probe must not forget the
    tools it once saw — the catalog renders them unavailable-with-reason
    instead, Johnny-trt.55). ``last_probe_*`` always update.
    """
    states = read_states(slug)
    record = dict(states.get(name) or {})
    record["last_probe_at"] = datetime.now(UTC).isoformat()
    record["last_probe_ok"] = bool(ok)
    record["last_probe_error"] = "" if ok else (error or "probe failed")
    if tools is not None:
        record["tools_cache"] = tools
    states[name] = record
    _atomic_write(_state_path(slug), json.dumps(states, indent=2) + "\n")


def remove_state(slug: str, name: str) -> None:
    """Drop a deleted server's probe state (best-effort housekeeping)."""
    states = read_states(slug)
    if name in states:
        del states[name]
        _atomic_write(_state_path(slug), json.dumps(states, indent=2) + "\n")


def rename_state(slug: str, old: str, new: str) -> None:
    """Carry a renamed server's probe state verbatim (preserve the timestamp —
    a rename is not a re-probe)."""
    if old == new:
        return
    states = read_states(slug)
    if old in states:
        states[new] = states.pop(old)
        _atomic_write(_state_path(slug), json.dumps(states, indent=2) + "\n")


def state_has_cache(record: dict[str, Any]) -> bool:
    """Whether a probe has ever cached a tool list (``tools_cache`` present).

    Distinguishes "never probed" (api ``tools=null``) from "probed, zero
    tools" (``tools=[]``) — the value object can't, since both yield ``()``.
    """
    return isinstance(record.get("tools_cache"), list)


def state_tools(record: dict[str, Any]) -> tuple[McpToolInfo, ...]:
    """The cached, unfiltered tool list from one server's probe state."""
    raw = record.get("tools_cache")
    if not isinstance(raw, list):
        return ()
    tools: list[McpToolInfo] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            tools.append(McpToolInfo(name=name, description=str(item.get("description") or "")))
    return tuple(tools)


def state_probe_ok(record: dict[str, Any]) -> bool | None:
    value = record.get("last_probe_ok")
    return value if isinstance(value, bool) else None


def state_probe_at(record: dict[str, Any]) -> str:
    value = record.get("last_probe_at")
    return value if isinstance(value, str) else ""


# --- runtime reads (the worker / session hot paths) --------------------------


def load_server_configs(slug: str | None) -> tuple[McpServerConfig, ...]:
    """One workspace's full server configs — the worker's fresh per-claim read.

    Secrets RESOLVED (``${VAR}`` expanded). DISABLED servers are INCLUDED
    (``enabled=False`` on the value object) so the executor distinguishes
    "connector isn't enabled" from "isn't configured". Unusable entries are
    skipped with a log. ``None``/empty slug (a stampless legacy claim) yields
    no servers — the promise-nothing degrade, mirroring the skills-dir
    resolver.
    """
    if not slug:
        return ()
    configs: list[McpServerConfig] = []
    for name, entry in read_servers_raw(slug).items():
        try:
            configs.append(entry_to_config(name, entry, resolve_secrets=True))
        except (McpConfigError, ValueError):
            logger.exception(
                "mcp store: skipping unusable server %r in workspace %r", name, slug
            )
    return tuple(sorted(configs, key=lambda c: c.name))


def load_server_snapshots(slug: str | None) -> tuple[McpServerSnapshot, ...]:
    """One workspace's catalog view: secretless configs + cached tools + probe state.

    ENABLED servers only (catalog assembly's filter). Tool cache + probe
    verdict come from the sibling ``.mcp-state.json``. Invalid entries are
    skipped. ``None``/empty slug yields no snapshots.
    """
    if not slug:
        return ()
    states = read_states(slug)
    snapshots: list[McpServerSnapshot] = []
    for name, entry in read_servers_raw(slug).items():
        try:
            config = entry_to_config(name, entry, resolve_secrets=False)
        except (McpConfigError, ValueError):
            logger.exception(
                "mcp store: skipping invalid server %r in workspace %r catalog",
                name,
                slug,
            )
            continue
        if not config.enabled:
            continue
        record = states.get(name, {})
        snapshots.append(
            McpServerSnapshot(
                config=config,
                tools=state_tools(record),
                probe_ok=state_probe_ok(record),
                probe_error=str(record.get("last_probe_error") or ""),
            )
        )
    return tuple(sorted(snapshots, key=lambda s: s.config.name))


def load_cached_kinds(slug: str | None) -> frozenset[str]:
    """Every qualified kind one workspace's servers can address, from cache.

    The capability-toggle's known-kinds source (the file twin of the old
    cross-workspace row scan): each server's CACHED tools with the server's
    filters applied, DISABLED servers INCLUDED — a deny written while a
    connector is off must be placeable and keep holding when it's re-enabled.
    Servers never probed (no cache) contribute nothing.
    """
    if not slug:
        return frozenset()
    states = read_states(slug)
    kinds: set[str] = set()
    for name, entry in read_servers_raw(slug).items():
        try:
            config = entry_to_config(name, entry, resolve_secrets=False)
        except (McpConfigError, ValueError):
            continue
        tools = state_tools(states.get(name, {}))
        if not tools:
            continue
        for tool_name in config.filtered_tool_names([t.name for t in tools]):
            kinds.add(qualified_tool_name(name, tool_name))
    return frozenset(kinds)


__all__ = [
    "entry_env_headers",
    "entry_secret_keys",
    "entry_to_config",
    "load_cached_kinds",
    "load_server_configs",
    "load_server_snapshots",
    "read_entry_fields",
    "read_servers_raw",
    "read_states",
    "remove_state",
    "rename_state",
    "serialize_entry",
    "state_has_cache",
    "state_probe_at",
    "state_probe_ok",
    "state_tools",
    "write_servers_raw",
    "write_state",
]
