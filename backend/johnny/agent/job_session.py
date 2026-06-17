"""Assemble one Meet session's ``AgentSession`` harness from its job payload (Johnny-9eh).

The agent worker (:mod:`johnny.agent.worker`) receives one Meet session's whole
configuration as a :class:`~johnny.agent.job_config.SessionJobConfig` in its LiveKit
job metadata. The translation seam (:mod:`johnny.agent.job_runtime`, Johnny-7we) turns
that payload into adapters + instructions + an :class:`AnswerConfig`; *this* module is
the next layer up — it wires every Phase-2 component the running session needs into a
single :class:`AgentRuntime`:

* the STT/LLM/TTS LiveKit adapters
  (:func:`~johnny.agent.job_runtime.build_session_adapters_for_job`);
* the session :class:`~johnny.agent.gate.TurnLedger` + :class:`~johnny.agent.gate.TurnIndex`
  (INV-1 authority + the durable str→int turn id, Johnny-o3z/d5z);
* the observability emit seams (:mod:`johnny.agent.observability`) publishing
  ``RouterDecisionMade`` / ``AgentSpoke`` / ``AgentSuggested`` / ``TranscriptFinalized`` /
  ``TurnTerminal`` / ``PipelineTiming`` to the Redis ``EventBus`` the existing subscriber
  persists (Johnny-d5z — emit-half parity, no new DB code);
* the router "should-speak" :class:`~johnny.agent.router_gate.RouterGate` (Johnny-xpa);
* the out-of-band barge-in classifier (:class:`~johnny.agent.barge_in.BargeInClassifier`,
  Johnny-k8t);
* the :class:`~johnny.agent.session.JohnnyAgent` with the noise gate, the answer-path
  nodes, transcript rehydration, and the metrics listener (Johnny-cmd/5ag/re2/d5z).

The one piece this module does **not** build is the
:class:`~livekit.agents.AgentSession` itself and the ``approval_required``
coordinator: both need a live LiveKit job context (the multilingual turn detector
and ``AgentSession.generate_reply``), so the worker constructs the session and — only
in ``approval_required`` mode — calls
:func:`~johnny.agent.approval_wiring.build_approval_coordinator` with the
:class:`AgentRuntime`'s ledger / gate / approval gate / decision sink afterwards. The
runtime carries those handles so the worker can finish that wiring without re-deriving
anything, and :meth:`AgentRuntime.aclose` drains the rest at teardown.

Requires the ``agent`` extra (it reaches the livekit-backed adapters + gate); imported
only by the agent worker / its tests, never from the import-safe top-level
:mod:`johnny.agent` package.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from app.providers.base import (
    LLMProvider,
    ProviderConfig,
    ProviderKind,
    get_registry,
)
from johnny.agent.adapters.factory import (
    AgentSessionSetupError,
    SessionAdapters,
    warm_up_session_providers,
)
from johnny.agent.answer import degrade_speaking_mode_if_no_tts
from johnny.agent.barge_in import BargeInClassifier, BargeInClassifierConfig
from johnny.agent.gate import TurnIndex, TurnLedger
from johnny.agent.internal_tools import (
    InternalToolContext,
    build_internal_task_executor,
    executor_known_kinds,
    internal_catalog_entries,
    merge_task_catalog,
)
from johnny.agent.job_config import (
    APPROVAL_REQUIRED_MODE,
    AUTONOMOUS_MODE,
    LIMITED_AUTO_SPEAK_MODE,
    PROVIDER_CONFIG_ROUTER_LLM_KEY,
    SessionJobConfig,
    reasoning_llm_from_provider_config,
    workspace_from_agent_snapshot,
)
from johnny.agent.job_runtime import (
    answer_config_from_job,
    build_session_adapters_for_job,
    instructions_config_from_job,
)
from johnny.agent.noise_filter import NoiseFilterConfig
from johnny.agent.observability import (
    AgentSpeechInterimForwarder,
    MetricsTranslator,
    build_decision_emitter,
    build_interruption_emitter,
    build_policy_denied_emitter,
    build_session_terminal_emitter,
    build_spoke_emitter,
    build_suggested_emitter,
    build_transcript_finalized_emitter,
    build_triage_timing_emitter,
)
from johnny.agent.router_gate import RouterGate, RouterGateConfig
from johnny.agent.sandbox_tools import (
    build_sandbox_tools,
    resolve_sandbox_policy,
    sandbox_full_access_enabled,
)
from johnny.agent.session import TOOL_USE_NOTES, JohnnyAgent, build_johnny_agent
from johnny.agent.speech_floor import (
    DEFAULT_CLAIM_WINDOW_MS,
    FloorBackend,
    RedisFloorBackend,
    SpeechFloor,
    session_relative_ms,
)
from johnny.agent.task_catalog import render_capability_notes
from johnny.agent.tasks import (
    QueuedTask,
    TaskCoordinator,
    TaskExecutor,
    TaskResult,
    stub_executor,
)
from johnny.mcp.catalog import McpServerSnapshot, mcp_catalog_entries, mcp_known_kinds
from johnny.skills.capability_policy import apply_policy_to_catalog
from johnny.voice_pipeline.audio_recorder import SpokenAudioRecorder, build_recorder_from_env
from johnny.voice_pipeline.event_bus import (
    DEFAULT_CHANNEL_PREFIX,
    EventBus,
    InMemoryEventBus,
    RedisEventBus,
)
from johnny.voice_pipeline.events import TranscriptFiltered

if TYPE_CHECKING:
    from collections.abc import Callable

    from livekit.agents.vad import VAD
    from sqlalchemy.orm import Session

    from app.providers.base import ProviderRegistry
    from johnny.skills.registry import SkillRegistry
    from johnny.skills.sandbox import SandboxClient
    from johnny.voice_pipeline.approval import ApprovalGate
    from johnny.voice_pipeline.transcript_history import TranscriptHistoryLoader

logger = logging.getLogger(__name__)


class _NativeToolInvoker:
    """Off-turn :class:`~johnny.agent.inline_promotion.ToolInvoker` over the
    session's native LiveKit ``function_tool``\\s (Johnny-d6w.24).

    The mid-loop continuation re-runs the exact tools the inline loop uses —
    sandbox ``exec``/``read``/``write``/``list_dir`` + the MCP gateway meta-tools
    — by calling the same callables directly (``FunctionTool.__call__`` forwards
    to the closure; Johnny's tools take no ``RunContext``, so they are reachable
    off a live turn). Keyed by the tool name the model emits (``tool.info.name``,
    the same name the schema builder advertises)."""

    def __init__(self, tools: list[Any]) -> None:
        self._by_name: dict[str, Any] = {}
        for tool in tools:
            name = getattr(getattr(tool, "info", None), "name", None)
            if name:
                self._by_name[str(name)] = tool

    async def invoke(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self._by_name.get(name)
        if tool is None:
            return f"(no such tool: {name})"
        result = await tool(**arguments)
        return result if isinstance(result, str) else str(result)


# Modes whose router verdict may turn into a spoken delegate ack + an async
# task (Johnny-trt.18). Exactly ``johnny.voice_pipeline.reasoning.SPEAKING_MODES``
# (a drift-guard test asserts equality): the ack is spoken audio, so non-speaking
# modes (listen_only, suggest_only) never need the task sink — and the no-TTS
# degrade rewrites speaking modes to suggest_only *before* this gate, so a
# session that cannot speak gets no task wiring either.
DELEGATION_CAPABLE_MODES: frozenset[str] = frozenset(
    {APPROVAL_REQUIRED_MODE, LIMITED_AUTO_SPEAK_MODE, AUTONOMOUS_MODE}
)


def build_event_bus(redis_url: str | None) -> EventBus:
    """Connect the Redis :class:`EventBus` when ``redis_url`` is set, else in-memory.

    Mirrors :func:`johnny.meet_worker.bootstrap.build_event_bus` (the meet-worker's
    own factory) but without importing that Playwright-heavy bootstrap module: a
    blank URL falls back to :class:`InMemoryEventBus` (a smoke / no-Redis run, where
    status events simply do not reach the API), and a real URL builds a
    :class:`RedisEventBus` on the default ``johnny.session`` channel prefix — the
    exact channel ``app.services.session_status_subscriber`` reads, so the agent
    path's events land in the same DB tables as the legacy pipeline's.
    """
    if not redis_url:
        logger.warning(
            "no redis_url in job payload; using in-memory event bus "
            "(status/decision/transcript events will NOT reach the API)"
        )
        return InMemoryEventBus()
    from redis.asyncio import Redis

    client = Redis.from_url(redis_url, decode_responses=False)
    return RedisEventBus(client, channel_prefix=DEFAULT_CHANNEL_PREFIX)


# LLM roles a session builds raw providers for (Johnny-trt.42). ``router``
# drives the triage gate + barge-in classifier; ``answer`` the allowed-reply
# coercion (the AgentSession reply node itself runs the adapter built from the
# same ``llm`` entry by the factory). The ``reasoning`` role is NOT built here
# — it is a credential-less stamp on agent_tasks rows (see
# :func:`johnny.agent.job_config.reasoning_llm_from_provider_config`).
LLM_ROLE_ROUTER = "router"
LLM_ROLE_ANSWER = "answer"


def _llm_entry_for_role(
    provider_config: Mapping[str, Any], role: str
) -> Mapping[str, Any] | None:
    """The payload entry serving ``role``: ``router_llm`` (router only) → ``llm``.

    The agent-resolution layer (Johnny-trt.42) emits the optional
    ``router_llm`` key only when the agent's router pin resolves to a
    different provider row than the answer entry; its absence means both
    roles share the ``llm`` entry — and the caller reuses one live instance.
    """
    if role == LLM_ROLE_ROUTER:
        entry = provider_config.get(PROVIDER_CONFIG_ROUTER_LLM_KEY)
        if isinstance(entry, Mapping):
            return entry
    entry = provider_config.get(ProviderKind.LLM.value)
    return entry if isinstance(entry, Mapping) else None


def _build_llm_provider(
    provider_config: Mapping[str, Any],
    *,
    registry: ProviderRegistry | None = None,
    role: str = LLM_ROLE_ANSWER,
) -> LLMProvider:
    """Instantiate the raw :class:`LLMProvider` serving ``role`` from the job payload.

    The router gate (:class:`RouterGate`) and the allowed-reply coercion both need
    the *raw* :class:`~app.providers.base.LLMProvider` — not the session
    :class:`~johnny.agent.adapters.johnny_llm.JohnnyLLM` adapter (which only exposes
    the provider *name*). Since Johnny-trt.42 the two stages can run DIFFERENT
    providers: the ``router`` role reads the optional ``router_llm`` payload entry
    (the agent's triage pin) falling back to ``llm``; the ``answer`` role always
    reads ``llm`` — the same agent-resolved entry the adapter factory builds the
    session's reply node from, so the coercion and the spoken replies stay on one
    provider. Fail-fast :class:`AgentSessionSetupError` on a missing/blank/
    wrong-type entry, like the split adapter factory.
    """
    entry = _llm_entry_for_role(provider_config, role)
    if entry is None:
        raise AgentSessionSetupError(
            "no active LLM provider in the dispatched job payload — the router gate "
            f"and answer stage need an 'llm' entry in provider_config (role={role})"
        )
    provider_name = str(entry.get("provider_name") or "").strip()
    if not provider_name:
        raise AgentSessionSetupError(
            f"the LLM entry serving role={role} in the job payload has no provider_name"
        )
    config = ProviderConfig(
        kind=ProviderKind.LLM,
        provider_name=provider_name,
        display_name=str(entry.get("display_name") or provider_name),
        credentials={str(k): str(v) for k, v in (entry.get("credentials") or {}).items()},
        options=dict(entry.get("options") or {}),
    )
    reg = registry if registry is not None else get_registry()
    instance = reg.instantiate(config)
    if not isinstance(instance, LLMProvider):
        raise AgentSessionSetupError(
            f"the active LLM provider is not an LLMProvider: {type(instance).__name__}"
        )
    return instance


@dataclass(slots=True)
class AgentRuntime:
    """Everything the worker needs to start + tear down one session's ``AgentSession``.

    :func:`build_agent_runtime` assembles the gate / agent / adapters / observability;
    the worker (Johnny-9eh) builds the :class:`~livekit.agents.AgentSession` from
    :attr:`adapters`, starts it with :attr:`agent`, and — only when
    :attr:`needs_approval_wiring` — finishes the ``approval_required`` wiring with the
    ledger / gate / approval gate / decision sink carried here. The delegated-task
    pieces (Johnny-trt.18) need no live session, so :attr:`task_coordinator` arrives
    fully wired for the gate's delegate branch (Johnny-trt.17). :meth:`aclose` drains
    the metrics publisher and in-flight task resolvers, closes the approval gate +
    task wake + owned event bus, and releases the shared sync DB session at teardown;
    the gate / ledger are swept by :meth:`~johnny.agent.session.JohnnyAgent.on_exit`
    (``RouterGate.aclose``).
    """

    config: SessionJobConfig
    session_id: str
    agent: JohnnyAgent
    adapters: SessionAdapters
    ledger: TurnLedger
    gate: RouterGate
    metrics_translator: MetricsTranslator
    event_bus: EventBus
    enable_barge_in: bool
    min_interruption_duration_s: float | None
    # Native tool-loop cap (Johnny-3gx) from the agent snapshot, threaded to
    # build_agent_session by the worker / browser surfaces. 0 = unlimited.
    max_tool_steps: int = 0
    # Live bot-reply caption emitter (Johnny-trt.39): the agent's tts_node
    # feeds it one sentence per flush; drained at aclose like the metrics
    # translator. ``None`` only on hand-built test runtimes.
    speech_interim_forwarder: AgentSpeechInterimForwarder | None = None
    approval_gate: ApprovalGate | None = None
    decision_sink: Any = None
    # Delegated-task pieces (Johnny-trt.18): the synchronous agent_tasks sink +
    # the coordinator the gate's delegate branch drives (Johnny-trt.17). Built
    # for delegation-capable (speaking) modes with a DB factory; None otherwise.
    task_sink: Any = None
    task_coordinator: TaskCoordinator | None = None
    # Skill registry + sandbox plumbing (Johnny-trt.23): the loaded skills
    # (catalog source + executor lookup) and the one HTTP client the
    # ``sandbox.exec`` tool talks to the skills-sandbox through. ``None``
    # whenever the runtime has no task pieces.
    skill_registry: SkillRegistry | None = None
    # Internal-tool seams (Johnny-trt.57): the session-local context the
    # in-process meeting.leave / session.end runners act through. ``None``
    # whenever the runtime has no task pieces.
    internal_tools: InternalToolContext | None = None
    # Phase-5 speech-queue wiring (Johnny-trt.28): the task-event listener +
    # gated delivery loop + queue, attached by the session surface (worker /
    # browser session) right after ``session.start`` via
    # :func:`johnny.agent.task_wiring.attach_task_speech_wiring` — it needs
    # the live ``AgentSession``, the same reason the approval coordinator
    # attaches late. ``None`` until attached (and forever on non-delegating
    # runtimes); torn down first in :meth:`aclose`.
    task_speech: Any = None
    # The meeting's shared speech floor (Johnny-trt.46): built only for
    # meeting-scoped sessions with Redis (the multi-agent surface) and
    # attached to the gate + agent + deliverer; ``None`` everywhere else
    # (every playground session). Torn down right after the task-speech
    # wiring so a mid-delivery lease settles before the backend closes.
    speech_floor: SpeechFloor | None = None
    _sandbox_client: SandboxClient | None = None
    # MCP gateway client manager (Johnny-3gx): the per-session McpClientManager
    # that the list_mcp_tools / call_mcp_tool gateway tools connect through.
    # Built only for native-tools sessions whose workspace has enabled MCP
    # servers; closed at teardown so held-open connections don't leak.
    _mcp_manager: Any = None
    _task_wake: Any = None
    _db_session: Session | None = None
    _owns_event_bus: bool = True

    async def warm_up(self) -> None:
        """Pre-load the session providers' lazy heavy state (Johnny-trt.8).

        Delegates to :func:`~johnny.agent.adapters.factory.warm_up_session_providers`
        over :attr:`adapters`' raw providers (whisper weights, Piper voice ONNX,
        local-LLM model load). Run it as a background task right after assembly —
        concurrently with session start, never gating the ready signal. Never
        raises; per-provider failures are logged and mean only that the first
        turn pays the lazy load as before.
        """
        await warm_up_session_providers(self.adapters, session_id=self.session_id)

    @property
    def needs_approval_wiring(self) -> bool:
        """Whether the worker should build the out-of-band approval coordinator.

        True only for ``approval_required`` mode *with* a live approval gate +
        decision sink (both built from ``redis_url`` + ``DATABASE_URL`` in
        :func:`build_agent_runtime`). A misconfigured approval session (no redis / no
        DB) leaves these ``None``, so the gate's own misconfig branch terminalizes the
        turn ``no_reply(approval_rejected)`` — the agent-path analogue of the legacy
        "approval_required but no JOHNNY_REDIS_URL → auto-reject on timeout".
        """
        return (
            self.config.mode == APPROVAL_REQUIRED_MODE
            and self.approval_gate is not None
            and self.decision_sink is not None
        )

    async def aclose(self) -> None:
        """Drain + release the resources the agent ``on_exit`` does not own.

        Defensive throughout: teardown of one resource never blocks the next, so a
        flaky Redis close cannot strand the DB session. ``RouterGate.aclose`` (the
        ledger sweep + approval-resolver cancellation) is fired by
        :meth:`JohnnyAgent.on_exit`; this handles the metrics drain, the approval
        gate, the owned event bus, and the approval DB session.
        """
        sid = self.session_id
        try:
            await self.metrics_translator.aclose()
        except Exception:
            logger.exception("agent runtime: metrics translator close failed for %s", sid)
        if self.speech_interim_forwarder is not None:
            try:
                await self.speech_interim_forwarder.aclose()
            except Exception:
                logger.exception("agent runtime: speech interim forwarder close failed for %s", sid)
        # Phase-5 speech wiring first (Johnny-trt.28): stop the task-event
        # listener + delivery loop and settle every undelivered speech item
        # exactly once BEFORE the coordinator (whose registry the callbacks
        # touch) and the event bus (whose publish the non-teardown drops use)
        # go away.
        if self.task_speech is not None:
            try:
                await self.task_speech.aclose()
            except Exception:
                logger.exception("agent runtime: task speech wiring close failed for %s", sid)
        # Speech floor next (Johnny-trt.46): releases any lease the stopped
        # delivery loop (or a dying gate path) still holds — a torn-down
        # session must free the meeting's floor immediately, not after a TTL.
        if self.speech_floor is not None:
            try:
                await self.speech_floor.aclose()
            except Exception:
                logger.exception("agent runtime: speech floor close failed for %s", sid)
        # Drain in-flight task resolvers BEFORE the DB session closes below —
        # a cancelled resolver marks its row ``cancelled`` through the sink.
        if self.task_coordinator is not None:
            try:
                await self.task_coordinator.aclose()
            except Exception:
                logger.exception("agent runtime: task coordinator close failed for %s", sid)
        # After the coordinator drain: in-flight skill executors may still be
        # mid-exec against the sandbox until that drain completes.
        if self._sandbox_client is not None:
            try:
                await self._sandbox_client.aclose()
            except Exception:
                logger.exception("agent runtime: sandbox client close failed for %s", sid)
        # MCP gateway connections (Johnny-3gx): close any held-open connector
        # sessions the list_mcp_tools / call_mcp_tool gateway tools opened —
        # after the sandbox close, since a stdio connector's process lives in
        # that sandbox container.
        if self._mcp_manager is not None:
            try:
                await self._mcp_manager.aclose()
            except Exception:
                logger.exception("agent runtime: mcp manager close failed for %s", sid)
        # Same ordering rationale for the internal-tool control client
        # (Johnny-trt.57): an in-flight meeting.leave / session.end resolver
        # may be mid-POST until the coordinator drain settles it.
        if self.internal_tools is not None:
            try:
                await self.internal_tools.aclose()
            except Exception:
                logger.exception("agent runtime: internal tools close failed for %s", sid)
        if self._task_wake is not None:
            try:
                await self._task_wake.close()
            except Exception:
                logger.exception("agent runtime: task wake close failed for %s", sid)
        if self.approval_gate is not None:
            try:
                await self.approval_gate.close()
            except Exception:
                logger.exception("agent runtime: approval gate close failed for %s", sid)
        if self._owns_event_bus:
            try:
                await self.event_bus.close()
            except Exception:
                logger.exception("agent runtime: event bus close failed for %s", sid)
        if self._db_session is not None:
            try:
                self._db_session.close()
            except Exception:
                logger.exception("agent runtime: db session close failed for %s", sid)


def _build_sync_persistence(
    config: SessionJobConfig,
    *,
    db_session_factory: Callable[[], Session] | None,
) -> tuple[ApprovalGate | None, Any, Any, Session | None]:
    """Build the synchronous DB-backed pieces: approval gate/sink + task sink.

    Two flows need a row to exist *synchronously* inside the turn hook — unlike
    every other event, which the async Redis subscriber persists:

    * ``approval_required`` needs the ``pending`` decision row's id before it
      parks a turn (Johnny-qzj) — the Redis-backed
      :class:`~app.services.approval.RedisApprovalGate` plus a
      :class:`~app.services.router_decisions.SqlAlchemyDecisionSink`;
    * every delegation-capable (speaking) mode needs the ``queued``
      ``agent_tasks`` row before the delegate ack is spoken (Johnny-trt.18) —
      a :class:`~app.services.agent_tasks.SqlAlchemyTaskSink`.

    Both ride one shared DB session from ``db_session_factory``
    (``SessionLocal`` in production). Degrades are per-piece and the session
    always still runs: no DB factory → ``(None, None, None, None)`` (approval
    auto-rejects on the gate's misconfig branch; delegate verdicts terminalize
    ``no_reply(stage_error)``); approval without redis loses only the approval
    pieces — the task sink does not need Redis (the wake ping is optional),
    so delegation keeps working.

    Returns ``(approval_gate, decision_sink, task_sink, db_session)``.
    """
    is_approval = config.mode == APPROVAL_REQUIRED_MODE
    delegation_capable = config.mode in DELEGATION_CAPABLE_MODES
    if not is_approval and not delegation_capable:
        return None, None, None, None
    session_id = str(config.bot_session_id)
    if db_session_factory is None:
        if is_approval:
            logger.warning(
                "mode=approval_required but no DB session factory for %s — "
                "cannot persist the pending decision row; turns auto-reject",
                session_id,
            )
        else:
            logger.warning(
                "mode=%s but no DB session factory for %s — cannot persist "
                "agent_tasks rows; delegate verdicts will not be honoured",
                config.mode,
                session_id,
            )
        return None, None, None, None

    db_session: Session | None = None

    def _db() -> Session:
        nonlocal db_session
        if db_session is None:
            db_session = db_session_factory()
        return db_session

    approval_gate: ApprovalGate | None = None
    decision_sink: Any = None
    if is_approval:
        redis_url = config.redis_url
        if not redis_url:
            logger.warning(
                "mode=approval_required but no redis_url in job payload for %s — "
                "approval clicks cannot reach the agent; turns auto-reject",
                session_id,
            )
        else:
            from app.services.approval import RedisApprovalGate
            from app.services.router_decisions import SqlAlchemyDecisionSink

            decision_sink = SqlAlchemyDecisionSink(_db(), config.bot_session_id)
            approval_gate = RedisApprovalGate(redis_url=redis_url, session_id=session_id)
            logger.info("approval gate wired to redis channel johnny.approval.%s", session_id)

    task_sink: Any = None
    if delegation_capable:
        from app.services.agent_tasks import SqlAlchemyTaskSink

        # Per-agent reasoning model (Johnny-trt.42): every queued row carries
        # the requesting agent's resolved reasoning-LLM identity so the worker
        # executor can run multi-step kinds on it. Credential-less by contract.
        # Workspace stamp (Johnny-wks.1): the session's frozen workspace
        # identity rides each queued row the same way, so the worker resolver
        # runs the task in the SAME workspace this session's catalog promised.
        task_sink = SqlAlchemyTaskSink(
            _db(),
            config.bot_session_id,
            reasoning_llm=reasoning_llm_from_provider_config(config.provider_config),
            workspace=workspace_from_agent_snapshot(config.agent_snapshot),
        )
        logger.info(
            "task sink wired for session %s (delegation-capable mode=%s)",
            session_id,
            config.mode,
        )

    return approval_gate, decision_sink, task_sink, db_session


def _env_int(name: str, default: int) -> int:
    """A positive-integer env knob with a defensive fallback.

    Used for the runtime tuning knobs that must not crash an assembly on a
    typo (e.g. ``JOHNNY_TURN_CLAIM_WINDOW_MS``, Johnny-trt.47): a missing /
    malformed / non-positive value degrades to the shipped default.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer — using %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("%s=%d must be positive — using %d", name, value, default)
        return default
    return value


def resolve_session_sandbox_url(config: SessionJobConfig) -> str:
    """Which sandbox serves this session's capability probes — keyed by WORKSPACE.

    The session-assembly twin of
    :func:`app.services.task_worker.resolve_sandbox_url` (Johnny-trt.63):
    ONE function on purpose. Keyed by the agent snapshot's workspace stamp
    (Johnny-wks.1): EVERY stamped workspace — the DEFAULT (id 1) included
    (Johnny-etu.5: lazy-launched like finance/ops, no longer special-cased to
    the always-on ``skills-sandbox``) — resolves to its own container's
    canonical endpoint; only a legacy snapshot with no stamp
    (``workspace_id is None``) falls back to the global skills-sandbox from
    ``JOHNNY_SKILLS_SANDBOX_URL``. Until that container exists (Johnny-wks.2),
    probes against it degrade through ``SandboxUnavailableError`` to an EMPTY
    availability snapshot for that key — the catalog promises nothing, never
    a crash. Nothing downstream needs to change, because the availability
    snapshot and the task catalog are already derived per session from
    whatever client this resolves — there is no cross-session snapshot cache
    on the session side to re-key (the worker's URL-keyed cache lives in
    :class:`app.services.task_worker.SandboxExecutorProvider`).
    """
    from johnny.skills.sandbox import sandbox_url_for_workspace, sandbox_url_from_env

    workspace_id = config.workspace_id
    if workspace_id is None:
        return sandbox_url_from_env()
    return sandbox_url_for_workspace(workspace_id)


def resolve_session_skills_dir(config: SessionJobConfig) -> str | None:
    """Which skills DIRECTORY this session's catalog is discovered from —
    keyed by WORKSPACE (Johnny-wks.3).

    The discovery twin of :func:`resolve_session_sandbox_url` (its worker
    sibling is :func:`app.services.task_worker.resolve_skills_dir`): a legacy
    snapshot with no stamp (``workspace_id is None``) scans the shared volume
    from ``JOHNNY_SKILLS_DIR``; EVERY stamped workspace — the DEFAULT (slug
    ``default``) included (Johnny-etu.5) — scans its OWN packages under
    ``~/.johnny/workspaces/<slug>/skills`` (mounted at the same ``/skills``
    path inside that workspace's container, so packages stay relocatable).
    A skill installed only in the Finance workspace is therefore promised
    only in sessions of agents attached to it — and a workspace dir that
    does not exist yet simply yields an empty catalog.

    ``None`` (a stamp with no usable slug — hand-built or corrupted) means
    the directory cannot be located: the caller loads NO skills rather than
    guessing, the same promise-nothing degrade as an unreachable sandbox.
    """
    from johnny.skills.sandbox import skills_dir_from_env, workspace_skills_dir

    if config.workspace_id is None:
        return skills_dir_from_env()
    slug = config.workspace_slug
    if not slug:
        logger.warning(
            "agent runtime: workspace %s stamp carries no slug — loading no "
            "workspace-local skills for session %s",
            config.workspace_id,
            config.bot_session_id,
        )
        return None
    return workspace_skills_dir(slug)


async def _build_skill_pieces(
    config: SessionJobConfig,
    *,
    skill_registry: SkillRegistry | None,
    sandbox_client: SandboxClient | None,
) -> tuple[SkillRegistry, SandboxClient | None]:
    """Load the skill registry for the session's task catalog (Johnny-trt.23/.55).

    Called only for delegation-capable assemblies (a task sink exists). One
    volume scan + the batched sandbox probes (``/bins``, the trt.55 env
    probe, and the declared availability checks) per session assembly —
    session start is not the per-turn hot path, and the resulting
    availability snapshot stays frozen for the session (the documented
    trt.55 lifecycle). Defensive throughout: any failure degrades to an
    empty registry (⇒ empty catalog ⇒ the router never learns undeliverable
    kinds), so session assembly never breaks on a broken volume or a down
    sandbox.

    The session no longer builds a skill *executor* (Johnny-trt.24): skill
    kinds are worker-owned — claimed, run against the sandbox, and settled by
    :mod:`app.services.task_worker`, which re-runs the availability check at
    claim time (the trt.55 recheck) before the run argv. The registry here
    feeds only the router's catalog and the answer model's capability notes.

    Returns ``(registry, sandbox_client)``; the caller stores the client on
    the runtime for teardown (the loader's probes are its only use now).

    The sandbox endpoint comes from :func:`resolve_session_sandbox_url`
    (Johnny-trt.63) — the single place a session's sandbox identity is
    decided, so Phase 7's per-agent sandboxes change that function and
    nothing here.
    """
    from johnny.skills.registry import (
        EMPTY_SKILL_REGISTRY,
        build_sandbox_availability_runner,
        load_skill_registry,
    )
    from johnny.skills.sandbox import SandboxClient as _SandboxClient

    session_id = str(config.bot_session_id)
    if sandbox_client is None:
        sandbox_client = _SandboxClient(base_url=resolve_session_sandbox_url(config))
    if skill_registry is None:
        # Discovery is workspace-keyed too (Johnny-wks.3): the catalog scans
        # the SAME workspace's skills dir the probes above target, so a
        # workspace-local skill is promised only to its own sessions.
        skills_dir = resolve_session_skills_dir(config)
        if skills_dir is None:
            skill_registry = EMPTY_SKILL_REGISTRY
        else:
            try:
                skill_registry = await load_skill_registry(
                    skills_dir,
                    check_bins=sandbox_client.check_bins,
                    check_env=sandbox_client.check_env,
                    run_check=build_sandbox_availability_runner(sandbox_client),
                )
            except Exception:
                logger.exception(
                    "agent runtime: skill registry load failed for %s — running without skills",
                    session_id,
                )
                skill_registry = EMPTY_SKILL_REGISTRY
    logger.info(
        "agent runtime: skill catalog loaded for %s (%s)",
        session_id,
        skill_registry.summary(),
    )
    return skill_registry, sandbox_client


def _load_mcp_snapshots(
    config: SessionJobConfig,
) -> tuple[McpServerSnapshot, ...]:
    """The MCP servers' cached capability view for this session (Johnny-trt.36).

    Reads the agent's workspace's ``.johnny/.mcp.json`` (Johnny-hp1, no DB):
    enabled servers → secretless configs + the last successful probe's cached
    tool list + the latest probe verdict (from the sibling ``.mcp-state.json``).
    Assembly NEVER connects to MCP servers — the probe endpoint and the
    worker's lazy claim-time client own connections; a server whose latest
    probe failed contributes its cached tools as unavailable-with-reason
    entries (Johnny-trt.55) instead of vanishing.

    Workspace-keyed (Johnny-wks.8): the catalog promises exactly the agent's
    workspace's MCP tools — the same set the worker resolves at claim time
    via :func:`app.services.mcp_servers.slug_for_stamp`, the default/legacy
    stamp resolving to the seeded default workspace's servers. Defensive like
    the skill loader: any failure degrades to no MCP entries, never a broken
    assembly.
    """
    try:
        from app.services.mcp_servers import slug_for_stamp
        from johnny.mcp.store import load_server_snapshots

        snapshots = load_server_snapshots(
            slug_for_stamp(config.workspace_id, config.workspace_slug)
        )
    except Exception:
        logger.exception(
            "agent runtime: mcp server snapshot load failed for %s — "
            "running without MCP tools",
            config.bot_session_id,
        )
        return ()
    if snapshots:
        logger.info(
            "agent runtime: mcp catalog loaded for %s (%d server(s))",
            config.bot_session_id,
            len(snapshots),
        )
    return snapshots


async def build_agent_runtime(
    config: SessionJobConfig,
    *,
    vad: VAD | None = None,
    event_bus: EventBus | None = None,
    registry: ProviderRegistry | None = None,
    transcript_history_loader: TranscriptHistoryLoader | None = None,
    db_session_factory: Callable[[], Session] | None = None,
    session_started_at: float = 0.0,
    audio_recorder: SpokenAudioRecorder | None = None,
    skill_registry: SkillRegistry | None = None,
    sandbox_client: SandboxClient | None = None,
    floor_scope: str | None = None,
    floor_backend: FloorBackend | None = None,
) -> AgentRuntime:
    """Assemble the full :class:`AgentRuntime` for one dispatched Meet session.

    Wires, in the legacy split pipeline assembly order: the STT/LLM/TTS adapters
    (from the agent-resolved ``provider_config``), the raw router/answer LLM,
    the turn ledger + index, the observability emitters, the router gate, the barge-in
    classifier, and the :class:`JohnnyAgent` (noise gate + answer nodes + transcript
    rehydration + metrics listener). ``approval_required`` mode additionally builds the
    Redis approval gate + synchronous decision sink (left for the worker to attach to a
    live :class:`AgentSession`); every delegation-capable (speaking) mode also gets the
    synchronous ``agent_tasks`` sink + a fully wired :class:`TaskCoordinator`
    (Johnny-trt.18 — no live session needed).

    A speaking mode dispatched with no configured TTS is degraded to ``suggest_only``
    (Johnny-un2): the assembled config's ``mode`` is rewritten, the agent runs with
    ``tts_available=False``, and the router still records decisions surfaced as
    suggestions — parity with the meet-worker's graceful TTS-missing degrade, rather
    than failing the job. Missing STT or LLM still raises.

    ``event_bus`` defaults to one built from ``config.redis_url`` (and is then owned —
    closed by :meth:`AgentRuntime.aclose`); an injected bus is left for the caller to
    own. ``vad`` is the process-warmed Silero model (the worker's prewarm), forwarded to
    the batch-STT adapter wrapping. ``db_session_factory`` (``SessionLocal``) backs the
    approval decision sink. Raises :class:`AgentSessionSetupError` for a unified payload
    or a missing STT / LLM provider — the agent path is split-only (a missing TTS
    degrades to ``suggest_only`` rather than raising).

    ``skill_registry`` / ``sandbox_client`` (Johnny-trt.23) are test seams: by
    default a delegation-capable runtime loads the registry from the skills
    volume (``JOHNNY_SKILLS_DIR``) with eligibility probed inside the
    skills-sandbox (``JOHNNY_SKILLS_SANDBOX_URL``); an injected registry skips
    the load, an injected client skips construction (the runtime still owns
    closing it).

    ``floor_scope`` (Johnny-trt.48) builds the shared speech floor for a
    meeting-less co-agent surface: the multi-agent playground group passes its
    ``browser-group-{id}`` scope so member sessions contend on one lock exactly
    like meeting co-agents. ``None`` keeps the trt.46 rule (floor iff
    ``meeting_config_id``). ``floor_backend`` substitutes the Redis backend
    (the ensemble scenario's shared in-memory hub — hermetic regression runs).
    """
    session_id = str(config.bot_session_id)

    # The session's reply-audio recorder (Johnny-od1): the TTS adapter feeds it
    # every synthesized segment and the spoke emitter flushes one WAV per kept
    # reply under JOHNNY_SESSION_AUDIO_DIR/<bot_session_id>/. Disabled (no-op)
    # when the env var is unset. Tests inject a tmp-rooted recorder.
    recorder = (
        audio_recorder
        if audio_recorder is not None
        else build_recorder_from_env(config.bot_session_id)
    )

    # STT/LLM adapters (required) + an optional TTS + the raw LLM the router gate /
    # coercion reuse. Built first so a misconfigured payload (unified / missing
    # STT or LLM) fails fast before any event-bus / Redis / DB resource is created.
    adapters = build_session_adapters_for_job(
        config, registry=registry, vad=vad, tts_recorder=recorder
    )
    # Raw LLMs by role (Johnny-trt.42): the router gate (+ barge-in) may run a
    # different provider than the answer-side coercion when the agent pinned a
    # triage model. Absent ``router_llm`` key → both roles share the ``llm``
    # entry AND one live instance (the pre-trt.42 shape — some providers hold
    # client state worth not duplicating).
    answer_llm = _build_llm_provider(
        config.provider_config, registry=registry, role=LLM_ROLE_ANSWER
    )
    router_llm = (
        _build_llm_provider(config.provider_config, registry=registry, role=LLM_ROLE_ROUTER)
        if isinstance(config.provider_config.get(PROVIDER_CONFIG_ROUTER_LLM_KEY), Mapping)
        else answer_llm
    )

    # Graceful no-TTS degrade (Johnny-un2), parity with the meet-worker's
    # ``pipeline_runner._assemble_pipeline``: a speaking mode with no configured TTS
    # downgrades to ``suggest_only`` so the router still records decisions —
    # surfaced through the normal ``RouterDecisionMade`` / ``AgentSuggested`` events
    # — instead of approving a reply that can never play (or the worker abandoning
    # the job). Threaded onto ``config`` so every downstream consumer (the approval
    # pieces, the gate, the answer nodes, the decision emitter) sees the effective
    # mode. Missing STT / LLM stays fail-fast above; only TTS degrades.
    tts_available = adapters.tts is not None
    effective_mode = degrade_speaking_mode_if_no_tts(config.mode, tts_available=tts_available)
    if effective_mode != config.mode:
        logger.warning(
            "agent runtime: mode=%s but no TTS configured for session=%s — degrading "
            "to %s (router still records decisions, surfaced as suggestions)",
            config.mode,
            session_id,
            effective_mode,
        )
        config = config.with_mode(effective_mode)

    bus = event_bus if event_bus is not None else build_event_bus(config.redis_url)
    owns_bus = event_bus is None

    # Turn accounting (INV-1 ledger + the shared str→int index) and the observability
    # emit seams that publish to the EventBus the subscriber persists.
    turn_index = TurnIndex()
    ledger = TurnLedger(build_session_terminal_emitter(bus, turn_index, session_id=session_id))

    is_approval = config.mode == APPROVAL_REQUIRED_MODE
    approval_gate, decision_sink, task_sink, db_session = _build_sync_persistence(
        config, db_session_factory=db_session_factory
    )
    persist_pending_decision = None
    if decision_sink is not None:
        from johnny.agent.approval_wiring import build_persist_pending_decision

        persist_pending_decision = build_persist_pending_decision(
            decision_sink, session_id=session_id, bot_session_id=config.bot_session_id
        )

    # Delegated-task coordination (Johnny-trt.18): needs no live AgentSession
    # (unlike the approval coordinator), so it is assembled right here and
    # carried on the runtime for the gate's delegate branch (Johnny-trt.17).
    # The skill registry (Johnny-trt.23) is loaded first — one volume scan +
    # at most one sandbox /bins probe per assembly, never on the turn loop —
    # to feed the router's task catalog below. Execution is split by
    # locality (Johnny-trt.24): the session executor runs ONLY the internal
    # tools (trt.57 — session-local by definition); every other kind stays
    # queued for the worker executor pass, with the coordinator's default
    # internal-kind predicate doing the routing and a read-only watcher
    # keeping the trt.53 failure correction alive.
    task_coordinator = None
    task_wake = None
    internal_tools = None
    # Mid-inline-loop off-turn promotion (Johnny-d6w.24): the promoted
    # continuation runs as an in-session task whose executor needs the answer LLM
    # + native tool surface assembled further below — bind it late through this
    # one-slot holder, read by the coordinator's executor dispatcher.
    inline_continuation_slot: list[TaskExecutor] = []
    if task_sink is not None:
        skill_registry, sandbox_client = await _build_skill_pieces(
            config,
            skill_registry=skill_registry,
            sandbox_client=sandbox_client,
        )
        # Internal tools (Johnny-trt.57) — the only kinds the session itself
        # executes since Johnny-trt.24. The fail-fast stub stays as the
        # fallback for defence (an in-session-routed kind missing from the
        # internal registry settles honestly instead of hanging). The
        # session-local context carries the Meet linkage (the surface
        # predicate for meeting.leave) and the api base for the in-app
        # control calls; the farewell-wait seam attaches after the gate is
        # built below (the attach_say ordering pattern).
        internal_tools = InternalToolContext(
            bot_session_id=config.bot_session_id,
            calendar_event_id=config.calendar_event_id,
        )
        internal_executor = build_internal_task_executor(
            internal_tools, fallback=stub_executor
        )
        # The coordinator's single executor dispatches the synthetic
        # ``inline.continuation`` kind (Johnny-d6w.24) to the late-bound runner,
        # falling through to the internal executor otherwise. The slot stays
        # empty in non-native modes — but no inline.continuation task is ever
        # begun without a promoter, so the fall-through is never exercised.
        from johnny.agent.inline_promotion import INLINE_CONTINUATION_KIND
        from johnny.agent.internal_tools import is_internal_kind

        async def _session_task_executor(queued: QueuedTask) -> TaskResult:
            if queued.spec.kind == INLINE_CONTINUATION_KIND and inline_continuation_slot:
                return await inline_continuation_slot[0](queued)
            return await internal_executor(queued)

        def _runs_in_session(kind: str) -> bool:
            return is_internal_kind(kind) or kind == INLINE_CONTINUATION_KIND

        from johnny.agent.task_wiring import build_task_coordinator

        task_coordinator, task_wake = build_task_coordinator(
            task_sink=task_sink,
            event_bus=bus,
            session_id=session_id,
            redis_url=config.redis_url,
            executor=_session_task_executor,
            runs_in_session=_runs_in_session,
        )
    else:
        skill_registry = None
        sandbox_client = None

    # Task catalog (Johnny-trt.19): teach the router the delegate
    # vocabulary only when a coordinator exists to honour it — a gate
    # without task wiring stage_errors delegate verdicts, so advertising
    # kinds there would invite turns that can only fail. Sources, in
    # resolution order (Johnny-trt.57): the internal tools scoped to
    # THIS surface (meeting.leave renders unavailable off the Meet
    # surface, Johnny-trt.55, so the playground router declines it
    # honestly instead of promising a meeting it isn't in), then the
    # skill loader (Johnny-trt.23): eligible SKILL.md packages on the
    # skills volume, each carrying its trt.55 availability verdict
    # (credentials/env evaluated at this assembly — the session's
    # frozen snapshot).
    # The capability policy resolved at dispatch rides the agent snapshot
    # (Johnny-trt.38) — re-read HERE, never from the policy tables. Applied
    # to the merged catalog below: a policy-denied kind becomes a hidden
    # unavailable entry — absent from every rendered prompt block (the
    # canonical least-privilege scenario: the progress agent's prompt never
    # even mentions finance kinds), still present in the tuple so the gate's
    # unavailable backstop degrades a forced delegate to the spoken decline
    # and emits the policy_denied event naming the layer.
    capability_policy = config.capability_policy()
    # MCP-contributed tools (Johnny-trt.36): the third catalog source —
    # cached probe results read from the workspace's .johnny/.mcp.json
    # (Johnny-hp1; no connections at assembly), one entry per enabled
    # server's filter-surviving tool, qualified as
    # mcp__<server>__<tool>. Policy filtering below applies to them exactly
    # like skills (deny globs such as mcp__shady__* hide a whole server).
    mcp_snapshots = (
        _load_mcp_snapshots(config) if task_coordinator is not None else ()
    )
    # Johnny-3ow native-tools flag: when the answer agent carries native sandbox
    # tools (full-access flag + a real sandbox) it can run exec/read/write/
    # list_dir directly. Johnny-d6w.27 REVERSED the original cutover's
    # router-blinding: the router KEEPS its full delegate catalog (internal +
    # skills + MCP) regardless of this flag, so skill-shaped requests route to
    # `delegate` (→ background worker) instead of being executed inline by the
    # answer model. Under the operator's 2026-06-18 architecture the speak/answer
    # LLM is a refiner/voicer, not a skill executor; the native tools that remain
    # here are a vestigial fallback that Johnny-d6w.28 removes. The answer-side
    # grounding still follows this flag (TOOL_USE_NOTES vs render_capability_notes,
    # below) until d6w.28 lands.
    native_tools_active = sandbox_client is not None and sandbox_full_access_enabled()

    task_catalog = (
        apply_policy_to_catalog(
            merge_task_catalog(
                internal_catalog_entries(
                    meeting_backed=config.calendar_event_id is not None
                ),
                skill_registry.catalog_entries() if skill_registry is not None else (),
                mcp_catalog_entries(mcp_snapshots),
            ),
            capability_policy,
        )
        if task_coordinator is not None
        else ()
    )
    # Pre-ack kind validation set (Johnny-trt.62): the kinds the executor
    # chain can actually resolve — internal tools + every skill on the
    # volume regardless of eligibility (broken skills still settle honestly
    # with skill-specific copy) + the MCP servers' cached qualified tools;
    # only kinds outside this set hit the stub's unsupported-kind leg. The
    # gate degrades delegate verdicts outside it to SPEAK before any ack is
    # spoken; the catalog above stays the spoken projection, so a kind the
    # render missed but the executor can run still delegates.
    executor_kinds = (
        executor_known_kinds(
            skill_registry.kinds() if skill_registry is not None else (),
            mcp_kinds=mcp_known_kinds(mcp_snapshots),
        )
        if task_coordinator is not None
        else frozenset()
    )

    # Johnny-d6w.27: the native-tools flag intentionally NO LONGER strips skills/
    # MCP from the router catalog. Capability awareness is the single source of
    # delegation truth — the router must see the workspace's skills to choose
    # `delegate` for skill-shaped work — so `task_catalog` / `executor_kinds`
    # keep their full value (above) even when the answer agent also holds native
    # tools. (Pre-d6w.27 a `if native_tools_active:` block overwrote them with
    # internal_catalog_entries + INTERNAL_TOOL_KINDS, blinding the router and
    # forcing skill-shaped asks onto the inline answer-model tool loop — the
    # session-26 bug. That inline execution path is removed in Johnny-d6w.28.)

    # Behavior knobs ride the dispatch payload from the session's frozen
    # agent snapshot (Johnny-trt.41) — the gate never re-reads config tables
    # at turn time. ``agent_display_name`` is the one identity string used
    # everywhere a co-agent might see this session: the floor/claim payloads,
    # the peer-selectivity prompt, and the handoff name match (Johnny-trt.47).
    agent_display_name = (
        str(config.agent_snapshot.get("name") or "").strip()
        or f"agent-{config.bot_session_id}"
    )
    gate_config = RouterGateConfig(
        mode=config.mode,
        character_prompt=config.character_prompt,
        context=config.context,
        calendar_context=config.calendar_context,
        calendar_attachments_text=config.calendar_attachments_text,
        prior_session_context=config.prior_session_context,
        allowed_replies=tuple(config.allowed_replies),
        confidence_threshold=config.confidence_threshold,
        # Router-triage timeout + on-timeout fallback (Johnny-xql), read from
        # the frozen agent snapshot like the other behavior knobs.
        router_llm_timeout_s=config.router_llm_timeout_s,
        router_timeout_retries=config.router_timeout_retries,
        router_timeout_fallback_mode=config.router_timeout_fallback_mode,
        router_timeout_fallback_text=config.router_timeout_fallback_text,
        task_catalog=task_catalog,
        executor_kinds=executor_kinds,
        agent_name=agent_display_name,
        peer_agent_names=config.peer_names,
        # Surface predicate for keyword delegate-recovery (Johnny-etu.6): a
        # meeting-backed session leaves a SPEAK verdict untouched so ambient
        # meeting talk never triggers an unasked skill run; the playground
        # (calendar_event_id is None) recovers dropped capability asks.
        meeting_backed=config.calendar_event_id is not None,
        # Johnny-3gx: tell the gate the answer agent has native tools so it can
        # drop a misrouted session-control delegate (a data request the
        # internal-only router catalog forced onto meeting.leave/session.end) to
        # SPEAK instead of declining or ending the session.
        native_tools_active=native_tools_active,
    )
    from app.services.model_calls import SqlAlchemyModelCallSink

    # Router-call observability (US-004 / Johnny-d6w.4): the gate records each
    # decided turn's router LLM call as a ``role='router'`` agent_model_calls row,
    # symmetric with the answer-loop ``role='answer'`` rows wired below. DB-only —
    # no live ModelCallObserved publish here (surfacing the router call live is the
    # Decisions-column story US-104); the gate resolves each call's durable turn id
    # through the same shared TurnIndex as its decision row.
    router_model_call_sink = SqlAlchemyModelCallSink(
        bot_session_id=config.bot_session_id
    )
    gate = RouterGate(
        router_llm,
        config=gate_config,
        ledger=ledger,
        persist_pending_decision=persist_pending_decision,
        record_decision=build_decision_emitter(
            bus,
            turn_index,
            mode=config.mode,
            approval_timeout_seconds=(
                gate_config.approval_timeout_seconds if is_approval else None
            ),
            # Run-config snapshot keys (Johnny-trt.54): persisted into every
            # decision row's input_window so the session replay and the
            # timeline's context step reconstruct what the router saw. The
            # free-form instructions override was retired (Johnny-trt.45) —
            # the emitter's default "" keeps the row shape unchanged.
            confidence_threshold=gate_config.confidence_threshold,
            session_id=session_id,
        ),
        record_spoke=build_spoke_emitter(
            bus,
            mode=config.mode,
            session_id=session_id,
            recorder=recorder,
            # Exact-turn final_text stamping (Johnny-trt.54): the AgentSpoke
            # carries the durable int turn id resolved through the same shared
            # index as the decision/terminal events.
            turn_index=turn_index,
        ),
        record_suggested=build_suggested_emitter(bus, session_id=session_id),
        # Triage-stage timing (Johnny-trt.19): the router LLM is a side call
        # LiveKit emits no metric for, so the gate publishes its own
        # ``router_llm`` PipelineTiming per decided turn — same session-start
        # reference as the MetricsTranslator below.
        record_triage_timing=build_triage_timing_emitter(
            bus,
            turn_index,
            provider_name=router_llm.name,
            session_started_at=session_started_at,
            session_id=session_id,
        ),
        # Conversation dynamics (Johnny-trt.49): one InterruptionRecorded per
        # cut speech, persisted by the subscriber to ``conversation_events`` —
        # who interrupted whom and the onset→audio-stop cut latency. Same
        # session-start reference as the timing emitters above.
        record_interruption=build_interruption_emitter(
            bus,
            turn_index,
            session_started_at=session_started_at,
            session_id=session_id,
        ),
        # Policy enforcement (Johnny-trt.38): one PolicyDenied per delegate
        # verdict degraded over a policy-hidden kind, persisted by the
        # subscriber to ``conversation_events`` with the denying layer.
        record_policy_denied=build_policy_denied_emitter(
            bus,
            turn_index,
            session_started_at=session_started_at,
            session_id=session_id,
        ),
        reply_audio=recorder,
        # Delegate branch seams (Johnny-trt.17): the coordinator queues the
        # async task row-before-ack; the resolver stamps the agent_tasks row
        # with the same durable int turn id the turn's decision/terminal carry.
        # The say() seam arrives later via JohnnyAgent.on_enter (the session
        # does not exist yet here).
        tasks=task_coordinator,
        resolve_turn_id=turn_index.resolve,
        # Mint the per-turn correlation id (US-003) into the SAME shared
        # TurnIndex the emitters read, so request_id reaches every one of the
        # turn's events + the delegated task.
        assign_request_id=turn_index.assign_request_id,
        model_call_sink=router_model_call_sink,
    )

    # Internal teardown tools wait for the farewell ack to finish playing
    # before disconnecting (Johnny-trt.57). The gate owns say(), so the seam
    # can only attach once the gate exists — same ordering reason as
    # attach_say / attach_approval.
    if internal_tools is not None:
        internal_tools.attach_farewell_wait(gate.wait_recent_say_done)

    # Shared speech floor (Johnny-trt.46): only a session that can have
    # co-agents builds one — every speak path then acquires it before its
    # first audio frame and peers' floor windows label/suppress their speech
    # in this session's STT. The floor's scope token is the meeting's id for
    # Meet sessions (the scheduler launches one session per enabled
    # assignment, Johnny-trt.45) or the caller-provided ``floor_scope`` for
    # meeting-less co-agent surfaces (the multi-agent playground group,
    # Johnny-trt.48 — a ``browser-group-{id}`` namespace that cannot collide
    # with meeting ids). Single-agent sessions (no meeting, no scope) and
    # no-Redis smoke runs leave it None: speak paths ungated, zero floor
    # events (the events.py single-agent contract). The holder identity is
    # the frozen snapshot's display name — what peers print in transcripts.
    # ``floor_backend`` (test seam) substitutes the Redis lock+broadcast with
    # an injected backend (the scenario harness's shared in-memory hub).
    speech_floor: SpeechFloor | None = None
    scope: str | None = floor_scope
    if scope is None and config.meeting_config_id is not None:
        scope = str(config.meeting_config_id)
    if scope is not None and (floor_backend is not None or config.redis_url):
        backend: FloorBackend = (
            floor_backend
            if floor_backend is not None
            else RedisFloorBackend(redis_url=config.redis_url or "", meeting_id=scope)
        )
        speech_floor = SpeechFloor(
            backend=backend,
            session_id=session_id,
            agent_name=agent_display_name,
            publish_event=bus.publish,
            timestamp_ms=session_relative_ms(session_started_at),
            # Turn-claim bucket window (Johnny-trt.47): the tuning knob —
            # claims whose end-of-speech anchors fall within this window are
            # the same utterance. Env-tunable so playground/live tuning needs
            # no rebuild.
            claim_window_ms=_env_int(
                "JOHNNY_TURN_CLAIM_WINDOW_MS", DEFAULT_CLAIM_WINDOW_MS
            ),
        )
        speech_floor.start()
        gate.attach_speech_floor(speech_floor)

    # The metrics translator resolves a LiveKit metric's speech_id (the reply
    # SpeechHandle.id, on LLM/TTS metrics) to the durable int turn id via the gate's
    # live reply→turn binding, falling back to the most recent turn for STT metrics
    # (which carry no speech_id) — the analogue of the legacy timing's utterance-count
    # fallback.
    def _resolve_turn_id(speech_id: str | None) -> int:
        active = gate.active_reply
        if active is not None and speech_id is not None and active[1].id == speech_id:
            return turn_index.resolve(active[0])
        return turn_index.last()

    metrics_translator = MetricsTranslator(
        bus,
        resolve_turn_id=_resolve_turn_id,
        session_started_at=session_started_at,
        session_id=session_id,
    )

    # Live bot-reply captions (Johnny-trt.39): each sentence the agent's
    # tts_node flushes into TTS goes out as an ephemeral AgentSpeechInterim.
    # The turn id mirrors the metrics resolver's gate binding, minus the
    # last-turn fallback — an ungated speech (say(), an approval reply) emits
    # turn_id=None rather than mis-attributing to the most recent gated turn.
    def _resolve_speech_turn() -> int | None:
        active = gate.active_reply
        if active is None:
            return None
        return turn_index.resolve(active[0])

    # The active turn's request_id (US-003 correlation), read at the mid-loop
    # promotion seam so the promoted workstream carries it (Johnny-d6w.24).
    def _resolve_request_id() -> str | None:
        active = gate.active_reply
        if active is None:
            return None
        return turn_index.request_id_for(active[0])

    speech_interim_forwarder = AgentSpeechInterimForwarder(
        bus,
        resolve_turn=_resolve_speech_turn,
        session_id=session_id,
    )

    # Tee the same per-sentence flushes into the gate's caption buffer
    # (Johnny-trt.58): when a barge-in cuts a speech, the gate's done-callback
    # takes the buffered sentences as the partial actually delivered, so the
    # interrupted text is kept (marked interrupted) instead of vanishing from
    # the chat/history. Gate first — note() is sync and trivially cheap — then
    # the forwarder schedules the live-caption publish.
    def _on_sentence_flushed(text: str, sequence: int) -> None:
        gate.note_speech_caption(text, sequence)
        speech_interim_forwarder.on_sentence_flushed(text, sequence)

    barge_in = BargeInClassifier(
        router_llm,
        # ``instructions`` keeps its parity default "" — the per-meeting
        # override it used to fold into the classifier prompt was retired
        # (Johnny-trt.45; it had been empty since the trt.41 rebuild).
        config=BargeInClassifierConfig(enable_barge_in=True),
    )

    async def _publish_transcript_filtered(event: TranscriptFiltered) -> None:
        await bus.publish(event)

    # Capability grounding for the ANSWER side (Johnny-trt.55 + Johnny-etu.7):
    # the router's catalog stops bad delegations, but a capability ask routed
    # to speak is answered by the answer model — which never sees that catalog.
    # It must learn what the session CAN do (so a speak-verdict on an available
    # capability never denies it — the etu.7 "wrong sandbox" fabrication) and
    # what it CANNOT (the same gaps + reasons or it improvises a pretend-check).
    # Empty only when the session has no user-facing capability and no gap,
    # leaving the prompt byte-identical.
    # Native sandbox tools (Johnny-3ow): under the cutover flag the answer
    # agent is handed exec/read/write/list_dir bound to THIS session's sandbox
    # container with full access (the container is the security boundary), so
    # it composes the user's REAL arguments and runs skills as files — instead
    # of the keyword router firing a fixed argv with JOHNNY_TASK_ARGS_JSON="{}".
    # Every call traces to agent_tool_calls through the SAME sink the worker
    # uses, so the reasoning timeline renders native calls unchanged. Off the
    # flag (or with no sandbox) → None → the tool-less prompt/behaviour is
    # byte-identical.
    # Per-model-call observability (Johnny-gal): every answer-loop LLM call
    # (each step of the native tool loop) records one agent_model_calls row with
    # its prompt, response, tool calls, tokens and timing — the answer-side
    # itemisation the operator could not see. Bound onto the JohnnyLLM the
    # AgentSession uses (adapters.llm); the resolver attributes each call to its
    # issuing turn. The router call stays captured in agent_decisions.
    if hasattr(adapters.llm, "bind_model_call_sink"):
        adapters.llm.bind_model_call_sink(
            SqlAlchemyModelCallSink(
                bot_session_id=config.bot_session_id,
                publish_observed=bus.publish,
            ),
            _resolve_speech_turn,
        )

    sandbox_tools = None
    mcp_manager = None
    if native_tools_active:
        # ``native_tools_active`` already implies a live sandbox client; assert it
        # so the type narrows for build_sandbox_tools / base_url below.
        assert sandbox_client is not None
        from app.services.agent_tasks import SqlAlchemyToolCallTraceSink

        # One sink spans the whole session, so it resolves the issuing turn
        # per call off the gate's live reply→turn binding (the same seam the
        # metrics translator uses). Without this every inline tool call
        # persisted turn_id=NULL and the timeline silently dropped it
        # (Johnny-5sm — the "black box"). Shared by the sandbox tools AND the
        # MCP gateway tools so both render in the timeline identically.
        native_trace_sink = SqlAlchemyToolCallTraceSink(
            bot_session_id=config.bot_session_id,
            resolve_turn_id=_resolve_speech_turn,
            publish_observed=bus.publish,
        )
        sandbox_tools = build_sandbox_tools(
            sandbox_client,
            policy=resolve_sandbox_policy(full_access=True),
            trace_sink=native_trace_sink,
        )
        # MCP gateway tools (Johnny-3gx): the cutover dropped the workspace's
        # configured MCP servers from the model's reach (router catalog forced
        # internal-only above; sandbox tools never advertised MCP), so the bot
        # could not list or call any connector. Re-add them as the three
        # discover→load→call meta-tools (list_mcp_servers / list_mcp_tools /
        # call_mcp_tool) — built only when the workspace actually has enabled
        # servers (mcp_snapshots non-empty). stdio servers spawn in the session's
        # skills-sandbox; the manager is owned by the runtime and closed at aclose.
        if mcp_snapshots:
            from app.services.mcp_servers import slug_for_stamp
            from johnny.agent.mcp_tools import build_mcp_tools
            from johnny.mcp.client import McpClientManager

            mcp_manager = McpClientManager()
            sandbox_tools = [
                *sandbox_tools,
                *build_mcp_tools(
                    slug=slug_for_stamp(config.workspace_id, config.workspace_slug),
                    manager=mcp_manager,
                    sandbox_url=sandbox_client.base_url,
                    trace_sink=native_trace_sink,
                ),
            ]

    # Mid-inline-loop off-turn promotion (Johnny-d6w.24): with native tools
    # active, a coordinator to honour the delegate, and the threshold enabled,
    # wire the promoter onto the answer adapter (the seam reads it) and register
    # the continuation runner for the synthetic kind. The runner reuses the SAME
    # native tool surface the inline loop ran, continuing the captured
    # investigation off-turn until a final answer (delivered as a task_result).
    if (
        native_tools_active
        and task_coordinator is not None
        and sandbox_tools
        and config.inline_promote_tool_step_threshold > 0
        and hasattr(adapters.llm, "bind_inline_promoter")
    ):
        from johnny.agent.inline_promotion import build_inline_promotion

        inline_promoter, inline_continuation_executor = build_inline_promotion(
            coordinator=task_coordinator,
            provider=answer_llm,
            tool_invoker=_NativeToolInvoker(sandbox_tools),
            threshold=config.inline_promote_tool_step_threshold,
            resolve_request_id=_resolve_request_id,
            max_steps=config.max_tool_steps,
        )
        inline_continuation_slot.append(inline_continuation_executor)
        adapters.llm.bind_inline_promoter(inline_promoter)
        logger.info(
            "inline-promote: wired for session %s (threshold=%s, max_off_turn_steps=%s)",
            session_id,
            config.inline_promote_tool_step_threshold,
            config.max_tool_steps or "default",
        )

    prompt_config = replace(
        instructions_config_from_job(config),
        # Under the cutover the catalog is internal-only, so its capability
        # grounding moves to TOOL_USE_NOTES; force the catalog notes empty so a
        # playground's unavailable meeting.leave can't leak a "CANNOT" line and
        # the two blocks never contradict each other (Johnny-3ow).
        capability_notes=(
            "" if native_tools_active else render_capability_notes(task_catalog)
        ),
        # Teach the model the native tool surface + skill-discovery recipe only
        # when it actually has those tools. Empty otherwise → byte-identical.
        tool_use_notes=TOOL_USE_NOTES if native_tools_active else "",
    )

    agent = await build_johnny_agent(
        prompt_config=prompt_config,
        transcript_history_loader=transcript_history_loader,
        session_id=session_id,
        bot_session_id=config.bot_session_id,
        router_gate=gate,
        barge_in=barge_in,
        answer_llm=answer_llm,
        answer_config=answer_config_from_job(config),
        tts_available=tts_available,
        noise_filter=NoiseFilterConfig(),
        transcript_filtered_sink=_publish_transcript_filtered,
        transcript_finalized_sink=build_transcript_finalized_emitter(bus, session_id=session_id),
        speech_interim_sink=_on_sentence_flushed,
        metrics_listener=metrics_translator.on_metrics_collected,
        peer_floor=speech_floor,
        agent_display_name=agent_display_name,
        # Human meet roster (US-401): calendar attendees, for 1:1 attribution.
        participants=config.participants,
        sandbox_tools=sandbox_tools,
    )

    return AgentRuntime(
        config=config,
        session_id=session_id,
        agent=agent,
        adapters=adapters,
        ledger=ledger,
        gate=gate,
        metrics_translator=metrics_translator,
        speech_interim_forwarder=speech_interim_forwarder,
        event_bus=bus,
        enable_barge_in=barge_in.enabled,
        min_interruption_duration_s=None,
        # Per-agent native tool-loop cap (Johnny-3gx) from the frozen snapshot;
        # the session surfaces hand it to build_agent_session. 0 = unlimited.
        max_tool_steps=config.max_tool_steps,
        approval_gate=approval_gate,
        decision_sink=decision_sink,
        task_sink=task_sink,
        task_coordinator=task_coordinator,
        skill_registry=skill_registry,
        internal_tools=internal_tools,
        speech_floor=speech_floor,
        _sandbox_client=sandbox_client,
        _mcp_manager=mcp_manager,
        _task_wake=task_wake,
        _db_session=db_session,
        _owns_event_bus=owns_bus,
    )


__all__ = [
    "AgentRuntime",
    "build_agent_runtime",
    "build_event_bus",
    "resolve_session_sandbox_url",
    "resolve_session_skills_dir",
]
