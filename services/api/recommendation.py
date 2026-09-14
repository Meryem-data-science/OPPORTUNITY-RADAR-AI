"""Read-only public surface for the persisted Recommendation of the server profile.

Phase 11.1A exposes what Phase 9B already computed and stored, and nothing else.
The current state comes from the Recommendation read model, its integrity from
the Recommendation persistence audit, and the opportunity metadata beside each
ranked assessment from persisted rows, through the existing Phase 8 decoder and
the existing link-priority helpers. No engine, synchronization, classifier or
scorer is reachable from this module, and nothing is written.

The persisted ranking is the product: items are emitted in exactly the order
the read model returns them. The read model, the audit and the metadata are
read inside one deferred read transaction, so they describe one database state,
and a run whose metadata cannot be read whole is refused whole.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel

from services.api.fine_classification import (
    FineEvidenceResponse,
    decode_fine_classification,
)
from services.api.link_priority import (
    SourceObservation,
    preferred_link,
    select_original_url,
)
from services.collector.config import DatabaseBackend, load_settings
from services.collector.database.connection import connect_readonly_database
from services.collector.logging_config import get_logger
from services.collector.qualification.fine_taxonomy import FineCategory
from services.recommendation.persistence_audit import (
    audit_recommendation_profile_history,
)
from services.recommendation.read_model import read_current_recommendation


LOGGER = get_logger("services.collector.api.recommendation")
PUBLIC_RECOMMENDATION_ERROR = "Recommendation data is temporarily unavailable."
#: The Phase 9 reason partition, read from the persisted payload's `result`.
REASON_PARTITIONS = ("strengths", "confirmed_gaps", "unknowns")


class RecommendationApiReadError(RuntimeError):
    """Raised when Recommendation data cannot safely be exposed."""


class RecommendationIntegrityResponse(BaseModel):
    ok: bool
    audit_version: str
    audit_fingerprint: str


class RecommendationReadinessIssueResponse(BaseModel):
    """One stored readiness issue. The stored message is diagnostic text that
    can carry upstream error details, so only the code and posting are public."""

    code: str
    opportunity_id: int | None


class RecommendationOpportunityResponse(BaseModel):
    id: int
    canonical_title: str
    organization: str
    location: str | None
    last_seen_at: str
    original_url: str
    #: The persisted Phase 8 fine classification, with the same three states as
    #: `GET /api/opportunities`: all five `None` when never fine-classified.
    fine_primary_category: FineCategory | None
    fine_secondary_categories: list[FineCategory] | None
    fine_category_evidence: list[FineEvidenceResponse] | None
    fine_reasons: list[str] | None
    fine_classifier_version: str | None


class RecommendationSnapshotResponse(BaseModel):
    disposition: Literal[
        "RECOMMENDED", "UNCERTAIN", "OUTSIDE_PREFERENCES", "KNOWN_BLOCKER"
    ]
    #: `None` means no evidence was available; it is never a zero.
    recommendation_score: float | None
    evidence_coverage: float
    assessment_fingerprint: str
    strengths: list[str]
    confirmed_gaps: list[str]
    unknowns: list[str]
    explanation: dict[str, Any]


class RecommendationItemResponse(BaseModel):
    opportunity_id: int
    rank_position: int
    opportunity: RecommendationOpportunityResponse
    recommendation: RecommendationSnapshotResponse


class RecommendationCurrentRunResponse(BaseModel):
    run_id: int
    created_at: str
    assessment_count: int
    persistence_version: str
    input_assembly_version: str
    recommendation_engine_version: str
    recommendation_rules_version: str
    source_matching_run_id: int
    source_matching_run_fingerprint: str
    batch_fingerprint: str
    run_fingerprint: str
    items: list[RecommendationItemResponse]


class RecommendationResponse(BaseModel):
    profile_id: int
    status: Literal["NOT_SYNCED", "INCOMPLETE", "READY"]
    persistence_version: str | None
    input_assembly_version: str | None
    history_count: int
    readiness_issues: list[RecommendationReadinessIssueResponse]
    current_run: RecommendationCurrentRunResponse | None
    integrity: RecommendationIntegrityResponse


def _profile_id() -> int:
    raw = os.environ.get("OPPORTUNITY_RADAR_PROFILE_ID")
    try:
        value = int(raw) if raw is not None else 0
    except ValueError as error:
        raise RecommendationApiReadError(PUBLIC_RECOMMENDATION_ERROR) from error
    if raw is None or value <= 0 or raw.strip() != str(value):
        raise RecommendationApiReadError(PUBLIC_RECOMMENDATION_ERROR)
    return value


def _json_value(value: Any) -> Any:
    """Copy the immutable read model into ordinary JSON-compatible containers."""
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _metadata(
    connection: Any, opportunity_ids: list[int]
) -> dict[int, RecommendationOpportunityResponse]:
    """Project persisted opportunity rows; classify nothing and rank nothing."""
    if not opportunity_ids:
        return {}
    placeholders = ", ".join("?" for _ in opportunity_ids)
    # LEFT JOIN: an opportunity with no qualification row is never-classified,
    # not missing. The coarse `qualification` only validates the fine values.
    rows = connection.execute(
        f"""SELECT opportunities.id, opportunities.canonical_title,
        opportunities.organization, opportunities.location,
        opportunities.last_seen_at, opportunities.source_url,
        opportunities.application_url, opportunities.canonical_url,
        opportunity_qualifications.qualification,
        opportunity_qualifications.fine_primary_category,
        opportunity_qualifications.fine_secondary_categories_json,
        opportunity_qualifications.fine_category_evidence_json,
        opportunity_qualifications.fine_reasons_json,
        opportunity_qualifications.fine_classifier_version
        FROM opportunities LEFT JOIN opportunity_qualifications
        ON opportunity_qualifications.opportunity_id = opportunities.id
        WHERE opportunities.id IN ({placeholders}) ORDER BY opportunities.id""",
        tuple(opportunity_ids),
    ).fetchall()
    observation_rows = connection.execute(
        f"""SELECT opportunity_sources.opportunity_id, opportunity_sources.id,
        sources.type, opportunity_sources.application_url,
        opportunity_sources.source_url, opportunity_sources.canonical_url
        FROM opportunity_sources JOIN sources
        ON sources.id = opportunity_sources.source_id
        WHERE opportunity_sources.opportunity_id IN ({placeholders})
        ORDER BY opportunity_sources.opportunity_id, opportunity_sources.id""",
        tuple(opportunity_ids),
    ).fetchall()
    observations: dict[int, list[SourceObservation]] = {}
    for row in observation_rows:
        observations.setdefault(int(row[0]), []).append(
            SourceObservation(
                observation_id=int(row[1]),
                source_type=row[2],
                application_url=row[3],
                source_url=row[4],
                canonical_url=row[5],
            )
        )
    result = {}
    for row in rows:
        fallback = preferred_link(row[6], row[5], row[7]) or row[5]
        fine = decode_fine_classification(
            row[8], row[9], row[10], row[11], row[12], row[13]
        )
        result[int(row[0])] = RecommendationOpportunityResponse(
            id=row[0],
            canonical_title=row[1],
            organization=row[2],
            location=row[3],
            last_seen_at=row[4],
            original_url=select_original_url(
                observations.get(int(row[0]), ()), fallback=fallback
            ),
            fine_primary_category=fine.primary_category,
            fine_secondary_categories=fine.secondary_categories,
            fine_category_evidence=fine.category_evidence,
            fine_reasons=fine.reasons,
            fine_classifier_version=fine.classifier_version,
        )
    return result


def _snapshot(assessment: Any) -> RecommendationSnapshotResponse:
    """Copy one persisted assessment; the reason partition is never re-judged."""
    explanation = _json_value(assessment.assessment_payload)
    result = explanation.get("result") if isinstance(explanation, dict) else None
    if not isinstance(result, dict):
        raise RecommendationApiReadError(PUBLIC_RECOMMENDATION_ERROR)
    partition = {}
    for name in REASON_PARTITIONS:
        codes = result.get(name)
        if not isinstance(codes, list) or not all(
            isinstance(code, str) and code for code in codes
        ):
            raise RecommendationApiReadError(PUBLIC_RECOMMENDATION_ERROR)
        partition[name] = codes
    return RecommendationSnapshotResponse(
        disposition=assessment.disposition.value,
        recommendation_score=assessment.recommendation_score,
        evidence_coverage=assessment.evidence_coverage,
        assessment_fingerprint=assessment.assessment_fingerprint,
        strengths=partition["strengths"],
        confirmed_gaps=partition["confirmed_gaps"],
        unknowns=partition["unknowns"],
        explanation=explanation,
    )


def _current_run(connection: Any, run: Any) -> RecommendationCurrentRunResponse:
    """The READY run, whole and in persisted rank order, or a refusal."""
    ids = [item.opportunity_id for item in run.assessments]
    if len(ids) != len(set(ids)) or len(ids) != run.assessment_count:
        raise RecommendationApiReadError(PUBLIC_RECOMMENDATION_ERROR)
    metadata = _metadata(connection, ids)
    if set(metadata) != set(ids):
        LOGGER.error(
            "Recommendation opportunity metadata is incomplete.",
            extra={
                "event": "recommendation_api_metadata_incomplete",
                "missing_count": len(set(ids) - set(metadata)),
            },
        )
        raise RecommendationApiReadError(PUBLIC_RECOMMENDATION_ERROR)
    items = [
        RecommendationItemResponse(
            opportunity_id=assessment.opportunity_id,
            rank_position=assessment.rank_position,
            opportunity=metadata[assessment.opportunity_id],
            recommendation=_snapshot(assessment),
        )
        for assessment in run.assessments
    ]
    return RecommendationCurrentRunResponse(
        run_id=run.run_id,
        created_at=run.created_at,
        assessment_count=run.assessment_count,
        persistence_version=run.persistence_version,
        input_assembly_version=run.input_assembly_version,
        recommendation_engine_version=run.recommendation_engine_version,
        recommendation_rules_version=run.recommendation_rules_version,
        source_matching_run_id=run.source_matching_run_id,
        source_matching_run_fingerprint=run.source_matching_run_fingerprint,
        batch_fingerprint=run.batch_fingerprint,
        run_fingerprint=run.run_fingerprint,
        items=items,
    )


def _project(connection: Any, profile_id: int) -> RecommendationResponse:
    current = read_current_recommendation(connection, profile_id)
    audit = audit_recommendation_profile_history(connection, profile_id)
    if not audit.ok:
        LOGGER.error(
            "Recommendation persistence audit failed.",
            extra={
                "event": "recommendation_api_audit_failed",
                "issue_codes": [item.code for item in audit.issues],
            },
        )
        raise RecommendationApiReadError(PUBLIC_RECOMMENDATION_ERROR)
    if (audit.status, audit.current_run_id) != (
        current.status,
        current.current_run_id,
    ):
        LOGGER.error(
            "Recommendation audit and read model disagree.",
            extra={"event": "recommendation_api_state_mismatch"},
        )
        raise RecommendationApiReadError(PUBLIC_RECOMMENDATION_ERROR)
    run_response = None
    if current.status == "READY":
        if current.current_run is None:
            raise RecommendationApiReadError(PUBLIC_RECOMMENDATION_ERROR)
        run_response = _current_run(connection, current.current_run)
    return RecommendationResponse(
        profile_id=current.profile_id,
        status=current.status,
        persistence_version=current.persistence_version,
        input_assembly_version=current.input_assembly_version,
        history_count=current.history_count,
        readiness_issues=[
            RecommendationReadinessIssueResponse(
                code=issue.code.value, opportunity_id=issue.opportunity_id
            )
            for issue in current.readiness_issues
        ],
        current_run=run_response,
        integrity=RecommendationIntegrityResponse(
            ok=True,
            audit_version=audit.audit_version,
            audit_fingerprint=audit.audit_fingerprint,
        ),
    )


def _read_one_snapshot(connection: Any, profile_id: int) -> RecommendationResponse:
    """Read state, audit and metadata as one picture, then release it.

    A plain deferred `BEGIN` takes no write lock, and `ROLLBACK` is the one
    ending that cannot write. A transaction the connection already holds is
    borrowed and left alone.
    """
    owns_snapshot = not connection.in_transaction
    if owns_snapshot:
        connection.execute("BEGIN")
    try:
        return _project(connection, profile_id)
    finally:
        if owns_snapshot and connection.in_transaction:
            connection.execute("ROLLBACK")


def read_recommendation_surface() -> RecommendationResponse:
    """Read and audit the configured profile's persisted Recommendation, read-only."""
    connection = None
    try:
        profile_id = _profile_id()
        settings = load_settings()
        # `mode=ro` opens an existing file only: a GET never creates a database
        # or its directory, even before `query_only` is set.
        if (
            settings.database_backend is not DatabaseBackend.SQLITE
            or settings.sqlite_database_path is None
        ):
            raise RecommendationApiReadError(PUBLIC_RECOMMENDATION_ERROR)
        connection = connect_readonly_database(settings.sqlite_database_path)
        connection.execute("PRAGMA query_only = ON")
        enabled = connection.execute("PRAGMA query_only").fetchone()
        if enabled is None or enabled[0] != 1:
            raise RecommendationApiReadError(PUBLIC_RECOMMENDATION_ERROR)
        response = _read_one_snapshot(connection, profile_id)
        LOGGER.info(
            "Recommendation API request succeeded.",
            extra={
                "event": "recommendation_api_request_succeeded",
                "status": response.status,
                "assessment_count": 0
                if response.current_run is None
                else response.current_run.assessment_count,
            },
        )
        return response
    except RecommendationApiReadError:
        raise
    except Exception as error:
        LOGGER.error(
            "Recommendation API request failed.",
            extra={
                "event": "recommendation_api_request_failed",
                "error_type": type(error).__name__,
            },
        )
        raise RecommendationApiReadError(PUBLIC_RECOMMENDATION_ERROR) from None
    finally:
        if connection is not None:
            connection.close()
