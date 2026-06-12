"""Add ``conversation_events`` — the conversation-dynamics record (Johnny-trt.49).

Operator-requested observability: interruptions and "all those small
actions" (speech-floor handoffs, turn claims, peer-speech suppression)
must be tracked durably for later analysis. ``session_timings`` is the
wrong home — those rows are turn-keyed (``turn_id NOT NULL``) and shaped
around stage costs, while floor/claim/suppression events are
session-scoped with agent attribution.

One row per event, written only by the status subscriber from the
pipeline's conversation-dynamics events (``interruption_recorded`` ships
live with this bead from the single-agent barge-in paths; the floor /
claim / suppression types are the Johnny-trt.46 multi-agent vocabulary,
persisted-ready ahead of their emitters). ``timestamp_ms`` is the
session-relative offset (the ``session_timings.started_at_ms`` time
base); per-meeting analysis joins through
``bot_sessions.meeting_config_id``. Column-use-per-type is documented on
the ORM model (``app.db.models.ConversationEvent``).

The migration is reversible and re-runnable (idempotent against a
half-applied state via the inspector check), mirroring 0008.

Revision ID: 0029
Revises: 0028
Create Date: 2026-06-12 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Allowed values for conversation_events.event_type. Keep in sync with
# :data:`app.db.models.CONVERSATION_EVENT_TYPES` (and the wire ``type``
# discriminators in :mod:`johnny.voice_pipeline.events`).
CONVERSATION_EVENT_TYPES = (
    "interruption_recorded",
    "floor_acquired",
    "floor_released",
    "floor_expired",
    "turn_claim_won",
    "turn_claim_lost",
    "peer_speech_suppressed",
)


def _in_list(column: str, values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _table_names(inspector: sa.Inspector) -> set[str]:
    return set(inspector.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "conversation_events" in _table_names(inspector):
        return

    op.create_table(
        "conversation_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("bot_session_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("timestamp_ms", sa.Integer(), nullable=False),
        sa.Column("turn_id", sa.Integer(), nullable=True),
        sa.Column("agent_name", sa.String(length=128), nullable=True),
        sa.Column("counterpart_name", sa.String(length=128), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "reason", sa.String(length=255), nullable=False, server_default=""
        ),
        sa.Column(
            "details",
            sa.dialects.postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["bot_session_id"],
            ["bot_sessions.id"],
            ondelete="CASCADE",
            name="fk_conversation_events_bot_session_id",
        ),
        sa.CheckConstraint(
            _in_list("event_type", CONVERSATION_EVENT_TYPES),
            name="ck_conversation_events_event_type",
        ),
    )

    op.create_index(
        "ix_conversation_events_session_ts",
        "conversation_events",
        ["bot_session_id", "timestamp_ms"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "conversation_events" not in _table_names(inspector):
        return

    op.drop_index(
        "ix_conversation_events_session_ts", table_name="conversation_events"
    )
    op.drop_table("conversation_events")
