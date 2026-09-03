"""Real SQLite integration coverage using the shared Foundation migration."""

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.database.opportunities import persist_opportunities
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.sources import SourceConfig


def test_sqlite_persistence_is_same_source_idempotent(tmp_path) -> None:
    connection = connect_database(tmp_path / "persistence.db")
    source = SourceConfig(
        "test_greenhouse",
        "greenhouse",
        True,
        "TEST ONLY Org",
        "test-only-board",
        "jobs",
        None,
        60,
        "active",
    )
    candidate = OpportunityCandidate(
        source_id=source.id,
        source_external_id="TEST-REAL-SQLITE-1",
        canonical_title="TEST ONLY SQLite Opportunity",
        organization=source.organization,
        location=None,
        description="é" * 12_001,
        published_at=None,
        source_url="https://example.invalid/jobs/TEST-REAL-SQLITE-1",
        application_url="https://example.invalid/jobs/TEST-REAL-SQLITE-1",
        canonical_url="https://example.invalid/jobs/TEST-REAL-SQLITE-1",
    )
    timestamps = iter(["2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00"])
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
            "0017",
            "0018",
            "0019",
            "0020",
        ]
        first = persist_opportunities(
            connection, source, [candidate], clock=lambda: next(timestamps)
        )
        first_seen, old_last_seen = connection.execute(
            "SELECT first_seen_at, last_seen_at FROM opportunities"
        ).fetchone()
        second = persist_opportunities(
            connection, source, [candidate], clock=lambda: next(timestamps)
        )
        new_first_seen, new_last_seen = connection.execute(
            "SELECT first_seen_at, last_seen_at FROM opportunities"
        ).fetchone()
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("sources", "opportunities", "opportunity_sources")
        }
        stored_url = connection.execute(
            "SELECT source_url FROM opportunity_sources"
        ).fetchone()[0]
        stored_description = connection.execute(
            "SELECT description FROM opportunities"
        ).fetchone()[0]
    finally:
        connection.close()

    assert (first.created, first.updated) == (1, 0)
    assert (second.created, second.updated) == (0, 1)
    assert counts == {"sources": 1, "opportunities": 1, "opportunity_sources": 1}
    assert stored_url == candidate.source_url
    assert stored_description == candidate.description
    assert len(stored_description) == 12_001
    assert new_first_seen == first_seen
    assert new_last_seen >= old_last_seen
