"""SQLite-level proof that qualification and fine-category auditing is read-only."""

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from services.collector.database.migrations import apply_migrations
from services.collector.qualification.audit import audit_database, open_read_only_database
from services.collector.qualification.fine_classifier import FINE_CLASSIFIER_VERSION
from services.collector.qualification.fine_taxonomy import FineCategory


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
        # Controlled rows covering every fine-audit outcome: an assigned primary,
        # a primary with a secondary, an explicit OTHER, and an unqualified row.
        extra = (
            ("MLOps Engineer", "Operate model serving and model monitoring."),
            ("Machine Learning Engineer, Computer Vision", "Train detection models."),
            ("Data Governance Analyst", "Own the data governance charter."),
            ("Software Engineer", "We are an AI company building the future."),
        )
        for index, (extra_title, extra_description) in enumerate(extra):
            url = f"https://example.test/extra/{index}"
            extra_row = connection.execute(
                """INSERT INTO opportunities (
                    canonical_title, organization, location, description, discovered_at,
                    first_seen_at, last_seen_at, source_url, application_url, canonical_url,
                    status, is_active
                ) VALUES (?, 'Example', 'Casablanca', ?, '2026-01-01', '2026-01-01',
                          '2026-01-01', ?, ?, ?, 'visible', 1)""",
                (extra_title, extra_description, url, url, url),
            )
            connection.execute(
                """INSERT INTO opportunity_sources (
                    opportunity_id, source_id, source_url, application_url, canonical_url,
                    discovered_at
                ) VALUES (?, 'greenhouse:test', ?, ?, ?, '2026-01-01')""",
                (extra_row.lastrowid, url, url, url),
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

    assert report.total_active_opportunities == 5
    assert report.qualification_counts == {
        "ADJACENT_TARGET": 1, "CORE_TARGET": 3, "UNCERTAIN": 1,
    }
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


def test_fine_category_counts_are_correct_and_leave_the_database_untouched(tmp_path: Path) -> None:
    path = _database(tmp_path)
    before_hash = _hash(path)

    report = audit_database(path)

    assert report.fine_primary_category_counts == {
        "COMPUTER_VISION": 1, "DATA_ENGINEERING": 1, "MLOPS": 1, "OTHER": 1,
    }
    assert report.fine_secondary_category_counts == {"MACHINE_LEARNING": 1}
    # The merge tombstone and the inactive row are Data Scientist postings; if
    # either were audited they would appear as an extra DATA_SCIENCE primary.
    assert "DATA_SCIENCE" not in report.fine_primary_category_counts
    # "Software Engineer" with AI-company boilerplate stays UNCERTAIN and is
    # therefore uncategorized rather than OTHER.
    assert report.fine_uncategorized_count == 1
    by_title = {item.title: item.fine_classification for item in report.opportunities}
    assert by_title["Data Engineer"].primary_category is FineCategory.DATA_ENGINEERING
    assert by_title["MLOps Engineer"].primary_category is FineCategory.MLOPS
    assert by_title["Data Governance Analyst"].primary_category is FineCategory.OTHER
    vision = by_title["Machine Learning Engineer, Computer Vision"]
    assert vision.primary_category is FineCategory.COMPUTER_VISION
    assert vision.secondary_categories == (FineCategory.MACHINE_LEARNING,)
    assert by_title["Software Engineer"].primary_category is None
    assert by_title["Software Engineer"].evidence == ()
    assert all(
        item.fine_classification.classifier_version == FINE_CLASSIFIER_VERSION
        for item in report.opportunities
    )
    assert _hash(path) == before_hash


def test_the_fine_audit_serializes_without_touching_persistence(tmp_path: Path) -> None:
    path = _database(tmp_path)
    before_hash = _hash(path)

    payload = audit_database(path).to_dict()

    assert payload["fine_uncategorized_count"] == 1
    entry = next(item for item in payload["opportunities"] if item["title"] == "MLOps Engineer")
    assert entry["fine_classification"]["primary_category"] is FineCategory.MLOPS
    assert entry["fine_classification"]["classifier_version"] == FINE_CLASSIFIER_VERSION
    assert entry["fine_classification"]["evidence"]
    assert json.loads(json.dumps(payload, default=str))
    connection = sqlite3.connect(path)
    try:
        # Phase 8A.1 is read-only: no fine-category column, table or migration.
        columns = {row[1] for row in connection.execute("PRAGMA table_info(opportunity_qualifications)")}
        assert not {name for name in columns if "fine" in name or "category" in name}
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert not {name for name in tables if "fine" in name}
    finally:
        connection.close()
    assert _hash(path) == before_hash


def test_the_calibration_reason_counts_are_read_only_aggregations(tmp_path: Path) -> None:
    """Phase 8A.2 adds two read-time breakdowns of rules that already ran.

    They exist so the real-corpus calibration can be read without writing
    anything: no extra query, no column, no table, no migration.
    """
    path = _database(tmp_path)
    before_hash = _hash(path)
    before = sqlite3.connect(path)
    try:
        schema_before = before.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    finally:
        before.close()

    report = audit_database(path)

    assert sum(report.qualification_reason_counts.values()) == report.total_active_opportunities
    assert sum(report.fine_reason_counts.values()) == report.total_active_opportunities
    assert report.qualification_reason_counts["explicit core Data/AI title signal"] == 3
    assert report.qualification_reason_counts["explicit adjacent Data/AI title signal"] == 1
    assert list(report.qualification_reason_counts) == sorted(report.qualification_reason_counts)
    assert list(report.fine_reason_counts) == sorted(report.fine_reason_counts)
    assert audit_database(path) == report
    assert _hash(path) == before_hash
    after = sqlite3.connect(path)
    try:
        assert after.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall() == schema_before
    finally:
        after.close()
