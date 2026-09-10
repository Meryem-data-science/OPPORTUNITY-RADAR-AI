"""Read-only extraction of a frozen evaluation snapshot from operational SQLite.

Every statement in this module is a `SELECT`. There is no INSERT, no UPDATE, no
DELETE, no migration, no synchronization and no `persist=True` anywhere in the
package, and a test reads this file to keep it that way. The intended entry
point opens the database through `connect_readonly_database`, the project's own
`mode=ro` URI helper, so the guarantee is enforced by SQLite and not merely
documented here.

Three properties are what this module is for.

**One database state.** The extraction is several dozen `SELECT`s across ten
tables. Python's `sqlite3` opens a transaction only for DML, so without a
boundary each of those statements would be its own implicit read transaction and
a writer committing in between would leave the snapshot describing a mixture of
two states — a Matching run from before a synchronization beside opportunities
from after it. A single deferred `BEGIN` held across the whole read, released
with `ROLLBACK`, is the same pattern Phase 9B.2a established for its read model,
and it is used here for the same reason.

**Canonical order.** Records come out ordered by `opportunity_id`, which is an
INTEGER PRIMARY KEY and therefore a total order with no tie to break. No
collection anywhere in a record is left in whatever sequence SQLite returned it.

**Nothing repaired, nothing invented.** Where an upstream produced nothing, the
record says `None`. A missing eligibility decision does not become INELIGIBLE, an
opportunity the Recommendation Engine never ranked does not become a zero-scored
one, and `UNCERTAIN`/`UNKNOWN` states travel through untouched.
"""

from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .cohort import count_excluded_opportunities, select_evaluation_cohort_ids
from .fingerprint import evaluation_content_fingerprint
from .readers import (
    read_absorbed_duplicates,
    read_eligibilities,
    read_geography,
    read_matching,
    read_opportunities,
    read_qualifications,
    read_recommendation,
    read_source_identity,
    read_sources,
    resolve_user_id,
)
from .schema import (
    EVALUATION_CANONICAL_ORDER,
    EVALUATION_COHORT_CRITERIA,
    EVALUATION_COHORT_VERSION,
    EVALUATION_DATASET_SCHEMA_VERSION,
    EvaluationCohortDefinition,
    EvaluationDataset,
    EvaluationDatasetError,
    EvaluationDatasetManifest,
    EvaluationEligibilityRecord,
    EvaluationMatchingRecord,
    EvaluationOpportunityRecord,
    EvaluationQualificationRecord,
    EvaluationRecommendationRecord,
    EvaluationUpstreamProvenance,
)

__all__ = [
    "build_evaluation_dataset",
    "resolve_git_commit",
]


def resolve_git_commit(repository_root: str | Path = ".") -> str | None:
    """Return the current commit, or `None` when it cannot be read cleanly.

    Deliberately optional and deliberately not guessed. A snapshot taken from a
    tarball, a container without `git`, or a directory that is not a checkout
    has no commit to record, and writing a plausible-looking one would be worse
    than writing nothing. The manifest field is nullable for exactly this case.
    """
    try:
        completed = subprocess.run(
            ("git", "-C", str(repository_root), "rev-parse", "HEAD"),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    commit = completed.stdout.strip()
    return commit or None


@contextmanager
def _read_snapshot(connection: sqlite3.Connection) -> Iterator[None]:
    """Hold one deferred read transaction across the whole extraction.

    A plain `BEGIN` takes no lock until the first read and then pins one
    snapshot for every statement after it. Never `BEGIN IMMEDIATE`: that
    reserves the write lock, which is precisely what a read-only extraction must
    not do, and it would fail outright on a `mode=ro` connection.

    A transaction the caller already owns is borrowed and left open — ending
    someone else's transaction would silently finish work this module cannot
    see. One opened here is always ended with `ROLLBACK`, the one ending that
    cannot write even by accident.
    """
    if connection.in_transaction:
        yield
        return
    try:
        connection.execute("BEGIN")
    except sqlite3.Error as error:
        raise EvaluationDatasetError(
            "cannot open a read snapshot of the operational database"
        ) from error
    try:
        yield
    except BaseException:
        # The failure inside the body is the report the caller needs; a failure
        # while releasing the snapshot must not replace it.
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error as error:
        raise EvaluationDatasetError(
            "cannot release the read snapshot of the operational database"
        ) from error


def build_evaluation_dataset(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    database_path: str | Path,
    git_commit: str | None = None,
    generated_at: str | None = None,
) -> EvaluationDataset:
    """Extract one frozen evaluation dataset, reading and writing nothing else.

    `generated_at` and `git_commit` are supplied by the caller rather than taken
    here, so the library stays a pure function of the database it is pointed at
    and a test can hold the clock still. Neither reaches the content
    fingerprint.
    """
    if not isinstance(profile_id, int) or isinstance(profile_id, bool):
        raise EvaluationDatasetError("profile_id must be an integer")
    if profile_id <= 0:
        raise EvaluationDatasetError("profile_id must be a positive integer")

    with _read_snapshot(connection):
        user_id = resolve_user_id(connection, profile_id)
        cohort_ids = select_evaluation_cohort_ids(connection)
        cohort = frozenset(cohort_ids)
        opportunities = read_opportunities(connection, cohort)
        sources = read_sources(connection, cohort)
        absorbed = read_absorbed_duplicates(connection, cohort)
        qualifications = read_qualifications(connection, cohort)
        geography = read_geography(connection, cohort)
        eligibilities = read_eligibilities(connection, cohort, user_id)
        matching, matching_provenance = read_matching(connection, profile_id)
        recommendation, recommendation_provenance = read_recommendation(
            connection, profile_id
        )
        excluded_counts = count_excluded_opportunities(connection)
        source_identity = read_source_identity(connection, database_path)

    upstream = EvaluationUpstreamProvenance(
        **matching_provenance,  # type: ignore[arg-type]
        **recommendation_provenance,  # type: ignore[arg-type]
    )

    records = tuple(
        _build_record(
            row=opportunities[opportunity_id],
            sources=sources.get(opportunity_id, ()),
            absorbed=absorbed.get(opportunity_id, ()),
            qualifications=qualifications,
            geography=geography.get(opportunity_id, ()),
            eligibility=eligibilities.get(opportunity_id),
            matching=matching.get(opportunity_id),
            recommendation=recommendation.get(opportunity_id),
        )
        # `cohort_ids` is already the canonical order, straight from the one
        # `ORDER BY` that defines it.
        for opportunity_id in cohort_ids
    )

    cohort_definition = EvaluationCohortDefinition(
        version=EVALUATION_COHORT_VERSION,
        criteria=EVALUATION_COHORT_CRITERIA,
        ordering=EVALUATION_CANONICAL_ORDER,
        excluded_counts=excluded_counts,
    )
    content_fingerprint = evaluation_content_fingerprint(
        schema_version=EVALUATION_DATASET_SCHEMA_VERSION,
        cohort=cohort_definition,
        upstream=upstream,
        records=records,
    )
    manifest = EvaluationDatasetManifest(
        schema_version=EVALUATION_DATASET_SCHEMA_VERSION,
        # Derived from the content, so the same data always lands in the same
        # directory and a second extraction overwrites itself byte for byte
        # instead of accumulating near-identical snapshots.
        dataset_id=f"{EVALUATION_DATASET_SCHEMA_VERSION}-{content_fingerprint[:16]}",
        generated_at=(
            datetime.now(UTC).isoformat() if generated_at is None else generated_at
        ),
        git_commit=git_commit,
        profile_id=profile_id,
        user_id=user_id,
        record_count=len(records),
        source=source_identity,
        cohort=cohort_definition,
        upstream=upstream,
        content_fingerprint=content_fingerprint,
    )
    return EvaluationDataset(manifest=manifest, records=records)


def _build_record(
    *,
    row: tuple,
    sources,
    absorbed,
    qualifications: Mapping[int, EvaluationQualificationRecord],
    geography,
    eligibility: EvaluationEligibilityRecord | None,
    matching: EvaluationMatchingRecord | None,
    recommendation: EvaluationRecommendationRecord | None,
) -> EvaluationOpportunityRecord:
    opportunity_id = int(row[0])
    qualification = qualifications.get(opportunity_id)
    if qualification is None:
        # Unreachable through the cohort's INNER JOIN, and refused rather than
        # papered over: a cohort member with no qualification row would mean
        # the selection and this read disagree about what the database holds.
        raise EvaluationDatasetError(
            f"opportunity {opportunity_id} is in the cohort without a "
            "persisted qualification"
        )
    return EvaluationOpportunityRecord(
        opportunity_id=opportunity_id,
        canonical_title=str(row[1]),
        organization=str(row[2]),
        opportunity_type=None if row[3] is None else str(row[3]),
        employment_type=None if row[4] is None else str(row[4]),
        location=None if row[5] is None else str(row[5]),
        country=None if row[6] is None else str(row[6]),
        remote_type=None if row[7] is None else str(row[7]),
        source_url=str(row[8]),
        application_url=None if row[9] is None else str(row[9]),
        canonical_url=None if row[10] is None else str(row[10]),
        status=str(row[11]),
        is_active=bool(row[12]),
        published_at=None if row[13] is None else str(row[13]),
        deadline=None if row[14] is None else str(row[14]),
        discovered_at=str(row[15]),
        first_seen_at=str(row[16]),
        last_seen_at=str(row[17]),
        absorbed_duplicate_ids=tuple(absorbed),
        sources=tuple(sources),
        qualification=qualification,
        geography_segments=tuple(geography),
        eligibility=eligibility,
        matching=matching,
        recommendation=recommendation,
    )
