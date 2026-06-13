"""capability_policies base layer goes global → per-workspace (Johnny-wks.9).

The capstone of the "workspace is the only home for tools" direction: there
is NO global capability policy. The base policy layer is a property of the
WORKSPACE an agent runs in — the ``global`` scope becomes ``workspace``,
keyed by a new ``workspace_id``. Resolution is now workspace → agent →
session_mode → session (the old global → agent → … chain with the base
re-homed).

Behavior-preserving: the single existing ``global`` row is re-scoped onto the
seeded DEFAULT workspace, so default-workspace agents keep the exact same
policy with zero reconfig. The agent / session_mode / session override layers
are untouched (they were never "global").

Steps, each guarded so a re-run / half-applied state self-heals (the
0032/0033/0034 precedent):

1. Resolve (seed if absent — belt-and-braces over 0032) the default workspace.
2. Add ``capability_policies.workspace_id`` (NULLABLE — only the base layer
   carries it), re-scope ``global`` → ``workspace`` + stamp the default
   workspace id, swap the ``uq_capability_policies_global`` partial unique
   index for ``uq_capability_policies_workspace`` (one base row PER
   workspace), retarget the scope + target-shape CHECKs onto ``workspace``,
   and add the ``ON DELETE CASCADE`` FK (a workspace's policy is owned
   content).

Postgres takes plain ALTERs (drop CHECKs → UPDATE → re-add CHECKs → swap
index → add FK). SQLite cannot ALTER a CHECK, and the old CHECK would reject
the transitional ``workspace`` value, so it does ONE manual table recreate
whose ``INSERT … SELECT`` renames the scope with a ``CASE`` — the data lands
already valid under the new CHECK, no two-phase dance.

Revision ID: 0035
Revises: 0034
"""

from __future__ import annotations

import logging

import sqlalchemy as sa

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# Mirrors app.services.workspaces — duplicated so the migration stays frozen
# if the service constants ever evolve (the 0032/0034 precedent).
DEFAULT_WORKSPACE_NAME = "Default"
DEFAULT_WORKSPACE_SLUG = "default"
DEFAULT_WORKSPACE_DESCRIPTION = (
    "The shared execution environment every agent starts on — today's "
    "skills-sandbox container. Non-deletable."
)

CAPABILITY_POLICY_SESSION_MODES = ("meet", "browser")

# scope vocabulary BEFORE / AFTER this migration (CHECK-enforced). Keep AFTER
# in sync with app.db.models.CAPABILITY_POLICY_SCOPES /
# johnny.skills.capability_policy.POLICY_SCOPE_ORDER.
SCOPES_BEFORE = ("global", "agent", "session_mode", "session")
SCOPES_AFTER = ("workspace", "agent", "session_mode", "session")

_SCOPE_CK = "ck_capability_policies_scope"
_TARGET_CK = "ck_capability_policies_target"

# Target-shape: each scope carries EXACTLY its own key column, all others NULL.
_TARGET_SHAPE_BEFORE = (
    "(scope = 'global' AND agent_id IS NULL AND session_mode IS NULL "
    "AND bot_session_id IS NULL) OR "
    "(scope = 'agent' AND agent_id IS NOT NULL AND session_mode IS NULL "
    "AND bot_session_id IS NULL) OR "
    "(scope = 'session_mode' AND agent_id IS NULL AND session_mode IN "
    f"({', '.join(repr(m) for m in CAPABILITY_POLICY_SESSION_MODES)}) "
    "AND bot_session_id IS NULL) OR "
    "(scope = 'session' AND agent_id IS NULL AND session_mode IS NULL "
    "AND bot_session_id IS NOT NULL)"
)
_TARGET_SHAPE_AFTER = (
    "(scope = 'workspace' AND workspace_id IS NOT NULL AND agent_id IS NULL "
    "AND session_mode IS NULL AND bot_session_id IS NULL) OR "
    "(scope = 'agent' AND workspace_id IS NULL AND agent_id IS NOT NULL "
    "AND session_mode IS NULL AND bot_session_id IS NULL) OR "
    "(scope = 'session_mode' AND workspace_id IS NULL AND agent_id IS NULL "
    "AND session_mode IN "
    f"({', '.join(repr(m) for m in CAPABILITY_POLICY_SESSION_MODES)}) "
    "AND bot_session_id IS NULL) OR "
    "(scope = 'session' AND workspace_id IS NULL AND agent_id IS NULL "
    "AND session_mode IS NULL AND bot_session_id IS NOT NULL)"
)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def _column_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table)}


def _default_workspace_id(bind: sa.Connection) -> int:
    """The seeded default workspace's id, seeding it if absent (0034 precedent)."""
    row = bind.execute(
        sa.text("SELECT id FROM workspaces WHERE is_default LIMIT 1")
    ).first()
    if row is not None:
        return int(row[0])
    bind.execute(
        sa.text(
            "INSERT INTO workspaces (name, slug, description, is_default) "
            "VALUES (:name, :slug, :description, TRUE)"
        ),
        {
            "name": DEFAULT_WORKSPACE_NAME,
            "slug": DEFAULT_WORKSPACE_SLUG,
            "description": DEFAULT_WORKSPACE_DESCRIPTION,
        },
    )
    row = bind.execute(
        sa.text("SELECT id FROM workspaces WHERE is_default LIMIT 1")
    ).first()
    assert row is not None  # just inserted
    return int(row[0])


def _create_capability_policies(name: str, *, with_workspace: bool) -> None:
    """Recreate ``capability_policies`` under ``name`` via ``op.create_table``
    (raw DDL, so the FK string refs need no referent in a local MetaData).

    ``with_workspace=True`` is the post-migration shape (workspace_id column +
    FK, ``workspace`` scope + target CHECK); ``False`` is the 0030 shape (no
    workspace_id, ``global`` scope) — used by the downgrade recreate. Must
    stay byte-equivalent to 0030's ``create_table`` apart from those deltas.
    """
    columns: list[Any] = [
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope", sa.String(length=16), nullable=False),
    ]
    if with_workspace:
        columns.append(sa.Column("workspace_id", sa.Integer(), nullable=True))
    columns += [
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("session_mode", sa.String(length=16), nullable=True),
        sa.Column("bot_session_id", sa.Integer(), nullable=True),
        sa.Column("document", _json_type(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    ]
    constraints: list[Any] = [
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            ondelete="CASCADE",
            name="fk_capability_policies_agent_id",
        ),
        sa.ForeignKeyConstraint(
            ["bot_session_id"],
            ["bot_sessions.id"],
            ondelete="CASCADE",
            name="fk_capability_policies_bot_session_id",
        ),
        sa.CheckConstraint(
            _in_list("scope", SCOPES_AFTER if with_workspace else SCOPES_BEFORE),
            name=_SCOPE_CK,
        ),
        sa.CheckConstraint(
            _TARGET_SHAPE_AFTER if with_workspace else _TARGET_SHAPE_BEFORE,
            name=_TARGET_CK,
        ),
    ]
    if with_workspace:
        constraints.append(
            sa.ForeignKeyConstraint(
                ["workspace_id"],
                ["workspaces.id"],
                ondelete="CASCADE",
                name="fk_capability_policies_workspace_id",
            )
        )
    op.create_table(name, *columns, *constraints)


def _create_indexes(*, base_index: str, base_columns: list[str], base_where: str) -> None:
    """Recreate the four partial unique indexes after a SQLite table swap.

    The base layer's index name/columns/predicate differ between the two
    shapes (``uq_capability_policies_workspace`` on ``workspace_id`` vs the
    0030 ``uq_capability_policies_global`` on ``scope``); the override-layer
    indexes are identical in both.
    """
    op.create_index(
        base_index,
        "capability_policies",
        base_columns,
        unique=True,
        postgresql_where=sa.text(base_where),
        sqlite_where=sa.text(base_where),
    )
    op.create_index(
        "uq_capability_policies_agent",
        "capability_policies",
        ["agent_id"],
        unique=True,
        postgresql_where=sa.text("agent_id IS NOT NULL"),
        sqlite_where=sa.text("agent_id IS NOT NULL"),
    )
    op.create_index(
        "uq_capability_policies_session_mode",
        "capability_policies",
        ["session_mode"],
        unique=True,
        postgresql_where=sa.text("session_mode IS NOT NULL"),
        sqlite_where=sa.text("session_mode IS NOT NULL"),
    )
    op.create_index(
        "uq_capability_policies_session",
        "capability_policies",
        ["bot_session_id"],
        unique=True,
        postgresql_where=sa.text("bot_session_id IS NOT NULL"),
        sqlite_where=sa.text("bot_session_id IS NOT NULL"),
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "capability_policies" not in set(inspector.get_table_names()):
        return  # nothing to migrate (stripped schema)
    if "workspace_id" in _column_names(inspector, "capability_policies"):
        return  # already per-workspace — idempotent re-run

    default_id = _default_workspace_id(bind)

    if bind.dialect.name == "postgresql":
        op.add_column(
            "capability_policies", sa.Column("workspace_id", sa.Integer(), nullable=True)
        )
        op.drop_constraint(_TARGET_CK, "capability_policies", type_="check")
        op.drop_constraint(_SCOPE_CK, "capability_policies", type_="check")
        bind.execute(
            sa.text(
                "UPDATE capability_policies SET scope = 'workspace', "
                "workspace_id = :wid WHERE scope = 'global'"
            ),
            {"wid": default_id},
        )
        op.create_check_constraint(
            _SCOPE_CK, "capability_policies", _in_list("scope", SCOPES_AFTER)
        )
        op.create_check_constraint(
            _TARGET_CK, "capability_policies", _TARGET_SHAPE_AFTER
        )
        op.drop_index("uq_capability_policies_global", table_name="capability_policies")
        op.create_index(
            "uq_capability_policies_workspace",
            "capability_policies",
            ["workspace_id"],
            unique=True,
            postgresql_where=sa.text("scope = 'workspace'"),
        )
        op.create_foreign_key(
            "fk_capability_policies_workspace_id",
            "capability_policies",
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )
    else:
        # SQLite: ONE manual recreate. The INSERT…SELECT renames 'global' →
        # 'workspace' and stamps the default workspace id in the same pass, so
        # the rows land valid under the new CHECK (no transitional value ever
        # violates it). Dropping the old table drops its indexes; recreate all.
        _create_capability_policies("_capability_policies_new", with_workspace=True)
        bind.execute(
            sa.text(
                "INSERT INTO _capability_policies_new "
                "(id, scope, workspace_id, agent_id, session_mode, "
                "bot_session_id, document, created_at, updated_at) "
                "SELECT id, "
                "CASE WHEN scope = 'global' THEN 'workspace' ELSE scope END, "
                "CASE WHEN scope = 'global' THEN :wid ELSE NULL END, "
                "agent_id, session_mode, bot_session_id, document, "
                "created_at, updated_at FROM capability_policies"
            ),
            {"wid": default_id},
        )
        op.drop_table("capability_policies")
        op.rename_table("_capability_policies_new", "capability_policies")
        _create_indexes(
            base_index="uq_capability_policies_workspace",
            base_columns=["workspace_id"],
            base_where="scope = 'workspace'",
        )

    logger.info(
        "Johnny-wks.9: capability_policies base layer re-homed onto workspaces; "
        "%s 'workspace' base row(s) now keyed by workspace_id (default id=%s)",
        bind.execute(
            sa.text("SELECT COUNT(*) FROM capability_policies WHERE scope = 'workspace'")
        ).scalar(),
        default_id,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "capability_policies" not in set(inspector.get_table_names()):
        return
    if "workspace_id" not in _column_names(inspector, "capability_policies"):
        return  # already pre-workspace

    if bind.dialect.name == "postgresql":
        op.drop_constraint(
            "fk_capability_policies_workspace_id", "capability_policies", type_="foreignkey"
        )
        op.drop_index(
            "uq_capability_policies_workspace", table_name="capability_policies"
        )
        op.drop_constraint(_TARGET_CK, "capability_policies", type_="check")
        op.drop_constraint(_SCOPE_CK, "capability_policies", type_="check")
        bind.execute(
            sa.text("UPDATE capability_policies SET scope = 'global' WHERE scope = 'workspace'")
        )
        op.create_check_constraint(
            _SCOPE_CK, "capability_policies", _in_list("scope", SCOPES_BEFORE)
        )
        op.create_check_constraint(
            _TARGET_CK, "capability_policies", _TARGET_SHAPE_BEFORE
        )
        op.create_index(
            "uq_capability_policies_global",
            "capability_policies",
            ["scope"],
            unique=True,
            postgresql_where=sa.text("scope = 'global'"),
        )
        op.drop_column("capability_policies", "workspace_id")
    else:
        _create_capability_policies("_capability_policies_old", with_workspace=False)
        bind.execute(
            sa.text(
                "INSERT INTO _capability_policies_old "
                "(id, scope, agent_id, session_mode, bot_session_id, "
                "document, created_at, updated_at) "
                "SELECT id, "
                "CASE WHEN scope = 'workspace' THEN 'global' ELSE scope END, "
                "agent_id, session_mode, bot_session_id, document, "
                "created_at, updated_at FROM capability_policies"
            )
        )
        op.drop_table("capability_policies")
        op.rename_table("_capability_policies_old", "capability_policies")
        _create_indexes(
            base_index="uq_capability_policies_global",
            base_columns=["scope"],
            base_where="scope = 'global'",
        )
