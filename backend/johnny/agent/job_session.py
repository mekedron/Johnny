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
from johnny.agent.job_config import (
    APPROVAL_REQUIRED_MODE,
    AUTONOMOUS_MODE,
    LIMITED_AUTO_SPEAK_MODE,
    SessionJobConfig,
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
    build_session_terminal_emitter,
    build_spoke_emitter,
    build_suggested_emitter,
    build_transcript_finalized_emitter,
    build_triage_timing_emitter,
)
from johnny.agent.router_gate import RouterGate, RouterGateConfig
from johnny.agent.session import JohnnyAgent, build_johnny_agent
from johnny.agent.task_catalog import STUB_TASK_CATALOG
from johnny.agent.tasks import TaskCoordinator
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
    from johnny.voice_pipeline.approval import ApprovalGate
    from johnny.voice_pipeline.transcript_history import TranscriptHistoryLoader

logger = logging.getLogger(__name__)

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


def _build_llm_provider(
    provider_config: Mapping[str, Any],
    *,
    registry: ProviderRegistry | None = None,
) -> LLMProvider:
    """Instantiate the raw answer/router :class:`LLMProvider` from the job payload.

    The router gate (:class:`RouterGate`) and the allowed-reply coercion both need
    the *raw* :class:`~app.providers.base.LLMProvider` — not the session
    :class:`~johnny.agent.adapters.johnny_llm.JohnnyLLM` adapter (which only exposes
    the provider *name*). One instance is reused for both, mirroring the legacy
    meet-worker's ``router_llm=answer_llm=_as_llm(llm)`` (the same provider drives the
    router decision and the answer stage). Built from the same personality-resolved
    ``provider_config`` entry the adapter factory reads, so the session, the router,
    and the coercion all run the operator's configured (and personality-overridden)
    LLM. Fail-fast :class:`AgentSessionSetupError` on a missing/blank/wrong-type
    entry, like the split adapter factory.
    """
    entry = provider_config.get(ProviderKind.LLM.value)
    if not isinstance(entry, Mapping):
        raise AgentSessionSetupError(
            "no active LLM provider in the dispatched job payload — the router gate "
            "and answer stage need an 'llm' entry in provider_config"
        )
    provider_name = str(entry.get("provider_name") or "").strip()
    if not provider_name:
        raise AgentSessionSetupError("the 'llm' entry in the job payload has no provider_name")
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
        # Drain in-flight task resolvers BEFORE the DB session closes below —
        # a cancelled resolver marks its row ``cancelled`` through the sink.
        if self.task_coordinator is not None:
            try:
                await self.task_coordinator.aclose()
            except Exception:
                logger.exception("agent runtime: task coordinator close failed for %s", sid)
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

        task_sink = SqlAlchemyTaskSink(_db(), config.bot_session_id)
        logger.info(
            "task sink wired for session %s (delegation-capable mode=%s)",
            session_id,
            config.mode,
        )

    return approval_gate, decision_sink, task_sink, db_session


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
) -> AgentRuntime:
    """Assemble the full :class:`AgentRuntime` for one dispatched Meet session.

    Wires, in the legacy split pipeline assembly order: the STT/LLM/TTS adapters
    (from the personality-resolved ``provider_config``), the raw router/answer LLM,
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
    router_llm = _build_llm_provider(config.provider_config, registry=registry)

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
        config = replace(config, mode=effective_mode)

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
    # Phase 3 runs the stub executor — every kind fails fast with speech-ready
    # text, so an ack can never become a dead promise.
    task_coordinator = None
    task_wake = None
    if task_sink is not None:
        from johnny.agent.task_wiring import build_task_coordinator

        task_coordinator, task_wake = build_task_coordinator(
            task_sink=task_sink,
            event_bus=bus,
            session_id=session_id,
            redis_url=config.redis_url,
        )

    gate_config = RouterGateConfig(
        mode=config.mode,
        personality_prompt=config.personality_prompt,
        instructions=config.instructions,
        context=config.context,
        calendar_context=config.calendar_context,
        calendar_attachments_text=config.calendar_attachments_text,
        prior_session_context=config.prior_session_context,
        # Task catalog (Johnny-trt.19): teach the router the delegate
        # vocabulary only when a coordinator exists to honour it — a gate
        # without task wiring stage_errors delegate verdicts, so advertising
        # kinds there would invite turns that can only fail. Phase-3 stub
        # entries; the Phase-4 skill loader (trt.23) becomes the source.
        task_catalog=(STUB_TASK_CATALOG if task_coordinator is not None else ()),
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
            session_id=session_id,
        ),
        record_spoke=build_spoke_emitter(
            bus, mode=config.mode, session_id=session_id, recorder=recorder
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
        reply_audio=recorder,
        # Delegate branch seams (Johnny-trt.17): the coordinator queues the
        # async task row-before-ack; the resolver stamps the agent_tasks row
        # with the same durable int turn id the turn's decision/terminal carry.
        # The say() seam arrives later via JohnnyAgent.on_enter (the session
        # does not exist yet here).
        tasks=task_coordinator,
        resolve_turn_id=turn_index.resolve,
    )

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

    speech_interim_forwarder = AgentSpeechInterimForwarder(
        bus,
        resolve_turn=_resolve_speech_turn,
        session_id=session_id,
    )

    barge_in = BargeInClassifier(
        router_llm,
        config=BargeInClassifierConfig(enable_barge_in=True, instructions=config.instructions),
    )

    async def _publish_transcript_filtered(event: TranscriptFiltered) -> None:
        await bus.publish(event)

    agent = await build_johnny_agent(
        prompt_config=instructions_config_from_job(config),
        transcript_history_loader=transcript_history_loader,
        session_id=session_id,
        bot_session_id=config.bot_session_id,
        router_gate=gate,
        barge_in=barge_in,
        answer_llm=router_llm,
        answer_config=answer_config_from_job(config),
        tts_available=tts_available,
        noise_filter=NoiseFilterConfig(),
        transcript_filtered_sink=_publish_transcript_filtered,
        transcript_finalized_sink=build_transcript_finalized_emitter(bus, session_id=session_id),
        speech_interim_sink=speech_interim_forwarder.on_sentence_flushed,
        metrics_listener=metrics_translator.on_metrics_collected,
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
        approval_gate=approval_gate,
        decision_sink=decision_sink,
        task_sink=task_sink,
        task_coordinator=task_coordinator,
        _task_wake=task_wake,
        _db_session=db_session,
        _owns_event_bus=owns_bus,
    )


__all__ = [
    "AgentRuntime",
    "build_agent_runtime",
    "build_event_bus",
]
