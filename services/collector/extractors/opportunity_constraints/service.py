"""Orchestration: read postings, extract, and reconcile the projection.

This is the only module that puts the pure extractor and the repository
together, and it is deliberately thin. It selects the postings, hands each one
to `extract_opportunity_constraints` as a plain value object, and asks the
repository to store the result — one posting, one transaction. A failure on one
posting rolls that posting back and stops the run rather than leaving a mixture
of readings behind.

The selection mirrors the qualification pipeline: active postings that are not
merged duplicates, oldest id first. The `opportunity_type` the Phase 2
classifier already computed is joined in as one more input, because it is the
only structured type this database actually holds — see below.

**A note the audit made necessary.** `opportunities` declares `country`,
`remote_type`, `opportunity_type`, `employment_type` and `deadline`, but no
collector writes any of them: the insert in
`services/collector/database/opportunities.py` sets title, organization,
location, description, published_at, the timestamps, the URLs and the status,
and nothing else. So in practice `remote_type` and `country` are `NULL` for
every row, and the structured type lives in `opportunity_qualifications`. The
extractor still reads all of them — a `LEFT JOIN`, and `None` where the column
is empty — because the columns are real, a later collector may fill them, and
a rule that ignores a populated field would be a bug waiting for that day. What
it does **not** do is compensate for their absence by guessing: a posting with
no `remote_type` and no explicit wording has an UNKNOWN work mode, not an
`ON_SITE` one deduced from having an address.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Iterable

from services.collector.extractors.opportunity_constraints.extractor import (
    extract_opportunity_constraints,
    source_fingerprint,
)
from services.collector.extractors.opportunity_constraints.models import (
    EXTRACTOR_VERSION,
    ConstraintKind,
    ConventionRequirement,
    ExtractedConstraints,
    OpportunitySource,
    VisaSponsorship,
    WorkAuthorization,
)
from services.collector.extractors.opportunity_constraints.repository import (
    OpportunityConstraintRepositoryError,
    store_opportunity_constraints,
    stored_signature,
)

__all__ = [
    "ConstraintSyncSummary",
    "OpportunityConstraintServiceError",
    "extract_one_opportunity",
    "load_opportunity_source",
    "load_opportunity_sources",
    "summarize_constraints",
    "synchronize_opportunity_constraints",
]


class OpportunityConstraintServiceError(RuntimeError):
    """Raised when constraint synchronization cannot safely proceed."""


#: Every posting the projection covers, with the one derived input it uses.
#: "In scope" here is a collection filter — active, not a merged duplicate —
#: and never a judgement about whether anybody could apply.
#: `is_active = 1 AND status != 'merged_duplicate'` is the same filter the
#: qualification pipeline applies, so the two projections describe one set.
_SOURCE_SQL = """
    SELECT o.id, o.canonical_title, o.description, o.location, o.country,
           o.remote_type, q.opportunity_type
      FROM opportunities AS o
      LEFT JOIN opportunity_qualifications AS q ON q.opportunity_id = o.id
     WHERE o.is_active = 1 AND o.status != 'merged_duplicate'
"""


def _source_from_row(row) -> OpportunitySource:
    return OpportunitySource(
        opportunity_id=int(row[0]),
        canonical_title=None if row[1] is None else str(row[1]),
        description=None if row[2] is None else str(row[2]),
        location=None if row[3] is None else str(row[3]),
        country=None if row[4] is None else str(row[4]),
        remote_type=None if row[5] is None else str(row[5]),
        qualification_type=None if row[6] is None else str(row[6]),
    )


def _require_schema(connection: sqlite3.Connection) -> None:
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='opportunity_constraints'"
    ).fetchone() is None:
        raise OpportunityConstraintServiceError(
            "migration 0012 is required; explicitly apply migrations first"
        )


def load_opportunity_sources(
    connection: sqlite3.Connection, *, limit: int | None = None
) -> tuple[OpportunitySource, ...]:
    """Every in-scope posting's extractor inputs, oldest id first."""
    if limit is not None and (isinstance(limit, bool) or limit < 1):
        raise ValueError("limit must be positive")
    sql = _SOURCE_SQL + " ORDER BY o.id"
    parameters: tuple[int, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        parameters = (limit,)
    return tuple(
        _source_from_row(row) for row in connection.execute(sql, parameters).fetchall()
    )


def load_opportunity_source(
    connection: sqlite3.Connection, opportunity_id: int
) -> OpportunitySource | None:
    """One posting's extractor inputs, or None when it is absent or excluded."""
    row = connection.execute(
        _SOURCE_SQL + " AND o.id = ?", (opportunity_id,)
    ).fetchone()
    return None if row is None else _source_from_row(row)


@dataclass(frozen=True)
class ConstraintSyncSummary:
    """What one run did, and how much is now known. Counters, never a value.

    Every `known_*` counter is "how many postings stated this", never "how many
    are suitable": a posting that never mentions visas is counted as not
    stating it, which is different from stating that it will not sponsor. The
    summary is printed and logged, and a posting's words are not.
    """

    total: int
    processed: int
    unchanged: int
    created: int
    replaced: int
    known_opportunity_type: int
    known_education: int
    known_experience: int
    known_duration: int
    known_start: int
    known_location: int
    known_work_mode: int
    known_visa_sponsorship: int
    known_work_authorization: int
    known_convention: int
    conflicts: int
    extractor_version: str

    @property
    def changed(self) -> bool:
        return bool(self.created or self.replaced)

    def as_dict(self) -> dict[str, object]:
        return {
            "total_opportunities": self.total,
            "processed": self.processed,
            "unchanged": self.unchanged,
            "created": self.created,
            "replaced": self.replaced,
            "known_opportunity_type": self.known_opportunity_type,
            "known_education": self.known_education,
            "known_experience": self.known_experience,
            "known_duration": self.known_duration,
            "known_start": self.known_start,
            "known_location": self.known_location,
            "known_work_mode": self.known_work_mode,
            "known_visa_sponsorship": self.known_visa_sponsorship,
            "known_work_authorization": self.known_work_authorization,
            "known_convention": self.known_convention,
            "conflicts": self.conflicts,
            "extractor_version": self.extractor_version,
            "changed": self.changed,
        }


def summarize_constraints(
    readings: Iterable[ExtractedConstraints],
) -> dict[str, int]:
    """Count what is known across a set of readings. Never what is in them."""
    counters = {
        name: 0
        for name in (
            "known_opportunity_type", "known_education", "known_experience",
            "known_duration", "known_start", "known_location",
            "known_work_mode", "known_visa_sponsorship",
            "known_work_authorization", "known_convention", "conflicts",
        )
    }
    for reading in readings:
        counters["known_opportunity_type"] += reading.opportunity_type is not None
        counters["known_education"] += bool(reading.education)
        counters["known_experience"] += reading.experience.known
        counters["known_duration"] += reading.duration.known
        counters["known_start"] += reading.start.known
        counters["known_location"] += bool(reading.locations)
        counters["known_work_mode"] += reading.work_mode is not None
        counters["known_visa_sponsorship"] += (
            reading.visa_sponsorship is not VisaSponsorship.UNKNOWN
        )
        counters["known_work_authorization"] += (
            reading.work_authorization is not WorkAuthorization.UNKNOWN
        )
        counters["known_convention"] += (
            reading.convention is not ConventionRequirement.UNKNOWN
        )
        counters["conflicts"] += len(reading.conflicts)
    return counters


def extract_one_opportunity(
    connection: sqlite3.Connection,
    opportunity_id: int,
    *,
    extractor_version: str = EXTRACTOR_VERSION,
    extracted_at: str | None = None,
) -> tuple[ExtractedConstraints, bool]:
    """Extract and store one posting. Returns the reading and whether it wrote.

    `False` means the stored fingerprint and version already matched, so
    nothing was rewritten — not even a timestamp.
    """
    _require_schema(connection)
    source = load_opportunity_source(connection, opportunity_id)
    if source is None:
        raise OpportunityConstraintServiceError(
            f"opportunity {opportunity_id} is absent, inactive or a merged duplicate"
        )
    signature = stored_signature(connection, opportunity_id)
    fingerprint = source_fingerprint(source)
    reading = extract_opportunity_constraints(source)
    if signature == (fingerprint, extractor_version):
        return reading, False
    store_opportunity_constraints(connection, reading, extracted_at=extracted_at)
    return reading, True


def synchronize_opportunity_constraints(
    connection: sqlite3.Connection,
    *,
    limit: int | None = None,
    extractor_version: str = EXTRACTOR_VERSION,
    extracted_at: str | None = None,
    after_source: Callable[[OpportunitySource], None] | None = None,
) -> ConstraintSyncSummary:
    """Bring every in-scope posting's projection up to date.

    Each posting is its own transaction, so one unreadable posting cannot undo
    the ones already reconciled — but the failure is raised rather than
    swallowed, because a run that silently skipped postings would report a
    coverage it does not have.

    A posting whose stored `(fingerprint, version)` already matches is left
    exactly as it is: no delete, no insert, no timestamp moved. That is what
    makes a second run write nothing and report `changed=false`.

    `after_source` is a test seam invoked with each source before it is stored;
    production callers leave it unset.
    """
    _require_schema(connection)
    if not extractor_version.strip():
        raise ValueError("extractor_version must not be empty")
    timestamp = extracted_at or datetime.now(UTC).isoformat(timespec="microseconds")
    sources = load_opportunity_sources(connection, limit=limit)

    readings: list[ExtractedConstraints] = []
    unchanged = created = replaced = 0
    for source in sources:
        signature = stored_signature(connection, source.opportunity_id)
        fingerprint = source_fingerprint(source)
        reading = extract_opportunity_constraints(source)
        readings.append(reading)
        if signature == (fingerprint, extractor_version):
            unchanged += 1
            continue
        if after_source is not None:
            after_source(source)
        store_opportunity_constraints(connection, reading, extracted_at=timestamp)
        if signature is None:
            created += 1
        else:
            replaced += 1

    counters = summarize_constraints(readings)
    return ConstraintSyncSummary(
        total=len(sources),
        processed=len(sources),
        unchanged=unchanged,
        created=created,
        replaced=replaced,
        extractor_version=extractor_version,
        **counters,
    )
