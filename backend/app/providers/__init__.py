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

__all__ = [
    "ChatMessage",
    "ChatRole",
    "LLMError",
    "LLMProvider",
    "LLMResponse",
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
