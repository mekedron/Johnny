"""Provider adapter layer: Johnny ABCs -> LiveKit plugins (Phase 1).

This subpackage holds the three adapters that let LiveKit's
``AgentSession`` drive every admin-configured Johnny provider unchanged:

* ``JohnnySTT(stt.STT)`` — wraps ``STTProvider.transcribe_stream``
  (Johnny-c81);
* :class:`~johnny.agent.adapters.johnny_llm.JohnnyLLM` — wraps
  ``LLMProvider.chat`` / ``stream_chat`` (Johnny-6nl);
* ``JohnnyTTS(tts.TTS)`` — wraps ``TTSProvider.synthesize_stream``
  (Johnny-7a3).

The :func:`~johnny.agent.adapters.factory.build_session_adapters` factory
(Johnny-zb3) builds the three instances from
``app.providers.loader.load_active_providers()`` at session start, so the
provider registry / schema / Fernet stack stays untouched.

The concrete modules subclass the LiveKit SDK, so importing them pulls in
the ``agent`` extra (``livekit-agents``); the factory additionally pulls in
SQLAlchemy via the loader. To keep ``import johnny.agent.adapters`` cheap and
safe where those deps are absent (mirroring :mod:`johnny.agent`'s lazy
discipline), the adapter classes and the factory are exposed via :pep:`562`
``__getattr__`` — ``from johnny.agent.adapters import JohnnyLLM`` (or
``build_session_adapters``) triggers the backing import on first access, while
a bare ``import johnny.agent.adapters`` does not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from johnny.agent.adapters.factory import (
        AgentSessionSetupError,
        SessionAdapters,
        build_session_adapters,
    )
    from johnny.agent.adapters.johnny_llm import JohnnyLLM
    from johnny.agent.adapters.johnny_stt import JohnnySTT
    from johnny.agent.adapters.johnny_tts import JohnnyTTS

__all__ = [
    "AgentSessionSetupError",
    "JohnnyLLM",
    "JohnnySTT",
    "JohnnyTTS",
    "SessionAdapters",
    "build_session_adapters",
]

_FACTORY_EXPORTS = frozenset(
    {"AgentSessionSetupError", "SessionAdapters", "build_session_adapters"}
)


def __getattr__(name: str) -> Any:
    if name == "JohnnySTT":
        from johnny.agent.adapters.johnny_stt import JohnnySTT

        return JohnnySTT
    if name == "JohnnyLLM":
        from johnny.agent.adapters.johnny_llm import JohnnyLLM

        return JohnnyLLM
    if name == "JohnnyTTS":
        from johnny.agent.adapters.johnny_tts import JohnnyTTS

        return JohnnyTTS
    if name in _FACTORY_EXPORTS:
        from johnny.agent.adapters import factory

        return getattr(factory, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
