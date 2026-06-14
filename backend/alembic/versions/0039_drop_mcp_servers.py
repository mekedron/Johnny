"""drop mcp_servers — MCP config moved to per-workspace .mcp.json (Johnny-hp1).

MCP servers are no longer DB-backed: each workspace's
``~/.johnny/workspaces/<slug>/.johnny/.mcp.json`` (FastMCP ``mcpServers``
format) is the source of truth now — see :mod:`johnny.mcp.store`. This drops
the ``mcp_servers`` table (created in 0031, given ``workspace_id`` in 0034).
No data is carried over: the cutover starts fresh (the operator weren't
relying on DB-stored servers), and ``run.sh`` seeds n8n into the default
workspace's file.

Idempotent (the 0030 convention): drop only if present; downgrade recreates
the empty post-0034 shape so the revision is reversible.

Revision ID: 0039
Revises: 0038
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

MCP_TRANSPORTS = ("stdio", "http")

_TRANSPORT_SHAPE_SQL = (
    "(transport = 'stdio' AND command <> '' AND url = '') OR "
    "(transport = 'http' AND url <> '' AND command = '')"
)


def _json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "mcp_servers" in set(inspector.get_table_names()):
        op.drop_table("mcp_servers")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "mcp_servers" in set(inspector.get_table_names()):
        return
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("transport", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("command", sa.Text(), nullable=False, server_default=""),
        sa.Column("args", _json_type(), nullable=False, server_default="[]"),
        sa.Column("url", sa.Text(), nullable=False, server_default=""),
        sa.Column("secrets_encrypted", sa.Text(), nullable=True),
        sa.Column("tool_include", _json_type(), nullable=True),
        sa.Column("tool_exclude", _json_type(), nullable=False, server_default="[]"),
        sa.Column("connect_timeout_s", sa.Float(), nullable=False, server_default="10.0"),
        sa.Column("call_timeout_s", sa.Float(), nullable=False, server_default="60.0"),
        sa.Column("idle_ttl_s", sa.Float(), nullable=False, server_default="300.0"),
        sa.Column("tools_cache", _json_type(), nullable=True),
        sa.Column("last_probe_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_probe_ok", sa.Boolean(), nullable=True),
        sa.Column("last_probe_error", sa.Text(), nullable=False, server_default=""),
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
        sa.UniqueConstraint(
            "workspace_id", "name", name="uq_mcp_servers_workspace_name"
        ),
        sa.CheckConstraint(
            _in_list("transport", MCP_TRANSPORTS), name="ck_mcp_servers_transport"
        ),
        sa.CheckConstraint(_TRANSPORT_SHAPE_SQL, name="ck_mcp_servers_transport_shape"),
    )
