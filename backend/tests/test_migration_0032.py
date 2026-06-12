"""Migration tests for 0032_workspaces (Johnny-wks.1).

Covers: clean-DB creation with the seeded default workspace, the
single-default partial unique index, the unique name/slug constraints, the
``agents.workspace_id`` FK addition (NULL = default workspace, so existing
rows need no backfill), idempotent re-upgrade (no duplicate default, data
intact), and a clean downgrade.

The agents table is a prerequisite, so the fixture replays 0027's create
first (via the ORM metadata — the column shapes the FK needs are identical).
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
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0032_workspaces.py"
)


def _load_migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("_migration_0032", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load_migration_module()


@pytest.fixture
def engine(tmp_path: Path) -> sa.Engine:
    eng = sa.create_engine(f"sqlite:///{tmp_path}/migration_0032.db")
    # Pre-0032 schema slice: the agents table (without workspace_id) the
    # migration alters. Minimal columns — the batch alter recreates the
    # table from the live shape, not from the ORM.
    with eng.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE agents ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name VARCHAR(128) NOT NULL, "
                "mode VARCHAR(32) NOT NULL DEFAULT 'listen_only', "
                "is_default BOOLEAN NOT NULL DEFAULT 0)"
            )
        )
        conn.execute(sa.text("INSERT INTO agents (name, is_default) VALUES ('Johnny', 1)"))
    return eng


def _upgrade(engine: sa.Engine) -> None:
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx), conn.begin():
            _MODULE.upgrade()


def _downgrade(engine: sa.Engine) -> None:
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx), conn.begin():
            _MODULE.downgrade()


def _table_names(engine: sa.Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def _agent_columns(engine: sa.Engine) -> set[str]:
    return {col["name"] for col in sa.inspect(engine).get_columns("agents")}


def test_upgrade_creates_table_seed_and_agent_fk(engine: sa.Engine) -> None:
    _upgrade(engine)
    assert "workspaces" in _table_names(engine)
    assert "workspace_id" in _agent_columns(engine)

    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT name, slug, is_default FROM workspaces")
        ).fetchall()
    assert len(rows) == 1
    name, slug, is_default = rows[0]
    assert name == _MODULE.DEFAULT_WORKSPACE_NAME
    assert slug == _MODULE.DEFAULT_WORKSPACE_SLUG
    assert is_default in (1, True)

    # Existing agents needed NO backfill: NULL workspace_id = the default
    # workspace by convention (byte-identical behavior).
    with engine.connect() as conn:
        ws = conn.execute(
            sa.text("SELECT workspace_id FROM agents WHERE name='Johnny'")
        ).scalar()
    assert ws is None


def _insert_workspace(
    conn: sa.Connection, *, name: str, slug: str, is_default: int = 0
) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO workspaces (name, slug, is_default) "
            "VALUES (:name, :slug, :is_default)"
        ),
        {"name": name, "slug": slug, "is_default": is_default},
    )


def test_constraints_unique_name_slug_single_default(engine: sa.Engine) -> None:
    _upgrade(engine)
    with engine.begin() as conn:
        _insert_workspace(conn, name="Finance", slug="finance")
    # Unique name.
    with engine.begin() as conn, pytest.raises(sa.exc.IntegrityError):
        _insert_workspace(conn, name="Finance", slug="finance-2")
    # Unique slug.
    with engine.begin() as conn, pytest.raises(sa.exc.IntegrityError):
        _insert_workspace(conn, name="Finance 2", slug="finance")
    # Single default (partial unique index).
    with engine.begin() as conn, pytest.raises(sa.exc.IntegrityError):
        _insert_workspace(conn, name="Another", slug="another", is_default=1)


def test_upgrade_is_idempotent(engine: sa.Engine) -> None:
    _upgrade(engine)
    with engine.begin() as conn:
        _insert_workspace(conn, name="Finance", slug="finance")
        conn.execute(
            sa.text(
                "UPDATE agents SET workspace_id = "
                "(SELECT id FROM workspaces WHERE slug='finance')"
            )
        )
    _upgrade(engine)  # second run: no duplicate default, attachment intact
    with engine.connect() as conn:
        count = conn.execute(sa.text("SELECT COUNT(*) FROM workspaces")).scalar()
        defaults = conn.execute(
            sa.text("SELECT COUNT(*) FROM workspaces WHERE is_default")
        ).scalar()
        ws = conn.execute(sa.text("SELECT workspace_id FROM agents")).scalar()
    assert count == 2
    assert defaults == 1
    assert ws is not None


def test_downgrade_drops_column_and_table(engine: sa.Engine) -> None:
    _upgrade(engine)
    _downgrade(engine)
    assert "workspaces" not in _table_names(engine)
    assert "workspace_id" not in _agent_columns(engine)
    _downgrade(engine)  # idempotent downgrade too
