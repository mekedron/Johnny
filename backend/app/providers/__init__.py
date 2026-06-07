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
    TTSErrorCategory,
    TTSProvider,
    UnknownProviderError,
    get_registry,
)
from app.providers.cartesia_tts import CartesiaTTS
from app.providers.cartesia_tts import register as _register_cartesia_tts
from app.providers.deepgram_stt import DeepgramSTT
from app.providers.deepgram_stt import register as _register_deepgram_stt
from app.providers.elevenlabs_stt import ElevenLabsSTT
from app.providers.elevenlabs_stt import register as _register_elevenlabs_stt
from app.providers.elevenlabs_tts import ElevenLabsTTS
from app.providers.elevenlabs_tts import register as _register_elevenlabs_tts
from app.providers.faster_whisper_stt import FasterWhisperSTT
from app.providers.faster_whisper_stt import register as _register_faster_whisper_stt
from app.providers.gemini_live_s2s import GeminiLiveS2S
from app.providers.gemini_live_s2s import register as _register_gemini_live_s2s
from app.providers.gemini_llm import GeminiLLM
from app.providers.gemini_llm import register as _register_gemini_llm
from app.providers.openai_compatible_llm import OpenAICompatibleLLM
from app.providers.openai_compatible_llm import register as _register_openai_compatible_llm
from app.providers.openai_llm import OpenAILLM
from app.providers.openai_llm import register as _register_openai_llm
from app.providers.openai_realtime_s2s import OpenAIRealtimeS2S
from app.providers.openai_realtime_s2s import register as _register_openai_realtime_s2s
from app.providers.openai_realtime_stt import OpenAIRealtimeSTT
from app.providers.openai_realtime_stt import register as _register_openai_realtime_stt
from app.providers.openai_tts import OpenAITTS
from app.providers.openai_tts import register as _register_openai_tts
from app.providers.parakeet_stt import ParakeetSTT
from app.providers.parakeet_stt import register as _register_parakeet_stt
from app.providers.piper_tts import PiperTTS
from app.providers.piper_tts import register as _register_piper_tts
from app.providers.s2s_base import (
    S2SAudioFrame,
    S2SError,
    S2SEvent,
    S2SProvider,
    S2SResponseCompleted,
    S2SResponseStarted,
    S2SRole,
    S2SSession,
    S2SToolCall,
    S2STranscript,
)
from app.providers.schema import (
    FieldDef,
    FieldGroup,
    FieldOption,
    FieldType,
    ProviderSchema,
)
from app.providers.schema_validation import (
    FieldValidationError,
    split_values,
    validate_payload,
)
from app.providers.stub_s2s import StubS2S, StubS2SSession
from app.providers.stub_s2s import register as _register_stub_s2s

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
_register_cartesia_tts(replace=True)
_register_openai_compatible_llm(replace=True)
_register_openai_llm(replace=True)
_register_anthropic_llm(replace=True)
_register_gemini_llm(replace=True)
_register_faster_whisper_stt(replace=True)
_register_parakeet_stt(replace=True)
_register_deepgram_stt(replace=True)
_register_openai_realtime_stt(replace=True)
_register_elevenlabs_stt(replace=True)
_register_stub_s2s(replace=True)
_register_gemini_live_s2s(replace=True)
_register_openai_realtime_s2s(replace=True)

__all__ = [
    "AnthropicLLM",
    "CartesiaTTS",
    "ChatMessage",
    "ChatRole",
    "DeepgramSTT",
    "ElevenLabsSTT",
    "ElevenLabsTTS",
    "FasterWhisperSTT",
    "FieldDef",
    "FieldGroup",
    "FieldOption",
    "FieldType",
    "FieldValidationError",
    "GeminiLLM",
    "GeminiLiveS2S",
    "LLMError",
    "LLMProvider",
    "LLMResponse",
    "OpenAICompatibleLLM",
    "OpenAILLM",
    "OpenAIRealtimeS2S",
    "OpenAIRealtimeSTT",
    "OpenAITTS",
    "ParakeetSTT",
    "PiperTTS",
    "ProviderConfig",
    "ProviderError",
    "ProviderFactory",
    "ProviderInstance",
    "ProviderKind",
    "ProviderRegistry",
    "ProviderSchema",
    "S2SAudioFrame",
    "S2SError",
    "S2SEvent",
    "S2SProvider",
    "S2SResponseCompleted",
    "S2SResponseStarted",
    "S2SRole",
    "S2SSession",
    "S2SToolCall",
    "S2STranscript",
    "STTError",
    "STTProvider",
    "StubS2S",
    "StubS2SSession",
    "TTSError",
    "TTSErrorCategory",
    "TTSProvider",
    "ToolCall",
    "ToolDefinition",
    "TranscriptEvent",
    "UnknownProviderError",
    "get_registry",
    "split_values",
    "validate_payload",
]
