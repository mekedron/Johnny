"""Per-assignment join identity on meeting_agents (Johnny-trt.45).

Multi-agent meetings launch one bot session per enabled assignment, and a
Google account cannot join the same Meet twice as two participants — each
co-attending agent needs its own identity account to appear under its own
name. This adds the optional ``identity_account_id`` FK to
``meeting_agents``; ``NULL`` falls back to the meeting-level
``meeting_configs.identity_account_id`` at dispatch, and deleting the
account resets the assignment to that fallback (``ON DELETE SET NULL``).

``batch_alter_table`` is used so the FK add works on SQLite (the test
engine) — a plain ``op.add_column`` with an inline ForeignKey raises
``NotImplementedError`` there; on Postgres the batch context passes the
ALTERs through unchanged.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | None = None
depends_on: str | None = None

_FK_NAME = "fk_meeting_agents_identity_account_id"


def upgrade() -> None:
    with op.batch_alter_table("meeting_agents") as batch:
        batch.add_column(
            sa.Column("identity_account_id", sa.Integer(), nullable=True)
        )
        batch.create_foreign_key(
            _FK_NAME,
            "google_accounts",
            ["identity_account_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("meeting_agents") as batch:
        batch.drop_constraint(_FK_NAME, type_="foreignkey")
        batch.drop_column("identity_account_id")
