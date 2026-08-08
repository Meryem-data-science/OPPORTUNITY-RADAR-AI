"""Local database connection and migration utilities."""

from services.collector.database.connection import (
    connect_configured_database,
    connect_database,
)
from services.collector.database.health import check_database_health
from services.collector.database.migrations import apply_migrations
from services.collector.database.turso import (
    DatabaseConnectionError,
    DatabaseDependencyError,
)

__all__ = [
    "DatabaseConnectionError",
    "DatabaseDependencyError",
    "apply_migrations",
    "check_database_health",
    "connect_configured_database",
    "connect_database",
]
