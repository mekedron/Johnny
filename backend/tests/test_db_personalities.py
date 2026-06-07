"""Model-shape tests for the ``personalities`` table (Johnny-oly.2).

Asserts the declarative metadata is well-formed — columns, the two
``SET NULL`` provider FKs, the ``display_name`` unique constraint, and the
``is_default`` partial unique index — without requiring a live database.
"""

from __future__ import annotations

from app.db import Base
from app.db.models import Personality


def test_personalities_table_registered() -> None:
    assert "personalities" in Base.metadata.tables


def test_columns_present() -> None:
    table = Base.metadata.tables["personalities"]
    columns = {c.name for c in table.columns}
    assert {
        "id",
        "display_name",
        "description",
        "is_default",
        "llm_provider_id",
        "tts_provider_id",
        "default_mode",
        "metadata",
        "created_at",
        "updated_at",
    } == columns


def test_metadata_column_maps_to_extra_metadata_attr() -> None:
    """DB column is the clean ``metadata``; the ORM attribute is renamed."""
    assert Personality.extra_metadata.property.columns[0].name == "metadata"


def test_provider_fks_are_set_null() -> None:
    table = Base.metadata.tables["personalities"]
    for col_name in ("llm_provider_id", "tts_provider_id"):
        fks = list(table.columns[col_name].foreign_keys)
        assert len(fks) == 1
        fk = fks[0]
        assert fk.column.table.name == "provider_credentials"
        assert fk.column.name == "id"
        assert fk.ondelete == "SET NULL"


def test_display_name_unique_constraint() -> None:
    table = Base.metadata.tables["personalities"]
    uniques = {
        c.name
        for c in table.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    }
    assert "uq_personalities_display_name" in uniques


def test_single_default_partial_unique_index() -> None:
    table = Base.metadata.tables["personalities"]
    idx = next(
        i for i in table.indexes if i.name == "uq_personalities_single_default"
    )
    assert idx.unique is True
    assert {c.name for c in idx.columns} == {"is_default"}
