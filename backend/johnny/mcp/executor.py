"""The MCP task executor — third link in the worker's resolution chain.

Resolution order is the Johnny-trt.24 contract: internal → skills → **mcp**
→ fallback. The worker builds this executor as the *fallback* of the skill
runner (:func:`johnny.skills.executor.build_skill_task_executor`), so a kind
reaches here only after the internal locality guard and the skill registry
both passed on it; non-MCP-shaped kinds fall through to ``fallback`` (the
fail-fast stub).

Lazy lifecycle: nothing connects until a claimed kind actually references a
server — :class:`~johnny.mcp.client.McpClientManager` dials on first use,
reuses the live connection, and the worker's sweep evicts it after the
config's idle TTL. ``load_servers`` is called fresh per execution (the
trt.38 no-restart pattern): an operator's enable/disable/filter edit bites
the very next claim without a worker restart.

Every failure leg settles ``failed`` with **spoken-form** ``result_text``
and the diagnostic in ``error`` — an MCP server being down, misconfigured,
or slow must never crash the executor pass or leave an ack a dead promise.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence

from johnny.agent.tasks import QueuedTask, TaskExecutor, TaskResult, stub_executor
from johnny.mcp.config import McpServerConfig, parse_qualified_tool_name
from johnny.skills.executor import (
    RESULT_TEXT_CAP_CHARS,
    TaskProgressReporter,
    _report,
)

logger = logging.getLogger(__name__)

LoadServers = Callable[[], Sequence[McpServerConfig]]
"""Fresh enabled-server configs, read per execution (worker: a DB read)."""


def _cap_speech(text: str) -> str:
    cleaned = text.strip()
    if len(cleaned) <= RESULT_TEXT_CAP_CHARS:
        return cleaned
    return cleaned[: RESULT_TEXT_CAP_CHARS - 1].rstrip() + "…"


def _result_json(
    kind: str,
    server: str,
    tool: str,
    *,
    duration_ms: int | None = None,
    is_error: bool = False,
) -> dict[str, object]:
    return {
        "kind": kind,
        "mcp_server": server,
        "mcp_tool": tool,
        "duration_ms": duration_ms,
        "is_error": is_error,
    }


def build_mcp_task_executor(
    manager: object,
    *,
    load_servers: LoadServers,
    sandbox_url: str,
    fallback: TaskExecutor = stub_executor,
    progress_reporter: TaskProgressReporter | None = None,
) -> TaskExecutor:
    """The worker's MCP leg: qualified kinds run their server tool, rest fall through.

    ``manager`` is an :class:`~johnny.mcp.client.McpClientManager` (typed as
    ``object`` so importing this module never pulls the ``mcp`` SDK —
    the worker passes the real one, tests pass fakes). ``sandbox_url`` is the
    claimed task's resolved sandbox (stdio servers spawn there; the Phase-7
    per-agent resolver changes the caller, never this executor).

    ``progress_reporter`` (US-202), when given, narrates one ``mcp_call``
    milestone just before the tool call — a single-shot leg, so one milestone
    is the honest count. Reporting never affects the settle.
    """

    async def _execute(task: QueuedTask) -> TaskResult:
        kind = task.spec.kind
        parsed = parse_qualified_tool_name(kind)
        if parsed is None:
            return await fallback(task)
        server_name, tool = parsed

        # Typed errors only inside the call leg; this import stays lazy so
        # the executor module is importable without the SDK installed.
        from johnny.mcp.client import (
            McpCallTimeoutError,
            McpToolError,
            McpUnavailableError,
        )

        try:
            configs = load_servers()
        except Exception:
            logger.exception(
                "mcp executor: server-config read failed for kind=%s — failing closed",
                kind,
            )
            return TaskResult(
                status="failed",
                result_text=(
                    f"I couldn't check whether the {server_name} connector is "
                    "set up right now, so I didn't start that."
                ),
                error="mcp server-config read failed",
            )

        config = next((c for c in configs if c.name == server_name), None)
        if config is None or not config.enabled:
            state = "configured" if config is None else "enabled"
            return TaskResult(
                status="failed",
                result_text=(
                    f"The {tool} tool isn't available — the {server_name} "
                    f"connector isn't {state} right now."
                ),
                result_json=_result_json(kind, server_name, tool),
                error=f"mcp server {server_name!r} not {state}",
            )
        if not config.allows_tool(tool):
            return TaskResult(
                status="failed",
                result_text=(
                    f"The {tool} tool is switched off for the {server_name} "
                    "connector right now."
                ),
                result_json=_result_json(kind, server_name, tool),
                error=(
                    f"tool {tool!r} excluded by the {server_name!r} server's "
                    "include/exclude filters"
                ),
            )

        try:
            arguments = dict(task.spec.args)
        except (TypeError, ValueError):
            arguments = {}
        try:
            json.dumps(arguments, default=str)  # guard: args must be JSON-able
        except (TypeError, ValueError):
            arguments = {}

        await _report(
            progress_reporter, f"Running {tool} on {server_name}…", phase="mcp_call"
        )
        try:
            result = await manager.call_tool(  # type: ignore[attr-defined]
                config, sandbox_url=sandbox_url, tool=tool, arguments=arguments
            )
        except McpCallTimeoutError as exc:
            logger.info("mcp executor: kind=%s timed out (%s)", kind, exc)
            return TaskResult(
                status="failed",
                result_text=f"The {tool} task took too long, so I stopped it.",
                result_json=_result_json(kind, server_name, tool),
                error=str(exc),
            )
        except McpToolError as exc:
            logger.info("mcp executor: kind=%s rejected by server (%s)", kind, exc)
            return TaskResult(
                status="failed",
                result_text=(
                    f"The {server_name} connector couldn't run the {tool} tool."
                ),
                result_json=_result_json(kind, server_name, tool),
                error=str(exc),
            )
        except McpUnavailableError as exc:
            logger.info("mcp executor: kind=%s server unreachable (%s)", kind, exc)
            return TaskResult(
                status="failed",
                result_text=(
                    f"I couldn't reach the {server_name} connector, so the "
                    f"{tool} tool didn't run."
                ),
                result_json=_result_json(kind, server_name, tool),
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 — never crash the executor pass
            logger.exception("mcp executor: kind=%s unexpected failure", kind)
            return TaskResult(
                status="failed",
                result_text=f"The {tool} task didn't work this time.",
                result_json=_result_json(kind, server_name, tool),
                error=f"unexpected mcp failure: {type(exc).__name__}: {exc}",
            )

        payload = _result_json(
            kind,
            server_name,
            tool,
            duration_ms=result.duration_ms,
            is_error=result.is_error,
        )
        if result.is_error:
            # Tool-level error (isError): the protocol worked, the tool said
            # no. Its content is model/operator-facing, not authored for the
            # ear — speak a generic honest failure, keep the text diagnostic.
            logger.info(
                "mcp executor: kind=%s settled failed (tool isError)", kind
            )
            return TaskResult(
                status="failed",
                result_text=f"The {tool} tool reported a problem.",
                result_json=payload,
                error=_cap_speech(result.text) or "tool returned isError with no detail",
            )
        logger.info(
            "mcp executor: kind=%s settled done (%sms)", kind, result.duration_ms
        )
        return TaskResult(
            status="done",
            result_text=_cap_speech(result.text)
            or f"The {tool} task finished, but there was nothing to report.",
            result_json=payload,
        )

    return _execute


__all__ = ["LoadServers", "build_mcp_task_executor"]
