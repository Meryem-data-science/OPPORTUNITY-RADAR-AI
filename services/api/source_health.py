"""Read-only source health service and response models.

The HTTP layer only reshapes what the database read model already decided. The
zero-result streak, the anomaly code and the anomaly message are computed once,
in the backend, so no client has to re-derive them.

Reading is read-only in the strong sense. Source health is defined over the
operational SQLite database, so this service opens that database itself through
a ``mode=ro`` connection rather than through the general backend factory: a
request cannot create the file, and it cannot reach a remote backend.
"""

from pydantic import BaseModel

from services.collector.config import DatabaseBackend, Settings, load_settings
from services.collector.database.connection import (
    DatabaseConnection,
    connect_readonly_database,
)
from services.collector.database.source_health import (
    SourceHealth,
    read_source_health as read_source_health_model,
)
from services.collector.logging_config import get_logger
from services.collector.sources import SourceConfig, load_source_registry


LOGGER = get_logger("services.collector.api.source_health")
PUBLIC_SOURCE_HEALTH_ERROR = "Source health data is temporarily unavailable."


class SourceHealthResponse(BaseModel):
    """Public health summary of one known source.

    Every optional field is ``None`` when the underlying run did not know it, or
    when the source has never run at all. ``None`` is never replaced by a zero.
    """

    source_id: str
    enabled: bool
    last_run_at: str | None
    status: str | None
    items_found: int | None
    new_items: int | None
    relevant_items: int | None
    error_type: str | None
    error_message: str | None
    zero_result_streak: int
    anomaly_code: str | None
    anomaly_message: str | None


class SourceHealthListResponse(BaseModel):
    """Stable envelope holding one entry per known source."""

    items: list[SourceHealthResponse]
    returned: int


class SourceHealthReadError(RuntimeError):
    """Raised when configured source health cannot be read safely."""


class SourceHealthBackendError(RuntimeError):
    """Raised when the configured backend is not the operational SQLite one.

    Source health is derived from the operational SQLite database. Asked for any
    other backend it refuses locally instead of reaching for a remote one, so no
    credential is requested and no network call is made.
    """


def _to_response(health: SourceHealth) -> SourceHealthResponse:
    return SourceHealthResponse(
        source_id=health.source_id,
        enabled=health.enabled,
        last_run_at=health.last_run_at,
        status=health.status,
        items_found=health.items_found,
        new_items=health.new_items,
        relevant_items=health.relevant_items,
        error_type=health.error_type,
        error_message=health.error_message,
        zero_result_streak=health.zero_result_streak,
        anomaly_code=health.anomaly_code,
        anomaly_message=health.anomaly_message,
    )


def _configured_sources() -> list[SourceConfig]:
    """Read the validated source catalogue so a never-run source is still shown."""
    return load_source_registry()


def _readonly_connection(settings: Settings) -> DatabaseConnection:
    """Open the operational SQLite database read-only, or refuse locally.

    Any non-SQLite backend is rejected here, before any connection is attempted,
    so no remote connector is called and no token is read for this request.
    """
    if settings.database_backend is not DatabaseBackend.SQLITE:
        raise SourceHealthBackendError(
            "source health reads the operational SQLite database only"
        )
    if settings.sqlite_database_path is None:
        raise SourceHealthBackendError(
            "SQLite database path is missing from validated settings"
        )
    return connect_readonly_database(settings.sqlite_database_path)


def read_source_health() -> SourceHealthListResponse:
    """Read health for every known source from the configured database.

    The read is strictly read-only: the database is opened through a ``mode=ro``
    SQLite URI and then also set ``query_only``, so a missing database cannot be
    created and an existing one cannot be written. No migration is applied, no
    row is created for a source that has never run, and nothing external is
    contacted — a non-SQLite backend is refused locally rather than dialled.
    """
    connection = None
    try:
        settings = load_settings()
        sources = _configured_sources()
        connection = _readonly_connection(settings)
        # The connection already cannot write; this is the second lock on it.
        connection.execute("PRAGMA query_only = ON")
        entries = read_source_health_model(connection, configured_sources=sources)
        items = [_to_response(entry) for entry in entries]
        response = SourceHealthListResponse(items=items, returned=len(items))
        LOGGER.info(
            "Source health API request succeeded.",
            extra={
                "event": "source_health_api_request_succeeded",
                "sources_returned": response.returned,
                "anomalies_returned": sum(
                    1 for item in items if item.anomaly_code is not None
                ),
            },
        )
        return response
    except Exception as error:
        LOGGER.error(
            "Source health API request failed.",
            extra={
                "event": "source_health_api_request_failed",
                "error_type": type(error).__name__,
            },
        )
        raise SourceHealthReadError(PUBLIC_SOURCE_HEALTH_ERROR) from None
    finally:
        if connection is not None:
            connection.close()
