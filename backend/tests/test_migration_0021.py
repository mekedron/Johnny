"""Migration tests for 0021_bot_session_waiting_for_relogin (Johnny-ebf).

The CHECK-constraint swap is a PostgreSQL-only step (SQLite can't
``ALTER DROP CONSTRAINT`` — the migration guards on the dialect). What is
worth exercising on SQLite is the *portable* downgrade data migration: any
``waiting_for_relogin`` row must be rewritten to ``failed`` before the
constraint is narrowed, or the recreate would reject it. The seed table is
created without the CHECK constraint so both directions run end-to-end on
SQLite.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_MIGRATION_FILE = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0021_bot_session_waiting_for_relogin.py"
)


def _load_migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("_migration_0021", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load_migration_module()


def _seed_schema(engine: sa.Engine) -> None:
    """Minimal pre-migration ``bot_sessions`` table (no CHECK constraint)."""
    md = sa.MetaData()
    sa.Table(
        "bot_sessions",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_reason", sa.Text),
    )
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO bot_sessions (id, status, error_reason) VALUES "
                "(1, 'waiting_for_relogin', 'account signed out'),"
                "(2, 'joined', NULL),"
                "(3, 'waiting_for_relogin', 'still signed out'),"
                "(4, 'failed', 'other reason')"
            )
        )


def _run(engine: sa.Engine, direction: str) -> None:
    func = getattr(_MODULE, direction)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            func()


def _statuses(engine: sa.Engine) -> dict[int, str]:
    with engine.begin() as conn:
        return {
            r[0]: r[1]
            for r in conn.execute(
                sa.text("SELECT id, status FROM bot_sessions")
            ).fetchall()
        }


def test_metadata_revision_chain() -> None:
    assert _MODULE.revision == "0021"
    assert _MODULE.down_revision == "0020"
    assert "waiting_for_relogin" in _MODULE.NEW_STATUSES
    assert "waiting_for_relogin" not in _MODULE.OLD_STATUSES


def test_upgrade_is_data_noop_on_sqlite(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}", future=True)
    _seed_schema(engine)
    _run(engine, "upgrade")
    # Upgrade only widens the allowed set — no rows change.
    assert _statuses(engine) == {
        1: "waiting_for_relogin",
        2: "joined",
        3: "waiting_for_relogin",
        4: "failed",
    }


def test_downgrade_rewrites_waiting_rows_to_failed(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}", future=True)
    _seed_schema(engine)
    _run(engine, "downgrade")
    statuses = _statuses(engine)
    # Both waiting rows settle to failed; the others are untouched.
    assert statuses[1] == "failed"
    assert statuses[3] == "failed"
    assert statuses[2] == "joined"
    assert statuses[4] == "failed"
    # The originally-failed row keeps its own reason.
    with engine.begin() as conn:
        reason4 = conn.execute(
            sa.text("SELECT error_reason FROM bot_sessions WHERE id = 4")
        ).scalar()
    assert reason4 == "other reason"


def test_upgrade_then_downgrade_runs_clean(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}", future=True)
    _seed_schema(engine)
    _run(engine, "upgrade")
    _run(engine, "downgrade")
    statuses = _statuses(engine)
    assert "waiting_for_relogin" not in statuses.values()
