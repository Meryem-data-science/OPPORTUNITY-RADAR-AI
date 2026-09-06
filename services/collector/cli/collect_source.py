"""Read-only command for dry-running one configured opportunity source.

Generic by type: the collector comes from the registry via `collector_for`, so
every configured source is dry-run through the collector it is actually
registered to. `--dry-run` stays a required safeguard and nothing here persists —
candidates are printed and discarded.
"""

import argparse
import json
import sys

from services.collector.collectors.factory import collector_for
from services.collector.logging_config import get_logger
from services.collector.sources import (
    DisabledSourceError,
    SourceConfigurationError,
    UnknownSourceError,
    get_enabled_source,
)


def positive_limit(value: str) -> int:
    """Parse a strictly positive display limit."""
    try:
        limit = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("limit must be an integer") from error
    if limit <= 0:
        raise argparse.ArgumentTypeError("limit must be greater than zero")
    return limit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dry-run a configured source")
    parser.add_argument("--source", required=True)
    parser.add_argument("--limit", type=positive_limit, default=5)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        required=True,
        help="required safeguard: collect and display without persistence",
    )
    return parser


def run(source_id: str, limit: int) -> list[dict[str, str | None]]:
    """Collect and print at most limit read-only candidate summaries."""
    source = get_enabled_source(source_id)
    logger = get_logger("services.collector.cli.collect_source")
    logger.info(
        "Source collection started.",
        extra={"event": "source_collection_started", "source_id": source.id},
    )
    try:
        # The factory, not a named collector: this command dry-runs whatever
        # the source's type is registered to, so a new collector needs no change
        # here. It was hardcoded to Greenhouse, which quietly meant every other
        # configured source was dry-run through the wrong collector.
        candidates = collector_for(source).collect()
    except (RuntimeError, ValueError) as error:
        logger.error(
            f"Source collection failed: {error}",
            extra={"event": "source_collection_failed", "source_id": source.id},
        )
        raise
    returned = candidates[:limit]
    summaries = [
        {
            "source_id": item.source_id,
            "source_external_id": item.source_external_id,
            "canonical_title": item.canonical_title,
            "organization": item.organization,
            "location": item.location,
            "source_url": item.source_url,
        }
        for item in returned
    ]
    logger.info(
        "Source collection succeeded.",
        extra={
            "event": "source_collection_succeeded",
            "source_id": source.id,
            "items_found": len(candidates),
            "items_returned": len(returned),
        },
    )
    for summary in summaries:
        print(json.dumps(summary, ensure_ascii=False))
    return summaries


def main() -> None:
    args = build_parser().parse_args()
    try:
        run(args.source, args.limit)
    except (
        SourceConfigurationError,
        UnknownSourceError,
        DisabledSourceError,
        RuntimeError,
        ValueError,
    ) as error:
        print(f"collection error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
