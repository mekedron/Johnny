"""Per-agent provider resolution matrix (Johnny-trt.42).

:func:`app.services.agent_providers.resolve_agent_provider_payload` turns the
agent snapshot's pins into the session's effective ``provider_config``. The
matrix here pins the contract:

* no snapshot / no pins → payload passes through (plus the always-on
  reasoning descriptor of the global LLM);
* answer / router pins swap the right entries — ``router_llm`` emitted only
  when the resolved row differs from the answer entry, and a pinned ANSWER
  with an unpinned router still routes triage to the GLOBAL active;
* TTS pins replace the entry and merge ``tts_voice_id`` / ``tts_options``
  into the adapter-facing ``options`` — and the merge never applies to a
  fallback provider;
* pins honor INACTIVE rows (only one row per kind can be globally active —
  the two-agents-two-voices acceptance depends on it);
* unusable pins (missing row / wrong kind / undecryptable credentials) fall
  back to the global-active entry with a :class:`ProviderFallbackWarning`,
  and :func:`persist_provider_fallback_warnings` renders each as a turn-0
  ``provider_switch`` row in ``session_timings`` naming agent + provider.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import BotSession, ProviderCredential, SessionTiming
from app.providers.base import ProviderKind
from app.security.crypto import CredentialCrypto, encrypt_json
from app.services.agent_providers import (
    persist_provider_fallback_warnings,
    resolve_agent_provider_payload,
)
from app.services.provider_payload import build_provider_payload


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    # Only the tables this seam touches; SQLite leaves FK enforcement off so
    # the bot_sessions row needs no meeting/account graph.
    Base.metadata.create_all(
        bind=eng,
        tables=[
            ProviderCredential.__table__,  # type: ignore[list-item]
            BotSession.__table__,  # type: ignore[list-item]
            SessionTiming.__table__,  # type: ignore[list-item]
        ],
    )
    return eng


@pytest.fixture
def db(engine: sa.Engine) -> Iterator[Session]:
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def crypto() -> CredentialCrypto:
    return CredentialCrypto(Fernet.generate_key())


def _add_provider(
    db: Session,
    crypto: CredentialCrypto,
    *,
    kind: ProviderKind,
    provider_name: str,
    display_name: str,
    credentials: dict[str, str] | None = None,
    options: dict[str, Any] | None = None,
    is_active: bool = False,
    garbage_ciphertext: bool = False,
) -> ProviderCredential:
    row = ProviderCredential(
        kind=kind,
        provider_name=provider_name,
        display_name=display_name,
        credentials_encrypted=(
            "not-fernet"
            if garbage_ciphertext
            else encrypt_json(crypto, credentials or {"api_key": "k"})
        ),
        config=dict(options or {}),
        is_active=is_active,
    )
    db.add(row)
    db.flush()
    return row


def _snapshot(pins: dict[str, Any], *, agent_id: int = 5, name: str = "Echo") -> dict[str, Any]:
    """A trt.41-shaped agent snapshot carrying only what the resolver reads."""
    providers = {
        "router_llm_provider_id": None,
        "answer_llm_provider_id": None,
        "reasoning_llm_provider_id": None,
        "tts_provider_id": None,
        "tts_voice_id": None,
        "tts_options": {},
    }
    providers.update(pins)
    return {"agent_id": agent_id, "name": name, "providers": providers}


@pytest.fixture
def base_rows(
    db: Session, crypto: CredentialCrypto
) -> dict[str, ProviderCredential]:
    """The canonical global-active trio + spare inactive rows to pin."""
    return {
        "stt": _add_provider(
            db, crypto, kind=ProviderKind.STT, provider_name="parakeet",
            display_name="Parakeet", is_active=True,
        ),
        "llm": _add_provider(
            db, crypto, kind=ProviderKind.LLM, provider_name="openai-compatible",
            display_name="Ollama tiny", options={"model": "llama3.2:3b"},
            is_active=True,
        ),
        "tts": _add_provider(
            db, crypto, kind=ProviderKind.TTS, provider_name="piper",
            display_name="Piper", options={"voice_id": "en_US-amy"},
            is_active=True,
        ),
        "llm_big": _add_provider(
            db, crypto, kind=ProviderKind.LLM, provider_name="openai",
            display_name="Cloud big", options={"model": "gpt-large"},
        ),
        "tts_eleven": _add_provider(
            db, crypto, kind=ProviderKind.TTS, provider_name="elevenlabs",
            display_name="ElevenLabs", options={"model_id": "eleven_v3"},
        ),
    }


def _base_payload(db: Session, crypto: CredentialCrypto) -> dict[str, Any]:
    return build_provider_payload(db, crypto)


# --- pass-through ------------------------------------------------------------


def test_no_snapshot_passes_payload_through(
    db: Session, crypto: CredentialCrypto, base_rows: dict[str, ProviderCredential]
) -> None:
    base = _base_payload(db, crypto)
    resolved = resolve_agent_provider_payload(db, crypto, base_payload=base, snapshot=None)
    assert resolved.payload == base
    assert resolved.warnings == ()


def test_base_payload_entries_carry_provider_id(
    db: Session, crypto: CredentialCrypto, base_rows: dict[str, ProviderCredential]
) -> None:
    base = _base_payload(db, crypto)
    assert base["llm"]["provider_id"] == base_rows["llm"].id
    assert base["tts"]["provider_id"] == base_rows["tts"].id


def test_unpinned_agent_keeps_entries_and_stamps_global_reasoning(
    db: Session, crypto: CredentialCrypto, base_rows: dict[str, ProviderCredential]
) -> None:
    base = _base_payload(db, crypto)
    resolved = resolve_agent_provider_payload(
        db, crypto, base_payload=base, snapshot=_snapshot({})
    )
    assert resolved.warnings == ()
    assert resolved.payload["llm"] == base["llm"]
    assert resolved.payload["tts"] == base["tts"]
    assert "router_llm" not in resolved.payload
    # Reasoning falls through the chain to the global active LLM — identity
    # only, never credentials.
    descriptor = resolved.payload["reasoning_llm"]
    assert descriptor == {
        "provider_name": "openai-compatible",
        "display_name": "Ollama tiny",
        "provider_id": base_rows["llm"].id,
        "model": "llama3.2:3b",
    }
    assert "credentials" not in descriptor


# --- LLM role pins -----------------------------------------------------------


def test_answer_pin_swaps_llm_and_keeps_router_on_global(
    db: Session, crypto: CredentialCrypto, base_rows: dict[str, ProviderCredential]
) -> None:
    """Pinning ONLY the answer slot must not drag triage onto the big model:
    the router role inherits the GLOBAL chain, so an explicit ``router_llm``
    entry pointing back at the global active appears."""
    base = _base_payload(db, crypto)
    resolved = resolve_agent_provider_payload(
        db, crypto, base_payload=base,
        snapshot=_snapshot({"answer_llm_provider_id": base_rows["llm_big"].id}),
    )
    assert resolved.warnings == ()
    assert resolved.payload["llm"]["display_name"] == "Cloud big"
    assert resolved.payload["llm"]["provider_id"] == base_rows["llm_big"].id
    assert resolved.payload["llm"]["credentials"] == {"api_key": "k"}
    assert resolved.payload["router_llm"]["display_name"] == "Ollama tiny"
    assert resolved.payload["router_llm"]["provider_id"] == base_rows["llm"].id


def test_router_pin_emits_router_entry_and_keeps_answer(
    db: Session, crypto: CredentialCrypto, base_rows: dict[str, ProviderCredential]
) -> None:
    base = _base_payload(db, crypto)
    resolved = resolve_agent_provider_payload(
        db, crypto, base_payload=base,
        snapshot=_snapshot({"router_llm_provider_id": base_rows["llm_big"].id}),
    )
    assert resolved.warnings == ()
    assert resolved.payload["llm"] == base["llm"]
    assert resolved.payload["router_llm"]["display_name"] == "Cloud big"


def test_identical_router_and_answer_pins_collapse_to_one_entry(
    db: Session, crypto: CredentialCrypto, base_rows: dict[str, ProviderCredential]
) -> None:
    base = _base_payload(db, crypto)
    resolved = resolve_agent_provider_payload(
        db, crypto, base_payload=base,
        snapshot=_snapshot(
            {
                "router_llm_provider_id": base_rows["llm_big"].id,
                "answer_llm_provider_id": base_rows["llm_big"].id,
            }
        ),
    )
    assert resolved.payload["llm"]["display_name"] == "Cloud big"
    # Same provider row → no router_llm key → the session builds ONE instance.
    assert "router_llm" not in resolved.payload


def test_pins_honor_inactive_rows(
    db: Session, crypto: CredentialCrypto, base_rows: dict[str, ProviderCredential]
) -> None:
    """Only one row per kind can be globally active, so per-agent pins MUST
    resolve inactive rows — otherwise two agents could never run two TTS
    providers (the trt.42 A/B acceptance)."""
    assert base_rows["llm_big"].is_active is False
    base = _base_payload(db, crypto)
    resolved = resolve_agent_provider_payload(
        db, crypto, base_payload=base,
        snapshot=_snapshot({"answer_llm_provider_id": base_rows["llm_big"].id}),
    )
    assert resolved.warnings == ()
    assert resolved.payload["llm"]["display_name"] == "Cloud big"


def test_reasoning_pin_stamps_descriptor_without_credentials(
    db: Session, crypto: CredentialCrypto, base_rows: dict[str, ProviderCredential]
) -> None:
    base = _base_payload(db, crypto)
    resolved = resolve_agent_provider_payload(
        db, crypto, base_payload=base,
        snapshot=_snapshot({"reasoning_llm_provider_id": base_rows["llm_big"].id}),
    )
    descriptor = resolved.payload["reasoning_llm"]
    assert descriptor["display_name"] == "Cloud big"
    assert descriptor["model"] == "gpt-large"
    assert descriptor["provider_id"] == base_rows["llm_big"].id
    assert "credentials" not in descriptor
    # The whole payload (descriptor included) survives the dispatch JSON.
    json.dumps(resolved.payload)


# --- TTS pin + voice ---------------------------------------------------------


def test_tts_pin_swaps_entry_and_applies_voice_and_options(
    db: Session, crypto: CredentialCrypto, base_rows: dict[str, ProviderCredential]
) -> None:
    base = _base_payload(db, crypto)
    resolved = resolve_agent_provider_payload(
        db, crypto, base_payload=base,
        snapshot=_snapshot(
            {
                "tts_provider_id": base_rows["tts_eleven"].id,
                "tts_voice_id": "Rachel",
                "tts_options": {"stability": 0.4},
            }
        ),
    )
    assert resolved.warnings == ()
    tts = resolved.payload["tts"]
    assert tts["provider_name"] == "elevenlabs"
    # Agent options merge over the row config; the voice wins last — the
    # exact ``options["voice_id"]`` key the adapter factory feeds JohnnyTTS.
    assert tts["options"] == {
        "model_id": "eleven_v3",
        "stability": 0.4,
        "voice_id": "Rachel",
    }


def test_tts_pin_without_voice_keeps_provider_default(
    db: Session, crypto: CredentialCrypto, base_rows: dict[str, ProviderCredential]
) -> None:
    base = _base_payload(db, crypto)
    resolved = resolve_agent_provider_payload(
        db, crypto, base_payload=base,
        snapshot=_snapshot({"tts_provider_id": base_rows["tts_eleven"].id}),
    )
    assert resolved.payload["tts"]["options"] == {"model_id": "eleven_v3"}


# --- unusable pins → fallback + warning ---------------------------------------


def test_missing_pinned_provider_falls_back_with_warning(
    db: Session, crypto: CredentialCrypto, base_rows: dict[str, ProviderCredential]
) -> None:
    base = _base_payload(db, crypto)
    resolved = resolve_agent_provider_payload(
        db, crypto, base_payload=base,
        snapshot=_snapshot(
            {"tts_provider_id": 9999, "tts_voice_id": "Rachel"},
            name="Echo",
        ),
    )
    # Session still starts on the global default…
    assert resolved.payload["tts"]["provider_name"] == "piper"
    # …and the pinned voice must NOT leak onto the fallback provider (the
    # global row's own configured voice survives untouched).
    assert resolved.payload["tts"]["options"] == {"voice_id": "en_US-amy"}
    assert len(resolved.warnings) == 1
    warning = resolved.warnings[0]
    assert warning.role == "tts"
    assert warning.reason == "missing"
    assert warning.agent_name == "Echo"
    assert warning.pinned_provider_id == 9999
    assert warning.fallback_provider_name == "Piper"
    assert "Echo" in warning.message and "9999" in warning.message


def test_wrong_kind_pin_falls_back_with_warning(
    db: Session, crypto: CredentialCrypto, base_rows: dict[str, ProviderCredential]
) -> None:
    base = _base_payload(db, crypto)
    resolved = resolve_agent_provider_payload(
        db, crypto, base_payload=base,
        snapshot=_snapshot({"answer_llm_provider_id": base_rows["tts_eleven"].id}),
    )
    assert resolved.payload["llm"]["display_name"] == "Ollama tiny"
    assert [w.reason for w in resolved.warnings] == ["wrong_kind"]
    assert resolved.warnings[0].role == "answer_llm"
    assert resolved.warnings[0].pinned_provider_name == "ElevenLabs"


def test_undecryptable_pin_falls_back_with_warning(
    db: Session, crypto: CredentialCrypto, base_rows: dict[str, ProviderCredential]
) -> None:
    broken = _add_provider(
        db, crypto, kind=ProviderKind.LLM, provider_name="openai",
        display_name="Rotated key", garbage_ciphertext=True,
    )
    base = _base_payload(db, crypto)
    resolved = resolve_agent_provider_payload(
        db, crypto, base_payload=base,
        snapshot=_snapshot({"router_llm_provider_id": broken.id}),
    )
    assert "router_llm" not in resolved.payload  # fell back to the answer entry
    assert [w.reason for w in resolved.warnings] == ["decrypt_failed"]


def test_fallback_without_global_entry_warns_with_none_fallback(
    db: Session, crypto: CredentialCrypto
) -> None:
    """A broken TTS pin with NO global TTS configured: the warning still
    surfaces (fallback name None) and the payload simply has no tts entry —
    the session degrades to suggest_only exactly like an unpinned one."""
    _add_provider(
        db, crypto, kind=ProviderKind.LLM, provider_name="openai-compatible",
        display_name="Ollama tiny", is_active=True,
    )
    base = _base_payload(db, crypto)
    assert "tts" not in base
    resolved = resolve_agent_provider_payload(
        db, crypto, base_payload=base,
        snapshot=_snapshot({"tts_provider_id": 4242}),
    )
    assert "tts" not in resolved.payload
    assert resolved.warnings[0].fallback_provider_name is None


# --- activity-log persistence -------------------------------------------------


def test_warnings_persist_as_turn0_provider_switch_rows(
    db: Session, crypto: CredentialCrypto, base_rows: dict[str, ProviderCredential]
) -> None:
    session_row = BotSession()
    db.add(session_row)
    db.flush()

    base = _base_payload(db, crypto)
    resolved = resolve_agent_provider_payload(
        db, crypto, base_payload=base,
        snapshot=_snapshot({"tts_provider_id": 9999}, agent_id=5, name="Echo"),
    )
    persist_provider_fallback_warnings(
        db, bot_session_id=session_row.id, warnings=resolved.warnings
    )
    db.flush()

    rows = db.scalars(sa.select(SessionTiming)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.bot_session_id == session_row.id
    assert row.turn_id == 0
    assert row.stage == "provider_switch"
    assert row.provider_name == "Piper"
    assert row.details["agent_name"] == "Echo"
    assert row.details["pinned_provider_id"] == 9999
    assert row.details["reason"] == "missing"
    assert "Echo" in row.details["message"]


def test_summary_names_what_actually_resolved(
    db: Session, crypto: CredentialCrypto, base_rows: dict[str, ProviderCredential]
) -> None:
    base = _base_payload(db, crypto)
    resolved = resolve_agent_provider_payload(
        db, crypto, base_payload=base,
        snapshot=_snapshot(
            {
                "router_llm_provider_id": base_rows["llm_big"].id,
                "tts_provider_id": base_rows["tts_eleven"].id,
                "tts_voice_id": "Rachel",
            }
        ),
    )
    assert resolved.summary == {
        "router_llm": "Cloud big",
        "answer_llm": "Ollama tiny",
        "reasoning_llm": "Ollama tiny",
        "tts": "ElevenLabs voice=Rachel",
    }
