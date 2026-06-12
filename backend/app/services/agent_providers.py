"""Per-agent provider resolution — pins become the session payload (Johnny-trt.42).

The :class:`~app.db.models.Agent` row stores provider *pins* (three LLM role
slots + a TTS pin with voice/options, Johnny-trt.41) and
:func:`~app.services.agents.build_agent_snapshot` freezes them onto
``bot_sessions.agent_snapshot`` at dispatch. This module is where those pins
finally become REAL: :func:`resolve_agent_provider_payload` takes the
global-active provider payload
(:func:`~app.services.provider_payload.build_provider_payload`) plus the
frozen snapshot and produces the session's effective ``provider_config`` —
the exact dict the dispatched agent worker / in-process browser runner build
their adapters from. Both session-start surfaces (the Meet scheduler and the
browser API) call it right after the snapshot freeze, so a session runs the
agent's providers without any turn-time DB reads.

Resolution per role (the documented fallback chain: agent role slot →
global active for that kind):

* **answer LLM** — ``answer_llm_provider_id`` replaces the payload's ``llm``
  entry (the AgentSession reply node + the allowed-reply coercion).
* **router LLM** — ``router_llm_provider_id`` lands under the optional
  ``router_llm`` key (:data:`~johnny.agent.job_config.PROVIDER_CONFIG_ROUTER_LLM_KEY`),
  emitted only when it resolves to a *different provider row* than the
  effective answer entry; absent → the session reuses the ``llm`` entry (and
  one live instance) for both stages. Notably, an agent that pins ONLY the
  answer slot still gets an explicit ``router_llm`` pointing at the global
  active LLM — the router role inherits the *global* chain, not the agent's
  answer pin, so cheap triage stays cheap when the answer model goes cloud.
* **reasoning LLM** — ``reasoning_llm_provider_id`` becomes the
  credential-less ``reasoning_llm`` descriptor
  (:func:`~johnny.agent.job_config.reasoning_llm_from_provider_config`),
  stamped onto ``agent_tasks`` rows at delegation time so the worker executor
  can use the requesting agent's reasoning model once multi-step kinds land.
* **TTS** — ``tts_provider_id`` replaces the ``tts`` entry, with the agent's
  ``tts_options`` merged over the row's config and ``tts_voice_id`` written
  into ``options["voice_id"]`` — the exact key the adapter factory threads
  into :class:`~johnny.agent.adapters.johnny_tts.JohnnyTTS`, so the voice is
  applied at the adapter layer (absent voice → the provider's own default).

**Pins honor inactive rows — deliberately.** ``provider_credentials.is_active``
selects the single GLOBAL default per kind (partial unique index
``uq_provider_credentials_active_per_kind``: at most one active row per
kind), so two agents pinned to two different TTS providers — the whole point
of per-agent voices — necessarily reference at least one inactive row. A pin
therefore resolves whenever the row exists, is the right kind, and its
credentials decrypt; the bead's "missing/inactive → fall back" rule applies
to pins that are genuinely *unusable* (row vanished, kind mismatch from a
stale snapshot, undecryptable ciphertext). The per-start playground override
path (``_resolve_provider_overrides``) set this precedent: it loads rows by
id with no ``is_active`` check.

An unusable pin falls back to the global-active entry for that kind and
emits a :class:`ProviderFallbackWarning`, which
:func:`persist_provider_fallback_warnings` writes into ``session_timings`` as
a turn-0 ``provider_switch`` row — the session detail page's activity log
renders it (the stage label already exists), naming the agent and the missing
provider. The session always still starts; resolution failures degrade, never
block.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.db.models import ProviderCredential, SessionTiming
from app.providers.base import ProviderKind
from app.security.crypto import CredentialCrypto, CryptoError, decrypt_json
from johnny.agent.job_config import (
    PROVIDER_CONFIG_REASONING_LLM_KEY,
    PROVIDER_CONFIG_ROUTER_LLM_KEY,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
# Surface the per-session "agent providers resolved" breadcrumb (and the
# fallback warnings) in ``docker logs api`` — the root logger defaults to
# WARNING, which would hide the INFO summary line that is the "provider name
# in logs" half of the trt.42 acceptance. Mirrors the adapter factory's
# warm-up logging idiom (johnny.agent.adapters.factory); attach only when the
# chain has none of our own so a future project-wide logging setup wins.
logger.setLevel(logging.INFO)
if not any(getattr(h, "_johnny_agent_providers", False) for h in logger.handlers):
    _h = logging.StreamHandler()
    _h.setLevel(logging.INFO)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    _h._johnny_agent_providers = True  # type: ignore[attr-defined]
    logger.addHandler(_h)
    logger.propagate = False

# The roles a warning can name, in the order they are resolved.
ROLE_ROUTER_LLM = "router_llm"
ROLE_ANSWER_LLM = "answer_llm"
ROLE_REASONING_LLM = "reasoning_llm"
ROLE_TTS = "tts"

# Why a pin was unusable. ``missing`` = the row id no longer exists;
# ``wrong_kind`` = the row exists but is not the role's kind (a stale /
# hand-edited snapshot — CRUD validation makes this unreachable through the
# API); ``decrypt_failed`` = the ciphertext no longer decrypts (rotated
# Fernet key).
REASON_MISSING = "missing"
REASON_WRONG_KIND = "wrong_kind"
REASON_DECRYPT_FAILED = "decrypt_failed"

# The model-name option key — uniform for LLM rows (factory._LLM_MODEL_KEYS).
_LLM_MODEL_KEY = "model"


@dataclass(frozen=True, slots=True)
class ProviderFallbackWarning:
    """One unusable agent pin and what the session runs instead.

    ``fallback_provider_name`` is the display name of the global-active
    entry that serves the role instead — ``None`` when nothing is configured
    for that kind (the session then degrades exactly as an unpinned one
    would: fail-fast for LLM, ``suggest_only`` for TTS).
    """

    role: str
    agent_id: int | None
    agent_name: str
    pinned_provider_id: int
    pinned_provider_name: str | None
    fallback_provider_name: str | None
    reason: str

    @property
    def message(self) -> str:
        """Human sentence for logs + the activity-log details bag."""
        pinned = (
            f"provider #{self.pinned_provider_id}"
            if not self.pinned_provider_name
            else f"{self.pinned_provider_name} (#{self.pinned_provider_id})"
        )
        ran = (
            f"fell back to {self.fallback_provider_name}"
            if self.fallback_provider_name
            else "no fallback provider is configured for this role"
        )
        return (
            f"agent {self.agent_name!r}: pinned {self.role} {pinned} "
            f"is unusable ({self.reason}); {ran}"
        )


@dataclass(frozen=True, slots=True)
class ResolvedProviderPayload:
    """Outcome of :func:`resolve_agent_provider_payload`.

    ``payload`` is the session's effective ``provider_config``; ``warnings``
    the unusable-pin fallbacks to surface; ``summary`` a small
    ``{role: display}`` map of what actually resolved, for the session-start
    log line (the "provider name in logs" half of the trt.42 acceptance).
    """

    payload: dict[str, Any]
    warnings: tuple[ProviderFallbackWarning, ...] = ()
    summary: dict[str, str] | None = None


def _entry_from_row(
    row: ProviderCredential, crypto: CredentialCrypto
) -> dict[str, Any] | None:
    """A pinned row as a payload entry, or ``None`` when it can't decrypt."""
    try:
        credentials = decrypt_json(crypto, row.credentials_encrypted)
    except (CryptoError, ValueError) as exc:
        logger.warning(
            "agent provider pin %s/%s (#%s): credential decrypt failed: %s",
            row.kind,
            row.provider_name,
            row.id,
            exc,
        )
        return None
    return {
        "provider_id": row.id,
        "provider_name": row.provider_name,
        "display_name": row.display_name,
        "credentials": credentials,
        "options": dict(row.config or {}),
    }


def _entry_identity(entry: Mapping[str, Any] | None) -> Any:
    """Comparison key for "same provider row?" between two payload entries.

    Prefers the ``provider_id`` both the base payload and pin resolution
    stamp; falls back to the name pair for hand-built payloads (tests, older
    serialized configs) that predate the id.
    """
    if not isinstance(entry, Mapping):
        return None
    provider_id = entry.get("provider_id")
    if provider_id is not None:
        return ("id", provider_id)
    return ("name", entry.get("provider_name"), entry.get("display_name"))


def _entry_display(entry: Mapping[str, Any] | None) -> str:
    if not isinstance(entry, Mapping):
        return "none"
    return str(entry.get("display_name") or entry.get("provider_name") or "none")


@dataclass(frozen=True, slots=True)
class _PinResolution:
    """One pin lookup: the usable entry, or the warning reason + row name."""

    entry: dict[str, Any] | None = None
    reason: str | None = None
    pinned_name: str | None = None


def _resolve_pin(
    db: Session,
    crypto: CredentialCrypto,
    pin_id: int,
    expected_kind: ProviderKind,
) -> _PinResolution:
    row = db.get(ProviderCredential, pin_id)
    if row is None:
        return _PinResolution(reason=REASON_MISSING)
    if row.kind is not expected_kind:
        return _PinResolution(reason=REASON_WRONG_KIND, pinned_name=row.display_name)
    entry = _entry_from_row(row, crypto)
    if entry is None:
        return _PinResolution(reason=REASON_DECRYPT_FAILED, pinned_name=row.display_name)
    return _PinResolution(entry=entry, pinned_name=row.display_name)


def _coerce_pin_id(value: Any) -> int | None:
    """A snapshot pin value as an int id; ``None`` for absent/blank/junk."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def resolve_agent_provider_payload(
    db: Session,
    crypto: CredentialCrypto,
    *,
    base_payload: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
    context_label: str = "",
) -> ResolvedProviderPayload:
    """Apply the agent snapshot's provider pins to the global provider payload.

    ``base_payload`` is the global-active payload
    (:func:`~app.services.provider_payload.build_provider_payload`);
    ``snapshot`` the frozen agent snapshot (or ``None`` for an agent-less
    session — the payload then passes through untouched). Never raises for a
    bad pin: unusable pins degrade to the base entry with a
    :class:`ProviderFallbackWarning` so a session start can't be blocked by a
    stale snapshot. ``context_label`` (e.g. ``"bot_session=12"``) prefixes
    the per-session summary breadcrumb this logs.
    """
    payload: dict[str, Any] = {
        kind: dict(entry) if isinstance(entry, Mapping) else entry
        for kind, entry in base_payload.items()
    }
    pins: Mapping[str, Any] = {}
    if isinstance(snapshot, Mapping):
        raw_pins = snapshot.get("providers")
        if isinstance(raw_pins, Mapping):
            pins = raw_pins
    if not pins:
        return ResolvedProviderPayload(payload=payload)

    agent_id = snapshot.get("agent_id") if isinstance(snapshot, Mapping) else None
    agent_name = (
        str(snapshot.get("name") or "unknown") if isinstance(snapshot, Mapping) else "unknown"
    )
    warnings: list[ProviderFallbackWarning] = []

    def _warn(
        role: str,
        pin_id: int,
        resolution: _PinResolution,
        fallback_entry: Mapping[str, Any] | None,
    ) -> None:
        warnings.append(
            ProviderFallbackWarning(
                role=role,
                agent_id=agent_id if isinstance(agent_id, int) else None,
                agent_name=agent_name,
                pinned_provider_id=pin_id,
                pinned_provider_name=resolution.pinned_name,
                fallback_provider_name=(
                    _entry_display(fallback_entry)
                    if isinstance(fallback_entry, Mapping)
                    else None
                ),
                reason=resolution.reason or REASON_MISSING,
            )
        )

    base_llm = base_payload.get(ProviderKind.LLM.value)
    base_llm = base_llm if isinstance(base_llm, Mapping) else None
    base_tts = base_payload.get(ProviderKind.TTS.value)
    base_tts = base_tts if isinstance(base_tts, Mapping) else None

    def _resolve_llm_role(role: str, pin_value: Any) -> Mapping[str, Any] | None:
        """Effective entry for one LLM role: pin → (warn +) global active."""
        pin_id = _coerce_pin_id(pin_value)
        if pin_id is None:
            return base_llm
        resolution = _resolve_pin(db, crypto, pin_id, ProviderKind.LLM)
        if resolution.entry is not None:
            return resolution.entry
        _warn(role, pin_id, resolution, base_llm)
        return base_llm

    answer_entry = _resolve_llm_role(ROLE_ANSWER_LLM, pins.get("answer_llm_provider_id"))
    router_entry = _resolve_llm_role(ROLE_ROUTER_LLM, pins.get("router_llm_provider_id"))
    reasoning_entry = _resolve_llm_role(
        ROLE_REASONING_LLM, pins.get("reasoning_llm_provider_id")
    )

    if answer_entry is not None:
        payload[ProviderKind.LLM.value] = dict(answer_entry)
    # The router entry is emitted only when it is a different provider row
    # than the answer entry — absent means "reuse the llm entry (and the one
    # live instance)", the pre-trt.42 shape.
    if router_entry is not None and _entry_identity(router_entry) != _entry_identity(
        answer_entry
    ):
        payload[PROVIDER_CONFIG_ROUTER_LLM_KEY] = dict(router_entry)
    else:
        payload.pop(PROVIDER_CONFIG_ROUTER_LLM_KEY, None)
    # Credential-less descriptor only — the session never instantiates the
    # reasoning provider; the descriptor is stamped onto agent_tasks rows at
    # delegation time (and re-sanitized by reasoning_llm_from_provider_config
    # on the consuming side).
    if reasoning_entry is not None:
        options = reasoning_entry.get("options")
        model = options.get(_LLM_MODEL_KEY) if isinstance(options, Mapping) else None
        descriptor: dict[str, Any] = {
            "provider_name": reasoning_entry.get("provider_name"),
            "display_name": reasoning_entry.get("display_name"),
        }
        if reasoning_entry.get("provider_id") is not None:
            descriptor["provider_id"] = reasoning_entry.get("provider_id")
        if isinstance(model, str) and model:
            descriptor["model"] = model
        payload[PROVIDER_CONFIG_REASONING_LLM_KEY] = descriptor
    else:
        payload.pop(PROVIDER_CONFIG_REASONING_LLM_KEY, None)

    # TTS: the pin replaces the entry AND carries the agent's voice/options.
    # The voice merge applies ONLY when the agent's own pin resolved — a
    # voice id is provider-specific (CRUD enforces voice ⇒ pin), so applying
    # it to the fallback provider would request a nonexistent voice there.
    tts_entry = base_tts
    tts_pin_id = _coerce_pin_id(pins.get("tts_provider_id"))
    tts_voice_applied: str | None = None
    if tts_pin_id is not None:
        resolution = _resolve_pin(db, crypto, tts_pin_id, ProviderKind.TTS)
        if resolution.entry is not None:
            entry = resolution.entry
            agent_tts_options = pins.get("tts_options")
            if isinstance(agent_tts_options, Mapping) and agent_tts_options:
                entry["options"] = {**entry["options"], **dict(agent_tts_options)}
            voice = pins.get("tts_voice_id")
            if isinstance(voice, str) and voice.strip():
                entry["options"]["voice_id"] = voice.strip()
                tts_voice_applied = voice.strip()
            tts_entry = entry
        else:
            _warn(ROLE_TTS, tts_pin_id, resolution, base_tts)
    if tts_entry is not None:
        payload[ProviderKind.TTS.value] = dict(tts_entry)

    summary = {
        ROLE_ROUTER_LLM: _entry_display(
            router_entry if router_entry is not None else answer_entry
        ),
        ROLE_ANSWER_LLM: _entry_display(answer_entry),
        ROLE_REASONING_LLM: _entry_display(reasoning_entry),
        ROLE_TTS: _entry_display(tts_entry)
        + (f" voice={tts_voice_applied}" if tts_voice_applied else ""),
    }
    logger.info(
        "agent providers resolved%s agent=%r: %s%s",
        f" for {context_label}" if context_label else "",
        agent_name,
        summary,
        f" ({len(warnings)} pin fallback(s))" if warnings else "",
    )
    return ResolvedProviderPayload(
        payload=payload, warnings=tuple(warnings), summary=summary
    )


def persist_provider_fallback_warnings(
    db: Session,
    *,
    bot_session_id: int,
    warnings: tuple[ProviderFallbackWarning, ...] | list[ProviderFallbackWarning],
) -> None:
    """Surface unusable-pin fallbacks in the session's activity log.

    One ``session_timings`` row per warning: turn 0 (pre-first-turn, session
    assembly), stage ``provider_switch`` — the stage the timing vocabulary
    reserved for "active provider changed" and the session detail page
    already labels. ``provider_name`` carries what the session actually runs
    (the fallback); the ``details`` bag names the agent and the unusable pin.
    Also logs each warning so ``docker logs api`` shows the degrade. Rows are
    added to the caller's transaction (no commit here); failures are
    swallowed — visibility must never break a session start.
    """
    for warning in warnings:
        logger.warning(
            "agent provider fallback for session %s: %s", bot_session_id, warning.message
        )
        try:
            db.add(
                SessionTiming(
                    bot_session_id=bot_session_id,
                    turn_id=0,
                    stage="provider_switch",
                    started_at_ms=0,
                    duration_ms=0,
                    provider_name=warning.fallback_provider_name,
                    details={
                        "role": warning.role,
                        "agent_id": warning.agent_id,
                        "agent_name": warning.agent_name,
                        "pinned_provider_id": warning.pinned_provider_id,
                        "pinned_provider_name": warning.pinned_provider_name,
                        "reason": warning.reason,
                        "message": warning.message,
                    },
                )
            )
        except Exception:  # noqa: BLE001 — visibility must never block a start
            logger.exception(
                "failed to persist provider fallback warning for session %s",
                bot_session_id,
            )


__all__ = [
    "ProviderFallbackWarning",
    "ResolvedProviderPayload",
    "persist_provider_fallback_warnings",
    "resolve_agent_provider_payload",
]
