"""Apply migrations to the explicitly configured database backend."""

import argparse
import os
from collections.abc import Sequence

from services.collector.config import DatabaseBackend, load_settings
from services.collector.database.connection import connect_configured_database
from services.collector.database.migrations import apply_migrations
from services.collector.logging_config import get_logger


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the explicit permission to modify the configured database."""
    parser = argparse.ArgumentParser(
        description="Apply migrations to the configured database backend."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Explicitly authorize applying pending migrations.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Apply configured migrations with additional protection for Turso."""
    logger = get_logger("services.collector.cli.migrate_configured")
    backend = "unconfigured"
    try:
        args = parse_args(argv)
        settings = load_settings()
        backend = settings.database_backend.value
        if not args.apply:
            raise PermissionError("Migration application requires --apply")
        if (
            settings.database_backend is DatabaseBackend.TURSO
            and os.environ.get("RUN_TURSO_LIVE_MIGRATION") != "1"
        ):
            raise PermissionError(
                "Turso migration requires RUN_TURSO_LIVE_MIGRATION=1"
            )

        connection = connect_configured_database(settings)
        try:
            applied = apply_migrations(connection)
        finally:
            connection.close()
    except Exception as error:
        logger.error(
            "Database migrations failed.",
            extra={
                "event": "database_migrations_failed",
                "database_backend": backend,
                "error_type": type(error).__name__,
            },
        )
        return 1

    logger.info(
        "Database migrations succeeded.",
        extra={
            "event": "database_migrations_succeeded",
            "database_backend": backend,
            "migrations_applied": len(applied),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
