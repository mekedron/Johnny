"""Native sandbox tools for the answer agent — the openclaw-style tool surface.

Johnny-3ow: the cutover from a keyword-router → fixed-`task.kind` → one
hardcoded ``argv`` (always run with ``JOHNNY_TASK_ARGS_JSON="{}"`` — the
"weather is always London" bug) to **native function calling**. The answer LLM
is handed a small set of generic LiveKit ``@function_tool``\\s — ``exec`` /
``read`` / ``write`` / ``list_dir`` — that it composes arguments for itself and
calls in a loop, exactly like openclaw's ``exec``/``read``/``write`` tools.
Skills stop being a special invocation path: they are files the model
*discovers* (``list_dir('/skills')``), *reads* (``read('/skills/<name>/SKILL.md')``)
and *runs* (``exec(command=[...])`` with the user's real values).

Every tool routes through the existing :class:`~johnny.skills.tools.SandboxExecTool`
→ ``POST /exec`` path, so:

* the **bin policy** still vets each call (under full access,
  :meth:`ExecBinPolicy.permit_all` lets everything through — the container is
  the security boundary);
* the **daemon caps** (timeout ceiling + per-stream output cap) still bound it;
* every call lands one ``agent_tool_calls`` row via the shared
  :class:`~johnny.skills.executor.ToolCallTraceSink`, so the reasoning timeline
  (Johnny-etu.4/etu.16) renders native tool calls unchanged — only the ``phase``
  differs ("exec"/"read"/"write"/"list" instead of the task path's "run").

``read``/``write``/``list_dir`` are synthesized over the ``/exec``-only daemon
(``cat``/``tee``-via-base64/``ls``) rather than new daemon endpoints, keeping the
single audited sandbox surface (:mod:`johnny.skills.sandbox`) frozen.

Requires the ``agent`` extra (``livekit-agents``) for ``function_tool``; imported
only where that extra is installed (the api / agent image), like
:mod:`johnny.agent.session`.
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from livekit.agents.llm import function_tool

from johnny.skills.executor import ToolCallTraceSink, build_tool_call_trace
from johnny.skills.policy import ExecBinPolicy
from johnny.skills.sandbox import SandboxClient
from johnny.skills.tools import SandboxExecTool, ToolOutcome

if TYPE_CHECKING:
    from livekit.agents.llm import Tool

logger = logging.getLogger(__name__)

SANDBOX_FULL_ACCESS_ENV = "JOHNNY_SANDBOX_FULL_ACCESS"
"""When set truthy, the agent gets the native sandbox tools with the
:meth:`ExecBinPolicy.permit_all` policy (full container access). The flag gates
the whole cutover so each phase stays revertible until it is flipped on."""

DEFAULT_READ_LINES = 400
"""Lines a ranged ``read`` returns when ``limit`` is omitted but ``offset`` set."""

SKILLS_ROOT = "/skills"
"""Where skill packages live in the sandbox — the ``list_dir`` discovery root."""

_TRUNCATION_HINT = "\n[output truncated by the sandbox output cap]"


def sandbox_full_access_enabled() -> bool:
    """True when ``JOHNNY_SANDBOX_FULL_ACCESS`` opts this process into the cutover."""
    return os.environ.get(SANDBOX_FULL_ACCESS_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _format_outcome(outcome: ToolOutcome, *, empty_ok: str) -> str:
    """Shape a :class:`ToolOutcome` into the text the model sees as the tool result.

    Success → the command's stdout (with any stderr appended so the model never
    loses a warning); a non-empty-but-blank result degrades to ``empty_ok``.
    Failure → an explicit ``ERROR:`` line plus whatever partial output exists, so
    the answer model can ground on the real failure (Johnny-etu.7) and correct
    its next call rather than fabricating a result.
    """
    if outcome.ok:
        text = outcome.output.strip("\n")
        stderr = str(outcome.data.get("stderr") or "").strip()
        if stderr and stderr not in text:
            text = f"{text}\n[stderr] {stderr}" if text.strip() else f"[stderr] {stderr}"
        if not text.strip():
            text = empty_ok
        if outcome.data.get("truncated") or outcome.data.get("stdout_truncated"):
            text += _TRUNCATION_HINT
        return text

    parts = [f"ERROR: {outcome.error}" if outcome.error else "ERROR: the command failed"]
    if outcome.output.strip():
        parts.append(outcome.output.strip())
    return "\n".join(parts)


def build_sandbox_tools(
    sandbox: SandboxClient,
    *,
    policy: ExecBinPolicy,
    trace_sink: ToolCallTraceSink | None = None,
    list_root: str = SKILLS_ROOT,
) -> list[Tool]:
    """The native tool surface bound to one session's sandbox container.

    Returns LiveKit ``@function_tool``\\s the caller hangs on the
    :class:`~johnny.agent.session.JohnnyAgent` (``Agent(tools=...)``); the
    :mod:`johnny.agent.adapters.johnny_llm` adapter forwards them to the
    provider's native tool-calling surface and runs the tool loop. Each tool
    closes over the per-session :class:`SandboxClient` + :class:`ExecBinPolicy`
    + optional :class:`ToolCallTraceSink`.
    """
    exec_tool = SandboxExecTool(sandbox, policy=policy)

    async def _run(phase: str, request: dict[str, Any], *, empty_ok: str) -> str:
        started_at = datetime.now(timezone.utc)
        outcome = await exec_tool.run(request)
        finished_at = datetime.now(timezone.utc)
        if trace_sink is not None:
            try:
                await trace_sink.record(
                    build_tool_call_trace(
                        exec_tool.name,
                        phase,
                        request,
                        outcome,
                        started_at=started_at,
                        finished_at=finished_at,
                    )
                )
            except Exception:  # pragma: no cover - tracing is best-effort
                logger.warning(
                    "sandbox tool: trace sink failed (phase=%s) — continuing",
                    phase,
                    exc_info=True,
                )
        return _format_outcome(outcome, empty_ok=empty_ok)

    @function_tool(name="exec")
    async def exec_(
        command: list[str] | None = None,
        cmd: str | None = None,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> str:
        """Run one command inside your private sandbox container and return its
        combined output and exit status.

        Provide EXACTLY ONE of: `command` (a program and its arguments as a
        list, e.g. ["bash", "/skills/weather/run.sh"] — strongly preferred) or
        `cmd` (a single bash string, only when you genuinely need a pipe or
        redirection). Always pass the user's REAL values (a city, a ticker, a
        date) as arguments — never a placeholder. The container is yours alone
        and never touches the host."""
        request: dict[str, Any] = {}
        if command:
            request["argv"] = [str(c) for c in command]
        if cmd:
            request["cmd"] = cmd
        if workdir:
            request["cwd"] = workdir
        if env:
            request["env"] = {str(k): str(v) for k, v in env.items()}
        if timeout is not None:
            request["timeout_s"] = timeout
        return await _run("exec", request, empty_ok="(the command produced no output; exit 0)")

    @function_tool(name="read")
    async def read_(path: str, offset: int | None = None, limit: int | None = None) -> str:
        """Read a text file from your sandbox.

        Use this to read a skill's `/skills/<name>/SKILL.md` before you run it,
        or any file you created. `offset` (1-based first line) and `limit`
        (line count) read a slice of a large file."""
        if offset is not None or limit is not None:
            start = max(int(offset or 1), 1)
            count = max(int(limit or DEFAULT_READ_LINES), 1)
            end = start + count - 1
            request: dict[str, Any] = {"argv": ["sed", "-n", f"{start},{end}p", "--", path]}
        else:
            request = {"argv": ["cat", "--", path]}
        return await _run("read", request, empty_ok="(the file is empty)")

    @function_tool(name="write")
    async def write_(path: str, content: str) -> str:
        """Create or overwrite a text file in your sandbox (parent directories
        are created).

        Use this for scratch files you need to build up. Your skills under
        `/skills` are read-only — write your own working files elsewhere
        (e.g. under `/tmp`)."""
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        script = (
            'mkdir -p "$(dirname "$1")" && '
            'printf %s "$2" | base64 -d > "$1" && '
            'printf "wrote %s bytes to %s" "$(wc -c < "$1")" "$1"'
        )
        request = {"argv": ["bash", "-c", script, "johnny-write", path, encoded]}
        return await _run("write", request, empty_ok=f"wrote {path}")

    @function_tool(name="list_dir")
    async def list_dir_(path: str = list_root) -> str:
        """List a directory in your sandbox.

        Start at `/skills` to discover which skills you have available, then
        `read` the SKILL.md of the one that fits."""
        request = {"argv": ["ls", "-la", "--", path]}
        return await _run("list", request, empty_ok="(the directory is empty)")

    return [exec_, read_, write_, list_dir_]


def resolve_sandbox_policy(
    *,
    full_access: bool,
    curated: ExecBinPolicy | None = None,
) -> ExecBinPolicy:
    """Pick the policy the native tools enforce.

    Full access (the Johnny-3ow operator decision) → :meth:`ExecBinPolicy.permit_all`.
    Otherwise the caller's ``curated`` policy (the trt.23/38 computed allow set)
    — or an empty-allow policy if none was supplied (every undeclared bin denied).
    """
    if full_access:
        return ExecBinPolicy.permit_all()
    return curated if curated is not None else ExecBinPolicy(allowed=frozenset())


__all__ = [
    "SANDBOX_FULL_ACCESS_ENV",
    "SKILLS_ROOT",
    "build_sandbox_tools",
    "resolve_sandbox_policy",
    "sandbox_full_access_enabled",
]
