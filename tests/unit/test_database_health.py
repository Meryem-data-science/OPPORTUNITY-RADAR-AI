"""Tests for the backend-neutral database healthcheck."""

import sqlite3

from services.collector.database.connection import connect_database
from services.collector.database.health import check_database_health


def test_sqlite_healthcheck_uses_select_one_without_creating_tables(tmp_path) -> None:
    database = tmp_path / "health.db"
    connection = connect_database(database)

    assert check_database_health(connection) is True
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    connection.close()

    assert tables == []


def test_healthcheck_rejects_unexpected_result() -> None:
    class UnexpectedCursor:
        def fetchone(self) -> tuple[int]:
            return (0,)

    class UnexpectedConnection:
        def execute(self, statement: str) -> UnexpectedCursor:
            assert statement == "SELECT 1"
            return UnexpectedCursor()

    assert check_database_health(UnexpectedConnection()) is False
