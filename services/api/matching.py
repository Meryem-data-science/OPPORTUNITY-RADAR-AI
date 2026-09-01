"""Read-only public surface for persisted matching snapshots."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel

from services.api.link_priority import (
    SourceObservation,
    preferred_link,
    select_original_url,
)
from services.collector.config import DatabaseBackend, load_settings
from services.collector.database.connection import connect_configured_database
from services.collector.logging_config import get_logger
from services.collector.matching.persistence_audit import audit_matching_profile_history
from services.collector.matching.read_model import read_current_matching


LOGGER = get_logger("services.collector.api.matching")
PUBLIC_MATCHING_ERROR = "Matching data is temporarily unavailable."
LANES = ("PRIMARY", "UNCERTAIN", "OUTSIDE_PREFERENCES")


class MatchingApiReadError(RuntimeError):
    """Raised when matching data cannot safely be exposed."""


class MatchingIntegrityResponse(BaseModel):
    ok: bool
    audit_version: str
    audit_fingerprint: str


class MatchingOpportunityResponse(BaseModel):
    id: int
    canonical_title: str
    organization: str
    location: str | None
    last_seen_at: str
    original_url: str


class MatchingSnapshotResponse(BaseModel):
    lane: Literal["PRIMARY", "UNCERTAIN", "OUTSIDE_PREFERENCES"]
    match_quality: float | None
    evidence_coverage: float
    assessment_fingerprint: str
    explanation: dict[str, Any]


class MatchingItemResponse(BaseModel):
    opportunity_id: int
    opportunity: MatchingOpportunityResponse
    matching: MatchingSnapshotResponse


class MatchingCurrentRunResponse(BaseModel):
    run_id: int
    created_at: str
    assessment_count: int
    persistence_version: str
    selection_version: str
    matching_engine_version: str
    matching_rules_version: str
    semantic_percentile_version: str
    run_fingerprint: str
    batch_fingerprint: str
    lane_counts: dict[str, int]
    items: list[MatchingItemResponse]


class MatchingResponse(BaseModel):
    profile_id: int
    status: Literal["NOT_SYNCED", "EMPTY", "READY"]
    persistence_version: str | None
    selection_version: str | None
    history_count: int
    current_run: MatchingCurrentRunResponse | None
    integrity: MatchingIntegrityResponse


def _profile_id() -> int:
    raw = os.environ.get("OPPORTUNITY_RADAR_PROFILE_ID")
    try:
        value = int(raw) if raw is not None else 0
    except ValueError as error:
        raise MatchingApiReadError(PUBLIC_MATCHING_ERROR) from error
    if value <= 0 or raw is None or raw.strip() != str(value):
        raise MatchingApiReadError(PUBLIC_MATCHING_ERROR)
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
) -> dict[int, MatchingOpportunityResponse]:
    if not opportunity_ids:
        return {}
    placeholders = ", ".join("?" for _ in opportunity_ids)
    rows = connection.execute(
        f"""SELECT id,canonical_title,organization,location,last_seen_at,
        source_url,application_url,canonical_url FROM opportunities
        WHERE id IN ({placeholders}) ORDER BY id ASC""",
        tuple(opportunity_ids),
    ).fetchall()
    observation_rows = connection.execute(
        f"""SELECT opportunity_sources.opportunity_id,opportunity_sources.id,
        sources.type,opportunity_sources.application_url,opportunity_sources.source_url,
        opportunity_sources.canonical_url FROM opportunity_sources JOIN sources
        ON sources.id=opportunity_sources.source_id
        WHERE opportunity_sources.opportunity_id IN ({placeholders})
        ORDER BY opportunity_sources.opportunity_id,opportunity_sources.id""",
        tuple(opportunity_ids),
    ).fetchall()
    observations: dict[int, list[SourceObservation]] = {}
    for row in observation_rows:
        observations.setdefault(int(row[0]), []).append(SourceObservation(*row[1:]))
    result = {}
    for row in rows:
        fallback = preferred_link(row[6], row[5], row[7]) or row[5]
        result[int(row[0])] = MatchingOpportunityResponse(
            id=row[0],
            canonical_title=row[1],
            organization=row[2],
            location=row[3],
            last_seen_at=row[4],
            original_url=select_original_url(
                observations.get(int(row[0]), ()), fallback=fallback
            ),
        )
    return result


def read_matching_surface() -> MatchingResponse:
    """Read and audit the configured profile's persisted snapshot without writes."""
    connection = None
    try:
        profile_id = _profile_id()
        settings = load_settings()
        connection = connect_configured_database(settings)
        if settings.database_backend is DatabaseBackend.SQLITE:
            connection.execute("PRAGMA query_only = ON")
            enabled = connection.execute("PRAGMA query_only").fetchone()
            if enabled is None or enabled[0] != 1:
                raise MatchingApiReadError(PUBLIC_MATCHING_ERROR)
        current = read_current_matching(connection, profile_id)
        audit = audit_matching_profile_history(connection, profile_id)
        if not audit.ok:
            LOGGER.error(
                "Matching persistence audit failed.",
                extra={
                    "event": "matching_api_audit_failed",
                    "issue_codes": [item.code for item in audit.issues],
                },
            )
            raise MatchingApiReadError(PUBLIC_MATCHING_ERROR)
        integrity = MatchingIntegrityResponse(
            ok=True,
            audit_version=audit.audit_version,
            audit_fingerprint=audit.audit_fingerprint,
        )
        run_response = None
        if current.status == "READY":
            run = current.current_run
            if run is None:
                raise MatchingApiReadError(PUBLIC_MATCHING_ERROR)
            ids = [item.opportunity_id for item in run.assessments]
            metadata = _metadata(connection, ids)
            if set(metadata) != set(ids):
                raise MatchingApiReadError(PUBLIC_MATCHING_ERROR)
            counts = {lane: 0 for lane in LANES}
            items = []
            for assessment in run.assessments:
                if assessment.lane not in counts:
                    raise MatchingApiReadError(PUBLIC_MATCHING_ERROR)
                counts[assessment.lane] += 1
                items.append(
                    MatchingItemResponse(
                        opportunity_id=assessment.opportunity_id,
                        opportunity=metadata[assessment.opportunity_id],
                        matching=MatchingSnapshotResponse(
                            lane=assessment.lane,
                            match_quality=assessment.match_quality,
                            evidence_coverage=assessment.evidence_coverage,
                            assessment_fingerprint=assessment.assessment_fingerprint,
                            explanation=_json_value(assessment.assessment_payload),
                        ),
                    )
                )
            if len(items) != run.assessment_count:
                raise MatchingApiReadError(PUBLIC_MATCHING_ERROR)
            run_response = MatchingCurrentRunResponse(
                run_id=run.run_id,
                created_at=run.created_at,
                assessment_count=run.assessment_count,
                persistence_version=run.persistence_version,
                selection_version=run.selection_version,
                matching_engine_version=run.matching_engine_version,
                matching_rules_version=run.matching_rules_version,
                semantic_percentile_version=run.semantic_percentile_version,
                run_fingerprint=run.run_fingerprint,
                batch_fingerprint=run.batch_fingerprint,
                lane_counts=counts,
                items=items,
            )
        response = MatchingResponse(
            profile_id=current.profile_id,
            status=current.status,
            persistence_version=current.persistence_version,
            selection_version=current.selection_version,
            history_count=current.history_count,
            current_run=run_response,
            integrity=integrity,
        )
        LOGGER.info(
            "Matching API request succeeded.",
            extra={
                "event": "matching_api_request_succeeded",
                "status": response.status,
                "assessment_count": 0
                if run_response is None
                else run_response.assessment_count,
            },
        )
        return response
    except MatchingApiReadError:
        raise
    except Exception as error:
        LOGGER.error(
            "Matching API request failed.",
            extra={
                "event": "matching_api_request_failed",
                "error_type": type(error).__name__,
            },
        )
        raise MatchingApiReadError(PUBLIC_MATCHING_ERROR) from None
    finally:
        if connection is not None:
            connection.close()
