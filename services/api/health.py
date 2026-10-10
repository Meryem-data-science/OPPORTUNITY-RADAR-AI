"""Public readiness of the configured API database, without creating or writing it."""

import os
import re
from typing import Literal

from pydantic import BaseModel, Field

from services.collector.config import DatabaseBackend, load_settings
from services.collector.database.connection import (
    DatabaseConnection,
    connect_readonly_database,
)
from services.collector.database.health import check_database_health
from services.collector.database.turso import connect_turso


# The operational web surfaces need Recommendation persistence. Later optional
# migrations (including CV staging) must not make an otherwise ready MVP fail.
MINIMUM_PRODUCT_MIGRATION = 26
REQUIRED_MIGRATIONS = {f"{version:04d}" for version in range(1, MINIMUM_PRODUCT_MIGRATION + 1)}
REQUIRED_TABLES = {
    "schema_migrations", "sources", "opportunities", "opportunity_sources",
    "source_runs", "profiles", "opportunity_qualifications",
    "opportunity_location_resolutions", "matching_runs", "matching_assessments",
    "matching_profile_state", "priority_runs", "priority_assessments",
    "priority_profile_state", "portfolio_runs", "portfolio_assessments",
    "portfolio_profile_state", "recommendation_runs", "recommendation_assessments",
    "recommendation_profile_state", "applications", "application_events",
}
HealthReason = Literal[
    "CONFIGURATION_INVALID", "DATABASE_UNAVAILABLE", "SCHEMA_NOT_READY",
    "SCHEMA_UNREADABLE", "BACKEND_NOT_OPERATIONAL", "PROFILE_NOT_CONFIGURED",
    "PROFILE_NOT_FOUND",
]


class SystemHealthResponse(BaseModel):
    """A fixed public vocabulary: no path, token, profile ID or driver message."""

    status: Literal["ok", "degraded", "error"]
    api: Literal["available"] = "available"
    databaseBackend: Literal["sqlite", "turso", "unknown"] = "unknown"
    database: Literal["connected", "unavailable"] = "unavailable"
    schema_status: Literal["ready", "not_ready", "unknown"] = Field(default="unknown", alias="schema")
    migrationVersion: str | None = None
    minimumMigrationVersion: str = f"{MINIMUM_PRODUCT_MIGRATION:04d}"
    profile: Literal["ready", "not_configured", "not_found", "unknown"] = "unknown"
    reason: HealthReason | None = None


def read_system_health() -> SystemHealthResponse:
    """Check connectivity, required schema and configured profile, read-only.

    SQLite is opened with mode=ro and query_only, so even an incorrect path
    cannot create a database. Turso is queried only with SELECT statements and
    is reported as degraded: the operational product surfaces require SQLite.
    Neither matching freshness nor opportunity quality is asserted here.
    """
    try:
        settings = load_settings()
    except Exception:
        return SystemHealthResponse(status="error", reason="CONFIGURATION_INVALID")

    backend = settings.database_backend.value
    connection: DatabaseConnection | None = None
    connected = False
    migration_version = None
    try:
        if settings.database_backend is DatabaseBackend.SQLITE:
            if settings.sqlite_database_path is None:
                raise ValueError("Missing validated SQLite configuration")
            connection = connect_readonly_database(settings.sqlite_database_path)
            connection.execute("PRAGMA query_only = ON")
            row = connection.execute("PRAGMA query_only").fetchone()
            if row is None or row[0] != 1:
                raise ValueError("Read-only connection required")
            connection.execute("BEGIN")
        else:
            connection = connect_turso(settings)
        if not check_database_health(connection):
            raise ValueError("Unexpected connectivity result")
        connected = True

        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        versions: set[str] = set()
        if "schema_migrations" in tables:
            versions = {
                str(row[0]) for row in connection.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            if any(not re.fullmatch(r"[0-9]{4,10}", version) for version in versions):
                return SystemHealthResponse(
                    status="degraded", databaseBackend=backend,
                    database="connected", schema="not_ready", reason="SCHEMA_NOT_READY",
                )
            if versions:
                migration_version = max(versions, key=int)

        if settings.database_backend is DatabaseBackend.TURSO:
            return SystemHealthResponse(
                status="degraded", databaseBackend=backend, database="connected",
                schema="not_ready", migrationVersion=migration_version,
                reason="BACKEND_NOT_OPERATIONAL",
            )
        if not REQUIRED_TABLES.issubset(tables) or not REQUIRED_MIGRATIONS.issubset(versions):
            return SystemHealthResponse(
                status="degraded", databaseBackend=backend, database="connected",
                schema="not_ready", migrationVersion=migration_version,
                reason="SCHEMA_NOT_READY",
            )

        # LIMIT 0 verifies common read keys without fetching personal data.
        for statement in (
            "SELECT id, canonical_title, is_active FROM opportunities LIMIT 0",
            "SELECT opportunity_id, source_id FROM opportunity_sources LIMIT 0",
            "SELECT id, profile_id FROM recommendation_runs LIMIT 0",
            "SELECT profile_id, current_run_id FROM recommendation_profile_state LIMIT 0",
            "SELECT id, profile_id, opportunity_id, status FROM applications LIMIT 0",
        ):
            connection.execute(statement).fetchall()

        raw_profile_id = os.environ.get("OPPORTUNITY_RADAR_PROFILE_ID")
        try:
            profile_id = int(raw_profile_id) if raw_profile_id is not None else 0
        except ValueError:
            profile_id = 0
        if profile_id <= 0 or raw_profile_id is None or raw_profile_id.strip() != str(profile_id):
            return SystemHealthResponse(
                status="degraded", databaseBackend=backend, database="connected",
                schema="ready", migrationVersion=migration_version,
                profile="not_configured", reason="PROFILE_NOT_CONFIGURED",
            )
        if connection.execute(
            "SELECT 1 FROM profiles WHERE id = ?", (profile_id,)
        ).fetchone() is None:
            return SystemHealthResponse(
                status="degraded", databaseBackend=backend, database="connected",
                schema="ready", migrationVersion=migration_version,
                profile="not_found", reason="PROFILE_NOT_FOUND",
            )
        return SystemHealthResponse(
            status="ok", databaseBackend=backend, database="connected",
            schema="ready", migrationVersion=migration_version, profile="ready",
        )
    except Exception:
        return SystemHealthResponse(
            status="degraded" if connected else "error", databaseBackend=backend,
            database="connected" if connected else "unavailable",
            schema="not_ready" if connected else "unknown",
            migrationVersion=migration_version,
            reason="SCHEMA_UNREADABLE" if connected else "DATABASE_UNAVAILABLE",
        )
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
