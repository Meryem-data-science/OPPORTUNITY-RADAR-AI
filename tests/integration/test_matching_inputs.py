"""Phase 4.1 assembly against disposable, synthetic SQLite data only."""

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.extractors.opportunity_constraints.requirements.service import (
    extract_one_opportunity_requirements,
)
from services.collector.extractors.opportunity_constraints.service import (
    synchronize_opportunity_constraints,
)
from services.collector.matching import (
    MatchingInputError,
    load_matching_input,
    load_opportunity_matching_input,
    load_profile_matching_input,
    matching_input_fingerprint,
)
from services.collector.matching.models import MatchingProfileInput
from services.digital_twin.preferences.models import (
    CareerObjectives,
    OpportunityPreferences,
    OpportunityType,
    WorkMode,
)
from services.digital_twin.preferences.service import (
    set_profile_career_objectives,
    set_profile_preferences,
)
from services.digital_twin.preferences.repository import synchronize_profile_preferences
from services.digital_twin.repository import ensure_user_profile

RICH_DESCRIPTION = (
    "<h3>Required Qualifications</h3><ul><li>Strong Python skills</li>"
    "<li>SQL or R</li></ul><h3>Nice to have</h3><ul><li>Airflow</li></ul>"
)


@pytest.fixture
def database(tmp_path):
    connection = connect_database(tmp_path / "matching.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _fact(connection, profile_id, fact_type, value, status="ACCEPTED"):
    decided_at = None if status == "PROPOSED" else "2099-01-01"
    return int(
        connection.execute(
            """INSERT INTO profile_facts
               (profile_id, fact_type, value, status, decided_at)
             VALUES (?, ?, ?, ?, ?) RETURNING id""",
            (profile_id, fact_type, value, status, decided_at),
        ).fetchone()[0]
    )


def _seed_profile_projections(connection, profile_id):
    skill_fact = _fact(connection, profile_id, "SKILL", "Python")
    experience_fact = _fact(connection, profile_id, "EXPERIENCE", "experience")
    project_fact = _fact(connection, profile_id, "PROJECT", "project")
    education_fact = _fact(connection, profile_id, "EDUCATION", "education")
    _fact(connection, profile_id, "SKILL", "Leaked proposed skill", "PROPOSED")
    _fact(connection, profile_id, "SKILL", "Leaked rejected skill", "REJECTED")

    skill_id = int(
        connection.execute(
            "INSERT INTO skills (canonical_key, canonical_name) VALUES ('python', 'Python') RETURNING id"
        ).fetchone()[0]
    )
    profile_skill_id = int(
        connection.execute(
            "INSERT INTO profile_skills (profile_id, skill_id) VALUES (?, ?) RETURNING id",
            (profile_id, skill_id),
        ).fetchone()[0]
    )
    connection.execute(
        """INSERT INTO profile_skill_evidence
               (profile_skill_id, fact_id, normalizer_version, normalization_rule_id)
             VALUES (?, ?, 'skill-normalizer-v1', 'CANONICAL_FORM_V1')""",
        (profile_skill_id, skill_fact),
    )
    connection.execute(
        """INSERT INTO profile_experiences
               (profile_id, fact_id, role_text, organization_text, period_text,
                description_text, structurer_version, structuring_rule_id)
             VALUES (?, ?, 'Data Analyst', 'EXCLUDED ORG', 'EXCLUDED PERIOD',
                     'Built pipelines', 'structured-profile-v1', 'TEST_RULE')""",
        (profile_id, experience_fact),
    )
    connection.execute(
        """INSERT INTO profile_projects
               (profile_id, fact_id, title_text, period_text, description_text,
                structurer_version, structuring_rule_id)
             VALUES (?, ?, 'Opportunity Radar', 'EXCLUDED PERIOD', 'Collected jobs',
                     'structured-profile-v1', 'TEST_RULE')""",
        (profile_id, project_fact),
    )
    connection.execute(
        """INSERT INTO profile_educations
               (profile_id, fact_id, institution_text, program_text, period_text,
                description_text, structurer_version, structuring_rule_id)
             VALUES (?, ?, 'EXCLUDED SCHOOL', 'Data programme', 'EXCLUDED PERIOD',
                     NULL, 'structured-profile-v1', 'TEST_RULE')""",
        (profile_id, education_fact),
    )
    connection.commit()


def _opportunity(connection, description, remote_type=None):
    opportunity_id = int(
        connection.execute(
            """INSERT INTO opportunities
               (canonical_title, organization, remote_type, description, discovered_at,
                first_seen_at, last_seen_at, source_url, status)
             VALUES ('TEST ONLY Data Role', 'TEST ONLY Org', ?, ?, 't', 't', 't',
                     'https://example.invalid/job', 'new') RETURNING id""",
            (remote_type, description),
        ).fetchone()[0]
    )
    connection.execute(
        """INSERT INTO opportunity_qualifications
               (opportunity_id, qualification, primary_domain, opportunity_type,
                employment_type, listing_quality, matched_domains_json,
                matched_title_signals_json, matched_description_signals_json,
                matched_exclusion_signals_json, reasons_json, classifier_version,
                input_fingerprint, classified_at)
             VALUES (?, 'CORE_TARGET', 'DATA_ENGINEERING', 'INTERNSHIP', 'FULL_TIME',
                     'NORMAL_LISTING', '[]', '[]', '[]', '[]', '[]',
                     'qualification-v1', ?, '2099-01-01')""",
        (opportunity_id, "a" * 64),
    )
    connection.commit()
    return opportunity_id


def test_profile_input_uses_only_phase_3_projections_and_preserves_absence(database):
    profile_id = ensure_user_profile(database, "matching@example.invalid").profile_id
    empty_profile_id = ensure_user_profile(database, "empty@example.invalid").profile_id
    _seed_profile_projections(database, profile_id)

    profile = load_profile_matching_input(database, profile_id)
    empty = load_profile_matching_input(database, empty_profile_id)

    assert isinstance(profile, MatchingProfileInput)
    assert [
        (item.canonical_key, item.normalizer_versions) for item in profile.skills
    ] == [("python", ("skill-normalizer-v1",))]
    assert profile.experiences[0].role_text == "Data Analyst"
    assert profile.projects[0].title_text == "Opportunity Radar"
    assert profile.educations[0].program_text == "Data programme"
    assert "Leaked" not in repr(profile)
    assert "EXCLUDED" not in repr(profile)
    assert empty.preferences is None
    assert empty.career_objectives is None


def test_preferences_and_objectives_are_loaded_without_hard_constraints(database):
    profile_id = ensure_user_profile(database, "preferences@example.invalid").profile_id
    set_profile_preferences(
        database,
        profile_id,
        OpportunityPreferences(
            opportunity_types=(OpportunityType.PFE,),
            work_modes=(WorkMode.REMOTE,),
            preferred_domains=("Data Engineering",),
            constraints=("EXCLUDED",),
        ),
    )
    set_profile_career_objectives(
        database, profile_id, CareerObjectives(("Build data platforms",))
    )
    synchronize_profile_preferences(database, profile_id)
    profile = load_profile_matching_input(database, profile_id)
    assert profile.preferences.opportunity_types == ("PFE",)
    assert profile.preferences.work_modes == ("REMOTE",)
    assert profile.preferences.preferred_domains == ("Data Engineering",)
    assert "EXCLUDED" not in repr(profile)
    assert profile.career_objectives.objectives == ("Build data platforms",)


def test_opportunity_unknown_empty_and_extracted_requirements_are_distinct(database):
    absent_id = _opportunity(database, "No extraction yet", "REMOTE")
    empty_id = _opportunity(database, "A friendly team and an office.")
    rich_id = _opportunity(database, RICH_DESCRIPTION)
    synchronize_opportunity_constraints(database)
    extract_one_opportunity_requirements(database, empty_id)
    extract_one_opportunity_requirements(database, rich_id)

    absent = load_opportunity_matching_input(database, absent_id)
    empty = load_opportunity_matching_input(database, empty_id)
    rich = load_opportunity_matching_input(database, rich_id)
    assert absent.qualification.classifier_version == "qualification-v1"
    assert absent.requirements is None
    assert empty.requirements is not None
    assert empty.requirements.skills == ()
    assert empty.requirements.ambiguities == ()
    kinds = {
        (item.canonical_key, item.requirement) for item in rich.requirements.skills
    }
    assert ("python", "REQUIRED") in kinds
    assert ("apache airflow", "PREFERRED") in kinds
    assert rich.requirements.ambiguities


def test_missing_entities_fail_explicitly_and_qualification_may_be_absent(database):
    profile_id = ensure_user_profile(database, "errors@example.invalid").profile_id
    opportunity_id = _opportunity(database, None)
    database.execute(
        "DELETE FROM opportunity_qualifications WHERE opportunity_id = ?",
        (opportunity_id,),
    )
    database.commit()
    assert (
        load_opportunity_matching_input(database, opportunity_id).qualification is None
    )
    with pytest.raises(MatchingInputError, match="profile"):
        load_profile_matching_input(database, profile_id + 999)
    with pytest.raises(MatchingInputError, match="opportunity"):
        load_opportunity_matching_input(database, opportunity_id + 999)


def test_complete_foundation_is_read_only(database):
    profile_id = ensure_user_profile(database, "readonly@example.invalid").profile_id
    opportunity_id = _opportunity(database, "Not extracted")
    before = database.total_changes
    profile = load_profile_matching_input(database, profile_id)
    opportunity = load_opportunity_matching_input(database, opportunity_id)
    inputs = load_matching_input(database, profile_id, opportunity_id)
    fingerprint = matching_input_fingerprint(inputs)
    after = database.total_changes
    assert profile == inputs.profile
    assert opportunity == inputs.opportunity
    assert len(fingerprint) == 64
    assert (before, after) == (before, before)
