"""Worker executor pass for delegated agent tasks (Johnny-trt.24, Phase 4).

The session side queues: a ``delegate`` verdict persists an ``agent_tasks``
row (row-before-ack, Johnny-trt.18), announces ``TaskQueued``, and pings the
shared ``johnny.tasks.wake`` channel. This module is the other half — the
process that actually runs the work, per the hand-rolled-asyncio decision of
docs/TASK-ENGINE.md (Johnny-trt.22):

* **Claim** — ``SELECT … FOR UPDATE SKIP LOCKED`` + ``UPDATE … RETURNING``
  in one transaction (:func:`claim_queued_tasks`): atomic under concurrent
  claimers, increments ``attempts`` as the liveness/fence stamp. Internal
  kinds (Johnny-trt.57 — ``meeting.leave``, ``session.end``) are **never
  claimed**: they execute session-locally only; the session coordinator's
  locality split (:data:`johnny.agent.tasks.RunsInSession`) is the mirror
  guarantee, so one task can never have two executors.
* **Run** — the kind resolves through the trt.23 deterministic skill runner
  (:func:`johnny.skills.executor.build_skill_task_executor`) against the
  skills-sandbox; the worker never executes skill commands in its own
  container. The sandbox endpoint is resolved per task through
  :func:`resolve_sandbox_url` — ONE function on purpose: Phase 7's per-agent
  sandboxes change only this resolver, never the claim/run/settle loop. The
  future multi-step instruction loop (and its per-agent reasoning-model
  resolution, Johnny-trt.42) plugs in behind the same ``TaskExecutor`` seam.
  Bounded concurrency (semaphore) + a hard per-task timeout below the
  requeue TTL keep a slow tool from starving anything and keep "stale
  ``running`` row" synonymous with "dead worker".
* **Settle** — ``done`` / ``failed`` + speech-ready ``result_text`` written
  with an attempts-fenced UPDATE (:func:`settle_claimed_task`): a runner
  whose row was TTL-requeued and re-claimed while it straggled matches zero
  rows, writes nothing, and announces nothing — no duplicate completion
  events, ever.
* **Announce** — after the terminal row write (the trt.25 row-before-event
  discipline), ``TaskProgress`` (on claim) and ``TaskCompleted`` (on settle)
  go out on BOTH ``johnny.session.<id>`` (live UI / WS passthrough) and
  ``johnny.tasks.<id>`` (the Phase-5 in-session listener, Johnny-trt.28).
  Events are best-effort; the row is the record.
* **Crash safety** — :func:`sweep_stale_tasks` requeues ``running`` rows
  whose ``updated_at`` went stale past the TTL (attempts cap settles them
  ``failed`` honestly with a ``TaskCompleted``), and clears stranded
  session-local rows (internal kinds from a crashed session) to
  ``cancelled`` — which announces nothing, per the trt.25 contract.

Dispatch latency is not bound to the poll interval: :class:`TaskWorker`
subscribes ``johnny.tasks.wake`` and claims immediately on a ping, with the
poll as the fallback for missed pings and pre-crash backlog.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

import sqlalchemy as sa

from app.db.models import AgentTask, AgentTaskStatus
from johnny.agent.internal_tools import INTERNAL_TOOL_KINDS
from johnny.agent.task_wiring import TASKS_CHANNEL_PREFIX, TASKS_WAKE_CHANNEL
from johnny.agent.tasks import (
    EXECUTOR_RESULT_STATUSES,
    QueuedTask,
    TaskExecutor,
    TaskResult,
    TaskSpec,
    executor_error_text,
    stub_executor,
)
from johnny.mcp.config import McpServerConfig, is_mcp_kind
from johnny.voice_pipeline.event_bus import DEFAULT_CHANNEL_PREFIX, RedisEventBus
from johnny.voice_pipeline.events import PolicyDenied, TaskCompleted, TaskProgress

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

DEFAULT_TASK_POLL_INTERVAL_S = 5.0
DEFAULT_TASK_CONCURRENCY = 4
DEFAULT_TASK_RUNNING_TTL_S = 300.0
DEFAULT_TASK_MAX_ATTEMPTS = 3
DEFAULT_TASK_EXEC_TIMEOUT_S = 240.0
DEFAULT_TASK_SWEEP_INTERVAL_S = 30.0
DEFAULT_TASK_REGISTRY_TTL_S = 60.0

_WAKE_RECONNECT_BACKOFF_S = 2.0


def _env_float(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("ignoring invalid %s=%r; using default %s", name, raw, default)
        return default
    return max(minimum, value)


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("ignoring invalid %s=%r; using default %s", name, raw, default)
        return default
    return max(minimum, value)


def task_executor_enabled() -> bool:
    """Operational escape hatch: ``JOHNNY_TASK_EXECUTOR_ENABLED=false`` keeps
    the worker process up without the executor pass (rows then sit queued)."""
    raw = os.environ.get("JOHNNY_TASK_EXECUTOR_ENABLED", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def get_task_poll_interval_seconds() -> float:
    return _env_float(
        "JOHNNY_TASK_POLL_INTERVAL_SECONDS", DEFAULT_TASK_POLL_INTERVAL_S, minimum=0.5
    )


def get_task_concurrency() -> int:
    return _env_int("JOHNNY_TASK_CONCURRENCY", DEFAULT_TASK_CONCURRENCY, minimum=1)


def get_task_running_ttl_seconds() -> float:
    return _env_float(
        "JOHNNY_TASK_RUNNING_TTL_SECONDS", DEFAULT_TASK_RUNNING_TTL_S, minimum=10.0
    )


def get_task_max_attempts() -> int:
    return _env_int("JOHNNY_TASK_MAX_ATTEMPTS", DEFAULT_TASK_MAX_ATTEMPTS, minimum=1)


def get_task_exec_timeout_seconds() -> float:
    """Per-task hard timeout. Clamped under the requeue TTL so a stale
    ``running`` row always means a dead worker, never a slow-but-alive one."""
    ttl = get_task_running_ttl_seconds()
    timeout = _env_float(
        "JOHNNY_TASK_EXEC_TIMEOUT_SECONDS", DEFAULT_TASK_EXEC_TIMEOUT_S, minimum=5.0
    )
    if timeout >= ttl:
        clamped = max(5.0, ttl * 0.8)
        logger.warning(
            "JOHNNY_TASK_EXEC_TIMEOUT_SECONDS=%s >= running TTL %s — clamping to %s",
            timeout,
            ttl,
            clamped,
        )
        return clamped
    return timeout


def get_task_sweep_interval_seconds() -> float:
    return _env_float(
        "JOHNNY_TASK_SWEEP_INTERVAL_SECONDS", DEFAULT_TASK_SWEEP_INTERVAL_S, minimum=1.0
    )


def get_task_registry_ttl_seconds() -> float:
    return _env_float(
        "JOHNNY_TASK_REGISTRY_TTL_SECONDS", DEFAULT_TASK_REGISTRY_TTL_S, minimum=0.0
    )


def _utcnow(now: datetime | None = None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def _clock_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class ClaimedTask:
    """One row this worker just moved ``queued`` → ``running``.

    ``attempts`` is the post-claim value — the fence
    :func:`settle_claimed_task` requires, so a straggling runner from an
    older attempt can never clobber a newer claim's row.

    ``workspace_id`` / ``workspace_is_default`` (Johnny-wks.1) come from the
    row's ``request_json["workspace"]`` stamp — the session's frozen
    workspace identity. Rows with no stamp (legacy, default-workspace
    sessions) carry ``None``/``True``: the default workspace, byte-identical
    pre-workspaces behavior.
    """

    task_id: int
    bot_session_id: int
    kind: str
    args: dict[str, Any]
    ack_text: str
    turn_id: int | None
    decision_id: int | None
    attempts: int
    workspace_id: int | None = None
    workspace_is_default: bool = True

    def as_queued_task(self) -> QueuedTask:
        """The executor-facing shape (the trt.23 runner contract)."""
        return QueuedTask(
            task_id=self.task_id,
            spec=TaskSpec(
                kind=self.kind,
                args=dict(self.args),
                ack_text=self.ack_text,
                turn_id=self.turn_id,
                decision_id=self.decision_id,
            ),
        )


@dataclass(frozen=True, slots=True)
class SweptFailedTask:
    """A stale row the sweep settled ``failed`` (attempts cap) — event input."""

    task_id: int
    bot_session_id: int
    kind: str
    turn_id: int | None
    result_text: str
    error: str


@dataclass(frozen=True, slots=True)
class SweepResult:
    requeued: tuple[int, ...] = ()
    failed: tuple[SweptFailedTask, ...] = ()
    cancelled: tuple[int, ...] = ()


def _kind_filters(
    only_kinds: frozenset[str] | None, exclude_kinds: frozenset[str]
) -> list[Any]:
    clauses: list[Any] = []
    if only_kinds is not None:
        clauses.append(AgentTask.kind.in_(sorted(only_kinds)))
    if exclude_kinds:
        clauses.append(AgentTask.kind.notin_(sorted(exclude_kinds)))
    return clauses


def claim_queued_tasks(
    db: Session,
    *,
    limit: int,
    exclude_kinds: frozenset[str] = INTERNAL_TOOL_KINDS,
    only_kinds: frozenset[str] | None = None,
    now: datetime | None = None,
) -> list[ClaimedTask]:
    """Atomically claim up to ``limit`` queued rows for this worker.

    Two statements in the caller's transaction: a ``FOR UPDATE SKIP LOCKED``
    id-select (concurrent claimers skip each other's locked rows instead of
    blocking — SQLite ignores the locking clause, which is fine for
    single-threaded unit tests; the race guarantee is integration-proven on
    Postgres) and the claim UPDATE re-checking ``status='queued'``.
    ``attempts`` increments here — claim *is* the attempt. The caller
    commits; rows are not claimed until it does.

    ``exclude_kinds`` defaults to the internal tools (the Johnny-trt.57
    locality guard at the SQL level — the worker must never even claim
    them). ``only_kinds`` scopes a non-production claimer (integration tests
    sharing the dev stack's table) to its own rows.
    """
    if limit <= 0:
        return []
    id_select = (
        sa.select(AgentTask.id)
        .where(
            AgentTask.status == AgentTaskStatus.QUEUED,
            *_kind_filters(only_kinds, exclude_kinds),
        )
        .order_by(AgentTask.created_at, AgentTask.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    ids = list(db.scalars(id_select))
    if not ids:
        return []
    rows = db.execute(
        sa.update(AgentTask)
        .where(AgentTask.id.in_(ids), AgentTask.status == AgentTaskStatus.QUEUED)
        .values(
            status=AgentTaskStatus.RUNNING,
            attempts=AgentTask.attempts + 1,
            updated_at=_utcnow(now),
        )
        .returning(
            AgentTask.id,
            AgentTask.bot_session_id,
            AgentTask.kind,
            AgentTask.request_json,
            AgentTask.turn_id,
            AgentTask.agent_decision_id,
            AgentTask.attempts,
            AgentTask.ack_text,
        )
        .execution_options(synchronize_session=False)
    ).all()
    claimed = []
    for row in rows:
        request = row.request_json if isinstance(row.request_json, dict) else {}
        args = request.get("args")
        workspace_id, workspace_is_default = _workspace_from_request(request)
        claimed.append(
            ClaimedTask(
                task_id=int(row.id),
                bot_session_id=int(row.bot_session_id),
                kind=str(row.kind),
                args=dict(args) if isinstance(args, dict) else {},
                ack_text=str(row.ack_text or ""),
                turn_id=row.turn_id,
                decision_id=row.agent_decision_id,
                attempts=int(row.attempts),
                workspace_id=workspace_id,
                workspace_is_default=workspace_is_default,
            )
        )
    return claimed


def _workspace_from_request(request: dict[str, Any]) -> tuple[int | None, bool]:
    """Parse the row's workspace stamp (Johnny-wks.1) → ``(id, is_default)``.

    Lenient like the rest of the claim parse: a missing / malformed stamp
    degrades to ``(None, True)`` — the default workspace, exactly what every
    pre-workspaces row meant.
    """
    entry = request.get("workspace")
    if not isinstance(entry, dict):
        return None, True
    raw_id = entry.get("id")
    try:
        workspace_id = int(raw_id) if raw_id is not None and raw_id != "" else None
    except (TypeError, ValueError):
        workspace_id = None
    if workspace_id is None:
        return None, True
    return workspace_id, bool(entry.get("is_default"))


def settle_claimed_task(
    db: Session,
    *,
    task_id: int,
    claim_attempts: int,
    status: Literal["done", "failed"],
    result_text: str,
    result_json: dict[str, Any] | None = None,
    error: str = "",
    now: datetime | None = None,
) -> bool:
    """Write the terminal status — fenced on ``status='running'`` AND the
    claim's ``attempts`` value.

    Returns ``False`` when the row no longer belongs to this claim (the TTL
    sweep requeued it and someone else re-claimed, bumping ``attempts``) —
    the caller must then discard the result and publish **nothing**, which is
    exactly the no-duplicate-completion-events acceptance. The caller
    commits.
    """
    result = db.execute(
        sa.update(AgentTask)
        .where(
            AgentTask.id == task_id,
            AgentTask.status == AgentTaskStatus.RUNNING,
            AgentTask.attempts == claim_attempts,
        )
        .values(
            status=AgentTaskStatus.DONE if status == "done" else AgentTaskStatus.FAILED,
            result_text=result_text or None,
            result_json=result_json,
            error=error or None,
            updated_at=_utcnow(now),
        )
        .execution_options(synchronize_session=False)
    )
    # Session.execute on an UPDATE is a CursorResult at runtime; the base
    # Result annotation just doesn't carry rowcount.
    return bool(getattr(result, "rowcount", 0) == 1)


def sweep_stale_tasks(
    db: Session,
    *,
    ttl_s: float,
    max_attempts: int,
    internal_kinds: frozenset[str] = INTERNAL_TOOL_KINDS,
    only_kinds: frozenset[str] | None = None,
    now: datetime | None = None,
) -> SweepResult:
    """Recover rows stranded by a crash — the TTL requeue (crash-safety leg).

    ``running`` rows whose ``updated_at`` went stale past ``ttl_s`` (claim
    stamps it; the per-task execution timeout sits below the TTL, so stale
    means the claimer died):

    * internal kinds → ``cancelled`` (their session is gone and the worker
      may never run them; ``cancelled`` announces nothing, trt.25 contract);
    * ``attempts >= max_attempts`` → ``failed`` with honest speech-ready
      text, returned so the caller announces ``TaskCompleted`` after commit;
    * otherwise → back to ``queued`` for the next claim (which re-increments
      ``attempts``).

    Stale ``queued`` internal-kind rows (a session crashed between
    row-insert and its in-process resolver) are cleared to ``cancelled`` the
    same way. The candidate select is ``FOR UPDATE SKIP LOCKED`` so
    concurrent sweeps cannot double-settle (and double-announce) a row. The
    caller commits.
    """
    cutoff = _utcnow(now) - timedelta(seconds=ttl_s)
    stale_running = AgentTask.status == AgentTaskStatus.RUNNING
    stranded_internal = (
        sa.and_(
            AgentTask.status == AgentTaskStatus.QUEUED,
            AgentTask.kind.in_(sorted(internal_kinds)),
        )
        if internal_kinds
        else sa.false()
    )
    stmt = (
        sa.select(AgentTask)
        .where(sa.or_(stale_running, stranded_internal), AgentTask.updated_at < cutoff)
        .with_for_update(skip_locked=True)
    )
    if only_kinds is not None:
        stmt = stmt.where(AgentTask.kind.in_(sorted(only_kinds)))
    requeued: list[int] = []
    failed: list[SweptFailedTask] = []
    cancelled: list[int] = []
    for row in db.scalars(stmt):
        if row.kind in internal_kinds:
            prior = row.status.value
            row.status = AgentTaskStatus.CANCELLED
            row.result_text = (
                f"The {row.kind} task didn't finish before its session went away."
            )
            row.error = (
                f"stale {prior} session-local row swept to cancelled "
                f"after {ttl_s:.0f}s (Johnny-trt.24)"
            )
            cancelled.append(int(row.id))
            continue
        if row.attempts >= max_attempts:
            result_text = (
                f"I couldn't finish the {row.kind} task — it kept getting "
                "interrupted, so I gave up."
            )
            error = (
                f"requeue TTL exceeded after {row.attempts} attempts "
                f"(max {max_attempts}); settling failed (Johnny-trt.24)"
            )
            row.status = AgentTaskStatus.FAILED
            row.result_text = result_text
            row.error = error
            failed.append(
                SweptFailedTask(
                    task_id=int(row.id),
                    bot_session_id=int(row.bot_session_id),
                    kind=row.kind,
                    turn_id=row.turn_id,
                    result_text=result_text,
                    error=error,
                )
            )
            continue
        row.status = AgentTaskStatus.QUEUED
        row.updated_at = _utcnow(now)
        requeued.append(int(row.id))
    return SweepResult(
        requeued=tuple(requeued), failed=tuple(failed), cancelled=tuple(cancelled)
    )


def resolve_sandbox_url(claimed: ClaimedTask) -> str:
    """Which sandbox runs this task's CLI work — keyed by WORKSPACE (Johnny-wks.1).

    Keyed by the claimed row's workspace stamp: the DEFAULT workspace — and
    every legacy row with no stamp — gets the global skills-sandbox from
    ``JOHNNY_SKILLS_SANDBOX_URL`` (byte-identical pre-workspaces behavior);
    a non-default workspace gets its own container's canonical endpoint.
    Until that container exists (Johnny-wks.2), the registry probe against
    it degrades to all-skills-unavailable and the task settles ``failed``
    with honest speech — never a crash. The claim/run/settle loop never
    needed to change (the Phase-7 promise of this seam). The
    session-assembly twin is
    :func:`johnny.agent.job_session.resolve_session_sandbox_url`
    (Johnny-trt.63) — re-keying sandbox identity means changing exactly
    these two functions.
    """
    from johnny.skills.sandbox import sandbox_url_for_workspace, sandbox_url_from_env

    if claimed.workspace_id is None or claimed.workspace_is_default:
        return sandbox_url_from_env()
    return sandbox_url_for_workspace(claimed.workspace_id)


def load_mcp_server_configs() -> tuple[McpServerConfig, ...]:
    """Fresh enabled MCP server configs (Johnny-trt.36), read per execution.

    The MCP twin of the per-claim policy resolution: never cached, so an
    operator's enable/disable/filter edit bites the very next claimed task
    without a worker restart. Raises on a failed read — the executor's
    config leg settles the task with could-not-verify speech (fail closed),
    mirroring :meth:`TaskWorker._resolve_policy`.
    """
    from app.db.session import SessionLocal
    from app.security.crypto import get_crypto
    from app.services.mcp_servers import load_server_configs

    db = SessionLocal()
    try:
        configs = load_server_configs(db, get_crypto())
        db.commit()
        return configs
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@dataclass(slots=True)
class _ExecutorEntry:
    registry: Any
    client: Any
    loaded_at: float


class SandboxExecutorProvider:
    """Per-sandbox executor resolution with a TTL'd registry snapshot.

    The worker mirrors a session assembly per sandbox URL: one volume scan +
    batched sandbox probes building a :class:`SkillRegistry`, then the
    trt.23 deterministic runner over it. The snapshot refreshes after
    ``registry_ttl_s`` — and immediately (once per claim) when the claimed
    kind is missing or unavailable in the cached snapshot: the session's
    catalog said yes, so a disagreeing worker snapshot is presumed stale
    before the task is failed. Claim-time availability revalidation
    (Johnny-trt.55) stays inside the runner itself.

    The CACHE holds (client, registry) per URL; the runner + exec tool are
    rebuilt per task (cheap closures) so the freshly-resolved capability
    policy (Johnny-trt.38) shapes each task's exec-bin allow set — the
    operator-edited safe-bins baseline in, policy-denied bins and denied
    skills' grants out — with denials attributed to the denying layer.

    Model resolution for future multi-step kinds will live behind one
    function here, next to :func:`resolve_sandbox_url`: each queued row's
    ``request_json["reasoning_llm"]`` already stamps the requesting agent's
    resolved reasoning provider (``{provider_id, provider_name, display_name,
    model}``, Johnny-trt.42 — identity only, credentials re-read from the
    DB), so the resolver reads the stamp and falls back to the global active
    LLM when absent.
    """

    def __init__(
        self,
        *,
        registry_ttl_s: float | None = None,
        mcp_manager: Any | None = None,
        mcp_config_loader: Callable[[], tuple[McpServerConfig, ...]] | None = None,
    ) -> None:
        self._registry_ttl_s = (
            registry_ttl_s if registry_ttl_s is not None else get_task_registry_ttl_seconds()
        )
        self._entries: dict[str, _ExecutorEntry] = {}
        self._lock = asyncio.Lock()
        # MCP connector (Johnny-trt.36): connections live HERE — one manager
        # per worker process, lazily created on the first mcp__ claim so the
        # SDK import never taxes a worker that has no servers configured.
        # Both are injection seams for tests.
        self._mcp_manager = mcp_manager
        self._mcp_config_loader = (
            mcp_config_loader if mcp_config_loader is not None else load_mcp_server_configs
        )

    def _mcp_manager_lazy(self) -> Any:
        if self._mcp_manager is None:
            from johnny.mcp.client import McpClientManager

            self._mcp_manager = McpClientManager()
        return self._mcp_manager

    async def executor_for(
        self, claimed: ClaimedTask, *, policy: Any | None = None
    ) -> TaskExecutor:
        from johnny.mcp.executor import build_mcp_task_executor
        from johnny.skills.executor import build_skill_task_executor
        from johnny.skills.policy import ExecBinPolicy, compute_allowed_bins
        from johnny.skills.tools import SandboxExecTool

        url = resolve_sandbox_url(claimed)
        async with self._lock:
            entry = self._entries.get(url)
            age = time.monotonic() - entry.loaded_at if entry is not None else None
            if entry is None or age is None or age >= self._registry_ttl_s:
                entry = await self._load(url, reuse=entry)
            elif not self._kind_ready(entry, claimed.kind):
                logger.info(
                    "task worker: kind=%s not available in the cached snapshot "
                    "(age %.0fs) — refreshing registry for %s",
                    claimed.kind,
                    age,
                    url,
                )
                entry = await self._load(url, reuse=entry)
            registry = entry.registry
            client = entry.client
        if policy is not None:
            # Johnny-trt.38 enforcement point #3 (sandbox.exec argv[0]): the
            # edited safe-bins baseline replaces BASELINE_BINS, denied
            # skills' requires.bins grants never enter the union, and the
            # policy filter drops bins_deny matches + removed baseline bins.
            allowed = compute_allowed_bins(
                tuple(
                    skill.document.requires
                    for skill in registry.eligible()
                    if policy.check_tool(skill.name).allowed
                ),
                policy=policy,
            )
            bin_policy = ExecBinPolicy(allowed=allowed, policy_check=policy.check_bin)
        else:
            bin_policy = ExecBinPolicy(allowed=registry.allowed_bins)
        exec_tool = SandboxExecTool(client, policy=bin_policy)
        # Resolution chain (Johnny-trt.24): internal guard → skills → mcp →
        # stub. The MCP leg is the skill runner's fallback: configs re-read
        # fresh per execution (the no-restart pattern), connections lazy +
        # cached on the manager, stdio servers spawned in THIS task's
        # resolved sandbox (the Phase-7 per-agent seam rides ``url``).
        mcp_executor = build_mcp_task_executor(
            self._mcp_manager_lazy(),
            load_servers=self._mcp_config_loader,
            sandbox_url=url,
            fallback=stub_executor,
        )
        return build_skill_task_executor(registry, exec_tool, fallback=mcp_executor)

    def _kind_ready(self, entry: _ExecutorEntry, kind: str) -> bool:
        if is_mcp_kind(kind):
            # MCP kinds never live in the skill registry — refreshing it for
            # them would force a full volume scan + sandbox probe per claim.
            return True
        skill = entry.registry.get(kind)
        return skill is not None and bool(skill.eligible) and bool(skill.available)

    async def sweep_mcp_idle(self) -> None:
        """Evict MCP connections idle past their TTL (the worker's sweep hook)."""
        if self._mcp_manager is None:
            return
        try:
            await self._mcp_manager.sweep_idle()
        except Exception:  # noqa: BLE001 — the sweep must never kill the pass
            logger.exception("task worker: mcp idle sweep failed")

    async def _load(self, url: str, *, reuse: _ExecutorEntry | None) -> _ExecutorEntry:
        from johnny.skills.registry import (
            build_sandbox_availability_runner,
            load_skill_registry,
        )
        from johnny.skills.sandbox import SandboxClient, skills_dir_from_env

        client = reuse.client if reuse is not None else SandboxClient(base_url=url)
        registry = await load_skill_registry(
            skills_dir_from_env(),
            check_bins=client.check_bins,
            check_env=client.check_env,
            run_check=build_sandbox_availability_runner(client),
        )
        entry = _ExecutorEntry(registry=registry, client=client, loaded_at=time.monotonic())
        self._entries[url] = entry
        logger.info("task worker: skill registry loaded for %s (%s)", url, registry.summary())
        return entry

    async def aclose(self) -> None:
        for entry in self._entries.values():
            try:
                await entry.client.aclose()
            except Exception:  # noqa: BLE001 — teardown is best-effort
                logger.exception("task worker: sandbox client close failed")
        self._entries.clear()
        if self._mcp_manager is not None:
            try:
                await self._mcp_manager.aclose()
            except Exception:  # noqa: BLE001 — teardown is best-effort
                logger.exception("task worker: mcp manager close failed")


class TaskWorker:
    """The persistent executor pass: wake-subscribed claim → run → settle loop.

    Runs on its own event loop (the worker process gives it a dedicated
    daemon thread, like the session-status subscriber) so a slow tool can
    never delay the heartbeat / calendar-poll / scheduler passes; within the
    loop the semaphore bounds concurrent executions and claims never exceed
    free capacity, so ``running`` always means *actually running*.
    """

    def __init__(
        self,
        *,
        redis_url: str | None,
        session_factory: Callable[[], Session] | None = None,
        executor: TaskExecutor | None = None,
        poll_interval_s: float | None = None,
        concurrency: int | None = None,
        running_ttl_s: float | None = None,
        max_attempts: int | None = None,
        exec_timeout_s: float | None = None,
        sweep_interval_s: float | None = None,
        exclude_kinds: frozenset[str] = INTERNAL_TOOL_KINDS,
        only_kinds: frozenset[str] | None = None,
        clock_ms: Callable[[], int] = _clock_ms,
    ) -> None:
        self._redis_url = redis_url
        self._session_factory = session_factory
        # Injected single executor = test seam; None = the production
        # sandbox-backed chain resolved per task (the Phase-7 seam).
        self._executor = executor
        self._provider = SandboxExecutorProvider() if executor is None else None
        self._poll_interval_s = (
            poll_interval_s if poll_interval_s is not None else get_task_poll_interval_seconds()
        )
        self._concurrency = concurrency if concurrency is not None else get_task_concurrency()
        self._running_ttl_s = (
            running_ttl_s if running_ttl_s is not None else get_task_running_ttl_seconds()
        )
        self._max_attempts = (
            max_attempts if max_attempts is not None else get_task_max_attempts()
        )
        self._exec_timeout_s = (
            exec_timeout_s if exec_timeout_s is not None else get_task_exec_timeout_seconds()
        )
        self._sweep_interval_s = (
            sweep_interval_s
            if sweep_interval_s is not None
            else get_task_sweep_interval_seconds()
        )
        self._exclude_kinds = exclude_kinds
        self._only_kinds = only_kinds
        self._clock_ms = clock_ms
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._semaphore = asyncio.Semaphore(self._concurrency)
        self._inflight: set[asyncio.Task[None]] = set()
        self._redis_client: Any | None = None
        self._buses: tuple[RedisEventBus, ...] | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def request_stop(self) -> None:
        """Ask the loop to wind down (tests / graceful shutdown)."""
        self._stop.set()
        self._wake.set()

    async def run(self) -> None:
        """The pass itself. Returns only after :meth:`request_stop`."""
        listener: asyncio.Task[None] | None = None
        if self._redis_url:
            listener = asyncio.ensure_future(self._wake_listener())
        next_sweep = 0.0  # immediate first sweep: recover pre-crash backlog at boot
        logger.info(
            "task worker: executor pass starting (concurrency=%d poll=%.1fs "
            "ttl=%.0fs max_attempts=%d exec_timeout=%.0fs)",
            self._concurrency,
            self._poll_interval_s,
            self._running_ttl_s,
            self._max_attempts,
            self._exec_timeout_s,
        )
        try:
            while not self._stop.is_set():
                now = asyncio.get_running_loop().time()
                if now >= next_sweep:
                    await self._sweep_once()
                    next_sweep = now + self._sweep_interval_s
                # Clear BEFORE claiming: a wake landing during the claim
                # re-sets the event and the wait below returns immediately.
                self._wake.clear()
                if await self._claim_once():
                    continue  # drain the queue while capacity remains
                timeout = min(
                    self._poll_interval_s,
                    max(0.1, next_sweep - asyncio.get_running_loop().time()),
                )
                await self._wait_for_nudge(timeout)
        finally:
            if listener is not None:
                listener.cancel()
                try:
                    await listener
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            await self._drain_inflight()
            if self._provider is not None:
                await self._provider.aclose()
            await self._close_redis()
            logger.info("task worker: executor pass stopped")

    async def _drain_inflight(self) -> None:
        """Brief grace for in-flight runners, then cancel (graceful stop).

        A cancelled runner leaves its row ``running`` — the TTL sweep
        requeues it after restart, which is the documented crash model.
        """
        pending = [task for task in self._inflight if not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=5.0)
        for task in list(self._inflight):
            task.cancel()
        for task in list(self._inflight):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _wait_for_nudge(self, timeout: float) -> None:
        waiters = [
            asyncio.ensure_future(self._wake.wait()),
            asyncio.ensure_future(self._stop.wait()),
        ]
        try:
            await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
                try:
                    await waiter
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------ #
    # DB access (sync SQLAlchemy, the codebase's worker-pass style)       #
    # ------------------------------------------------------------------ #

    @contextmanager
    def _scoped_db(self) -> Iterator[Session]:
        factory = self._session_factory
        if factory is None:
            from app.db.session import SessionLocal

            factory = SessionLocal
            self._session_factory = factory
        db = factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # ------------------------------------------------------------------ #
    # Claim / run / settle                                                #
    # ------------------------------------------------------------------ #

    async def _claim_once(self) -> bool:
        capacity = self._concurrency - len(self._inflight)
        if capacity <= 0:
            return False
        try:
            with self._scoped_db() as db:
                claimed = claim_queued_tasks(
                    db,
                    limit=capacity,
                    exclude_kinds=self._exclude_kinds,
                    only_kinds=self._only_kinds,
                )
        except Exception:
            logger.exception("task worker: claim pass failed")
            return False
        for task in claimed:
            logger.info(
                "task worker: claimed task_id=%s kind=%s attempt=%d (session %s)",
                task.task_id,
                task.kind,
                task.attempts,
                task.bot_session_id,
            )
            # Row-before-event: the claim committed above.
            await self._publish_progress(task)
            runner = asyncio.ensure_future(self._run_claimed(task))
            self._inflight.add(runner)
            runner.add_done_callback(self._on_runner_done)
        return bool(claimed)

    def _on_runner_done(self, task: asyncio.Task[None]) -> None:
        self._inflight.discard(task)
        # A freed slot is a claim opportunity — don't wait out the poll.
        self._wake.set()

    def _resolve_policy(self, claimed: ClaimedTask) -> Any:
        """Resolve the capability policy FRESH from the DB (Johnny-trt.38).

        Per claimed task, never cached: this is the no-restart enforcement —
        a policy edit bites a running session's very next delegation. Raises
        on a failed read; the caller settles the task with the trt.55
        could-not-verify stance (fail closed — a guardrail that silently
        vanishes on a DB hiccup is not a guardrail).
        """
        from app.services.capability_policies import resolve_policy_for_bot_session

        with self._scoped_db() as db:
            return resolve_policy_for_bot_session(db, claimed.bot_session_id)

    async def _run_claimed(self, claimed: ClaimedTask) -> None:
        async with self._semaphore:
            # Johnny-trt.38 enforcement point #2 (executor tool dispatch):
            # check the claimed kind against the freshly-resolved policy
            # BEFORE any executor work; a denial settles failed with the
            # spoken-form reason and emits policy_denied naming the layer.
            policy_ctx: Any | None = None
            try:
                policy_ctx = self._resolve_policy(claimed)
            except Exception:
                logger.exception(
                    "task worker: capability-policy resolution failed for "
                    "task_id=%s — failing closed (could-not-verify)",
                    claimed.task_id,
                )
                await self._settle(
                    claimed,
                    "failed",
                    TaskResult(
                        status="failed",
                        result_text=(
                            f"I couldn't verify the {claimed.kind} task is allowed "
                            "right now, so I didn't start it."
                        ),
                        error="capability-policy resolution failed (Johnny-trt.38)",
                    ),
                )
                return
            decision = policy_ctx.policy.check_tool(claimed.kind)
            if not decision.allowed:
                logger.warning(
                    "task worker: task_id=%s kind=%s DENIED by capability policy "
                    "(layer=%s rule=%r) — settling failed (Johnny-trt.38)",
                    claimed.task_id,
                    claimed.kind,
                    decision.layer,
                    decision.rule,
                )
                await self._settle(
                    claimed,
                    "failed",
                    TaskResult(
                        status="failed",
                        result_text=(
                            f"I'm not allowed to run the {claimed.kind} task — "
                            "my operator's policy has it switched off for this session."
                        ),
                        error=(
                            f"capability policy denied kind {claimed.kind!r} at the "
                            f"{decision.layer} layer (rule {decision.rule or 'allow-list'!r})"
                        ),
                    ),
                )
                await self._publish_policy_denied(
                    claimed,
                    capability=claimed.kind,
                    capability_kind="tool",
                    layer=decision.layer,
                    rule=decision.rule,
                    layer_detail=decision.detail,
                    surface="worker",
                    timestamp_ms=policy_ctx.session_relative_ms,
                )
                return
            try:
                executor = (
                    self._executor
                    if self._executor is not None
                    else await self._provider.executor_for(  # type: ignore[union-attr]
                        claimed, policy=policy_ctx.policy
                    )
                )
                result = await asyncio.wait_for(
                    executor(claimed.as_queued_task()), timeout=self._exec_timeout_s
                )
            except asyncio.CancelledError:
                # Worker teardown mid-task: leave the row running — the TTL
                # sweep requeues it with an attempts increment (crash model).
                raise
            except TimeoutError:
                result = TaskResult(
                    status="failed",
                    result_text=f"The {claimed.kind} task took too long, so I stopped it.",
                    error=f"worker execution timeout after {self._exec_timeout_s:.0f}s",
                )
            except Exception as exc:
                logger.exception(
                    "task worker: executor errored for task_id=%s kind=%s",
                    claimed.task_id,
                    claimed.kind,
                )
                result = TaskResult(
                    status="failed",
                    result_text=executor_error_text(claimed.kind),
                    error=f"executor error: {type(exc).__name__}: {exc}",
                )
            if result.status in EXECUTOR_RESULT_STATUSES:
                status: Literal["done", "failed"] = result.status
            else:  # defensive: executors may only settle done/failed
                logger.error(
                    "task worker: executor returned illegal status %r for "
                    "task_id=%s — recording failed",
                    result.status,
                    claimed.task_id,
                )
                status = "failed"
            await self._settle(claimed, status, result, policy_ctx=policy_ctx)

    async def _settle(
        self,
        claimed: ClaimedTask,
        status: Literal["done", "failed"],
        result: TaskResult,
        *,
        policy_ctx: Any | None = None,
    ) -> None:
        try:
            with self._scoped_db() as db:
                settled = settle_claimed_task(
                    db,
                    task_id=claimed.task_id,
                    claim_attempts=claimed.attempts,
                    status=status,
                    result_text=result.result_text,
                    result_json=result.result_json,
                    error=result.error,
                )
        except Exception:
            logger.exception(
                "task worker: terminal write failed for task_id=%s — the TTL "
                "sweep will requeue it",
                claimed.task_id,
            )
            return
        if not settled:
            logger.info(
                "task worker: task_id=%s (attempt %d) was requeued/re-claimed while "
                "running — discarding this result, announcing nothing",
                claimed.task_id,
                claimed.attempts,
            )
            return
        logger.info(
            "task worker: task_id=%s kind=%s settled %s (attempt %d)",
            claimed.task_id,
            claimed.kind,
            status,
            claimed.attempts,
        )
        await self._publish_completed(
            task_id=claimed.task_id,
            bot_session_id=claimed.bot_session_id,
            kind=claimed.kind,
            status=status,
            result_text=result.result_text,
            error=result.error,
            turn_id=claimed.turn_id,
        )
        # Johnny-trt.38 enforcement point #3 surfaced: the run hit a
        # policy-blocked binary inside sandbox.exec (attribution rode the
        # outcome into result_json) — announce it after the row settle, the
        # trt.25 row-before-event discipline.
        policy_denied = (
            result.result_json.get("policy_denied")
            if isinstance(result.result_json, dict)
            else None
        )
        if isinstance(policy_denied, dict):
            await self._publish_policy_denied(
                claimed,
                capability=str(policy_denied.get("bin") or ""),
                capability_kind="bin",
                layer=str(policy_denied.get("layer") or ""),
                rule=str(policy_denied.get("rule") or ""),
                layer_detail=str(policy_denied.get("detail") or ""),
                surface="sandbox_exec",
                timestamp_ms=(
                    policy_ctx.session_relative_ms if policy_ctx is not None else 0
                ),
            )

    # ------------------------------------------------------------------ #
    # TTL sweep                                                           #
    # ------------------------------------------------------------------ #

    async def _sweep_once(self) -> None:
        # MCP idle eviction rides the same cadence (Johnny-trt.36): a
        # connection unused past its server's idle_ttl_s closes here and
        # transparently reconnects on the next claimed mcp__ kind.
        if self._provider is not None:
            await self._provider.sweep_mcp_idle()
        try:
            with self._scoped_db() as db:
                swept = sweep_stale_tasks(
                    db,
                    ttl_s=self._running_ttl_s,
                    max_attempts=self._max_attempts,
                    only_kinds=self._only_kinds,
                )
        except Exception:
            logger.exception("task worker: stale-task sweep failed")
            return
        if swept.requeued or swept.failed or swept.cancelled:
            logger.info(
                "task worker: sweep requeued=%s failed=%s cancelled=%s",
                list(swept.requeued),
                [f.task_id for f in swept.failed],
                list(swept.cancelled),
            )
        # Row-before-event: the sweep committed above. Whoever settles the
        # row announces it (trt.25) — the attempts-cap settles are ours.
        for failed in swept.failed:
            await self._publish_completed(
                task_id=failed.task_id,
                bot_session_id=failed.bot_session_id,
                kind=failed.kind,
                status="failed",
                result_text=failed.result_text,
                error=failed.error,
                turn_id=failed.turn_id,
            )

    # ------------------------------------------------------------------ #
    # Redis: wake subscription + dual-channel event publishing            #
    # ------------------------------------------------------------------ #

    async def _wake_listener(self) -> None:
        """Subscribe ``johnny.tasks.wake`` so claims aren't poll-bound.

        The ``get_message(timeout=…)`` loop with a swallowed ``TimeoutError``
        is the status subscriber's proven read shape (a bare ``listen()``
        surfaces idle-socket read timeouts as crashes); it also gives the
        stop flag a 1 s check cadence. Self-healing: a dropped connection
        logs, backs off, and resubscribes — the poll interval covers the
        gap. The payload content is ignored; the durable queue is the table.
        """
        from redis.asyncio import Redis

        redis_url = self._redis_url
        if redis_url is None:  # run() only spawns the listener with a URL
            return
        while not self._stop.is_set():
            client: Any | None = None
            try:
                client = Redis.from_url(redis_url, decode_responses=False)
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                await pubsub.subscribe(TASKS_WAKE_CHANNEL)
                logger.info("task worker: subscribed to %s", TASKS_WAKE_CHANNEL)
                while not self._stop.is_set():
                    try:
                        message = await pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=1.0
                        )
                    except TimeoutError:
                        continue
                    if message is not None and message.get("type") == "message":
                        self._wake.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "task worker: wake subscription dropped — reconnecting in %.0fs",
                    _WAKE_RECONNECT_BACKOFF_S,
                )
                await asyncio.sleep(_WAKE_RECONNECT_BACKOFF_S)
            finally:
                if client is not None:
                    try:
                        await client.aclose()
                    except Exception:  # noqa: BLE001
                        pass

    def _event_buses(self) -> tuple[RedisEventBus, ...]:
        """Both announce surfaces, lazily: the UI session channel
        (``johnny.session.<id>``) and the agent task channel
        (``johnny.tasks.<id>``, Johnny-trt.28's subscription). One shared
        Redis client; the worker closes it once at teardown."""
        if self._buses is not None:
            return self._buses
        if not self._redis_url:
            self._buses = ()
            return self._buses
        from redis.asyncio import Redis

        self._redis_client = Redis.from_url(self._redis_url, decode_responses=False)
        self._buses = (
            RedisEventBus(self._redis_client, channel_prefix=DEFAULT_CHANNEL_PREFIX),
            RedisEventBus(self._redis_client, channel_prefix=TASKS_CHANNEL_PREFIX),
        )
        return self._buses

    async def _close_redis(self) -> None:
        if self._redis_client is None:
            return
        try:
            await self._redis_client.aclose()
        except Exception:  # noqa: BLE001
            logger.exception("task worker: redis close failed")
        self._redis_client = None
        self._buses = None

    async def _publish(self, event: Any) -> None:
        for bus in self._event_buses():
            try:
                await bus.publish(event)
            except Exception:  # noqa: BLE001 — events are best-effort; the row is the record
                logger.warning(
                    "task worker: event publish failed for %s task_id=%s",
                    getattr(event, "type", "?"),
                    getattr(event, "task_id", "?"),
                )

    async def _publish_progress(self, claimed: ClaimedTask) -> None:
        await self._publish(
            TaskProgress(
                task_id=claimed.task_id,
                kind=claimed.kind,
                timestamp_ms=self._clock_ms(),
                progress_text="",  # bare claim signal (the documented shape)
                turn_id=claimed.turn_id,
                session_id=str(claimed.bot_session_id),
            )
        )

    async def _publish_completed(
        self,
        *,
        task_id: int,
        bot_session_id: int,
        kind: str,
        status: Literal["done", "failed"],
        result_text: str,
        error: str,
        turn_id: int | None,
    ) -> None:
        await self._publish(
            TaskCompleted(
                task_id=task_id,
                kind=kind,
                status=status,
                timestamp_ms=self._clock_ms(),
                result_text=result_text,
                error=error,
                turn_id=turn_id,
                session_id=str(bot_session_id),
            )
        )

    async def _publish_policy_denied(
        self,
        claimed: ClaimedTask,
        *,
        capability: str,
        capability_kind: str,
        layer: str,
        rule: str,
        layer_detail: str,
        surface: str,
        timestamp_ms: int,
    ) -> None:
        """Announce one enforced policy denial (Johnny-trt.38).

        Published on the session channel like every worker event; the status
        subscriber persists it to ``conversation_events`` with the denying
        layer as the row's ``reason``. ``timestamp_ms`` is session-relative
        (the conversation-events time base), resolved alongside the policy.
        """
        await self._publish(
            PolicyDenied(
                capability=capability,
                capability_kind=capability_kind,
                layer=layer,
                rule=rule,
                layer_detail=layer_detail,
                surface=surface,
                timestamp_ms=max(0, timestamp_ms),
                turn_id=claimed.turn_id,
                session_id=str(claimed.bot_session_id),
            )
        )


async def run_task_executor_loop(
    *,
    redis_url: str | None,
    worker: TaskWorker | None = None,
) -> None:
    """Entry point for the worker process (and tests): build + run the pass."""
    if worker is None:
        worker = TaskWorker(redis_url=redis_url)
    await worker.run()


__all__ = [
    "DEFAULT_TASK_CONCURRENCY",
    "DEFAULT_TASK_EXEC_TIMEOUT_S",
    "DEFAULT_TASK_MAX_ATTEMPTS",
    "DEFAULT_TASK_POLL_INTERVAL_S",
    "DEFAULT_TASK_RUNNING_TTL_S",
    "DEFAULT_TASK_SWEEP_INTERVAL_S",
    "ClaimedTask",
    "SandboxExecutorProvider",
    "SweepResult",
    "SweptFailedTask",
    "TaskWorker",
    "claim_queued_tasks",
    "get_task_concurrency",
    "get_task_exec_timeout_seconds",
    "get_task_max_attempts",
    "get_task_poll_interval_seconds",
    "get_task_registry_ttl_seconds",
    "get_task_running_ttl_seconds",
    "get_task_sweep_interval_seconds",
    "resolve_sandbox_url",
    "run_task_executor_loop",
    "settle_claimed_task",
    "sweep_stale_tasks",
    "task_executor_enabled",
]
