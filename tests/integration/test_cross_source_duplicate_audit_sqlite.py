"""SQLite integration proof for the strictly read-only duplicate audit."""

import sqlite3
import json

import pytest

from services.collector.cli import audit_duplicates
from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.deduplication.audit import (
    STRONG_CANDIDATE,
    audit_database,
    open_read_only_database,
)


def create_fixture_database(path) -> None:
    connection = connect_database(path)
    try:
        apply_migrations(connection)
        connection.executemany(
            "INSERT INTO sources (id, type, status) VALUES (?, ?, ?)",
            [("test_greenhouse", "greenhouse", "active"), ("test_alert", "linkedin_job_alert", "active")],
        )
        opportunities = [
            (1, "Data Engineer", "Test Org", "Paris", "https://ats.invalid/1"),
            (2, "Data Engineer", "TEST ORG", None, "https://alert.invalid/2"),
            (3, "Software Engineer", "Other Org", "London", "https://ats.invalid/3"),
        ]
        connection.executemany(
            """INSERT INTO opportunities
               (id, canonical_title, organization, location, discovered_at,
                first_seen_at, last_seen_at, source_url, status)
               VALUES (?, ?, ?, ?, '2026-01-01', '2026-01-01', '2026-01-01', ?, 'visible')""",
            opportunities,
        )
        connection.executemany(
            """INSERT INTO opportunity_sources
               (opportunity_id, source_id, source_url, discovered_at)
               VALUES (?, ?, ?, '2026-01-01')""",
            [(1, "test_greenhouse", "https://ats.invalid/1"), (2, "test_alert", "https://alert.invalid/2"), (3, "test_greenhouse", "https://ats.invalid/3")],
        )
        connection.commit()
    finally:
        connection.close()


def snapshot(path) -> tuple[bytes, tuple[int, int, int]]:
    connection = sqlite3.connect(path)
    try:
        counts = tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("sources", "opportunities", "opportunity_sources"))
    finally:
        connection.close()
    return path.read_bytes(), counts


def test_audit_reads_multiple_sources_detects_pair_and_leaves_database_unchanged(tmp_path) -> None:
    path = tmp_path / "audit-fixture.db"
    create_fixture_database(path)
    before = snapshot(path)

    report = audit_database(path)

    after = snapshot(path)
    assert report.total_opportunities == 3
    assert report.sources == ("test_alert", "test_greenhouse")
    assert any(
        candidate.classification == STRONG_CANDIDATE
        and {candidate.opportunity_a.id, candidate.opportunity_b.id} == {1, 2}
        for candidate in report.candidates
    )
    assert before == after
    assert after[1] == (2, 3, 3)  # no INSERT, UPDATE, DELETE, or merge


def test_audit_connection_technically_rejects_writes(tmp_path) -> None:
    path = tmp_path / "protected.db"
    create_fixture_database(path)
    connection = open_read_only_database(path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO sources (id, type, status) VALUES ('forbidden', 'test', 'active')")
    finally:
        connection.close()


def test_missing_database_is_rejected_without_creation(tmp_path) -> None:
    path = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        audit_database(path)
    assert not path.exists()


def test_cli_provides_human_and_machine_readable_reports(tmp_path, capsys) -> None:
    path = tmp_path / "cli-fixture.db"
    create_fixture_database(path)

    assert audit_duplicates.main(["--database", str(path), "--limit", "1"]) == 0
    human = capsys.readouterr().out
    assert "Opportunities inspected: 3" in human
    assert "[1] STRONG_CANDIDATE" in human

    assert audit_duplicates.main(
        ["--database", str(path), "--limit", "1", "--format", "json"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_opportunities"] == 3
    assert payload["display_limit"] == 1
    assert len(payload["candidates"]) == 1
