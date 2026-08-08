"""Unit tests for database connections and SQL migrations."""

import sqlite3

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations


EXPECTED_TABLES = {
    "schema_migrations",
    "sources",
    "opportunities",
    "opportunity_sources",
}


def test_empty_database_receives_foundation_schema(tmp_path) -> None:
    with connect_database(tmp_path / "unit.db") as connection:
        applied = apply_migrations(connection)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        recorded = connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()

    assert applied == ["0001"]
    assert EXPECTED_TABLES <= tables
    assert recorded == [("0001",)]


def test_migrations_are_idempotent_and_do_not_seed_data(tmp_path) -> None:
    with connect_database(tmp_path / "unit.db") as connection:
        assert apply_migrations(connection) == ["0001"]
        assert apply_migrations(connection) == []

        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("sources", "opportunities", "opportunity_sources")
        }
        migration_count = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]

    assert counts == {"sources": 0, "opportunities": 0, "opportunity_sources": 0}
    assert migration_count == 1


def test_opportunity_requires_source_url(tmp_path) -> None:
    with connect_database(tmp_path / "unit.db") as connection:
        apply_migrations(connection)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO opportunities (
                    canonical_title, organization, discovered_at, first_seen_at,
                    last_seen_at, status
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("Test role", "Test organization", "2026-01-01", "2026-01-01", "2026-01-01", "new"),
            )


def test_opportunity_source_foreign_keys_are_enforced(tmp_path) -> None:
    with connect_database(tmp_path / "unit.db") as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        apply_migrations(connection)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO opportunity_sources (
                    opportunity_id, source_id, source_url, discovered_at
                ) VALUES (?, ?, ?, ?)
                """,
                (999, "missing-source", "https://test.invalid/item", "2026-01-01"),
            )


def test_failed_migration_is_rolled_back_and_not_recorded(tmp_path) -> None:
    migrations_directory = tmp_path / "migrations"
    migrations_directory.mkdir()
    (migrations_directory / "0002_invalid.sql").write_text(
        "CREATE TABLE partial_table (id INTEGER);\nINVALID SQL;",
        encoding="utf-8",
    )

    with connect_database(tmp_path / "unit.db") as connection:
        with pytest.raises(sqlite3.Error):
            apply_migrations(connection, migrations_directory)

        recorded = connection.execute(
            "SELECT version FROM schema_migrations WHERE version = '0002'"
        ).fetchall()
        partial_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'partial_table'"
        ).fetchall()

    assert recorded == []
    assert partial_table == []
