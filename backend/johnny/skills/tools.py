"""The tool layer: core ``sandbox.exec`` + the registry MCP will extend.

In the three-layer capability model (Johnny-trt.23) tools are the only
things that *execute*. Tool metadata reuses the provider stack's
:class:`~app.providers.base.ToolDefinition` / :class:`~app.providers.base.ToolCall`
value objects, so the execution engine (Johnny-trt.22 decides it,
Johnny-trt.24 wires it into the worker) can hand the same definitions to any
LLM provider's tool-calling surface unchanged.

v1 ships exactly one core tool, ``sandbox.exec``: run a command inside the
skills-sandbox container (Johnny-trt.35) — never on the host, never in the
api / worker / agent-worker containers. Every request is vetted by the exec
bin policy (:mod:`johnny.skills.policy`) *before* it leaves the process, and
the sandbox daemon's own timeout ceiling + output caps bound what comes back.
Phase 6 adds MCP-contributed tools (``mcp__server__tool``,
Johnny-trt.36) into the same :class:`ToolRegistry`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.providers.base import ToolCall, ToolDefinition
from johnny.skills.policy import ExecBinPolicy
from johnny.skills.sandbox import (
    SandboxClient,
    SandboxError,
    SandboxExecResult,
    SandboxRequestError,
)

logger = logging.getLogger(__name__)

SANDBOX_EXEC_TOOL_NAME = "sandbox.exec"

# Defensive in-process ceilings mirroring the daemon's documented compose
# defaults — the daemon still enforces its own (env-tunable) caps.
DEFAULT_EXEC_TIMEOUT_S = 30.0
MAX_EXEC_TIMEOUT_S = 300.0


def sandbox_exec_tool_definition() -> ToolDefinition:
    """The ``sandbox.exec`` schema an LLM tool-calling surface receives."""
    return ToolDefinition(
        name=SANDBOX_EXEC_TOOL_NAME,
        description=(
            "Run one CLI command inside the skills sandbox container. Provide "
            "exactly one of 'argv' (preferred: the program and its arguments "
            "as a list) or 'cmd' (a shell string, for pipes). Only the "
            "sandbox baseline toolset and binaries declared by eligible "
            "skills may be invoked. Output is captured and size-capped; the "
            "command is killed at its timeout."
        ),
        parameters={
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Program + arguments, executed without a shell.",
                },
                "cmd": {
                    "type": "string",
                    "description": (
                        "A bash command line (use only when pipes/redirection are needed)."
                    ),
                },
                "timeout_s": {
                    "type": "number",
                    "description": (
                        "Seconds before the command is killed "
                        f"(default {DEFAULT_EXEC_TIMEOUT_S:g})."
                    ),
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory inside the sandbox.",
                },
                "env": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Extra environment variables for the command.",
                },
            },
            "additionalProperties": False,
        },
    )


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """What one tool invocation produced.

    ``output`` is the consumer-facing text (what an execution engine feeds
    back to the model, or what the deterministic skill runner maps to
    speech); ``error`` is the operator/log diagnostic; ``data`` carries the
    structured details (exit code, durations, truncation flags …).
    """

    ok: bool
    output: str = ""
    error: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class SandboxExecTool:
    """The core exec tool: policy check in-process, then ``POST /exec``.

    A denial never reaches the sandbox; an unreachable sandbox comes back as
    a failed outcome (callers translate to honest speech), never an
    exception into the turn/task loop.
    """

    name = SANDBOX_EXEC_TOOL_NAME

    def __init__(self, sandbox: SandboxClient, *, policy: ExecBinPolicy) -> None:
        self._sandbox = sandbox
        self._policy = policy

    @property
    def definition(self) -> ToolDefinition:
        return sandbox_exec_tool_definition()

    @property
    def policy(self) -> ExecBinPolicy:
        """The active policy (inspectable allow set, Johnny-trt.37/38)."""
        return self._policy

    async def run(self, arguments: dict[str, Any]) -> ToolOutcome:
        argv_raw = arguments.get("argv")
        cmd_raw = arguments.get("cmd")
        argv = [str(a) for a in argv_raw] if isinstance(argv_raw, list) and argv_raw else None
        cmd = cmd_raw if isinstance(cmd_raw, str) and cmd_raw.strip() else None
        if (argv is None) == (cmd is None):
            return ToolOutcome(
                ok=False,
                error="sandbox.exec needs exactly one of 'argv' (non-empty list) or 'cmd' (string)",
            )

        timeout_raw = arguments.get("timeout_s")
        timeout_s = DEFAULT_EXEC_TIMEOUT_S
        if isinstance(timeout_raw, (int, float)) and not isinstance(timeout_raw, bool):
            timeout_s = min(max(float(timeout_raw), 1.0), MAX_EXEC_TIMEOUT_S)
        cwd = arguments.get("cwd") if isinstance(arguments.get("cwd"), str) else None
        env_raw = arguments.get("env")
        env = (
            {str(k): str(v) for k, v in env_raw.items()}
            if isinstance(env_raw, dict)
            else None
        )

        denial = self._policy.check(argv=argv, cmd=cmd)
        if denial is not None:
            logger.info("sandbox.exec denied by bin policy: %s", denial)
            return ToolOutcome(ok=False, error=denial, data={"denied": True})

        try:
            result = await self._sandbox.exec(
                argv=argv, cmd=cmd, timeout_s=timeout_s, cwd=cwd, env=env
            )
        except SandboxRequestError as exc:
            return ToolOutcome(ok=False, error=f"sandbox rejected the request: {exc}")
        except SandboxError as exc:
            return ToolOutcome(
                ok=False,
                error=f"skills sandbox unreachable: {exc}",
                data={"unreachable": True},
            )
        return self._outcome_from_result(result)

    @staticmethod
    def _outcome_from_result(result: SandboxExecResult) -> ToolOutcome:
        data = {
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "timed_out": result.timed_out,
            "truncated": result.truncated,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        if result.timed_out:
            return ToolOutcome(
                ok=False,
                output=result.stdout,
                error="command killed at its timeout",
                data=data,
            )
        if result.exit_code != 0:
            stderr_tail = result.stderr.strip()[-500:]
            error = f"exit {result.exit_code}"
            if stderr_tail:
                error = f"{error}: {stderr_tail}"
            return ToolOutcome(ok=False, output=result.stdout, error=error, data=data)
        return ToolOutcome(ok=True, output=result.stdout, data=data)


class ToolRegistry:
    """Name → tool lookup for the execution engine's tool-calling loop.

    v1 holds ``sandbox.exec``; the MCP connector (Johnny-trt.36) registers
    ``mcp__server__tool`` entries into the same map, and internal tools
    (Johnny-trt.57) stay OUT — they execute session-locally, never here.
    """

    def __init__(self) -> None:
        self._tools: dict[str, SandboxExecTool] = {}

    def register(self, tool: SandboxExecTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> SandboxExecTool | None:
        return self._tools.get(name)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self._tools.values())

    async def run(self, call: ToolCall) -> ToolOutcome:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolOutcome(ok=False, error=f"unknown tool: {call.name!r}")
        return await tool.run(dict(call.arguments))


__all__ = [
    "DEFAULT_EXEC_TIMEOUT_S",
    "MAX_EXEC_TIMEOUT_S",
    "SANDBOX_EXEC_TOOL_NAME",
    "SandboxExecTool",
    "ToolOutcome",
    "ToolRegistry",
    "sandbox_exec_tool_definition",
]
