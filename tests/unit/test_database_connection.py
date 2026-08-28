"""Tests for backend-specific connection selection."""

import sqlite3

import pytest

from services.collector.config import (
    ApplicationEnvironment,
    DatabaseBackend,
    Settings,
)
from services.collector.database import turso
from services.collector.database.connection import (
    connect_configured_database,
    connect_readonly_database,
)
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


def test_readonly_connection_reads_an_existing_database(tmp_path) -> None:
    database_path = tmp_path / "readable.db"
    writable = sqlite3.connect(database_path)
    try:
        writable.execute("CREATE TABLE sample (id INTEGER)")
        writable.execute("INSERT INTO sample (id) VALUES (7)")
        writable.commit()
    finally:
        writable.close()

    connection = connect_readonly_database(database_path)
    try:
        assert connection.execute("SELECT id FROM sample").fetchone() == (7,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO sample (id) VALUES (8)",
        "UPDATE sample SET id = 9",
        "DELETE FROM sample",
        "CREATE TABLE injected (id INTEGER)",
    ],
)
def test_readonly_connection_refuses_every_write(tmp_path, statement: str) -> None:
    database_path = tmp_path / "readable.db"
    writable = sqlite3.connect(database_path)
    try:
        writable.execute("CREATE TABLE sample (id INTEGER)")
        writable.execute("INSERT INTO sample (id) VALUES (7)")
        writable.commit()
    finally:
        writable.close()

    connection = connect_readonly_database(database_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(statement)
    finally:
        connection.close()

    check = sqlite3.connect(database_path)
    try:
        assert check.execute("SELECT id FROM sample").fetchall() == [(7,)]
    finally:
        check.close()


def test_readonly_connection_never_creates_a_missing_database(tmp_path) -> None:
    """Unlike `connect_database`, it creates neither the file nor its directory."""
    database_path = tmp_path / "missing" / "nested" / "absent.db"

    with pytest.raises(sqlite3.OperationalError):
        connect_readonly_database(database_path)

    assert database_path.exists() is False
    assert database_path.parent.exists() is False
    assert (tmp_path / "missing").exists() is False
    assert list(tmp_path.iterdir()) == []


def test_readonly_connection_accepts_a_path_with_uri_punctuation(tmp_path) -> None:
    """A legal path is a path, not URI syntax to be reinterpreted."""
    database_path = tmp_path / "odd name?with#punctuation.db"
    writable = sqlite3.connect(database_path)
    try:
        writable.execute("CREATE TABLE sample (id INTEGER)")
        writable.execute("INSERT INTO sample (id) VALUES (3)")
        writable.commit()
    finally:
        writable.close()

    connection = connect_readonly_database(database_path)
    try:
        assert connection.execute("SELECT id FROM sample").fetchone() == (3,)
    finally:
        connection.close()
