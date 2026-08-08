"""Read-only foundation schema verification for the configured database."""

from services.collector.config import load_settings
from services.collector.database.connection import connect_configured_database
from services.collector.database.schema import check_foundation_schema
from services.collector.logging_config import get_logger


def main() -> int:
    """Check the configured database for the expected foundation tables."""
    logger = get_logger("services.collector.cli.db_schema")
    backend = "unconfigured"
    try:
        settings = load_settings()
        backend = settings.database_backend.value
        connection = connect_configured_database(settings)
        try:
            schema_ready = check_foundation_schema(connection)
        finally:
            connection.close()
        if not schema_ready:
            raise RuntimeError("Foundation database schema is incomplete")
    except Exception as error:
        logger.error(
            "Database schema check failed.",
            extra={
                "event": "database_schema_check_failed",
                "database_backend": backend,
                "error_type": type(error).__name__,
            },
        )
        return 1

    logger.info(
        "Database schema check succeeded.",
        extra={
            "event": "database_schema_check_succeeded",
            "database_backend": backend,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
