"""SQLAlchemy declarative base for all ORM models."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base; all ORM models inherit from this."""
