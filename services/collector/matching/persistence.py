"""Atomic, append-only persistence for already-computed matching batches."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

from .engine import (
    MATCHING_ENGINE_VERSION,
    MATCHING_RULES_VERSION,
    SEMANTIC_PERCENTILE_VERSION,
    MatchLane,
    MatchingBatchResult,
)
from .engine_fingerprint import (
    canonical_matching_assessment_payload,
    canonical_matching_batch_payload,
    matching_assessment_fingerprint,
    matching_batch_fingerprint,
)
from .fingerprint import canonical_json
from .tfidf_fingerprint import SEMANTIC_BINDING_VERSION
from .persistence_fingerprint import (
    MATCHING_PERSISTENCE_VERSION,
    canonical_matching_run_payload,
    matching_run_fingerprint,
)


class MatchingPersistenceError(RuntimeError):
    """Raised when matching data cannot be safely persisted."""


@dataclass(frozen=True)
class MatchingStoreResult:
    run_id: int
    run_fingerprint: str
    created: bool


def _valid_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _valid_fingerprint(value: object) -> bool:
    return (
        _valid_text(value)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def _prepare(profile_id: int, batch: MatchingBatchResult, selection_version: str):
    if (
        not isinstance(profile_id, int)
        or isinstance(profile_id, bool)
        or profile_id <= 0
    ):
        raise MatchingPersistenceError("profile_id must be a positive integer")
    if not _valid_text(selection_version):
        raise MatchingPersistenceError(
            "selection_version must be non-empty and trimmed"
        )
    if not isinstance(batch, MatchingBatchResult):
        raise MatchingPersistenceError("batch must be a MatchingBatchResult")
    if not batch.assessments or batch.assessment_count != len(batch.assessments):
        raise MatchingPersistenceError(
            "batch must be non-empty with an exact assessment_count"
        )
    expected_versions = (
        MATCHING_ENGINE_VERSION,
        MATCHING_RULES_VERSION,
        SEMANTIC_PERCENTILE_VERSION,
    )
    if (
        batch.matching_engine_version,
        batch.matching_rules_version,
        batch.semantic_percentile_version,
    ) != expected_versions:
        raise MatchingPersistenceError("unexpected matching batch versions")
    if not all(
        _valid_fingerprint(value)
        for value in (
            batch.corpus_fingerprint,
            batch.tfidf_model_fingerprint,
            batch.batch_fingerprint,
        )
    ):
        raise MatchingPersistenceError("malformed batch fingerprint")
    ids = [item.opportunity_id for item in batch.assessments]
    if len(ids) != len(set(ids)):
        raise MatchingPersistenceError("duplicate opportunity IDs")
    prepared = []
    for item in batch.assessments:
        if (
            item.profile_id != profile_id
            or not isinstance(item.opportunity_id, int)
            or item.opportunity_id <= 0
        ):
            raise MatchingPersistenceError("assessment operational ID mismatch")
        if (
            item.matching_engine_version,
            item.matching_rules_version,
            item.semantic_percentile_version,
        ) != expected_versions:
            raise MatchingPersistenceError("unexpected assessment versions")
        if not isinstance(item.lane, MatchLane):
            raise MatchingPersistenceError("invalid assessment lane")
        coverage, quality = item.evidence_coverage, item.match_quality
        if (
            not isinstance(coverage, (int, float))
            or not math.isfinite(coverage)
            or not 0 <= coverage <= 1
        ):
            raise MatchingPersistenceError("invalid evidence coverage")
        if (coverage == 0 and quality is not None) or (
            coverage > 0 and quality is None
        ):
            raise MatchingPersistenceError("match quality availability mismatch")
        if quality is not None and (
            not isinstance(quality, (int, float))
            or not math.isfinite(quality)
            or not 0 <= quality <= 1
        ):
            raise MatchingPersistenceError("invalid match quality")
        payload = canonical_json(canonical_matching_assessment_payload(item))
        fingerprint = matching_assessment_fingerprint(item)
        if (
            not _valid_fingerprint(item.assessment_fingerprint)
            or item.assessment_fingerprint != fingerprint
        ):
            raise MatchingPersistenceError(
                "assessment fingerprint does not match canonical payload"
            )
        prepared.append((item, payload))
    content_payload = canonical_matching_batch_payload(batch)
    if matching_batch_fingerprint(batch) != batch.batch_fingerprint:
        raise MatchingPersistenceError(
            "batch fingerprint does not match canonical payload"
        )
    binding = _validated_binding(batch)
    # The stored payload is the content payload plus, for a run that has it, the
    # identity-aware binding provenance. The batch *fingerprint* stays the digest
    # of the content half alone — the binding is protected by the run
    # fingerprint below, which is the layer that owns operational ids. Storing
    # it in the existing column keeps `matching_runs` unchanged: no migration,
    # no new table, and every legacy row still reads.
    batch_payload = canonical_json({**content_payload, **binding})
    operational_assessments = tuple(
        (item.opportunity_id, item.assessment_fingerprint) for item, _ in prepared
    )
    run_values = dict(
        profile_id=profile_id,
        selection_version=selection_version,
        matching_engine_version=batch.matching_engine_version,
        matching_rules_version=batch.matching_rules_version,
        semantic_percentile_version=batch.semantic_percentile_version,
        corpus_fingerprint=batch.corpus_fingerprint,
        tfidf_model_fingerprint=batch.tfidf_model_fingerprint,
        batch_fingerprint=batch.batch_fingerprint,
        assessments=operational_assessments,
        **binding,
    )
    run_payload = canonical_matching_run_payload(**run_values)
    run_fingerprint = matching_run_fingerprint(**run_values)
    if canonical_json(run_payload) != canonical_json(
        canonical_matching_run_payload(**run_values)
    ):
        raise MatchingPersistenceError("run payload is incoherent")
    return prepared, batch_payload, run_fingerprint


def _validated_binding(batch: MatchingBatchResult) -> dict[str, str]:
    """Return the binding keys to persist, or an empty dict for a legacy batch.

    Both or neither: a batch carrying half a provenance is refused here rather
    than stored, because the run fingerprint below could not then be reproduced
    from what was written.
    """
    version = batch.semantic_binding_version
    fingerprint = batch.semantic_binding_fingerprint
    if version is None and fingerprint is None:
        return {}
    if version is None or fingerprint is None:
        raise MatchingPersistenceError(
            "semantic binding provenance needs both a version and a fingerprint"
        )
    if version != SEMANTIC_BINDING_VERSION:
        raise MatchingPersistenceError(
            f"unsupported semantic binding version: {version!r}"
        )
    if not _valid_fingerprint(fingerprint):
        raise MatchingPersistenceError("invalid semantic binding fingerprint")
    return {
        "semantic_binding_version": version,
        "semantic_binding_fingerprint": fingerprint,
    }


def _upsert_state(
    connection: sqlite3.Connection,
    profile_id: int,
    state: str,
    run_id: int | None,
    selection_version: str,
) -> None:
    connection.execute(
        """INSERT INTO matching_profile_state (profile_id, state, current_run_id, persistence_version, selection_version)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(profile_id) DO UPDATE SET state=excluded.state,
             current_run_id=excluded.current_run_id, persistence_version=excluded.persistence_version,
             selection_version=excluded.selection_version, updated_at=CURRENT_TIMESTAMP""",
        (profile_id, state, run_id, MATCHING_PERSISTENCE_VERSION, selection_version),
    )


def _verify_existing(
    connection: sqlite3.Connection,
    row: tuple,
    profile_id: int,
    batch: MatchingBatchResult,
    selection_version: str,
    prepared: list,
    batch_payload: str,
) -> None:
    expected = (
        profile_id,
        MATCHING_PERSISTENCE_VERSION,
        selection_version,
        batch.matching_engine_version,
        batch.matching_rules_version,
        batch.semantic_percentile_version,
        batch.corpus_fingerprint,
        batch.tfidf_model_fingerprint,
        batch.batch_fingerprint,
        batch.assessment_count,
        batch_payload,
    )
    if tuple(row[1:]) != expected:
        raise MatchingPersistenceError("existing run metadata is corrupt")
    stored = connection.execute(
        "SELECT opportunity_id,lane,match_quality,evidence_coverage,assessment_fingerprint,assessment_payload_json FROM matching_assessments WHERE run_id=? ORDER BY opportunity_id",
        (row[0],),
    ).fetchall()
    wanted = sorted(
        (
            (
                item.opportunity_id,
                item.lane.value,
                item.match_quality,
                item.evidence_coverage,
                item.assessment_fingerprint,
                payload,
            )
            for item, payload in prepared
        )
    )
    if stored != wanted:
        raise MatchingPersistenceError("existing run assessments are corrupt")


def store_matching_batch(
    connection: sqlite3.Connection,
    profile_id: int,
    batch: MatchingBatchResult,
    *,
    selection_version: str,
    after_assessment_insert=None,
) -> MatchingStoreResult:
    """Store one precomputed batch atomically; reuse and audit identical runs."""
    prepared, batch_payload, run_fp = _prepare(profile_id, batch, selection_version)
    connection.execute("BEGIN IMMEDIATE")
    try:
        if (
            connection.execute(
                "SELECT 1 FROM profiles WHERE id=?", (profile_id,)
            ).fetchone()
            is None
        ):
            raise MatchingPersistenceError(f"profile {profile_id} does not exist")
        missing = [
            item.opportunity_id
            for item, _ in prepared
            if connection.execute(
                "SELECT 1 FROM opportunities WHERE id=?", (item.opportunity_id,)
            ).fetchone()
            is None
        ]
        if missing:
            raise MatchingPersistenceError(f"opportunities do not exist: {missing}")
        found = connection.execute(
            "SELECT id,profile_id,persistence_version,selection_version,matching_engine_version,matching_rules_version,semantic_percentile_version,corpus_fingerprint,tfidf_model_fingerprint,batch_fingerprint,assessment_count,batch_payload_json FROM matching_runs WHERE run_fingerprint=?",
            (run_fp,),
        ).fetchone()
        created = found is None
        if found is not None:
            _verify_existing(
                connection,
                found,
                profile_id,
                batch,
                selection_version,
                prepared,
                batch_payload,
            )
            run_id = int(found[0])
        else:
            row = connection.execute(
                """INSERT INTO matching_runs (profile_id,persistence_version,selection_version,matching_engine_version,matching_rules_version,semantic_percentile_version,corpus_fingerprint,tfidf_model_fingerprint,batch_fingerprint,run_fingerprint,assessment_count,batch_payload_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
                (
                    profile_id,
                    MATCHING_PERSISTENCE_VERSION,
                    selection_version,
                    batch.matching_engine_version,
                    batch.matching_rules_version,
                    batch.semantic_percentile_version,
                    batch.corpus_fingerprint,
                    batch.tfidf_model_fingerprint,
                    batch.batch_fingerprint,
                    run_fp,
                    batch.assessment_count,
                    batch_payload,
                ),
            ).fetchone()
            run_id = int(row[0])
            for position, (item, payload) in enumerate(prepared):
                connection.execute(
                    "INSERT INTO matching_assessments (run_id,opportunity_id,lane,match_quality,evidence_coverage,assessment_fingerprint,assessment_payload_json) VALUES (?,?,?,?,?,?,?)",
                    (
                        run_id,
                        item.opportunity_id,
                        item.lane.value,
                        item.match_quality,
                        item.evidence_coverage,
                        item.assessment_fingerprint,
                        payload,
                    ),
                )
                if after_assessment_insert is not None:
                    after_assessment_insert(position)
            if (
                connection.execute(
                    "SELECT COUNT(*) FROM matching_assessments WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
                != batch.assessment_count
            ):
                raise MatchingPersistenceError("stored assessment count mismatch")
        _upsert_state(connection, profile_id, "READY", run_id, selection_version)
        connection.execute("COMMIT")
        return MatchingStoreResult(run_id, run_fp, created)
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def set_matching_state_empty(
    connection: sqlite3.Connection, profile_id: int, *, selection_version: str
) -> None:
    if (
        not isinstance(profile_id, int)
        or profile_id <= 0
        or not _valid_text(selection_version)
    ):
        raise MatchingPersistenceError("invalid EMPTY state arguments")
    connection.execute("BEGIN IMMEDIATE")
    try:
        if (
            connection.execute(
                "SELECT 1 FROM profiles WHERE id=?", (profile_id,)
            ).fetchone()
            is None
        ):
            raise MatchingPersistenceError(f"profile {profile_id} does not exist")
        _upsert_state(connection, profile_id, "EMPTY", None, selection_version)
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def read_matching_profile_state(connection: sqlite3.Connection, profile_id: int):
    return connection.execute(
        "SELECT profile_id,state,current_run_id,persistence_version,selection_version,created_at,updated_at FROM matching_profile_state WHERE profile_id=?",
        (profile_id,),
    ).fetchone()
