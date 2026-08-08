"""Database connection factory and SQLite connection helpers."""

import sqlite3
from pathlib import Path
from typing import Any, Protocol

from services.collector.config import DatabaseBackend, Settings
from services.collector.database.turso import DatabaseConnectionError, connect_turso


class DatabaseConnection(Protocol):
    """Minimal connection interface shared by the healthcheck backends."""

    def execute(self, statement: str, *parameters: Any) -> Any:
        """Execute one statement and return a cursor-like result."""

    def close(self) -> None:
        """Close the connection."""


def connect_database(database: str | Path) -> sqlite3.Connection:
    """Open an explicit SQLite database path with foreign keys enabled."""
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def connect_configured_database(settings: Settings) -> DatabaseConnection:
    """Connect to the backend selected by validated runtime settings."""
    if settings.database_backend is DatabaseBackend.SQLITE:
        if settings.sqlite_database_path is None:
            raise DatabaseConnectionError(
                "SQLite database path is missing from validated settings"
            )
        return connect_database(settings.sqlite_database_path)
    if settings.database_backend is DatabaseBackend.TURSO:
        return connect_turso(settings)
    raise DatabaseConnectionError("Unsupported database backend")
