"""CLI for staging and reviewing cross-source duplicate candidates."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from collections.abc import Sequence

from services.collector.database.connection import connect_database
from services.collector.deduplication.audit import WEAK_CANDIDATE, audit_database
from services.collector.deduplication.decisions import (
    CONFIRMED_DUPLICATE,
    NOT_DUPLICATE,
    DecisionError,
    decide_pair,
    list_decisions,
    stage_candidates,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review cross-source duplicate candidates without merging opportunities")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="audit candidates; dry-run unless --apply is supplied")
    scan.add_argument("--database", required=True, type=Path)
    scan.add_argument("--apply", action="store_true", help="persist strong and possible candidates")
    scan.add_argument("--format", choices=("human", "json"), default="human")
    listing = commands.add_parser("list", help="list the review registry")
    listing.add_argument("--database", required=True, type=Path)
    listing.add_argument("--format", choices=("human", "json"), default="human")
    decide = commands.add_parser("decide", help="confirm or reject an existing staged pair")
    decide.add_argument("--database", required=True, type=Path)
    decide.add_argument("--opportunity-a", required=True, type=int)
    decide.add_argument("--opportunity-b", required=True, type=int)
    decide.add_argument("--decision", required=True, choices=(CONFIRMED_DUPLICATE, NOT_DUPLICATE))
    decide.add_argument("--note")
    return parser.parse_args(argv)


def _existing_connection(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {resolved}")
    connection = connect_database(resolved)
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'deduplication_decisions'"
    ).fetchone()
    if table is None:
        connection.close()
        raise DecisionError("deduplication registry is unavailable; explicitly apply migration 0002 first")
    return connection


def _scan(args: argparse.Namespace) -> dict[str, object]:
    report = audit_database(args.database)
    weak = report.classification_counts[WEAK_CANDIDATE]
    stageable = [candidate for candidate in report.candidates if candidate.classification != WEAK_CANDIDATE]
    staged = 0
    if args.apply:
        with _existing_connection(args.database) as connection:
            staged = len(stage_candidates(connection, stageable))
    return {
        "mode": "APPLY" if args.apply else "DRY_RUN",
        "strong_candidates": report.classification_counts["STRONG_CANDIDATE"],
        "possible_candidates": report.classification_counts["POSSIBLE_CANDIDATE"],
        "stageable_candidates": len(stageable),
        "weak_candidates_ignored": weak,
        "decisions_written": staged,
    }


def _display_scan(payload: dict[str, object], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(payload, indent=2))
        return
    print(f"Cross-source review scan ({payload['mode']})")
    print(f"STRONG_CANDIDATE: {payload['strong_candidates']}")
    print(f"POSSIBLE_CANDIDATE: {payload['possible_candidates']}")
    print(f"Would stage: {payload['stageable_candidates']}")
    print(f"WEAK_CANDIDATE ignored: {payload['weak_candidates_ignored']}")
    print(f"Decisions written: {payload['decisions_written']}")


def _list(args: argparse.Namespace) -> None:
    with _existing_connection(args.database) as connection:
        decisions = list_decisions(connection)
    if args.format == "json":
        print(json.dumps([asdict(item) for item in decisions], ensure_ascii=False, indent=2))
        return
    print("id | opportunity_a | opportunity_b | sources_a | sources_b | status | audit_classification | title_similarity | organization_similarity | reviewed_at")
    for item in decisions:
        print(f"{item.id} | {item.opportunity_a_id} | {item.opportunity_b_id} | {','.join(item.sources_a)} | {','.join(item.sources_b)} | {item.status} | {item.audit_classification} | {item.title_similarity:.4f} | {item.organization_similarity:.4f} | {item.reviewed_at or ''}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "scan":
            _display_scan(_scan(args), args.format)
        elif args.command == "list":
            _list(args)
        else:
            with _existing_connection(args.database) as connection:
                result = decide_pair(connection, args.opportunity_a, args.opportunity_b, args.decision, args.note)
            print(f"pair ({result.opportunity_a_id}, {result.opportunity_b_id}) -> {result.status}")
    except (DecisionError, FileNotFoundError, OSError, sqlite3.Error, ValueError) as error:
        print(f"review error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
