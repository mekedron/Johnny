"""Unit tests: the MCP gateway tool surface for the answer agent (Johnny-3gx).

Drives :func:`johnny.agent.mcp_tools.build_mcp_tools` against a fake
:class:`~johnny.mcp.client.McpClientManager` and a tmp workspace ``.mcp.json``
(no real server, no sandbox). Pins the three meta-tools the cutover was missing:

* ``list_mcp_servers``  — enumerates enabled connectors with reachability;
* ``list_mcp_tools``    — loads ONE connector's tools (names + sigs + schema);
* ``call_mcp_tool``     — runs a tool, returns its text, traces the call.

The store is read fresh on each call, so tests seed it via ``johnny.mcp.store``
and point ``JOHNNY_WORKSPACES_DIR`` at ``tmp_path`` (the test_store fixture).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from johnny.agent.adapters.johnny_llm import tools_to_definitions
from johnny.agent.mcp_tools import (
    CALL_RESULT_CAP_CHARS,
    TOOLS_LISTED_CAP,
    build_mcp_tools,
)
from johnny.mcp.catalog import McpToolInfo
from johnny.mcp.client import (
    McpCallResult,
    McpCallTimeoutError,
    McpToolError,
    McpUnavailableError,
)
from johnny.mcp.config import McpServerConfig
from johnny.skills.executor import ToolCallTrace
from johnny.mcp import store

SLUG = "ws1"
SANDBOX_URL = "http://sb:8088"


@pytest.fixture(autouse=True)
def workspaces_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store's path resolver at a tmp workspaces root (test_store seam)."""
    monkeypatch.setenv("JOHNNY_WORKSPACES_DIR", str(tmp_path))
    return tmp_path


class _Recorder:
    """A :class:`ToolCallTraceSink` that just collects the traces."""

    def __init__(self) -> None:
        self.traces: list[ToolCallTrace] = []

    async def record(self, trace: ToolCallTrace) -> None:
        self.traces.append(trace)


class FakeManager:
    """Stand-in for :class:`~johnny.mcp.client.McpClientManager` (duck-typed)."""

    def __init__(self) -> None:
        self.list_result: tuple[McpToolInfo, ...] = ()
        self.list_error: Exception | None = None
        self.call_result = McpCallResult(text="echo: hi", is_error=False, duration_ms=5)
        self.call_error: Exception | None = None
        self.list_calls: list[dict[str, Any]] = []
        self.call_calls: list[dict[str, Any]] = []

    async def list_tools(
        self, config: McpServerConfig, *, sandbox_url: str
    ) -> tuple[McpToolInfo, ...]:
        self.list_calls.append({"server": config.name, "sandbox_url": sandbox_url})
        if self.list_error is not None:
            raise self.list_error
        return self.list_result

    async def call_tool(
        self,
        config: McpServerConfig,
        *,
        sandbox_url: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> McpCallResult:
        self.call_calls.append(
            {"server": config.name, "tool": tool, "arguments": arguments, "sandbox_url": sandbox_url}
        )
        if self.call_error is not None:
            raise self.call_error
        return self.call_result


def _stdio_entry(*, enabled: bool = True, tool_exclude: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "stdio",
        "command": "python3",
        "args": ["/opt/sandbox/mcp_demo_server.py"],
        "johnny": {
            "enabled": enabled,
            "tool_include": None,
            "tool_exclude": tool_exclude or [],
        },
    }


def _seed_servers(servers: dict[str, dict[str, Any]]) -> None:
    store.write_servers_raw(SLUG, servers)


def _tools(
    manager: FakeManager, sink: _Recorder | None = None
) -> tuple[dict[str, Any], list[Any]]:
    """``name -> raw closure`` for the gateway tools (the @function_tool ._func)."""
    built = build_mcp_tools(
        slug=SLUG, manager=manager, sandbox_url=SANDBOX_URL, trace_sink=sink
    )
    return {t.info.name: t._func for t in built}, built


# --- tool surface (schema) ---------------------------------------------------


def test_tool_surface_names_and_params() -> None:
    _fns, built = _tools(FakeManager())
    defs = {d.name: d for d in (tools_to_definitions(built) or [])}
    assert set(defs) == {"list_mcp_servers", "list_mcp_tools", "call_mcp_tool"}
    assert set(defs["list_mcp_servers"].parameters.get("properties", {})) == set()
    assert set(defs["list_mcp_tools"].parameters["properties"]) == {"server"}
    assert set(defs["call_mcp_tool"].parameters["properties"]) == {
        "server",
        "tool",
        "arguments",
    }


# --- list_mcp_servers --------------------------------------------------------


async def test_list_servers_empty() -> None:
    fns, _ = _tools(FakeManager())
    out = await fns["list_mcp_servers"]()
    assert "No MCP connectors are configured" in out


async def test_list_servers_reports_status_and_hides_disabled() -> None:
    _seed_servers(
        {
            "demo-tools": _stdio_entry(),
            "demo-http": _stdio_entry(),
            "off-server": _stdio_entry(enabled=False),
        }
    )
    # demo-tools probed with two cached tools; demo-http never probed.
    store.write_state(
        SLUG,
        "demo-tools",
        ok=True,
        error="",
        tools=[{"name": "current_time", "description": "now"}, {"name": "uuid", "description": "id"}],
    )
    fns, _ = _tools(FakeManager())
    out = await fns["list_mcp_servers"]()
    assert "2 MCP connector(s)" in out  # enabled only; disabled excluded
    assert "demo-tools — reachable; 2 tool(s)" in out
    assert "demo-http — not loaded yet" in out
    assert "off-server" not in out


async def test_list_servers_marks_failed_probe() -> None:
    _seed_servers({"flaky": _stdio_entry()})
    store.write_state(SLUG, "flaky", ok=False, error="connection refused", tools=None)
    fns, _ = _tools(FakeManager())
    out = await fns["list_mcp_servers"]()
    assert "flaky — last probe failed" in out


# --- list_mcp_tools ----------------------------------------------------------


async def test_list_tools_returns_names_descriptions_and_signatures() -> None:
    _seed_servers({"demo-tools": _stdio_entry()})
    manager = FakeManager()
    manager.list_result = (
        McpToolInfo(
            name="base64_encode",
            description="Base64-encode the given text.",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
        McpToolInfo(name="current_time", description="Current UTC time.", input_schema={"type": "object", "properties": {}}),
    )
    fns, _ = _tools(manager)
    out = await fns["list_mcp_tools"](server="demo-tools")
    assert manager.list_calls == [{"server": "demo-tools", "sandbox_url": SANDBOX_URL}]
    assert "demo-tools" in out and "2 tool(s)" in out
    assert "base64_encode(text: string)" in out
    assert "Base64-encode the given text." in out
    assert "current_time(no arguments)" in out


async def test_list_tools_unknown_server() -> None:
    fns, _ = _tools(FakeManager())
    out = await fns["list_mcp_tools"](server="ghost")
    assert 'No MCP connector named "ghost"' in out


async def test_list_tools_disabled_server() -> None:
    _seed_servers({"off-server": _stdio_entry(enabled=False)})
    fns, _ = _tools(FakeManager())
    out = await fns["list_mcp_tools"](server="off-server")
    assert "turned off" in out


async def test_list_tools_applies_exclude_filter() -> None:
    _seed_servers({"demo-tools": _stdio_entry(tool_exclude=["secret"])})
    manager = FakeManager()
    manager.list_result = (
        McpToolInfo(name="echo", description="echo"),
        McpToolInfo(name="secret", description="hidden"),
    )
    fns, _ = _tools(manager)
    out = await fns["list_mcp_tools"](server="demo-tools")
    assert "echo" in out
    assert "secret" not in out


async def test_list_tools_connect_error_returns_error_line() -> None:
    _seed_servers({"demo-tools": _stdio_entry()})
    manager = FakeManager()
    manager.list_error = McpUnavailableError("connect refused")
    fns, _ = _tools(manager)
    out = await fns["list_mcp_tools"](server="demo-tools")
    assert out.startswith("ERROR:")
    assert "connect refused" in out


async def test_list_tools_caps_long_lists() -> None:
    _seed_servers({"big": _stdio_entry()})
    manager = FakeManager()
    manager.list_result = tuple(
        McpToolInfo(name=f"tool_{i}", description="x") for i in range(TOOLS_LISTED_CAP + 25)
    )
    fns, _ = _tools(manager)
    out = await fns["list_mcp_tools"](server="big")
    assert "and 25 more" in out
    assert out.count("\n- ") == TOOLS_LISTED_CAP


# --- call_mcp_tool -----------------------------------------------------------


async def test_call_success_returns_text_and_traces() -> None:
    _seed_servers({"demo-tools": _stdio_entry()})
    manager = FakeManager()
    manager.call_result = McpCallResult(text="2026-06-14T00:00:00+00:00", is_error=False, duration_ms=7)
    sink = _Recorder()
    fns, _ = _tools(manager, sink)
    out = await fns["call_mcp_tool"](server="demo-tools", tool="current_time", arguments={})
    assert out == "2026-06-14T00:00:00+00:00"
    assert manager.call_calls == [
        {"server": "demo-tools", "tool": "current_time", "arguments": {}, "sandbox_url": SANDBOX_URL}
    ]
    assert len(sink.traces) == 1
    trace = sink.traces[0]
    assert trace.tool_name == "mcp__demo-tools__current_time"
    assert trace.phase == "mcp"
    assert trace.ok is True
    assert isinstance(trace.duration_ms, int) and trace.duration_ms >= 0
    assert trace.stdout == "2026-06-14T00:00:00+00:00"
    assert trace.started_at is not None and trace.finished_at is not None


async def test_list_operations_are_traced() -> None:
    """list_mcp_servers / list_mcp_tools also land timeline rows (phase mcp), so
    the full discover→load→call sequence renders in /history (Johnny-3gx)."""
    _seed_servers({"demo-tools": _stdio_entry()})
    manager = FakeManager()
    manager.list_result = (McpToolInfo(name="echo", description="e"),)
    sink = _Recorder()
    fns, _ = _tools(manager, sink)
    await fns["list_mcp_servers"]()
    await fns["list_mcp_tools"](server="demo-tools")
    assert [t.tool_name for t in sink.traces] == ["list_mcp_servers", "list_mcp_tools"]
    assert all(t.phase == "mcp" for t in sink.traces)
    assert all(t.ok for t in sink.traces)


async def test_list_tools_connect_error_traces_failure() -> None:
    """A failed live load is traced as not-ok with the error retained."""
    _seed_servers({"demo-tools": _stdio_entry()})
    manager = FakeManager()
    manager.list_error = McpUnavailableError("connect refused")
    sink = _Recorder()
    fns, _ = _tools(manager, sink)
    await fns["list_mcp_tools"](server="demo-tools")
    assert len(sink.traces) == 1
    assert sink.traces[0].tool_name == "list_mcp_tools"
    assert sink.traces[0].ok is False


async def test_call_passes_arguments_verbatim() -> None:
    _seed_servers({"demo-tools": _stdio_entry()})
    manager = FakeManager()
    fns, _ = _tools(manager)
    await fns["call_mcp_tool"](
        server="demo-tools", tool="base64_encode", arguments={"text": "Helsinki"}
    )
    assert manager.call_calls[0]["arguments"] == {"text": "Helsinki"}


async def test_call_none_arguments_becomes_empty_dict() -> None:
    _seed_servers({"demo-tools": _stdio_entry()})
    manager = FakeManager()
    fns, _ = _tools(manager)
    await fns["call_mcp_tool"](server="demo-tools", tool="uuid", arguments=None)
    assert manager.call_calls[0]["arguments"] == {}


async def test_call_tool_level_error_marks_failure() -> None:
    _seed_servers({"demo-fixture": _stdio_entry()})
    manager = FakeManager()
    manager.call_result = McpCallResult(text="missing credential FOO", is_error=True, duration_ms=3)
    sink = _Recorder()
    fns, _ = _tools(manager, sink)
    out = await fns["call_mcp_tool"](server="demo-fixture", tool="always-fail", arguments={})
    assert out.startswith("ERROR:")
    assert "missing credential FOO" in out
    assert sink.traces[0].ok is False


async def test_call_unavailable_returns_error_and_traces() -> None:
    _seed_servers({"demo-tools": _stdio_entry()})
    manager = FakeManager()
    manager.call_error = McpUnavailableError("connect refused")
    sink = _Recorder()
    fns, _ = _tools(manager, sink)
    out = await fns["call_mcp_tool"](server="demo-tools", tool="uuid", arguments={})
    assert out.startswith("ERROR:") and "couldn't reach" in out
    assert sink.traces[0].ok is False
    assert "connect refused" in sink.traces[0].error


async def test_call_timeout_marks_timed_out() -> None:
    _seed_servers({"demo-tools": _stdio_entry()})
    manager = FakeManager()
    manager.call_error = McpCallTimeoutError("60s elapsed")
    sink = _Recorder()
    fns, _ = _tools(manager, sink)
    out = await fns["call_mcp_tool"](server="demo-tools", tool="uuid", arguments={})
    assert "took too long" in out
    assert sink.traces[0].timed_out is True


async def test_call_protocol_error_returns_error() -> None:
    _seed_servers({"demo-tools": _stdio_entry()})
    manager = FakeManager()
    manager.call_error = McpToolError("unknown tool 'nope'")
    fns, _ = _tools(manager)
    out = await fns["call_mcp_tool"](server="demo-tools", tool="nope", arguments={})
    assert out.startswith("ERROR:") and "rejected" in out


async def test_call_unknown_server_no_call_no_trace() -> None:
    manager = FakeManager()
    sink = _Recorder()
    fns, _ = _tools(manager, sink)
    out = await fns["call_mcp_tool"](server="ghost", tool="echo", arguments={})
    assert 'No MCP connector named "ghost"' in out
    assert manager.call_calls == []
    assert sink.traces == []


async def test_call_filtered_tool_declined_without_calling() -> None:
    _seed_servers({"demo-tools": _stdio_entry(tool_exclude=["secret"])})
    manager = FakeManager()
    sink = _Recorder()
    fns, _ = _tools(manager, sink)
    out = await fns["call_mcp_tool"](server="demo-tools", tool="secret", arguments={})
    assert "not available" in out and "filtered" in out
    assert manager.call_calls == []
    assert sink.traces == []


# --- Johnny-3gx fixes: tolerant server-name resolution + result cap ---------
# (sessions 5/6 failures: the model abbreviated 'mcp-metabase-server' to
# 'metabase' and dead-ended; one tool result was 16 KB and derailed the loop.)


async def test_list_tools_resolves_abbreviated_server_name() -> None:
    """'metabase' must resolve to 'mcp-metabase-server' (the session-5 dead-end),
    connecting under the canonical name rather than erroring out."""
    _seed_servers({"mcp-metabase-server": _stdio_entry()})
    manager = FakeManager()
    manager.list_result = (McpToolInfo(name="list_dashboards", description="List dashboards."),)
    fns, _ = _tools(manager)
    out = await fns["list_mcp_tools"](server="metabase")
    assert manager.list_calls == [{"server": "mcp-metabase-server", "sandbox_url": SANDBOX_URL}]
    assert "mcp-metabase-server" in out and "list_dashboards" in out


async def test_list_tools_case_insensitive_server_name() -> None:
    _seed_servers({"demo-tools": _stdio_entry()})
    manager = FakeManager()
    manager.list_result = (McpToolInfo(name="uuid", description="id"),)
    fns, _ = _tools(manager)
    out = await fns["list_mcp_tools"](server="Demo-Tools")
    assert manager.list_calls[0]["server"] == "demo-tools"
    assert "uuid" in out


async def test_call_resolves_abbreviated_server_name_and_traces_canonical() -> None:
    _seed_servers({"mcp-metabase-server": _stdio_entry()})
    manager = FakeManager()
    manager.call_result = McpCallResult(text="[]", is_error=False, duration_ms=4)
    sink = _Recorder()
    fns, _ = _tools(manager, sink)
    out = await fns["call_mcp_tool"](server="metabase", tool="list_dashboards", arguments={})
    assert out == "[]"
    assert manager.call_calls[0]["server"] == "mcp-metabase-server"
    assert sink.traces[0].tool_name == "mcp__mcp-metabase-server__list_dashboards"


async def test_unknown_server_lists_available_names() -> None:
    """A genuinely-unknown name returns the real connector names so the model can
    retry in one step instead of floundering (session-5 recovery)."""
    _seed_servers({"demo-tools": _stdio_entry(), "mcp-metabase-server": _stdio_entry()})
    fns, _ = _tools(FakeManager())
    out = await fns["list_mcp_tools"](server="salesforce")
    assert 'No MCP connector named "salesforce"' in out
    assert "demo-tools" in out and "mcp-metabase-server" in out


async def test_ambiguous_abbreviation_lists_candidates_without_guessing() -> None:
    """An abbreviation matching >1 connector is never silently picked — the model
    is shown the options and nothing is connected."""
    _seed_servers({"demo-tools": _stdio_entry(), "demo-http": _stdio_entry()})
    manager = FakeManager()
    fns, _ = _tools(manager)
    out = await fns["list_mcp_tools"](server="demo")
    assert "No MCP connector named" in out
    assert "demo-tools" in out and "demo-http" in out
    assert manager.list_calls == []


async def test_call_large_result_is_capped() -> None:
    """A huge tool result is bounded with a refine hint so it can't swamp the loop
    (the session-6 16 KB dump)."""
    _seed_servers({"demo-tools": _stdio_entry()})
    manager = FakeManager()
    manager.call_result = McpCallResult(text="x" * 20000, is_error=False, duration_ms=9)
    sink = _Recorder()
    fns, _ = _tools(manager, sink)
    out = await fns["call_mcp_tool"](server="demo-tools", tool="dump", arguments={})
    assert len(out) < 20000
    assert "truncated" in out
    assert CALL_RESULT_CAP_CHARS <= len(out) <= CALL_RESULT_CAP_CHARS + 200
    assert "truncated" in sink.traces[0].stdout
