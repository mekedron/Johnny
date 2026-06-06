"""Serialise active provider credentials for the meet-worker.

The meet-worker container does not have DB access — it is intentionally
SQLAlchemy-free so the image stays small. To run the voice pipeline it
still needs every active provider's name, decrypted credentials, and
options. We build that payload on the API side, encode it as JSON, and
inject it via ``JOHNNY_PROVIDER_CONFIG`` env var on the spawned
container.

The shape mirrors what
:meth:`app.providers.base.ProviderRegistry.instantiate` expects, so the
meet-worker can rebuild every provider with one line per kind::

    config = ProviderConfig(**payload["stt"])
    stt = registry.instantiate(config)

If a row is missing for some kind (e.g. no active TTS configured), that
key is absent from the payload and the meet-worker treats the pipeline
as listen-only for that channel.

Security note: this payload contains plaintext API keys at runtime.
Docker's ``docker inspect <container>`` exposes container env vars to
anyone on the host with docker access — acceptable for local dev where
the operator already has the Fernet key. A future change should switch
to short-lived credentials handed back over an HTTP call from the
meet-worker to the API.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ProviderCredential
from app.providers.base import ProviderKind
from app.security.crypto import CredentialCrypto, CryptoError, decrypt_json

logger = logging.getLogger(__name__)


def build_provider_payload(
    db: Session,
    crypto: CredentialCrypto,
) -> dict[str, Any]:
    """Return ``{kind: {provider_name, credentials, options, display_name}}``.

    Iterates every active row in ``provider_credentials``. Rows whose
    ciphertext can't be decrypted are skipped with a warning so a single
    rotated key doesn't disable every meeting.
    """
    payload: dict[str, Any] = {}
    rows = db.scalars(
        select(ProviderCredential).where(ProviderCredential.is_active.is_(True))
    ).all()
    for row in rows:
        try:
            credentials = decrypt_json(crypto, row.credentials_encrypted)
        except (CryptoError, ValueError) as exc:
            logger.warning(
                "skipping provider %s/%s — credential decrypt failed: %s",
                row.kind,
                row.provider_name,
                exc,
            )
            continue
        kind_key = (
            row.kind.value if isinstance(row.kind, ProviderKind) else str(row.kind)
        )
        payload[kind_key] = {
            "provider_name": row.provider_name,
            "display_name": row.display_name,
            "credentials": credentials,
            "options": dict(row.config or {}),
        }
    return payload


__all__ = ["build_provider_payload"]
