"""Read-only listing of opportunities in the configured database."""

import argparse
from collections.abc import Sequence
import json

from services.collector.config import load_settings
from services.collector.database.connection import connect_configured_database

MAX_LIMIT = 100


def bounded_limit(value: str) -> int:
    """Parse a positive listing limit capped to protect terminal output."""
    try:
        limit = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("limit must be an integer") from error
    if limit <= 0 or limit > MAX_LIMIT:
        raise argparse.ArgumentTypeError(f"limit must be between 1 and {MAX_LIMIT}")
    return limit


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List persisted opportunities without modifying the database"
    )
    parser.add_argument("--limit", type=bounded_limit, default=5)
    return parser.parse_args(argv)


def list_opportunities(limit: int) -> list[dict[str, object]]:
    """Return and display the most recently discovered opportunities."""
    settings = load_settings()
    connection = connect_configured_database(settings)
    try:
        rows = connection.execute(
            """
            SELECT id, canonical_title, organization, location, source_url,
                   application_url, discovered_at, last_seen_at, status,
                   length(description)
            FROM opportunities
            ORDER BY discovered_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        connection.close()
    keys = (
        "id",
        "canonical_title",
        "organization",
        "location",
        "source_url",
        "application_url",
        "discovered_at",
        "last_seen_at",
        "status",
        "description_length",
    )
    opportunities = [dict(zip(keys, row, strict=True)) for row in rows]
    for opportunity in opportunities:
        print(json.dumps(opportunity, ensure_ascii=False))
    print(f"{len(opportunities)} opportunities")
    return opportunities


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        list_opportunities(args.limit)
    except Exception as error:
        print(f"listing error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
