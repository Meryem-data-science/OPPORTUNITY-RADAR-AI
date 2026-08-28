"""Persistent history of every attempted source run.

Each attempt writes exactly one terminal row when it completes. ``started_at``
is captured before collection begins, so the stored window is truthful, but no
row exists while a run is in flight: a crashed process would otherwise leave a
permanently unfinished row that nothing in this phase could reconcile, and the
status taxonomy stays the closed pair the history actually needs.
"""

from datetime import datetime, timezone
from typing import Callable

from services.collector.config import DatabaseBackend, Settings
from services.collector.database.connection import (
    DatabaseConnection,
    connect_configured_database,
)
from services.collector.models.source_run import (
    FAILED,
    NO_METRICS,
    SOURCE_RUN_STATUSES,
    SUCCESS,
    SourceRun,
    SourceRunAttempt,
    SourceRunMetrics,
    redact_error_message,
)
from services.collector.sources import SourceConfig

DEFAULT_HISTORY_LIMIT = 20

_RUN_COLUMNS = (
    "id, source_id, started_at, finished_at, status, pages_checked, items_found, "
    "new_items, relevant_items, http_status, error_type, error_message, parser_version"
)


class SourceRunPersistenceError(RuntimeError):
    """Raised when a source run cannot be recorded or read back."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def start_source_run(
    source_id: str, *, clock: Callable[[], str] = _utc_now
) -> SourceRunAttempt:
    """Open an attempt by capturing the instant its collection begins."""
    if not source_id or not source_id.strip():
        raise SourceRunPersistenceError("source run requires a source id")
    return SourceRunAttempt(source_id=source_id, started_at=clock())


def _register_source_statement(
    source: SourceConfig, finished_at: str
) -> tuple[str, tuple[object, ...]]:
    """Ensure the run's source row exists, touching only its last-run stamp.

    A never-persisted source still needs its catalogue row for the foreign key,
    so it is inserted from validated configuration. An existing source keeps
    every other column it already has.
    """
    return (
        """
        INSERT INTO sources (
            id, type, enabled, category, country, frequency_minutes, status, last_run_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            last_run_at = excluded.last_run_at,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            source.id,
            source.type,
            int(source.enabled),
            source.category,
            source.country,
            source.frequency_minutes,
            source.status,
            finished_at,
        ),
    )


def record_source_run(
    connection: DatabaseConnection,
    source: SourceConfig,
    attempt: SourceRunAttempt,
    *,
    status: str,
    metrics: SourceRunMetrics = NO_METRICS,
    error: BaseException | None = None,
    clock: Callable[[], str] = _utc_now,
) -> SourceRun:
    """Close one attempt: persist its terminal row and stamp ``last_run_at``.

    Both writes share a single transaction so a source's last-run stamp can
    never disagree with the run that produced it. The transaction covers this
    attempt only, so an earlier committed source is never rolled back here.
    """
    if status not in SOURCE_RUN_STATUSES:
        raise SourceRunPersistenceError(f"unsupported source run status: {status!r}")
    if attempt.source_id != source.id:
        raise SourceRunPersistenceError("attempt source id does not match source")
    if status == FAILED and error is None:
        raise SourceRunPersistenceError("a failed source run requires its error")

    error_type = type(error).__name__ if error is not None and status == FAILED else None
    error_message = (
        redact_error_message(str(error)) if error is not None and status == FAILED else None
    )
    finished_at = clock()
    if finished_at < attempt.started_at:
        finished_at = attempt.started_at

    register_statement, register_parameters = _register_source_statement(source, finished_at)
    connection.execute("BEGIN")
    try:
        connection.execute(register_statement, register_parameters)
        row = connection.execute(
            f"""
            INSERT INTO source_runs (
                source_id, started_at, finished_at, status, pages_checked, items_found,
                new_items, relevant_items, http_status, error_type, error_message,
                parser_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING {_RUN_COLUMNS}
            """,
            (
                source.id,
                attempt.started_at,
                finished_at,
                status,
                metrics.pages_checked,
                metrics.items_found,
                metrics.new_items,
                metrics.relevant_items,
                metrics.http_status,
                error_type,
                error_message,
                metrics.parser_version,
            ),
        ).fetchone()
        if row is None:
            raise SourceRunPersistenceError("source run insert returned no row")
        connection.execute("COMMIT")
    except Exception as failure:
        try:
            connection.execute("ROLLBACK")
        except Exception as rollback_failure:
            raise SourceRunPersistenceError(
                "source run failed to record and rollback was unsuccessful"
            ) from rollback_failure
        if isinstance(failure, SourceRunPersistenceError):
            raise
        raise SourceRunPersistenceError("source run could not be recorded") from failure
    return _to_source_run(row)


def record_successful_source_run(
    connection: DatabaseConnection,
    source: SourceConfig,
    attempt: SourceRunAttempt,
    *,
    metrics: SourceRunMetrics = NO_METRICS,
    clock: Callable[[], str] = _utc_now,
) -> SourceRun:
    """Finalize an attempt whose collection and persistence both completed."""
    return record_source_run(
        connection, source, attempt, status=SUCCESS, metrics=metrics, clock=clock
    )


def record_failed_source_run(
    connection: DatabaseConnection,
    source: SourceConfig,
    attempt: SourceRunAttempt,
    *,
    error: BaseException,
    metrics: SourceRunMetrics = NO_METRICS,
    clock: Callable[[], str] = _utc_now,
) -> SourceRun:
    """Finalize an attempt that raised, keeping whatever it did manage to learn."""
    return record_source_run(
        connection, source, attempt, status=FAILED, metrics=metrics, error=error, clock=clock
    )


def recent_source_runs(
    connection: DatabaseConnection,
    source_id: str,
    *,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> list[SourceRun]:
    """Read one source's most recent runs, newest first."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise SourceRunPersistenceError("limit must be a positive integer")
    rows = connection.execute(
        f"""
        SELECT {_RUN_COLUMNS}
        FROM source_runs
        WHERE source_id = ?
        ORDER BY started_at DESC, id DESC
        LIMIT ?
        """,
        (source_id, limit),
    ).fetchall()
    return [_to_source_run(row) for row in rows]


def record_configured_source_run(
    settings: Settings,
    source: SourceConfig,
    attempt: SourceRunAttempt,
    *,
    status: str,
    metrics: SourceRunMetrics = NO_METRICS,
    error: BaseException | None = None,
) -> SourceRun:
    """Record one run in the operational SQLite database; reject remote writes."""
    if settings.database_backend is DatabaseBackend.TURSO:
        raise SourceRunPersistenceError(
            "Remote Turso source run writes are disabled in this phase"
        )
    connection = connect_configured_database(settings)
    try:
        return record_source_run(
            connection, source, attempt, status=status, metrics=metrics, error=error
        )
    finally:
        connection.close()


def _to_source_run(row: tuple[object, ...]) -> SourceRun:
    return SourceRun(
        id=int(row[0]),
        source_id=str(row[1]),
        started_at=str(row[2]),
        finished_at=str(row[3]),
        status=str(row[4]),
        pages_checked=_optional_int(row[5]),
        items_found=_optional_int(row[6]),
        new_items=_optional_int(row[7]),
        relevant_items=_optional_int(row[8]),
        http_status=_optional_int(row[9]),
        error_type=_optional_text(row[10]),
        error_message=_optional_text(row[11]),
        parser_version=_optional_text(row[12]),
    )


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)  # type: ignore[arg-type]


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)
