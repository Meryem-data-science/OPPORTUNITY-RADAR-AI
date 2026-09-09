"""Atomic, append-only persistence for already-computed recommendation batches.

The engine in `engine.py` stays pure and this module does not change that: it
receives a `RecommendationBatchResult` that has already been assessed and ranked,
validates it against Phase 9A's own contract, and writes it. It recomputes no
score, no disposition, no reason and no ranking — but it re-derives every digest
it is handed, because a persistence layer that trusts the object it is given is
an audit trail that proves nothing.

Three things are written together or not at all:

    recommendation_runs           one row, append-only, keyed on the
                                  operational identity of the run
    recommendation_assessments    one row per assessment, in rank order
    recommendation_profile_state  the single mutable pointer: READY, and the
                                  run it now names

Idempotence is by operational identity, not by the UNIQUE index alone. A batch
whose `run_fingerprint` already exists is read back in full and compared field
by field against what would have been written; only an exact match is reused.
Anything else is a corrupt history and is refused rather than repaired.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

from services.collector.matching.fingerprint import canonical_json

from .engine import rank_recommendation_assessments
from .fingerprint import (
    canonical_recommendation_assessment_payload,
    canonical_recommendation_batch_payload,
    recommendation_assessment_fingerprint,
    recommendation_batch_fingerprint,
)
from .input_assembly import RECOMMENDATION_INPUT_ASSEMBLY_VERSION
from .models import (
    RECOMMENDATION_ENGINE_VERSION,
    RECOMMENDATION_RULES_VERSION,
    RecommendationAssessment,
    RecommendationBatchResult,
    RecommendationDisposition,
)
from .persistence_fingerprint import (
    RECOMMENDATION_PERSISTENCE_VERSION,
    recommendation_run_fingerprint,
)

__all__ = [
    "RecommendationPersistenceError",
    "RecommendationStoreResult",
    "store_recommendation_batch",
]

#: A READY state carries no readiness issue by construction. The literal is the
#: canonical encoding of the empty list, so the column holds the same bytes any
#: other canonical JSON in this schema would.
_NO_READINESS_ISSUES = canonical_json([])


class RecommendationPersistenceError(RuntimeError):
    """Raised when a recommendation batch cannot be safely persisted."""


@dataclass(frozen=True)
class RecommendationStoreResult:
    run_id: int
    run_fingerprint: str
    created: bool


def _valid_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _valid_fingerprint(value: object) -> bool:
    return (
        _valid_text(value)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid_ratio(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and 0.0 <= value <= 1.0
    )


def _prepare(
    profile_id: int,
    batch: RecommendationBatchResult,
    source_matching_run_id: int,
    source_matching_run_fingerprint: str,
    input_assembly_version: str,
) -> tuple[list[tuple[RecommendationAssessment, str]], str, str]:
    """Validate everything decidable without the database, and digest the run."""
    if not _positive_int(profile_id):
        raise RecommendationPersistenceError("profile_id must be a positive integer")
    if not _positive_int(source_matching_run_id):
        raise RecommendationPersistenceError(
            "source_matching_run_id must be a positive integer"
        )
    if not _valid_fingerprint(source_matching_run_fingerprint):
        raise RecommendationPersistenceError(
            "invalid source matching run fingerprint"
        )
    if not _valid_text(input_assembly_version):
        raise RecommendationPersistenceError(
            "input_assembly_version must be non-empty and trimmed"
        )
    if input_assembly_version != RECOMMENDATION_INPUT_ASSEMBLY_VERSION:
        raise RecommendationPersistenceError(
            f"unexpected input assembly version: {input_assembly_version!r}"
        )
    if not isinstance(batch, RecommendationBatchResult):
        raise RecommendationPersistenceError(
            "batch must be a RecommendationBatchResult"
        )
    if batch.profile_id != profile_id:
        raise RecommendationPersistenceError("batch belongs to another profile")
    if not batch.assessments or batch.assessment_count != len(batch.assessments):
        raise RecommendationPersistenceError(
            "batch must be non-empty with an exact assessment_count"
        )
    expected_versions = (RECOMMENDATION_ENGINE_VERSION, RECOMMENDATION_RULES_VERSION)
    if (
        batch.recommendation_engine_version,
        batch.recommendation_rules_version,
    ) != expected_versions:
        raise RecommendationPersistenceError("unexpected recommendation batch versions")
    if not _valid_fingerprint(batch.batch_fingerprint):
        raise RecommendationPersistenceError("malformed batch fingerprint")
    if recommendation_batch_fingerprint(batch) != batch.batch_fingerprint:
        raise RecommendationPersistenceError(
            "batch fingerprint does not match canonical payload"
        )

    identifiers = [item.opportunity_id for item in batch.assessments]
    if len(identifiers) != len(set(identifiers)):
        raise RecommendationPersistenceError("duplicate opportunity IDs")

    prepared: list[tuple[RecommendationAssessment, str]] = []
    for item in batch.assessments:
        if not isinstance(item, RecommendationAssessment):
            raise RecommendationPersistenceError(
                "batch must hold RecommendationAssessment values"
            )
        if item.profile_id != profile_id or not _positive_int(item.opportunity_id):
            raise RecommendationPersistenceError("assessment operational ID mismatch")
        if (
            item.recommendation_engine_version,
            item.recommendation_rules_version,
        ) != expected_versions:
            raise RecommendationPersistenceError("unexpected assessment versions")
        if not isinstance(item.disposition, RecommendationDisposition):
            raise RecommendationPersistenceError("invalid assessment disposition")
        coverage, score = (
            item.recommendation_evidence_coverage,
            item.recommendation_score,
        )
        if not _valid_ratio(coverage):
            raise RecommendationPersistenceError("invalid recommendation coverage")
        if (coverage == 0.0 and score is not None) or (
            coverage > 0.0 and score is None
        ):
            raise RecommendationPersistenceError(
                "recommendation score availability mismatch"
            )
        if score is not None and not _valid_ratio(score):
            raise RecommendationPersistenceError("invalid recommendation score")
        if (
            not _valid_fingerprint(item.assessment_fingerprint)
            or item.assessment_fingerprint
            != recommendation_assessment_fingerprint(item)
        ):
            raise RecommendationPersistenceError(
                "assessment fingerprint does not match canonical payload"
            )
        prepared.append(
            (item, canonical_json(canonical_recommendation_assessment_payload(item)))
        )

    # The order the batch arrives in *is* the ranking, and this layer must not
    # re-rank. What it may do — and does — is check the received order against
    # Phase 9A's own ranking primitive rather than against a copied sort key.
    # `rank_recommendation_assessments` is a pure sort over the assessments
    # themselves, so this recomputes no score, no disposition and no reason: it
    # reorders the objects already in hand and refuses a sequence Phase 9A would
    # not have produced. Opportunity ids are unique inside a batch and are the
    # ranking's final key, so the expected order is total and the comparison is
    # exact rather than merely plausible.
    if rank_recommendation_assessments(batch.assessments) != batch.assessments:
        raise RecommendationPersistenceError(
            "batch assessments are not in Phase 9A ranking order"
        )

    batch_payload = canonical_json(canonical_recommendation_batch_payload(batch))
    run_fingerprint = recommendation_run_fingerprint(
        profile_id=profile_id,
        source_matching_run_id=source_matching_run_id,
        source_matching_run_fingerprint=source_matching_run_fingerprint,
        input_assembly_version=input_assembly_version,
        recommendation_engine_version=batch.recommendation_engine_version,
        recommendation_rules_version=batch.recommendation_rules_version,
        batch_fingerprint=batch.batch_fingerprint,
        ranked_assessments=tuple(
            (item.opportunity_id, item.assessment_fingerprint)
            for item, _ in prepared
        ),
    )
    return prepared, batch_payload, run_fingerprint


_RUN_COLUMNS = (
    "id,profile_id,source_matching_run_id,persistence_version,input_assembly_version,"
    "recommendation_engine_version,recommendation_rules_version,"
    "source_matching_run_fingerprint,batch_fingerprint,assessment_count,batch_payload_json"
)

_ASSESSMENT_COLUMNS = (
    "opportunity_id,rank_position,disposition,recommendation_score,evidence_coverage,"
    "assessment_fingerprint,assessment_payload_json"
)


def _verify_existing(
    connection: sqlite3.Connection,
    row: tuple,
    profile_id: int,
    batch: RecommendationBatchResult,
    source_matching_run_id: int,
    source_matching_run_fingerprint: str,
    input_assembly_version: str,
    prepared: list[tuple[RecommendationAssessment, str]],
    batch_payload: str,
) -> None:
    """Refuse to reuse a stored run that is not, field for field, this one.

    The UNIQUE index on `run_fingerprint` says two rows agree on an identity; it
    says nothing about whether the row still holds what that identity claims.
    Everything the run and its assessments store is compared here, `rank_position`
    included, so a history someone edited underneath us is reported instead of
    being silently adopted as the current state.
    """
    expected = (
        profile_id,
        source_matching_run_id,
        RECOMMENDATION_PERSISTENCE_VERSION,
        input_assembly_version,
        batch.recommendation_engine_version,
        batch.recommendation_rules_version,
        source_matching_run_fingerprint,
        batch.batch_fingerprint,
        batch.assessment_count,
        batch_payload,
    )
    if tuple(row[1:]) != expected:
        raise RecommendationPersistenceError("existing run metadata is corrupt")
    stored = connection.execute(
        f"SELECT {_ASSESSMENT_COLUMNS} FROM recommendation_assessments"
        " WHERE run_id=? ORDER BY rank_position",
        (row[0],),
    ).fetchall()
    wanted = [
        (
            item.opportunity_id,
            position,
            item.disposition.value,
            item.recommendation_score,
            item.recommendation_evidence_coverage,
            item.assessment_fingerprint,
            payload,
        )
        for position, (item, payload) in enumerate(prepared, start=1)
    ]
    if stored != wanted:
        raise RecommendationPersistenceError("existing run assessments are corrupt")


def _upsert_ready_state(
    connection: sqlite3.Connection,
    profile_id: int,
    run_id: int,
    input_assembly_version: str,
) -> None:
    """Point the one mutable pointer at this run. Runs themselves never move."""
    connection.execute(
        """INSERT INTO recommendation_profile_state
             (profile_id, state, current_run_id, persistence_version,
              input_assembly_version, readiness_issues_json)
           VALUES (?, 'READY', ?, ?, ?, ?)
           ON CONFLICT(profile_id) DO UPDATE SET state=excluded.state,
             current_run_id=excluded.current_run_id,
             persistence_version=excluded.persistence_version,
             input_assembly_version=excluded.input_assembly_version,
             readiness_issues_json=excluded.readiness_issues_json,
             updated_at=CURRENT_TIMESTAMP""",
        (
            profile_id,
            run_id,
            RECOMMENDATION_PERSISTENCE_VERSION,
            input_assembly_version,
            _NO_READINESS_ISSUES,
        ),
    )


def store_recommendation_batch(
    connection: sqlite3.Connection,
    profile_id: int,
    batch: RecommendationBatchResult,
    *,
    source_matching_run_id: int,
    source_matching_run_fingerprint: str,
    input_assembly_version: str = RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
    after_assessment_insert=None,
) -> RecommendationStoreResult:
    """Store one precomputed, ranked batch atomically; reuse identical runs.

    `after_assessment_insert` is a test-only seam, the same one matching
    persistence exposes: it is called with each 0-based insert position so a test
    can interrupt the write mid-run and prove the rollback leaves no partial run,
    no partial assessment and no partial state. Exceptions raised through it are
    never swallowed.
    """
    prepared, batch_payload, run_fingerprint = _prepare(
        profile_id,
        batch,
        source_matching_run_id,
        source_matching_run_fingerprint,
        input_assembly_version,
    )
    connection.execute("BEGIN IMMEDIATE")
    try:
        if (
            connection.execute(
                "SELECT 1 FROM profiles WHERE id=?", (profile_id,)
            ).fetchone()
            is None
        ):
            raise RecommendationPersistenceError(
                f"profile {profile_id} does not exist"
            )
        # The source snapshot must exist, belong to this profile, and be the very
        # run the caller says it is. Nothing is recomputed: the stored matching
        # run fingerprint is read and compared, never regenerated.
        matching_run = connection.execute(
            "SELECT profile_id,run_fingerprint FROM matching_runs WHERE id=?",
            (source_matching_run_id,),
        ).fetchone()
        if matching_run is None:
            raise RecommendationPersistenceError(
                f"matching run {source_matching_run_id} does not exist"
            )
        if matching_run[0] != profile_id:
            raise RecommendationPersistenceError(
                f"matching run {source_matching_run_id} belongs to another profile"
            )
        if matching_run[1] != source_matching_run_fingerprint:
            raise RecommendationPersistenceError(
                "source matching run fingerprint does not match the stored run"
            )
        missing = [
            item.opportunity_id
            for item, _ in prepared
            if connection.execute(
                "SELECT 1 FROM opportunities WHERE id=?", (item.opportunity_id,)
            ).fetchone()
            is None
        ]
        if missing:
            raise RecommendationPersistenceError(
                f"opportunities do not exist: {missing}"
            )

        found = connection.execute(
            f"SELECT {_RUN_COLUMNS} FROM recommendation_runs WHERE run_fingerprint=?",
            (run_fingerprint,),
        ).fetchone()
        created = found is None
        if found is not None:
            _verify_existing(
                connection,
                found,
                profile_id,
                batch,
                source_matching_run_id,
                source_matching_run_fingerprint,
                input_assembly_version,
                prepared,
                batch_payload,
            )
            run_id = int(found[0])
        else:
            run_id = int(
                connection.execute(
                    """INSERT INTO recommendation_runs
                         (profile_id,source_matching_run_id,persistence_version,
                          input_assembly_version,recommendation_engine_version,
                          recommendation_rules_version,source_matching_run_fingerprint,
                          batch_fingerprint,run_fingerprint,assessment_count,
                          batch_payload_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
                    (
                        profile_id,
                        source_matching_run_id,
                        RECOMMENDATION_PERSISTENCE_VERSION,
                        input_assembly_version,
                        batch.recommendation_engine_version,
                        batch.recommendation_rules_version,
                        source_matching_run_fingerprint,
                        batch.batch_fingerprint,
                        run_fingerprint,
                        batch.assessment_count,
                        batch_payload,
                    ),
                ).fetchone()[0]
            )
            for position, (item, payload) in enumerate(prepared):
                connection.execute(
                    f"INSERT INTO recommendation_assessments (run_id,{_ASSESSMENT_COLUMNS})"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        item.opportunity_id,
                        position + 1,
                        item.disposition.value,
                        item.recommendation_score,
                        item.recommendation_evidence_coverage,
                        item.assessment_fingerprint,
                        payload,
                    ),
                )
                if after_assessment_insert is not None:
                    after_assessment_insert(position)
            if (
                connection.execute(
                    "SELECT COUNT(*) FROM recommendation_assessments WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
                != batch.assessment_count
            ):
                raise RecommendationPersistenceError("stored assessment count mismatch")
        _upsert_ready_state(connection, profile_id, run_id, input_assembly_version)
        connection.execute("COMMIT")
        return RecommendationStoreResult(run_id, run_fingerprint, created)
    except BaseException:
        connection.execute("ROLLBACK")
        raise
