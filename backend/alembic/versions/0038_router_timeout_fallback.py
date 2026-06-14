"""agents — configurable router-triage timeout + on-timeout fallback (Johnny-xql).

Adds four per-agent behavior columns the router gate reads (via the frozen
``bot_sessions.agent_snapshot``) to bound and degrade the per-turn triage call:

* ``router_llm_timeout_s`` — wall-clock budget on the triage LLM call
  (mirrors ``voice_pipeline.reasoning.DEFAULT_ROUTER_LLM_TIMEOUT_S``, 8.0 s);
  ``<= 0`` disables the bound.
* ``router_timeout_retries`` — re-run the triage this many times on timeout
  before giving up (0 = single attempt, the pre-xql behavior).
* ``router_timeout_fallback_mode`` — ``disabled`` | ``static`` | ``llm``.
* ``router_timeout_fallback_text`` — spoken in ``static`` mode (and the
  ``llm`` degrade target).

Additive, idempotent (the 0030 convention): inspector-guarded add, drop on
downgrade. ``server_default`` backfills existing rows so the NOT NULL columns
land cleanly; new rows get their value from the ORM-side default.

Revision ID: 0038
Revises: 0037
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

_DEFAULT_FALLBACK_TEXT = "Sorry, I didn't catch that in time — could you say that again?"

_COLUMNS: tuple[tuple[str, sa.Column], ...] = (
    (
        "router_llm_timeout_s",
        sa.Column(
            "router_llm_timeout_s",
            sa.Float(),
            nullable=False,
            server_default="8.0",
        ),
    ),
    (
        "router_timeout_retries",
        sa.Column(
            "router_timeout_retries",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    ),
    (
        "router_timeout_fallback_mode",
        sa.Column(
            "router_timeout_fallback_mode",
            sa.String(length=16),
            nullable=False,
            server_default="static",
        ),
    ),
    (
        "router_timeout_fallback_text",
        sa.Column(
            "router_timeout_fallback_text",
            sa.Text(),
            nullable=False,
            server_default=_DEFAULT_FALLBACK_TEXT,
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
