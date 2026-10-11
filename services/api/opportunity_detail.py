"""Read one collected, visible opportunity without contacting its source."""

import os
from typing import Any

from services.api.morocco_catalogue import read_morocco_catalogue_ids
from services.api.opportunities import (
    OpportunityReadError,
    OpportunityResponse,
    PUBLIC_DATABASE_ERROR,
    _read_observations,
    _to_response,
)
from services.collector.config import DatabaseBackend, load_settings
from services.collector.database.connection import connect_readonly_database
from services.collector.extractors.opportunity_constraints.text import (
    normalize_description,
)
from services.collector.logging_config import get_logger
from services.collector.qualification.taxonomy import (
    OpportunityType as QualificationOpportunityType,
)


LOGGER = get_logger("services.collector.api.opportunity_detail")
PUBLIC_OPPORTUNITY_NOT_FOUND = "Opportunity is unavailable."


class OpportunityDetailResponse(OpportunityResponse):
    """The existing summary plus text and dates actually collected.

    ``description`` is text normalized from the stored source content, never
    HTML to execute or a generated summary. A missing or empty description is
    None. ``employment_type`` is the collected value, not an inferred CDI/CDD.
    ``original_url`` retains the shared official/application link policy.
    """

    description: str | None
    published_at: str | None
    deadline: str | None
    employment_type: str | None
    listing_type: QualificationOpportunityType | None


class OpportunityNotFoundError(RuntimeError):
    """The requested posting is missing or is no longer publicly visible."""


def _is_tracked_for_configured_profile(connection: Any, opportunity_id: int) -> bool:
    """Keep access to an already tracked foreign posting without expanding search."""
    raw = os.environ.get("OPPORTUNITY_RADAR_PROFILE_ID")
    try:
        profile_id = int(raw) if raw is not None else 0
    except ValueError:
        return False
    if raw is None or profile_id <= 0 or raw.strip() != str(profile_id):
        return False
    row = connection.execute(
        "SELECT 1 FROM applications WHERE profile_id = ? AND opportunity_id = ? LIMIT 1",
        (profile_id, opportunity_id),
    ).fetchone()
    return row is not None


def _read_detail_snapshot(
    connection: Any, opportunity_id: int
) -> OpportunityDetailResponse:
    """Read the posting and its link observations in one deferred snapshot."""
    owns_snapshot = not connection.in_transaction
    if owns_snapshot:
        connection.execute("BEGIN")
    try:
        if (opportunity_id not in read_morocco_catalogue_ids(connection, (opportunity_id,))
                and not _is_tracked_for_configured_profile(connection, opportunity_id)):
            raise OpportunityNotFoundError(PUBLIC_OPPORTUNITY_NOT_FOUND)
        # The first 17 columns deliberately match the existing listing reader:
        # the same adapter decodes fine classification and chooses the original
        # link. LEFT JOIN preserves a visible posting with no qualification.
        row = connection.execute(
            """
            SELECT o.id, o.canonical_title, o.organization, o.location,
                   o.source_url, o.application_url, o.canonical_url,
                   o.discovered_at, o.last_seen_at, o.status,
                   LENGTH(COALESCE(o.description, '')),
                   q.qualification, q.fine_primary_category,
                   q.fine_secondary_categories_json,
                   q.fine_category_evidence_json, q.fine_reasons_json,
                   q.fine_classifier_version,
                   o.description, o.published_at, o.deadline,
                   o.employment_type, q.opportunity_type
            FROM opportunities AS o
            LEFT JOIN opportunity_qualifications AS q
                   ON q.opportunity_id = o.id
            WHERE o.id = ? AND o.status = ? AND o.is_active = ?
            """,
            (opportunity_id, "visible", 1),
        ).fetchone()
        if row is None:
            raise OpportunityNotFoundError(PUBLIC_OPPORTUNITY_NOT_FOUND)
        observations = _read_observations(connection, (opportunity_id,))
        summary = _to_response(row, observations.get(opportunity_id, ()))
        return OpportunityDetailResponse(
            **summary.model_dump(),
            description=normalize_description(row[17]) or None,
            published_at=row[18],
            deadline=row[19],
            employment_type=row[20],
            listing_type=row[21],
        )
    finally:
        if owns_snapshot and connection.in_transaction:
            connection.execute("ROLLBACK")


def read_opportunity_detail(opportunity_id: int) -> OpportunityDetailResponse:
    """Read an existing SQLite posting; create, update and classify nothing."""
    connection = None
    try:
        settings = load_settings()
        # Like Explorer, this local read surface refuses unsupported backends.
        # mode=ro requires an existing file and creates no database directory.
        if (
            settings.database_backend is not DatabaseBackend.SQLITE
            or settings.sqlite_database_path is None
        ):
            raise OpportunityReadError(PUBLIC_DATABASE_ERROR)
        connection = connect_readonly_database(settings.sqlite_database_path)
        connection.execute("PRAGMA query_only = ON")
        enabled = connection.execute("PRAGMA query_only").fetchone()
        if enabled is None or enabled[0] != 1:
            raise OpportunityReadError(PUBLIC_DATABASE_ERROR)
        detail = _read_detail_snapshot(connection, opportunity_id)
        LOGGER.info(
            "Opportunity detail request succeeded.",
            extra={"event": "opportunity_detail_request_succeeded"},
        )
        return detail
    except OpportunityNotFoundError:
        raise
    except Exception as error:
        # Neither a URL, collected description nor underlying exception text
        # belongs in a log or a public 503 response.
        LOGGER.error(
            "Opportunity detail request failed.",
            extra={
                "event": "opportunity_detail_request_failed",
                "error_type": type(error).__name__,
            },
        )
        raise OpportunityReadError(PUBLIC_DATABASE_ERROR) from None
    finally:
        if connection is not None:
            connection.close()
