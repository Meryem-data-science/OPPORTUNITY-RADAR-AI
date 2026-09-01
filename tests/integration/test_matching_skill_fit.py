"""Phase 4.3 through real loaders and disposable SQLite only."""

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
    RequirementsState,
    SkillSignalKind,
    build_opportunity_skill_signals,
    build_skill_fit,
    load_matching_input,
    skill_fit_fingerprint,
)
from services.digital_twin.repository import ensure_user_profile


@pytest.fixture
def database(tmp_path):
    connection = connect_database(tmp_path / "skill-fit.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _seed_profile(connection):
    profile_id = ensure_user_profile(connection, "fit@example.invalid").profile_id
    for key, name in (("python", "Python"), ("apache airflow", "Apache Airflow"),
                      ("apache kafka", "Apache Kafka")):
        fact_id = connection.execute(
            "INSERT INTO profile_facts (profile_id, fact_type, value, status, decided_at) "
            "VALUES (?, 'SKILL', ?, 'ACCEPTED', '2099') RETURNING id",
            (profile_id, name),
        ).fetchone()[0]
        skill_id = connection.execute(
            "INSERT INTO skills (canonical_key, canonical_name) VALUES (?, ?) RETURNING id",
            (key, name),
        ).fetchone()[0]
        profile_skill_id = connection.execute(
            "INSERT INTO profile_skills (profile_id, skill_id) VALUES (?, ?) RETURNING id",
            (profile_id, skill_id),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO profile_skill_evidence "
            "(profile_skill_id, fact_id, normalizer_version, normalization_rule_id) "
            "VALUES (?, ?, 'skill-normalizer-v1', 'TEST')",
            (profile_skill_id, fact_id),
        )
    connection.commit()
    return profile_id


def _opportunity(connection, title, description):
    opportunity_id = connection.execute(
        "INSERT INTO opportunities "
        "(canonical_title, organization, description, discovered_at, first_seen_at, "
        "last_seen_at, source_url, status) VALUES (?, 'TEST', ?, 't', 't', 't', "
        "'https://example.invalid', 'new') RETURNING id",
        (title, description),
    ).fetchone()[0]
    connection.commit()
    return opportunity_id


def test_real_pipeline_extracted_unknown_and_read_only(database):
    profile_id = _seed_profile(database)
    extracted_id = _opportunity(
        database, "Python Data Engineer",
        "Required Qualifications\nPython and SQL\nPreferred Skills\nAirflow and dbt\n"
        "Responsibilities\nOperate Kafka pipelines.",
    )
    unknown_id = _opportunity(database, "SQL analyst", "Tech Stack\nPython")
    synchronize_opportunity_constraints(database)
    extract_one_opportunity_requirements(database, extracted_id)

    before = database.total_changes
    extracted_input = load_matching_input(database, profile_id, extracted_id)
    extracted_signals = build_opportunity_skill_signals(extracted_input.opportunity)
    extracted = build_skill_fit(extracted_input, extracted_signals)
    unknown_input = load_matching_input(database, profile_id, unknown_id)
    unknown_signals = build_opportunity_skill_signals(unknown_input.opportunity)
    unknown = build_skill_fit(unknown_input, unknown_signals)
    fingerprints = (
        skill_fit_fingerprint(extracted), skill_fit_fingerprint(unknown)
    )
    repeated = (
        skill_fit_fingerprint(build_skill_fit(extracted_input, extracted_signals)),
        skill_fit_fingerprint(build_skill_fit(unknown_input, unknown_signals)),
    )
    after = database.total_changes

    evaluations = {item.canonical_key: item for item in extracted.evaluations}
    assert evaluations["python"].kind is SkillSignalKind.REQUIRED
    assert evaluations["python"].matched
    assert not evaluations["sql"].matched
    assert evaluations["apache airflow"].kind is SkillSignalKind.PREFERRED
    assert evaluations["apache airflow"].matched
    assert not evaluations["dbt"].matched
    assert evaluations["apache kafka"].kind is SkillSignalKind.CONTEXT
    assert evaluations["apache kafka"].matched
    assert extracted.requirements_state is RequirementsState.EXTRACTED
    assert unknown.requirements_state is RequirementsState.UNKNOWN
    assert unknown.required_coverage.ratio is None
    assert fingerprints == repeated
    assert after == before
