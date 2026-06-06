"""Add 'autonomous' to the BotMode CHECK constraints.

Autonomous mode lets the bot reply free-form, governed solely by the
profile template's instructions and context — no ``allowed_replies``
allowlist, no per-utterance approval round. The router's
confidence_threshold and a per-session rate limit (default cap lower
than limited_auto_speak's, since each utterance is longer) keep cost
and over-talking in check. Used by ``profile_templates``,
``meeting_configs``, and ``agent_utterances`` so all three CHECK
constraints need the value added.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-06 16:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OLD_MODES = (
    "listen_only",
    "suggest_only",
    "approval_required",
    "limited_auto_speak",
    "free_auto_speak",
)
NEW_MODES = (
    "listen_only",
    "suggest_only",
    "approval_required",
    "limited_auto_speak",
    "free_auto_speak",
    "autonomous",
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
