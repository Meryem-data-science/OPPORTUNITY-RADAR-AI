"""Persistent history of every attempted source run.

A run is written before its collection begins, as ``RUNNING``, and the same row
is later closed as ``SUCCESS`` or ``FAILED``. One attempt is always exactly one
row: an attempt that started leaves proof it started, even if the process dies
before it can end. Deciding that a ``RUNNING`` row is abnormally old is a later
concern and is deliberately not implemented here.
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
    RUNNING,
    SUCCESS,
    TERMINAL_STATUSES,
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
    """Raised when a source run cannot be started, finalized, or read back."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _register_source_statement(source: SourceConfig) -> tuple[str, tuple[object, ...]]:
    """Ensure the run's source row exists, without claiming anything about it.

    A never-persisted source still needs its catalogue row for the foreign key,
    so it is inserted from validated configuration. An existing source is left
    untouched, and in particular ``last_run_at`` is not stamped here: a run that
    has only started has not finished.
    """
    return (
        """
        INSERT INTO sources (
            id, type, enabled, category, country, frequency_minutes, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (
            source.id,
            source.type,
            int(source.enabled),
            source.category,
            source.country,
            source.frequency_minutes,
            source.status,
        ),
    )


def start_source_run(
    connection: DatabaseConnection,
    source: SourceConfig,
    *,
    clock: Callable[[], str] = _utc_now,
) -> SourceRunAttempt:
    """Persist a ``RUNNING`` row before collection begins and return its handle.

    The row and its source registration share one transaction, which commits
    before the caller collects anything, so the started attempt is already
    durable and visible while the collector runs.
    """
    started_at = clock()
    register_statement, register_parameters = _register_source_statement(source)
    connection.execute("BEGIN")
    try:
        connection.execute(register_statement, register_parameters)
        row = connection.execute(
            """
            INSERT INTO source_runs (source_id, started_at, status)
            VALUES (?, ?, ?)
            RETURNING id
            """,
            (source.id, started_at, RUNNING),
        ).fetchone()
        if row is None:
            raise SourceRunPersistenceError("source run insert returned no id")
        connection.execute("COMMIT")
    except Exception as failure:
        try:
            connection.execute("ROLLBACK")
        except Exception as rollback_failure:
            raise SourceRunPersistenceError(
                "source run failed to start and rollback was unsuccessful"
            ) from rollback_failure
        if isinstance(failure, SourceRunPersistenceError):
            raise
        raise SourceRunPersistenceError("source run could not be started") from failure
    return SourceRunAttempt(id=int(row[0]), source_id=source.id, started_at=started_at)


def finalize_source_run(
    connection: DatabaseConnection,
    attempt: SourceRunAttempt,
    *,
    status: str,
    metrics: SourceRunMetrics = NO_METRICS,
    error: BaseException | None = None,
    clock: Callable[[], str] = _utc_now,
) -> SourceRun:
    """Close the running row this handle points at and stamp ``last_run_at``.

    Only ``RUNNING`` may be closed, and only once: a row that already reached a
    terminal state is never rewritten. Closing the run and stamping the source
    share one transaction, so the stamp can never disagree with the run that
    produced it.
    """
    if status not in TERMINAL_STATUSES:
        raise SourceRunPersistenceError(f"unsupported terminal status: {status!r}")
    if status == FAILED and error is None:
        raise SourceRunPersistenceError("a failed source run requires its error")
    if status == SUCCESS and error is not None:
        raise SourceRunPersistenceError("a successful source run cannot carry an error")

    error_type = type(error).__name__ if status == FAILED else None
    error_message = redact_error_message(str(error)) if status == FAILED else None

    connection.execute("BEGIN")
    try:
        current = connection.execute(
            "SELECT status, started_at FROM source_runs WHERE id = ?", (attempt.id,)
        ).fetchone()
        if current is None:
            raise SourceRunPersistenceError(f"source run {attempt.id} does not exist")
        if current[0] != RUNNING:
            raise SourceRunPersistenceError(
                f"source run {attempt.id} is already {current[0]} and cannot be finalized again"
            )
        finished_at = max(clock(), str(current[1]))
        row = connection.execute(
            f"""
            UPDATE source_runs SET
                finished_at = ?,
                status = ?,
                pages_checked = ?,
                items_found = ?,
                new_items = ?,
                relevant_items = ?,
                http_status = ?,
                error_type = ?,
                error_message = ?,
                parser_version = ?
            WHERE id = ? AND status = ?
            RETURNING {_RUN_COLUMNS}
            """,
            (
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
                attempt.id,
                RUNNING,
            ),
        ).fetchone()
        if row is None:
            raise SourceRunPersistenceError(f"source run {attempt.id} was not finalized")
        connection.execute(
            "UPDATE sources SET last_run_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (finished_at, attempt.source_id),
        )
        connection.execute("COMMIT")
    except Exception as failure:
        try:
            connection.execute("ROLLBACK")
        except Exception as rollback_failure:
            raise SourceRunPersistenceError(
                "source run failed to finalize and rollback was unsuccessful"
            ) from rollback_failure
        if isinstance(failure, SourceRunPersistenceError):
            raise
        raise SourceRunPersistenceError("source run could not be finalized") from failure
    return _to_source_run(row)


def finalize_successful_source_run(
    connection: DatabaseConnection,
    attempt: SourceRunAttempt,
    *,
    metrics: SourceRunMetrics = NO_METRICS,
    clock: Callable[[], str] = _utc_now,
) -> SourceRun:
    """Close an attempt whose collection and persistence both completed."""
    return finalize_source_run(
        connection, attempt, status=SUCCESS, metrics=metrics, clock=clock
    )


def finalize_failed_source_run(
    connection: DatabaseConnection,
    attempt: SourceRunAttempt,
    *,
    error: BaseException,
    metrics: SourceRunMetrics = NO_METRICS,
    clock: Callable[[], str] = _utc_now,
) -> SourceRun:
    """Close an attempt that raised, keeping whatever it did manage to learn."""
    return finalize_source_run(
        connection, attempt, status=FAILED, metrics=metrics, error=error, clock=clock
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


def _configured_connection(settings: Settings) -> DatabaseConnection:
    if settings.database_backend is DatabaseBackend.TURSO:
        raise SourceRunPersistenceError(
            "Remote Turso source run writes are disabled in this phase"
        )
    return connect_configured_database(settings)


def start_configured_source_run(
    settings: Settings, source: SourceConfig
) -> SourceRunAttempt:
    """Start one run in the operational SQLite database; reject remote writes."""
    connection = _configured_connection(settings)
    try:
        return start_source_run(connection, source)
    finally:
        connection.close()


def finalize_configured_source_run(
    settings: Settings,
    attempt: SourceRunAttempt,
    *,
    status: str,
    metrics: SourceRunMetrics = NO_METRICS,
    error: BaseException | None = None,
) -> SourceRun:
    """Close one run in the operational SQLite database; reject remote writes."""
    connection = _configured_connection(settings)
    try:
        return finalize_source_run(
            connection, attempt, status=status, metrics=metrics, error=error
        )
    finally:
        connection.close()


def _to_source_run(row: tuple[object, ...]) -> SourceRun:
    return SourceRun(
        id=int(row[0]),
        source_id=str(row[1]),
        started_at=str(row[2]),
        finished_at=_optional_text(row[3]),
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
