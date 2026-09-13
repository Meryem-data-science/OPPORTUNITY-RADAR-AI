"""The Phase 10.5 contract: the closed vocabularies, the shapes, the identities.

These tests are about `schema.py` and `fingerprint.py` — what an experiment
artefact may say, what it may not, and what its digest covers. Nothing here
computes a projection; the tests that do live beside the authorities that
produce them.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from evaluation.dataset import EvaluationDatasetError
from evaluation.metrics import (
    EvaluationUniverseKind,
    MetricName,
    MetricResult,
    MetricStatus,
    MetricSupport,
    MetricUnavailableReason,
    RankingSource,
)

from evaluation.experiments import (
    EXPERIMENT_CONTRACT_VERSION,
    EXPERIMENT_RANKING_SOURCE,
    EXPERIMENT_RANKING_UNIVERSE_KIND,
    EXPERIMENT_RUN_CONTEXT_SCHEMA_VERSION,
    EXPERIMENT_RUN_SCHEMA_VERSION,
    OVERLAP_PAIR_ORDER,
    OVERLAP_PAIR_PROJECTIONS,
    OVERLAP_RESULT_SCHEMA_VERSION,
    PROJECTION_ORDER,
    PROJECTION_RESULT_SCHEMA_VERSION,
    RANKING_EXPERIMENT_QUESTIONS,
    RANKING_EXPERIMENT_RESULT_SCHEMA_VERSION,
    CohortProjection,
    DirectionalRate,
    DirectionalRateName,
    ExperimentArgumentError,
    ExperimentBindingError,
    ExperimentContractError,
    ExperimentRunContextBinding,
    ExperimentRunProvenance,
    ExperimentsError,
    OverlapPair,
    OverlapResult,
    OverlapStatus,
    OverlapUnavailableReason,
    ProjectionResult,
    ProjectionStatus,
    ProjectionUnavailableReason,
    RankingExperimentResult,
    RankingExperimentStatus,
    RankingExperimentUnavailableReason,
    RankingMetricEntry,
    canonical_overlap_result_payload,
    canonical_projection_result_payload,
    canonical_ranking_experiment_result_payload,
    experiment_run_context_fingerprint,
    overlap_pair_projections,
    overlap_result_fingerprint,
    projection_result_fingerprint,
    ranking_experiment_result_fingerprint,
    require_supported_experiment_contract_version,
    require_supported_experiment_run_schema_version,
    validate_cohort_size,
    validate_experiment_fingerprint,
    validate_experiment_opportunity_id,
    validate_overlap_result_structure,
    validate_projection_result_structure,
    validate_ranking_experiment_result_structure,
    verify_experiment_run_context_fingerprint,
    verify_overlap_result_fingerprint,
    verify_projection_result_fingerprint,
    verify_ranking_experiment_result_fingerprint,
)

DATASET_ID = "evaluation-dataset-v3-" + "a" * 16
CONTENT = "a" * 64
PROFILE_CONTEXT = "9" * 64
OTHER = "b" * 64


# ====================================================================
# versions and errors
# ====================================================================


def test_the_versions_are_the_frozen_v1_names() -> None:
    assert EXPERIMENT_CONTRACT_VERSION == "evaluation-experiment-contract-v1"
    assert EXPERIMENT_RUN_CONTEXT_SCHEMA_VERSION == "experiment-run-context-v1"
    assert EXPERIMENT_RUN_SCHEMA_VERSION == "experiment-run-v1"
    assert RANKING_EXPERIMENT_RESULT_SCHEMA_VERSION == (
        "ranking-experiment-result-v1"
    )
    assert PROJECTION_RESULT_SCHEMA_VERSION == "projection-result-v1"
    assert OVERLAP_RESULT_SCHEMA_VERSION == "overlap-result-v1"


def test_every_error_is_a_phase_10_1_error() -> None:
    assert issubclass(ExperimentsError, EvaluationDatasetError)
    for error in (
        ExperimentContractError,
        ExperimentBindingError,
        ExperimentArgumentError,
    ):
        assert issubclass(error, ExperimentsError)


@pytest.mark.parametrize(
    "value", ["evaluation-experiment-contract-v2", "", None, 1]
)
def test_an_unsupported_contract_version_is_refused(value) -> None:
    with pytest.raises(ExperimentContractError):
        require_supported_experiment_contract_version(value)


@pytest.mark.parametrize("value", ["experiment-run-v2", "", None])
def test_an_unsupported_run_schema_version_is_refused(value) -> None:
    with pytest.raises(ExperimentContractError):
        require_supported_experiment_run_schema_version(value)


# ====================================================================
# the closed vocabularies
# ====================================================================


def test_there_are_exactly_five_projections_in_canonical_order() -> None:
    assert len(CohortProjection) == 5
    assert PROJECTION_ORDER == (
        CohortProjection.FROZEN_COHORT,
        CohortProjection.GEO_NOT_EXPLICITLY_OUT_OF_TARGET,
        CohortProjection.DATA_AI_NOT_EXPLICITLY_OUT_OF_SCOPE,
        CohortProjection.MATCHING_OBSERVED,
        CohortProjection.RECOMMENDATION_OBSERVED,
    )
    assert set(PROJECTION_ORDER) == set(CohortProjection)


def test_there_are_exactly_six_overlaps_over_the_four_questions() -> None:
    """Six because four questions have six unordered pairs. G, A, M, R."""
    assert len(OverlapPair) == 6
    assert set(OVERLAP_PAIR_ORDER) == set(OverlapPair)
    assert len(set(OVERLAP_PAIR_ORDER)) == 6


def test_the_frozen_cohort_is_never_one_side_of_an_overlap() -> None:
    """It is the reference set every overlap is completed against."""
    for pair in OverlapPair:
        left, right = overlap_pair_projections(pair)
        assert CohortProjection.FROZEN_COHORT not in (left, right)


def test_each_pair_is_the_two_projections_in_the_canonical_g_a_m_r_order() -> None:
    questions = [
        CohortProjection.GEO_NOT_EXPLICITLY_OUT_OF_TARGET,
        CohortProjection.DATA_AI_NOT_EXPLICITLY_OUT_OF_SCOPE,
        CohortProjection.MATCHING_OBSERVED,
        CohortProjection.RECOMMENDATION_OBSERVED,
    ]
    seen = set()
    for pair in OVERLAP_PAIR_ORDER:
        left, right = OVERLAP_PAIR_PROJECTIONS[pair]
        assert questions.index(left) < questions.index(right)
        seen.add((left, right))
    # Exactly the six unordered pairs, each once.
    assert len(seen) == 6


def test_the_four_ranking_questions_are_fixed_and_ordered() -> None:
    assert RANKING_EXPERIMENT_QUESTIONS == (
        (MetricName.PRECISION_AT_K, 5),
        (MetricName.PRECISION_AT_K, 10),
        (MetricName.RECALL_AT_K, 10),
        (MetricName.NDCG_AT_K, 10),
    )


def test_no_k_other_than_five_and_ten_exists_in_v1() -> None:
    assert {k for _, k in RANKING_EXPERIMENT_QUESTIONS} == {5, 10}


def test_the_only_evaluable_ranking_is_the_frozen_recommendation() -> None:
    assert EXPERIMENT_RANKING_SOURCE is RankingSource.FROZEN_DATASET_RECOMMENDATION
    assert EXPERIMENT_RANKING_SOURCE is not RankingSource.DECLARED_OFFLINE_RANKING


def test_the_ranking_universe_is_the_whole_frozen_cohort() -> None:
    """Never the observed recommendation set: recall could not see a miss."""
    assert EXPERIMENT_RANKING_UNIVERSE_KIND is (
        EvaluationUniverseKind.FROZEN_DATASET_COHORT
    )


def test_the_reason_vocabularies_are_closed_and_minimal() -> None:
    assert [str(item) for item in ProjectionUnavailableReason] == [
        "TARGET_COUNTRY_UNAVAILABLE"
    ]
    assert sorted(str(item) for item in OverlapUnavailableReason) == [
        "EMPTY_REFERENCE_PROJECTION",
        "INPUT_PROJECTION_UNAVAILABLE",
    ]
    assert [str(item) for item in RankingExperimentUnavailableReason] == [
        "NO_OBSERVED_RECOMMENDATION_RANKING"
    ]


def test_no_causal_vocabulary_appears_anywhere_in_the_contract() -> None:
    """D35: membership, non-membership, share and overlap. Nothing else."""
    import pathlib

    names = set()
    for enum in (
        CohortProjection,
        OverlapPair,
        OverlapUnavailableReason,
        ProjectionUnavailableReason,
        RankingExperimentUnavailableReason,
        DirectionalRateName,
        ProjectionStatus,
        OverlapStatus,
        RankingExperimentStatus,
    ):
        names |= {str(item) for item in enum}
    fields = set()
    for shape in (
        ProjectionResult,
        OverlapResult,
        RankingExperimentResult,
        ExperimentRunContextBinding,
    ):
        fields |= set(shape.__dataclass_fields__)
    forbidden = (
        "conversion",
        "retention",
        "drop_off",
        "dropoff",
        "funnel",
        "improvement",
        "caused",
        "uplift",
        "global_score",
        "quality_score",
        "computed_count",
        "na_count",
    )
    for word in forbidden:
        assert not any(word in name.lower() for name in names | fields), word


# ====================================================================
# primitive validators
# ====================================================================


@pytest.mark.parametrize("value", ["", "A" * 64, "a" * 63, None, 1, "a" * 65])
def test_a_fingerprint_is_a_lowercase_sha256_shape(value) -> None:
    with pytest.raises(ExperimentBindingError):
        validate_experiment_fingerprint(value, subject="a digest")


@pytest.mark.parametrize("value", [0, -1, True, False, "1", None, 1.0])
def test_an_opportunity_id_is_a_positive_integer_and_bool_is_not_one(value) -> None:
    with pytest.raises(ExperimentBindingError):
        validate_experiment_opportunity_id(value, subject="an id")


def test_an_empty_cohort_is_refused_rather_than_divided_by() -> None:
    with pytest.raises(ExperimentBindingError, match="not a cohort"):
        validate_cohort_size(0, subject="the cohort")
    assert validate_cohort_size(1, subject="the cohort") == 1


# ====================================================================
# the projection result
# ====================================================================


def projection(
    *,
    which: CohortProjection = CohortProjection.MATCHING_OBSERVED,
    included: tuple[int, ...] | None = (1, 2),
    cohort_size: int = 4,
    status: ProjectionStatus = ProjectionStatus.COMPUTED,
    reason=None,
    seal: bool = True,
    **overrides,
) -> ProjectionResult:
    fields = {
        "result_schema_version": PROJECTION_RESULT_SCHEMA_VERSION,
        "contract_version": EXPERIMENT_CONTRACT_VERSION,
        "projection": which,
        "status": status,
        "dataset_id": DATASET_ID,
        "dataset_content_fingerprint": CONTENT,
        "cohort_size": cohort_size,
        "result_fingerprint": "0" * 64,
        "unavailable_reason": reason,
    }
    if status is ProjectionStatus.COMPUTED:
        fields.update(
            included_opportunity_ids=included,
            included_count=len(included),
            excluded_count=cohort_size - len(included),
            cohort_share=len(included) / cohort_size,
        )
    fields.update(overrides)
    draft = ProjectionResult(**fields)
    if not seal:
        return draft
    return replace(draft, result_fingerprint=projection_result_fingerprint(draft))


def test_a_computed_projection_states_its_membership_and_its_counts() -> None:
    result = projection()
    validate_projection_result_structure(result)
    assert verify_projection_result_fingerprint(result) == result.result_fingerprint
    assert result.included_ids == (1, 2)


def test_a_projection_whose_count_disagrees_with_its_membership_is_refused() -> None:
    with pytest.raises(ExperimentBindingError, match="included_count"):
        validate_projection_result_structure(
            projection(included_count=3, seal=False)
        )


def test_a_projection_whose_counts_do_not_partition_the_cohort_is_refused() -> None:
    with pytest.raises(ExperimentBindingError, match="partitions its cohort"):
        validate_projection_result_structure(
            projection(excluded_count=1, seal=False)
        )


def test_a_projection_whose_share_is_stated_rather_than_derived_is_refused() -> None:
    with pytest.raises(ExperimentBindingError, match="cohort share"):
        validate_projection_result_structure(
            projection(cohort_share=0.75, seal=False)
        )


@pytest.mark.parametrize("share", [-0.1, 1.5, float("nan"), float("inf")])
def test_a_share_outside_zero_to_one_is_refused(share) -> None:
    with pytest.raises((ExperimentArgumentError, ExperimentBindingError)):
        validate_projection_result_structure(
            projection(cohort_share=share, seal=False)
        )


def test_a_membership_out_of_canonical_order_is_refused() -> None:
    with pytest.raises(ExperimentBindingError, match="canonical order"):
        validate_projection_result_structure(
            projection(included_opportunity_ids=(2, 1), seal=False)
        )


def test_a_membership_repeating_an_id_is_refused() -> None:
    with pytest.raises(ExperimentBindingError, match="repeats"):
        validate_projection_result_structure(
            projection(
                included_opportunity_ids=(1, 1),
                included_count=2,
                excluded_count=2,
                cohort_share=0.5,
                seal=False,
            )
        )


def test_the_frozen_cohort_projection_must_be_the_whole_cohort() -> None:
    with pytest.raises(ExperimentBindingError, match="whole frozen cohort"):
        validate_projection_result_structure(
            projection(which=CohortProjection.FROZEN_COHORT, seal=False)
        )


def test_an_unavailable_projection_fabricates_no_membership() -> None:
    result = projection(
        which=CohortProjection.GEO_NOT_EXPLICITLY_OUT_OF_TARGET,
        status=ProjectionStatus.N_A,
        reason=ProjectionUnavailableReason.TARGET_COUNTRY_UNAVAILABLE,
    )
    validate_projection_result_structure(result)
    assert result.included_opportunity_ids is None
    assert result.included_count is None
    assert result.excluded_count is None
    assert result.cohort_share is None
    # The cohort size stays: it is a fact about the snapshot, not about this
    # projection, and a reader of an N_A still needs it.
    assert result.cohort_size == 4


def test_reading_an_unavailable_projections_membership_raises() -> None:
    """No empty tuple, and no `None` a caller forgets to check."""
    result = projection(
        status=ProjectionStatus.N_A,
        reason=ProjectionUnavailableReason.TARGET_COUNTRY_UNAVAILABLE,
    )
    with pytest.raises(ExperimentContractError, match="not even an empty one"):
        result.included_ids


@pytest.mark.parametrize(
    "field",
    [
        "included_opportunity_ids",
        "included_count",
        "excluded_count",
        "cohort_share",
    ],
)
def test_an_unavailable_projection_stating_a_count_is_refused(field) -> None:
    value = (1,) if field.endswith("ids") else 1
    with pytest.raises(ExperimentContractError, match="not even a zero"):
        validate_projection_result_structure(
            projection(
                status=ProjectionStatus.N_A,
                reason=ProjectionUnavailableReason.TARGET_COUNTRY_UNAVAILABLE,
                seal=False,
                **{field: value},
            )
        )


def test_an_unavailable_projection_needs_a_reason() -> None:
    with pytest.raises(ExperimentContractError):
        validate_projection_result_structure(
            projection(status=ProjectionStatus.N_A, seal=False)
        )


def test_a_computed_projection_may_not_state_a_reason() -> None:
    with pytest.raises(ExperimentContractError, match="COMPUTED and states"):
        validate_projection_result_structure(
            projection(
                reason=ProjectionUnavailableReason.TARGET_COUNTRY_UNAVAILABLE,
                seal=False,
            )
        )


# ====================================================================
# the projection identity — the exact ids are covered
# ====================================================================


def test_the_projection_digest_domain_excludes_only_its_own_digest() -> None:
    result = projection()
    domain = canonical_projection_result_payload(result)
    assert "result_fingerprint" not in domain
    assert domain["included_opportunity_ids"] == [1, 2]


def test_same_counts_over_different_ids_are_different_fingerprints() -> None:
    """The property that makes a membership unforgeable under a valid digest."""
    first = projection(included=(1, 2))
    second = projection(included=(3, 4))
    assert first.included_count == second.included_count
    assert first.cohort_share == second.cohort_share
    assert first.result_fingerprint != second.result_fingerprint


def test_an_edited_membership_no_longer_matches_its_declared_digest() -> None:
    result = projection()
    forged = replace(
        result,
        included_opportunity_ids=(1, 3),
    )
    with pytest.raises(ExperimentBindingError, match="not what it says it is"):
        verify_projection_result_fingerprint(forged)


# ====================================================================
# the directional rates
# ====================================================================


def rate(
    name: DirectionalRateName = DirectionalRateName.RIGHT_AMONG_LEFT,
    *,
    numerator: int = 1,
    denominator: int = 2,
) -> DirectionalRate:
    if denominator == 0:
        return DirectionalRate(
            name=name,
            status=OverlapStatus.N_A,
            unavailable_reason=(
                OverlapUnavailableReason.EMPTY_REFERENCE_PROJECTION
            ),
            numerator=0,
            denominator=0,
            value=None,
        )
    return DirectionalRate(
        name=name,
        status=OverlapStatus.COMPUTED,
        numerator=numerator,
        denominator=denominator,
        value=numerator / denominator,
    )


def overlap(
    *,
    pair: OverlapPair = OverlapPair.MATCHING_AND_RECOMMENDATION,
    both: tuple[int, ...] | None = (1,),
    left_only: tuple[int, ...] | None = (2,),
    right_only: tuple[int, ...] | None = (3,),
    neither: tuple[int, ...] | None = (4,),
    cohort_size: int = 4,
    status: OverlapStatus = OverlapStatus.COMPUTED,
    reason=None,
    seal: bool = True,
    **overrides,
) -> OverlapResult:
    left_projection, right_projection = overlap_pair_projections(pair)
    fields = {
        "result_schema_version": OVERLAP_RESULT_SCHEMA_VERSION,
        "contract_version": EXPERIMENT_CONTRACT_VERSION,
        "pair": pair,
        "status": status,
        "dataset_id": DATASET_ID,
        "dataset_content_fingerprint": CONTENT,
        "cohort_size": cohort_size,
        "left_projection": left_projection,
        "right_projection": right_projection,
        "left_projection_result_fingerprint": CONTENT,
        "right_projection_result_fingerprint": OTHER,
        "result_fingerprint": "0" * 64,
        "unavailable_reason": reason,
    }
    if status is OverlapStatus.COMPUTED:
        fields.update(
            both_opportunity_ids=both,
            left_only_opportunity_ids=left_only,
            right_only_opportunity_ids=right_only,
            neither_opportunity_ids=neither,
            both_count=len(both),
            left_only_count=len(left_only),
            right_only_count=len(right_only),
            neither_count=len(neither),
            right_among_left=rate(
                DirectionalRateName.RIGHT_AMONG_LEFT,
                numerator=len(both),
                denominator=len(both) + len(left_only),
            ),
            left_among_right=rate(
                DirectionalRateName.LEFT_AMONG_RIGHT,
                numerator=len(both),
                denominator=len(both) + len(right_only),
            ),
        )
    fields.update(overrides)
    draft = OverlapResult(**fields)
    if not seal:
        return draft
    return replace(draft, result_fingerprint=overlap_result_fingerprint(draft))


def test_a_computed_overlap_partitions_its_cohort_exactly() -> None:
    result = overlap()
    validate_overlap_result_structure(result)
    assert verify_overlap_result_fingerprint(result) == result.result_fingerprint


def test_overlapping_partitions_are_refused() -> None:
    with pytest.raises(ExperimentBindingError, match="disjoint"):
        validate_overlap_result_structure(
            overlap(
                left_only=(1,),
                left_only_count=1,
                seal=False,
            )
        )


def test_partitions_that_do_not_cover_the_cohort_are_refused() -> None:
    with pytest.raises(ExperimentBindingError, match="partition the whole"):
        validate_overlap_result_structure(
            overlap(neither=(), neither_count=0, seal=False)
        )


def test_a_rate_whose_fraction_is_not_the_partitions_own_is_refused() -> None:
    with pytest.raises(ExperimentBindingError, match="the partitions imply"):
        validate_overlap_result_structure(
            overlap(
                right_among_left=rate(
                    DirectionalRateName.RIGHT_AMONG_LEFT,
                    numerator=1,
                    denominator=4,
                ),
                seal=False,
            )
        )


def test_a_rate_value_that_is_stated_rather_than_derived_is_refused() -> None:
    bogus = DirectionalRate(
        name=DirectionalRateName.RIGHT_AMONG_LEFT,
        status=OverlapStatus.COMPUTED,
        numerator=1,
        denominator=2,
        value=0.9,
    )
    with pytest.raises(ExperimentBindingError, match="implies"):
        validate_overlap_result_structure(
            overlap(right_among_left=bogus, seal=False)
        )


def test_a_zero_denominator_rate_is_na_and_never_zero() -> None:
    """`0/0` reported as `0.0` would be a measurement of nothing."""
    result = overlap(both=(), left_only=(), right_only=(3,), neither=(1, 2, 4))
    validate_overlap_result_structure(result)
    assert result.status is OverlapStatus.COMPUTED
    assert result.right_among_left.status is OverlapStatus.N_A
    assert result.right_among_left.unavailable_reason is (
        OverlapUnavailableReason.EMPTY_REFERENCE_PROJECTION
    )
    assert result.right_among_left.numerator == 0
    assert result.right_among_left.denominator == 0
    assert result.right_among_left.value is None
    # The other direction still has a denominator, so it is computed.
    assert result.left_among_right.status is OverlapStatus.COMPUTED
    assert result.left_among_right.value == 0.0


def test_a_rate_that_refuses_over_a_non_empty_reference_is_refused() -> None:
    bogus = DirectionalRate(
        name=DirectionalRateName.RIGHT_AMONG_LEFT,
        status=OverlapStatus.N_A,
        unavailable_reason=OverlapUnavailableReason.EMPTY_REFERENCE_PROJECTION,
        numerator=0,
        denominator=2,
        value=None,
    )
    with pytest.raises(ExperimentBindingError, match="not empty"):
        validate_overlap_result_structure(
            overlap(right_among_left=bogus, seal=False)
        )


def test_an_unavailable_overlap_fabricates_no_partition() -> None:
    result = overlap(
        status=OverlapStatus.N_A,
        reason=OverlapUnavailableReason.INPUT_PROJECTION_UNAVAILABLE,
    )
    validate_overlap_result_structure(result)
    for name in (
        "both_opportunity_ids",
        "left_only_opportunity_ids",
        "right_only_opportunity_ids",
        "neither_opportunity_ids",
        "both_count",
        "right_among_left",
        "left_among_right",
    ):
        assert getattr(result, name) is None


def test_an_unavailable_overlap_may_only_state_the_input_reason() -> None:
    with pytest.raises(ExperimentContractError, match="whole overlap"):
        validate_overlap_result_structure(
            overlap(
                status=OverlapStatus.N_A,
                reason=OverlapUnavailableReason.EMPTY_REFERENCE_PROJECTION,
                seal=False,
            )
        )


def test_an_overlap_naming_the_wrong_projections_for_its_pair_is_refused() -> None:
    with pytest.raises(ExperimentBindingError, match="fixed by the contract"):
        validate_overlap_result_structure(
            overlap(left_projection=CohortProjection.FROZEN_COHORT, seal=False)
        )


def test_the_overlap_digest_covers_the_exact_partitions() -> None:
    domain = canonical_overlap_result_payload(overlap())
    assert "result_fingerprint" not in domain
    assert domain["both_opportunity_ids"] == [1]
    first = overlap(both=(1,), left_only=(2,), right_only=(3,), neither=(4,))
    second = overlap(both=(2,), left_only=(1,), right_only=(3,), neither=(4,))
    assert first.both_count == second.both_count
    assert first.result_fingerprint != second.result_fingerprint


# ====================================================================
# the ranking experiment result
# ====================================================================


def metric_result(
    metric: MetricName,
    k: int,
    *,
    status: MetricStatus = MetricStatus.COMPUTED,
    value: float | None = 0.5,
    reason=None,
) -> MetricResult:
    return MetricResult(
        metric=metric,
        status=status,
        value=value if status is MetricStatus.COMPUTED else None,
        reason=reason,
        support=MetricSupport(k_requested=k, k_effective=min(k, 10)),
    )


def ranking(
    *,
    status: RankingExperimentStatus = RankingExperimentStatus.COMPUTED,
    entries=None,
    reason=None,
    seal: bool = True,
    **overrides,
) -> RankingExperimentResult:
    if entries is None and status is RankingExperimentStatus.COMPUTED:
        entries = tuple(
            RankingMetricEntry(
                metric=metric,
                k_requested=k,
                result=metric_result(metric, k),
            )
            for metric, k in RANKING_EXPERIMENT_QUESTIONS
        )
    fields = {
        "result_schema_version": RANKING_EXPERIMENT_RESULT_SCHEMA_VERSION,
        "contract_version": EXPERIMENT_CONTRACT_VERSION,
        "status": status,
        "dataset_id": DATASET_ID,
        "dataset_content_fingerprint": CONTENT,
        "profile_id": 1,
        "profile_context_fingerprint": PROFILE_CONTEXT,
        "recommendation_projection_result_fingerprint": OTHER,
        "result_fingerprint": "0" * 64,
        "unavailable_reason": reason,
        "metric_results": entries or (),
    }
    if status is RankingExperimentStatus.COMPUTED:
        from evaluation.metrics import EvidenceClass

        fields.update(
            evaluation_run_fingerprint="1" * 64,
            evaluation_universe_fingerprint="2" * 64,
            ranking_fingerprint="3" * 64,
            labelset_fingerprint="4" * 64,
            evidence_class=EvidenceClass.DIAGNOSTIC_CALIBRATION,
        )
    fields.update(overrides)
    draft = RankingExperimentResult(**fields)
    if not seal:
        return draft
    return replace(
        draft, result_fingerprint=ranking_experiment_result_fingerprint(draft)
    )


def test_a_computed_ranking_holds_exactly_the_four_questions_in_order() -> None:
    result = ranking()
    validate_ranking_experiment_result_structure(result)
    assert [
        (entry.metric, entry.k_requested) for entry in result.metric_results
    ] == list(RANKING_EXPERIMENT_QUESTIONS)
    assert verify_ranking_experiment_result_fingerprint(result) == (
        result.result_fingerprint
    )


def test_a_fifth_question_is_a_contract_failure() -> None:
    entries = tuple(
        RankingMetricEntry(metric=metric, k_requested=k, result=metric_result(metric, k))
        for metric, k in RANKING_EXPERIMENT_QUESTIONS
    ) + (
        RankingMetricEntry(
            metric=MetricName.PRECISION_AT_K,
            k_requested=20,
            result=metric_result(MetricName.PRECISION_AT_K, 20),
        ),
    )
    with pytest.raises(ExperimentContractError, match="exactly"):
        validate_ranking_experiment_result_structure(
            ranking(entries=entries, seal=False)
        )


def test_a_missing_question_is_a_contract_failure() -> None:
    entries = tuple(
        RankingMetricEntry(metric=metric, k_requested=k, result=metric_result(metric, k))
        for metric, k in RANKING_EXPERIMENT_QUESTIONS[:3]
    )
    with pytest.raises(ExperimentContractError, match="exactly"):
        validate_ranking_experiment_result_structure(
            ranking(entries=entries, seal=False)
        )


def test_reordering_the_questions_is_a_contract_failure() -> None:
    ordered = list(RANKING_EXPERIMENT_QUESTIONS)
    ordered[0], ordered[1] = ordered[1], ordered[0]
    entries = tuple(
        RankingMetricEntry(metric=metric, k_requested=k, result=metric_result(metric, k))
        for metric, k in ordered
    )
    with pytest.raises(ExperimentContractError, match="the contract's"):
        validate_ranking_experiment_result_structure(
            ranking(entries=entries, seal=False)
        )


def test_an_entry_whose_result_is_about_another_k_is_refused() -> None:
    entries = list(
        RankingMetricEntry(metric=metric, k_requested=k, result=metric_result(metric, k))
        for metric, k in RANKING_EXPERIMENT_QUESTIONS
    )
    entries[0] = RankingMetricEntry(
        metric=MetricName.PRECISION_AT_K,
        k_requested=5,
        result=metric_result(MetricName.PRECISION_AT_K, 10),
    )
    with pytest.raises(ExperimentBindingError, match="support records"):
        validate_ranking_experiment_result_structure(
            ranking(entries=tuple(entries), seal=False)
        )


def test_an_na_metric_entry_is_legal_inside_a_computed_block() -> None:
    """Phase 10.3's own refusals are not block-level refusals."""
    entries = list(
        RankingMetricEntry(metric=metric, k_requested=k, result=metric_result(metric, k))
        for metric, k in RANKING_EXPERIMENT_QUESTIONS
    )
    entries[2] = RankingMetricEntry(
        metric=MetricName.RECALL_AT_K,
        k_requested=10,
        result=metric_result(
            MetricName.RECALL_AT_K,
            10,
            status=MetricStatus.N_A,
            reason=MetricUnavailableReason.RECALL_DENOMINATOR_UNKNOWN,
        ),
    )
    result = ranking(entries=tuple(entries))
    validate_ranking_experiment_result_structure(result)
    assert result.status is RankingExperimentStatus.COMPUTED
    assert result.entry(MetricName.RECALL_AT_K, 10).result.status is MetricStatus.N_A


def test_the_unavailable_ranking_block_states_no_binding_and_no_entry() -> None:
    result = ranking(
        status=RankingExperimentStatus.N_A,
        reason=(
            RankingExperimentUnavailableReason.NO_OBSERVED_RECOMMENDATION_RANKING
        ),
    )
    validate_ranking_experiment_result_structure(result)
    assert result.metric_results == ()
    assert result.evaluation_run_fingerprint is None
    assert result.evaluation_universe_fingerprint is None
    assert result.ranking_fingerprint is None
    assert result.labelset_fingerprint is None
    assert result.evidence_class is None
    # Always stated: the emptiness of that projection is what licenses the
    # refusal, so the refusal names it.
    assert result.recommendation_projection_result_fingerprint == OTHER


def test_the_ranking_digest_covers_the_four_metric_payloads_in_full() -> None:
    domain = canonical_ranking_experiment_result_payload(ranking())
    assert "result_fingerprint" not in domain
    assert len(domain["metric_results"]) == 4
    assert domain["metric_results"][0]["result"]["support"]["k_requested"] == 5
    # A changed value moves the ranking identity, with no metric-level digest
    # in between.
    entries = list(
        RankingMetricEntry(metric=metric, k_requested=k, result=metric_result(metric, k))
        for metric, k in RANKING_EXPERIMENT_QUESTIONS
    )
    entries[0] = RankingMetricEntry(
        metric=MetricName.PRECISION_AT_K,
        k_requested=5,
        result=metric_result(MetricName.PRECISION_AT_K, 5, value=0.6),
    )
    assert ranking().result_fingerprint != ranking(
        entries=tuple(entries)
    ).result_fingerprint


def test_no_metric_level_phase_10_5_fingerprint_exists() -> None:
    """D41: Phase 10.5 mints no identity for a Phase 10.3 artefact."""
    assert "result_fingerprint" not in RankingMetricEntry.__dataclass_fields__
    assert not hasattr(MetricResult, "result_fingerprint")


# ====================================================================
# the context binding and its D44 domain
# ====================================================================


def binding(**overrides) -> ExperimentRunContextBinding:
    fields = {
        "run_context_schema_version": EXPERIMENT_RUN_CONTEXT_SCHEMA_VERSION,
        "experiment_contract_version": EXPERIMENT_CONTRACT_VERSION,
        "dataset_id": DATASET_ID,
        "dataset_content_fingerprint": CONTENT,
        "profile_id": 1,
        "profile_context_fingerprint": PROFILE_CONTEXT,
        "business_metric_run_fingerprint": "1" * 64,
        "ranking_evaluation_run_fingerprint": "2" * 64,
        "context_fingerprint": "0" * 64,
    }
    fields.update(overrides)
    draft = ExperimentRunContextBinding(**fields)
    return replace(
        draft, context_fingerprint=experiment_run_context_fingerprint(draft)
    )


def test_the_context_domain_is_exactly_the_eight_semantic_fields() -> None:
    from evaluation.experiments import canonical_experiment_run_context_payload

    domain = canonical_experiment_run_context_payload(binding())
    assert set(domain) == {
        "run_context_schema_version",
        "experiment_contract_version",
        "dataset_id",
        "dataset_content_fingerprint",
        "profile_id",
        "profile_context_fingerprint",
        "business_metric_run_fingerprint",
        "ranking_evaluation_run_fingerprint",
    }


def test_the_context_digest_survives_recomputation() -> None:
    value = binding()
    assert verify_experiment_run_context_fingerprint(value) == (
        value.context_fingerprint
    )


def test_a_null_ranking_run_is_a_different_context() -> None:
    assert (
        binding(ranking_evaluation_run_fingerprint=None).context_fingerprint
        != binding().context_fingerprint
    )


def test_an_edited_context_binding_fails_its_own_digest() -> None:
    forged = replace(binding(), business_metric_run_fingerprint="9" * 64)
    with pytest.raises(ExperimentBindingError, match="not what it says it is"):
        verify_experiment_run_context_fingerprint(forged)


# ====================================================================
# the provenance block
# ====================================================================


def test_the_provenance_holds_the_five_informative_paths() -> None:
    assert set(ExperimentRunProvenance.__dataclass_fields__) == {
        "generated_at",
        "dataset_directory",
        "business_metric_run_path",
        "label_root",
        "benchmark_records_path",
    }


def test_provenance_fields_default_to_not_stated() -> None:
    provenance = ExperimentRunProvenance()
    assert provenance.generated_at is None
    assert provenance.label_root is None


def test_a_padded_provenance_path_is_refused() -> None:
    with pytest.raises(ExperimentBindingError, match="padded"):
        ExperimentRunProvenance(label_root=" data/labels ")


# ====================================================================
# provenance is validated, and by one authority
# ====================================================================
#
# `provenance` is outside `run_fingerprint` and stays there. Being outside
# identity is not a reason to be unchecked: D50 asks for a structurally verified
# provenance, and the lesson is Phase 10.4's — a block validated only in
# `__post_init__` is unchecked for every object that never ran it.


def _unchecked(cls, **fields):
    """An instance that skipped `__post_init__`. The forger's constructor."""
    forged = object.__new__(cls)
    for name, value in fields.items():
        object.__setattr__(forged, name, value)
    return forged


def _bare_provenance(**overrides):
    """A provenance built through `object.__new__`, so nothing was checked."""
    fields = {
        "generated_at": None,
        "dataset_directory": None,
        "business_metric_run_path": None,
        "label_root": None,
        "benchmark_records_path": None,
    }
    fields.update(overrides)
    return _unchecked(ExperimentRunProvenance, **fields)


def test_an_empty_provenance_is_valid_and_states_nothing() -> None:
    from evaluation.experiments import validate_experiment_run_provenance

    provenance = ExperimentRunProvenance()
    assert validate_experiment_run_provenance(provenance) is provenance


@pytest.mark.parametrize(
    "value",
    [
        "not-a-timestamp",
        "2026-03-01",
        "2026-03-01T09:00:00",
        "2026-03-01 09:00:00+00:00",
        "2026-02-30T09:00:00Z",
        "2026-13-01T09:00:00Z",
        " 2026-03-01T09:00:00Z",
        1,
        True,
    ],
)
def test_a_generated_at_that_is_not_an_rfc_3339_instant_is_refused(value) -> None:
    """`"not-a-timestamp"` used to pass as optional text. It does not now."""
    with pytest.raises(ExperimentBindingError):
        ExperimentRunProvenance(generated_at=value)


@pytest.mark.parametrize(
    "value",
    [
        "2026-03-01T09:00:00Z",
        "2026-03-01T09:00:00z",
        "2026-03-01T09:00:00+00:00",
        "2026-03-01T09:00:00-07:00",
        "2026-03-01T09:00:00.123456Z",
        "2026-03-01t09:00:00Z",
    ],
)
def test_a_real_instant_with_an_offset_is_accepted(value) -> None:
    assert ExperimentRunProvenance(generated_at=value).generated_at == value


@pytest.mark.parametrize(
    "field",
    [
        "dataset_directory",
        "business_metric_run_path",
        "label_root",
        "benchmark_records_path",
    ],
)
def test_a_padded_or_empty_path_is_refused(field) -> None:
    for value in (" data/labels ", "", "   "):
        with pytest.raises(ExperimentBindingError):
            ExperimentRunProvenance(**{field: value})


def test_the_four_paths_are_not_held_to_the_instant_rule() -> None:
    """A path is a path. Only `generated_at` is an instant."""
    provenance = ExperimentRunProvenance(
        dataset_directory="/datasets/x",
        business_metric_run_path="/runs/y",
        label_root="relative/labels",
        benchmark_records_path="gold.jsonl",
    )
    assert provenance.label_root == "relative/labels"


def test_no_path_is_checked_against_the_filesystem() -> None:
    """A run moved to another machine did not become a different experiment."""
    provenance = ExperimentRunProvenance(
        dataset_directory="/nowhere/at/all",
        label_root="/does/not/exist",
    )
    from evaluation.experiments import validate_experiment_run_provenance

    assert validate_experiment_run_provenance(provenance) is provenance


def test_something_that_is_not_a_provenance_is_refused() -> None:
    from evaluation.experiments import validate_experiment_run_provenance

    with pytest.raises(ExperimentContractError, match="not an experiment run"):
        validate_experiment_run_provenance({"generated_at": None})


def test_the_post_init_and_the_verifier_share_one_authority() -> None:
    """One function, two callers — not two copies of the rules."""
    import ast
    import inspect
    import pathlib

    from evaluation.experiments import validate_experiment_run_provenance

    source = pathlib.Path("evaluation/experiments/schema.py").read_text()
    tree = ast.parse(source)
    callers = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "validate_experiment_run_provenance"
        ):
            callers.add(node.lineno)
    # Called from `__post_init__` and from the structural verifier.
    assert len(callers) >= 2
    assert "validate_experiment_run_provenance" in inspect.getsource(
        ExperimentRunProvenance.__post_init__
    )


def test_the_validator_covers_every_field_the_contract_declares() -> None:
    """A field added to the block cannot go quietly unchecked."""
    from evaluation.experiments.schema import PROVENANCE_PATH_FIELDS

    declared = set(ExperimentRunProvenance.__dataclass_fields__)
    assert declared == {"generated_at", *PROVENANCE_PATH_FIELDS}
