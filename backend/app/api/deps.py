"""Shared FastAPI dependencies: DB session, credential crypto."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.security.crypto import CredentialCrypto
from app.security.crypto import get_crypto as _get_crypto


def get_session() -> Iterator[Session]:
    """Yield a request-scoped SQLAlchemy session, committing on success."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_crypto() -> CredentialCrypto:
    """Return the application-wide :class:`CredentialCrypto` instance."""
    return _get_crypto()


__all__ = ["get_crypto", "get_session"]
