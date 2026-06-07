"""Add the ``personalities`` table + bootstrap "Johnny" default (Johnny-oly.2).

A *personality* is a named, reusable preset bundling an LLM-provider
override, a TTS-provider override, and a default decision mode. It is the
schema half of the personality-library epic (Johnny-oly): the session
resolver (Johnny-oly.3) and the management UI (Johnny-oly.4/.5) build on
top of this table.

The two provider columns are nullable FKs into ``provider_credentials``
with ``ON DELETE SET NULL`` — deleting a provider must never destroy a
personality; the session resolver falls back to the globally-active
provider and surfaces a warning instead. ``default_mode`` is the usual
VARCHAR + CHECK enum (no native PG enum, so SQLite tests stay portable).
A partial unique index on ``is_default WHERE is_default`` enforces the
"exactly one default" invariant, mirroring the active-per-kind index on
``provider_credentials`` (``0002``).

The bootstrap row is seeded with **NULL** provider FKs and a **NULL**
``default_mode`` so it means "inherit the globally-active providers and
today's mode resolution". That delivers literal zero behaviour change for
an operator who never opens the page: NULL FKs fall through to
``build_provider_payload`` (unchanged) and a NULL mode falls through to
``meeting.mode`` / ``free_auto_speak`` (unchanged). NULL also beats
copying today's active provider ids, which would pin "Johnny" to a
snapshot and silently diverge the moment the operator activates a
different provider. The seed runs ``WHERE NOT EXISTS`` so re-applying the
migration never inserts a duplicate; ``created_at`` / ``updated_at`` /
``metadata`` are filled by their server defaults so the INSERT stays
portable across Postgres and the SQLite test harness.

Reversible (``downgrade`` drops the table cleanly) and re-runnable
(idempotent against a half-applied state via the inspector check).

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-07 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Allowed values for personalities.default_mode. Keep in sync with
# :class:`app.db.models.BotMode`.
BOT_MODES = (
    "listen_only",
    "suggest_only",
    "approval_required",
    "limited_auto_speak",
    "free_auto_speak",
    "autonomous",
)


def _in_list(column: str, values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _table_names(inspector: sa.Inspector) -> set[str]:
    return set(inspector.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "personalities" in _table_names(inspector):
        return

    op.create_table(
        "personalities",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "is_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("llm_provider_id", sa.Integer(), nullable=True),
        sa.Column("tts_provider_id", sa.Integer(), nullable=True),
        sa.Column("default_mode", sa.String(length=32), nullable=True),
        sa.Column(
            "metadata",
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["llm_provider_id"],
            ["provider_credentials.id"],
            ondelete="SET NULL",
            name="fk_personalities_llm_provider_id",
        ),
        sa.ForeignKeyConstraint(
            ["tts_provider_id"],
            ["provider_credentials.id"],
            ondelete="SET NULL",
            name="fk_personalities_tts_provider_id",
        ),
        sa.UniqueConstraint("display_name", name="uq_personalities_display_name"),
        sa.CheckConstraint(
            _in_list("default_mode", BOT_MODES),
            name="ck_personalities_default_mode",
        ),
    )

    # Partial unique index: only ``is_default=true`` rows are indexed and
    # they all share value true, so at most one default can exist. Mirrors
    # ``uq_provider_credentials_active_per_kind`` (0002).
    op.create_index(
        "uq_personalities_single_default",
        "personalities",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
        sqlite_where=sa.text("is_default"),
    )

    # Seed the bootstrap "Johnny" default. NULL provider FKs + NULL mode =
    # inherit global active + today's mode resolution → zero behaviour
    # change. server defaults fill metadata/created_at/updated_at, keeping
    # the INSERT portable (no NOW()/CURRENT_TIMESTAMP split). ``WHERE NOT
    # EXISTS`` makes a re-run a no-op.
    op.execute(
        "INSERT INTO personalities (display_name, description, is_default) "
        "SELECT 'Johnny', "
        "'Default personality (inherits the globally active providers).', "
        "TRUE "
        "WHERE NOT EXISTS (SELECT 1 FROM personalities WHERE is_default)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "personalities" not in _table_names(inspector):
        return
    op.drop_index("uq_personalities_single_default", table_name="personalities")
    op.drop_table("personalities")
