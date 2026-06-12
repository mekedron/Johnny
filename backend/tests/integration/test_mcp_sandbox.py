"""Integration tests: the MCP stdio bridge + fixture server (Johnny-trt.36).

These run against the REAL ``skills-sandbox`` compose service — the bridge
endpoints in ``sandbox/execd.py`` spawning the baked-in reference server
``/opt/sandbox/mcp_fixture_server.py``. Intended runner::

    docker compose exec api pytest tests/integration/test_mcp_sandbox.py

Skips loudly when the sandbox is unreachable (host-side run, CI without the
stack) or when the running sandbox image predates the bridge (rebuild with
``docker compose build skills-sandbox``).

Layers covered here, bottom-up:

* the raw bridge protocol (start → send/recv JSON-RPC lines → stop);
* the full Johnny client chain (``sandbox_stdio_client`` + SDK session +
  ``probe_mcp_server``) against the real daemon — the same code path the
  api probe endpoint and the worker's lazy connections run in production;
* the acceptance sad path: a command that exits immediately degrades to an
  ``ok=false`` probe with the stderr tail, never an exception.

No database access — this file talks HTTP to the sandbox only.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from johnny.mcp.client import McpClientManager, probe_mcp_server
from johnny.mcp.config import McpServerConfig

SANDBOX_URL = os.environ.get(
    "JOHNNY_SKILLS_SANDBOX_URL", "http://skills-sandbox:8088"
).rstrip("/")

FIXTURE_ARGV = ["python3", "/opt/sandbox/mcp_fixture_server.py"]


def _bridge_available() -> bool:
    """Sandbox reachable AND its image carries the /mcp bridge + fixture."""
    try:
        with httpx.Client(base_url=SANDBOX_URL, timeout=5.0) as client:
            if client.get("/health").status_code != 200:
                return False
            started = client.post("/mcp/start", json={"argv": ["true"]})
            if started.status_code == 404:  # pre-bridge image
                return False
            sid = started.json().get("sid")
            if sid:
                client.post("/mcp/stop", json={"sid": sid})
            return True
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(
    not _bridge_available(),
    reason=(
        f"skills-sandbox mcp bridge not reachable at {SANDBOX_URL} — run inside "
        "the compose stack after rebuilding the sandbox image: "
        "docker compose build skills-sandbox && docker compose up -d skills-sandbox"
    ),
)


def _config(**overrides: object) -> McpServerConfig:
    base: dict[str, object] = {
        "name": "fixture",
        "transport": "stdio",
        "command": FIXTURE_ARGV[0],
        "args": tuple(FIXTURE_ARGV[1:]),
        "connect_timeout_s": 15.0,
        "call_timeout_s": 15.0,
    }
    base.update(overrides)
    return McpServerConfig(**base)  # type: ignore[arg-type]


def test_bridge_protocol_raw_round_trip() -> None:
    """start → initialize line in/out → stop, straight over the daemon."""
    with httpx.Client(base_url=SANDBOX_URL, timeout=30.0) as client:
        started = client.post("/mcp/start", json={"argv": FIXTURE_ARGV})
        assert started.status_code == 200, started.text
        sid = started.json()["sid"]
        try:
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "raw-test", "version": "0"},
                },
            }
            sent = client.post(
                "/mcp/send", json={"sid": sid, "line": json.dumps(request)}
            )
            assert sent.status_code == 200, sent.text
            received = client.get(
                "/mcp/recv", params={"sid": sid, "timeout": 10}
            )
            assert received.status_code == 200
            line = received.json()["line"]
            assert line, received.text
            reply = json.loads(line)
            assert reply["id"] == 1
            assert reply["result"]["serverInfo"]["name"] == "johnny-mcp-fixture"
        finally:
            stopped = client.post("/mcp/stop", json={"sid": sid})
            assert stopped.status_code == 200
        # The session is gone after stop.
        assert client.get("/mcp/recv", params={"sid": sid}).status_code == 404


async def test_probe_full_chain_against_real_fixture() -> None:
    """The api probe path: SDK session over the bridge, tools listed."""
    result = await probe_mcp_server(_config(), sandbox_url=SANDBOX_URL)
    assert result.ok, result.error
    names = [tool.name for tool in result.tools]
    assert names == ["echo", "add", "always-fail"]
    assert "johnny-mcp-fixture" in result.server_info


async def test_manager_call_tool_against_real_fixture() -> None:
    """The worker path: lazy connect, two calls on one connection, clean close."""
    manager = McpClientManager()
    config = _config(tool_exclude=("always-fail",))
    try:
        echoed = await manager.call_tool(
            config,
            sandbox_url=SANDBOX_URL,
            tool="echo",
            arguments={"message": "integration"},
        )
        assert echoed.text == "echo: integration"
        assert not echoed.is_error

        added = await manager.call_tool(
            config, sandbox_url=SANDBOX_URL, tool="add", arguments={"a": 20, "b": 22}
        )
        assert added.text == "42"

        failing = await manager.call_tool(
            config, sandbox_url=SANDBOX_URL, tool="always-fail", arguments={}
        )
        assert failing.is_error
    finally:
        await manager.aclose()


async def test_probe_of_instantly_exiting_command_degrades() -> None:
    """A broken server (exits before speaking MCP) → ok=false + diagnostics."""
    config = _config(
        name="broken",
        command="bash",
        args=("-c", "echo doomed-server-stderr >&2; exit 3"),
        connect_timeout_s=10.0,
    )
    result = await probe_mcp_server(config, sandbox_url=SANDBOX_URL)
    assert not result.ok
    assert "exited" in result.error or "timed out" in result.error
