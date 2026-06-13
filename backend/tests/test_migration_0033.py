"""Migration tests for 0033_agent_meeting_bot_account (Johnny-wks.7).

Adds ``agents.meeting_bot_account_id`` — a nullable FK to ``google_accounts``
that holds the agent's meeting-bot join identity. Verifies on SQLite (the
``batch_alter_table`` table-recreate path; production Postgres takes the
plain-ALTER branch of the same op):

* the column is added and existing agent rows survive with it ``NULL``
  (behavior-preserving — no backfill, so resolution is unchanged from before);
* ``upgrade`` is idempotent against an already-migrated DB;
* ``downgrade`` drops the column again.

The pre-migration schema is built by hand (raw metadata, no app models — the
current ORM models already describe the POST-migration world).
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
    / "0033_agent_meeting_bot_account.py"
)


def _load_migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("_migration_0033", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load_migration_module()


@pytest.fixture
def engine(tmp_path: Path) -> sa.Engine:
    # File-backed (not :memory:) so batch_alter_table's table-recreate works
    # across the multiple connections alembic ops may open (0027 precedent).
    return sa.create_engine(f"sqlite:///{tmp_path}/migration_0033.db")


def _seed_pre_schema(engine: sa.Engine) -> None:
    """The pre-0033 shape of the tables the migration touches: google_accounts
    (the FK target) and an agents table WITHOUT meeting_bot_account_id."""
    md = sa.MetaData()
    sa.Table(
        "google_accounts",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
    )
    sa.Table(
        "agents",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("character_prompt", sa.Text, nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("allowed_replies", sa.JSON, nullable=False),
        sa.Column("confidence_threshold", sa.Float, nullable=False),
        sa.Column("is_default", sa.Boolean, nullable=False),
        sa.Column("tts_options", sa.JSON, nullable=False),
    )
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO agents (id, name, character_prompt, mode, "
                "allowed_replies, confidence_threshold, is_default, tts_options) "
                "VALUES (1, 'Johnny', '', 'autonomous', '[]', 0.7, 1, '{}')"
            )
        )


def _run(engine: sa.Engine, direction: str) -> None:
    func = getattr(_MODULE, direction)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            func()


def _columns(engine: sa.Engine, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def test_upgrade_adds_nullable_column_preserving_rows(engine: sa.Engine) -> None:
    _seed_pre_schema(engine)
    _run(engine, "upgrade")

    cols = {c["name"]: c for c in sa.inspect(engine).get_columns("agents")}
    assert "meeting_bot_account_id" in cols
    assert cols["meeting_bot_account_id"]["nullable"] is True

    # Behavior-preserving: the existing row survives with the new column NULL,
    # so meeting-bot identity resolution is unchanged for pre-wks.7 agents.
    with engine.begin() as conn:
        rows = conn.execute(
            sa.text("SELECT id, name, meeting_bot_account_id FROM agents")
        ).fetchall()
    assert rows == [(1, "Johnny", None)]


def test_upgrade_is_idempotent(engine: sa.Engine) -> None:
    _seed_pre_schema(engine)
    _run(engine, "upgrade")
    _run(engine, "upgrade")  # must not raise (column-exists guard)
    assert "meeting_bot_account_id" in _columns(engine, "agents")


def test_downgrade_drops_the_column(engine: sa.Engine) -> None:
    _seed_pre_schema(engine)
    _run(engine, "upgrade")
    _run(engine, "downgrade")
    assert "meeting_bot_account_id" not in _columns(engine, "agents")
