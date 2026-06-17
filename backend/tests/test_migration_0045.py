"""Migration tests for 0045_requested_by_participant (Johnny-d6w.19, US-401).

Verifies the additive ``requested_by`` column lands on the three audit tables
(agent_decisions / agent_utterances / agent_workstreams), is nullable + 128-wide,
and the migration is idempotent + reversible. SQLite is enough — the migration
uses ``add_column`` with inspector guards (the 0040/0043 pattern).
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
    / "0045_requested_by_participant.py"
)

_TABLES = ("agent_decisions", "agent_utterances", "agent_workstreams")
_COLUMN = "requested_by"


def _load_migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("_migration_0045", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load_migration_module()


def _seed_schema(engine: sa.Engine) -> None:
    """Minimal pre-0045 shape of the three tables the column is added to."""
    md = sa.MetaData()
    for table in _TABLES:
        sa.Table(table, md, sa.Column("id", sa.Integer, primary_key=True))
    md.create_all(engine)


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> sa.Engine:
    url = f"sqlite:///{tmp_path / 'mig45.db'}"
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
    for table in _TABLES:
        cols = {c["name"]: c for c in inspector.get_columns(table)}
        assert _COLUMN in cols, f"{table}.{_COLUMN} missing after upgrade"
        assert cols[_COLUMN]["nullable"] is True


def test_upgrade_is_idempotent(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    _run(sqlite_engine, "upgrade")  # second run hits the column guards
    inspector = sa.inspect(sqlite_engine)
    for table in _TABLES:
        cols = {c["name"] for c in inspector.get_columns(table)}
        assert _COLUMN in cols


def test_downgrade_drops_columns(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    _run(sqlite_engine, "downgrade")
    inspector = sa.inspect(sqlite_engine)
    for table in _TABLES:
        cols = {c["name"] for c in inspector.get_columns(table)}
        assert _COLUMN not in cols, f"{table}.{_COLUMN} should be dropped"
    # A second downgrade is a guarded no-op (drop on absent columns).
    _run(sqlite_engine, "downgrade")


def test_chain_metadata() -> None:
    assert _MODULE.revision == "0045"
    assert _MODULE.down_revision == "0044"
