"""Add ``capability_policies`` + the ``policy_denied`` event type (Johnny-trt.38).

Operator-requested configurable capability policies: layered allow/deny over
tools (catalog kinds, glob-matched), an editable exec safe-bins baseline, and
per-skill enable/disable — resolved global → per-agent → per-session-mode →
per-session with deny winning at every merge (the openclaw
``agent-tools.policy.ts`` precedent, Johnny-shaped; normative order pinned in
:mod:`johnny.skills.capability_policy`).

One table, at most one row per scope target (the provider-settings pattern):

* the single ``global`` row,
* one ``agent`` row per agent (CASCADE with the agent),
* one ``session_mode`` row per surface (``meet`` / ``browser`` — meeting
  modes can be stricter than the playground),
* one ``session`` row per bot session (the per-session override; CASCADE).

Target shape per scope is CHECK-enforced; one-row-per-target is enforced by
partial unique indexes (the ``is_default`` precedent from 0027).

Also extends the ``conversation_events.event_type`` CHECK with
``policy_denied`` — the observability row a policy-denied ATTEMPT emits,
naming the denying layer (``reason`` column). On SQLite the constraint swap
goes through a ``batch_alter_table`` recreate with an explicit ``copy_from``
(constraint reflection is not relied on); Postgres takes the plain
drop/create branch.

The migration is reversible and re-runnable (idempotent against a
half-applied state via inspector checks), mirroring 0029.

Revision ID: 0030
Revises: 0029
Create Date: 2026-06-12 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Allowed scope / session-mode values. Keep in sync with
# :data:`app.db.models.CAPABILITY_POLICY_SCOPES` /
# :data:`app.db.models.CAPABILITY_POLICY_SESSION_MODES` (and
# :data:`johnny.skills.capability_policy.POLICY_SCOPE_ORDER`).
CAPABILITY_POLICY_SCOPES = ("global", "agent", "session_mode", "session")
CAPABILITY_POLICY_SESSION_MODES = ("meet", "browser")

# conversation_events.event_type values BEFORE / AFTER this migration. Keep
# the AFTER list in sync with :data:`app.db.models.CONVERSATION_EVENT_TYPES`.
CONVERSATION_EVENT_TYPES_BEFORE = (
    "interruption_recorded",
    "floor_acquired",
    "floor_released",
    "floor_expired",
    "turn_claim_won",
    "turn_claim_lost",
    "peer_speech_suppressed",
)
CONVERSATION_EVENT_TYPES_AFTER = (*CONVERSATION_EVENT_TYPES_BEFORE, "policy_denied")

_EVENT_TYPE_CK = "ck_conversation_events_event_type"


def _in_list(column: str, values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _table_names(inspector: sa.Inspector) -> set[str]:
    return set(inspector.get_table_names())


def _json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


_TARGET_SHAPE_SQL = (
    "(scope = 'global' AND agent_id IS NULL AND session_mode IS NULL "
    "AND bot_session_id IS NULL) OR "
    "(scope = 'agent' AND agent_id IS NOT NULL AND session_mode IS NULL "
    "AND bot_session_id IS NULL) OR "
    "(scope = 'session_mode' AND agent_id IS NULL AND session_mode IN "
    f"({', '.join(repr(m) for m in CAPABILITY_POLICY_SESSION_MODES)}) "
    "AND bot_session_id IS NULL) OR "
    "(scope = 'session' AND agent_id IS NULL AND session_mode IS NULL "
    "AND bot_session_id IS NOT NULL)"
)


def _conversation_events_copy_from(check_values: Sequence[str]) -> sa.Table:
    """The full ``conversation_events`` shape (0029) with the given CHECK.

    ``batch_alter_table(copy_from=…)`` recreates the table per this exact
    definition — no constraint reflection involved, which is what makes the
    CHECK swap reliable on SQLite. Must stay byte-equivalent to the 0029
    ``create_table`` apart from the amended value list.
    """
    md = sa.MetaData()
    return sa.Table(
        "conversation_events",
        md,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("bot_session_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("timestamp_ms", sa.Integer(), nullable=False),
        sa.Column("turn_id", sa.Integer(), nullable=True),
        sa.Column("agent_name", sa.String(length=128), nullable=True),
        sa.Column("counterpart_name", sa.String(length=128), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=False, server_default=""),
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
            _in_list("event_type", check_values),
            name=_EVENT_TYPE_CK,
        ),
        sa.Index("ix_conversation_events_session_ts", "bot_session_id", "timestamp_ms"),
    )


def _swap_event_type_check(check_values: Sequence[str]) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_constraint(_EVENT_TYPE_CK, "conversation_events", type_="check")
        op.create_check_constraint(
            _EVENT_TYPE_CK,
            "conversation_events",
            _in_list("event_type", check_values),
        )
        return
    # SQLite: full table recreate from the explicit definition (data copied).
    with op.batch_alter_table(
        "conversation_events",
        copy_from=_conversation_events_copy_from(check_values),
        recreate="always",
    ):
        pass


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "capability_policies" not in _table_names(inspector):
        op.create_table(
            "capability_policies",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("scope", sa.String(length=16), nullable=False),
            sa.Column("agent_id", sa.Integer(), nullable=True),
            sa.Column("session_mode", sa.String(length=16), nullable=True),
            sa.Column("bot_session_id", sa.Integer(), nullable=True),
            sa.Column("document", _json_type(), nullable=False, server_default="{}"),
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
                ["agent_id"],
                ["agents.id"],
                ondelete="CASCADE",
                name="fk_capability_policies_agent_id",
            ),
            sa.ForeignKeyConstraint(
                ["bot_session_id"],
                ["bot_sessions.id"],
                ondelete="CASCADE",
                name="fk_capability_policies_bot_session_id",
            ),
            sa.CheckConstraint(
                _in_list("scope", CAPABILITY_POLICY_SCOPES),
                name="ck_capability_policies_scope",
            ),
            sa.CheckConstraint(
                _TARGET_SHAPE_SQL,
                name="ck_capability_policies_target",
            ),
        )
        op.create_index(
            "uq_capability_policies_global",
            "capability_policies",
            ["scope"],
            unique=True,
            postgresql_where=sa.text("scope = 'global'"),
            sqlite_where=sa.text("scope = 'global'"),
        )
        op.create_index(
            "uq_capability_policies_agent",
            "capability_policies",
            ["agent_id"],
            unique=True,
            postgresql_where=sa.text("agent_id IS NOT NULL"),
            sqlite_where=sa.text("agent_id IS NOT NULL"),
        )
        op.create_index(
            "uq_capability_policies_session_mode",
            "capability_policies",
            ["session_mode"],
            unique=True,
            postgresql_where=sa.text("session_mode IS NOT NULL"),
            sqlite_where=sa.text("session_mode IS NOT NULL"),
        )
        op.create_index(
            "uq_capability_policies_session",
            "capability_policies",
            ["bot_session_id"],
            unique=True,
            postgresql_where=sa.text("bot_session_id IS NOT NULL"),
            sqlite_where=sa.text("bot_session_id IS NOT NULL"),
        )

    if "conversation_events" in _table_names(inspector):
        _swap_event_type_check(CONVERSATION_EVENT_TYPES_AFTER)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "conversation_events" in _table_names(inspector):
        # Structural restore only: policy_denied rows would violate the
        # narrowed CHECK, so they are removed first (one-way data, like the
        # 0027 stance on merged columns).
        bind.execute(
            sa.text("DELETE FROM conversation_events WHERE event_type = 'policy_denied'")
        )
        _swap_event_type_check(CONVERSATION_EVENT_TYPES_BEFORE)

    if "capability_policies" in _table_names(inspector):
        for index_name in (
            "uq_capability_policies_session",
            "uq_capability_policies_session_mode",
            "uq_capability_policies_agent",
            "uq_capability_policies_global",
        ):
            op.drop_index(index_name, table_name="capability_policies")
        op.drop_table("capability_policies")
