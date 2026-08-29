"""Source health read on a real SQLite database written by the run persistence.

These tests never insert a run by hand: every row is produced by the same
``start``/``finalize`` calls ``RadarAgent`` uses, so the read model is checked
against what the writer actually stores rather than against a hand-made shape.
"""

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.database.source_health import (
    ZERO_RESULT_ANOMALY_CODE,
    read_source_health,
    zero_result_anomaly_message,
)
from services.collector.database.source_runs import (
    finalize_failed_source_run,
    finalize_successful_source_run,
    start_source_run,
)
from services.collector.models.source_run import FAILED, RUNNING, SUCCESS, SourceRunMetrics
from services.collector.sources import SourceConfig


def source(source_id: str, *, enabled: bool = True) -> SourceConfig:
    return SourceConfig(source_id, "greenhouse", enabled, "Org", "board", status="active")


@pytest.fixture()
def connection(tmp_path):
    handle = connect_database(tmp_path / "source-health-integration.db")
    try:
        assert apply_migrations(handle) == ["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008"]
        yield handle
    finally:
        handle.close()


def clock(day: int):
    """A deterministic clock producing one distinct instant per call."""
    instants = iter(
        [f"2026-08-{day:02d}T09:0{index}:00+00:00" for index in range(1, 10)]
    )
    return lambda: next(instants)


def run_success(connection, configured, *, day: int, items_found=None, new_items=None):
    tick = clock(day)
    attempt = start_source_run(connection, configured, clock=tick)
    return finalize_successful_source_run(
        connection,
        attempt,
        metrics=SourceRunMetrics(items_found=items_found, new_items=new_items),
        clock=tick,
    )


def run_failure(connection, configured, *, day: int, error: BaseException):
    tick = clock(day)
    attempt = start_source_run(connection, configured, clock=tick)
    return finalize_failed_source_run(connection, attempt, error=error, clock=tick)


def only(entries):
    assert len(entries) == 1
    return entries[0]


def test_three_real_zero_runs_produce_the_anomaly_on_a_real_database(connection) -> None:
    configured = source("greenhouse_zero")
    for day in (1, 2, 3):
        run_success(connection, configured, day=day, items_found=0, new_items=0)

    health = only(read_source_health(connection, configured_sources=[configured]))

    assert health.status == SUCCESS
    assert health.items_found == 0
    assert health.new_items == 0
    assert health.relevant_items is None
    assert health.zero_result_streak == 3
    assert health.anomaly_code == ZERO_RESULT_ANOMALY_CODE
    assert health.anomaly_message == zero_result_anomaly_message(3)


def test_a_fourth_real_zero_run_keeps_the_anomaly(connection) -> None:
    configured = source("greenhouse_zero")
    for day in (1, 2, 3, 4):
        run_success(connection, configured, day=day, items_found=0, new_items=0)

    health = only(read_source_health(connection, configured_sources=[configured]))

    assert health.zero_result_streak == 4
    assert health.anomaly_code == ZERO_RESULT_ANOMALY_CODE


def test_a_real_failure_exposes_its_redacted_message_and_no_anomaly(connection) -> None:
    configured = source("greenhouse_failing")
    run_success(connection, configured, day=1, items_found=0, new_items=0)
    run_failure(
        connection,
        configured,
        day=2,
        error=RuntimeError(
            "board fetch rejected: Authorization: Bearer sk-test-not-a-real-secret-value"
        ),
    )

    health = only(read_source_health(connection, configured_sources=[configured]))

    assert health.status == FAILED
    assert health.error_type == "RuntimeError"
    assert health.error_message is not None
    assert "[REDACTED]" in health.error_message
    assert "sk-test-not-a-real-secret-value" not in health.error_message
    assert health.zero_result_streak == 0
    assert health.anomaly_code is None


def test_an_unfinished_real_run_is_reported_as_running(connection) -> None:
    configured = source("greenhouse_running")
    run_success(connection, configured, day=1, items_found=0, new_items=0)
    start_source_run(connection, configured, clock=clock(2))

    health = only(read_source_health(connection, configured_sources=[configured]))

    assert health.status == RUNNING
    assert health.last_run_at.startswith("2026-08-02")
    assert health.items_found is None
    assert health.zero_result_streak == 0
    assert health.anomaly_code is None


def test_a_configured_source_that_never_ran_appears_beside_one_that_did(connection) -> None:
    executed = source("greenhouse_executed")
    never_run = source("greenhouse_never_run", enabled=False)
    run_success(connection, executed, day=1, items_found=4, new_items=2)

    entries = read_source_health(
        connection, configured_sources=[executed, never_run]
    )

    assert [entry.source_id for entry in entries] == [
        "greenhouse_executed",
        "greenhouse_never_run",
    ]
    assert entries[0].status == SUCCESS
    assert entries[0].items_found == 4
    assert entries[1].enabled is False
    assert entries[1].status is None
    assert entries[1].last_run_at is None
    persisted = connection.execute("SELECT id FROM sources ORDER BY id").fetchall()
    assert [row[0] for row in persisted] == ["greenhouse_executed"]


def test_reading_health_writes_nothing_to_the_database(connection, tmp_path) -> None:
    configured = source("greenhouse_stable")
    for day in (1, 2, 3):
        run_success(connection, configured, day=day, items_found=0, new_items=0)
    before = connection.execute(
        "SELECT id, source_id, started_at, finished_at, status, items_found FROM source_runs"
        " ORDER BY id"
    ).fetchall()
    sources_before = connection.execute(
        "SELECT id, enabled, last_run_at FROM sources ORDER BY id"
    ).fetchall()

    read_source_health(connection, configured_sources=[configured])

    assert (
        connection.execute(
            "SELECT id, source_id, started_at, finished_at, status, items_found"
            " FROM source_runs ORDER BY id"
        ).fetchall()
        == before
    )
    assert (
        connection.execute(
            "SELECT id, enabled, last_run_at FROM sources ORDER BY id"
        ).fetchall()
        == sources_before
    )


def test_two_real_sources_do_not_share_their_run_history(connection) -> None:
    anomalous = source("greenhouse_a")
    healthy = source("greenhouse_b")
    for day in (1, 3, 5):
        run_success(connection, anomalous, day=day, items_found=0, new_items=0)
    for day in (2, 4, 6):
        run_success(connection, healthy, day=day, items_found=6, new_items=1)

    entries = {
        entry.source_id: entry
        for entry in read_source_health(
            connection, configured_sources=[anomalous, healthy]
        )
    }

    assert entries["greenhouse_a"].zero_result_streak == 3
    assert entries["greenhouse_a"].anomaly_code == ZERO_RESULT_ANOMALY_CODE
    assert entries["greenhouse_b"].zero_result_streak == 0
    assert entries["greenhouse_b"].anomaly_code is None
    assert entries["greenhouse_b"].items_found == 6
