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
from services.collector.database.source_runs import (
    SourceRunPersistenceError,
    recent_source_runs,
    record_configured_source_run,
    record_failed_source_run,
    record_source_run,
    record_successful_source_run,
    start_source_run,
)
from services.collector.database.turso import (
    DatabaseConnectionError,
    DatabaseDependencyError,
)

__all__ = [
    "DatabaseConnectionError",
    "DatabaseDependencyError",
    "MigrationError",
    "SourceRunPersistenceError",
    "apply_migrations",
    "check_database_health",
    "check_foundation_schema",
    "connect_configured_database",
    "connect_database",
    "recent_source_runs",
    "record_configured_source_run",
    "record_failed_source_run",
    "record_source_run",
    "record_successful_source_run",
    "split_sql_statements",
    "start_source_run",
]
