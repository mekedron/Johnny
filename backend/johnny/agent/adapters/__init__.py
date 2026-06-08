"""Provider adapter layer: Johnny ABCs -> LiveKit plugins (Phase 1).

This subpackage holds the three adapters that let LiveKit's
``AgentSession`` drive every admin-configured Johnny provider unchanged:

* ``JohnnySTT(stt.STT)`` — wraps ``STTProvider.transcribe_stream``
  (Johnny-c81);
* :class:`~johnny.agent.adapters.johnny_llm.JohnnyLLM` — wraps
  ``LLMProvider.chat`` / ``stream_chat`` (Johnny-6nl);
* ``JohnnyTTS(tts.TTS)`` — wraps ``TTSProvider.synthesize_stream``
  (Johnny-7a3).

A factory (Johnny-zb3) builds the three instances from
``app.providers.loader.load_active_providers()`` at session start, so the
provider registry / schema / Fernet stack stays untouched.

The concrete modules subclass the LiveKit SDK, so importing them pulls in
the ``agent`` extra (``livekit-agents``). To keep ``import
johnny.agent.adapters`` cheap and safe where that extra is absent
(mirroring :mod:`johnny.agent`'s lazy discipline), the adapter classes are
exposed via :pep:`562` ``__getattr__`` — ``from johnny.agent.adapters
import JohnnyLLM`` triggers the livekit-backed import on first access, while
a bare ``import johnny.agent.adapters`` does not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from johnny.agent.adapters.johnny_llm import JohnnyLLM

__all__ = ["JohnnyLLM"]


def __getattr__(name: str) -> Any:
    if name == "JohnnyLLM":
        from johnny.agent.adapters.johnny_llm import JohnnyLLM

        return JohnnyLLM
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
