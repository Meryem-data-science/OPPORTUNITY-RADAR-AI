"""Unit coverage for the read-only opportunity HTTP contract."""

from io import StringIO
import json
import logging

from fastapi.testclient import TestClient
import pytest

from services.api import main
from services.api import opportunities
from services.api.opportunities import (
    OpportunityListResponse,
    OpportunityReadError,
    OpportunityResponse,
    PUBLIC_DATABASE_ERROR,
)
from services.collector import logging_config
from services.collector.config import (
    ApplicationEnvironment,
    DatabaseBackend,
    Settings,
)


def _response(count: int) -> OpportunityListResponse:
    items = [
        OpportunityResponse(
            id=index,
            canonical_title=f"TEST ONLY role {index}",
            organization="TEST ONLY organization",
            location=None,
            source_url=f"https://example.invalid/source/{index}",
            application_url=f"https://example.invalid/apply/{index}",
            canonical_url=None,
            original_url=f"https://example.invalid/apply/{index}",
            discovered_at="2026-01-01T00:00:00+00:00",
            last_seen_at="2026-01-02T00:00:00+00:00",
            status="visible",
            description_length=42,
            # This fixture says nothing about classification, so it uses the
            # honest never-classified state rather than a default: the five
            # fine fields are required, so no construction site can omit them
            # and accidentally publish "not classified" for a classified row.
            fine_primary_category=None,
            fine_secondary_categories=None,
            fine_category_evidence=None,
            fine_reasons=None,
            fine_classifier_version=None,
        )
        for index in range(count)
    ]
    return OpportunityListResponse(items=items, returned=count, total=count)


@pytest.mark.parametrize(
    ("query", "expected_limit"),
    [("", 20), ("?limit=1", 1), ("?limit=100", 100)],
)
def test_valid_limits(monkeypatch, query: str, expected_limit: int) -> None:
    observed = []

    def read(limit: int) -> OpportunityListResponse:
        observed.append(limit)
        return _response(1)

    monkeypatch.setattr(main, "read_opportunities", read)
    response = TestClient(main.app).get(f"/api/opportunities{query}")

    assert response.status_code == 200
    assert observed == [expected_limit]
    assert "description" not in response.json()["items"][0]


@pytest.mark.parametrize("value", ["0", "101", "not-a-number"])
def test_invalid_limits_return_clean_422(value: str) -> None:
    response = TestClient(main.app).get(f"/api/opportunities?limit={value}")

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")


class _Cursor:
    def __init__(self, *, row=None, rows=None) -> None:
        self.row = row
        self.rows = rows

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class _ReadOnlyConnection:
    def execute(self, statement, parameters=()):
        if statement == "PRAGMA query_only = ON":
            return _Cursor()
        if "COUNT(*)" in statement:
            return _Cursor(row=(1,))
        if "opportunity_sources" in statement:
            return _Cursor(rows=[])
        return _Cursor(
            rows=[
                (
                    7,
                    "TEST ONLY role",
                    "TEST ONLY organization",
                    None,
                    "https://example.invalid/source/7",
                    None,
                    None,
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-02T00:00:00+00:00",
                    "visible",
                    37,
                    # The persisted coarse qualification, read only to validate
                    # the fine values against it, then the five fine columns
                    # migration 0025 adds, in the order the read model selects
                    # them.
                    "CORE_TARGET",
                    "MLOPS",
                    '["MACHINE_LEARNING"]',
                    '[{"category":"MLOPS","field":"TITLE","kind":"ROLE_PHRASE",'
                    '"signal":"mlops engineer"}]',
                    '["fine categories evidenced by the title take precedence"]',
                    "fine-data-ai-rules-v2",
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


def test_api_events_use_structured_json_logger_without_sensitive_content(
    monkeypatch,
) -> None:
    stream = StringIO()
    logging.getLogger(logging_config.LOGGER_NAME).handlers.clear()
    service_logger = logging_config.configure_logging(stream)
    secret = "TEST_ONLY_TURSO_AUTH_TOKEN_DO_NOT_EXPOSE"
    description = "TEST ONLY full description must not be logged"
    monkeypatch.setattr(opportunities, "load_settings", _sqlite_settings)
    monkeypatch.setattr(
        opportunities,
        "connect_configured_database",
        lambda _settings: _ReadOnlyConnection(),
    )

    try:
        response = TestClient(main.app).get("/api/opportunities?limit=7")
        records = [json.loads(line) for line in stream.getvalue().splitlines()]

        def fail_with_secret(_settings):
            raise RuntimeError(secret)

        monkeypatch.setattr(
            opportunities, "connect_configured_database", fail_with_secret
        )
        failed_response = TestClient(main.app).get("/api/opportunities?limit=3")
        records = [json.loads(line) for line in stream.getvalue().splitlines()]
    finally:
        service_logger.handlers.clear()

    assert response.status_code == 200
    # The persisted fine classification is decoded and returned, and none of it
    # reaches the log line asserted below.
    item = response.json()["items"][0]
    assert item["fine_primary_category"] == "MLOPS"
    assert item["fine_secondary_categories"] == ["MACHINE_LEARNING"]
    assert item["fine_category_evidence"] == [
        {
            "category": "MLOPS",
            "field": "TITLE",
            "kind": "ROLE_PHRASE",
            "signal": "mlops engineer",
        }
    ]
    assert item["fine_classifier_version"] == "fine-data-ai-rules-v2"
    succeeded = next(
        record
        for record in records
        if record["event"] == "opportunity_api_request_succeeded"
    )
    assert succeeded["context"] == {"items_returned": 1, "request_limit": 7}

    assert failed_response.status_code == 503
    assert failed_response.json() == {"detail": PUBLIC_DATABASE_ERROR}
    failed = next(
        record
        for record in records
        if record["event"] == "opportunity_api_request_failed"
    )
    assert failed["context"] == {
        "error_type": "RuntimeError",
        "request_limit": 3,
    }
    rendered = stream.getvalue()
    assert secret not in rendered
    assert description not in rendered
    assert secret not in failed_response.text
    assert description not in failed_response.text
    assert "Traceback" not in failed_response.text


def test_database_error_is_sanitized(monkeypatch) -> None:
    secret = "TEST_ONLY_TURSO_AUTH_TOKEN_DO_NOT_EXPOSE"

    def fail(_limit: int) -> OpportunityListResponse:
        raise OpportunityReadError(PUBLIC_DATABASE_ERROR)

    monkeypatch.setattr(main, "read_opportunities", fail)
    response = TestClient(main.app).get("/api/opportunities")

    assert response.status_code == 503
    assert response.json() == {"detail": PUBLIC_DATABASE_ERROR}
    assert secret not in response.text
    assert "Traceback" not in response.text
