"""Unit tests for the source-run persistence boundary."""

import sqlite3

import pytest

from services.collector.config import ApplicationEnvironment, DatabaseBackend, Settings
from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.database.source_runs import (
    SourceRunPersistenceError,
    finalize_configured_source_run,
    finalize_failed_source_run,
    finalize_source_run,
    finalize_successful_source_run,
    recent_source_runs,
    start_configured_source_run,
    start_source_run,
)
from services.collector.models.source_run import (
    FAILED,
    RUNNING,
    SUCCESS,
    SourceRunAttempt,
    SourceRunMetrics,
)
from services.collector.sources import SourceConfig


def source(source_id="source_a"):
    return SourceConfig(source_id, "greenhouse", True, "Org", "board", status="active")


@pytest.fixture()
def connection(tmp_path):
    database = connect_database(tmp_path / "runs.db")
    try:
        apply_migrations(database)
        yield database
    finally:
        database.close()


def last_run_at(database, source_id):
    return database.execute(
        "SELECT last_run_at FROM sources WHERE id = ?", (source_id,)
    ).fetchone()[0]


def test_starting_a_run_persists_it_as_running_without_claiming_it_finished(connection):
    attempt = start_source_run(
        connection, source(), clock=lambda: "2026-01-01T00:00:00.000000+00:00"
    )
    assert isinstance(attempt, SourceRunAttempt)
    assert attempt.id > 0
    assert attempt.source_id == "source_a"
    assert attempt.started_at == "2026-01-01T00:00:00.000000+00:00"

    runs = recent_source_runs(connection, "source_a")
    assert len(runs) == 1
    run = runs[0]
    assert run.id == attempt.id
    assert run.status == RUNNING
    assert run.started_at == attempt.started_at
    assert run.finished_at is None
    assert run.error_type is None
    assert run.error_message is None
    # A run that has only started has not finished, so nothing is stamped yet.
    assert last_run_at(connection, "source_a") is None


def test_success_finalizes_the_same_row_and_stamps_last_run_at(connection):
    attempt = start_source_run(connection, source())
    run = finalize_successful_source_run(
        connection, attempt, metrics=SourceRunMetrics(items_found=4, new_items=3)
    )

    assert run.id == attempt.id
    assert run.status == SUCCESS
    assert run.started_at == attempt.started_at
    assert run.finished_at is not None
    assert run.finished_at >= run.started_at
    assert (run.items_found, run.new_items) == (4, 3)
    assert run.error_type is None
    assert run.error_message is None
    assert run.pages_checked is None
    assert run.relevant_items is None
    assert run.http_status is None
    assert run.parser_version is None

    assert len(recent_source_runs(connection, "source_a")) == 1
    assert last_run_at(connection, "source_a") == run.finished_at


def test_failure_finalizes_the_same_row_with_a_usable_and_redacted_error(connection):
    attempt = start_source_run(connection, source())
    error = RuntimeError("board unreachable TURSO_AUTH_TOKEN=supersecret")
    run = finalize_failed_source_run(connection, attempt, error=error)

    assert run.id == attempt.id
    assert run.status == FAILED
    assert run.error_type == "RuntimeError"
    assert "board unreachable" in run.error_message
    assert "supersecret" not in run.error_message
    assert run.finished_at is not None
    assert run.finished_at >= run.started_at
    assert run.items_found is None
    assert run.new_items is None
    assert len(recent_source_runs(connection, "source_a")) == 1
    assert last_run_at(connection, "source_a") == run.finished_at


def test_a_failure_whose_message_redacts_to_nothing_still_records_its_type(connection):
    attempt = start_source_run(connection, source())
    run = finalize_failed_source_run(connection, attempt, error=RuntimeError("   "))
    assert run.status == FAILED
    assert run.error_type == "RuntimeError"
    assert run.error_message is None


def test_a_failed_run_keeps_the_counts_it_did_manage_to_observe(connection):
    attempt = start_source_run(connection, source())
    run = finalize_failed_source_run(
        connection, attempt,
        error=RuntimeError("database unavailable"),
        metrics=SourceRunMetrics(items_found=7, pages_checked=2, http_status=500),
    )
    assert (run.items_found, run.pages_checked, run.http_status) == (7, 2, 500)
    assert run.new_items is None


def test_a_terminal_run_can_never_be_finalized_again(connection):
    succeeded = start_source_run(connection, source())
    finalize_successful_source_run(connection, succeeded)
    for status, error in ((SUCCESS, None), (FAILED, RuntimeError("late"))):
        with pytest.raises(SourceRunPersistenceError, match="already SUCCESS"):
            finalize_source_run(connection, succeeded, status=status, error=error)

    failed = start_source_run(connection, source())
    finalize_failed_source_run(connection, failed, error=ValueError("bad payload"))
    for status, error in ((SUCCESS, None), (FAILED, RuntimeError("late"))):
        with pytest.raises(SourceRunPersistenceError, match="already FAILED"):
            finalize_source_run(connection, failed, status=status, error=error)

    stored = connection.execute(
        "SELECT id, status FROM source_runs ORDER BY id"
    ).fetchall()
    assert stored == [(succeeded.id, SUCCESS), (failed.id, FAILED)]


def test_a_handle_naming_another_source_cannot_finalize_or_stamp_anything(connection):
    running = start_source_run(connection, source("source_a"))
    finalize_successful_source_run(connection, start_source_run(connection, source("source_b")))
    source_b_stamp = last_run_at(connection, "source_b")
    assert source_b_stamp is not None

    # A handle that points at source_a's run while claiming to be source_b must
    # not close that run, and must not stamp either source.
    forged = SourceRunAttempt(running.id, "source_b", running.started_at)
    with pytest.raises(SourceRunPersistenceError, match="belongs to source 'source_a'"):
        finalize_source_run(connection, forged, status=SUCCESS)

    untouched = recent_source_runs(connection, "source_a")
    assert len(untouched) == 1
    assert untouched[0].id == running.id
    assert untouched[0].status == RUNNING
    assert untouched[0].finished_at is None
    assert last_run_at(connection, "source_a") is None
    assert last_run_at(connection, "source_b") == source_b_stamp
    assert connection.execute("SELECT COUNT(*) FROM source_runs").fetchone() == (2,)

    # The rejected attempt rolled back cleanly, so the real handle still works.
    finalized = finalize_successful_source_run(connection, running)
    assert finalized.id == running.id
    assert finalized.status == SUCCESS
    assert last_run_at(connection, "source_a") == finalized.finished_at
    assert last_run_at(connection, "source_b") == source_b_stamp


def test_a_finalized_run_always_stamps_its_own_source(connection):
    attempt = start_source_run(connection, source())
    run = finalize_successful_source_run(connection, attempt)
    stamps = connection.execute(
        "SELECT id, last_run_at FROM sources ORDER BY id"
    ).fetchall()
    assert stamps == [("source_a", run.finished_at)]


def test_starting_registers_a_missing_source_and_leaves_an_existing_one_alone(connection):
    connection.execute(
        """INSERT INTO sources (id, type, enabled, category, country, status, last_run_at)
           VALUES ('existing', 'greenhouse', 1, 'tech', 'MA', 'active', '2026-01-01T00:00:00+00:00')"""
    )
    connection.commit()
    existing = SourceConfig("existing", "greenhouse", True, "Org", "board", status="paused")

    start_source_run(connection, existing)
    stored = connection.execute(
        "SELECT category, country, status, last_run_at FROM sources WHERE id = 'existing'"
    ).fetchone()
    assert stored == ("tech", "MA", "active", "2026-01-01T00:00:00+00:00")

    start_source_run(connection, source("brand_new"))
    created = connection.execute(
        "SELECT type, enabled, status, last_run_at FROM sources WHERE id = 'brand_new'"
    ).fetchone()
    assert created == ("greenhouse", 1, "active", None)


def test_history_keeps_every_execution_and_never_deduplicates(connection):
    for _ in range(3):
        finalize_successful_source_run(
            connection, start_source_run(connection, source()),
            metrics=SourceRunMetrics(items_found=1, new_items=0),
        )
    finalize_failed_source_run(
        connection, start_source_run(connection, source()), error=ValueError("bad payload")
    )
    start_source_run(connection, source())

    history = recent_source_runs(connection, "source_a")
    assert len(history) == 5
    assert len({run.id for run in history}) == 5
    assert history[0].status == RUNNING
    assert history[1].status == FAILED
    assert [run.status for run in history[2:]] == [SUCCESS, SUCCESS, SUCCESS]
    assert len(recent_source_runs(connection, "source_a", limit=2)) == 2
    assert recent_source_runs(connection, "unknown_source") == []

    for invalid in (0, -1, True, "2"):
        with pytest.raises(SourceRunPersistenceError, match="positive integer"):
            recent_source_runs(connection, "source_a", limit=invalid)


def test_finalizing_rejects_inconsistent_arguments(connection):
    attempt = start_source_run(connection, source())
    with pytest.raises(SourceRunPersistenceError, match="unsupported terminal status"):
        finalize_source_run(connection, attempt, status=RUNNING)
    with pytest.raises(SourceRunPersistenceError, match="requires its error"):
        finalize_source_run(connection, attempt, status=FAILED)
    with pytest.raises(SourceRunPersistenceError, match="cannot carry an error"):
        finalize_source_run(connection, attempt, status=SUCCESS, error=RuntimeError("x"))
    with pytest.raises(SourceRunPersistenceError, match="does not exist"):
        finalize_source_run(
            connection, SourceRunAttempt(9999, "source_a", attempt.started_at), status=SUCCESS
        )
    assert [run.status for run in recent_source_runs(connection, "source_a")] == [RUNNING]


def test_a_failed_start_is_rolled_back_and_leaves_no_partial_state(tmp_path):
    database = connect_database(tmp_path / "no-source-runs.db")
    try:
        apply_migrations(database)
        database.execute("DROP TABLE source_runs")
        database.commit()
        with pytest.raises(SourceRunPersistenceError, match="could not be started"):
            start_source_run(database, source())
        assert database.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    finally:
        database.close()


def test_schema_rejects_a_run_whose_source_does_not_exist(connection):
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """INSERT INTO source_runs (source_id, started_at, status)
               VALUES ('missing_source', '2026-01-01', 'RUNNING')"""
        )


def test_a_source_with_run_history_cannot_be_deleted_with_its_audit(connection):
    attempt = start_source_run(connection, source())
    finalize_successful_source_run(connection, attempt)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM sources WHERE id = 'source_a'")

    assert [run.id for run in recent_source_runs(connection, "source_a")] == [attempt.id]
    assert connection.execute(
        "SELECT COUNT(*) FROM sources WHERE id = 'source_a'"
    ).fetchone()[0] == 1


def test_configured_lifecycle_uses_sqlite_and_refuses_remote_writes(tmp_path):
    path = tmp_path / "configured-runs.db"
    database = connect_database(path)
    try:
        apply_migrations(database)
    finally:
        database.close()

    settings = Settings(ApplicationEnvironment.TEST, DatabaseBackend.SQLITE, path)
    attempt = start_configured_source_run(settings, source())

    database = connect_database(path)
    try:
        assert [run.status for run in recent_source_runs(database, "source_a")] == [RUNNING]
        assert last_run_at(database, "source_a") is None
    finally:
        database.close()

    run = finalize_configured_source_run(
        settings, attempt, status=SUCCESS,
        metrics=SourceRunMetrics(items_found=2, new_items=2),
    )
    assert (run.id, run.status, run.items_found, run.new_items) == (attempt.id, SUCCESS, 2, 2)

    database = connect_database(path)
    try:
        assert len(recent_source_runs(database, "source_a")) == 1
        assert last_run_at(database, "source_a") == run.finished_at
    finally:
        database.close()

    turso = Settings(
        ApplicationEnvironment.TEST, DatabaseBackend.TURSO,
        turso_database_url="libsql://example.invalid", turso_auth_token="unused",
    )
    for call in (
        lambda: start_configured_source_run(turso, source()),
        lambda: finalize_configured_source_run(turso, attempt, status=SUCCESS),
    ):
        with pytest.raises(SourceRunPersistenceError, match="Turso source run writes are disabled"):
            call()
