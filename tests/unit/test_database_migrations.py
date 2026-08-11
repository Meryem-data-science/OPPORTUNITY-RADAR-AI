"""Unit tests for database connections and SQL migrations."""

import sqlite3

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import MigrationError, apply_migrations


EXPECTED_TABLES = {
    "schema_migrations",
    "sources",
    "opportunities",
    "opportunity_sources",
    "deduplication_decisions",
    "opportunity_qualifications",
}


def test_empty_database_receives_foundation_schema(tmp_path) -> None:
    with connect_database(tmp_path / "unit.db") as connection:
        applied = apply_migrations(connection)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        recorded = connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()

    assert applied == ["0001", "0002", "0003", "0004"]
    assert EXPECTED_TABLES <= tables
    assert recorded == [("0001",), ("0002",), ("0003",), ("0004",)]


def test_migrations_are_idempotent_and_do_not_seed_data(tmp_path) -> None:
    with connect_database(tmp_path / "unit.db") as connection:
        assert apply_migrations(connection) == ["0001", "0002", "0003", "0004"]
        assert apply_migrations(connection) == []

        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("sources", "opportunities", "opportunity_sources", "deduplication_decisions")
        }
        migration_count = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]

    assert counts == {"sources": 0, "opportunities": 0, "opportunity_sources": 0, "deduplication_decisions": 0}
    assert migration_count == 4


def test_opportunity_requires_source_url(tmp_path) -> None:
    with connect_database(tmp_path / "unit.db") as connection:
        apply_migrations(connection)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO opportunities (
                    canonical_title, organization, discovered_at, first_seen_at,
                    last_seen_at, status
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("Test role", "Test organization", "2026-01-01", "2026-01-01", "2026-01-01", "new"),
            )


def test_opportunity_source_foreign_keys_are_enforced(tmp_path) -> None:
    with connect_database(tmp_path / "unit.db") as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        apply_migrations(connection)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO opportunity_sources (
                    opportunity_id, source_id, source_url, discovered_at
                ) VALUES (?, ?, ?, ?)
                """,
                (999, "missing-source", "https://test.invalid/item", "2026-01-01"),
            )


def test_failed_migration_is_rolled_back_and_not_recorded(tmp_path) -> None:
    migrations_directory = tmp_path / "migrations"
    migrations_directory.mkdir()
    (migrations_directory / "0002_invalid.sql").write_text(
        "CREATE TABLE partial_table (id INTEGER);\nINVALID SQL;",
        encoding="utf-8",
    )

    with connect_database(tmp_path / "unit.db") as connection:
        with pytest.raises(MigrationError):
            apply_migrations(connection, migrations_directory)

        recorded = connection.execute(
            "SELECT version FROM schema_migrations WHERE version = '0002'"
        ).fetchall()
        partial_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'partial_table'"
        ).fetchall()

    assert recorded == []
    assert partial_table == []


def test_database_at_0001_receives_only_0002(tmp_path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    source = open("migrations/0001_opportunity_foundation.sql", encoding="utf-8").read()
    (migrations / "0001_foundation.sql").write_text(source, encoding="utf-8")
    with connect_database(tmp_path / "upgrade.db") as connection:
        assert apply_migrations(connection, migrations) == ["0001"]
        (migrations / "0002_registry.sql").write_text(
            open("migrations/0002_deduplication_decisions.sql", encoding="utf-8").read(),
            encoding="utf-8",
        )
        assert apply_migrations(connection, migrations) == ["0002"]
        assert apply_migrations(connection, migrations) == []


def test_database_at_0002_receives_only_0003(tmp_path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for name in ("0001_opportunity_foundation.sql", "0002_deduplication_decisions.sql"):
        (migrations / name).write_text(
            open(f"migrations/{name}", encoding="utf-8").read(), encoding="utf-8"
        )
    with connect_database(tmp_path / "upgrade-0002.db") as connection:
        assert apply_migrations(connection, migrations) == ["0001", "0002"]
        name = "0003_deduplication_merges.sql"
        (migrations / name).write_text(
            open(f"migrations/{name}", encoding="utf-8").read(), encoding="utf-8"
        )
        assert apply_migrations(connection, migrations) == ["0003"]
        assert apply_migrations(connection, migrations) == []


def test_database_at_0003_receives_qualification_schema_and_constraints(tmp_path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for name in (
        "0001_opportunity_foundation.sql", "0002_deduplication_decisions.sql",
        "0003_deduplication_merges.sql",
    ):
        (migrations / name).write_text(
            open(f"migrations/{name}", encoding="utf-8").read(), encoding="utf-8"
        )
    with connect_database(tmp_path / "upgrade-0003.db") as connection:
        assert apply_migrations(connection, migrations) == ["0001", "0002", "0003"]
        name = "0004_opportunity_qualifications.sql"
        (migrations / name).write_text(
            open(f"migrations/{name}", encoding="utf-8").read(), encoding="utf-8"
        )
        assert apply_migrations(connection, migrations) == ["0004"]
        columns = {row[1] for row in connection.execute("PRAGMA table_info(opportunity_qualifications)")}
        assert {"opportunity_id", "qualification", "classifier_version", "input_fingerprint"} <= columns
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(opportunity_qualifications)")}
        assert {
            "idx_opportunity_qualifications_qualification",
            "idx_opportunity_qualifications_primary_domain",
            "idx_opportunity_qualifications_opportunity_type",
        } <= indexes
        connection.execute("""INSERT INTO opportunities
            (id, canonical_title, organization, discovered_at, first_seen_at, last_seen_at,
             source_url, status) VALUES (1, 'Role', 'Org', '2026-01-01', '2026-01-01',
             '2026-01-01', 'https://example.test', 'visible')""")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("""INSERT INTO opportunity_qualifications
                (opportunity_id, qualification, primary_domain, opportunity_type,
                 employment_type, listing_quality, matched_domains_json,
                 matched_title_signals_json, matched_description_signals_json,
                 matched_exclusion_signals_json, reasons_json, classifier_version,
                 input_fingerprint, classified_at)
                VALUES (1, 'INVALID', 'UNKNOWN', 'UNKNOWN', 'UNKNOWN', 'NORMAL_LISTING',
                        '[]', '[]', '[]', '[]', '[]', 'v1', ?, '2026-01-01')""", ("a" * 64,))


def test_decision_schema_enforces_pair_status_and_foreign_keys(tmp_path) -> None:
    with connect_database(tmp_path / "constraints.db") as connection:
        apply_migrations(connection)
        connection.execute("INSERT INTO sources (id, type, status) VALUES ('a', 'test', 'active')")
        for identifier in (1, 2):
            connection.execute("""INSERT INTO opportunities
                (id, canonical_title, organization, discovered_at, first_seen_at,
                 last_seen_at, source_url, status)
                VALUES (?, 'Role', 'Org', '2026-01-01', '2026-01-01',
                        '2026-01-01', 'https://test.invalid', 'visible')""", (identifier,))

        def insert(a, b, status="POSSIBLE_DUPLICATE"):
            connection.execute("""INSERT INTO deduplication_decisions
                (opportunity_a_id, opportunity_b_id, status, audit_classification,
                 title_similarity, organization_similarity, title_normalized_exact,
                 organization_normalized_exact, location_signal, shared_source_url,
                 shared_application_url, shared_canonical_url, reasons_json,
                 first_detected_at, last_detected_at)
                VALUES (?, ?, ?, 'STRONG_CANDIDATE', 1, 1, 1, 1, 'EXACT', 0, 0, 0,
                        '[]', '2026-01-01', '2026-01-01')""", (a, b, status))

        insert(1, 2)
        with pytest.raises(sqlite3.IntegrityError):
            insert(1, 2)
        with pytest.raises(sqlite3.IntegrityError):
            insert(2, 1)
        with pytest.raises(sqlite3.IntegrityError):
            insert(1, 1)
        with pytest.raises(sqlite3.IntegrityError):
            insert(1, 999)
        with pytest.raises(sqlite3.IntegrityError):
            insert(1, 2, "INVALID")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM opportunities WHERE id = 1")
