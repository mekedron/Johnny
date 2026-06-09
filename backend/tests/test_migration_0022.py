"""Migration tests for 0022_agent_utterance_audio_file (Johnny-od1).

A plain additive column with an exists-guard, so the interesting properties
are: idempotent upgrade (re-run is a no-op, not a duplicate-column crash),
existing rows defaulting to NULL, and a clean downgrade that drops the
column without touching other data.
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
    / "0022_agent_utterance_audio_file.py"
)


def _load_migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("_migration_0022", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load_migration_module()


def _seed_schema(engine: sa.Engine) -> None:
    """Minimal pre-migration ``agent_utterances`` table."""
    md = sa.MetaData()
    sa.Table(
        "agent_utterances",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("output_text", sa.Text, nullable=False),
    )
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO agent_utterances (id, output_text) VALUES "
                "(1, 'hello'), (2, 'world')"
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
    return {col["name"] for col in inspector.get_columns("agent_utterances")}


def test_metadata_revision_chain() -> None:
    assert _MODULE.revision == "0022"
    assert _MODULE.down_revision == "0021"


def test_upgrade_adds_nullable_column(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}", future=True)
    _seed_schema(engine)
    _run(engine, "upgrade")
    assert "audio_file" in _columns(engine)
    with engine.begin() as conn:
        rows = conn.execute(
            sa.text("SELECT id, audio_file FROM agent_utterances ORDER BY id")
        ).fetchall()
    # Pre-existing rows default to NULL (no audio captured for legacy rows).
    assert [tuple(r) for r in rows] == [(1, None), (2, None)]


def test_upgrade_is_idempotent(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}", future=True)
    _seed_schema(engine)
    _run(engine, "upgrade")
    _run(engine, "upgrade")  # second run must not crash on a duplicate column
    assert "audio_file" in _columns(engine)


def test_downgrade_drops_column_and_keeps_rows(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}", future=True)
    _seed_schema(engine)
    _run(engine, "upgrade")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE agent_utterances SET audio_file = 'utt-1-1.wav' WHERE id = 1"
            )
        )
    _run(engine, "downgrade")
    assert "audio_file" not in _columns(engine)
    with engine.begin() as conn:
        count = conn.execute(
            sa.text("SELECT COUNT(*) FROM agent_utterances")
        ).scalar()
    assert count == 2


def test_downgrade_noop_when_column_absent(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}", future=True)
    _seed_schema(engine)
    _run(engine, "downgrade")  # nothing to drop — must not crash
    assert "audio_file" not in _columns(engine)
