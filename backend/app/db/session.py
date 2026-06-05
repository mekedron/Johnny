"""SQLAlchemy engine and session factory.

`create_engine` is lazy; it does not connect at import time, so importing this
module from tests without a live PostgreSQL instance is safe.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def _build_engine() -> Engine:
    settings = get_settings()
    return create_engine(settings.database_url, future=True, pool_pre_ping=True)


engine: Engine = _build_engine()
SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, future=True
)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Yield a transactional session; commit on success, rollback on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
