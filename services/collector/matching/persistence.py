"""Atomic, append-only persistence for already-computed matching batches.

Everything here receives a batch that was scored somewhere else. That is what
the module is for, and it is also its limit: it never sees the profile the batch
was computed from, so it is in no position to certify that the run describes the
profile as it stands now. None of these functions writes a synchronization
watermark. `sync_matching` computes and persists under one `BEGIN IMMEDIATE`,
and is therefore the only caller entitled to make that claim.

Each write comes in two forms: a `*_in_transaction` primitive that borrows the
caller's transaction, and the historical public function that owns one. The
persistence contract of the public form is unchanged.
"""

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


def _require_transaction(connection: sqlite3.Connection, what: str) -> None:
    """A primitive runs inside its caller's transaction, or not at all.

    Refusing here rather than writing outside one is what makes the extraction
    safe: a caller that forgot the `BEGIN IMMEDIATE` gets an error instead of a
    half-published snapshot nobody is holding a lock for.
    """
    if not connection.in_transaction:
        raise MatchingPersistenceError(
            f"{what} requires the transaction its caller opened"
        )


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


def store_matching_batch_in_transaction(
    connection: sqlite3.Connection,
    profile_id: int,
    batch: MatchingBatchResult,
    *,
    selection_version: str,
    after_assessment_insert=None,
) -> MatchingStoreResult:
    """`store_matching_batch`, in the transaction the caller already opened.

    It opens none, commits none and rolls none back. That is what lets
    `sync_matching` hold one `BEGIN IMMEDIATE` across the selection, the
    profile read, the scoring and this write — so the batch cannot have been
    computed from a profile that changed before it landed.

    It writes no watermark. Storing a batch is not evidence that the batch
    describes the profile as it is now; only the caller that just computed it
    under this transaction knows that, and only that caller may say so.
    """
    _require_transaction(connection, "storing a matching batch")
    prepared, batch_payload, run_fp = _prepare(profile_id, batch, selection_version)
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
    return MatchingStoreResult(run_id, run_fp, created)


def store_matching_batch(
    connection: sqlite3.Connection,
    profile_id: int,
    batch: MatchingBatchResult,
    *,
    selection_version: str,
    after_assessment_insert=None,
) -> MatchingStoreResult:
    """Store one precomputed batch atomically; reuse and audit identical runs.

    The historical entry point, and its persistence contract is unchanged: one
    `BEGIN IMMEDIATE`, one `COMMIT`, the same reuse and the same audit.

    What it deliberately does **not** do is advance the Matching watermark. The
    batch reached it already computed, from a profile this transaction never
    saw, so it cannot certify that the run describes the active revision — and a
    batch computed before a CV activation must never be able to claim the
    revision that activation created. Only `sync_matching`, which computes and
    persists under one transaction, is in a position to make that claim.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        result = store_matching_batch_in_transaction(
            connection,
            profile_id,
            batch,
            selection_version=selection_version,
            after_assessment_insert=after_assessment_insert,
        )
        connection.execute("COMMIT")
        return result
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def set_matching_state_empty_in_transaction(
    connection: sqlite3.Connection, profile_id: int, *, selection_version: str
) -> None:
    """`set_matching_state_empty`, in the transaction the caller already opened.

    Writes no watermark, for the same reason the batch primitive does not: the
    emptiness was decided by a selection this call never saw.
    """
    _require_transaction(connection, "publishing an empty matching state")
    if (
        not isinstance(profile_id, int)
        or profile_id <= 0
        or not _valid_text(selection_version)
    ):
        raise MatchingPersistenceError("invalid EMPTY state arguments")
    if (
        connection.execute(
            "SELECT 1 FROM profiles WHERE id=?", (profile_id,)
        ).fetchone()
        is None
    ):
        raise MatchingPersistenceError(f"profile {profile_id} does not exist")
    _upsert_state(connection, profile_id, "EMPTY", None, selection_version)


def set_matching_state_empty(
    connection: sqlite3.Connection, profile_id: int, *, selection_version: str
) -> None:
    """Publish the EMPTY state on its own. Persistence contract unchanged.

    Like `store_matching_batch`, it advances no watermark: the selection that
    found nothing to match happened outside this transaction.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        set_matching_state_empty_in_transaction(
            connection, profile_id, selection_version=selection_version
        )
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def read_matching_profile_state(connection: sqlite3.Connection, profile_id: int):
    return connection.execute(
        "SELECT profile_id,state,current_run_id,persistence_version,selection_version,created_at,updated_at FROM matching_profile_state WHERE profile_id=?",
        (profile_id,),
    ).fetchone()
