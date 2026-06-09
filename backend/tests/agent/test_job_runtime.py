"""Consumer-side threading: a dispatched ``SessionJobConfig`` → worker pieces (Johnny-7we).

Two layers:

* unit tests for the pure mappers in :mod:`johnny.agent.job_runtime`
  (:func:`instructions_config_from_job` / :func:`answer_config_from_job` /
  :func:`build_session_adapters_for_job`);
* the **acceptance round trip** — a session configured with provider/personality X
  yields adapters + instructions X *inside the worker*. The test drives the REAL API
  assembly (:func:`app.services.provider_payload.build_provider_payload` +
  :func:`app.services.personality_resolver.apply_personality`), the REAL producer
  (:func:`app.services.agent_dispatch.session_job_config_from_launch_context`), the
  REAL dispatch serialisation (``to_metadata`` → ``from_metadata``), and the REAL
  consumer (the job-runtime builders), so nothing about the threading is faked.

Guarded by ``importorskip`` so the suite still collects without the ``agent`` extra.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import (
    BotMode,
    MeetingConfig,
    Personality,
    ProviderCredential,
    ProviderKind,
)
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
from app.security.crypto import CredentialCrypto, encrypt_json
from app.services.personality_resolver import apply_personality, select_personality
from app.services.provider_payload import build_provider_payload
from johnny.agent.job_config import (
    APPROVAL_REQUIRED_MODE,
    SUGGEST_ONLY_MODE,
    UNIFIED_PIPELINE_MODE,
    SessionJobConfig,
)

pytest.importorskip("livekit.agents")

from johnny.agent.adapters.factory import (  # noqa: E402
    AgentSessionSetupError,
    SessionAdapters,
)
from johnny.agent.adapters.johnny_llm import JohnnyLLM  # noqa: E402
from johnny.agent.adapters.johnny_stt import JohnnySTT  # noqa: E402
from johnny.agent.adapters.johnny_tts import JohnnyTTS  # noqa: E402
from johnny.agent.job_runtime import (  # noqa: E402
    answer_config_from_job,
    build_session_adapters_for_job,
    instructions_config_from_job,
)
from johnny.agent.session import build_agent_instructions  # noqa: E402

# --- Fake providers (record the config the registry hands them) -------------


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
    reg.register(ProviderKind.STT, "deepgram", _FakeSTT)
    reg.register(ProviderKind.LLM, "openai", _FakeLLM)
    reg.register(ProviderKind.LLM, "anthropic", _FakeLLM)
    reg.register(ProviderKind.TTS, "cartesia", _FakeTTS)
    return reg


def _entry(provider_name: str, **extra: Any) -> dict[str, Any]:
    base = {
        "provider_name": provider_name,
        "display_name": provider_name,
        "credentials": {"api_key": "k"},
        "options": {},
    }
    base.update(extra)
    return base


def _split_provider_config() -> dict[str, Any]:
    return {
        "stt": _entry("deepgram"),
        "llm": _entry("openai"),
        "tts": _entry("cartesia"),
    }


def _job(**overrides: Any) -> SessionJobConfig:
    fields: dict[str, Any] = {
        "bot_session_id": 7,
        "room_name": "johnny-session-7",
        "provider_config": _split_provider_config(),
    }
    fields.update(overrides)
    return SessionJobConfig(**fields)


# --- instructions_config_from_job -------------------------------------------


def test_instructions_config_copies_all_prompt_fields() -> None:
    job = _job(
        instructions="Be brief.",
        personality_prompt="[personality: Aria]\nWarm and curious.",
        context="Quarterly sync.",
        calendar_context="Q3 planning",
        calendar_attachments_text="doc body",
        prior_session_context="Last week we agreed X.",
    )

    cfg = instructions_config_from_job(job)

    assert cfg.instructions == "Be brief."
    assert cfg.personality_prompt == "[personality: Aria]\nWarm and curious."
    assert cfg.context == "Quarterly sync."
    assert cfg.calendar_context == "Q3 planning"
    assert cfg.calendar_attachments_text == "doc body"
    assert cfg.prior_session_context == "Last week we agreed X."

    # The rendered system prompt carries the personality identity.
    system = build_agent_instructions(cfg)
    assert "Aria" in system
    assert "Warm and curious." in system


def test_instructions_config_defaults_are_empty() -> None:
    cfg = instructions_config_from_job(_job())
    assert cfg.instructions == ""
    assert cfg.personality_prompt == ""
    assert cfg.prior_session_context == ""


# --- answer_config_from_job -------------------------------------------------


@pytest.mark.parametrize("mode", [SUGGEST_ONLY_MODE, APPROVAL_REQUIRED_MODE])
def test_answer_config_threads_mode(mode: str) -> None:
    cfg = answer_config_from_job(_job(mode=mode))
    assert cfg.mode == mode
    # allowed_replies is not part of the dispatch contract -> stays empty.
    assert cfg.allowed_replies == ()


# --- build_session_adapters_for_job -----------------------------------------


def test_build_adapters_for_job_split() -> None:
    adapters = build_session_adapters_for_job(_job(), registry=_registry())

    assert isinstance(adapters, SessionAdapters)
    assert isinstance(adapters.stt, JohnnySTT)
    assert isinstance(adapters.llm, JohnnyLLM)
    assert isinstance(adapters.tts, JohnnyTTS)
    assert adapters.stt.provider == "deepgram"
    assert adapters.llm.provider == "openai"
    assert adapters.tts.provider == "cartesia"


def test_build_adapters_for_job_honours_payload_override() -> None:
    # The job runtime builds from the payload's provider_config, so a payload whose
    # llm entry was overridden (personality, API-side) yields that provider.
    pc = _split_provider_config()
    pc["llm"] = _entry("anthropic", options={"model": "claude"})

    adapters = build_session_adapters_for_job(_job(provider_config=pc), registry=_registry())

    assert adapters.llm.provider == "anthropic"
    assert adapters.llm.model == "claude"


def test_build_adapters_for_job_rejects_unified() -> None:
    job = _job(pipeline_mode=UNIFIED_PIPELINE_MODE)
    with pytest.raises(AgentSessionSetupError) as excinfo:
        build_session_adapters_for_job(job, registry=_registry())
    assert "unified" in str(excinfo.value).lower()


# --- Acceptance: provider/personality X -> adapters/instructions X ----------


@pytest.fixture
def crypto() -> CredentialCrypto:
    return CredentialCrypto(Fernet.generate_key())


@pytest.fixture
def db() -> Session:
    engine = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            ProviderCredential.__table__,  # type: ignore[list-item]
            Personality.__table__,  # type: ignore[list-item]
        ],
    )
    return Session(engine)


def _seed_provider(
    db: Session,
    crypto: CredentialCrypto,
    *,
    kind: ProviderKind,
    provider_name: str,
    credentials: dict[str, str],
    options: dict[str, Any] | None = None,
) -> ProviderCredential:
    row = ProviderCredential(
        kind=kind,
        provider_name=provider_name,
        display_name=f"{kind.value}:{provider_name}",
        credentials_encrypted=encrypt_json(crypto, credentials),
        config=options or {},
        is_active=True,
    )
    db.add(row)
    db.flush()
    return row


def test_session_config_round_trips_provider_personality_mode(
    db: Session, crypto: CredentialCrypto
) -> None:
    # 1. Seed the admin-active split stack + a meeting personality "Aria" that
    #    points at the active LLM/TTS rows (the realistic v1 shape) and carries a
    #    persona description + a default mode.
    _seed_provider(
        db,
        crypto,
        kind=ProviderKind.STT,
        provider_name="deepgram",
        credentials={"api_key": "stt-secret"},
        options={"model": "nova-3", "language": "en-US"},
    )
    llm_row = _seed_provider(
        db,
        crypto,
        kind=ProviderKind.LLM,
        provider_name="openai",
        credentials={"api_key": "llm-secret"},
        options={"model": "gpt-4o"},
    )
    tts_row = _seed_provider(
        db,
        crypto,
        kind=ProviderKind.TTS,
        provider_name="cartesia",
        credentials={"api_key": "tts-secret"},
        options={"voice_id": "sonic"},
    )
    aria = Personality(
        display_name="Aria",
        description="Warm, curious, and concise.",
        is_default=True,
        llm_provider_id=llm_row.id,
        tts_provider_id=tts_row.id,
        default_mode=BotMode.SUGGEST_ONLY,
    )
    db.add(aria)
    db.flush()

    meeting = MeetingConfig(personality_id=aria.id, mode=BotMode.SUGGEST_ONLY)

    # 2. Run the REAL API assembly: payload + personality resolution.
    base_payload = build_provider_payload(db, crypto)
    personality = select_personality(db, requested_id=None, meeting=meeting)
    assert personality is not None and personality.display_name == "Aria"
    resolution = apply_personality(db, base_payload, personality, crypto=crypto)

    # 3. Build the launch context the scheduler would, then run the REAL producer.
    from app.services.agent_dispatch import session_job_config_from_launch_context
    from app.services.session_scheduler import LaunchContext

    ctx = LaunchContext(
        bot_session_id=42,
        meeting_config_id=11,
        calendar_event_id=99,
        identity_account_id=3,
        meet_link="https://meet.example/abc",
        container_name="meet-worker-session-42",
        mode=str(meeting.mode.value),
        instructions="Stick to the agenda.",
        personality_prompt=resolution.personality_prompt,
        context="Internal sync.",
        provider_config=resolution.payload,
        pipeline_mode="split",
    )
    config = session_job_config_from_launch_context(ctx, redis_url="redis://r:6379/0")

    # 4. Cross the dispatch wire exactly as LiveKit would (metadata round trip).
    rehydrated = SessionJobConfig.from_metadata(config.to_metadata())

    # 5. Consume it inside the "worker": the configured providers + the personality
    #    identity + the meeting mode all survive end-to-end.
    adapters = build_session_adapters_for_job(rehydrated, registry=_registry())
    assert adapters.tts is not None
    assert adapters.stt.provider == "deepgram"
    assert adapters.llm.provider == "openai"
    assert adapters.tts.provider == "cartesia"
    # The operator's selections (model/voice/language) rode through the payload.
    assert isinstance(adapters.stt, JohnnySTT)
    assert adapters.stt.model == "nova-3"
    assert adapters.stt._language == "en-US"
    assert adapters.llm.model == "gpt-4o"
    assert adapters.tts._voice == "sonic"
    # The decrypted credentials reached the wrapped provider DB-free.
    llm_provider = adapters.llm._provider
    assert isinstance(llm_provider, _FakeLLM)
    assert llm_provider.config.credentials == {"api_key": "llm-secret"}

    system = build_agent_instructions(instructions_config_from_job(rehydrated))
    assert "Aria" in system
    assert "Warm, curious, and concise." in system
    assert "Stick to the agenda." in system

    assert answer_config_from_job(rehydrated).mode == SUGGEST_ONLY_MODE
    # Approval / event-bus wiring rides along too.
    assert rehydrated.redis_url == "redis://r:6379/0"
    assert rehydrated.room_name == "johnny-session-42"


def test_round_trip_metadata_is_plain_json(db: Session, crypto: CredentialCrypto) -> None:
    # Sanity that the producer output is wire-serialisable as LiveKit metadata
    # (a JSON string) and decodes to the personality-resolved provider set.
    _seed_provider(
        db, crypto, kind=ProviderKind.LLM, provider_name="openai", credentials={"api_key": "k"}
    )
    base_payload = build_provider_payload(db, crypto)

    from app.services.agent_dispatch import session_job_config_from_launch_context
    from app.services.session_scheduler import LaunchContext

    ctx = LaunchContext(
        bot_session_id=5,
        meeting_config_id=1,
        calendar_event_id=1,
        identity_account_id=1,
        meet_link="",
        container_name="c",
        mode="approval_required",
        provider_config=base_payload,
    )
    metadata = session_job_config_from_launch_context(ctx).to_metadata()

    decoded = json.loads(metadata)
    assert decoded["mode"] == "approval_required"
    assert decoded["room_name"] == "johnny-session-5"
    assert decoded["provider_config"]["llm"]["provider_name"] == "openai"
