"""Migration tests for 0034_mcp_servers_workspace (Johnny-wks.8).

Scopes ``mcp_servers`` to a workspace: adds a NOT NULL ``workspace_id`` FK,
maps existing (global) rows onto the seeded DEFAULT workspace
(behavior-preserving), and swaps the global ``uq_mcp_servers_name`` for the
per-workspace ``uq_mcp_servers_workspace_name``. Verified on SQLite (the
``batch_alter_table`` table-recreate path; production Postgres takes the
plain-ALTER branch):

* existing rows survive and are mapped onto the default workspace;
* ``workspace_id`` is NOT NULL afterwards;
* names are unique PER WORKSPACE (same name in two workspaces is legal; a dup
  within one workspace is rejected);
* the 0031 transport-shape CHECK survives the recreate (a stdio+url row is
  still rejected);
* ``upgrade`` is idempotent; ``downgrade`` drops the column + restores the
  global unique name.

The pre-migration schema is built by hand (raw metadata) — the current ORM
models already describe the POST-migration world.
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
    / "0034_mcp_servers_workspace.py"
)


def _load_migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("_migration_0034", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load_migration_module()

_TRANSPORT_SHAPE_SQL = (
    "(transport = 'stdio' AND command <> '' AND url = '') OR "
    "(transport = 'http' AND url <> '' AND command = '')"
)


@pytest.fixture
def engine(tmp_path: Path) -> sa.Engine:
    # File-backed (not :memory:) so batch_alter_table's table-recreate works
    # across the connections alembic ops may open (the 0027/0033 precedent).
    return sa.create_engine(f"sqlite:///{tmp_path}/migration_0034.db")


def _seed_pre_schema(engine: sa.Engine) -> tuple[int, int]:
    """The pre-0034 shape: a ``workspaces`` table (default + one more) and a
    GLOBAL ``mcp_servers`` (unique name, the 0031 shape CHECK, no workspace_id).
    Returns ``(default_id, finance_id)``."""
    md = sa.MetaData()
    sa.Table(
        "workspaces",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("slug", sa.String(128), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("is_default", sa.Boolean, nullable=False),
    )
    sa.Table(
        "mcp_servers",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("transport", sa.String(16), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("command", sa.Text, nullable=False),
        sa.Column("args", sa.JSON, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("secrets_encrypted", sa.Text),
        sa.Column("tool_include", sa.JSON),
        sa.Column("tool_exclude", sa.JSON, nullable=False),
        sa.Column("connect_timeout_s", sa.Float, nullable=False),
        sa.Column("call_timeout_s", sa.Float, nullable=False),
        sa.Column("idle_ttl_s", sa.Float, nullable=False),
        sa.Column("tools_cache", sa.JSON),
        sa.Column("last_probe_at", sa.DateTime(timezone=True)),
        sa.Column("last_probe_ok", sa.Boolean),
        sa.Column("last_probe_error", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", name="uq_mcp_servers_name"),
        sa.CheckConstraint(
            "transport IN ('stdio', 'http')", name="ck_mcp_servers_transport"
        ),
        sa.CheckConstraint(
            _TRANSPORT_SHAPE_SQL, name="ck_mcp_servers_transport_shape"
        ),
    )
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO workspaces (name, slug, description, is_default) "
                "VALUES ('Default', 'default', '', TRUE), "
                "('Finance', 'finance', '', FALSE)"
            )
        )
        rows = conn.execute(
            sa.text("SELECT id, slug FROM workspaces ORDER BY id")
        ).fetchall()
        # Two pre-existing global servers, to be mapped onto the default.
        conn.execute(
            sa.text(
                "INSERT INTO mcp_servers (name, transport, enabled, command, args, "
                "url, tool_exclude, connect_timeout_s, call_timeout_s, idle_ttl_s, "
                "last_probe_error, created_at, updated_at) VALUES "
                "(:n, 'stdio', 1, 'python3', '[]', '', '[]', 10, 60, 300, '', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            [{"n": "alpha"}, {"n": "beta"}],
        )
    by_slug = {slug: int(rid) for rid, slug in rows}
    return by_slug["default"], by_slug["finance"]


def _run(engine: sa.Engine, direction: str) -> None:
    func = getattr(_MODULE, direction)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            func()


def _columns(engine: sa.Engine, table: str) -> dict[str, Any]:
    return {c["name"]: c for c in sa.inspect(engine).get_columns(table)}


def test_upgrade_maps_existing_rows_onto_default(engine: sa.Engine) -> None:
    default_id, _finance_id = _seed_pre_schema(engine)
    _run(engine, "upgrade")

    cols = _columns(engine, "mcp_servers")
    assert "workspace_id" in cols
    assert cols["workspace_id"]["nullable"] is False  # NOT NULL after backfill

    with engine.begin() as conn:
        rows = conn.execute(
            sa.text("SELECT name, workspace_id FROM mcp_servers ORDER BY name")
        ).fetchall()
    # Behavior-preserving: every old global server now belongs to the default.
    assert rows == [("alpha", default_id), ("beta", default_id)]

    # The FK is present and points at workspaces.
    fks = sa.inspect(engine).get_foreign_keys("mcp_servers")
    assert any(fk["referred_table"] == "workspaces" for fk in fks)


def test_name_unique_per_workspace_not_global(engine: sa.Engine) -> None:
    default_id, finance_id = _seed_pre_schema(engine)
    _run(engine, "upgrade")

    def _insert(conn: sa.Connection, name: str, workspace_id: int) -> None:
        conn.execute(
            sa.text(
                "INSERT INTO mcp_servers (workspace_id, name, transport, enabled, "
                "command, args, url, tool_exclude, connect_timeout_s, "
                "call_timeout_s, idle_ttl_s, last_probe_error, created_at, "
                "updated_at) VALUES (:w, :n, 'stdio', 1, 'python3', '[]', '', "
                "'[]', 10, 60, 300, '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"w": workspace_id, "n": name},
        )

    # 'alpha' already exists on the default. The SAME name on a DIFFERENT
    # workspace is legal (per-workspace uniqueness).
    with engine.begin() as conn:
        _insert(conn, "alpha", finance_id)

    # The same name on the SAME workspace is still rejected.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            _insert(conn, "alpha", default_id)


def test_transport_shape_check_survives_recreate(engine: sa.Engine) -> None:
    default_id, _finance_id = _seed_pre_schema(engine)
    _run(engine, "upgrade")

    # The 0031 shape CHECK must ride the batch recreate: a stdio row with a
    # url set is still rejected (it would otherwise silently relax).
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO mcp_servers (workspace_id, name, transport, "
                    "enabled, command, args, url, tool_exclude, connect_timeout_s, "
                    "call_timeout_s, idle_ttl_s, last_probe_error, created_at, "
                    "updated_at) VALUES (:w, 'bad', 'stdio', 1, 'python3', '[]', "
                    "'https://nope.test', '[]', 10, 60, 300, '', CURRENT_TIMESTAMP, "
                    "CURRENT_TIMESTAMP)"
                ),
                {"w": default_id},
            )


def test_upgrade_is_idempotent(engine: sa.Engine) -> None:
    _seed_pre_schema(engine)
    _run(engine, "upgrade")
    _run(engine, "upgrade")  # column-exists guard → no-op
    assert "workspace_id" in _columns(engine, "mcp_servers")


def test_downgrade_drops_column_and_restores_global_unique(engine: sa.Engine) -> None:
    _seed_pre_schema(engine)
    _run(engine, "upgrade")
    _run(engine, "downgrade")
    assert "workspace_id" not in _columns(engine, "mcp_servers")

    # The global unique name is back: a duplicate name is rejected again.
    def _insert(conn: sa.Connection, name: str) -> None:
        conn.execute(
            sa.text(
                "INSERT INTO mcp_servers (name, transport, enabled, command, args, "
                "url, tool_exclude, connect_timeout_s, call_timeout_s, idle_ttl_s, "
                "last_probe_error, created_at, updated_at) VALUES (:n, 'stdio', 1, "
                "'python3', '[]', '', '[]', 10, 60, 300, '', CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP)"
            ),
            {"n": name},
        )

    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            _insert(conn, "alpha")  # already exists from the seed
