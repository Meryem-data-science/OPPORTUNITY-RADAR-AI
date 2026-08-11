"""Deterministic, read-only audit of potential cross-source duplicates.

Candidate generation compares every pair of distinct opportunities (O(n²)) and
discards same-source-only pairs.  This is intentionally simple for current
volumes; a later phase can add blocking without changing comparison signals.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
import re
import sqlite3
import unicodedata


STRONG_CANDIDATE = "STRONG_CANDIDATE"
POSSIBLE_CANDIDATE = "POSSIBLE_CANDIDATE"
WEAK_CANDIDATE = "WEAK_CANDIDATE"


@dataclass(frozen=True)
class AuditThresholds:
    """Central conservative thresholds; classifications never imply auto-merge."""

    possible_title_similarity: float = 0.85
    possible_organization_similarity: float = 0.85
    weak_title_similarity: float = 0.65
    weak_organization_similarity: float = 0.65
    nearby_days: int = 30


DEFAULT_THRESHOLDS = AuditThresholds()


@dataclass(frozen=True)
class AuditOpportunity:
    id: int
    canonical_title: str
    organization: str
    location: str | None
    description: str | None
    published_at: str | None
    discovered_at: str
    first_seen_at: str
    last_seen_at: str
    source_url: str
    application_url: str | None
    canonical_url: str | None
    sources: tuple[str, ...]
    source_urls: tuple[str, ...]
    source_application_urls: tuple[str, ...]
    source_canonical_urls: tuple[str, ...]


@dataclass(frozen=True)
class CandidatePair:
    opportunity_a: AuditOpportunity
    opportunity_b: AuditOpportunity
    title_normalized_exact: bool
    organization_normalized_exact: bool
    title_similarity: float
    organization_similarity: float
    location_signal: str
    shared_source_url: bool
    shared_canonical_url: bool
    shared_application_url: bool
    date_distance_days: int | None
    date_signal: str
    classification: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class AuditReport:
    total_opportunities: int
    source_count: int
    sources: tuple[str, ...]
    cross_source_pairs_examined: int
    candidates_retained: int
    classification_counts: dict[str, int]
    candidates: tuple[CandidatePair, ...]
    limitations: tuple[str, ...] = (
        "source_external_id is not available in the persisted foundation schema",
        "candidate generation is O(n²) before same-source filtering",
        "audit classifications are preliminary heuristics and never AUTO_MERGE",
    )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _normalize(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def normalize_title(value: str | None) -> str:
    """Normalize Unicode, case, whitespace, and non-semantic punctuation."""
    return _normalize(value)


def normalize_organization(value: str | None) -> str:
    """Conservatively normalize formatting; legal suffixes remain significant."""
    return _normalize(value)


def normalize_location(value: str | None) -> str:
    """Apply only the shared light string normalization to a location."""
    return _normalize(value)


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    left_tokens, right_tokens = set(left.split()), set(right.split())
    jaccard = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    sequence = SequenceMatcher(None, left, right, autojunk=False).ratio()
    return round(max(jaccard, sequence), 4)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _date_values(item: AuditOpportunity) -> datetime | None:
    return _parse_date(item.published_at) or _parse_date(item.discovered_at)


def _has_different_sources(left: AuditOpportunity, right: AuditOpportunity) -> bool:
    return any(a != b for a in left.sources for b in right.sources)


def compare_pair(
    left: AuditOpportunity,
    right: AuditOpportunity,
    thresholds: AuditThresholds = DEFAULT_THRESHOLDS,
) -> CandidatePair | None:
    """Compare a cross-source pair and retain only explainable candidates."""
    if left.id == right.id or not _has_different_sources(left, right):
        return None
    title_a, title_b = normalize_title(left.canonical_title), normalize_title(right.canonical_title)
    org_a, org_b = normalize_organization(left.organization), normalize_organization(right.organization)
    title_exact = bool(title_a) and title_a == title_b
    org_exact = bool(org_a) and org_a == org_b
    title_similarity = _similarity(title_a, title_b)
    organization_similarity = _similarity(org_a, org_b)

    location_a, location_b = normalize_location(left.location), normalize_location(right.location)
    if not location_a or not location_b:
        location_signal = "UNKNOWN"
    elif location_a == location_b:
        location_signal = "EXACT"
    elif set(location_a.split()) & set(location_b.split()):
        location_signal = "COMPATIBLE"
    else:
        location_signal = "DIFFERENT"

    source_urls = bool(set(left.source_urls) & set(right.source_urls))
    canonical_urls = bool(set(left.source_canonical_urls) & set(right.source_canonical_urls))
    application_urls = bool(set(left.source_application_urls) & set(right.source_application_urls))
    date_a, date_b = _date_values(left), _date_values(right)
    distance = abs((date_a.date() - date_b.date()).days) if date_a and date_b else None
    date_signal = "UNKNOWN" if distance is None else ("NEARBY" if distance <= thresholds.nearby_days else "DISTANT")

    reasons: list[str] = []
    if title_exact and org_exact:
        classification = STRONG_CANDIDATE
        reasons.append("normalized title and organization are exact")
    elif (canonical_urls or application_urls or source_urls) and organization_similarity >= thresholds.possible_organization_similarity:
        classification = STRONG_CANDIDATE
        reasons.append("an exact URL is shared with a closely matching organization")
    elif title_similarity >= thresholds.possible_title_similarity and organization_similarity >= thresholds.possible_organization_similarity:
        classification = POSSIBLE_CANDIDATE
        reasons.append("title and organization meet possible-candidate thresholds")
    elif title_similarity >= thresholds.weak_title_similarity and organization_similarity >= thresholds.weak_organization_similarity:
        classification = WEAK_CANDIDATE
        reasons.append("title and organization meet weak-candidate thresholds")
    else:
        return None
    if location_signal in {"EXACT", "COMPATIBLE"}:
        reasons.append(f"location is {location_signal.lower()}")
    elif location_signal == "UNKNOWN":
        reasons.append("location comparison is unknown")
    if date_signal == "NEARBY":
        reasons.append(f"relevant dates are within {thresholds.nearby_days} days")

    return CandidatePair(
        left, right, title_exact, org_exact, title_similarity,
        organization_similarity, location_signal, source_urls, canonical_urls,
        application_urls, distance, date_signal, classification, tuple(reasons)
    )


def open_read_only_database(database: str | Path) -> sqlite3.Connection:
    """Open an existing SQLite file with OS/SQLite-enforced read-only access."""
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {path}")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def load_opportunities(connection: sqlite3.Connection) -> list[AuditOpportunity]:
    """Load persisted values and aggregate all source observations per row."""
    rows = connection.execute(
        """
        SELECT o.id, o.canonical_title, o.organization, o.location, o.description,
               o.published_at, o.discovered_at, o.first_seen_at, o.last_seen_at,
               o.source_url, o.application_url, o.canonical_url,
               os.source_id, os.source_url, os.application_url, os.canonical_url
        FROM opportunities o
        JOIN opportunity_sources os ON os.opportunity_id = o.id
        ORDER BY o.id, os.id
        """
    ).fetchall()
    grouped: dict[int, dict[str, object]] = {}
    for row in rows:
        item = grouped.setdefault(row[0], {"base": row[:12], "sources": [], "source_urls": [], "application_urls": [], "canonical_urls": []})
        item["sources"].append(row[12])
        item["source_urls"].append(row[13])
        if row[14]: item["application_urls"].append(row[14])
        if row[15]: item["canonical_urls"].append(row[15])
    result = []
    for item in grouped.values():
        base = item["base"]
        source_urls = set(item["source_urls"])
        application_urls = set(item["application_urls"])
        canonical_urls = set(item["canonical_urls"])
        source_urls.add(base[9])
        if base[10]:
            application_urls.add(base[10])
        if base[11]:
            canonical_urls.add(base[11])
        result.append(
            AuditOpportunity(
                *base,
                tuple(sorted(set(item["sources"]))),
                tuple(sorted(source_urls)),
                tuple(sorted(application_urls)),
                tuple(sorted(canonical_urls)),
            )
        )
    return result


def audit_opportunities(
    opportunities: list[AuditOpportunity],
    thresholds: AuditThresholds = DEFAULT_THRESHOLDS,
) -> AuditReport:
    sources = tuple(sorted({source for item in opportunities for source in item.sources}))
    examined = 0
    candidates = []
    for left, right in combinations(opportunities, 2):
        if not _has_different_sources(left, right):
            continue
        examined += 1
        candidate = compare_pair(left, right, thresholds)
        if candidate:
            candidates.append(candidate)
    rank = {STRONG_CANDIDATE: 3, POSSIBLE_CANDIDATE: 2, WEAK_CANDIDATE: 1}
    candidates.sort(key=lambda candidate: (rank[candidate.classification], candidate.organization_similarity, candidate.title_similarity), reverse=True)
    counts = {name: sum(c.classification == name for c in candidates) for name in rank}
    return AuditReport(len(opportunities), len(sources), sources, examined, len(candidates), counts, tuple(candidates))


def audit_database(database: str | Path) -> AuditReport:
    connection = open_read_only_database(database)
    try:
        return audit_opportunities(load_opportunities(connection))
    finally:
        connection.close()
