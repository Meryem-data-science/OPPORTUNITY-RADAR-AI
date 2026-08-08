"""Command-line database connectivity healthcheck."""

from services.collector.config import load_settings
from services.collector.database.connection import connect_configured_database
from services.collector.database.health import check_database_health
from services.collector.logging_config import get_logger


def main() -> int:
    """Check the selected database backend and close its connection."""
    logger = get_logger("services.collector.cli.db_health")
    backend = "unconfigured"
    try:
        settings = load_settings()
        backend = settings.database_backend.value
        connection = connect_configured_database(settings)
        try:
            healthy = check_database_health(connection)
        finally:
            connection.close()
        if not healthy:
            raise RuntimeError("Database healthcheck returned an unexpected result")
    except Exception as error:
        logger.error(
            "Database healthcheck failed.",
            extra={
                "event": "database_healthcheck_failed",
                "database_backend": backend,
                "error_type": type(error).__name__,
            },
        )
        return 1

    logger.info(
        "Database healthcheck succeeded.",
        extra={
            "event": "database_healthcheck_succeeded",
            "database_backend": backend,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
