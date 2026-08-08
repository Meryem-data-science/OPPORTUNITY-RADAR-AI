"""Local database connection and migration utilities."""

from services.collector.database.connection import (
    connect_configured_database,
    connect_database,
)
from services.collector.database.health import check_database_health
from services.collector.database.migrations import (
    MigrationError,
    apply_migrations,
    split_sql_statements,
)
from services.collector.database.schema import check_foundation_schema
from services.collector.database.turso import (
    DatabaseConnectionError,
    DatabaseDependencyError,
)

__all__ = [
    "DatabaseConnectionError",
    "DatabaseDependencyError",
    "MigrationError",
    "apply_migrations",
    "check_database_health",
    "check_foundation_schema",
    "connect_configured_database",
    "connect_database",
    "split_sql_statements",
]
