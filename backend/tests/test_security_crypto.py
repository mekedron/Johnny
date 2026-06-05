"""Tests for :mod:`app.security.crypto`."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.security.crypto import (
    CredentialCrypto,
    CryptoError,
    decrypt_json,
    encrypt_json,
)


def _fresh_key() -> bytes:
    return Fernet.generate_key()


def test_round_trip_string() -> None:
    c = CredentialCrypto(_fresh_key())
    assert c.decrypt(c.encrypt("hello world")) == "hello world"


def test_round_trip_unicode() -> None:
    c = CredentialCrypto(_fresh_key())
    plaintext = "héllo — wörld 🌍"
    assert c.decrypt(c.encrypt(plaintext)) == plaintext


def test_ciphertext_is_not_plaintext() -> None:
    c = CredentialCrypto(_fresh_key())
    secret = "sk-abcdef123456"
    cipher = c.encrypt(secret)
    assert secret not in cipher


def test_each_encrypt_is_unique() -> None:
    c = CredentialCrypto(_fresh_key())
    # Fernet adds a random IV, so two encryptions of the same plaintext differ.
    assert c.encrypt("x") != c.encrypt("x")


def test_wrong_key_fails_to_decrypt() -> None:
    a = CredentialCrypto(_fresh_key())
    b = CredentialCrypto(_fresh_key())
    cipher = a.encrypt("secret")
    with pytest.raises(CryptoError):
        b.decrypt(cipher)


def test_garbage_ciphertext_raises_cryptoerror() -> None:
    c = CredentialCrypto(_fresh_key())
    with pytest.raises(CryptoError):
        c.decrypt("not-a-real-fernet-token")


def test_empty_key_rejected() -> None:
    with pytest.raises(CryptoError):
        CredentialCrypto("")


def test_invalid_key_format_raises_cryptoerror() -> None:
    with pytest.raises(CryptoError):
        CredentialCrypto("not-a-valid-base64-fernet-key-of-correct-length")


def test_accepts_bytes_key_directly() -> None:
    key = _fresh_key()
    c1 = CredentialCrypto(key)
    c2 = CredentialCrypto(key.decode("ascii"))
    # Both should be able to decrypt each other's ciphertext.
    assert c2.decrypt(c1.encrypt("x")) == "x"


def test_encrypt_json_round_trip() -> None:
    c = CredentialCrypto(_fresh_key())
    payload = {"api_key": "sk-test", "endpoint": "https://example.invalid"}
    cipher = encrypt_json(c, payload)
    assert decrypt_json(c, cipher) == payload


def test_encrypt_json_string_coerces_non_string_values() -> None:
    c = CredentialCrypto(_fresh_key())
    cipher = encrypt_json(c, {"version": "v1"})
    # Round-trip preserves string values.
    decrypted = decrypt_json(c, cipher)
    assert decrypted == {"version": "v1"}


def test_decrypt_json_rejects_non_object_payload() -> None:
    c = CredentialCrypto(_fresh_key())
    # Fernet-encrypt a JSON list instead of an object.
    cipher = c.encrypt("[1, 2, 3]")
    with pytest.raises(ValueError):
        decrypt_json(c, cipher)


def test_decrypt_json_propagates_crypto_error() -> None:
    c = CredentialCrypto(_fresh_key())
    with pytest.raises(CryptoError):
        decrypt_json(c, "garbage")
