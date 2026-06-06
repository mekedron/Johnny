"""Provider catalog used by the wizard.

This is a wizard-only description layer — it does not replace the
backend's :mod:`app.providers` package. The wizard needs human-friendly
metadata (display names, signup URLs, "best for" notes, allowed model
sizes) that the runtime adapters do not carry, and we keep this here so
the runtime adapters stay slim.

The shape mirrors the ``POST /providers`` payload:

* ``kind`` — STT/LLM/TTS
* ``provider_name`` — verbatim string the backend factory key uses
  (``"deepgram"``, ``"openai"``, ``"anthropic"``, ``"gemini"``,
  ``"openai-realtime"``, ``"openai-compatible"``, ``"faster-whisper"``,
  ``"piper"``, ``"elevenlabs"``)
* ``display_name`` — default text the user can override
* ``credential_keys`` — what the wizard needs to prompt for
* ``default_options`` — what to put into the ``options`` dict
* ``env_key`` — the existing ``.env`` variable that, if set, lets the
  wizard skip the API-key prompt

Local providers carry an ``install`` field that lists the local artifacts
they require (whisper models, piper voices, Ollama tags) so the model
download module can act on the user's selection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Kind(StrEnum):
    """Provider kind, matching :class:`app.providers.base.ProviderKind`."""

    STT = "stt"
    LLM = "llm"
    TTS = "tts"


class Hosting(StrEnum):
    """Whether the provider runs in the cloud or fully on-device."""

    CLOUD = "cloud"
    LOCAL = "local"


@dataclass(frozen=True)
class ProviderChoice:
    """A single user-visible option in a wizard provider menu."""

    kind: Kind
    hosting: Hosting
    provider_name: str
    label: str
    display_name: str
    summary: str
    signup_url: str | None
    credential_keys: tuple[str, ...] = ()
    default_options: dict[str, Any] = field(default_factory=dict)
    env_key: str | None = None  # `.env` var that pre-fills the API key
    install: dict[str, Any] | None = None  # local-only: what to download
    notes: str | None = None  # extra one-line tip


# --- Local-model recommendation tables -------------------------------------


WHISPER_MODELS = [
    {
        "id": "tiny.en",
        "label": "tiny.en (~75 MB, fastest, OK quality)",
        "size_mb": 75,
        "huggingface_repo": "Systran/faster-whisper-tiny.en",
    },
    {
        "id": "base.en",
        "label": "base.en (~140 MB, good quality, RECOMMENDED)",
        "size_mb": 140,
        "huggingface_repo": "Systran/faster-whisper-base.en",
    },
    {
        "id": "small.en",
        "label": "small.en (~460 MB, very good)",
        "size_mb": 460,
        "huggingface_repo": "Systran/faster-whisper-small.en",
    },
    {
        "id": "medium.en",
        "label": "medium.en (~1.5 GB, excellent)",
        "size_mb": 1_500,
        "huggingface_repo": "Systran/faster-whisper-medium.en",
    },
    {
        "id": "large-v3",
        "label": "large-v3 (~3.0 GB, best, multilingual)",
        "size_mb": 3_000,
        "huggingface_repo": "Systran/faster-whisper-large-v3",
    },
]

OLLAMA_MODELS = [
    {
        "id": "llama3.1:8b-instruct-q4_K_M",
        "label": "Llama 3.1 8B Instruct Q4 (~4.9 GB, Meta flagship, RECOMMENDED)",
        "size_mb": 4_900,
    },
    {
        "id": "qwen2.5:7b-instruct-q4_K_M",
        "label": "Qwen 2.5 7B Instruct Q4 (~4.7 GB, very capable)",
        "size_mb": 4_700,
    },
    {
        "id": "qwen2.5:3b-instruct-q4_K_M",
        "label": "Qwen 2.5 3B Instruct Q4 (~1.9 GB, smaller, faster)",
        "size_mb": 1_900,
    },
]

PIPER_VOICES = [
    {
        "id": "en_US-amy-medium",
        "label": "en_US-amy-medium (female, neutral, RECOMMENDED)",
        "size_mb": 63,
        "onnx_url": (
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/"
            "amy/medium/en_US-amy-medium.onnx"
        ),
        "json_url": (
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/"
            "amy/medium/en_US-amy-medium.onnx.json"
        ),
    },
    {
        "id": "en_US-ryan-medium",
        "label": "en_US-ryan-medium (male, neutral)",
        "size_mb": 63,
        "onnx_url": (
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/"
            "ryan/medium/en_US-ryan-medium.onnx"
        ),
        "json_url": (
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/"
            "ryan/medium/en_US-ryan-medium.onnx.json"
        ),
    },
    {
        "id": "en_GB-alan-medium",
        "label": "en_GB-alan-medium (British male)",
        "size_mb": 63,
        "onnx_url": (
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/"
            "alan/medium/en_GB-alan-medium.onnx"
        ),
        "json_url": (
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/"
            "alan/medium/en_GB-alan-medium.onnx.json"
        ),
    },
]


# --- Catalog ---------------------------------------------------------------
# The order matters: the first item with hosting=LOCAL is the wizard's
# recommended local choice, and the first with hosting=CLOUD is the
# recommended cloud choice.


CATALOG: list[ProviderChoice] = [
    # --- STT --------------------------------------------------------------
    ProviderChoice(
        kind=Kind.STT,
        hosting=Hosting.LOCAL,
        provider_name="faster-whisper",
        label="faster-whisper (local Whisper via CTranslate2)",
        display_name="Local Whisper",
        summary="Runs entirely on-device. CPU-friendly. No audio leaves your host.",
        signup_url=None,
        default_options={"model_size": "base.en", "device": "cpu", "compute_type": "int8"},
        install={"kind": "whisper", "default_model": "base.en", "models": WHISPER_MODELS},
        notes="Recommended for the local-first setup.",
    ),
    ProviderChoice(
        kind=Kind.STT,
        hosting=Hosting.CLOUD,
        provider_name="deepgram",
        label="Deepgram (lowest latency, best for live conversation)",
        display_name="Deepgram",
        summary="Lowest streaming latency. Excellent diarization. Pay-as-you-go.",
        signup_url="https://console.deepgram.com/signup",
        credential_keys=("api_key",),
        default_options={"model": "nova-2"},
        env_key="DEEPGRAM_API_KEY",
    ),
    ProviderChoice(
        kind=Kind.STT,
        hosting=Hosting.CLOUD,
        provider_name="openai-realtime",
        label="OpenAI Realtime (streaming Whisper-large via WebSocket)",
        display_name="OpenAI Realtime",
        summary="OpenAI Realtime STT. Solid accuracy, slightly higher latency than Deepgram.",
        signup_url="https://platform.openai.com/signup",
        credential_keys=("api_key",),
        default_options={"model": "whisper-1"},
        env_key="OPENAI_API_KEY",
    ),
    # --- LLM --------------------------------------------------------------
    ProviderChoice(
        kind=Kind.LLM,
        hosting=Hosting.LOCAL,
        provider_name="openai-compatible",
        label="Ollama (local LLM via OpenAI-compatible endpoint)",
        display_name="Local Ollama",
        summary="Llama / Qwen on your machine via Ollama. Runs on CPU or Apple Silicon.",
        signup_url=None,
        credential_keys=("api_key",),
        default_options={
            "base_url": "http://host.docker.internal:11434/v1",
            "model": "llama3.1:8b-instruct-q4_K_M",
        },
        install={
            "kind": "ollama",
            "default_model": "llama3.1:8b-instruct-q4_K_M",
            "models": OLLAMA_MODELS,
        },
        notes="Recommended for the local-first setup. Ollama runs on the host, "
        "the API container reaches it via host.docker.internal.",
    ),
    ProviderChoice(
        kind=Kind.LLM,
        hosting=Hosting.CLOUD,
        provider_name="anthropic",
        label="Anthropic (Claude — best reasoning quality)",
        display_name="Claude",
        summary="Anthropic Claude. Strong reasoning, careful tone, low hallucination.",
        signup_url="https://console.anthropic.com/",
        credential_keys=("api_key",),
        default_options={"model": "claude-3-5-haiku-20241022"},
        env_key="ANTHROPIC_API_KEY",
    ),
    ProviderChoice(
        kind=Kind.LLM,
        hosting=Hosting.CLOUD,
        provider_name="openai",
        label="OpenAI (GPT-4o family — broad ecosystem)",
        display_name="GPT-4o mini",
        summary="OpenAI GPT-4o family. Solid all-rounder, native tool calling.",
        signup_url="https://platform.openai.com/signup",
        credential_keys=("api_key",),
        default_options={"model": "gpt-4o-mini"},
        env_key="OPENAI_API_KEY",
    ),
    ProviderChoice(
        kind=Kind.LLM,
        hosting=Hosting.CLOUD,
        provider_name="gemini",
        label="Google Gemini (large context, fast Flash tier)",
        display_name="Gemini Flash",
        summary="Google Gemini. 1M-token context, JSON-mode, very fast Flash tier.",
        signup_url="https://aistudio.google.com/app/apikey",
        credential_keys=("api_key",),
        default_options={"model": "gemini-1.5-flash"},
        env_key="GOOGLE_API_KEY",
    ),
    # --- TTS --------------------------------------------------------------
    ProviderChoice(
        kind=Kind.TTS,
        hosting=Hosting.LOCAL,
        provider_name="piper",
        label="Piper (local TTS, small voice models)",
        display_name="Local Piper",
        summary="Local Piper TTS. ~60 MB voices, CPU-only, no audio leaves host.",
        signup_url=None,
        default_options={"voice_id": "en_US-amy-medium"},
        install={"kind": "piper", "default_voice": "en_US-amy-medium", "voices": PIPER_VOICES},
        notes="Recommended for the local-first setup.",
    ),
    ProviderChoice(
        kind=Kind.TTS,
        hosting=Hosting.CLOUD,
        provider_name="elevenlabs",
        label="ElevenLabs (highest-quality cloud voices)",
        display_name="ElevenLabs",
        summary="ElevenLabs. Most natural cloud voices, fast streaming.",
        signup_url="https://elevenlabs.io/sign-up",
        credential_keys=("api_key",),
        default_options={"voice_id": "EXAVITQu4vr4xnSDxMaL"},
        env_key="ELEVENLABS_API_KEY",
    ),
    ProviderChoice(
        kind=Kind.TTS,
        hosting=Hosting.CLOUD,
        provider_name="openai",
        label="OpenAI TTS (consistent neutral voices)",
        display_name="OpenAI TTS",
        summary="OpenAI TTS. Consistent neutral voices, integrates with OpenAI key.",
        signup_url="https://platform.openai.com/signup",
        credential_keys=("api_key",),
        default_options={"voice_id": "alloy"},
        env_key="OPENAI_API_KEY",
    ),
]


def choices_for(kind: Kind, hosting: Hosting | None = None) -> list[ProviderChoice]:
    """Filter the catalog by ``kind`` and (optionally) ``hosting``."""
    out: list[ProviderChoice] = []
    for entry in CATALOG:
        if entry.kind is not kind:
            continue
        if hosting is not None and entry.hosting is not hosting:
            continue
        out.append(entry)
    return out


def recommended_local(kind: Kind) -> ProviderChoice | None:
    """First local provider for ``kind`` (the wizard's recommended choice)."""
    options = choices_for(kind, Hosting.LOCAL)
    return options[0] if options else None


def recommended_cloud(kind: Kind) -> ProviderChoice | None:
    """First cloud provider for ``kind`` (the wizard's recommended choice)."""
    options = choices_for(kind, Hosting.CLOUD)
    return options[0] if options else None


def get_choice(kind: Kind, provider_name: str) -> ProviderChoice | None:
    """Look up a catalog entry by kind + provider name. Returns ``None`` if unknown."""
    for entry in CATALOG:
        if entry.kind is kind and entry.provider_name == provider_name:
            return entry
    return None


def schema_for(choice: ProviderChoice) -> Any:
    """Look up the runtime ``field_schema()`` for a catalog entry.

    Returns the adapter's :class:`app.providers.schema.ProviderSchema`
    when the registered factory declares one, otherwise ``None``. The
    wizard uses this to drive prompts off the same source of truth the
    HTTP API and the /providers UI consume — adding a new credential
    key in the adapter automatically flows through to the CLI without
    touching this catalog.
    """
    # Imported lazily to avoid the wizard pulling in the SQLAlchemy
    # transitively at import time.
    from app.providers.base import ProviderKind, get_registry  # noqa: PLC0415

    registry = get_registry()
    kind = ProviderKind(choice.kind.value)
    if not registry.has(kind, choice.provider_name):
        return None
    factory = registry.get(kind, choice.provider_name)
    field_schema = getattr(factory, "field_schema", None)
    if not callable(field_schema):
        return None
    try:
        return field_schema()
    except NotImplementedError:
        return None


__all__ = [
    "CATALOG",
    "Hosting",
    "Kind",
    "OLLAMA_MODELS",
    "PIPER_VOICES",
    "ProviderChoice",
    "WHISPER_MODELS",
    "choices_for",
    "get_choice",
    "recommended_cloud",
    "recommended_local",
    "schema_for",
]
