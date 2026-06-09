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
    LIMITED_AUTO_SPEAK_MODE,
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
    assert runtime.adapters.stt.provider == "deepgram"
    assert runtime.adapters.llm.provider == "openai"
    assert runtime.adapters.tts.provider == "cartesia"

    # Router gate: mode + the observability emitters wired, no approval persist.
    assert isinstance(runtime.gate, RouterGate)
    assert runtime.gate._config.mode == LIMITED_AUTO_SPEAK_MODE
    assert runtime.gate._record_decision is not None
    assert runtime.gate._record_spoke is not None
    assert runtime.gate._record_suggested is not None
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
