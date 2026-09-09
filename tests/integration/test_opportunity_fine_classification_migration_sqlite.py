"""Integration coverage for migration 0025: fine classification on the current row.

The subject here is a database that already holds coarse qualification rows —
the shape every existing installation is in. Migration 0025 must be able to
arrive on top of it without changing a single stored value and without claiming
a fine classification that never ran, and the rows it touched must then be
reconcilable by persistence, once, and idempotently afterwards.

These tests talk to SQLite rather than to the persistence function alone,
because the two NULL semantics the migration encodes have to hold whoever is
writing.
"""

import json
from pathlib import Path
import sqlite3

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import (
    DEFAULT_MIGRATIONS_DIRECTORY, apply_migrations, discover_migrations,
)
from services.collector.qualification.classifier import CLASSIFIER_VERSION, classify_opportunity
from services.collector.qualification.fine_classifier import FINE_CLASSIFIER_VERSION
from services.collector.qualification.persistence import input_fingerprint, persist_qualifications

FINE_COLUMNS = (
    "fine_primary_category", "fine_secondary_categories_json", "fine_category_evidence_json",
    "fine_reasons_json", "fine_classifier_version",
)
COARSE_COLUMNS = (
    "opportunity_id", "qualification", "primary_domain", "opportunity_type", "employment_type",
    "listing_quality", "matched_domains_json", "matched_title_signals_json",
    "matched_description_signals_json", "matched_exclusion_signals_json", "reasons_json",
    "classifier_version", "input_fingerprint", "classified_at", "created_at", "updated_at",
)
TITLE = "Data Engineer"
DESCRIPTION = "Build reliable data pipelines and ETL systems. Full-time role."
URL = "https://example.test/jobs/1"
TIMESTAMP = "2026-02-01T09:00:00.000000+00:00"


def migrations_below(tmp_path: Path, version: str) -> Path:
    """A migrations directory holding every migration before ``version``."""
    directory = tmp_path / f"migrations-below-{version}"
    directory.mkdir()
    for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY):
        if migration.version < version:
            (directory / migration.path.name).write_text(
                migration.path.read_text(encoding="utf-8"), encoding="utf-8"
            )
    return directory


def opportunity(connection: sqlite3.Connection, title: str = TITLE, url: str = URL) -> int:
    row = connection.execute(
        """INSERT INTO opportunities (
            canonical_title, organization, location, description, discovered_at,
            first_seen_at, last_seen_at, source_url, application_url, canonical_url,
            status, is_active
        ) VALUES (?, 'Example Org', 'Casablanca', ?, '2026-01-01', '2026-01-01',
                  '2026-01-01', ?, ?, ?, 'visible', 1) RETURNING id""",
        (title, DESCRIPTION, url, url, url),
    ).fetchone()
    connection.commit()
    return int(row[0])


def legacy_qualification(connection: sqlite3.Connection, opportunity_id: int, title: str = TITLE):
    """One coarse row exactly as persistence wrote it before Phase 8B.1."""
    result = classify_opportunity(
        title, DESCRIPTION, source_url=URL, application_url=URL, canonical_url=URL,
    )
    connection.execute(
        """INSERT INTO opportunity_qualifications (
            opportunity_id, qualification, primary_domain, opportunity_type, employment_type,
            listing_quality, matched_domains_json, matched_title_signals_json,
            matched_description_signals_json, matched_exclusion_signals_json, reasons_json,
            classifier_version, input_fingerprint, classified_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            opportunity_id, str(result.qualification), str(result.primary_domain),
            str(result.opportunity_type), str(result.employment_type),
            str(result.listing_quality),
            *(
                json.dumps([str(value) for value in values], separators=(",", ":"))
                for values in (
                    result.matched_domains, result.matched_title_signals,
                    result.matched_description_signals, result.matched_exclusion_signals,
                    result.reasons,
                )
            ),
            CLASSIFIER_VERSION,
            input_fingerprint(title, DESCRIPTION, URL, URL, URL),
            TIMESTAMP, TIMESTAMP, TIMESTAMP,
        ),
    )
    connection.commit()
    return result


def coarse_only_database(tmp_path: Path, name: str = "coarse-only.db") -> sqlite3.Connection:
    """A database migrated to 0024 holding one already-classified opportunity."""
    connection = connect_database(tmp_path / name)
    apply_migrations(connection, migrations_below(tmp_path, "0025"))
    legacy_qualification(connection, opportunity(connection))
    return connection


def coarse_values(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    columns = ", ".join(COARSE_COLUMNS)
    return connection.execute(
        f"SELECT {columns} FROM opportunity_qualifications ORDER BY opportunity_id"
    ).fetchall()


def test_migration_0025_adds_five_columns_to_the_existing_table_and_no_table_at_all(
    tmp_path: Path,
) -> None:
    connection = connect_database(tmp_path / "full.db")
    try:
        apply_migrations(connection)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(opportunity_qualifications)")
        }
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}

        assert set(FINE_COLUMNS) <= columns
        assert "0025" in applied
        # One current derived row per opportunity: no second fine table, and no
        # history table under any of its usual names.
        assert not {name for name in tables if "fine" in name}
        assert not {
            name for name in tables
            if "qualification_history" in name or "classification_event" in name
        }
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_a_database_below_0025_has_none_of_the_fine_columns(tmp_path: Path) -> None:
    connection = coarse_only_database(tmp_path, "below.db")
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(opportunity_qualifications)")
        }
        assert not columns & set(FINE_COLUMNS)
    finally:
        connection.close()


def test_0025_changes_no_existing_value_and_writes_no_fine_classification(
    tmp_path: Path,
) -> None:
    connection = coarse_only_database(tmp_path)
    try:
        before = coarse_values(connection)
        assert before and before[0][1] == "CORE_TARGET"

        # Everything still pending is applied here. `0025` is the one under
        # test, and it is the first to arrive; later slices ride along behind
        # it without touching the coarse row either.
        assert apply_migrations(connection)[0] == "0025"

        assert coarse_values(connection) == before
        # No backfill, and above all no invented fine version: the row has not
        # been fine-classified, and it must say so rather than look reconciled.
        assert connection.execute(
            f"SELECT {', '.join(FINE_COLUMNS)} FROM opportunity_qualifications"
        ).fetchall() == [(None, None, None, None, None)]
    finally:
        connection.close()


def test_persistence_after_0025_reconciles_the_legacy_row_then_stays_idempotent(
    tmp_path: Path,
) -> None:
    connection = coarse_only_database(tmp_path)
    try:
        apply_migrations(connection)
        before = coarse_values(connection)

        first = persist_qualifications(connection)

        assert (first.created, first.updated, first.unchanged, first.total) == (0, 1, 0, 1)
        row = connection.execute(
            f"SELECT {', '.join(FINE_COLUMNS)} FROM opportunity_qualifications"
        ).fetchone()
        assert row[0] == "DATA_ENGINEERING"
        assert json.loads(row[1]) == []
        assert json.loads(row[2])
        assert json.loads(row[3])
        assert row[4] == FINE_CLASSIFIER_VERSION
        # The coarse half was recomputed to the same values it already held; only
        # the timestamps a reconciliation legitimately moves are allowed to differ.
        after = coarse_values(connection)
        assert [values[:13] for values in after] == [values[:13] for values in before]
        assert [values[14] for values in after] == [values[14] for values in before]

        second = persist_qualifications(connection)

        assert (second.created, second.updated, second.unchanged, second.total) == (0, 0, 1, 1)
        assert coarse_values(connection) == after
    finally:
        connection.close()


def test_the_closed_fine_taxonomy_is_enforced_by_the_database(tmp_path: Path) -> None:
    connection = coarse_only_database(tmp_path)
    try:
        apply_migrations(connection)
        persist_qualifications(connection)
        for category in ("DATA_SCIENCE", "MLOPS", "OTHER"):
            connection.execute(
                "UPDATE opportunity_qualifications SET fine_primary_category = ?", (category,)
            )
        for invented in ("data_science", "LLM_ENGINEERING", "UNKNOWN", ""):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE opportunity_qualifications SET fine_primary_category = ?", (invented,)
                )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "assignments",
    [
        # A version without the JSON columns it must have written: a row that
        # claims to have been classified but shows nothing of the classification.
        "fine_classifier_version = 'fine-data-ai-rules-v2'",
        "fine_classifier_version = 'fine-data-ai-rules-v2',"
        " fine_secondary_categories_json = '[]', fine_category_evidence_json = '[]'",
        # A blank or padded version is not a version.
        "fine_classifier_version = '  ', fine_secondary_categories_json = '[]',"
        " fine_category_evidence_json = '[]', fine_reasons_json = '[]'",
        "fine_classifier_version = ' fine-data-ai-rules-v2 ',"
        " fine_secondary_categories_json = '[]', fine_category_evidence_json = '[]',"
        " fine_reasons_json = '[]'",
        # Fine values without a version: an unattributable classification.
        "fine_primary_category = 'MLOPS'",
        "fine_secondary_categories_json = '[]', fine_category_evidence_json = '[]',"
        " fine_reasons_json = '[]'",
    ],
)
def test_the_two_null_semantics_cannot_be_conflated(tmp_path: Path, assignments: str) -> None:
    """Either nothing fine is recorded, or a real classification is — never half."""
    connection = coarse_only_database(tmp_path)
    try:
        apply_migrations(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f"UPDATE opportunity_qualifications SET {assignments}")
    finally:
        connection.close()


def test_a_classified_row_with_no_category_is_a_legal_state(tmp_path: Path) -> None:
    """Case B: the classifier ran on an unqualified opportunity and assigned nothing."""
    connection = coarse_only_database(tmp_path)
    try:
        apply_migrations(connection)
        connection.execute(
            """UPDATE opportunity_qualifications SET
                fine_primary_category = NULL, fine_secondary_categories_json = '[]',
                fine_category_evidence_json = '[]',
                fine_reasons_json = '["opportunity is not qualified as Data/AI"]',
                fine_classifier_version = ?""",
            (FINE_CLASSIFIER_VERSION,),
        )
        assert connection.execute(
            "SELECT fine_primary_category, fine_classifier_version FROM opportunity_qualifications"
        ).fetchone() == (None, FINE_CLASSIFIER_VERSION)
    finally:
        connection.close()
