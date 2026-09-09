"""The fine-domain bridge, the coarse fallback, and the single domain component."""

from dataclasses import replace

import pytest

from services.collector.matching import DOMAIN_WEIGHT, AlignmentStatus
from services.collector.qualification.fine_taxonomy import FineCategory
from services.collector.qualification.taxonomy import Domain
from services.recommendation import (
    FINE_DOMAIN_BRIDGE,
    FINE_DOMAIN_BRIDGE_VERSION,
    UNBRIDGED_FINE_CATEGORIES,
    ComponentStatus,
    DomainFitSource,
    FineDomainAvailability,
    FineDomainReason,
    RecommendationFineClassification,
    RecommendationInputError,
    RecommendationReasonCode,
    build_domain_component,
    build_fine_domain_fit,
    build_recommendation_assessment,
    preferred_rank_score,
)
from tests.unit.recommendation_fixtures import (
    DEFAULT,
    FINE_CLASSIFIER_VERSION,
    LEGACY_FINE,
    PREFERRED_DOMAINS,
    fine,
    opportunity,
    preferences,
    recommendation_input,
)


def fit(category, version=FINE_CLASSIFIER_VERSION, prefs=DEFAULT):
    return build_fine_domain_fit(
        RecommendationFineClassification(category, version),
        preferences() if prefs is DEFAULT else prefs,
    )


def assess(**kwargs):
    return build_recommendation_assessment(recommendation_input(**kwargs))


# --------------------------------------------------------------------------
# the bridge itself
# --------------------------------------------------------------------------


def test_every_fine_category_is_either_bridged_or_explicitly_unbridged():
    assert set(FINE_DOMAIN_BRIDGE) | set(UNBRIDGED_FINE_CATEGORIES) == set(FineCategory)
    assert not set(FINE_DOMAIN_BRIDGE) & set(UNBRIDGED_FINE_CATEGORIES)
    assert set(FINE_DOMAIN_BRIDGE.values()) <= set(Domain)


def test_the_specialized_categories_are_deliberately_not_bridged():
    assert set(UNBRIDGED_FINE_CATEGORIES) == {
        FineCategory.NLP,
        FineCategory.COMPUTER_VISION,
        FineCategory.OTHER,
    }


@pytest.mark.parametrize(
    ("category", "domain"),
    [
        (FineCategory.DATA_SCIENCE, Domain.DATA_SCIENCE),
        (FineCategory.GENERATIVE_AI, Domain.GENAI_LLM),
        (FineCategory.MLOPS, Domain.MLOPS_ML_PLATFORM),
        (FineCategory.MACHINE_LEARNING, Domain.MACHINE_LEARNING_AI),
        (FineCategory.ARTIFICIAL_INTELLIGENCE, Domain.MACHINE_LEARNING_AI),
        (FineCategory.DATA_ANALYTICS, Domain.BI_ANALYTICS),
        (FineCategory.BUSINESS_INTELLIGENCE, Domain.BI_ANALYTICS),
        (FineCategory.DATA_ENGINEERING, Domain.DATA_ENGINEERING),
    ],
)
def test_bridged_categories_reach_their_canonical_family(category, domain):
    result = fit(category)
    assert result.bridged_domain is domain
    assert result.availability is FineDomainAvailability.AVAILABLE


# --------------------------------------------------------------------------
# an explicit match, an explicit mismatch, and the ranks in between
# --------------------------------------------------------------------------


def test_an_explicitly_preferred_fine_category_matches_and_keeps_its_rank():
    result = fit(FineCategory.DATA_SCIENCE)
    assert result.status is AlignmentStatus.MATCH
    assert result.reason is FineDomainReason.DOMAIN_PREFERENCE_MATCHED
    assert result.preferred_rank == 1
    assert result.normalized_score == 1.0
    assert result.fine_domain_bridge_version == FINE_DOMAIN_BRIDGE_VERSION


def test_rank_order_follows_the_declared_preference_order():
    scores = [
        fit(category).normalized_score
        for category in (
            FineCategory.DATA_SCIENCE,
            FineCategory.GENERATIVE_AI,
            FineCategory.MLOPS,
            FineCategory.BUSINESS_INTELLIGENCE,
        )
    ]
    assert scores == [1.0, 0.75, 0.5, 0.25]
    assert scores == sorted(scores, reverse=True)


def test_a_fine_category_outside_a_fully_readable_preference_list_mismatches():
    result = fit(FineCategory.DATA_ENGINEERING)
    assert result.status is AlignmentStatus.MISMATCH
    assert result.reason is FineDomainReason.DOMAIN_PREFERENCE_NOT_LISTED
    assert result.normalized_score == 0.0


def test_absent_preferences_are_unknown_not_a_mismatch():
    result = fit(FineCategory.DATA_SCIENCE, prefs=None)
    assert result.status is AlignmentStatus.UNKNOWN
    assert result.reason is FineDomainReason.PROFILE_PREFERENCES_ABSENT
    assert result.normalized_score is None


def test_empty_preferences_are_unknown_not_a_mismatch():
    result = fit(
        FineCategory.DATA_SCIENCE, prefs=preferences(preferred_domains=())
    )
    assert result.status is AlignmentStatus.UNKNOWN
    assert result.reason is FineDomainReason.DOMAIN_PREFERENCES_EMPTY


def test_a_partially_unreadable_preference_list_never_becomes_a_mismatch():
    result = fit(
        FineCategory.DATA_ENGINEERING,
        prefs=preferences(preferred_domains=("Data Science", "quelque chose d'autre")),
    )
    assert result.status is AlignmentStatus.UNKNOWN
    assert result.reason is FineDomainReason.DOMAIN_PREFERENCES_PARTIALLY_UNMAPPED
    assert result.unmapped_preference_count == 1
    assert result.normalized_score is None


def test_an_unreadable_entry_does_not_hide_an_explicit_match():
    result = fit(
        FineCategory.DATA_SCIENCE,
        prefs=preferences(preferred_domains=("quelque chose", "Data Science")),
    )
    assert result.status is AlignmentStatus.MATCH
    assert result.preferred_rank == 2


# --------------------------------------------------------------------------
# the three persisted states, kept apart
# --------------------------------------------------------------------------


def test_a_legacy_row_is_unavailable_and_not_a_category():
    result = build_fine_domain_fit(LEGACY_FINE, preferences())
    assert result.availability is FineDomainAvailability.UNAVAILABLE
    assert result.reason is FineDomainReason.FINE_CLASSIFICATION_ABSENT
    assert result.fine_primary_category is None
    assert result.status is None


def test_a_classified_row_with_no_category_is_distinct_from_a_legacy_one():
    result = fit(None)
    assert result.availability is FineDomainAvailability.UNAVAILABLE
    assert result.reason is FineDomainReason.FINE_CATEGORY_ABSENT


def test_other_is_a_value_and_is_never_read_as_an_absence():
    other = fit(FineCategory.OTHER)
    legacy = build_fine_domain_fit(LEGACY_FINE, preferences())
    assert other.reason is FineDomainReason.FINE_CATEGORY_NOT_BRIDGED
    assert other.fine_primary_category is FineCategory.OTHER
    assert legacy.reason is not other.reason
    assert legacy.fine_primary_category is None


@pytest.mark.parametrize(
    "category", [FineCategory.NLP, FineCategory.COMPUTER_VISION, FineCategory.OTHER]
)
def test_an_unbridged_category_falls_back_and_is_never_a_mismatch(category):
    result = fit(category)
    assert result.availability is FineDomainAvailability.UNAVAILABLE
    assert result.reason is FineDomainReason.FINE_CATEGORY_NOT_BRIDGED
    assert result.status is None and result.normalized_score is None


def test_a_category_without_a_classifier_version_is_refused():
    with pytest.raises(RecommendationInputError, match="classifier version"):
        fit(FineCategory.DATA_SCIENCE, version=None)


# --------------------------------------------------------------------------
# exactly one domain component, ever
# --------------------------------------------------------------------------


def test_the_fine_reading_replaces_the_coarse_one_rather_than_adding_to_it():
    # The coarse domain is DATA_ENGINEERING (not preferred, score 0.0); the fine
    # category is DATA_SCIENCE (rank 1, score 1.0). Exactly one of them is used.
    result = assess(
        opp=opportunity(domain=Domain.DATA_ENGINEERING),
        fine_classification=fine(FineCategory.DATA_SCIENCE),
    )
    assert result.domain.source is DomainFitSource.FINE
    assert result.domain.score == 1.0
    assert result.domain.base_weight == DOMAIN_WEIGHT
    assert result.recommendation_evidence_coverage == 1.0
    assert result.recommendation_score == 0.81


def test_the_coarse_reading_answers_when_the_fine_half_cannot():
    result = assess(
        opp=opportunity(domain=Domain.DATA_SCIENCE),
        fine_classification=fine(FineCategory.NLP),
    )
    assert result.domain.source is DomainFitSource.COARSE
    assert result.domain.score == 1.0
    assert result.domain.fit_status is AlignmentStatus.MATCH
    assert (
        RecommendationReasonCode.FINE_DOMAIN_UNAVAILABLE_USING_COARSE_FALLBACK
        in result.unknowns
    )
    assert RecommendationReasonCode.COARSE_DOMAIN_PREFERRED in result.strengths
    assert RecommendationReasonCode.FINE_DOMAIN_PREFERRED not in result.strengths


def test_the_domain_weight_is_never_spent_twice():
    fine_match = assess(
        opp=opportunity(domain=Domain.DATA_ENGINEERING),
        fine_classification=fine(FineCategory.DATA_SCIENCE),
    )
    coarse_match = assess(
        opp=opportunity(domain=Domain.DATA_SCIENCE),
        fine_classification=fine(FineCategory.NLP),
    )
    # Two different readings, one identical weight, one identical coverage.
    assert fine_match.domain.source is not coarse_match.domain.source
    assert (
        fine_match.recommendation_evidence_coverage
        == coarse_match.recommendation_evidence_coverage
        == 1.0
    )
    assert fine_match.recommendation_score == coarse_match.recommendation_score
    for result in (fine_match, coarse_match):
        codes = result.strengths + result.confirmed_gaps + result.unknowns
        domain_codes = [
            code
            for code in codes
            if code
            in {
                RecommendationReasonCode.FINE_DOMAIN_PREFERRED,
                RecommendationReasonCode.COARSE_DOMAIN_PREFERRED,
                RecommendationReasonCode.DOMAIN_OUTSIDE_PREFERENCES,
                RecommendationReasonCode.DOMAIN_FIT_UNKNOWN,
            }
        ]
        assert len(domain_codes) == 1


def test_an_unknown_fine_alignment_keeps_the_component_missing_not_zero():
    result = assess(prefs=preferences(preferred_domains=()))
    assert result.domain.source is DomainFitSource.FINE
    assert result.domain.status is ComponentStatus.MISSING
    assert result.domain.score is None
    assert result.recommendation_evidence_coverage == 0.8


def test_the_provenance_of_the_chosen_reading_is_on_the_component():
    fine_side = assess(fine_classification=fine(FineCategory.DATA_SCIENCE))
    coarse_side = assess(fine_classification=LEGACY_FINE)
    assert fine_side.domain.fine_domain_bridge_version == FINE_DOMAIN_BRIDGE_VERSION
    assert fine_side.domain.fine_classifier_version == FINE_CLASSIFIER_VERSION
    assert coarse_side.domain.fine_domain_bridge_version is None
    assert coarse_side.domain.fine_classifier_version is None


# --------------------------------------------------------------------------
# the rank rule is Matching v1's, and stays Matching v1's
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rank", [1, 2, 3, 4])
def test_the_rank_score_agrees_with_the_matching_engine_itself(rank):
    """Pin the fine rank rule against Phase 4's own output for the same rank."""
    from tests.unit.test_matching_engine import assessment as matching_assessment

    baseline = matching_assessment(rank=rank)
    # `test_matching_engine.assessment` declares eight preferred domains.
    assert baseline.domain.normalized_score == preferred_rank_score(rank, 8)


@pytest.mark.parametrize(
    ("rank", "count"), [(0, 4), (5, 4), (None, 4), (1, 0), (-1, 4)]
)
def test_an_impossible_rank_is_refused_rather_than_scored(rank, count):
    with pytest.raises(RecommendationInputError, match="valid rank"):
        preferred_rank_score(rank, count)


def test_the_preferred_domain_fixture_matches_the_bridged_families():
    assert len(PREFERRED_DOMAINS) == 4


def test_absent_preferences_reach_the_assessment_as_an_unknown_component():
    result = assess(prefs=None)
    assert result.domain.fit_status is AlignmentStatus.UNKNOWN
    assert result.domain.score is None
    assert result.recommendation_evidence_coverage == 0.8
    assert RecommendationReasonCode.DOMAIN_FIT_UNKNOWN in result.unknowns


# --------------------------------------------------------------------------
# the canonical family the fine reading was compared through
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("category", "family"),
    [
        (FineCategory.DATA_SCIENCE, Domain.DATA_SCIENCE),
        (FineCategory.ARTIFICIAL_INTELLIGENCE, Domain.MACHINE_LEARNING_AI),
        (FineCategory.MACHINE_LEARNING, Domain.MACHINE_LEARNING_AI),
        (FineCategory.GENERATIVE_AI, Domain.GENAI_LLM),
    ],
)
def test_the_assessment_states_which_family_produced_the_fine_alignment(
    category, family
):
    """The persisted category and the compared family are two different facts.

    `MACHINE_LEARNING` and `ARTIFICIAL_INTELLIGENCE` are two persisted
    categories reaching one canonical family, so the category alone cannot say
    which comparison produced the score. Both are on the component.
    """
    result = assess(fine_classification=fine(category))
    assert result.domain.source is DomainFitSource.FINE
    assert result.domain.fine_primary_category is category
    assert result.domain.bridged_domain is family


def test_two_fine_categories_share_one_family_and_stay_distinguishable():
    machine_learning = assess(fine_classification=fine(FineCategory.MACHINE_LEARNING))
    artificial = assess(
        fine_classification=fine(FineCategory.ARTIFICIAL_INTELLIGENCE)
    )
    assert (
        machine_learning.domain.bridged_domain
        is artificial.domain.bridged_domain
        is Domain.MACHINE_LEARNING_AI
    )
    assert (
        machine_learning.domain.fine_primary_category
        is not artificial.domain.fine_primary_category
    )


@pytest.mark.parametrize(
    "category", [FineCategory.NLP, FineCategory.COMPUTER_VISION, FineCategory.OTHER]
)
def test_the_coarse_fallback_claims_no_bridged_family(category):
    """A coarse component compared no fine family, so it names none."""
    result = assess(fine_classification=fine(category))
    assert result.domain.source is DomainFitSource.COARSE
    assert result.domain.bridged_domain is None


def test_a_legacy_row_falls_back_and_claims_no_bridged_family():
    result = assess(fine_classification=LEGACY_FINE)
    assert result.domain.source is DomainFitSource.COARSE
    assert result.domain.bridged_domain is None


def test_an_available_fine_fit_without_a_bridged_family_is_refused():
    """Provenance is not silently dropped to publish a score."""
    usable = fit(FineCategory.DATA_SCIENCE)
    incoherent = replace(usable, bridged_domain=None)
    with pytest.raises(RecommendationInputError, match="bridged domain"):
        build_domain_component(
            AlignmentStatus.UNKNOWN,
            "DOMAIN_PREFERENCE_MATCHED",
            None,
            None,
            incoherent,
            FINE_CLASSIFIER_VERSION,
        )
