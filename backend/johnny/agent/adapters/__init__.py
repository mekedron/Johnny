"""Provider adapter layer: Johnny ABCs -> LiveKit plugins (Phase 1).

This subpackage will hold the three adapters that let LiveKit's
``AgentSession`` drive every admin-configured Johnny provider unchanged:

* ``JohnnySTT(stt.STT)`` — wraps ``STTProvider.transcribe_stream``
  (Johnny-c81);
* ``JohnnyLLM(llm.LLM)`` — wraps ``LLMProvider.chat`` / ``stream_chat``
  (Johnny-6nl);
* ``JohnnyTTS(tts.TTS)`` — wraps ``TTSProvider.synthesize_stream``
  (Johnny-7a3).

A factory (Johnny-zb3) builds the three instances from
``app.providers.loader.load_active_providers()`` at session start, so the
provider registry / schema / Fernet stack stays untouched. The concrete
modules arrive in Phase 1; this Phase-0 placeholder keeps the package
importable (and pytest-collectable) without ``livekit`` installed.
"""

from __future__ import annotations

__all__: list[str] = []
