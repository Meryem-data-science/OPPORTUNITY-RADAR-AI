"""Phase 4.5 assembly from disposable persisted SQLite projections."""

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.matching import (
    AlignmentStatus,
    build_role_domain_preference_signals,
    load_matching_input,
    role_domain_preferences_fingerprint,
)
from services.digital_twin.preferences.models import (
    OpportunityPreferences,
    OpportunityType,
    WorkMode,
)
from services.digital_twin.preferences.repository import synchronize_profile_preferences
from services.digital_twin.preferences.service import set_profile_preferences
from services.digital_twin.repository import ensure_user_profile


def test_persisted_signals_are_deterministic_and_read_only(tmp_path):
    connection = connect_database(tmp_path / "role-domain-preferences.db")
    apply_migrations(connection)
    profile_id = ensure_user_profile(connection, "phase45@example.invalid").profile_id
    set_profile_preferences(
        connection,
        profile_id,
        OpportunityPreferences(
            opportunity_types=(OpportunityType.PFE,),
            work_modes=(WorkMode.REMOTE,),
            preferred_domains=("Data Science", "Data Engineering"),
        ),
    )
    synchronize_profile_preferences(connection, profile_id)
    opportunity_id = int(
        connection.execute(
            """INSERT INTO opportunities
               (canonical_title, organization, remote_type, discovered_at,
                first_seen_at, last_seen_at, source_url, status)
               VALUES ('irrelevant', 'synthetic', 'REMOTE', 't', 't', 't',
                       'https://example.invalid/phase45', 'new') RETURNING id"""
        ).fetchone()[0]
    )
    connection.execute(
        """INSERT INTO opportunity_qualifications
           (opportunity_id, qualification, primary_domain, opportunity_type,
            employment_type, listing_quality, matched_domains_json,
            matched_title_signals_json, matched_description_signals_json,
            matched_exclusion_signals_json, reasons_json, classifier_version,
            input_fingerprint, classified_at)
           VALUES (?, 'CORE_TARGET', 'DATA_ENGINEERING', 'PFE', 'FULL_TIME',
                   'NORMAL_LISTING', '[]', '[]', '[]', '[]', '[]',
                   'qualification-v1', ?, '2099-01-01')""",
        (opportunity_id, "b" * 64),
    )
    connection.commit()

    before = connection.total_changes
    matching_input = load_matching_input(connection, profile_id, opportunity_id)
    first = build_role_domain_preference_signals(matching_input)
    second = build_role_domain_preference_signals(
        load_matching_input(connection, profile_id, opportunity_id)
    )
    first_fingerprint = role_domain_preferences_fingerprint(first)

    assert matching_input.profile.preferences.preferred_domains == (
        "Data Science",
        "Data Engineering",
    )
    assert matching_input.opportunity.qualification.primary_domain == "DATA_ENGINEERING"
    assert matching_input.opportunity.remote_type == "REMOTE"
    assert first.opportunity_type.status is AlignmentStatus.MATCH
    assert first.domain.status is AlignmentStatus.MATCH
    assert first.domain.preferred_rank == 2
    assert first.work_mode.status is AlignmentStatus.MATCH
    assert first == second
    assert first_fingerprint == role_domain_preferences_fingerprint(second)
    assert connection.total_changes == before
    connection.close()
