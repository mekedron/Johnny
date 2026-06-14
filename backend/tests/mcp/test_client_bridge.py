"""Hermetic end-to-end of the sandbox-stdio MCP chain (Johnny-trt.36).

No docker, no subprocess, no network: an ``httpx.MockTransport`` plays the
sandbox daemon's ``/mcp/*`` bridge, backed by an in-process MCP server
speaking the same protocol as ``sandbox/mcp_fixture_server.py``. What runs
for real is everything Johnny ships: :func:`sandbox_stdio_client`'s pump
loops, the SDK :class:`ClientSession` handshake, :func:`probe_mcp_server`,
and :class:`McpClientManager` + :class:`McpConnection` over it. The
integration suite (``tests/integration/test_mcp_sandbox.py``) repeats the
happy path against the REAL daemon + fixture file inside the compose stack.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from johnny.mcp.client import (
    McpClientManager,
    McpConnection,
    probe_mcp_server,
)
from johnny.mcp.config import McpServerConfig

_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": True}
_ECHO_SCHEMA = {
    "type": "object",
    "properties": {"message": {"type": "string"}},
    "required": ["message"],
    "additionalProperties": False,
}
FIXTURE_TOOLS = [
    {"name": "echo", "description": "Echo the message back.", "inputSchema": _ECHO_SCHEMA},
    {"name": "add", "description": "Add two numbers.", "inputSchema": _SCHEMA},
    {"name": "always-fail", "description": "Always errors.", "inputSchema": _SCHEMA},
]


class FakeBridge:
    """The sandbox daemon's /mcp endpoints + the fixture server, in-process."""

    def __init__(
        self,
        *,
        start_status: int = 200,
        exit_after_start: bool = False,
    ) -> None:
        self.start_status = start_status
        self.exit_after_start = exit_after_start
        self.started: list[dict[str, Any]] = []
        self.stopped: list[str] = []
        self._queues: dict[str, asyncio.Queue[str]] = {}
        self._counter = 0

    # ----- the fixture server brain (one JSON-RPC request → reply line) ----- #

    def _serve(self, sid: str, line: str) -> None:
        msg = json.loads(line)
        method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if msg_id is None:
            return  # notifications/initialized etc.
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-fixture", "version": "9.9.9"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": FIXTURE_TOOLS}
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "echo":
                result = {
                    "content": [
                        {"type": "text", "text": f"echo: {arguments.get('message', '')}"}
                    ],
                    "isError": False,
                }
            elif name == "add":
                total = float(arguments["a"]) + float(arguments["b"])
                result = {
                    "content": [{"type": "text", "text": str(total)}],
                    "isError": False,
                }
            elif name == "always-fail":
                result = {
                    "content": [{"type": "text", "text": "this tool always fails"}],
                    "isError": True,
                }
            else:
                self._queues[sid].put_nowait(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "error": {"code": -32602, "message": f"unknown tool: {name}"},
                        }
                    )
                )
                return
        else:
            self._queues[sid].put_nowait(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32601, "message": f"no method {method}"},
                    }
                )
            )
            return
        self._queues[sid].put_nowait(
            json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result})
        )

    # ----- the HTTP surface ----- #

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/mcp/start":
            if self.start_status != 200:
                return httpx.Response(
                    self.start_status, json={"error": "mcp session cap reached (8)"}
                )
            self._counter += 1
            sid = f"mcp-test-{self._counter}"
            self.started.append(json.loads(request.content))
            self._queues[sid] = asyncio.Queue()
            return httpx.Response(200, json={"sid": sid})
        if path == "/mcp/send":
            body = json.loads(request.content)
            sid = body["sid"]
            if sid not in self._queues:
                return httpx.Response(404, json={"error": f"unknown mcp session: {sid}"})
            if self.exit_after_start:
                return httpx.Response(
                    409,
                    json={
                        "error": "mcp server process has exited",
                        "exited": True,
                        "exit_code": 1,
                        "stderr_tail": "Traceback: kaboom",
                    },
                )
            self._serve(sid, body["line"])
            return httpx.Response(200, json={"ok": True})
        if path == "/mcp/recv":
            sid = request.url.params.get("sid", "")
            queue = self._queues.get(sid)
            if queue is None:
                return httpx.Response(404, json={"error": f"unknown mcp session: {sid}"})
            if self.exit_after_start:
                return httpx.Response(
                    200,
                    json={
                        "line": None,
                        "exited": True,
                        "exit_code": 1,
                        "stderr_tail": "Traceback: kaboom",
                        "error": "",
                    },
                )
            try:
                line = queue.get_nowait()
                return httpx.Response(200, json={"line": line})
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.01)  # keep the long-poll loop cool
                return httpx.Response(
                    200,
                    json={"line": None, "exited": False, "exit_code": None, "error": ""},
                )
        if path == "/mcp/stop":
            sid = json.loads(request.content)["sid"]
            self.stopped.append(sid)
            self._queues.pop(sid, None)
            return httpx.Response(200, json={"ok": True, "exit_code": 0})
        return httpx.Response(404, json={"error": f"unknown path: {path}"})

    def http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler), base_url="http://sb:8088"
        )


def _config(**overrides: Any) -> McpServerConfig:
    base: dict[str, Any] = {
        "name": "fixture",
        "transport": "stdio",
        "command": "python3",
        "args": ("/opt/sandbox/mcp_fixture_server.py",),
        "env": {"FIXTURE_FLAG": "1"},
        "connect_timeout_s": 5.0,
        "call_timeout_s": 5.0,
    }
    base.update(overrides)
    return McpServerConfig(**base)


async def test_probe_lists_tools_through_real_sdk_session() -> None:
    bridge = FakeBridge()
    async with bridge.http_client() as http:
        result = await probe_mcp_server(
            _config(), sandbox_url="http://sb:8088", http_client=http
        )
    assert result.ok, result.error
    assert [t.name for t in result.tools] == ["echo", "add", "always-fail"]
    assert result.tools[0].description == "Echo the message back."
    # Johnny-3gx: the live listing path carries each tool's inputSchema so the
    # answer agent's list_mcp_tools can show the model how to call it.
    assert result.tools[0].input_schema == _ECHO_SCHEMA
    assert result.tools[1].input_schema == _SCHEMA
    assert "fake-fixture" in result.server_info
    # The spawn crossed the bridge with argv + env, and the session was
    # stopped on the way out (no orphan processes in the sandbox).
    assert bridge.started == [
        {
            "argv": ["python3", "/opt/sandbox/mcp_fixture_server.py"],
            "env": {"FIXTURE_FLAG": "1"},
        }
    ]
    assert bridge.stopped == ["mcp-test-1"]


async def test_probe_reports_exit_as_error_not_exception() -> None:
    bridge = FakeBridge(exit_after_start=True)
    async with bridge.http_client() as http:
        result = await probe_mcp_server(
            _config(), sandbox_url="http://sb:8088", http_client=http
        )
    assert not result.ok
    assert "exited" in result.error
    assert "kaboom" in result.error


async def test_probe_reports_session_cap_as_error() -> None:
    bridge = FakeBridge(start_status=503)
    async with bridge.http_client() as http:
        result = await probe_mcp_server(
            _config(), sandbox_url="http://sb:8088", http_client=http
        )
    assert not result.ok
    assert "session cap" in result.error


async def test_manager_call_tool_end_to_end_and_idle_evict() -> None:
    bridge = FakeBridge()
    clients: list[httpx.AsyncClient] = []

    def factory(config: McpServerConfig, *, sandbox_url: str, clock: Any) -> McpConnection:
        http = bridge.http_client()
        clients.append(http)
        return McpConnection(
            config, sandbox_url=sandbox_url, clock=clock, http_client=http
        )

    manager = McpClientManager(connection_factory=factory)
    config = _config()
    try:
        result = await manager.call_tool(
            config,
            sandbox_url="http://sb:8088",
            tool="echo",
            arguments={"message": "hello"},
        )
        assert result.text == "echo: hello"
        assert not result.is_error

        added = await manager.call_tool(
            config, sandbox_url="http://sb:8088", tool="add", arguments={"a": 2, "b": 3}
        )
        assert added.text == "5.0"
        assert len(bridge.started) == 1  # one live connection served both

        failing = await manager.call_tool(
            config, sandbox_url="http://sb:8088", tool="always-fail", arguments={}
        )
        assert failing.is_error
        assert "always fails" in failing.text
    finally:
        await manager.aclose()
        for client in clients:
            await client.aclose()
    assert bridge.stopped == ["mcp-test-1"]  # eviction stopped the bridge session
