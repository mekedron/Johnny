"""Session assembly from a dispatched job payload (Johnny-9eh).

:func:`johnny.agent.job_session.build_agent_runtime` is the layer that wires every
Phase-2 component (adapters, router gate, observability emitters, barge-in, the noise
gate + answer nodes + transcript rehydration + metrics listener) into one
:class:`AgentRuntime` the worker starts. These tests assert the wiring holds — the
right emitters land on the gate, the agent carries the noise filter / answer config /
metrics listener, the approval pieces are built only for ``approval_required`` (and
degrade when redis / DB are absent), and teardown is defensive — without standing up a
live LiveKit session (the ``AgentSession`` + turn detector need a job context, so the
worker builds those; see ``test_worker.py``).

Guarded by ``importorskip`` so the suite still collects without the ``agent`` extra.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from app.providers.base import (
    ChatMessage,
    LLMProvider,
    LLMResponse,
    ProviderConfig,
    ProviderRegistry,
    STTProvider,
    ToolDefinition,
    TranscriptEvent,
    TTSProvider,
)
from app.providers.base import ProviderKind as Kind
from johnny.agent.job_config import (
    APPROVAL_REQUIRED_MODE,
    AUTONOMOUS_MODE,
    LIMITED_AUTO_SPEAK_MODE,
    LISTEN_ONLY_MODE,
    SUGGEST_ONLY_MODE,
    UNIFIED_PIPELINE_MODE,
    SessionJobConfig,
)
from johnny.voice_pipeline.event_bus import InMemoryEventBus, RedisEventBus

pytest.importorskip("livekit.agents")

from johnny.agent.adapters.factory import AgentSessionSetupError  # noqa: E402
from johnny.agent.gate import TurnLedger  # noqa: E402
from johnny.agent.job_session import (  # noqa: E402
    AgentRuntime,
    build_agent_runtime,
    build_event_bus,
)
from johnny.agent.router_gate import RouterGate  # noqa: E402
from johnny.agent.session import JohnnyAgent  # noqa: E402
from johnny.agent.tasks import TaskCoordinator  # noqa: E402

# --- Fakes (mirror tests/agent/test_job_runtime.py) -------------------------


class _FakeSTT(STTProvider):
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.provider_name

    async def transcribe_stream(
        self, audio_iter: AsyncIterator[bytes]
    ) -> AsyncIterator[TranscriptEvent]:
        async for _frame in audio_iter:
            yield TranscriptEvent(text="", is_final=True, timestamp_ms=0)


class _FakeLLM(LLMProvider):
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.provider_name

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        return LLMResponse(text="", finish_reason="stop")


class _FakeTTS(TTSProvider):
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.provider_name

    async def synthesize_stream(
        self, text: str, voice_id: str | None = None
    ) -> AsyncIterator[bytes]:
        for ch in text:
            yield ch.encode("ascii")


def _registry() -> ProviderRegistry:
    reg = ProviderRegistry()
    reg.register(Kind.STT, "deepgram", _FakeSTT)
    reg.register(Kind.LLM, "openai", _FakeLLM)
    reg.register(Kind.TTS, "cartesia", _FakeTTS)
    return reg


def _entry(provider_name: str) -> dict[str, Any]:
    return {
        "provider_name": provider_name,
        "display_name": provider_name,
        "credentials": {"api_key": "k"},
        "options": {},
    }


def _split_provider_config() -> dict[str, Any]:
    return {"stt": _entry("deepgram"), "llm": _entry("openai"), "tts": _entry("cartesia")}


def _provider_config_without_tts() -> dict[str, Any]:
    pc = _split_provider_config()
    del pc["tts"]
    return pc


def _job(**overrides: Any) -> SessionJobConfig:
    fields: dict[str, Any] = {
        "bot_session_id": 7,
        "room_name": "johnny-session-7",
        "provider_config": _split_provider_config(),
    }
    fields.update(overrides)
    return SessionJobConfig(**fields)


# --- build_event_bus --------------------------------------------------------


def test_build_event_bus_in_memory_without_url() -> None:
    assert isinstance(build_event_bus(None), InMemoryEventBus)
    assert isinstance(build_event_bus(""), InMemoryEventBus)


def test_build_event_bus_redis_with_url() -> None:
    # Redis.from_url is lazy (no connection), so this is safe offline.
    bus = build_event_bus("redis://localhost:6379/0")
    assert isinstance(bus, RedisEventBus)


# --- build_agent_runtime (non-approval) -------------------------------------


async def test_build_runtime_wires_full_session() -> None:
    bus = InMemoryEventBus()
    runtime = await build_agent_runtime(
        _job(mode=LIMITED_AUTO_SPEAK_MODE, instructions="Be brief."),
        event_bus=bus,
        registry=_registry(),
    )

    assert isinstance(runtime, AgentRuntime)
    # Adapters from the payload.
    assert runtime.adapters.tts is not None
    assert runtime.adapters.stt.provider == "deepgram"
    assert runtime.adapters.llm.provider == "openai"
    assert runtime.adapters.tts.provider == "cartesia"

    # Router gate: mode + the observability emitters wired, no approval persist.
    assert isinstance(runtime.gate, RouterGate)
    assert runtime.gate._config.mode == LIMITED_AUTO_SPEAK_MODE
    assert runtime.gate._record_decision is not None
    assert runtime.gate._record_spoke is not None
    assert runtime.gate._record_suggested is not None
    # Triage timing (Johnny-trt.19): wired unconditionally — the router_llm
    # PipelineTiming is how session_timings sees the triage cost.
    assert runtime.gate._record_triage_timing is not None
    assert runtime.gate._persist_pending_decision is None
    assert isinstance(runtime.ledger, TurnLedger)

    # The agent carries every per-session seam.
    assert isinstance(runtime.agent, JohnnyAgent)
    assert runtime.agent._router_gate is runtime.gate
    assert runtime.agent._barge_in is not None
    assert runtime.agent._answer_llm is not None
    assert runtime.agent._answer_config is not None
    assert runtime.agent._answer_config.mode == LIMITED_AUTO_SPEAK_MODE
    assert runtime.agent._noise_filter is not None
    assert runtime.agent._noise_filter.enabled is True
    assert runtime.agent._transcript_filtered_sink is not None
    assert runtime.agent._transcript_finalized_sink is not None
    assert runtime.agent._metrics_listener is not None
    assert runtime.agent._session_id == "7"

    # Live bot-reply captions (Johnny-trt.39): the runtime carries the
    # forwarder (drained at aclose) and the agent's tts_node feeds it.
    assert runtime.speech_interim_forwarder is not None
    assert (
        runtime.agent._speech_interim_sink == runtime.speech_interim_forwarder.on_sentence_flushed
    )

    # Barge-in enabled; no approval wiring; injected bus is not owned.
    assert runtime.enable_barge_in is True
    assert runtime.needs_approval_wiring is False
    assert runtime.approval_gate is None
    assert runtime._owns_event_bus is False


async def test_build_runtime_owns_self_built_event_bus() -> None:
    # No injected bus and no redis_url -> it builds (and therefore owns) an in-memory bus.
    runtime = await build_agent_runtime(_job(), registry=_registry())
    assert isinstance(runtime.event_bus, InMemoryEventBus)
    assert runtime._owns_event_bus is True
    await runtime.aclose()  # defensive teardown never raises


async def test_build_runtime_rejects_unified_payload() -> None:
    with pytest.raises(AgentSessionSetupError):
        await build_agent_runtime(_job(pipeline_mode=UNIFIED_PIPELINE_MODE), registry=_registry())


async def test_build_runtime_rejects_missing_llm() -> None:
    pc = _split_provider_config()
    del pc["llm"]
    with pytest.raises(AgentSessionSetupError):
        await build_agent_runtime(_job(provider_config=pc), registry=_registry())


# --- graceful no-TTS degrade (Johnny-un2) -----------------------------------


async def test_build_runtime_degrades_speaking_mode_without_tts() -> None:
    # A speaking mode dispatched with no TTS entry degrades to suggest_only: the
    # adapters carry no TTS, the assembled config + gate + answer config all run
    # suggest_only, and the agent runs with tts_available=False — so the router
    # still records decisions (surfaced as suggestions) instead of the worker
    # abandoning the job. Parity with meet_worker.pipeline_runner._assemble_pipeline.
    runtime = await build_agent_runtime(
        _job(mode=LIMITED_AUTO_SPEAK_MODE, provider_config=_provider_config_without_tts()),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
    )

    assert runtime.adapters.tts is None
    assert runtime.config.mode == SUGGEST_ONLY_MODE
    assert runtime.gate._config.mode == SUGGEST_ONLY_MODE
    assert runtime.agent._answer_config is not None
    assert runtime.agent._answer_config.mode == SUGGEST_ONLY_MODE
    assert runtime.agent._tts_available is False


async def test_build_runtime_approval_without_tts_degrades_to_suggest() -> None:
    # approval_required is a speaking mode, so a missing TTS degrades it to
    # suggest_only BEFORE the approval pieces are considered (the legacy order: the
    # TTS check rewrites the mode, then the approval gate keys off the rewritten
    # mode). The rewritten config.mode is the smoking gun — _build_sync_persistence
    # short-circuits on a non-approval, non-delegation-capable mode, so no
    # gate/sink/persist is built even though redis is configured (and the DB is
    # never consulted).
    runtime = await build_agent_runtime(
        _job(
            mode=APPROVAL_REQUIRED_MODE,
            redis_url="redis://r:6379/0",
            provider_config=_provider_config_without_tts(),
        ),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
    )

    assert runtime.config.mode == SUGGEST_ONLY_MODE
    assert runtime.gate._config.mode == SUGGEST_ONLY_MODE
    assert runtime.needs_approval_wiring is False
    assert runtime.approval_gate is None
    assert runtime.decision_sink is None
    assert runtime.gate._persist_pending_decision is None


async def test_build_runtime_non_speaking_mode_without_tts_unchanged() -> None:
    # listen_only never needs TTS, so a missing TTS is NOT a degrade: the mode is
    # untouched (no spurious suggest_only rewrite), tts is None, tts_available False.
    runtime = await build_agent_runtime(
        _job(mode=LISTEN_ONLY_MODE, provider_config=_provider_config_without_tts()),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
    )

    assert runtime.adapters.tts is None
    assert runtime.config.mode == LISTEN_ONLY_MODE
    assert runtime.gate._config.mode == LISTEN_ONLY_MODE
    assert runtime.agent._tts_available is False


# --- approval_required wiring -----------------------------------------------


class _FakeApprovalGate:
    def __init__(self, *, redis_url: str, session_id: str) -> None:
        self.redis_url = redis_url
        self.session_id = session_id
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeDecisionSink:
    def __init__(self, session: Any, bot_session_id: int) -> None:
        self.session = session
        self.bot_session_id = bot_session_id


class _FakeDbSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


async def test_approval_mode_wires_gate_and_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.approval.RedisApprovalGate", _FakeApprovalGate)
    monkeypatch.setattr("app.services.router_decisions.SqlAlchemyDecisionSink", _FakeDecisionSink)
    db = _FakeDbSession()

    runtime = await build_agent_runtime(
        _job(mode=APPROVAL_REQUIRED_MODE, redis_url="redis://r:6379/0"),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=lambda: db,
    )

    assert runtime.needs_approval_wiring is True
    assert isinstance(runtime.approval_gate, _FakeApprovalGate)
    assert isinstance(runtime.decision_sink, _FakeDecisionSink)
    assert runtime.decision_sink.bot_session_id == 7
    # The gate's pending-decision persist is wired for the approval round.
    assert runtime.gate._persist_pending_decision is not None

    # Teardown closes the approval gate and releases the DB session.
    await runtime.aclose()
    assert runtime.approval_gate.closed is True
    assert db.closed is True


async def test_approval_mode_degrades_without_redis() -> None:
    # approval_required but no redis_url -> no gate/sink (the gate auto-rejects),
    # mirroring the legacy "approval_required but no JOHNNY_REDIS_URL" degrade.
    runtime = await build_agent_runtime(
        _job(mode=APPROVAL_REQUIRED_MODE, redis_url=None),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=lambda: _FakeDbSession(),
    )
    assert runtime.needs_approval_wiring is False
    assert runtime.approval_gate is None
    assert runtime.decision_sink is None
    assert runtime.gate._persist_pending_decision is None


async def test_approval_mode_degrades_without_db_factory() -> None:
    runtime = await build_agent_runtime(
        _job(mode=APPROVAL_REQUIRED_MODE, redis_url="redis://r:6379/0"),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=None,
    )
    assert runtime.needs_approval_wiring is False
    assert runtime.approval_gate is None


# --- delegated-task wiring (Johnny-trt.18) -----------------------------------


@pytest.mark.parametrize("mode", [LIMITED_AUTO_SPEAK_MODE, AUTONOMOUS_MODE, APPROVAL_REQUIRED_MODE])
async def test_delegation_capable_modes_wire_task_sink_and_coordinator(
    mode: str,
) -> None:
    from app.services.agent_tasks import SqlAlchemyTaskSink

    db = _FakeDbSession()
    runtime = await build_agent_runtime(
        _job(mode=mode, redis_url="redis://r:6379/0"),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=lambda: db,
    )

    assert isinstance(runtime.task_sink, SqlAlchemyTaskSink)
    assert runtime.task_sink.bot_session_id == 7
    assert isinstance(runtime.task_coordinator, TaskCoordinator)
    assert runtime._task_wake is not None  # redis_url present -> wake ping wired
    # The gate's delegate branch (Johnny-trt.17) drives this same coordinator
    # and stamps agent_tasks rows with the shared TurnIndex's int turn id.
    assert runtime.gate._tasks is runtime.task_coordinator
    assert runtime.gate._resolve_turn_id is not None
    # Task catalog (Johnny-trt.19): a delegation-capable runtime teaches the
    # router the Phase-3 stub kinds through the gate config.
    from johnny.agent.task_catalog import STUB_TASK_CATALOG

    assert runtime.gate._config.task_catalog == STUB_TASK_CATALOG

    await runtime.aclose()
    assert db.closed is True


@pytest.mark.parametrize("mode", [LISTEN_ONLY_MODE, SUGGEST_ONLY_MODE])
async def test_non_speaking_modes_get_no_task_pieces(mode: str) -> None:
    runtime = await build_agent_runtime(
        _job(mode=mode, redis_url="redis://r:6379/0"),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=lambda: _FakeDbSession(),
    )
    assert runtime.task_sink is None
    assert runtime.task_coordinator is None
    assert runtime._task_wake is None
    assert runtime._db_session is None  # nothing needed the sync DB session
    assert runtime.gate._config.task_catalog == ()  # no delegation, no catalog


async def test_approval_without_redis_still_wires_tasks() -> None:
    # The approval pieces need Redis (clicks travel over it); the task sink
    # does not — a delegate verdict must keep working when only the DB exists.
    from app.services.agent_tasks import SqlAlchemyTaskSink

    db = _FakeDbSession()
    runtime = await build_agent_runtime(
        _job(mode=APPROVAL_REQUIRED_MODE, redis_url=None),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=lambda: db,
    )
    assert runtime.needs_approval_wiring is False
    assert runtime.approval_gate is None
    assert runtime.decision_sink is None
    assert isinstance(runtime.task_sink, SqlAlchemyTaskSink)
    assert isinstance(runtime.task_coordinator, TaskCoordinator)
    assert runtime._task_wake is None  # no redis -> no wake ping
    await runtime.aclose()
    assert db.closed is True


async def test_delegation_mode_without_db_factory_gets_no_task_pieces() -> None:
    runtime = await build_agent_runtime(
        _job(mode=LIMITED_AUTO_SPEAK_MODE),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=None,
    )
    assert runtime.task_sink is None
    assert runtime.task_coordinator is None
    # No coordinator on the gate either — its delegate branch terminalizes
    # no_reply(stage_error) instead of promising unrecordable work (trt.17).
    assert runtime.gate._tasks is None
    # And no catalog (trt.19): the router is never taught kinds that could
    # only stage_error here.
    assert runtime.gate._config.task_catalog == ()


async def test_speaking_mode_without_tts_degrade_drops_task_wiring() -> None:
    # The no-TTS degrade rewrites the mode to suggest_only BEFORE the
    # persistence gate, so a session that cannot speak an ack gets no task
    # pieces (an unspeakable ack must never queue work).
    runtime = await build_agent_runtime(
        _job(
            mode=LIMITED_AUTO_SPEAK_MODE,
            provider_config=_provider_config_without_tts(),
        ),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=lambda: _FakeDbSession(),
    )
    assert runtime.config.mode == SUGGEST_ONLY_MODE
    assert runtime.task_sink is None
    assert runtime.task_coordinator is None


async def test_approval_mode_shares_one_db_session_between_sinks() -> None:
    # Both synchronous sinks ride one DB session (one connection per session,
    # released once at teardown) — no second factory call.
    from app.services.agent_tasks import SqlAlchemyTaskSink
    from app.services.router_decisions import SqlAlchemyDecisionSink

    sessions: list[_FakeDbSession] = []

    def factory() -> _FakeDbSession:
        sessions.append(_FakeDbSession())
        return sessions[-1]

    runtime = await build_agent_runtime(
        _job(mode=APPROVAL_REQUIRED_MODE, redis_url="redis://r:6379/0"),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=factory,
    )
    assert len(sessions) == 1
    assert isinstance(runtime.decision_sink, SqlAlchemyDecisionSink)
    assert isinstance(runtime.task_sink, SqlAlchemyTaskSink)
    assert runtime.decision_sink._session is runtime.task_sink._session
    await runtime.aclose()
    assert sessions[0].closed is True


async def test_aclose_drains_coordinator_and_wake_before_db_close() -> None:
    # A cancelled resolver settles its row THROUGH the sink, so the DB session
    # must still be open when the coordinator drains.
    events: list[str] = []

    class _ProbeCoordinator:
        async def aclose(self) -> None:
            events.append("coordinator")

    class _ProbeWake:
        async def close(self) -> None:
            events.append("wake")

    class _ProbeDb:
        def close(self) -> None:
            events.append("db")

    runtime = await build_agent_runtime(
        _job(mode=LIMITED_AUTO_SPEAK_MODE),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
    )
    runtime.task_coordinator = _ProbeCoordinator()  # type: ignore[assignment]
    runtime._task_wake = _ProbeWake()
    runtime._db_session = _ProbeDb()  # type: ignore[assignment]

    await runtime.aclose()
    assert events == ["coordinator", "wake", "db"]
