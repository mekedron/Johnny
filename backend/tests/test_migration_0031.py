"""Migration tests for 0031_mcp_servers (Johnny-trt.36).

A standalone table (no FKs), so the surface here is: clean-DB creation with
the transport + transport-shape CHECKs and the unique name, idempotent
re-upgrade, and a clean downgrade. SQLite enforces CHECKs on insert, so the
shape rules run for real.
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
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0031_mcp_servers.py"
)


def _load_migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("_migration_0031", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load_migration_module()


@pytest.fixture
def engine(tmp_path: Path) -> sa.Engine:
    return sa.create_engine(f"sqlite:///{tmp_path}/migration_0031.db")


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


def _insert(conn: sa.Connection, **values: Any) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    conn.execute(
        sa.text(f"INSERT INTO mcp_servers ({columns}) VALUES ({placeholders})"),
        values,
    )


def test_upgrade_creates_table_with_shape_checks(engine: sa.Engine) -> None:
    _upgrade(engine)
    assert "mcp_servers" in _table_names(engine)

    with engine.begin() as conn:
        _insert(conn, name="fixture", transport="stdio", command="python3")
        _insert(conn, name="remote", transport="http", url="https://x.test/mcp")

        # Unique name.
        with pytest.raises(sa.exc.IntegrityError):
            _insert(conn, name="fixture", transport="stdio", command="other")

    with engine.begin() as conn:
        # Transport value CHECK.
        with pytest.raises(sa.exc.IntegrityError):
            _insert(conn, name="bad-transport", transport="ws", url="https://x")

    with engine.begin() as conn:
        # Shape CHECK: stdio without a command.
        with pytest.raises(sa.exc.IntegrityError):
            _insert(conn, name="bad-stdio", transport="stdio")

    with engine.begin() as conn:
        # Shape CHECK: http carrying a command.
        with pytest.raises(sa.exc.IntegrityError):
            _insert(
                conn,
                name="bad-http",
                transport="http",
                url="https://x.test",
                command="python3",
            )

    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT name, enabled, tool_exclude, idle_ttl_s FROM mcp_servers")
        ).fetchall()
    by_name = {row[0]: row for row in rows}
    assert set(by_name) == {"fixture", "remote"}
    # Server defaults landed (enabled, empty exclude, documented TTL).
    assert by_name["fixture"][1] in (1, True)
    assert by_name["fixture"][2] == "[]"
    assert float(by_name["fixture"][3]) == 300.0


def test_upgrade_is_idempotent(engine: sa.Engine) -> None:
    _upgrade(engine)
    with engine.begin() as conn:
        _insert(conn, name="fixture", transport="stdio", command="python3")
    _upgrade(engine)  # second run: no-op, data intact
    with engine.connect() as conn:
        count = conn.execute(sa.text("SELECT COUNT(*) FROM mcp_servers")).scalar()
    assert count == 1


def test_downgrade_drops_the_table(engine: sa.Engine) -> None:
    _upgrade(engine)
    _downgrade(engine)
    assert "mcp_servers" not in _table_names(engine)
    _downgrade(engine)  # idempotent downgrade too
