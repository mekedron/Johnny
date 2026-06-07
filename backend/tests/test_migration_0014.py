"""Migration tests for 0014_personalities (Johnny-oly.2).

Verifies the bootstrap "Johnny" seed (exactly one default, NULL provider
FKs, NULL mode — the zero-behaviour-change contract), idempotency of both
``upgrade`` and ``downgrade`` against half-applied state, and that the
single-default partial unique index is actually enforced after upgrade.

SQLite is enough: the migration stores ``metadata`` as
``JSONB().with_variant(JSON(), "sqlite")`` and every other column is a
portable type, so the dialect difference doesn't change the produced shape.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import Personality, ProviderCredential

_MIGRATION_FILE = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0014_personalities.py"
)


def _load_migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("_migration_0014", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> sa.Engine:
    url = f"sqlite:///{tmp_path / 'mig.db'}"
    engine = sa.create_engine(url, future=True)

    @sa.event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn: object, _record: object) -> None:
        cursor = dbapi_conn.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    # The FK target must exist before 0014 creates personalities.
    Base.metadata.create_all(
        bind=engine,
        tables=[ProviderCredential.__table__],  # type: ignore[list-item]
    )
    return engine


def _run(engine: sa.Engine, direction: str) -> None:
    migration = _load_migration_module()
    func = getattr(migration, direction)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            func()


def _table_names(engine: sa.Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def test_upgrade_creates_table_and_seeds_johnny(sqlite_engine: sa.Engine) -> None:
    assert "personalities" not in _table_names(sqlite_engine)
    _run(sqlite_engine, "upgrade")
    assert "personalities" in _table_names(sqlite_engine)

    with Session(sqlite_engine) as session:
        rows = session.scalars(sa.select(Personality)).all()
        assert len(rows) == 1
        johnny = rows[0]
        assert johnny.display_name == "Johnny"
        assert johnny.is_default is True
        assert johnny.llm_provider_id is None
        assert johnny.tts_provider_id is None
        assert johnny.default_mode is None
        assert johnny.extra_metadata == {}
        assert johnny.created_at is not None
        assert johnny.updated_at is not None


def test_upgrade_is_idempotent_no_duplicate_johnny(
    sqlite_engine: sa.Engine,
) -> None:
    _run(sqlite_engine, "upgrade")
    # Second pass: the table-exists guard makes upgrade a no-op, so the
    # seed must not run again.
    _run(sqlite_engine, "upgrade")
    with Session(sqlite_engine) as session:
        rows = session.scalars(sa.select(Personality)).all()
        assert len(rows) == 1
        assert rows[0].display_name == "Johnny"


def test_single_default_index_enforced(sqlite_engine: sa.Engine) -> None:
    """A second is_default=true row violates the partial unique index."""
    _run(sqlite_engine, "upgrade")
    with sqlite_engine.begin() as conn:
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO personalities (display_name, is_default, metadata) "
                    "VALUES ('Other', TRUE, '{}')"
                )
            )


def test_non_default_rows_unconstrained(sqlite_engine: sa.Engine) -> None:
    """Many is_default=false rows coexist (partial index only covers true)."""
    _run(sqlite_engine, "upgrade")
    with sqlite_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO personalities (display_name, is_default, metadata) "
                "VALUES ('A', FALSE, '{}'), ('B', FALSE, '{}')"
            )
        )
    with Session(sqlite_engine) as session:
        assert len(session.scalars(sa.select(Personality)).all()) == 3


def test_downgrade_drops_table(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    _run(sqlite_engine, "downgrade")
    assert "personalities" not in _table_names(sqlite_engine)
    # provider_credentials (an unaffected table) survives.
    assert "provider_credentials" in _table_names(sqlite_engine)


def test_downgrade_is_idempotent(sqlite_engine: sa.Engine) -> None:
    _run(sqlite_engine, "upgrade")
    _run(sqlite_engine, "downgrade")
    # Second downgrade against the already-dropped shape must not crash.
    _run(sqlite_engine, "downgrade")
    assert "personalities" not in _table_names(sqlite_engine)
