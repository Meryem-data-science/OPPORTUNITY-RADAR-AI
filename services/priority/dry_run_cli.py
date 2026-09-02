"""CLI for the read-only Priority v1 diagnostic dry-run."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import date

from services.collector.database.connection import connect_readonly_database

from .dry_run import run_priority_dry_run
from .input_assembly import PriorityReadinessStatus


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from error


def _json_default(value):
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"cannot serialize {type(value).__name__}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--profile-id", required=True, type=int)
    parser.add_argument("--evaluation-date", required=True, type=_date)
    parser.add_argument("--show", type=int)
    args = parser.parse_args(argv)
    if args.show is not None and args.show < 0:
        parser.error("--show must be non-negative")
    with connect_readonly_database(args.database) as connection:
        report = run_priority_dry_run(connection, args.profile_id, args.evaluation_date)
    payload = asdict(report)
    if args.show is not None:
        payload["lines"] = payload["lines"][: args.show]
    print(json.dumps(payload, default=_json_default, sort_keys=True, indent=2))
    return 0 if report.status is PriorityReadinessStatus.READY else 2


if __name__ == "__main__":
    raise SystemExit(main())
