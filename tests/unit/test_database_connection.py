"""Tests for backend-specific connection selection."""

import sqlite3

import pytest

from services.collector.config import (
    ApplicationEnvironment,
    DatabaseBackend,
    Settings,
)
from services.collector.database import turso
from services.collector.database.connection import connect_configured_database
from services.collector.database.turso import (
    DatabaseConnectionError,
    TursoHttpConnection,
)


def test_factory_selects_sqlite_and_connection_closes(tmp_path) -> None:
    settings = Settings(
        environment=ApplicationEnvironment.TEST,
        database_backend=DatabaseBackend.SQLITE,
        sqlite_database_path=tmp_path / "connection.db",
    )

    connection = connect_configured_database(settings)

    assert isinstance(connection, sqlite3.Connection)
    assert connection.execute("SELECT 1").fetchone() == (1,)

    connection.close()

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_sqlite_connection_creates_missing_parent_directory(tmp_path) -> None:
    database_path = tmp_path / "missing" / "nested" / "connection.db"

    assert not database_path.parent.exists()

    settings = Settings(
        environment=ApplicationEnvironment.TEST,
        database_backend=DatabaseBackend.SQLITE,
        sqlite_database_path=database_path,
    )

    connection = connect_configured_database(settings)

    try:
        assert database_path.parent.is_dir()
        assert database_path.is_file()
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    finally:
        connection.close()


def test_factory_selects_turso_http_adapter(monkeypatch) -> None:
    expected_client = object()
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        turso.httpx,
        "Client",
        lambda **kwargs: calls.append(kwargs) or expected_client,
    )

    settings = Settings(
        environment=ApplicationEnvironment.TEST,
        database_backend=DatabaseBackend.TURSO,
        turso_database_url="libsql://fixture.invalid",
        turso_auth_token="TEST_ONLY_SECRET",
    )

    connection = connect_configured_database(settings)

    assert isinstance(connection, TursoHttpConnection)
    assert connection._endpoint == "https://fixture.invalid/v2/pipeline"
    assert calls[0]["headers"] == {"Authorization": "Bearer TEST_ONLY_SECRET"}
    assert isinstance(calls[0]["timeout"], turso.httpx.Timeout)


def test_turso_connection_error_does_not_expose_secret() -> None:
    secret = "TEST_ONLY_SECRET"

    settings = Settings(
        environment=ApplicationEnvironment.TEST,
        database_backend=DatabaseBackend.TURSO,
        turso_database_url="libsql://fixture.invalid",
        turso_auth_token=secret,
    )

    connection = connect_configured_database(settings)

    assert secret not in repr(connection)
    connection.close()


def test_invalid_turso_url_does_not_affect_sqlite(tmp_path) -> None:
    sqlite_settings = Settings(
        environment=ApplicationEnvironment.TEST,
        database_backend=DatabaseBackend.SQLITE,
        sqlite_database_path=tmp_path / "connection.db",
    )

    turso_settings = Settings(
        environment=ApplicationEnvironment.TEST,
        database_backend=DatabaseBackend.TURSO,
        turso_database_url="https://fixture.invalid",
        turso_auth_token="TEST_ONLY_SECRET",
    )

    sqlite_connection = connect_configured_database(sqlite_settings)
    sqlite_connection.close()

    with pytest.raises(DatabaseConnectionError, match="libsql"):
        connect_configured_database(turso_settings)
