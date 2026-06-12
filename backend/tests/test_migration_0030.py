"""Migration tests for 0030_capability_policies (Johnny-trt.38).

Exercises both halves on SQLite end-to-end (the conversation_events CHECK
swap goes through the batch table-recreate with an explicit ``copy_from``,
so the full path runs here; production Postgres takes the plain
drop/create-constraint branch of the same operation):

* clean-DB shape: ``capability_policies`` created with the scope/target
  CHECKs + the four partial unique indexes; ``policy_denied`` accepted by
  the amended ``conversation_events`` CHECK;
* existing conversation_events rows survive the CHECK swap byte-for-byte;
* one-row-per-target enforcement (the global partial unique index);
* idempotency of ``upgrade`` against an already-migrated DB;
* ``downgrade``: the table is gone, ``policy_denied`` rows are removed, and
  the narrowed CHECK refuses new ones.
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
    / "0030_capability_policies.py"
)


def _load_migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("_migration_0030", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load_migration_module()


@pytest.fixture
def engine(tmp_path: Path) -> sa.Engine:
    # File-backed (not :memory:) so batch_alter_table's table-recreate works
    # across the multiple connections alembic ops may open.
    return sa.create_engine(f"sqlite:///{tmp_path}/migration_0030.db")


def _seed_pre_schema(engine: sa.Engine) -> None:
    """The post-0029 shape of every table 0030 touches or references."""
    md = sa.MetaData()
    sa.Table(
        "agents",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
    )
    sa.Table(
        "bot_sessions",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
    )
    sa.Table(
        "conversation_events",
        md,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("bot_session_id", sa.Integer, nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("timestamp_ms", sa.Integer, nullable=False),
        sa.Column("turn_id", sa.Integer, nullable=True),
        sa.Column("agent_name", sa.String(128), nullable=True),
        sa.Column("counterpart_name", sa.String(128), nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("reason", sa.String(255), nullable=False, server_default=""),
        sa.Column("details", sa.JSON, nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["bot_session_id"],
            ["bot_sessions.id"],
            ondelete="CASCADE",
            name="fk_conversation_events_bot_session_id",
        ),
        sa.CheckConstraint(
            "event_type IN ("
            + ", ".join(f"'{v}'" for v in _MODULE.CONVERSATION_EVENT_TYPES_BEFORE)
            + ")",
            name="ck_conversation_events_event_type",
        ),
        sa.Index("ix_conversation_events_session_ts", "bot_session_id", "timestamp_ms"),
    )
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO agents (id, name) VALUES (1, 'Progress Bot')")
        )
        conn.execute(
            sa.text(
                "INSERT INTO bot_sessions (id, source, status) "
                "VALUES (7, 'browser', 'ended')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO conversation_events "
                "(bot_session_id, event_type, timestamp_ms, reason, details) "
                "VALUES (7, 'floor_acquired', 1200, '', '{}')"
            )
        )


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


def _index_names(engine: sa.Engine, table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(engine).get_indexes(table)}


def test_upgrade_creates_capability_policies_with_unique_targets(
    engine: sa.Engine,
) -> None:
    _seed_pre_schema(engine)
    _upgrade(engine)

    assert "capability_policies" in _table_names(engine)
    assert {
        "uq_capability_policies_global",
        "uq_capability_policies_agent",
        "uq_capability_policies_session_mode",
        "uq_capability_policies_session",
    } <= _index_names(engine, "capability_policies")

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO capability_policies (scope, document) "
                "VALUES ('global', '{}')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO capability_policies (scope, agent_id, document) "
                "VALUES ('agent', 1, '{}')"
            )
        )
    # The single-global-row partial unique index holds.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO capability_policies (scope, document) "
                    "VALUES ('global', '{}')"
                )
            )
    # The scope CHECK refuses unknown scopes.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO capability_policies (scope, document) "
                    "VALUES ('galaxy', '{}')"
                )
            )
    # The target-shape CHECK refuses a global row carrying an agent key.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO capability_policies (scope, agent_id, document) "
                    "VALUES ('session_mode', 1, '{}')"
                )
            )


def test_upgrade_amends_the_event_type_check_and_keeps_rows(
    engine: sa.Engine,
) -> None:
    _seed_pre_schema(engine)
    # Before: policy_denied violates the 0029 CHECK.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO conversation_events "
                    "(bot_session_id, event_type, timestamp_ms, reason, details) "
                    "VALUES (7, 'policy_denied', 1, 'global', '{}')"
                )
            )
    _upgrade(engine)

    with engine.begin() as conn:
        # The pre-existing row survived the batch recreate.
        rows = conn.execute(
            sa.text("SELECT event_type, timestamp_ms FROM conversation_events")
        ).all()
        assert rows == [("floor_acquired", 1200)]
        # After: policy_denied is legal.
        conn.execute(
            sa.text(
                "INSERT INTO conversation_events "
                "(bot_session_id, event_type, timestamp_ms, reason, details) "
                "VALUES (7, 'policy_denied', 2400, 'agent', '{}')"
            )
        )
        # Unknown types still refused (the CHECK survived, amended).
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO conversation_events "
                    "(bot_session_id, event_type, timestamp_ms, reason, details) "
                    "VALUES (7, 'made_up', 1, '', '{}')"
                )
            )
    assert "ix_conversation_events_session_ts" in _index_names(
        engine, "conversation_events"
    )


def test_upgrade_is_idempotent(engine: sa.Engine) -> None:
    _seed_pre_schema(engine)
    _upgrade(engine)
    _upgrade(engine)  # second run must be a no-op for the table create
    assert "capability_policies" in _table_names(engine)


def test_downgrade_restores_the_pre_0030_shape(engine: sa.Engine) -> None:
    _seed_pre_schema(engine)
    _upgrade(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversation_events "
                "(bot_session_id, event_type, timestamp_ms, reason, details) "
                "VALUES (7, 'policy_denied', 2400, 'agent', '{}')"
            )
        )
    _downgrade(engine)

    assert "capability_policies" not in _table_names(engine)
    with engine.begin() as conn:
        rows = conn.execute(
            sa.text("SELECT event_type FROM conversation_events")
        ).scalars().all()
        assert rows == ["floor_acquired"]  # the policy_denied row was removed
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO conversation_events "
                    "(bot_session_id, event_type, timestamp_ms, reason, details) "
                    "VALUES (7, 'policy_denied', 1, '', '{}')"
                )
            )
