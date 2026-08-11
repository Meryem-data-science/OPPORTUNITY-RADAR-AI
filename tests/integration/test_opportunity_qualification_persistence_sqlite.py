"""SQLite integration coverage for versioned qualification persistence."""

import json
from pathlib import Path
import sqlite3

import pytest

from services.collector.database.migrations import apply_migrations
from services.collector.qualification.classifier import classify_opportunity
from services.collector.qualification.persistence import persist_qualifications


def _connection(tmp_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(tmp_path / "persist.db")
    connection.execute("PRAGMA foreign_keys = ON")
    apply_migrations(connection)
    return connection


def _insert(connection: sqlite3.Connection, title: str, **overrides: object) -> int:
    values = {
        "organization": "Example Org", "location": "Paris",
        "description": "Build reliable data pipelines and ETL systems. Full-time role.",
        "source_url": "https://example.test/jobs/1",
        "application_url": "https://example.test/apply/1",
        "canonical_url": "https://example.test/jobs/1", "status": "visible", "is_active": 1,
    }
    values.update(overrides)
    cursor = connection.execute(
        """INSERT INTO opportunities (
            canonical_title, organization, location, description, discovered_at,
            first_seen_at, last_seen_at, source_url, application_url, canonical_url,
            status, is_active
        ) VALUES (?, ?, ?, ?, '2026-01-01', '2026-01-01', '2026-01-01', ?, ?, ?, ?, ?)""",
        (title, values["organization"], values["location"], values["description"],
         values["source_url"], values["application_url"], values["canonical_url"],
         values["status"], values["is_active"]),
    )
    connection.commit()
    return cursor.lastrowid


def test_persists_complete_result_and_is_idempotent_without_touching_source(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    opportunity_id = _insert(connection, "Data Engineer Internship")
    before = connection.execute("SELECT * FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()

    first = persist_qualifications(connection)
    row = connection.execute("SELECT * FROM opportunity_qualifications").fetchone()
    columns = [item[0] for item in connection.execute("SELECT * FROM opportunity_qualifications").description]
    persisted = dict(zip(columns, row, strict=True))

    assert first.created == first.total == 1
    assert (persisted["qualification"], persisted["primary_domain"]) == ("CORE_TARGET", "DATA_ENGINEERING")
    assert (persisted["opportunity_type"], persisted["employment_type"], persisted["listing_quality"]) == (
        "INTERNSHIP", "FULL_TIME", "NORMAL_LISTING",
    )
    for field in (
        "matched_domains_json", "matched_title_signals_json",
        "matched_description_signals_json", "matched_exclusion_signals_json", "reasons_json",
    ):
        assert isinstance(json.loads(persisted[field]), list)
    timestamps = persisted["created_at"], persisted["classified_at"], persisted["updated_at"]
    second = persist_qualifications(connection)
    assert (second.created, second.updated, second.unchanged, second.total) == (0, 0, 1, 1)
    assert connection.execute(
        "SELECT created_at, classified_at, updated_at FROM opportunity_qualifications"
    ).fetchone() == timestamps
    assert connection.execute("SELECT * FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone() == before
    connection.close()


@pytest.mark.parametrize("field,value", [
    ("canonical_title", "Product Manager"),
    ("description", "Manage product strategy and customer relationships."),
    ("application_url", None),
])
def test_classifier_input_change_updates_fingerprint(tmp_path: Path, field: str, value: object) -> None:
    connection = _connection(tmp_path)
    opportunity_id = _insert(connection, "Data Engineer")
    persist_qualifications(connection)
    old = connection.execute(
        "SELECT input_fingerprint, created_at FROM opportunity_qualifications"
    ).fetchone()
    connection.execute(f"UPDATE opportunities SET {field} = ? WHERE id = ?", (value, opportunity_id))
    connection.commit()
    summary = persist_qualifications(connection)
    new = connection.execute(
        "SELECT input_fingerprint, created_at FROM opportunity_qualifications"
    ).fetchone()
    assert summary.updated == 1
    assert new[0] != old[0]
    assert new[1] == old[1]
    connection.close()


@pytest.mark.parametrize("field", ["location", "organization"])
def test_metadata_change_is_unchanged(tmp_path: Path, field: str) -> None:
    connection = _connection(tmp_path)
    opportunity_id = _insert(connection, "Data Scientist")
    persist_qualifications(connection)
    connection.execute(f"UPDATE opportunities SET {field} = 'Changed' WHERE id = ?", (opportunity_id,))
    connection.commit()
    assert persist_qualifications(connection).unchanged == 1
    connection.close()


def test_version_change_reclassifies_and_closed_outcomes_persist(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    _insert(connection, "Sales Manager")
    _insert(connection, "Interesting Opportunity", description="Short")
    first = persist_qualifications(connection)
    assert first.created == 2
    outcomes = connection.execute(
        "SELECT qualification, primary_domain FROM opportunity_qualifications ORDER BY opportunity_id"
    ).fetchall()
    assert outcomes == [("OUT_OF_SCOPE", "NON_TARGET"), ("UNCERTAIN", "UNKNOWN")]
    second = persist_qualifications(connection, classifier_version="qualification-rules-v2-test")
    assert (second.created, second.updated, second.unchanged) == (0, 2, 0)
    connection.close()


def test_inactive_and_merge_tombstones_are_excluded(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    _insert(connection, "Data Engineer")
    _insert(connection, "Data Scientist", is_active=0)
    _insert(connection, "ML Engineer", status="merged_duplicate")
    summary = persist_qualifications(connection)
    assert summary.total == summary.created == 1
    connection.close()


def test_batch_failure_rolls_back_every_write(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    _insert(connection, "Data Engineer")
    _insert(connection, "Data Scientist")
    calls = 0

    def failing_classifier(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected batch failure")
        return classify_opportunity(*args, **kwargs)

    with pytest.raises(RuntimeError, match="injected"):
        persist_qualifications(connection, classifier=failing_classifier)
    assert connection.execute("SELECT COUNT(*) FROM opportunity_qualifications").fetchone() == (0,)
    connection.close()
