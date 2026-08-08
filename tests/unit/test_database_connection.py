"""Tests for backend-specific connection selection."""

import sqlite3
from types import SimpleNamespace

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
    DatabaseDependencyError,
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


def test_factory_selects_turso_and_passes_validated_credentials(monkeypatch) -> None:
    calls: list[dict[str, str]] = []
    expected_connection = object()
    fake_libsql = SimpleNamespace(
        connect=lambda **kwargs: calls.append(kwargs) or expected_connection
    )
    monkeypatch.setattr(turso, "import_module", lambda name: fake_libsql)
    settings = Settings(
        environment=ApplicationEnvironment.TEST,
        database_backend=DatabaseBackend.TURSO,
        turso_database_url="libsql://fixture.invalid",
        turso_auth_token="TEST_ONLY_SECRET",
    )

    connection = connect_configured_database(settings)

    assert connection is expected_connection
    assert calls == [
        {
            "database": "libsql://fixture.invalid",
            "auth_token": "TEST_ONLY_SECRET",
        }
    ]


def test_turso_connection_error_does_not_expose_secret(monkeypatch) -> None:
    secret = "TEST_ONLY_SECRET"

    def fail_connection(**kwargs) -> None:
        raise RuntimeError(f"Driver failed with {kwargs['auth_token']}")

    monkeypatch.setattr(
        turso,
        "import_module",
        lambda name: SimpleNamespace(connect=fail_connection),
    )
    settings = Settings(
        environment=ApplicationEnvironment.TEST,
        database_backend=DatabaseBackend.TURSO,
        turso_database_url="libsql://fixture.invalid",
        turso_auth_token=secret,
    )

    with pytest.raises(DatabaseConnectionError) as error:
        connect_configured_database(settings)

    assert secret not in str(error.value)


def test_missing_libsql_only_blocks_turso(monkeypatch, tmp_path) -> None:
    def missing_libsql(name: str) -> None:
        raise ModuleNotFoundError("No module named 'libsql'", name="libsql")

    monkeypatch.setattr(turso, "import_module", missing_libsql)
    sqlite_settings = Settings(
        environment=ApplicationEnvironment.TEST,
        database_backend=DatabaseBackend.SQLITE,
        sqlite_database_path=tmp_path / "connection.db",
    )
    turso_settings = Settings(
        environment=ApplicationEnvironment.TEST,
        database_backend=DatabaseBackend.TURSO,
        turso_database_url="libsql://fixture.invalid",
        turso_auth_token="TEST_ONLY_SECRET",
    )

    sqlite_connection = connect_configured_database(sqlite_settings)
    sqlite_connection.close()

    with pytest.raises(DatabaseDependencyError, match="libsql"):
        connect_configured_database(turso_settings)
