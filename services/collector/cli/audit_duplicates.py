"""CLI for the strictly read-only cross-source duplicate audit."""

import argparse
from collections.abc import Sequence
import json
from pathlib import Path
import sqlite3

from services.collector.deduplication.audit import AuditReport, audit_database


def positive_limit(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("limit must be an integer") from error
    if result < 1:
        raise argparse.ArgumentTypeError("limit must be positive")
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit potential cross-source duplicates without modifying SQLite")
    parser.add_argument("--database", required=True, type=Path, help="existing SQLite database file")
    parser.add_argument("--limit", type=positive_limit, default=50, help="maximum candidates displayed")
    parser.add_argument("--format", choices=("human", "json"), default="human")
    return parser.parse_args(argv)


def _human(report: AuditReport, limit: int) -> str:
    counts = report.classification_counts
    lines = [
        "Cross-source duplicate audit (READ-ONLY; classifications are not AUTO_MERGE)",
        f"Opportunities inspected: {report.total_opportunities}",
        f"Sources ({report.source_count}): {', '.join(report.sources) or '(none)'}",
        f"Cross-source pairs examined: {report.cross_source_pairs_examined}",
        f"Candidates retained: {report.candidates_retained}",
        f"STRONG_CANDIDATE: {counts['STRONG_CANDIDATE']}",
        f"POSSIBLE_CANDIDATE: {counts['POSSIBLE_CANDIDATE']}",
        f"WEAK_CANDIDATE: {counts['WEAK_CANDIDATE']}",
    ]
    for index, candidate in enumerate(report.candidates[:limit], 1):
        a, b = candidate.opportunity_a, candidate.opportunity_b
        lines.extend([
            "", f"[{index}] {candidate.classification}",
            f"A: id={a.id} sources={list(a.sources)} title={a.canonical_title!r} organization={a.organization!r} location={a.location!r}",
            f"B: id={b.id} sources={list(b.sources)} title={b.canonical_title!r} organization={b.organization!r} location={b.location!r}",
            f"Signals: title_similarity={candidate.title_similarity:.4f} title_exact={candidate.title_normalized_exact}; organization_similarity={candidate.organization_similarity:.4f} organization_exact={candidate.organization_normalized_exact}; location={candidate.location_signal}",
            f"URLs: source={candidate.shared_source_url} canonical={candidate.shared_canonical_url} application={candidate.shared_application_url}",
            f"Date: {candidate.date_signal} distance_days={candidate.date_distance_days}",
            f"Reasons: {'; '.join(candidate.reasons)}",
        ])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = audit_database(args.database)
    except (FileNotFoundError, OSError, sqlite3.Error, ValueError) as error:
        print(f"audit error: {error}")
        return 1
    if args.format == "json":
        payload = report.to_dict()
        payload["candidates"] = payload["candidates"][: args.limit]
        payload["display_limit"] = args.limit
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_human(report, args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
