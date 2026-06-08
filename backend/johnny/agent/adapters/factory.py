"""Adapter factory — admin-active providers → LiveKit session plugins (Johnny-zb3).

The single point where Johnny's admin-configured providers become a live
LiveKit ``AgentSession``. :func:`build_session_adapters` calls the UNCHANGED
:func:`app.providers.loader.load_active_providers` (registry lookup + DB config
+ Fernet decrypt) for the three split-pipeline kinds and wraps each resolved
Johnny provider in its LiveKit plugin adapter
(:class:`~johnny.agent.adapters.johnny_stt.JohnnySTT` /
:class:`~johnny.agent.adapters.johnny_llm.JohnnyLLM` /
:class:`~johnny.agent.adapters.johnny_tts.JohnnyTTS`), returning a
:class:`SessionAdapters` ready to spread into
:func:`johnny.agent.session.build_agent_session`.

The provider registry / schema / Fernet stack is **untouched**: this module
only *consumes* ``load_active_providers`` and the existing adapter classes, so
switching the active provider in admin yields a different live adapter at the
next session start with no change to ``app.providers``' public surface.

**Split mode only.** A LiveKit ``AgentSession`` in split mode needs all three
of STT + LLM + TTS (the harness takes them as required arguments), so a missing
active row for any of them is a fail-fast :class:`AgentSessionSetupError` at
session start rather than a mid-meeting surprise — mirroring the meet-worker's
``PipelineSetupError`` and the loader's own "fail fast at startup" contract.
Unified (S2S) mode does not use this factory.

Requires both the ``agent`` extra (``livekit-agents``, via the adapters) and
SQLAlchemy (via the loader); imported only in the api/agent image, never from
the import-safe top-level :mod:`johnny.agent` / :mod:`johnny.agent.adapters`
packages — it is lazy-exported through the adapters' :pep:`562` ``__getattr__``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.db.models import ProviderCredential
from app.providers.base import (
    LLMProvider,
    ProviderError,
    ProviderInstance,
    ProviderKind,
    STTProvider,
    TTSProvider,
)
from app.providers.loader import load_active_providers
from johnny.agent.adapters.johnny_llm import JohnnyLLM
from johnny.agent.adapters.johnny_stt import build_stt_adapter
from johnny.agent.adapters.johnny_tts import JohnnyTTS

if TYPE_CHECKING:
    from collections.abc import Mapping

    from livekit.agents.stt import STT
    from livekit.agents.vad import VAD
    from sqlalchemy.orm import Session

    from app.providers.base import ProviderRegistry
    from app.providers.loader import CredentialDecryptor

# The three split-pipeline kinds a LiveKit AgentSession drives. Passing these
# to the loader scopes its query to the split stack; an active S2S row (unified
# mode) is intentionally not loaded — that mode bypasses this factory entirely.
_SPLIT_KINDS: tuple[ProviderKind, ...] = (
    ProviderKind.STT,
    ProviderKind.LLM,
    ProviderKind.TTS,
)

# Admin-config option keys carrying the operator's voice / model / language
# selection, in priority order per kind. The admin UI writes the choice into
# ``provider_credentials.config`` under a provider-specific key, and those keys
# are NOT uniform across the split stack: STT model is ``model`` (Deepgram),
# ``model_id`` (ElevenLabs, Parakeet) or ``model_size`` (faster-whisper); STT
# language is ``language`` or ``language_code`` (ElevenLabs); TTS model is
# ``model`` (OpenAI) or ``model_id`` (the rest); TTS voice is always
# ``voice_id``; LLM model is always ``model``. Reading the config row (rather
# than the live provider) is the one uniform source — provider instances expose
# these under inconsistent property names, and some not at all (``openai`` LLM
# has no ``model`` property; faster-whisper's ``model`` is the loaded weights
# object, not its name). These selections are threaded into the adapters purely
# so LiveKit metrics / traces name the real model+voice (and so STT stamps the
# configured language onto each transcript); the *behaviour* is provider-owned —
# every live provider already applies its own config — so a key this map misses
# degrades only the label, never the audio. ``app.providers`` stays untouched.
_VOICE_KEYS: tuple[str, ...] = ("voice_id",)
_TTS_MODEL_KEYS: tuple[str, ...] = ("model", "model_id")
_LLM_MODEL_KEYS: tuple[str, ...] = ("model",)
_STT_MODEL_KEYS: tuple[str, ...] = ("model", "model_id", "model_size")
_STT_LANGUAGE_KEYS: tuple[str, ...] = ("language", "language_code")


class AgentSessionSetupError(ProviderError):
    """Raised when the admin-active providers can't build a split AgentSession.

    Subclasses :class:`~app.providers.base.ProviderError` so a caller can catch
    every provider-side setup failure — this plus the loader's
    :class:`~app.providers.base.UnknownProviderError` — with one ``except``.
    """


@dataclass(frozen=True, slots=True)
class SessionAdapters:
    """The three LiveKit plugin instances for one split-pipeline session.

    Spread straight into the session harness::

        adapters = build_session_adapters(db, vad=vad)
        session = build_agent_session(
            stt=adapters.stt, llm=adapters.llm, tts=adapters.tts, vad=vad
        )

    ``stt`` is a :class:`~livekit.agents.stt.STT`: a bare :class:`JohnnySTT` for
    truly-streaming providers, or a VAD-buffered
    :class:`~livekit.agents.stt.StreamAdapter` wrapping one for batch-only
    providers (:func:`~johnny.agent.adapters.johnny_stt.build_stt_adapter`,
    Johnny-4fn).
    """

    stt: STT[Any]
    llm: JohnnyLLM
    tts: JohnnyTTS


def _require(
    active: Mapping[ProviderKind, ProviderInstance],
    kind: ProviderKind,
) -> ProviderInstance:
    """Return the active provider for ``kind`` or fail fast.

    Raises :class:`AgentSessionSetupError` when no active row resolved for
    ``kind`` — the operator hasn't configured that stage, or the deployment is
    in S2S mode (which bypasses this factory). The concrete-ABC narrowing each
    adapter constructor needs is done by the ``isinstance`` checks in
    :func:`build_session_adapters` (mirroring the meet-worker's ``_as_stt``
    guards): the registry maps a ``kind`` to a factory of the matching provider
    ABC, but that contract isn't expressible to the type checker.
    """
    instance = active.get(kind)
    if instance is None:
        raise AgentSessionSetupError(
            f"no active {kind.value} provider — a split AgentSession needs an "
            f"active kind={kind.value!r} row "
            "(configure one in admin, or run unified/S2S mode, which does not "
            "use this factory)"
        )
    return instance


def _wrong_type(kind: ProviderKind, instance: ProviderInstance, expected: str) -> str:
    return (
        f"active {kind.value} provider is not a {expected}: "
        f"{type(instance).__name__} (registry misconfiguration)"
    )


def _active_options(
    session: Session,
    kinds: tuple[ProviderKind, ...],
) -> dict[ProviderKind, dict[str, Any]]:
    """Read each active split-kind row's admin config (the ``config`` JSON).

    A second, read-only pass over ``provider_credentials`` alongside
    :func:`~app.providers.loader.load_active_providers` (which returns live
    instances and discards their config). The partial unique index
    ``uq_provider_credentials_active_per_kind`` guarantees at most one active
    row per kind, so the result has one entry per configured kind. Kept here —
    not in the loader — so ``app.providers`` stays untouched.
    """
    stmt = (
        select(ProviderCredential.kind, ProviderCredential.config)
        .where(ProviderCredential.is_active.is_(True))
        .where(ProviderCredential.kind.in_(list(kinds)))
    )
    return {row.kind: dict(row.config or {}) for row in session.execute(stmt).all()}


def _selected(options: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """First non-empty string value in ``options`` under any of ``keys``.

    Returns ``None`` when the operator left the selection unset, so the adapter
    falls back to the provider's own configured default (and the label simply
    reflects "not overridden" rather than a guessed value).
    """
    for key in keys:
        value = options.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def build_session_adapters(
    session: Session,
    *,
    registry: ProviderRegistry | None = None,
    decrypt: CredentialDecryptor | None = None,
    vad: VAD | None = None,
) -> SessionAdapters:
    """Build the LiveKit STT/LLM/TTS plugin set from the admin-active providers.

    Calls the UNCHANGED :func:`~app.providers.loader.load_active_providers`
    (registry + DB config + Fernet decrypt) scoped to the split-pipeline kinds
    and wraps each resolved Johnny provider in its LiveKit adapter. ``registry``
    / ``decrypt`` are forwarded to the loader: production passes the
    Fernet-backed decryptor; tests can inject a fake registry + identity
    decryptor.

    The STT adapter is built via
    :func:`~johnny.agent.adapters.johnny_stt.build_stt_adapter`: truly-streaming
    providers (Deepgram) become a bare :class:`JohnnySTT`, while batch-only
    providers (faster-whisper, Parakeet, ElevenLabs) are wrapped in a
    VAD-buffered :class:`~livekit.agents.stt.StreamAdapter` so they present a
    streaming surface to ``AgentSession`` (Johnny-4fn). Pass ``vad`` (the same
    Silero model the session uses for turn detection) so only one model is
    loaded; when a batch-only STT is active and ``vad`` is ``None`` a default
    Silero VAD is loaded lazily.

    The operator's admin selections are threaded into the adapters so a session
    uses — and reports — exactly the configured values (Johnny-88n): the TTS
    voice is passed as the adapter's ``voice`` (and the TTS model as its
    ``model`` label), the LLM model as the LLM adapter's ``model`` label, and
    the STT model + language into :func:`build_stt_adapter` (language is also
    stamped onto every transcript). The values come from the active rows'
    ``config`` JSON (:func:`_active_options`) — the one uniform source of the
    operator's choice — not from re-deriving them off the live providers, whose
    config-property names are inconsistent (see :data:`_VOICE_KEYS` &c.).
    Behaviour was already correct without this (each provider applies its own
    config, and ``voice=None`` falls through to the provider default); this adds
    label/observability parity and the explicit voice pass-through. *Tool*
    definitions are not a factory concern: Johnny has no admin-configured static
    tools — in a LiveKit session tools come from the :class:`Agent` per turn and
    :class:`JohnnyLLM` already forwards them to ``LLMProvider.chat(tools=...)``.

    Raises :class:`AgentSessionSetupError` if any of STT / LLM / TTS has no
    active provider, so misconfiguration fails fast at session start instead of
    mid-meeting.
    """
    active = load_active_providers(
        session,
        registry=registry,
        decrypt=decrypt,
        kinds=_SPLIT_KINDS,
    )
    options = _active_options(session, _SPLIT_KINDS)
    stt = _require(active, ProviderKind.STT)
    if not isinstance(stt, STTProvider):
        raise AgentSessionSetupError(_wrong_type(ProviderKind.STT, stt, "STTProvider"))
    llm = _require(active, ProviderKind.LLM)
    if not isinstance(llm, LLMProvider):
        raise AgentSessionSetupError(_wrong_type(ProviderKind.LLM, llm, "LLMProvider"))
    tts = _require(active, ProviderKind.TTS)
    if not isinstance(tts, TTSProvider):
        raise AgentSessionSetupError(_wrong_type(ProviderKind.TTS, tts, "TTSProvider"))
    stt_opts = options.get(ProviderKind.STT, {})
    llm_opts = options.get(ProviderKind.LLM, {})
    tts_opts = options.get(ProviderKind.TTS, {})
    return SessionAdapters(
        stt=build_stt_adapter(
            stt,
            vad=vad,
            language=_selected(stt_opts, _STT_LANGUAGE_KEYS),
            model=_selected(stt_opts, _STT_MODEL_KEYS),
        ),
        llm=JohnnyLLM(llm, model=_selected(llm_opts, _LLM_MODEL_KEYS)),
        tts=JohnnyTTS(
            tts,
            voice=_selected(tts_opts, _VOICE_KEYS),
            model=_selected(tts_opts, _TTS_MODEL_KEYS),
        ),
    )


__all__ = [
    "AgentSessionSetupError",
    "SessionAdapters",
    "build_session_adapters",
]
