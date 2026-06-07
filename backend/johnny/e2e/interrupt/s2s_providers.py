"""Real S2S provider loader for the interrupt harness (Johnny-ckz.22).

Mirrors :mod:`real_providers` but for the unified S2S pipeline path.
A single :class:`S2SProvider` collapses the STT+LLM+TTS triple into
one bidirectional session — Johnny-ckz.19 (OpenAI GPT-Realtime) and
Johnny-ckz.20 (Gemini Live) are the two production options.

Two loader entry points:

* :func:`load_s2s_provider_from_json` — pull the active S2S row from a
  providers.json file (same shape the API seeder accepts), or pin by
  ``provider_name``. Mirrors the split-mode loader so the same JSON can
  drive either pipeline.
* :func:`load_s2s_provider_from_env` — synthesise the credentials
  directly from environment variables (``OPENAI_API_KEY``,
  ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY``). This is what the CLI uses
  when no providers JSON is provided and the operator passes
  ``--provider=openai-realtime`` or ``--provider=gemini-live``.

Both return a closeable :class:`S2SProviderBundle` so the HTTP / WS
client owned by the adapter releases its pool cleanly between runs.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Side-effect: registers every concrete adapter against the registry.
import app.providers  # noqa: F401
from app.providers.base import ProviderConfig, ProviderKind, get_registry
from app.providers.s2s_base import S2SProvider

logger = logging.getLogger(__name__)


class S2SProviderError(RuntimeError):
    """Raised when the requested S2S provider can't be instantiated."""


@dataclass(slots=True)
class S2SProviderBundle:
    """A single S2S adapter + display metadata + close hook."""

    provider: S2SProvider
    display_name: str
    provider_name: str
    voice_id: str | None = None

    async def aclose(self) -> None:
        try:
            await self.provider.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            logger.warning(
                "S2S provider %s close() raised; continuing",
                self.provider.name,
            )


# Provider-name → required env var(s). First non-empty value wins.
_ENV_KEYS_FOR_PROVIDER: dict[str, tuple[str, ...]] = {
    "openai-realtime": ("OPENAI_API_KEY",),
    "gemini-live": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}

# Default voice when the operator doesn't override one.
_DEFAULT_VOICES: dict[str, str] = {
    "openai-realtime": "marin",
    "gemini-live": "Kore",
}


def supported_s2s_providers() -> tuple[str, ...]:
    """Provider names the CLI's ``--provider`` flag accepts."""
    return tuple(_ENV_KEYS_FOR_PROVIDER.keys())


def required_env_for(provider_name: str) -> tuple[str, ...]:
    """Return the env var names that satisfy auth for ``provider_name``."""
    return _ENV_KEYS_FOR_PROVIDER.get(provider_name, ())


def _build_provider(
    config: ProviderConfig,
) -> S2SProvider:
    instance = get_registry().instantiate(config)
    if not isinstance(instance, S2SProvider):
        raise S2SProviderError(
            f"provider {config.provider_name!r} resolved to "
            f"{type(instance).__name__}, not an S2SProvider"
        )
    return instance


# Per-provider option overlay applied when the harness wants a
# deterministic turn-detection mode (the synthetic-tone scenarios don't
# fire either server's VAD, so the turn never closes). Keys are
# provider-specific because OpenAI Realtime and Gemini Live disable
# server VAD via different option names.
_DISABLE_SERVER_VAD_OPTIONS: dict[str, dict[str, Any]] = {
    "openai-realtime": {"turn_detection": "none"},
    "gemini-live": {"disable_server_vad": True},
}


def disable_server_vad_options(provider_name: str) -> dict[str, Any]:
    """Per-provider options to drive turns explicitly instead of via VAD.

    The synthetic-tone speaker audio the harness generates doesn't
    reliably fire either OpenAI's or Gemini's server-side VAD (it's a
    440 Hz tone, not real speech). Forcing manual-VAD lets the harness
    drive deterministic turns via ``commit_user_turn`` / ``activityEnd``
    — exactly what the providers' own live integration tests do.
    """
    return dict(_DISABLE_SERVER_VAD_OPTIONS.get(provider_name, {}))


def load_s2s_provider_from_env(
    provider_name: str,
    *,
    voice_id: str | None = None,
    display_name: str | None = None,
    extra_options: dict[str, Any] | None = None,
) -> S2SProviderBundle:
    """Build an :class:`S2SProvider` from environment-variable credentials.

    Reads the API key from the provider's env var (see
    :data:`_ENV_KEYS_FOR_PROVIDER`). Raises :class:`S2SProviderError`
    when no env var is set — the caller (the CLI) translates that to a
    SKIP with a clear reason rather than a crash.

    ``extra_options`` is merged into the provider config's options
    dict; the harness uses this to enable manual-VAD mode so synthetic
    audio drives deterministic turns.
    """
    if provider_name not in _ENV_KEYS_FOR_PROVIDER:
        raise S2SProviderError(
            f"provider {provider_name!r} not supported by the harness; "
            f"expected one of {supported_s2s_providers()!r}"
        )
    env_keys = _ENV_KEYS_FOR_PROVIDER[provider_name]
    api_key = ""
    used_env: str | None = None
    for key in env_keys:
        candidate = os.environ.get(key, "").strip()
        if candidate:
            api_key = candidate
            used_env = key
            break
    if not api_key:
        joined = " / ".join(env_keys)
        raise S2SProviderError(
            f"no API key found for {provider_name} — set one of {joined}"
        )

    effective_voice = voice_id or _DEFAULT_VOICES.get(provider_name)
    options: dict[str, Any] = {}
    if effective_voice is not None:
        options["voice_id"] = effective_voice
    if extra_options:
        options.update(extra_options)
    config = ProviderConfig(
        kind=ProviderKind.S2S,
        provider_name=provider_name,
        display_name=display_name or f"{provider_name} (harness)",
        credentials={"api_key": api_key},
        options=options,
    )
    provider = _build_provider(config)
    logger.info(
        "loaded S2S provider %s via env var %s (voice=%s extra=%s)",
        provider_name,
        used_env,
        effective_voice,
        sorted(extra_options.keys()) if extra_options else (),
    )
    return S2SProviderBundle(
        provider=provider,
        display_name=config.display_name,
        provider_name=provider_name,
        voice_id=effective_voice,
    )


def load_s2s_provider_from_json(
    path: Path | str,
    *,
    provider_name: str | None = None,
) -> S2SProviderBundle:
    """Build an :class:`S2SProvider` from a providers.json file.

    Selection: if ``provider_name`` is given, pick the first row whose
    ``kind == "s2s"`` AND ``provider_name`` matches. Otherwise pick the
    first ``kind == "s2s"`` row marked ``is_active=True`` — if none is
    active, fall back to the first s2s row in the file.
    """
    src = Path(path)
    if not src.exists():
        raise S2SProviderError(f"providers JSON {src} does not exist")
    try:
        raw = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise S2SProviderError(f"could not parse {src}: {exc}") from exc
    if not isinstance(raw, dict):
        raise S2SProviderError(
            f"providers JSON {src} is not a JSON object at the top level"
        )
    version = raw.get("version")
    if version != 1:
        raise S2SProviderError(
            f"providers JSON version {version!r} is not supported (need 1)"
        )
    providers = raw.get("providers") or []
    if not isinstance(providers, list):
        raise S2SProviderError(
            f"providers JSON {src} is missing a 'providers' array"
        )
    s2s_rows = [
        p
        for p in providers
        if isinstance(p, dict) and p.get("kind") == ProviderKind.S2S.value
    ]
    if not s2s_rows:
        raise S2SProviderError(
            f"no S2S rows in {src} — add one via /providers UI or pass "
            "--provider=<name> to synthesise from env"
        )
    if provider_name is not None:
        matches = [
            row for row in s2s_rows if row.get("provider_name") == provider_name
        ]
        if not matches:
            raise S2SProviderError(
                f"no S2S row with provider_name={provider_name!r} in {src}"
            )
        chosen = matches[0]
    else:
        actives = [row for row in s2s_rows if row.get("is_active")]
        chosen = actives[0] if actives else s2s_rows[0]

    raw_creds = chosen.get("credentials") or {}
    raw_opts = chosen.get("options") or {}
    config = ProviderConfig(
        kind=ProviderKind.S2S,
        provider_name=str(chosen["provider_name"]),
        display_name=str(chosen.get("display_name") or chosen["provider_name"]),
        credentials={str(k): str(v) for k, v in raw_creds.items()},
        options=dict(raw_opts),
    )
    provider = _build_provider(config)
    voice = config.options.get("voice_id") or _DEFAULT_VOICES.get(
        config.provider_name
    )
    logger.info(
        "loaded S2S provider %s (display=%s) from %s",
        config.provider_name,
        config.display_name,
        src,
    )
    return S2SProviderBundle(
        provider=provider,
        display_name=config.display_name,
        provider_name=config.provider_name,
        voice_id=str(voice) if voice else None,
    )


__all__ = [
    "S2SProviderBundle",
    "S2SProviderError",
    "disable_server_vad_options",
    "load_s2s_provider_from_env",
    "load_s2s_provider_from_json",
    "required_env_for",
    "supported_s2s_providers",
]
