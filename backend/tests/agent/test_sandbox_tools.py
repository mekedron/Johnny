"""Phase 0 unit tests for the native sandbox tool surface (Johnny-3ow.1).

Drives :func:`johnny.agent.sandbox_tools.build_sandbox_tools` against a
``MockTransport`` :class:`SandboxClient` (no real container) and asserts the
contract that fixes the "weather is always London" bug: the model's arguments
reach the sandbox verbatim. Also pins the openclaw-parity tool surface
(exec/read/write/list_dir), the synthesized read/write/list over ``/exec``,
the full-access policy, and the ``agent_tool_calls`` tracing.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Callable

import httpx
import pytest

from johnny.agent.adapters.johnny_llm import tools_to_definitions
from johnny.agent.sandbox_tools import (
    SANDBOX_FULL_ACCESS_ENV,
    build_sandbox_tools,
    resolve_sandbox_policy,
    sandbox_full_access_enabled,
)
from johnny.skills.executor import ToolCallTrace
from johnny.skills.policy import ExecBinPolicy
from johnny.skills.sandbox import SandboxClient


class _Recorder:
    """A :class:`ToolCallTraceSink` that just collects the traces."""

    def __init__(self) -> None:
        self.traces: list[ToolCallTrace] = []

    async def record(self, trace: ToolCallTrace) -> None:
        self.traces.append(trace)


def _exec_reply(
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    truncated: bool = False,
    timed_out: bool = False,
) -> dict[str, Any]:
    return {
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": truncated,
        "stdout_truncated": truncated,
        "stderr_truncated": False,
        "timed_out": timed_out,
        "duration_ms": 5,
    }


def _harness(
    responder: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    *,
    policy: ExecBinPolicy | None = None,
    trace_sink: _Recorder | None = None,
):
    """Build the tools over a recording MockTransport sandbox.

    Returns ``(fns, bodies, sink)`` where ``fns`` maps tool-name → the raw
    closure (the ``@function_tool`` wrapper's ``_func``), ``bodies`` is the list
    of ``POST /exec`` JSON bodies seen, and ``sink`` is the trace recorder.
    """
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        reply = responder(body) if responder is not None else _exec_reply(stdout="ok")
        return httpx.Response(200, json=reply)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://sb")
    sink = trace_sink if trace_sink is not None else _Recorder()
    tools = build_sandbox_tools(
        SandboxClient(http_client=client),
        policy=policy if policy is not None else ExecBinPolicy.permit_all(),
        trace_sink=sink,
    )
    fns = {tool.info.name: tool._func for tool in tools}
    return fns, bodies, sink, tools


# --- tool surface (schema) ---------------------------------------------------


def test_tool_surface_matches_openclaw_names_and_params() -> None:
    fns, _bodies, _sink, tools = _harness()
    assert set(fns) == {"exec", "read", "write", "list_dir"}

    defs = {d.name: d for d in (tools_to_definitions(tools) or [])}
    assert set(defs) == {"exec", "read", "write", "list_dir"}
    # exec takes either a command list or a cmd string + env/workdir/timeout.
    exec_props = defs["exec"].parameters["properties"]
    assert {"command", "cmd", "env", "workdir", "timeout"} <= set(exec_props)
    # read takes a path with optional offset/limit slice.
    assert set(defs["read"].parameters["properties"]) == {"path", "offset", "limit"}
    assert set(defs["write"].parameters["properties"]) == {"path", "content"}
    assert set(defs["list_dir"].parameters["properties"]) == {"path"}


# --- read / list_dir / write synthesized over /exec --------------------------


async def test_read_issues_cat_and_returns_file_contents() -> None:
    fns, bodies, _sink, _tools = _harness(
        lambda b: _exec_reply(stdout="name: weather\n# weather skill\n")
    )
    out = await fns["read"](path="/skills/weather/SKILL.md")
    assert bodies[-1]["argv"] == ["cat", "--", "/skills/weather/SKILL.md"]
    assert "weather skill" in out


async def test_read_ranged_issues_sed_slice() -> None:
    fns, bodies, _sink, _tools = _harness(lambda b: _exec_reply(stdout="line3\nline4\n"))
    await fns["read"](path="/tmp/big.txt", offset=3, limit=2)
    assert bodies[-1]["argv"] == ["sed", "-n", "3,4p", "--", "/tmp/big.txt"]


async def test_list_dir_issues_ls() -> None:
    fns, bodies, _sink, _tools = _harness(lambda b: _exec_reply(stdout="total 0\n"))
    await fns["list_dir"]()
    assert bodies[-1]["argv"] == ["ls", "-la", "--", "/skills"]


async def test_write_round_trips_content_via_base64() -> None:
    fns, bodies, _sink, _tools = _harness(lambda b: _exec_reply(stdout="wrote 5 bytes to /tmp/x"))
    out = await fns["write"](path="/tmp/x", content="hello")
    argv = bodies[-1]["argv"]
    assert argv[0] == "bash" and argv[1] == "-c"
    # The content travels as base64 in argv (never raw) and decodes back.
    assert base64.b64decode(argv[5]).decode() == "hello"
    assert argv[4] == "/tmp/x"
    assert "wrote" in out


# --- exec: the argument round-trip (the bug this whole epic fixes) -----------


async def test_exec_passes_model_argv_and_env_verbatim() -> None:
    """The canonical fix: a Helsinki request reaches the sandbox AS Helsinki,
    not the script's London default — args are no longer dropped to ``{}``."""
    fns, bodies, _sink, _tools = _harness(
        lambda b: _exec_reply(stdout="Right now in Helsinki: Cloudy +3°C")
    )
    out = await fns["exec"](
        command=["bash", "/skills/weather/run.sh"],
        env={"JOHNNY_TASK_ARGS_JSON": json.dumps({"location": "Helsinki"})},
    )
    body = bodies[-1]
    assert body["argv"] == ["bash", "/skills/weather/run.sh"]
    assert json.loads(body["env"]["JOHNNY_TASK_ARGS_JSON"]) == {"location": "Helsinki"}
    assert "Helsinki" in out


async def test_exec_supports_cmd_string_for_pipes() -> None:
    fns, bodies, _sink, _tools = _harness(lambda b: _exec_reply(stdout="42"))
    await fns["exec"](cmd="curl -s https://wttr.in/Oslo?format=3 | head -1")
    assert bodies[-1]["cmd"].startswith("curl -s")
    assert "argv" not in bodies[-1]


async def test_exec_rejects_both_argv_and_cmd_without_hitting_sandbox() -> None:
    fns, bodies, _sink, _tools = _harness()
    out = await fns["exec"](command=["echo", "hi"], cmd="echo hi")
    # SandboxExecTool enforces exactly-one; the denial never reaches the daemon.
    assert bodies == []
    assert "exactly one" in out.lower()


# --- failures surface to the model (etu.7 answer grounding) ------------------


async def test_nonzero_exit_surfaces_error_text_to_model() -> None:
    fns, _bodies, _sink, _tools = _harness(
        lambda b: _exec_reply(exit_code=1, stderr="curl: (6) could not resolve host")
    )
    out = await fns["exec"](command=["bash", "/skills/weather/run.sh"])
    assert out.startswith("ERROR:")
    assert "could not resolve host" in out


async def test_truncation_appends_a_hint() -> None:
    fns, _bodies, _sink, _tools = _harness(
        lambda b: _exec_reply(stdout="x" * 100, truncated=True)
    )
    out = await fns["read"](path="/tmp/huge")
    assert "truncated" in out.lower()


# --- policy: curated denial vs full access -----------------------------------


async def test_curated_policy_denies_undeclared_bin() -> None:
    fns, bodies, sink, _tools = _harness(policy=ExecBinPolicy(allowed=frozenset({"cat"})))
    out = await fns["exec"](command=["rm", "-rf", "/"])
    assert bodies == []  # denied in-process, never sent to the sandbox
    assert "rm" in out and "ERROR" in out
    assert sink.traces[-1].denied is True


async def test_permit_all_allows_any_bin_and_substitution() -> None:
    policy = ExecBinPolicy.permit_all()
    assert policy.check_detailed(argv=["rm", "-rf", "/tmp/x"]) is None
    assert policy.check_detailed(cmd="echo $(whoami) && curl `cat url`") is None
    fns, bodies, _sink, _tools = _harness(policy=policy)
    await fns["exec"](command=["python3", "-c", "print(1)"])
    assert bodies[-1]["argv"] == ["python3", "-c", "print(1)"]


# --- observability: every call traces to agent_tool_calls --------------------


async def test_every_call_records_one_trace_with_phase_and_request() -> None:
    sink = _Recorder()
    fns, _bodies, _sink, _tools = _harness(
        lambda b: _exec_reply(stdout="ok"), trace_sink=sink
    )
    await fns["read"](path="/skills/x/SKILL.md")
    await fns["exec"](command=["echo", "hi"])
    assert [t.phase for t in sink.traces] == ["read", "exec"]
    assert all(t.tool_name == "sandbox.exec" for t in sink.traces)
    assert sink.traces[0].request["argv"] == ["cat", "--", "/skills/x/SKILL.md"]
    assert all(t.ok for t in sink.traces)


# --- the full-access flag + policy resolver ----------------------------------


def test_full_access_flag_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SANDBOX_FULL_ACCESS_ENV, raising=False)
    assert sandbox_full_access_enabled() is False
    monkeypatch.setenv(SANDBOX_FULL_ACCESS_ENV, "1")
    assert sandbox_full_access_enabled() is True
    monkeypatch.setenv(SANDBOX_FULL_ACCESS_ENV, "off")
    assert sandbox_full_access_enabled() is False


def test_resolve_sandbox_policy_picks_permit_all_under_full_access() -> None:
    assert resolve_sandbox_policy(full_access=True).allow_all is True
    curated = ExecBinPolicy(allowed=frozenset({"cat"}))
    assert resolve_sandbox_policy(full_access=False, curated=curated) is curated
