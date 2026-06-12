"""Remove the S2S/unified pipeline surface (Johnny-trt.43).

The product ships split-only: Johnny-trt.43 removed the unified (S2S)
pipeline and the ``s2s`` provider kind from the application (providers
unregistered, ``ProviderKind.S2S`` deleted, ``UnifiedVoicePipeline`` and
its wiring gone). Re-introduction is deferred to epic Johnny-20h; the
pre-removal code lives at git SHA
``fc16a1e785595ff2fd1db6d60b56f07711c5ddae`` (tombstone in
``docs/PIPELINE.md``).

Two DB-side consequences handled here:

* ``pipeline_settings`` (the split/unified singleton toggle from 0009) is
  dropped — nothing reads it any more.
* Existing ``provider_credentials`` rows with ``kind='s2s'`` are
  **deactivated, not deleted**: their encrypted credentials remain as a
  historical record, but an active row would otherwise be invisible-yet-
  active (the active-per-kind partial index would keep blocking nothing,
  and nothing can ever load it again). Each deactivated row is logged so
  the operator sees exactly what happened instead of a silent orphan.
  Application queries exclude the ``s2s`` kind everywhere, so the rows
  are inert. The widened CHECK from 0010 is intentionally left in place —
  narrowing it would fail against these historical rows.

Reversible only structurally: ``downgrade`` recreates ``pipeline_settings``
(as 0009 built it) but cannot know which s2s row used to be active, so
rows stay inactive — reactivation is a deliberate operator action.

Revision ID: 0026
Revises: 0025
Create Date: 2026-06-12 01:00:00.000000
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")


def _table_names(inspector: sa.Inspector) -> set[str]:
    return set(inspector.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. Deactivate historical s2s provider rows — explicitly and loudly.
    rows = bind.execute(
        sa.text(
            "SELECT id, provider_name, display_name, is_active "
            "FROM provider_credentials WHERE kind = 's2s'"
        )
    ).fetchall()
    for row in rows:
        logger.warning(
            "Johnny-trt.43: deactivating s2s provider row id=%s (%s / %r, "
            "was_active=%s) — the S2S pipeline was removed from the product; "
            "credentials are preserved on the row but it will never be "
            "loaded again (re-introduction tracked in Johnny-20h)",
            row.id,
            row.provider_name,
            row.display_name,
            row.is_active,
        )
    if rows:
        bind.execute(
            sa.text(
                "UPDATE provider_credentials SET is_active = false "
                "WHERE kind = 's2s'"
            )
        )

    # 2. Drop the split/unified toggle table.
    if "pipeline_settings" in _table_names(inspector):
        op.drop_table("pipeline_settings")


def downgrade() -> None:
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
            "pipeline_mode IN ('split', 'unified')",
            name="ck_pipeline_settings_mode",
        ),
    )
    op.execute(
        "INSERT INTO pipeline_settings (id, pipeline_mode) VALUES (1, 'split')"
    )
