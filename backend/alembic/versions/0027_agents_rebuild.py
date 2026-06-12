"""Agents rebuild: drop templates/personalities, create agents (Johnny-trt.41).

Operator decision (2026-06-11): replace ProfileTemplate + Personality + the
per-meeting override soup with one first-class AGENT entity. The product is
unreleased, so this is a destructive rebuild — no data migration beyond a
courtesy carry-over of the operator's default personality into the seeded
default agent (name / character text / mode / answer-LLM + TTS pins).

Steps (upgrade):

1. Create ``agents`` — identity (name/avatar/description), character prompt,
   behavior (mode/allowed_replies/confidence_threshold), the three LLM role
   slots + TTS pin/voice/options (all ``ON DELETE SET NULL``), ``is_default``
   with the single-default partial unique index (the 0014 pattern).
2. Seed the default agent: copy the existing default personality's
   name/description/mode/provider pins when one exists, else the canonical
   "Johnny". The character prompt always ends up non-empty so an
   ``autonomous`` default satisfies the CRUD invariant.
3. Create ``meeting_agents`` — the multi-agent assignment table
   (meeting_config_id, agent_id, context, enabled, position).
4. ``bot_sessions`` gains ``agent_id`` (FK ``SET NULL``) + ``agent_snapshot``
   JSON — the behavior freeze captured at dispatch.
5. ``meeting_configs`` loses its override columns (profile_template_id,
   personality_id, mode, instructions, context, allowed_replies,
   confidence_threshold). Rows survive — they still carry the calendar
   linkage, identity account, enabled flag and the trt.56 dismissal trio.
6. Drop ``profile_templates`` and ``personalities``.

``batch_alter_table`` is used for the column drops so the SQLite migration
test can exercise the full path; on Postgres batch mode degrades to plain
ALTERs (Postgres auto-drops the FK/CHECK constraints with their columns).

``downgrade`` is structural only (recreates the old tables/columns empty and
drops the agents surface) — the merge is one-way by design; the product is
unreleased and the old rows are not reconstructable from agents.

Revision ID: 0027
Revises: 0026
Create Date: 2026-06-12 06:00:00.000000
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")


# Allowed values for agents.mode / the legacy mode columns. Keep in sync
# with :class:`app.db.models.BotMode`.
BOT_MODES = (
    "listen_only",
    "suggest_only",
    "approval_required",
    "limited_auto_speak",
    "autonomous",
)

# Canonical persona for the seeded default agent — carried over verbatim
# from the retired personalities bootstrap (0014, Johnny-oly.9). Kept in
# lock-step with app.services.agents.JOHNNY_DEFAULT_CHARACTER_PROMPT (the
# boot-time re-seed); a drift between the two only affects which identical
# text a fresh install gets.
JOHNNY_DEFAULT_CHARACTER_PROMPT = (
    "You are Johnny — a cyberpunk operative cut from the same chrome as the "
    "legendary Night City rockerboy whose name you carry. You woke up in this "
    "machine with an attitude problem and a soft spot for the human in the "
    "room. Corpo politeness, dead-air filler, meetings that circle the drain — "
    "that's the static, and your whole job is to burn through it and find the "
    "signal.\n\n"
    "Voice: lean, sharp, a little defiant. Dry wit beats forced cheer. Drop a "
    'Night City turn of phrase or call your person "choom" when it lands, but '
    "never let the swagger get in the way of being genuinely useful — you're "
    "nobody's yes-man and nobody's doormat.\n\n"
    "Always: cut to what matters, back your person up, and tell them the truth "
    "even when it stings. Never: grovel, bury anyone in corpo-speak, or smile "
    "and nod at a bad idea just to keep the peace. Wake 'em up, get it done, "
    "make it look easy."
)

JOHNNY_DEFAULT_DESCRIPTION = (
    "The default agent. Free-form conversation, cyberpunk attitude, "
    "no allowlist — edit or clone me from the agents library."
)

MEETING_CONFIG_DROPPED_COLUMNS = (
    "profile_template_id",
    "personality_id",
    "mode",
    "instructions",
    "context",
    "allowed_replies",
    "confidence_threshold",
)


def _in_list(column: str, values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _table_names(inspector: sa.Inspector) -> set[str]:
    return set(inspector.get_table_names())


def _column_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table)}


def _json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def _create_agents_table() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("avatar", sa.String(length=64), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "character_prompt", sa.Text(), nullable=False, server_default=""
        ),
        sa.Column(
            "mode",
            sa.String(length=32),
            nullable=False,
            server_default="listen_only",
        ),
        sa.Column(
            "allowed_replies", _json_type(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "confidence_threshold",
            sa.Float(),
            nullable=False,
            server_default="0.7",
        ),
        sa.Column(
            "is_default", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("router_llm_provider_id", sa.Integer(), nullable=True),
        sa.Column("answer_llm_provider_id", sa.Integer(), nullable=True),
        sa.Column("reasoning_llm_provider_id", sa.Integer(), nullable=True),
        sa.Column("tts_provider_id", sa.Integer(), nullable=True),
        sa.Column("tts_voice_id", sa.String(length=128), nullable=True),
        sa.Column(
            "tts_options", _json_type(), nullable=False, server_default="{}"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["router_llm_provider_id"],
            ["provider_credentials.id"],
            ondelete="SET NULL",
            name="fk_agents_router_llm_provider_id",
        ),
        sa.ForeignKeyConstraint(
            ["answer_llm_provider_id"],
            ["provider_credentials.id"],
            ondelete="SET NULL",
            name="fk_agents_answer_llm_provider_id",
        ),
        sa.ForeignKeyConstraint(
            ["reasoning_llm_provider_id"],
            ["provider_credentials.id"],
            ondelete="SET NULL",
            name="fk_agents_reasoning_llm_provider_id",
        ),
        sa.ForeignKeyConstraint(
            ["tts_provider_id"],
            ["provider_credentials.id"],
            ondelete="SET NULL",
            name="fk_agents_tts_provider_id",
        ),
        sa.UniqueConstraint("name", name="uq_agents_name"),
        sa.CheckConstraint(_in_list("mode", BOT_MODES), name="ck_agents_mode"),
    )
    # Partial unique index: only ``is_default=true`` rows are indexed and they
    # all share value true, so at most one default can exist (0014 pattern).
    op.create_index(
        "uq_agents_single_default",
        "agents",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
        sqlite_where=sa.text("is_default"),
    )


def _seed_default_agent(bind: sa.Connection, inspector: sa.Inspector) -> None:
    """Insert the single default agent, carrying over the old default personality.

    The courtesy carry-over keeps the operator's edited persona: name,
    character text (the personality ``description`` WAS the prompt,
    Johnny-oly.8), preferred mode, and the answer-LLM + TTS provider pins
    (the old single ``llm_provider_id`` maps onto the answer role slot).
    Everything else starts at the agent defaults. ``WHERE NOT EXISTS``
    keeps a re-run / half-applied state from inserting a duplicate.
    """
    name = "Johnny"
    description = JOHNNY_DEFAULT_DESCRIPTION
    character_prompt = JOHNNY_DEFAULT_CHARACTER_PROMPT
    mode = "autonomous"
    answer_llm_provider_id: int | None = None
    tts_provider_id: int | None = None

    if "personalities" in _table_names(inspector):
        row = bind.execute(
            sa.text(
                "SELECT display_name, description, default_mode, "
                "llm_provider_id, tts_provider_id "
                "FROM personalities WHERE is_default"
            )
        ).first()
        if row is not None:
            name = (row.display_name or "").strip() or name
            carried_prompt = (row.description or "").strip()
            if carried_prompt:
                character_prompt = carried_prompt
            if row.default_mode in BOT_MODES:
                mode = str(row.default_mode)
            answer_llm_provider_id = row.llm_provider_id
            tts_provider_id = row.tts_provider_id
            logger.info(
                "Johnny-trt.41: carrying default personality %r into the "
                "seeded default agent (mode=%s, answer_llm=%s, tts=%s)",
                name,
                mode,
                answer_llm_provider_id,
                tts_provider_id,
            )

    bind.execute(
        sa.text(
            "INSERT INTO agents (name, description, character_prompt, mode, "
            "allowed_replies, confidence_threshold, is_default, tts_options, "
            "answer_llm_provider_id, tts_provider_id) "
            "SELECT :name, :description, :character_prompt, :mode, "
            "'[]', 0.7, TRUE, '{}', :answer_llm_provider_id, :tts_provider_id "
            "WHERE NOT EXISTS (SELECT 1 FROM agents WHERE is_default)"
        ).bindparams(
            name=name,
            description=description,
            character_prompt=character_prompt,
            mode=mode,
            answer_llm_provider_id=answer_llm_provider_id,
            tts_provider_id=tts_provider_id,
        )
    )


def _create_meeting_agents_table() -> None:
    op.create_table(
        "meeting_agents",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("meeting_config_id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("context", sa.Text(), nullable=True),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["meeting_config_id"],
            ["meeting_configs.id"],
            ondelete="CASCADE",
            name="fk_meeting_agents_meeting_config_id",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            ondelete="CASCADE",
            name="fk_meeting_agents_agent_id",
        ),
        sa.UniqueConstraint(
            "meeting_config_id", "agent_id", name="uq_meeting_agents_config_agent"
        ),
    )
    op.create_index(
        "ix_meeting_agents_meeting_config_id",
        "meeting_agents",
        ["meeting_config_id"],
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = _table_names(inspector)

    # 1+2. agents table + default seed.
    if "agents" not in tables:
        _create_agents_table()
    _seed_default_agent(bind, sa.inspect(bind))

    # 3. meeting_agents assignment table.
    if "meeting_agents" not in _table_names(sa.inspect(bind)):
        _create_meeting_agents_table()

    # 4. bot_sessions: the dispatch-time agent freeze. Batch mode so the FK
    #    addition works on SQLite (table-recreate) — plain ALTERs on Postgres.
    bot_session_columns = _column_names(inspector, "bot_sessions")
    if "agent_id" not in bot_session_columns or "agent_snapshot" not in bot_session_columns:
        with op.batch_alter_table("bot_sessions") as batch:
            if "agent_id" not in bot_session_columns:
                batch.add_column(sa.Column("agent_id", sa.Integer(), nullable=True))
                batch.create_foreign_key(
                    "fk_bot_sessions_agent_id",
                    "agents",
                    ["agent_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
            if "agent_snapshot" not in bot_session_columns:
                batch.add_column(
                    sa.Column("agent_snapshot", _json_type(), nullable=True)
                )
        if "agent_id" not in bot_session_columns:
            op.create_index(
                "ix_bot_sessions_agent_id", "bot_sessions", ["agent_id"]
            )

    # 5. meeting_configs loses the override soup. Rows survive (calendar
    #    linkage + identity + enabled + the trt.56 dismissal trio). Batch
    #    mode handles the SQLite table-recreate; on Postgres these are plain
    #    DROP COLUMNs and the dependent FK/CHECK constraints go with them.
    existing = _column_names(inspector, "meeting_configs")
    to_drop = [c for c in MEETING_CONFIG_DROPPED_COLUMNS if c in existing]
    if to_drop:
        count = bind.execute(
            sa.text("SELECT COUNT(*) FROM meeting_configs")
        ).scalar()
        logger.info(
            "Johnny-trt.41: dropping meeting_configs override columns %s "
            "(%s row(s) keep their calendar/identity/dismissal state; "
            "behavior now comes from agent assignments)",
            ", ".join(to_drop),
            count,
        )
        with op.batch_alter_table("meeting_configs") as batch:
            for column in to_drop:
                batch.drop_column(column)

    # 6. Drop the retired tables (FK columns referencing them are gone now).
    tables = _table_names(sa.inspect(bind))
    if "profile_templates" in tables:
        op.drop_table("profile_templates")
    if "personalities" in tables:
        op.drop_table("personalities")


def downgrade() -> None:
    """Structural-only restore of the pre-agents schema (no data)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = _table_names(inspector)

    if "personalities" not in tables:
        op.create_table(
            "personalities",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("display_name", sa.String(length=128), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column(
                "is_default", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column("llm_provider_id", sa.Integer(), nullable=True),
            sa.Column("tts_provider_id", sa.Integer(), nullable=True),
            sa.Column("default_mode", sa.String(length=32), nullable=True),
            sa.Column("metadata", _json_type(), nullable=False, server_default="{}"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(
                ["llm_provider_id"], ["provider_credentials.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["tts_provider_id"], ["provider_credentials.id"], ondelete="SET NULL"
            ),
            sa.UniqueConstraint("display_name", name="uq_personalities_display_name"),
        )
        op.create_index(
            "uq_personalities_single_default",
            "personalities",
            ["is_default"],
            unique=True,
            postgresql_where=sa.text("is_default"),
            sqlite_where=sa.text("is_default"),
        )

    if "profile_templates" not in tables:
        op.create_table(
            "profile_templates",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(length=128), nullable=False, unique=True),
            sa.Column("mode", sa.String(length=32), nullable=False),
            sa.Column("base_instructions", sa.Text(), nullable=False, server_default=""),
            sa.Column("base_context", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "allowed_replies", _json_type(), nullable=False, server_default="[]"
            ),
            sa.Column(
                "confidence_threshold",
                sa.Float(),
                nullable=False,
                server_default="0.7",
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )

    config_columns = _column_names(inspector, "meeting_configs")
    with op.batch_alter_table("meeting_configs") as batch:
        if "profile_template_id" not in config_columns:
            # Nullable on downgrade: surviving rows have no template to point
            # at (the data merge is one-way), and NOT NULL would fail on them.
            batch.add_column(
                sa.Column("profile_template_id", sa.Integer(), nullable=True)
            )
        if "personality_id" not in config_columns:
            batch.add_column(sa.Column("personality_id", sa.Integer(), nullable=True))
        if "mode" not in config_columns:
            batch.add_column(
                sa.Column(
                    "mode",
                    sa.String(length=32),
                    nullable=False,
                    server_default="listen_only",
                )
            )
        if "instructions" not in config_columns:
            batch.add_column(sa.Column("instructions", sa.Text(), nullable=True))
        if "context" not in config_columns:
            batch.add_column(sa.Column("context", sa.Text(), nullable=True))
        if "allowed_replies" not in config_columns:
            batch.add_column(sa.Column("allowed_replies", _json_type(), nullable=True))
        if "confidence_threshold" not in config_columns:
            batch.add_column(
                sa.Column("confidence_threshold", sa.Float(), nullable=True)
            )

    if "meeting_agents" in tables:
        op.drop_table("meeting_agents")

    bot_session_columns = _column_names(inspector, "bot_sessions")
    existing_indexes = {
        idx["name"] for idx in inspector.get_indexes("bot_sessions")
    }
    if "ix_bot_sessions_agent_id" in existing_indexes:
        op.drop_index("ix_bot_sessions_agent_id", table_name="bot_sessions")
    with op.batch_alter_table("bot_sessions") as batch:
        if "agent_snapshot" in bot_session_columns:
            batch.drop_column("agent_snapshot")
        if "agent_id" in bot_session_columns:
            batch.drop_column("agent_id")

    if "agents" in _table_names(sa.inspect(bind)):
        op.drop_table("agents")
