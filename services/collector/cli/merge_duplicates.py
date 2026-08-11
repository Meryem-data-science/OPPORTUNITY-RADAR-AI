"""CLI for explicit dry-run/apply duplicate merges and rollbacks."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from collections.abc import Sequence

from services.collector.database.connection import connect_database
from services.collector.deduplication.merges import (
    MergeError, apply_merge, list_merges, preflight_merge, preflight_rollback,
    rollback_merge,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely merge confirmed duplicate opportunities")
    commands = parser.add_subparsers(dest="command", required=True)
    merge = commands.add_parser("merge", help="preflight a merge; write only with --apply")
    merge.add_argument("--database", required=True, type=Path)
    merge.add_argument("--opportunity-a", required=True, type=int)
    merge.add_argument("--opportunity-b", required=True, type=int)
    merge.add_argument("--canonical", required=True, type=int)
    merge.add_argument("--apply", action="store_true")
    merge.add_argument("--format", choices=("human", "json"), default="human")
    rollback = commands.add_parser("rollback", help="preflight a rollback; write only with --apply")
    rollback.add_argument("--database", required=True, type=Path)
    rollback.add_argument("--merge-id", required=True, type=int)
    rollback.add_argument("--apply", action="store_true")
    rollback.add_argument("--format", choices=("human", "json"), default="human")
    listing = commands.add_parser("list", help="list immutable merge history")
    listing.add_argument("--database", required=True, type=Path)
    listing.add_argument("--format", choices=("human", "json"), default="human")
    return parser.parse_args(argv)


def _connection(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {resolved}")
    connection = connect_database(resolved)
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deduplication_merges'").fetchone() is None:
        connection.close()
        raise MergeError("MIGRATION_REQUIRED", "explicitly apply migration 0003 first")
    return connection


def _print(payload: object, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    elif isinstance(payload, list):
        print("id | decision | canonical | merged | status | applied_at | rolled_back_at")
        for item in payload:
            print(f"{item['id']} | {item['decision_id']} | {item['canonical_opportunity_id']} | {item['merged_opportunity_id']} | {item['status']} | {item['applied_at']} | {item['rolled_back_at'] or ''}")
    else:
        for key, value in payload.items():
            print(f"{key} = {value}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        with _connection(args.database) as connection:
            if args.command == "list":
                payload: object = list_merges(connection)
            elif args.command == "merge":
                if args.apply:
                    payload = apply_merge(connection, args.opportunity_a, args.opportunity_b, args.canonical)
                    payload["mode"] = "APPLY"
                else:
                    payload = {"mode": "DRY_RUN", **asdict(preflight_merge(connection, args.opportunity_a, args.opportunity_b, args.canonical))}
            elif args.apply:
                payload = rollback_merge(connection, args.merge_id)
                payload["mode"] = "APPLY"
            else:
                payload = {"mode": "DRY_RUN", **asdict(preflight_rollback(connection, args.merge_id))}
        _print(payload, args.format)
        return 0
    except (MergeError, FileNotFoundError, OSError, sqlite3.Error, ValueError) as error:
        print(f"merge error: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
