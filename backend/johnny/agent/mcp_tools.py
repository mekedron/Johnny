"""MCP gateway tools for the answer agent — progressive-disclosure connector access.

Johnny-3gx. The native-tool cutover (Johnny-3ow) handed the answer LLM
``exec``/``read``/``write``/``list_dir`` but **dropped** the workspace's configured
MCP servers: in native mode the router catalog is forced internal-only
(:mod:`johnny.agent.job_session`) and :func:`~johnny.agent.sandbox_tools.build_sandbox_tools`
never advertised MCP — so the model could neither *see* nor *call* any connector
(the reported "the bot can't use MCP at all"). This module restores that.

NOT by flattening every server's tools into the prompt (one connector like
``n8n-mcp`` exposes ~500 tools — it would blow the context), but as three meta-tools
the model drives itself, exactly the discover → load → call shape an operator asked
for:

* ``list_mcp_servers()`` — which connectors exist (cheap; reads the cached catalog).
* ``list_mcp_tools(server)`` — load ONE connector's full tool list (a live connect:
  names + descriptions + input schemas).
* ``call_mcp_tool(server, tool, arguments)`` — run a tool and return its result.

Every tool routes through the SAME :class:`~johnny.mcp.client.McpClientManager` the
worker uses (lazy connect, fingerprint reuse, idle eviction) and the SAME
:class:`~johnny.skills.executor.ToolCallTraceSink` the native sandbox tools use, so
each gateway call lands one ``agent_tool_calls`` row (``phase = "mcp"``; the actual
call's ``tool_name`` is the qualified ``mcp__<server>__<tool>`` kind) and streams a
``ToolCallObserved`` — rendering the full discover→load→call sequence live in the
session/history timeline exactly like ``exec``/``read``/``write`` (Johnny-iy6).

The store is read FRESH on every call (``load_server_configs`` /
``load_server_snapshots``) rather than closed over, so an operator's mid-session edit
(add a connector, flip a filter, fix a secret) takes effect on the next tool call
without rebuilding the session.

Requires the ``agent`` extra (``livekit-agents``) for ``function_tool``; imported only
where that extra is installed (the agent image), like :mod:`johnny.agent.sandbox_tools`.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from livekit.agents.llm import function_tool

from johnny.mcp.catalog import McpToolInfo
from johnny.mcp.client import (
    McpCallTimeoutError,
    McpToolError,
    McpUnavailableError,
)
from johnny.mcp.config import McpServerConfig, qualified_tool_name
from johnny.mcp.store import load_server_configs, load_server_snapshots
from johnny.skills.executor import ToolCallTrace, ToolCallTraceSink

if TYPE_CHECKING:
    from livekit.agents.llm import Tool

logger = logging.getLogger(__name__)

MCP_TOOL_PHASE = "mcp"
"""``agent_tool_calls.phase`` for an MCP call — the timeline label alongside the
native sandbox tools' "exec"/"read"/"write"/"list"."""

TOOLS_LISTED_CAP = 100
"""Most tools ``list_mcp_tools`` enumerates for one server — a connector with
hundreds of tools (n8n-mcp) must not flood the model's context; the rest are
summarised as a count with a refine hint."""

CALL_RESULT_CAP_CHARS = 8000
"""Bound on one tool result handed back to the model — a single huge payload
(e.g. a 16 KB metabase dump) bloats the tool loop and derails the reply; the
overflow is dropped with a refine hint so the model can narrow and re-call."""


def _resolve_server(
    slug: str | None, server: str
) -> tuple[McpServerConfig | None, list[str]]:
    """Resolve a connector name tolerantly; return ``(config, all_names)``.

    The model often abbreviates (``metabase`` for ``mcp-metabase-server``), which
    used to dead-end on "no such connector" and send it floundering. Match
    exact → case-insensitive → unique case-insensitive substring; an ambiguous or
    absent name returns ``None`` plus every configured name so the caller can show
    the model the exact spellings. Disabled servers are INCLUDED (the caller
    reports "turned off" after resolving). ``all_names`` is secrets-free.
    """
    configs = load_server_configs(slug)
    names = [c.name for c in configs]
    wanted = (server or "").strip()
    if not wanted:
        return None, names
    for config in configs:  # exact
        if config.name == wanted:
            return config, names
    low = wanted.lower()
    insensitive = [c for c in configs if c.name.lower() == low]
    if len(insensitive) == 1:
        return insensitive[0], names
    substring = [c for c in configs if low in c.name.lower()]
    if len(substring) == 1:
        return substring[0], names
    return None, names


def _unknown_server_msg(name: str, available: list[str]) -> str:
    """A not-found message that names the real connectors so the model retries right."""
    if not available:
        return (
            f'No MCP connector named "{name}". No MCP connectors are configured '
            "for this workspace."
        )
    return (
        f'No MCP connector named "{name}". Available connectors: '
        + ", ".join(available)
        + ". Use one of those exact names."
    )


def _cap_result(text: str) -> str:
    """Bound one tool result so a huge payload can't swamp the tool loop.

    The truncation note pushes the model to KEEP GOING autonomously (call again
    with a filter / drill into the specific item it needs) rather than stalling to
    ask the user — the session-9 failure where it fixated on "the rest got
    truncated in my feed" and offered to re-run in chunks instead of answering."""
    if len(text) <= CALL_RESULT_CAP_CHARS:
        return text
    return (
        text[:CALL_RESULT_CAP_CHARS].rstrip()
        + f"\n[result truncated at {CALL_RESULT_CAP_CHARS} chars — this is only the "
        "first part. If you need the rest, call the tool again with a filter, id, "
        "or narrower query and keep going; do not stop to ask the user.]"
    )


def _first_line(text: str, *, cap: int = 160) -> str:
    line = (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""
    return line[: cap - 1].rstrip() + "…" if len(line) > cap else line


def _param_hint(schema: dict | None) -> str:
    """A compact ``name: type`` signature from a tool's JSON-Schema (``[opt]``)."""
    if not isinstance(schema, dict):
        return ""
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return "no arguments"
    required = set(schema.get("required") or [])
    parts: list[str] = []
    for pname, pspec in props.items():
        ptype = str(pspec.get("type") or "") if isinstance(pspec, dict) else ""
        token = f"{pname}: {ptype}".strip().rstrip(":").strip()
        parts.append(token if pname in required else f"[{token}]")
    return ", ".join(parts)


def _tool_line(tool: McpToolInfo) -> str:
    sig = _param_hint(tool.input_schema)
    head = f"- {tool.name}({sig})" if sig else f"- {tool.name}"
    desc = _first_line(tool.description)
    return f"{head} — {desc}" if desc else head


def _server_line(name: str, *, probe_ok: bool | None, tool_count: int, reason: str) -> str:
    if probe_ok is False:
        return f"- {name} — last probe failed ({reason or 'not reachable'}); call list_mcp_tools(\"{name}\") to retry."
    if probe_ok is None or tool_count == 0:
        return f"- {name} — not loaded yet; call list_mcp_tools(\"{name}\") to load its tools."
    return f"- {name} — reachable; {tool_count} tool(s). Call list_mcp_tools(\"{name}\") for the list."


def build_mcp_tools(
    *,
    slug: str | None,
    manager: Any,
    sandbox_url: str,
    trace_sink: ToolCallTraceSink | None = None,
) -> list[Tool]:
    """The MCP gateway tool surface for one session's workspace.

    Returns LiveKit ``@function_tool``\\s the caller appends to the
    :class:`~johnny.agent.session.JohnnyAgent`'s native tool list (next to the
    sandbox tools). ``manager`` is an :class:`~johnny.mcp.client.McpClientManager`
    (duck-typed so tests pass a fake); ``sandbox_url`` is where stdio servers are
    spawned (the session's skills-sandbox — http servers ignore it). ``slug`` keys
    the workspace whose ``.johnny/.mcp.json`` is read fresh on each call.
    """

    async def _record(trace: ToolCallTrace) -> None:
        if trace_sink is None:
            return
        try:
            await trace_sink.record(trace)
        except Exception:  # pragma: no cover - tracing is best-effort
            logger.warning("mcp tool: trace sink failed — continuing", exc_info=True)

    async def _trace(
        tool_name: str,
        request: dict[str, Any],
        text: str,
        *,
        ok: bool,
        started_at: datetime,
        t0: float,
        timed_out: bool = False,
        error: str = "",
    ) -> None:
        """Persist one gateway call's trace — phase "mcp", so the whole
        discover→load→call sequence renders in the session/history timeline."""
        await _record(
            ToolCallTrace(
                tool_name=tool_name,
                phase=MCP_TOOL_PHASE,
                request=request,
                ok=ok,
                exit_code=None,
                stdout=text,
                stderr="",
                duration_ms=int((time.monotonic() - t0) * 1000),
                timed_out=timed_out,
                truncated=False,
                denied=False,
                error=error,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
            )
        )

    @function_tool(name="list_mcp_servers")
    async def list_mcp_servers() -> str:
        """List the external MCP connectors available in this workspace.

        Start here when a request might need an external system (a workflow
        engine, a database, an analytics tool, a CRM). Returns each connector's
        name and whether it is reachable. To see what a connector can actually
        do, call `list_mcp_tools` with its name; then `call_mcp_tool` to use a
        tool. This list is cheap and does not connect to anything."""
        started_at = datetime.now(timezone.utc)
        t0 = time.monotonic()
        snapshots = load_server_snapshots(slug)
        if not snapshots:
            text = (
                "No MCP connectors are configured for this workspace. "
                "You still have your sandbox tools (exec/read/write/list_dir)."
            )
        else:
            lines = [
                _server_line(
                    s.config.name,
                    probe_ok=s.probe_ok,
                    tool_count=len(s.filtered_tools()),
                    reason=s.probe_error,
                )
                for s in snapshots
            ]
            text = (
                f"You have {len(snapshots)} MCP connector(s). Call "
                'list_mcp_tools("<name>") to load one\'s tools, then call_mcp_tool to '
                "use a tool:\n" + "\n".join(lines)
            )
        await _trace("list_mcp_servers", {}, text, ok=True, started_at=started_at, t0=t0)
        return text

    async def _do_list_tools(name: str) -> str:
        config, available = _resolve_server(slug, name)
        if config is None:
            return _unknown_server_msg(name, available)
        if not config.enabled:
            return f'The "{config.name}" connector is turned off in this workspace.'
        try:
            tools = await manager.list_tools(config, sandbox_url=sandbox_url)
        except (McpUnavailableError, McpCallTimeoutError, McpToolError) as exc:
            logger.info("mcp tool: list_mcp_tools(%s) failed: %s", config.name, exc)
            return f'ERROR: couldn\'t load tools from "{config.name}" — {exc}'
        kept_names = set(config.filtered_tool_names([t.name for t in tools]))
        kept = [t for t in tools if t.name in kept_names]
        if not kept:
            return f'The "{config.name}" connector exposes no tools you can use right now.'
        shown = kept[:TOOLS_LISTED_CAP]
        lines = [_tool_line(t) for t in shown]
        header = (
            f'The "{config.name}" connector exposes {len(kept)} tool(s). Call '
            f'call_mcp_tool("{config.name}", "<tool>", {{…}}) to use one:'
        )
        body = "\n".join(lines)
        if len(kept) > len(shown):
            body += (
                f"\n…and {len(kept) - len(shown)} more — name a tool or narrow the "
                "request and I can look it up."
            )
        return f"{header}\n{body}"

    @function_tool(name="list_mcp_tools")
    async def list_mcp_tools(server: str) -> str:
        """Load the full tool list for ONE MCP connector by name.

        Connects to the named connector (from `list_mcp_servers`) and returns
        every tool it exposes — each tool's name, a short description, and its
        argument signature (`name: type`, `[optional]` in brackets). Use the
        signatures to build the `arguments` object for `call_mcp_tool`. Call this
        only for the connector you actually need, not every one."""
        started_at = datetime.now(timezone.utc)
        t0 = time.monotonic()
        name = (server or "").strip()
        text = await _do_list_tools(name)
        ok = not text.startswith("ERROR:")
        await _trace(
            "list_mcp_tools",
            {"server": name},
            text,
            ok=ok,
            error="" if ok else text,
            started_at=started_at,
            t0=t0,
        )
        return text

    @function_tool(name="call_mcp_tool")
    async def call_mcp_tool(
        server: str, tool: str, arguments: dict[str, Any] | None = None
    ) -> str:
        """Call one tool on an MCP connector and return its result.

        `server` and `tool` come from `list_mcp_tools`; `arguments` is the object
        matching that tool's signature (omit or pass {} for a tool that takes no
        arguments). Pass the person's REAL values — never a placeholder. Answer
        from what the tool actually returns; if it returns an ERROR line, say what
        went wrong rather than inventing a result."""
        srv = (server or "").strip()
        tname = (tool or "").strip()
        args = dict(arguments) if isinstance(arguments, dict) else {}
        config, available = _resolve_server(slug, srv)
        if config is None:
            return _unknown_server_msg(srv, available)
        if not config.enabled:
            return f'The "{config.name}" connector is turned off in this workspace.'
        if not config.allows_tool(tname):
            return (
                f'The "{tname}" tool on "{config.name}" is not available — it is '
                "filtered out by this workspace's policy."
            )
        srv = config.name  # canonical name for the call, the kind, and the trace
        kind = qualified_tool_name(srv, tname)
        started_at = datetime.now(timezone.utc)
        t0 = time.monotonic()
        ok = False
        timed_out = False
        error = ""
        try:
            result = await manager.call_tool(
                config, sandbox_url=sandbox_url, tool=tname, arguments=args
            )
            ok = not bool(result.is_error)
            if ok:
                text = _cap_result(result.text)
            else:
                inner = result.text
                error = "tool reported an error (isError)"
                text = f"ERROR: {inner}" if inner else "ERROR: the tool reported a problem."
        except McpCallTimeoutError as exc:
            timed_out = True
            error = str(exc)
            text = f'ERROR: the "{tname}" tool on "{srv}" took too long and was stopped.'
        except McpUnavailableError as exc:
            error = str(exc)
            text = f'ERROR: couldn\'t reach the "{srv}" connector — {exc}'
        except McpToolError as exc:
            error = str(exc)
            text = f'ERROR: "{srv}" rejected "{tname}" — {exc}'

        await _trace(
            kind, args, text, ok=ok, timed_out=timed_out, error=error, started_at=started_at, t0=t0
        )
        return text

    return [list_mcp_servers, list_mcp_tools, call_mcp_tool]


__all__ = ["MCP_TOOL_PHASE", "TOOLS_LISTED_CAP", "build_mcp_tools"]
