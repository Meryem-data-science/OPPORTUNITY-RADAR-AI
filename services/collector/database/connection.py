"""Database connection factory and SQLite connection helpers."""

import sqlite3
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

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
    database_path = Path(database)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def connect_readonly_database(database: str | Path) -> sqlite3.Connection:
    """Open an **existing** SQLite database read-only, creating nothing.

    ``connect_database`` is a read-write entry point: it creates the parent
    directory and lets SQLite create the file. That is right for collection and
    migration, and wrong for a request that only reads — a query against a
    mistyped or not-yet-migrated path would leave an empty database behind that
    later reads as a real, empty one.

    This helper opens the same file through a ``mode=ro`` SQLite URI instead, so
    the file must already exist and the connection cannot create, migrate, or
    write anything. A missing database raises ``sqlite3.OperationalError``
    rather than being brought into existence.
    """
    database_path = Path(database)
    # quote leaves "/" intact and escapes the characters SQLite would otherwise
    # read as URI syntax, so an odd but legal path stays the path it is.
    uri = f"file:{quote(str(database_path.resolve()))}?mode=ro"
    return sqlite3.connect(uri, uri=True)


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
