"""The six overlaps: four partitions, two rates, and the empty/unavailable split.

These tests are about `overlaps.py`. The distinction the module turns on — an
empty *available* projection is a fact, an *unavailable* one is the absence of
one — gets the most attention, because collapsing the two is how a refusal
becomes a measurement.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from evaluation.experiments import (
    OVERLAP_PAIR_ORDER,
    OVERLAP_PAIR_PROJECTIONS,
    CohortProjection,
    DirectionalRateName,
    ExperimentBindingError,
    ExperimentContractError,
    OverlapPair,
    OverlapStatus,
    OverlapUnavailableReason,
    compute_overlap,
    compute_projection,
    overlap_pair_projections,
    verify_overlap_against_projections,
    verify_overlap_result_fingerprint,
)

from tests.unit.experiment_fixtures import (
    OTHER_COUNTRY,
    experiment_context,
    matching,
    qualification,
    ranked_records,
    record,
    recommendation,
    segment,
)

GEO = CohortProjection.GEO_NOT_EXPLICITLY_OUT_OF_TARGET
DATA_AI = CohortProjection.DATA_AI_NOT_EXPLICITLY_OUT_OF_SCOPE
MATCHING = CohortProjection.MATCHING_OBSERVED
RECOMMENDATION = CohortProjection.RECOMMENDATION_OBSERVED


def posting(
    opportunity_id: int,
    *,
    in_target: bool = True,
    in_scope: bool = True,
    matched: bool = True,
    recommended: int | None = None,
):
    """One posting, described by which of the four questions include it."""
    return record(
        opportunity_id,
        geography_segments=(
            (
                segment("RESOLVED")
                if in_target
                else segment("RESOLVED", country_code=OTHER_COUNTRY)
            ),
        ),
        qualification=(
            qualification() if in_scope else qualification(value="OUT_OF_SCOPE")
        ),
        matching=matching() if matched else None,
        recommendation=(
            None if recommended is None else recommendation(recommended)
        ),
    )


# ====================================================================
# the authority itself
# ====================================================================


def test_compute_overlap_is_the_only_public_way_to_get_a_result() -> None:
    import evaluation.experiments.overlaps as module

    assert module.__all__ == ["compute_overlap"]
    for forbidden in ("make_overlap_result", "seal_overlap", "overlap_result"):
        assert not hasattr(module, forbidden)


def test_the_six_pairs_are_closed_and_exhaustive() -> None:
    from evaluation.experiments.overlaps import _OVERLAP_INPUTS

    assert set(_OVERLAP_INPUTS) == set(OverlapPair)
    assert len(OVERLAP_PAIR_ORDER) == 6


def test_something_that_is_not_a_pair_is_refused() -> None:
    with pytest.raises(ExperimentContractError, match="not an overlap pair"):
        compute_overlap(experiment_context(), "GEO_AND_MATCHING")


def test_the_overlap_never_reinterprets_a_business_record() -> None:
    """It reads projection results, never a record field.

    Asserted structurally rather than by grepping the text: the module imports
    **nothing** from `evaluation.frozen_facts` and reads no record field, so it
    has no primitive with which to form a second opinion about which postings
    are out of target or out of scope. Were it to grow one, the overlap could
    disagree with the projection it claims to be about.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("evaluation/experiments/overlaps.py").read_text())
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any("frozen_facts" in name for name in modules)
    assert "evaluation.dataset" not in modules

    # And no record field is subscripted anywhere in the code.
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    docstrings = {ast.get_docstring(tree)} | {
        ast.get_docstring(node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    }
    for field in (
        "qualification",
        "geography_segments",
        "matching",
        "recommendation",
        "opportunity_id",
    ):
        assert field not in literals - docstrings, field


# ====================================================================
# the four-way partition
# ====================================================================


def test_the_four_partitions_are_exact_disjoint_and_exhaustive() -> None:
    records = (
        posting(1, matched=True, recommended=1),
        posting(2, matched=True, recommended=None),
        posting(3, matched=False, recommended=2),
        posting(4, matched=False, recommended=None),
    )
    context = experiment_context(records)
    result = compute_overlap(context, OverlapPair.MATCHING_AND_RECOMMENDATION)
    assert result.status is OverlapStatus.COMPUTED
    assert result.both_opportunity_ids == (1,)
    assert result.left_only_opportunity_ids == (2,)
    assert result.right_only_opportunity_ids == (3,)
    assert result.neither_opportunity_ids == (4,)
    assert result.both_count == 1
    assert (
        result.both_count
        + result.left_only_count
        + result.right_only_count
        + result.neither_count
        == result.cohort_size
    )


def test_the_neither_partition_is_completed_against_the_cohort() -> None:
    """"In neither" is a statement about every posting in the snapshot."""
    records = (
        posting(1, in_target=False, in_scope=False),
        posting(2, in_target=True, in_scope=True),
    )
    context = experiment_context(records, with_ranking=False)
    result = compute_overlap(context, OverlapPair.GEO_AND_DATA_AI)
    assert result.neither_opportunity_ids == (1,)
    assert result.both_opportunity_ids == (2,)


def test_every_partition_is_in_canonical_order() -> None:
    records = tuple(
        posting(index, matched=index % 2 == 1, recommended=index)
        for index in range(1, 7)
    )
    result = compute_overlap(
        experiment_context(records), OverlapPair.MATCHING_AND_RECOMMENDATION
    )
    for name in (
        "both_opportunity_ids",
        "left_only_opportunity_ids",
        "right_only_opportunity_ids",
        "neither_opportunity_ids",
    ):
        ids = getattr(result, name)
        assert list(ids) == sorted(ids)


def test_all_six_pairs_are_computable_over_one_cohort() -> None:
    context = experiment_context(
        ranked_records(size=10, ranked=6, out_of_target=(9,), out_of_scope=(10,))
    )
    for pair in OVERLAP_PAIR_ORDER:
        result = compute_overlap(context, pair)
        left, right = overlap_pair_projections(pair)
        assert result.left_projection is left
        assert result.right_projection is right
        assert result.status is OverlapStatus.COMPUTED


def test_each_pair_names_its_two_projections_by_recomputed_digest() -> None:
    context = experiment_context(ranked_records(size=8, ranked=5))
    for pair in OVERLAP_PAIR_ORDER:
        result = compute_overlap(context, pair)
        left, right = OVERLAP_PAIR_PROJECTIONS[pair]
        assert result.left_projection_result_fingerprint == (
            compute_projection(context, left).result_fingerprint
        )
        assert result.right_projection_result_fingerprint == (
            compute_projection(context, right).result_fingerprint
        )


# ====================================================================
# the two directional rates
# ====================================================================


def test_both_directional_rates_are_stated_and_are_asymmetric() -> None:
    records = (
        posting(1, matched=True, recommended=1),
        posting(2, matched=True, recommended=None),
        posting(3, matched=True, recommended=None),
        posting(4, matched=False, recommended=2),
    )
    result = compute_overlap(
        experiment_context(records), OverlapPair.MATCHING_AND_RECOMMENDATION
    )
    # |M| = 3, |R| = 2, |M ∩ R| = 1
    assert result.right_among_left.name is DirectionalRateName.RIGHT_AMONG_LEFT
    assert result.right_among_left.numerator == 1
    assert result.right_among_left.denominator == 3
    assert result.right_among_left.value == 1 / 3
    assert result.left_among_right.name is DirectionalRateName.LEFT_AMONG_RIGHT
    assert result.left_among_right.denominator == 2
    assert result.left_among_right.value == 0.5
    assert result.right_among_left.value != result.left_among_right.value


def test_a_rate_is_never_rounded() -> None:
    records = tuple(
        posting(index, matched=True, recommended=index if index <= 1 else None)
        for index in range(1, 4)
    )
    result = compute_overlap(
        experiment_context(records), OverlapPair.MATCHING_AND_RECOMMENDATION
    )
    assert result.right_among_left.value == 1 / 3
    assert repr(result.right_among_left.value).startswith("0.3333333333")


def test_an_empty_reference_projection_makes_only_that_rate_na() -> None:
    """The overlap stays COMPUTED: its four partitions are well defined."""
    records = (
        posting(1, matched=False, recommended=1),
        posting(2, matched=False, recommended=None),
    )
    result = compute_overlap(
        experiment_context(records), OverlapPair.MATCHING_AND_RECOMMENDATION
    )
    assert result.status is OverlapStatus.COMPUTED
    assert result.right_among_left.status is OverlapStatus.N_A
    assert result.right_among_left.unavailable_reason is (
        OverlapUnavailableReason.EMPTY_REFERENCE_PROJECTION
    )
    assert result.right_among_left.numerator == 0
    assert result.right_among_left.denominator == 0
    assert result.right_among_left.value is None
    assert result.left_among_right.status is OverlapStatus.COMPUTED
    assert result.left_among_right.value == 0.0


def test_both_rates_na_when_both_projections_are_empty_but_computed() -> None:
    records = (
        posting(1, matched=False, recommended=None),
        posting(2, matched=False, recommended=None),
    )
    result = compute_overlap(
        experiment_context(records, with_ranking=False),
        OverlapPair.MATCHING_AND_RECOMMENDATION,
    )
    assert result.status is OverlapStatus.COMPUTED
    assert result.neither_opportunity_ids == (1, 2)
    for rate in (result.right_among_left, result.left_among_right):
        assert rate.status is OverlapStatus.N_A
        assert rate.value is None


# ====================================================================
# empty vs unavailable — the distinction the module turns on
# ====================================================================


def test_an_unavailable_input_makes_the_whole_overlap_na() -> None:
    context = experiment_context(
        ranked_records(size=6, ranked=4), target_country=None
    )
    result = compute_overlap(context, OverlapPair.GEO_AND_MATCHING)
    assert result.status is OverlapStatus.N_A
    assert result.unavailable_reason is (
        OverlapUnavailableReason.INPUT_PROJECTION_UNAVAILABLE
    )


def test_an_unavailable_overlap_fabricates_no_partition() -> None:
    context = experiment_context(
        ranked_records(size=6, ranked=4), target_country=None
    )
    result = compute_overlap(context, OverlapPair.GEO_AND_RECOMMENDATION)
    for name in (
        "both_opportunity_ids",
        "left_only_opportunity_ids",
        "right_only_opportunity_ids",
        "neither_opportunity_ids",
        "both_count",
        "left_only_count",
        "right_only_count",
        "neither_count",
        "right_among_left",
        "left_among_right",
    ):
        assert getattr(result, name) is None, name


def test_only_the_pairs_involving_the_unavailable_projection_are_na() -> None:
    context = experiment_context(
        ranked_records(size=6, ranked=4), target_country=None
    )
    unavailable = {
        OverlapPair.GEO_AND_DATA_AI,
        OverlapPair.GEO_AND_MATCHING,
        OverlapPair.GEO_AND_RECOMMENDATION,
    }
    for pair in OVERLAP_PAIR_ORDER:
        result = compute_overlap(context, pair)
        expected = (
            OverlapStatus.N_A if pair in unavailable else OverlapStatus.COMPUTED
        )
        assert result.status is expected, pair


def test_an_empty_computed_projection_is_not_an_unavailable_one() -> None:
    """The whole point: an empty membership is a fact with real partitions."""
    records = (
        posting(1, matched=False, recommended=None),
        posting(2, matched=False, recommended=None),
    )
    empty = compute_overlap(
        experiment_context(records, with_ranking=False),
        OverlapPair.MATCHING_AND_RECOMMENDATION,
    )
    unavailable = compute_overlap(
        experiment_context(records, with_ranking=False, target_country=None),
        OverlapPair.GEO_AND_MATCHING,
    )
    assert empty.status is OverlapStatus.COMPUTED
    assert empty.neither_count == 2
    assert unavailable.status is OverlapStatus.N_A
    assert unavailable.neither_count is None


# ====================================================================
# identity and re-derivation
# ====================================================================


def test_every_overlap_survives_its_own_verifier() -> None:
    context = experiment_context(ranked_records(size=9, ranked=5))
    for pair in OVERLAP_PAIR_ORDER:
        result = compute_overlap(context, pair)
        assert verify_overlap_result_fingerprint(result) == (
            result.result_fingerprint
        )


def test_an_overlap_is_re_derivable_from_its_two_projections() -> None:
    context = experiment_context(ranked_records(size=9, ranked=5))
    cohort = compute_projection(context, CohortProjection.FROZEN_COHORT)
    for pair in OVERLAP_PAIR_ORDER:
        result = compute_overlap(context, pair)
        left, right = overlap_pair_projections(pair)
        verify_overlap_against_projections(
            result,
            compute_projection(context, left),
            compute_projection(context, right),
            cohort.included_ids,
        )


def test_a_forged_partition_fails_re_derivation_even_when_self_consistent() -> None:
    """Disjoint, covering, rates matching — and not this run's memberships."""
    records = (
        posting(1, matched=True, recommended=1),
        posting(2, matched=True, recommended=None),
        posting(3, matched=False, recommended=2),
        posting(4, matched=False, recommended=None),
    )
    context = experiment_context(records)
    pair = OverlapPair.MATCHING_AND_RECOMMENDATION
    result = compute_overlap(context, pair)
    # Swap two ids between partitions: still four disjoint sets covering the
    # cohort, still the same counts, still the same two rates.
    forged = replace(
        result,
        both_opportunity_ids=(2,),
        left_only_opportunity_ids=(1,),
    )
    from evaluation.experiments import validate_overlap_result_structure

    validate_overlap_result_structure(forged)
    left, right = overlap_pair_projections(pair)
    cohort = compute_projection(context, CohortProjection.FROZEN_COHORT)
    with pytest.raises(ExperimentBindingError, match="two projections imply"):
        verify_overlap_against_projections(
            forged,
            compute_projection(context, left),
            compute_projection(context, right),
            cohort.included_ids,
        )


def test_a_projection_that_is_not_the_named_one_is_refused() -> None:
    context = experiment_context(ranked_records(size=8, ranked=5))
    pair = OverlapPair.MATCHING_AND_RECOMMENDATION
    result = compute_overlap(context, pair)
    cohort = compute_projection(context, CohortProjection.FROZEN_COHORT)
    with pytest.raises(ExperimentBindingError, match="names left projection"):
        verify_overlap_against_projections(
            result,
            compute_projection(context, GEO),
            compute_projection(context, RECOMMENDATION),
            cohort.included_ids,
        )


def test_a_reference_cohort_of_the_wrong_size_is_refused() -> None:
    context = experiment_context(ranked_records(size=8, ranked=5))
    pair = OverlapPair.MATCHING_AND_RECOMMENDATION
    result = compute_overlap(context, pair)
    left, right = overlap_pair_projections(pair)
    with pytest.raises(ExperimentBindingError, match="reference membership"):
        verify_overlap_against_projections(
            result,
            compute_projection(context, left),
            compute_projection(context, right),
            (1, 2, 3),
        )


def test_the_overlap_is_deterministic_across_two_computations() -> None:
    context = experiment_context(ranked_records(size=9, ranked=5))
    first = compute_overlap(context, OverlapPair.GEO_AND_RECOMMENDATION)
    second = compute_overlap(context, OverlapPair.GEO_AND_RECOMMENDATION)
    assert first.result_fingerprint == second.result_fingerprint


def test_same_counts_over_different_partitions_are_different_identities() -> None:
    left = compute_overlap(
        experiment_context(
            (
                posting(1, matched=True, recommended=1),
                posting(2, matched=True, recommended=None),
            )
        ),
        OverlapPair.MATCHING_AND_RECOMMENDATION,
    )
    right = compute_overlap(
        experiment_context(
            (
                posting(1, matched=True, recommended=None),
                posting(2, matched=True, recommended=1),
            )
        ),
        OverlapPair.MATCHING_AND_RECOMMENDATION,
    )
    assert left.both_count == right.both_count == 1
    assert left.result_fingerprint != right.result_fingerprint
