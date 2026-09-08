"""The Phase 9A rules: the score, the four routing signals, and the ranking."""

from dataclasses import FrozenInstanceError, replace

import pytest

from services.collector.matching import (
    DOMAIN_WEIGHT,
    REQUIRED_SKILL_WEIGHT,
    SEMANTIC_WEIGHT,
    AlignmentStatus,
    SemanticSimilarityStatus,
)
from services.collector.qualification.fine_taxonomy import FineCategory
from services.collector.qualification.taxonomy import Domain, OpportunityType
from services.digital_twin.preferences.models import MobilityScope
from services.eligibility import GlobalStatus
from services.recommendation import (
    DISPOSITION_PRECEDENCE,
    DISPOSITION_RANK,
    ComponentStatus,
    DomainFitSource,
    EligibilitySignalStatus,
    GeographyState,
    RecommendationDisposition,
    RecommendationInputError,
    RecommendationReasonCode,
    build_recommendation_assessment,
    build_recommendation_batch,
    rank_recommendation_assessments,
)
from tests.unit.recommendation_fixtures import (
    LEGACY_FINE,
    OPPORTUNITY_ID,
    fine,
    geography,
    opportunity,
    preferences,
    recommendation_input,
)


def assess(**kwargs):
    return build_recommendation_assessment(recommendation_input(**kwargs))


# --------------------------------------------------------------------------
# the numeric score: three weights, renormalized over what is available
# --------------------------------------------------------------------------


def test_three_available_components_use_the_audited_matching_weights():
    result = assess()
    assert (REQUIRED_SKILL_WEIGHT, SEMANTIC_WEIGHT, DOMAIN_WEIGHT) == (0.5, 0.3, 0.2)
    assert result.domain.score == 1.0
    assert result.recommendation_score == 0.81
    assert result.recommendation_evidence_coverage == 1.0
    assert result.required_skill.status is ComponentStatus.AVAILABLE
    assert result.semantic.status is ComponentStatus.AVAILABLE
    assert result.domain.status is ComponentStatus.AVAILABLE


def test_missing_required_skills_renormalize_and_never_score_zero():
    result = assess(required=None, required_matched=0, required_total=0)
    # 0.3 * 0.7 + 0.2 * 1.0, over the 0.5 that remained available.
    assert result.recommendation_score == pytest.approx(0.41 / 0.5)
    assert result.recommendation_evidence_coverage == 0.5
    assert result.required_skill.score is None
    assert (
        RecommendationReasonCode.REQUIRED_SKILLS_UNAVAILABLE in result.unknowns
    )


def test_missing_semantic_renormalizes_and_is_reported_as_an_unknown():
    result = assess(semantic=None)
    assert result.recommendation_score == pytest.approx(0.6 / 0.7)
    assert result.recommendation_evidence_coverage == 0.7
    assert result.semantic.semantic_status is (
        SemanticSimilarityStatus.EMPTY_OPPORTUNITY_DOCUMENT
    )
    assert RecommendationReasonCode.SEMANTIC_EVIDENCE_UNAVAILABLE in result.unknowns


def test_missing_domain_renormalizes_over_the_two_remaining_components():
    result = assess(prefs=preferences(preferred_domains=()))
    assert result.domain.score is None
    assert result.recommendation_score == pytest.approx(0.61 / 0.8)
    assert result.recommendation_evidence_coverage == 0.8
    assert RecommendationReasonCode.DOMAIN_FIT_UNKNOWN in result.unknowns


def test_no_numeric_evidence_at_all_is_none_and_zero_coverage():
    result = assess(
        prefs=preferences(preferred_domains=()),
        required=None,
        required_matched=0,
        required_total=0,
        semantic=None,
        match_quality=None,
        evidence_coverage=0.0,
    )
    assert result.recommendation_score is None
    assert result.recommendation_evidence_coverage == 0.0
    assert RecommendationReasonCode.NO_NUMERIC_EVIDENCE in result.unknowns


@pytest.mark.parametrize("required", [0.0, 0.25, 1.0])
def test_score_and_coverage_stay_within_the_unit_interval(required):
    result = assess(required=required)
    assert 0.0 <= result.recommendation_score <= 1.0
    assert 0.0 <= result.recommendation_evidence_coverage <= 1.0


def test_a_zero_component_is_not_a_missing_one():
    zero = assess(required=0.0, required_matched=0, required_total=5)
    assert zero.required_skill.status is ComponentStatus.AVAILABLE
    assert zero.recommendation_evidence_coverage == 1.0
    assert zero.recommendation_score == pytest.approx(0.41)
    # Nothing confirmed is still an unknown, never a confirmed gap.
    assert (
        RecommendationReasonCode.REQUIRED_SKILLS_NOT_ALL_CONFIRMED in zero.unknowns
    )
    assert zero.confirmed_gaps == ()


def test_baseline_matching_provenance_is_carried_untouched():
    result = assess()
    assert result.baseline_match_quality == 0.75
    assert result.baseline_evidence_coverage == 1.0
    assert result.baseline_matching_assessment_fingerprint == "a" * 64
    assert result.recommendation_score != result.baseline_match_quality
    with pytest.raises(FrozenInstanceError):
        result.recommendation_score = 1.0


# --------------------------------------------------------------------------
# UNKNOWN is never a soft MISMATCH, on any of the four routing signals
# --------------------------------------------------------------------------


def test_opportunity_type_unknown_is_not_a_mismatch():
    unknown = assess(opp=opportunity(opportunity_type=OpportunityType.UNKNOWN))
    assert unknown.opportunity_type.status is AlignmentStatus.UNKNOWN
    assert unknown.disposition is RecommendationDisposition.UNCERTAIN
    assert RecommendationReasonCode.OPPORTUNITY_TYPE_UNKNOWN in unknown.unknowns
    assert unknown.confirmed_gaps == ()

    mismatch = assess(opp=opportunity(opportunity_type=OpportunityType.PFE))
    assert mismatch.opportunity_type.status is AlignmentStatus.MISMATCH
    assert mismatch.disposition is RecommendationDisposition.OUTSIDE_PREFERENCES
    assert (
        RecommendationReasonCode.OPPORTUNITY_TYPE_OUTSIDE_PREFERENCES
        in mismatch.confirmed_gaps
    )


def test_work_mode_unknown_is_not_a_mismatch():
    unknown = assess(opp=opportunity(remote_type="teletravail partiel"))
    assert unknown.work_mode.status is AlignmentStatus.UNKNOWN
    assert unknown.disposition is RecommendationDisposition.UNCERTAIN
    assert RecommendationReasonCode.WORK_MODE_UNKNOWN in unknown.unknowns
    assert unknown.confirmed_gaps == ()

    absent = assess(opp=opportunity(remote_type=None))
    assert absent.work_mode.status is AlignmentStatus.UNKNOWN
    assert absent.disposition is RecommendationDisposition.UNCERTAIN

    mismatch = assess(opp=opportunity(remote_type="on_site"))
    assert mismatch.work_mode.status is AlignmentStatus.MISMATCH
    assert mismatch.work_mode.opportunity_mode == "ON_SITE"
    assert mismatch.disposition is RecommendationDisposition.OUTSIDE_PREFERENCES
    assert (
        RecommendationReasonCode.WORK_MODE_OUTSIDE_PREFERENCES
        in mismatch.confirmed_gaps
    )


def test_work_mode_carries_no_weight_at_all():
    allowed = assess()
    refused = assess(opp=opportunity(remote_type="on_site"))
    assert allowed.recommendation_score == refused.recommendation_score
    assert (
        allowed.recommendation_evidence_coverage
        == refused.recommendation_evidence_coverage
    )


# --------------------------------------------------------------------------
# geography: Phase 7's own verdicts, plus the OPEN case a verdict cannot state
# --------------------------------------------------------------------------


def test_target_country_and_a_city_in_it_match():
    result = assess()
    assert result.geography.state is GeographyState.MATCH
    assert result.geography.target_country == "MA"
    assert result.geography.matching_segments == 1
    assert RecommendationReasonCode.GEOGRAPHY_MATCHED in result.strengths


def test_every_location_resolved_elsewhere_is_out_of_target():
    result = assess(geo=geography(locations=("Paris, France",)))
    assert result.geography.state is GeographyState.OUT_OF_TARGET
    assert result.disposition is RecommendationDisposition.OUTSIDE_PREFERENCES
    assert (
        RecommendationReasonCode.GEOGRAPHY_OUT_OF_TARGET in result.confirmed_gaps
    )


def test_one_unplaceable_location_withholds_the_refusal():
    result = assess(geo=geography(locations=("Paris, France", "APAC")))
    assert result.geography.state is GeographyState.UNKNOWN
    assert result.disposition is RecommendationDisposition.UNCERTAIN
    assert RecommendationReasonCode.GEOGRAPHY_UNKNOWN in result.unknowns
    assert result.confirmed_gaps == ()


def test_open_mobility_is_neutral_and_never_an_uncertainty():
    result = assess(geo=geography(scope=MobilityScope.OPEN, declared=()))
    assert result.geography.state is GeographyState.NOT_APPLICABLE
    assert result.geography.verdict_rule_id is None
    assert result.geography.mobility_rule_id == "mobility-open-v1"
    # The counters still describe the posting; only the comparison is absent.
    assert (result.geography.segments, result.geography.resolved_segments) == (1, 1)
    assert result.geography.matching_segments == 0
    assert result.disposition is RecommendationDisposition.RECOMMENDED
    codes = result.strengths + result.confirmed_gaps + result.unknowns
    assert not any(code.value.startswith("GEOGRAPHY_") for code in codes)


def test_open_mobility_stays_neutral_even_for_a_posting_abroad():
    result = assess(
        geo=geography(
            scope=MobilityScope.OPEN, declared=(), locations=("Paris, France",)
        )
    )
    assert result.geography.state is GeographyState.NOT_APPLICABLE
    assert result.disposition is RecommendationDisposition.RECOMMENDED


def test_absent_mobility_is_unknown_and_never_out_of_target():
    result = assess(geo=geography(declared=(), locations=("Paris, France",)))
    assert result.geography.state is GeographyState.UNKNOWN
    assert result.geography.target_country is None
    assert result.disposition is RecommendationDisposition.UNCERTAIN


def test_a_multi_country_mobility_never_picks_the_first_country():
    result = assess(
        geo=geography(declared=("Maroc", "France"), locations=("Casablanca",))
    )
    assert result.geography.state is GeographyState.UNKNOWN
    assert result.geography.mobility_rule_id == "mobility-multiple-countries-v1"


def test_geography_carries_no_weight_either():
    inside = assess()
    outside = assess(geo=geography(locations=("Paris, France",)))
    assert inside.recommendation_score == outside.recommendation_score


def test_resolutions_from_another_opportunity_are_refused():
    with pytest.raises(RecommendationInputError, match="another opportunity"):
        assess(geo=geography(opportunity_id=OPPORTUNITY_ID + 1))


# --------------------------------------------------------------------------
# eligibility: three verdicts and an absence, none of them invented
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "status", "disposition", "code"),
    [
        (
            GlobalStatus.ELIGIBLE,
            EligibilitySignalStatus.ELIGIBLE,
            RecommendationDisposition.RECOMMENDED,
            RecommendationReasonCode.ELIGIBILITY_NO_KNOWN_BLOCKER,
        ),
        (
            GlobalStatus.UNKNOWN,
            EligibilitySignalStatus.UNKNOWN,
            RecommendationDisposition.UNCERTAIN,
            RecommendationReasonCode.ELIGIBILITY_UNKNOWN,
        ),
        (
            GlobalStatus.INELIGIBLE,
            EligibilitySignalStatus.INELIGIBLE,
            RecommendationDisposition.KNOWN_BLOCKER,
            RecommendationReasonCode.ELIGIBILITY_KNOWN_BLOCKER,
        ),
        (
            None,
            EligibilitySignalStatus.MISSING,
            RecommendationDisposition.UNCERTAIN,
            RecommendationReasonCode.ELIGIBILITY_SNAPSHOT_MISSING,
        ),
    ],
)
def test_eligibility_states_route_without_scoring(stored, status, disposition, code):
    result = assess(eligible=stored)
    assert result.eligibility.status is status
    assert result.disposition is disposition
    assert code in result.strengths + result.confirmed_gaps + result.unknowns
    assert result.recommendation_score == 0.81


def test_a_missing_eligibility_snapshot_is_never_a_blocker():
    result = assess(eligible=None)
    assert result.eligibility.status is not EligibilitySignalStatus.INELIGIBLE
    assert result.disposition is not RecommendationDisposition.KNOWN_BLOCKER
    assert result.eligibility.engine_version is None


# --------------------------------------------------------------------------
# disposition precedence
# --------------------------------------------------------------------------


def test_a_perfectly_scored_opportunity_stays_a_known_blocker():
    result = assess(required=1.0, required_matched=5, semantic=1.0, eligible=GlobalStatus.INELIGIBLE)
    assert result.recommendation_score == 1.0
    assert result.disposition is RecommendationDisposition.KNOWN_BLOCKER


def test_a_perfectly_scored_opportunity_out_of_target_stays_outside_preferences():
    result = assess(
        required=1.0,
        required_matched=5,
        semantic=1.0,
        geo=geography(locations=("Paris, France",)),
    )
    assert result.recommendation_score == 1.0
    assert result.disposition is RecommendationDisposition.OUTSIDE_PREFERENCES


def test_a_blocker_outranks_a_contradicted_preference_and_an_unknown():
    result = assess(
        opp=opportunity(opportunity_type=OpportunityType.PFE, remote_type=None),
        geo=geography(locations=("Paris, France",)),
        eligible=GlobalStatus.INELIGIBLE,
    )
    assert result.disposition is RecommendationDisposition.KNOWN_BLOCKER


def test_a_contradicted_preference_outranks_an_unknown():
    result = assess(
        opp=opportunity(opportunity_type=OpportunityType.PFE, remote_type=None)
    )
    assert result.work_mode.status is AlignmentStatus.UNKNOWN
    assert result.disposition is RecommendationDisposition.OUTSIDE_PREFERENCES


def test_a_non_preferred_domain_is_priced_and_never_routed():
    result = assess(
        opp=opportunity(domain=Domain.DATA_ENGINEERING),
        fine_classification=fine(FineCategory.DATA_ENGINEERING),
    )
    assert result.domain.fit_status is AlignmentStatus.MISMATCH
    assert result.domain.score == 0.0
    assert (
        RecommendationReasonCode.DOMAIN_OUTSIDE_PREFERENCES in result.confirmed_gaps
    )
    assert result.disposition is RecommendationDisposition.RECOMMENDED


# --------------------------------------------------------------------------
# ranking
# --------------------------------------------------------------------------


def _ranked(*inputs):
    return build_recommendation_batch(inputs[0].matching.profile_id, list(inputs))


def _input(opportunity_id, **kwargs):
    opp = opportunity(opportunity_id=opportunity_id, **kwargs.pop("opp_kwargs", {}))
    return recommendation_input(
        opp=opp, geo=geography(opportunity_id=opportunity_id), **kwargs
    )


def test_the_precedence_and_the_ranking_are_exact_reverses():
    assert DISPOSITION_PRECEDENCE == (
        RecommendationDisposition.KNOWN_BLOCKER,
        RecommendationDisposition.OUTSIDE_PREFERENCES,
        RecommendationDisposition.UNCERTAIN,
        RecommendationDisposition.RECOMMENDED,
    )
    assert [
        disposition
        for disposition, _ in sorted(DISPOSITION_RANK.items(), key=lambda p: p[1])
    ] == list(reversed(DISPOSITION_PRECEDENCE))


def test_dispositions_order_before_any_score():
    blocked = _input(1, required=1.0, semantic=1.0, eligible=GlobalStatus.INELIGIBLE)
    outside = _input(2, required=1.0, semantic=1.0, opp_kwargs={"opportunity_type": OpportunityType.PFE})
    uncertain = _input(3, required=1.0, semantic=1.0, eligible=GlobalStatus.UNKNOWN)
    recommended = _input(4, required=0.0, semantic=0.0)
    batch = _ranked(blocked, outside, uncertain, recommended)
    assert [item.opportunity_id for item in batch.assessments] == [4, 3, 2, 1]
    assert [item.disposition for item in batch.assessments] == [
        RecommendationDisposition.RECOMMENDED,
        RecommendationDisposition.UNCERTAIN,
        RecommendationDisposition.OUTSIDE_PREFERENCES,
        RecommendationDisposition.KNOWN_BLOCKER,
    ]


def test_within_one_disposition_score_then_coverage_then_id_decide():
    high = _input(1, required=0.9)
    low = _input(2, required=0.1)
    partial = _input(3, required=None, required_matched=0, required_total=0)
    batch = _ranked(low, partial, high)
    assert [item.opportunity_id for item in batch.assessments] == [1, 3, 2]

    # Identical scores, different coverage: more evidence ranks first, and it
    # does so against the id tie-break rather than alongside it.
    full = _input(5, required=0.02, semantic=0.30)
    thin = _input(4, required=0.02, semantic=None)
    ranked = _ranked(thin, full)
    assert [item.recommendation_score for item in ranked.assessments] == [0.3, 0.3]
    assert [item.recommendation_evidence_coverage for item in ranked.assessments] == [
        1.0,
        0.7,
    ]
    assert [item.opportunity_id for item in ranked.assessments] == [5, 4]


def test_a_none_score_sorts_after_every_scored_result_in_its_lane():
    scored_zero = _input(9, required=0.0, semantic=0.0, prefs=preferences(preferred_domains=()))
    unscored = _input(
        1,
        required=None,
        required_matched=0,
        required_total=0,
        semantic=None,
        match_quality=None,
        evidence_coverage=0.0,
        prefs=preferences(preferred_domains=()),
    )
    batch = _ranked(unscored, scored_zero)
    assert [item.opportunity_id for item in batch.assessments] == [9, 1]
    assert batch.assessments[1].recommendation_score is None


def test_equal_everything_is_broken_by_the_opportunity_id():
    batch = _ranked(_input(3), _input(1), _input(2))
    assert [item.opportunity_id for item in batch.assessments] == [1, 2, 3]


def test_the_ranking_does_not_depend_on_the_input_order():
    items = [_input(3, required=0.4), _input(1, required=0.9), _input(2, required=0.4)]
    forward = _ranked(*items)
    backward = _ranked(*reversed(items))
    assert [item.opportunity_id for item in forward.assessments] == [
        item.opportunity_id for item in backward.assessments
    ]
    assert forward.batch_fingerprint == backward.batch_fingerprint


def test_ranking_alone_is_pure_over_assessments():
    batch = _ranked(_input(2), _input(1))
    assert rank_recommendation_assessments(
        list(reversed(batch.assessments))
    ) == batch.assessments


def test_a_duplicated_or_foreign_opportunity_is_refused():
    with pytest.raises(RecommendationInputError, match="duplicate"):
        _ranked(_input(1), _input(1))
    with pytest.raises(RecommendationInputError, match="another profile"):
        build_recommendation_batch(999, [_input(1)])


# --------------------------------------------------------------------------
# input validation: nothing stale is silently mixed in
# --------------------------------------------------------------------------


def test_a_snapshot_that_no_longer_describes_the_signals_is_refused():
    inputs = recommendation_input()
    stale = replace(
        inputs,
        matching=replace(inputs.matching, structured_upstream_fingerprint="b" * 64),
    )
    with pytest.raises(RecommendationInputError, match="synchronize Matching"):
        build_recommendation_assessment(stale)


def test_an_identity_mismatch_is_refused():
    inputs = recommendation_input()
    wrong = replace(
        inputs, matching=replace(inputs.matching, opportunity_id=OPPORTUNITY_ID + 1)
    )
    with pytest.raises(RecommendationInputError, match="identity mismatch"):
        build_recommendation_assessment(wrong)


def test_a_semantic_percentile_without_an_available_status_is_refused():
    inputs = recommendation_input()
    broken = replace(
        inputs,
        matching=replace(
            inputs.matching,
            semantic_status=SemanticSimilarityStatus.EMPTY_PROFILE_DOCUMENT,
        ),
    )
    with pytest.raises(RecommendationInputError, match="semantic percentile"):
        build_recommendation_assessment(broken)


def test_legacy_fine_rows_still_assess_through_the_coarse_component():
    result = assess(fine_classification=LEGACY_FINE)
    assert result.domain.source is DomainFitSource.COARSE
    assert result.recommendation_score == 0.81
