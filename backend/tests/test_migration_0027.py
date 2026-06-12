"""Migration tests for 0027_agents_rebuild (Johnny-trt.41).

Exercises the destructive rebuild on SQLite end-to-end (the column drops go
through ``batch_alter_table``, so the full path runs here; production
Postgres takes the plain-ALTER branch of the same operations):

* clean-DB shape: ``agents`` + ``meeting_agents`` created, the canonical
  "Johnny" default seeded, ``bot_sessions`` gains agent_id/agent_snapshot,
  ``meeting_configs`` loses the override soup, the retired tables are gone;
* populated-DB carry-over: the operator's default personality rides into
  the seeded default agent (name / character text / mode / answer-LLM +
  TTS pins) and existing meeting_configs / bot_sessions rows survive with
  their calendar/identity/dismissal state intact;
* idempotency of ``upgrade`` against an already-migrated DB;
* structural ``downgrade`` (old tables/columns back, agents surface gone).

The pre-migration schema is built by hand (raw metadata, no app models —
the current ORM models describe the POST-migration world).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_MIGRATION_FILE = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0027_agents_rebuild.py"
)


def _load_migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("_migration_0027", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load_migration_module()


@pytest.fixture
def engine(tmp_path: Path) -> sa.Engine:
    # File-backed (not :memory:) so batch_alter_table's table-recreate works
    # across the multiple connections alembic ops may open.
    return sa.create_engine(f"sqlite:///{tmp_path}/migration_0027.db")


def _seed_pre_schema(engine: sa.Engine) -> None:
    """The pre-0027 shape of every table the migration touches."""
    md = sa.MetaData()
    sa.Table(
        "provider_credentials",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("provider_name", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("credentials_encrypted", sa.Text, nullable=False),
        sa.Column("config", sa.JSON, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False),
    )
    sa.Table(
        "personalities",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("is_default", sa.Boolean, nullable=False),
        sa.Column("llm_provider_id", sa.Integer),
        sa.Column("tts_provider_id", sa.Integer),
        sa.Column("default_mode", sa.String(32)),
        sa.Column("metadata", sa.JSON, nullable=False),
    )
    sa.Table(
        "profile_templates",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("base_instructions", sa.Text, nullable=False),
        sa.Column("base_context", sa.Text, nullable=False),
        sa.Column("allowed_replies", sa.JSON, nullable=False),
        sa.Column("confidence_threshold", sa.Float, nullable=False),
    )
    sa.Table(
        "google_accounts",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
    )
    sa.Table(
        "calendar_events",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("account_id", sa.Integer, nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
    )
    sa.Table(
        "meeting_configs",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "calendar_event_id",
            sa.Integer,
            sa.ForeignKey("calendar_events.id"),
            nullable=False,
        ),
        sa.Column(
            "profile_template_id",
            sa.Integer,
            sa.ForeignKey("profile_templates.id"),
            nullable=False,
        ),
        sa.Column(
            "identity_account_id",
            sa.Integer,
            sa.ForeignKey("google_accounts.id"),
            nullable=False,
        ),
        sa.Column(
            "personality_id", sa.Integer, sa.ForeignKey("personalities.id")
        ),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("instructions", sa.Text),
        sa.Column("context", sa.Text),
        sa.Column("allowed_replies", sa.JSON),
        sa.Column("confidence_threshold", sa.Float),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("bot_dismissed_at", sa.DateTime(timezone=True)),
        sa.Column("bot_dismissed_by", sa.String(16)),
        sa.Column("bot_dismissed_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    sa.Table(
        "bot_sessions",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("meeting_config_id", sa.Integer),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("bot_name", sa.String(128)),
    )
    md.create_all(engine)


def _run(engine: sa.Engine, direction: str) -> None:
    func = getattr(_MODULE, direction)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            func()


def _tables(engine: sa.Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def _columns(engine: sa.Engine, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def _agents_rows(engine: sa.Engine) -> list[Any]:
    with engine.begin() as conn:
        return conn.execute(
            sa.text(
                "SELECT name, character_prompt, mode, is_default, "
                "answer_llm_provider_id, tts_provider_id FROM agents"
            )
        ).fetchall()


# --- clean install ------------------------------------------------------------


def test_upgrade_clean_creates_agents_surface(engine: sa.Engine) -> None:
    _seed_pre_schema(engine)
    _run(engine, "upgrade")

    tables = _tables(engine)
    assert "agents" in tables
    assert "meeting_agents" in tables
    assert "profile_templates" not in tables
    assert "personalities" not in tables

    rows = _agents_rows(engine)
    assert len(rows) == 1
    name, prompt, mode, is_default, answer_llm, tts = rows[0]
    assert name == "Johnny"
    assert "Night City" in prompt
    assert mode == "autonomous"
    assert bool(is_default) is True
    assert answer_llm is None and tts is None

    # bot_sessions gained the dispatch freeze columns.
    assert {"agent_id", "agent_snapshot"} <= _columns(engine, "bot_sessions")

    # meeting_configs lost the override soup, kept its own state.
    config_columns = _columns(engine, "meeting_configs")
    for dropped in (
        "profile_template_id",
        "personality_id",
        "mode",
        "instructions",
        "context",
        "allowed_replies",
        "confidence_threshold",
    ):
        assert dropped not in config_columns
    for kept in (
        "calendar_event_id",
        "identity_account_id",
        "enabled",
        "bot_dismissed_at",
        "bot_dismissed_by",
        "bot_dismissed_until",
    ):
        assert kept in config_columns


def test_upgrade_is_idempotent(engine: sa.Engine) -> None:
    _seed_pre_schema(engine)
    _run(engine, "upgrade")
    _run(engine, "upgrade")  # must not raise or duplicate the seed
    assert len(_agents_rows(engine)) == 1


def test_single_default_index_enforced_after_upgrade(engine: sa.Engine) -> None:
    _seed_pre_schema(engine)
    _run(engine, "upgrade")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO agents (name, character_prompt, mode, "
                "allowed_replies, confidence_threshold, is_default, tts_options) "
                "VALUES ('Second', '', 'listen_only', '[]', 0.7, FALSE, '{}')"
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO agents (name, character_prompt, mode, "
                    "allowed_replies, confidence_threshold, is_default, tts_options) "
                    "VALUES ('Third', '', 'listen_only', '[]', 0.7, TRUE, '{}')"
                )
            )


# --- populated dev DB -----------------------------------------------------------


def _seed_populated(engine: sa.Engine) -> None:
    _seed_pre_schema(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO provider_credentials "
                "(id, kind, provider_name, display_name, credentials_encrypted, "
                "config, is_active) VALUES "
                "(11, 'llm', 'ollama', 'Ollama', 'enc', '{}', 1), "
                "(12, 'tts', 'piper', 'Piper', 'enc', '{}', 1)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO personalities (id, display_name, description, "
                "is_default, llm_provider_id, tts_provider_id, default_mode, "
                "metadata) VALUES "
                "(1, 'Custom Johnny', 'My edited persona.', 1, 11, 12, "
                "'approval_required', '{}'), "
                "(2, 'Spare', 'Unused.', 0, NULL, NULL, NULL, '{}')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO profile_templates (id, name, mode, "
                "base_instructions, base_context, allowed_replies, "
                "confidence_threshold) VALUES "
                "(1, 'Standup', 'listen_only', 'listen', 'ctx', '[]', 0.7)"
            )
        )
        conn.execute(
            sa.text("INSERT INTO google_accounts (id, email) VALUES (1, 'a@b.c')")
        )
        conn.execute(
            sa.text(
                "INSERT INTO calendar_events (id, account_id, external_id) "
                "VALUES (1, 1, 'evt-1')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO meeting_configs (id, calendar_event_id, "
                "profile_template_id, identity_account_id, personality_id, "
                "mode, instructions, context, allowed_replies, "
                "confidence_threshold, enabled, bot_dismissed_by) VALUES "
                "(1, 1, 1, 1, 1, 'approval_required', 'instr', 'ctx', "
                "'[\"Yes.\"]', 0.8, 1, 'ui')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO bot_sessions (id, meeting_config_id, status, "
                "bot_name) VALUES (1, 1, 'ended', 'Custom Johnny')"
            )
        )


def test_upgrade_populated_carries_default_personality(engine: sa.Engine) -> None:
    _seed_populated(engine)
    _run(engine, "upgrade")

    rows = _agents_rows(engine)
    assert len(rows) == 1
    name, prompt, mode, is_default, answer_llm, tts = rows[0]
    assert name == "Custom Johnny"
    assert prompt == "My edited persona."
    assert mode == "approval_required"
    assert bool(is_default) is True
    assert answer_llm == 11  # old llm pin → answer role slot
    assert tts == 12

    # Existing rows survive the reshape with their state intact.
    with engine.begin() as conn:
        config = conn.execute(
            sa.text(
                "SELECT calendar_event_id, identity_account_id, enabled, "
                "bot_dismissed_by FROM meeting_configs WHERE id = 1"
            )
        ).one()
        assert tuple(config) == (1, 1, 1, "ui")
        session_row = conn.execute(
            sa.text(
                "SELECT bot_name, agent_id, agent_snapshot FROM bot_sessions "
                "WHERE id = 1"
            )
        ).one()
        assert session_row.bot_name == "Custom Johnny"
        assert session_row.agent_id is None
        assert session_row.agent_snapshot is None


# --- downgrade -------------------------------------------------------------------


def test_downgrade_restores_structure(engine: sa.Engine) -> None:
    _seed_pre_schema(engine)
    _run(engine, "upgrade")
    _run(engine, "downgrade")

    tables = _tables(engine)
    assert "agents" not in tables
    assert "meeting_agents" not in tables
    assert "profile_templates" in tables
    assert "personalities" in tables

    config_columns = _columns(engine, "meeting_configs")
    for restored in (
        "profile_template_id",
        "personality_id",
        "mode",
        "instructions",
        "context",
        "allowed_replies",
        "confidence_threshold",
    ):
        assert restored in config_columns

    session_columns = _columns(engine, "bot_sessions")
    assert "agent_id" not in session_columns
    assert "agent_snapshot" not in session_columns


def test_downgrade_is_idempotent(engine: sa.Engine) -> None:
    _seed_pre_schema(engine)
    _run(engine, "upgrade")
    _run(engine, "downgrade")
    _run(engine, "downgrade")  # second pass must be a clean no-op
