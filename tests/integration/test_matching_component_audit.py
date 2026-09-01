"""Read-only component audit selection against disposable SQLite."""

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.matching import audit_matching_database
from services.digital_twin.repository import ensure_user_profile


def test_database_audit_selects_only_active_persisted_in_scope(tmp_path):
    path = tmp_path / "audit.db"
    connection = connect_database(path)
    apply_migrations(connection)
    profile_id = ensure_user_profile(connection, "audit@example.invalid").profile_id

    def insert(title: str, *, active: int = 1) -> int:
        return int(
            connection.execute(
                """INSERT INTO opportunities
               (canonical_title, organization, description, discovered_at,
                first_seen_at, last_seen_at, source_url, status, is_active)
               VALUES (?, 'private', 'python data engineering', 't', 't', 't',
                       'https://example.invalid', 'new', ?) RETURNING id""",
                (title, active),
            ).fetchone()[0]
        )

    included = insert("Data engineer")
    excluded_scope = insert("Sales")
    insert("Inactive data", active=0)
    for opportunity_id, qualification in (
        (included, "CORE_TARGET"),
        (excluded_scope, "OUT_OF_SCOPE"),
    ):
        connection.execute(
            """INSERT INTO opportunity_qualifications
               (opportunity_id, qualification, primary_domain, opportunity_type,
                employment_type, listing_quality, matched_domains_json,
                matched_title_signals_json, matched_description_signals_json,
                matched_exclusion_signals_json, reasons_json, classifier_version,
                input_fingerprint, classified_at)
               VALUES (?, ?, 'DATA_ENGINEERING', 'INTERNSHIP', 'UNKNOWN',
                       'NORMAL_LISTING', '[]', '[]', '[]', '[]', '[]',
                       'persisted-v1', ?, 't')""",
            (opportunity_id, qualification, "f" * 64),
        )
    connection.commit()
    connection.close()

    first = audit_matching_database(path, profile_id)
    second = audit_matching_database(path, profile_id)
    assert first.observation_count == 1
    assert first.audit_fingerprint == second.audit_fingerprint
    assert first.total_changes_before == first.total_changes_after == 0
