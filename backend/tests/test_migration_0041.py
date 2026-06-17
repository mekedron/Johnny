"""Migration tests for 0041_agent_workstreams (Johnny-d6w.2, US-002).

Verifies both tables are created with the full column set, the enum CHECK
constraints (limited to the EMITTED states) mirror the model enums so they
can't drift, the status/delivery defaults, the ``UNIQUE(agent_task_id)`` and
``UNIQUE(workstream_id, sequence)`` constraints, and that the migration is
idempotent and reversible.

SQLite is enough — the migration uses ``create_table`` / ``create_index`` with
the portable JSON variant (the 0023 pattern).
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
    / "0041_agent_workstreams.py"
)

EXPECTED_WORKSTREAM_COLUMNS = {
    "id",
    "bot_session_id",
    "agent_id",
    "workspace_id",
    "source_kind",
    "source_turn_id",
    "source_decision_id",
    "agent_task_id",
    "request_id",
    "title",
    "user_request_text",
    "status",
    "delivery_status",
    "started_at",
    "completed_at",
    "delivered_at",
    "result_available_at",
    "result_expires_at",
    "expired_reason",
    "delivered_utterance_id",
    "result_text",
    "result_json",
    "error",
    "created_at",
    "updated_at",
}

EXPECTED_EVENT_COLUMNS = {
    "id",
    "workstream_id",
    "bot_session_id",
    "sequence",
    "event_type",
    "text",
    "payload_json",
    "created_at",
}


def _load_migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("_migration_0041", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load_migration_module()


def _seed_schema(engine: sa.Engine) -> None:
    """Pre-0041 parents the FKs reference (agent_workstreams/_events don't exist)."""
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
def sqlite_engine(tmp_path: Path) -> sa.Engine:
    url = f"sqlite:///{tmp_path / 'mig41.db'}"
    engine = sa.create_engine(url, future=True)
    _seed_schema(engine)
    return engine


def _run(engine: sa.Engine, direction: str) -> None:
    func = getattr(_MODULE, direction)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            func()


def test_upgrade_creates_both_tables_with_expected_columns(
    sqlite_engine: sa.Engine,
) -> None:
    _run(sqlite_engine, "upgrade")
    inspector = sa.inspect(sqlite_engine)
    names = inspector.get_table_names()
    assert "agent_workstreams" in names
    assert "agent_workstream_events" in names
    ws_cols = {c["name"] for c in inspector.get_columns("agent_workstreams")}
    ev_cols = {c["name"] for c in inspector.get_columns("agent_workstream_events")}
    assert ws_cols == EXPECTED_WORKSTREAM_COLUMNS
    assert ev_cols == EXPECTED_EVENT_COLUMNS


def test_upgrade_creates_workstream_indexes(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    inspector = sa.inspect(sqlite_engine)
    index_names = {ix["name"] for ix in inspector.get_indexes("agent_workstreams")}
    assert "ix_agent_workstreams_session_created" in index_names
    assert "ix_agent_workstreams_agent_task_id" in index_names


def test_row_defaults(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    with sqlite_engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO bot_sessions (id) VALUES (1)"))
        conn.execute(
            sa.text(
                "INSERT INTO agent_workstreams (bot_session_id, source_kind) "
                "VALUES (1, 'delegate')"
            )
        )
        row = conn.execute(
            sa.text(
                "SELECT status, delivery_status, created_at, updated_at "
                "FROM agent_workstreams"
            )
        ).one()
    assert row[0] == "queued"  # status server default
    assert row[1] == "not_ready"  # delivery_status server default
    assert row[2] is not None
    assert row[3] is not None


def test_check_constraints_match_model_enums(sqlite_engine: sa.Engine) -> None:
    # The migration's value lists are the contract the CHECKs enforce; assert
    # they mirror the (emitted-only) model enums so the two can't drift.
    from app.db.models import (
        WorkstreamDeliveryStatus,
        WorkstreamSourceKind,
        WorkstreamStatus,
    )

    assert set(_MODULE.WORKSTREAM_STATUSES) == {m.value for m in WorkstreamStatus}
    assert set(_MODULE.WORKSTREAM_DELIVERY_STATUSES) == {
        m.value for m in WorkstreamDeliveryStatus
    }
    # 0041 is frozen at the INITIAL emitted source-kind set; ``external_callback``
    # was promoted to emitted by migration 0044 (US-303), which widens this CHECK.
    # So 0041's tuple is now a SUBSET of the enum; ``test_migration_0044`` asserts
    # the head CHECK equals the full enum (the live no-drift contract).
    assert set(_MODULE.WORKSTREAM_SOURCE_KINDS) <= {
        m.value for m in WorkstreamSourceKind
    }

    _run(sqlite_engine, "upgrade")
    with sqlite_engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO bot_sessions (id) VALUES (1)"))
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO agent_workstreams (bot_session_id, source_kind, status) "
                    "VALUES (1, 'delegate', 'exploded')"
                )
            )


def test_unique_agent_task_id(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    with sqlite_engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO bot_sessions (id) VALUES (1)"))
        conn.execute(sa.text("INSERT INTO agent_tasks (id) VALUES (5)"))
        conn.execute(
            sa.text(
                "INSERT INTO agent_workstreams (bot_session_id, source_kind, agent_task_id) "
                "VALUES (1, 'delegate', 5)"
            )
        )
        # A second envelope for the same task is rejected (one workstream/task).
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO agent_workstreams (bot_session_id, source_kind, agent_task_id) "
                    "VALUES (1, 'delegate', 5)"
                )
            )


def test_multiple_inline_workstreams_allowed(sqlite_engine: sa.Engine) -> None:
    # Inline workstreams carry NULL agent_task_id; UNIQUE must permit many NULLs.
    _run(sqlite_engine, "upgrade")
    with sqlite_engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO bot_sessions (id) VALUES (1)"))
        for _ in range(3):
            conn.execute(
                sa.text(
                    "INSERT INTO agent_workstreams (bot_session_id, source_kind) "
                    "VALUES (1, 'foreground_tool_loop')"
                )
            )
        count = conn.execute(
            sa.text("SELECT COUNT(*) FROM agent_workstreams")
        ).scalar_one()
    assert count == 3


def test_upgrade_is_idempotent(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    _run(sqlite_engine, "upgrade")  # second run hits the inspector guard, no raise
    inspector = sa.inspect(sqlite_engine)
    assert "agent_workstreams" in inspector.get_table_names()
    assert "agent_workstream_events" in inspector.get_table_names()


def test_upgrade_then_downgrade_drops_both_tables(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    _run(sqlite_engine, "downgrade")
    inspector = sa.inspect(sqlite_engine)
    names = inspector.get_table_names()
    assert "agent_workstreams" not in names
    assert "agent_workstream_events" not in names
    # Downgrade on absent tables is also a guarded no-op.
    _run(sqlite_engine, "downgrade")
