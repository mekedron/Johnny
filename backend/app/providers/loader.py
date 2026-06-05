"""DB-coupled provider loading.

Reads active rows from ``provider_credentials`` and turns them into live
provider instances by looking up the factory in :class:`ProviderRegistry`.
Kept separate from ``base.py`` so the meet-worker can import the provider
ABCs without pulling in SQLAlchemy.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ProviderCredential
from app.providers.base import (
    ProviderConfig,
    ProviderInstance,
    ProviderKind,
    ProviderRegistry,
    get_registry,
)

CredentialDecryptor = Callable[[str], dict[str, str]]


def _identity_decryptor(blob: str) -> dict[str, str]:
    """Default decryptor: assume ``credentials_encrypted`` is already a JSON map.

    Real deployments inject a Fernet-backed decryptor via :func:`load_active_providers`'s
    ``decrypt`` parameter. This default exists for tests that don't exercise the
    encryption layer (added in US-005 / US-018).
    """
    parsed = json.loads(blob)
    if not isinstance(parsed, dict):
        raise ValueError("credentials_encrypted must decode to a JSON object")
    return {str(k): str(v) for k, v in parsed.items()}


def load_active_providers(
    session: Session,
    *,
    registry: ProviderRegistry | None = None,
    decrypt: CredentialDecryptor | None = None,
    kinds: Iterable[ProviderKind] | None = None,
) -> dict[ProviderKind, ProviderInstance]:
    """Materialize the active provider per kind from ``provider_credentials``.

    Returns a mapping of :class:`ProviderKind` to the instantiated provider.
    Kinds with no active row are absent from the result. Raises
    :class:`UnknownProviderError` if an active row references a
    ``provider_name`` that is not registered, so misconfiguration fails fast
    at startup rather than mid-meeting.
    """
    reg = registry if registry is not None else get_registry()
    dec = decrypt if decrypt is not None else _identity_decryptor

    stmt = select(ProviderCredential).where(ProviderCredential.is_active.is_(True))
    if kinds is not None:
        stmt = stmt.where(ProviderCredential.kind.in_(list(kinds)))

    active: dict[ProviderKind, ProviderInstance] = {}
    for row in session.scalars(stmt).all():
        config = ProviderConfig(
            kind=row.kind,
            provider_name=row.provider_name,
            display_name=row.display_name,
            credentials=dec(row.credentials_encrypted),
            options=dict(row.config or {}),
        )
        instance = reg.instantiate(config)
        active[row.kind] = instance
    return active


__all__ = [
    "CredentialDecryptor",
    "load_active_providers",
]
