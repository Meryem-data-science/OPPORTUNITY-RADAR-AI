"""One `SELECT` per upstream table, and nothing but `SELECT`s.

Split out of `snapshot.py` so that the orchestration — cohort, canonical order,
fingerprint, manifest — stays readable beside the row-by-row reading it drives.
Every function here takes an open connection and returns plain records; none of
them opens a transaction, decides an order that is not the canonical one, or
writes anything.

Two conventions run through the module.

* **A raw `sqlite3.Error` is never the contract.** Each read is wrapped so a
  locked database, a missing table or a schema older than this build surfaces as
  an `EvaluationDatasetError` naming what could not be read, with the original
  error kept as its cause.
* **NULL survives.** A nullable column reads back as `None`, never as `False`,
  `0.0`, `""` or `[]`. `0025`'s two distinct meanings of NULL, `0014`'s UNKNOWN
  verdict and `0024`'s unresolved country all depend on it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from services.collector.matching.read_model import (
    MatchingReadError,
    read_current_matching,
)
from services.recommendation.read_model import (
    RecommendationReadError,
    read_current_recommendation,
)

from .schema import (
    EvaluationDatasetError,
    EvaluationEligibilityRecord,
    EvaluationGeographySegmentRecord,
    EvaluationMatchingRecord,
    EvaluationQualificationRecord,
    EvaluationRecommendationRecord,
    EvaluationSourceIdentity,
    EvaluationSourceRecord,
)

__all__ = [
    "decode_optional_string_list",
    "query",
    "read_absorbed_duplicates",
    "read_eligibilities",
    "read_geography",
    "read_matching",
    "read_opportunities",
    "read_qualifications",
    "read_recommendation",
    "read_source_identity",
    "read_sources",
    "resolve_user_id",
]

#: Read in 1 MiB blocks so a multi-gigabyte database is identified without being
#: loaded into memory. The database is digested, never copied.
_HASH_BLOCK_BYTES = 1024 * 1024


def query(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple,
    description: str,
) -> list[tuple]:
    """Run one SELECT, and never let a raw `sqlite3.Error` become the contract."""
    try:
        return connection.execute(sql, parameters).fetchall()
    except sqlite3.Error as error:
        raise EvaluationDatasetError(f"cannot read {description}") from error


def read_source_identity(
    connection: sqlite3.Connection, database_path: str | Path
) -> EvaluationSourceIdentity:
    """Identify the source database without copying it.

    The digest is of the main database file. When a `-wal` sidecar is present
    and non-empty, committed state lives outside that file and the digest alone
    does not identify what was read; the flag and its size are recorded so a
    reader can see that rather than trust a digest that does not cover
    everything.
    """
    path = Path(database_path)
    try:
        size = path.stat().st_size
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while block := handle.read(_HASH_BLOCK_BYTES):
                digest.update(block)
    except OSError as error:
        raise EvaluationDatasetError(
            f"cannot identify the source database at {path}"
        ) from error
    wal = path.with_name(path.name + "-wal")
    shm = path.with_name(path.name + "-shm")
    wal_present = wal.exists()
    migrations = query(
        connection,
        "SELECT version FROM schema_migrations ORDER BY version",
        (),
        "the applied migration versions",
    )
    return EvaluationSourceIdentity(
        database_path=str(path),
        database_bytes=size,
        database_sha256=digest.hexdigest(),
        wal_present=wal_present,
        wal_bytes=wal.stat().st_size if wal_present else None,
        shm_present=shm.exists(),
        applied_migrations=tuple(str(row[0]) for row in migrations),
    )


def resolve_user_id(connection: sqlite3.Connection, profile_id: int) -> int:
    """The person the eligibility decisions in this snapshot belong to.

    Read from `profiles`, exactly as Phase 9A's input assembly reads it, rather
    than asked of the caller: a mistyped user id would silently produce a
    dataset whose eligibility column is empty everywhere, which reads as "no
    decision was ever stored" and is a lie about the database.
    """
    row = query(
        connection,
        "SELECT user_id FROM profiles WHERE id = ?",
        (profile_id,),
        f"the owner of profile {profile_id}",
    )
    if not row:
        raise EvaluationDatasetError(f"profile {profile_id} does not exist")
    return int(row[0][0])


def decode_optional_string_list(
    raw: object, description: str
) -> tuple[str, ...] | None:
    """Decode a nullable JSON array column, keeping NULL distinct from `[]`.

    `None` means the column was never written — the classifier did not run.
    `()` means it ran and matched nothing. Collapsing the two would erase the
    distinction migration `0025` was written to preserve.
    """
    if raw is None:
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise EvaluationDatasetError(f"{description} is not stored JSON") from error
    if not isinstance(decoded, list) or not all(
        isinstance(item, str) for item in decoded
    ):
        raise EvaluationDatasetError(f"{description} is not an array of strings")
    return tuple(decoded)


def read_opportunities(
    connection: sqlite3.Connection, cohort: frozenset[int]
) -> dict[int, tuple]:
    """The cohort's `opportunities` rows.

    The whole table is read and filtered against the cohort in Python rather
    than re-stated as a second `WHERE`. The cohort predicate then lives in
    exactly one place — `cohort.py` — instead of two that can drift, and no
    query has to bind one parameter per selected id.
    """
    rows = query(
        connection,
        """SELECT id, canonical_title, organization, opportunity_type,
                  employment_type, location, country, remote_type, source_url,
                  application_url, canonical_url, status, is_active,
                  published_at, deadline, discovered_at, first_seen_at,
                  last_seen_at
             FROM opportunities
            ORDER BY id""",
        (),
        "the opportunities table",
    )
    return {int(row[0]): row for row in rows if int(row[0]) in cohort}


def read_sources(
    connection: sqlite3.Connection, cohort: frozenset[int]
) -> dict[int, list[EvaluationSourceRecord]]:
    rows = query(
        connection,
        """SELECT os.opportunity_id, os.source_id, s.type, os.source_url,
                  os.application_url, os.canonical_url, os.discovered_at
             FROM opportunity_sources AS os
             LEFT JOIN sources AS s ON s.id = os.source_id
            ORDER BY os.opportunity_id, os.source_id, os.discovered_at,
                     os.source_url, os.id""",
        (),
        "the opportunity sources",
    )
    grouped: dict[int, list[EvaluationSourceRecord]] = {}
    for row in rows:
        opportunity_id = int(row[0])
        if opportunity_id not in cohort:
            continue
        grouped.setdefault(opportunity_id, []).append(
            EvaluationSourceRecord(
                source_id=str(row[1]),
                # LEFT JOIN: a source row deleted from the catalogue leaves the
                # occurrence behind. `None` says the type is unknown, which is
                # the truth, rather than a guessed one.
                source_type=None if row[2] is None else str(row[2]),
                source_url=str(row[3]),
                application_url=None if row[4] is None else str(row[4]),
                canonical_url=None if row[5] is None else str(row[5]),
                discovered_at=str(row[6]),
            )
        )
    return grouped


def read_absorbed_duplicates(
    connection: sqlite3.Connection, cohort: frozenset[int]
) -> dict[int, list[int]]:
    """Which postings each cohort member absorbed as an applied merge.

    Only `APPLIED` merges count: a `ROLLED_BACK` row records a merge that was
    undone, and reporting it would describe a duplicate relationship that no
    longer holds.
    """
    rows = query(
        connection,
        """SELECT canonical_opportunity_id, merged_opportunity_id
             FROM deduplication_merges
            WHERE status = 'APPLIED'
            ORDER BY canonical_opportunity_id, merged_opportunity_id""",
        (),
        "the applied deduplication merges",
    )
    grouped: dict[int, list[int]] = {}
    for canonical_id, merged_id in rows:
        canonical_id = int(canonical_id)
        if canonical_id not in cohort:
            continue
        grouped.setdefault(canonical_id, []).append(int(merged_id))
    return grouped


def read_qualifications(
    connection: sqlite3.Connection, cohort: frozenset[int]
) -> dict[int, EvaluationQualificationRecord]:
    rows = query(
        connection,
        """SELECT opportunity_id, qualification, primary_domain,
                  opportunity_type, employment_type, listing_quality,
                  classifier_version, input_fingerprint, classified_at,
                  fine_primary_category, fine_secondary_categories_json,
                  fine_classifier_version
             FROM opportunity_qualifications
            ORDER BY opportunity_id""",
        (),
        "the persisted qualifications",
    )
    qualifications: dict[int, EvaluationQualificationRecord] = {}
    for row in rows:
        opportunity_id = int(row[0])
        if opportunity_id not in cohort:
            continue
        qualifications[opportunity_id] = EvaluationQualificationRecord(
            qualification=str(row[1]),
            primary_domain=str(row[2]),
            opportunity_type=str(row[3]),
            employment_type=str(row[4]),
            listing_quality=str(row[5]),
            classifier_version=str(row[6]),
            input_fingerprint=str(row[7]),
            classified_at=str(row[8]),
            fine_primary_category=None if row[9] is None else str(row[9]),
            fine_secondary_categories=decode_optional_string_list(
                row[10],
                f"fine_secondary_categories_json of opportunity {opportunity_id}",
            ),
            fine_classifier_version=None if row[11] is None else str(row[11]),
        )
    return qualifications


def read_geography(
    connection: sqlite3.Connection, cohort: frozenset[int]
) -> dict[int, list[EvaluationGeographySegmentRecord]]:
    rows = query(
        connection,
        """SELECT opportunity_id, segment_position, raw_segment,
                  resolution_status, resolution_rule_id, country_code,
                  city_key, resolver_version
             FROM opportunity_location_resolutions
            ORDER BY opportunity_id, segment_position, source_location_id, id""",
        (),
        "the persisted location resolutions",
    )
    grouped: dict[int, list[EvaluationGeographySegmentRecord]] = {}
    for row in rows:
        opportunity_id = int(row[0])
        if opportunity_id not in cohort:
            continue
        grouped.setdefault(opportunity_id, []).append(
            EvaluationGeographySegmentRecord(
                segment_position=int(row[1]),
                raw_segment=str(row[2]),
                status=str(row[3]),
                rule_id=str(row[4]),
                # NULL is the resolver's UNKNOWN and stays NULL.
                country_code=None if row[5] is None else str(row[5]),
                city_key=None if row[6] is None else str(row[6]),
                resolver_version=str(row[7]),
            )
        )
    return grouped


def read_eligibilities(
    connection: sqlite3.Connection, cohort: frozenset[int], user_id: int
) -> dict[int, EvaluationEligibilityRecord]:
    rows = query(
        connection,
        """SELECT opportunity_id, status, engine_version, input_fingerprint,
                  satisfied_count, violated_count, unknown_count,
                  not_applicable_count, not_evaluated_count,
                  blocking_unknown_count, evaluated_at
             FROM opportunity_eligibilities
            WHERE user_id = ?
            ORDER BY opportunity_id""",
        (user_id,),
        f"the eligibility decisions of user {user_id}",
    )
    decisions: dict[int, EvaluationEligibilityRecord] = {}
    for row in rows:
        opportunity_id = int(row[0])
        if opportunity_id not in cohort:
            continue
        decisions[opportunity_id] = EvaluationEligibilityRecord(
            status=str(row[1]),
            engine_version=str(row[2]),
            input_fingerprint=str(row[3]),
            satisfied_count=int(row[4]),
            violated_count=int(row[5]),
            unknown_count=int(row[6]),
            not_applicable_count=int(row[7]),
            not_evaluated_count=int(row[8]),
            blocking_unknown_count=int(row[9]),
            evaluated_at=str(row[10]),
        )
    return decisions


def read_matching(
    connection: sqlite3.Connection, profile_id: int
) -> tuple[dict[int, EvaluationMatchingRecord], dict[str, object]]:
    """Phase 4's current run for this profile, through Phase 4's own read model.

    A profile with no run at all is a legitimate state, not a failure: the
    dataset then carries no matching signal, every record's `matching` is
    `None`, and `matching_status` in the manifest says which of NOT_SYNCED or
    EMPTY that was — a far smaller claim than "matching rejected these".
    """
    try:
        model = read_current_matching(connection, profile_id)
    except MatchingReadError as error:
        raise EvaluationDatasetError(
            f"cannot read the current matching snapshot of profile {profile_id}"
        ) from error
    run = model.current_run
    if run is None:
        return {}, {
            "matching_status": model.status,
            "matching_run_id": None,
            "matching_run_fingerprint": None,
            "matching_engine_version": None,
            "matching_rules_version": None,
            "matching_selection_version": model.selection_version,
        }
    assessments = {
        assessment.opportunity_id: EvaluationMatchingRecord(
            lane=assessment.lane,
            match_quality=assessment.match_quality,
            evidence_coverage=assessment.evidence_coverage,
            assessment_fingerprint=assessment.assessment_fingerprint,
        )
        for assessment in run.assessments
    }
    return assessments, {
        "matching_status": model.status,
        "matching_run_id": run.run_id,
        "matching_run_fingerprint": run.run_fingerprint,
        "matching_engine_version": run.matching_engine_version,
        "matching_rules_version": run.matching_rules_version,
        "matching_selection_version": run.selection_version,
    }


def read_recommendation(
    connection: sqlite3.Connection, profile_id: int
) -> tuple[dict[int, EvaluationRecommendationRecord], dict[str, object]]:
    """Phase 9B's current run, through Phase 9B's own read model.

    NOT_SYNCED and INCOMPLETE are both legitimate: the dataset then carries no
    recommendation signal and every record's `recommendation` is `None`. That is
    a *smaller* claim than "the engine rejected these", and the manifest's
    `recommendation_status` is what tells the two apart.
    """
    try:
        model = read_current_recommendation(connection, profile_id)
    except RecommendationReadError as error:
        raise EvaluationDatasetError(
            "cannot read the current recommendation snapshot of profile "
            f"{profile_id}"
        ) from error
    run = model.current_run
    if run is None:
        return {}, {
            "recommendation_status": model.status,
            "recommendation_run_id": None,
            "recommendation_run_fingerprint": None,
            "recommendation_engine_version": None,
            "recommendation_rules_version": None,
        }
    assessments = {
        assessment.opportunity_id: EvaluationRecommendationRecord(
            rank_position=assessment.rank_position,
            disposition=assessment.disposition.value,
            recommendation_score=assessment.recommendation_score,
            evidence_coverage=assessment.evidence_coverage,
            assessment_fingerprint=assessment.assessment_fingerprint,
        )
        for assessment in run.assessments
    }
    return assessments, {
        "recommendation_status": model.status,
        "recommendation_run_id": run.run_id,
        "recommendation_run_fingerprint": run.run_fingerprint,
        "recommendation_engine_version": run.recommendation_engine_version,
        "recommendation_rules_version": run.recommendation_rules_version,
    }
