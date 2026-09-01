"""CLI for an explicit matching sync against a named SQLite database."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from services.collector.database.connection import (
    connect_database,
    connect_readonly_database,
)

from .sync import MatchingSyncResult, sync_matching


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run deterministic matching for one profile."
    )
    parser.add_argument(
        "--database", required=True, help="Explicit SQLite database path."
    )
    parser.add_argument("--profile-id", required=True, type=_positive_integer)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute using SQLite mode=ro without persistence.",
    )
    return parser.parse_args(argv)


def _payload(result: MatchingSyncResult) -> dict[str, object]:
    return {
        "batch_fingerprint": result.batch_fingerprint,
        "corpus_fingerprint": result.corpus_fingerprint,
        "created": result.created,
        "lane_counts": dict(result.lane_counts),
        "persisted": result.persisted,
        "profile_id": result.profile_id,
        "run_fingerprint": result.run_fingerprint,
        "run_id": result.run_id,
        "selected_count": result.selected_count,
        "selection_version": result.selection_version,
        "state": result.state,
        "tfidf_model_fingerprint": result.tfidf_model_fingerprint,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    connector = connect_readonly_database if args.dry_run else connect_database
    try:
        with connector(args.database) as connection:
            result = sync_matching(
                connection, args.profile_id, persist=not args.dry_run
            )
    except Exception as error:
        print(f"matching sync failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(_payload(result), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
