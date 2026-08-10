"""Command-line entry point for one explicitly authorized radar run."""

import argparse
from collections.abc import Sequence

from services.collector.agent import RadarAgent
from services.collector.logging_config import get_logger


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run all active radar sources once")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--apply", action="store_true", help="Authorize database writes")
    parser.add_argument("--source", action="append", dest="sources", help="Run only this enabled, active source (repeatable)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logger = get_logger(__name__)
    try:
        args = parse_args(argv)
        if not args.once:
            raise PermissionError("This phase requires --once")
        if not args.apply:
            raise PermissionError("Radar persistence requires --apply")
        summary = RadarAgent(source_ids=args.sources).run_once()
    except Exception as error:
        logger.error(
            "Radar run failed.",
            extra={"event": "radar_run_failed", "error_type": type(error).__name__},
        )
        return 1

    print("Radar run completed")
    for field in (
        "sources_total", "sources_succeeded", "sources_failed",
        "items_collected", "items_created", "items_updated",
    ):
        print(f"{field}={getattr(summary, field)}")
    return 1 if summary.sources_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
