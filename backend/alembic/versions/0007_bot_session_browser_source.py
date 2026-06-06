"""Add ``source`` column to ``bot_sessions`` plus ``playground_overrides``.

Johnny-ckz.6 introduces the in-browser voice/text chat surface — both
the per-event "Try with bot" button and the standalone ``/playground``
page create real bot_sessions rows, but the audio path is a browser
WebSocket instead of the meet-worker container. We need to:

1. Tag every session with its origin so the UI can badge browser
   sessions distinctly from real-meeting sessions (AC #5).
2. Allow ``meeting_config_id`` to be ``NULL`` so the playground (no
   calendar event) can create a session without inventing a fake
   meeting_config row.
3. Persist per-session provider/system-prompt overrides as JSON so
   the pipeline can apply them transiently without mutating the
   global active-provider selection (AC #6 — overrides scoped to the
   single playground session).

Johnny-ckz.9: this revision shipped without ever running against the
live DB, leaving the ORM expecting ``source`` / ``playground_overrides``
on a 0006 schema. Every step is now wrapped in an inspector check so
the migration is safe to re-run against a half-applied state — anyone
who manually added one column but not the other can ``alembic upgrade
head`` cleanly without an ``DuplicateColumn`` crash. All ALTERs run
inside ``batch_alter_table`` so the same migration also works on the
SQLite engine used by the unit tests.

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-06 17:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


BOT_SESSION_SOURCES = ("meet", "browser")


def _in_list(column: str, values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _columns(inspector: sa.Inspector, table: str) -> dict[str, dict[str, object]]:
    return {col["name"]: col for col in inspector.get_columns(table)}


def _check_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {
        c["name"]
        for c in inspector.get_check_constraints(table)
        if c.get("name")
    }


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = _columns(inspector, "bot_sessions")
    checks = _check_names(inspector, "bot_sessions")
    meeting_config_id = cols.get("meeting_config_id")

    with op.batch_alter_table("bot_sessions") as batch:
        # source: defaults to 'meet' so every existing row is interpreted
        # as the legacy meet-worker path, which matches reality.
        if "source" not in cols:
            batch.add_column(
                sa.Column(
                    "source",
                    sa.String(length=16),
                    nullable=False,
                    server_default="meet",
                )
            )
        if "ck_bot_sessions_source" not in checks:
            batch.create_check_constraint(
                "ck_bot_sessions_source",
                _in_list("source", BOT_SESSION_SOURCES),
            )

        # playground_overrides: nullable JSON, populated only for browser
        # sessions where the user overrode providers or the system prompt
        # for that single run. Shape:
        #
        #   {
        #     "providers": {"stt": {...}, "llm": {...}, "tts": {...}},
        #     "system_prompt": "...",
        #     "persona": "...",
        #     "playground": bool
        #   }
        if "playground_overrides" not in cols:
            batch.add_column(
                sa.Column(
                    "playground_overrides",
                    sa.dialects.postgresql.JSONB().with_variant(
                        sa.JSON(), "sqlite"
                    ),
                    nullable=True,
                )
            )

        # Drop NOT NULL on meeting_config_id so playground sessions (no
        # calendar event) can omit it. We keep the FK so meet sessions
        # stay tied to a valid meeting_config, and add the CHECK below
        # so a browser session that IS tied to an event still has a
        # meeting_config.
        if meeting_config_id is not None and not meeting_config_id["nullable"]:
            batch.alter_column(
                "meeting_config_id",
                existing_type=sa.Integer(),
                nullable=True,
            )

        if "ck_bot_sessions_meet_has_config" not in checks:
            batch.create_check_constraint(
                "ck_bot_sessions_meet_has_config",
                "source != 'meet' OR meeting_config_id IS NOT NULL",
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = _columns(inspector, "bot_sessions")
    checks = _check_names(inspector, "bot_sessions")
    meeting_config_id = cols.get("meeting_config_id")

    with op.batch_alter_table("bot_sessions") as batch:
        if "ck_bot_sessions_meet_has_config" in checks:
            batch.drop_constraint(
                "ck_bot_sessions_meet_has_config", type_="check"
            )
        if meeting_config_id is not None and meeting_config_id["nullable"]:
            batch.alter_column(
                "meeting_config_id",
                existing_type=sa.Integer(),
                nullable=False,
            )
        if "playground_overrides" in cols:
            batch.drop_column("playground_overrides")
        if "ck_bot_sessions_source" in checks:
            batch.drop_constraint("ck_bot_sessions_source", type_="check")
        if "source" in cols:
            batch.drop_column("source")
