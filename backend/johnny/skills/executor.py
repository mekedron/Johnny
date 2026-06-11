"""The v1 skill task executor — deterministic runner, speech-ready results.

:func:`build_skill_task_executor` produces the
:data:`~johnny.agent.tasks.TaskExecutor` callable the session's
:class:`~johnny.agent.tasks.TaskCoordinator` drives (and the Phase-4 worker
pass, Johnny-trt.24, will reuse): resolve ``task.kind`` to a loaded skill
and run its declared ``metadata.johnny.run`` argv inside the skills-sandbox
through the policy-checked ``sandbox.exec`` tool.

The runner contract (documented in ``skills/README.md``):

* exit ``0`` → the task settles ``done``; **stdout is the speech-ready
  result** (the skill's script formats for the ear, not the eye);
* any other exit → ``failed``; when the script printed to stdout, that text
  is the skill-authored spoken failure copy (e.g. google-calendar's
  "no Google account connected" line) — stderr stays diagnostic-only;
* timeout / unreachable sandbox / policy denial → ``failed`` with generic
  honest speech and the detail in ``error``.

Kinds with no matching skill fall through to ``fallback`` (the Phase-3
:func:`~johnny.agent.tasks.stub_executor` fail-fast), and eligible skills
*without* a run spec (openclaw skills dropped in unchanged) settle ``failed``
honestly until the LLM execution engine lands (Johnny-trt.22 decides it,
Johnny-trt.24 wires it) — an ack must never become a dead promise.

Task args are not interpreted in v1; they ride to the script as
``JOHNNY_TASK_ARGS_JSON`` (+ ``JOHNNY_TASK_KIND``) so skills can start
honouring them without an executor change.
"""

from __future__ import annotations

import json
import logging

from johnny.agent.internal_tools import is_internal_kind
from johnny.agent.tasks import QueuedTask, TaskExecutor, TaskResult, stub_executor
from johnny.skills.registry import SkillRegistry
from johnny.skills.tools import SandboxExecTool, ToolOutcome

logger = logging.getLogger(__name__)

RESULT_TEXT_CAP_CHARS = 1200
"""Hard ceiling on speech-ready result text — a runaway script must not turn
into a minutes-long monologue; skills should format well under this."""

TASK_KIND_ENV = "JOHNNY_TASK_KIND"
TASK_ARGS_ENV = "JOHNNY_TASK_ARGS_JSON"


def _cap_speech(text: str) -> str:
    cleaned = text.strip()
    if len(cleaned) <= RESULT_TEXT_CAP_CHARS:
        return cleaned
    return cleaned[: RESULT_TEXT_CAP_CHARS - 1].rstrip() + "…"


def _result_json(kind: str, outcome: ToolOutcome) -> dict[str, object]:
    """Structured row payload for the tasks panel / machine consumers."""
    return {
        "kind": kind,
        "exit_code": outcome.data.get("exit_code"),
        "duration_ms": outcome.data.get("duration_ms"),
        "timed_out": outcome.data.get("timed_out", False),
        "truncated": outcome.data.get("truncated", False),
    }


def build_skill_task_executor(
    registry: SkillRegistry,
    exec_tool: SandboxExecTool,
    *,
    fallback: TaskExecutor = stub_executor,
) -> TaskExecutor:
    """The session's executor: skills run in the sandbox, the rest fail fast."""

    async def _execute(task: QueuedTask) -> TaskResult:
        kind = task.spec.kind
        if is_internal_kind(kind):
            # Locality guard (Johnny-trt.57): internal kinds run in the live
            # agent process only — session-local actions like meeting.leave /
            # session.end must never reach the sandbox (or a future external
            # worker pass reusing this executor, Johnny-trt.24). In-session
            # they never get here (the internal executor resolves first);
            # this refuses stale-catalog or hand-queued rows honestly.
            logger.error(
                "skill executor: refusing internal kind %r — internal tools "
                "run only in the live agent process (locality guard)",
                kind,
            )
            return TaskResult(
                status="failed",
                result_text=(
                    f"The {kind} action can only run inside the live session, "
                    "not in the background worker."
                ),
                error=(
                    f"locality guard: internal kind {kind!r} must run in the "
                    "agent process (Johnny-trt.57); sandbox/worker execution refused"
                ),
            )
        skill = registry.get(kind)
        if skill is None:
            return await fallback(task)

        if not skill.eligible:
            # Defensive: ineligible skills are not in the catalog, so the
            # router should never target them — but a stale catalog or a
            # hand-queued row must still settle honestly.
            reason = skill.reasons[0] if skill.reasons else "it is not available right now"
            return TaskResult(
                status="failed",
                result_text=f"The {kind} skill isn't usable right now — {reason}.",
                error=f"skill ineligible: {'; '.join(skill.reasons)}",
            )

        run = skill.document.run
        if run is None:
            return TaskResult(
                status="failed",
                result_text=(
                    f"I found the {kind} skill, but I can't follow its instructions "
                    "on my own yet."
                ),
                error=(
                    "skill has no metadata.johnny.run spec; autonomous instruction-"
                    "following lands with the execution engine (Johnny-trt.22/24)"
                ),
            )

        try:
            args_json = json.dumps(task.spec.args, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            args_json = "{}"
        outcome = await exec_tool.run(
            {
                "argv": list(run.argv),
                "timeout_s": run.timeout_s,
                "env": {TASK_KIND_ENV: kind, TASK_ARGS_ENV: args_json},
            }
        )

        spoken = _cap_speech(outcome.output)
        if outcome.ok:
            logger.info(
                "skill executor: kind=%s settled done (exit 0, %sms)",
                kind,
                outcome.data.get("duration_ms"),
            )
            return TaskResult(
                status="done",
                result_text=spoken
                or f"The {kind} task finished, but there was nothing to report.",
                result_json=_result_json(kind, outcome),
            )

        if outcome.data.get("denied"):
            result_text = f"I'm not allowed to run what the {kind} skill asked for."
        elif outcome.data.get("unreachable"):
            result_text = (
                f"I couldn't run the {kind} task — my tools sandbox isn't reachable right now."
            )
        elif outcome.data.get("timed_out"):
            result_text = f"The {kind} task took too long, so I stopped it."
        else:
            # The skill's script may have authored the spoken failure copy on
            # stdout (the graceful "not connected" leg); otherwise speak a
            # generic honest failure and keep the diagnostics in error.
            result_text = spoken or f"The {kind} task didn't work this time."
        logger.info(
            "skill executor: kind=%s settled failed (%s)", kind, outcome.error or "no detail"
        )
        return TaskResult(
            status="failed",
            result_text=result_text,
            result_json=_result_json(kind, outcome),
            error=outcome.error,
        )

    return _execute


__all__ = [
    "RESULT_TEXT_CAP_CHARS",
    "TASK_ARGS_ENV",
    "TASK_KIND_ENV",
    "build_skill_task_executor",
]
