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
    finalize_configured_source_run,
    finalize_failed_source_run,
    finalize_source_run,
    finalize_successful_source_run,
    recent_source_runs,
    start_configured_source_run,
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
    "finalize_configured_source_run",
    "finalize_failed_source_run",
    "finalize_source_run",
    "finalize_successful_source_run",
    "recent_source_runs",
    "split_sql_statements",
    "start_configured_source_run",
    "start_source_run",
]
