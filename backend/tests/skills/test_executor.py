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


async def test_internal_kinds_refused_by_locality_guard(tmp_path: Path) -> None:
    """Internal kinds never enter the sandbox.exec path (Johnny-trt.57).

    Even a skill package that *claims* an internal kind on the volume must
    not run for it: the guard fires before the registry lookup, the exec
    request list stays empty, and the stub fallback is never consulted.
    """
    from johnny.agent.internal_tools import MEETING_LEAVE_KIND, SESSION_END_KIND

    registry = await _registry_with_runner(tmp_path)
    bodies: list[dict[str, Any]] = []
    executor = build_skill_task_executor(
        registry, _tool(registry, dict(_BASE_REPLY), bodies)
    )
    for kind in (MEETING_LEAVE_KIND, SESSION_END_KIND):
        result = await executor(_task(kind))
        assert result.status == "failed"
        assert "only run inside the live session" in result.result_text
        assert "locality guard" in result.error
        assert "Johnny-trt.57" in result.error
    assert bodies == []  # nothing reached the sandbox


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


# --- claim-time availability revalidation (Johnny-trt.55) ----------------------


async def _registry_with_check(
    tmp_path: Path, *, available_at_load: bool = True
) -> SkillRegistry:
    """A calendar-style skill declaring both a runner and an availability check."""
    metadata = {
        "openclaw": {"requires": {"bins": ["grep"]}},
        "johnny": {
            "run": {"argv": ["bash", "/skills/cal/run.sh"], "timeout_s": 45},
            "availability": {
                "check": {"argv": ["bash", "/skills/cal/check.sh"], "timeout_s": 10},
                "unavailable_reason": "no account linked — connect one first.",
            },
        },
    }
    directory = tmp_path / "cal"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: cal\ndescription: \"Calendar.\"\n"
        f"metadata: '{json.dumps(metadata)}'\n---\n\nInstructions.\n",
        encoding="utf-8",
    )

    async def all_present(names: list[str]) -> dict[str, bool]:
        return {name: True for name in names}

    from johnny.skills.registry import AvailabilityProbeOutcome

    async def load_check(_spec: Any) -> AvailabilityProbeOutcome:
        if available_at_load:
            return AvailabilityProbeOutcome(ran=True, exit_code=0)
        return AvailabilityProbeOutcome(
            ran=True, exit_code=2, stdout="no account linked — connect one first."
        )

    return await load_skill_registry(
        tmp_path, check_bins=all_present, run_check=load_check
    )


def _sequenced_tool(
    registry: SkillRegistry,
    replies: list[dict[str, Any]],
    bodies: list[dict[str, Any]],
) -> SandboxExecTool:
    """Each /exec request consumes the next reply — check first, then run."""

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode()))
        return httpx.Response(200, json=replies[len(bodies) - 1])

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://s")
    client = SandboxClient("http://s", http_client=http)
    return SandboxExecTool(client, policy=ExecBinPolicy(allowed=registry.allowed_bins))


async def test_recheck_passes_then_run_executes(tmp_path: Path) -> None:
    """Happy path: the claim-time check (exit 0) precedes the run argv."""
    registry = await _registry_with_check(tmp_path)
    bodies: list[dict[str, Any]] = []
    tool = _sequenced_tool(
        registry,
        [dict(_BASE_REPLY), dict(_BASE_REPLY, stdout="Two events tomorrow.\n")],
        bodies,
    )
    result = await build_skill_task_executor(registry, tool)(_task("cal"))

    assert result.status == "done"
    assert result.result_text == "Two events tomorrow."
    assert [body["argv"] for body in bodies] == [
        ["bash", "/skills/cal/check.sh"],
        ["bash", "/skills/cal/run.sh"],
    ]
    assert bodies[0]["timeout"] == 10  # the check's own budget, not the run's


async def test_recheck_failure_blocks_run_and_speaks_the_same_reason(
    tmp_path: Path,
) -> None:
    """The link broke between ack and claim (trt.55 acceptance): the recheck
    fails with the skill-authored copy, the run argv never executes, and the
    failed settle carries the SAME actionable reason the catalog decline uses
    (the trt.53 correction then speaks it)."""
    registry = await _registry_with_check(tmp_path)
    bodies: list[dict[str, Any]] = []
    tool = _sequenced_tool(
        registry,
        [dict(_BASE_REPLY, exit_code=2, stdout="no account linked — connect one first.\n")],
        bodies,
    )
    result = await build_skill_task_executor(registry, tool)(_task("cal"))

    assert result.status == "failed"
    assert result.result_text == "no account linked — connect one first."
    assert "availability recheck failed" in result.error
    assert len(bodies) == 1  # the run argv never reached the sandbox


async def test_recheck_unreachable_speaks_could_not_verify(tmp_path: Path) -> None:
    """A sandbox blip at claim time must NOT assert the credential gap — the
    honest copy is could-not-verify, and nothing runs."""
    registry = await _registry_with_check(tmp_path)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(boom), base_url="http://s")
    tool = SandboxExecTool(
        SandboxClient("http://s", http_client=http),
        policy=ExecBinPolicy(allowed=registry.allowed_bins),
    )
    result = await build_skill_task_executor(registry, tool)(_task("cal"))

    assert result.status == "failed"
    assert "couldn't verify" in result.result_text
    assert "didn't start it" in result.result_text
    assert "no account linked" not in result.result_text


async def test_snapshot_unavailable_skill_settles_without_any_exec(tmp_path: Path) -> None:
    """Defense in depth: a skill the session snapshot already holds unavailable
    settles failed with the same spoken reason — no sandbox call at all."""
    registry = await _registry_with_check(tmp_path, available_at_load=False)
    bodies: list[dict[str, Any]] = []
    tool = _sequenced_tool(registry, [dict(_BASE_REPLY)], bodies)
    result = await build_skill_task_executor(registry, tool)(_task("cal"))

    assert result.status == "failed"
    assert result.result_text == "no account linked — connect one first."
    assert "unavailable at session snapshot" in result.error
    assert bodies == []  # nothing reached the sandbox


async def test_skill_without_check_pays_no_recheck_exec(tmp_path: Path) -> None:
    """No declared check ⇒ exactly one exec (the run) — the trt.23 path
    byte-for-byte; the run script owns its own graceful failure leg."""
    registry = await _registry_with_runner(tmp_path)
    bodies: list[dict[str, Any]] = []
    reply = dict(_BASE_REPLY, stdout="ok\n")
    executor = build_skill_task_executor(registry, _tool(registry, reply, bodies))
    result = await executor(_task("fetch-news"))
    assert result.status == "done"
    assert [body["argv"] for body in bodies] == [["bash", "/skills/fetch-news/run.sh"]]
