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

Claim-time revalidation (Johnny-trt.55): before running the argv, a skill
that declares ``metadata.johnny.availability.check`` gets that probe re-run
in the sandbox — the session catalog is a start-of-session snapshot and
links can break between ack and claim. A failing recheck settles ``failed``
with the skill-authored spoken reason (the same copy the catalog decline
carries), which the Johnny-trt.53 correction then speaks.

Task args are not interpreted in v1; they ride to the script as
``JOHNNY_TASK_ARGS_JSON`` (+ ``JOHNNY_TASK_KIND``) so skills can start
honouring them without an executor change.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol

from johnny.agent.internal_tools import is_internal_kind
from johnny.agent.tasks import QueuedTask, TaskExecutor, TaskResult, stub_executor
from johnny.skills.registry import (
    DEFAULT_AVAILABILITY_CHECK_TIMEOUT_S,
    LoadedSkill,
    SkillRegistry,
)
from johnny.skills.tools import SandboxExecTool, ToolOutcome

logger = logging.getLogger(__name__)

RESULT_TEXT_CAP_CHARS = 1200
"""Hard ceiling on speech-ready result text — a runaway script must not turn
into a minutes-long monologue; skills should format well under this."""

TASK_KIND_ENV = "JOHNNY_TASK_KIND"
TASK_ARGS_ENV = "JOHNNY_TASK_ARGS_JSON"

PHASE_AVAILABILITY_CHECK = "availability_check"
"""The claim-time availability recheck leg (Johnny-trt.55)."""
PHASE_RUN = "run"
"""The skill's declared run argv leg."""


@dataclass(frozen=True, slots=True)
class ToolCallTrace:
    """One tool invocation's full record for the reasoning timeline (Johnny-etu.4).

    What ``sandbox.exec`` (or a future tool) was asked to do and what it
    returned — the previously-ephemeral args + stdout/stderr/exit/duration the
    session detail page now persists and renders. The session/task/turn/kind
    binding is the *sink's* job (it knows the executing context); the trace
    carries only what one call produced. ``request`` is the exact arguments
    dict handed to the tool; ``error`` is the operator diagnostic, never spoken.
    """

    tool_name: str
    phase: str
    request: dict[str, Any]
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int | None
    timed_out: bool
    truncated: bool
    denied: bool
    error: str
    # Wall-clock bounds of the call (Johnny-oeq). Stamped by the caller that
    # owns the timing (the native tool wrapper brackets the sandbox round-trip);
    # None when not stamped → the sink/UI fall back to ``created_at``.
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ToolCallTraceSink(Protocol):
    """Durable persistence for per-tool-call traces (the ``agent_tool_calls`` table).

    Mirrors the :class:`~johnny.agent.tasks.TaskSink` split so the skills layer
    stays SQLAlchemy-free: production wires
    :class:`app.services.agent_tasks.SqlAlchemyToolCallTraceSink`; tests pass a
    simple collector. The executor calls :meth:`record` after every tool call;
    a sink that raises must never break the task (the caller swallows + logs).
    """

    async def record(self, trace: ToolCallTrace) -> None: ...


class TaskProgressReporter(Protocol):
    """Best-effort per-step progress signal for a running task (US-202, Johnny-d6w.14).

    Sibling to :class:`ToolCallTraceSink`: the worker binds a reporter to the
    claimed task; the executor calls :meth:`report` at meaningful milestones.
    The executor supplies only human-facing ``text`` + a ``phase`` tag — the
    reporter owns the binding (session/task/turn/kind + the monotonic step
    counter) and the publish. A reporter that raises must NEVER break the task
    (the caller swallows + logs, exactly like the trace sink).
    """

    async def report(self, text: str, *, phase: str | None = None) -> None: ...


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _trace_from_outcome(
    tool_name: str, phase: str, request: dict[str, Any], outcome: ToolOutcome
) -> ToolCallTrace:
    """Build a :class:`ToolCallTrace` from the args sent and the outcome returned."""
    data = outcome.data
    return ToolCallTrace(
        tool_name=tool_name,
        phase=phase,
        request=dict(request),
        ok=outcome.ok,
        exit_code=_int_or_none(data.get("exit_code")),
        stdout=str(data.get("stdout") or ""),
        stderr=str(data.get("stderr") or ""),
        duration_ms=_int_or_none(data.get("duration_ms")),
        timed_out=bool(data.get("timed_out", False)),
        truncated=bool(data.get("truncated", False)),
        denied=bool(data.get("denied", False)),
        error=outcome.error or "",
    )


def build_tool_call_trace(
    tool_name: str,
    phase: str,
    request: dict[str, Any],
    outcome: ToolOutcome,
    *,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> ToolCallTrace:
    """Public builder for an ``agent_tool_calls`` trace (Johnny-3ow).

    The task executor traces through :func:`_run_traced`; the agent's native
    sandbox tools (:mod:`johnny.agent.sandbox_tools`) run the SAME
    :class:`~johnny.skills.tools.SandboxExecTool` outside the task queue and
    persist through the SAME :class:`ToolCallTraceSink`, so they need this one
    seam to shape a trace from an outcome without reaching into the private
    helper. ``started_at``/``finished_at`` are the wall-clock bounds the native
    wrapper measured around the call (Johnny-oeq); omitted on the worker path.
    """
    trace = _trace_from_outcome(tool_name, phase, request, outcome)
    if started_at is not None or finished_at is not None:
        trace = replace(trace, started_at=started_at, finished_at=finished_at)
    return trace


async def _run_traced(
    exec_tool: SandboxExecTool,
    request: dict[str, Any],
    *,
    phase: str,
    trace_sink: ToolCallTraceSink | None,
) -> ToolOutcome:
    """Run one tool call and persist its trace (Johnny-etu.4).

    Tracing is best-effort observability and must NEVER affect execution: a
    sink failure is logged and swallowed so a write hiccup can't fail a task.
    """
    outcome = await exec_tool.run(request)
    if trace_sink is not None:
        try:
            await trace_sink.record(
                _trace_from_outcome(exec_tool.name, phase, request, outcome)
            )
        except Exception:  # pragma: no cover - defensive: tracing is best-effort
            logger.warning(
                "skill executor: tool-call trace sink failed (phase=%s) — continuing",
                phase,
                exc_info=True,
            )
    return outcome


async def _report(
    reporter: TaskProgressReporter | None, text: str, *, phase: str | None
) -> None:
    """Emit one best-effort progress milestone (US-202).

    Mirrors :func:`_run_traced`'s swallow discipline: progress is observability,
    never execution — a reporter failure is logged and swallowed so a publish
    hiccup can't fail a task.
    """
    if reporter is None:
        return
    try:
        await reporter.report(text, phase=phase)
    except Exception:  # pragma: no cover - defensive: progress is best-effort
        logger.warning(
            "skill executor: progress reporter failed (phase=%s) — continuing",
            phase,
            exc_info=True,
        )


def _cap_speech(text: str) -> str:
    cleaned = text.strip()
    if len(cleaned) <= RESULT_TEXT_CAP_CHARS:
        return cleaned
    return cleaned[: RESULT_TEXT_CAP_CHARS - 1].rstrip() + "…"


def _result_json(kind: str, outcome: ToolOutcome) -> dict[str, object]:
    """Structured row payload for the tasks panel / machine consumers."""
    payload: dict[str, object] = {
        "kind": kind,
        "exit_code": outcome.data.get("exit_code"),
        "duration_ms": outcome.data.get("duration_ms"),
        "timed_out": outcome.data.get("timed_out", False),
        "truncated": outcome.data.get("truncated", False),
    }
    policy_denied = outcome.data.get("policy_denied")
    if isinstance(policy_denied, dict):
        # trt.38 attribution (a capability-policy bin denial): ride the row
        # so the worker emits the policy_denied event naming the layer.
        payload["policy_denied"] = dict(policy_denied)
    return payload


async def _revalidate_availability(
    skill: LoadedSkill,
    exec_tool: SandboxExecTool,
    *,
    trace_sink: ToolCallTraceSink | None = None,
) -> TaskResult | None:
    """Re-run the skill's declared availability check at claim time (Johnny-trt.55).

    The session catalog is a start-of-session snapshot; links can break
    between the ack and the claim. Skills declaring
    ``metadata.johnny.availability.check`` get the same in-sandbox probe the
    loader ran: exit 0 → ``None`` (proceed to the run argv); any other exit →
    a ``failed`` result whose ``result_text`` is the check's stdout (the
    skill-authored spoken copy — the SAME actionable reason the catalog
    decline carries) so the trt.53 correction walks the ack back honestly.
    A probe that could not run (sandbox unreachable / denied / timed out)
    fails with could-not-verify speech rather than asserting a credential
    gap. Skills without a check pay nothing here — their run script owns the
    graceful failure leg (the trt.23 contract).
    """
    availability = skill.document.availability
    if availability is None or availability.check is None:
        return None
    check = availability.check
    kind = skill.name
    outcome = await _run_traced(
        exec_tool,
        {
            "argv": list(check.argv),
            "timeout_s": check.timeout_s or DEFAULT_AVAILABILITY_CHECK_TIMEOUT_S,
        },
        phase=PHASE_AVAILABILITY_CHECK,
        trace_sink=trace_sink,
    )
    if outcome.ok:
        return None
    if (
        outcome.data.get("unreachable")
        or outcome.data.get("denied")
        or outcome.data.get("timed_out")
        or outcome.data.get("exit_code") is None
    ):
        logger.info(
            "skill executor: kind=%s availability recheck could not run (%s)",
            kind,
            outcome.error or "no detail",
        )
        return TaskResult(
            status="failed",
            result_text=(
                f"I couldn't verify the {kind} task can run right now, so I didn't start it."
            ),
            result_json=_result_json(kind, outcome),
            error=f"availability recheck did not run: {outcome.error or 'no detail'}",
        )
    spoken = (
        _cap_speech(outcome.output)
        or availability.unavailable_reason
        or f"The {kind} capability isn't connected right now."
    )
    logger.info(
        "skill executor: kind=%s availability recheck failed (exit %s) — task not run",
        kind,
        outcome.data.get("exit_code"),
    )
    return TaskResult(
        status="failed",
        result_text=spoken,
        result_json=_result_json(kind, outcome),
        error=f"availability recheck failed: {outcome.error or 'non-zero exit'}",
    )


def build_skill_task_executor(
    registry: SkillRegistry,
    exec_tool: SandboxExecTool,
    *,
    fallback: TaskExecutor = stub_executor,
    trace_sink: ToolCallTraceSink | None = None,
    progress_reporter: TaskProgressReporter | None = None,
) -> TaskExecutor:
    """The session's executor: skills run in the sandbox, the rest fail fast.

    ``trace_sink`` (Johnny-etu.4), when given, receives a :class:`ToolCallTrace`
    after every ``sandbox.exec`` this executor makes — the availability recheck
    and the run argv alike — so the previously-ephemeral tool args + output are
    persisted for the session detail timeline. Tracing never affects the settle.

    ``progress_reporter`` (US-202), when given, receives a human-facing
    milestone before the availability recheck (``availability_check``) and
    before the run argv (``run``) — so the Workstreams column can narrate a
    running task. Like tracing, reporting never affects the settle.
    """

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

        if not skill.available:
            # Defensive (Johnny-trt.55): the catalog carries this kind as
            # unavailable and the gate degrades delegate verdicts targeting
            # it, so reaching here means a hand-queued row or a bypassed
            # gate — settle with the same spoken-form reason the decline
            # uses.
            return TaskResult(
                status="failed",
                result_text=skill.unavailable_reason
                or f"The {kind} skill isn't available in this session right now.",
                error=f"skill unavailable at session snapshot: {skill.unavailable_reason}",
            )

        # US-202 milestone: only narrate the availability probe when the skill
        # actually declares one (skills without a check emit just the run step).
        availability = skill.document.availability
        if availability is not None and availability.check is not None:
            await _report(
                progress_reporter,
                f"Checking the {kind} connection…",
                phase=PHASE_AVAILABILITY_CHECK,
            )
        recheck = await _revalidate_availability(
            skill, exec_tool, trace_sink=trace_sink
        )
        if recheck is not None:
            return recheck

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
        await _report(
            progress_reporter, f"Running the {kind} task…", phase=PHASE_RUN
        )
        outcome = await _run_traced(
            exec_tool,
            {
                "argv": list(run.argv),
                "timeout_s": run.timeout_s,
                "env": {TASK_KIND_ENV: kind, TASK_ARGS_ENV: args_json},
            },
            phase=PHASE_RUN,
            trace_sink=trace_sink,
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
            if isinstance(outcome.data.get("policy_denied"), dict):
                # trt.38: the capability policy (not the v1 grant model)
                # blocked the binary — say so in operator-actionable terms.
                result_text = (
                    f"I'm not allowed to run what the {kind} skill asked for — "
                    "my operator's policy blocks one of its tools."
                )
            else:
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
    "PHASE_AVAILABILITY_CHECK",
    "PHASE_RUN",
    "RESULT_TEXT_CAP_CHARS",
    "TASK_ARGS_ENV",
    "TASK_KIND_ENV",
    "ToolCallTrace",
    "ToolCallTraceSink",
    "TaskProgressReporter",
    "build_tool_call_trace",
    "build_skill_task_executor",
]
