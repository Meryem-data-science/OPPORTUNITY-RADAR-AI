"""Read-only source health service and response models.

The HTTP layer only reshapes what the database read model already decided. The
zero-result streak, the anomaly code and the anomaly message are computed once,
in the backend, so no client has to re-derive them.
"""

from pydantic import BaseModel

from services.collector.config import DatabaseBackend, load_settings
from services.collector.database.connection import connect_configured_database
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


def read_source_health() -> SourceHealthListResponse:
    """Read health for every known source from the configured database.

    The read is strictly read-only: SQLite is opened in ``query_only`` mode, no
    migration is applied, no row is created for a source that has never run, and
    nothing external is contacted.
    """
    connection = None
    try:
        settings = load_settings()
        sources = _configured_sources()
        connection = connect_configured_database(settings)
        if settings.database_backend is DatabaseBackend.SQLITE:
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
