"""Personality-driven provider + mode resolution at session start (Johnny-oly.3).

A *personality* (``personalities`` table, Johnny-oly.2) is a named preset that
can override the globally-active LLM and TTS providers for a single session and
seed a default decision mode — **without ever mutating** the
``provider_credentials.is_active`` rows. This module is the resolver the PRD
(``tasks/prd-personality-library.md`` §4) specifies. It sits **between**
``build_provider_payload`` (global active per kind) and
``_resolve_provider_overrides`` (explicit per-start override) at every
session-start callsite:

    global active  →  apply_personality (HERE)  →  explicit request override

Two entry points:

* :func:`select_personality` — picks WHICH personality applies, per the §4a
  precedence: explicit request id → the meeting's attached personality → the
  single ``is_default`` row → ``None`` (no personality → behave exactly like
  today).
* :func:`apply_personality` — layers the chosen personality's LLM / TTS FK
  overrides onto a base provider payload with **loud fallback**.

Fallback rule (PRD §4b, bead §A.2/§D). A personality FK is honored only when
the referenced row **exists**, is **``is_active``**, and **decrypts**.
Otherwise the kind keeps its global-active entry and a
:class:`PersonalityFallback` is recorded *and* logged as a single
``personality.fallback:`` line so ``docker logs api`` can be filtered to
surface "operator deactivated/rotated a provider still wired into a
personality". A ``NULL`` FK is the *designed* inherit path (the bootstrap
"Johnny" carries NULL FKs) and is intentionally **silent** — logging it would
fire on every default-personality session and drown the real alerts, defeating
the filterability the bead's acceptance asks for.

Scope notes:

* **STT is never overridden** (PRD non-goal) — only ``llm`` and ``tts``.
* ``default_mode`` is surfaced on the result so the caller can seed the
  *playground* mode; per §4c it never overrides a calendar meeting's non-null
  ``mode`` at session start.
* ``metadata.tts_options`` voice tuning is **stored but not consumed in v1**
  (PRD §8.6); the resolver leaves it untouched.

Given the one-active-per-kind invariant (``uq_provider_credentials_active_per_kind``),
a v1 personality whose FKs point at the active providers resolves byte-for-byte
to the global-active payload, and a pin to a now-inactive provider falls back to
it — so v1's observable effect is the seeded mode + the fallback diagnostics +
the UI decoration. The provider-switching power lights up in v2 (multi-active
providers / consumed voice metadata).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import MeetingConfig, Personality, ProviderCredential
from app.security.crypto import CredentialCrypto, CryptoError, decrypt_json

logger = logging.getLogger(__name__)

# (payload key, Personality FK attribute) for the kinds a personality may
# override. STT stays global per the PRD non-goal.
_PERSONALITY_KINDS: tuple[tuple[str, str], ...] = (
    ("llm", "llm_provider_id"),
    ("tts", "tts_provider_id"),
)


@dataclass(frozen=True)
class PersonalityFallback:
    """One kind that could not honor the personality's pinned provider.

    ``reason`` is one of:

    * ``"missing"`` — the FK points at a ``provider_credentials`` row that no
      longer exists (data corruption; ``ON DELETE SET NULL`` normally turns a
      deleted provider into a silent ``NULL`` FK instead).
    * ``"deactivated"`` — the row exists but ``is_active`` is false (the
      operator activated a different provider for that kind).
    * ``"undecryptable"`` — the row exists and is active but its credentials
      failed to decrypt (e.g. a rotated ``FERNET_KEY``).

    In every case the session still starts on the global-active provider for
    ``kind``.
    """

    kind: str
    personality_id: int
    reason: str


@dataclass(frozen=True)
class PersonalityResolution:
    """Outcome of :func:`apply_personality`.

    ``payload`` is the (possibly) overridden provider payload — a fresh dict;
    the caller's ``base_payload`` is never mutated. The remaining fields
    decorate it so the session UI (Johnny-oly.5) can render which personality
    served the session, seed the playground mode, and warn on any fallback.
    """

    payload: dict[str, Any]
    personality_id: int | None = None
    personality_name: str | None = None
    default_mode: str | None = None
    fallbacks: tuple[PersonalityFallback, ...] = field(default_factory=tuple)

    @property
    def fell_back(self) -> bool:
        return bool(self.fallbacks)


def select_personality(
    session: Session,
    *,
    requested_id: int | None,
    meeting: MeetingConfig | None,
) -> Personality | None:
    """Pick the personality for this session (PRD §4a precedence).

    1. ``requested_id`` — explicit this-start choice (``payload.personality_id``).
    2. ``meeting.personality_id`` — the calendar meeting's attached personality.
    3. the single ``is_default`` personality (bootstrap "Johnny").
    4. ``None`` — no personality row exists at all; the caller then behaves
       exactly like today (pure global active).

    An explicit ``requested_id`` that no longer exists falls *through* to the
    meeting/default chain rather than failing the session — reliability beats
    strictness here (PRD §4a step 4): a stale id from the UI must never abort a
    start.
    """
    if requested_id is not None:
        row = session.get(Personality, requested_id)
        if row is not None:
            return row
        logger.warning(
            "personality.select: requested personality_id=%s not found; "
            "falling back to meeting/default selection",
            requested_id,
        )

    if meeting is not None and meeting.personality_id is not None:
        row = session.get(Personality, meeting.personality_id)
        if row is not None:
            return row

    return session.scalar(select(Personality).where(Personality.is_default.is_(True)))


def apply_personality(
    session: Session,
    base_payload: dict[str, Any],
    personality: Personality | None,
    *,
    crypto: CredentialCrypto,
) -> PersonalityResolution:
    """Layer ``personality``'s LLM/TTS overrides onto ``base_payload``.

    ``base_payload`` is the global-active payload from
    :func:`app.services.provider_payload.build_provider_payload`: a mapping of
    ``{kind: {provider_name, display_name, credentials, options}}``. The
    returned :attr:`PersonalityResolution.payload` is a shallow copy, so the
    caller can still log / diff the pre-personality payload.

    When ``personality`` is ``None`` this is a pure copy with no decoration —
    identical to today's behaviour (regression guard).
    """
    merged: dict[str, Any] = {kind: dict(entry) for kind, entry in base_payload.items()}
    if personality is None:
        return PersonalityResolution(payload=merged)

    fallbacks: list[PersonalityFallback] = []
    for kind, attr in _PERSONALITY_KINDS:
        provider_id = getattr(personality, attr)
        if provider_id is None:
            # Designed inherit path (NULL FK). Silent: logging here would fire
            # on every bootstrap-Johnny session and bury the real alerts.
            continue
        entry, reason = _resolve_override_entry(session, provider_id, crypto)
        if entry is not None:
            merged[kind] = entry
            continue
        fallbacks.append(
            PersonalityFallback(kind=kind, personality_id=personality.id, reason=reason)
        )
        logger.warning(
            "personality.fallback: kind=%s personality_id=%s provider_id=%s reason=%s",
            kind,
            personality.id,
            provider_id,
            reason,
        )

    return PersonalityResolution(
        payload=merged,
        personality_id=personality.id,
        personality_name=personality.display_name,
        default_mode=_mode_value(personality.default_mode),
        fallbacks=tuple(fallbacks),
    )


def _resolve_override_entry(
    session: Session,
    provider_id: int,
    crypto: CredentialCrypto,
) -> tuple[dict[str, Any] | None, str]:
    """Return ``(payload_entry, "")`` for a usable provider, else ``(None, reason)``.

    ``reason`` is the :class:`PersonalityFallback` reason string. Provider-kind
    alignment is guaranteed by the personalities CRUD (it rejects an
    ``llm_provider_id`` that points at a non-``llm`` row with a 422), so the
    entry is always the right kind for the slot.
    """
    row = session.get(ProviderCredential, provider_id)
    if row is None:
        return None, "missing"
    if not row.is_active:
        return None, "deactivated"
    try:
        credentials = decrypt_json(crypto, row.credentials_encrypted)
    except (CryptoError, ValueError):
        return None, "undecryptable"
    return (
        {
            "provider_name": row.provider_name,
            "display_name": row.display_name,
            "credentials": credentials,
            "options": dict(row.config or {}),
        },
        "",
    )


def _mode_value(mode: Any) -> str | None:
    """Normalise a ``BotMode | str | None`` to its string value (or ``None``)."""
    if mode is None:
        return None
    return mode.value if hasattr(mode, "value") else str(mode)


__all__ = [
    "PersonalityFallback",
    "PersonalityResolution",
    "apply_personality",
    "select_personality",
]
