"""Migration tests for 0019_turn_terminal_state (INV-1, Johnny-ckz.28.3).

Verifies the backfill that lets pre-invariant history satisfy
"every transcribed turn has a terminal state": ``terminal_state`` mapped
from the existing ``outcome`` (spoken → replied, pending → pending_approval,
everything else → no_reply) and a synthesised ``legacy`` ``no_reply_reason``
on the rows that became no_reply (so the parity guard's "no_reply needs a
reason" rule holds on historical rows).

SQLite is enough — the migration uses only ``add_column`` and plain UPDATEs.
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
    / "0019_turn_terminal_state.py"
)


def _load_migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("_migration_0019", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load_migration_module()
LEGACY_REASON = _MODULE.LEGACY_NO_REPLY_REASON


def _seed_schema(engine: sa.Engine) -> None:
    """Pre-0019 agent_decisions: has ``outcome`` but none of the new columns."""
    md = sa.MetaData()
    sa.Table(
        "agent_decisions",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("outcome", sa.String(32), nullable=False),
    )
    md.create_all(engine)


def _insert(engine: sa.Engine, sql: str) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text(sql))


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> sa.Engine:
    url = f"sqlite:///{tmp_path / 'mig19.db'}"
    engine = sa.create_engine(url, future=True)
    _seed_schema(engine)
    # One row per outcome so the full mapping is exercised.
    _insert(
        engine,
        "INSERT INTO agent_decisions (id, outcome) VALUES "
        "(1, 'spoken'),"
        "(2, 'pending'),"
        "(3, 'suppressed'),"
        "(4, 'rejected'),"
        "(5, 'suggested')",
    )
    return engine


def _run(engine: sa.Engine, direction: str) -> None:
    func = getattr(_MODULE, direction)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            func()


def _rows(engine: sa.Engine, sql: str) -> list[Any]:
    with engine.begin() as conn:
        return list(conn.execute(sa.text(sql)).fetchall())


def test_upgrade_maps_terminal_state_from_outcome(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    rows = {
        r[0]: r[1]
        for r in _rows(
            sqlite_engine, "SELECT id, terminal_state FROM agent_decisions"
        )
    }
    assert rows[1] == "replied"  # spoken
    assert rows[2] == "pending_approval"  # pending
    assert rows[3] == "no_reply"  # suppressed
    assert rows[4] == "no_reply"  # rejected
    assert rows[5] == "no_reply"  # suggested


def test_upgrade_stamps_legacy_reason_only_on_no_reply(
    sqlite_engine: sa.Engine,
) -> None:
    _run(sqlite_engine, "upgrade")
    rows = {
        r[0]: r[1]
        for r in _rows(
            sqlite_engine, "SELECT id, no_reply_reason FROM agent_decisions"
        )
    }
    # replied / pending_approval never carry a no_reply_reason.
    assert rows[1] is None
    assert rows[2] is None
    # Every backfilled no_reply row names a reason so the guard is satisfiable.
    assert rows[3] == LEGACY_REASON
    assert rows[4] == LEGACY_REASON
    assert rows[5] == LEGACY_REASON


def test_upgrade_leaves_turn_id_null(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    rows = _rows(sqlite_engine, "SELECT turn_id FROM agent_decisions")
    assert all(r[0] is None for r in rows)


def test_upgrade_then_downgrade_drops_columns(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    cols_up = {
        c["name"] for c in sa.inspect(sqlite_engine).get_columns("agent_decisions")
    }
    assert {"turn_id", "terminal_state", "no_reply_reason"} <= cols_up

    _run(sqlite_engine, "downgrade")
    cols_down = {
        c["name"] for c in sa.inspect(sqlite_engine).get_columns("agent_decisions")
    }
    for col in ("turn_id", "terminal_state", "no_reply_reason"):
        assert col not in cols_down
