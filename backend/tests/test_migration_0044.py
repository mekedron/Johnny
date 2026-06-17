"""Migration tests for 0044_workstream_source_kind_external_callback (US-303).

Verifies the ``ck_agent_workstreams_source_kind`` CHECK is widened to accept
``external_callback`` (the SQLite path goes through the batch table-recreate
with an explicit ``copy_from``; production Postgres takes the plain
drop/create-constraint branch of the same operation), that the head value list
mirrors the model enum (the live no-drift contract that 0041 can no longer
enforce now that the enum grew), that pre-existing rows survive the swap, and
that ``downgrade`` narrows the CHECK back.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_M41 = _load("_migration_0041", _VERSIONS / "0041_agent_workstreams.py")
_M44 = _load(
    "_migration_0044",
    _VERSIONS / "0044_workstream_source_kind_external_callback.py",
)


def _seed_schema(engine: sa.Engine) -> None:
    """Pre-0041 parents the agent_workstreams FKs reference."""
    md = sa.MetaData()
    for table in (
        "bot_sessions",
        "agents",
        "workspaces",
        "agent_decisions",
        "agent_tasks",
        "agent_utterances",
    ):
        sa.Table(table, md, sa.Column("id", sa.Integer, primary_key=True))
    md.create_all(engine)


@pytest.fixture
def engine(tmp_path: Path) -> sa.Engine:
    # File-backed (not :memory:) so batch_alter_table's table-recreate works
    # across the multiple connections alembic ops may open (the 0030 pattern).
    eng = sa.create_engine(f"sqlite:///{tmp_path}/migration_0044.db")
    _seed_schema(eng)
    return eng


def _run(engine: sa.Engine, module: Any, direction: str) -> None:
    func = getattr(module, direction)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            func()


def _insert(conn: sa.Connection, source_kind: str) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO agent_workstreams (bot_session_id, source_kind) "
            "VALUES (1, :sk)"
        ),
        {"sk": source_kind},
    )


def test_head_check_values_match_model_enum() -> None:
    # 0044 is the head migration touching this CHECK; its value list IS the live
    # no-drift contract (0041 froze the initial subset).
    from app.db.models import WorkstreamSourceKind

    assert set(_M44.NEW_SOURCE_KINDS) == {m.value for m in WorkstreamSourceKind}
    # 0044 widens exactly what 0041 created — no gap, no overlap drift.
    assert set(_M44.OLD_SOURCE_KINDS) == set(_M41.WORKSTREAM_SOURCE_KINDS)
    assert "external_callback" in _M44.NEW_SOURCE_KINDS


def test_external_callback_rejected_before_then_accepted_after(
    engine: sa.Engine,
) -> None:
    _run(engine, _M41, "upgrade")
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO bot_sessions (id) VALUES (1)"))
        # Pre-0044 the CHECK only allows delegate|foreground_tool_loop.
        with pytest.raises(sa.exc.IntegrityError):
            _insert(conn, "external_callback")

    _run(engine, _M44, "upgrade")
    with engine.begin() as conn:
        _insert(conn, "external_callback")  # now accepted
        with pytest.raises(sa.exc.IntegrityError):
            _insert(conn, "totally_bogus")  # junk still rejected
        count = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM agent_workstreams "
                "WHERE source_kind = 'external_callback'"
            )
        ).scalar_one()
    assert count == 1


def test_existing_rows_survive_the_swap(engine: sa.Engine) -> None:
    _run(engine, _M41, "upgrade")
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO bot_sessions (id) VALUES (1)"))
        _insert(conn, "delegate")
        _insert(conn, "foreground_tool_loop")
    _run(engine, _M44, "upgrade")
    with engine.begin() as conn:
        total = conn.execute(
            sa.text("SELECT COUNT(*) FROM agent_workstreams")
        ).scalar_one()
    assert total == 2


def test_downgrade_narrows_the_check_back(engine: sa.Engine) -> None:
    _run(engine, _M41, "upgrade")
    _run(engine, _M44, "upgrade")
    _run(engine, _M44, "downgrade")
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO bot_sessions (id) VALUES (1)"))
        with pytest.raises(sa.exc.IntegrityError):
            _insert(conn, "external_callback")
        _insert(conn, "delegate")  # the original set still works


def test_upgrade_preserves_indexes_and_unique(engine: sa.Engine) -> None:
    _run(engine, _M41, "upgrade")
    _run(engine, _M44, "upgrade")
    inspector = sa.inspect(engine)
    index_names = {ix["name"] for ix in inspector.get_indexes("agent_workstreams")}
    assert "ix_agent_workstreams_session_created" in index_names
    assert "ix_agent_workstreams_agent_task_id" in index_names
    # UNIQUE(agent_task_id) still enforced after the recreate.
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO bot_sessions (id) VALUES (1)"))
        conn.execute(sa.text("INSERT INTO agent_tasks (id) VALUES (7)"))
        conn.execute(
            sa.text(
                "INSERT INTO agent_workstreams (bot_session_id, source_kind, "
                "agent_task_id) VALUES (1, 'external_callback', 7)"
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO agent_workstreams (bot_session_id, source_kind, "
                    "agent_task_id) VALUES (1, 'delegate', 7)"
                )
            )
