"""Read-only opportunity listing service and response models."""

from typing import Any, Sequence

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
from services.collector.database.connection import connect_configured_database
from services.collector.logging_config import get_logger
from services.collector.qualification.fine_taxonomy import FineCategory


LOGGER = get_logger("services.collector.api.opportunities")
PUBLIC_DATABASE_ERROR = "Opportunity data is temporarily unavailable."


class OpportunityResponse(BaseModel):
    """Public summary of one persisted opportunity."""

    id: int
    canonical_title: str
    organization: str
    location: str | None
    source_url: str
    application_url: str | None
    canonical_url: str | None
    original_url: str
    discovered_at: str
    last_seen_at: str
    status: str
    description_length: int
    #: The Phase 8 fine Data/AI classification, read from
    #: `opportunity_qualifications` exactly as persistence wrote it. Nothing is
    #: classified during a request. All five are `None` together when the row was
    #: never fine-classified — or has no qualification row at all — which is a
    #: different statement from a classified row carrying no category, and from
    #: `OTHER`. Once `fine_classifier_version` is present the three collections
    #: are lists, possibly empty, and never `None`.
    fine_primary_category: FineCategory | None
    fine_secondary_categories: list[FineCategory] | None
    fine_category_evidence: list[FineEvidenceResponse] | None
    fine_reasons: list[str] | None
    fine_classifier_version: str | None


class OpportunityListResponse(BaseModel):
    """Stable envelope for a bounded opportunity listing."""

    items: list[OpportunityResponse]
    returned: int
    total: int


class OpportunityReadError(RuntimeError):
    """Raised when configured opportunity data cannot be read safely."""


def _read_observations(
    connection: Any, opportunity_ids: Sequence[int]
) -> dict[int, list[SourceObservation]]:
    """Read every persisted observation of these opportunities, read-only."""
    if not opportunity_ids:
        return {}
    placeholders = ", ".join("?" for _ in opportunity_ids)
    rows = connection.execute(
        f"""
        SELECT opportunity_sources.opportunity_id, opportunity_sources.id,
               sources.type, opportunity_sources.application_url,
               opportunity_sources.source_url, opportunity_sources.canonical_url
        FROM opportunity_sources
        JOIN sources ON sources.id = opportunity_sources.source_id
        WHERE opportunity_sources.opportunity_id IN ({placeholders})
        ORDER BY opportunity_sources.opportunity_id, opportunity_sources.id
        """,
        tuple(opportunity_ids),
    ).fetchall()
    observations: dict[int, list[SourceObservation]] = {}
    for row in rows:
        observations.setdefault(int(row[0]), []).append(
            SourceObservation(
                observation_id=int(row[1]),
                source_type=row[2],
                application_url=row[3],
                source_url=row[4],
                canonical_url=row[5],
            )
        )
    return observations


def _to_response(
    row: Any, observations: Sequence[SourceObservation]
) -> OpportunityResponse:
    fallback = preferred_link(row[5], row[4], row[6]) or row[4]
    original_url = select_original_url(observations, fallback=fallback)
    # `row[11]` is the persisted coarse qualification. It is read only so the
    # fine values can be validated against it, and is deliberately not exposed:
    # this phase adds five public fields and no sixth.
    fine = decode_fine_classification(
        row[11], row[12], row[13], row[14], row[15], row[16]
    )
    return OpportunityResponse(
        id=row[0],
        canonical_title=row[1],
        organization=row[2],
        location=row[3],
        source_url=row[4],
        application_url=row[5],
        canonical_url=row[6],
        original_url=original_url,
        discovered_at=row[7],
        last_seen_at=row[8],
        status=row[9],
        description_length=row[10],
        fine_primary_category=fine.primary_category,
        fine_secondary_categories=fine.secondary_categories,
        fine_category_evidence=fine.category_evidence,
        fine_reasons=fine.reasons,
        fine_classifier_version=fine.classifier_version,
    )


def read_opportunities(limit: int) -> OpportunityListResponse:
    """Read visible, active opportunities from the configured database."""
    connection = None
    try:
        settings = load_settings()
        connection = connect_configured_database(settings)
        if settings.database_backend is DatabaseBackend.SQLITE:
            connection.execute("PRAGMA query_only = ON")
        total_row = connection.execute(
            "SELECT COUNT(*) FROM opportunities WHERE status = ? AND is_active = ?",
            ("visible", 1),
        ).fetchone()
        # The join is additive and must stay a LEFT JOIN: an opportunity that
        # has never been qualified is still a visible, active opportunity, and
        # it keeps its place in this listing and in `total`. Migration 0004
        # makes `opportunity_qualifications.opportunity_id` the primary key, so
        # the join adds columns and can never add or drop a row. The coarse
        # `qualification` is selected to validate the fine values against it;
        # it stays internal and reaches no response field.
        rows = connection.execute(
            """
            SELECT opportunities.id, opportunities.canonical_title,
                   opportunities.organization, opportunities.location,
                   opportunities.source_url, opportunities.application_url,
                   opportunities.canonical_url, opportunities.discovered_at,
                   opportunities.last_seen_at, opportunities.status,
                   LENGTH(COALESCE(opportunities.description, '')),
                   opportunity_qualifications.qualification,
                   opportunity_qualifications.fine_primary_category,
                   opportunity_qualifications.fine_secondary_categories_json,
                   opportunity_qualifications.fine_category_evidence_json,
                   opportunity_qualifications.fine_reasons_json,
                   opportunity_qualifications.fine_classifier_version
            FROM opportunities
            LEFT JOIN opportunity_qualifications
                   ON opportunity_qualifications.opportunity_id = opportunities.id
            WHERE opportunities.status = ? AND opportunities.is_active = ?
            ORDER BY opportunities.last_seen_at DESC, opportunities.id DESC
            LIMIT ?
            """,
            ("visible", 1, limit),
        ).fetchall()
        observations = _read_observations(
            connection, [int(row[0]) for row in rows]
        )
        items = [
            _to_response(row, observations.get(int(row[0]), ())) for row in rows
        ]
        response = OpportunityListResponse(
            items=items, returned=len(items), total=int(total_row[0])
        )
        LOGGER.info(
            "Opportunity API request succeeded.",
            extra={
                "event": "opportunity_api_request_succeeded",
                "items_returned": response.returned,
                "request_limit": limit,
            },
        )
        return response
    except Exception as error:
        LOGGER.error(
            "Opportunity API request failed.",
            extra={
                "event": "opportunity_api_request_failed",
                "error_type": type(error).__name__,
                "request_limit": limit,
            },
        )
        raise OpportunityReadError(PUBLIC_DATABASE_ERROR) from None
    finally:
        if connection is not None:
            connection.close()
