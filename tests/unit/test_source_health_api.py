"""Unit coverage for the read-only source health HTTP contract."""

from io import StringIO
import json
import logging

from fastapi.testclient import TestClient

from services.api import main
from services.api import source_health
from services.api.source_health import (
    PUBLIC_SOURCE_HEALTH_ERROR,
    SourceHealthListResponse,
    SourceHealthReadError,
    SourceHealthResponse,
)
from services.collector import logging_config
from services.collector.config import (
    ApplicationEnvironment,
    DatabaseBackend,
    Settings,
)
from services.collector.database.source_health import (
    ZERO_RESULT_ANOMALY_CODE,
    zero_result_anomaly_message,
)
from services.collector.models.source_run import RUNNING, SUCCESS
from services.collector.sources import SourceConfig

EXPECTED_FIELDS = {
    "source_id",
    "enabled",
    "last_run_at",
    "status",
    "items_found",
    "new_items",
    "relevant_items",
    "error_type",
    "error_message",
    "zero_result_streak",
    "anomaly_code",
    "anomaly_message",
}


def _entry(**overrides) -> SourceHealthResponse:
    values = {
        "source_id": "test_source",
        "enabled": True,
        "last_run_at": "2026-08-03T09:00:00+00:00",
        "status": SUCCESS,
        "items_found": 4,
        "new_items": 1,
        "relevant_items": None,
        "error_type": None,
        "error_message": None,
        "zero_result_streak": 0,
        "anomaly_code": None,
        "anomaly_message": None,
    }
    values.update(overrides)
    return SourceHealthResponse(**values)


def _listing(*entries: SourceHealthResponse) -> SourceHealthListResponse:
    return SourceHealthListResponse(items=list(entries), returned=len(entries))


def test_listing_returns_the_full_deterministic_entry_shape(monkeypatch) -> None:
    monkeypatch.setattr(main, "read_source_health", lambda: _listing(_entry()))
    response = TestClient(main.app).get("/api/source-health")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"items", "returned"}
    assert payload["returned"] == 1
    assert set(payload["items"][0]) == EXPECTED_FIELDS


def test_unknown_values_are_serialized_as_null_and_never_as_zero(monkeypatch) -> None:
    never_run = _entry(
        source_id="never_run",
        last_run_at=None,
        status=None,
        items_found=None,
        new_items=None,
        relevant_items=None,
    )
    monkeypatch.setattr(main, "read_source_health", lambda: _listing(never_run))
    payload = TestClient(main.app).get("/api/source-health").json()

    entry = payload["items"][0]
    assert entry["last_run_at"] is None
    assert entry["status"] is None
    assert entry["items_found"] is None
    assert entry["new_items"] is None
    assert entry["relevant_items"] is None
    assert entry["zero_result_streak"] == 0
    assert entry["anomaly_code"] is None


def test_a_running_entry_is_exposed_without_metrics(monkeypatch) -> None:
    running = _entry(status=RUNNING, items_found=None, new_items=None)
    monkeypatch.setattr(main, "read_source_health", lambda: _listing(running))
    entry = TestClient(main.app).get("/api/source-health").json()["items"][0]

    assert entry["status"] == RUNNING
    assert entry["items_found"] is None
    assert entry["anomaly_code"] is None


def test_the_zero_result_anomaly_is_exposed_with_its_streak(monkeypatch) -> None:
    anomalous = _entry(
        items_found=0,
        new_items=0,
        zero_result_streak=3,
        anomaly_code=ZERO_RESULT_ANOMALY_CODE,
        anomaly_message=zero_result_anomaly_message(3),
    )
    monkeypatch.setattr(main, "read_source_health", lambda: _listing(anomalous))
    entry = TestClient(main.app).get("/api/source-health").json()["items"][0]

    assert entry["zero_result_streak"] == 3
    assert entry["anomaly_code"] == ZERO_RESULT_ANOMALY_CODE
    assert entry["anomaly_message"] == zero_result_anomaly_message(3)


def test_several_sources_are_returned_separately(monkeypatch) -> None:
    monkeypatch.setattr(
        main,
        "read_source_health",
        lambda: _listing(
            _entry(source_id="source_a", items_found=0, zero_result_streak=2),
            _entry(source_id="source_b", enabled=False, items_found=9),
        ),
    )
    payload = TestClient(main.app).get("/api/source-health").json()

    assert payload["returned"] == 2
    assert [item["source_id"] for item in payload["items"]] == ["source_a", "source_b"]
    assert payload["items"][0]["zero_result_streak"] == 2
    assert payload["items"][1]["enabled"] is False
    assert payload["items"][1]["items_found"] == 9


def test_the_endpoint_accepts_no_parameters(monkeypatch) -> None:
    monkeypatch.setattr(main, "read_source_health", lambda: _listing(_entry()))

    assert TestClient(main.app).get("/api/source-health?limit=5").status_code == 200


def test_database_error_is_sanitized(monkeypatch) -> None:
    secret = "TEST_ONLY_TURSO_AUTH_TOKEN_DO_NOT_EXPOSE"

    def fail() -> SourceHealthListResponse:
        raise SourceHealthReadError(PUBLIC_SOURCE_HEALTH_ERROR)

    monkeypatch.setattr(main, "read_source_health", fail)
    response = TestClient(main.app).get("/api/source-health")

    assert response.status_code == 503
    assert response.json() == {"detail": PUBLIC_SOURCE_HEALTH_ERROR}
    assert secret not in response.text
    assert "Traceback" not in response.text


class _Cursor:
    def __init__(self, *, rows=None) -> None:
        self.rows = rows

    def fetchone(self):
        return None

    def fetchall(self):
        return self.rows


class _ReadOnlyConnection:
    """A connection that records every statement it is asked to run."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement, parameters=()):
        self.statements.append(statement)
        if statement == "PRAGMA query_only = ON":
            return _Cursor()
        if "FROM sources" in statement:
            return _Cursor(rows=[("persisted_source", 1)])
        return _Cursor(
            rows=[
                (
                    "persisted_source",
                    "2026-08-03T09:00:00+00:00",
                    SUCCESS,
                    0,
                    0,
                    None,
                    None,
                    None,
                )
            ]
        )

    def close(self) -> None:
        pass


def _sqlite_settings() -> Settings:
    return Settings(
        environment=ApplicationEnvironment.TEST,
        database_backend=DatabaseBackend.SQLITE,
        sqlite_database_path=None,
    )


def _registry() -> list[SourceConfig]:
    return [
        SourceConfig("configured_source", "greenhouse", False, "Org", "board"),
    ]


def test_the_service_reads_only_and_merges_the_configured_registry(monkeypatch) -> None:
    connection = _ReadOnlyConnection()
    monkeypatch.setattr(source_health, "load_settings", _sqlite_settings)
    monkeypatch.setattr(source_health, "_configured_sources", _registry)
    monkeypatch.setattr(
        source_health, "connect_configured_database", lambda _settings: connection
    )

    listing = source_health.read_source_health()

    assert listing.returned == 2
    assert [item.source_id for item in listing.items] == [
        "configured_source",
        "persisted_source",
    ]
    assert listing.items[0].enabled is False
    assert listing.items[0].status is None
    assert listing.items[1].status == SUCCESS
    assert listing.items[1].zero_result_streak == 1
    assert connection.statements[0] == "PRAGMA query_only = ON"
    assert not any(
        keyword in statement.upper()
        for statement in connection.statements
        for keyword in ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP")
    )


def test_api_events_use_structured_json_logger_without_sensitive_content(
    monkeypatch,
) -> None:
    stream = StringIO()
    logging.getLogger(logging_config.LOGGER_NAME).handlers.clear()
    service_logger = logging_config.configure_logging(stream)
    secret = "TEST_ONLY_TURSO_AUTH_TOKEN_DO_NOT_EXPOSE"
    monkeypatch.setattr(source_health, "load_settings", _sqlite_settings)
    monkeypatch.setattr(source_health, "_configured_sources", _registry)
    monkeypatch.setattr(
        source_health,
        "connect_configured_database",
        lambda _settings: _ReadOnlyConnection(),
    )

    try:
        response = TestClient(main.app).get("/api/source-health")

        def fail_with_secret(_settings):
            raise RuntimeError(secret)

        monkeypatch.setattr(
            source_health, "connect_configured_database", fail_with_secret
        )
        failed_response = TestClient(main.app).get("/api/source-health")
        records = [json.loads(line) for line in stream.getvalue().splitlines()]
    finally:
        service_logger.handlers.clear()

    assert response.status_code == 200
    succeeded = next(
        record
        for record in records
        if record["event"] == "source_health_api_request_succeeded"
    )
    assert succeeded["context"] == {"sources_returned": 2, "anomalies_returned": 0}

    assert failed_response.status_code == 503
    assert failed_response.json() == {"detail": PUBLIC_SOURCE_HEALTH_ERROR}
    failed = next(
        record
        for record in records
        if record["event"] == "source_health_api_request_failed"
    )
    assert failed["context"] == {"error_type": "RuntimeError"}
    rendered = stream.getvalue()
    assert secret not in rendered
    assert secret not in failed_response.text
    assert "Traceback" not in failed_response.text
