"""Abstract base classes and registry for swappable STT / LLM / TTS providers.

The voice pipeline never imports a concrete adapter. It depends only on the
:class:`STTProvider`, :class:`LLMProvider`, and :class:`TTSProvider` ABCs and
the :class:`ProviderRegistry` defined here. Adapter modules call
``get_registry().register(kind, name, factory)`` at import time; the runtime
resolves rows in ``provider_credentials`` against the registry to instantiate
live providers via :func:`app.providers.loader.load_active_providers`.

This module is **SQLAlchemy-free** so the meet-worker image (which only ships
the ``johnny`` package + a minimal copy of provider ABCs) can import it
without pulling in the ORM stack. DB-coupled wiring lives in
``app/providers/loader.py``.

Audio frames carried by :class:`STTProvider` and :class:`TTSProvider` are
16 kHz mono signed-16-bit little-endian PCM, matching the meet-worker audio
bridge format.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from app.providers.schema import ProviderSchema

PCM_SAMPLE_RATE_HZ = 16_000
PCM_SAMPLE_WIDTH_BYTES = 2
PCM_CHANNELS = 1

ChatRole = Literal["system", "user", "assistant", "tool"]


class ProviderKind(enum.StrEnum):
    """Categorical role of a provider in the pipeline.

    ``STT``/``LLM``/``TTS`` are the three stages of the split pipeline.
    ``S2S`` is a unified speech-to-speech provider (OpenAI GPT-Realtime,
    Gemini Live) that collapses all three into one bidirectional session.
    A session runs in EITHER split mode (uses STT+LLM+TTS) OR unified
    mode (uses S2S) — never both at once. Which mode runs is governed by
    the per-deployment ``pipeline_settings.pipeline_mode`` setting.
    """

    STT = "stt"
    LLM = "llm"
    TTS = "tts"
    S2S = "s2s"


# --- Errors ---------------------------------------------------------------


class ProviderError(Exception):
    """Base class for any provider-side failure."""


class STTError(ProviderError):
    """Raised when an STT adapter fails (auth, transport, decode, etc.)."""


class LLMError(ProviderError):
    """Raised when an LLM adapter fails (auth, rate limit, schema, etc.)."""


TTSErrorCategory = Literal[
    "quota_exceeded",
    "auth_failed",
    "rate_limited",
    "unknown",
]
"""Why a :class:`TTSError` fired (Johnny-g2n).

Lets the pipeline branch on the *kind* of failure without parsing the
exception message:

* ``quota_exceeded`` — provider rejected the call because the account
  is out of credits / past its monthly quota. Terminal for the session:
  retrying just burns more error responses, and the operator needs to
  top up before any TTS will work again.
* ``auth_failed`` — bad / revoked API key, or an expired token. Also
  terminal for the session.
* ``rate_limited`` — provider asked us to back off; transient, but per
  Johnny-g2n we still want a structured event so the operator can see
  why a turn fell silent.
* ``unknown`` — every other failure (network blip, 5xx, decode error).
  Not terminal; the next turn re-attempts.
"""


class TTSError(ProviderError):
    """Raised when a TTS adapter fails (auth, transport, synth, etc.).

    ``category`` (Johnny-g2n) tags the failure type so the voice pipeline
    can emit a structured ``agent_tts_failed`` event with a meaningful
    reason for the UI and decide whether to trip a per-session circuit
    breaker (terminal categories: ``quota_exceeded`` / ``auth_failed``).
    Adapters that don't categorise their failures inherit
    ``category='unknown'`` so the broad ``except TTSError`` path still
    works.
    """

    def __init__(
        self,
        message: str,
        *,
        category: TTSErrorCategory = "unknown",
    ) -> None:
        super().__init__(message)
        self.category: TTSErrorCategory = category


class UnknownProviderError(ProviderError, KeyError):
    """No factory is registered for the requested ``(kind, name)`` pair."""

    def __init__(self, kind: ProviderKind, name: str) -> None:
        self.kind = kind
        self.name = name
        super().__init__(f"no provider registered for {kind.value}:{name}")


# --- Value objects ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TranscriptEvent:
    """A unit of STT output, partial or final.

    ``timestamp_ms`` is the offset since the start of the audio stream, not
    wall-clock time. ``confidence`` is provider-specific in [0, 1] when known.
    """

    text: str
    is_final: bool
    timestamp_ms: int
    confidence: float | None = None
    speaker: str | None = None


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Declarative tool/function the LLM is permitted to call."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A single tool/function invocation requested by the LLM."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """A single turn in an LLM chat context."""

    role: ChatRole
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """A completed LLM response.

    ``text`` always carries the model's free-text output (empty string when
    the response is pure tool calls). ``structured_output`` is the parsed
    object when ``response_format`` was supplied; otherwise ``None``.
    """

    text: str
    finish_reason: str
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    structured_output: Any = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LLMModelInfo:
    """One model exposed by an LLM provider's catalog endpoint (Johnny-9eq).

    Returned by each LLM provider's module-level ``fetch_model_catalog``
    so the ``/providers`` modal can render a live, current dropdown
    instead of a stale hand-curated FieldOption list. ``id`` is the
    canonical identifier sent to the chat completion endpoint (the
    ``model`` body field for OpenAI/Anthropic/openai-compatible, the
    URL path segment for Gemini). ``label`` is a UI-facing string —
    typically equal to ``id`` but adapters may decorate it with the
    short alias or the model family. ``description`` is an optional
    one-line summary when the provider's catalog supplies one.
    """

    id: str
    label: str
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "label": self.label}
        if self.description is not None:
            out["description"] = self.description
        return out


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Adapter-agnostic configuration passed to every provider factory.

    ``credentials`` holds decrypted secrets (api keys, tokens) freshly
    materialized from the DB. ``options`` carries non-secret settings such
    as ``model``, ``voice_id``, ``base_url``, or sample-rate overrides.
    ``display_name`` is the user-facing label for logging / UI surfaces.
    """

    kind: ProviderKind
    provider_name: str
    display_name: str
    credentials: dict[str, str] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)


# --- ABCs ------------------------------------------------------------------


class _ProviderBase(ABC):
    """Shared lifecycle for STT / LLM / TTS providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Canonical provider name (e.g. ``"deepgram"``, ``"openai"``)."""

    @classmethod
    def field_schema(cls) -> ProviderSchema:
        """Describe the form fields the configuration UI should render.

        Adapters override this to declare their per-provider field set.
        The default raises ``NotImplementedError`` so any registered
        adapter that surfaces in ``/providers/schemas`` must opt in.
        """
        raise NotImplementedError(
            f"{cls.__name__} has not declared a field_schema()"
        )

    async def close(self) -> None:  # noqa: B027 — intentional non-abstract hook
        """Release any held resources (HTTP sessions, model handles).

        Default is a no-op so simple stateless adapters need not override.
        """


class STTProvider(_ProviderBase):
    """Streaming speech-to-text adapter contract."""

    @abstractmethod
    def transcribe_stream(
        self,
        audio_iter: AsyncIterator[bytes],
    ) -> AsyncIterator[TranscriptEvent]:
        """Stream :class:`TranscriptEvent` objects from PCM input.

        ``audio_iter`` yields chunks of 16 kHz mono signed-16-bit PCM.
        Implementations are async generators (``async def`` with ``yield``);
        the ABC just declares the contract. Both partial and final events
        may be emitted; consumers identify finals via ``event.is_final``.
        """


class LLMProvider(_ProviderBase):
    """Chat-completion adapter contract."""

    @abstractmethod
    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Produce a chat completion.

        ``tools`` enables function/tool calling. ``response_format`` is a
        JSON Schema describing the expected structured output; the parsed
        object is returned on :attr:`LLMResponse.structured_output`.
        Adapters raise :class:`LLMError` on transport / schema failure.
        """

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
    ) -> AsyncIterator[str]:
        """Stream the assistant's free-text response as deltas.

        The default implementation calls :meth:`chat` and yields the full
        text as a single delta, so adapters that don't implement true
        streaming still satisfy the contract. Adapters that *do* stream
        (OpenAI, Anthropic, Gemini, etc.) should override and yield as
        soon as bytes arrive — the voice pipeline uses this to start
        TTS as early as possible.
        """
        response = await self.chat(messages)
        if response.text:
            yield response.text


class TTSProvider(_ProviderBase):
    """Streaming text-to-speech adapter contract."""

    @abstractmethod
    def synthesize_stream(
        self,
        text: str,
        voice_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        """Stream 16 kHz mono signed-16-bit PCM frames for ``text``.

        ``voice_id`` overrides the provider-default voice. Implementations
        are async generators; the ABC declares the contract only.
        """


ProviderInstance = _ProviderBase
"""Runtime alias for "any provider ABC".

Originally :class:`STTProvider` / :class:`LLMProvider` / :class:`TTSProvider`
was an explicit union; Johnny-ckz.17 added :class:`S2SProvider`
(in :mod:`app.providers.s2s_base`) and the natural ``A | B | C | D``
declaration here would force an import cycle. The shared base class
is the common supertype of all four, so aliasing to ``_ProviderBase``
keeps runtime ``isinstance`` checks correct and lets the loader still
return ``ProviderInstance`` for any concrete factory output.
"""

ProviderFactory = Callable[[ProviderConfig], ProviderInstance]
"""A callable that produces a configured provider instance.

Adapter classes themselves are factories — ``DeepgramSTT(config)`` returns an
:class:`STTProvider` because ``__init__`` matches ``Callable[..., Self]``.
"""


# --- Registry --------------------------------------------------------------


class ProviderRegistry:
    """Maps ``(ProviderKind, provider_name)`` to a factory callable.

    Adapter modules register their factories at import time (or via an
    explicit registration hook). Loading at startup is a two-step dance:
    register the available factories first, then call
    :func:`app.providers.loader.load_active_providers` to materialize live
    instances from the active rows in ``provider_credentials``.
    """

    def __init__(self) -> None:
        self._factories: dict[tuple[ProviderKind, str], ProviderFactory] = {}

    def register(
        self,
        kind: ProviderKind,
        name: str,
        factory: ProviderFactory,
        *,
        replace: bool = False,
    ) -> None:
        """Register ``factory`` under ``(kind, name)``.

        Raises :class:`ValueError` if a factory is already registered and
        ``replace`` is False — protects against accidental shadowing when
        two adapter modules use the same name.
        """
        key = (kind, name)
        if key in self._factories and not replace:
            raise ValueError(
                f"provider already registered for {kind.value}:{name}; "
                "pass replace=True to override"
            )
        self._factories[key] = factory

    def unregister(self, kind: ProviderKind, name: str) -> None:
        """Remove the factory for ``(kind, name)`` if present."""
        self._factories.pop((kind, name), None)

    def get(self, kind: ProviderKind, name: str) -> ProviderFactory:
        """Return the factory for ``(kind, name)`` or raise :class:`UnknownProviderError`."""
        try:
            return self._factories[(kind, name)]
        except KeyError as exc:
            raise UnknownProviderError(kind, name) from exc

    def has(self, kind: ProviderKind, name: str) -> bool:
        return (kind, name) in self._factories

    def names(self, kind: ProviderKind) -> list[str]:
        """All provider names registered under ``kind``, sorted for stability."""
        return sorted(n for k, n in self._factories if k == kind)

    def kinds(self) -> set[ProviderKind]:
        """All distinct kinds present in the registry."""
        return {k for k, _ in self._factories}

    def clear(self) -> None:
        """Remove every registration. Intended for tests only."""
        self._factories.clear()

    def instantiate(self, config: ProviderConfig) -> ProviderInstance:
        """Look up the factory for ``config`` and invoke it.

        Note: ``factory(config)`` builds a fresh provider instance every
        call. Adapters whose underlying state is expensive to load
        (Parakeet's NeMo model, Piper voices, faster-whisper weights)
        currently work around this with module-level caches inside the
        adapter — see ``parakeet_stt._LAST`` for the canonical pattern.
        When a third adapter pays for this individually, lift the cache
        up to the registry keyed by ``(kind, name, frozenset(options))``
        and add a row-edit invalidation hook.
        """
        factory = self.get(config.kind, config.provider_name)
        return factory(config)


_global_registry = ProviderRegistry()


def get_registry() -> ProviderRegistry:
    """Return the process-wide :class:`ProviderRegistry` singleton."""
    return _global_registry


__all__ = [
    "ChatMessage",
    "ChatRole",
    "LLMError",
    "LLMModelInfo",
    "LLMProvider",
    "LLMResponse",
    "PCM_CHANNELS",
    "PCM_SAMPLE_RATE_HZ",
    "PCM_SAMPLE_WIDTH_BYTES",
    "ProviderConfig",
    "ProviderError",
    "ProviderFactory",
    "ProviderInstance",
    "ProviderKind",
    "ProviderRegistry",
    "STTError",
    "STTProvider",
    "TTSError",
    "TTSErrorCategory",
    "TTSProvider",
    "ToolCall",
    "ToolDefinition",
    "TranscriptEvent",
    "UnknownProviderError",
    "get_registry",
]
