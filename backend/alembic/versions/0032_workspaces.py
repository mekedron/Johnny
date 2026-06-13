"""workspaces — first-class execution environments + agent attachment (Johnny-wks.1).

Three steps, each idempotent so a re-run / half-applied state self-heals:

1. ``workspaces`` table — name (display, renameable), slug (the storage-dir
   derivation key, frozen at creation), description, ``is_default`` under the
   single-default partial unique index (the agents/personalities pattern).
2. Seed the non-deletable DEFAULT workspace ("Default" / slug ``default``) —
   today's shared skills-sandbox, so every existing agent keeps
   byte-identical behavior. ``WHERE NOT EXISTS`` keeps re-runs from
   duplicating it.
3. ``agents.workspace_id`` — nullable FK, ``NULL`` = the default workspace
   (the provider-pin NULL-inherits convention; no backfill needed, which is
   what keeps pre-workspaces rows and fixtures byte-identical).
   ``ON DELETE RESTRICT``: a workspace cannot be deleted out from under its
   attached agents (the API additionally refuses with a 409 before the FK
   ever fires).

Revision ID: 0032
Revises: 0031
"""

from __future__ import annotations

import logging

import sqlalchemy as sa

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# Mirrors app.services.workspaces — duplicated here so the migration stays
# frozen if the service constants ever evolve (the 0027 persona precedent).
DEFAULT_WORKSPACE_NAME = "Default"
DEFAULT_WORKSPACE_SLUG = "default"
DEFAULT_WORKSPACE_DESCRIPTION = (
    "The execution environment every agent starts on — its own "
    "lazily-launched sandbox container under ~/.johnny/workspaces/default, "
    "like every other workspace. Non-deletable."
)


def _table_names(inspector: sa.Inspector) -> set[str]:
    return set(inspector.get_table_names())


def _column_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table)}


def _create_workspaces_table() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "is_default", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
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
        sa.UniqueConstraint("name", name="uq_workspaces_name"),
        sa.UniqueConstraint("slug", name="uq_workspaces_slug"),
    )
    # Partial unique index: only ``is_default=true`` rows are indexed and they
    # all share value true, so at most one default can exist (0027 pattern).
    op.create_index(
        "uq_workspaces_single_default",
        "workspaces",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
        sqlite_where=sa.text("is_default"),
    )


def _seed_default_workspace(bind: sa.Connection) -> None:
    bind.execute(
        sa.text(
            "INSERT INTO workspaces (name, slug, description, is_default) "
            "SELECT :name, :slug, :description, TRUE "
            "WHERE NOT EXISTS (SELECT 1 FROM workspaces WHERE is_default)"
        ),
        {
            "name": DEFAULT_WORKSPACE_NAME,
            "slug": DEFAULT_WORKSPACE_SLUG,
            "description": DEFAULT_WORKSPACE_DESCRIPTION,
        },
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "workspaces" not in _table_names(inspector):
        _create_workspaces_table()
    _seed_default_workspace(bind)

    # agents.workspace_id — batch mode so the FK addition works on SQLite
    # (table-recreate); plain ALTERs on Postgres (the 0027 precedent).
    if "workspace_id" not in _column_names(sa.inspect(bind), "agents"):
        with op.batch_alter_table("agents") as batch:
            batch.add_column(sa.Column("workspace_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                "fk_agents_workspace_id",
                "workspaces",
                ["workspace_id"],
                ["id"],
                ondelete="RESTRICT",
            )
        logger.info("Johnny-wks.1: agents.workspace_id added (NULL = default workspace)")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "workspace_id" in _column_names(inspector, "agents"):
        with op.batch_alter_table("agents") as batch:
            batch.drop_constraint("fk_agents_workspace_id", type_="foreignkey")
            batch.drop_column("workspace_id")

    if "workspaces" in _table_names(sa.inspect(bind)):
        op.drop_index("uq_workspaces_single_default", table_name="workspaces")
        op.drop_table("workspaces")
