"""Migration tests for 0042_request_id_correlation (Johnny-d6w.3, US-003).

Verifies the additive ``request_id`` / ``answers_request_id`` columns land on the
three pre-existing tables, the three new indexes are created (boot drift only
checks columns, so the migration is the sole guarantee the indexes exist), and
the migration is idempotent + reversible. SQLite is enough — the migration uses
``add_column`` / ``create_index`` with inspector guards (the 0040 pattern).
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
    / "0042_request_id_correlation.py"
)

ADDED_COLUMNS = {
    "agent_decisions": "request_id",
    "agent_utterances": "answers_request_id",
    "agent_tasks": "request_id",
}
ADDED_INDEXES = {
    "agent_decisions": {
        "ix_agent_decisions_request_id",
        "ix_agent_decisions_turn_id",
    },
    "agent_utterances": {"ix_agent_utterances_answers_request_id"},
}


def _load_migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("_migration_0042", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load_migration_module()


def _seed_schema(engine: sa.Engine) -> None:
    """The pre-0042 tables the new columns/indexes are added to.

    ``agent_decisions`` needs ``turn_id`` so ``ix_agent_decisions_turn_id`` has a
    column to index.
    """
    md = sa.MetaData()
    sa.Table(
        "agent_decisions",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("turn_id", sa.Integer),
    )
    sa.Table("agent_utterances", md, sa.Column("id", sa.Integer, primary_key=True))
    sa.Table("agent_tasks", md, sa.Column("id", sa.Integer, primary_key=True))
    md.create_all(engine)


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> sa.Engine:
    url = f"sqlite:///{tmp_path / 'mig42.db'}"
    engine = sa.create_engine(url, future=True)
    _seed_schema(engine)
    return engine


def _run(engine: sa.Engine, direction: str) -> None:
    func = getattr(_MODULE, direction)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            func()


def test_upgrade_adds_columns(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    inspector = sa.inspect(sqlite_engine)
    for table, column in ADDED_COLUMNS.items():
        cols = {c["name"]: c for c in inspector.get_columns(table)}
        assert column in cols, f"{table}.{column} missing after upgrade"
        assert cols[column]["nullable"] is True  # all are nullable UUID handles


def test_upgrade_adds_indexes(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    inspector = sa.inspect(sqlite_engine)
    for table, names in ADDED_INDEXES.items():
        existing = {ix["name"] for ix in inspector.get_indexes(table)}
        assert names.issubset(existing), f"{table} missing {names - existing}"


def test_upgrade_is_idempotent(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    _run(sqlite_engine, "upgrade")  # second run hits the column + index guards
    inspector = sa.inspect(sqlite_engine)
    cols = {c["name"] for c in inspector.get_columns("agent_decisions")}
    assert "request_id" in cols
    index_names = {ix["name"] for ix in inspector.get_indexes("agent_decisions")}
    assert "ix_agent_decisions_request_id" in index_names


def test_downgrade_drops_columns_and_indexes(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    _run(sqlite_engine, "downgrade")
    inspector = sa.inspect(sqlite_engine)
    for table, column in ADDED_COLUMNS.items():
        cols = {c["name"] for c in inspector.get_columns(table)}
        assert column not in cols, f"{table}.{column} should be dropped"
    for table, names in ADDED_INDEXES.items():
        existing = {ix["name"] for ix in inspector.get_indexes(table)}
        assert not (names & existing), f"{table} kept {names & existing}"
    # A second downgrade is a guarded no-op (drop on absent columns/indexes).
    _run(sqlite_engine, "downgrade")


def test_chain_metadata() -> None:
    assert _MODULE.revision == "0042"
    assert _MODULE.down_revision == "0041"
