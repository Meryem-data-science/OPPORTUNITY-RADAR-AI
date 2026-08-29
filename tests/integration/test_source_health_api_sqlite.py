"""FastAPI source health integration test on a migrated temporary SQLite database.

The database is disposable, the runs are written by the real persistence layer,
and the source catalogue is a real YAML registry parsed by the real loader.
"""

import sqlite3
import textwrap

from fastapi.testclient import TestClient
import pytest

from services.api import source_health as source_health_service
from services.api.main import app
from services.collector.database.connection import (
    connect_database,
    connect_readonly_database,
)
from services.collector.database.migrations import apply_migrations
from services.collector.database.source_health import (
    ZERO_RESULT_ANOMALY_CODE,
    zero_result_anomaly_message,
)
from services.collector.database.source_runs import (
    finalize_failed_source_run,
    finalize_successful_source_run,
    start_source_run,
)
from services.collector.models.source_run import FAILED, RUNNING, SUCCESS, SourceRunMetrics
from services.collector.sources import SourceConfig, load_source_registry

REGISTRY = textwrap.dedent(
    """
    sources:
      - id: test_api_anomalous
        type: greenhouse
        enabled: true
        category: jobs
        country: null
        frequency_minutes: 120
        status: active
        organization: TEST ONLY organization
        board_token: test-only-anomalous
      - id: test_api_failing
        type: greenhouse
        enabled: true
        category: jobs
        country: null
        frequency_minutes: 120
        status: active
        organization: TEST ONLY organization
        board_token: test-only-failing
      - id: test_api_running
        type: greenhouse
        enabled: true
        category: jobs
        country: null
        frequency_minutes: 120
        status: active
        organization: TEST ONLY organization
        board_token: test-only-running
      - id: test_api_never_run
        type: greenhouse
        enabled: false
        category: jobs
        country: null
        frequency_minutes: 120
        status: active
        organization: TEST ONLY organization
        board_token: test-only-never-run
    """
).strip()

SECRET = "TEST-ONLY-not-a-real-token-abcdefghijklmnopqrstuvwxyz012345"


def _source(source_id: str) -> SourceConfig:
    return SourceConfig(source_id, "greenhouse", True, "TEST ONLY organization", "board")


def _clock(day: int):
    instants = iter([f"2026-08-{day:02d}T09:0{index}:00+00:00" for index in range(1, 10)])
    return lambda: next(instants)


def _success(connection, source: SourceConfig, *, day: int, items_found, new_items=None):
    tick = _clock(day)
    attempt = start_source_run(connection, source, clock=tick)
    finalize_successful_source_run(
        connection,
        attempt,
        metrics=SourceRunMetrics(items_found=items_found, new_items=new_items),
        clock=tick,
    )


def _failure(connection, source: SourceConfig, *, day: int, error: BaseException):
    tick = _clock(day)
    attempt = start_source_run(connection, source, clock=tick)
    finalize_failed_source_run(connection, attempt, error=error, clock=tick)


def _seed(path) -> tuple[int, int]:
    connection = connect_database(path)
    try:
        assert apply_migrations(connection) == ["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008"]
        anomalous = _source("test_api_anomalous")
        failing = _source("test_api_failing")
        running = _source("test_api_running")
        for day in (1, 2, 3):
            _success(connection, anomalous, day=day, items_found=0, new_items=0)
        _success(connection, failing, day=1, items_found=5, new_items=2)
        _failure(
            connection,
            failing,
            day=2,
            error=RuntimeError(f"board fetch rejected: api_key={SECRET}"),
        )
        _success(connection, running, day=1, items_found=0, new_items=0)
        start_source_run(connection, running, clock=_clock(2))
        runs = connection.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0]
        sources = connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    finally:
        connection.close()
    return runs, sources


def _counts(path) -> tuple[int, int]:
    connection = connect_database(path)
    try:
        return (
            connection.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
        )
    finally:
        connection.close()


def test_real_sqlite_source_health_is_deterministic_honest_and_read_only(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "source-health-api.db"
    registry_path = tmp_path / "sources.yaml"
    registry_path.write_text(REGISTRY, encoding="utf-8")
    before = _seed(path)

    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    monkeypatch.setattr(
        source_health_service,
        "_configured_sources",
        lambda: load_source_registry(registry_path),
    )
    response = TestClient(app).get("/api/source-health")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert set(payload) == {"items", "returned"}
    assert payload["returned"] == 4
    entries = {item["source_id"]: item for item in payload["items"]}
    assert [item["source_id"] for item in payload["items"]] == sorted(entries)

    anomalous = entries["test_api_anomalous"]
    assert anomalous["enabled"] is True
    assert anomalous["status"] == SUCCESS
    assert anomalous["items_found"] == 0
    assert anomalous["new_items"] == 0
    assert anomalous["relevant_items"] is None
    assert anomalous["error_type"] is None
    assert anomalous["error_message"] is None
    assert anomalous["zero_result_streak"] == 3
    assert anomalous["anomaly_code"] == ZERO_RESULT_ANOMALY_CODE
    assert anomalous["anomaly_message"] == zero_result_anomaly_message(3)
    assert anomalous["last_run_at"].startswith("2026-08-03")

    failing = entries["test_api_failing"]
    assert failing["status"] == FAILED
    assert failing["error_type"] == "RuntimeError"
    assert failing["error_message"] is not None
    assert "[REDACTED]" in failing["error_message"]
    assert failing["zero_result_streak"] == 0
    assert failing["anomaly_code"] is None
    assert failing["anomaly_message"] is None

    running = entries["test_api_running"]
    assert running["status"] == RUNNING
    assert running["items_found"] is None
    assert running["new_items"] is None
    assert running["error_type"] is None
    assert running["zero_result_streak"] == 0
    assert running["anomaly_code"] is None
    assert running["last_run_at"].startswith("2026-08-02")

    never_run = entries["test_api_never_run"]
    assert never_run["enabled"] is False
    assert never_run["last_run_at"] is None
    assert never_run["status"] is None
    assert never_run["items_found"] is None
    assert never_run["new_items"] is None
    assert never_run["relevant_items"] is None
    assert never_run["error_type"] is None
    assert never_run["error_message"] is None
    assert never_run["zero_result_streak"] == 0
    assert never_run["anomaly_code"] is None

    assert SECRET not in response.text
    assert "Traceback" not in response.text
    assert _counts(path) == before


def test_a_missing_database_stays_missing_and_answers_unavailable(
    tmp_path, monkeypatch
) -> None:
    """A read must never bring the database it was pointed at into existence."""
    registry_path = tmp_path / "sources.yaml"
    registry_path.write_text(REGISTRY, encoding="utf-8")
    # A missing file inside a missing directory: neither may be created.
    missing_directory = tmp_path / "absent"
    database_path = missing_directory / "not-migrated.db"
    assert database_path.exists() is False
    assert missing_directory.exists() is False

    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(database_path))
    monkeypatch.setattr(
        source_health_service,
        "_configured_sources",
        lambda: load_source_registry(registry_path),
    )

    response = TestClient(app).get("/api/source-health")

    assert response.status_code == 503
    assert response.json() == {
        "detail": source_health_service.PUBLIC_SOURCE_HEALTH_ERROR
    }
    assert "Traceback" not in response.text
    assert database_path.exists() is False
    assert missing_directory.exists() is False
    assert list(tmp_path.iterdir()) == [registry_path]


def test_an_existing_database_is_opened_read_only(tmp_path, monkeypatch) -> None:
    """The connection itself must refuse a write, not merely decline to make one."""
    path = tmp_path / "read-only.db"
    registry_path = tmp_path / "sources.yaml"
    registry_path.write_text(REGISTRY, encoding="utf-8")
    before = _seed(path)

    connection = connect_readonly_database(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "INSERT INTO sources (id, type, enabled, status)"
                " VALUES ('test_api_injected', 'greenhouse', 1, 'active')"
            )
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE test_api_injected (id INTEGER)")
    finally:
        connection.close()

    assert _counts(path) == before
