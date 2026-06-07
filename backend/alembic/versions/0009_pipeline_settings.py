"""Add singleton ``pipeline_settings`` table for split/unified routing (Johnny-ckz.17).

The voice pipeline today is a fixed STT → LLM → TTS chain (split mode).
This migration introduces a global ``pipeline_settings.pipeline_mode``
toggle so the runner can switch entire sessions to a unified
speech-to-speech provider (OpenAI GPT-Realtime, Gemini Live) without
touching the provider_credentials schema or breaking the existing
split-pipeline tests.

The table is a singleton: ``id`` is constrained to 1 via a CHECK so
both API readers and the seeder don't have to handle multi-row
ambiguity. ``pipeline_mode`` defaults to ``"split"`` so every existing
deployment continues to run the split pipeline after migrating, with no
behaviour change.

The companion ``provider_credentials.kind`` column already stores its
enum as VARCHAR with no DB-level CHECK constraint (see
:class:`app.db.models.ProviderCredential.kind` — ``native_enum=False``,
no CHECK is generated), so adding the new ``"s2s"`` kind requires no
schema change. The enum widening is purely Python-side.

Reversible (``downgrade`` drops the table cleanly) and re-runnable
(idempotent against a half-applied state via the inspector check).

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-07 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Allowed values for pipeline_settings.pipeline_mode. Keep in sync with
# :class:`app.db.models.PipelineMode`.
PIPELINE_MODES = ("split", "unified")


def _in_list(column: str, values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _table_names(inspector: sa.Inspector) -> set[str]:
    return set(inspector.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "pipeline_settings" in _table_names(inspector):
        return

    op.create_table(
        "pipeline_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "pipeline_mode",
            sa.String(length=16),
            nullable=False,
            server_default="split",
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
        sa.CheckConstraint("id = 1", name="ck_pipeline_settings_singleton"),
        sa.CheckConstraint(
            _in_list("pipeline_mode", PIPELINE_MODES),
            name="ck_pipeline_settings_mode",
        ),
    )

    # Seed the singleton row so the API never has to handle a missing row.
    op.execute(
        "INSERT INTO pipeline_settings (id, pipeline_mode) "
        "VALUES (1, 'split')"
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "pipeline_settings" not in _table_names(inspector):
        return
    op.drop_table("pipeline_settings")
