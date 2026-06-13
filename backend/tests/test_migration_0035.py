"""Migration tests for 0035_capability_policies_workspace (Johnny-wks.9).

Re-homes the capability-policy base layer global → per-workspace: the single
``global`` row becomes a ``workspace`` row keyed by a new ``workspace_id`` FK,
mapped onto the seeded DEFAULT workspace (behavior-preserving); the
``uq_capability_policies_global`` index becomes the per-workspace
``uq_capability_policies_workspace``; the scope + target-shape CHECKs are
retargeted onto ``workspace``. Verified on SQLite (the manual table-recreate
path whose ``INSERT … SELECT`` renames the scope with a ``CASE`` so the rows
land valid under the new CHECK; production Postgres takes the plain-ALTER
branch):

* the global row is re-scoped onto the default workspace; the agent /
  session_mode / session override rows are untouched (no workspace_id);
* one base row PER workspace (same workspace rejected, another workspace OK);
* the retargeted target CHECK holds (a ``workspace`` row needs a workspace_id;
  an ``agent`` row must NOT carry one);
* ``upgrade`` is idempotent; ``downgrade`` restores the ``global`` scope +
  drops the column.

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
    / "0035_capability_policies_workspace.py"
)


def _load_migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("_migration_0035", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load_migration_module()

# The 0030 shape's scope CHECK + target CHECK (pre-0035), inlined so the test
# seeds exactly what 0030 created.
_SCOPE_CK_BEFORE = (
    "scope IN ('global', 'agent', 'session_mode', 'session')"
)
_TARGET_SHAPE_BEFORE = (
    "(scope = 'global' AND agent_id IS NULL AND session_mode IS NULL "
    "AND bot_session_id IS NULL) OR "
    "(scope = 'agent' AND agent_id IS NOT NULL AND session_mode IS NULL "
    "AND bot_session_id IS NULL) OR "
    "(scope = 'session_mode' AND agent_id IS NULL AND session_mode IN "
    "('meet', 'browser') AND bot_session_id IS NULL) OR "
    "(scope = 'session' AND agent_id IS NULL AND session_mode IS NULL "
    "AND bot_session_id IS NOT NULL)"
)


@pytest.fixture
def engine(tmp_path: Path) -> sa.Engine:
    # File-backed so the table-recreate works across connections (0034 precedent).
    return sa.create_engine(f"sqlite:///{tmp_path}/migration_0035.db")


def _seed_pre_schema(engine: sa.Engine) -> int:
    """The pre-0035 shape: a ``workspaces`` table (default + one more) and a
    GLOBAL-scoped ``capability_policies`` (0030 shape: scope/target CHECKs +
    the global partial unique index, no workspace_id). Seeds a global row + an
    agent row + a session_mode row. Returns the default workspace id."""
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
        "capability_policies",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("agent_id", sa.Integer),
        sa.Column("session_mode", sa.String(16)),
        sa.Column("bot_session_id", sa.Integer),
        sa.Column("document", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_SCOPE_CK_BEFORE, name="ck_capability_policies_scope"),
        sa.CheckConstraint(_TARGET_SHAPE_BEFORE, name="ck_capability_policies_target"),
        sa.Index(
            "uq_capability_policies_global",
            "scope",
            unique=True,
            sqlite_where=sa.text("scope = 'global'"),
        ),
        sa.Index(
            "uq_capability_policies_agent",
            "agent_id",
            unique=True,
            sqlite_where=sa.text("agent_id IS NOT NULL"),
        ),
        sa.Index(
            "uq_capability_policies_session_mode",
            "session_mode",
            unique=True,
            sqlite_where=sa.text("session_mode IS NOT NULL"),
        ),
        sa.Index(
            "uq_capability_policies_session",
            "bot_session_id",
            unique=True,
            sqlite_where=sa.text("bot_session_id IS NOT NULL"),
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
        default_id = int(
            conn.execute(
                sa.text("SELECT id FROM workspaces WHERE is_default LIMIT 1")
            ).scalar_one()
        )
        # A global base row (to be re-homed) + an agent + a session_mode row
        # (override layers, untouched by the migration).
        conn.execute(
            sa.text(
                "INSERT INTO capability_policies (scope, document, created_at, "
                "updated_at) VALUES ('global', '{\"tools_deny\": [\"gmail.*\"]}', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO capability_policies (scope, agent_id, document, "
                "created_at, updated_at) VALUES ('agent', 5, '{}', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO capability_policies (scope, session_mode, document, "
                "created_at, updated_at) VALUES ('session_mode', 'meet', '{}', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    return default_id


def _run(engine: sa.Engine, direction: str) -> None:
    func = getattr(_MODULE, direction)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            func()


def _columns(engine: sa.Engine, table: str) -> dict[str, Any]:
    return {c["name"]: c for c in sa.inspect(engine).get_columns(table)}


def _index_names(engine: sa.Engine, table: str) -> set[str]:
    return {ix["name"] for ix in sa.inspect(engine).get_indexes(table)}


def test_upgrade_rescopes_global_onto_default_workspace(engine: sa.Engine) -> None:
    default_id = _seed_pre_schema(engine)
    _run(engine, "upgrade")

    assert "workspace_id" in _columns(engine, "capability_policies")

    with engine.begin() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT scope, workspace_id, agent_id, session_mode, document "
                "FROM capability_policies ORDER BY scope"
            )
        ).fetchall()
    by_scope = {r[0]: r for r in rows}
    # The old global row is now the default workspace's base layer, content kept.
    assert "global" not in by_scope
    ws_row = by_scope["workspace"]
    assert ws_row[1] == default_id
    assert "gmail.*" in ws_row[4]
    # Override layers untouched: no workspace_id, same targets.
    assert by_scope["agent"][1] is None and by_scope["agent"][2] == 5
    assert by_scope["session_mode"][1] is None and by_scope["session_mode"][3] == "meet"

    # The base index swapped; the FK points at workspaces.
    indexes = _index_names(engine, "capability_policies")
    assert "uq_capability_policies_workspace" in indexes
    assert "uq_capability_policies_global" not in indexes
    fks = sa.inspect(engine).get_foreign_keys("capability_policies")
    assert any(fk["referred_table"] == "workspaces" for fk in fks)


def test_base_layer_unique_per_workspace(engine: sa.Engine) -> None:
    default_id = _seed_pre_schema(engine)
    _run(engine, "upgrade")

    def _insert_base(conn: sa.Connection, workspace_id: int) -> None:
        conn.execute(
            sa.text(
                "INSERT INTO capability_policies (scope, workspace_id, document, "
                "created_at, updated_at) VALUES ('workspace', :w, '{}', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"w": workspace_id},
        )

    finance_id = default_id + 1  # seeded second
    # A base row for ANOTHER workspace is legal (one per workspace).
    with engine.begin() as conn:
        _insert_base(conn, finance_id)
    # A SECOND base row for the default is rejected (the partial unique index).
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            _insert_base(conn, default_id)


def test_target_check_is_retargeted_onto_workspace(engine: sa.Engine) -> None:
    _seed_pre_schema(engine)
    _run(engine, "upgrade")

    # A 'workspace' row MUST carry a workspace_id (the new target CHECK).
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO capability_policies (scope, document, created_at, "
                    "updated_at) VALUES ('workspace', '{}', CURRENT_TIMESTAMP, "
                    "CURRENT_TIMESTAMP)"
                )
            )
    # An 'agent' row must NOT carry one.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO capability_policies (scope, workspace_id, agent_id, "
                    "document, created_at, updated_at) VALUES ('agent', 1, 9, '{}', "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )


def test_upgrade_is_idempotent(engine: sa.Engine) -> None:
    _seed_pre_schema(engine)
    _run(engine, "upgrade")
    _run(engine, "upgrade")  # column-exists guard → no-op
    assert "workspace_id" in _columns(engine, "capability_policies")


def test_downgrade_restores_global_scope(engine: sa.Engine) -> None:
    _seed_pre_schema(engine)
    _run(engine, "upgrade")
    _run(engine, "downgrade")

    assert "workspace_id" not in _columns(engine, "capability_policies")
    indexes = _index_names(engine, "capability_policies")
    assert "uq_capability_policies_global" in indexes
    assert "uq_capability_policies_workspace" not in indexes
    with engine.begin() as conn:
        scopes = {
            r[0]
            for r in conn.execute(
                sa.text("SELECT DISTINCT scope FROM capability_policies")
            ).fetchall()
        }
    assert "workspace" not in scopes and "global" in scopes
