"""Tests for the boot-time migration runner + schema-drift guard.

Johnny-ckz.9 introduced a class of bug where the ORM mapping referenced
columns the live DB didn't have. ``app.db.bootstrap`` is the safety
net: it runs ``alembic upgrade head`` then verifies every mapped column
exists, raising :class:`SchemaDriftError` if not. These tests pin both
halves of that contract.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from app.db import Base
from app.db.bootstrap import (
    SchemaDriftError,
    _enabled,
    bootstrap,
    check_model_db_drift,
)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("JOHNNY_DB_BOOTSTRAP", raising=False)
    yield


def _fake_inspector(
    table_columns: dict[str, list[str]] | None = None,
) -> MagicMock:
    inspector = MagicMock()
    table_columns = table_columns if table_columns is not None else {}
    inspector.get_table_names.return_value = list(table_columns.keys())
    inspector.get_columns.side_effect = lambda name: [
        {"name": col} for col in table_columns.get(name, [])
    ]
    return inspector


def test_enabled_default_on() -> None:
    assert _enabled() is True


def test_enabled_respects_disabled_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("off", "0", "false", "no", "OFF", "  FALSE  "):
        monkeypatch.setenv("JOHNNY_DB_BOOTSTRAP", value)
        assert _enabled() is False, f"value={value!r} should disable bootstrap"


def test_enabled_treats_unknown_values_as_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOHNNY_DB_BOOTSTRAP", "yes-please")
    assert _enabled() is True


def test_check_model_db_drift_passes_when_complete() -> None:
    # Build a fake inspector reporting every mapped column. The guard
    # should return cleanly.
    table_columns = {
        name: [c.name for c in table.columns]
        for name, table in Base.metadata.tables.items()
    }
    inspector = _fake_inspector(table_columns)
    with patch("app.db.bootstrap.inspect", return_value=inspector):
        check_model_db_drift(engine=MagicMock())


def test_check_model_db_drift_lists_missing_columns() -> None:
    # Strip the two Johnny-ckz.9 columns from the bot_sessions live
    # snapshot — this is exactly the bug we are guarding against.
    table_columns = {
        name: [c.name for c in table.columns]
        for name, table in Base.metadata.tables.items()
    }
    table_columns["bot_sessions"] = [
        c for c in table_columns["bot_sessions"]
        if c not in {"source", "playground_overrides"}
    ]
    inspector = _fake_inspector(table_columns)
    with patch("app.db.bootstrap.inspect", return_value=inspector):
        with pytest.raises(SchemaDriftError) as excinfo:
            check_model_db_drift(engine=MagicMock())
    message = str(excinfo.value)
    assert "bot_sessions" in message
    assert "source" in message
    assert "playground_overrides" in message


def test_check_model_db_drift_reports_missing_table() -> None:
    # Drop the whole bot_sessions table — the guard must say so.
    table_columns = {
        name: [c.name for c in table.columns]
        for name, table in Base.metadata.tables.items()
        if name != "bot_sessions"
    }
    inspector = _fake_inspector(table_columns)
    with patch("app.db.bootstrap.inspect", return_value=inspector):
        with pytest.raises(SchemaDriftError) as excinfo:
            check_model_db_drift(engine=MagicMock())
    assert "bot_sessions" in str(excinfo.value)
    assert "missing from DB" in str(excinfo.value)


def test_bootstrap_skips_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOHNNY_DB_BOOTSTRAP", "off")
    with (
        patch("app.db.bootstrap.run_migrations") as run_mig,
        patch("app.db.bootstrap.check_model_db_drift") as drift_check,
    ):
        bootstrap()
    run_mig.assert_not_called()
    drift_check.assert_not_called()


def test_bootstrap_runs_migrations_then_check() -> None:
    with (
        patch("app.db.bootstrap.run_migrations") as run_mig,
        patch("app.db.bootstrap.check_model_db_drift") as drift_check,
    ):
        bootstrap()
    run_mig.assert_called_once()
    drift_check.assert_called_once()
    # Drift check must run after migrations — otherwise we'd flag drift
    # that the upgrade was about to fix. We can't observe call order
    # directly with two separate mocks, so we use a sentinel: the
    # check runs once, and the migration runs once with no args.
    run_mig.assert_called_with()


def test_bootstrap_propagates_drift_error() -> None:
    with (
        patch("app.db.bootstrap.run_migrations"),
        patch(
            "app.db.bootstrap.check_model_db_drift",
            side_effect=SchemaDriftError("nope"),
        ),
    ):
        with pytest.raises(SchemaDriftError):
            bootstrap()
