"""Orchestration: read descriptions, extract requirements, reconcile the rows.

The only module that puts the pure extractor and the repository together, and
deliberately thin. It selects the postings, hands each description to
`extract_opportunity_requirements` as a plain value object, and asks the
repository to store the result — one posting, one transaction.

**Phase 3.5A is a prerequisite, and the failure is loud.** Every 3.5B table
hangs off `opportunity_constraints`, so a posting with no 3.5A projection cannot
receive a 3.5B one. Rather than skip it quietly and report a coverage the
database does not have, the run refuses before writing anything and says what to
do about it. It does **not** run the 3.5A synchronization itself: two phases
that trigger each other are two phases nobody can reason about separately, and
an operator who wanted both can run both.

The selection mirrors 3.5A's, which mirrors the qualification pipeline: active
postings that are not merged duplicates, oldest id first. "In scope" is a
collection filter, never a judgement about whether anybody could apply.

**No profile table appears in any query in this module**, and none ever should.
What a posting requires is not a question about a person; joining the two sides
is Phase 3.6 and it does not exist.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Iterable

from services.collector.extractors.opportunity_constraints.requirements.extractor import (
    extract_opportunity_requirements,
    requirement_source_fingerprint,
)
from services.collector.extractors.opportunity_constraints.requirements.models import (
    REQUIREMENT_EXTRACTOR_VERSION,
    ExtractedRequirements,
    RequirementLevel,
    RequirementSource,
)
from services.collector.extractors.opportunity_constraints.requirements.repository import (
    store_opportunity_requirements,
    stored_requirement_signature,
)

__all__ = [
    "MISSING_CONSTRAINTS_ERROR",
    "OpportunityRequirementServiceError",
    "RequirementSyncSummary",
    "extract_one_opportunity_requirements",
    "load_requirement_source",
    "load_requirement_sources",
    "summarize_requirements",
    "synchronize_opportunity_requirements",
]


class OpportunityRequirementServiceError(RuntimeError):
    """Raised when requirement synchronization cannot safely proceed."""


MISSING_CONSTRAINTS_ERROR = (
    "some in-scope postings have no Phase 3.5A constraint projection; "
    "run Phase 3.5A constraint sync first"
)

#: Every posting the reading covers, with the one field it reads and a flag
#: saying whether 3.5A has been through it. Same scope filter as 3.5A.
_SOURCE_SQL = """
    SELECT o.id, o.description, c.opportunity_id IS NOT NULL
      FROM opportunities AS o
      LEFT JOIN opportunity_constraints AS c ON c.opportunity_id = o.id
     WHERE o.is_active = 1 AND o.status != 'merged_duplicate'
"""


def _require_schema(connection: sqlite3.Connection) -> None:
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='opportunity_requirement_extraction_state'"
    ).fetchone() is None:
        raise OpportunityRequirementServiceError(
            "migration 0013 is required; explicitly apply migrations first"
        )


def load_requirement_sources(
    connection: sqlite3.Connection, *, limit: int | None = None
) -> tuple[tuple[RequirementSource, bool], ...]:
    """Every in-scope posting's inputs and whether 3.5A has projected it."""
    if limit is not None and (isinstance(limit, bool) or limit < 1):
        raise ValueError("limit must be positive")
    sql = _SOURCE_SQL + " ORDER BY o.id"
    parameters: tuple[int, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        parameters = (limit,)
    return tuple(
        (
            RequirementSource(
                opportunity_id=int(row[0]),
                description=None if row[1] is None else str(row[1]),
            ),
            bool(row[2]),
        )
        for row in connection.execute(sql, parameters).fetchall()
    )


def load_requirement_source(
    connection: sqlite3.Connection, opportunity_id: int
) -> tuple[RequirementSource, bool] | None:
    """One posting's inputs, or None when it is absent or out of scope."""
    row = connection.execute(_SOURCE_SQL + " AND o.id = ?", (opportunity_id,)).fetchone()
    if row is None:
        return None
    return (
        RequirementSource(
            opportunity_id=int(row[0]),
            description=None if row[1] is None else str(row[1]),
        ),
        bool(row[2]),
    )


@dataclass(frozen=True)
class RequirementSyncSummary:
    """What one run did, and how much is now known. Counters, never a value.

    Every counter is "how many postings stated this" or "how many rows say so",
    never "how many are suitable". There is no coverage ratio, no score and no
    threshold: a posting requiring nothing is not a worse posting.
    """

    total: int
    processed: int
    unchanged: int
    created: int
    replaced: int
    opportunities_with_required_skills: int
    opportunities_with_preferred_skills: int
    required_skill_rows: int
    preferred_skill_rows: int
    opportunities_with_language_requirements: int
    required_language_rows: int
    preferred_language_rows: int
    skill_evidence_rows: int
    language_evidence_rows: int
    new_skill_vocabulary_rows: int
    ambiguity_rows: int
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
            "opportunities_with_required_skills": self.opportunities_with_required_skills,
            "opportunities_with_preferred_skills": self.opportunities_with_preferred_skills,
            "required_skill_rows": self.required_skill_rows,
            "preferred_skill_rows": self.preferred_skill_rows,
            "opportunities_with_language_requirements": (
                self.opportunities_with_language_requirements
            ),
            "required_language_rows": self.required_language_rows,
            "preferred_language_rows": self.preferred_language_rows,
            "skill_evidence_rows": self.skill_evidence_rows,
            "language_evidence_rows": self.language_evidence_rows,
            "new_skill_vocabulary_rows": self.new_skill_vocabulary_rows,
            "ambiguity_rows": self.ambiguity_rows,
            "extractor_version": self.extractor_version,
            "changed": self.changed,
        }


def summarize_requirements(
    readings: Iterable[ExtractedRequirements],
) -> dict[str, int]:
    """Count what is known across a set of readings. Never what is in them."""
    counters = {
        name: 0
        for name in (
            "opportunities_with_required_skills",
            "opportunities_with_preferred_skills",
            "required_skill_rows",
            "preferred_skill_rows",
            "opportunities_with_language_requirements",
            "required_language_rows",
            "preferred_language_rows",
            "skill_evidence_rows",
            "language_evidence_rows",
            "ambiguity_rows",
        )
    }
    for reading in readings:
        required_skills = reading.skills_at(RequirementLevel.REQUIRED)
        preferred_skills = reading.skills_at(RequirementLevel.PREFERRED)
        counters["opportunities_with_required_skills"] += bool(required_skills)
        counters["opportunities_with_preferred_skills"] += bool(preferred_skills)
        counters["required_skill_rows"] += len(required_skills)
        counters["preferred_skill_rows"] += len(preferred_skills)
        counters["opportunities_with_language_requirements"] += bool(reading.languages)
        counters["required_language_rows"] += len(
            reading.languages_at(RequirementLevel.REQUIRED)
        )
        counters["preferred_language_rows"] += len(
            reading.languages_at(RequirementLevel.PREFERRED)
        )
        counters["skill_evidence_rows"] += sum(
            len(item.evidence) for item in reading.skills
        )
        counters["language_evidence_rows"] += sum(
            len(item.evidence) for item in reading.languages
        )
        counters["ambiguity_rows"] += len(reading.ambiguities)
    return counters


def extract_one_opportunity_requirements(
    connection: sqlite3.Connection,
    opportunity_id: int,
    *,
    extracted_at: str | None = None,
) -> tuple[ExtractedRequirements, bool, int]:
    """Extract and store one posting. Returns the reading, whether it wrote, and
    how many vocabulary rows it created.

    `False` means the stored fingerprint and version already matched, so nothing
    was rewritten — not even a timestamp.

    There is no `extractor_version` argument, here or in
    `synchronize_opportunity_requirements`, and that is deliberate.
    `REQUIREMENT_EXTRACTOR_VERSION` is the version of *this code*, so it is the
    only honest label for what this code produced. A caller allowed to pass
    another one could ask for a recomputation under a version it invented; the
    run would re-extract and then store the reading under the real version
    anyway, because the reading carries its own — and the label would claim a
    version that never read the posting. Making a new version means editing the
    constant; the rows follow.
    """
    _require_schema(connection)
    loaded = load_requirement_source(connection, opportunity_id)
    if loaded is None:
        raise OpportunityRequirementServiceError(
            f"opportunity {opportunity_id} is absent, inactive or a merged duplicate"
        )
    source, projected = loaded
    if not projected:
        raise OpportunityRequirementServiceError(
            f"opportunity {opportunity_id} has no Phase 3.5A constraint projection; "
            "run Phase 3.5A constraint sync first"
        )
    signature = stored_requirement_signature(connection, opportunity_id)
    fingerprint = requirement_source_fingerprint(source)
    reading = extract_opportunity_requirements(source)
    if signature == (fingerprint, REQUIREMENT_EXTRACTOR_VERSION):
        return reading, False, 0
    created = store_opportunity_requirements(
        connection, reading, extracted_at=extracted_at
    )
    return reading, True, created


def synchronize_opportunity_requirements(
    connection: sqlite3.Connection,
    *,
    limit: int | None = None,
    extracted_at: str | None = None,
    after_source: Callable[[RequirementSource], None] | None = None,
) -> RequirementSyncSummary:
    """Bring every in-scope posting's requirement reading up to date.

    The 3.5A prerequisite is checked for the **whole** selection before anything
    is written, so a half-migrated corpus fails as one refusal rather than as a
    partly-written run.

    Each posting is then its own transaction, so one unreadable posting cannot
    undo the ones already reconciled — but the failure is raised rather than
    swallowed, because a run that silently skipped postings would report a
    coverage it does not have.

    A posting whose stored `(fingerprint, version)` already matches is left
    exactly as it is: no delete, no insert, no timestamp moved. That is what
    makes a second run write nothing and report `changed=false`, and it holds
    for a posting that requires nothing just as it does for one that requires a
    dozen things.

    `after_source` is a test seam invoked with each source before it is stored;
    production callers leave it unset.
    """
    _require_schema(connection)
    timestamp = extracted_at or datetime.now(UTC).isoformat(timespec="microseconds")
    loaded = load_requirement_sources(connection, limit=limit)
    if any(not projected for _, projected in loaded):
        raise OpportunityRequirementServiceError(MISSING_CONSTRAINTS_ERROR)

    readings: list[ExtractedRequirements] = []
    unchanged = created = replaced = vocabulary = 0
    for source, _ in loaded:
        signature = stored_requirement_signature(connection, source.opportunity_id)
        fingerprint = requirement_source_fingerprint(source)
        reading = extract_opportunity_requirements(source)
        readings.append(reading)
        if signature == (fingerprint, REQUIREMENT_EXTRACTOR_VERSION):
            unchanged += 1
            continue
        if after_source is not None:
            after_source(source)
        vocabulary += store_opportunity_requirements(
            connection, reading, extracted_at=timestamp
        )
        if signature is None:
            created += 1
        else:
            replaced += 1

    counters = summarize_requirements(readings)
    return RequirementSyncSummary(
        total=len(loaded),
        processed=len(loaded),
        unchanged=unchanged,
        created=created,
        replaced=replaced,
        new_skill_vocabulary_rows=vocabulary,
        extractor_version=REQUIREMENT_EXTRACTOR_VERSION,
        **counters,
    )
