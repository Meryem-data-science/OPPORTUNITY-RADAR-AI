"""The observed ranking experiment: four questions, asked of Phase 10.3.

These tests are about `ranking.py` — which ranking may be evaluated, against
which universe, over which membership, and that Phase 10.5 owns no formula. The
mathematics itself is Phase 10.3's and is tested in
`test_evaluation_metric_formulas.py`; what is tested here is that this adapter
asks exactly the four contract questions and records the answers unmodified.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from evaluation.experiments import (
    EXPERIMENT_RANKING_SOURCE,
    EXPERIMENT_RANKING_UNIVERSE_KIND,
    RANKING_EXPERIMENT_QUESTIONS,
    CohortProjection,
    ExperimentBindingError,
    ExperimentContractError,
    ExperimentRunContext,
    RankingEvaluationInputs,
    RankingExperimentStatus,
    RankingExperimentUnavailableReason,
    compute_projection,
    compute_ranking_experiment,
    verify_ranking_experiment_result_fingerprint,
)
from evaluation.metrics import (
    EvidenceClass,
    MetricName,
    MetricStatus,
    MetricUnavailableReason,
    effective_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)
from evaluation.metrics.schema import MetricRunContext

from tests.unit.experiment_fixtures import (
    business_run,
    coverage_of,
    dataset_of,
    evaluation_run_over,
    experiment_context,
    fully_judged,
    manifest_payload,
    ranked_records,
    record,
    recommendation,
    segment,
)


def ranking_of(records=None, **kwargs):
    return compute_ranking_experiment(experiment_context(records, **kwargs))


# ====================================================================
# Phase 10.5 owns no formula
# ====================================================================


def test_the_module_computes_no_ranking_arithmetic_of_its_own() -> None:
    """No DCG, no gain table, no discount, no threshold, no effective_k."""
    import pathlib

    source = pathlib.Path("evaluation/experiments/ranking.py").read_text()
    for forbidden in (
        "def _discounted",
        "def _dcg",
        "def _idcg",
        "relevance_gain",
        "rank_discount",
        "is_relevant_grade",
        "RELEVANT_GRADE_THRESHOLD",
        "def _effective_k",
        "min(k",
    ):
        assert forbidden not in source, forbidden


def test_the_four_authorities_are_phase_10_3s_own_functions() -> None:
    from evaluation.experiments.ranking import _METRIC_AUTHORITIES

    assert _METRIC_AUTHORITIES == {
        MetricName.PRECISION_AT_K: precision_at_k,
        MetricName.RECALL_AT_K: recall_at_k,
        MetricName.NDCG_AT_K: ndcg_at_k,
    }


def test_the_four_results_are_exactly_what_phase_10_3_returns() -> None:
    """Stored unmodified: same status, same value, same reason, same support."""
    records = ranked_records(size=12, ranked=8)
    dataset = dataset_of(records)
    coverage = fully_judged(dataset)
    context = experiment_context(records, coverage=coverage)
    result = compute_ranking_experiment(context)

    inputs = context.ranking_evaluation_inputs
    metric_context = MetricRunContext(
        dataset=dataset, run=inputs.evaluation_run, coverage=coverage
    )
    expected = {
        (MetricName.PRECISION_AT_K, 5): precision_at_k(metric_context, 5),
        (MetricName.PRECISION_AT_K, 10): precision_at_k(metric_context, 10),
        (MetricName.RECALL_AT_K, 10): recall_at_k(metric_context, 10),
        (MetricName.NDCG_AT_K, 10): ndcg_at_k(metric_context, 10),
    }
    for entry in result.metric_results:
        direct = expected[(entry.metric, entry.k_requested)]
        assert entry.result.status is direct.status
        assert entry.result.value == direct.value
        assert entry.result.reason is direct.reason
        assert entry.result.support == direct.support


def test_effective_k_stays_phase_10_3s_min_of_k_and_length() -> None:
    """A ranking of 6 asked at K=10 reports an effective 6, not a padded 10."""
    records = ranked_records(size=10, ranked=6)
    dataset = dataset_of(records)
    context = experiment_context(records, coverage=fully_judged(dataset))
    result = compute_ranking_experiment(context)
    ranking = context.ranking_evaluation_inputs.evaluation_run.ranking
    for entry in result.metric_results:
        assert entry.result.support.k_effective == effective_k(
            entry.k_requested, ranking
        )
    assert result.entry(MetricName.PRECISION_AT_K, 10).result.support.k_effective == 6
    assert result.entry(MetricName.PRECISION_AT_K, 5).result.support.k_effective == 5


# ====================================================================
# exactly four questions
# ====================================================================


def test_a_computed_block_answers_exactly_the_four_questions_in_order() -> None:
    result = ranking_of(ranked_records(size=12, ranked=8))
    assert result.status is RankingExperimentStatus.COMPUTED
    assert [
        (entry.metric, entry.k_requested) for entry in result.metric_results
    ] == list(RANKING_EXPERIMENT_QUESTIONS)


def test_compute_ranking_experiment_takes_no_k_parameter() -> None:
    """The cut-offs are the contract's, not a caller's."""
    import inspect

    signature = inspect.signature(compute_ranking_experiment)
    assert list(signature.parameters) == ["context"]


def test_the_public_surface_is_the_one_function() -> None:
    import evaluation.experiments.ranking as module

    assert module.__all__ == ["compute_ranking_experiment"]


# ====================================================================
# the universe and the ranking under evaluation
# ====================================================================


def test_the_universe_is_the_whole_frozen_cohort() -> None:
    records = ranked_records(size=12, ranked=8)
    context = experiment_context(records)
    result = compute_ranking_experiment(context)
    run = context.ranking_evaluation_inputs.evaluation_run
    assert run.universe.kind is EXPERIMENT_RANKING_UNIVERSE_KIND
    assert run.universe.size == 12
    assert result.evaluation_universe_fingerprint == run.universe.fingerprint
    for entry in result.metric_results:
        assert entry.result.support.universe_size == 12


def test_the_universe_is_never_the_observed_recommendation_set() -> None:
    """A recall over the ranked set could not see a posting the ranking missed."""
    records = ranked_records(size=12, ranked=8)
    context = experiment_context(records)
    observed = compute_projection(context, CohortProjection.RECOMMENDATION_OBSERVED)
    run = context.ranking_evaluation_inputs.evaluation_run
    assert observed.included_count == 8
    assert run.universe.size == 12
    assert run.universe.size != observed.included_count


def test_only_the_frozen_recommendation_ranking_is_evaluated() -> None:
    records = ranked_records(size=10, ranked=6)
    context = experiment_context(records)
    run = context.ranking_evaluation_inputs.evaluation_run
    assert run.ranking.source is EXPERIMENT_RANKING_SOURCE


def test_a_posting_the_run_never_covered_is_absent_not_last() -> None:
    records = ranked_records(size=10, ranked=6)
    context = experiment_context(records)
    run = context.ranking_evaluation_inputs.evaluation_run
    assert run.ranking.length == 6
    assert set(run.ranking.opportunity_ids) == {1, 2, 3, 4, 5, 6}
    for unranked in (7, 8, 9, 10):
        assert unranked not in run.ranking.opportunity_ids


# ====================================================================
# the membership equality
# ====================================================================


def test_the_ranking_members_are_the_observed_recommendation_set() -> None:
    records = ranked_records(size=12, ranked=8)
    context = experiment_context(records)
    observed = compute_projection(context, CohortProjection.RECOMMENDATION_OBSERVED)
    run = context.ranking_evaluation_inputs.evaluation_run
    assert set(run.ranking.opportunity_ids) == set(observed.included_ids)
    result = compute_ranking_experiment(context)
    assert result.recommendation_projection_result_fingerprint == (
        observed.result_fingerprint
    )


def test_a_ranking_over_another_snapshot_is_refused() -> None:
    """Two derivations of one fact, held against each other."""
    records = ranked_records(size=10, ranked=6)
    other = ranked_records(size=10, ranked=9)
    other_dataset = dataset_of(other)
    coverage = fully_judged(other_dataset)
    with pytest.raises(Exception):
        compute_ranking_experiment(
            ExperimentRunContext(
                frozen_dataset=dataset_of(records),
                manifest=manifest_payload(records),
                business_metric_run=business_run(records),
                ranking_evaluation_inputs=RankingEvaluationInputs(
                    evaluation_run=evaluation_run_over(other_dataset, coverage),
                    label_coverage=coverage,
                ),
            )
        )


def test_the_membership_is_compared_as_a_set_not_as_a_sequence() -> None:
    """The production order need not be `opportunity_id ASC`, and usually isn't."""
    records = (
        record(
            1,
            geography_segments=(segment("RESOLVED"),),
            recommendation=recommendation(3),
        ),
        record(
            2,
            geography_segments=(segment("RESOLVED"),),
            recommendation=recommendation(1),
        ),
        record(
            3,
            geography_segments=(segment("RESOLVED"),),
            recommendation=recommendation(2),
        ),
    )
    context = experiment_context(records)
    run = context.ranking_evaluation_inputs.evaluation_run
    # Ranked 2, 3, 1 — not ascending.
    assert run.ranking.opportunity_ids == (2, 3, 1)
    observed = compute_projection(context, CohortProjection.RECOMMENDATION_OBSERVED)
    assert observed.included_ids == (1, 2, 3)
    result = compute_ranking_experiment(context)
    assert result.status is RankingExperimentStatus.COMPUTED


# ====================================================================
# the one whole-block refusal
# ====================================================================


def test_an_empty_observed_recommendation_is_the_one_block_refusal() -> None:
    records = ranked_records(size=6, ranked=0)
    context = experiment_context(records, with_ranking=False)
    result = compute_ranking_experiment(context)
    assert result.status is RankingExperimentStatus.N_A
    assert result.unavailable_reason is (
        RankingExperimentUnavailableReason.NO_OBSERVED_RECOMMENDATION_RANKING
    )
    assert result.metric_results == ()
    assert result.evaluation_run_fingerprint is None
    assert result.evidence_class is None


def test_the_refusal_names_the_projection_that_licenses_it() -> None:
    records = ranked_records(size=6, ranked=0)
    context = experiment_context(records, with_ranking=False)
    observed = compute_projection(context, CohortProjection.RECOMMENDATION_OBSERVED)
    result = compute_ranking_experiment(context)
    assert observed.included_count == 0
    assert result.recommendation_projection_result_fingerprint == (
        observed.result_fingerprint
    )


def test_a_non_empty_observed_recommendation_demands_a_real_ranking() -> None:
    """No inputs for a snapshot that has a ranking is a precondition failure."""
    records = ranked_records(size=8, ranked=5)
    with pytest.raises(ExperimentBindingError, match="requires the Phase 10.3"):
        compute_ranking_experiment(
            ExperimentRunContext(
                frozen_dataset=dataset_of(records),
                manifest=manifest_payload(records),
                business_metric_run=business_run(records),
                ranking_evaluation_inputs=None,
            )
        )


def test_an_empty_label_coverage_is_a_precondition_failure_not_an_na() -> None:
    records = ranked_records(size=8, ranked=5)
    dataset = dataset_of(records)
    coverage = fully_judged(dataset)
    empty = replace(coverage, labels=())
    with pytest.raises(ExperimentBindingError, match="non-empty labelset"):
        compute_ranking_experiment(
            ExperimentRunContext(
                frozen_dataset=dataset,
                manifest=manifest_payload(records),
                business_metric_run=business_run(records),
                ranking_evaluation_inputs=RankingEvaluationInputs(
                    evaluation_run=evaluation_run_over(dataset, coverage),
                    label_coverage=empty,
                ),
            )
        )


# ====================================================================
# partial labels produce Phase 10.3's own refusals, inside a COMPUTED block
# ====================================================================


def test_a_partially_judged_top_k_is_phase_10_3s_own_na() -> None:
    records = ranked_records(size=10, ranked=6)
    dataset = dataset_of(records)
    # Only the first two postings judged: positions 3..6 of the top K are not.
    coverage = coverage_of({1: 3, 2: 1}, dataset)
    result = ranking_of(records, coverage=coverage)
    assert result.status is RankingExperimentStatus.COMPUTED
    precision = result.entry(MetricName.PRECISION_AT_K, 5).result
    assert precision.status is MetricStatus.N_A
    assert precision.reason is MetricUnavailableReason.TOP_K_NOT_FULLY_JUDGED


def test_a_partially_judged_universe_makes_recall_and_ndcg_na() -> None:
    records = ranked_records(size=10, ranked=6)
    dataset = dataset_of(records)
    coverage = coverage_of(
        {index: 3 if index <= 3 else 1 for index in range(1, 7)}, dataset
    )
    result = ranking_of(records, coverage=coverage)
    assert result.status is RankingExperimentStatus.COMPUTED
    recall = result.entry(MetricName.RECALL_AT_K, 10).result
    ndcg = result.entry(MetricName.NDCG_AT_K, 10).result
    assert recall.status is MetricStatus.N_A
    assert recall.reason is MetricUnavailableReason.RECALL_DENOMINATOR_UNKNOWN
    assert ndcg.status is MetricStatus.N_A
    assert ndcg.reason is (
        MetricUnavailableReason.EVALUATION_UNIVERSE_NOT_FULLY_JUDGED
    )
    # And the top-6 precision is computable, because that top *is* fully judged.
    assert (
        result.entry(MetricName.PRECISION_AT_K, 5).result.status
        is MetricStatus.COMPUTED
    )


def test_a_fully_judged_universe_computes_all_four() -> None:
    records = ranked_records(size=10, ranked=6)
    dataset = dataset_of(records)
    result = ranking_of(records, coverage=fully_judged(dataset))
    for entry in result.metric_results:
        assert entry.result.status is MetricStatus.COMPUTED
        assert entry.result.value is not None


def test_no_unavailable_metric_is_ever_reported_as_a_zero() -> None:
    records = ranked_records(size=10, ranked=6)
    dataset = dataset_of(records)
    result = ranking_of(records, coverage=coverage_of({1: 3}, dataset))
    for entry in result.metric_results:
        if entry.result.status is MetricStatus.N_A:
            assert entry.result.value is None
            assert entry.result.reason is not None


# ====================================================================
# the evidence class stays diagnostic
# ====================================================================


def test_the_evidence_class_is_read_off_the_bound_run_and_stays_diagnostic() -> None:
    records = ranked_records(size=10, ranked=6)
    context = experiment_context(records)
    result = compute_ranking_experiment(context)
    assert result.evidence_class is EvidenceClass.DIAGNOSTIC_CALIBRATION
    assert (
        result.evidence_class
        is context.ranking_evaluation_inputs.evaluation_run.evidence_class
    )


def test_nothing_here_can_promote_a_calibration_to_a_benchmark() -> None:
    """No parameter exists, and Phase 10.3's own gate fails closed."""
    import inspect

    source = inspect.getsource(compute_ranking_experiment)
    assert "INDEPENDENT_BENCHMARK" not in source
    assert "evidence_class=" not in source.split("return _sealed")[0]
    from evaluation.metrics import INDEPENDENT_BENCHMARK_PROTOCOL_VERSIONS

    assert INDEPENDENT_BENCHMARK_PROTOCOL_VERSIONS == ()


# ====================================================================
# identity
# ====================================================================


def test_the_ranking_result_survives_its_own_verifier() -> None:
    result = ranking_of(ranked_records(size=10, ranked=6))
    assert verify_ranking_experiment_result_fingerprint(result) == (
        result.result_fingerprint
    )


def test_the_result_is_deterministic_across_two_computations() -> None:
    context = experiment_context(ranked_records(size=10, ranked=6))
    first = compute_ranking_experiment(context)
    second = compute_ranking_experiment(context)
    assert first.result_fingerprint == second.result_fingerprint


def test_a_different_labelset_is_a_different_ranking_result() -> None:
    records = ranked_records(size=10, ranked=6)
    dataset = dataset_of(records)
    generous = ranking_of(records, coverage=fully_judged(dataset, relevant_upto=6))
    strict = ranking_of(records, coverage=fully_judged(dataset, relevant_upto=2))
    assert generous.result_fingerprint != strict.result_fingerprint
    assert generous.labelset_fingerprint != strict.labelset_fingerprint


def test_an_edited_metric_value_fails_the_declared_digest() -> None:
    result = ranking_of(ranked_records(size=10, ranked=6))
    entry = result.metric_results[0]
    forged = replace(
        result,
        metric_results=(
            replace(entry, result=replace(entry.result, value=0.999)),
            *result.metric_results[1:],
        ),
    )
    with pytest.raises(ExperimentBindingError, match="not what it says it is"):
        verify_ranking_experiment_result_fingerprint(forged)
