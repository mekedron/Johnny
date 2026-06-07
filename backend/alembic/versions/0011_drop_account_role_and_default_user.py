"""Drop ``role`` enum + ``is_default_user`` from ``google_accounts``; allow nullable refresh token (Johnny-pia).

The accounts redesign collapses the calendar-account / bot-identity split
to a single row per Google identity. Capability is derived rather than
declared:

* Calendar source — ``refresh_token_encrypted IS NOT NULL`` and decryptable.
* Bot identity — Playwright ``storage_state.json`` exists at the per-account
  path on the ``google_auth_state`` volume.

That makes the ``role`` column meaningless (every row could in principle
carry both capabilities) and removes the only reason ``is_default_user``
existed (a UI ordering hint that no critical path actually consulted).

This migration:

1. Drops the ``ck_google_accounts_role`` CHECK constraint and the ``role``
   column.
2. Drops the ``is_default_user`` column.
3. Removes ``NOT NULL`` from ``refresh_token_encrypted`` so bot-only rows
   (created by the noVNC sign-in flow, no OAuth tokens) can persist.

Existing rows survive unchanged: a former ``role='bot'`` row keeps any
refresh token it picked up via OAuth (now carries both capabilities); a
former ``role='user'`` row without a storage_state file simply has no bot
capability.

Downgrade restores the columns + constraint and back-fills role='user'
plus is_default_user=false for every row so the NOT NULL constraints
hold. The migration is destructive in one direction (the original
intent of each row is forgotten on upgrade) — the downgrade is
best-effort, not lossless.

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-07 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CHECK_CONSTRAINT_NAME = "ck_google_accounts_role"
ACCOUNT_ROLES = ("user", "bot")


def _check_constraint_names(inspector: sa.Inspector, table: str) -> set[str]:
    try:
        return {c["name"] for c in inspector.get_check_constraints(table)}
    except (NotImplementedError, AttributeError):  # pragma: no cover
        return {CHECK_CONSTRAINT_NAME}


def _in_list(column: str, values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    existing = _check_constraint_names(inspector, "google_accounts")
    if CHECK_CONSTRAINT_NAME in existing:
        op.drop_constraint(
            CHECK_CONSTRAINT_NAME, "google_accounts", type_="check"
        )

    op.drop_column("google_accounts", "role")
    op.drop_column("google_accounts", "is_default_user")
    op.alter_column(
        "google_accounts",
        "refresh_token_encrypted",
        existing_type=sa.Text(),
        nullable=True,
    )


def downgrade() -> None:
    # Re-add the columns with back-fill defaults so existing rows pass
    # the restored NOT NULL constraints. The original role/default
    # intent is unrecoverable; every existing row gets role='user',
    # is_default_user=false. Best-effort only.
    op.alter_column(
        "google_accounts",
        "refresh_token_encrypted",
        existing_type=sa.Text(),
        nullable=False,
    )
    op.add_column(
        "google_accounts",
        sa.Column(
            "is_default_user",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "google_accounts",
        sa.Column(
            "role",
            sa.String(length=16),
            nullable=False,
            server_default="user",
        ),
    )
    op.create_check_constraint(
        CHECK_CONSTRAINT_NAME,
        "google_accounts",
        _in_list("role", ACCOUNT_ROLES),
    )
    # Drop server defaults — production rows pick role/default explicitly.
    op.alter_column("google_accounts", "role", server_default=None)
    op.alter_column(
        "google_accounts", "is_default_user", server_default=None
    )
