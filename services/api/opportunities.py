"""Read-only opportunity listing service and response models."""

from typing import Any, Sequence

from pydantic import BaseModel

from services.api.link_priority import (
    SourceObservation,
    preferred_link,
    select_original_url,
)
from services.collector.config import DatabaseBackend, load_settings
from services.collector.database.connection import connect_configured_database
from services.collector.logging_config import get_logger


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
        rows = connection.execute(
            """
            SELECT id, canonical_title, organization, location, source_url,
                   application_url, canonical_url, discovered_at, last_seen_at,
                   status, LENGTH(COALESCE(description, ''))
            FROM opportunities
            WHERE status = ? AND is_active = ?
            ORDER BY last_seen_at DESC, id DESC
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
