"""Matching engine through official loaders and disposable SQLite only."""

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.matching import (
    MatchLane,
    build_matching_assessments,
    load_opportunity_matching_input,
    load_profile_matching_input,
)
from services.digital_twin.preferences.models import (
    CareerObjectives,
    OpportunityPreferences,
    OpportunityType,
    WorkMode,
)
from services.digital_twin.preferences.repository import synchronize_profile_preferences
from services.digital_twin.preferences.service import (
    set_profile_career_objectives,
    set_profile_preferences,
)
from services.digital_twin.repository import ensure_user_profile


def test_disposable_database_pipeline_is_read_only_and_deterministic(tmp_path):
    connection = connect_database(tmp_path / "matching-engine.db")
    apply_migrations(connection)
    profile_id = ensure_user_profile(connection, "engine@example.invalid").profile_id
    set_profile_preferences(
        connection,
        profile_id,
        OpportunityPreferences(
            opportunity_types=(OpportunityType.INTERNSHIP,),
            work_modes=(WorkMode.REMOTE,),
            preferred_domains=("Data Engineering", "Data Science"),
            constraints=(),
        ),
    )
    set_profile_career_objectives(
        connection, profile_id, CareerObjectives(("Build Python data pipelines",))
    )
    synchronize_profile_preferences(connection, profile_id)

    ids = []
    for title, description, domain in (
        ("Python Data Engineer", "Build Python data pipelines", "DATA_ENGINEERING"),
        ("Research Analyst", "Study market economics", "DATA_SCIENCE"),
    ):
        opportunity_id = int(
            connection.execute(
                """INSERT INTO opportunities
                   (canonical_title, organization, description, discovered_at,
                    first_seen_at, last_seen_at, source_url, status)
                   VALUES (?, 'TEST', ?, 't', 't', 't', ?, 'new') RETURNING id""",
                (title, description, f"https://example.invalid/{len(ids)}"),
            ).fetchone()[0]
        )
        connection.execute(
            """INSERT INTO opportunity_qualifications
               (opportunity_id, qualification, primary_domain, opportunity_type,
                employment_type, listing_quality, matched_domains_json,
                matched_title_signals_json, matched_description_signals_json,
                matched_exclusion_signals_json, reasons_json, classifier_version,
                input_fingerprint, classified_at)
               VALUES (?, 'CORE_TARGET', ?, 'INTERNSHIP', 'UNKNOWN',
                       'NORMAL_LISTING', '[]', '[]', '[]', '[]', '[]',
                       'classifier-v1', ?, 't')""",
            (opportunity_id, domain, str(len(ids)) * 64),
        )
        ids.append(opportunity_id)
    connection.commit()

    profile = load_profile_matching_input(connection, profile_id)
    opportunities = tuple(
        load_opportunity_matching_input(connection, identifier) for identifier in ids
    )
    before = connection.total_changes
    first = build_matching_assessments(profile, opportunities)
    second = build_matching_assessments(profile, tuple(reversed(opportunities)))
    after = connection.total_changes
    connection.close()

    assert first == second
    assert first.batch_fingerprint == second.batch_fingerprint
    assert first.assessment_count == 2
    assert all(item.lane is MatchLane.PRIMARY for item in first.assessments)
    assert [item.semantic.percentile for item in first.assessments] == [1.0, 0.0]
    assert [item.domain.normalized_score for item in first.assessments] == [1.0, 0.5]
    assert all(
        item.required_skill.normalized_score is None for item in first.assessments
    )
    assert all(item.evidence_coverage == 0.5 for item in first.assessments)
    assert [item.match_quality for item in first.assessments] == [1.0, 0.2]
    assert after == before
