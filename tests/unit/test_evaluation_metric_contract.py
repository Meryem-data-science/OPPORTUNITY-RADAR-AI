"""Phase 10.3a as a contract: what binds a metric run, and what forbids a number.

Nothing here reads the operational database, writes a label, or computes a
ranking metric — because no ranking metric exists to compute yet, which is
itself asserted below. Every dataset is invented, every judgement is invented,
and the frozen datasets are built in memory rather than on disk: the integrity
gate that turns two files into a `FrozenEvaluationDataset` is Phase 10.2's and
is exercised against real files in `tests/integration`.

The regression cases the slice exists for are grouped last, under the failure
each one prevents.
"""

import ast
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import evaluation.metrics as metrics_package
from evaluation.labeling import (
    CALIBRATION_V0_PROVENANCE,
    FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION,
    HUMAN_LABEL_PROTOCOL_VERSION,
    HUMAN_LABEL_SCHEMA_VERSION,
    RELEVANCE_GRADE_NAMES,
    FrozenEvaluationDataset,
    HumanLabelError,
    HumanRelevanceLabel,
    LabelDiagnostics,
)
from evaluation.metrics import (
    EVALUATION_RANKING_VERSION,
    EVALUATION_RUN_SCHEMA_VERSION,
    EVALUATION_UNIVERSE_VERSION,
    INDEPENDENT_BENCHMARK_PROTOCOL_VERSIONS,
    METRIC_CONTRACT_VERSION,
    RELEVANT_GRADE_THRESHOLD,
    SUPPORTED_METRIC_CONTRACT_VERSIONS,
    EvaluationBindingError,
    EvaluationMetricsError,
    EvaluationRankingEntry,
    EvaluationRunProvenance,
    EvaluationUniverseKind,
    EvidenceClass,
    EvidenceClassError,
    LabelCoverage,
    MetricArgumentError,
    MetricAvailability,
    MetricAvailabilityDecision,
    MetricContractError,
    MetricName,
    MetricResult,
    MetricStatus,
    MetricSupport,
    MetricUnavailableReason,
    RankingSource,
    UnjudgedOpportunityError,
    build_evaluation_ranking,
    build_evaluation_run,
    build_evaluation_universe,
    build_label_coverage,
    effective_k,
    evaluation_ranking_fingerprint,
    evaluation_run_fingerprint,
    evaluation_universe_fingerprint,
    is_relevant_grade,
    ndcg_at_k_availability,
    precision_at_k_availability,
    ranking_from_frozen_dataset,
    recall_at_k_availability,
    relevant_count_in_universe,
    require_evidence_class,
    top_k_judged_coverage,
    universe_judged_coverage,
    validate_k,
    verify_evaluation_run,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

DATASET_CONTENT_FINGERPRINT = "a" * 64
DATASET_ID = "evaluation-dataset-v3-" + DATASET_CONTENT_FINGERPRINT[:16]
PROFILE_CONTEXT_FINGERPRINT = "9" * 64
LABELSET_FINGERPRINT = "b" * 64


# --------------------------------------------------------------------------
# invented artefacts
# --------------------------------------------------------------------------


def record(opportunity_id: int, rank_position: int | None) -> dict[str, Any]:
    """One frozen record, reduced to the two fields this slice reads.

    A `recommendation` of `None` is Phase 10.1's statement that the
    Recommendation run never covered the posting — the candidate false negative
    the snapshot exists to preserve — and it is exactly what must not become a
    rank of 0 or a place at the bottom of the list.
    """
    recommendation = (
        None
        if rank_position is None
        else {
            "rank_position": rank_position,
            "disposition": "RECOMMENDED",
            "recommendation_score": 0.5,
            "evidence_coverage": 0.8,
            "assessment_fingerprint": "f" * 64,
        }
    )
    return {"opportunity_id": opportunity_id, "recommendation": recommendation}


def frozen_dataset(
    records: Sequence[Mapping[str, Any]],
    *,
    dataset_id: str = DATASET_ID,
    content_fingerprint: str = DATASET_CONTENT_FINGERPRINT,
    profile_id: int = 1,
    profile_context_fingerprint: str = PROFILE_CONTEXT_FINGERPRINT,
) -> FrozenEvaluationDataset:
    return FrozenEvaluationDataset(
        directory=Path("/invented/datasets") / dataset_id,
        schema_version="evaluation-dataset-v3",
        dataset_id=dataset_id,
        content_fingerprint=content_fingerprint,
        record_count=len(records),
        profile_id=profile_id,
        user_id=1,
        profile_context_fingerprint=profile_context_fingerprint,
        ordering="opportunity_id ASC",
        records=tuple(records),
    )


def ranked_dataset(
    size: int = 20, ranked: int = 12, **kwargs: Any
) -> FrozenEvaluationDataset:
    """`size` opportunities, the first `ranked` of which the pipeline ordered."""
    return frozen_dataset(
        [
            record(index, index if index <= ranked else None)
            for index in range(1, size + 1)
        ],
        **kwargs,
    )


def label(
    opportunity_id: int,
    grade: int,
    *,
    dataset: FrozenEvaluationDataset,
    revision: int = 1,
    relabel_reason: str | None = None,
    protocol_version: str = HUMAN_LABEL_PROTOCOL_VERSION,
) -> HumanRelevanceLabel:
    return HumanRelevanceLabel(
        label_schema_version=HUMAN_LABEL_SCHEMA_VERSION,
        protocol_version=protocol_version,
        dataset_id=dataset.dataset_id,
        dataset_content_fingerprint=dataset.content_fingerprint,
        profile_id=dataset.profile_id,
        profile_context_fingerprint=dataset.profile_context_fingerprint,
        opportunity_id=opportunity_id,
        relevance_grade=grade,
        diagnostics=LabelDiagnostics(),
        reason_tags=(),
        note=None,
        labeled_at="2026-04-01T00:00:00+00:00",
        revision=revision,
        relabel_reason=relabel_reason,
    )


def coverage_of(
    grades: Mapping[int, int], dataset: FrozenEvaluationDataset
) -> LabelCoverage:
    return build_label_coverage(
        dataset,
        [
            label(opportunity_id, grade, dataset=dataset)
            for opportunity_id, grade in sorted(grades.items())
        ],
        labelset_fingerprint=LABELSET_FINGERPRINT,
    )


def run_over(
    dataset: FrozenEvaluationDataset,
    *,
    universe_ids: Sequence[int] | None = None,
    **kwargs: Any,
):
    universe = build_evaluation_universe(dataset, universe_ids)
    ranking = ranking_from_frozen_dataset(dataset)
    return build_evaluation_run(
        dataset=dataset,
        universe=universe,
        ranking=ranking,
        evidence_class=EvidenceClass.DIAGNOSTIC_CALIBRATION,
        label_protocol_version=HUMAN_LABEL_PROTOCOL_VERSION,
        labelset_fingerprint=LABELSET_FINGERPRINT,
        **kwargs,
    )


@pytest.fixture
def dataset() -> FrozenEvaluationDataset:
    return ranked_dataset()


# --------------------------------------------------------------------------
# the evaluation universe
# --------------------------------------------------------------------------


def test_the_default_universe_is_the_whole_frozen_cohort(dataset):
    """Not the ranked subset, and emphatically not the labelled one."""
    universe = build_evaluation_universe(dataset)
    assert universe.kind is EvaluationUniverseKind.FROZEN_DATASET_COHORT
    assert universe.size == 20
    assert universe.opportunity_ids == tuple(range(1, 21))
    assert universe.universe_version == EVALUATION_UNIVERSE_VERSION
    assert universe.dataset_id == dataset.dataset_id
    assert universe.dataset_content_fingerprint == dataset.content_fingerprint


def test_a_fixed_benchmark_pool_is_representable_without_new_mathematics(dataset):
    """The shape a future benchmark takes, expressible today."""
    universe = build_evaluation_universe(dataset, [4, 2, 9])
    assert universe.kind is EvaluationUniverseKind.FIXED_BENCHMARK_POOL
    # A set, held in Phase 10.1's canonical order rather than the draw order.
    assert universe.opportunity_ids == (2, 4, 9)
    assert universe.size == 3


def test_a_universe_is_never_the_set_of_labelled_ids(dataset):
    """The asymmetry the whole slice rests on, asserted rather than described."""
    universe = build_evaluation_universe(dataset)
    coverage = coverage_of({1: 3, 2: 0}, dataset)
    judged = universe_judged_coverage(universe, coverage)
    assert judged.judged_count == 2
    assert judged.total_count == 20
    assert not judged.fully_judged
    assert len(judged.unjudged_opportunity_ids) == 18


def test_a_duplicated_universe_member_is_refused(dataset):
    with pytest.raises(EvaluationBindingError, match="repeats opportunity ids"):
        build_evaluation_universe(dataset, [3, 4, 3])


def test_a_universe_member_outside_the_frozen_dataset_is_refused(dataset):
    with pytest.raises(EvaluationBindingError, match="absent from dataset"):
        build_evaluation_universe(dataset, [3, 4, 999])


@pytest.mark.parametrize("value", [True, False, "7", 7.0, None, -1, 0])
def test_a_malformed_universe_member_is_refused(dataset, value):
    with pytest.raises(EvaluationBindingError):
        build_evaluation_universe(dataset, [3, value])


def test_a_declared_universe_size_that_disagrees_with_its_members_is_refused(
    dataset,
):
    with pytest.raises(EvaluationBindingError, match="declares size"):
        build_evaluation_universe(dataset, [3, 4], declared_size=3)


def test_an_empty_universe_is_refused(dataset):
    with pytest.raises(EvaluationBindingError, match="at least one opportunity"):
        build_evaluation_universe(dataset, [])


def test_a_partial_cohort_cannot_claim_to_be_the_whole_one(dataset):
    with pytest.raises(EvaluationBindingError, match="whole frozen cohort"):
        build_evaluation_universe(
            dataset, [1, 2, 3], kind=EvaluationUniverseKind.FROZEN_DATASET_COHORT
        )


# --------------------------------------------------------------------------
# the ranking
# --------------------------------------------------------------------------


def test_the_ranking_is_projected_from_the_frozen_recommendation_block(dataset):
    ranking = ranking_from_frozen_dataset(dataset)
    assert ranking.source is RankingSource.FROZEN_DATASET_RECOMMENDATION
    assert ranking.ranking_version == EVALUATION_RANKING_VERSION
    assert ranking.length == 12
    assert ranking.opportunity_ids == tuple(range(1, 13))
    assert [entry.rank_position for entry in ranking.entries] == list(
        range(1, 13)
    )
    assert ranking.profile_id == dataset.profile_id


def test_an_unranked_opportunity_is_absent_from_the_ranking_not_last():
    """`recommendation is None` is a candidate false negative, not rank ∞."""
    dataset = frozen_dataset([record(1, 1), record(2, None), record(3, 2)])
    ranking = ranking_from_frozen_dataset(dataset)
    assert ranking.opportunity_ids == (1, 3)
    assert 2 not in ranking.opportunity_ids


def test_holes_in_the_frozen_positions_are_projected_and_not_discarded():
    """A cohort drops postings that went inactive; the surviving ranks have gaps.

    The evaluation positions are numbered 1..N so that "the top K" means one
    thing everywhere, and the upstream positions stay on the entries so the
    projection is auditable rather than assumed.
    """
    dataset = frozen_dataset([record(4, 2), record(7, 9), record(11, 4)])
    ranking = ranking_from_frozen_dataset(dataset)
    assert ranking.opportunity_ids == (4, 11, 7)
    assert [entry.rank_position for entry in ranking.entries] == [1, 2, 3]
    assert [entry.source_rank_position for entry in ranking.entries] == [2, 4, 9]


def test_a_duplicated_rank_position_is_refused(dataset):
    with pytest.raises(EvaluationBindingError, match="repeats rank positions"):
        build_evaluation_ranking(
            dataset,
            [
                EvaluationRankingEntry(rank_position=1, opportunity_id=1),
                EvaluationRankingEntry(rank_position=1, opportunity_id=2),
            ],
        )


def test_a_duplicated_ranked_opportunity_is_refused(dataset):
    with pytest.raises(EvaluationBindingError, match="repeats opportunity ids"):
        build_evaluation_ranking(
            dataset,
            [
                EvaluationRankingEntry(rank_position=1, opportunity_id=1),
                EvaluationRankingEntry(rank_position=2, opportunity_id=1),
            ],
        )


def test_a_non_contiguous_declared_ranking_is_refused(dataset):
    with pytest.raises(EvaluationBindingError, match="canonical contiguous"):
        build_evaluation_ranking(
            dataset,
            [
                EvaluationRankingEntry(rank_position=1, opportunity_id=1),
                EvaluationRankingEntry(rank_position=3, opportunity_id=2),
            ],
        )


@pytest.mark.parametrize("value", [True, False, 0, -2, "1", 1.0, None])
def test_a_malformed_rank_position_is_refused(dataset, value):
    with pytest.raises(EvaluationBindingError):
        build_evaluation_ranking(
            dataset,
            [EvaluationRankingEntry(rank_position=value, opportunity_id=1)],
        )


def test_a_boolean_frozen_rank_is_refused_rather_than_read_as_the_top():
    dataset = frozen_dataset([record(1, True), record(2, 2)])
    with pytest.raises(EvaluationBindingError, match="rank position"):
        ranking_from_frozen_dataset(dataset)


def test_duplicate_frozen_rank_positions_are_refused():
    dataset = frozen_dataset([record(1, 2), record(2, 2)])
    with pytest.raises(EvaluationBindingError, match="duplicate recommendation"):
        ranking_from_frozen_dataset(dataset)


def test_a_dataset_with_no_frozen_ranking_has_nothing_to_evaluate():
    dataset = frozen_dataset([record(1, None), record(2, None)])
    with pytest.raises(EvaluationBindingError, match="no recommendation position"):
        ranking_from_frozen_dataset(dataset)


def test_an_empty_ranking_is_refused(dataset):
    with pytest.raises(EvaluationBindingError, match="at least one"):
        build_evaluation_ranking(dataset, [])


# --------------------------------------------------------------------------
# the label coverage view
# --------------------------------------------------------------------------


def test_coverage_reuses_the_phase_10_2_revision_interpretation(dataset):
    """The latest revision is the effective judgement — Phase 10.2's rule, not ours."""
    coverage = build_label_coverage(
        dataset,
        [
            label(1, 0, dataset=dataset),
            label(
                1,
                3,
                dataset=dataset,
                revision=2,
                relabel_reason="the rubric was clarified",
            ),
            label(2, 1, dataset=dataset),
        ],
        labelset_fingerprint=LABELSET_FINGERPRINT,
    )
    assert coverage.grade(1) == 3
    assert coverage.grade(2) == 1
    assert coverage.judged_opportunity_ids == (1, 2)


def test_a_broken_revision_chain_is_refused_by_phase_10_2(dataset):
    with pytest.raises(HumanLabelError):
        build_label_coverage(
            dataset,
            [
                label(1, 0, dataset=dataset),
                label(1, 3, dataset=dataset, revision=3, relabel_reason="why"),
            ],
            labelset_fingerprint=LABELSET_FINGERPRINT,
        )


def test_a_labelset_with_no_judgement_cannot_bind_a_run(dataset):
    with pytest.raises(EvaluationBindingError, match="no judgement"):
        build_label_coverage(
            dataset, [], labelset_fingerprint=LABELSET_FINGERPRINT
        )


def test_a_label_protocol_this_build_cannot_interpret_is_refused(dataset):
    with pytest.raises(HumanLabelError, match="protocol version"):
        build_label_coverage(
            dataset,
            [
                label(
                    1,
                    2,
                    dataset=dataset,
                    protocol_version=FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION,
                )
            ],
            labelset_fingerprint=LABELSET_FINGERPRINT,
        )


def test_a_labelset_mixing_two_protocols_is_refused(dataset):
    """Judgements of two rubrics are answers to two questions, never one labelset."""
    labels = [
        label(1, 2, dataset=dataset),
        label(
            2,
            2,
            dataset=dataset,
            protocol_version=FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION,
        ),
    ]
    with pytest.raises(EvaluationMetricsError, match="mixes label protocols"):
        build_label_coverage(
            dataset, labels, labelset_fingerprint=LABELSET_FINGERPRINT
        )


# --------------------------------------------------------------------------
# the run contract
# --------------------------------------------------------------------------


def test_a_run_binds_everything_a_metric_would_depend_on(dataset):
    run = run_over(dataset)
    assert run.run_schema_version == EVALUATION_RUN_SCHEMA_VERSION
    assert run.metric_contract_version == METRIC_CONTRACT_VERSION
    assert run.evidence_class is EvidenceClass.DIAGNOSTIC_CALIBRATION
    assert run.dataset_id == dataset.dataset_id
    assert run.dataset_content_fingerprint == dataset.content_fingerprint
    assert run.profile_context_fingerprint == dataset.profile_context_fingerprint
    assert run.universe.fingerprint
    assert run.ranking.fingerprint
    assert run.label_protocol_version == HUMAN_LABEL_PROTOCOL_VERSION
    assert run.labelset_fingerprint == LABELSET_FINGERPRINT
    assert verify_evaluation_run(run) == run.run_fingerprint


def test_a_universe_built_for_another_dataset_is_refused(dataset):
    other = ranked_dataset(dataset_id="evaluation-dataset-v3-" + "c" * 16)
    with pytest.raises(EvaluationBindingError, match="built for dataset"):
        build_evaluation_run(
            dataset=dataset,
            universe=build_evaluation_universe(other),
            ranking=ranking_from_frozen_dataset(dataset),
            evidence_class=EvidenceClass.DIAGNOSTIC_CALIBRATION,
            label_protocol_version=HUMAN_LABEL_PROTOCOL_VERSION,
            labelset_fingerprint=LABELSET_FINGERPRINT,
        )


def test_a_moved_dataset_content_fingerprint_is_refused(dataset):
    other = ranked_dataset(content_fingerprint="e" * 64)
    with pytest.raises(EvaluationBindingError, match="content fingerprint"):
        build_evaluation_run(
            dataset=dataset,
            universe=build_evaluation_universe(dataset),
            ranking=ranking_from_frozen_dataset(other),
            evidence_class=EvidenceClass.DIAGNOSTIC_CALIBRATION,
            label_protocol_version=HUMAN_LABEL_PROTOCOL_VERSION,
            labelset_fingerprint=LABELSET_FINGERPRINT,
        )


def test_a_ranking_produced_for_another_profile_context_is_refused(dataset):
    other = ranked_dataset(profile_context_fingerprint="7" * 64)
    with pytest.raises(EvaluationBindingError, match="profile context"):
        build_evaluation_run(
            dataset=dataset,
            universe=build_evaluation_universe(dataset),
            ranking=ranking_from_frozen_dataset(other),
            evidence_class=EvidenceClass.DIAGNOSTIC_CALIBRATION,
            label_protocol_version=HUMAN_LABEL_PROTOCOL_VERSION,
            labelset_fingerprint=LABELSET_FINGERPRINT,
        )


def test_a_ranking_naming_an_opportunity_outside_the_universe_is_refused(dataset):
    with pytest.raises(EvaluationBindingError, match="outside the evaluation universe"):
        run_over(dataset, universe_ids=[1, 2, 3])


def test_an_unsupported_metric_contract_version_is_refused(dataset):
    with pytest.raises(MetricContractError, match="unsupported metric contract"):
        run_over(dataset, metric_contract_version="evaluation-metric-contract-v9")


def test_a_malformed_labelset_fingerprint_is_refused(dataset):
    with pytest.raises(EvaluationBindingError, match="SHA-256"):
        build_evaluation_run(
            dataset=dataset,
            universe=build_evaluation_universe(dataset),
            ranking=ranking_from_frozen_dataset(dataset),
            evidence_class=EvidenceClass.DIAGNOSTIC_CALIBRATION,
            label_protocol_version=HUMAN_LABEL_PROTOCOL_VERSION,
            labelset_fingerprint="not-a-digest",
        )


def test_a_forged_run_fingerprint_does_not_survive_recomputation(dataset):
    run = run_over(dataset)
    forged = replace(run, run_fingerprint="0" * 64)
    with pytest.raises(EvaluationBindingError, match="claims fingerprint"):
        verify_evaluation_run(forged)


def test_a_forged_universe_fingerprint_does_not_survive_recomputation(dataset):
    run = run_over(dataset)
    forged = replace(run, universe=replace(run.universe, size=3))
    with pytest.raises(EvaluationBindingError, match="claims fingerprint"):
        verify_evaluation_run(forged)


def test_a_forged_ranking_fingerprint_does_not_survive_recomputation(dataset):
    run = run_over(dataset)
    forged = replace(
        run, ranking=replace(run.ranking, entries=run.ranking.entries[:-1])
    )
    with pytest.raises(EvaluationBindingError, match="claims fingerprint"):
        verify_evaluation_run(forged)


# --------------------------------------------------------------------------
# fingerprint domains
# --------------------------------------------------------------------------


def test_identical_semantic_inputs_produce_an_identical_run_fingerprint(dataset):
    assert run_over(dataset).run_fingerprint == run_over(dataset).run_fingerprint


def test_provenance_is_outside_the_run_fingerprint(dataset):
    """The clock, the directory and the label root say where, never what."""
    bare = run_over(dataset)
    annotated = run_over(
        dataset,
        provenance=EvaluationRunProvenance(
            generated_at="2026-09-11T08:00:00+00:00",
            dataset_directory="/somewhere/else/datasets/x",
            label_root="/somewhere/else/labels",
        ),
    )
    assert annotated.provenance.generated_at
    assert annotated.run_fingerprint == bare.run_fingerprint


def test_a_changed_ranking_order_changes_the_ranking_and_run_fingerprints(dataset):
    run = run_over(dataset)
    reordered_ids = (2, 1) + run.ranking.opportunity_ids[2:]
    swapped = build_evaluation_ranking(
        dataset,
        [
            EvaluationRankingEntry(
                rank_position=position, opportunity_id=opportunity_id
            )
            for position, opportunity_id in enumerate(reordered_ids, start=1)
        ],
        source=RankingSource.FROZEN_DATASET_RECOMMENDATION,
    )
    assert swapped.fingerprint != run.ranking.fingerprint
    reordered = build_evaluation_run(
        dataset=dataset,
        universe=run.universe,
        ranking=swapped,
        evidence_class=EvidenceClass.DIAGNOSTIC_CALIBRATION,
        label_protocol_version=HUMAN_LABEL_PROTOCOL_VERSION,
        labelset_fingerprint=LABELSET_FINGERPRINT,
    )
    assert reordered.run_fingerprint != run.run_fingerprint


def test_a_changed_universe_changes_the_run_fingerprint(dataset):
    whole = run_over(dataset)
    pool = run_over(dataset, universe_ids=list(range(1, 13)))
    assert pool.universe.fingerprint != whole.universe.fingerprint
    assert pool.run_fingerprint != whole.run_fingerprint


def test_a_changed_labelset_fingerprint_changes_the_run_fingerprint(dataset):
    run = run_over(dataset)
    other = build_evaluation_run(
        dataset=dataset,
        universe=run.universe,
        ranking=run.ranking,
        evidence_class=EvidenceClass.DIAGNOSTIC_CALIBRATION,
        label_protocol_version=HUMAN_LABEL_PROTOCOL_VERSION,
        labelset_fingerprint="d" * 64,
    )
    assert other.run_fingerprint != run.run_fingerprint


def test_a_changed_evidence_class_changes_the_run_fingerprint(dataset):
    """Built by hand: no supported path produces an `INDEPENDENT_BENCHMARK` run.

    The fingerprint function is pure, so the domain can still be asserted — and
    it must be, because the day a benchmark labelset exists the same arithmetic
    under two classes must not share one identity.
    """
    run = run_over(dataset)
    reclassified = replace(
        run, evidence_class=EvidenceClass.INDEPENDENT_BENCHMARK
    )
    assert evaluation_run_fingerprint(reclassified) != run.run_fingerprint


def test_a_changed_metric_contract_version_changes_the_run_fingerprint(dataset):
    run = run_over(dataset)
    future = replace(run, metric_contract_version="evaluation-metric-contract-v2")
    assert evaluation_run_fingerprint(future) != run.run_fingerprint


def test_the_universe_fingerprint_covers_its_members_and_its_kind(dataset):
    pool = build_evaluation_universe(dataset, [1, 2, 3])
    assert evaluation_universe_fingerprint(pool) == pool.fingerprint
    assert (
        evaluation_universe_fingerprint(
            replace(pool, kind=EvaluationUniverseKind.FROZEN_DATASET_COHORT)
        )
        != pool.fingerprint
    )
    assert (
        evaluation_universe_fingerprint(
            replace(pool, opportunity_ids=(1, 2, 4), size=3)
        )
        != pool.fingerprint
    )


def test_the_ranking_fingerprint_covers_the_upstream_positions(dataset):
    """Two identical orders derived from different upstream ranks are not one."""
    ranking = ranking_from_frozen_dataset(dataset)
    rederived = replace(
        ranking,
        entries=(replace(ranking.entries[0], source_rank_position=99),)
        + ranking.entries[1:],
    )
    assert evaluation_ranking_fingerprint(rederived) != ranking.fingerprint


# --------------------------------------------------------------------------
# K
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [True, False])
def test_a_boolean_k_is_refused(value):
    with pytest.raises(MetricArgumentError, match="positive integer"):
        validate_k(value)


@pytest.mark.parametrize("value", [0, -1, 1.0, "10", None])
def test_a_malformed_k_is_refused(value):
    with pytest.raises(MetricArgumentError):
        validate_k(value)


def test_k_larger_than_the_ranking_is_capped_rather_than_padded(dataset):
    ranking = ranking_from_frozen_dataset(dataset)
    assert ranking.length == 12
    assert effective_k(50, ranking) == 12
    assert effective_k(5, ranking) == 5
    assert effective_k(12, ranking) == 12


def test_effective_k_never_invents_a_position(dataset):
    """A property over every K a caller could ask: the cut-off is real or capped."""
    ranking = ranking_from_frozen_dataset(dataset)
    coverage = coverage_of(dict.fromkeys(range(1, 13), 2), dataset)
    for k in range(1, 60):
        used = effective_k(k, ranking)
        assert used == min(k, ranking.length)
        judged = top_k_judged_coverage(ranking, coverage, k)
        assert judged.total_count == used
        assert judged.fully_judged


# --------------------------------------------------------------------------
# the availability gates
# --------------------------------------------------------------------------


def test_a_fully_judged_top_k_makes_precision_available_and_nothing_else(dataset):
    run = run_over(dataset)
    coverage = coverage_of(dict.fromkeys(range(1, 13), 2), dataset)
    precision = precision_at_k_availability(run, coverage, 10)
    assert precision.available
    assert precision.reason is None
    assert precision.support.k_requested == 10
    assert precision.support.k_effective == 10
    assert precision.support.judged_in_top_k == 10
    assert precision.support.universe_size == 20
    assert precision.support.judged_count == 12
    # And still no number, anywhere.
    assert precision.support.numerator is None
    assert precision.support.denominator is None


def test_an_available_metric_cannot_be_turned_into_a_result(dataset):
    """Availability is not a score, and this slice refuses to let it become one."""
    run = run_over(dataset)
    coverage = coverage_of(dict.fromkeys(range(1, 13), 2), dataset)
    availability = precision_at_k_availability(run, coverage, 10)
    with pytest.raises(MetricContractError, match="computes no metric"):
        availability.as_result()


def test_an_unavailable_metric_converts_to_an_n_a_result(dataset):
    run = run_over(dataset)
    coverage = coverage_of(dict.fromkeys(range(1, 13), 2), dataset)
    result = ndcg_at_k_availability(run, coverage, 10).as_result()
    assert result.status is MetricStatus.N_A
    assert result.value is None
    assert result.reason is MetricUnavailableReason.EVALUATION_UNIVERSE_NOT_FULLY_JUDGED


def test_a_recall_denominator_is_refused_over_a_partly_judged_universe(dataset):
    universe = build_evaluation_universe(dataset)
    coverage = coverage_of({1: 3, 2: 2}, dataset)
    with pytest.raises(EvaluationBindingError, match="not knowable"):
        relevant_count_in_universe(universe, coverage)


def test_labels_from_another_labelset_make_every_metric_unavailable(dataset):
    run = run_over(dataset)
    coverage = replace(
        coverage_of(dict.fromkeys(range(1, 21), 3), dataset),
        labelset_fingerprint="c" * 64,
    )
    for gate in (
        precision_at_k_availability,
        recall_at_k_availability,
        ndcg_at_k_availability,
    ):
        availability = gate(run, coverage, 10)
        assert not availability.available
        assert availability.reason is MetricUnavailableReason.LABELSET_BINDING_MISMATCH


def test_labels_made_for_another_profile_are_refused_outright(dataset):
    """A different labelset is an answerless question; a different profile is a wrong one."""
    run = run_over(dataset)
    coverage = replace(
        coverage_of(dict.fromkeys(range(1, 21), 3), dataset),
        profile_context_fingerprint="4" * 64,
    )
    with pytest.raises(EvaluationBindingError, match="profile context"):
        precision_at_k_availability(run, coverage, 10)


def test_labels_made_against_another_dataset_are_refused_outright(dataset):
    run = run_over(dataset)
    coverage = replace(
        coverage_of(dict.fromkeys(range(1, 21), 3), dataset),
        dataset_id="evaluation-dataset-v3-" + "f" * 16,
    )
    with pytest.raises(EvaluationBindingError, match="made against dataset"):
        recall_at_k_availability(run, coverage, 10)


# --------------------------------------------------------------------------
# the evidence class
# --------------------------------------------------------------------------


def test_the_evidence_class_is_declared_and_never_counted(dataset):
    """Twelve judgements are not a benchmark because they are twelve."""
    run = run_over(dataset)
    assert run.evidence_class is EvidenceClass.DIAGNOSTIC_CALIBRATION
    # The class is in the run fingerprint, so it is part of the question asked.
    assert "evidence_class" in metrics_package.evaluation_run_payload(run)


def test_no_protocol_in_this_build_can_establish_an_independent_benchmark():
    assert INDEPENDENT_BENCHMARK_PROTOCOL_VERSIONS == ()
    with pytest.raises(EvidenceClassError, match="provenance this repository"):
        require_evidence_class(
            EvidenceClass.INDEPENDENT_BENCHMARK,
            label_protocol_version=HUMAN_LABEL_PROTOCOL_VERSION,
            labelset_fingerprint="1" * 64,
        )


def test_an_unknown_evidence_class_is_refused():
    with pytest.raises(EvidenceClassError, match="not an evaluation evidence class"):
        require_evidence_class(
            "PUBLISHED_RESULT",
            label_protocol_version=HUMAN_LABEL_PROTOCOL_VERSION,
            labelset_fingerprint="1" * 64,
        )


def test_a_diagnostic_run_needs_a_protocol_this_build_interprets():
    with pytest.raises(EvidenceClassError, match="interprets"):
        require_evidence_class(
            EvidenceClass.DIAGNOSTIC_CALIBRATION,
            label_protocol_version=FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION,
            labelset_fingerprint="1" * 64,
        )


# ====================================================================
# the regressions this slice exists to prevent
# ====================================================================


def test_1_an_unjudged_opportunity_never_becomes_grade_zero(dataset):
    """The failure the whole of Phase 10 is built against.

    Three surfaces, one rule: the coverage view has no key for an unjudged
    opportunity, asking for its grade raises rather than returning 0, and the
    gates count it as unjudged rather than as a miss.
    """
    coverage = coverage_of({1: 0, 2: 3}, dataset)
    assert coverage.is_judged(1)
    assert coverage.grade(1) == 0
    assert not coverage.is_judged(3)
    assert 3 not in coverage.grades
    with pytest.raises(UnjudgedOpportunityError, match="not a 0"):
        coverage.grade(3)
    judged = universe_judged_coverage(build_evaluation_universe(dataset), coverage)
    assert judged.judged_count == 2
    assert 3 in judged.unjudged_opportunity_ids


def test_2_one_unjudged_item_in_the_top_ten_makes_precision_unavailable(dataset):
    run = run_over(dataset)
    grades = dict.fromkeys(range(1, 13), 2)
    del grades[7]
    coverage = coverage_of(grades, dataset)
    availability = precision_at_k_availability(run, coverage, 10)
    assert not availability.available
    assert availability.reason is MetricUnavailableReason.TOP_K_NOT_FULLY_JUDGED
    assert availability.support.unjudged_in_top_k == (7,)
    assert availability.support.judged_in_top_k == 9


def test_3_a_judged_top_ten_is_not_enough_for_ndcg_or_recall(dataset):
    """The rule most likely to be weakened by somebody in a hurry.

    Precision asks only about what was shown, so a judged top 10 answers it.
    NDCG is a ratio against the ideal ordering and Recall is a fraction of a
    total, and both of those live in the rest of the universe.
    """
    run = run_over(dataset)
    coverage = coverage_of(dict.fromkeys(range(1, 11), 2), dataset)
    assert precision_at_k_availability(run, coverage, 10).available

    ndcg = ndcg_at_k_availability(run, coverage, 10)
    assert not ndcg.available
    assert ndcg.reason is MetricUnavailableReason.EVALUATION_UNIVERSE_NOT_FULLY_JUDGED

    recall = recall_at_k_availability(run, coverage, 10)
    assert not recall.available
    assert recall.reason is MetricUnavailableReason.RECALL_DENOMINATOR_UNKNOWN
    assert recall.support.relevant_count is None


def test_4_a_fully_judged_universe_with_nothing_relevant_has_no_recall(dataset):
    """The denominator is known, and it is zero. That is not a recall of 0.0."""
    run = run_over(dataset)
    coverage = coverage_of(dict.fromkeys(range(1, 21), 1), dataset)
    recall = recall_at_k_availability(run, coverage, 10)
    assert not recall.available
    assert recall.reason is MetricUnavailableReason.NO_RELEVANT_ITEMS
    assert recall.support.relevant_count == 0
    assert recall.support.universe_size == 20
    assert recall.support.judged_count == 20


def test_5_a_fully_judged_universe_with_relevant_items_opens_every_gate(dataset):
    run = run_over(dataset)
    grades = {index: (3 if index <= 4 else 0) for index in range(1, 21)}
    coverage = coverage_of(grades, dataset)
    availabilities = [
        precision_at_k_availability(run, coverage, 10),
        recall_at_k_availability(run, coverage, 10),
        ndcg_at_k_availability(run, coverage, 10),
    ]
    assert [item.available for item in availabilities] == [True, True, True]
    assert [item.reason for item in availabilities] == [None, None, None]
    # And still not a single score: availability is permission, not a number.
    for item in availabilities:
        assert item.support.numerator is None
        assert item.support.denominator is None
        with pytest.raises(MetricContractError):
            item.as_result()


def test_6_a_boolean_k_is_refused_by_every_gate(dataset):
    run = run_over(dataset)
    coverage = coverage_of(dict.fromkeys(range(1, 21), 2), dataset)
    for gate in (
        precision_at_k_availability,
        recall_at_k_availability,
        ndcg_at_k_availability,
    ):
        with pytest.raises(MetricArgumentError, match="positive integer"):
            gate(run, coverage, True)


def test_12_the_known_calibration_round_cannot_be_an_independent_benchmark():
    """Its own recorded provenance says what it is not; this refuses to forget."""
    with pytest.raises(EvidenceClassError) as error:
        require_evidence_class(
            EvidenceClass.INDEPENDENT_BENCHMARK,
            label_protocol_version=CALIBRATION_V0_PROVENANCE.protocol_version,
            labelset_fingerprint=CALIBRATION_V0_PROVENANCE.labelset_fingerprint,
        )
    message = str(error.value)
    assert CALIBRATION_V0_PROVENANCE.labelset_fingerprint in message
    assert CALIBRATION_V0_PROVENANCE.method in message
    for statement in CALIBRATION_V0_PROVENANCE.not_a:
        assert statement in message


def test_12b_the_calibration_round_is_still_usable_as_diagnostic_evidence():
    """Fail closed is not fail always: a diagnostic dry-run is what v0 is for."""
    assert (
        require_evidence_class(
            EvidenceClass.DIAGNOSTIC_CALIBRATION,
            label_protocol_version=CALIBRATION_V0_PROVENANCE.protocol_version,
            labelset_fingerprint=CALIBRATION_V0_PROVENANCE.labelset_fingerprint,
        )
        is EvidenceClass.DIAGNOSTIC_CALIBRATION
    )


def test_13_no_production_package_imports_the_evaluation_metrics(dataset):
    """The arrow, restated for this package because it is the tempting one to break.

    A dashboard that wanted to show "how well are we doing" would import a
    metric, and the runtime would then depend on an audit trail.
    """
    offenders = []
    for path in (REPOSITORY_ROOT / "services").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "evaluation.metrics" in text or "from evaluation" in text:
            offenders.append(str(path.relative_to(REPOSITORY_ROOT)))
    assert offenders == []


def test_14_the_metrics_package_imports_nothing_that_could_open_a_database():
    """Checked against the import lines, so the prose may say "no SQLite" out loud."""
    for path in sorted((REPOSITORY_ROOT / "evaluation" / "metrics").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
        for module in modules:
            lowered = module.lower()
            assert "sqlite" not in lowered, f"{path.name}: {module}"
            assert "libsql" not in lowered, f"{path.name}: {module}"
            assert not lowered.startswith("evaluation.dataset.readers"), (
                f"{path.name}: {module}"
            )


def test_the_slice_contains_no_metric_formula():
    """The deliverable, asserted rather than promised.

    Read off the syntax tree: no public function of this package names a metric
    it could be mistaken for computing, and no module defines one.
    """
    forbidden = ("precision_at_k", "recall_at_k", "ndcg_at_k", "dcg", "idcg")
    for path in sorted((REPOSITORY_ROOT / "evaluation" / "metrics").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            name = node.name.lower()
            for token in forbidden:
                assert not (name.startswith(token) and "availability" not in name), (
                    f"{path.name} defines {node.name}, which is a metric formula"
                )


def test_the_relevance_threshold_is_the_frozen_one_and_not_a_new_definition():
    assert RELEVANT_GRADE_THRESHOLD == 2
    assert RELEVANCE_GRADE_NAMES[RELEVANT_GRADE_THRESHOLD] == "RELEVANT"
    assert is_relevant_grade(3) and is_relevant_grade(2)
    assert not is_relevant_grade(1) and not is_relevant_grade(0)


@pytest.mark.parametrize("value", [True, False, 2.0, "2", None])
def test_a_malformed_grade_is_not_a_relevance_verdict(value):
    with pytest.raises(MetricArgumentError):
        is_relevant_grade(value)


def test_a_metric_result_cannot_be_na_and_carry_a_number():
    support = MetricSupport(k_requested=10, k_effective=10)
    with pytest.raises(MetricContractError, match="not even a zero"):
        MetricResult(
            metric=MetricName.RECALL_AT_K,
            status=MetricStatus.N_A,
            support=support,
            value=0.0,
            reason=MetricUnavailableReason.NO_RELEVANT_ITEMS,
        )
    with pytest.raises(MetricContractError, match="without a stated reason"):
        MetricResult(
            metric=MetricName.RECALL_AT_K,
            status=MetricStatus.N_A,
            support=support,
        )
    with pytest.raises(MetricContractError, match="states no value"):
        MetricResult(
            metric=MetricName.RECALL_AT_K,
            status=MetricStatus.COMPUTED,
            support=support,
        )


def test_an_unavailable_decision_must_state_a_reason():
    with pytest.raises(MetricContractError, match="without a stated reason"):
        MetricAvailability(
            metric=MetricName.NDCG_AT_K,
            decision=MetricAvailabilityDecision.UNAVAILABLE,
            support=MetricSupport(),
        )
    with pytest.raises(MetricContractError, match="available and states reason"):
        MetricAvailability(
            metric=MetricName.NDCG_AT_K,
            decision=MetricAvailabilityDecision.AVAILABLE,
            support=MetricSupport(),
            reason=MetricUnavailableReason.NO_RELEVANT_ITEMS,
        )


def test_the_contract_version_is_supported_and_alone():
    assert SUPPORTED_METRIC_CONTRACT_VERSIONS == (METRIC_CONTRACT_VERSION,)


def test_every_refusal_belongs_to_one_evaluation_exception_hierarchy():
    """No low-level exception leaks out of a public function of this package.

    Phase 10.2's `HumanLabelError` is part of the same hierarchy rather than an
    exception to the rule: both it and everything raised here descend from the
    Phase 10.1 `EvaluationDatasetError`, so a caller that refuses a broken
    evaluation artefact refuses a broken metric run without a second `except`.
    """
    from evaluation.dataset import EvaluationDatasetError

    for error_type in (
        EvaluationMetricsError,
        EvaluationBindingError,
        EvidenceClassError,
        MetricArgumentError,
        MetricContractError,
        UnjudgedOpportunityError,
        HumanLabelError,
    ):
        assert issubclass(error_type, EvaluationDatasetError)
