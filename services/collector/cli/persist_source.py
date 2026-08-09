"""Explicit write command for collecting and persisting one source."""

import argparse
from collections.abc import Sequence
import os

from services.collector.cli.collect_source import positive_limit
from services.collector.collectors.greenhouse import GreenhouseCollector
from services.collector.config import DatabaseBackend, load_settings
from services.collector.database.connection import connect_configured_database
from services.collector.database.opportunities import persist_opportunities
from services.collector.logging_config import get_logger
from services.collector.sources import get_enabled_source


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse a bounded persistence request with explicit write permission."""
    parser = argparse.ArgumentParser(
        description="Collect and persist a bounded batch from one configured source"
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--limit", required=True, type=positive_limit)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Explicitly authorize database writes",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Collect then persist, refusing unapproved local or remote writes."""
    logger = get_logger("services.collector.cli.persist_source")
    source_id = "unconfigured"
    backend = "unconfigured"
    try:
        args = parse_args(argv)
        source_id = args.source
        if not args.apply:
            raise PermissionError("Opportunity persistence requires --apply")

        source = get_enabled_source(args.source)
        candidates = GreenhouseCollector(source).collect()
        selected = candidates[: args.limit]
        settings = load_settings()
        backend = settings.database_backend.value
        if (
            settings.database_backend is DatabaseBackend.TURSO
            and os.environ.get("RUN_TURSO_LIVE_PERSIST") != "1"
        ):
            raise PermissionError("Turso persistence requires RUN_TURSO_LIVE_PERSIST=1")

        logger.info(
            "Source persistence started.",
            extra={
                "event": "source_persistence_started",
                "source_id": source.id,
                "items_collected": len(candidates),
                "items_selected": len(selected),
                "database_backend": backend,
            },
        )
        connection = connect_configured_database(settings)
        try:
            summary = persist_opportunities(connection, source, selected)
        finally:
            connection.close()
    except Exception as error:
        logger.error(
            "Source persistence failed.",
            extra={
                "event": "source_persistence_failed",
                "source_id": source_id,
                "database_backend": backend,
                "error_type": type(error).__name__,
            },
        )
        return 1

    logger.info(
        "Source persistence succeeded.",
        extra={
            "event": "source_persistence_succeeded",
            "source_id": source.id,
            "items_collected": len(candidates),
            "items_selected": len(selected),
            "created": summary.created,
            "updated": summary.updated,
            "database_backend": backend,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
