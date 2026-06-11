"""Migration tests for 0023_agent_tasks (Johnny-trt.18).

Verifies the ``agent_tasks`` table is created with the full column set,
defaults (``status='queued'``, ``attempts=0``), the status CHECK constraint
values, both indexes, and that the migration is idempotent (re-running the
upgrade on an applied schema is a no-op) and reversible.

SQLite is enough — the migration uses ``create_table`` / ``create_index``
with the portable JSON variant (the 0014 pattern).
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
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0023_agent_tasks.py"
)

EXPECTED_COLUMNS = {
    "id",
    "bot_session_id",
    "agent_decision_id",
    "turn_id",
    "kind",
    "request_json",
    "status",
    "ack_text",
    "result_text",
    "result_json",
    "error",
    "attempts",
    "callback_token",
    "created_at",
    "updated_at",
}


def _load_migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("_migration_0023", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load_migration_module()


def _seed_schema(engine: sa.Engine) -> None:
    """Pre-0023 parents: bot_sessions + agent_decisions exist, agent_tasks doesn't."""
    md = sa.MetaData()
    sa.Table("bot_sessions", md, sa.Column("id", sa.Integer, primary_key=True))
    sa.Table("agent_decisions", md, sa.Column("id", sa.Integer, primary_key=True))
    md.create_all(engine)


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> sa.Engine:
    url = f"sqlite:///{tmp_path / 'mig23.db'}"
    engine = sa.create_engine(url, future=True)
    _seed_schema(engine)
    return engine


def _run(engine: sa.Engine, direction: str) -> None:
    func = getattr(_MODULE, direction)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            func()


def test_upgrade_creates_table_with_expected_columns(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    inspector = sa.inspect(sqlite_engine)
    assert "agent_tasks" in inspector.get_table_names()
    cols = {c["name"] for c in inspector.get_columns("agent_tasks")}
    assert cols == EXPECTED_COLUMNS


def test_upgrade_creates_both_indexes(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    inspector = sa.inspect(sqlite_engine)
    index_names = {ix["name"] for ix in inspector.get_indexes("agent_tasks")}
    assert "ix_agent_tasks_session_created" in index_names
    assert "ix_agent_tasks_status" in index_names


def test_upgrade_row_defaults_and_writability(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    with sqlite_engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO bot_sessions (id) VALUES (1)"))
        conn.execute(
            sa.text(
                "INSERT INTO agent_tasks (bot_session_id, kind, request_json) "
                "VALUES (1, 'web_search', '{}')"
            )
        )
        row = conn.execute(
            sa.text("SELECT status, attempts, created_at, updated_at FROM agent_tasks")
        ).one()
    assert row[0] == "queued"  # server default
    assert row[1] == 0  # server default
    assert row[2] is not None
    assert row[3] is not None


def test_status_check_constraint_matches_model_enum(sqlite_engine: sa.Engine) -> None:
    # The migration's value list is the contract the CHECK enforces; assert it
    # mirrors AgentTaskStatus so the two can't drift silently.
    from app.db.models import AgentTaskStatus

    assert set(_MODULE.TASK_STATUSES) == {m.value for m in AgentTaskStatus}

    _run(sqlite_engine, "upgrade")
    with sqlite_engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO bot_sessions (id) VALUES (1)"))
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO agent_tasks (bot_session_id, kind, request_json, status) "
                    "VALUES (1, 'web_search', '{}', 'exploded')"
                )
            )


def test_upgrade_is_idempotent(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    _run(sqlite_engine, "upgrade")  # second run hits the inspector guard, no raise
    inspector = sa.inspect(sqlite_engine)
    assert "agent_tasks" in inspector.get_table_names()


def test_upgrade_then_downgrade_drops_table(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    _run(sqlite_engine, "downgrade")
    inspector = sa.inspect(sqlite_engine)
    assert "agent_tasks" not in inspector.get_table_names()
    # Downgrade on an absent table is also a guarded no-op.
    _run(sqlite_engine, "downgrade")
