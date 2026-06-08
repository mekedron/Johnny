"""Migration tests for 0018_decision_utterance_parity (Johnny-ckz.28.2).

Verifies the backfill that lets pre-parity history render under the new
canonical-record guard: ``decision_recommended_text`` copied from
``suggested_reply``, ``final_text`` pulled from the most recent linked
utterance, and a synthesised ``legacy`` override on rows where the two differ.

SQLite is enough here — the migration uses only ``add_column`` and
correlated-subquery UPDATEs, both portable to the in-process harness. The seed
tables are created with just the columns the migration reads/writes.
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
    / "0018_decision_utterance_parity.py"
)


def _load_migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("_migration_0018", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load_migration_module()
LEGACY_REASON = _MODULE.LEGACY_DIVERGENCE_REASON
LEGACY_ACTOR = _MODULE.LEGACY_OVERRIDE_ACTOR


def _seed_schema(engine: sa.Engine) -> None:
    """Pre-0018 tables: agent_decisions WITHOUT the four new columns."""
    md = sa.MetaData()
    sa.Table(
        "agent_decisions",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("suggested_reply", sa.Text),
    )
    sa.Table(
        "agent_utterances",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("agent_decision_id", sa.Integer),
        sa.Column("output_text", sa.Text, nullable=False),
    )
    md.create_all(engine)


def _insert(engine: sa.Engine, sql: str) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text(sql))


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> sa.Engine:
    url = f"sqlite:///{tmp_path / 'mig18.db'}"
    engine = sa.create_engine(url, future=True)
    _seed_schema(engine)
    # D1 diverges (recommended ≠ spoken); D2 matches; D3 has no utterance;
    # D4 has a NULL recommendation.
    _insert(
        engine,
        "INSERT INTO agent_decisions (id, suggested_reply) VALUES "
        "(1, 'recommended A'),"
        "(2, 'same text'),"
        "(3, 'no utterance here'),"
        "(4, NULL)",
    )
    # D1 gets two utterances — the latest (highest id) is the spoken text.
    _insert(
        engine,
        "INSERT INTO agent_utterances (id, agent_decision_id, output_text) VALUES "
        "(10, 1, 'an earlier draft'),"
        "(11, 1, 'spoken B'),"
        "(12, 2, 'same text'),"
        "(13, 4, 'spoke despite null rec')",
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


def test_upgrade_snapshots_recommended_text(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    rows = {
        r[0]: r[1]
        for r in _rows(
            sqlite_engine,
            "SELECT id, decision_recommended_text FROM agent_decisions",
        )
    }
    assert rows[1] == "recommended A"
    assert rows[2] == "same text"
    assert rows[3] == "no utterance here"
    assert rows[4] is None  # NULL suggested_reply stays NULL


def test_upgrade_pulls_latest_utterance_into_final_text(
    sqlite_engine: sa.Engine,
) -> None:
    _run(sqlite_engine, "upgrade")
    rows = {
        r[0]: r[1]
        for r in _rows(sqlite_engine, "SELECT id, final_text FROM agent_decisions")
    }
    assert rows[1] == "spoken B"  # latest utterance, not the earlier draft
    assert rows[2] == "same text"
    assert rows[3] is None  # no utterance → NULL
    assert rows[4] == "spoke despite null rec"


def test_upgrade_flags_legacy_divergence(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    rows = {
        r[0]: (r[1], r[2])
        for r in _rows(
            sqlite_engine,
            "SELECT id, divergence_reason, override_actor FROM agent_decisions",
        )
    }
    # D1: recommended ≠ spoken → legacy override recorded.
    assert rows[1] == (LEGACY_REASON, LEGACY_ACTOR)
    # D2: recommended == spoken → no override.
    assert rows[2] == (None, None)
    # D3: no spoken text → nothing to reconcile.
    assert rows[3] == (None, None)
    # D4: NULL recommendation → not a divergence even though it spoke.
    assert rows[4] == (None, None)


def test_upgrade_then_downgrade_drops_columns(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    cols_after_up = {
        c["name"]
        for c in sa.inspect(sqlite_engine).get_columns("agent_decisions")
    }
    assert {"decision_recommended_text", "final_text"} <= cols_after_up

    _run(sqlite_engine, "downgrade")
    cols_after_down = {
        c["name"]
        for c in sa.inspect(sqlite_engine).get_columns("agent_decisions")
    }
    for col in (
        "decision_recommended_text",
        "final_text",
        "divergence_reason",
        "override_actor",
    ):
        assert col not in cols_after_down
