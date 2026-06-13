"""Migration tests for 0036_agent_tool_calls (Johnny-etu.4).

An additive table for per-tool-call traces: clean-DB creation with its two
indexes + the boolean server-defaults, an insert that exercises the defaults,
idempotent re-upgrade, and a clean (idempotent) downgrade.
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
    / "0036_agent_tool_calls.py"
)


def _load_migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("_migration_0036", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load_migration_module()


@pytest.fixture
def engine(tmp_path: Path) -> sa.Engine:
    return sa.create_engine(f"sqlite:///{tmp_path}/migration_0036.db")


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


def _index_names(engine: sa.Engine) -> set[str]:
    return {ix["name"] for ix in sa.inspect(engine).get_indexes("agent_tool_calls")}


def test_upgrade_creates_table_with_indexes_and_defaults(engine: sa.Engine) -> None:
    _upgrade(engine)
    assert "agent_tool_calls" in _table_names(engine)
    assert _index_names(engine) == {
        "ix_agent_tool_calls_session_created",
        "ix_agent_tool_calls_task",
    }

    # A minimal insert: the boolean flags + created_at all carry server defaults.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO agent_tool_calls "
                "(bot_session_id, tool_name, request_json, ok) "
                "VALUES (1, 'sandbox.exec', '{}', 1)"
            )
        )
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT timed_out, truncated, denied, created_at "
                "FROM agent_tool_calls"
            )
        ).one()
    assert row[0] in (0, False)
    assert row[1] in (0, False)
    assert row[2] in (0, False)
    assert row[3] is not None  # created_at default landed


def test_upgrade_is_idempotent(engine: sa.Engine) -> None:
    _upgrade(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO agent_tool_calls "
                "(bot_session_id, tool_name, request_json, ok) "
                "VALUES (1, 'sandbox.exec', '{}', 1)"
            )
        )
    _upgrade(engine)  # second run: no-op, data intact
    with engine.connect() as conn:
        count = conn.execute(
            sa.text("SELECT COUNT(*) FROM agent_tool_calls")
        ).scalar()
    assert count == 1


def test_downgrade_drops_the_table(engine: sa.Engine) -> None:
    _upgrade(engine)
    _downgrade(engine)
    assert "agent_tool_calls" not in _table_names(engine)
    _downgrade(engine)  # idempotent downgrade too
