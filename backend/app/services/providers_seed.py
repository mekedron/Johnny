"""Seed provider credentials from a JSON config file on stack startup (Johnny-d3e).

The user fills out provider settings in the UI, exports them as JSON
(via :doc:`Johnny-k3z </../tasks/providers-export>`), commits the file
to a config directory mounted into the api container, and on the next
startup the rows are auto-loaded into ``provider_credentials``.

Configuration (env vars, all optional):

* ``JOHNNY_PROVIDERS_FILE`` — path to the JSON file. Default
  ``/config/providers.json``.
* ``JOHNNY_PROVIDERS_SEED_MODE`` — one of ``insert-only`` (default),
  ``overwrite``, or ``disabled``.

  ``insert-only`` is the safest default: rows matching
  ``(kind, provider_name, display_name)`` are skipped if they already
  exist in the DB, so a user's UI edits never get silently clobbered
  by a stale config file. ``overwrite`` forces every row in the file
  to win over the DB (credentials re-encrypted, options replaced,
  ``is_active`` flag synced). ``disabled`` skips the seeder entirely.

Shape of ``providers.json`` (version 1) — this is the canonical
interchange format for both the seeder and the export endpoint
(Johnny-k3z), so any change here must update both sides::

    {
      "version": 1,
      "providers": [
        {
          "kind": "stt" | "llm" | "tts",
          "provider_name": "deepgram",
          "display_name": "Deepgram primary",
          "credentials": {"api_key": "..."},
          "options": {"model": "nova-2"},
          "is_active": true
        }
      ]
    }

Idempotency: running the seeder N times against the same file produces
the same DB state (insert-only mode skips already-present rows;
overwrite mode is identity for matching rows). The seeder never deletes
DB rows the file omits — operators remove providers via the UI / DELETE
endpoint, not by mutating the seed file.

The active-per-kind invariant is enforced by a partial unique index on
``(kind) WHERE is_active`` — the file should declare at most one
``is_active: true`` per kind. When more than one is found the seeder
keeps the last in file order active and warns about the others; this
mirrors how the UI's ``POST /providers/{id}/activate`` deactivates
siblings.
"""

from __future__ import annotations

import enum
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from app.db.models import ProviderCredential
from app.providers.base import ProviderKind
from app.security.crypto import CredentialCrypto, encrypt_json

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

PROVIDERS_FILE_ENV = "JOHNNY_PROVIDERS_FILE"
PROVIDERS_SEED_MODE_ENV = "JOHNNY_PROVIDERS_SEED_MODE"

DEFAULT_PROVIDERS_FILE = Path("/config/providers.json")
SUPPORTED_FILE_VERSION = 1

# Cap the file size so a malformed mount can't OOM the API at startup.
# Real exports run a few KiB per provider; 8 MiB is comfortably above
# the realistic ceiling for hundreds of providers.
MAX_PROVIDERS_FILE_BYTES = 8 * 1024 * 1024


class SeedMode(enum.StrEnum):
    """How the seeder reconciles file rows with existing DB rows."""

    DISABLED = "disabled"
    INSERT_ONLY = "insert-only"
    OVERWRITE = "overwrite"


DEFAULT_SEED_MODE = SeedMode.INSERT_ONLY


class ProvidersFileError(ValueError):
    """Raised when the JSON file is malformed or violates the schema."""


@dataclass(frozen=True, slots=True)
class ProviderSeedEntry:
    """A single normalized row parsed out of ``providers.json``."""

    kind: ProviderKind
    provider_name: str
    display_name: str
    credentials: dict[str, str]
    options: dict[str, Any]
    is_active: bool

    @property
    def identity(self) -> tuple[str, str, str]:
        """The DB unique-key tuple (matches ``uq_provider_credentials``)."""
        return (self.kind.value, self.provider_name, self.display_name)


@dataclass(slots=True)
class SeedResult:
    """Summary returned by :func:`seed_providers_from_file`."""

    created: list[ProviderSeedEntry] = field(default_factory=list)
    updated: list[ProviderSeedEntry] = field(default_factory=list)
    skipped: list[ProviderSeedEntry] = field(default_factory=list)
    activated: list[ProviderSeedEntry] = field(default_factory=list)
    mode: SeedMode = DEFAULT_SEED_MODE
    source: Path | None = None

    @property
    def total(self) -> int:
        return len(self.created) + len(self.updated) + len(self.skipped)

    def to_log_summary(self) -> str:
        """Single-line summary suitable for INFO logging."""
        return (
            f"providers seed mode={self.mode.value} source={self.source} "
            f"created={len(self.created)} updated={len(self.updated)} "
            f"skipped={len(self.skipped)} activated={len(self.activated)}"
        )


def get_providers_file_path() -> Path:
    """Return the configured providers.json path (env override or default)."""
    override = os.environ.get(PROVIDERS_FILE_ENV, "").strip()
    if override:
        return Path(override)
    return DEFAULT_PROVIDERS_FILE


def get_seed_mode() -> SeedMode:
    """Parse :data:`PROVIDERS_SEED_MODE_ENV` into a :class:`SeedMode`.

    Unknown values fall back to the default with a warning so a
    typo doesn't silently disable the seeder.
    """
    raw = os.environ.get(PROVIDERS_SEED_MODE_ENV, "").strip().lower()
    if not raw:
        return DEFAULT_SEED_MODE
    try:
        return SeedMode(raw)
    except ValueError:
        logger.warning(
            "unknown %s=%r — falling back to %s",
            PROVIDERS_SEED_MODE_ENV,
            raw,
            DEFAULT_SEED_MODE.value,
        )
        return DEFAULT_SEED_MODE


def _ensure_str_mapping(value: Any, field_name: str) -> dict[str, str]:
    """Coerce a JSON object into ``dict[str, str]``, rejecting non-objects."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProvidersFileError(
            f"'{field_name}' must be a JSON object, got {type(value).__name__}"
        )
    out: dict[str, str] = {}
    for key, val in value.items():
        if not isinstance(key, str):
            raise ProvidersFileError(
                f"'{field_name}' keys must be strings, got {type(key).__name__}"
            )
        if val is None:
            raise ProvidersFileError(
                f"'{field_name}.{key}' must not be null"
            )
        # All credentials are strings on the wire (api keys, tokens, urls).
        out[key] = str(val)
    return out


def _ensure_json_object(value: Any, field_name: str) -> dict[str, Any]:
    """Coerce a JSON object into ``dict[str, Any]``."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProvidersFileError(
            f"'{field_name}' must be a JSON object, got {type(value).__name__}"
        )
    for key in value:
        if not isinstance(key, str):
            raise ProvidersFileError(
                f"'{field_name}' keys must be strings, got {type(key).__name__}"
            )
    return dict(value)


def _parse_entry(raw: Any, index: int) -> ProviderSeedEntry:
    if not isinstance(raw, dict):
        raise ProvidersFileError(
            f"providers[{index}] must be a JSON object"
        )

    kind_raw = raw.get("kind")
    if not isinstance(kind_raw, str):
        raise ProvidersFileError(
            f"providers[{index}].kind must be a string"
        )
    try:
        kind = ProviderKind(kind_raw)
    except ValueError as exc:
        raise ProvidersFileError(
            f"providers[{index}].kind={kind_raw!r} is not a recognised kind "
            f"(expected one of {[k.value for k in ProviderKind]})"
        ) from exc

    provider_name = raw.get("provider_name")
    if not isinstance(provider_name, str) or not provider_name.strip():
        raise ProvidersFileError(
            f"providers[{index}].provider_name must be a non-empty string"
        )

    display_name = raw.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        raise ProvidersFileError(
            f"providers[{index}].display_name must be a non-empty string"
        )

    credentials = _ensure_str_mapping(raw.get("credentials"), f"providers[{index}].credentials")
    options = _ensure_json_object(raw.get("options"), f"providers[{index}].options")

    is_active_raw = raw.get("is_active", False)
    if not isinstance(is_active_raw, bool):
        raise ProvidersFileError(
            f"providers[{index}].is_active must be a boolean"
        )

    return ProviderSeedEntry(
        kind=kind,
        provider_name=provider_name.strip(),
        display_name=display_name.strip(),
        credentials=credentials,
        options=options,
        is_active=is_active_raw,
    )


def parse_providers_file(path: Path) -> list[ProviderSeedEntry]:
    """Read and parse ``path`` into a list of :class:`ProviderSeedEntry`.

    Raises :class:`FileNotFoundError` if the file is missing — callers
    decide whether that's fatal (an operator typo) or expected (no
    config mounted). Raises :class:`ProvidersFileError` on any
    schema / shape violation; the file is rejected as a whole so a
    partial parse can never leave the DB in a half-seeded state.
    """
    raw_bytes = path.read_bytes()
    if len(raw_bytes) > MAX_PROVIDERS_FILE_BYTES:
        raise ProvidersFileError(
            f"providers file is too large: {len(raw_bytes)} bytes "
            f"(limit {MAX_PROVIDERS_FILE_BYTES})"
        )

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProvidersFileError("providers file must be UTF-8 JSON") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProvidersFileError(
            f"providers file is not valid JSON: {exc.msg} (line {exc.lineno})"
        ) from exc

    if not isinstance(data, dict):
        raise ProvidersFileError("providers JSON must be an object at the top level")

    version = data.get("version")
    if version != SUPPORTED_FILE_VERSION:
        raise ProvidersFileError(
            f"unsupported providers file version: {version!r} "
            f"(expected {SUPPORTED_FILE_VERSION})"
        )

    providers_raw = data.get("providers")
    if not isinstance(providers_raw, list):
        raise ProvidersFileError(
            "'providers' must be a JSON array"
        )

    entries: list[ProviderSeedEntry] = []
    seen: set[tuple[str, str, str]] = set()
    for idx, raw in enumerate(providers_raw):
        entry = _parse_entry(raw, idx)
        if entry.identity in seen:
            raise ProvidersFileError(
                f"providers[{idx}] is a duplicate of an earlier entry "
                f"(kind={entry.kind.value}, provider_name={entry.provider_name!r}, "
                f"display_name={entry.display_name!r})"
            )
        seen.add(entry.identity)
        entries.append(entry)
    return entries


def _index_existing(
    session: Session,
) -> dict[tuple[str, str, str], ProviderCredential]:
    # Live kinds only: a historical ``kind='s2s'`` row (tombstoned by
    # Johnny-trt.43, deactivated in migration 0026) would crash the enum
    # coercion at load — and this index runs during startup seeding.
    rows = session.scalars(
        select(ProviderCredential).where(
            ProviderCredential.kind.in_(list(ProviderKind))
        )
    ).all()
    return {
        (
            r.kind.value if isinstance(r.kind, ProviderKind) else str(r.kind),
            r.provider_name,
            r.display_name,
        ): r
        for r in rows
    }


def _activate_kind(
    session: Session, kind: ProviderKind, row: ProviderCredential
) -> None:
    """Mark ``row`` as the active provider for its kind; deactivate siblings.

    Mirrors the same write the ``POST /providers/{id}/activate`` endpoint
    performs, so the partial unique index on ``(kind) WHERE is_active``
    stays satisfied even when the file declares ``is_active: true`` on a
    row that already has a sibling activated.
    """
    session.execute(
        update(ProviderCredential)
        .where(ProviderCredential.kind == kind)
        .where(ProviderCredential.id != row.id)
        .values(is_active=False)
    )
    row.is_active = True


def seed_providers_from_file(
    session: Session,
    crypto: CredentialCrypto,
    *,
    path: Path | None = None,
    mode: SeedMode | None = None,
) -> SeedResult:
    """Reconcile ``provider_credentials`` with the JSON config file.

    Skips silently (returns an empty result) when:

    * the seeder is :data:`SeedMode.DISABLED`, or
    * the file doesn't exist at the configured path (operator hasn't
      mounted one — common during local dev), or
    * the file exists but contains zero providers.

    Raises :class:`ProvidersFileError` for malformed input; the lifespan
    hook in :mod:`app.main` catches and logs so a bad config file
    doesn't take down the API.
    """
    resolved_path = path if path is not None else get_providers_file_path()
    resolved_mode = mode if mode is not None else get_seed_mode()
    result = SeedResult(mode=resolved_mode, source=resolved_path)

    if resolved_mode is SeedMode.DISABLED:
        logger.info("provider seeding disabled via %s", PROVIDERS_SEED_MODE_ENV)
        return result

    if not resolved_path.exists():
        logger.info(
            "no providers seed file at %s — skipping (set %s to override)",
            resolved_path,
            PROVIDERS_FILE_ENV,
        )
        return result

    entries = parse_providers_file(resolved_path)
    if not entries:
        logger.info("providers seed file %s is empty — nothing to seed", resolved_path)
        return result

    existing = _index_existing(session)

    # Process active rows last so a same-kind activation wave doesn't
    # bounce between rows mid-loop. We collect activation requests and
    # apply at the end, picking the LAST active=true row per kind to
    # match the unique-index invariant.
    pending_activations: dict[ProviderKind, ProviderCredential] = {}

    for entry in entries:
        existing_row = existing.get(entry.identity)
        if existing_row is None:
            row = ProviderCredential(
                kind=entry.kind,
                provider_name=entry.provider_name,
                display_name=entry.display_name,
                credentials_encrypted=encrypt_json(crypto, entry.credentials),
                config=dict(entry.options),
                is_active=False,
            )
            session.add(row)
            session.flush()
            result.created.append(entry)
            if entry.is_active:
                pending_activations[entry.kind] = row
            continue

        if resolved_mode is SeedMode.INSERT_ONLY:
            result.skipped.append(entry)
            continue

        # OVERWRITE: re-encrypt credentials, replace options, sync active.
        existing_row.credentials_encrypted = encrypt_json(crypto, entry.credentials)
        existing_row.config = dict(entry.options)
        session.flush()
        result.updated.append(entry)
        if entry.is_active:
            pending_activations[entry.kind] = existing_row
        else:
            # Explicit is_active=false in OVERWRITE mode should clear the flag
            # so an export -> edit -> import roundtrip is faithful.
            existing_row.is_active = False

    for kind, row in pending_activations.items():
        _activate_kind(session, kind, row)
        # The activated entry is the last file entry of this kind with
        # is_active=true; find it in the input so the result reflects
        # file ordering precisely.
        for entry in reversed(entries):
            if entry.kind is kind and entry.is_active and entry.identity == (
                kind.value,
                row.provider_name,
                row.display_name,
            ):
                result.activated.append(entry)
                break

    duplicates_warned: set[ProviderKind] = set()
    for entry in entries:
        if not entry.is_active:
            continue
        active_row = pending_activations.get(entry.kind)
        if active_row is None:
            continue
        if (
            active_row.provider_name == entry.provider_name
            and active_row.display_name == entry.display_name
        ):
            continue
        if entry.kind in duplicates_warned:
            continue
        duplicates_warned.add(entry.kind)
        logger.warning(
            "providers seed file has multiple is_active=true entries for kind=%s; "
            "kept %r active (last in file order)",
            entry.kind.value,
            (active_row.provider_name, active_row.display_name),
        )

    session.commit()
    logger.info(result.to_log_summary())
    return result


__all__ = [
    "DEFAULT_PROVIDERS_FILE",
    "DEFAULT_SEED_MODE",
    "MAX_PROVIDERS_FILE_BYTES",
    "PROVIDERS_FILE_ENV",
    "PROVIDERS_SEED_MODE_ENV",
    "ProviderSeedEntry",
    "ProvidersFileError",
    "SUPPORTED_FILE_VERSION",
    "SeedMode",
    "SeedResult",
    "get_providers_file_path",
    "get_seed_mode",
    "parse_providers_file",
    "seed_providers_from_file",
]
