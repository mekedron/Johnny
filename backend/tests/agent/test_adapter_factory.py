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
* a missing active STT or LLM row fails fast with
  :class:`AgentSessionSetupError` instead of half-building a session, while a
  missing TTS degrades to ``adapters.tts = None`` (Johnny-un2) rather than failing;
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

from livekit.agents.llm import (  # noqa: E402
    ChatChunk,
    ChatContext,
    function_tool,
)
from livekit.agents.llm.chat_context import (  # noqa: E402
    ChatMessage as LKChatMessage,
)
from livekit.agents.stt import StreamAdapter  # noqa: E402
from livekit.agents.vad import VAD, VADCapabilities, VADStream  # noqa: E402

from app.providers.base import UnknownProviderError  # noqa: E402
from johnny.agent.adapters.factory import (  # noqa: E402
    AgentSessionSetupError,
    SessionAdapters,
    build_session_adapters,
    build_session_adapters_from_payload,
)
from johnny.agent.adapters.johnny_llm import JohnnyLLM  # noqa: E402
from johnny.agent.adapters.johnny_stt import JohnnySTT  # noqa: E402
from johnny.agent.adapters.johnny_tts import JohnnyTTS  # noqa: E402


class _NullVAD(VAD):
    """Minimal VAD for type-only assertions — its stream is never driven here.

    The factory only hands this to :class:`StreamAdapter` (which stores it
    without streaming), so the StreamAdapter-vs-JohnnySTT classification can be
    checked without loading a real Silero model. Driving the VAD-segmentation
    behaviour lives in ``test_stt_stream_adapter.py``.
    """

    def __init__(self) -> None:
        super().__init__(capabilities=VADCapabilities(update_interval=0.1))

    def stream(self) -> VADStream:  # pragma: no cover - not exercised here
        raise NotImplementedError

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
        self.received_tools: list[ToolDefinition] | None = None

    @property
    def name(self) -> str:
        return self.config.provider_name

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.received_tools = list(tools) if tools is not None else None
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
    # deepgram streams -> a bare JohnnySTT (not a StreamAdapter). Bind a local
    # so its provider-specific surface stays narrowed for the assertions below.
    stt_adapter = adapters.stt
    assert isinstance(stt_adapter, JohnnySTT)
    assert isinstance(adapters.llm, JohnnyLLM)
    assert isinstance(adapters.tts, JohnnyTTS)

    # The right provider was wrapped in each adapter...
    assert stt_adapter.provider == "deepgram"
    assert adapters.llm.provider == "openai"
    assert adapters.tts.provider == "cartesia"

    # ...and the decrypted credentials + options reached the provider.
    stt_provider = stt_adapter._provider
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
    stt_adapter = adapters.stt
    assert isinstance(stt_adapter, JohnnySTT)
    stt_provider = stt_adapter._provider
    assert isinstance(stt_provider, _FakeSTT)
    assert stt_provider.config.credentials == {"api_key": "DECRYPTED"}


def test_switching_active_provider_yields_a_different_adapter(session: Session) -> None:
    reg = _registry()
    reg.register(ProviderKind.STT, "elevenlabs", _FakeSTT)

    _seed_split(session, stt="deepgram")
    first = build_session_adapters(session, registry=reg, vad=_NullVAD())
    assert first.stt.provider == "deepgram"
    # deepgram streams -> driven directly as a bare JohnnySTT.
    assert isinstance(first.stt, JohnnySTT)

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

    second = build_session_adapters(session, registry=reg, vad=_NullVAD())

    # Next session start -> a different live adapter, same untouched registry.
    # elevenlabs is batch-only, so the switch also flips the STT surface from a
    # bare JohnnySTT to a VAD-buffered StreamAdapter (Johnny-4fn).
    assert second.stt.provider == "elevenlabs"
    assert second.stt is not first.stt
    assert isinstance(second.stt, StreamAdapter)


def test_active_batch_stt_is_vad_wrapped(session: Session) -> None:
    reg = _registry()
    reg.register(ProviderKind.STT, "faster-whisper", _FakeSTT)
    _seed_split(session, stt="faster-whisper")

    adapters = build_session_adapters(session, registry=reg, vad=_NullVAD())

    assert isinstance(adapters.stt, StreamAdapter)
    assert isinstance(adapters.stt.wrapped_stt, JohnnySTT)
    assert adapters.stt.provider == "faster-whisper"


def test_active_streaming_stt_is_not_wrapped(session: Session) -> None:
    # deepgram streams natively -> a bare JohnnySTT, no VAD needed even when one
    # is available.
    _seed_split(session, stt="deepgram")

    adapters = build_session_adapters(session, registry=_registry(), vad=_NullVAD())

    assert isinstance(adapters.stt, JohnnySTT)
    assert not isinstance(adapters.stt, StreamAdapter)


@pytest.mark.parametrize("omit", [ProviderKind.STT, ProviderKind.LLM])
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


def test_missing_tts_degrades_to_none(session: Session) -> None:
    # TTS is optional (Johnny-un2): STT + LLM active but no TTS row -> the session
    # binds no TTS (adapters.tts is None) and the worker degrades to suggest_only,
    # instead of the fail-fast the required kinds get.
    _insert(session, kind=ProviderKind.STT, provider_name="deepgram", credentials={"api_key": "k"})
    _insert(session, kind=ProviderKind.LLM, provider_name="openai", credentials={"api_key": "k"})

    adapters = build_session_adapters(session, registry=_registry())

    assert isinstance(adapters.stt, JohnnySTT)
    assert isinstance(adapters.llm, JohnnyLLM)
    assert adapters.tts is None


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


async def _collect_chunks(stream: Any) -> list[ChatChunk]:
    chunks: list[ChatChunk] = []
    async with stream:
        async for chunk in stream:
            chunks.append(chunk)
    return chunks


def test_selected_voice_model_language_propagate_into_adapters(
    session: Session,
) -> None:
    # The operator's admin selections (voice + model + language) must reach the
    # built adapters so the session uses — and reports — exactly them (Johnny-88n).
    _insert(
        session,
        kind=ProviderKind.STT,
        provider_name="deepgram",
        credentials={"api_key": "s"},
        options={"model": "nova-3", "language": "en-US"},
    )
    _insert(
        session,
        kind=ProviderKind.LLM,
        provider_name="openai",
        credentials={"api_key": "l"},
        options={"model": "gpt-4o"},
    )
    _insert(
        session,
        kind=ProviderKind.TTS,
        provider_name="cartesia",
        credentials={"api_key": "t"},
        options={"voice_id": "sonic", "model_id": "sonic-2024"},
    )

    adapters = build_session_adapters(session, registry=_registry())

    # deepgram streams -> a bare JohnnySTT; narrow for the model/language probe.
    stt_adapter = adapters.stt
    assert isinstance(stt_adapter, JohnnySTT)
    assert stt_adapter.model == "nova-3"
    assert stt_adapter._language == "en-US"

    assert adapters.llm.model == "gpt-4o"

    # voice_id is passed through explicitly (not left to the provider default),
    # and the TTS model label resolves via the model_id fallback key.
    assert adapters.tts is not None
    assert adapters.tts._voice == "sonic"
    assert adapters.tts.model == "sonic-2024"


def test_unset_selections_fall_back_to_provider_defaults(session: Session) -> None:
    # No voice/model/language in any config row: the adapters override nothing
    # (voice=None -> provider default), and the labels read "unknown".
    _seed_split(session)

    adapters = build_session_adapters(session, registry=_registry())

    stt_adapter = adapters.stt
    assert isinstance(stt_adapter, JohnnySTT)
    assert stt_adapter.model == "unknown"
    assert stt_adapter._language is None

    assert adapters.llm.model == "unknown"

    assert adapters.tts is not None
    assert adapters.tts._voice is None
    assert adapters.tts.model == "unknown"


@pytest.mark.parametrize(
    ("provider_name", "options", "expected_model", "expected_language"),
    [
        # ElevenLabs Scribe stores its selection under model_id / language_code.
        (
            "elevenlabs",
            {"model_id": "scribe_v2", "language_code": "fi"},
            "scribe_v2",
            "fi",
        ),
        # faster-whisper names its model option model_size.
        (
            "faster-whisper",
            {"model_size": "small", "language": "sv"},
            "small",
            "sv",
        ),
    ],
)
def test_heterogeneous_stt_config_keys_are_read(
    session: Session,
    provider_name: str,
    options: dict[str, Any],
    expected_model: str,
    expected_language: str,
) -> None:
    # The split STT providers use non-uniform config keys for model/language;
    # the factory's candidate-key lookup resolves each so the label is correct
    # regardless of which provider is active. Both of these are batch-only, so
    # the STT surface is a VAD-buffered StreamAdapter wrapping a JohnnySTT.
    reg = _registry()
    reg.register(ProviderKind.STT, provider_name, _FakeSTT)
    _insert(
        session,
        kind=ProviderKind.STT,
        provider_name=provider_name,
        credentials={"api_key": "s"},
        options=options,
    )
    _insert(session, kind=ProviderKind.LLM, provider_name="openai", credentials={"api_key": "l"})
    _insert(session, kind=ProviderKind.TTS, provider_name="cartesia", credentials={"api_key": "t"})

    adapters = build_session_adapters(session, registry=reg, vad=_NullVAD())

    assert isinstance(adapters.stt, StreamAdapter)
    wrapped = adapters.stt.wrapped_stt
    assert isinstance(wrapped, JohnnySTT)
    assert wrapped.model == expected_model
    assert wrapped._language == expected_language


async def test_configured_tools_propagate_through_factory_llm_adapter(
    session: Session,
) -> None:
    # Tools are not configured in admin and are not a factory-construction
    # concern — in a LiveKit session they come from the Agent per turn. Parity
    # means the factory-built JohnnyLLM forwards them to LLMProvider.chat: drive
    # a real function_tool through the built adapter and assert it lands on the
    # wrapped provider as a Johnny ToolDefinition.
    _seed_split(session)
    adapters = build_session_adapters(session, registry=_registry())
    llm_provider = adapters.llm._provider
    assert isinstance(llm_provider, _FakeLLM)

    @function_tool
    async def get_weather(location: str) -> str:
        """Look up the weather for a location."""
        return "sunny"

    ctx = ChatContext(items=[LKChatMessage(role="user", content=["weather?"])])
    await _collect_chunks(adapters.llm.chat(chat_ctx=ctx, tools=[get_weather]))

    assert llm_provider.received_tools is not None
    assert [tool.name for tool in llm_provider.received_tools] == ["get_weather"]


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


# --- build_session_adapters_from_payload (Johnny-7we) -----------------------
# The DB-free sibling: the dispatched agent worker rebuilds the same three
# adapters from the ``provider_config`` payload (the personality-resolved
# ``{kind: {provider_name, display_name, credentials, options}}`` dict the API
# serialised into the job metadata) instead of querying the DB. These mirror the
# DB-path tests above against the payload entry point.


def _entry(
    *,
    provider_name: str,
    credentials: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    return {
        "provider_name": provider_name,
        "display_name": display_name or provider_name,
        "credentials": credentials or {"api_key": "k"},
        "options": options or {},
    }


def _split_payload(
    *,
    stt: str = "deepgram",
    llm: str = "openai",
    tts: str = "cartesia",
) -> dict[str, Any]:
    return {
        "stt": _entry(provider_name=stt),
        "llm": _entry(provider_name=llm),
        "tts": _entry(provider_name=tts),
    }


def test_payload_builds_the_three_adapters() -> None:
    payload = {
        "stt": _entry(
            provider_name="deepgram",
            credentials={"api_key": "sk-stt"},
            options={"model": "nova-3", "language": "en-US"},
        ),
        "llm": _entry(
            provider_name="openai",
            credentials={"api_key": "sk-llm"},
            options={"model": "gpt-4o"},
        ),
        "tts": _entry(
            provider_name="cartesia",
            credentials={"api_key": "sk-tts"},
            options={"voice_id": "sonic", "model_id": "sonic-2024"},
        ),
    }

    adapters = build_session_adapters_from_payload(payload, registry=_registry())

    assert isinstance(adapters, SessionAdapters)
    stt_adapter = adapters.stt
    assert isinstance(stt_adapter, JohnnySTT)  # deepgram streams -> bare JohnnySTT
    assert isinstance(adapters.llm, JohnnyLLM)
    assert isinstance(adapters.tts, JohnnyTTS)

    # Right provider wrapped in each adapter...
    assert stt_adapter.provider == "deepgram"
    assert adapters.llm.provider == "openai"
    assert adapters.tts.provider == "cartesia"

    # ...credentials + options carried straight from the payload entry...
    stt_provider = stt_adapter._provider
    assert isinstance(stt_provider, _FakeSTT)
    assert stt_provider.config.credentials == {"api_key": "sk-stt"}
    assert stt_provider.config.options == {"model": "nova-3", "language": "en-US"}
    llm_provider = adapters.llm._provider
    assert isinstance(llm_provider, _FakeLLM)
    assert llm_provider.config.credentials == {"api_key": "sk-llm"}

    # ...and the operator's voice/model/language selections reach the adapters.
    assert stt_adapter.model == "nova-3"
    assert stt_adapter._language == "en-US"
    assert adapters.llm.model == "gpt-4o"
    assert adapters.tts._voice == "sonic"
    assert adapters.tts.model == "sonic-2024"


def test_payload_personality_override_drives_the_llm_adapter() -> None:
    # The whole point of building from the payload (not the DB): a personality
    # that overrode the LLM provider yields *that* provider in the adapter, even
    # though the DB's globally-active LLM row is a different one. apply_personality
    # has already swapped the "llm" entry on the API side, so the worker honours it.
    reg = _registry()
    reg.register(ProviderKind.LLM, "anthropic", _FakeLLM)
    payload = _split_payload()
    payload["llm"] = _entry(
        provider_name="anthropic",
        credentials={"api_key": "persona-key"},
        options={"model": "claude"},
    )

    adapters = build_session_adapters_from_payload(payload, registry=reg)

    assert adapters.llm.provider == "anthropic"
    assert adapters.llm.model == "claude"
    llm_provider = adapters.llm._provider
    assert isinstance(llm_provider, _FakeLLM)
    assert llm_provider.config.credentials == {"api_key": "persona-key"}


def test_payload_batch_stt_is_vad_wrapped() -> None:
    reg = _registry()
    reg.register(ProviderKind.STT, "faster-whisper", _FakeSTT)
    payload = _split_payload(stt="faster-whisper")

    adapters = build_session_adapters_from_payload(payload, registry=reg, vad=_NullVAD())

    assert isinstance(adapters.stt, StreamAdapter)
    assert isinstance(adapters.stt.wrapped_stt, JohnnySTT)
    assert adapters.stt.provider == "faster-whisper"


def test_payload_streaming_stt_is_not_wrapped() -> None:
    adapters = build_session_adapters_from_payload(
        _split_payload(stt="deepgram"), registry=_registry(), vad=_NullVAD()
    )
    assert isinstance(adapters.stt, JohnnySTT)
    assert not isinstance(adapters.stt, StreamAdapter)


@pytest.mark.parametrize("omit", ["stt", "llm"])
def test_payload_missing_required_kind_fails_fast(omit: str) -> None:
    payload = _split_payload()
    del payload[omit]

    with pytest.raises(AgentSessionSetupError) as excinfo:
        build_session_adapters_from_payload(payload, registry=_registry())
    assert omit in str(excinfo.value)


def test_payload_missing_tts_degrades_to_none() -> None:
    # TTS is optional (Johnny-un2): an absent TTS entry yields adapters.tts is None
    # (no fail-fast), so the worker can degrade a speaking mode to suggest_only.
    payload = _split_payload()
    del payload["tts"]

    adapters = build_session_adapters_from_payload(payload, registry=_registry())

    assert isinstance(adapters.stt, JohnnySTT)
    assert isinstance(adapters.llm, JohnnyLLM)
    assert adapters.tts is None


def test_payload_blank_tts_degrades_to_none() -> None:
    # A blank TTS provider_name reads as "no TTS configured" -> degrade, not raise.
    payload = _split_payload()
    payload["tts"] = {"provider_name": "  ", "credentials": {}, "options": {}}

    adapters = build_session_adapters_from_payload(payload, registry=_registry())

    assert adapters.tts is None


def test_payload_blank_provider_name_fails_fast() -> None:
    # A blank provider_name on a *required* kind (LLM) is still a fail-fast — only
    # TTS treats blank/absent as a degrade.
    payload = _split_payload()
    payload["llm"] = {"provider_name": "  ", "credentials": {}, "options": {}}

    with pytest.raises(AgentSessionSetupError) as excinfo:
        build_session_adapters_from_payload(payload, registry=_registry())
    assert "llm" in str(excinfo.value)
    assert "provider_name" in str(excinfo.value)


def test_payload_empty_fails_fast() -> None:
    with pytest.raises(AgentSessionSetupError) as excinfo:
        build_session_adapters_from_payload({}, registry=_registry())
    assert "stt" in str(excinfo.value)


def test_payload_s2s_only_is_not_enough() -> None:
    # A unified payload carries only the s2s entry; the split factory must still
    # fail fast on the missing STT rather than reach for s2s.
    payload = {"s2s": _entry(provider_name="openai_realtime")}

    with pytest.raises(AgentSessionSetupError) as excinfo:
        build_session_adapters_from_payload(payload, registry=_registry())
    assert "stt" in str(excinfo.value)


def test_payload_unknown_provider_raises_registry_error() -> None:
    # An entry naming a provider the registry doesn't know fails the same way the
    # DB path does — the registry's UnknownProviderError (a ProviderError).
    payload = _split_payload(llm="not-registered")

    with pytest.raises(UnknownProviderError):
        build_session_adapters_from_payload(payload, registry=_registry())


def test_payload_wrong_kind_factory_raises() -> None:
    reg = _registry()
    reg.register(ProviderKind.STT, "broken", _FakeLLM, replace=True)
    payload = _split_payload(stt="broken")

    with pytest.raises(AgentSessionSetupError) as excinfo:
        build_session_adapters_from_payload(payload, registry=reg)
    assert "STTProvider" in str(excinfo.value)


def test_payload_factory_is_lazy_exported_through_package() -> None:
    import johnny.agent.adapters as adapters
    from johnny.agent.adapters import factory

    assert (
        adapters.build_session_adapters_from_payload
        is factory.build_session_adapters_from_payload
    )


# --- Raw-provider carry + session prewarm (Johnny-trt.8) ---------------------
#
# SessionAdapters carries the raw Johnny providers the LiveKit adapters wrap so
# warm_up_session_providers can fire each one's warm_up() hook without reaching
# into adapter privates (the STT may be buried inside a LiveKit StreamAdapter).


class _WarmRecordingSTT(_FakeSTT):
    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self.warm_calls = 0

    async def warm_up(self) -> None:
        self.warm_calls += 1


class _WarmRecordingTTS(_FakeTTS):
    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self.warm_calls = 0

    async def warm_up(self) -> None:
        self.warm_calls += 1


class _WarmBoomLLM(_FakeLLM):
    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self.warm_calls = 0

    async def warm_up(self) -> None:
        self.warm_calls += 1
        raise RuntimeError("simulated warm-up failure")


def _provider_config(kind: ProviderKind, name: str) -> ProviderConfig:
    return ProviderConfig(kind=kind, provider_name=name, display_name=name)


def test_db_built_adapters_carry_the_raw_providers(session: Session) -> None:
    _seed_split(session)

    adapters = build_session_adapters(session, registry=_registry())

    # The carried raw providers are the SAME instances the adapters wrap —
    # warming them warms exactly what the session will drive.
    assert isinstance(adapters.stt_provider, _FakeSTT)
    assert adapters.llm_provider is adapters.llm._provider
    assert adapters.tts is not None
    assert adapters.tts_provider is adapters.tts._provider


def test_payload_built_adapters_carry_the_raw_providers() -> None:
    adapters = build_session_adapters_from_payload(_split_payload(), registry=_registry())

    assert isinstance(adapters.stt_provider, _FakeSTT)
    assert isinstance(adapters.llm_provider, _FakeLLM)
    assert adapters.tts is not None
    assert adapters.tts_provider is adapters.tts._provider


def test_missing_tts_carries_no_raw_tts(session: Session) -> None:
    _insert(session, kind=ProviderKind.STT, provider_name="deepgram", credentials={"api_key": "s"})
    _insert(session, kind=ProviderKind.LLM, provider_name="openai", credentials={"api_key": "l"})

    adapters = build_session_adapters(session, registry=_registry())

    assert adapters.tts is None
    assert adapters.tts_provider is None


async def test_warm_up_fires_every_hook_and_swallows_failures() -> None:
    """One failing warm_up must not stop the others, and never raises out."""
    from johnny.agent.adapters.factory import warm_up_session_providers

    stt = _WarmRecordingSTT(_provider_config(ProviderKind.STT, "deepgram"))
    llm = _WarmBoomLLM(_provider_config(ProviderKind.LLM, "openai"))
    tts = _WarmRecordingTTS(_provider_config(ProviderKind.TTS, "cartesia"))
    adapters = SessionAdapters(
        stt=JohnnySTT(stt),
        llm=JohnnyLLM(llm),
        tts=JohnnyTTS(tts),
        stt_provider=stt,
        llm_provider=llm,
        tts_provider=tts,
    )

    await warm_up_session_providers(adapters, session_id="warm-test")

    assert stt.warm_calls == 1
    assert llm.warm_calls == 1  # ran (and raised) without killing the gather
    assert tts.warm_calls == 1


async def test_warm_up_skips_absent_raw_providers() -> None:
    """A hand-built SessionAdapters without raw providers is a clean no-op."""
    from johnny.agent.adapters.factory import warm_up_session_providers

    stt = _FakeSTT(_provider_config(ProviderKind.STT, "deepgram"))
    llm = _FakeLLM(_provider_config(ProviderKind.LLM, "openai"))
    adapters = SessionAdapters(stt=JohnnySTT(stt), llm=JohnnyLLM(llm), tts=None)

    await warm_up_session_providers(adapters, session_id="warm-test")


def test_warm_up_helper_is_lazy_exported_through_package() -> None:
    import johnny.agent.adapters as adapters_pkg
    from johnny.agent.adapters import factory

    assert adapters_pkg.warm_up_session_providers is factory.warm_up_session_providers
