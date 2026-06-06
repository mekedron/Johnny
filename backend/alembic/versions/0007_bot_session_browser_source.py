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


def upgrade() -> None:
    # source: defaults to 'meet' so every existing row is interpreted as
    # the legacy meet-worker path, which matches reality.
    op.add_column(
        "bot_sessions",
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default="meet",
        ),
    )
    op.create_check_constraint(
        "ck_bot_sessions_source",
        "bot_sessions",
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
    #
    # Storing the whole shape as one column keeps the migration tight;
    # the app reads/writes it as a dict.
    op.add_column(
        "bot_sessions",
        sa.Column(
            "playground_overrides",
            sa.dialects.postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
    )

    # Drop NOT NULL on meeting_config_id so playground sessions (no
    # calendar event) can omit it. We keep the FK so meet sessions stay
    # tied to a valid meeting_config, and add a CHECK constraint so a
    # browser session that IS tied to an event still has a meeting_config.
    with op.batch_alter_table("bot_sessions") as batch:
        batch.alter_column(
            "meeting_config_id",
            existing_type=sa.Integer(),
            nullable=True,
        )

    # Constraint: meet sessions must have a meeting_config_id.
    op.create_check_constraint(
        "ck_bot_sessions_meet_has_config",
        "bot_sessions",
        "source != 'meet' OR meeting_config_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_bot_sessions_meet_has_config", "bot_sessions", type_="check"
    )
    with op.batch_alter_table("bot_sessions") as batch:
        batch.alter_column(
            "meeting_config_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
    op.drop_column("bot_sessions", "playground_overrides")
    op.drop_constraint("ck_bot_sessions_source", "bot_sessions", type_="check")
    op.drop_column("bot_sessions", "source")
