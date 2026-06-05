"""Fernet symmetric encryption for secrets at rest.

The same Fernet key encrypts Google OAuth tokens (US-005) and provider
credentials (US-018). The key is read from ``Settings.fernet_key`` (env var
``FERNET_KEY``) and must be persisted across container restarts — losing it
makes all previously-stored secrets unrecoverable.

Generate a key with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class CryptoError(Exception):
    """Raised when encryption or decryption fails."""


class CredentialCrypto:
    """Thin wrapper around :class:`cryptography.fernet.Fernet`.

    Constructed directly with a key — easy for tests and dependency injection.
    Production code uses :func:`get_crypto` which builds an instance from the
    application settings.
    """

    def __init__(self, key: bytes | str) -> None:
        if not key:
            raise CryptoError("fernet key must not be empty")
        if isinstance(key, str):
            key = key.encode("ascii")
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError) as exc:
            raise CryptoError(f"invalid fernet key: {exc}") from exc

    def encrypt(self, plaintext: str) -> str:
        """Return a URL-safe-base64 Fernet token for ``plaintext``."""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a Fernet token. Raises :class:`CryptoError` on bad input."""
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise CryptoError("ciphertext is invalid or wrong key") from exc


def get_crypto() -> CredentialCrypto:
    """Return a :class:`CredentialCrypto` built from application settings.

    Raises :class:`CryptoError` if ``FERNET_KEY`` is not configured.
    """
    settings = get_settings()
    if not settings.fernet_key:
        raise CryptoError(
            "FERNET_KEY is not set; generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"`"
        )
    return CredentialCrypto(settings.fernet_key)


def encrypt_json(crypto: CredentialCrypto, payload: dict[str, Any]) -> str:
    """JSON-encode ``payload`` then encrypt the resulting string."""
    return crypto.encrypt(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def decrypt_json(crypto: CredentialCrypto, ciphertext: str) -> dict[str, str]:
    """Decrypt then JSON-decode to a ``dict[str, str]``.

    Raises :class:`CryptoError` if decryption fails. Raises :class:`ValueError`
    if the decrypted payload is not a JSON object.
    """
    plain = crypto.decrypt(ciphertext)
    parsed = json.loads(plain)
    if not isinstance(parsed, dict):
        raise ValueError("decrypted payload must be a JSON object")
    return {str(k): str(v) for k, v in parsed.items()}


__all__ = [
    "CredentialCrypto",
    "CryptoError",
    "decrypt_json",
    "encrypt_json",
    "get_crypto",
]
