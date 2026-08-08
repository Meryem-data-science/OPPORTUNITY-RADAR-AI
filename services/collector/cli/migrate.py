"""Command-line entry point for applying local database migrations."""

import argparse
from pathlib import Path
import sqlite3

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations


def parse_args() -> argparse.Namespace:
    """Parse the required database location."""
    parser = argparse.ArgumentParser(description="Apply Opportunity Radar migrations.")
    parser.add_argument(
        "--database",
        required=True,
        type=Path,
        help="Explicit path to the local SQLite database.",
    )
    return parser.parse_args()


def main() -> int:
    """Apply migrations and report how many versions were applied."""
    args = parse_args()
    try:
        with connect_database(args.database) as connection:
            applied = apply_migrations(connection)
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"Migration failed: {error}")
        return 1

    print(f"Applied {len(applied)} migration(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
