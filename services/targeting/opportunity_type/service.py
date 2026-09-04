"""Orchestration: read the stored types, derive the target set, count verdicts.

The only module that puts the pure evaluator and the database together, and
deliberately thin. It reads `opportunity_constraints.opportunity_type` for the
in-scope postings, derives the profile's declared target set, and reports the
verdicts.

**It writes nothing.** There is no `store_`, no `synchronize_`, no `INSERT`, no
`UPDATE` and no `DELETE` in this package at all: a verdict depends on a profile
and a profile changes, so it is computed on demand and reported, never
projected. Phase 7B.1 therefore adds no migration and no table — the schema it
runs against is the one `0024` left behind.

It also extracts nothing. The structured type is Phase 3.5A's answer, read back
as it was stored; this module does not re-read a title, does not re-classify
and has no special case for any particular posting. A posting whose stored type
disagrees with its title is a defect of the extractor, and the distribution
below is how it becomes **visible** rather than how it gets quietly repaired.

The scope is the one the constraint, qualification and geographic pipelines
already use — active postings that are not merged duplicates — so the audit
describes the same set of postings they do. A posting Phase 3.5A never read at
all is in scope and counted `UNKNOWN`, not dropped: a posting nobody read is
not a posting of another kind.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from services.digital_twin.preferences.models import OpportunityType
from services.targeting.opportunity_type.evaluator import evaluate_type_target
from services.targeting.opportunity_type.models import (
    TARGETING_VERSION,
    ProfileTypeTarget,
    TargetVerdict,
    TypeTargetAssessment,
)
from services.targeting.opportunity_type.profile_target import (
    resolve_profile_type_target,
)

__all__ = [
    "OpportunityTypeReading",
    "OpportunityTypeTargetingAudit",
    "TypeTargetingServiceError",
    "audit_opportunity_type_targeting",
    "explain_opportunity_type_target",
    "load_opportunity_type",
    "load_opportunity_types",
    "type_distribution",
]


class TypeTargetingServiceError(RuntimeError):
    """Raised when the type-targeting audit cannot safely proceed."""


#: Active, not a merged duplicate: the same collection filter the qualification,
#: constraint and geographic pipelines apply. It is never a judgement about
#: whether anybody could apply, and never about what kind of posting it is.
_IN_SCOPE = "o.is_active = 1 AND o.status != 'merged_duplicate'"

#: A LEFT JOIN on purpose. A posting Phase 3.5A has not read yet has no
#: constraints row, and an INNER JOIN would drop it from the denominator —
#: reporting a coverage the corpus does not have. It is read as UNKNOWN and
#: counted, which is what `constraints_read` below makes legible.
_TYPE_SQL = f"""
    SELECT o.id, c.opportunity_id, c.opportunity_type
      FROM opportunities AS o
      LEFT JOIN opportunity_constraints AS c ON c.opportunity_id = o.id
     WHERE {_IN_SCOPE}
"""


@dataclass(frozen=True)
class OpportunityTypeReading:
    """One posting's structured type, exactly as `0012` stored it.

    `constraints_read` is False when Phase 3.5A has no row for this posting at
    all, and that is a different fact from a row whose `opportunity_type` is
    `NULL`. Both are UNKNOWN to the evaluator — neither is `OUT_OF_TARGET` —
    but only one of them is fixed by running the extractor.
    """

    opportunity_id: int
    constraints_read: bool
    opportunity_type: OpportunityType | None

    @property
    def known(self) -> bool:
        return self.opportunity_type is not None


def _require_schema(connection: sqlite3.Connection) -> None:
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='opportunity_constraints'"
    ).fetchone() is None:
        raise TypeTargetingServiceError(
            "migration 0012 is required; explicitly apply migrations first"
        )


def _reading_from_row(row) -> OpportunityTypeReading:
    """Map one stored row. `NULL` stays UNKNOWN; it is never read as FALSE."""
    return OpportunityTypeReading(
        opportunity_id=int(row[0]),
        constraints_read=row[1] is not None,
        opportunity_type=None if row[2] is None else OpportunityType(str(row[2])),
    )


def load_opportunity_types(
    connection: sqlite3.Connection, *, limit: int | None = None
) -> tuple[OpportunityTypeReading, ...]:
    """The structured type of every in-scope posting, oldest id first.

    `limit` bounds postings, so it means the same thing here as it does in
    every other command: read at most this many postings.
    """
    if limit is not None and (isinstance(limit, bool) or limit < 1):
        raise ValueError("limit must be positive")
    sql = _TYPE_SQL + " ORDER BY o.id"
    parameters: tuple[int, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        parameters = (limit,)
    return tuple(
        _reading_from_row(row) for row in connection.execute(sql, parameters).fetchall()
    )


def load_opportunity_type(
    connection: sqlite3.Connection, opportunity_id: int
) -> OpportunityTypeReading | None:
    """One posting's structured type, or None when it is absent or excluded."""
    row = connection.execute(_TYPE_SQL + " AND o.id = ?", (opportunity_id,)).fetchone()
    return None if row is None else _reading_from_row(row)


def type_distribution(
    readings: Iterable[OpportunityTypeReading],
) -> dict[str, int]:
    """How many postings hold each structured type, plus how many hold none.

    Every member of the shared registry gets a key even at zero, so two runs
    over two corpora have the same shape, and `UNKNOWN` is one of the keys
    rather than the absence of one. Counters and registry values only: no
    title, no organization, no description and no posting id appears here.
    """
    counters = {member.value: 0 for member in OpportunityType}
    counters["UNKNOWN"] = 0
    for reading in readings:
        key = (
            reading.opportunity_type.value
            if reading.opportunity_type is not None
            else "UNKNOWN"
        )
        counters[key] += 1
    return counters


@dataclass(frozen=True)
class OpportunityTypeTargetingAudit:
    """One profile's type targeting, over the stored types. Nothing persisted.

    The three verdict counters partition the in-scope postings: every posting
    is counted exactly once, including the ones Phase 3.5A never read, which
    are `UNKNOWN` and not `OUT_OF_TARGET`.
    """

    profile_id: int
    profile_target_known: bool
    target_types: tuple[OpportunityType, ...]
    target_type_count: int
    target_rule_id: str
    total_opportunities: int
    constraints_read: int
    opportunity_type_known: int
    opportunity_type_unknown: int
    match: int
    out_of_target: int
    unknown: int
    distribution: dict[str, int]
    targeting_version: str

    def as_dict(self) -> dict[str, object]:
        """Flat counters, registry values, rule ids and a version. Nothing else.

        `target_types` is the closed-registry set the person named, which is
        what makes a MATCH count readable at all; it is not their words, not
        their address and not anything a CV said.
        """
        summary: dict[str, object] = {
            "profile_id": self.profile_id,
            "profile_target_known": self.profile_target_known,
            "target_types": ",".join(value.value for value in self.target_types)
            or "UNKNOWN",
            "target_type_count": self.target_type_count,
            "target_rule_id": self.target_rule_id,
            "total_opportunities": self.total_opportunities,
            "constraints_read": self.constraints_read,
            "opportunity_type_known": self.opportunity_type_known,
            "opportunity_type_unknown": self.opportunity_type_unknown,
            "match": self.match,
            "out_of_target": self.out_of_target,
            "unknown": self.unknown,
        }
        for key, value in self.distribution.items():
            summary[f"type_{key}"] = value
        summary["targeting_version"] = self.targeting_version
        return summary


def audit_opportunity_type_targeting(
    connection: sqlite3.Connection,
    profile_id: int,
    *,
    limit: int | None = None,
    target: ProfileTypeTarget | None = None,
) -> OpportunityTypeTargetingAudit:
    """Report how the stored types answer one profile's declared target set.

    Reads and counts. It extracts nothing, corrects nothing, stores no verdict
    and touches neither `profile_preferences` nor `opportunity_constraints`.
    """
    _require_schema(connection)
    resolved = target or resolve_profile_type_target(connection, profile_id)
    readings = load_opportunity_types(connection, limit=limit)

    verdicts = {verdict: 0 for verdict in TargetVerdict}
    for reading in readings:
        assessment = evaluate_type_target(
            resolved.opportunity_types, reading.opportunity_type
        )
        verdicts[assessment.verdict] += 1

    known = sum(1 for reading in readings if reading.known)
    return OpportunityTypeTargetingAudit(
        profile_id=profile_id,
        profile_target_known=resolved.known,
        target_types=resolved.opportunity_types,
        target_type_count=resolved.count,
        target_rule_id=resolved.rule_id,
        total_opportunities=len(readings),
        constraints_read=sum(1 for reading in readings if reading.constraints_read),
        opportunity_type_known=known,
        opportunity_type_unknown=len(readings) - known,
        match=verdicts[TargetVerdict.MATCH],
        out_of_target=verdicts[TargetVerdict.OUT_OF_TARGET],
        unknown=verdicts[TargetVerdict.UNKNOWN],
        distribution=type_distribution(readings),
        targeting_version=TARGETING_VERSION,
    )


def explain_opportunity_type_target(
    connection: sqlite3.Connection,
    profile_id: int,
    opportunity_id: int,
    *,
    target: ProfileTypeTarget | None = None,
) -> tuple[OpportunityTypeReading, TypeTargetAssessment]:
    """Why one posting got the verdict it got. Reads; stores nothing.

    The counters an audit prints say how many postings landed in each answer,
    not which rule put any one of them there. This is how a reading that looks
    wrong — a posting stored as `PFE` under a title that reads nothing like one
    — is inspected rather than argued about: it reports the stored type, the
    target set and the rule, and it reports them for whichever posting it was
    asked about, with no special case for any of them.

    It prints no title, no organization, no description and no URL, so an
    anomaly can be looked at without the posting's words leaving the database.
    """
    _require_schema(connection)
    reading = load_opportunity_type(connection, opportunity_id)
    if reading is None:
        raise LookupError(
            "no in-scope opportunity carries that id; it may be inactive, a "
            "merged duplicate, or absent"
        )
    resolved = target or resolve_profile_type_target(connection, profile_id)
    return reading, evaluate_type_target(
        resolved.opportunity_types, reading.opportunity_type
    )
