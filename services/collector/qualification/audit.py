"""Strictly read-only SQLite qualification audit."""

from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
import sqlite3

from .classifier import Classification, classify_opportunity


@dataclass(frozen=True)
class AuditedOpportunity:
    id: int
    title: str
    organization: str
    location: str | None
    source_ids: tuple[str, ...]
    classification: Classification

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value.update(value.pop("classification"))
        return value


@dataclass(frozen=True)
class AuditReport:
    total_active_opportunities: int
    sources: tuple[str, ...]
    qualification_counts: dict[str, int]
    primary_domain_counts: dict[str, int]
    opportunity_type_counts: dict[str, int]
    employment_type_counts: dict[str, int]
    listing_quality_counts: dict[str, int]
    opportunities: tuple[AuditedOpportunity, ...]

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["opportunities"] = [item.to_dict() for item in self.opportunities]
        return value


def open_read_only_database(database: str | Path) -> sqlite3.Connection:
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {path}")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _count(items: list[AuditedOpportunity], attribute: str) -> dict[str, int]:
    return dict(sorted(Counter(str(getattr(item.classification, attribute)) for item in items).items()))


def audit_database(database: str | Path) -> AuditReport:
    """Read and classify active, non-merge-tombstone rows without migrations/writes."""
    connection = open_read_only_database(database)
    try:
        query_only = connection.execute("PRAGMA query_only").fetchone()
        if query_only != (1,):
            raise sqlite3.OperationalError("qualification audit requires query_only SQLite mode")
        rows = connection.execute(
            """
            SELECT o.id, o.canonical_title, o.organization, o.location,
                   o.description, o.source_url, o.application_url, o.canonical_url,
                   GROUP_CONCAT(os.source_id, char(31))
            FROM opportunities o
            JOIN opportunity_sources os ON os.opportunity_id = o.id
            WHERE o.is_active = 1 AND o.status != 'merged_duplicate'
            GROUP BY o.id
            ORDER BY o.id
            """
        ).fetchall()
        items = [
            AuditedOpportunity(
                row[0], row[1], row[2], row[3], tuple(sorted(set(row[8].split(chr(31))))),
                classify_opportunity(
                    row[1], row[4], source_url=row[5], application_url=row[6],
                    canonical_url=row[7], location=row[3],
                ),
            )
            for row in rows
        ]
        sources = tuple(sorted({source for item in items for source in item.source_ids}))
        return AuditReport(
            len(items), sources, _count(items, "qualification"),
            _count(items, "primary_domain"), _count(items, "opportunity_type"),
            _count(items, "employment_type"), _count(items, "listing_quality"),
            tuple(items),
        )
    finally:
        connection.close()
