"""Read-only public surface for audited persisted Portfolio snapshots."""

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
from services.portfolio.audit import (
    PortfolioProfileAuditStatus,
    audit_current_portfolio,
)
from services.portfolio.read_model import read_current_portfolio

LOGGER = get_logger("services.collector.api.portfolio")
PUBLIC_PORTFOLIO_ERROR = "Portfolio data is temporarily unavailable."
BUCKETS = ("SAFE", "TARGET", "AMBITIOUS")


class PortfolioApiReadError(RuntimeError):
    """Raised when Portfolio data cannot safely be exposed."""


class PortfolioIntegrityResponse(BaseModel):
    ok: bool


class PortfolioOpportunityResponse(BaseModel):
    id: int
    canonical_title: str
    organization: str
    location: str | None
    last_seen_at: str
    original_url: str


class PortfolioSnapshotResponse(BaseModel):
    disposition: Literal["INCLUDED", "EXCLUDED"]
    bucket: Literal["SAFE", "TARGET", "AMBITIOUS"] | None
    priority_category: Literal["URGENT", "HIGH", "MEDIUM", "LOW", "IGNORE"] | None
    eligibility_status: Literal["ELIGIBLE", "UNKNOWN", "INELIGIBLE"]
    matching_lane: Literal["PRIMARY", "UNCERTAIN", "OUTSIDE_PREFERENCES"]
    required_skill_score: float | None
    required_skill_matched_count: int
    required_skill_total_count: int
    safe_cap_applied: bool
    reason_codes: list[str]
    assessment_fingerprint: str
    explanation: dict[str, Any]


class PortfolioItemResponse(BaseModel):
    opportunity_id: int
    opportunity: PortfolioOpportunityResponse
    portfolio: PortfolioSnapshotResponse


class PortfolioCurrentRunResponse(BaseModel):
    run_id: int
    priority_run_id: int
    matching_run_id: int
    created_at: str
    assessment_count: int
    included_count: int
    excluded_count: int
    bucket_counts: dict[str, int]
    persistence_version: str
    input_assembly_version: str
    portfolio_engine_version: str
    portfolio_rules_version: str
    priority_run_fingerprint: str
    matching_run_fingerprint: str
    run_fingerprint: str
    items: list[PortfolioItemResponse]


class PortfolioResponse(BaseModel):
    profile_id: int
    status: Literal["NOT_SYNCED", "READY"]
    persistence_version: str | None
    input_assembly_version: str | None
    history_count: int
    current_run: PortfolioCurrentRunResponse | None
    integrity: PortfolioIntegrityResponse


def _profile_id() -> int:
    raw = os.environ.get("OPPORTUNITY_RADAR_PROFILE_ID")
    try:
        value = int(raw) if raw is not None else 0
    except ValueError as error:
        raise PortfolioApiReadError(PUBLIC_PORTFOLIO_ERROR) from error
    if raw is None or value <= 0 or raw.strip() != str(value):
        raise PortfolioApiReadError(PUBLIC_PORTFOLIO_ERROR)
    return value


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _metadata(
    connection: Any, ids: list[int]
) -> dict[int, PortfolioOpportunityResponse]:
    if not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    rows = connection.execute(
        f"""SELECT id,canonical_title,organization,location,last_seen_at,
        source_url,application_url,canonical_url FROM opportunities
        WHERE id IN ({placeholders}) ORDER BY id ASC""",
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
        result[int(row[0])] = PortfolioOpportunityResponse(
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


def _snapshot(assessment: Any) -> PortfolioSnapshotResponse:
    payload = _json_value(assessment.assessment_payload)
    try:
        reason_codes = payload["result"]["reason_codes"]
    except (KeyError, TypeError) as error:
        raise PortfolioApiReadError(PUBLIC_PORTFOLIO_ERROR) from error
    if not isinstance(reason_codes, list) or not all(
        isinstance(code, str) for code in reason_codes
    ):
        raise PortfolioApiReadError(PUBLIC_PORTFOLIO_ERROR)
    return PortfolioSnapshotResponse(
        disposition=assessment.disposition.value,
        bucket=None if assessment.bucket is None else assessment.bucket.value,
        priority_category=None
        if assessment.priority_category is None
        else assessment.priority_category.value,
        eligibility_status=assessment.eligibility_status.value,
        matching_lane=assessment.matching_lane.value,
        required_skill_score=assessment.required_skill_score,
        required_skill_matched_count=assessment.required_skill_matched_count,
        required_skill_total_count=assessment.required_skill_total_count,
        safe_cap_applied=assessment.safe_cap_applied,
        reason_codes=reason_codes,
        assessment_fingerprint=assessment.assessment_fingerprint,
        explanation=payload,
    )


def read_portfolio_surface() -> PortfolioResponse:
    """Expose only the audited snapshot through a query-only SQLite connection."""
    connection = None
    try:
        profile_id = _profile_id()
        settings = load_settings()
        if (
            settings.database_backend is not DatabaseBackend.SQLITE
            or settings.sqlite_database_path is None
        ):
            raise PortfolioApiReadError(PUBLIC_PORTFOLIO_ERROR)
        connection = connect_readonly_database(settings.sqlite_database_path)
        connection.execute("PRAGMA query_only = ON")
        enabled = connection.execute("PRAGMA query_only").fetchone()
        if enabled is None or enabled[0] != 1:
            raise PortfolioApiReadError(PUBLIC_PORTFOLIO_ERROR)
        current = read_current_portfolio(connection, profile_id)
        audit = audit_current_portfolio(connection, profile_id)
        if audit.status is PortfolioProfileAuditStatus.CORRUPT:
            LOGGER.error(
                "Portfolio audit failed.",
                extra={
                    "event": "portfolio_api_audit_failed",
                    "issue_codes": [issue.code.value for issue in audit.issues],
                },
            )
            raise PortfolioApiReadError(PUBLIC_PORTFOLIO_ERROR)
        run_response = None
        if current.status.value == "READY":
            if (
                audit.status is not PortfolioProfileAuditStatus.READY
                or current.current_run is None
            ):
                raise PortfolioApiReadError(PUBLIC_PORTFOLIO_ERROR)
            run = current.current_run
            ids = [item.opportunity_id for item in run.assessments]
            metadata = _metadata(connection, ids)
            if set(metadata) != set(ids) or len(ids) != len(set(ids)):
                raise PortfolioApiReadError(PUBLIC_PORTFOLIO_ERROR)
            items = [
                PortfolioItemResponse(
                    opportunity_id=a.opportunity_id,
                    opportunity=metadata[a.opportunity_id],
                    portfolio=_snapshot(a),
                )
                for a in run.assessments
            ]
            items.sort(key=lambda item: item.opportunity_id)
            bucket_counts = {
                "SAFE": run.safe_count,
                "TARGET": run.target_count,
                "AMBITIOUS": run.ambitious_count,
            }
            if (
                len(items) != run.assessment_count
                or run.included_count + run.excluded_count != run.assessment_count
                or sum(bucket_counts.values()) != run.included_count
            ):
                raise PortfolioApiReadError(PUBLIC_PORTFOLIO_ERROR)
            for item in items:
                snapshot = item.portfolio
                included = snapshot.disposition == "INCLUDED"
                matched, total, score = (
                    snapshot.required_skill_matched_count,
                    snapshot.required_skill_total_count,
                    snapshot.required_skill_score,
                )
                if (
                    included != (snapshot.bucket is not None)
                    or matched < 0
                    or total < 0
                    or matched > total
                    or (total == 0 and (matched != 0 or score is not None))
                    or (
                        total > 0
                        and (
                            score is None
                            or round(score, 12) != round(matched / total, 12)
                        )
                    )
                ):
                    raise PortfolioApiReadError(PUBLIC_PORTFOLIO_ERROR)
            run_response = PortfolioCurrentRunResponse(
                run_id=run.run_id,
                priority_run_id=run.priority_run_id,
                matching_run_id=run.matching_run_id,
                created_at=run.created_at,
                assessment_count=run.assessment_count,
                included_count=run.included_count,
                excluded_count=run.excluded_count,
                bucket_counts=bucket_counts,
                persistence_version=run.persistence_version,
                input_assembly_version=run.input_assembly_version,
                portfolio_engine_version=run.portfolio_engine_version,
                portfolio_rules_version=run.portfolio_rules_version,
                priority_run_fingerprint=run.priority_run_fingerprint,
                matching_run_fingerprint=run.matching_run_fingerprint,
                run_fingerprint=run.run_fingerprint,
                items=items,
            )
        return PortfolioResponse(
            profile_id=current.profile_id,
            status=current.status.value,
            persistence_version=current.persistence_version,
            input_assembly_version=current.input_assembly_version,
            history_count=current.history_count,
            current_run=run_response,
            integrity=PortfolioIntegrityResponse(ok=True),
        )
    except PortfolioApiReadError:
        raise
    except Exception as error:
        LOGGER.error(
            "Portfolio API request failed.",
            extra={
                "event": "portfolio_api_request_failed",
                "error_type": type(error).__name__,
            },
        )
        raise PortfolioApiReadError(PUBLIC_PORTFOLIO_ERROR) from None
    finally:
        if connection is not None:
            connection.close()
