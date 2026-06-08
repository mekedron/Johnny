"""Consolidate ``free_auto_speak`` into ``autonomous`` (Johnny-ckz.25).

``BotMode`` carried both ``free_auto_speak`` and ``autonomous``; the two were
operationally equivalent in the speak-or-not decision (no allowlist, no
approval round, the router's confidence_threshold gates speaking). The only
differences were autonomous's two guard rails: a non-empty-instructions
validation and a lower rate-limit cap. We drop ``free_auto_speak`` and keep
``autonomous`` as the single "answers freely" mode.

Storage model: ``BotMode`` is a VARCHAR + CHECK constraint (no native PG
enum — see ``app.db.models``), so there is no Postgres ``ALTER TYPE`` dance:
the migration is a data UPDATE plus a redefinition of the four CHECK
constraints that list the allowed values
(``profile_templates``, ``meeting_configs``, ``agent_utterances`` store
``mode``; ``personalities`` stores ``default_mode``).

Data conversion (``upgrade``):

1. Rows being migrated to autonomous whose own instructions are empty get a
   sensible default instruction string so they satisfy autonomous's
   non-empty-instructions validation on the next save. Templates first, then
   meeting configs whose *effective* instructions (own override OR the linked
   template's base) would still be empty — a meeting config that merely
   inherits a now-backfilled template is left inheriting (``instructions``
   stays ``NULL``) rather than pinned to a generic default.
2. Every ``free_auto_speak`` value is rewritten to ``autonomous`` across all
   four tables.
3. The CHECK constraints are tightened to drop ``free_auto_speak`` from the
   allowed set.

The CHECK-constraint swap runs on PostgreSQL only — SQLite cannot
``ALTER TABLE ... DROP CONSTRAINT`` (it needs batch/table-recreate), and the
in-process test harness builds its schema straight from the models (which
already exclude the value), so the data UPDATE is the portable part the
migration test exercises. Production is Postgres-only.

``downgrade`` re-widens the CHECK constraints so ``free_auto_speak`` is a
legal value again, restoring the pre-migration *schema* capability. The data
merge is intentionally one-way: once ``free_auto_speak`` rows are folded into
``autonomous`` they cannot be told apart from rows that were ``autonomous`` to
begin with, so the rows stay ``autonomous``.

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-08 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


DEFAULT_AUTONOMOUS_INSTRUCTIONS = (
    "Respond freely when confident the user wants an answer. "
    "Do not paraphrase questions back; just answer."
)

OLD_MODES = (
    "listen_only",
    "suggest_only",
    "approval_required",
    "limited_auto_speak",
    "free_auto_speak",
    "autonomous",
)
NEW_MODES = (
    "listen_only",
    "suggest_only",
    "approval_required",
    "limited_auto_speak",
    "autonomous",
)

# (table, column) for every place the mode value is stored.
MODE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("profile_templates", "mode"),
    ("meeting_configs", "mode"),
    ("agent_utterances", "mode"),
    ("personalities", "default_mode"),
)

# (constraint_name, table, column) for the CHECK constraints that enumerate
# the allowed values.
MODE_CONSTRAINTS: tuple[tuple[str, str, str], ...] = (
    ("ck_profile_templates_mode", "profile_templates", "mode"),
    ("ck_meeting_configs_mode", "meeting_configs", "mode"),
    ("ck_agent_utterances_mode", "agent_utterances", "mode"),
    ("ck_personalities_default_mode", "personalities", "default_mode"),
)


def _in_list(column: str, values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _swap_check_constraints(modes: Sequence[str]) -> None:
    """Redefine every mode CHECK constraint to allow exactly ``modes``.

    No-op on SQLite (cannot ``ALTER DROP CONSTRAINT``); production is
    Postgres and the test harness builds schema from the models.
    """
    if op.get_bind().dialect.name == "sqlite":
        return
    for name, table, column in MODE_CONSTRAINTS:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, _in_list(column, modes))


def upgrade() -> None:
    # 1a. Backfill empty instructions on templates migrating to autonomous.
    op.execute(
        sa.text(
            "UPDATE profile_templates SET base_instructions = :instr "
            "WHERE mode = 'free_auto_speak' "
            "AND (base_instructions IS NULL OR TRIM(base_instructions) = '')"
        ).bindparams(instr=DEFAULT_AUTONOMOUS_INSTRUCTIONS)
    )

    # 1b. Backfill meeting configs whose effective instructions would still be
    #     empty after migration: own override empty AND the linked template's
    #     base is empty too. Runs after 1a so a free_auto_speak template that
    #     was just backfilled is seen as non-empty and its inheriting meeting
    #     configs are left to inherit (instructions stays NULL).
    op.execute(
        sa.text(
            "UPDATE meeting_configs SET instructions = :instr "
            "WHERE mode = 'free_auto_speak' "
            "AND (instructions IS NULL OR TRIM(instructions) = '') "
            "AND profile_template_id IN ("
            "SELECT id FROM profile_templates "
            "WHERE base_instructions IS NULL OR TRIM(base_instructions) = ''"
            ")"
        ).bindparams(instr=DEFAULT_AUTONOMOUS_INSTRUCTIONS)
    )

    # 2. Rewrite the value everywhere it is stored.
    for table, column in MODE_COLUMNS:
        op.execute(
            sa.text(
                f"UPDATE {table} SET {column} = 'autonomous' "
                f"WHERE {column} = 'free_auto_speak'"
            )
        )

    # 3. Tighten the CHECK constraints to drop the value.
    _swap_check_constraints(NEW_MODES)


def downgrade() -> None:
    # Restore the pre-migration schema capability (free_auto_speak legal
    # again). Data stays autonomous — the merge is one-way.
    _swap_check_constraints(OLD_MODES)
