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
import json

import pytest
from sklearn.feature_extraction.text import TfidfVectorizer

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
from services.collector.matching import (
    MATCHING_SELECTION_VERSION,
    SEMANTIC_BINDING_VERSION,
    audit_matching_profile_history,
    load_opportunity_matching_input,
    matching_run_fingerprint,
    read_current_matching,
    select_matching_opportunity_ids,
    sync_matching,
)
from services.collector.matching.fingerprint import canonical_json
from services.collector.qualification.classifier import CLASSIFIER_VERSION
from services.collector.qualification.fine_classifier import FINE_CLASSIFIER_VERSION
from services.collector.qualification.fine_read_model import (
    UNCLASSIFIED,
    decode_fine_classification,
)
from services.collector.qualification.fine_taxonomy import FineCategory
from services.collector.qualification.persistence import persist_qualifications
from services.digital_twin.preferences.models import (
    CareerObjectives,
    ConventionStatus,
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
from services.collector.matching.skill_signals import SkillSignalKind, SkillSignalSource
from services.eligibility.models import Dimension, ReasonCode, RuleStatus
from services.eligibility.repository import read_eligibility, read_rule_results
from services.eligibility.service import synchronize_eligibility
from services.recommendation import (
    ComponentStatus,
    DomainFitSource,
    EligibilitySignalStatus,
    GeographyState,
    RecommendationDisposition,
    RecommendationReadinessIssueCode,
    RecommendationReadinessStatus,
    RecommendationReasonCode,
    assemble_recommendation_inputs,
    build_recommendation_batch,
    current_semantic_binding_fingerprint,
    current_semantic_corpus_fingerprint,
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

#: Two sentences that make Phase 3.6 answer something other than ELIGIBLE, by
#: giving its rules something real to read. Neither is a fixture verdict: the
#: eligibility engine runs for itself below and decides.
GERMAN_REQUIREMENT = (
    "<h3>Required Qualifications</h3><ul><li>Fluent German required</li></ul>"
)
CONVENTION_REQUIREMENT = "Convention de stage obligatoire."

#: name, title, description, location.
CORPUS = (
    (
        "strong",
        "TEST ONLY Data Scientist Internship",
        "Python, SQL and machine learning models for the data science team.",
        "Casablanca",
    ),
    (
        # A language nobody stated a level for: Phase 3.6 answers UNKNOWN, which
        # is a question and never a soft refusal.
        "unknown_eligibility",
        "TEST ONLY Generative AI Engineer Internship",
        "Large language models, RAG pipelines and prompt engineering with Python. "
        + GERMAN_REQUIREMENT,
        "Rabat",
    ),
    (
        "out_of_target",
        "TEST ONLY Data Scientist Internship in Europe",
        "Python, SQL and machine learning models for the data science team.",
        "Paris, France",
    ),
    (
        # The profile below states it cannot obtain a convention, and this
        # posting demands one: a real VIOLATED hard rule, so a real INELIGIBLE.
        "blocked",
        "TEST ONLY Business Intelligence Analyst Internship",
        "Power BI dashboards, KPI reporting and SQL. " + CONVENTION_REQUIREMENT,
        "Casablanca",
    ),
    (
        "unbridged_fine",
        "TEST ONLY NLP Engineer Internship",
        "Natural language processing, named entity recognition and text classification.",
        "Casablanca",
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
    for index, (name, title, description, location) in enumerate(CORPUS):
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
            # Stated, not inferred, and it is what makes the convention-demanding
            # posting a real blocker rather than a fixture decision.
            convention_status=ConventionStatus.NOT_AVAILABLE,
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
        # Phase 3.6 decides for itself, with its own engine and its own digest.
        # Nothing here writes a verdict or a fingerprint: a hand-written
        # decision would be exactly the stale snapshot the assembly now refuses.
        synchronize_eligibility(connection, identity.user_id, identity.profile_id)

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
            # Everything Eligibility reads is left exactly as it was, so the
            # only thing that went stale is the Matching alignment.
            convention_status=ConventionStatus.NOT_AVAILABLE,
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


# --------------------------------------------------------------------------
# Eligibility freshness: a stored verdict is only usable while it still
# describes the inputs it was given about
# --------------------------------------------------------------------------


def _restate_preferences(connection, profile_id, **overrides):
    """Restate the whole preference row, changing only what is named."""
    values = dict(
        opportunity_types=(OpportunityType.INTERNSHIP,),
        work_modes=(WorkMode.REMOTE,),
        preferred_domains=PREFERRED_DOMAINS,
        convention_status=ConventionStatus.NOT_AVAILABLE,
    )
    values.update(overrides)
    set_profile_preferences(connection, profile_id, OpportunityPreferences(**values))
    synchronize_profile_preferences(connection, profile_id)


def test_a_profile_input_change_without_an_eligibility_resync_stops_the_cohort(
    tmp_path,
):
    connection, _, identity, _, _ = build_corpus(tmp_path)
    # The person says they can obtain a convention after all. Phase 3.6 read
    # that field to decide, so every stored verdict now answers a question that
    # was asked about a different profile — including the INELIGIBLE one.
    _restate_preferences(
        connection,
        identity.profile_id,
        convention_status=ConventionStatus.AVAILABLE,
    )
    stale = assemble_recommendation_inputs(connection, identity.profile_id)
    assert stale.status is RecommendationReadinessStatus.INCOMPLETE
    assert stale.records == ()
    assert {issue.code for issue in stale.issues} == {
        RecommendationReadinessIssueCode.ELIGIBILITY_SNAPSHOT_STALE
    }

    # Resynchronizing Eligibility — on this temporary database, by the phase
    # that owns the decision — is what makes it readable again. The assembly
    # itself repaired nothing.
    synchronize_eligibility(connection, identity.user_id, identity.profile_id)
    ready, batch = assemble_and_rank(connection, identity.profile_id)
    connection.close()
    assert ready.status is RecommendationReadinessStatus.READY
    assert batch.assessment_count == len(CORPUS)
    # The blocker was a blocker of the old profile, and is gone with it.
    assert all(
        item.disposition is not RecommendationDisposition.KNOWN_BLOCKER
        for item in batch.assessments
    )


def test_an_opportunity_input_change_without_an_eligibility_resync_stops_it_too(
    tmp_path,
):
    connection, _, identity, ids, _ = build_corpus(tmp_path)
    # A language requirement is read by Phase 3.6 and by nothing in Matching, so
    # dropping it isolates the eligibility half of the freshness contract.
    connection.execute(
        "DELETE FROM opportunity_language_requirements WHERE opportunity_id = ?",
        (ids["unknown_eligibility"],),
    )
    connection.commit()
    assembly = assemble_recommendation_inputs(connection, identity.profile_id)
    connection.close()
    assert assembly.status is RecommendationReadinessStatus.INCOMPLETE
    assert assembly.records == ()
    assert [issue.code for issue in assembly.issues] == [
        RecommendationReadinessIssueCode.ELIGIBILITY_SNAPSHOT_STALE
    ]
    assert assembly.issues[0].opportunity_id == ids["unknown_eligibility"]


def test_a_decision_whose_inputs_can_no_longer_be_assembled_stops_the_cohort(
    tmp_path,
):
    """The Phase 3.5A reading a stored decision was taken from is gone.

    Foreign keys make this state unreachable by an ordinary delete — dropping
    `opportunity_constraints` cascades into everything Matching read as well,
    and the skill-fit check would notice first. It is reached here with
    enforcement off because the guard exists for the database that arrives
    without it: a restored backup, a hand-repaired row. The old verdict is not
    evidence about a question nobody can ask any more.
    """
    connection, _, identity, ids, _ = build_corpus(tmp_path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        "DELETE FROM opportunity_constraints WHERE opportunity_id = ?",
        (ids["strong"],),
    )
    connection.commit()
    connection.execute("PRAGMA foreign_keys = ON")
    assembly = assemble_recommendation_inputs(connection, identity.profile_id)
    connection.close()
    assert assembly.status is RecommendationReadinessStatus.INCOMPLETE
    assert assembly.records == ()
    assert [issue.code for issue in assembly.issues] == [
        RecommendationReadinessIssueCode.ELIGIBILITY_INPUT_INCOMPLETE
    ]
    assert assembly.issues[0].opportunity_id == ids["strong"]


def test_the_persisted_eligibility_reasons_are_exposed_without_re_evaluation(corpus):
    connection, _, identity, ids, _ = corpus
    _, batch = assemble_and_rank(connection, identity.profile_id)
    results = by_id(batch)

    blocked = results[ids["blocked"]]
    convention = [
        item
        for item in blocked.eligibility_evidence
        if item.dimension is Dimension.CONVENTION
    ]
    assert [item.status for item in convention] == [RuleStatus.VIOLATED]
    assert convention[0].is_blocking is True
    assert convention[0].reason_code is ReasonCode.CONVENTION_VIOLATED
    assert convention[0].explanation.strip()

    uncertain = results[ids["unknown_eligibility"]]
    language = [
        item
        for item in uncertain.eligibility_evidence
        if item.dimension is Dimension.LANGUAGE
    ]
    assert [item.status for item in language] == [RuleStatus.UNKNOWN]
    assert language[0].reason_code is ReasonCode.LANGUAGE_PROFILE_UNKNOWN
    # An UNKNOWN rule is a question about the profile, not a violation.
    assert language[0].status is not RuleStatus.VIOLATED

    # Every exposed row is one Phase 3.6 stored; none was produced here.
    persisted = read_rule_results(
        connection,
        read_eligibility(connection, identity.user_id, ids["blocked"]).id,
    )
    assert [item.rule_code for item in blocked.eligibility_evidence] == [
        item.rule_code for item in persisted
    ]


def test_a_missing_decision_carries_no_reasons_and_no_blocker(tmp_path):
    connection, _, identity, _, _ = build_corpus(tmp_path, eligibility=False)
    _, batch = assemble_and_rank(connection, identity.profile_id)
    connection.close()
    for assessment in batch.assessments:
        assert assessment.eligibility.status is EligibilitySignalStatus.MISSING
        assert assessment.eligibility_evidence == ()
        assert assessment.disposition is not RecommendationDisposition.KNOWN_BLOCKER


# --------------------------------------------------------------------------
# Geography freshness: a verdict is never read off a projection of another
# string, or of another resolver
# --------------------------------------------------------------------------


def test_a_location_edited_without_a_geography_resync_stops_the_cohort(tmp_path):
    connection, _, identity, ids, _ = build_corpus(tmp_path)
    # The posting now says Paris. The projection still says Casablanca, and
    # answering MATCH off it would be answering about a string nobody wrote.
    connection.execute(
        "UPDATE opportunity_constraint_locations SET location_text = ? "
        "WHERE opportunity_id = ?",
        ("Paris, France", ids["strong"]),
    )
    connection.commit()
    assembly = assemble_recommendation_inputs(connection, identity.profile_id)
    connection.close()
    assert assembly.status is RecommendationReadinessStatus.INCOMPLETE
    assert assembly.records == ()
    assert [issue.code for issue in assembly.issues] == [
        RecommendationReadinessIssueCode.GEOGRAPHY_PROJECTION_STALE
    ]
    assert assembly.issues[0].opportunity_id == ids["strong"]


def test_a_projection_stored_under_another_resolver_version_stops_the_cohort(
    tmp_path,
):
    connection, _, identity, ids, _ = build_corpus(tmp_path)
    connection.execute(
        "UPDATE opportunity_location_resolutions SET resolver_version = ? "
        "WHERE opportunity_id = ?",
        ("geographic-resolver-v0", ids["out_of_target"]),
    )
    connection.commit()
    assembly = assemble_recommendation_inputs(connection, identity.profile_id)
    connection.close()
    assert [issue.code for issue in assembly.issues] == [
        RecommendationReadinessIssueCode.GEOGRAPHY_PROJECTION_STALE
    ]
    # Above all: the posting in France did not reach a result as OUT_OF_TARGET.
    assert assembly.records == ()


def test_an_orphaned_projection_row_stops_the_cohort(tmp_path):
    """The location row is gone; its segments are not.

    `0024` cascades, so this too is only reachable with enforcement off — and
    that is exactly the database the check is for. A verdict read off these
    segments would be read off a location the posting no longer lists.
    """
    connection, _, identity, ids, _ = build_corpus(tmp_path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        "DELETE FROM opportunity_constraint_locations WHERE opportunity_id = ?",
        (ids["out_of_target"],),
    )
    connection.commit()
    connection.execute("PRAGMA foreign_keys = ON")
    assembly = assemble_recommendation_inputs(connection, identity.profile_id)
    connection.close()
    assert [issue.code for issue in assembly.issues] == [
        RecommendationReadinessIssueCode.GEOGRAPHY_PROJECTION_STALE
    ]


def test_a_posting_with_no_location_at_all_is_ready_and_geographically_unknown(
    tmp_path,
):
    connection, _, identity, ids, _ = build_corpus(tmp_path)
    # Neither a source nor a projection: nothing to be stale about, and the
    # evaluator's honest answer is UNKNOWN.
    connection.execute(
        "DELETE FROM opportunity_location_resolutions WHERE opportunity_id = ?",
        (ids["out_of_target"],),
    )
    connection.execute(
        "DELETE FROM opportunity_constraint_locations WHERE opportunity_id = ?",
        (ids["out_of_target"],),
    )
    connection.commit()
    assembly, batch = assemble_and_rank(connection, identity.profile_id)
    connection.close()
    assert assembly.status is RecommendationReadinessStatus.READY
    silent = by_id(batch)[ids["out_of_target"]]
    assert silent.geography.state is GeographyState.UNKNOWN
    assert silent.geography.resolved_countries == ()
    assert silent.disposition is RecommendationDisposition.UNCERTAIN


def test_a_current_projection_carries_its_countries_into_the_explanation(corpus):
    connection, _, identity, ids, _ = corpus
    _, batch = assemble_and_rank(connection, identity.profile_id)
    results = by_id(batch)
    assert results[ids["strong"]].geography.resolved_countries == ("MA",)
    assert results[ids["out_of_target"]].geography.resolved_countries == ("FR",)


# --------------------------------------------------------------------------
# Matching persistence integrity
# --------------------------------------------------------------------------


def test_a_corrupted_current_matching_assessment_stops_the_cohort(tmp_path):
    connection, _, identity, ids, matching = build_corpus(tmp_path)
    # The stored column and the stored payload now disagree. Phase 4's own
    # audit is what notices; Phase 9 does not re-derive its rules.
    connection.execute(
        "UPDATE matching_assessments SET match_quality = ? "
        "WHERE run_id = ? AND opportunity_id = ?",
        (0.123456, matching.run_id, ids["strong"]),
    )
    connection.commit()
    assembly = assemble_recommendation_inputs(connection, identity.profile_id)
    connection.close()
    assert assembly.status is RecommendationReadinessStatus.INCOMPLETE
    assert assembly.records == ()
    assert [issue.code for issue in assembly.issues] == [
        RecommendationReadinessIssueCode.MATCHING_PERSISTENCE_INVALID
    ]
    assert str(matching.run_id) in assembly.issues[0].message


def test_a_tampered_current_assessment_fingerprint_stops_the_cohort(tmp_path):
    connection, _, identity, ids, matching = build_corpus(tmp_path)
    connection.execute(
        "UPDATE matching_assessments SET assessment_fingerprint = ? "
        "WHERE run_id = ? AND opportunity_id = ?",
        ("0" * 64, matching.run_id, ids["blocked"]),
    )
    connection.commit()
    assembly = assemble_recommendation_inputs(connection, identity.profile_id)
    connection.close()
    assert [issue.code for issue in assembly.issues] == [
        RecommendationReadinessIssueCode.MATCHING_PERSISTENCE_INVALID
    ]


# --------------------------------------------------------------------------
# Skill provenance
# --------------------------------------------------------------------------


def test_the_assessment_names_each_skill_and_whether_the_profile_confirms_it(
    corpus,
):
    connection, _, identity, ids, _ = corpus
    _, batch = assemble_and_rank(connection, identity.profile_id)
    strong = by_id(batch)[ids["strong"]]
    by_key = {item.canonical_key: item for item in strong.skill_evidence}

    assert by_key["python"].canonical_name == "Python"
    assert by_key["python"].kind is SkillSignalKind.REQUIRED
    assert by_key["python"].confirmed_in_profile is True
    assert by_key["python"].profile_normalizer_versions == ("skill-normalizer-v1",)
    assert SkillSignalSource.REQUIREMENTS in by_key["python"].sources

    assert by_key["sql"].confirmed_in_profile is True

    # Signalled by the posting, not confirmed by the profile's facts. That is a
    # question about the profile, and it is never a confirmed gap.
    airflow = by_key["apache airflow"]
    assert airflow.kind is SkillSignalKind.PREFERRED
    assert airflow.confirmed_in_profile is False
    assert airflow.profile_normalizer_versions == ()
    assert strong.confirmed_gaps == ()

    # And the number is still the persisted one, not this recomputation.
    assert strong.required_skill.matched_count == 2
    assert strong.required_skill.total_count == 2
    assert len(strong.required_skill.upstream_fingerprint) == 64


def test_a_profile_skill_change_without_a_matching_resync_stops_the_cohort(tmp_path):
    # No stored eligibility, so nothing else can go stale at the same time and
    # mask what this test is about.
    connection, _, identity, _, _ = build_corpus(tmp_path, eligibility=False)
    # Airflow is signalled by every posting and was not confirmed when Matching
    # ran; a review has accepted it since. The persisted required-skill
    # component no longer describes the profile it was computed against.
    _seed_profile_skills(connection, identity.profile_id, "Apache Airflow")
    assembly = assemble_recommendation_inputs(connection, identity.profile_id)
    connection.close()
    assert assembly.status is RecommendationReadinessStatus.INCOMPLETE
    assert assembly.records == ()
    assert {issue.code for issue in assembly.issues} == {
        RecommendationReadinessIssueCode.STALE_MATCHING_SKILL_FIT
    }


# --------------------------------------------------------------------------
# Declared user constraints
# --------------------------------------------------------------------------


def test_declared_constraints_are_shown_read_and_never_interpreted(tmp_path):
    connection, _, identity, _, _ = build_corpus(tmp_path)
    _, before = assemble_and_rank(connection, identity.profile_id)
    baseline = by_id(before)

    _restate_preferences(
        connection,
        identity.profile_id,
        constraints=("TEST ONLY contrainte declaree",),
    )
    # Eligibility does not read `constraints`, and neither does Matching, so
    # nothing upstream went stale by stating one.
    _, after = assemble_and_rank(connection, identity.profile_id)
    connection.close()

    for assessment in after.assessments:
        assert assessment.declared_constraints == ("TEST ONLY contrainte declaree",)
        assert (
            RecommendationReasonCode.USER_CONSTRAINTS_NOT_AUTOMATICALLY_EVALUATED
            in assessment.unknowns
        )
        reference = baseline[assessment.opportunity_id]
        # Read and shown: not scored, not routed, and not guessed at.
        assert assessment.recommendation_score == reference.recommendation_score
        assert (
            assessment.recommendation_evidence_coverage
            == reference.recommendation_evidence_coverage
        )
        assert assessment.disposition is reference.disposition
        # But the explanation changed, so the digest has to change with it.
        assert assessment.assessment_fingerprint != reference.assessment_fingerprint
    assert after.batch_fingerprint != before.batch_fingerprint


# --------------------------------------------------------------------------
# Phase 8 qualification freshness: a domain is never read off a classification
# of text the posting no longer carries
# --------------------------------------------------------------------------


def test_an_edited_posting_without_a_qualification_resync_stops_the_cohort(tmp_path):
    connection, _, identity, ids, _ = build_corpus(tmp_path)
    # A Phase 8 input moved. The stored row still says CORE_TARGET / DATA_SCIENCE
    # about a description nobody would classify that way now, and the domain
    # component would be read straight off it.
    connection.execute(
        "UPDATE opportunities SET description = ? WHERE id = ?",
        ("A completely different posting about warehouse logistics.", ids["strong"]),
    )
    connection.commit()
    assembly = assemble_recommendation_inputs(connection, identity.profile_id)
    connection.close()
    assert assembly.status is RecommendationReadinessStatus.INCOMPLETE
    assert assembly.records == ()
    assert [issue.code for issue in assembly.issues] == [
        RecommendationReadinessIssueCode.QUALIFICATION_PROJECTION_STALE
    ]
    assert assembly.issues[0].opportunity_id == ids["strong"]
    assert "other opportunity fields" in assembly.issues[0].message


@pytest.mark.parametrize(
    "title_change", [True, False], ids=["canonical_title", "description"]
)
def test_any_phase_8_input_field_moving_is_enough_to_stop_the_cohort(
    tmp_path, title_change
):
    connection, _, identity, ids, _ = build_corpus(tmp_path)
    column = "canonical_title" if title_change else "description"
    connection.execute(
        f"UPDATE opportunities SET {column} = {column} || ? WHERE id = ?",
        (" (updated)", ids["unknown_eligibility"]),
    )
    connection.commit()
    assembly = assemble_recommendation_inputs(connection, identity.profile_id)
    connection.close()
    assert [issue.code for issue in assembly.issues] == [
        RecommendationReadinessIssueCode.QUALIFICATION_PROJECTION_STALE
    ]


def test_a_stale_coarse_classifier_version_stops_the_cohort(tmp_path):
    connection, _, identity, ids, _ = build_corpus(tmp_path)
    connection.execute(
        "UPDATE opportunity_qualifications SET classifier_version = ? "
        "WHERE opportunity_id = ?",
        ("qualification-rules-v1", ids["blocked"]),
    )
    connection.commit()
    assembly = assemble_recommendation_inputs(connection, identity.profile_id)
    connection.close()
    assert assembly.status is RecommendationReadinessStatus.INCOMPLETE
    assert assembly.records == ()
    assert [issue.code for issue in assembly.issues] == [
        RecommendationReadinessIssueCode.QUALIFICATION_PROJECTION_STALE
    ]
    assert CLASSIFIER_VERSION in assembly.issues[0].message


def test_a_stale_fine_classifier_version_stops_the_cohort(tmp_path):
    connection, _, identity, ids, _ = build_corpus(tmp_path)
    connection.execute(
        "UPDATE opportunity_qualifications SET fine_classifier_version = ? "
        "WHERE opportunity_id = ?",
        ("fine-data-ai-rules-v1", ids["strong"]),
    )
    connection.commit()
    assembly = assemble_recommendation_inputs(connection, identity.profile_id)
    connection.close()
    assert [issue.code for issue in assembly.issues] == [
        RecommendationReadinessIssueCode.QUALIFICATION_PROJECTION_STALE
    ]
    assert FINE_CLASSIFIER_VERSION in assembly.issues[0].message


def test_the_legacy_null_fine_version_is_a_valid_public_read_and_a_stale_input(
    tmp_path,
):
    """The two questions are different, and this pins both answers on one row.

    `fine_read_model` reports a NULL fine classifier version as the documented
    "never fine-classified" state, because that is a true statement about the
    row and the public contract depends on it. Recommendation readiness asks a
    stricter question — *is this a current Phase 8 projection* — and a row
    nothing has fine-classified is not one.
    """
    connection, _, identity, ids, _ = build_corpus(tmp_path)
    connection.execute(
        """UPDATE opportunity_qualifications
              SET fine_primary_category = NULL,
                  fine_secondary_categories_json = NULL,
                  fine_category_evidence_json = NULL,
                  fine_reasons_json = NULL,
                  fine_classifier_version = NULL
            WHERE opportunity_id = ?""",
        (ids["strong"],),
    )
    connection.commit()

    # The public read model still answers, exactly as Phase 8C documents.
    row = connection.execute(
        """SELECT qualification, fine_primary_category,
                  fine_secondary_categories_json, fine_category_evidence_json,
                  fine_reasons_json, fine_classifier_version
             FROM opportunity_qualifications WHERE opportunity_id = ?""",
        (ids["strong"],),
    ).fetchone()
    assert decode_fine_classification(*row) is UNCLASSIFIED

    # Recommendation does not, and says why.
    assembly = assemble_recommendation_inputs(connection, identity.profile_id)
    connection.close()
    assert assembly.status is RecommendationReadinessStatus.INCOMPLETE
    assert assembly.records == ()
    assert [issue.code for issue in assembly.issues] == [
        RecommendationReadinessIssueCode.QUALIFICATION_PROJECTION_STALE
    ]
    assert "not recorded" in assembly.issues[0].message


def test_a_valid_unbridgeable_fine_category_still_uses_the_coarse_fallback(corpus):
    """Stale provenance is refused; the fine-or-coarse fallback is untouched."""
    connection, _, identity, ids, _ = corpus
    assembly, batch = assemble_and_rank(connection, identity.profile_id)
    result = by_id(batch)[ids["unbridged_fine"]]
    assert assembly.status is RecommendationReadinessStatus.READY
    assert result.domain.fine_primary_category is FineCategory.NLP
    assert result.domain.fine_classifier_version == FINE_CLASSIFIER_VERSION
    assert result.domain.source is DomainFitSource.COARSE


# --------------------------------------------------------------------------
# Matching semantic corpus freshness: the same ids over different documents
# --------------------------------------------------------------------------

#: Prose carrying no catalogue skill, no requirement heading and no Data/AI
#: signal, so it moves the semantic document and nothing else.
NEUTRAL_SENTENCE = " Cette annonce a ete relue par notre equipe interne."


def test_a_content_edit_the_cohort_ids_cannot_see_stops_the_cohort(tmp_path):
    connection, _, identity, ids, matching = build_corpus(tmp_path)
    before = set(select_matching_opportunity_ids(connection))

    connection.execute(
        "UPDATE opportunities SET description = description || ? WHERE id = ?",
        (NEUTRAL_SENTENCE, ids["strong"]),
    )
    connection.commit()
    # Phase 8 is brought back to current, so its freshness check passes and this
    # test is about the semantic corpus alone.
    persist_qualifications(connection)
    # Matching is deliberately NOT resynchronized.

    assert set(select_matching_opportunity_ids(connection)) == before
    stale = assemble_recommendation_inputs(connection, identity.profile_id)
    assert stale.status is RecommendationReadinessStatus.INCOMPLETE
    assert stale.records == ()
    assert [issue.code for issue in stale.issues] == [
        RecommendationReadinessIssueCode.STALE_MATCHING_SEMANTIC_CORPUS
    ]

    # Resynchronizing Matching — on this temporary database, by the phase that
    # owns the corpus — is what makes it readable again.
    resynced = sync_matching(connection, identity.profile_id)
    ready, batch = assemble_and_rank(connection, identity.profile_id)
    connection.close()
    assert resynced.run_id != matching.run_id
    assert ready.status is RecommendationReadinessStatus.READY
    assert batch.assessment_count == len(CORPUS)


def test_the_qualification_and_corpus_edits_are_independent_signals(tmp_path):
    """Without the Phase 8 resync, the same edit is caught one step earlier."""
    connection, _, identity, ids, _ = build_corpus(tmp_path)
    connection.execute(
        "UPDATE opportunities SET description = description || ? WHERE id = ?",
        (NEUTRAL_SENTENCE, ids["strong"]),
    )
    connection.commit()
    assembly = assemble_recommendation_inputs(connection, identity.profile_id)
    connection.close()
    assert [issue.code for issue in assembly.issues] == [
        RecommendationReadinessIssueCode.QUALIFICATION_PROJECTION_STALE
    ]


def test_the_assembly_fits_no_tfidf_and_scores_no_similarity(tmp_path, monkeypatch):
    """The corpus proof is a document digest, not a model.

    Every entry point that would fit a vectorizer or score a similarity is
    replaced by something that fails loudly, and the whole assembly still runs
    to READY — including the new corpus fingerprint check.
    """
    connection, _, identity, _, _ = build_corpus(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("recommendation assembly must not fit or score TF-IDF")

    for target in (
        "services.collector.matching.tfidf_similarity.fit_tfidf_corpus",
        "services.collector.matching.tfidf_similarity.score_profile_against_tfidf_corpus",
        "services.collector.matching.tfidf_similarity.score_matching_input_semantic_similarity",
    ):
        monkeypatch.setattr(target, forbidden)
    monkeypatch.setattr(TfidfVectorizer, "fit", forbidden)
    monkeypatch.setattr(TfidfVectorizer, "fit_transform", forbidden)
    monkeypatch.setattr(TfidfVectorizer, "transform", forbidden)

    assembly, batch = assemble_and_rank(connection, identity.profile_id)
    connection.close()
    assert assembly.status is RecommendationReadinessStatus.READY
    assert batch.assessment_count == len(CORPUS)


def test_the_corpus_fingerprint_is_the_one_matching_persisted(corpus):
    connection, _, identity, ids, matching = corpus
    inputs = tuple(
        load_opportunity_matching_input(connection, opportunity_id)
        for opportunity_id in select_matching_opportunity_ids(connection)
    )
    run = read_current_matching(connection, identity.profile_id).current_run
    assert current_semantic_corpus_fingerprint(inputs) == run.corpus_fingerprint
    assert run.corpus_fingerprint == matching.corpus_fingerprint


# --------------------------------------------------------------------------
# Semantic identity binding: same cohort, same corpus content, swapped
# documents. The case an ID-free content digest cannot see.
# --------------------------------------------------------------------------


def _semantic_text(connection, opportunity_id):
    return connection.execute(
        "SELECT canonical_title, description FROM opportunities WHERE id = ?",
        (opportunity_id,),
    ).fetchone()


def _swap_semantic_text(connection, first, second):
    """Exchange two postings' title and description, and nothing else.

    Every other column stays put, so the corpus is the same multiset of
    documents and the cohort is the same set of ids — only the assignment
    between them moves.
    """
    left, right = _semantic_text(connection, first), _semantic_text(connection, second)
    for opportunity_id, (title, description) in (
        (first, right),
        (second, left),
    ):
        connection.execute(
            "UPDATE opportunities SET canonical_title = ?, description = ? WHERE id = ?",
            (title, description, opportunity_id),
        )
    connection.commit()


def _current_documents(connection):
    return tuple(
        load_opportunity_matching_input(connection, opportunity_id)
        for opportunity_id in select_matching_opportunity_ids(connection)
    )


def test_two_postings_swapping_their_documents_stops_the_cohort(tmp_path):
    """The exact case an ID-free corpus digest is blind to, end to end.

    A and B exchange their titles and descriptions. The cohort ids do not move,
    the corpus is the same multiset and therefore fingerprints identically, and
    Phase 8 is resynchronized so its provenance is current — yet every persisted
    percentile now describes the other posting. Only the identity binding sees
    it.
    """
    connection, _, identity, ids, _ = build_corpus(tmp_path)
    first, second = ids["strong"], ids["unbridged_fine"]

    ready, before = assemble_and_rank(connection, identity.profile_id)
    assert ready.status is RecommendationReadinessStatus.READY
    run = read_current_matching(connection, identity.profile_id).current_run
    cohort_before = select_matching_opportunity_ids(connection)
    corpus_before = run.corpus_fingerprint
    binding_before = run.semantic_binding_fingerprint
    scores_before = {
        item.opportunity_id: item.semantic.score for item in before.assessments
    }
    assert run.semantic_binding_version == SEMANTIC_BINDING_VERSION
    assert scores_before[first] != scores_before[second]

    _swap_semantic_text(connection, first, second)
    # Phase 8 is brought back to current so its freshness is not the reason.
    persist_qualifications(connection)
    # Matching is deliberately NOT resynchronized.

    documents = _current_documents(connection)
    assert select_matching_opportunity_ids(connection) == cohort_before
    assert current_semantic_corpus_fingerprint(documents) == corpus_before
    assert current_semantic_binding_fingerprint(documents) != binding_before

    stale = assemble_recommendation_inputs(connection, identity.profile_id)
    assert stale.status is RecommendationReadinessStatus.INCOMPLETE
    assert stale.records == ()
    assert [issue.code for issue in stale.issues] == [
        RecommendationReadinessIssueCode.STALE_MATCHING_SEMANTIC_BINDING
    ]
    assert "no longer carry the semantic documents" in stale.issues[0].message

    resynced = sync_matching(connection, identity.profile_id)
    after_ready, after = assemble_and_rank(connection, identity.profile_id)
    scores_after = {
        item.opportunity_id: item.semantic.score for item in after.assessments
    }
    connection.close()
    assert after_ready.status is RecommendationReadinessStatus.READY
    assert resynced.run_id is not None
    # The semantic values followed the documents to their new postings.
    assert scores_after[first] == scores_before[second]
    assert scores_after[second] == scores_before[first]


def test_a_matching_run_without_binding_provenance_is_not_fresh_enough(tmp_path):
    """A legacy run is valid history and still not a basis for recommending.

    Matching's own read model and its own persistence audit both accept it —
    asserted here — because it is a real run that was persisted before this
    provenance existed. Recommendation refuses it, because nothing in it proves
    the percentiles still belong to these postings, and it repairs nothing: the
    operator runs `sync_matching`.
    """
    connection, _, identity, _, matching = build_corpus(tmp_path)
    payload = json.loads(
        connection.execute(
            "SELECT batch_payload_json FROM matching_runs WHERE id=?",
            (matching.run_id,),
        ).fetchone()[0]
    )
    content = {
        key: value
        for key, value in payload.items()
        if not key.startswith("semantic_binding_")
    }
    legacy_run_fingerprint = matching_run_fingerprint(
        profile_id=identity.profile_id,
        selection_version=MATCHING_SELECTION_VERSION,
        matching_engine_version=content["matching_engine_version"],
        matching_rules_version=content["matching_rules_version"],
        semantic_percentile_version=content["semantic_percentile_version"],
        corpus_fingerprint=content["corpus_fingerprint"],
        tfidf_model_fingerprint=content["tfidf_model_fingerprint"],
        batch_fingerprint=connection.execute(
            "SELECT batch_fingerprint FROM matching_runs WHERE id=?",
            (matching.run_id,),
        ).fetchone()[0],
        assessments=tuple(
            connection.execute(
                "SELECT opportunity_id, assessment_fingerprint FROM "
                "matching_assessments WHERE run_id=?",
                (matching.run_id,),
            ).fetchall()
        ),
    )
    connection.execute(
        "UPDATE matching_runs SET batch_payload_json=?, run_fingerprint=? WHERE id=?",
        (canonical_json(content), legacy_run_fingerprint, matching.run_id),
    )
    connection.commit()

    # Matching itself is content with this run.
    run = read_current_matching(connection, identity.profile_id).current_run
    assert run.semantic_binding_version is None
    assert run.semantic_binding_fingerprint is None
    assert audit_matching_profile_history(connection, identity.profile_id).ok is True

    # Recommendation is not.
    assembly = assemble_recommendation_inputs(connection, identity.profile_id)
    assert assembly.status is RecommendationReadinessStatus.INCOMPLETE
    assert assembly.records == ()
    assert [issue.code for issue in assembly.issues] == [
        RecommendationReadinessIssueCode.STALE_MATCHING_SEMANTIC_BINDING
    ]
    assert "predates semantic binding provenance" in assembly.issues[0].message

    # The old run was not mutated or upgraded to get past it.
    assert (
        json.loads(
            connection.execute(
                "SELECT batch_payload_json FROM matching_runs WHERE id=?",
                (matching.run_id,),
            ).fetchone()[0]
        )
        == content
    )

    sync_matching(connection, identity.profile_id)
    ready, batch = assemble_and_rank(connection, identity.profile_id)
    connection.close()
    assert ready.status is RecommendationReadinessStatus.READY
    assert batch.assessment_count == len(CORPUS)


def test_an_ordinary_text_edit_still_reports_the_corpus_code_not_the_binding(
    tmp_path,
):
    """Ordering matters: content drift is the more specific answer."""
    connection, _, identity, ids, _ = build_corpus(tmp_path)
    connection.execute(
        "UPDATE opportunities SET description = description || ? WHERE id = ?",
        (NEUTRAL_SENTENCE, ids["strong"]),
    )
    connection.commit()
    persist_qualifications(connection)
    assembly = assemble_recommendation_inputs(connection, identity.profile_id)
    connection.close()
    assert [issue.code for issue in assembly.issues] == [
        RecommendationReadinessIssueCode.STALE_MATCHING_SEMANTIC_CORPUS
    ]


def test_the_binding_proof_still_fits_no_tfidf_and_scores_no_similarity(
    tmp_path, monkeypatch
):
    connection, _, identity, _, _ = build_corpus(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("recommendation assembly must not fit or score TF-IDF")

    for target in (
        "services.collector.matching.tfidf_similarity.fit_tfidf_corpus",
        "services.collector.matching.tfidf_similarity.score_profile_against_tfidf_corpus",
        "services.collector.matching.tfidf_similarity.score_matching_input_semantic_similarity",
    ):
        monkeypatch.setattr(target, forbidden)
    monkeypatch.setattr(TfidfVectorizer, "fit", forbidden)
    monkeypatch.setattr(TfidfVectorizer, "fit_transform", forbidden)
    monkeypatch.setattr(TfidfVectorizer, "transform", forbidden)

    documents = _current_documents(connection)
    assert len(current_semantic_binding_fingerprint(documents)) == 64
    assembly, batch = assemble_and_rank(connection, identity.profile_id)
    connection.close()
    assert assembly.status is RecommendationReadinessStatus.READY
    assert batch.assessment_count == len(CORPUS)
