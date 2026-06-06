"""Tests for the providers-from-JSON seeder (Johnny-d3e)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import ProviderCredential
from app.providers.base import ProviderKind
from app.security.crypto import CredentialCrypto, decrypt_json
from app.services import providers_seed
from app.services.providers_seed import (
    DEFAULT_PROVIDERS_FILE,
    DEFAULT_SEED_MODE,
    MAX_PROVIDERS_FILE_BYTES,
    PROVIDERS_FILE_ENV,
    PROVIDERS_SEED_MODE_ENV,
    SUPPORTED_FILE_VERSION,
    ProvidersFileError,
    SeedMode,
    SeedResult,
    get_providers_file_path,
    get_seed_mode,
    parse_providers_file,
    seed_providers_from_file,
)

# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    Base.metadata.create_all(bind=eng, tables=[ProviderCredential.__table__])  # type: ignore[list-item]
    return eng


@pytest.fixture
def db_session(engine: sa.Engine) -> Iterator[Session]:
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def crypto() -> CredentialCrypto:
    return CredentialCrypto(Fernet.generate_key())


def _provider_entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "stt",
        "provider_name": "deepgram",
        "display_name": "Deepgram primary",
        "credentials": {"api_key": "sk-test"},
        "options": {"model": "nova-2"},
        "is_active": False,
    }
    base.update(overrides)
    return base


def _write_seed_file(path: Path, *providers: dict[str, Any], version: int = 1) -> Path:
    body = {"version": version, "providers": list(providers)}
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


# --- env / config ---------------------------------------------------------


def test_get_providers_file_path_uses_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "custom.json"
    monkeypatch.setenv(PROVIDERS_FILE_ENV, str(target))
    assert get_providers_file_path() == target


def test_get_providers_file_path_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PROVIDERS_FILE_ENV, raising=False)
    assert get_providers_file_path() == DEFAULT_PROVIDERS_FILE


def test_get_seed_mode_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PROVIDERS_SEED_MODE_ENV, raising=False)
    assert get_seed_mode() is DEFAULT_SEED_MODE


def test_get_seed_mode_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROVIDERS_SEED_MODE_ENV, "disabled")
    assert get_seed_mode() is SeedMode.DISABLED


def test_get_seed_mode_overwrite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROVIDERS_SEED_MODE_ENV, "overwrite")
    assert get_seed_mode() is SeedMode.OVERWRITE


def test_get_seed_mode_unknown_falls_back_with_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(PROVIDERS_SEED_MODE_ENV, "wonky")
    with caplog.at_level("WARNING"):
        assert get_seed_mode() is DEFAULT_SEED_MODE
    assert any("wonky" in rec.message for rec in caplog.records)


# --- parse_providers_file -------------------------------------------------


def test_parse_minimal_file(tmp_path: Path) -> None:
    path = _write_seed_file(tmp_path / "providers.json", _provider_entry())
    entries = parse_providers_file(path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind is ProviderKind.STT
    assert entry.provider_name == "deepgram"
    assert entry.display_name == "Deepgram primary"
    assert entry.credentials == {"api_key": "sk-test"}
    assert entry.options == {"model": "nova-2"}
    assert entry.is_active is False


def test_parse_rejects_unsupported_version(tmp_path: Path) -> None:
    path = _write_seed_file(tmp_path / "p.json", _provider_entry(), version=99)
    with pytest.raises(ProvidersFileError, match="version"):
        parse_providers_file(path)


def test_parse_rejects_missing_version(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"providers": [_provider_entry()]}), encoding="utf-8")
    with pytest.raises(ProvidersFileError, match="version"):
        parse_providers_file(path)


def test_parse_rejects_non_object_root(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_text(json.dumps([_provider_entry()]), encoding="utf-8")
    with pytest.raises(ProvidersFileError, match="object at the top level"):
        parse_providers_file(path)


def test_parse_rejects_non_array_providers(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"version": 1, "providers": "nope"}), encoding="utf-8")
    with pytest.raises(ProvidersFileError, match="array"):
        parse_providers_file(path)


def test_parse_rejects_invalid_kind(tmp_path: Path) -> None:
    path = _write_seed_file(tmp_path / "p.json", _provider_entry(kind="speech"))
    with pytest.raises(ProvidersFileError, match="recognised kind"):
        parse_providers_file(path)


def test_parse_rejects_missing_provider_name(tmp_path: Path) -> None:
    path = _write_seed_file(tmp_path / "p.json", _provider_entry(provider_name=""))
    with pytest.raises(ProvidersFileError, match="provider_name"):
        parse_providers_file(path)


def test_parse_rejects_missing_display_name(tmp_path: Path) -> None:
    path = _write_seed_file(tmp_path / "p.json", _provider_entry(display_name=""))
    with pytest.raises(ProvidersFileError, match="display_name"):
        parse_providers_file(path)


def test_parse_rejects_non_dict_credentials(tmp_path: Path) -> None:
    path = _write_seed_file(tmp_path / "p.json", _provider_entry(credentials="nope"))
    with pytest.raises(ProvidersFileError, match="credentials"):
        parse_providers_file(path)


def test_parse_rejects_null_credential_value(tmp_path: Path) -> None:
    path = _write_seed_file(
        tmp_path / "p.json", _provider_entry(credentials={"api_key": None})
    )
    with pytest.raises(ProvidersFileError, match="null"):
        parse_providers_file(path)


def test_parse_rejects_non_dict_options(tmp_path: Path) -> None:
    path = _write_seed_file(tmp_path / "p.json", _provider_entry(options=[1, 2]))
    with pytest.raises(ProvidersFileError, match="options"):
        parse_providers_file(path)


def test_parse_rejects_non_bool_is_active(tmp_path: Path) -> None:
    path = _write_seed_file(tmp_path / "p.json", _provider_entry(is_active="true"))
    with pytest.raises(ProvidersFileError, match="is_active"):
        parse_providers_file(path)


def test_parse_rejects_duplicate_identity(tmp_path: Path) -> None:
    path = _write_seed_file(
        tmp_path / "p.json", _provider_entry(), _provider_entry()
    )
    with pytest.raises(ProvidersFileError, match="duplicate"):
        parse_providers_file(path)


def test_parse_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_text("not json at all", encoding="utf-8")
    with pytest.raises(ProvidersFileError, match="valid JSON"):
        parse_providers_file(path)


def test_parse_rejects_oversized_file(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_bytes(b"x" * (MAX_PROVIDERS_FILE_BYTES + 1))
    with pytest.raises(ProvidersFileError, match="too large"):
        parse_providers_file(path)


def test_parse_accepts_multiple_kinds(tmp_path: Path) -> None:
    path = _write_seed_file(
        tmp_path / "p.json",
        _provider_entry(kind="stt", provider_name="deepgram"),
        _provider_entry(kind="llm", provider_name="openai", display_name="LLM"),
        _provider_entry(kind="tts", provider_name="elevenlabs", display_name="TTS"),
    )
    entries = parse_providers_file(path)
    assert [e.kind for e in entries] == [
        ProviderKind.STT,
        ProviderKind.LLM,
        ProviderKind.TTS,
    ]


def test_parse_coerces_credential_values_to_strings(tmp_path: Path) -> None:
    path = _write_seed_file(
        tmp_path / "p.json",
        _provider_entry(credentials={"api_key": "abc", "port": 8080}),
    )
    entries = parse_providers_file(path)
    # Even if the file accidentally puts an int in credentials, the
    # encrypted blob is always dict[str, str] on disk.
    assert entries[0].credentials == {"api_key": "abc", "port": "8080"}


# --- seed_providers_from_file: empty / disabled / missing -----------------


def test_seed_disabled_mode_skips(
    db_session: Session, crypto: CredentialCrypto, tmp_path: Path
) -> None:
    path = _write_seed_file(tmp_path / "p.json", _provider_entry())
    result = seed_providers_from_file(
        db_session, crypto, path=path, mode=SeedMode.DISABLED
    )
    assert result.created == [] and result.skipped == [] and result.updated == []
    assert db_session.scalars(sa.select(ProviderCredential)).all() == []


def test_seed_missing_file_returns_empty_result(
    db_session: Session, crypto: CredentialCrypto, tmp_path: Path
) -> None:
    missing = tmp_path / "absent.json"
    result = seed_providers_from_file(db_session, crypto, path=missing)
    assert result.total == 0
    assert result.source == missing


def test_seed_empty_providers_array_returns_empty(
    db_session: Session, crypto: CredentialCrypto, tmp_path: Path
) -> None:
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"version": 1, "providers": []}), encoding="utf-8")
    result = seed_providers_from_file(db_session, crypto, path=path)
    assert result.total == 0


# --- seed_providers_from_file: create path --------------------------------


def test_seed_creates_row_for_missing_provider(
    db_session: Session, crypto: CredentialCrypto, tmp_path: Path
) -> None:
    path = _write_seed_file(tmp_path / "p.json", _provider_entry())
    result = seed_providers_from_file(db_session, crypto, path=path)
    assert len(result.created) == 1
    rows = db_session.scalars(sa.select(ProviderCredential)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.kind is ProviderKind.STT
    assert row.provider_name == "deepgram"
    assert row.display_name == "Deepgram primary"
    assert row.config == {"model": "nova-2"}
    assert row.is_active is False  # entry.is_active was False


def test_seed_encrypts_credentials_at_rest(
    db_session: Session, crypto: CredentialCrypto, tmp_path: Path
) -> None:
    path = _write_seed_file(
        tmp_path / "p.json", _provider_entry(credentials={"api_key": "sk-secret"})
    )
    seed_providers_from_file(db_session, crypto, path=path)
    row = db_session.scalars(sa.select(ProviderCredential)).first()
    assert row is not None
    # Cipher text never carries the plaintext secret.
    assert "sk-secret" not in row.credentials_encrypted
    assert decrypt_json(crypto, row.credentials_encrypted) == {"api_key": "sk-secret"}


def test_seed_activates_single_active_row(
    db_session: Session, crypto: CredentialCrypto, tmp_path: Path
) -> None:
    path = _write_seed_file(tmp_path / "p.json", _provider_entry(is_active=True))
    result = seed_providers_from_file(db_session, crypto, path=path)
    row = db_session.scalars(sa.select(ProviderCredential)).first()
    assert row is not None and row.is_active is True
    assert len(result.activated) == 1


def test_seed_creates_multiple_kinds(
    db_session: Session, crypto: CredentialCrypto, tmp_path: Path
) -> None:
    path = _write_seed_file(
        tmp_path / "p.json",
        _provider_entry(kind="stt", provider_name="deepgram"),
        _provider_entry(
            kind="llm",
            provider_name="openai",
            display_name="OpenAI primary",
            options={"model": "gpt-4o-mini"},
        ),
        _provider_entry(
            kind="tts",
            provider_name="elevenlabs",
            display_name="ElevenLabs primary",
            options={"voice_id": "voice123"},
        ),
    )
    seed_providers_from_file(db_session, crypto, path=path)
    rows = db_session.scalars(sa.select(ProviderCredential)).all()
    assert {r.kind for r in rows} == {
        ProviderKind.STT,
        ProviderKind.LLM,
        ProviderKind.TTS,
    }


# --- seed_providers_from_file: insert-only is the default ----------------


def test_seed_insert_only_skips_existing_row(
    db_session: Session, crypto: CredentialCrypto, tmp_path: Path
) -> None:
    # Pre-seed a row directly.
    existing = ProviderCredential(
        kind=ProviderKind.STT,
        provider_name="deepgram",
        display_name="Deepgram primary",
        credentials_encrypted=crypto.encrypt('{"api_key":"old"}'),
        config={"model": "old-model"},
        is_active=True,
    )
    db_session.add(existing)
    db_session.commit()

    path = _write_seed_file(
        tmp_path / "p.json",
        _provider_entry(
            credentials={"api_key": "new"},
            options={"model": "new-model"},
            is_active=False,
        ),
    )
    result = seed_providers_from_file(
        db_session, crypto, path=path, mode=SeedMode.INSERT_ONLY
    )
    assert len(result.skipped) == 1
    assert len(result.created) == 0
    db_session.refresh(existing)
    assert existing.config == {"model": "old-model"}
    assert existing.is_active is True  # not clobbered


def test_seed_insert_only_idempotent_on_repeat(
    db_session: Session, crypto: CredentialCrypto, tmp_path: Path
) -> None:
    path = _write_seed_file(tmp_path / "p.json", _provider_entry(is_active=True))

    first = seed_providers_from_file(db_session, crypto, path=path)
    assert len(first.created) == 1

    second = seed_providers_from_file(db_session, crypto, path=path)
    assert len(second.created) == 0
    assert len(second.skipped) == 1

    rows = db_session.scalars(sa.select(ProviderCredential)).all()
    assert len(rows) == 1  # no duplicate


def test_seed_insert_only_creates_new_alongside_existing(
    db_session: Session, crypto: CredentialCrypto, tmp_path: Path
) -> None:
    db_session.add(
        ProviderCredential(
            kind=ProviderKind.STT,
            provider_name="deepgram",
            display_name="Deepgram primary",
            credentials_encrypted=crypto.encrypt('{"api_key":"old"}'),
            config={},
            is_active=False,
        )
    )
    db_session.commit()

    path = _write_seed_file(
        tmp_path / "p.json",
        _provider_entry(),  # existing — skipped
        _provider_entry(
            kind="llm",
            provider_name="openai",
            display_name="OpenAI primary",
        ),  # new
    )
    result = seed_providers_from_file(db_session, crypto, path=path)
    assert len(result.created) == 1 and len(result.skipped) == 1
    rows = db_session.scalars(sa.select(ProviderCredential)).all()
    assert len(rows) == 2


# --- seed_providers_from_file: overwrite mode ----------------------------


def test_seed_overwrite_replaces_credentials_and_options(
    db_session: Session, crypto: CredentialCrypto, tmp_path: Path
) -> None:
    db_session.add(
        ProviderCredential(
            kind=ProviderKind.STT,
            provider_name="deepgram",
            display_name="Deepgram primary",
            credentials_encrypted=crypto.encrypt('{"api_key":"old"}'),
            config={"model": "old-model", "stale": "x"},
            is_active=False,
        )
    )
    db_session.commit()

    path = _write_seed_file(
        tmp_path / "p.json",
        _provider_entry(
            credentials={"api_key": "new"},
            options={"model": "new-model"},
            is_active=True,
        ),
    )
    result = seed_providers_from_file(
        db_session, crypto, path=path, mode=SeedMode.OVERWRITE
    )
    assert len(result.updated) == 1
    row = db_session.scalars(sa.select(ProviderCredential)).first()
    assert row is not None
    assert decrypt_json(crypto, row.credentials_encrypted) == {"api_key": "new"}
    assert row.config == {"model": "new-model"}
    assert row.is_active is True


def test_seed_overwrite_clears_active_when_file_says_false(
    db_session: Session, crypto: CredentialCrypto, tmp_path: Path
) -> None:
    db_session.add(
        ProviderCredential(
            kind=ProviderKind.STT,
            provider_name="deepgram",
            display_name="Deepgram primary",
            credentials_encrypted=crypto.encrypt('{"api_key":"old"}'),
            config={},
            is_active=True,
        )
    )
    db_session.commit()

    path = _write_seed_file(tmp_path / "p.json", _provider_entry(is_active=False))
    seed_providers_from_file(
        db_session, crypto, path=path, mode=SeedMode.OVERWRITE
    )
    row = db_session.scalars(sa.select(ProviderCredential)).first()
    assert row is not None and row.is_active is False


def test_seed_overwrite_creates_missing_and_updates_existing(
    db_session: Session, crypto: CredentialCrypto, tmp_path: Path
) -> None:
    db_session.add(
        ProviderCredential(
            kind=ProviderKind.STT,
            provider_name="deepgram",
            display_name="Deepgram primary",
            credentials_encrypted=crypto.encrypt('{"api_key":"old"}'),
            config={},
            is_active=False,
        )
    )
    db_session.commit()

    path = _write_seed_file(
        tmp_path / "p.json",
        _provider_entry(),  # exists — update
        _provider_entry(
            kind="llm",
            provider_name="openai",
            display_name="OpenAI primary",
        ),
    )
    result = seed_providers_from_file(
        db_session, crypto, path=path, mode=SeedMode.OVERWRITE
    )
    assert len(result.updated) == 1
    assert len(result.created) == 1


# --- seed_providers_from_file: active-per-kind invariant -----------------


def test_seed_with_two_active_same_kind_keeps_last(
    db_session: Session, crypto: CredentialCrypto, tmp_path: Path
) -> None:
    path = _write_seed_file(
        tmp_path / "p.json",
        _provider_entry(display_name="A", is_active=True),
        _provider_entry(display_name="B", is_active=True),
    )
    result = seed_providers_from_file(db_session, crypto, path=path)
    assert len(result.created) == 2
    active = db_session.scalars(
        sa.select(ProviderCredential).where(ProviderCredential.is_active.is_(True))
    ).all()
    assert len(active) == 1
    assert active[0].display_name == "B"  # last in file wins


def test_seed_activation_deactivates_existing_sibling(
    db_session: Session, crypto: CredentialCrypto, tmp_path: Path
) -> None:
    # Pre-seed a different stt row that is currently active.
    db_session.add(
        ProviderCredential(
            kind=ProviderKind.STT,
            provider_name="whisper",
            display_name="Whisper local",
            credentials_encrypted=crypto.encrypt("{}"),
            config={},
            is_active=True,
        )
    )
    db_session.commit()

    path = _write_seed_file(tmp_path / "p.json", _provider_entry(is_active=True))
    seed_providers_from_file(db_session, crypto, path=path)

    active_rows = db_session.scalars(
        sa.select(ProviderCredential).where(ProviderCredential.is_active.is_(True))
    ).all()
    assert len(active_rows) == 1
    assert active_rows[0].provider_name == "deepgram"


def test_seed_logs_warning_on_duplicate_active_per_kind(
    db_session: Session,
    crypto: CredentialCrypto,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = _write_seed_file(
        tmp_path / "p.json",
        _provider_entry(display_name="A", is_active=True),
        _provider_entry(display_name="B", is_active=True),
    )
    with caplog.at_level("WARNING"):
        seed_providers_from_file(db_session, crypto, path=path)
    assert any(
        "multiple is_active=true" in rec.message for rec in caplog.records
    )


# --- environment integration ---------------------------------------------


def test_seed_via_env_uses_default_mode(
    db_session: Session,
    crypto: CredentialCrypto,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_seed_file(tmp_path / "p.json", _provider_entry())
    monkeypatch.setenv(PROVIDERS_FILE_ENV, str(path))
    monkeypatch.delenv(PROVIDERS_SEED_MODE_ENV, raising=False)
    result = seed_providers_from_file(db_session, crypto)
    assert result.mode is DEFAULT_SEED_MODE
    assert result.source == path


def test_seed_via_env_disabled(
    db_session: Session,
    crypto: CredentialCrypto,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_seed_file(tmp_path / "p.json", _provider_entry())
    monkeypatch.setenv(PROVIDERS_FILE_ENV, str(path))
    monkeypatch.setenv(PROVIDERS_SEED_MODE_ENV, "disabled")
    result = seed_providers_from_file(db_session, crypto)
    assert result.mode is SeedMode.DISABLED
    assert db_session.scalars(sa.select(ProviderCredential)).all() == []


def test_seed_via_env_overwrite_mode(
    db_session: Session,
    crypto: CredentialCrypto,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_session.add(
        ProviderCredential(
            kind=ProviderKind.STT,
            provider_name="deepgram",
            display_name="Deepgram primary",
            credentials_encrypted=crypto.encrypt('{"api_key":"old"}'),
            config={"model": "old"},
            is_active=False,
        )
    )
    db_session.commit()

    path = _write_seed_file(
        tmp_path / "p.json",
        _provider_entry(
            credentials={"api_key": "new"}, options={"model": "new"}
        ),
    )
    monkeypatch.setenv(PROVIDERS_FILE_ENV, str(path))
    monkeypatch.setenv(PROVIDERS_SEED_MODE_ENV, "overwrite")
    result = seed_providers_from_file(db_session, crypto)
    assert len(result.updated) == 1
    row = db_session.scalars(sa.select(ProviderCredential)).first()
    assert row is not None
    assert decrypt_json(crypto, row.credentials_encrypted) == {"api_key": "new"}


def test_seed_result_log_summary_format(
    db_session: Session, crypto: CredentialCrypto, tmp_path: Path
) -> None:
    path = _write_seed_file(tmp_path / "p.json", _provider_entry())
    result = seed_providers_from_file(db_session, crypto, path=path)
    summary = result.to_log_summary()
    assert "mode=insert-only" in summary
    assert "created=1" in summary
    assert str(path) in summary


def test_supported_file_version_constant() -> None:
    """A drift guard so any future bump intentionally updates the JSON shape."""
    assert SUPPORTED_FILE_VERSION == 1


def test_seed_module_exposes_expected_api() -> None:
    """Surface check — these names are exported to other modules."""
    for name in (
        "parse_providers_file",
        "seed_providers_from_file",
        "ProvidersFileError",
        "SeedMode",
        "SeedResult",
    ):
        assert hasattr(providers_seed, name), name


def test_seed_result_dataclass_defaults() -> None:
    """SeedResult has the empty-state expected by the lifespan hook."""
    res = SeedResult()
    assert res.created == [] and res.updated == [] and res.skipped == []
    assert res.activated == []
    assert res.mode is DEFAULT_SEED_MODE
