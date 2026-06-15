"""agents — configurable native tool-loop depth (Johnny-3gx).

Adds one per-agent behavior column the answer session reads (via the frozen
``bot_sessions.agent_snapshot``) to bound the native tool loop:

* ``max_tool_steps`` — how many tool steps one turn may take. ``0`` = UNLIMITED
  (a metabase data query legitimately chains 6-12 calls: discover → fetch →
  query); a positive value caps a runaway loop. Mirrors ``router_llm_timeout_s``'s
  per-agent chain (Agent column → this migration → Pydantic → agent_snapshot →
  SessionJobConfig prop → build_agent_session).

Additive, idempotent (the 0030 convention): inspector-guarded add, drop on
downgrade. ``server_default`` backfills existing rows so the NOT NULL column
lands cleanly; new rows get their value from the ORM-side default.

Revision ID: 0040
Revises: 0039
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

_COLUMNS: tuple[tuple[str, sa.Column], ...] = (
    (
        "max_tool_steps",
        sa.Column(
            "max_tool_steps",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {col["name"] for col in inspector.get_columns("agents")}
    for name, column in _COLUMNS:
        if name not in existing:
            op.add_column("agents", column)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {col["name"] for col in inspector.get_columns("agents")}
    for name, _column in reversed(_COLUMNS):
        if name in existing:
            op.drop_column("agents", name)
