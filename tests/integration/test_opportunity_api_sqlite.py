"""FastAPI integration test using a migrated, real temporary SQLite database."""

from fastapi.testclient import TestClient

from services.api.main import app
from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.database.opportunities import persist_opportunities
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.sources import SourceConfig


def _candidate(source_id: str, suffix: str, application_url: str | None):
    return OpportunityCandidate(
        source_id=source_id,
        source_external_id=f"TEST-ONLY-{suffix}",
        canonical_title=f"TEST ONLY role {suffix}",
        organization="TEST ONLY organization",
        location="TEST ONLY location",
        description=f"TEST ONLY secret description {suffix}",
        published_at=None,
        source_url=f"https://example.invalid/source/{suffix}",
        application_url=application_url,
        canonical_url=f"https://example.invalid/canonical/{suffix}",
    )


def test_real_sqlite_listing_is_filtered_ordered_counted_and_read_only(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "api.db"
    source = SourceConfig(
        "test_api", "greenhouse", True, "TEST ONLY organization", "test-only",
        "jobs", None, 60, "active",
    )
    candidates = [
        _candidate(source.id, "old", "https://example.invalid/apply/old"),
        _candidate(source.id, "new", None),
        _candidate(source.id, "hidden", None),
        _candidate(source.id, "inactive", None),
    ]
    timestamps = iter(
        [
            "2026-01-01T00:00:00+00:00",
            "2026-01-04T00:00:00+00:00",
            "2026-01-03T00:00:00+00:00",
            "2026-01-02T00:00:00+00:00",
        ]
    )
    connection = connect_database(path)
    try:
        assert apply_migrations(connection) == ["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008"]
        assert persist_opportunities(
            connection, source, candidates, clock=lambda: next(timestamps)
        ).created == 4
        connection.execute(
            "UPDATE opportunities SET status = 'hidden' WHERE canonical_title = ?",
            ("TEST ONLY role hidden",),
        )
        connection.execute(
            "UPDATE opportunities SET is_active = 0 WHERE canonical_title = ?",
            ("TEST ONLY role inactive",),
        )
        connection.commit()
        before = connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    finally:
        connection.close()

    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    response = TestClient(app).get("/api/opportunities?limit=100")

    check = connect_database(path)
    try:
        after = check.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    finally:
        check.close()

    assert response.status_code == 200
    payload = response.json()
    assert payload["returned"] == payload["total"] == 2
    assert [item["canonical_title"] for item in payload["items"]] == [
        "TEST ONLY role new",
        "TEST ONLY role old",
    ]
    assert payload["items"][0]["original_url"] == "https://example.invalid/source/new"
    assert payload["items"][1]["original_url"] == "https://example.invalid/apply/old"
    assert all(item["status"] == "visible" for item in payload["items"])
    assert all("description" not in item for item in payload["items"])
    assert payload["items"][0]["description_length"] > 0
    assert before == after == 4
