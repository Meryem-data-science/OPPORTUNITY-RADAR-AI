"""Tests for the configured read-only opportunity listing CLI."""

import sqlite3

import pytest

from services.collector.cli import list_opportunities
from services.collector.config import ApplicationEnvironment, DatabaseBackend, Settings
from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.database.opportunities import persist_opportunities
from tests.unit.test_opportunity_persistence import candidate, source


def sqlite_settings(path) -> Settings:
    return Settings(
        environment=ApplicationEnvironment.TEST,
        database_backend=DatabaseBackend.SQLITE,
        sqlite_database_path=path,
    )


@pytest.mark.parametrize("value", ["0", "-1", "101", "invalid"])
def test_listing_limit_is_bounded(value) -> None:
    with pytest.raises(SystemExit):
        list_opportunities.parse_args(["--limit", value])


def test_empty_database_lists_zero_without_writing(
    tmp_path, monkeypatch, capsys
) -> None:
    path = tmp_path / "empty.db"
    connection = connect_database(path)
    try:
        apply_migrations(connection)
    finally:
        connection.close()
    monkeypatch.setattr(
        list_opportunities, "load_settings", lambda: sqlite_settings(path)
    )

    assert list_opportunities.list_opportunities(5) == []
    assert capsys.readouterr().out == "0 opportunities\n"
    connection = sqlite3.connect(path)
    try:
        assert (
            connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 0
        )
    finally:
        connection.close()


def test_listing_displays_real_fields_without_description(
    tmp_path, monkeypatch, capsys
) -> None:
    path = tmp_path / "listed.db"
    connection = connect_database(path)
    try:
        apply_migrations(connection)
        persist_opportunities(
            connection, source(), [candidate(description="é" * 12_001)]
        )
    finally:
        connection.close()
    monkeypatch.setattr(
        list_opportunities, "load_settings", lambda: sqlite_settings(path)
    )

    rows = list_opportunities.list_opportunities(1)

    output = capsys.readouterr().out
    assert len(rows) == 1
    assert rows[0]["canonical_title"] == "TEST ONLY Engineer"
    assert rows[0]["source_url"] == "https://example.invalid/jobs/TEST-1"
    assert rows[0]["description_length"] == 12_001
    assert "é" * 100 not in output
    assert output.endswith("1 opportunities\n")
