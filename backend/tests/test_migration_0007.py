"""Idempotency tests for migration 0007_bot_session_browser_source.

The migration was reworked under Johnny-ckz.9 so each step is guarded
by an inspector check — re-running ``upgrade()`` or ``downgrade()``
against a half-applied state must not raise. We exercise that here by
running each direction twice in a row.

SQLite is enough: the migration uses ``JSONB().with_variant(JSON(),
"sqlite")`` for ``playground_overrides`` and a portable ``String(16)``
for ``source``, so the dialect difference does not affect the shape
the migration produces.
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
    / "0007_bot_session_browser_source.py"
)


def _load_migration_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_migration_0007", _MIGRATION_FILE
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_0006_bot_sessions_table(engine: sa.Engine) -> None:
    """Recreate the pre-0007 bot_sessions table on SQLite."""
    metadata = sa.MetaData()
    sa.Table(
        "bot_sessions",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("meeting_config_id", sa.Integer, nullable=False),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="scheduled",
        ),
        sa.Column("container_name", sa.String(255), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "ended_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("logs", sa.Text, nullable=True),
        sa.Column("error_reason", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    metadata.create_all(engine)


def _column_names(engine: sa.Engine) -> set[str]:
    return {col["name"] for col in sa.inspect(engine).get_columns("bot_sessions")}


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> sa.Engine:
    url = f"sqlite:///{tmp_path / 'mig.db'}"
    engine = sa.create_engine(url, future=True)
    _seed_0006_bot_sessions_table(engine)
    return engine


def _run_migration_direction(engine: sa.Engine, direction: str) -> None:
    migration = _load_migration_module()
    func = getattr(migration, direction)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            func()


def test_upgrade_adds_both_columns(sqlite_engine: sa.Engine) -> None:
    assert "source" not in _column_names(sqlite_engine)
    assert "playground_overrides" not in _column_names(sqlite_engine)
    _run_migration_direction(sqlite_engine, "upgrade")
    cols = _column_names(sqlite_engine)
    assert {"source", "playground_overrides"}.issubset(cols)


def test_upgrade_is_idempotent_against_half_applied(
    sqlite_engine: sa.Engine,
) -> None:
    """Drop alembic_version semantics; call upgrade twice — must not crash."""
    _run_migration_direction(sqlite_engine, "upgrade")
    # Second pass against the already-upgraded shape — the inspector
    # checks inside upgrade() should turn every op into a no-op.
    _run_migration_direction(sqlite_engine, "upgrade")
    cols = _column_names(sqlite_engine)
    assert {"source", "playground_overrides"}.issubset(cols)


def test_upgrade_recovers_from_partial_state(sqlite_engine: sa.Engine) -> None:
    """Half-applied state: only 'source' got added before a previous crash."""
    with sqlite_engine.begin() as conn:
        conn.execute(
            sa.text(
                "ALTER TABLE bot_sessions ADD COLUMN source VARCHAR(16) "
                "NOT NULL DEFAULT 'meet'"
            )
        )
    cols = _column_names(sqlite_engine)
    assert "source" in cols
    assert "playground_overrides" not in cols

    _run_migration_direction(sqlite_engine, "upgrade")
    cols = _column_names(sqlite_engine)
    assert {"source", "playground_overrides"}.issubset(cols)


def test_downgrade_removes_both_columns(sqlite_engine: sa.Engine) -> None:
    _run_migration_direction(sqlite_engine, "upgrade")
    _run_migration_direction(sqlite_engine, "downgrade")
    cols = _column_names(sqlite_engine)
    assert "source" not in cols
    assert "playground_overrides" not in cols


def test_downgrade_is_idempotent(sqlite_engine: sa.Engine) -> None:
    _run_migration_direction(sqlite_engine, "upgrade")
    _run_migration_direction(sqlite_engine, "downgrade")
    # Second downgrade against the already-stripped shape must not crash.
    _run_migration_direction(sqlite_engine, "downgrade")
    cols = _column_names(sqlite_engine)
    assert "source" not in cols
    assert "playground_overrides" not in cols
