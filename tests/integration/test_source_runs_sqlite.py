"""End-to-end SQLite tests for RadarAgent source-run persistence."""

import pytest

from services.collector.agent import RadarAgent
from services.collector.config import ApplicationEnvironment, DatabaseBackend, Settings
from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.database.source_runs import recent_source_runs
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.models.source_run import FAILED, RUNNING, SUCCESS
from services.collector.sources import SourceConfig


def source(source_id):
    return SourceConfig(source_id, "greenhouse", True, "Org", "board", status="active")


def candidate(source_id, external_id="1"):
    url = f"https://example.com/{source_id}/{external_id}"
    return OpportunityCandidate(
        source_id, external_id, "Data Engineer", "Org", "Casablanca, Morocco",
        "description", None, url, url, url,
    )


class FakeCollector:
    """A controlled collector: it returns a fixed batch or raises a fixed error."""

    def __init__(self, candidates=(), error=None):
        self._candidates = list(candidates)
        self._error = error

    def collect(self):
        if self._error is not None:
            raise self._error
        return list(self._candidates)


class ObservingCollector:
    """A collector that reads the run history from inside its own collection."""

    def __init__(self, path, source_id, candidates=(), error=None):
        self._path = path
        self._source_id = source_id
        self._candidates = list(candidates)
        self._error = error
        self.observed_runs = None
        self.observed_last_run_at = None

    def collect(self):
        connection = connect_database(self._path)
        try:
            self.observed_runs = recent_source_runs(connection, self._source_id)
            self.observed_last_run_at = connection.execute(
                "SELECT last_run_at FROM sources WHERE id = ?", (self._source_id,)
            ).fetchone()[0]
        finally:
            connection.close()
        if self._error is not None:
            raise self._error
        return list(self._candidates)


def migrated_settings(tmp_path, name):
    path = tmp_path / name
    connection = connect_database(path)
    try:
        assert apply_migrations(connection) == [
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
        ]
    finally:
        connection.close()
    return path, Settings(ApplicationEnvironment.TEST, DatabaseBackend.SQLITE, path)


def read(path, query, parameters=()):
    connection = connect_database(path)
    try:
        return connection.execute(query, parameters).fetchall()
    finally:
        connection.close()


def history(path, source_id):
    connection = connect_database(path)
    try:
        return recent_source_runs(connection, source_id)
    finally:
        connection.close()


def test_the_run_is_already_persisted_as_running_while_the_collector_runs(tmp_path):
    path, settings = migrated_settings(tmp_path, "running-during-collect.db")
    collector = ObservingCollector(path, "board_a", [candidate("board_a")])

    RadarAgent(
        source_loader=lambda: [source("board_a")],
        collector_factory=lambda unused: collector,
        settings_loader=lambda: settings,
    ).run_once()

    # Observed from inside collect(), before the attempt could possibly end.
    assert collector.observed_runs is not None
    assert len(collector.observed_runs) == 1
    running = collector.observed_runs[0]
    assert running.status == RUNNING
    assert running.started_at
    assert running.finished_at is None
    assert running.error_type is None
    assert running.items_found is None
    assert collector.observed_last_run_at is None


def test_success_finalizes_the_very_row_that_was_running(tmp_path):
    path, settings = migrated_settings(tmp_path, "success.db")
    collector = ObservingCollector(
        path, "board_a", [candidate("board_a", "1"), candidate("board_a", "2")]
    )

    summary = RadarAgent(
        source_loader=lambda: [source("board_a")],
        collector_factory=lambda unused: collector,
        settings_loader=lambda: settings,
    ).run_once()

    assert (summary.sources_succeeded, summary.sources_failed) == (1, 0)
    started = collector.observed_runs[0]
    runs = history(path, "board_a")
    assert len(runs) == 1
    run = runs[0]
    assert run.id == started.id
    assert run.started_at == started.started_at
    assert run.source_id == "board_a"
    assert run.status == SUCCESS
    assert run.finished_at is not None
    assert run.finished_at >= run.started_at
    assert run.items_found == 2
    assert run.new_items == 2
    assert run.error_type is None
    assert run.error_message is None
    # Nothing in this phase measures these, so they stay unknown instead of zero.
    assert run.pages_checked is None
    assert run.relevant_items is None
    assert run.http_status is None
    assert run.parser_version is None

    assert read(path, "SELECT last_run_at FROM sources WHERE id = 'board_a'") == [
        (run.finished_at,)
    ]
    assert read(path, "SELECT COUNT(*) FROM opportunities") == [(2,)]


def test_second_run_over_known_items_records_new_items_as_zero_not_unknown(tmp_path):
    path, settings = migrated_settings(tmp_path, "repeat.db")
    collected = [candidate("board_a")]
    agent = RadarAgent(
        source_loader=lambda: [source("board_a")],
        collector_factory=lambda unused: FakeCollector(collected),
        settings_loader=lambda: settings,
    )

    agent.run_once()
    agent.run_once()

    runs = history(path, "board_a")
    assert [(run.items_found, run.new_items) for run in runs] == [(1, 0), (1, 1)]


def test_failure_finalizes_the_very_row_that_was_running_and_stores_no_credential(tmp_path):
    path, settings = migrated_settings(tmp_path, "failure.db")
    error = RuntimeError(
        "Gmail search rejected: Authorization: Bearer ya29-private-value "
        "TURSO_AUTH_TOKEN=supersecret"
    )
    collector = ObservingCollector(path, "board_a", error=error)

    summary = RadarAgent(
        source_loader=lambda: [source("board_a")],
        collector_factory=lambda unused: collector,
        settings_loader=lambda: settings,
    ).run_once()

    assert (summary.sources_succeeded, summary.sources_failed) == (0, 1)
    started = collector.observed_runs[0]
    assert started.status == RUNNING

    runs = history(path, "board_a")
    assert len(runs) == 1
    run = runs[0]
    assert run.id == started.id
    assert run.started_at == started.started_at
    assert run.status == FAILED
    assert run.finished_at is not None
    assert run.finished_at >= run.started_at
    assert run.error_type == "RuntimeError"
    assert "Gmail search rejected" in run.error_message
    assert "ya29-private-value" not in run.error_message
    assert "supersecret" not in run.error_message
    assert run.items_found is None
    assert run.new_items is None

    assert read(path, "SELECT last_run_at FROM sources WHERE id = 'board_a'") == [
        (run.finished_at,)
    ]
    assert read(path, "SELECT COUNT(*) FROM opportunities") == [(0,)]


def test_an_interruption_leaves_the_attempt_as_durable_running_evidence(tmp_path):
    path, settings = migrated_settings(tmp_path, "interrupted.db")
    collected = [candidate("board_a")]
    outcomes = [
        FakeCollector(collected),
        FakeCollector(error=KeyboardInterrupt("operator stopped the radar")),
    ]

    agent = RadarAgent(
        source_loader=lambda: [source("board_a")],
        collector_factory=lambda unused: outcomes.pop(0),
        settings_loader=lambda: settings,
    )
    agent.run_once()
    completed = history(path, "board_a")[0]
    assert completed.status == SUCCESS

    # RadarAgent only isolates Exception, so an interruption propagates untouched.
    with pytest.raises(KeyboardInterrupt):
        agent.run_once()

    runs = history(path, "board_a")
    assert len(runs) == 2
    interrupted = runs[0]
    assert interrupted.id != completed.id
    assert interrupted.status == RUNNING
    assert interrupted.started_at
    assert interrupted.finished_at is None
    assert interrupted.error_type is None
    assert interrupted.error_message is None
    # The interrupted attempt never claims to be a finished run.
    assert read(path, "SELECT last_run_at FROM sources WHERE id = 'board_a'") == [
        (completed.finished_at,)
    ]


def test_one_failing_source_neither_stops_nor_rolls_back_the_others(tmp_path):
    path, settings = migrated_settings(tmp_path, "multi-source.db")
    sources = [source("board_a"), source("board_b"), source("board_c")]

    def factory(config):
        if config.id == "board_b":
            return FakeCollector(error=ConnectionError("board_b is unreachable"))
        return FakeCollector([candidate(config.id)])

    summary = RadarAgent(
        source_loader=lambda: sources, collector_factory=factory,
        settings_loader=lambda: settings,
    ).run_once()

    assert (summary.sources_total, summary.sources_succeeded, summary.sources_failed) == (3, 2, 1)
    assert summary.items_created == 2

    runs = read(
        path,
        "SELECT source_id, status, items_found, new_items, error_type FROM source_runs ORDER BY id",
    )
    assert runs == [
        ("board_a", SUCCESS, 1, 1, None),
        ("board_b", FAILED, None, None, "ConnectionError"),
        ("board_c", SUCCESS, 1, 1, None),
    ]
    assert read(path, "SELECT COUNT(*) FROM source_runs") == [(3,)]

    # The two successes stay committed and every attempted source is stamped.
    assert read(
        path, "SELECT source_id, COUNT(*) FROM opportunity_sources GROUP BY source_id ORDER BY source_id"
    ) == [("board_a", 1), ("board_c", 1)]
    stamped = read(path, "SELECT id, last_run_at FROM sources ORDER BY id")
    assert [row[0] for row in stamped] == ["board_a", "board_b", "board_c"]
    assert all(row[1] is not None for row in stamped)
    assert summary.qualification.success


def test_repeated_executions_are_distinct_history_rows_and_are_never_deduplicated(tmp_path):
    path, settings = migrated_settings(tmp_path, "history.db")
    collected = [candidate("board_a")]
    outcomes = [
        FakeCollector(collected),
        FakeCollector(error=TimeoutError("board timed out")),
        FakeCollector(collected),
    ]

    agent = RadarAgent(
        source_loader=lambda: [source("board_a")],
        collector_factory=lambda unused: outcomes.pop(0),
        settings_loader=lambda: settings,
    )
    for _ in range(3):
        agent.run_once()

    runs = read(
        path, "SELECT id, status, started_at, finished_at FROM source_runs ORDER BY id"
    )
    assert len(runs) == 3
    assert len({row[0] for row in runs}) == 3
    assert [row[1] for row in runs] == [SUCCESS, FAILED, SUCCESS]
    assert [row[2] for row in runs] == sorted(row[2] for row in runs)
    assert read(path, "SELECT last_run_at FROM sources WHERE id = 'board_a'") == [
        (runs[-1][3],)
    ]

    recent = history(path, "board_a")
    assert [run.id for run in recent] == [runs[2][0], runs[1][0], runs[0][0]]


def test_a_collector_reporting_a_nonsense_http_status_is_still_recorded(tmp_path):
    path, settings = migrated_settings(tmp_path, "nonsense-metrics.db")

    class NonsenseMetricsCollector:
        def collect(self):
            return [candidate("board_a")]

        def run_metrics(self):
            return {"http_status": 999}

    summary = RadarAgent(
        source_loader=lambda: [source("board_a")],
        collector_factory=lambda unused: NonsenseMetricsCollector(),
        settings_loader=lambda: settings,
    ).run_once()

    assert (summary.sources_succeeded, summary.sources_failed) == (1, 0)
    runs = history(path, "board_a")
    assert len(runs) == 1
    assert runs[0].status == SUCCESS
    assert runs[0].http_status is None
    assert runs[0].items_found == 1
    assert runs[0].new_items == 1


def test_run_history_survives_a_qualification_failure(tmp_path):
    path, settings = migrated_settings(tmp_path, "qualification-failure.db")
    connection = connect_database(path)
    try:
        connection.execute("DROP TABLE opportunity_qualifications")
        connection.commit()
    finally:
        connection.close()

    summary = RadarAgent(
        source_loader=lambda: [source("board_a")],
        collector_factory=lambda unused: FakeCollector([candidate("board_a")]),
        settings_loader=lambda: settings,
    ).run_once()

    assert summary.sources_succeeded == 1
    assert not summary.qualification.success
    runs = history(path, "board_a")
    assert [run.status for run in runs] == [SUCCESS]
    assert runs[0].items_found == 1
