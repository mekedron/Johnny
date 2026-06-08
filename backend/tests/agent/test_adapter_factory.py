"""Integration tests for the adapter factory (Johnny-zb3).

Drives :func:`johnny.agent.adapters.factory.build_session_adapters` against a
seeded in-memory DB and a fake :class:`~app.providers.base.ProviderRegistry`,
asserting the contract the factory owns:

* it calls the UNCHANGED :func:`app.providers.loader.load_active_providers`
  (registry + DB config + decrypt) and wraps each active provider in the right
  LiveKit adapter (:class:`JohnnySTT` / :class:`JohnnyLLM` / :class:`JohnnyTTS`),
  with the decrypted credentials / options carried through to the provider;
* switching which row is active yields a different live adapter at the next
  build (no process restart, no registry edit);
* a missing active STT / LLM / TTS row fails fast with
  :class:`AgentSessionSetupError` instead of half-building a session;
* the factory is lazy-exported through the package ``__getattr__`` (so a bare
  ``import johnny.agent.adapters`` stays free of livekit + SQLAlchemy);
* the ``app.providers`` public surface the factory depends on is unchanged
  (golden API check).

Guarded by ``importorskip`` so the suite still collects where the ``agent``
extra (``livekit-agents``) is absent.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import ProviderCredential
from app.providers.base import (
    ChatMessage,
    LLMProvider,
    LLMResponse,
    ProviderConfig,
    ProviderKind,
    ProviderRegistry,
    STTProvider,
    ToolDefinition,
    TranscriptEvent,
    TTSProvider,
)
from app.providers.loader import load_active_providers

pytest.importorskip("livekit.agents")

from johnny.agent.adapters.factory import (  # noqa: E402
    AgentSessionSetupError,
    SessionAdapters,
    build_session_adapters,
)
from johnny.agent.adapters.johnny_llm import JohnnyLLM  # noqa: E402
from johnny.agent.adapters.johnny_stt import JohnnySTT  # noqa: E402
from johnny.agent.adapters.johnny_tts import JohnnyTTS  # noqa: E402

# --- Fake providers: record the config the loader hands them ----------------
# None of these stream — the factory only *constructs* the adapter around the
# provider, so the abstract methods exist purely to make the class concrete.


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


# --- DB fixture + row seeding (mirrors tests/providers/test_base.py) ---------


@pytest.fixture
def session() -> Session:
    engine = sa.create_engine("sqlite:///:memory:")
    # SQLAlchemy stubs type Model.__table__ as FromClause; runtime is Table.
    Base.metadata.create_all(engine, tables=[ProviderCredential.__table__])  # type: ignore[list-item]
    return Session(engine)


def _insert(
    session: Session,
    *,
    kind: ProviderKind,
    provider_name: str,
    credentials: dict[str, str],
    options: dict[str, Any] | None = None,
    is_active: bool = True,
    display_name: str | None = None,
) -> None:
    session.add(
        ProviderCredential(
            kind=kind,
            provider_name=provider_name,
            # (kind, provider_name, display_name) is unique — let callers vary
            # display_name when seeding two rows for the same kind/provider.
            display_name=display_name or f"{kind.value}:{provider_name}",
            credentials_encrypted=json.dumps(credentials),
            config=options or {},
            is_active=is_active,
        )
    )
    session.commit()


def _registry() -> ProviderRegistry:
    reg = ProviderRegistry()
    reg.register(ProviderKind.STT, "deepgram", _FakeSTT)
    reg.register(ProviderKind.LLM, "openai", _FakeLLM)
    reg.register(ProviderKind.TTS, "cartesia", _FakeTTS)
    return reg


def _seed_split(
    session: Session,
    *,
    stt: str = "deepgram",
    llm: str = "openai",
    tts: str = "cartesia",
) -> None:
    _insert(session, kind=ProviderKind.STT, provider_name=stt, credentials={"api_key": "s"})
    _insert(session, kind=ProviderKind.LLM, provider_name=llm, credentials={"api_key": "l"})
    _insert(session, kind=ProviderKind.TTS, provider_name=tts, credentials={"api_key": "t"})


# --- Tests ------------------------------------------------------------------


def test_builds_the_three_adapters_from_seeded_db(session: Session) -> None:
    _insert(
        session,
        kind=ProviderKind.STT,
        provider_name="deepgram",
        credentials={"api_key": "sk-stt"},
        options={"model": "nova-3"},
    )
    _insert(
        session,
        kind=ProviderKind.LLM,
        provider_name="openai",
        credentials={"api_key": "sk-llm"},
        options={"model": "gpt-4o"},
    )
    _insert(
        session,
        kind=ProviderKind.TTS,
        provider_name="cartesia",
        credentials={"api_key": "sk-tts"},
        options={"voice_id": "sonic"},
    )
    # An inactive row for the same kind must be ignored, not picked.
    _insert(
        session,
        kind=ProviderKind.STT,
        provider_name="deepgram",
        credentials={"api_key": "stale"},
        is_active=False,
        display_name="deepgram-inactive",
    )

    adapters = build_session_adapters(session, registry=_registry())

    assert isinstance(adapters, SessionAdapters)
    assert isinstance(adapters.stt, JohnnySTT)
    assert isinstance(adapters.llm, JohnnyLLM)
    assert isinstance(adapters.tts, JohnnyTTS)

    # The right provider was wrapped in each adapter...
    assert adapters.stt.provider == "deepgram"
    assert adapters.llm.provider == "openai"
    assert adapters.tts.provider == "cartesia"

    # ...and the decrypted credentials + options reached the provider.
    stt_provider = adapters.stt._provider
    assert isinstance(stt_provider, _FakeSTT)
    assert stt_provider.config.credentials == {"api_key": "sk-stt"}
    assert stt_provider.config.options == {"model": "nova-3"}

    llm_provider = adapters.llm._provider
    assert isinstance(llm_provider, _FakeLLM)
    assert llm_provider.config.credentials == {"api_key": "sk-llm"}

    tts_provider = adapters.tts._provider
    assert isinstance(tts_provider, _FakeTTS)
    assert tts_provider.config.options == {"voice_id": "sonic"}


def test_decrypt_callable_is_forwarded_to_loader(session: Session) -> None:
    # The row stores an opaque blob; only the injected decryptor knows how to
    # turn it into credentials. Proves the factory forwards `decrypt` to
    # load_active_providers and the decrypted secret reaches the provider.
    session.add(
        ProviderCredential(
            kind=ProviderKind.STT,
            provider_name="deepgram",
            display_name="stt",
            credentials_encrypted="opaque-fernet-token",
            config={},
            is_active=True,
        )
    )
    _insert(session, kind=ProviderKind.LLM, provider_name="openai", credentials={"api_key": "l"})
    _insert(session, kind=ProviderKind.TTS, provider_name="cartesia", credentials={"api_key": "t"})

    seen: list[str] = []

    def fake_decrypt(blob: str) -> dict[str, str]:
        seen.append(blob)
        return {"api_key": "DECRYPTED"}

    adapters = build_session_adapters(session, registry=_registry(), decrypt=fake_decrypt)

    assert "opaque-fernet-token" in seen
    stt_provider = adapters.stt._provider
    assert isinstance(stt_provider, _FakeSTT)
    assert stt_provider.config.credentials == {"api_key": "DECRYPTED"}


def test_switching_active_provider_yields_a_different_adapter(session: Session) -> None:
    reg = _registry()
    reg.register(ProviderKind.STT, "elevenlabs", _FakeSTT)

    _seed_split(session, stt="deepgram")
    first = build_session_adapters(session, registry=reg)
    assert first.stt.provider == "deepgram"

    # Operator switches the active STT provider in admin: deactivate the old
    # row, activate a new one.
    session.execute(
        sa.update(ProviderCredential)
        .where(ProviderCredential.kind == ProviderKind.STT)
        .values(is_active=False)
    )
    _insert(
        session,
        kind=ProviderKind.STT,
        provider_name="elevenlabs",
        credentials={"api_key": "new"},
    )
    session.commit()

    second = build_session_adapters(session, registry=reg)

    # Next session start -> a different live adapter, same untouched registry.
    assert second.stt.provider == "elevenlabs"
    assert second.stt is not first.stt


@pytest.mark.parametrize("omit", [ProviderKind.STT, ProviderKind.LLM, ProviderKind.TTS])
def test_missing_required_kind_fails_fast(session: Session, omit: ProviderKind) -> None:
    rows = {
        ProviderKind.STT: "deepgram",
        ProviderKind.LLM: "openai",
        ProviderKind.TTS: "cartesia",
    }
    for kind, name in rows.items():
        if kind is omit:
            continue
        _insert(session, kind=kind, provider_name=name, credentials={"api_key": "k"})

    with pytest.raises(AgentSessionSetupError) as excinfo:
        build_session_adapters(session, registry=_registry())

    # The error names the missing stage so a misconfiguration is obvious.
    assert omit.value in str(excinfo.value)


def test_empty_db_fails_fast(session: Session) -> None:
    with pytest.raises(AgentSessionSetupError):
        build_session_adapters(session, registry=_registry())


def test_active_s2s_only_is_not_enough(session: Session) -> None:
    # Unified mode rows do not satisfy the split factory (it scopes the loader
    # to STT/LLM/TTS); it must still fail fast rather than load the s2s row.
    _insert(session, kind=ProviderKind.S2S, provider_name="openai_realtime", credentials={"k": "v"})

    with pytest.raises(AgentSessionSetupError) as excinfo:
        build_session_adapters(session, registry=_registry())
    assert ProviderKind.STT.value in str(excinfo.value)


def test_misregistered_factory_wrong_type_raises(session: Session) -> None:
    # A factory registered under STT that returns a non-STTProvider is a
    # registry misconfiguration; the factory's isinstance guard catches it.
    reg = _registry()
    reg.register(ProviderKind.STT, "broken", _FakeLLM, replace=True)
    _seed_split(session, stt="broken")

    with pytest.raises(AgentSessionSetupError) as excinfo:
        build_session_adapters(session, registry=reg)
    assert "STTProvider" in str(excinfo.value)


def test_factory_is_lazy_exported_through_package() -> None:
    import johnny.agent.adapters as adapters
    from johnny.agent.adapters import factory

    assert adapters.build_session_adapters is factory.build_session_adapters
    assert adapters.SessionAdapters is factory.SessionAdapters
    assert adapters.AgentSessionSetupError is factory.AgentSessionSetupError

    with pytest.raises(AttributeError):
        _ = adapters.does_not_exist


def test_providers_public_surface_unchanged() -> None:
    # Golden API check: the factory must consume load_active_providers exactly
    # as published, and the registry/ABC surface it relies on stays intact —
    # the factory is built ON the providers package, never edits it.
    sig = inspect.signature(load_active_providers)
    params = list(sig.parameters.values())
    assert params[0].name == "session"
    assert params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    keyword_only = {
        p.name for p in params if p.kind is inspect.Parameter.KEYWORD_ONLY
    }
    assert keyword_only == {"registry", "decrypt", "kinds"}

    import app.providers.base as base

    for name in (
        "ProviderKind",
        "ProviderRegistry",
        "ProviderInstance",
        "STTProvider",
        "LLMProvider",
        "TTSProvider",
        "get_registry",
    ):
        assert name in base.__all__
