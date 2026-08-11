"""End-to-end SQLite workflow for audit, staging, and human review."""

from services.collector.cli import review_duplicates
from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.deduplication.decisions import CONFIRMED_DUPLICATE, NOT_DUPLICATE, get_decision


def fixture(path) -> None:
    with connect_database(path) as connection:
        apply_migrations(connection)
        connection.executemany("INSERT INTO sources (id, type, status) VALUES (?, 'test', 'active')", [("ats",), ("alert",)])
        rows = [
            (1, "Data Analyst Intern", "Acme Inc", "https://ats/1", "ats"),
            (2, "Data Analyst Intern", "Acme Inc", "https://alert/2", "alert"),
            (3, "Intern Data Analyst", "Acme Inc", "https://alert/3", "alert"),
            (4, "Data Analyst Trainee", "Acme Inc", "https://alert/4", "alert"),
            (5, "Gardener", "Other Org", "https://alert/5", "alert"),
        ]
        for identifier, title, org, url, source in rows:
            connection.execute("""INSERT INTO opportunities
                (id, canonical_title, organization, discovered_at, first_seen_at, last_seen_at, source_url, status)
                VALUES (?, ?, ?, '2026-01-01', '2026-01-01', '2026-01-01', ?, 'visible')""", (identifier, title, org, url))
            connection.execute("INSERT INTO opportunity_sources (opportunity_id, source_id, source_url, discovered_at) VALUES (?, ?, ?, '2026-01-01')", (identifier, source, url))


def counts(path):
    with connect_database(path) as connection:
        return tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("opportunities", "opportunity_sources", "deduplication_decisions"))


def test_complete_review_workflow_preserves_physical_rows(tmp_path) -> None:
    path = tmp_path / "workflow.db"
    fixture(path)
    before = counts(path)
    assert review_duplicates.main(["scan", "--database", str(path)]) == 0
    assert counts(path) == before

    assert review_duplicates.main(["scan", "--database", str(path), "--apply"]) == 0
    after_scan = counts(path)
    assert after_scan[:2] == before[:2]
    with connect_database(path) as connection:
        strong = get_decision(connection, 1, 2)
        possible = get_decision(connection, 1, 3)
        assert strong is not None and possible is not None
        assert get_decision(connection, 1, 4) is None

    assert review_duplicates.main(["decide", "--database", str(path), "--opportunity-a", "1", "--opportunity-b", "2", "--decision", CONFIRMED_DUPLICATE]) == 0
    assert review_duplicates.main(["decide", "--database", str(path), "--opportunity-a", "1", "--opportunity-b", "3", "--decision", NOT_DUPLICATE]) == 0
    assert review_duplicates.main(["scan", "--database", str(path), "--apply"]) == 0
    with connect_database(path) as connection:
        assert get_decision(connection, 1, 2).status == CONFIRMED_DUPLICATE
        assert get_decision(connection, 1, 3).status == NOT_DUPLICATE
    assert counts(path) == after_scan


def test_missing_path_is_not_created(tmp_path) -> None:
    missing = tmp_path / "missing.db"
    assert review_duplicates.main(["scan", "--database", str(missing)]) == 1
    assert not missing.exists()
