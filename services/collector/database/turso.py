"""Turso connection creation using the official libsql client."""

from importlib import import_module
from typing import Any

from services.collector.config import Settings


class DatabaseConnectionError(RuntimeError):
    """Raised when a configured database connection cannot be created."""


class DatabaseDependencyError(DatabaseConnectionError):
    """Raised when the selected database client is not installed."""


def connect_turso(settings: Settings) -> Any:
    """Create a Turso connection from validated settings without logging secrets."""
    try:
        libsql = import_module("libsql")
    except ModuleNotFoundError as error:
        raise DatabaseDependencyError(
            "The libsql package is required when DATABASE_BACKEND=turso"
        ) from error

    if settings.turso_database_url is None or settings.turso_auth_token is None:
        raise DatabaseConnectionError("Validated Turso settings are incomplete")

    try:
        return libsql.connect(
            database=settings.turso_database_url,
            auth_token=settings.turso_auth_token,
        )
    except Exception as error:
        raise DatabaseConnectionError(
            "Unable to connect to the Turso backend"
        ) from error
