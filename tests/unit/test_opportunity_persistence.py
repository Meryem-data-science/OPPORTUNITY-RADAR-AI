"""Unit coverage for transactional same-source opportunity persistence."""

import sqlite3

import pytest

from services.collector.database import opportunities
from services.collector.database.opportunities import (
    OpportunityPersistenceError,
    persist_opportunities,
)
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.sources import SourceConfig


def source(category: str = "jobs") -> SourceConfig:
    return SourceConfig(
        "test_greenhouse",
        "greenhouse",
        True,
        "TEST ONLY Org",
        "test-only-board",
        category,
        "US",
        60,
        "active",
    )


def candidate(**changes: object) -> OpportunityCandidate:
    values = {
        "source_id": "test_greenhouse",
        "source_external_id": "TEST-1",
        "canonical_title": "TEST ONLY Engineer",
        "organization": "TEST ONLY Org",
        "location": "Test City",
        "description": "TEST ONLY description",
        "published_at": "2026-01-01T00:00:00Z",
        "source_url": "https://example.invalid/jobs/TEST-1",
        "application_url": "https://example.invalid/jobs/TEST-1/apply",
        "canonical_url": "https://example.invalid/jobs/TEST-1",
    }
    values.update(changes)
    return OpportunityCandidate(**values)  # type: ignore[arg-type]


@pytest.fixture
def connection() -> sqlite3.Connection:
    database = sqlite3.connect(":memory:")
    database.executescript(
        open("migrations/0001_opportunity_foundation.sql", encoding="utf-8").read()
    )
    yield database
    database.close()


def test_source_and_new_occurrence_are_inserted_with_returned_id(connection) -> None:
    summary = persist_opportunities(
        connection, source(), [candidate()], clock=lambda: "2026-01-01T01:00:00+00:00"
    )

    assert (summary.created, summary.updated, summary.total) == (1, 0, 1)
    assert connection.execute(
        "SELECT type, enabled, category, country, frequency_minutes, status FROM sources"
    ).fetchone() == ("greenhouse", 1, "jobs", "US", 60, "active")
    opportunity_id = connection.execute("SELECT id FROM opportunities").fetchone()[0]
    occurrence = connection.execute(
        "SELECT opportunity_id, source_url, application_url, canonical_url "
        "FROM opportunity_sources"
    ).fetchone()
    assert occurrence == (
        opportunity_id,
        "https://example.invalid/jobs/TEST-1",
        "https://example.invalid/jobs/TEST-1/apply",
        "https://example.invalid/jobs/TEST-1",
    )


def test_source_upsert_and_existing_candidate_refresh_preserve_values(
    connection,
) -> None:
    times = iter(["2026-01-01T01:00:00+00:00", "2026-01-01T02:00:00+00:00"])
    persist_opportunities(
        connection, source(), [candidate()], clock=lambda: next(times)
    )
    summary = persist_opportunities(
        connection,
        source("updated-jobs"),
        [
            candidate(
                canonical_title="TEST ONLY Updated Engineer",
                location=None,
                description=None,
                published_at=None,
                application_url=None,
                canonical_url=None,
            )
        ],
        clock=lambda: next(times),
    )

    assert (summary.created, summary.updated) == (0, 1)
    assert connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 1
    assert (
        connection.execute("SELECT COUNT(*) FROM opportunity_sources").fetchone()[0]
        == 1
    )
    row = connection.execute(
        "SELECT canonical_title, location, description, published_at, "
        "first_seen_at, last_seen_at FROM opportunities"
    ).fetchone()
    assert row == (
        "TEST ONLY Updated Engineer",
        "Test City",
        "TEST ONLY description",
        "2026-01-01T00:00:00Z",
        "2026-01-01T01:00:00+00:00",
        "2026-01-01T02:00:00+00:00",
    )
    assert (
        connection.execute("SELECT category FROM sources").fetchone()[0]
        == "updated-jobs"
    )


def test_batch_error_rolls_back_every_write(connection, monkeypatch) -> None:
    original_insert = opportunities._insert_candidate
    calls = 0

    def fail_second_insert(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("TEST ONLY forced failure")
        return original_insert(*args, **kwargs)

    monkeypatch.setattr(opportunities, "_insert_candidate", fail_second_insert)
    with pytest.raises(OpportunityPersistenceError) as raised:
        persist_opportunities(
            connection,
            source(),
            [candidate(), candidate(source_url="https://example.invalid/jobs/TEST-2")],
        )

    assert isinstance(raised.value.__cause__, RuntimeError)
    for table in ("sources", "opportunities", "opportunity_sources"):
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
