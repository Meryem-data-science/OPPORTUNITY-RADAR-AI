"""Phase 10.3b: the three ranking metrics, checked against arithmetic by hand.

Every expected NDCG below is written out as the sum it is — `3/log2(2)`, not
`0.7397`— so a reader can check the test against the contract rather than
against a number somebody once printed. No metrics library is imported: the
point is to verify this implementation, and a second implementation agreeing
with it proves only that two people made the same choice of gain function.

Nothing here reads the operational database, writes a label or touches a real
opportunity. The artefacts are the invented ones in
`tests/unit/evaluation_metric_fixtures.py`.
"""

from dataclasses import replace
from math import log2

import pytest

from evaluation.dataset import EvaluationDatasetError
from evaluation.labeling import HumanLabelError, select_calibration_sample
from evaluation.metrics import (
    EvaluationBindingError,
    EvaluationMetricsError,
    EvaluationUniverseKind,
    MetricArgumentError,
    MetricName,
    MetricRunContext,
    MetricStatus,
    MetricUnavailableReason,
    build_evaluation_universe,
    build_metric_run_context,
    discounted_cumulative_gain,
    evaluation_run_fingerprint,
    evaluation_universe_fingerprint,
    ideal_grades_at_cutoff,
    metric_result_payload,
    ndcg_at_k,
    ndcg_at_k_availability,
    precision_at_k,
    precision_at_k_availability,
    rank_discount,
    recall_at_k,
    recall_at_k_availability,
    relevance_gain,
    top_k_judged_coverage,
    universe_judged_coverage,
)
from tests.unit.evaluation_metric_fixtures import (
    context_of,
    coverage_of,
    ranked_dataset,
    run_over,
)

#: Twenty opportunities, the first twelve of them ranked in id order — so the
#: ranking is `1, 2, ..., 12` and "the grade at rank r" is "the grade of
#: opportunity r". That keeps the hand arithmetic below readable.
COHORT = 20
RANKED = 12


@pytest.fixture
def dataset():
    return ranked_dataset(size=COHORT, ranked=RANKED)


def graded(dataset, grades: dict[int, int], **kwargs):
    """A verified context whose universe carries exactly these judgements."""
    coverage = coverage_of(grades, dataset)
    return context_of(dataset, coverage, **kwargs)


def all_graded(dataset, grade: int):
    return graded(dataset, dict.fromkeys(range(1, COHORT + 1), grade))


def dcg(*grades: int) -> float:
    """The contract's DCG, spelled out position by position in the test."""
    return sum(
        (2**grade - 1) / log2(position + 1)
        for position, grade in enumerate(grades, start=1)
    )


# --------------------------------------------------------------------------
# the frozen definitions
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "grade, gain", [(0, 0.0), (1, 1.0), (2, 3.0), (3, 7.0)]
)
def test_the_gain_is_exponential_and_exact(grade, gain):
    assert relevance_gain(grade) == gain


@pytest.mark.parametrize("grade", [99, -1, 4, True, "2", 2.0, None])
def test_a_grade_outside_the_frozen_scale_has_no_gain(grade):
    with pytest.raises(MetricArgumentError):
        relevance_gain(grade)


def test_the_discount_is_log2_of_the_rank_plus_one():
    assert rank_discount(1) == 1.0
    assert rank_discount(2) == log2(3)
    assert rank_discount(3) == 2.0
    assert rank_discount(7) == 3.0
    # Never zero, which is what lets the gate reason about gains alone.
    assert all(rank_discount(position) >= 1.0 for position in range(1, 200))


@pytest.mark.parametrize("position", [0, -1, True, 1.0, "1", None])
def test_a_malformed_rank_has_no_discount(position):
    with pytest.raises(EvaluationMetricsError):
        rank_discount(position)


def test_the_dcg_sum_is_the_contract_written_out():
    """`approx` here only because summing the same terms in another order moves
    the last bit of the mantissa — the terms themselves are exact."""
    assert discounted_cumulative_gain([3, 2, 0, 1]) == pytest.approx(
        7 / log2(2) + 3 / log2(3) + 0 / log2(4) + 1 / log2(5), rel=1e-15
    )
    assert discounted_cumulative_gain([]) == 0
    assert discounted_cumulative_gain([0, 0, 0]) == 0.0


# --------------------------------------------------------------------------
# Precision@K
# --------------------------------------------------------------------------


def test_a_perfectly_relevant_top_k_scores_one(dataset):
    context = all_graded(dataset, 3)
    result = precision_at_k(context, 10)
    assert result.status is MetricStatus.COMPUTED
    assert result.metric is MetricName.PRECISION_AT_K
    assert result.value == 1.0
    assert result.reason is None
    assert result.support.numerator == 10.0
    assert result.support.denominator == 10.0


def test_a_top_k_with_nothing_relevant_scores_zero(dataset):
    result = precision_at_k(all_graded(dataset, 0), 10)
    assert result.value == 0.0
    assert result.support.numerator == 0.0
    assert result.support.denominator == 10.0


def test_grade_one_is_not_relevant_and_grades_two_and_three_are(dataset):
    """The binary threshold, exercised across the whole scale at once.

    Ranks 1-4 are graded 3, 2, 1, 0 and repeat, so the top 8 holds four relevant
    postings and four that are not — and the two that are not include a grade 1,
    which is the one a lazy threshold would count.
    """
    grades = {
        opportunity_id: (3, 2, 1, 0)[(opportunity_id - 1) % 4]
        for opportunity_id in range(1, COHORT + 1)
    }
    context = graded(dataset, grades)
    result = precision_at_k(context, 8)
    assert result.value == 0.5
    assert result.support.numerator == 4.0
    assert result.support.denominator == 8.0
    # The grade-1 postings are judged, present, and counted as misses.
    assert context.coverage.grade(3) == 1
    assert precision_at_k(context, 4).value == 0.5


def test_k_beyond_the_ranking_is_capped_and_both_numbers_are_kept(dataset):
    grades = {
        opportunity_id: (3 if opportunity_id <= 6 else 0)
        for opportunity_id in range(1, COHORT + 1)
    }
    result = precision_at_k(graded(dataset, grades), 100)
    assert result.support.k_requested == 100
    assert result.support.k_effective == RANKED
    assert result.support.ranking_length == RANKED
    assert result.value == 6 / RANKED
    assert result.support.denominator == float(RANKED)


def test_one_unjudged_item_in_the_top_k_is_never_a_zero(dataset):
    """The rule the whole phase exists for, at the formula boundary."""
    grades = dict.fromkeys(range(1, COHORT + 1), 3)
    del grades[4]
    result = precision_at_k(graded(dataset, grades), 10)
    assert result.status is MetricStatus.N_A
    assert result.value is None
    assert result.reason is MetricUnavailableReason.TOP_K_NOT_FULLY_JUDGED
    assert result.support.unjudged_in_top_k == (4,)
    # Had the unjudged item been read as a 0, this would have been 0.9.
    assert result.support.numerator is None
    assert result.support.denominator is None


@pytest.mark.parametrize("k", [True, False])
def test_a_boolean_k_is_refused_by_every_formula(dataset, k):
    context = all_graded(dataset, 3)
    for metric in (precision_at_k, recall_at_k, ndcg_at_k):
        with pytest.raises(MetricArgumentError, match="positive integer"):
            metric(context, k)


@pytest.mark.parametrize("k", [0, -1, 1.0, "3", None])
def test_a_malformed_k_is_refused_by_every_formula(dataset, k):
    context = all_graded(dataset, 3)
    for metric in (precision_at_k, recall_at_k, ndcg_at_k):
        with pytest.raises(MetricArgumentError):
            metric(context, k)


# --------------------------------------------------------------------------
# Recall@K
# --------------------------------------------------------------------------


def test_recall_is_one_when_the_top_k_holds_every_relevant_item(dataset):
    grades = {
        opportunity_id: (3 if opportunity_id <= 5 else 0)
        for opportunity_id in range(1, COHORT + 1)
    }
    result = recall_at_k(graded(dataset, grades), 10)
    assert result.value == 1.0
    assert result.support.numerator == 5.0
    assert result.support.denominator == 5.0
    assert result.support.relevant_count == 5


def test_a_relevant_item_below_the_cut_off_is_in_the_denominator(dataset):
    """Ranked, relevant, and not surfaced in the top 5 — recall must notice."""
    grades = {opportunity_id: 0 for opportunity_id in range(1, COHORT + 1)}
    grades.update({1: 3, 2: 2, 11: 3, 12: 2})
    result = recall_at_k(graded(dataset, grades), 5)
    assert result.support.numerator == 2.0
    assert result.support.denominator == 4.0
    assert result.value == 0.5


def test_a_relevant_item_the_ranking_never_showed_is_in_the_denominator(dataset):
    """The candidate false negative: in the universe, never ranked at all.

    Opportunity 15 is in the frozen cohort and carries no recommendation, so it
    is in the universe and outside the ranking. A recall whose denominator came
    from the ranking would score 1.0 here and be wrong in the most flattering
    direction available.
    """
    grades = {opportunity_id: 0 for opportunity_id in range(1, COHORT + 1)}
    grades.update({1: 3, 15: 3})
    context = graded(dataset, grades)
    assert 15 not in context.ranking.opportunity_ids
    assert 15 in context.universe.opportunity_ids

    result = recall_at_k(context, 10)
    assert result.support.numerator == 1.0
    assert result.support.denominator == 2.0
    assert result.value == 0.5


def test_grade_one_is_not_counted_in_either_half_of_recall(dataset):
    grades = {opportunity_id: 1 for opportunity_id in range(1, COHORT + 1)}
    grades.update({1: 2, 14: 2})
    result = recall_at_k(graded(dataset, grades), 10)
    assert result.support.relevant_count == 2
    assert result.support.numerator == 1.0
    assert result.value == 0.5


def test_a_partly_judged_universe_has_no_recall_denominator(dataset):
    grades = dict.fromkeys(range(1, RANKED + 1), 3)
    result = recall_at_k(graded(dataset, grades), 10)
    assert result.status is MetricStatus.N_A
    assert result.value is None
    assert result.reason is MetricUnavailableReason.RECALL_DENOMINATOR_UNKNOWN
    assert result.support.relevant_count is None


def test_a_fully_judged_universe_with_nothing_relevant_has_no_recall(dataset):
    result = recall_at_k(all_graded(dataset, 1), 10)
    assert result.status is MetricStatus.N_A
    assert result.value is None
    assert result.reason is MetricUnavailableReason.NO_RELEVANT_ITEMS
    assert result.support.relevant_count == 0


# --------------------------------------------------------------------------
# NDCG@K
# --------------------------------------------------------------------------


def test_the_ideal_ranking_scores_one(dataset):
    """Grades descending down the ranking, and the universe holds no better."""
    grades = {opportunity_id: 0 for opportunity_id in range(1, COHORT + 1)}
    grades.update({1: 3, 2: 3, 3: 2, 4: 2, 5: 1})
    result = ndcg_at_k(graded(dataset, grades), 5)
    assert result.status is MetricStatus.COMPUTED
    assert result.value == 1.0
    assert result.support.numerator == result.support.denominator
    assert result.support.numerator == dcg(3, 3, 2, 2, 1)


def test_inverting_two_grades_gives_the_hand_computed_value(dataset):
    """Ranks 1 and 2 swapped: 2 then 3 instead of 3 then 2, over a K of 2.

        DCG  = (2**2 - 1)/log2(2) + (2**3 - 1)/log2(3) = 3/1 + 7/1.58496...
        IDCG = (2**3 - 1)/log2(2) + (2**2 - 1)/log2(3) = 7/1 + 3/1.58496...
    """
    grades = {opportunity_id: 0 for opportunity_id in range(1, COHORT + 1)}
    grades.update({1: 2, 2: 3})
    result = ndcg_at_k(graded(dataset, grades), 2)

    expected_dcg = 3 / log2(2) + 7 / log2(3)
    expected_idcg = 7 / log2(2) + 3 / log2(3)
    assert result.support.numerator == expected_dcg
    assert result.support.denominator == expected_idcg
    assert result.value == expected_dcg / expected_idcg
    assert 0.0 < result.value < 1.0


def test_the_ideal_is_built_from_the_whole_universe_not_from_the_ranking(dataset):
    """The best posting is unranked, so a perfect ranking still cannot score 1.

    Opportunity 15 is graded 3 and carries no recommendation. An IDCG built from
    the ranked items would not see it and would report 1.0; the contract builds
    the ideal from every grade in the declared universe, so the ceiling is
    higher than anything this ranking can reach.
    """
    grades = {opportunity_id: 0 for opportunity_id in range(1, COHORT + 1)}
    grades.update({1: 2, 2: 1, 15: 3})
    context = graded(dataset, grades)
    result = ndcg_at_k(context, 3)

    assert result.support.numerator == dcg(2, 1, 0)
    assert result.support.denominator == dcg(3, 2, 1)
    assert result.value == dcg(2, 1, 0) / dcg(3, 2, 1)
    assert result.value < 1.0
    assert ideal_grades_at_cutoff(context.universe, context.coverage, 3) == (3, 2, 1)


def test_the_ideal_cut_off_takes_the_best_grades_in_order(dataset):
    grades = {
        opportunity_id: (opportunity_id % 4) for opportunity_id in range(1, COHORT + 1)
    }
    context = graded(dataset, grades)
    ideal = ideal_grades_at_cutoff(context.universe, context.coverage, 6)
    assert ideal == (3, 3, 3, 3, 3, 2)
    assert list(ideal) == sorted(ideal, reverse=True)


def test_ndcg_over_a_partly_judged_universe_is_unavailable(dataset):
    grades = dict.fromkeys(range(1, RANKED + 1), 3)
    result = ndcg_at_k(graded(dataset, grades), 5)
    assert result.status is MetricStatus.N_A
    assert result.value is None
    assert (
        result.reason is MetricUnavailableReason.EVALUATION_UNIVERSE_NOT_FULLY_JUDGED
    )


def test_a_universe_graded_zero_throughout_has_no_ndcg(dataset):
    """IDCG = 0, so the ratio is undefined — and says so, in both places."""
    context = all_graded(dataset, 0)
    availability = ndcg_at_k_availability(context, 5)
    assert not availability.available
    assert availability.reason is MetricUnavailableReason.ZERO_IDEAL_DCG

    result = ndcg_at_k(context, 5)
    assert result.status is MetricStatus.N_A
    assert result.value is None
    assert result.reason is MetricUnavailableReason.ZERO_IDEAL_DCG
    # Not a 0.0, not a 1.0, not a NaN, not a division error.
    assert result.support.numerator is None
    assert result.support.denominator is None


def test_a_universe_of_grade_one_has_an_ndcg_but_no_recall(dataset):
    """The exact case `ZERO_IDEAL_DCG` exists to keep apart from Recall's code.

    Binary relevance sees nothing relevant; graded relevance sees `gain(1) == 1`
    everywhere, so the ideal is worth something and NDCG is perfectly defined.
    """
    context = all_graded(dataset, 1)
    recall = recall_at_k(context, 5)
    assert recall.reason is MetricUnavailableReason.NO_RELEVANT_ITEMS

    result = ndcg_at_k(context, 5)
    assert result.status is MetricStatus.COMPUTED
    assert result.value == 1.0
    assert result.support.numerator == dcg(1, 1, 1, 1, 1)
    assert result.support.denominator == dcg(1, 1, 1, 1, 1)


def test_ndcg_with_k_beyond_the_ranking_uses_the_effective_cut_off(dataset):
    grades = {opportunity_id: 0 for opportunity_id in range(1, COHORT + 1)}
    grades.update({1: 3, 2: 2})
    result = ndcg_at_k(graded(dataset, grades), 100)
    assert result.support.k_requested == 100
    assert result.support.k_effective == RANKED
    assert result.support.numerator == dcg(3, 2, *([0] * 10))
    assert result.support.denominator == dcg(3, 2, *([0] * 10))
    assert result.value == 1.0


def test_every_ndcg_value_stays_within_the_unit_interval(dataset):
    """A property over a spread of judged universes, with no clamping anywhere."""
    for offset in range(4):
        grades = {
            opportunity_id: (opportunity_id + offset) % 4
            for opportunity_id in range(1, COHORT + 1)
        }
        context = graded(dataset, grades)
        for k in (1, 3, 5, 12, 40):
            result = ndcg_at_k(context, k)
            if result.status is MetricStatus.N_A:
                assert result.reason is MetricUnavailableReason.ZERO_IDEAL_DCG
                continue
            assert 0.0 <= result.value <= 1.0 + 1e-12


# --------------------------------------------------------------------------
# coverage statistics beside the scores
# --------------------------------------------------------------------------


def test_the_two_coverage_ratios_are_the_declared_denominators(dataset):
    """Judged coverage@K over `k_effective`; labelled coverage over the universe.

    Never `labels / labels`, which is the ratio that is always 1.0 and always
    tells nobody anything.
    """
    grades = dict.fromkeys(range(1, 9), 2)
    context = graded(dataset, grades)

    top = top_k_judged_coverage(context.ranking, context.coverage, 10)
    assert top.judged_count == 8
    assert top.total_count == 10
    assert top.ratio == 0.8
    assert top.unjudged_opportunity_ids == (9, 10)

    universe = universe_judged_coverage(context.universe, context.coverage)
    assert universe.judged_count == 8
    assert universe.total_count == COHORT
    assert universe.ratio == 8 / COHORT
    assert not universe.fully_judged


# --------------------------------------------------------------------------
# the formulas are no less safe than the gates
# --------------------------------------------------------------------------


def resealed_universe(run, **changes):
    """A universe mutated and re-digested, inside a run also re-digested."""
    universe = replace(run.universe, **changes)
    universe = replace(
        universe, fingerprint=evaluation_universe_fingerprint(universe)
    )
    forged = replace(run, universe=universe)
    return replace(forged, run_fingerprint=evaluation_run_fingerprint(forged))


@pytest.mark.parametrize("metric", [precision_at_k, recall_at_k, ndcg_at_k])
def test_a_forged_context_is_refused_by_the_formula_as_well_as_the_gate(
    dataset, metric
):
    """The formula must not be a second, weaker door into the same house."""
    coverage = coverage_of(dict.fromkeys(range(1, COHORT + 1), 3), dataset)
    run = run_over(dataset, coverage=coverage)
    forged = resealed_universe(run, opportunity_ids=(1, 1, 2), size=3)
    context = MetricRunContext(dataset=dataset, run=forged, coverage=coverage)
    with pytest.raises(EvaluationMetricsError, match="repeats opportunity ids"):
        metric(context, 10)


@pytest.mark.parametrize("metric", [precision_at_k, recall_at_k, ndcg_at_k])
def test_an_opportunity_absent_from_the_dataset_is_refused(dataset, metric):
    coverage = coverage_of(dict.fromkeys(range(1, COHORT + 1), 3), dataset)
    run = run_over(dataset, coverage=coverage)
    members = run.universe.opportunity_ids[:-1] + (999,)
    forged = resealed_universe(
        run,
        kind=EvaluationUniverseKind.FIXED_BENCHMARK_POOL,
        opportunity_ids=members,
        size=len(members),
    )
    context = MetricRunContext(dataset=dataset, run=forged, coverage=coverage)
    with pytest.raises(EvaluationBindingError, match="absent from dataset"):
        metric(context, 10)


@pytest.mark.parametrize("metric", [precision_at_k, recall_at_k, ndcg_at_k])
def test_a_context_for_another_dataset_or_profile_is_refused(dataset, metric):
    coverage = coverage_of(dict.fromkeys(range(1, COHORT + 1), 3), dataset)
    run = run_over(dataset, coverage=coverage)

    other = ranked_dataset(
        size=COHORT, ranked=RANKED, dataset_id="evaluation-dataset-v3-" + "c" * 16
    )
    with pytest.raises(EvaluationDatasetError):
        metric(MetricRunContext(dataset=other, run=run, coverage=coverage), 10)

    profile_other = ranked_dataset(
        size=COHORT, ranked=RANKED, profile_context_fingerprint="4" * 64
    )
    with pytest.raises(EvaluationDatasetError):
        metric(
            MetricRunContext(
                dataset=profile_other,
                run=run,
                coverage=coverage_of({1: 2}, profile_other),
            ),
            10,
        )


@pytest.mark.parametrize("metric", [precision_at_k, recall_at_k, ndcg_at_k])
def test_a_different_labelset_yields_n_a_and_never_a_number(dataset, metric):
    grades = dict.fromkeys(range(1, COHORT + 1), 3)
    run = run_over(dataset, coverage=coverage_of(grades, dataset))
    other_lot = coverage_of(
        grades,
        dataset,
        selection=select_calibration_sample(dataset, sample_size=6),
    )
    context = MetricRunContext(dataset=dataset, run=run, coverage=other_lot)

    result = metric(context, 10)
    assert result.status is MetricStatus.N_A
    assert result.value is None
    assert result.reason is MetricUnavailableReason.LABELSET_BINDING_MISMATCH


@pytest.mark.parametrize("metric", [precision_at_k, recall_at_k, ndcg_at_k])
def test_a_tampered_grade_is_refused_by_the_formula(dataset, metric):
    """The labelset digest is recomputed by the gate before a grade is read."""
    coverage = coverage_of(dict.fromkeys(range(1, COHORT + 1), 0), dataset)
    run = run_over(dataset, coverage=coverage)
    tampered = replace(
        coverage,
        labels=(replace(coverage.labels[0], relevance_grade=3),)
        + coverage.labels[1:],
    )
    context = MetricRunContext(dataset=dataset, run=run, coverage=tampered)
    with pytest.raises(EvaluationBindingError, match="judgements it holds"):
        metric(context, 10)


@pytest.mark.parametrize("metric", [precision_at_k, recall_at_k, ndcg_at_k])
def test_a_grade_outside_the_frozen_scale_never_reaches_a_score(dataset, metric):
    coverage = coverage_of(dict.fromkeys(range(1, COHORT + 1), 2), dataset)
    run = run_over(dataset, coverage=coverage)
    broken = replace(
        coverage,
        labels=(replace(coverage.labels[0], relevance_grade=99),)
        + coverage.labels[1:],
    )
    context = MetricRunContext(dataset=dataset, run=run, coverage=broken)
    with pytest.raises(HumanLabelError, match="relevance_grade"):
        metric(context, 10)


def test_the_gate_and_the_formula_agree_on_every_unavailable_case(dataset):
    """A formula never converts an `N_A` into something a gate did not say."""
    cases = [
        (dict.fromkeys(range(1, RANKED + 1), 3), 10),
        (dict.fromkeys(range(1, COHORT + 1), 0), 5),
        (dict.fromkeys(range(1, COHORT + 1), 1), 5),
        ({index: 3 for index in range(1, COHORT + 1) if index != 4}, 10),
    ]
    for grades, k in cases:
        context = graded(dataset, grades)
        for metric, gate in (
            (precision_at_k, precision_at_k_availability),
            (recall_at_k, recall_at_k_availability),
            (ndcg_at_k, ndcg_at_k_availability),
        ):
            availability = gate(context, k)
            result = metric(context, k)
            if availability.available:
                assert result.status is MetricStatus.COMPUTED
                assert result.value is not None
            else:
                assert result.status is MetricStatus.N_A
                assert result.reason is availability.reason
                assert result.value is None


# --------------------------------------------------------------------------
# determinism and the payload
# --------------------------------------------------------------------------


def test_the_same_context_and_k_give_the_same_result_and_payload(dataset):
    grades = {
        opportunity_id: (opportunity_id % 4) for opportunity_id in range(1, COHORT + 1)
    }
    context = graded(dataset, grades)
    rebuilt = build_metric_run_context(
        dataset=dataset, run=context.run, coverage=context.coverage
    )
    for metric in (precision_at_k, recall_at_k, ndcg_at_k):
        first = metric(context, 7)
        second = metric(rebuilt, 7)
        assert first == second
        assert metric_result_payload(first) == metric_result_payload(second)


def test_a_computed_payload_states_its_fraction_and_its_cut_off(dataset):
    grades = {opportunity_id: 0 for opportunity_id in range(1, COHORT + 1)}
    grades.update({1: 3, 2: 2, 3: 1})
    payload = metric_result_payload(precision_at_k(graded(dataset, grades), 4))
    assert payload["metric"] == "PRECISION_AT_K"
    assert payload["status"] == "COMPUTED"
    assert payload["value"] == 0.5
    assert payload["reason"] is None
    assert payload["support"]["numerator"] == 2.0
    assert payload["support"]["denominator"] == 4.0
    assert payload["support"]["k_requested"] == 4
    assert payload["support"]["k_effective"] == 4
    assert payload["support"]["ranking_length"] == RANKED
    assert payload["support"]["universe_size"] == COHORT


def test_a_pool_universe_changes_the_recall_denominator_and_the_ideal(dataset):
    """The universe is a declaration, and both metrics answer to it.

    The same judgements over a six-posting benchmark pool: recall's denominator
    is the pool's relevant count, and NDCG's ideal is the pool's best grades.
    """
    grades = {opportunity_id: 0 for opportunity_id in range(1, COHORT + 1)}
    grades.update({1: 3, 2: 2, 3: 2, 15: 3, 16: 3})
    coverage = coverage_of(grades, dataset)
    # The pool is the ranked postings: a ranking must stay inside its universe,
    # so the two graded-3 postings the run never ranked fall outside it.
    pool_ids = list(range(1, RANKED + 1))
    pool = build_evaluation_universe(dataset, pool_ids)
    run = run_over(dataset, coverage=coverage, universe_ids=pool_ids)
    context = build_metric_run_context(
        dataset=dataset, run=run, coverage=coverage
    )
    assert context.universe.fingerprint == pool.fingerprint
    assert context.universe.kind is EvaluationUniverseKind.FIXED_BENCHMARK_POOL
    assert 15 not in context.universe.opportunity_ids

    # Over the whole cohort there are five relevant postings; over this pool
    # there are three, and recall answers to the universe that was declared.
    recall = recall_at_k(context, 3)
    assert recall.support.relevant_count == 3
    assert recall.support.universe_size == RANKED
    assert recall.value == 1.0

    # The ideal is the pool's best grades, so the unranked 3s do not raise it.
    result = ndcg_at_k(context, 3)
    assert result.support.denominator == dcg(3, 2, 2)
    assert result.value == 1.0
