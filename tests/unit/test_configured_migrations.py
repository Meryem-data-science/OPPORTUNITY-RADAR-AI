"""Tests for generic migration transactions and remote protection."""

from pathlib import Path

import pytest

from services.collector.cli import migrate_configured
from services.collector.config import ApplicationEnvironment, DatabaseBackend, Settings
from services.collector.database.migrations import MigrationError, apply_migrations


class Cursor:
    def __init__(self, rows=()) -> None:
        self.rows = list(rows)

    def fetchall(self) -> list[tuple[str]]:
        return self.rows


class RecordingConnection:
    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.fail_on = fail_on

    def execute(self, statement: str, *parameters: object) -> Cursor:
        normalized = statement.strip()
        self.calls.append((normalized, parameters))
        if self.fail_on and self.fail_on in normalized:
            raise RuntimeError("Test statement failure")
        return Cursor()

    def close(self) -> None:
        self.calls.append(("CLOSE", ()))


def _write_migration(directory: Path) -> None:
    (directory / "0001_test.sql").write_text(
        "CREATE TABLE first_table (id INTEGER);\n"
        "CREATE TABLE second_table (id INTEGER);\n",
        encoding="utf-8",
    )


def test_generic_connection_executes_transaction_in_order(tmp_path) -> None:
    _write_migration(tmp_path)
    connection = RecordingConnection()

    assert apply_migrations(connection, tmp_path) == ["0001"]

    statements = [call[0] for call in connection.calls]
    assert statements[-5:] == [
        "BEGIN",
        "CREATE TABLE first_table (id INTEGER);",
        "CREATE TABLE second_table (id INTEGER);",
        "INSERT INTO schema_migrations (version) VALUES (?)",
        "COMMIT",
    ]
    assert connection.calls[-2][1] == (("0001",),)


def test_generic_connection_rolls_back_without_recording_version(tmp_path) -> None:
    _write_migration(tmp_path)
    connection = RecordingConnection(fail_on="second_table")

    with pytest.raises(MigrationError):
        apply_migrations(connection, tmp_path)

    statements = [call[0] for call in connection.calls]
    assert statements[-1] == "ROLLBACK"
    assert "INSERT INTO schema_migrations (version) VALUES (?)" not in statements


def test_turso_cli_refuses_write_before_connecting(monkeypatch) -> None:
    settings = Settings(
        environment=ApplicationEnvironment.TEST,
        database_backend=DatabaseBackend.TURSO,
        turso_database_url="libsql://fixture.invalid",
        turso_auth_token="TEST_ONLY_SECRET",
    )
    connection_attempted = False

    def unexpected_connection(settings: Settings) -> RecordingConnection:
        nonlocal connection_attempted
        connection_attempted = True
        return RecordingConnection()

    monkeypatch.delenv("RUN_TURSO_LIVE_MIGRATION", raising=False)
    monkeypatch.setattr(migrate_configured, "load_settings", lambda: settings)
    monkeypatch.setattr(
        migrate_configured,
        "connect_configured_database",
        unexpected_connection,
    )

    assert migrate_configured.main(["--apply"]) == 1
    assert connection_attempted is False
