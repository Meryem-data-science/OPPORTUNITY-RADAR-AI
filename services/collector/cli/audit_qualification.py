"""CLI for deterministic, strictly read-only opportunity qualification."""

import argparse
from collections.abc import Sequence
import json
from pathlib import Path
import sqlite3

from services.collector.qualification.audit import AuditReport, audit_database
from services.collector.qualification.taxonomy import Qualification


def positive_limit(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("limit must be an integer") from error
    if result < 1:
        raise argparse.ArgumentTypeError("limit must be positive")
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit Data/AI qualification without modifying SQLite")
    parser.add_argument("--database", required=True, type=Path, help="existing SQLite database file")
    parser.add_argument("--format", choices=("human", "json"), default="human")
    parser.add_argument("--limit", type=positive_limit, default=50, help="maximum classifications displayed")
    parser.add_argument("--qualification", choices=tuple(Qualification), help="optional display filter")
    return parser.parse_args(argv)


def _selected(report: AuditReport, qualification: str | None, limit: int):
    items = report.opportunities
    if qualification:
        items = tuple(item for item in items if item.classification.qualification == qualification)
    return items[:limit]


def _human(report: AuditReport, qualification: str | None, limit: int) -> str:
    lines = [
        "Opportunity qualification + fine Data/AI category audit "
        "(STRICTLY READ-ONLY; no classification is persisted)",
        f"Active opportunities inspected: {report.total_active_opportunities}",
        f"Sources ({len(report.sources)}): {', '.join(report.sources) or '(none)'}",
        f"Qualification counts: {report.qualification_counts}",
        f"Primary domain counts: {report.primary_domain_counts}",
        f"Opportunity type counts: {report.opportunity_type_counts}",
        f"Employment type counts: {report.employment_type_counts}",
        f"Listing quality counts: {report.listing_quality_counts}",
        f"Fine primary category counts: {report.fine_primary_category_counts}",
        f"Fine secondary category counts: {report.fine_secondary_category_counts}",
        f"Fine uncategorized (no safe fine category): {report.fine_uncategorized_count}",
    ]
    for index, item in enumerate(_selected(report, qualification, limit), 1):
        result, fine = item.classification, item.fine_classification
        lines.extend((
            "", f"[{index}] id={item.id} {result.qualification} / {result.primary_domain}",
            f"Title: {item.title!r} | Organization: {item.organization!r}",
            f"Location: {item.location!r} | Sources: {list(item.source_ids)}",
            f"Domains: {[str(value) for value in result.matched_domains]}",
            f"Type: {result.opportunity_type} | Employment: {result.employment_type} | Quality: {result.listing_quality}",
            f"Quality flags: {list(result.quality_flags)}",
            f"Title signals: {list(result.matched_title_signals)}",
            f"Description signals: {list(result.matched_description_signals)}",
            f"Exclusions: {list(result.matched_exclusion_signals)}",
            f"Reasons: {'; '.join(result.reasons)}",
            f"Fine primary: {fine.primary_category} ({fine.classifier_version})"
            f" | Fine secondaries: {[str(value) for value in fine.secondary_categories]}",
            f"Fine evidence: {[(str(e.category), str(e.field), str(e.kind), e.signal) for e in fine.evidence]}",
            f"Fine reasons: {'; '.join(fine.reasons)}",
        ))
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
        payload["opportunities"] = [item.to_dict() for item in _selected(report, args.qualification, args.limit)]
        payload["display_limit"] = args.limit
        payload["qualification_filter"] = args.qualification
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_human(report, args.qualification, args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
