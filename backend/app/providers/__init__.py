"""Pluggable provider adapters for STT, LLM, and TTS.

The base module defines protocol ABCs and value objects that every concrete
adapter (Deepgram, OpenAI, Anthropic, Gemini, faster-whisper, Piper, etc.)
must implement. A process-wide :class:`ProviderRegistry` maps
``(kind, name)`` to factory callables; at startup, rows in
``provider_credentials`` are turned into live provider instances via the
registry by :func:`load_active_providers`.
"""

from app.providers.base import (
    ChatMessage,
    LLMError,
    LLMProvider,
    LLMResponse,
    ProviderConfig,
    ProviderError,
    ProviderFactory,
    ProviderInstance,
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
    load_active_providers,
)

__all__ = [
    "ChatMessage",
    "LLMError",
    "LLMProvider",
    "LLMResponse",
    "ProviderConfig",
    "ProviderError",
    "ProviderFactory",
    "ProviderInstance",
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
    "load_active_providers",
]
