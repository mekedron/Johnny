"""Pluggable provider adapters for STT, LLM, and TTS.

The base module defines protocol ABCs and value objects that every concrete
adapter (Deepgram, OpenAI, Anthropic, Gemini, faster-whisper, Piper, etc.)
must implement. A process-wide :class:`ProviderRegistry` maps
``(kind, name)`` to factory callables; at startup, rows in
``provider_credentials`` are turned into live provider instances via
:func:`app.providers.loader.load_active_providers`.

This package's top-level re-exports are SQLAlchemy-free so the meet-worker
image (which ships only the ``johnny`` package + a copy of these provider
ABCs) can ``from app.providers import STTProvider`` without dragging in the
ORM stack. Callers that need the DB-coupled loader must import it
explicitly from :mod:`app.providers.loader`.
"""

from app.providers.anthropic_llm import AnthropicLLM
from app.providers.anthropic_llm import register as _register_anthropic_llm
from app.providers.base import (
    ChatMessage,
    ChatRole,
    LLMError,
    LLMProvider,
    LLMResponse,
    ProviderConfig,
    ProviderError,
    ProviderFactory,
    ProviderInstance,
    ProviderKind,
    ProviderRegistry,
    STTError,
    STTProvider,
    ToolCall,
    ToolDefinition,
    TranscriptEvent,
    TTSError,
    TTSProvider,
    UnknownProviderError,
    get_registry,
)
from app.providers.elevenlabs_tts import ElevenLabsTTS
from app.providers.elevenlabs_tts import register as _register_elevenlabs_tts
from app.providers.faster_whisper_stt import FasterWhisperSTT
from app.providers.faster_whisper_stt import register as _register_faster_whisper_stt
from app.providers.gemini_llm import GeminiLLM
from app.providers.gemini_llm import register as _register_gemini_llm
from app.providers.openai_compatible_llm import OpenAICompatibleLLM
from app.providers.openai_compatible_llm import register as _register_openai_compatible_llm
from app.providers.openai_llm import OpenAILLM
from app.providers.openai_llm import register as _register_openai_llm
from app.providers.openai_tts import OpenAITTS
from app.providers.openai_tts import register as _register_openai_tts
from app.providers.piper_tts import PiperTTS
from app.providers.piper_tts import register as _register_piper_tts

# Auto-register adapters whose imports only pull in stdlib + httpx (a
# lightweight runtime dep already required by FastAPI / tests). Adapters
# whose imports require heavy optional deps (torch, openai-sdk, etc.) must
# register lazily from their own modules instead. FasterWhisperSTT counts
# as stdlib-only at import time — ``faster-whisper`` is lazy-imported in
# ``_load_model`` so the registration is safe even on the API container
# where the library is not installed.
_register_piper_tts(replace=True)
_register_openai_tts(replace=True)
_register_elevenlabs_tts(replace=True)
_register_openai_compatible_llm(replace=True)
_register_openai_llm(replace=True)
_register_anthropic_llm(replace=True)
_register_gemini_llm(replace=True)
_register_faster_whisper_stt(replace=True)

__all__ = [
    "AnthropicLLM",
    "ChatMessage",
    "ChatRole",
    "ElevenLabsTTS",
    "FasterWhisperSTT",
    "GeminiLLM",
    "LLMError",
    "LLMProvider",
    "LLMResponse",
    "OpenAICompatibleLLM",
    "OpenAILLM",
    "OpenAITTS",
    "PiperTTS",
    "ProviderConfig",
    "ProviderError",
    "ProviderFactory",
    "ProviderInstance",
    "ProviderKind",
    "ProviderRegistry",
    "STTError",
    "STTProvider",
    "TTSError",
    "TTSProvider",
    "ToolCall",
    "ToolDefinition",
    "TranscriptEvent",
    "UnknownProviderError",
    "get_registry",
]
