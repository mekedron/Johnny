"""Real provider bundle for the interrupt harness (Johnny-ckz.4).

The default in-process harness uses scripted providers
(:mod:`johnny.e2e.interrupt.providers`) for deterministic, reproducible
assertions. The bead Johnny-ckz.4 calls for REAL models — the same
production STT / LLM / TTS adapters wired to the production registry —
so the harness reproduces what fails in real meetings rather than only
what fails in a synthetic timeline.

This module loads an active provider per kind from the JSON file format
that :mod:`app.services.providers_seed` accepts (the same shape the
``providers.json`` seeder consumes; see :doc:`Johnny-d3e`/:doc:`Johnny-k3z`)
and instantiates them via the process-wide :class:`ProviderRegistry`.

Selection rules:

* For each kind, prefer the entry with ``is_active == True``.
* If no entry is active for a kind, fall back to the first entry of that
  kind that carries non-empty credentials. This is what lets the harness
  swap in Deepgram for STT when the production config has
  ``faster-whisper`` active (no model on the developer machine) but a
  Deepgram key is sitting alongside it in the file.
* Raise :class:`RealProviderError` if no usable entry exists for one of
  STT, LLM, or TTS.

The returned bundle is closed via :meth:`RealProviderBundle.aclose` so
HTTP clients in the adapters release their pools cleanly between
scenario runs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Importing app.providers triggers registration of every adapter against
# the process-wide ProviderRegistry — without this, instantiating a kind
# raises UnknownProviderError even if the JSON is well-formed.
import app.providers  # noqa: F401 — side effect: registry population
from app.providers.base import (
    LLMProvider,
    ProviderConfig,
    ProviderKind,
    STTProvider,
    TTSProvider,
    get_registry,
)

logger = logging.getLogger(__name__)


class RealProviderError(RuntimeError):
    """Raised when the JSON file does not contain a usable provider trio."""


@dataclass(slots=True)
class RealProviderBundle:
    """One real STT / LLM / TTS adapter triple plus their source config rows."""

    stt: STTProvider
    llm: LLMProvider
    tts: TTSProvider
    stt_display: str
    llm_display: str
    tts_display: str

    async def aclose(self) -> None:
        """Release any HTTP clients / model handles the adapters hold."""
        for provider in (self.stt, self.llm, self.tts):
            try:
                await provider.close()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "provider %s close() raised; continuing", provider.name
                )


def _load_entries(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RealProviderError(
            f"providers JSON {path} is not a JSON object at the top level"
        )
    version = raw.get("version")
    if version != 1:
        raise RealProviderError(
            f"providers JSON version {version!r} is not supported (need 1)"
        )
    providers = raw.get("providers")
    if not isinstance(providers, list):
        raise RealProviderError(
            f"providers JSON {path} is missing a 'providers' array"
        )
    return [p for p in providers if isinstance(p, dict)]


def _pick_for_kind(
    entries: list[dict[str, Any]],
    kind: ProviderKind,
    *,
    excluded_names: set[str] | None = None,
) -> dict[str, Any]:
    """Pick the best entry for ``kind`` from ``entries`` per the rules above.

    ``excluded_names`` lets callers veto providers known to require local
    resources the harness can't reasonably bring up (e.g. faster-whisper
    needs a downloaded model). Excluded providers are still considered if
    nothing else matches, but only after a warning — the operator may have
    actually staged the models.
    """
    kind_entries = [e for e in entries if e.get("kind") == kind.value]
    if not kind_entries:
        raise RealProviderError(f"no providers of kind={kind.value} in JSON")

    excluded = excluded_names or set()
    preferred = [
        e for e in kind_entries if e.get("provider_name") not in excluded
    ]

    def _has_credentials(entry: dict[str, Any]) -> bool:
        creds = entry.get("credentials")
        if not isinstance(creds, dict):
            return False
        return any(bool(v) for v in creds.values())

    # Local providers (piper, faster-whisper) carry empty creds; that's
    # fine and they should not be skipped by the credential check below.
    def _is_local(entry: dict[str, Any]) -> bool:
        return entry.get("provider_name") in {"faster-whisper", "piper"}

    def _usable(entry: dict[str, Any]) -> bool:
        return _is_local(entry) or _has_credentials(entry)

    # 1) is_active=True among preferred and usable.
    active_preferred = [
        e for e in preferred if e.get("is_active") and _usable(e)
    ]
    if active_preferred:
        return active_preferred[0]

    # 2) any usable among preferred.
    usable_preferred = [e for e in preferred if _usable(e)]
    if usable_preferred:
        return usable_preferred[0]

    # 3) any usable at all (including excluded — warn).
    usable_all = [e for e in kind_entries if _usable(e)]
    if usable_all:
        chosen = usable_all[0]
        logger.warning(
            "real-provider harness using %s for kind=%s — none of the "
            "preferred providers had credentials in the JSON",
            chosen.get("provider_name"),
            kind.value,
        )
        return chosen

    raise RealProviderError(
        f"no entry for kind={kind.value} has credentials in the JSON"
    )


def _build_config(entry: dict[str, Any]) -> ProviderConfig:
    kind = ProviderKind(entry["kind"])
    return ProviderConfig(
        kind=kind,
        provider_name=str(entry["provider_name"]),
        display_name=str(entry.get("display_name") or entry["provider_name"]),
        credentials={
            str(k): str(v)
            for k, v in (entry.get("credentials") or {}).items()
        },
        options=dict(entry.get("options") or {}),
    )


def _synthesise_openai_tts_entry(llm_entry: dict[str, Any]) -> dict[str, Any]:
    """Build a fake JSON entry for OpenAI TTS using the OpenAI LLM api_key.

    OpenAI's hosted TTS endpoint sits on the same key as the chat-completion
    endpoint, so reusing the LLM credential lets the harness fall back
    transparently when the active TTS row is unusable (e.g. ElevenLabs has
    exhausted its monthly credit pool — a real surface we observed while
    wiring this).
    """
    api_key = (llm_entry.get("credentials") or {}).get("api_key", "")
    return {
        "kind": "tts",
        "provider_name": "openai",
        "display_name": "OpenAI TTS (harness fallback)",
        "credentials": {"api_key": api_key},
        "options": {
            "voice_id": "alloy",
            "model": "tts-1",
            "native_sample_rate": 24000,
        },
        "is_active": True,
    }


def load_real_providers(
    path: Path | str,
    *,
    excluded_stt_names: set[str] | None = None,
    excluded_tts_names: set[str] | None = None,
    fallback_tts_to_openai: bool = False,
) -> RealProviderBundle:
    """Build a :class:`RealProviderBundle` from a providers JSON file.

    By default we exclude ``faster-whisper`` for STT selection because the
    harness host typically lacks both the package and the model weights;
    Deepgram (cloud, in the same file) is the natural substitute. Pass
    ``excluded_stt_names=set()`` to force the active-row choice.

    ``fallback_tts_to_openai`` is the escape hatch when every TTS row in
    the JSON is blocked — typically because ElevenLabs has run out of
    credits, which we observed mid-development. The harness synthesises
    an OpenAI TTS entry from the OpenAI LLM api_key and uses that
    instead. Adds a strong signal in the log so the operator isn't
    misled into thinking the configured provider was exercised.
    """
    src = Path(path)
    if not src.exists():
        raise RealProviderError(f"providers JSON {src} does not exist")
    entries = _load_entries(src)

    stt_excluded = (
        excluded_stt_names
        if excluded_stt_names is not None
        else {"faster-whisper"}
    )
    tts_excluded = excluded_tts_names or set()

    stt_entry = _pick_for_kind(
        entries, ProviderKind.STT, excluded_names=stt_excluded
    )
    llm_entry = _pick_for_kind(entries, ProviderKind.LLM)
    if fallback_tts_to_openai:
        tts_entry = _synthesise_openai_tts_entry(llm_entry)
        logger.warning(
            "real-provider harness: forcing OpenAI TTS fallback "
            "(configured TTS providers in JSON were skipped)"
        )
    else:
        tts_entry = _pick_for_kind(
            entries, ProviderKind.TTS, excluded_names=tts_excluded
        )

    registry = get_registry()
    stt = registry.instantiate(_build_config(stt_entry))
    llm = registry.instantiate(_build_config(llm_entry))
    tts = registry.instantiate(_build_config(tts_entry))

    assert isinstance(stt, STTProvider)
    assert isinstance(llm, LLMProvider)
    assert isinstance(tts, TTSProvider)

    logger.info(
        "real-provider bundle: stt=%s llm=%s tts=%s",
        stt_entry.get("display_name") or stt_entry.get("provider_name"),
        llm_entry.get("display_name") or llm_entry.get("provider_name"),
        tts_entry.get("display_name") or tts_entry.get("provider_name"),
    )
    return RealProviderBundle(
        stt=stt,
        llm=llm,
        tts=tts,
        stt_display=str(stt_entry.get("display_name") or stt_entry["provider_name"]),
        llm_display=str(llm_entry.get("display_name") or llm_entry["provider_name"]),
        tts_display=str(tts_entry.get("display_name") or tts_entry["provider_name"]),
    )


__all__ = [
    "RealProviderBundle",
    "RealProviderError",
    "load_real_providers",
]
