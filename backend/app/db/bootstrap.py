"""Boot-time database migration runner + drift guard (Johnny-ckz.9).

Called from the API/worker process entry so that:

1. ``alembic upgrade head`` runs against the live DB before any code
   queries an ORM-mapped table — that keeps a freshly-built image
   from booting against an outdated schema and silently 500-ing on
   the first ``SELECT``.
2. After the upgrade, the live schema is diffed against
   :data:`app.db.Base.metadata`. If the ORM expects a column that
   the DB does not have, the process aborts with
   :class:`SchemaDriftError`. The point of the abort is to turn a
   future "model change without a matching migration" mistake into
   a loud, boot-time crash instead of an opaque ``UndefinedColumn``
   on the first request.

Opt out with ``JOHNNY_DB_BOOTSTRAP=off`` so the in-process pytest
suite (which manages its own schema via ``Base.metadata.create_all``)
does not need a running Postgres just to import the app.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, inspect

from alembic import command
from app.db import Base
from app.db.session import engine as default_engine

logger = logging.getLogger(__name__)

_ENV_VAR = "JOHNNY_DB_BOOTSTRAP"
_DISABLED_VALUES = frozenset({"0", "off", "false", "no"})

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _BACKEND_DIR / "alembic.ini"
_ALEMBIC_SCRIPTS = _BACKEND_DIR / "alembic"


class SchemaDriftError(RuntimeError):
    """Raised when the live DB lacks one or more ORM-mapped columns/tables."""


def _enabled() -> bool:
    value = os.environ.get(_ENV_VAR, "on").strip().lower()
    return value not in _DISABLED_VALUES


def _alembic_config(database_url: str | None = None) -> AlembicConfig:
    if not _ALEMBIC_INI.exists():
        raise RuntimeError(
            f"alembic.ini not found at {_ALEMBIC_INI}; cannot run migrations"
        )
    cfg = AlembicConfig(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_ALEMBIC_SCRIPTS))
    if database_url is not None:
        cfg.set_main_option("sqlalchemy.url", database_url)
    # Tell alembic/env.py to skip its fileConfig() pass. Without this,
    # alembic.ini's [logger_root] level=WARNING would mute every INFO
    # log the worker / lifespan emits after bootstrap (Johnny-ckz.9).
    cfg.attributes["preserve_caller_logging"] = True
    return cfg


def run_migrations(database_url: str | None = None) -> None:
    """Upgrade the live database to the head Alembic revision."""
    cfg = _alembic_config(database_url)
    logger.info("alembic upgrade head — running")
    command.upgrade(cfg, "head")
    logger.info("alembic upgrade head — complete")


def check_model_db_drift(engine: Engine | None = None) -> None:
    """Abort boot if any ORM-mapped column or table is missing from the DB.

    Compares every table in :data:`Base.metadata` with what
    :class:`sqlalchemy.engine.reflection.Inspector` reports. Anything
    the ORM expects but the DB does not have is reported in a single
    :class:`SchemaDriftError` so the operator sees the full picture,
    not just the first column.
    """
    eng = engine or default_engine
    inspector = inspect(eng)
    db_tables = set(inspector.get_table_names())
    problems: list[str] = []
    for table_name, table in Base.metadata.tables.items():
        if table_name not in db_tables:
            problems.append(f"table {table_name!r} missing from DB")
            continue
        db_columns = {col["name"] for col in inspector.get_columns(table_name)}
        missing = sorted(c.name for c in table.columns if c.name not in db_columns)
        if missing:
            problems.append(
                f"table {table_name!r} missing columns: {', '.join(missing)}"
            )
    if problems:
        joined = "\n  - ".join(problems)
        raise SchemaDriftError(
            "Model/DB schema drift detected — boot aborted to avoid runtime "
            f"UndefinedColumn errors:\n  - {joined}"
        )


def bootstrap(engine: Engine | None = None) -> None:
    """Run :func:`run_migrations` then :func:`check_model_db_drift`.

    Either step may raise; the caller (lifespan / worker main) should
    let the exception propagate so the process exits non-zero instead
    of booting against a broken schema.
    """
    if not _enabled():
        logger.info(
            "%s is set to disabled; skipping migrations + drift check", _ENV_VAR
        )
        return
    run_migrations()
    check_model_db_drift(engine)


__all__ = [
    "SchemaDriftError",
    "bootstrap",
    "check_model_db_drift",
    "run_migrations",
]
