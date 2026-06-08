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

    The adapters are constructed with no ``voice`` / ``model`` / ``language``
    overrides — each provider already carries its admin-configured model and
    voice, and the TTS adapter's ``voice=None`` falls through to that same
    configured default — so the operator's choices are honored without the
    factory re-deriving them (``load_active_providers`` returns live instances,
    not their config, and stays untouched). Voice/model selection parity at the
    adapter layer is Johnny-88n.

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
    stt = _require(active, ProviderKind.STT)
    if not isinstance(stt, STTProvider):
        raise AgentSessionSetupError(_wrong_type(ProviderKind.STT, stt, "STTProvider"))
    llm = _require(active, ProviderKind.LLM)
    if not isinstance(llm, LLMProvider):
        raise AgentSessionSetupError(_wrong_type(ProviderKind.LLM, llm, "LLMProvider"))
    tts = _require(active, ProviderKind.TTS)
    if not isinstance(tts, TTSProvider):
        raise AgentSessionSetupError(_wrong_type(ProviderKind.TTS, tts, "TTSProvider"))
    return SessionAdapters(
        stt=build_stt_adapter(stt, vad=vad),
        llm=JohnnyLLM(llm),
        tts=JohnnyTTS(tts),
    )


__all__ = [
    "AgentSessionSetupError",
    "SessionAdapters",
    "build_session_adapters",
]
