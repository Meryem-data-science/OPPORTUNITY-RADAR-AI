"""The public surface for tracking real candidatures.

Five routes, one profile, and one authority. The browser never names a
profile: like every other Opportunity Radar surface, the server resolves the
single configured profile from ``OPPORTUNITY_RADAR_PROFILE_ID``, so a request
cannot choose whose candidature it is reading or changing. A body that tries
to is refused rather than trimmed.

Reads open the database read-only and query-only; writes open it read-write
but refuse to *create* it, because a mistyped path must fail loudly instead of
leaving an empty database behind that later reads as a real, empty one. Both
refuse a database that has not been migrated through 0023.

Nothing here echoes a stack trace, a SQL message or the content of a private
note back to the caller. The five answers this surface gives are: the request
was malformed (400), the thing does not exist for this profile (404), the
request contradicts the candidature (409), it worked (200/201), or the store
cannot be used safely right now (503).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from services.applications import (
    ApplicationAction,
    ApplicationConflictError,
    ApplicationDetailView,
    ApplicationNotFoundError,
    ApplicationOpportunityView,
    ApplicationRecord,
    ApplicationRequestError,
    ApplicationStatus,
    ApplicationView,
    MAX_NEXT_ACTION_LENGTH,
    MAX_NOTES_LENGTH,
    OpportunityNotFoundError,
    TrackingUpdate,
    apply_action,
    change_status,
    normalize_followup_date,
    normalize_text,
    read_application_detail,
    read_applications,
    read_opportunity_views,
    update_tracking,
)
from services.applications.repository import preflight, read_events
from services.collector.config import DatabaseBackend, load_settings
from services.collector.database.connection import (
    connect_database,
    connect_readonly_database,
)
from services.collector.logging_config import get_logger


LOGGER = get_logger("services.api.applications")

PUBLIC_APPLICATION_ERROR = "Application tracking is temporarily unavailable."
PUBLIC_APPLICATION_REQUEST_ERROR = "Application request is invalid."
PUBLIC_APPLICATION_NOT_FOUND = "Application not found."
PUBLIC_OPPORTUNITY_NOT_FOUND = "Opportunity not found."

#: The manual tracking fields, and the only fields this surface will change
#: outside a status route. Identity, ownership, status and the submission
#: instant are not in this set and cannot be written by a client at all.
TRACKING_FIELDS = ("next_action", "followup_date", "notes")


class ApplicationApiError(RuntimeError):
    """Raised when the applications surface cannot serve a request safely."""


class ApplicationApiRequestError(RuntimeError):
    """Raised when a client sent something this surface will not accept."""


class ApplicationApiNotFoundError(RuntimeError):
    """Raised when the named thing does not exist for the current profile."""


class ApplicationApiConflictError(RuntimeError):
    """Raised when a request contradicts the candidature it would change."""


class ApplicationOpportunityResponse(BaseModel):
    id: int
    canonical_title: str
    organization: str
    location: str | None
    original_url: str


class ApplicationEventResponse(BaseModel):
    id: int
    event_type: Literal[
        "APPLICATION_CREATED", "STATUS_CHANGED", "TRACKING_UPDATED"
    ]
    from_status: str | None
    to_status: str | None
    actor_type: Literal["USER", "SYSTEM"]
    occurred_at: str


class ApplicationResponse(BaseModel):
    id: int
    opportunity_id: int
    status: str
    submitted_at: str | None
    last_status_change: str
    next_action: str | None
    followup_date: str | None
    notes: str | None
    created_at: str
    updated_at: str
    opportunity: ApplicationOpportunityResponse


class ApplicationDetailResponse(ApplicationResponse):
    events: list[ApplicationEventResponse]


class ApplicationListResponse(BaseModel):
    profile_id: int
    items: list[ApplicationResponse]
    total: int


class ApplicationWriteResponse(BaseModel):
    """The result of a write: what happened, and the candidature that resulted.

    ``created`` and ``changed`` are the honest report an idempotent surface
    owes its caller. A second "Sauvegarder" answers with the same candidature
    and both flags false, which is how the page can say "already tracked"
    instead of pretending it just did something.
    """

    created: bool
    changed: bool
    application: ApplicationDetailResponse


def _profile_id() -> int:
    raw = os.environ.get("OPPORTUNITY_RADAR_PROFILE_ID")
    try:
        value = int(raw) if raw is not None else 0
    except ValueError as error:
        raise ApplicationApiError(PUBLIC_APPLICATION_ERROR) from error
    if raw is None or value <= 0 or raw.strip() != str(value):
        raise ApplicationApiError(PUBLIC_APPLICATION_ERROR)
    return value


def _database_path() -> Path:
    settings = load_settings()
    if (
        settings.database_backend is not DatabaseBackend.SQLITE
        or settings.sqlite_database_path is None
    ):
        # SQLite is the only operational store for this phase; a deployment
        # configured for anything else is refused rather than served wrongly.
        raise ApplicationApiError(PUBLIC_APPLICATION_ERROR)
    path = Path(settings.sqlite_database_path)
    if not path.is_file():
        raise ApplicationApiError(PUBLIC_APPLICATION_ERROR)
    return path


def _connect_read() -> sqlite3.Connection:
    connection = connect_readonly_database(_database_path())
    try:
        connection.execute("PRAGMA query_only = ON")
        enabled = connection.execute("PRAGMA query_only").fetchone()
        if enabled is None or enabled[0] != 1:
            raise ApplicationApiError(PUBLIC_APPLICATION_ERROR)
        preflight(connection)
    except Exception:
        connection.close()
        raise
    return connection


def _connect_write() -> sqlite3.Connection:
    connection = connect_database(_database_path())
    try:
        preflight(connection)
    except Exception:
        connection.close()
        raise
    return connection


def _opportunity_response(
    view: ApplicationOpportunityView,
) -> ApplicationOpportunityResponse:
    return ApplicationOpportunityResponse(
        id=view.id,
        canonical_title=view.canonical_title,
        organization=view.organization,
        location=view.location,
        original_url=view.original_url,
    )


def _fields(
    record: ApplicationRecord, opportunity: ApplicationOpportunityView
) -> dict[str, Any]:
    return {
        "id": record.id,
        "opportunity_id": record.opportunity_id,
        "status": record.status.value,
        "submitted_at": record.submitted_at,
        "last_status_change": record.last_status_change,
        "next_action": record.next_action,
        "followup_date": record.followup_date,
        "notes": record.notes,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "opportunity": _opportunity_response(opportunity),
    }


def _summary(view: ApplicationView) -> ApplicationResponse:
    return ApplicationResponse(**_fields(view.application, view.opportunity))


def _detail(view: ApplicationDetailView) -> ApplicationDetailResponse:
    return ApplicationDetailResponse(
        **_fields(view.application, view.opportunity),
        events=[
            ApplicationEventResponse(
                id=event.id,
                event_type=event.event_type.value,
                from_status=None if event.from_status is None else event.from_status.value,
                to_status=None if event.to_status is None else event.to_status.value,
                actor_type=event.actor_type.value,
                occurred_at=event.occurred_at,
            )
            for event in view.events
        ],
    )


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise ApplicationApiRequestError(PUBLIC_APPLICATION_REQUEST_ERROR)
    return value


def _identifier(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ApplicationApiRequestError(f"{field} must be a positive integer")
    return value


def parse_create_request(body: Any) -> tuple[int, ApplicationAction]:
    """Read the opportunity and the intention, and nothing else.

    A body carrying ``profile_id`` is refused rather than ignored: the whole
    point of resolving the profile on the server is that a client cannot pick
    an owner, and silently dropping the field would hide an attempt to.
    """
    payload = _mapping(body)
    if set(payload) != {"opportunity_id", "action"}:
        raise ApplicationApiRequestError(PUBLIC_APPLICATION_REQUEST_ERROR)
    opportunity_id = _identifier(payload.get("opportunity_id"), "opportunity_id")
    raw_action = payload.get("action")
    if not isinstance(raw_action, str):
        raise ApplicationApiRequestError(PUBLIC_APPLICATION_REQUEST_ERROR)
    try:
        action = ApplicationAction(raw_action)
    except ValueError as error:
        allowed = ", ".join(member.value for member in ApplicationAction)
        raise ApplicationApiRequestError(
            f"action must be one of: {allowed}"
        ) from error
    return opportunity_id, action


def parse_status_request(body: Any) -> ApplicationStatus:
    """Read the single status a person is setting by hand."""
    payload = _mapping(body)
    if set(payload) != {"status"}:
        raise ApplicationApiRequestError(PUBLIC_APPLICATION_REQUEST_ERROR)
    raw_status = payload.get("status")
    if not isinstance(raw_status, str):
        raise ApplicationApiRequestError(PUBLIC_APPLICATION_REQUEST_ERROR)
    try:
        return ApplicationStatus(raw_status)
    except ValueError as error:
        # An unknown word and a known-but-not-yet-usable status are both
        # refused, and the second is refused by the domain with a sentence
        # that says why.
        raise ApplicationApiRequestError("status is not a known status") from error


def parse_tracking_request(body: Any) -> TrackingUpdate:
    """Read the tracking fields a PATCH mentioned, keeping absent ≠ null.

    A field the body did not mention is left alone; a field set to ``null`` is
    cleared. Anything outside the three tracking fields — an attempt to write
    a status, an owner, or the submission instant through this route — is
    refused outright.
    """
    payload = _mapping(body)
    unknown = set(payload) - set(TRACKING_FIELDS)
    if unknown or not payload:
        raise ApplicationApiRequestError(PUBLIC_APPLICATION_REQUEST_ERROR)
    try:
        return TrackingUpdate(
            next_action=(
                (normalize_text(
                    payload["next_action"], "next_action", MAX_NEXT_ACTION_LENGTH
                ),)
                if "next_action" in payload
                else None
            ),
            followup_date=(
                (normalize_followup_date(payload["followup_date"]),)
                if "followup_date" in payload
                else None
            ),
            notes=(
                (normalize_text(payload["notes"], "notes", MAX_NOTES_LENGTH),)
                if "notes" in payload
                else None
            ),
        )
    except ApplicationRequestError as error:
        raise ApplicationApiRequestError(str(error)) from None


def _translate(error: Exception, operation: str) -> Exception:
    """Turn a domain refusal into the answer this surface owes the caller."""
    if isinstance(error, OpportunityNotFoundError):
        return ApplicationApiNotFoundError(PUBLIC_OPPORTUNITY_NOT_FOUND)
    if isinstance(error, ApplicationNotFoundError):
        return ApplicationApiNotFoundError(PUBLIC_APPLICATION_NOT_FOUND)
    if isinstance(error, ApplicationConflictError):
        # Only status names ever reach this message; no stored text does.
        return ApplicationApiConflictError(str(error))
    if isinstance(error, ApplicationRequestError):
        return ApplicationApiRequestError(str(error))
    LOGGER.error(
        "Application request failed.",
        extra={
            "event": f"application_{operation}_failed",
            "error_type": type(error).__name__,
        },
    )
    return ApplicationApiError(PUBLIC_APPLICATION_ERROR)


def _read(operation: str, action: Any) -> Any:
    connection = None
    try:
        profile_id = _profile_id()
        connection = _connect_read()
        return action(connection, profile_id)
    except (
        ApplicationApiError,
        ApplicationApiRequestError,
        ApplicationApiNotFoundError,
        ApplicationApiConflictError,
    ):
        raise
    except Exception as error:
        raise _translate(error, operation) from None
    finally:
        if connection is not None:
            connection.close()


def _write(operation: str, action: Any) -> ApplicationWriteResponse:
    connection = None
    try:
        profile_id = _profile_id()
        connection = _connect_write()
        return action(connection, profile_id)
    except (
        ApplicationApiError,
        ApplicationApiRequestError,
        ApplicationApiNotFoundError,
        ApplicationApiConflictError,
    ):
        raise
    except Exception as error:
        raise _translate(error, operation) from None
    finally:
        if connection is not None:
            connection.close()


def list_applications_surface() -> ApplicationListResponse:
    """Every candidature of the server-resolved profile, with its opportunity."""

    def action(connection: sqlite3.Connection, profile_id: int) -> ApplicationListResponse:
        views = read_applications(connection, profile_id=profile_id)
        items = [_summary(view) for view in views]
        return ApplicationListResponse(
            profile_id=profile_id, items=items, total=len(items)
        )

    response = _read("list", action)
    LOGGER.info(
        "Applications listed.",
        extra={
            "event": "application_list_succeeded",
            "profile_id": response.profile_id,
            "items_returned": response.total,
        },
    )
    return response


def read_application_surface(application_id: int) -> ApplicationDetailResponse:
    """One candidature of the server-resolved profile, with its whole timeline."""
    identifier = _identifier(application_id, "application_id")

    def action(
        connection: sqlite3.Connection, profile_id: int
    ) -> ApplicationDetailResponse:
        view = read_application_detail(
            connection, profile_id=profile_id, application_id=identifier
        )
        if view is None:
            raise ApplicationApiNotFoundError(PUBLIC_APPLICATION_NOT_FOUND)
        return _detail(view)

    return _read("detail", action)


def _write_response(
    connection: sqlite3.Connection, outcome: Any
) -> ApplicationWriteResponse:
    record = outcome.application
    views = read_opportunity_views(connection, [record.opportunity_id])
    events = read_events(connection, [record.id]).get(record.id, [])
    detail = _detail(
        ApplicationDetailView(
            application=record,
            opportunity=views[record.opportunity_id],
            events=tuple(events),
        )
    )
    return ApplicationWriteResponse(
        created=outcome.created, changed=outcome.changed, application=detail
    )


def create_application_surface(body: Any) -> ApplicationWriteResponse:
    """Turn a real opportunity into a tracked candidature, idempotently."""
    opportunity_id, requested = parse_create_request(body)

    def action(
        connection: sqlite3.Connection, profile_id: int
    ) -> ApplicationWriteResponse:
        outcome = apply_action(
            connection,
            profile_id=profile_id,
            opportunity_id=opportunity_id,
            action=requested,
        )
        return _write_response(connection, outcome)

    return _write("create", action)


def change_status_surface(application_id: int, body: Any) -> ApplicationWriteResponse:
    """Set a candidature's status by hand, within Phase 6.1's two rules."""
    identifier = _identifier(application_id, "application_id")
    status = parse_status_request(body)

    def action(
        connection: sqlite3.Connection, profile_id: int
    ) -> ApplicationWriteResponse:
        outcome = change_status(
            connection,
            profile_id=profile_id,
            application_id=identifier,
            status=status,
        )
        return _write_response(connection, outcome)

    return _write("status", action)


def update_tracking_surface(application_id: int, body: Any) -> ApplicationWriteResponse:
    """Write the notes, next action and follow-up date, and nothing else."""
    identifier = _identifier(application_id, "application_id")
    update = parse_tracking_request(body)

    def action(
        connection: sqlite3.Connection, profile_id: int
    ) -> ApplicationWriteResponse:
        outcome = update_tracking(
            connection,
            profile_id=profile_id,
            application_id=identifier,
            update=update,
        )
        return _write_response(connection, outcome)

    return _write("tracking", action)


__all__ = [
    "ApplicationApiConflictError",
    "ApplicationApiError",
    "ApplicationApiNotFoundError",
    "ApplicationApiRequestError",
    "ApplicationDetailResponse",
    "ApplicationEventResponse",
    "ApplicationListResponse",
    "ApplicationOpportunityResponse",
    "ApplicationResponse",
    "ApplicationWriteResponse",
    "PUBLIC_APPLICATION_ERROR",
    "PUBLIC_APPLICATION_NOT_FOUND",
    "PUBLIC_APPLICATION_REQUEST_ERROR",
    "PUBLIC_OPPORTUNITY_NOT_FOUND",
    "TRACKING_FIELDS",
    "change_status_surface",
    "create_application_surface",
    "list_applications_surface",
    "read_application_surface",
    "update_tracking_surface",
]
