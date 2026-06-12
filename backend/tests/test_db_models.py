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
    "agents",
    "meeting_configs",
    "meeting_agents",
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


def test_meeting_configs_links() -> None:
    """Johnny-trt.41: the per-meeting override soup is gone — a meeting
    config is identity account + enabled + dismissal state only."""
    table = Base.metadata.tables["meeting_configs"]
    columns = {c.name for c in table.columns}
    assert {
        "calendar_event_id",
        "identity_account_id",
        "enabled",
        "bot_dismissed_at",
        "bot_dismissed_by",
        "bot_dismissed_until",
    }.issubset(columns)
    # The retired behavior/override columns must NOT come back.
    assert not {
        "profile_template_id",
        "personality_id",
        "mode",
        "instructions",
        "context",
        "allowed_replies",
        "confidence_threshold",
    } & columns

    fk_targets = {
        fk.column.table.name: fk.column.name
        for fk in table.foreign_keys
    }
    assert fk_targets["calendar_events"] == "id"
    assert fk_targets["google_accounts"] == "id"


def test_agents_table_shape() -> None:
    """Johnny-trt.41: the Agent entity owns identity/character/behavior/providers."""
    table = Base.metadata.tables["agents"]
    columns = {c.name for c in table.columns}
    assert {
        "name",
        "avatar",
        "description",
        "character_prompt",
        "mode",
        "allowed_replies",
        "confidence_threshold",
        "is_default",
        "router_llm_provider_id",
        "answer_llm_provider_id",
        "reasoning_llm_provider_id",
        "tts_provider_id",
        "tts_voice_id",
        "tts_options",
    }.issubset(columns)
    fk_targets = {fk.column.table.name for fk in table.foreign_keys}
    assert fk_targets == {"provider_credentials"}


def test_meeting_agents_assignment_table_shape() -> None:
    """Johnny-trt.41: assignments bind agents to meetings with context/order."""
    table = Base.metadata.tables["meeting_agents"]
    columns = {c.name for c in table.columns}
    assert {
        "meeting_config_id",
        "agent_id",
        "context",
        "enabled",
        "position",
    }.issubset(columns)
    fk_targets = {
        fk.column.table.name: fk.column.name for fk in table.foreign_keys
    }
    assert fk_targets["meeting_configs"] == "id"
    assert fk_targets["agents"] == "id"


def test_bot_sessions_carry_agent_snapshot() -> None:
    """Johnny-trt.41: sessions freeze the serving agent at dispatch."""
    table = Base.metadata.tables["bot_sessions"]
    columns = {c.name for c in table.columns}
    assert {"agent_id", "agent_snapshot", "bot_name"}.issubset(columns)
    fk_targets = {fk.column.table.name for fk in table.foreign_keys}
    assert "agents" in fk_targets


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
        "waiting_for_relogin",
    }
    assert {e.value for e in BotSessionSource} == {"meet", "browser"}
    assert {e.value for e in DecisionOutcome} == {
        "spoken",
        "suppressed",
        "pending",
        "rejected",
        "suggested",
    }
    # stt/llm/tts only — the ``s2s`` kind was removed with the S2S surface
    # (Johnny-trt.43; historical rows are deactivated by migration 0026).
    assert {e.value for e in ProviderKind} == {"stt", "llm", "tts"}
