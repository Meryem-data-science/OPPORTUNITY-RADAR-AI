"""Tests for read-only foundation schema verification."""

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.database.schema import check_foundation_schema


def test_foundation_schema_is_absent_before_migrations(tmp_path) -> None:
    connection = connect_database(tmp_path / "schema.db")
    try:
        assert check_foundation_schema(connection) is False
    finally:
        connection.close()


def test_foundation_schema_is_present_after_migrations(tmp_path) -> None:
    connection = connect_database(tmp_path / "schema.db")
    try:
        apply_migrations(connection)
        assert check_foundation_schema(connection) is True
    finally:
        connection.close()
