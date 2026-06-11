"""Integration tests for the skill registry + sandbox.exec tool (Johnny-trt.23).

Run against the REAL ``skills-sandbox`` compose service and the REAL skills
volume, from inside the stack::

    docker compose exec api pytest tests/integration/test_skill_registry_sandbox.py

Mirrors ``test_skills_sandbox.py`` (Johnny-trt.35): the whole module skips
loudly when the sandbox is unreachable — the dev-stack run is the
acceptance gate.

The fixture-skill test writes into the live skills volume (api mounts it
read-write for the future install flow) under a clearly-test-named
directory and removes it in ``finally`` — same snapshot/restore discipline
as the rest of the dev-stack suites.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import httpx
import pytest

from johnny.agent.tasks import QueuedTask, TaskSpec
from johnny.skills.executor import build_skill_task_executor
from johnny.skills.policy import ExecBinPolicy, build_policy
from johnny.skills.registry import (
    build_sandbox_availability_runner,
    load_skill_registry,
)
from johnny.skills.sandbox import (
    SandboxClient,
    sandbox_url_from_env,
    skills_dir_from_env,
)
from johnny.skills.tools import SandboxExecTool

SANDBOX_URL = sandbox_url_from_env()
SKILLS_DIR = Path(skills_dir_from_env())

_FIXTURE_SKILL_DIR = "zz-trt23-fixture-skill"


def _sandbox_reachable() -> bool:
    try:
        return httpx.get(f"{SANDBOX_URL}/health", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(
    not _sandbox_reachable(),
    reason=(
        f"skills-sandbox not reachable at {SANDBOX_URL} — run inside the "
        "compose stack: docker compose exec api pytest "
        "tests/integration/test_skill_registry_sandbox.py"
    ),
)


async def test_pipe_fixture_through_grep_wc_via_exec_tool() -> None:
    """The acceptance shape: baseline-tool pipeline through sandbox.exec."""
    client = SandboxClient()
    try:
        tool = SandboxExecTool(client, policy=build_policy())
        outcome = await tool.run(
            {"cmd": "printf 'alpha\\nbeta\\ngamma\\nbeta\\n' | grep beta | wc -l"}
        )
        assert outcome.ok is True, outcome.error
        assert outcome.output.strip() == "2"
    finally:
        await client.aclose()


async def test_exec_tool_denies_undeclared_binary_by_name() -> None:
    client = SandboxClient()
    try:
        tool = SandboxExecTool(client, policy=build_policy())
        outcome = await tool.run({"argv": ["nmap", "-p", "80", "localhost"]})
        assert outcome.ok is False
        assert outcome.data.get("denied") is True
        assert "'nmap'" in outcome.error
    finally:
        await client.aclose()


async def test_fixture_skill_dropped_into_volume_appears_in_catalog() -> None:
    """Drop an openclaw-format skill dir in -> discovered, eligible, cataloged."""
    if not os.access(SKILLS_DIR, os.W_OK):
        pytest.skip(f"skills volume {SKILLS_DIR} not writable from this container")
    fixture = SKILLS_DIR / _FIXTURE_SKILL_DIR
    client = SandboxClient()
    try:
        fixture.mkdir(parents=True, exist_ok=True)
        (fixture / "SKILL.md").write_text(
            "---\n"
            f"name: {_FIXTURE_SKILL_DIR}\n"
            'description: "Count words in a fixture file."\n'
            'metadata: {"openclaw": {"requires": {"bins": ["jq", "rg"]}}}\n'
            "---\n\nUse jq and rg from the baseline toolset.\n",
            encoding="utf-8",
        )
        registry = await load_skill_registry(SKILLS_DIR, check_bins=client.check_bins)
        skill = registry.get(_FIXTURE_SKILL_DIR)
        assert skill is not None, registry.summary()
        assert skill.eligible is True, skill.reasons
        assert _FIXTURE_SKILL_DIR in [entry.kind for entry in registry.catalog_entries()]
    finally:
        shutil.rmtree(fixture, ignore_errors=True)
        await client.aclose()


async def test_google_calendar_skill_end_to_end_through_executor() -> None:
    """The first skill, against the real gog in the real sandbox.

    All auth states are valid acceptance legs (Johnny-trt.55 added the
    availability snapshot, so the registry is loaded with the full
    production seam set — ``check_env`` + the declared-check runner, same
    as both production assemblies): an AVAILABLE skill runs — authed gog
    settles ``done`` with a speech-ready calendar summary, a mid-run auth
    break settles ``failed`` with the skill-authored graceful copy; an
    UNAVAILABLE-at-snapshot skill (gog not linked) never runs and settles
    ``failed`` with the same spoken-form reason the catalog declined with.
    Never a dead promise, no raw diagnostics in the speech.
    """
    client = SandboxClient()
    try:
        registry = await load_skill_registry(
            SKILLS_DIR,
            check_bins=client.check_bins,
            check_env=client.check_env,
            run_check=build_sandbox_availability_runner(client),
        )
        skill = registry.get("google-calendar")
        assert skill is not None, (
            f"google-calendar skill not on the volume ({SKILLS_DIR}) — "
            "run ./run-dev.sh (or ./run.sh) to seed the first-party skills"
        )
        assert skill.eligible is True, skill.reasons
        assert "google-calendar" in [entry.kind for entry in registry.catalog_entries()]

        tool = SandboxExecTool(client, policy=ExecBinPolicy(allowed=registry.allowed_bins))
        executor = build_skill_task_executor(registry, tool)
        result = await executor(
            QueuedTask(task_id=0, spec=TaskSpec(kind="google-calendar"))
        )

        assert result.status in {"done", "failed"}
        assert result.result_text, "result_text must always be speech-ready"
        assert "Traceback" not in result.result_text
        if not skill.available:
            # Unauthed at snapshot (gog not linked): the executor refuses the
            # run and settles with the spoken-form unavailable reason — no
            # result_json because no run happened.
            assert result.status == "failed"
            assert result.result_text == (
                skill.unavailable_reason
                or "The google-calendar skill isn't available in this session right now."
            )
            assert result.result_json is None
            return
        assert result.result_json is not None
        assert result.result_json.get("kind") == "google-calendar"
        if result.status == "done":
            # Authed: a real summary ("You have N events..." / "clear").
            assert "calendar" in result.result_text.lower() or "event" in result.result_text.lower()
        else:
            # Auth broke between snapshot and run (or misconfigured): the
            # graceful skill-authored copy — spoken-form words, never a
            # stack trace or raw stderr.
            assert "gog" in result.result_text or "Google" in result.result_text
    finally:
        await client.aclose()


async def test_run_script_formats_synthetic_events_speech_ready() -> None:
    """The done-leg formatter, deterministic regardless of gog auth state."""
    client = SandboxClient()
    try:
        events = (
            '{"events": [{"summary": "Standup", "start": {"dateTime": '
            '"2099-06-12T10:00:00+03:00"}}]}'
        )
        result = await client.exec(
            cmd=(
                "printf '%s' \"$EVENTS\" | "
                "python3 /skills/google-calendar/format_events.py --days 7"
            ),
            env={"EVENTS": events},
        )
        assert result.exit_code == 0, result.stderr
        assert result.stdout.strip() == (
            "You have 1 event in the next 7 days: 'Standup' on Friday June 12 at 10:00."
        )
    finally:
        await client.aclose()
