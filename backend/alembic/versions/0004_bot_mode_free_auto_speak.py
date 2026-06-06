"""Add 'free_auto_speak' to the BotMode CHECK constraints.

Free-speech mode lets the bot reply naturally without the
``allowed_replies`` allowlist and without the approval round. The
router's confidence threshold still gates whether the bot speaks at
all. Used by ``profile_templates``, ``meeting_configs``, and
``agent_utterances`` so all three need the constraint widened.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-06 11:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OLD_MODES = ("listen_only", "suggest_only", "approval_required", "limited_auto_speak")
NEW_MODES = (
    "listen_only",
    "suggest_only",
    "approval_required",
    "limited_auto_speak",
    "free_auto_speak",
)


CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("ck_profile_templates_mode", "profile_templates"),
    ("ck_meeting_configs_mode", "meeting_configs"),
    ("ck_agent_utterances_mode", "agent_utterances"),
)


def _in_list(column: str, values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    for name, table in CONSTRAINTS:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, _in_list("mode", NEW_MODES))


def downgrade() -> None:
    for name, table in CONSTRAINTS:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, _in_list("mode", OLD_MODES))
