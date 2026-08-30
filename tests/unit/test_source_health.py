"""Unit coverage for the deterministic, read-only source health read model."""

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.database.source_health import (
    ZERO_RESULT_ANOMALY_CODE,
    ZERO_RESULT_STREAK_THRESHOLD,
    read_source_health,
    zero_result_anomaly_message,
)
from services.collector.models.source_run import FAILED, RUNNING, SUCCESS
from services.collector.sources import SourceConfig


def source(source_id: str, *, enabled: bool = True) -> SourceConfig:
    return SourceConfig(source_id, "greenhouse", enabled, "Org", "board", status="active")


@pytest.fixture()
def connection(tmp_path):
    """A disposable, fully migrated SQLite database."""
    handle = connect_database(tmp_path / "source-health.db")
    try:
        assert apply_migrations(handle) == [
            "0001",
            "0002",
            "0003",
            "0004",
            "0005",
            "0006",
            "0007",
            "0008",
            "0009",
            "0010",
            "0011",
            "0012",
            "0013",
        ]
        yield handle
    finally:
        handle.close()


def register(connection, source_id: str, *, enabled: bool = True) -> None:
    connection.execute(
        "INSERT INTO sources (id, type, enabled, status) VALUES (?, ?, ?, ?)",
        (source_id, "greenhouse", int(enabled), "active"),
    )
    connection.commit()


def insert_run(
    connection,
    source_id: str,
    *,
    day: int,
    status: str,
    items_found: int | None = None,
    new_items: int | None = None,
    relevant_items: int | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> None:
    """Insert one run exactly as the persistence layer would have left it."""
    started_at = f"2026-08-{day:02d}T09:00:00+00:00"
    finished_at = None if status == RUNNING else f"2026-08-{day:02d}T09:05:00+00:00"
    connection.execute(
        """
        INSERT INTO source_runs (
            source_id, started_at, finished_at, status, items_found, new_items,
            relevant_items, error_type, error_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            started_at,
            finished_at,
            status,
            items_found,
            new_items,
            relevant_items,
            error_type,
            error_message,
        ),
    )
    connection.commit()


def zero_runs(connection, source_id: str, count: int) -> None:
    for index in range(count):
        insert_run(
            connection,
            source_id,
            day=index + 1,
            status=SUCCESS,
            items_found=0,
            new_items=0,
        )


def only(entries):
    assert len(entries) == 1
    return entries[0]


def test_threshold_is_three_consecutive_zero_result_runs() -> None:
    assert ZERO_RESULT_STREAK_THRESHOLD == 3


# A. a configured source that has never run


def test_configured_source_without_any_run_is_reported_as_never_run(connection) -> None:
    health = only(read_source_health(connection, configured_sources=[source("never_run")]))

    assert health.source_id == "never_run"
    assert health.enabled is True
    assert health.last_run_at is None
    assert health.status is None
    assert health.items_found is None
    assert health.new_items is None
    assert health.relevant_items is None
    assert health.error_type is None
    assert health.error_message is None
    assert health.zero_result_streak == 0
    assert health.anomaly_code is None
    assert health.anomaly_message is None
    assert health.has_run is False


def test_reading_a_never_run_source_creates_no_row(connection) -> None:
    read_source_health(connection, configured_sources=[source("never_run")])

    assert connection.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0


def test_a_persisted_last_run_at_without_any_run_is_not_substituted(connection) -> None:
    register(connection, "stamped")
    connection.execute(
        "UPDATE sources SET last_run_at = ? WHERE id = ?",
        ("2026-08-01T09:05:00+00:00", "stamped"),
    )
    connection.commit()

    health = only(read_source_health(connection))

    assert health.last_run_at is None
    assert health.status is None


# B, C, D, E. the streak and its threshold


@pytest.mark.parametrize("count", [1, 2])
def test_fewer_than_three_zero_runs_count_without_raising_an_anomaly(
    connection, count: int
) -> None:
    register(connection, "zeroes")
    zero_runs(connection, "zeroes", count)

    health = only(read_source_health(connection))

    assert health.status == SUCCESS
    assert health.items_found == 0
    assert health.zero_result_streak == count
    assert health.anomaly_code is None
    assert health.anomaly_message is None


@pytest.mark.parametrize("count", [3, 4, 7])
def test_three_or_more_zero_runs_raise_the_zero_result_anomaly(
    connection, count: int
) -> None:
    register(connection, "zeroes")
    zero_runs(connection, "zeroes", count)

    health = only(read_source_health(connection))

    assert health.status == SUCCESS
    assert health.zero_result_streak == count
    assert health.anomaly_code == ZERO_RESULT_ANOMALY_CODE
    assert health.anomaly_message == zero_result_anomaly_message(count)
    assert str(count) in health.anomaly_message
    assert health.has_anomaly is True


def test_the_anomaly_message_is_a_fixed_deterministic_template() -> None:
    assert zero_result_anomaly_message(3) == (
        "0 résultat trouvé lors de 3 exécutions réussies consécutives. "
        "Défaillance possible du collecteur ou du parseur."
    )
    assert zero_result_anomaly_message(3) == zero_result_anomaly_message(3)


# F. a real result breaks the streak


def test_a_success_with_items_breaks_the_streak(connection) -> None:
    register(connection, "recovered")
    zero_runs(connection, "recovered", 2)
    insert_run(
        connection, "recovered", day=3, status=SUCCESS, items_found=12, new_items=4
    )

    health = only(read_source_health(connection))

    assert health.status == SUCCESS
    assert health.items_found == 12
    assert health.new_items == 4
    assert health.zero_result_streak == 0
    assert health.anomaly_code is None


def test_a_success_with_items_ends_an_existing_anomaly(connection) -> None:
    register(connection, "recovered")
    zero_runs(connection, "recovered", 4)
    insert_run(connection, "recovered", day=5, status=SUCCESS, items_found=1)

    health = only(read_source_health(connection))

    assert health.zero_result_streak == 0
    assert health.anomaly_code is None


# G. a failure is not a successful zero


def test_a_failed_run_is_not_counted_as_a_zero_and_exposes_its_error(connection) -> None:
    register(connection, "failing")
    zero_runs(connection, "failing", 1)
    insert_run(
        connection,
        "failing",
        day=2,
        status=FAILED,
        error_type="HTTPError",
        error_message="board request failed with authorization=[REDACTED]",
    )

    health = only(read_source_health(connection))

    assert health.status == FAILED
    assert health.error_type == "HTTPError"
    assert health.error_message == (
        "board request failed with authorization=[REDACTED]"
    )
    assert health.items_found is None
    assert health.zero_result_streak == 0
    assert health.anomaly_code is None


def test_a_failure_interrupts_an_otherwise_anomalous_history(connection) -> None:
    register(connection, "failing")
    zero_runs(connection, "failing", 3)
    insert_run(connection, "failing", day=4, status=FAILED, error_type="TimeoutError")

    health = only(read_source_health(connection))

    assert health.status == FAILED
    assert health.zero_result_streak == 0
    assert health.anomaly_code is None


# H. a running run is not a finished zero


def test_a_running_run_is_reported_without_being_counted_as_a_zero(connection) -> None:
    register(connection, "running")
    zero_runs(connection, "running", 1)
    insert_run(connection, "running", day=2, status=RUNNING)

    health = only(read_source_health(connection))

    assert health.status == RUNNING
    assert health.last_run_at == "2026-08-02T09:00:00+00:00"
    assert health.items_found is None
    assert health.new_items is None
    assert health.error_type is None
    assert health.error_message is None
    assert health.zero_result_streak == 0
    assert health.anomaly_code is None


# I. an unknown count is never a zero


def test_a_success_with_an_unknown_items_found_is_never_read_as_zero(connection) -> None:
    register(connection, "unknown")
    insert_run(connection, "unknown", day=1, status=SUCCESS, items_found=None)

    health = only(read_source_health(connection))

    assert health.status == SUCCESS
    assert health.items_found is None
    assert health.zero_result_streak == 0
    assert health.anomaly_code is None


def test_an_unknown_items_found_breaks_a_zero_streak(connection) -> None:
    register(connection, "unknown")
    zero_runs(connection, "unknown", 3)
    insert_run(connection, "unknown", day=4, status=SUCCESS, items_found=None)

    health = only(read_source_health(connection))

    assert health.zero_result_streak == 0
    assert health.anomaly_code is None


def test_an_unknown_relevant_items_stays_unknown(connection) -> None:
    register(connection, "unknown")
    insert_run(
        connection, "unknown", day=1, status=SUCCESS, items_found=5, new_items=2
    )

    health = only(read_source_health(connection))

    assert health.items_found == 5
    assert health.new_items == 2
    assert health.relevant_items is None


def test_a_known_relevant_items_is_reported_as_stored(connection) -> None:
    register(connection, "known")
    insert_run(
        connection,
        "known",
        day=1,
        status=SUCCESS,
        items_found=5,
        new_items=2,
        relevant_items=0,
    )

    health = only(read_source_health(connection))

    assert health.relevant_items == 0


# J. sources never share history


def test_two_sources_keep_separate_histories(connection) -> None:
    register(connection, "source_a")
    register(connection, "source_b")
    zero_runs(connection, "source_a", 3)
    insert_run(connection, "source_b", day=1, status=SUCCESS, items_found=9, new_items=3)

    entries = {entry.source_id: entry for entry in read_source_health(connection)}

    assert set(entries) == {"source_a", "source_b"}
    assert entries["source_a"].zero_result_streak == 3
    assert entries["source_a"].anomaly_code == ZERO_RESULT_ANOMALY_CODE
    assert entries["source_b"].zero_result_streak == 0
    assert entries["source_b"].anomaly_code is None
    assert entries["source_b"].items_found == 9


def test_interleaved_runs_are_attributed_to_their_own_source(connection) -> None:
    register(connection, "source_a")
    register(connection, "source_b")
    for day in (1, 3, 5):
        insert_run(connection, "source_a", day=day, status=SUCCESS, items_found=0)
    for day in (2, 4, 6):
        insert_run(connection, "source_b", day=day, status=SUCCESS, items_found=7)

    entries = {entry.source_id: entry for entry in read_source_health(connection)}

    assert entries["source_a"].zero_result_streak == 3
    assert entries["source_a"].last_run_at == "2026-08-05T09:00:00+00:00"
    assert entries["source_b"].zero_result_streak == 0
    assert entries["source_b"].last_run_at == "2026-08-06T09:00:00+00:00"


# K. the enabled flag


def test_a_disabled_configured_source_is_reported_as_disabled(connection) -> None:
    health = only(
        read_source_health(
            connection, configured_sources=[source("paused", enabled=False)]
        )
    )

    assert health.source_id == "paused"
    assert health.enabled is False


def test_a_disabled_persisted_source_is_reported_as_disabled(connection) -> None:
    register(connection, "paused", enabled=False)

    health = only(read_source_health(connection))

    assert health.enabled is False


def test_the_registry_flag_wins_over_a_stale_persisted_flag(connection) -> None:
    register(connection, "paused", enabled=True)
    zero_runs(connection, "paused", 1)

    health = only(
        read_source_health(
            connection, configured_sources=[source("paused", enabled=False)]
        )
    )

    assert health.enabled is False
    assert health.zero_result_streak == 1


# Shape of the whole listing


def test_a_configured_source_is_never_duplicated_by_its_persisted_row(connection) -> None:
    register(connection, "shared")
    zero_runs(connection, "shared", 1)

    entries = read_source_health(connection, configured_sources=[source("shared")])

    assert [entry.source_id for entry in entries] == ["shared"]


def test_entries_are_ordered_by_source_id(connection) -> None:
    register(connection, "zulu")
    register(connection, "alpha")

    entries = read_source_health(
        connection, configured_sources=[source("mike"), source("alpha")]
    )

    assert [entry.source_id for entry in entries] == ["alpha", "mike", "zulu"]


def test_reading_health_twice_returns_the_same_answer(connection) -> None:
    register(connection, "stable")
    zero_runs(connection, "stable", 3)

    assert read_source_health(connection) == read_source_health(connection)


def test_an_empty_database_without_configuration_reports_nothing(connection) -> None:
    assert read_source_health(connection) == []
