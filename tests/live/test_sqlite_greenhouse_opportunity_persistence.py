"""Opt-in real Greenhouse persistence through a temporary operational SQLite DB."""

import os

import pytest

from services.collector.collectors.greenhouse import GreenhouseCollector
from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.database.opportunities import persist_opportunities
from services.collector.sources import get_enabled_source

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_SQLITE_OPPORTUNITY_TEST") != "1",
    reason="set RUN_LIVE_SQLITE_OPPORTUNITY_TEST=1 for real Greenhouse SQLite persistence",
)


def test_real_greenhouse_candidate_is_idempotently_persisted_to_sqlite(
    tmp_path,
) -> None:
    source = get_enabled_source("scale_ai_greenhouse")
    candidate = GreenhouseCollector(source).collect()[0]
    connection = connect_database(tmp_path / "live-opportunities.db")
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
            "0011",
            "0012",
            "0013",
            "0014",
            "0015",
            "0016",
        ]
        first = persist_opportunities(connection, source, [candidate])
        stored = connection.execute("""
            SELECT canonical_title, organization, source_url, application_url,
                   canonical_url, description, length(description)
            FROM opportunities
            """).fetchone()
        second = persist_opportunities(connection, source, [candidate])
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("sources", "opportunities", "opportunity_sources")
        }
    finally:
        connection.close()

    assert stored == (
        candidate.canonical_title,
        candidate.organization,
        candidate.source_url,
        candidate.application_url,
        candidate.canonical_url,
        candidate.description,
        len(candidate.description) if candidate.description is not None else None,
    )
    assert (first.created, first.updated) == (1, 0)
    assert (second.created, second.updated) == (0, 1)
    assert counts == {"sources": 1, "opportunities": 1, "opportunity_sources": 1}
