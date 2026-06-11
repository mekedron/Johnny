"""The sandbox.exec tool + registry against a mocked exec daemon (Johnny-trt.23).

httpx.MockTransport stands in for the skills-sandbox API, so these pin the
client/tool mapping (timeout kill, truncation flags, denial-before-HTTP,
unreachable degrade) without a running stack; the dev-stack integration
suite exercises the same paths against the real daemon.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.providers.base import ToolCall
from johnny.skills.policy import build_policy
from johnny.skills.sandbox import (
    SandboxClient,
    SandboxRequestError,
    SandboxUnavailableError,
)
from johnny.skills.tools import (
    MAX_EXEC_TIMEOUT_S,
    SANDBOX_EXEC_TOOL_NAME,
    SandboxExecTool,
    ToolRegistry,
    sandbox_exec_tool_definition,
)

_OK_REPLY: dict[str, Any] = {
    "exit_code": 0,
    "stdout": "hello\n",
    "stderr": "",
    "truncated": False,
    "stdout_truncated": False,
    "stderr_truncated": False,
    "timed_out": False,
    "duration_ms": 12,
}


def _client_with(handler: httpx.MockTransport) -> SandboxClient:
    http = httpx.AsyncClient(transport=handler, base_url="http://sandbox-test")
    return SandboxClient("http://sandbox-test", http_client=http)


def _reply(payload: dict[str, Any], status: int = 200) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handle)


class _CapturingTransport:
    """Records exec request bodies while replying with a fixed payload."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.bodies: list[dict[str, Any]] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/exec":
                self.bodies.append(json.loads(request.content.decode()))
            return httpx.Response(200, json=self.payload)

        return httpx.MockTransport(handle)


# --- SandboxClient ------------------------------------------------------------


async def test_client_exec_round_trip() -> None:
    client = _client_with(_reply(_OK_REPLY))
    result = await client.exec(argv=["echo", "hello"])
    assert result.exit_code == 0
    assert result.stdout == "hello\n"
    assert result.timed_out is False
    await client.aclose()


async def test_client_maps_4xx_to_request_error() -> None:
    client = _client_with(_reply({"error": "'timeout' exceeds the cap"}, status=400))
    with pytest.raises(SandboxRequestError, match="cap"):
        await client.exec(cmd="true", timeout_s=9999)
    await client.aclose()


async def test_client_maps_transport_failure_to_unavailable() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _client_with(httpx.MockTransport(boom))
    with pytest.raises(SandboxUnavailableError):
        await client.exec(argv=["true"])
    with pytest.raises(SandboxUnavailableError):
        await client.check_bins(["grep"])
    await client.aclose()


async def test_client_check_bins_parses_map() -> None:
    client = _client_with(
        _reply({"bins": {"grep": True, "nmap": False}, "missing": ["nmap"], "all_present": False})
    )
    assert await client.check_bins(["grep", "nmap"]) == {"grep": True, "nmap": False}
    assert await client.check_bins([]) == {}  # no names — no HTTP call
    await client.aclose()


# --- SandboxExecTool ----------------------------------------------------------


async def test_tool_happy_path_outcome() -> None:
    tool = SandboxExecTool(_client_with(_reply(_OK_REPLY)), policy=build_policy())
    outcome = await tool.run({"argv": ["echo", "hello"]})
    assert outcome.ok is True
    assert outcome.output == "hello\n"
    assert outcome.data["exit_code"] == 0


async def test_tool_denies_before_any_http() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a denied exec must never reach the sandbox")

    tool = SandboxExecTool(_client_with(httpx.MockTransport(fail)), policy=build_policy())
    outcome = await tool.run({"argv": ["nmap", "host"]})
    assert outcome.ok is False
    assert outcome.data.get("denied") is True
    assert "'nmap'" in outcome.error


async def test_tool_maps_timeout_kill() -> None:
    reply = dict(_OK_REPLY, timed_out=True, exit_code=-9)
    tool = SandboxExecTool(_client_with(_reply(reply)), policy=build_policy())
    outcome = await tool.run({"cmd": "sleep 30", "timeout_s": 2})
    assert outcome.ok is False
    assert "timeout" in outcome.error
    assert outcome.data["timed_out"] is True


async def test_tool_maps_nonzero_exit_with_stderr_tail() -> None:
    reply = dict(_OK_REPLY, exit_code=2, stdout="partial", stderr="boom detail")
    tool = SandboxExecTool(_client_with(_reply(reply)), policy=build_policy())
    outcome = await tool.run({"argv": ["grep", "x", "/none"]})
    assert outcome.ok is False
    assert outcome.output == "partial"  # stdout survives for the caller
    assert outcome.error.startswith("exit 2")
    assert "boom detail" in outcome.error


async def test_tool_maps_unreachable_sandbox() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    tool = SandboxExecTool(_client_with(httpx.MockTransport(boom)), policy=build_policy())
    outcome = await tool.run({"argv": ["echo", "x"]})
    assert outcome.ok is False
    assert outcome.data.get("unreachable") is True


async def test_tool_requires_exactly_one_form() -> None:
    tool = SandboxExecTool(_client_with(_reply(_OK_REPLY)), policy=build_policy())
    both = await tool.run({"argv": ["true"], "cmd": "true"})
    neither = await tool.run({})
    assert both.ok is False and neither.ok is False


async def test_tool_clamps_timeout_to_ceiling() -> None:
    capture = _CapturingTransport(_OK_REPLY)
    tool = SandboxExecTool(_client_with(capture.transport()), policy=build_policy())
    await tool.run({"argv": ["echo", "x"], "timeout_s": 100000})
    assert capture.bodies[0]["timeout"] == MAX_EXEC_TIMEOUT_S


async def test_tool_passes_env_and_cwd() -> None:
    capture = _CapturingTransport(_OK_REPLY)
    tool = SandboxExecTool(_client_with(capture.transport()), policy=build_policy())
    await tool.run({"argv": ["pwd"], "cwd": "/tmp", "env": {"K": "v"}})
    body = capture.bodies[0]
    assert body["cwd"] == "/tmp"
    assert body["env"] == {"K": "v"}


# --- definition + registry -----------------------------------------------------


def test_tool_definition_shape() -> None:
    definition = sandbox_exec_tool_definition()
    assert definition.name == SANDBOX_EXEC_TOOL_NAME
    properties = definition.parameters["properties"]
    assert {"argv", "cmd", "timeout_s", "cwd", "env"} <= set(properties)


async def test_registry_runs_known_and_reports_unknown() -> None:
    registry = ToolRegistry()
    tool = SandboxExecTool(_client_with(_reply(_OK_REPLY)), policy=build_policy())
    registry.register(tool)
    assert [d.name for d in registry.definitions()] == [SANDBOX_EXEC_TOOL_NAME]

    ok = await registry.run(
        ToolCall(id="1", name=SANDBOX_EXEC_TOOL_NAME, arguments={"argv": ["echo", "x"]})
    )
    assert ok.ok is True
    unknown = await registry.run(ToolCall(id="2", name="mcp__nope__tool"))
    assert unknown.ok is False and "unknown tool" in unknown.error
