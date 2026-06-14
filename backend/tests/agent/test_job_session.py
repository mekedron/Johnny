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
from pathlib import Path
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
    resolve_session_sandbox_url,
)
from johnny.agent.router_gate import RouterGate  # noqa: E402
from johnny.agent.session import JohnnyAgent  # noqa: E402
from johnny.agent.tasks import TaskCoordinator  # noqa: E402
from johnny.skills.sandbox import (  # noqa: E402
    DEFAULT_SANDBOX_URL,
    SANDBOX_URL_ENV,
    SKILLS_DIR_ENV,
    WORKSPACES_DIR_ENV,
)


@pytest.fixture(autouse=True)
def _isolated_skills_volume(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Point the skill loader AND the MCP store at empty tmp volumes.

    These tests run inside the api container, where ``JOHNNY_SKILLS_DIR`` /
    ``JOHNNY_WORKSPACES_DIR`` target the real operator volumes — a
    delegation-capable assembly would otherwise load whatever skills the host
    happens to have (and probe the live sandbox) AND read the real default
    workspace's ``.johnny/.mcp.json`` (Johnny-hp1: the MCP snapshot read is
    file-based now, not a fake DB session). Empty dirs keep the default path
    deterministic: zero skills, zero MCP servers, zero sandbox round-trips.
    Tests that want specific skills/MCP set these env vars themselves (their
    body runs after this fixture, so their setenv wins).
    """
    monkeypatch.setenv(SKILLS_DIR_ENV, str(tmp_path_factory.mktemp("skills")))
    monkeypatch.setenv(WORKSPACES_DIR_ENV, str(tmp_path_factory.mktemp("workspaces")))


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


# Behavior kwargs fold into the agent snapshot (Johnny-trt.45): tests keep
# their readable `_job(mode=..., character_prompt=...)` shape while the
# contract itself only carries the snapshot. ``context`` maps to the
# snapshot's ``assignment_context`` slot; ``instructions`` was retired.
_SNAPSHOT_KWARGS = {
    "mode": "mode",
    "character_prompt": "character_prompt",
    "allowed_replies": "allowed_replies",
    "confidence_threshold": "confidence_threshold",
    "context": "assignment_context",
}


def _job(**overrides: Any) -> SessionJobConfig:
    snapshot: dict[str, Any] = dict(overrides.pop("agent_snapshot", {}) or {})
    for kwarg, key in _SNAPSHOT_KWARGS.items():
        if kwarg in overrides:
            value = overrides.pop(kwarg)
            snapshot[key] = list(value) if isinstance(value, tuple) else value
    fields: dict[str, Any] = {
        "bot_session_id": 7,
        "room_name": "johnny-session-7",
        "agent_snapshot": snapshot,
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
        _job(mode=LIMITED_AUTO_SPEAK_MODE, context="Be brief."),
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
    # forwarder (drained at aclose) and the agent's tts_node feeds it through
    # the trt.58 tee — one flush lands in BOTH the gate's caption buffer (the
    # interrupted-partial source) and the forwarder's live-caption publish.
    assert runtime.speech_interim_forwarder is not None
    sink = runtime.agent._speech_interim_sink
    assert sink is not None
    sink("First sentence of the reply.", 0)
    assert runtime.gate._captions.take() == "First sentence of the reply."
    assert runtime.speech_interim_forwarder._tasks  # publish scheduled
    await runtime.speech_interim_forwarder.aclose()

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
    # abandoning the job (degrade behaviour inherited from the retired
    # in-worker assembler).
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
    # Task catalog (Johnny-trt.19/trt.23/trt.57): internal tools head the
    # catalog; the skill loader follows. The isolated (empty) skills volume
    # loads a registry with no skills, so only the internal kinds remain —
    # meeting.leave rendered UNAVAILABLE (Johnny-trt.55 surface scoping,
    # this job has no calendar_event_id) so the router declines it honestly,
    # and session.end available everywhere.
    assert runtime.skill_registry is not None
    assert runtime.skill_registry.skills == ()
    catalog = runtime.gate._config.task_catalog
    assert [(entry.kind, entry.available) for entry in catalog] == [
        ("meeting.leave", False),
        ("session.end", True),
    ]
    assert "no meeting to leave" in catalog[0].unavailable_reason
    assert catalog[0].keywords == ()  # unavailable kinds feed the scorer nothing
    # Pre-ack kind validation (Johnny-trt.62): the executor-known set is
    # filled alongside the catalog — internal tools only on an empty volume.
    assert runtime.gate._config.executor_kinds == frozenset(
        {"meeting.leave", "session.end"}
    )
    # The ANSWER prompt carries the same gap honestly (Johnny-trt.55): the
    # agent's persistent instructions teach the no-pretend-check decline.
    assert "no meeting to leave" in runtime.agent.instructions
    assert "Never pretend to check" in runtime.agent.instructions
    # The internal-tool context is wired with the session linkage + the
    # gate's farewell-wait seam (Johnny-trt.57).
    assert runtime.internal_tools is not None
    assert runtime.internal_tools.bot_session_id == 7
    assert runtime.internal_tools.calendar_event_id is None
    assert runtime.internal_tools.wait_for_farewell == runtime.gate.wait_recent_say_done

    await runtime.aclose()
    assert db.closed is True


async def test_delegation_capable_runtime_catalogs_injected_skills(tmp_path: Path) -> None:
    """The Phase-4 catalog source (Johnny-trt.23): eligible skills become
    the router's delegate vocabulary, and the runtime carries the registry."""
    from johnny.skills.registry import load_skill_registry

    (tmp_path / "fetch-news").mkdir()
    (tmp_path / "fetch-news" / "SKILL.md").write_text(
        "---\nname: fetch-news\ndescription: \"Fetch today's news.\"\n"
        "metadata: '{\"johnny\": {\"keywords\": [\"news\"]}}'\n---\nInstructions.\n",
        encoding="utf-8",
    )

    async def no_probe(names: list[str]) -> dict[str, bool]:
        raise AssertionError("baseline-only skill must not probe the sandbox")

    registry_obj = await load_skill_registry(tmp_path, check_bins=no_probe)
    db = _FakeDbSession()
    runtime = await build_agent_runtime(
        _job(mode=AUTONOMOUS_MODE, redis_url="redis://r:6379/0"),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=lambda: db,
        skill_registry=registry_obj,
    )

    assert runtime.skill_registry is registry_obj
    # Internal kinds first (resolution order, Johnny-trt.57), then the
    # skill loader's entries (Johnny-trt.23). No Meet linkage, so
    # meeting.leave rides along as the trt.55 unavailable entry.
    assert [entry.kind for entry in runtime.gate._config.task_catalog] == [
        "meeting.leave",
        "session.end",
        "fetch-news",
    ]
    assert [entry.available for entry in runtime.gate._config.task_catalog] == [
        False,
        True,
        True,
    ]
    assert runtime.gate._config.task_catalog[2:] == registry_obj.catalog_entries()
    # The executor-known set carries the skill kind too (Johnny-trt.62) —
    # membership truth = internal tools + the volume, not the render.
    assert runtime.gate._config.executor_kinds == frozenset(
        {"meeting.leave", "session.end", "fetch-news"}
    )
    await runtime.aclose()
    assert db.closed is True


async def test_answer_prompt_grounds_capability_self_awareness_in_live_catalog(
    tmp_path: Path,
) -> None:
    """Johnny-etu.17: the answer model's persistent instructions ground a
    capability ask in the session's REAL catalog — the available skill's
    one-liner is present (catalog → capability_notes → instructions path), and
    the always-present self-awareness guard rides alongside it so the small
    model can never roleplay a fake skill or deflect 'I'm just a bot'."""
    from johnny.agent.session import _SELF_AWARENESS_NOTE
    from johnny.skills.registry import load_skill_registry

    (tmp_path / "fetch-news").mkdir()
    (tmp_path / "fetch-news" / "SKILL.md").write_text(
        "---\nname: fetch-news\ndescription: \"Fetch today's news.\"\n---\nInstructions.\n",
        encoding="utf-8",
    )

    async def no_probe(names: list[str]) -> dict[str, bool]:
        raise AssertionError("baseline-only skill must not probe the sandbox")

    registry_obj = await load_skill_registry(tmp_path, check_bins=no_probe)
    db = _FakeDbSession()
    runtime = await build_agent_runtime(
        _job(mode=AUTONOMOUS_MODE, redis_url="redis://r:6379/0"),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=lambda: db,
        skill_registry=registry_obj,
    )

    instructions = runtime.agent.instructions
    # Regression guard (AC): a non-empty catalog yields non-empty capability
    # notes, and the real skill is named in the prompt the answer model reads.
    assert "- fetch-news: Fetch today's news." in instructions
    assert "handled for you by background tools" in instructions
    # The self-awareness guard is always present and tells the model the listed
    # tools are its own skills to name, rather than inventing or deflecting.
    assert _SELF_AWARENESS_NOTE in instructions
    assert "are YOUR OWN skills" in instructions
    await runtime.aclose()
    assert db.closed is True


async def test_cutover_flag_narrows_router_and_wires_native_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Johnny-3ow Phase 3 cutover: with JOHNNY_SANDBOX_FULL_ACCESS the router
    catalog collapses to the internal session-control kinds (skills drop out —
    the model discovers them as files via list_dir), the agent carries the
    native exec/read/write/list_dir tools, and the prompt grounds on the
    openclaw tool-use recipe instead of the keyword capability notes."""
    from johnny.agent.adapters.johnny_llm import tools_to_definitions
    from johnny.agent.internal_tools import INTERNAL_TOOL_KINDS
    from johnny.skills.registry import load_skill_registry

    monkeypatch.setenv("JOHNNY_SANDBOX_FULL_ACCESS", "1")

    (tmp_path / "fetch-news").mkdir()
    (tmp_path / "fetch-news" / "SKILL.md").write_text(
        "---\nname: fetch-news\ndescription: \"Fetch today's news.\"\n---\nInstructions.\n",
        encoding="utf-8",
    )

    async def no_probe(names: list[str]) -> dict[str, bool]:
        raise AssertionError("baseline-only skill must not probe the sandbox")

    registry_obj = await load_skill_registry(tmp_path, check_bins=no_probe)
    db = _FakeDbSession()
    runtime = await build_agent_runtime(
        _job(mode=AUTONOMOUS_MODE, redis_url="redis://r:6379/0"),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=lambda: db,
        skill_registry=registry_obj,
    )

    # Router catalog is internal-only: fetch-news is gone from the router (it is
    # discovered as a file), and the executor set is the internal kinds.
    assert [e.kind for e in runtime.gate._config.task_catalog] == [
        "meeting.leave",
        "session.end",
    ]
    assert runtime.gate._config.executor_kinds == INTERNAL_TOOL_KINDS
    # The agent carries the native sandbox tools, surfaced as provider tools.
    names = {d.name for d in (tools_to_definitions(list(runtime.agent.tools)) or [])}
    assert names == {"exec", "read", "write", "list_dir"}
    # Grounding moved to the openclaw tool recipe; the keyword capability block
    # (and the skill one-liner) is suppressed so the two never contradict.
    instructions = runtime.agent.instructions
    assert "list_dir('/skills')" in instructions
    assert "Never substitute a placeholder" in instructions
    assert "handled for you by background tools" not in instructions
    assert "- fetch-news:" not in instructions
    await runtime.aclose()
    assert db.closed is True


async def test_meet_backed_runtime_advertises_meeting_leave() -> None:
    """Surface scoping (Johnny-trt.57): a job with a calendar_event_id is a
    Meet-backed session — meeting.leave joins the catalog ahead of
    session.end, and the internal context carries the event linkage the
    voice dismissal posts against."""
    db = _FakeDbSession()
    runtime = await build_agent_runtime(
        _job(mode=AUTONOMOUS_MODE, calendar_event_id=31, meeting_config_id=5),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=lambda: db,
    )
    assert [entry.kind for entry in runtime.gate._config.task_catalog] == [
        "meeting.leave",
        "session.end",
    ]
    assert runtime.internal_tools is not None
    assert runtime.internal_tools.calendar_event_id == 31
    assert runtime.internal_tools.meeting_backed is True
    await runtime.aclose()
    assert db.closed is True


async def test_delegation_capable_runtime_catalogs_mcp_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third catalog source (Johnny-trt.36): enabled MCP servers' cached
    probe results become mcp__<server>__<tool> entries (filters applied,
    probe-failed servers unavailable-with-reason) and join executor_kinds.
    Read straight from the workspace's .johnny/.mcp.json (Johnny-hp1, no DB),
    so assembly never spends a second factory session on MCP."""
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from app.db import Base
    from johnny.mcp import store

    monkeypatch.setenv("JOHNNY_WORKSPACES_DIR", str(tmp_path))

    engine = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine)

    # MCP servers now live in the workspace's .johnny/.mcp.json keyed by slug
    # (Johnny-hp1); the _job stamp carries no workspace_id, so it resolves to
    # the default workspace's file. Probe verdicts + tool cache live alongside.
    def _entry(
        transport: str,
        *,
        enabled: bool = True,
        command: str = "",
        url: str = "",
        tool_exclude: list[str] | None = None,
    ) -> dict[str, Any]:
        return store.serialize_entry(
            transport=transport,
            enabled=enabled,
            command=command,
            args=[],
            env={},
            url=url,
            headers={},
            tool_include=None,
            tool_exclude=tool_exclude or [],
            connect_timeout_s=10.0,
            call_timeout_s=60.0,
            idle_ttl_s=300.0,
        )

    store.write_servers_raw(
        "default",
        {
            "fixture": _entry("stdio", command="python3", tool_exclude=["add"]),
            "downed": _entry("http", url="https://down.test/mcp"),
            "off": _entry("stdio", enabled=False, command="python3"),
        },
    )
    store.write_state(
        "default",
        "fixture",
        ok=True,
        error="",
        tools=[
            {"name": "echo", "description": "Echo a message."},
            {"name": "add", "description": "Add two numbers."},
        ],
    )
    store.write_state(
        "default",
        "downed",
        ok=False,
        error="connect refused",
        tools=[{"name": "search", "description": "Search things."}],
    )
    store.write_state(
        "default",
        "off",
        ok=True,
        error="",
        tools=[{"name": "ghost", "description": "Never appears."}],
    )

    sessions: list[Any] = []

    def factory() -> Any:
        sessions.append(maker())
        return sessions[-1]

    runtime = await build_agent_runtime(
        _job(mode=AUTONOMOUS_MODE, redis_url="redis://r:6379/0"),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=factory,
    )
    assert len(sessions) == 1  # the sinks' session; MCP reads the file, not the DB
    catalog = {entry.kind: entry for entry in runtime.gate._config.task_catalog}
    # Filter-surviving tool from the healthy server: available.
    assert catalog["mcp__fixture__echo"].available
    assert catalog["mcp__fixture__echo"].one_liner == "Echo a message."
    # The excluded tool and the disabled server's tools never appear.
    assert "mcp__fixture__add" not in catalog
    assert "mcp__off__ghost" not in catalog
    # Probe-failed server: unavailable-with-reason (Johnny-trt.55), not gone.
    downed = catalog["mcp__downed__search"]
    assert not downed.available
    assert "downed connector" in downed.unavailable_reason
    # Pre-ack membership truth (Johnny-trt.62) carries both reachable and
    # probe-failed kinds — the gate degrades the latter to a spoken decline.
    assert {"mcp__fixture__echo", "mcp__downed__search"} <= runtime.gate._config.executor_kinds
    assert "mcp__fixture__add" not in runtime.gate._config.executor_kinds
    await runtime.aclose()


# --- sandbox endpoint resolution (Johnny-trt.63, the Phase-7 seam) -----------


def test_session_sandbox_resolver_returns_the_global_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy snapshot (no workspace stamp) resolves to the global
    skills-sandbox — byte-identical pre-workspaces behavior (Johnny-wks.1
    re-keyed this seam by workspace)."""
    monkeypatch.delenv(SANDBOX_URL_ENV, raising=False)
    assert resolve_session_sandbox_url(_job(mode=AUTONOMOUS_MODE)) == DEFAULT_SANDBOX_URL
    monkeypatch.setenv(SANDBOX_URL_ENV, "http://sandbox-trt63:9999/")
    assert (
        resolve_session_sandbox_url(_job(mode=AUTONOMOUS_MODE))
        == "http://sandbox-trt63:9999"
    )


def test_session_sandbox_resolver_keys_by_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Johnny-wks.1 / Johnny-etu.5: the snapshot's workspace stamp keys the
    endpoint.

    EVERY stamped workspace → its own container's canonical endpoint, the
    DEFAULT (id 1) included now (lazy-launched like finance/ops). Only a
    legacy snapshot with no stamp falls back to the global URL (covered by
    :func:`test_session_sandbox_resolver_returns_the_global_url`)."""
    monkeypatch.setenv(SANDBOX_URL_ENV, "http://sandbox-global:8088")

    default_stamped = _job(
        mode=AUTONOMOUS_MODE,
        agent_snapshot={
            "workspace_id": 1,
            "workspace": {"id": 1, "name": "Default", "slug": "default", "is_default": True},
        },
    )
    assert resolve_session_sandbox_url(default_stamped) == "http://johnny-workspace-1:8088"

    finance = _job(
        mode=AUTONOMOUS_MODE,
        agent_snapshot={
            "workspace_id": 7,
            "workspace": {"id": 7, "name": "Finance", "slug": "finance", "is_default": False},
        },
    )
    assert resolve_session_sandbox_url(finance) == "http://johnny-workspace-7:8088"


def test_session_skills_dir_resolver_keys_by_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Johnny-wks.3 / Johnny-etu.5: catalog DISCOVERY is keyed by the same
    workspace stamp as the probes — EVERY stamped workspace scans its own
    packages dir, the DEFAULT (slug ``default``) included now; a legacy stamp
    with no workspace scans the shared volume; a stamp with no usable slug
    resolves to None (load nothing rather than guess)."""
    from johnny.agent.job_session import resolve_session_skills_dir
    from johnny.skills.sandbox import SKILLS_DIR_ENV, WORKSPACES_DIR_ENV

    monkeypatch.setenv(SKILLS_DIR_ENV, "/shared-skills")
    monkeypatch.setenv(WORKSPACES_DIR_ENV, "/ws-root")

    assert resolve_session_skills_dir(_job(mode=AUTONOMOUS_MODE)) == "/shared-skills"
    default_stamped = _job(
        mode=AUTONOMOUS_MODE,
        agent_snapshot={
            "workspace_id": 1,
            "workspace": {"id": 1, "name": "Default", "slug": "default", "is_default": True},
        },
    )
    assert resolve_session_skills_dir(default_stamped) == "/ws-root/default/.johnny/skills"
    finance = _job(
        mode=AUTONOMOUS_MODE,
        agent_snapshot={
            "workspace_id": 7,
            "workspace": {"id": 7, "name": "Finance", "slug": "finance", "is_default": False},
        },
    )
    assert resolve_session_skills_dir(finance) == "/ws-root/finance/.johnny/skills"
    slugless = _job(
        mode=AUTONOMOUS_MODE,
        agent_snapshot={"workspace_id": 9, "workspace": {"id": 9, "is_default": False}},
    )
    assert resolve_session_skills_dir(slugless) is None


async def test_default_sandbox_client_is_built_on_the_resolved_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delegation-capable assembly with no injected client builds it on
    :func:`resolve_session_sandbox_url`'s verdict — never on a hardcoded
    endpoint — so re-keying sandbox identity per agent (Phase 7) is one
    function away."""
    monkeypatch.setenv(SANDBOX_URL_ENV, "http://sandbox-trt63:9999")
    db = _FakeDbSession()
    runtime = await build_agent_runtime(
        _job(mode=AUTONOMOUS_MODE),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=lambda: db,
    )
    assert runtime._sandbox_client is not None
    assert runtime._sandbox_client.base_url == "http://sandbox-trt63:9999"
    await runtime.aclose()
    assert db.closed is True


async def test_assembly_catalog_derives_from_the_agents_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Johnny-wks.3 end-to-end at assembly level: a skill installed only in
    the Finance workspace is promised only to Finance-attached sessions, and
    the shared volume's skills never leak into them (and vice versa)."""

    def _write_skill(root: Any, name: str) -> None:
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: A {name} helper.\n---\nSteps.",
            encoding="utf-8",
        )

    shared = tmp_path / "shared-skills"
    shared.mkdir()
    _write_skill(shared, "sharedskill")
    _write_skill(tmp_path / "workspaces" / "finance" / ".johnny" / "skills", "financeskill")
    monkeypatch.setenv("JOHNNY_SKILLS_DIR", str(shared))
    monkeypatch.setenv("JOHNNY_WORKSPACES_DIR", str(tmp_path / "workspaces"))

    finance_job = _job(
        mode=AUTONOMOUS_MODE,
        agent_snapshot={
            "workspace_id": 7,
            "workspace": {"id": 7, "name": "Finance", "slug": "finance", "is_default": False},
        },
    )
    runtime = await build_agent_runtime(
        finance_job,
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=lambda: _FakeDbSession(),
    )
    assert runtime.skill_registry is not None
    assert runtime.skill_registry.kinds() == frozenset({"financeskill"})
    await runtime.aclose()

    default_runtime = await build_agent_runtime(
        _job(mode=AUTONOMOUS_MODE),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=lambda: _FakeDbSession(),
    )
    assert default_runtime.skill_registry is not None
    assert default_runtime.skill_registry.kinds() == frozenset({"sharedskill"})
    await default_runtime.aclose()


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
    assert runtime.skill_registry is None  # and no skills load either (trt.23)


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
    # only stage_error here — internal kinds included (trt.57).
    assert runtime.gate._config.task_catalog == ()
    # The executor-known set stays empty too (trt.62) — membership
    # validation is moot when every delegate verdict stage_errors anyway.
    assert runtime.gate._config.executor_kinds == frozenset()
    assert runtime.internal_tools is None


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


# --- per-agent LLM role split (Johnny-trt.42) --------------------------------


def _registry_with_second_llm() -> ProviderRegistry:
    reg = _registry()
    reg.register(Kind.LLM, "ollama", _FakeLLM)
    return reg


async def test_router_llm_entry_splits_gate_from_answer_provider() -> None:
    """A ``router_llm`` payload entry (the agent's triage pin) drives the gate
    + barge-in on a DIFFERENT provider than the answer coercion + reply node."""
    pc = _split_provider_config()
    pc["router_llm"] = {
        "provider_name": "ollama",
        "display_name": "Tiny triage",
        "credentials": {},
        "options": {},
    }
    runtime = await build_agent_runtime(
        _job(mode=AUTONOMOUS_MODE, character_prompt="Be Johnny.", provider_config=pc),
        event_bus=InMemoryEventBus(),
        registry=_registry_with_second_llm(),
    )

    assert runtime.gate._router_llm.name == "ollama"
    assert runtime.agent._barge_in is not None
    assert runtime.agent._barge_in._router_llm is runtime.gate._router_llm
    # Answer side: the coercion provider AND the session reply adapter stay
    # on the ``llm`` entry.
    assert runtime.agent._answer_llm is not None
    assert runtime.agent._answer_llm.name == "openai"
    assert runtime.adapters.llm.provider == "openai"
    assert runtime.gate._router_llm is not runtime.agent._answer_llm
    await runtime.aclose()


async def test_absent_router_llm_reuses_one_answer_instance() -> None:
    """No ``router_llm`` key (agent pins nothing / pins match) → the
    pre-trt.42 shape: one raw provider instance serves gate and coercion."""
    runtime = await build_agent_runtime(
        _job(mode=AUTONOMOUS_MODE, character_prompt="Be Johnny."),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
    )
    assert runtime.gate._router_llm is runtime.agent._answer_llm
    assert runtime.gate._router_llm.name == "openai"
    await runtime.aclose()


async def test_triage_timing_names_the_router_provider() -> None:
    """session_timings' router_llm stage must stamp the TRIAGE provider — the
    trt.42 acceptance's 'different providers for router vs answer stages'."""
    pc = _split_provider_config()
    pc["router_llm"] = {
        "provider_name": "ollama",
        "display_name": "Tiny triage",
        "credentials": {},
        "options": {},
    }
    bus = InMemoryEventBus()
    runtime = await build_agent_runtime(
        _job(mode=AUTONOMOUS_MODE, character_prompt="Be Johnny.", provider_config=pc),
        event_bus=bus,
        registry=_registry_with_second_llm(),
    )
    emit = runtime.gate._record_triage_timing
    assert emit is not None
    await emit("turn-1", 0.0, 0.0125, "speak")
    timing = next(
        e for e in bus.snapshot() if getattr(e, "type", "") == "pipeline_timing"
    )
    assert timing.stage == "router_llm"
    assert timing.provider_name == "ollama"
    await runtime.aclose()


async def test_reasoning_descriptor_reaches_the_task_sink() -> None:
    """The payload's credential-less ``reasoning_llm`` descriptor lands on the
    task sink, so every queued agent_tasks row records the requesting agent's
    reasoning model (Johnny-trt.42)."""
    pc = _split_provider_config()
    pc["reasoning_llm"] = {
        "provider_id": 9,
        "provider_name": "openai",
        "display_name": "Cloud reasoning",
        "model": "gpt-large",
    }
    db = _FakeDbSession()
    runtime = await build_agent_runtime(
        _job(mode=AUTONOMOUS_MODE, character_prompt="Be Johnny.", provider_config=pc),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=lambda: db,
    )
    sink = runtime.task_sink
    assert sink is not None
    assert sink._reasoning_llm == {
        "provider_id": 9,
        "provider_name": "openai",
        "display_name": "Cloud reasoning",
        "model": "gpt-large",
    }
    await runtime.aclose()


# --- speech floor wiring (Johnny-trt.46) -------------------------------------


async def test_meeting_scoped_runtime_builds_and_attaches_speech_floor() -> None:
    """A meeting-scoped session with Redis gets the shared floor: attached to
    the gate (speak-path gating), the agent (peer suppression), and carried
    on the runtime (deliverer wiring + teardown)."""
    db = _FakeDbSession()
    runtime = await build_agent_runtime(
        _job(
            mode=AUTONOMOUS_MODE,
            calendar_event_id=31,
            meeting_config_id=5,
            redis_url="redis://r:6379/0",
            agent_snapshot={"name": "Echo B", "mode": "autonomous"},
        ),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=lambda: db,
    )
    try:
        assert runtime.speech_floor is not None
        assert runtime.gate._floor is runtime.speech_floor
        assert runtime.agent._peer_floor is runtime.speech_floor
        # The holder identity peers will see is the snapshot's display name.
        assert runtime.speech_floor._agent_name == "Echo B"
    finally:
        await runtime.aclose()


async def test_playground_runtime_has_no_speech_floor() -> None:
    """No meeting_config_id (every playground session) → no floor anywhere:
    speak paths ungated, no floor events possible (the events.py contract)."""
    db = _FakeDbSession()
    runtime = await build_agent_runtime(
        _job(mode=AUTONOMOUS_MODE, redis_url="redis://r:6379/0"),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=lambda: db,
    )
    try:
        assert runtime.speech_floor is None
        assert runtime.gate._floor is None
        assert runtime.agent._peer_floor is None
    finally:
        await runtime.aclose()


async def test_meeting_without_redis_runs_floorless() -> None:
    """meeting_config_id but no Redis (smoke run): floor skipped, never a
    blocked speak path."""
    db = _FakeDbSession()
    runtime = await build_agent_runtime(
        _job(mode=AUTONOMOUS_MODE, calendar_event_id=31, meeting_config_id=5),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=lambda: db,
    )
    try:
        assert runtime.speech_floor is None
    finally:
        await runtime.aclose()


async def test_floor_scope_builds_floor_for_meetingless_group_member() -> None:
    """A playground-group member (Johnny-trt.48): no meeting_config_id, but a
    ``browser-group-*`` floor scope → the floor builds and keys the lock in
    the string namespace (collision-free with integer meeting scopes)."""
    db = _FakeDbSession()
    runtime = await build_agent_runtime(
        _job(
            mode=AUTONOMOUS_MODE,
            redis_url="redis://r:6379/0",
            agent_snapshot={"name": "Alex", "mode": "autonomous"},
        ),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=lambda: db,
        floor_scope="browser-group-7",
    )
    try:
        floor = runtime.speech_floor
        assert floor is not None
        assert runtime.gate._floor is floor
        assert runtime.agent._peer_floor is floor
        assert floor._agent_name == "Alex"
        assert floor._backend._lock_key == "johnny:floor:lock:meeting:browser-group-7"
    finally:
        await runtime.aclose()


async def test_injected_floor_backend_skips_redis() -> None:
    """The ensemble scenario seam: an injected backend builds the floor even
    with no redis_url at all (hermetic regression runs share one in-memory
    hub across members)."""
    from johnny.agent.speech_floor import InMemoryFloorBackend, InMemoryFloorHub

    db = _FakeDbSession()
    hub = InMemoryFloorHub()
    backend = InMemoryFloorBackend(hub)
    runtime = await build_agent_runtime(
        _job(mode=AUTONOMOUS_MODE, agent_snapshot={"name": "Echo", "mode": "autonomous"}),
        event_bus=InMemoryEventBus(),
        registry=_registry(),
        db_session_factory=lambda: db,
        floor_scope="browser-group-ensemble",
        floor_backend=backend,
    )
    try:
        floor = runtime.speech_floor
        assert floor is not None
        assert floor._backend is backend
    finally:
        await runtime.aclose()
