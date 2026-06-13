"""mcp_servers.workspace_id — MCP connectors move global → per-workspace (Johnny-wks.8).

Part of the "workspace is the only home for tools" direction: an MCP server
is OWNED by exactly one workspace, and an agent's MCP toolset is exactly its
workspace's servers (the executor/assembly resolve them by the agent's
workspace, the wks.3 routing precedent). There is no global MCP registry.

Behavior-preserving migration: every existing (global) ``mcp_servers`` row is
mapped onto the seeded DEFAULT workspace, so agents on the default keep the
same MCP toolset with zero reconfig. Steps, each guarded so a re-run /
half-applied state self-heals (the 0032/0033 precedent):

1. Resolve (seed if absent — belt-and-braces over 0032) the default workspace.
2. Add ``mcp_servers.workspace_id`` NULLABLE, backfill every row to the
   default workspace id, then — in one batch recreate so SQLite is happy —
   make it NOT NULL, swap the global ``uq_mcp_servers_name`` for the
   per-workspace ``uq_mcp_servers_workspace_name`` (two workspaces may each
   own a ``github`` connector; resolution is workspace-keyed so the
   ``mcp__github__<tool>`` kinds never collide), and add the
   ``ON DELETE CASCADE`` FK (a workspace's servers are owned content).

Batch mode so the FK + constraint changes work on SQLite (table-recreate);
production Postgres takes the plain-ALTER branch of the same ops. The 0031
transport / transport-shape CHECK constraints ride through the recreate via
reflection (a migration test asserts shape enforcement survives).

Revision ID: 0034
Revises: 0033
"""

from __future__ import annotations

import logging

import sqlalchemy as sa

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# Mirrors app.services.workspaces — duplicated so the migration stays frozen
# if the service constants ever evolve (the 0027/0032 precedent).
DEFAULT_WORKSPACE_NAME = "Default"
DEFAULT_WORKSPACE_SLUG = "default"
DEFAULT_WORKSPACE_DESCRIPTION = (
    "The shared execution environment every agent starts on — today's "
    "skills-sandbox container. Non-deletable."
)


def _column_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table)}


def _default_workspace_id(bind: sa.Connection) -> int:
    """The seeded default workspace's id, seeding it if absent.

    0032 already seeds it (and 0034 revises 0033 → 0032), so this normally
    just reads. The seed is belt-and-braces for a hand-built pre-schema —
    the same insurance :func:`app.services.workspaces.seed_default_workspace`
    gives the boot path.
    """
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


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "workspace_id" in _column_names(inspector, "mcp_servers"):
        return  # already per-workspace — idempotent re-run

    default_id = _default_workspace_id(bind)

    # Add nullable first so existing rows can be backfilled before the column
    # becomes NOT NULL (a NOT NULL add would fail on a non-empty table).
    op.add_column("mcp_servers", sa.Column("workspace_id", sa.Integer(), nullable=True))
    bind.execute(
        sa.text("UPDATE mcp_servers SET workspace_id = :wid WHERE workspace_id IS NULL"),
        {"wid": default_id},
    )

    with op.batch_alter_table("mcp_servers") as batch:
        batch.alter_column(
            "workspace_id", existing_type=sa.Integer(), nullable=False
        )
        batch.drop_constraint("uq_mcp_servers_name", type_="unique")
        batch.create_unique_constraint(
            "uq_mcp_servers_workspace_name", ["workspace_id", "name"]
        )
        batch.create_foreign_key(
            "fk_mcp_servers_workspace_id",
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )
    logger.info(
        "Johnny-wks.8: mcp_servers.workspace_id added; %s existing server(s) "
        "mapped onto the default workspace (id=%s)",
        bind.execute(sa.text("SELECT COUNT(*) FROM mcp_servers")).scalar(),
        default_id,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "workspace_id" not in _column_names(inspector, "mcp_servers"):
        return

    with op.batch_alter_table("mcp_servers") as batch:
        batch.drop_constraint("fk_mcp_servers_workspace_id", type_="foreignkey")
        batch.drop_constraint("uq_mcp_servers_workspace_name", type_="unique")
        batch.create_unique_constraint("uq_mcp_servers_name", ["name"])
        batch.drop_column("workspace_id")
