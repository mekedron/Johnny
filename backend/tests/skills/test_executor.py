"""The v1 skill task executor's settle matrix (Johnny-trt.23).

A real registry + a real SandboxExecTool over httpx.MockTransport, so the
matrix pins exactly what reaches the agent_tasks row: done-with-speech,
script-authored failure copy, timeout/unreachable/denial degrades, the
no-runner leg for dropped-in openclaw skills, and the stub fallback for
unknown kinds — an ack must never become a dead promise.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from johnny.agent.tasks import QueuedTask, TaskSpec
from johnny.skills.executor import (
    RESULT_TEXT_CAP_CHARS,
    TASK_ARGS_ENV,
    TASK_KIND_ENV,
    build_skill_task_executor,
)
from johnny.skills.policy import ExecBinPolicy
from johnny.skills.registry import SkillRegistry, load_skill_registry
from johnny.skills.sandbox import SandboxClient
from johnny.skills.tools import SandboxExecTool

_BASE_REPLY: dict[str, Any] = {
    "exit_code": 0,
    "stdout": "",
    "stderr": "",
    "truncated": False,
    "stdout_truncated": False,
    "stderr_truncated": False,
    "timed_out": False,
    "duration_ms": 40,
}


async def _registry_with_runner(tmp_path: Path, *, with_run: bool = True) -> SkillRegistry:
    metadata: dict[str, Any] = {"openclaw": {"requires": {"bins": ["grep"]}}}
    if with_run:
        metadata["johnny"] = {
            "run": {"argv": ["bash", "/skills/fetch-news/run.sh"], "timeout_s": 45}
        }
    directory = tmp_path / "fetch-news"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\n"
        "name: fetch-news\n"
        'description: "Fetch the news."\n'
        f"metadata: '{json.dumps(metadata)}'\n"
        "---\n\nInstructions.\n",
        encoding="utf-8",
    )

    async def all_present(names: list[str]) -> dict[str, bool]:
        return {name: True for name in names}

    return await load_skill_registry(tmp_path, check_bins=all_present)


def _tool(
    registry: SkillRegistry, reply: dict[str, Any], bodies: list[dict[str, Any]] | None = None
) -> SandboxExecTool:
    def handle(request: httpx.Request) -> httpx.Response:
        if bodies is not None and request.url.path == "/exec":
            bodies.append(json.loads(request.content.decode()))
        return httpx.Response(200, json=reply)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://s")
    client = SandboxClient("http://s", http_client=http)
    return SandboxExecTool(client, policy=ExecBinPolicy(allowed=registry.allowed_bins))


def _task(kind: str, args: dict[str, Any] | None = None) -> QueuedTask:
    return QueuedTask(task_id=1, spec=TaskSpec(kind=kind, args=args or {}))


async def test_exit_zero_settles_done_with_stdout_as_speech(tmp_path: Path) -> None:
    registry = await _registry_with_runner(tmp_path)
    bodies: list[dict[str, Any]] = []
    reply = dict(_BASE_REPLY, stdout="Here are today's three headlines.\n")
    executor = build_skill_task_executor(registry, _tool(registry, reply, bodies))

    result = await executor(_task("fetch-news", {"topic": "ai"}))
    assert result.status == "done"
    assert result.result_text == "Here are today's three headlines."
    assert result.result_json == {
        "kind": "fetch-news",
        "exit_code": 0,
        "duration_ms": 40,
        "timed_out": False,
        "truncated": False,
    }
    # The declared argv ran with the task context env (forward-compat seam).
    body = bodies[0]
    assert body["argv"] == ["bash", "/skills/fetch-news/run.sh"]
    assert body["timeout"] == 45
    assert body["env"][TASK_KIND_ENV] == "fetch-news"
    assert json.loads(body["env"][TASK_ARGS_ENV]) == {"topic": "ai"}


async def test_exit_zero_empty_stdout_gets_default_copy(tmp_path: Path) -> None:
    registry = await _registry_with_runner(tmp_path)
    executor = build_skill_task_executor(registry, _tool(registry, dict(_BASE_REPLY)))
    result = await executor(_task("fetch-news"))
    assert result.status == "done"
    assert "nothing to report" in result.result_text


async def test_nonzero_exit_speaks_script_authored_copy(tmp_path: Path) -> None:
    registry = await _registry_with_runner(tmp_path)
    reply = dict(
        _BASE_REPLY,
        exit_code=2,
        stdout="I can't reach the news service yet — connect an account first.\n",
        stderr="auth: no token",
    )
    executor = build_skill_task_executor(registry, _tool(registry, reply))
    result = await executor(_task("fetch-news"))
    assert result.status == "failed"
    assert result.result_text.startswith("I can't reach the news service")
    assert "exit 2" in result.error
    assert "auth: no token" in result.error


async def test_nonzero_exit_without_stdout_gets_generic_copy(tmp_path: Path) -> None:
    registry = await _registry_with_runner(tmp_path)
    reply = dict(_BASE_REPLY, exit_code=1, stderr="stack trace")
    executor = build_skill_task_executor(registry, _tool(registry, reply))
    result = await executor(_task("fetch-news"))
    assert result.status == "failed"
    assert result.result_text == "The fetch-news task didn't work this time."


async def test_timeout_speaks_too_long(tmp_path: Path) -> None:
    registry = await _registry_with_runner(tmp_path)
    reply = dict(_BASE_REPLY, exit_code=-9, timed_out=True)
    executor = build_skill_task_executor(registry, _tool(registry, reply))
    result = await executor(_task("fetch-news"))
    assert result.status == "failed"
    assert "took too long" in result.result_text


async def test_unreachable_sandbox_speaks_honestly(tmp_path: Path) -> None:
    registry = await _registry_with_runner(tmp_path)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(boom), base_url="http://s")
    tool = SandboxExecTool(
        SandboxClient("http://s", http_client=http),
        policy=ExecBinPolicy(allowed=registry.allowed_bins),
    )
    executor = build_skill_task_executor(registry, tool)
    result = await executor(_task("fetch-news"))
    assert result.status == "failed"
    assert "sandbox isn't reachable" in result.result_text
    assert "unreachable" in result.error or "sandbox" in result.error


async def test_policy_denial_speaks_not_allowed(tmp_path: Path) -> None:
    registry = await _registry_with_runner(tmp_path)
    tool_denying_everything = SandboxExecTool(
        SandboxClient("http://s", http_client=httpx.AsyncClient(base_url="http://s")),
        policy=ExecBinPolicy(allowed=frozenset()),
    )
    executor = build_skill_task_executor(registry, tool_denying_everything)
    result = await executor(_task("fetch-news"))
    assert result.status == "failed"
    assert "not allowed" in result.result_text
    assert "'bash'" in result.error


async def test_unknown_kind_falls_through_to_stub(tmp_path: Path) -> None:
    registry = await _registry_with_runner(tmp_path)
    executor = build_skill_task_executor(registry, _tool(registry, dict(_BASE_REPLY)))
    result = await executor(_task("no-such-kind"))
    assert result.status == "failed"
    assert "don't know how to run no-such-kind" in result.result_text


async def test_openclaw_skill_without_runner_fails_honestly(tmp_path: Path) -> None:
    registry = await _registry_with_runner(tmp_path, with_run=False)
    executor = build_skill_task_executor(registry, _tool(registry, dict(_BASE_REPLY)))
    result = await executor(_task("fetch-news"))
    assert result.status == "failed"
    assert "can't follow its instructions on my own yet" in result.result_text
    assert "Johnny-trt.22/24" in result.error


async def test_ineligible_skill_settles_with_reason(tmp_path: Path) -> None:
    directory = tmp_path / "mail"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: mail\ndescription: d\n"
        "metadata: '"
        + json.dumps(
            {
                "openclaw": {"requires": {"bins": ["himalaya"]}},
                "johnny": {"run": {"argv": ["himalaya"]}},
            }
        )
        + "'\n---\n",
        encoding="utf-8",
    )

    async def nothing_present(names: list[str]) -> dict[str, bool]:
        return {name: False for name in names}

    registry = await load_skill_registry(tmp_path, check_bins=nothing_present)
    executor = build_skill_task_executor(registry, _tool(registry, dict(_BASE_REPLY)))
    result = await executor(_task("mail"))
    assert result.status == "failed"
    assert "isn't usable right now" in result.result_text
    assert "himalaya" in result.error


async def test_result_text_capped_for_speech(tmp_path: Path) -> None:
    registry = await _registry_with_runner(tmp_path)
    reply = dict(_BASE_REPLY, stdout="word " * 2000)
    executor = build_skill_task_executor(registry, _tool(registry, reply))
    result = await executor(_task("fetch-news"))
    assert result.status == "done"
    assert len(result.result_text) <= RESULT_TEXT_CAP_CHARS
    assert result.result_text.endswith("…")
