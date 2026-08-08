"""Offline tests for Greenhouse fetching, parsing, and normalization."""

import json
from pathlib import Path

import httpx
import pytest

from services.collector.collectors.greenhouse import (
    GreenhouseCollectionError,
    GreenhouseCollector,
    GreenhousePayloadError,
)
from services.collector.sources import SourceConfig


@pytest.fixture
def source() -> SourceConfig:
    return SourceConfig("test_greenhouse", "greenhouse", True, "Test Org", "test")


@pytest.fixture
def payload() -> dict[str, object]:
    return json.loads(Path("tests/fixtures/greenhouse_jobs.json").read_text())


def test_valid_payload_normalizes_real_fields(
    source: SourceConfig, payload: dict[str, object]
) -> None:
    candidate = GreenhouseCollector(source).normalize(
        GreenhouseCollector(source).parse(payload)[0]
    )
    assert candidate.source_external_id == "12345"
    assert candidate.canonical_title == "Test Data Engineer"
    assert candidate.location == "Test City"
    assert candidate.description == "<p>TEST FIXTURE — NON PRODUCTION DATA</p>"
    assert candidate.published_at is None
    assert candidate.source_url == "https://example.test/jobs/12345"
    assert candidate.application_url == candidate.source_url
    assert candidate.canonical_url == candidate.source_url


@pytest.mark.parametrize("payload", [{}, {"other": []}])
def test_payload_without_jobs_is_rejected(
    source: SourceConfig, payload: object
) -> None:
    with pytest.raises(GreenhousePayloadError, match="missing jobs"):
        GreenhouseCollector(source).parse(payload)


def test_non_list_jobs_is_rejected(source: SourceConfig) -> None:
    with pytest.raises(GreenhousePayloadError, match="must be a list"):
        GreenhouseCollector(source).parse({"jobs": {}})


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"id": None}, "valid id"),
        ({"title": ""}, "missing title"),
        ({"absolute_url": None}, "absolute_url"),
        ({"absolute_url": "http://localhost/job"}, "absolute_url"),
    ],
)
def test_invalid_job_is_rejected(
    source: SourceConfig,
    payload: dict[str, object],
    update: dict[str, object],
    message: str,
) -> None:
    job = dict(payload["jobs"][0])  # type: ignore[index]
    job.update(update)
    with pytest.raises(GreenhousePayloadError, match=message):
        GreenhouseCollector(source).normalize(job)


def test_timeout_is_converted_to_explicit_error(source: SourceConfig) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("test timeout", request=request)

    client = httpx.Client(transport=httpx.MockTransport(timeout))
    with pytest.raises(GreenhouseCollectionError, match="timed out"):
        GreenhouseCollector(source, client).fetch()


def test_http_error_is_converted_to_explicit_error(source: SourceConfig) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    with pytest.raises(GreenhouseCollectionError, match="HTTP 503"):
        GreenhouseCollector(source, client).fetch()


def test_invalid_json_is_explicit(source: SourceConfig) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"not-json")
        )
    )
    with pytest.raises(GreenhousePayloadError, match="invalid JSON"):
        GreenhouseCollector(source, client).fetch()
