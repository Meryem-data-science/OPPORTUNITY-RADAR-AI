"""Unit tests for the source-run persistence boundary."""

import sqlite3

import pytest

from services.collector.config import ApplicationEnvironment, DatabaseBackend, Settings
from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.database.source_runs import (
    SourceRunPersistenceError,
    recent_source_runs,
    record_configured_source_run,
    record_failed_source_run,
    record_source_run,
    record_successful_source_run,
    start_source_run,
)
from services.collector.models.source_run import (
    FAILED,
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


def test_starting_a_run_captures_the_instant_collection_begins():
    attempt = start_source_run("source_a", clock=lambda: "2026-01-01T00:00:00.000000+00:00")
    assert attempt == SourceRunAttempt("source_a", "2026-01-01T00:00:00.000000+00:00")
    for invalid in ("", "   "):
        with pytest.raises(SourceRunPersistenceError, match="source id"):
            start_source_run(invalid)


def test_successful_run_persists_known_metrics_and_stamps_last_run_at(connection):
    attempt = start_source_run("source_a")
    run = record_successful_source_run(
        connection, source(), attempt, metrics=SourceRunMetrics(items_found=4, new_items=3)
    )

    assert run.status == SUCCESS
    assert run.source_id == "source_a"
    assert run.started_at == attempt.started_at
    assert run.finished_at >= run.started_at
    assert (run.items_found, run.new_items) == (4, 3)
    assert run.error_type is None
    assert run.error_message is None
    assert run.pages_checked is None
    assert run.relevant_items is None
    assert run.http_status is None
    assert run.parser_version is None
    assert last_run_at(connection, "source_a") == run.finished_at


def test_failed_run_is_persisted_with_a_usable_and_redacted_error(connection):
    attempt = start_source_run("source_a")
    error = RuntimeError("board unreachable TURSO_AUTH_TOKEN=supersecret")
    run = record_failed_source_run(connection, source(), attempt, error=error)

    assert run.status == FAILED
    assert run.error_type == "RuntimeError"
    assert "board unreachable" in run.error_message
    assert "supersecret" not in run.error_message
    assert run.finished_at >= run.started_at
    assert run.items_found is None
    assert run.new_items is None
    assert last_run_at(connection, "source_a") == run.finished_at


def test_a_failed_run_keeps_the_counts_it_did_manage_to_observe(connection):
    attempt = start_source_run("source_a")
    run = record_failed_source_run(
        connection, source(), attempt,
        error=RuntimeError("database unavailable"),
        metrics=SourceRunMetrics(items_found=7, pages_checked=2, http_status=500),
    )
    assert (run.items_found, run.pages_checked, run.http_status) == (7, 2, 500)
    assert run.new_items is None


def test_recording_registers_a_missing_source_but_only_touches_last_run_at(connection):
    connection.execute(
        """INSERT INTO sources (id, type, enabled, category, country, status)
           VALUES ('existing', 'greenhouse', 1, 'tech', 'MA', 'active')"""
    )
    connection.commit()
    existing = SourceConfig("existing", "greenhouse", True, "Org", "board", status="active")

    record_successful_source_run(connection, existing, start_source_run("existing"))
    stored = connection.execute(
        "SELECT category, country, last_run_at FROM sources WHERE id = 'existing'"
    ).fetchone()
    assert stored[:2] == ("tech", "MA")
    assert stored[2] is not None

    record_successful_source_run(connection, source("brand_new"), start_source_run("brand_new"))
    created = connection.execute(
        "SELECT type, enabled, status, last_run_at FROM sources WHERE id = 'brand_new'"
    ).fetchone()
    assert created[:3] == ("greenhouse", 1, "active")
    assert created[3] is not None


def test_history_keeps_every_execution_and_never_deduplicates(connection):
    for _ in range(3):
        record_successful_source_run(
            connection, source(), start_source_run("source_a"),
            metrics=SourceRunMetrics(items_found=1, new_items=0),
        )
    record_failed_source_run(
        connection, source(), start_source_run("source_a"), error=ValueError("bad payload")
    )

    history = recent_source_runs(connection, "source_a")
    assert len(history) == 4
    assert len({run.id for run in history}) == 4
    assert history[0].status == FAILED
    assert [run.status for run in history[1:]] == [SUCCESS, SUCCESS, SUCCESS]
    assert len(recent_source_runs(connection, "source_a", limit=2)) == 2
    assert recent_source_runs(connection, "unknown_source") == []

    for invalid in (0, -1, True, "2"):
        with pytest.raises(SourceRunPersistenceError, match="positive integer"):
            recent_source_runs(connection, "source_a", limit=invalid)


def test_recording_rejects_inconsistent_arguments(connection):
    attempt = start_source_run("source_a")
    with pytest.raises(SourceRunPersistenceError, match="unsupported source run status"):
        record_source_run(connection, source(), attempt, status="RUNNING")
    with pytest.raises(SourceRunPersistenceError, match="does not match source"):
        record_source_run(connection, source("other"), attempt, status=SUCCESS)
    with pytest.raises(SourceRunPersistenceError, match="requires its error"):
        record_source_run(connection, source(), attempt, status=FAILED)
    assert recent_source_runs(connection, "source_a") == []


def test_a_failed_recording_is_rolled_back_and_leaves_no_partial_state(tmp_path):
    database = connect_database(tmp_path / "no-source-runs.db")
    try:
        apply_migrations(database)
        database.execute("DROP TABLE source_runs")
        database.commit()
        with pytest.raises(SourceRunPersistenceError, match="could not be recorded"):
            record_successful_source_run(database, source(), start_source_run("source_a"))
        assert database.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    finally:
        database.close()


def test_schema_rejects_a_run_whose_source_does_not_exist(connection):
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """INSERT INTO source_runs (source_id, started_at, finished_at, status)
               VALUES ('missing_source', '2026-01-01', '2026-01-01', 'SUCCESS')"""
        )


def test_configured_recording_uses_sqlite_and_refuses_remote_writes(tmp_path):
    path = tmp_path / "configured-runs.db"
    database = connect_database(path)
    try:
        apply_migrations(database)
    finally:
        database.close()

    settings = Settings(ApplicationEnvironment.TEST, DatabaseBackend.SQLITE, path)
    run = record_configured_source_run(
        settings, source(), start_source_run("source_a"),
        status=SUCCESS, metrics=SourceRunMetrics(items_found=2, new_items=2),
    )
    assert (run.status, run.items_found, run.new_items) == (SUCCESS, 2, 2)

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
    with pytest.raises(SourceRunPersistenceError, match="Turso source run writes are disabled"):
        record_configured_source_run(
            turso, source(), start_source_run("source_a"), status=SUCCESS
        )
