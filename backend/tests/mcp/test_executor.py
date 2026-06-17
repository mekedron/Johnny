"""Unit tests: the MCP task executor's spoken-form contract (Johnny-trt.36).

Every leg must settle ``done``/``failed`` with speech-ready ``result_text``
and the diagnostic in ``error`` — never raise into the worker pass — and
non-MCP kinds must fall through to the fallback untouched.
"""

from __future__ import annotations

from typing import Any

from johnny.agent.tasks import QueuedTask, TaskResult, TaskSpec
from johnny.mcp.client import (
    McpCallResult,
    McpCallTimeoutError,
    McpToolError,
    McpUnavailableError,
)
from johnny.mcp.config import McpServerConfig
from johnny.mcp.executor import build_mcp_task_executor
from johnny.skills.executor import RESULT_TEXT_CAP_CHARS


class FakeManager:
    def __init__(self) -> None:
        self.result: McpCallResult | None = McpCallResult(
            text="echo: hi", is_error=False, duration_ms=5
        )
        self.error: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    async def call_tool(
        self,
        config: McpServerConfig,
        *,
        sandbox_url: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> McpCallResult:
        self.calls.append(
            {
                "server": config.name,
                "sandbox_url": sandbox_url,
                "tool": tool,
                "arguments": arguments,
            }
        )
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _task(kind: str, args: dict[str, Any] | None = None) -> QueuedTask:
    return QueuedTask(task_id=1, spec=TaskSpec(kind=kind, args=args or {}))


def _configs(**overrides: Any) -> list[McpServerConfig]:
    base: dict[str, Any] = {"name": "fixture", "transport": "stdio", "command": "python3"}
    base.update(overrides)
    return [McpServerConfig(**base)]


def _executor(
    manager: FakeManager,
    configs: list[McpServerConfig] | Exception,
    **kwargs: Any,
) -> Any:
    def _load() -> list[McpServerConfig]:
        if isinstance(configs, Exception):
            raise configs
        return configs

    return build_mcp_task_executor(
        manager, load_servers=_load, sandbox_url="http://sb:8088", **kwargs
    )


class FakeVoicer:
    """A stand-in :class:`johnny.mcp.executor.Voicer` (Johnny-d6w.30).

    Records each call and returns ``reply`` (or raises ``raise_exc``) so tests
    can assert the gate fires only on structured payloads and that any failure
    falls back to the raw text.
    """

    def __init__(
        self,
        reply: str | None = "voiced summary",
        *,
        raise_exc: Exception | None = None,
    ) -> None:
        self.reply = reply
        self.raise_exc = raise_exc
        self.calls: list[dict[str, Any]] = []

    async def voice(
        self,
        raw_text: str,
        *,
        tool: str,
        server: str,
        arguments: dict[str, Any],
    ) -> str | None:
        self.calls.append(
            {
                "raw_text": raw_text,
                "tool": tool,
                "server": server,
                "arguments": dict(arguments),
            }
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.reply


async def test_success_maps_text_to_speech_and_passes_args() -> None:
    manager = FakeManager()
    execute = _executor(manager, _configs())
    result = await execute(_task("mcp__fixture__echo", {"message": "hi"}))
    assert result.status == "done"
    assert result.result_text == "echo: hi"
    assert result.result_json == {
        "kind": "mcp__fixture__echo",
        "mcp_server": "fixture",
        "mcp_tool": "echo",
        "duration_ms": 5,
        "is_error": False,
    }
    assert manager.calls == [
        {
            "server": "fixture",
            "sandbox_url": "http://sb:8088",
            "tool": "echo",
            "arguments": {"message": "hi"},
        }
    ]


async def test_empty_result_text_speaks_nothing_to_report() -> None:
    manager = FakeManager()
    manager.result = McpCallResult(text="", is_error=False, duration_ms=2)
    execute = _executor(manager, _configs())
    result = await execute(_task("mcp__fixture__echo"))
    assert result.status == "done"
    assert "nothing to report" in result.result_text


async def test_long_result_text_capped_for_speech() -> None:
    manager = FakeManager()
    manager.result = McpCallResult(text="y" * 5000, is_error=False, duration_ms=2)
    execute = _executor(manager, _configs())
    result = await execute(_task("mcp__fixture__echo"))
    assert len(result.result_text) <= RESULT_TEXT_CAP_CHARS
    assert result.result_text.endswith("…")


async def test_non_mcp_kind_falls_through_to_fallback() -> None:
    manager = FakeManager()
    sentinel = TaskResult(status="failed", result_text="stub spoke")

    async def fallback(task: QueuedTask) -> TaskResult:
        return sentinel

    execute = _executor(manager, _configs(), fallback=fallback)
    assert await execute(_task("google-calendar")) is sentinel
    assert manager.calls == []


async def test_unknown_server_settles_failed_with_honest_speech() -> None:
    manager = FakeManager()
    execute = _executor(manager, _configs())
    result = await execute(_task("mcp__ghost__echo"))
    assert result.status == "failed"
    assert "ghost connector isn't configured" in result.result_text
    assert manager.calls == []


async def test_disabled_server_settles_failed() -> None:
    manager = FakeManager()
    execute = _executor(manager, _configs(enabled=False))
    result = await execute(_task("mcp__fixture__echo"))
    assert result.status == "failed"
    assert "isn't enabled" in result.result_text
    assert manager.calls == []


async def test_filtered_out_tool_settles_failed_without_calling() -> None:
    manager = FakeManager()
    execute = _executor(manager, _configs(tool_exclude=("echo",)))
    result = await execute(_task("mcp__fixture__echo"))
    assert result.status == "failed"
    assert "switched off" in result.result_text
    assert manager.calls == []


async def test_config_read_failure_fails_closed() -> None:
    manager = FakeManager()
    execute = _executor(manager, RuntimeError("db down"))
    result = await execute(_task("mcp__fixture__echo"))
    assert result.status == "failed"
    assert "couldn't check" in result.result_text
    assert manager.calls == []


async def test_unreachable_server_degrades_spoken() -> None:
    manager = FakeManager()
    manager.error = McpUnavailableError("connect refused")
    execute = _executor(manager, _configs())
    result = await execute(_task("mcp__fixture__echo"))
    assert result.status == "failed"
    assert "couldn't reach the fixture connector" in result.result_text
    assert "connect refused" in result.error


async def test_call_timeout_degrades_spoken() -> None:
    manager = FakeManager()
    manager.error = McpCallTimeoutError("60s elapsed")
    execute = _executor(manager, _configs())
    result = await execute(_task("mcp__fixture__echo"))
    assert result.status == "failed"
    assert "took too long" in result.result_text


async def test_protocol_rejection_degrades_spoken() -> None:
    manager = FakeManager()
    manager.error = McpToolError("unknown tool 'nope'")
    execute = _executor(manager, _configs())
    result = await execute(_task("mcp__fixture__nope", {}))
    assert result.status == "failed"
    assert "couldn't run" in result.result_text
    assert "unknown tool" in result.error


async def test_unexpected_exception_never_escapes() -> None:
    manager = FakeManager()
    manager.error = ValueError("boom")
    execute = _executor(manager, _configs())
    result = await execute(_task("mcp__fixture__echo"))
    assert result.status == "failed"
    assert "didn't work this time" in result.result_text
    assert "ValueError" in result.error


async def test_tool_level_is_error_settles_failed_with_diagnostic() -> None:
    manager = FakeManager()
    manager.result = McpCallResult(
        text="missing credential FOO", is_error=True, duration_ms=3
    )
    execute = _executor(manager, _configs())
    result = await execute(_task("mcp__fixture__always-fail"))
    assert result.status == "failed"
    assert "reported a problem" in result.result_text
    assert "missing credential FOO" in result.error
    assert result.result_json is not None and result.result_json["is_error"] is True


async def test_chained_behind_skill_executor_resolves_mcp_kinds() -> None:
    """The trt.24 chain: skills miss → MCP leg runs (the worker's wiring shape)."""
    from johnny.skills.executor import build_skill_task_executor
    from johnny.skills.policy import ExecBinPolicy
    from johnny.skills.registry import EMPTY_SKILL_REGISTRY
    from johnny.skills.sandbox import SandboxClient
    from johnny.skills.tools import SandboxExecTool

    manager = FakeManager()
    mcp_executor = _executor(manager, _configs())
    exec_tool = SandboxExecTool(
        SandboxClient(base_url="http://unused:1"), policy=ExecBinPolicy(allowed=frozenset())
    )
    chained = build_skill_task_executor(
        EMPTY_SKILL_REGISTRY, exec_tool, fallback=mcp_executor
    )
    result = await chained(_task("mcp__fixture__echo", {"message": "hi"}))
    assert result.status == "done"
    assert result.result_text == "echo: hi"
    # Internal kinds still hit the locality guard BEFORE the MCP leg.
    internal = await chained(_task("meeting.leave"))
    assert internal.status == "failed"
    assert "live session" in internal.result_text
    assert manager.calls and manager.calls[0]["tool"] == "echo"


# --- Johnny-d6w.30: voice structured tool output, never speak raw JSON --------


async def test_structured_json_result_is_voiced() -> None:
    manager = FakeManager()
    manager.result = McpCallResult(
        text='{"temp_c": 21, "city": "Paris"}', is_error=False, duration_ms=7
    )
    voicer = FakeVoicer("It's 21 degrees in Paris.")
    execute = _executor(manager, _configs(), voicer=voicer)
    result = await execute(_task("mcp__fixture__weather", {"city": "Paris"}))
    assert result.status == "done"
    # The SPOKEN text is the prose, never the JSON.
    assert result.result_text == "It's 21 degrees in Paris."
    # The raw payload is preserved for machine consumers / the trace UI.
    assert result.result_json is not None
    assert result.result_json["raw_text"] == '{"temp_c": 21, "city": "Paris"}'
    assert result.result_json["voiced"] is True
    assert result.result_json["mcp_server"] == "fixture"
    assert result.result_json["mcp_tool"] == "weather"
    # The voicer saw the raw output + runtime tool/server context (not literals).
    assert voicer.calls == [
        {
            "raw_text": '{"temp_c": 21, "city": "Paris"}',
            "tool": "weather",
            "server": "fixture",
            "arguments": {"city": "Paris"},
        }
    ]


async def test_json_array_result_is_voiced() -> None:
    manager = FakeManager()
    manager.result = McpCallResult(
        text='[{"id": 1}, {"id": 2}]', is_error=False, duration_ms=3
    )
    voicer = FakeVoicer("I found two dashboards.")
    execute = _executor(manager, _configs(), voicer=voicer)
    result = await execute(_task("mcp__fixture__list"))
    assert result.status == "done"
    assert result.result_text == "I found two dashboards."
    assert len(voicer.calls) == 1


async def test_prose_result_is_not_voiced() -> None:
    manager = FakeManager()  # default text "echo: hi" — not JSON
    voicer = FakeVoicer(raise_exc=AssertionError("must not voice prose"))
    execute = _executor(manager, _configs(), voicer=voicer)
    result = await execute(_task("mcp__fixture__echo"))
    assert result.status == "done"
    assert result.result_text == "echo: hi"
    assert voicer.calls == []
    # No voicing ⇒ result_json keeps its exact metadata shape (no raw_text/voiced).
    assert result.result_json is not None
    assert "raw_text" not in result.result_json
    assert "voiced" not in result.result_json


async def test_bare_scalar_json_is_not_voiced() -> None:
    manager = FakeManager()
    manager.result = McpCallResult(text="42", is_error=False, duration_ms=1)
    voicer = FakeVoicer(raise_exc=AssertionError("must not voice a bare scalar"))
    execute = _executor(manager, _configs(), voicer=voicer)
    result = await execute(_task("mcp__fixture__count"))
    assert result.status == "done"
    assert result.result_text == "42"
    assert voicer.calls == []


async def test_no_voicer_speaks_raw_json_unchanged() -> None:
    """Backward-compat / fallback: with no voicer the prior behavior holds."""
    manager = FakeManager()
    manager.result = McpCallResult(text='{"a": 1}', is_error=False, duration_ms=2)
    execute = _executor(manager, _configs())  # voicer omitted (None)
    result = await execute(_task("mcp__fixture__echo"))
    assert result.status == "done"
    assert result.result_text == '{"a": 1}'
    assert result.result_json is not None and "raw_text" not in result.result_json


async def test_voicer_raising_falls_back_to_raw() -> None:
    manager = FakeManager()
    manager.result = McpCallResult(text='{"a": 1}', is_error=False, duration_ms=2)
    voicer = FakeVoicer(raise_exc=RuntimeError("llm down"))
    execute = _executor(manager, _configs(), voicer=voicer)
    result = await execute(_task("mcp__fixture__echo"))
    assert result.status == "done"  # never fail the task on a voicing error
    assert result.result_text == '{"a": 1}'
    assert result.result_json is not None and "raw_text" not in result.result_json
    assert len(voicer.calls) == 1  # it WAS consulted (structured), then failed


async def test_voicer_empty_reply_falls_back_to_raw() -> None:
    manager = FakeManager()
    manager.result = McpCallResult(text='{"a": 1}', is_error=False, duration_ms=2)
    for empty in (None, "", "   "):
        voicer = FakeVoicer(empty)
        execute = _executor(manager, _configs(), voicer=voicer)
        result = await execute(_task("mcp__fixture__echo"))
        assert result.status == "done"
        assert result.result_text == '{"a": 1}'


async def test_voiced_output_is_capped() -> None:
    manager = FakeManager()
    manager.result = McpCallResult(text='{"a": 1}', is_error=False, duration_ms=2)
    voicer = FakeVoicer("z" * 5000)
    execute = _executor(manager, _configs(), voicer=voicer)
    result = await execute(_task("mcp__fixture__echo"))
    assert len(result.result_text) <= RESULT_TEXT_CAP_CHARS
    assert result.result_text.endswith("…")


async def test_voicer_is_generic_across_kinds() -> None:
    """No per-kind/tool/server branching: two different MCP kinds are voiced by
    the identical code path with only their runtime context differing."""
    manager = FakeManager()
    manager.result = McpCallResult(text='{"v": 1}', is_error=False, duration_ms=1)
    voicer = FakeVoicer("ok")
    for kind, server in (
        ("mcp__alpha__list", "alpha"),
        ("mcp__beta__fetch", "beta"),
    ):
        execute = _executor(manager, _configs(name=server), voicer=voicer)
        result = await execute(_task(kind))
        assert result.status == "done"
        assert result.result_text == "ok"
    assert [(c["server"], c["tool"]) for c in voicer.calls] == [
        ("alpha", "list"),
        ("beta", "fetch"),
    ]


async def test_tool_level_is_error_is_not_voiced() -> None:
    """The voicer only touches the SUCCESS leg; a tool isError stays a generic
    spoken failure with the raw text kept diagnostic in ``error``."""
    manager = FakeManager()
    manager.result = McpCallResult(
        text='{"error": "nope"}', is_error=True, duration_ms=3
    )
    voicer = FakeVoicer(raise_exc=AssertionError("must not voice a failure"))
    execute = _executor(manager, _configs(), voicer=voicer)
    result = await execute(_task("mcp__fixture__always-fail"))
    assert result.status == "failed"
    assert "reported a problem" in result.result_text
    assert '{"error": "nope"}' in result.error
    assert voicer.calls == []
