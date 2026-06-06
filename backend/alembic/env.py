"""Alembic environment script.

Reads the database URL from app settings (which in turn reads `DATABASE_URL`
from the environment) rather than from `alembic.ini`. This keeps a single
source of truth for connection config.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.config import get_settings
from app.db import (
    Base,
    models,  # noqa: F401  ensure models register with Base.metadata
)

config = context.config

# When called programmatically (``command.upgrade`` from inside the API /
# worker process via ``app.db.bootstrap``) the caller has already
# configured its own logging — and a fileConfig() pass here would
# silently downgrade the root logger to WARNING (Johnny-ckz.9). The
# bootstrap sets ``preserve_caller_logging`` on the config to opt out.
if config.config_file_name is not None and not config.attributes.get(
    "preserve_caller_logging"
):
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = config.get_main_option("sqlalchemy.url") or ""
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
