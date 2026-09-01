"""Phase 4.4 through real loaders and disposable SQLite only."""

from dataclasses import replace

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.matching import (
    SemanticSimilarityInputError,
    build_profile_semantic_document,
    fit_tfidf_corpus,
    load_matching_input,
    load_opportunity_matching_input,
    load_profile_matching_input,
    score_matching_input_semantic_similarity,
    score_profile_against_tfidf_corpus,
)
from services.digital_twin.preferences.models import CareerObjectives
from services.digital_twin.preferences.service import set_profile_career_objectives
from services.digital_twin.preferences.repository import synchronize_profile_preferences
from services.digital_twin.repository import ensure_user_profile


@pytest.fixture
def database(tmp_path):
    connection = connect_database(tmp_path / "tfidf.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _fact(connection, profile_id, kind, value):
    return connection.execute(
        "INSERT INTO profile_facts (profile_id, fact_type, value, status, decided_at) "
        "VALUES (?, ?, ?, 'ACCEPTED', '2099') RETURNING id",
        (profile_id, kind, value),
    ).fetchone()[0]


def _seed_profile(connection):
    profile_id = ensure_user_profile(connection, "semantic@example.invalid").profile_id
    experience = _fact(connection, profile_id, "EXPERIENCE", "experience")
    project = _fact(connection, profile_id, "PROJECT", "project")
    education = _fact(connection, profile_id, "EDUCATION", "education")
    connection.execute(
        "INSERT INTO profile_experiences (profile_id, fact_id, role_text, description_text, "
        "structurer_version, structuring_rule_id) VALUES (?, ?, 'Data Engineer', "
        "'Built pipelines', 'v1', 'TEST')", (profile_id, experience)
    )
    connection.execute(
        "INSERT INTO profile_projects (profile_id, fact_id, title_text, description_text, "
        "structurer_version, structuring_rule_id) VALUES (?, ?, 'Radar', "
        "'Python models', 'v1', 'TEST')", (profile_id, project)
    )
    connection.execute(
        "INSERT INTO profile_educations (profile_id, fact_id, program_text, description_text, "
        "structurer_version, structuring_rule_id) VALUES (?, ?, 'Data MSc', "
        "'Analytics science', 'v1', 'TEST')", (profile_id, education)
    )
    skill = _fact(connection, profile_id, "SKILL", "LEAKED_STRUCTURED_SKILL")
    skill_id = connection.execute(
        "INSERT INTO skills (canonical_key, canonical_name) VALUES "
        "('leaked', 'LEAKED_STRUCTURED_SKILL') RETURNING id"
    ).fetchone()[0]
    profile_skill = connection.execute(
        "INSERT INTO profile_skills (profile_id, skill_id) VALUES (?, ?) RETURNING id",
        (profile_id, skill_id),
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO profile_skill_evidence (profile_skill_id, fact_id, normalizer_version, "
        "normalization_rule_id) VALUES (?, ?, 'v1', 'TEST')", (profile_skill, skill)
    )
    connection.commit()
    set_profile_career_objectives(
        connection, profile_id, CareerObjectives(("Lead data platforms",))
    )
    synchronize_profile_preferences(connection, profile_id)
    connection.commit()
    return profile_id


def _opportunity(connection, title, description):
    value = connection.execute(
        "INSERT INTO opportunities (canonical_title, organization, description, discovered_at, "
        "first_seen_at, last_seen_at, source_url, status) VALUES (?, 'EXCLUDED', ?, 't', "
        "'t', 't', 'https://example.invalid', 'new') RETURNING id",
        (title, description),
    ).fetchone()[0]
    connection.commit()
    return value


def test_load_fit_score_is_deterministic_aligned_and_read_only(database):
    profile_id = _seed_profile(database)
    first_id = _opportunity(database, "Data Engineer", "Python pipelines analytics")
    second_id = _opportunity(database, "Marketing", "Sales campaigns")
    before = database.total_changes

    profile = load_profile_matching_input(database, profile_id)
    opportunities = tuple(
        load_opportunity_matching_input(database, identity)
        for identity in (first_id, second_id)
    )
    corpus = fit_tfidf_corpus(opportunities)
    results = score_profile_against_tfidf_corpus(profile, corpus)
    repeated = score_profile_against_tfidf_corpus(profile, fit_tfidf_corpus(opportunities))
    pair = score_matching_input_semantic_similarity(
        load_matching_input(database, profile_id, first_id), corpus
    )
    after = database.total_changes

    text = build_profile_semantic_document(profile).text
    assert all(value in text for value in (
        "data engineer", "built pipelines", "radar", "python models",
        "data msc", "analytics science", "lead data platforms",
    ))
    assert "leaked_structured_skill" not in text
    assert results == repeated
    assert pair == next(item for item in results if item.opportunity_id == first_id)
    assert pair.similarity > 0
    assert after == before

    stale = replace(opportunities[0], description="changed snapshot")
    with pytest.raises(SemanticSimilarityInputError, match="snapshot"):
        score_matching_input_semantic_similarity(
            replace(load_matching_input(database, profile_id, first_id), opportunity=stale),
            corpus,
        )
