"""Orchestration: read the collected location strings, resolve, reconcile.

The only module that puts the pure resolver and the repository together, and
deliberately thin. It selects the `opportunity_constraint_locations` rows of
in-scope postings, hands each string to `resolve_location_text`, and asks the
repository to store the result — one source row, one transaction. A failure on
one row rolls that row back and stops the run rather than leaving a mixture of
readings behind.

The scope is the one the constraint and qualification pipelines already use —
active postings that are not merged duplicates — so the three projections
describe one set of postings rather than three overlapping ones.

`audit_geographic_targeting` is the other half: it reads the **stored**
projection, derives the profile's target country, and reports the verdicts. It
computes them and prints counters; it stores nothing, because a verdict is a
statement about a profile and a profile changes. Run `sync` first — the audit
reports what is on disk, and stale rows are reported as what they are.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from services.geography.evaluator import evaluate_target
from services.geography.models import (
    RESOLVER_VERSION,
    LocationResolution,
    LocationSource,
    ProfileTarget,
    ResolutionStatus,
    TargetVerdict,
)
from services.geography.profile_target import resolve_profile_target
from services.geography.repository import (
    read_opportunity_resolutions,
    store_location_resolutions,
    stored_signature,
)
from services.geography.resolver import location_fingerprint, resolve_location_text

__all__ = [
    "GeographicServiceError",
    "GeographicTargetingAudit",
    "LocationResolutionSyncSummary",
    "audit_geographic_targeting",
    "in_scope_opportunity_ids",
    "load_location_sources",
    "resolve_one_location",
    "summarize_resolutions",
    "synchronize_location_resolutions",
]


class GeographicServiceError(RuntimeError):
    """Raised when the geographic projection cannot safely proceed."""


#: Active, not a merged duplicate: the same collection filter the qualification
#: and constraint pipelines apply. It is never a judgement about whether
#: anybody could apply, and never about where a posting is.
_IN_SCOPE = "o.is_active = 1 AND o.status != 'merged_duplicate'"

_SOURCE_SQL = f"""
    SELECT l.opportunity_id, l.id, l.position, l.location_text
      FROM opportunity_constraint_locations AS l
      JOIN opportunities AS o ON o.id = l.opportunity_id
     WHERE {_IN_SCOPE}
"""

_SCOPE_SQL = f"SELECT o.id FROM opportunities AS o WHERE {_IN_SCOPE} ORDER BY o.id"


def _require_schema(connection: sqlite3.Connection) -> None:
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='opportunity_location_resolutions'"
    ).fetchone() is None:
        raise GeographicServiceError(
            "migration 0024 is required; explicitly apply migrations first"
        )


def in_scope_opportunity_ids(
    connection: sqlite3.Connection, *, limit: int | None = None
) -> tuple[int, ...]:
    """Every posting the projection covers, oldest id first."""
    if limit is not None and (isinstance(limit, bool) or limit < 1):
        raise ValueError("limit must be positive")
    sql = _SCOPE_SQL
    parameters: tuple[int, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        parameters = (limit,)
    return tuple(int(row[0]) for row in connection.execute(sql, parameters).fetchall())


def load_location_sources(
    connection: sqlite3.Connection, *, limit: int | None = None
) -> tuple[LocationSource, ...]:
    """The collected location rows of in-scope postings, in collection order.

    `limit` bounds **postings**, not rows, so it means the same thing here as
    it does in every other command: read at most this many postings.
    """
    scope = in_scope_opportunity_ids(connection, limit=limit)
    if limit is not None and not scope:
        return ()
    sql = _SOURCE_SQL
    parameters: tuple[int, ...] = ()
    if limit is not None:
        placeholders = ", ".join("?" for _ in scope)
        sql += f" AND l.opportunity_id IN ({placeholders})"
        parameters = scope
    sql += " ORDER BY l.opportunity_id, l.position"
    return tuple(
        LocationSource(
            opportunity_id=int(row[0]),
            source_location_id=int(row[1]),
            position=int(row[2]),
            location_text=str(row[3]),
        )
        for row in connection.execute(sql, parameters).fetchall()
    )


@dataclass(frozen=True)
class LocationResolutionSyncSummary:
    """What one run did, and what is now resolved. Counters, never a place.

    `resolved`, `ambiguous` and `unknown` count **segments**, because a segment
    is the unit that gets a status: one string naming three cities is three
    answers. `known_country` counts the distinct countries the whole run
    resolved, which is how an operator sees at a glance that a corpus is not
    all one country.
    """

    total_opportunities: int
    source_location_rows: int
    processed: int
    unchanged: int
    created: int
    replaced: int
    segments: int
    resolved: int
    ambiguous: int
    unknown: int
    known_country: int
    resolver_version: str

    @property
    def changed(self) -> bool:
        return bool(self.created or self.replaced)

    def as_dict(self) -> dict[str, object]:
        return {
            "total_opportunities": self.total_opportunities,
            "source_location_rows": self.source_location_rows,
            "processed": self.processed,
            "unchanged": self.unchanged,
            "created": self.created,
            "replaced": self.replaced,
            "segments": self.segments,
            "resolved": self.resolved,
            "ambiguous": self.ambiguous,
            "unknown": self.unknown,
            "known_country": self.known_country,
            "resolver_version": self.resolver_version,
            "changed": self.changed,
        }


def summarize_resolutions(
    resolutions: Iterable[LocationResolution],
) -> dict[str, int]:
    """Count the statuses of a set of segments. Never the places in them."""
    counters = {"segments": 0, "resolved": 0, "ambiguous": 0, "unknown": 0}
    countries: set[str] = set()
    for resolution in resolutions:
        counters["segments"] += 1
        if resolution.status is ResolutionStatus.RESOLVED:
            counters["resolved"] += 1
            if resolution.country_code is not None:
                countries.add(resolution.country_code)
        elif resolution.status is ResolutionStatus.AMBIGUOUS:
            counters["ambiguous"] += 1
        else:
            counters["unknown"] += 1
    counters["known_country"] = len(countries)
    return counters


def resolve_one_location(
    connection: sqlite3.Connection,
    source: LocationSource,
    *,
    resolved_at: str | None = None,
) -> tuple[bool, int]:
    """Resolve and store one source row. Returns whether it wrote, and how.

    `(False, 0)` means the stored fingerprint and version already matched, so
    nothing was rewritten — not even a timestamp. The second element is `1` for
    a first reading and `2` for a replaced one, which is what the summary
    counts.

    There is no `resolver_version` argument, here or in
    `synchronize_location_resolutions`, and that is deliberate — the same
    reasoning `extract_one_opportunity` spells out for the extractor.
    `RESOLVER_VERSION` is the version of *this code*, so it is the only honest
    label for what this code produced; a caller allowed to name another one
    could store rows claiming rules that never read them.
    """
    signature = stored_signature(connection, source.source_location_id)
    fingerprint = location_fingerprint(source.location_text)
    if signature == (fingerprint, RESOLVER_VERSION):
        return False, 0
    segments = resolve_location_text(source.location_text)
    store_location_resolutions(
        connection,
        source,
        segments,
        resolver_version=RESOLVER_VERSION,
        source_fingerprint=fingerprint,
        resolved_at=resolved_at,
    )
    return True, 1 if signature is None else 2


def synchronize_location_resolutions(
    connection: sqlite3.Connection,
    *,
    limit: int | None = None,
    resolved_at: str | None = None,
    after_source: Callable[[LocationSource], None] | None = None,
) -> LocationResolutionSyncSummary:
    """Bring every in-scope posting's geographic projection up to date.

    Each source row is its own transaction, so one unreadable string cannot
    undo the rows already reconciled — but the failure is raised rather than
    swallowed, because a run that silently skipped rows would report a coverage
    it does not have.

    A source row whose stored `(fingerprint, version)` already matches is left
    exactly as it is: no delete, no insert, no timestamp moved. That is what
    makes a second run write nothing and report `changed=false`. A row stored
    under an older `resolver_version` is re-read even when its string is
    identical.

    `after_source` is a test seam invoked with each source before it is stored;
    production callers leave it unset.
    """
    _require_schema(connection)
    timestamp = resolved_at or datetime.now(UTC).isoformat(timespec="microseconds")
    scope = in_scope_opportunity_ids(connection, limit=limit)
    sources = load_location_sources(connection, limit=limit)

    unchanged = created = replaced = 0
    segments: list[LocationResolution] = []
    for source in sources:
        if after_source is not None:
            after_source(source)
        wrote, kind = resolve_one_location(connection, source, resolved_at=timestamp)
        if not wrote:
            unchanged += 1
        elif kind == 1:
            created += 1
        else:
            replaced += 1

    for opportunity_id in scope:
        segments.extend(read_opportunity_resolutions(connection, opportunity_id))

    counters = summarize_resolutions(segments)
    return LocationResolutionSyncSummary(
        total_opportunities=len(scope),
        source_location_rows=len(sources),
        processed=len(sources),
        unchanged=unchanged,
        created=created,
        replaced=replaced,
        resolver_version=RESOLVER_VERSION,
        **counters,
    )


@dataclass(frozen=True)
class GeographicTargetingAudit:
    """One profile's targeting, over the stored projection. Nothing persisted.

    The three verdict counters partition the in-scope postings: every posting
    is counted exactly once, including the ones with no location at all, which
    are `UNKNOWN` and not `OUT_OF_TARGET`.
    """

    profile_id: int
    target_country_code: str | None
    target_rule_id: str
    total_opportunities: int
    opportunities_with_locations: int
    source_location_rows: int
    segments: int
    resolved: int
    ambiguous: int
    unknown: int
    resolved_target_country: int
    resolved_other_country: int
    match: int
    out_of_target: int
    unknown_target: int
    resolver_version: str

    def as_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "target_country_code": self.target_country_code or "UNKNOWN",
            "target_rule_id": self.target_rule_id,
            "total_opportunities": self.total_opportunities,
            "opportunities_with_locations": self.opportunities_with_locations,
            "source_location_rows": self.source_location_rows,
            "segments": self.segments,
            "resolved": self.resolved,
            "ambiguous": self.ambiguous,
            "unknown": self.unknown,
            "resolved_target_country": self.resolved_target_country,
            "resolved_other_country": self.resolved_other_country,
            "match": self.match,
            "out_of_target": self.out_of_target,
            "unknown_target": self.unknown_target,
            "resolver_version": self.resolver_version,
        }


def audit_geographic_targeting(
    connection: sqlite3.Connection,
    profile_id: int,
    *,
    limit: int | None = None,
    target: ProfileTarget | None = None,
) -> GeographicTargetingAudit:
    """Report how the stored projection answers one profile's target country.

    Reads and counts. It resolves nothing, stores no verdict and touches
    neither `profile_mobility` nor `opportunity_constraint_locations`.
    """
    _require_schema(connection)
    resolved_target = target or resolve_profile_target(connection, profile_id)
    scope = in_scope_opportunity_ids(connection, limit=limit)

    segments: list[LocationResolution] = []
    with_locations = 0
    verdicts = {verdict: 0 for verdict in TargetVerdict}
    for opportunity_id in scope:
        resolutions = read_opportunity_resolutions(connection, opportunity_id)
        segments.extend(resolutions)
        with_locations += bool(resolutions)
        assessment = evaluate_target(resolved_target.country_code, resolutions)
        verdicts[assessment.verdict] += 1

    counters = summarize_resolutions(segments)
    in_target = sum(
        1
        for segment in segments
        if segment.status is ResolutionStatus.RESOLVED
        and segment.country_code == resolved_target.country_code
        and resolved_target.country_code is not None
    )
    return GeographicTargetingAudit(
        profile_id=profile_id,
        target_country_code=resolved_target.country_code,
        target_rule_id=resolved_target.rule_id,
        total_opportunities=len(scope),
        opportunities_with_locations=with_locations,
        source_location_rows=len({segment.source_location_id for segment in segments}),
        segments=counters["segments"],
        resolved=counters["resolved"],
        ambiguous=counters["ambiguous"],
        unknown=counters["unknown"],
        resolved_target_country=in_target,
        resolved_other_country=counters["resolved"] - in_target,
        match=verdicts[TargetVerdict.MATCH],
        out_of_target=verdicts[TargetVerdict.OUT_OF_TARGET],
        unknown_target=verdicts[TargetVerdict.UNKNOWN],
        resolver_version=RESOLVER_VERSION,
    )
