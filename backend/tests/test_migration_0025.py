"""Migration tests for 0025_meeting_bot_dismissal (Johnny-trt.56).

Three plain additive nullable columns with exists-guards, so the interesting
properties are: all three land, idempotent upgrade (re-run is a no-op, not a
duplicate-column crash), pre-existing rows defaulting to NULL (nothing was
dismissed before the feature existed), and a clean reversible downgrade.
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
    / "0025_meeting_bot_dismissal.py"
)

NEW_COLUMNS = {"bot_dismissed_at", "bot_dismissed_by", "bot_dismissed_until"}


def _load_migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("_migration_0025", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load_migration_module()


def _seed_schema(engine: sa.Engine) -> None:
    """Minimal pre-migration ``meeting_configs`` table with one row."""
    md = sa.MetaData()
    sa.Table(
        "meeting_configs",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("calendar_event_id", sa.Integer, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
    )
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO meeting_configs (id, calendar_event_id, enabled) "
                "VALUES (1, 10, 1)"
            )
        )


def _run(engine: sa.Engine, direction: str) -> None:
    func = getattr(_MODULE, direction)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            func()


def _columns(engine: sa.Engine) -> set[str]:
    inspector = sa.inspect(engine)
    return {col["name"] for col in inspector.get_columns("meeting_configs")}


def _check_names(engine: sa.Engine) -> set[str]:
    inspector = sa.inspect(engine)
    return {
        c["name"]
        for c in inspector.get_check_constraints("meeting_configs")
        if c.get("name")
    }


def test_metadata_revision_chain() -> None:
    assert _MODULE.revision == "0025"
    assert _MODULE.down_revision == "0024"


def test_upgrade_adds_all_three_nullable_columns(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}", future=True)
    _seed_schema(engine)
    _run(engine, "upgrade")
    assert NEW_COLUMNS <= _columns(engine)
    assert "ck_meeting_configs_bot_dismissed_by" in _check_names(engine)
    with engine.begin() as conn:
        row = conn.execute(
            sa.text(
                "SELECT bot_dismissed_at, bot_dismissed_by, bot_dismissed_until "
                "FROM meeting_configs WHERE id = 1"
            )
        ).one()
    # The pre-existing row was never dismissed.
    assert tuple(row) == (None, None, None)


def test_check_constraint_accepts_actors_and_null(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}", future=True)
    _seed_schema(engine)
    _run(engine, "upgrade")
    with engine.begin() as conn:
        for actor in ("ui", "voice", "schedule", None):
            conn.execute(
                sa.text(
                    "UPDATE meeting_configs SET bot_dismissed_by = :a WHERE id = 1"
                ),
                {"a": actor},
            )
    with engine.begin() as conn, pytest.raises(sa.exc.IntegrityError):
        conn.execute(
            sa.text(
                "UPDATE meeting_configs SET bot_dismissed_by = 'gremlin' "
                "WHERE id = 1"
            )
        )


def test_upgrade_is_idempotent(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}", future=True)
    _seed_schema(engine)
    _run(engine, "upgrade")
    _run(engine, "upgrade")  # second run must not crash on duplicate columns
    assert NEW_COLUMNS <= _columns(engine)


def test_upgrade_completes_a_half_applied_schema(tmp_path: Path) -> None:
    """One column already present (half-applied) → the rest still land."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}", future=True)
    _seed_schema(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text("ALTER TABLE meeting_configs ADD COLUMN bot_dismissed_at TIMESTAMP")
        )
    _run(engine, "upgrade")
    assert NEW_COLUMNS <= _columns(engine)


def test_downgrade_drops_columns_and_keeps_rows(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}", future=True)
    _seed_schema(engine)
    _run(engine, "upgrade")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE meeting_configs SET bot_dismissed_by = 'ui' WHERE id = 1"
            )
        )
    _run(engine, "downgrade")
    assert not (NEW_COLUMNS & _columns(engine))
    with engine.begin() as conn:
        count = conn.execute(sa.text("SELECT COUNT(*) FROM meeting_configs")).scalar()
    assert count == 1


def test_downgrade_noop_when_columns_absent(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}", future=True)
    _seed_schema(engine)
    _run(engine, "downgrade")  # nothing to drop — must not crash
    assert not (NEW_COLUMNS & _columns(engine))
