"""Widen ``provider_credentials.kind`` CHECK to include ``s2s`` (Johnny-ckz.21).

Johnny-ckz.17 widened the Python ``ProviderKind`` enum to include a
fourth value (``s2s``) for unified speech-to-speech providers, but the
existing DB CHECK constraint ``ck_provider_credentials_kind`` from
migration 0001 still only allowed ``stt|llm|tts``. Inserting an S2S row
therefore failed with an IntegrityError, which the API surfaced as
the misleading "display name already exists" — the catch-all branch for
``IntegrityError`` on insert.

This migration drops the existing constraint and recreates it with the
widened value list. Reversible: the downgrade restores the original
3-value constraint (and would fail if any s2s rows exist at that point —
correct, since downgrading the column behaviour past existing data must
be a noisy failure rather than silent data loss).

Re-runnable via inspector-based existence checks so a half-applied
state doesn't error.

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-07 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OLD_PROVIDER_KINDS = ("stt", "llm", "tts")
NEW_PROVIDER_KINDS = ("stt", "llm", "tts", "s2s")
CONSTRAINT_NAME = "ck_provider_credentials_kind"


def _in_list(column: str, values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _check_constraint_names(inspector: sa.Inspector, table: str) -> set[str]:
    try:
        return {c["name"] for c in inspector.get_check_constraints(table)}
    except (NotImplementedError, AttributeError):  # pragma: no cover
        # SQLite reflection may not return CHECK metadata; assume present.
        return {CONSTRAINT_NAME}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = _check_constraint_names(inspector, "provider_credentials")
    if CONSTRAINT_NAME in existing:
        op.drop_constraint(
            CONSTRAINT_NAME, "provider_credentials", type_="check"
        )
    op.create_check_constraint(
        CONSTRAINT_NAME,
        "provider_credentials",
        _in_list("kind", NEW_PROVIDER_KINDS),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = _check_constraint_names(inspector, "provider_credentials")
    if CONSTRAINT_NAME in existing:
        op.drop_constraint(
            CONSTRAINT_NAME, "provider_credentials", type_="check"
        )
    op.create_check_constraint(
        CONSTRAINT_NAME,
        "provider_credentials",
        _in_list("kind", OLD_PROVIDER_KINDS),
    )
