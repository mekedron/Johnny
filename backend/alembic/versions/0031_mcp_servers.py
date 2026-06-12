"""mcp_servers — MCP connector config (Johnny-trt.36).

One row per configured MCP server, the provider-settings pattern: transport
(stdio command/args spawned inside the skills-sandbox, or a direct http
url), enabled flag, per-server tool include/exclude globs, timeouts, the
Fernet-encrypted env/headers blob, and the probe cache (last successful
``tools/list`` + the latest probe verdict) the catalog assembly reads
instead of connecting.

Shape rules CHECK-enforced here (the model deliberately carries no
CheckConstraints — the 0030 convention): a valid transport value, and the
transport↔field pairing (stdio ⇒ command set + url empty; http ⇒ url set +
command empty).

Revision ID: 0031
Revises: 0030
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0031"
down_revision = "0030"
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
        return

    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("transport", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("command", sa.Text(), nullable=False, server_default=""),
        sa.Column("args", _json_type(), nullable=False, server_default="[]"),
        sa.Column("url", sa.Text(), nullable=False, server_default=""),
        sa.Column("secrets_encrypted", sa.Text(), nullable=True),
        sa.Column("tool_include", _json_type(), nullable=True),
        sa.Column("tool_exclude", _json_type(), nullable=False, server_default="[]"),
        sa.Column(
            "connect_timeout_s", sa.Float(), nullable=False, server_default="10.0"
        ),
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
        sa.UniqueConstraint("name", name="uq_mcp_servers_name"),
        sa.CheckConstraint(
            _in_list("transport", MCP_TRANSPORTS), name="ck_mcp_servers_transport"
        ),
        sa.CheckConstraint(_TRANSPORT_SHAPE_SQL, name="ck_mcp_servers_transport_shape"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "mcp_servers" in set(inspector.get_table_names()):
        op.drop_table("mcp_servers")
