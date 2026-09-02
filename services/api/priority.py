"""Read-only public surface for audited persisted Priority snapshots."""

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
from services.collector.database.connection import connect_readonly_database
from services.collector.logging_config import get_logger
from services.priority.audit import PriorityProfileAuditStatus, audit_current_priority
from services.priority.read_model import read_current_priority

LOGGER = get_logger("services.collector.api.priority")
PUBLIC_PRIORITY_ERROR = "Priority data is temporarily unavailable."
CATEGORIES = ("URGENT", "HIGH", "MEDIUM", "LOW", "IGNORE", "NONE")


class PriorityApiReadError(RuntimeError):
    """Raised when Priority data cannot safely be exposed."""


class PriorityIntegrityResponse(BaseModel):
    ok: bool


class PriorityOpportunityResponse(BaseModel):
    id: int
    canonical_title: str
    organization: str
    location: str | None
    last_seen_at: str
    original_url: str


class PrioritySnapshotResponse(BaseModel):
    category: Literal["URGENT", "HIGH", "MEDIUM", "LOW", "IGNORE"] | None
    priority_score: float | None
    priority_evidence_coverage: float
    eligibility_status: str
    matching_lane: str
    assessment_fingerprint: str
    published_at: str | None
    deadline: str | None
    deadline_status: str
    reason_codes: list[str]
    hard_blocker: str | None
    coverage_guardrail_applied: bool
    outside_preferences_cap_applied: bool
    urgent_promotion_applied: bool
    explanation: dict[str, Any]


class PriorityItemResponse(BaseModel):
    opportunity_id: int
    opportunity: PriorityOpportunityResponse
    priority: PrioritySnapshotResponse


class PriorityCurrentRunResponse(BaseModel):
    run_id: int
    matching_run_id: int
    evaluation_date: str
    created_at: str
    assessment_count: int
    persistence_version: str
    input_assembly_version: str
    priority_engine_version: str
    priority_rules_version: str
    freshness_version: str
    quality_version: str
    matching_run_fingerprint: str
    run_fingerprint: str
    category_counts: dict[str, int]
    items: list[PriorityItemResponse]


class PriorityResponse(BaseModel):
    profile_id: int
    status: Literal["NOT_SYNCED", "READY"]
    persistence_version: str | None
    input_assembly_version: str | None
    history_count: int
    current_run: PriorityCurrentRunResponse | None
    integrity: PriorityIntegrityResponse


def _profile_id() -> int:
    raw = os.environ.get("OPPORTUNITY_RADAR_PROFILE_ID")
    try:
        value = int(raw) if raw is not None else 0
    except ValueError as error:
        raise PriorityApiReadError(PUBLIC_PRIORITY_ERROR) from error
    if raw is None or value <= 0 or raw.strip() != str(value):
        raise PriorityApiReadError(PUBLIC_PRIORITY_ERROR)
    return value


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _metadata(
    connection: Any, ids: list[int]
) -> dict[int, PriorityOpportunityResponse]:
    if not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    rows = connection.execute(
        f"""SELECT id,canonical_title,organization,location,last_seen_at,
        source_url,application_url,canonical_url FROM opportunities
        WHERE id IN ({placeholders}) ORDER BY id""",
        tuple(ids),
    ).fetchall()
    source_rows = connection.execute(
        f"""SELECT opportunity_sources.opportunity_id,opportunity_sources.id,sources.type,
        opportunity_sources.application_url,opportunity_sources.source_url,
        opportunity_sources.canonical_url FROM opportunity_sources JOIN sources
        ON sources.id=opportunity_sources.source_id
        WHERE opportunity_sources.opportunity_id IN ({placeholders})
        ORDER BY opportunity_sources.opportunity_id,opportunity_sources.id""",
        tuple(ids),
    ).fetchall()
    observations: dict[int, list[SourceObservation]] = {}
    for row in source_rows:
        observations.setdefault(int(row[0]), []).append(SourceObservation(*row[1:]))
    result = {}
    for row in rows:
        fallback = preferred_link(row[6], row[5], row[7]) or row[5]
        result[int(row[0])] = PriorityOpportunityResponse(
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


def _snapshot(assessment: Any) -> PrioritySnapshotResponse:
    payload = _json_value(assessment.assessment_payload)
    try:
        dates, result = payload["dates"], payload["result"]
        published_at, deadline = dates["published_at"], dates["deadline"]
        reason_codes = result["reason_codes"]
        values = (
            result["deadline_status"],
            result["hard_blocker"],
            result["coverage_guardrail_applied"],
            result["outside_preferences_cap_applied"],
            result["urgent_promotion_applied"],
        )
    except (KeyError, TypeError) as error:
        raise PriorityApiReadError(PUBLIC_PRIORITY_ERROR) from error
    if (
        not isinstance(dates, dict)
        or not isinstance(result, dict)
        or not all(
            value is None or isinstance(value, str)
            for value in (published_at, deadline)
        )
        or not isinstance(reason_codes, list)
        or not all(isinstance(code, str) for code in reason_codes)
        or not isinstance(values[0], str)
        or not (values[1] is None or isinstance(values[1], str))
        or not all(isinstance(value, bool) for value in values[2:])
    ):
        raise PriorityApiReadError(PUBLIC_PRIORITY_ERROR)
    return PrioritySnapshotResponse(
        category=None
        if assessment.priority_category is None
        else assessment.priority_category.value,
        priority_score=assessment.priority_score,
        priority_evidence_coverage=assessment.priority_evidence_coverage,
        eligibility_status=assessment.eligibility_status.value,
        matching_lane=assessment.matching_lane.value,
        assessment_fingerprint=assessment.assessment_fingerprint,
        published_at=published_at,
        deadline=deadline,
        deadline_status=values[0],
        reason_codes=reason_codes,
        hard_blocker=values[1],
        coverage_guardrail_applied=values[2],
        outside_preferences_cap_applied=values[3],
        urgent_promotion_applied=values[4],
        explanation=payload,
    )


def read_priority_surface() -> PriorityResponse:
    """Read through a mode=ro SQLite connection and audit before exposure."""
    connection = None
    try:
        profile_id = _profile_id()
        settings = load_settings()
        if (
            settings.database_backend is not DatabaseBackend.SQLITE
            or settings.sqlite_database_path is None
        ):
            raise PriorityApiReadError(PUBLIC_PRIORITY_ERROR)
        connection = connect_readonly_database(settings.sqlite_database_path)
        connection.execute("PRAGMA query_only = ON")
        enabled = connection.execute("PRAGMA query_only").fetchone()
        if enabled is None or enabled[0] != 1:
            raise PriorityApiReadError(PUBLIC_PRIORITY_ERROR)
        current = read_current_priority(connection, profile_id)
        audit = audit_current_priority(connection, profile_id)
        if audit.status is PriorityProfileAuditStatus.CORRUPT:
            LOGGER.error(
                "Priority audit failed.",
                extra={
                    "event": "priority_api_audit_failed",
                    "issue_codes": [issue.code.value for issue in audit.issues],
                },
            )
            raise PriorityApiReadError(PUBLIC_PRIORITY_ERROR)
        run_response = None
        if current.status.value == "READY":
            if (
                audit.status is not PriorityProfileAuditStatus.READY
                or current.current_run is None
            ):
                raise PriorityApiReadError(PUBLIC_PRIORITY_ERROR)
            run = current.current_run
            ids = [item.opportunity_id for item in run.assessments]
            metadata = _metadata(connection, ids)
            if set(metadata) != set(ids) or len(ids) != len(set(ids)):
                raise PriorityApiReadError(PUBLIC_PRIORITY_ERROR)
            counts = {category: 0 for category in CATEGORIES}
            items = []
            for assessment in run.assessments:
                category = (
                    "NONE"
                    if assessment.priority_category is None
                    else assessment.priority_category.value
                )
                counts[category] += 1
                items.append(
                    PriorityItemResponse(
                        opportunity_id=assessment.opportunity_id,
                        opportunity=metadata[assessment.opportunity_id],
                        priority=_snapshot(assessment),
                    )
                )
            rank = {category: index for index, category in enumerate(CATEGORIES)}
            items.sort(
                key=lambda item: (
                    rank[
                        "NONE"
                        if item.priority.category is None
                        else item.priority.category
                    ],
                    item.priority.priority_score is None,
                    -(item.priority.priority_score or 0),
                    item.opportunity_id,
                )
            )
            if (
                len(items) != run.assessment_count
                or sum(counts.values()) != run.assessment_count
            ):
                raise PriorityApiReadError(PUBLIC_PRIORITY_ERROR)
            run_response = PriorityCurrentRunResponse(
                run_id=run.run_id,
                matching_run_id=run.matching_run_id,
                evaluation_date=run.evaluation_date.isoformat(),
                created_at=run.created_at,
                assessment_count=run.assessment_count,
                persistence_version=run.persistence_version,
                input_assembly_version=run.input_assembly_version,
                priority_engine_version=run.priority_engine_version,
                priority_rules_version=run.priority_rules_version,
                freshness_version=run.freshness_version,
                quality_version=run.quality_version,
                matching_run_fingerprint=run.matching_run_fingerprint,
                run_fingerprint=run.run_fingerprint,
                category_counts=counts,
                items=items,
            )
        return PriorityResponse(
            profile_id=current.profile_id,
            status=current.status.value,
            persistence_version=current.persistence_version,
            input_assembly_version=current.input_assembly_version,
            history_count=current.history_count,
            current_run=run_response,
            integrity=PriorityIntegrityResponse(ok=True),
        )
    except PriorityApiReadError:
        raise
    except Exception as error:
        LOGGER.error(
            "Priority API request failed.",
            extra={
                "event": "priority_api_request_failed",
                "error_type": type(error).__name__,
            },
        )
        raise PriorityApiReadError(PUBLIC_PRIORITY_ERROR) from None
    finally:
        if connection is not None:
            connection.close()
