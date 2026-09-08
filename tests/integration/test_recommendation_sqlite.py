"""Phase 9A end to end, on disposable SQLite databases built by these tests.

Every database here is created under `tmp_path` and thrown away. Every posting,
every organization and every person is invented; **the operational database is
never opened**, no number taken from it appears below, and nothing in this file
writes to any database a person uses.

The corpus is built by the real upstream slices rather than by hand-written
rows: the coarse and fine classifications come from Phase 8's own persistence,
the location projection from Phase 7's own synchronization, the preferences from
the Digital Twin's own commands and the matching snapshot from Phase 4's own
`sync_matching`. A recommendation that only works against a fixture nobody else
produces is not a recommendation over this system's data.

The flow under test is:

    profile -> preferences -> matching snapshot -> Phase 8 fine classification
    -> Phase 7 location resolutions -> eligibility -> input assembly
    -> recommendation batch -> ranked results
"""

import hashlib

import pytest

from services.collector.database.connection import (
    connect_database,
    connect_readonly_database,
)
from services.collector.database.migrations import apply_migrations
from services.collector.extractors.opportunity_constraints.requirements.service import (
    synchronize_opportunity_requirements,
)
from services.collector.extractors.opportunity_constraints.service import (
    synchronize_opportunity_constraints,
)
from services.collector.matching import sync_matching
from services.collector.qualification.fine_taxonomy import FineCategory
from services.collector.qualification.persistence import persist_qualifications
from services.digital_twin.preferences.models import (
    CareerObjectives,
    MobilityPreference,
    MobilityScope,
    OpportunityPreferences,
    OpportunityType,
    WorkMode,
)
from services.digital_twin.preferences.repository import (
    synchronize_profile_preferences,
)
from services.digital_twin.preferences.service import (
    set_profile_career_objectives,
    set_profile_mobility,
    set_profile_preferences,
)
from services.digital_twin.skills.repository import synchronize_profile_skills
from services.digital_twin.repository import ensure_user_profile
from services.geography.service import synchronize_location_resolutions
from services.eligibility import ELIGIBILITY_ENGINE_VERSION
from services.recommendation import (
    ComponentStatus,
    DomainFitSource,
    EligibilitySignalStatus,
    GeographyState,
    RecommendationDisposition,
    RecommendationReadinessIssueCode,
    RecommendationReadinessStatus,
    assemble_recommendation_inputs,
    build_recommendation_batch,
)

# TEST ONLY values, invented for these tests.
TEST_ONLY_EMAIL = "recommendation-tests@example.invalid"
PREFERRED_DOMAINS = ("Data Science", "GenAI/LLM", "MLOps/ML Platform", "BI/Analytics")

#: A requirements section every posting carries, so Phase 3.5B extracts real
#: required skills and the required-skill component is actually available.
REQUIREMENTS = (
    "<h3>Required Qualifications</h3><ul><li>Strong Python skills</li>"
    "<li>SQL</li></ul><h3>Nice to have</h3><ul><li>Airflow</li></ul>"
)

#: title, description, location, and the eligibility verdict to record.
CORPUS = (
    (
        "strong",
        "TEST ONLY Data Scientist Internship",
        "Python, SQL and machine learning models for the data science team.",
        "Casablanca",
        "ELIGIBLE",
    ),
    (
        "unknown_eligibility",
        "TEST ONLY Generative AI Engineer Internship",
        "Large language models, RAG pipelines and prompt engineering with Python.",
        "Rabat",
        "UNKNOWN",
    ),
    (
        "out_of_target",
        "TEST ONLY Data Scientist Internship in Europe",
        "Python, SQL and machine learning models for the data science team.",
        "Paris, France",
        "ELIGIBLE",
    ),
    (
        "blocked",
        "TEST ONLY Business Intelligence Analyst Internship",
        "Power BI dashboards, KPI reporting and SQL.",
        "Casablanca",
        "INELIGIBLE",
    ),
    (
        "unbridged_fine",
        "TEST ONLY NLP Engineer Internship",
        "Natural language processing, named entity recognition and text classification.",
        "Casablanca",
        "ELIGIBLE",
    ),
)


def _seed_profile_skills(connection, profile_id, *skills):
    """Accept a SKILL fact per name, then project it the Digital Twin's own way.

    The facts are written directly because this file is not testing the CV
    review loop; everything downstream of them — normalization, the projection,
    the evidence — is the real repository's work.
    """
    for name in skills:
        connection.execute(
            """INSERT INTO profile_facts (profile_id, fact_type, value, status,
                                          decided_at)
               VALUES (?, 'SKILL', ?, 'ACCEPTED', '2099-01-01')""",
            (profile_id, name),
        )
    connection.commit()
    synchronize_profile_skills(connection, profile_id)


def _insert_opportunity(connection, index, title, description, location):
    return int(
        connection.execute(
            """INSERT INTO opportunities (
                   canonical_title, organization, location, description,
                   remote_type, discovered_at, first_seen_at, last_seen_at,
                   source_url, status, is_active
               ) VALUES (?, 'TEST ONLY Org', ?, ?, 'remote',
                         '2099-01-01', '2099-01-01', '2099-01-01', ?, 'new', 1)
               RETURNING id""",
            (
                title,
                location,
                f"{description} {REQUIREMENTS}",
                f"https://example.invalid/{index}",
            ),
        ).fetchone()[0]
    )


#: Counters `0014` requires to agree with each verdict: an ELIGIBLE decision
#: violated nothing and left no blocking question, an UNKNOWN one left exactly
#: the question it is named after, and an INELIGIBLE one contradicted a rule.
_ELIGIBILITY_COUNTS = {
    "ELIGIBLE": (1, 0, 0, 0),
    "UNKNOWN": (0, 0, 1, 1),
    "INELIGIBLE": (0, 1, 0, 0),
}


def _record_eligibility(connection, user_id, opportunity_id, status):
    satisfied, violated, unknown, blocking = _ELIGIBILITY_COUNTS[status]
    connection.execute(
        """INSERT INTO opportunity_eligibilities
           (user_id, opportunity_id, status, engine_version, input_fingerprint,
            satisfied_count, violated_count, unknown_count, not_applicable_count,
            not_evaluated_count, blocking_unknown_count, evaluated_at)
           VALUES (?,?,?,?,?,?,?,?,0,0,?,'2099-01-01')""",
        (
            user_id,
            opportunity_id,
            status,
            ELIGIBILITY_ENGINE_VERSION,
            f"{opportunity_id:x}".zfill(64),
            satisfied,
            violated,
            unknown,
            blocking,
        ),
    )


def build_corpus(tmp_path, *, eligibility=True, mobility=("Maroc",)):
    """Build the whole upstream chain, each slice through its own entry point."""
    path = tmp_path / "recommendation.db"
    connection = connect_database(path)
    apply_migrations(connection)
    # A decoy user, so the profile id and the user id differ and a test can
    # catch the one confusing the two: eligibility is stored per user, and
    # everything else in this phase is keyed by profile.
    connection.execute("INSERT INTO users(email) VALUES ('decoy@example.invalid')")
    connection.commit()
    identity = ensure_user_profile(connection, TEST_ONLY_EMAIL)
    ids = {}
    for index, (name, title, description, location, _) in enumerate(CORPUS):
        ids[name] = _insert_opportunity(
            connection, index, title, description, location
        )
    connection.commit()

    persist_qualifications(connection)
    synchronize_opportunity_constraints(connection)
    synchronize_opportunity_requirements(connection)
    _seed_profile_skills(connection, identity.profile_id, "Python", "SQL")

    set_profile_preferences(
        connection,
        identity.profile_id,
        OpportunityPreferences(
            opportunity_types=(OpportunityType.INTERNSHIP,),
            work_modes=(WorkMode.REMOTE,),
            preferred_domains=PREFERRED_DOMAINS,
        ),
    )
    set_profile_career_objectives(
        connection,
        identity.profile_id,
        CareerObjectives(
            objectives=(
                "Build machine learning models and data science pipelines in Python.",
            )
        ),
    )
    if mobility is not None:
        set_profile_mobility(
            connection,
            identity.profile_id,
            MobilityPreference(scope=MobilityScope.RESTRICTED, locations=mobility),
        )
    synchronize_profile_preferences(connection, identity.profile_id)

    synchronize_location_resolutions(connection)

    if eligibility:
        for name, _, _, _, status in CORPUS:
            _record_eligibility(connection, identity.user_id, ids[name], status)
        connection.commit()

    # Matching is synchronized last, so its snapshot describes the preferences
    # and the qualifications that are in the database right now.
    matching = sync_matching(connection, identity.profile_id)
    return connection, path, identity, ids, matching


@pytest.fixture
def corpus(tmp_path):
    connection, path, identity, ids, matching = build_corpus(tmp_path)
    yield connection, path, identity, ids, matching
    connection.close()


def by_id(batch):
    return {item.opportunity_id: item for item in batch.assessments}


def assemble_and_rank(connection, profile_id):
    assembly = assemble_recommendation_inputs(connection, profile_id)
    assert assembly.status is RecommendationReadinessStatus.READY, assembly.issues
    return assembly, build_recommendation_batch(
        profile_id,
        [record.recommendation_input for record in assembly.records],
    )


def test_the_whole_flow_assembles_ranks_and_explains(corpus):
    connection, _, identity, ids, matching = corpus
    assembly, batch = assemble_and_rank(connection, identity.profile_id)

    assert assembly.user_id == identity.user_id
    assert assembly.matching_run_id == matching.run_id
    assert identity.profile_id != identity.user_id
    assert len(assembly.records) == len(CORPUS) == batch.assessment_count
    assert {record.context.opportunity_id for record in assembly.records} == set(
        ids.values()
    )

    results = by_id(batch)
    strong = results[ids["strong"]]
    assert strong.disposition is RecommendationDisposition.RECOMMENDED
    assert strong.geography.state is GeographyState.MATCH
    assert strong.eligibility.status is EligibilitySignalStatus.ELIGIBLE
    assert strong.domain.source is DomainFitSource.FINE
    assert strong.domain.fine_primary_category is FineCategory.DATA_SCIENCE
    assert strong.domain.score == 1.0
    assert strong.required_skill.status is ComponentStatus.AVAILABLE
    assert strong.semantic.status is ComponentStatus.AVAILABLE
    assert strong.recommendation_evidence_coverage == 1.0
    assert 0.0 <= strong.recommendation_score <= 1.0
    assert strong.confirmed_gaps == ()

    uncertain = results[ids["unknown_eligibility"]]
    assert uncertain.disposition is RecommendationDisposition.UNCERTAIN
    assert uncertain.eligibility.status is EligibilitySignalStatus.UNKNOWN
    assert uncertain.geography.state is GeographyState.MATCH

    outside = results[ids["out_of_target"]]
    assert outside.disposition is RecommendationDisposition.OUTSIDE_PREFERENCES
    assert outside.geography.state is GeographyState.OUT_OF_TARGET
    assert outside.eligibility.status is EligibilitySignalStatus.ELIGIBLE

    blocked = results[ids["blocked"]]
    assert blocked.disposition is RecommendationDisposition.KNOWN_BLOCKER
    assert blocked.eligibility.status is EligibilitySignalStatus.INELIGIBLE

    order = [item.opportunity_id for item in batch.assessments]
    assert order.index(ids["strong"]) < order.index(ids["unknown_eligibility"])
    assert order.index(ids["unknown_eligibility"]) < order.index(ids["out_of_target"])
    assert order.index(ids["out_of_target"]) < order.index(ids["blocked"])


def test_an_unbridged_fine_category_falls_back_to_the_persisted_coarse_domain(corpus):
    connection, _, identity, ids, _ = corpus
    _, batch = assemble_and_rank(connection, identity.profile_id)
    result = by_id(batch)[ids["unbridged_fine"]]
    assert result.domain.fine_primary_category is FineCategory.NLP
    assert result.domain.source is DomainFitSource.COARSE
    # MACHINE_LEARNING_AI is not among the four preferred families, and the
    # coarse component says so; the fine half asserted nothing either way.
    assert result.domain.score == 0.0
    assert result.disposition is RecommendationDisposition.RECOMMENDED


def test_the_score_is_the_renormalized_mix_of_its_three_components(corpus):
    connection, _, identity, _, _ = corpus
    _, batch = assemble_and_rank(connection, identity.profile_id)
    for assessment in batch.assessments:
        components = (
            (assessment.required_skill.score, assessment.required_skill.base_weight),
            (assessment.semantic.score, assessment.semantic.base_weight),
            (assessment.domain.score, assessment.domain.base_weight),
        )
        available = sum(weight for score, weight in components if score is not None)
        expected = sum(
            score * weight for score, weight in components if score is not None
        )
        assert assessment.recommendation_evidence_coverage == pytest.approx(available)
        assert assessment.recommendation_score == pytest.approx(expected / available)
        # Exactly one domain component, carrying exactly the audited weight.
        assert assessment.domain.base_weight == 0.2
        assert (
            assessment.required_skill.base_weight,
            assessment.semantic.base_weight,
        ) == (0.5, 0.3)


def test_the_batch_is_reproducible_from_the_same_database(corpus):
    connection, _, identity, _, _ = corpus
    _, first = assemble_and_rank(connection, identity.profile_id)
    _, second = assemble_and_rank(connection, identity.profile_id)
    assert first.batch_fingerprint == second.batch_fingerprint
    assert [item.assessment_fingerprint for item in first.assessments] == [
        item.assessment_fingerprint for item in second.assessments
    ]


def test_the_whole_phase_writes_nothing_at_all(tmp_path):
    connection, path, identity, _, _ = build_corpus(tmp_path)
    connection.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with connect_readonly_database(path) as readonly:
        assembly, batch = assemble_and_rank(readonly, identity.profile_id)
        tables = {
            row[0]
            for row in readonly.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert batch.assessment_count == len(CORPUS)
    assert not any(name.startswith("recommendation") for name in tables)
    assert assembly.status is RecommendationReadinessStatus.READY


def test_a_missing_eligibility_snapshot_is_carried_not_invented(tmp_path):
    connection, _, identity, ids, _ = build_corpus(tmp_path, eligibility=False)
    _, batch = assemble_and_rank(connection, identity.profile_id)
    connection.close()
    for assessment in batch.assessments:
        assert assessment.eligibility.status is EligibilitySignalStatus.MISSING
        assert assessment.disposition in (
            RecommendationDisposition.UNCERTAIN,
            RecommendationDisposition.OUTSIDE_PREFERENCES,
        )
        assert assessment.disposition is not RecommendationDisposition.KNOWN_BLOCKER


def test_an_open_mobility_is_neutral_across_the_whole_corpus(tmp_path):
    connection, _, identity, ids, _ = build_corpus(tmp_path, mobility=None)
    set_profile_mobility(
        connection,
        identity.profile_id,
        MobilityPreference(scope=MobilityScope.OPEN, locations=()),
    )
    synchronize_profile_preferences(connection, identity.profile_id)
    _, batch = assemble_and_rank(connection, identity.profile_id)
    connection.close()
    results = by_id(batch)
    for assessment in batch.assessments:
        assert assessment.geography.state is GeographyState.NOT_APPLICABLE
    # The posting in France is no longer outside a preference nobody stated.
    assert (
        results[ids["out_of_target"]].disposition
        is RecommendationDisposition.RECOMMENDED
    )


def test_a_stale_matching_snapshot_stops_the_cohort_instead_of_being_repaired(
    tmp_path,
):
    connection, _, identity, _, _ = build_corpus(tmp_path)
    # The person reorders their preferred domains after Matching was persisted.
    set_profile_preferences(
        connection,
        identity.profile_id,
        OpportunityPreferences(
            opportunity_types=(OpportunityType.INTERNSHIP,),
            work_modes=(WorkMode.REMOTE,),
            preferred_domains=tuple(reversed(PREFERRED_DOMAINS)),
        ),
    )
    synchronize_profile_preferences(connection, identity.profile_id)
    assembly = assemble_recommendation_inputs(connection, identity.profile_id)
    connection.close()
    assert assembly.status is RecommendationReadinessStatus.INCOMPLETE
    assert assembly.records == ()
    assert {issue.code for issue in assembly.issues} == {
        RecommendationReadinessIssueCode.STALE_MATCHING_SNAPSHOT
    }


def test_a_cohort_that_grew_since_the_matching_run_is_refused(tmp_path):
    connection, _, identity, _, _ = build_corpus(tmp_path)
    _insert_opportunity(
        connection,
        99,
        "TEST ONLY Machine Learning Engineer Internship",
        "Model training, feature engineering and scikit-learn.",
        "Casablanca",
    )
    connection.commit()
    persist_qualifications(connection)
    assembly = assemble_recommendation_inputs(connection, identity.profile_id)
    connection.close()
    assert assembly.status is RecommendationReadinessStatus.INCOMPLETE
    assert [issue.code for issue in assembly.issues] == [
        RecommendationReadinessIssueCode.STALE_MATCHING_COHORT
    ]


def test_the_recommendation_never_contradicts_its_own_baseline_provenance(corpus):
    connection, _, identity, _, _ = corpus
    assembly, batch = assemble_and_rank(connection, identity.profile_id)
    persisted = {
        item.opportunity_id: item
        for record in assembly.records
        for item in [record.recommendation_input.matching]
    }
    for assessment in batch.assessments:
        snapshot = persisted[assessment.opportunity_id]
        assert (
            assessment.baseline_matching_assessment_fingerprint
            == snapshot.assessment_fingerprint
        )
        assert assessment.baseline_match_quality == snapshot.match_quality
        assert assessment.baseline_evidence_coverage == snapshot.evidence_coverage
        assert assessment.baseline_matching_lane is snapshot.lane
