"""agents.meeting_bot_account_id — per-agent meeting-bot join identity (Johnny-wks.7).

The Meeting Bot account becomes a property of the AGENT: the Google identity
it JOINS meetings as (the Playwright ``storage_state.json`` the meet-worker
mounts). A nullable FK to ``google_accounts``; ``NULL`` = no agent-level
identity, so the per-assignment / per-meeting resolution
(``MeetingAgent.identity_account_id`` → ``MeetingConfig.identity_account_id``,
Johnny-trt.45/46) is left byte-identical — the migration is behavior
preserving, no backfill. ``ON DELETE SET NULL``: deleting the account detaches
every agent that joined as it (the row stays, falling back to per-meeting
resolution) rather than blocking the delete or orphaning the agent.

This is the MEETING-BOT identity only — unrelated to the gog workspace keyring
(Johnny-wks.4) and to ``agents.workspace_id`` (Johnny-wks.1). ``google_accounts``
rows stay GLOBAL and dual-capability (one row can be both a calendar source
and a bot identity).

Idempotent (the 0032 precedent): re-running a half-applied state self-heals.
Batch mode so the FK addition works on SQLite (table-recreate); plain ALTERs
on Postgres.

Revision ID: 0033
Revises: 0032
"""

from __future__ import annotations

import logging

import sqlalchemy as sa

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def _column_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "meeting_bot_account_id" not in _column_names(inspector, "agents"):
        with op.batch_alter_table("agents") as batch:
            batch.add_column(
                sa.Column("meeting_bot_account_id", sa.Integer(), nullable=True)
            )
            batch.create_foreign_key(
                "fk_agents_meeting_bot_account_id",
                "google_accounts",
                ["meeting_bot_account_id"],
                ["id"],
                ondelete="SET NULL",
            )
        logger.info(
            "Johnny-wks.7: agents.meeting_bot_account_id added "
            "(NULL = no agent-level meeting-bot identity)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "meeting_bot_account_id" in _column_names(inspector, "agents"):
        with op.batch_alter_table("agents") as batch:
            batch.drop_constraint(
                "fk_agents_meeting_bot_account_id", type_="foreignkey"
            )
            batch.drop_column("meeting_bot_account_id")
