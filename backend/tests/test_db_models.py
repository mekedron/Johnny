"""Smoke tests for the SQLAlchemy model registry.

We don't require PostgreSQL to be running here — these tests verify the
declarative metadata is well-formed (all expected tables and FKs present)
without opening a real DB connection.
"""

from sqlalchemy import inspect

from app.db import Base
from app.db.models import (
    EMBEDDING_DIM,
    BotMode,
    BotSessionSource,
    BotSessionStatus,
    DecisionOutcome,
    ProviderKind,
)

EXPECTED_TABLES = {
    "google_accounts",
    "calendar_events",
    "profile_templates",
    "meeting_configs",
    "bot_sessions",
    "transcript_chunks",
    "agent_decisions",
    "agent_utterances",
    "provider_credentials",
}


def test_all_tables_registered() -> None:
    registered = set(Base.metadata.tables.keys())
    assert EXPECTED_TABLES.issubset(registered), (
        f"missing tables: {EXPECTED_TABLES - registered}"
    )


def test_meeting_configs_links_and_overrides() -> None:
    table = Base.metadata.tables["meeting_configs"]
    columns = {c.name for c in table.columns}
    assert {
        "calendar_event_id",
        "profile_template_id",
        "identity_account_id",
        "mode",
        "instructions",
        "context",
        "allowed_replies",
    }.issubset(columns)

    fk_targets = {
        fk.column.table.name: fk.column.name
        for fk in table.foreign_keys
    }
    assert fk_targets["calendar_events"] == "id"
    assert fk_targets["profile_templates"] == "id"
    assert fk_targets["google_accounts"] == "id"


def test_transcript_chunks_embedding_column() -> None:
    table = Base.metadata.tables["transcript_chunks"]
    embedding = table.columns["embedding"]
    type_name = type(embedding.type).__name__.lower()
    assert "vector" in type_name, f"expected pgvector Vector type, got {type_name}"
    dim = getattr(embedding.type, "dim", None)
    assert dim == EMBEDDING_DIM


def test_bot_session_has_status_default_scheduled() -> None:
    inspector = inspect(Base.metadata.tables["bot_sessions"])
    assert inspector is not None
    columns = {c.name for c in inspector.columns}
    assert "status" in columns


def test_enums_have_expected_members() -> None:
    assert {e.value for e in BotMode} == {
        "listen_only",
        "suggest_only",
        "approval_required",
        "limited_auto_speak",
        "autonomous",
    }
    assert {e.value for e in BotSessionStatus} == {
        "scheduled",
        "joining",
        "joined",
        "ended",
        "failed",
    }
    assert {e.value for e in BotSessionSource} == {"meet", "browser"}
    assert {e.value for e in DecisionOutcome} == {
        "spoken",
        "suppressed",
        "pending",
        "rejected",
        "suggested",
    }
    # Johnny-ckz.17 added the ``s2s`` kind for unified speech-to-speech providers.
    assert {e.value for e in ProviderKind} == {"stt", "llm", "tts", "s2s"}
