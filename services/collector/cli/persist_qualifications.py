"""Explicit apply-only CLI for qualification persistence in existing SQLite databases."""

import argparse
from collections.abc import Sequence
from pathlib import Path
import sqlite3

from services.collector.cli.audit_qualification import positive_limit
from services.collector.database.connection import connect_database
from services.collector.qualification.persistence import (
    QualificationPersistenceError, persist_qualifications,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Persist versioned opportunity qualifications")
    parser.add_argument("--database", required=True, type=Path, help="existing SQLite database file")
    parser.add_argument("--apply", action="store_true", help="required to write qualifications")
    parser.add_argument("--limit", type=positive_limit)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.apply:
        print("persistence refused: --apply is required; no writes performed")
        return 2
    path = args.database.expanduser().resolve()
    if not path.is_file():
        print(f"persistence error: SQLite database does not exist: {path}")
        return 1
    try:
        with connect_database(path) as connection:
            summary = persist_qualifications(connection, limit=args.limit)
        print(
            f"created = {summary.created}\nupdated = {summary.updated}\n"
            f"unchanged = {summary.unchanged}\ntotal = {summary.total}"
        )
        return 0
    except (QualificationPersistenceError, OSError, sqlite3.Error, ValueError) as error:
        print(f"persistence error: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
