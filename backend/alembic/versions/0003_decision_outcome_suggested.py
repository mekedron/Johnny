"""Add 'suggested' to agent_decisions.outcome CHECK constraint.

Used by suggest-only mode (US-026): the router runs and writes a decision
row, but the answer/TTS stages are skipped — the row is persisted with
``outcome='suggested'`` so the audit trail clearly distinguishes
"router approved but mode prevented speaking" from "router suppressed".

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-06 10:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OLD_OUTCOMES = ("spoken", "suppressed", "pending", "rejected")
NEW_OUTCOMES = ("spoken", "suppressed", "pending", "rejected", "suggested")


def _in_list(column: str, values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    op.drop_constraint(
        "ck_agent_decisions_outcome",
        "agent_decisions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_agent_decisions_outcome",
        "agent_decisions",
        _in_list("outcome", NEW_OUTCOMES),
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_agent_decisions_outcome",
        "agent_decisions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_agent_decisions_outcome",
        "agent_decisions",
        _in_list("outcome", OLD_OUTCOMES),
    )
