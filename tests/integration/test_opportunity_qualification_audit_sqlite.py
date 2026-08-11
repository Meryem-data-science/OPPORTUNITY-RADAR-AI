"""SQLite-level proof that qualification auditing is strictly read-only."""

import hashlib
from pathlib import Path
import sqlite3

import pytest

from services.collector.database.migrations import apply_migrations
from services.collector.qualification.audit import audit_database, open_read_only_database


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "qualification.db"
    connection = sqlite3.connect(path)
    try:
        apply_migrations(connection)
        connection.execute(
            "INSERT INTO sources (id, type, status) VALUES ('greenhouse:test', 'greenhouse', 'active')"
        )
        values = (
            "Data Engineer", "Example", "Paris", "Build reliable pipelines. Full-time.",
            "2026-01-01", "2026-01-01", "2026-01-01", "https://example.test/job",
            "https://example.test/apply", "https://example.test/job", "visible",
        )
        cursor = connection.execute(
            """INSERT INTO opportunities (
                canonical_title, organization, location, description, discovered_at,
                first_seen_at, last_seen_at, source_url, application_url, canonical_url,
                status, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            values,
        )
        connection.execute(
            """INSERT INTO opportunity_sources (
                opportunity_id, source_id, source_url, application_url, canonical_url, discovered_at
            ) VALUES (?, 'greenhouse:test', ?, ?, ?, '2026-01-01')""",
            (cursor.lastrowid, values[7], values[8], values[9]),
        )
        # Active merge tombstones and inactive rows must not be inspected.
        for status, active in (("merged_duplicate", 1), ("visible", 0)):
            other = connection.execute(
                """INSERT INTO opportunities (
                    canonical_title, organization, discovered_at, first_seen_at, last_seen_at,
                    source_url, status, is_active
                ) VALUES ('Data Scientist', 'Hidden', '2026-01-01', '2026-01-01',
                          '2026-01-01', ?, ?, ?)""",
                (f"https://example.test/{status}/{active}", status, active),
            )
            connection.execute(
                """INSERT INTO opportunity_sources (
                    opportunity_id, source_id, source_url, discovered_at
                ) VALUES (?, 'greenhouse:test', ?, '2026-01-01')""",
                (other.lastrowid, f"https://example.test/{status}/{active}"),
            )
        connection.commit()
    finally:
        connection.close()
    return path


def test_audit_preserves_database_bytes_rows_status_schema_and_taxonomy(tmp_path: Path) -> None:
    path = _database(tmp_path)
    before_hash = _hash(path)
    before = sqlite3.connect(path)
    try:
        rows_before = before.execute(
            "SELECT id, opportunity_type, employment_type, status, is_active, relevance_score FROM opportunities ORDER BY id"
        ).fetchall()
        schema_before = before.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        migrations_before = before.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    finally:
        before.close()

    report = audit_database(path)

    assert report.total_active_opportunities == 1
    assert report.qualification_counts == {"CORE_TARGET": 1}
    assert report.sources == ("greenhouse:test",)
    assert _hash(path) == before_hash
    after = sqlite3.connect(path)
    try:
        assert after.execute(
            "SELECT id, opportunity_type, employment_type, status, is_active, relevance_score FROM opportunities ORDER BY id"
        ).fetchall() == rows_before
        assert after.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall() == schema_before
        assert after.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == migrations_before
        columns = {row[1] for row in after.execute("PRAGMA table_info(opportunities)")}
        assert "qualification" not in columns
        assert "primary_domain" not in columns
    finally:
        after.close()


def test_missing_database_is_refused_without_creating_file(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        audit_database(path)
    assert not path.exists()


def test_audit_connection_enforces_sqlite_and_pragma_read_only(tmp_path: Path) -> None:
    path = _database(tmp_path)
    connection = open_read_only_database(path)
    try:
        assert connection.execute("PRAGMA query_only").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("UPDATE opportunities SET is_active = 0")
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE forbidden (id INTEGER)")
    finally:
        connection.close()
