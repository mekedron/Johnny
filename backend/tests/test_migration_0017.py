"""Migration tests for 0017_drop_free_auto_speak_mode (Johnny-ckz.25).

Verifies the data conversion: every ``free_auto_speak`` value becomes
``autonomous`` across all four tables that store the mode, and rows whose
own instructions are empty receive the default instruction string so they
satisfy autonomous's non-empty-instructions validation — while rows that
inherit a now-backfilled template are left inheriting (``instructions``
stays ``NULL``).

SQLite is enough for the data conversion: the migration's CHECK-constraint
swap is a PostgreSQL-only step (SQLite can't ``ALTER DROP CONSTRAINT``), and
the in-process app test harness builds its schema from the models — which
already exclude ``free_auto_speak`` — so the portable UPDATE logic is the
part worth exercising here. The seed tables are created without the CHECK
constraints so the upgrade runs end-to-end on SQLite.
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
    / "0017_drop_free_auto_speak_mode.py"
)


def _load_migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("_migration_0017", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load_migration_module()
DEFAULT = _MODULE.DEFAULT_AUTONOMOUS_INSTRUCTIONS


def _seed_schema(engine: sa.Engine) -> None:
    """Create minimal pre-migration tables (no CHECK constraints)."""
    md = sa.MetaData()
    sa.Table(
        "profile_templates",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(128)),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("base_instructions", sa.Text),
    )
    sa.Table(
        "meeting_configs",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("instructions", sa.Text),
        sa.Column("profile_template_id", sa.Integer),
    )
    sa.Table(
        "agent_utterances",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("mode", sa.String(32), nullable=False),
    )
    sa.Table(
        "personalities",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("default_mode", sa.String(32)),
    )
    md.create_all(engine)


def _insert(engine: sa.Engine, sql: str) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text(sql))


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> sa.Engine:
    url = f"sqlite:///{tmp_path / 'mig.db'}"
    engine = sa.create_engine(url, future=True)
    _seed_schema(engine)
    # profile_templates: T1 empty free, T2 non-empty free, T3 limited empty, T4 already autonomous
    _insert(
        engine,
        "INSERT INTO profile_templates (id, name, mode, base_instructions) VALUES "
        "(1, 'T1', 'free_auto_speak', ''),"
        "(2, 'T2', 'free_auto_speak', 'Keep it short.'),"
        "(3, 'T3', 'limited_auto_speak', ''),"
        "(4, 'T4', 'autonomous', 'Already autonomous.')",
    )
    # meeting_configs: M1 inherits backfilled T1, M2 inherits empty T3, M3 own override, M4 limited
    _insert(
        engine,
        "INSERT INTO meeting_configs (id, mode, instructions, profile_template_id) VALUES "
        "(1, 'free_auto_speak', NULL, 1),"
        "(2, 'free_auto_speak', NULL, 3),"
        "(3, 'free_auto_speak', 'Per-meeting brief.', 3),"
        "(4, 'limited_auto_speak', NULL, 3)",
    )
    _insert(
        engine,
        "INSERT INTO agent_utterances (id, mode) VALUES "
        "(1, 'free_auto_speak'),(2, 'autonomous'),(3, 'limited_auto_speak')",
    )
    _insert(
        engine,
        "INSERT INTO personalities (id, default_mode) VALUES "
        "(1, 'free_auto_speak'),(2, NULL),(3, 'listen_only')",
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


def test_upgrade_converts_every_free_auto_speak_row(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    for table, col in (
        ("profile_templates", "mode"),
        ("meeting_configs", "mode"),
        ("agent_utterances", "mode"),
        ("personalities", "default_mode"),
    ):
        remaining = _rows(
            sqlite_engine,
            f"SELECT COUNT(*) FROM {table} WHERE {col} = 'free_auto_speak'",
        )
        assert remaining[0][0] == 0, f"{table}.{col} still has free_auto_speak"


def test_upgrade_backfills_empty_template_instructions(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    rows = dict(
        _rows(sqlite_engine, "SELECT id, mode || '|' || base_instructions FROM profile_templates")
    )
    # T1 (empty free) → autonomous + default text.
    assert rows[1] == f"autonomous|{DEFAULT}"
    # T2 (non-empty free) → autonomous, instructions preserved.
    assert rows[2] == "autonomous|Keep it short."
    # T3 (limited, empty) → untouched.
    assert rows[3] == "limited_auto_speak|"
    # T4 (already autonomous) → untouched.
    assert rows[4] == "autonomous|Already autonomous."


def test_upgrade_meeting_config_inherit_vs_backfill(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    rows = {
        r[0]: (r[1], r[2])
        for r in _rows(
            sqlite_engine, "SELECT id, mode, instructions FROM meeting_configs"
        )
    }
    # M1 inherits T1 (now backfilled non-empty) → stays inheriting (NULL).
    assert rows[1] == ("autonomous", None)
    # M2 inherits T3 (still empty) → gets meeting-level default.
    assert rows[2] == ("autonomous", DEFAULT)
    # M3 had its own override → preserved.
    assert rows[3] == ("autonomous", "Per-meeting brief.")
    # M4 is limited_auto_speak → untouched.
    assert rows[4] == ("limited_auto_speak", None)


def test_every_migrated_autonomous_row_passes_validation(
    sqlite_engine: sa.Engine,
) -> None:
    """Effective instructions (own override OR template base) must be
    non-empty for every autonomous template/meeting after migration."""
    _run(sqlite_engine, "upgrade")
    templates = {
        r[0]: (r[1] or "").strip()
        for r in _rows(
            sqlite_engine, "SELECT id, base_instructions FROM profile_templates"
        )
    }
    for cfg_id, mode, instr, tpl_id in _rows(
        sqlite_engine,
        "SELECT id, mode, instructions, profile_template_id FROM meeting_configs",
    ):
        if mode != "autonomous":
            continue
        effective = (instr or "").strip() or templates.get(tpl_id, "")
        assert effective != "", f"meeting_config {cfg_id} has empty effective instructions"
    for tpl_id, base in templates.items():
        # Only autonomous templates are gated; both migrated ones are autonomous.
        mode = _rows(
            sqlite_engine,
            f"SELECT mode FROM profile_templates WHERE id = {tpl_id}",
        )[0][0]
        if mode == "autonomous":
            assert base != "", f"template {tpl_id} has empty base_instructions"


def test_personalities_null_default_mode_preserved(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    rows = {
        r[0]: r[1]
        for r in _rows(sqlite_engine, "SELECT id, default_mode FROM personalities")
    }
    assert rows[1] == "autonomous"  # was free_auto_speak
    assert rows[2] is None  # NULL preserved
    assert rows[3] == "listen_only"  # untouched


def test_upgrade_then_downgrade_runs_clean(sqlite_engine: sa.Engine) -> None:
    """On SQLite the constraint swap is a no-op, so the pair must run without
    raising (the data merge itself is intentionally one-way)."""
    _run(sqlite_engine, "upgrade")
    _run(sqlite_engine, "downgrade")
    # Data stays autonomous after downgrade — the merge can't be reversed.
    remaining = _rows(
        sqlite_engine,
        "SELECT COUNT(*) FROM profile_templates WHERE mode = 'free_auto_speak'",
    )
    assert remaining[0][0] == 0
