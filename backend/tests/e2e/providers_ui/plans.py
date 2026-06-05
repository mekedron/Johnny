"""Declarative provider plans driving the E2E test matrix.

Each :class:`ProviderPlan` describes one (kind, backend) row the harness
will create, test, activate, switch, and delete. Plans are SKIP-aware:
they encode which ``.env`` keys or local-asset paths they need so the
runner can mark "no key" or "no model file" as SKIP instead of FAIL.

Why a declarative table: the same plans drive both the chrome-devtools
agent flow and the ``pytest -m e2e_ui`` API flow. Keeping the schema in
one place means a new provider only adds a row here — both layers pick
it up automatically.

The display name uses an ``e2e-`` prefix so test rows never collide with
operator-created rows even if a cleanup pass is interrupted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.providers.base import ProviderKind


class PlanOutcome(StrEnum):
    """Outcome of a single provider plan."""

    PASS = "PASS"
    SKIP = "SKIP"
    FAIL = "FAIL"


@dataclass(frozen=True)
class ProviderPlan:
    """One (kind, backend) plan in the E2E test matrix.

    ``credential_env`` and ``options_env`` resolve at runtime against
    ``os.environ`` (which docker-compose populates from ``.env`` and the
    harness inherits when invoked outside the container). Plans where
    every required ``.env`` key is blank — and any ``local_asset`` is
    missing — are marked SKIP without a network call.
    """

    plan_id: str
    kind: ProviderKind
    provider_name: str
    display_name: str
    # Static credential entries (key -> literal value). Used for fields
    # that are not driven by .env, like ``base_url`` for Ollama.
    static_credentials: dict[str, str] = field(default_factory=dict)
    # Credential entries sourced from .env: form key -> env var name.
    credential_env: dict[str, str] = field(default_factory=dict)
    # Static option entries (model, voice_id, etc.).
    static_options: dict[str, Any] = field(default_factory=dict)
    # Option entries sourced from .env: form key -> env var name.
    options_env: dict[str, str] = field(default_factory=dict)
    # A local-filesystem asset that must exist for the backend to work
    # (faster-whisper model file, piper voice file). ``None`` for cloud
    # providers. SKIP if missing.
    local_asset: Path | None = None
    # Optional probe URL to confirm a local server is reachable before
    # we try to use it (e.g. Ollama). SKIP if unreachable.
    probe_url: str | None = None
    # Human-readable hint shown in the report for SKIP rows.
    skip_hint: str = ""

    def resolved_credentials(self) -> dict[str, str]:
        """Merge static + env-derived credentials into a single map."""
        out: dict[str, str] = dict(self.static_credentials)
        for form_key, env_var in self.credential_env.items():
            value = os.environ.get(env_var, "").strip()
            if value:
                out[form_key] = value
        return out

    def resolved_options(self) -> dict[str, Any]:
        """Merge static + env-derived options into a single map."""
        out: dict[str, Any] = dict(self.static_options)
        for form_key, env_var in self.options_env.items():
            value = os.environ.get(env_var, "").strip()
            if value:
                out[form_key] = value
        return out

    def requires_keys(self) -> list[str]:
        """The .env keys this plan needs at least one of (for SKIP messages)."""
        return list(self.credential_env.values()) or list(self.options_env.values())


# --- The matrix -----------------------------------------------------------
#
# Order: per-kind, cloud first then local. Display names use a stable
# ``e2e-<kind>-<backend>`` slug so screenshots and assertions can match.


PROVIDER_PLANS: tuple[ProviderPlan, ...] = (
    # STT
    ProviderPlan(
        plan_id="stt-deepgram",
        kind=ProviderKind.STT,
        provider_name="deepgram",
        display_name="e2e-stt-deepgram",
        credential_env={"api_key": "DEEPGRAM_API_KEY"},
        static_options={"model": "nova-2"},
        skip_hint="DEEPGRAM_API_KEY blank in .env",
    ),
    ProviderPlan(
        plan_id="stt-openai-realtime",
        kind=ProviderKind.STT,
        provider_name="openai-realtime",
        display_name="e2e-stt-openai-realtime",
        credential_env={"api_key": "OPENAI_API_KEY"},
        static_options={"model": "whisper-1"},
        skip_hint="OPENAI_API_KEY blank in .env",
    ),
    ProviderPlan(
        plan_id="stt-faster-whisper",
        kind=ProviderKind.STT,
        provider_name="faster-whisper",
        display_name="e2e-stt-faster-whisper",
        static_options={"model_size": "tiny"},
        local_asset=Path("/var/lib/johnny/whisper-models"),
        skip_hint="whisper_models volume empty — no local model available",
    ),
    # LLM
    ProviderPlan(
        plan_id="llm-openai",
        kind=ProviderKind.LLM,
        provider_name="openai",
        display_name="e2e-llm-openai",
        credential_env={"api_key": "OPENAI_API_KEY"},
        static_options={"model": "gpt-4o-mini"},
        skip_hint="OPENAI_API_KEY blank in .env",
    ),
    ProviderPlan(
        plan_id="llm-anthropic",
        kind=ProviderKind.LLM,
        provider_name="anthropic",
        display_name="e2e-llm-anthropic",
        credential_env={"api_key": "ANTHROPIC_API_KEY"},
        # claude-haiku-4-5 is the cheapest current model. Old ``claude-3-5``
        # tags 404 on accounts created after Anthropic's tier cleanup; the
        # 4.5 family is the lowest version every active account can call.
        static_options={"model": "claude-haiku-4-5"},
        skip_hint="ANTHROPIC_API_KEY blank in .env",
    ),
    ProviderPlan(
        plan_id="llm-gemini",
        kind=ProviderKind.LLM,
        provider_name="gemini",
        display_name="e2e-llm-gemini",
        credential_env={"api_key": "GOOGLE_API_KEY"},
        # ``gemini-1.5-flash`` and ``gemini-2.0-flash`` are retired on
        # the v1beta endpoint. ``gemini-2.5-flash`` is the current cheap
        # default served to consumer keys.
        static_options={"model": "gemini-2.5-flash"},
        skip_hint="GOOGLE_API_KEY (or GEMINI_API_KEY) blank in .env",
    ),
    ProviderPlan(
        plan_id="llm-openai-compatible-ollama",
        kind=ProviderKind.LLM,
        provider_name="openai-compatible",
        display_name="e2e-llm-ollama",
        # Ollama runs on the host; the API container reaches it via
        # ``host.docker.internal``. ``OLLAMA_BASE_URL`` (if set) wins so
        # non-Docker runs can point at ``http://localhost:11434/v1``.
        options_env={"base_url": "OLLAMA_BASE_URL"},
        static_options={
            "base_url": "http://host.docker.internal:11434/v1",
            "model": "qwen2.5:7b-instruct-q4_K_M",
        },
        # Probe runs from the host machine — Ollama listens on localhost
        # there even when the API will reach it via host.docker.internal.
        probe_url="http://localhost:11434/api/tags",
        skip_hint="Ollama not reachable on localhost:11434",
    ),
    # TTS
    ProviderPlan(
        plan_id="tts-elevenlabs",
        kind=ProviderKind.TTS,
        provider_name="elevenlabs",
        display_name="e2e-tts-elevenlabs",
        credential_env={"api_key": "ELEVENLABS_API_KEY"},
        static_options={
            # ElevenLabs' default ``Rachel`` voice id — every account has it.
            "voice_id": "21m00Tcm4TlvDq8ikWAM",
            "model_id": "eleven_multilingual_v2",
        },
        skip_hint="ELEVENLABS_API_KEY blank in .env",
    ),
    ProviderPlan(
        plan_id="tts-openai",
        kind=ProviderKind.TTS,
        provider_name="openai",
        display_name="e2e-tts-openai",
        credential_env={"api_key": "OPENAI_API_KEY"},
        static_options={"voice_id": "alloy", "model": "tts-1"},
        skip_hint="OPENAI_API_KEY blank in .env",
    ),
    ProviderPlan(
        plan_id="tts-piper",
        kind=ProviderKind.TTS,
        provider_name="piper",
        display_name="e2e-tts-piper",
        static_options={"voice_id": "en_US-amy-low"},
        # The compose volume is ``piper_models`` (the PRD nicknames it
        # ``piper_voices``; both point at the same on-disk dir).
        local_asset=Path("/var/lib/johnny/piper-models"),
        skip_hint="piper_models volume empty — no local voice available",
    ),
)


def plans_by_kind(kind: ProviderKind) -> tuple[ProviderPlan, ...]:
    """Return plans for one kind, in declared order."""
    return tuple(p for p in PROVIDER_PLANS if p.kind is kind)


__all__ = [
    "PROVIDER_PLANS",
    "PlanOutcome",
    "ProviderPlan",
    "plans_by_kind",
]
