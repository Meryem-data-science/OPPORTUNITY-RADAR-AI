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
import inspect
from collections.abc import Mapping, Sequence
from dataclasses import FrozenInstanceError, replace
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
    CalibrationSelection,
    FrozenEvaluationDataset,
    HumanLabelError,
    HumanRelevanceLabel,
    LabelDiagnostics,
    canonical_label_order,
    labelset_fingerprint,
    select_calibration_sample,
)
from evaluation.dataset import EvaluationDatasetError
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
    MetricRunContext,
    MetricStatus,
    MetricSupport,
    MetricUnavailableReason,
    RankingSource,
    UnjudgedOpportunityError,
    build_evaluation_ranking,
    build_evaluation_run,
    build_evaluation_universe,
    assert_ranking_against_dataset,
    assert_universe_against_dataset,
    build_label_coverage,
    build_metric_run_context,
    canonical_opportunity_ids,
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
    validate_evaluation_ranking_structure,
    validate_evaluation_universe_structure,
    validate_k,
    verify_evaluation_run,
    verify_evaluation_run_structure,
    verify_label_coverage,
    verify_label_coverage_structure,
    verify_metric_run_context,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

DATASET_CONTENT_FINGERPRINT = "a" * 64
DATASET_ID = "evaluation-dataset-v3-" + DATASET_CONTENT_FINGERPRINT[:16]
PROFILE_CONTEXT_FINGERPRINT = "9" * 64


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


def whole_lot(dataset: FrozenEvaluationDataset) -> CalibrationSelection:
    """A calibration lot covering the whole dataset.

    Phase 10.2 draws a *stratified* lot, so a lot smaller than the cohort holds
    postings nobody chose by hand. Most tests here want to decide which postings
    are judged rather than which are selected, so they select everything and
    then judge a subset — which is also the state a real round is in for most of
    its life: a lot drawn, and part of it answered.
    """
    return select_calibration_sample(dataset, sample_size=len(dataset.records))


def coverage_of(
    grades: Mapping[int, int],
    dataset: FrozenEvaluationDataset,
    *,
    selection: CalibrationSelection | None = None,
    expected_labelset_fingerprint: str | None = None,
) -> LabelCoverage:
    return build_label_coverage(
        dataset,
        [
            label(opportunity_id, grade, dataset=dataset)
            for opportunity_id, grade in sorted(grades.items())
        ],
        selection=whole_lot(dataset) if selection is None else selection,
        expected_labelset_fingerprint=expected_labelset_fingerprint,
    )


def run_over(
    dataset: FrozenEvaluationDataset,
    *,
    universe_ids: Sequence[int] | None = None,
    coverage: LabelCoverage | None = None,
    **kwargs: Any,
):
    universe = build_evaluation_universe(dataset, universe_ids)
    ranking = ranking_from_frozen_dataset(dataset)
    return build_evaluation_run(
        dataset=dataset,
        universe=universe,
        ranking=ranking,
        coverage=coverage_of({1: 2}, dataset) if coverage is None else coverage,
        evidence_class=EvidenceClass.DIAGNOSTIC_CALIBRATION,
        **kwargs,
    )


def context_of(
    dataset: FrozenEvaluationDataset,
    coverage: LabelCoverage | None = None,
    **kwargs: Any,
):
    """A verified context: the only thing an availability gate accepts."""
    resolved = coverage_of({1: 2}, dataset) if coverage is None else coverage
    return build_metric_run_context(
        dataset=dataset,
        run=run_over(dataset, coverage=resolved, **kwargs),
        coverage=resolved,
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


@pytest.mark.parametrize("value", [True, False, 1.0, "1", 0, -1])
def test_a_malformed_declared_universe_size_is_refused(dataset, value):
    """`declared_size=True` must not confirm a one-member universe.

    `True == 1` in Python, so a size check that compared before validating would
    pass here — not because anything was counted, but because a boolean was.
    """
    with pytest.raises(EvaluationBindingError):
        build_evaluation_universe(dataset, [7], declared_size=value)
    # The honest value still works, and a wrong honest value is still refused.
    assert build_evaluation_universe(dataset, [7], declared_size=1).size == 1
    with pytest.raises(EvaluationBindingError, match="declares size"):
        build_evaluation_universe(dataset, [7], declared_size=2)
    # `None` is "not declared", which is a different statement and is allowed.
    assert build_evaluation_universe(dataset, [7], declared_size=None).size == 1


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
    """The latest revision is the effective judgement: Phase 10.2's rule, reused."""
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
        selection=whole_lot(dataset),
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
            selection=whole_lot(dataset),
        )


def test_a_labelset_with_no_judgement_cannot_bind_a_run(dataset):
    with pytest.raises(EvaluationBindingError, match="no judgement"):
        build_label_coverage(dataset, [], selection=whole_lot(dataset))


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
            selection=whole_lot(dataset),
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
        build_label_coverage(dataset, labels, selection=whole_lot(dataset))


# --------------------------------------------------------------------------
# the run contract
# --------------------------------------------------------------------------


def test_a_run_binds_everything_a_metric_would_depend_on(dataset):
    coverage = coverage_of({1: 2}, dataset)
    run = run_over(dataset, coverage=coverage)
    assert run.run_schema_version == EVALUATION_RUN_SCHEMA_VERSION
    assert run.metric_contract_version == METRIC_CONTRACT_VERSION
    assert run.evidence_class is EvidenceClass.DIAGNOSTIC_CALIBRATION
    assert run.dataset_id == dataset.dataset_id
    assert run.dataset_content_fingerprint == dataset.content_fingerprint
    assert run.profile_context_fingerprint == dataset.profile_context_fingerprint
    assert run.universe.fingerprint
    assert run.ranking.fingerprint
    assert run.label_protocol_version == HUMAN_LABEL_PROTOCOL_VERSION
    # Derived from the verified coverage, never accepted as an argument.
    assert run.labelset_fingerprint == coverage.labelset_fingerprint
    assert run.labelset_fingerprint == verify_label_coverage(coverage, dataset)
    assert verify_evaluation_run(run, dataset) == run.run_fingerprint
    assert verify_evaluation_run_structure(run) == run.run_fingerprint


def test_a_universe_built_for_another_dataset_is_refused(dataset):
    other = ranked_dataset(dataset_id="evaluation-dataset-v3-" + "c" * 16)
    with pytest.raises(EvaluationBindingError, match="built for dataset"):
        build_evaluation_run(
            dataset=dataset,
            universe=build_evaluation_universe(other),
            ranking=ranking_from_frozen_dataset(dataset),
            coverage=coverage_of({1: 2}, dataset),
            evidence_class=EvidenceClass.DIAGNOSTIC_CALIBRATION,
        )


def test_a_moved_dataset_content_fingerprint_is_refused(dataset):
    other = ranked_dataset(content_fingerprint="e" * 64)
    with pytest.raises(EvaluationBindingError, match="content fingerprint"):
        build_evaluation_run(
            dataset=dataset,
            universe=build_evaluation_universe(dataset),
            ranking=ranking_from_frozen_dataset(other),
            coverage=coverage_of({1: 2}, dataset),
            evidence_class=EvidenceClass.DIAGNOSTIC_CALIBRATION,
        )


def test_a_ranking_produced_for_another_profile_context_is_refused(dataset):
    other = ranked_dataset(profile_context_fingerprint="7" * 64)
    with pytest.raises(EvaluationBindingError, match="profile context"):
        build_evaluation_run(
            dataset=dataset,
            universe=build_evaluation_universe(dataset),
            ranking=ranking_from_frozen_dataset(other),
            coverage=coverage_of({1: 2}, dataset),
            evidence_class=EvidenceClass.DIAGNOSTIC_CALIBRATION,
        )


def test_a_ranking_naming_an_opportunity_outside_the_universe_is_refused(dataset):
    with pytest.raises(EvaluationBindingError, match="outside the evaluation universe"):
        run_over(dataset, universe_ids=[1, 2, 3])


def test_an_unsupported_metric_contract_version_is_refused(dataset):
    with pytest.raises(MetricContractError, match="unsupported metric contract"):
        run_over(dataset, metric_contract_version="evaluation-metric-contract-v9")


def test_a_run_cannot_be_bound_to_a_bare_fingerprint(dataset):
    """The builder takes verified evidence, not a string that looks like one.

    Blocker A in one line: there is no parameter here through which a caller can
    name a labelset that has no judgements behind it, and a forged digest on an
    otherwise well-formed coverage does not survive recomputation either.
    """
    coverage = coverage_of({1: 2, 2: 3}, dataset)
    with pytest.raises(TypeError):
        build_evaluation_run(
            dataset=dataset,
            universe=build_evaluation_universe(dataset),
            ranking=ranking_from_frozen_dataset(dataset),
            evidence_class=EvidenceClass.DIAGNOSTIC_CALIBRATION,
            label_protocol_version=HUMAN_LABEL_PROTOCOL_VERSION,
            labelset_fingerprint="d" * 64,
        )
    with pytest.raises(EvaluationBindingError, match="SHA-256"):
        run_over(
            dataset,
            coverage=replace(coverage, labelset_fingerprint="not-a-digest"),
        )
    with pytest.raises(EvaluationBindingError, match="judgements it holds"):
        run_over(
            dataset, coverage=replace(coverage, labelset_fingerprint="d" * 64)
        )


def test_a_forged_run_fingerprint_does_not_survive_recomputation(dataset):
    run = run_over(dataset)
    forged = replace(run, run_fingerprint="0" * 64)
    with pytest.raises(EvaluationBindingError, match="claims fingerprint"):
        verify_evaluation_run(forged, dataset)


def test_a_forged_universe_fingerprint_does_not_survive_recomputation(dataset):
    """A structurally valid universe whose members were edited under its digest."""
    run = run_over(dataset)
    narrowed = replace(
        run.universe,
        opportunity_ids=run.universe.opportunity_ids[:-1],
        size=run.universe.size - 1,
    )
    with pytest.raises(EvaluationBindingError, match="claims fingerprint"):
        verify_evaluation_run(replace(run, universe=narrowed), dataset)


def test_a_forged_ranking_fingerprint_does_not_survive_recomputation(dataset):
    run = run_over(dataset)
    forged = replace(
        run, ranking=replace(run.ranking, entries=run.ranking.entries[:-1])
    )
    with pytest.raises(EvaluationBindingError, match="claims fingerprint"):
        verify_evaluation_run(forged, dataset)


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
        coverage=coverage_of({1: 2}, dataset),
        evidence_class=EvidenceClass.DIAGNOSTIC_CALIBRATION,
    )
    assert reordered.run_fingerprint != run.run_fingerprint


def test_a_changed_universe_changes_the_run_fingerprint(dataset):
    whole = run_over(dataset)
    pool = run_over(dataset, universe_ids=list(range(1, 13)))
    assert pool.universe.fingerprint != whole.universe.fingerprint
    assert pool.run_fingerprint != whole.run_fingerprint


def test_a_changed_labelset_changes_the_run_fingerprint(dataset):
    """A different judgement is a different labelset is a different measurement."""
    run = run_over(dataset, coverage=coverage_of({1: 2}, dataset))
    other = run_over(dataset, coverage=coverage_of({1: 3}, dataset))
    assert other.labelset_fingerprint != run.labelset_fingerprint
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
    coverage = coverage_of(dict.fromkeys(range(1, 13), 2), dataset)
    precision = precision_at_k_availability(context_of(dataset, coverage), 10)
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
    coverage = coverage_of(dict.fromkeys(range(1, 13), 2), dataset)
    availability = precision_at_k_availability(context_of(dataset, coverage), 10)
    with pytest.raises(MetricContractError, match="computes no metric"):
        availability.as_result()


def test_an_unavailable_metric_converts_to_an_n_a_result(dataset):
    coverage = coverage_of(dict.fromkeys(range(1, 13), 2), dataset)
    result = ndcg_at_k_availability(context_of(dataset, coverage), 10).as_result()
    assert result.status is MetricStatus.N_A
    assert result.value is None
    assert result.reason is MetricUnavailableReason.EVALUATION_UNIVERSE_NOT_FULLY_JUDGED


def test_a_recall_denominator_is_refused_over_a_partly_judged_universe(dataset):
    universe = build_evaluation_universe(dataset)
    coverage = coverage_of({1: 3, 2: 2}, dataset)
    with pytest.raises(EvaluationBindingError, match="not knowable"):
        relevant_count_in_universe(universe, coverage)


def test_labels_from_another_labelset_make_every_metric_unavailable(dataset):
    """Two real labelsets over one dataset: a different lot is a different question."""
    grades = dict.fromkeys(range(1, 21), 3)
    run = run_over(dataset, coverage=coverage_of(grades, dataset))
    coverage = coverage_of(
        grades,
        dataset,
        selection=select_calibration_sample(dataset, sample_size=6),
    )
    assert coverage.labelset_fingerprint != run.labelset_fingerprint
    context = build_metric_run_context(
        dataset=dataset, run=run, coverage=coverage
    )
    assert not context.labelset_matches
    for gate in (
        precision_at_k_availability,
        recall_at_k_availability,
        ndcg_at_k_availability,
    ):
        availability = gate(context, 10)
        assert not availability.available
        assert availability.reason is MetricUnavailableReason.LABELSET_BINDING_MISMATCH


def test_labels_made_for_another_profile_are_refused_outright(dataset):
    """A different labelset is an unanswerable question; a different profile is
    a wrong one."""
    run = run_over(dataset)
    other = ranked_dataset(profile_context_fingerprint="4" * 64)
    coverage = coverage_of(dict.fromkeys(range(1, 21), 3), other)
    # Refused by Phase 10.2's own selection binding, which is the most specific
    # statement available: the lot itself was drawn for another profile state.
    with pytest.raises(HumanLabelError, match="profile context"):
        build_metric_run_context(dataset=dataset, run=run, coverage=coverage)
    with pytest.raises(HumanLabelError, match="profile context"):
        verify_label_coverage(coverage, dataset)


def test_labels_made_against_another_dataset_are_refused_outright(dataset):
    run = run_over(dataset)
    other = ranked_dataset(dataset_id="evaluation-dataset-v3-" + "c" * 16)
    coverage = coverage_of(dict.fromkeys(range(1, 21), 3), other)
    with pytest.raises(HumanLabelError, match="drawn from dataset"):
        build_metric_run_context(dataset=dataset, run=run, coverage=coverage)


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


# --------------------------------------------------------------------------
# Blocker A: a labelset identity is recomputed, never accepted
# --------------------------------------------------------------------------


def test_the_labelset_digest_is_the_phase_10_2_one_recomputed(dataset):
    """Not a parallel digest: Phase 10.2's own function, over the same domain."""
    selection = whole_lot(dataset)
    labels = [label(1, 3, dataset=dataset), label(2, 0, dataset=dataset)]
    coverage = build_label_coverage(dataset, labels, selection=selection)
    assert coverage.labelset_fingerprint == labelset_fingerprint(
        label_schema_version=HUMAN_LABEL_SCHEMA_VERSION,
        protocol_version=HUMAN_LABEL_PROTOCOL_VERSION,
        dataset_id=dataset.dataset_id,
        dataset_content_fingerprint=dataset.content_fingerprint,
        profile_id=dataset.profile_id,
        profile_context_fingerprint=dataset.profile_context_fingerprint,
        selector_version=selection.selector_version,
        selection_fingerprint=selection.selection_fingerprint,
        labels=labels,
    )


def test_judgements_outside_the_lot_do_not_enter_the_labelset(dataset):
    """The defect: a digest over lot A, exposed beside grades that are not in A.

    Phase 10.2 leaves out-of-lot judgements out of the labelset digest and
    reports them separately. A coverage that exposed them anyway would hand the
    gates grades the run's labelset fingerprint does not cover.
    """
    small = select_calibration_sample(dataset, sample_size=4)
    inside = set(small.opportunity_ids)
    outside = sorted(set(range(1, 21)) - inside)[:3]
    labels = [
        label(opportunity_id, 3, dataset=dataset)
        for opportunity_id in sorted(inside) + outside
    ]
    coverage = build_label_coverage(dataset, labels, selection=small)

    assert set(coverage.grades) == inside
    assert coverage.judged_outside_selection == tuple(outside)
    for opportunity_id in outside:
        assert not coverage.is_judged(opportunity_id)
        with pytest.raises(UnjudgedOpportunityError):
            coverage.grade(opportunity_id)
    # And the digest is the one Phase 10.2 computes over the lot alone: adding
    # judgements outside it does not move the labelset's identity.
    in_lot_only = [
        item for item in labels if item.opportunity_id in inside
    ]
    assert coverage.labelset_fingerprint == build_label_coverage(
        dataset, in_lot_only, selection=small
    ).labelset_fingerprint


def test_an_expected_labelset_fingerprint_is_compared_and_never_substituted(
    dataset,
):
    grades = {1: 3, 2: 2}
    honest = coverage_of(grades, dataset)
    assert (
        coverage_of(
            grades,
            dataset,
            expected_labelset_fingerprint=honest.labelset_fingerprint,
        ).labelset_fingerprint
        == honest.labelset_fingerprint
    )
    with pytest.raises(EvaluationBindingError, match="expected to be"):
        coverage_of(
            grades, dataset, expected_labelset_fingerprint="e" * 64
        )
    with pytest.raises(EvaluationBindingError, match="SHA-256"):
        coverage_of(grades, dataset, expected_labelset_fingerprint="nope")


def test_a_coverage_whose_grades_were_edited_under_its_digest_is_refused(dataset):
    """The whole point of recomputing: an edited grade changes the identity."""
    coverage = coverage_of({1: 0, 2: 0}, dataset)
    tampered = replace(
        coverage,
        labels=(
            replace(coverage.labels[0], relevance_grade=3),
            coverage.labels[1],
        ),
    )
    assert tampered.grade(1) == 3
    with pytest.raises(EvaluationBindingError, match="judgements it holds"):
        verify_label_coverage(tampered, dataset)
    run = run_over(dataset, coverage=coverage)
    with pytest.raises(EvaluationBindingError, match="judgements it holds"):
        build_metric_run_context(dataset=dataset, run=run, coverage=tampered)
    with pytest.raises(EvaluationBindingError, match="judgements it holds"):
        run_over(dataset, coverage=tampered)


def test_a_coverage_cannot_hold_two_judgements_of_one_opportunity(dataset):
    coverage = coverage_of({1: 3, 2: 0}, dataset)
    with pytest.raises(EvaluationBindingError, match="appears twice"):
        replace(coverage, labels=(coverage.labels[0], coverage.labels[0]))


def test_a_coverage_cannot_claim_a_judgement_is_both_in_and_out_of_the_lot(
    dataset,
):
    coverage = coverage_of({1: 3, 2: 0}, dataset)
    with pytest.raises(EvaluationBindingError, match="both inside and outside"):
        verify_label_coverage(
            replace(coverage, judged_outside_selection=(1,)), dataset
        )


def test_a_lot_drawn_from_another_dataset_is_refused(dataset):
    """Phase 10.2's own selection-binding check, applied before anything is read."""
    other = ranked_dataset(dataset_id="evaluation-dataset-v3-" + "c" * 16)
    with pytest.raises(HumanLabelError):
        build_label_coverage(
            dataset,
            [label(1, 3, dataset=dataset)],
            selection=whole_lot(other),
        )


def test_a_lot_with_no_judgement_in_it_cannot_bind_a_run(dataset):
    """Judging only outside the lot is not judging the lot."""
    small = select_calibration_sample(dataset, sample_size=3)
    outside = sorted(set(range(1, 21)) - set(small.opportunity_ids))[:2]
    with pytest.raises(EvaluationBindingError, match="belongs to lot"):
        build_label_coverage(
            dataset,
            [label(opportunity_id, 2, dataset=dataset) for opportunity_id in outside],
            selection=small,
        )


def resealed_coverage(coverage, **changes):
    """A coverage mutated and re-digested through Phase 10.2's own function.

    Every fingerprint in the result is freshly recomputed. Nothing here is a
    stale digest: the adversary is a caller who edited the evidence and then did
    the arithmetic correctly.
    """
    tampered = replace(coverage, **changes)
    return replace(
        tampered,
        labelset_fingerprint=labelset_fingerprint(
            label_schema_version=tampered.label_schema_version,
            protocol_version=tampered.label_protocol_version,
            dataset_id=tampered.dataset_id,
            dataset_content_fingerprint=tampered.dataset_content_fingerprint,
            profile_id=tampered.profile_id,
            profile_context_fingerprint=tampered.profile_context_fingerprint,
            selector_version=tampered.selection.selector_version,
            selection_fingerprint=tampered.selection.selection_fingerprint,
            labels=tampered.labels,
        ),
    )


def test_a_label_outside_the_lot_is_refused_though_every_digest_is_recomputed(
    dataset,
):
    """The hole: lot A's fingerprint beside an answer to a posting outside A.

    The labelset digest is recomputed over the pair, so it is self-consistent —
    and the statement "these are the answers of lot A" is still false. The lot
    is retained as an object, so the claim can be checked instead of believed.
    """
    small = select_calibration_sample(dataset, sample_size=4)
    inside = sorted(small.opportunity_ids)
    intruder = next(
        opportunity_id
        for opportunity_id in range(1, 21)
        if opportunity_id not in set(inside)
    )
    honest = coverage_of(dict.fromkeys(inside, 2), dataset, selection=small)
    smuggled = resealed_coverage(
        honest,
        labels=canonical_label_order(
            honest.labels + (label(intruder, 3, dataset=dataset),)
        ),
    )
    # The digest really was recomputed: it is not the honest one, and it is the
    # digest Phase 10.2 would compute over exactly these labels and this lot.
    assert smuggled.labelset_fingerprint != honest.labelset_fingerprint
    assert smuggled.grade(intruder) == 3

    with pytest.raises(EvaluationBindingError, match="not in lot"):
        verify_label_coverage_structure(smuggled)
    with pytest.raises(EvaluationBindingError, match="not in lot"):
        verify_label_coverage(smuggled, dataset)
    with pytest.raises(EvaluationBindingError, match="not in lot"):
        run_over(dataset, coverage=smuggled)


def test_a_lot_whose_members_do_not_match_its_digest_is_refused(dataset):
    """The selection fingerprint is recomputed from the lot's own members."""
    coverage = coverage_of({1: 2}, dataset)
    forged_lot = replace(
        coverage.selection,
        items=coverage.selection.items[:1],
        effective_sample_size=1,
    )
    with pytest.raises(EvaluationBindingError, match="lot claims fingerprint"):
        verify_label_coverage_structure(replace(coverage, selection=forged_lot))


@pytest.mark.parametrize(
    "field_name, value",
    [
        ("dataset_id", "evaluation-dataset-v3-" + "c" * 16),
        ("dataset_content_fingerprint", "e" * 64),
        ("profile_id", 2),
        ("profile_context_fingerprint", "4" * 64),
        ("label_schema_version", "human-label-v2"),
        ("protocol_version", FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION),
    ],
)
def test_a_label_contradicting_the_coverage_bindings_is_refused(
    dataset, field_name, value
):
    """The grade is untouched, so the semantic digest does not move by itself.

    The label's *bindings* are not in Phase 10.2's semantic label payload — only
    the judgement is — so a row smuggled in from another snapshot or another
    rubric contributes the same grade and leaves the labelset digest unchanged.
    It is refused on its bindings, which is the only place the difference shows.
    """
    honest = coverage_of({1: 2, 2: 3}, dataset)
    contradicting = replace(honest.labels[0], **{field_name: value})
    smuggled = resealed_coverage(
        honest, labels=(contradicting,) + honest.labels[1:]
    )
    # The judgement itself did not change, so the digest did not either.
    assert smuggled.labelset_fingerprint == honest.labelset_fingerprint
    assert smuggled.grades == honest.grades
    with pytest.raises(EvaluationBindingError, match="and the labelset states"):
        verify_label_coverage_structure(smuggled)
    with pytest.raises(EvaluationBindingError):
        run_over(dataset, coverage=smuggled)


@pytest.mark.parametrize("grade", [99, -1, 4])
def test_a_label_grade_outside_the_frozen_domain_is_refused(dataset, grade):
    """Phase 10.2's rubric, reused: there is no grade 99 to be relevant.

    Such a label has no canonical Phase 10.2 payload at all — the semantic
    payload names the grade, and there is no name for 99 — so no digest can
    honestly be computed around it. The grade is therefore checked before the
    digest is recomputed, and both refusals are asserted here.
    """
    honest = coverage_of({1: 2, 2: 3}, dataset)
    broken = (replace(honest.labels[0], relevance_grade=grade),) + honest.labels[1:]
    with pytest.raises(KeyError):
        labelset_fingerprint(
            label_schema_version=honest.label_schema_version,
            protocol_version=honest.label_protocol_version,
            dataset_id=honest.dataset_id,
            dataset_content_fingerprint=honest.dataset_content_fingerprint,
            profile_id=honest.profile_id,
            profile_context_fingerprint=honest.profile_context_fingerprint,
            selector_version=honest.selector_version,
            selection_fingerprint=honest.selection_fingerprint,
            labels=broken,
        )
    smuggled = replace(honest, labels=broken)
    with pytest.raises(HumanLabelError, match="relevance_grade"):
        verify_label_coverage_structure(smuggled)
    with pytest.raises(HumanLabelError, match="relevance_grade"):
        run_over(dataset, coverage=smuggled)


def test_the_coverage_grade_mapping_cannot_be_mutated(dataset):
    """Read-only, and derived: there is no way to add a grade after the fact."""
    coverage = coverage_of({1: 2}, dataset)
    with pytest.raises(TypeError):
        coverage.grades[7] = 3
    with pytest.raises(TypeError):
        del coverage.grades[1]
    with pytest.raises(AttributeError):
        coverage.grades.clear()
    with pytest.raises(FrozenInstanceError):
        coverage.labelset_fingerprint = "f" * 64


# --------------------------------------------------------------------------
# Blocker B: structure is re-established, not inferred from a valid digest
# --------------------------------------------------------------------------


def resealed_universe(run, **changes):
    """A universe mutated and re-digested, inside a run that is also re-digested.

    The adversary this models is not a corrupted file — it is a caller that
    built an invalid artefact and computed perfectly good fingerprints over it.
    """
    universe = replace(run.universe, **changes)
    universe = replace(
        universe, fingerprint=evaluation_universe_fingerprint(universe)
    )
    forged = replace(run, universe=universe)
    return replace(forged, run_fingerprint=evaluation_run_fingerprint(forged))


def resealed_ranking(run, **changes):
    ranking = replace(run.ranking, **changes)
    ranking = replace(ranking, fingerprint=evaluation_ranking_fingerprint(ranking))
    forged = replace(run, ranking=ranking)
    return replace(forged, run_fingerprint=evaluation_run_fingerprint(forged))


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"opportunity_ids": (1, 1, 2), "size": 3}, "repeats opportunity ids"),
        ({"opportunity_ids": (2, 1, 3), "size": 3}, "canonical order"),
        ({"opportunity_ids": (1, 2, 3), "size": 4}, "declares size"),
        ({"opportunity_ids": (), "size": 0}, "at least one opportunity"),
        ({"opportunity_ids": (1, True), "size": 2}, "opportunity id"),
        ({"opportunity_ids": (0, 1), "size": 2}, "positive opportunity id"),
        ({"opportunity_ids": (1, 2), "size": True}, "non-integer size"),
        ({"kind": "FROZEN_DATASET_COHORT"}, "universe kind"),
        ({"dataset_content_fingerprint": "nope"}, "SHA-256"),
    ],
)
def test_a_structurally_invalid_universe_is_refused_however_it_is_digested(
    dataset, changes, message
):
    run = run_over(dataset)
    with pytest.raises(EvaluationMetricsError, match=message):
        verify_evaluation_run(resealed_universe(run, **changes), dataset)


def test_an_unsupported_universe_version_is_refused_after_re_digesting(dataset):
    run = run_over(dataset)
    with pytest.raises(MetricContractError, match="universe version"):
        verify_evaluation_run(
            resealed_universe(run, universe_version="evaluation-universe-v2"),
            dataset,
        )


def entries(*pairs):
    return tuple(
        EvaluationRankingEntry(rank_position=position, opportunity_id=identifier)
        for position, identifier in pairs
    )


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"entries": entries((1, 5), (1, 6))}, "repeats rank positions"),
        ({"entries": entries((1, 5), (2, 5))}, "repeats opportunity ids"),
        ({"entries": entries((1, 5), (3, 6))}, "canonical contiguous"),
        ({"entries": entries((2, 5), (1, 6))}, "canonical contiguous"),
        ({"entries": ()}, "at least one ranked opportunity"),
        ({"entries": entries((True, 5))}, "rank position"),
        ({"entries": entries((1, True))}, "opportunity id"),
        ({"source": "FROZEN_DATASET_RECOMMENDATION"}, "ranking source"),
        ({"profile_id": True}, "non-integer profile_id"),
        ({"profile_context_fingerprint": "nope"}, "SHA-256"),
    ],
)
def test_a_structurally_invalid_ranking_is_refused_however_it_is_digested(
    dataset, changes, message
):
    run = run_over(dataset)
    with pytest.raises(EvaluationMetricsError, match=message):
        verify_evaluation_run(resealed_ranking(run, **changes), dataset)


def test_an_unsupported_ranking_version_is_refused_after_re_digesting(dataset):
    run = run_over(dataset)
    with pytest.raises(MetricContractError, match="ranking version"):
        verify_evaluation_run(
            resealed_ranking(run, ranking_version="evaluation-ranking-v2"),
            dataset,
        )


def test_an_invalid_source_rank_position_is_refused_after_re_digesting(dataset):
    run = run_over(dataset)
    broken = (replace(run.ranking.entries[0], source_rank_position=0),) + (
        run.ranking.entries[1:]
    )
    with pytest.raises(EvaluationBindingError, match="upstream rank position"):
        verify_evaluation_run(resealed_ranking(run, entries=broken), dataset)


def test_a_re_digested_ranking_leaving_the_universe_is_still_refused(dataset):
    """A valid ranking, validly digested, over postings the universe excludes."""
    run = run_over(dataset, universe_ids=list(range(1, 13)))
    outside = replace(
        run.ranking.entries[-1], opportunity_id=20, source_rank_position=None
    )
    with pytest.raises(EvaluationBindingError, match="outside the evaluation universe"):
        verify_evaluation_run(
            resealed_ranking(run, entries=run.ranking.entries[:-1] + (outside,)),
            dataset,
        )


# --------------------------------------------------------------------------
# membership: only the frozen dataset can say an id names a real posting
# --------------------------------------------------------------------------


def test_a_universe_naming_an_absent_opportunity_is_refused_though_re_digested(
    dataset,
):
    """`999` digests exactly as well as a real opportunity does.

    Structurally this universe is perfect — unique, ordered, sized, positive
    ids — and its fingerprint and the run's are freshly recomputed around it.
    Only the snapshot knows that 999 is not in it, so only a check that holds
    the snapshot can refuse this.
    """
    run = run_over(dataset)
    members = run.universe.opportunity_ids[:-1] + (999,)
    forged = resealed_universe(
        run,
        kind=EvaluationUniverseKind.FIXED_BENCHMARK_POOL,
        opportunity_ids=members,
        size=len(members),
    )
    # The structural verifier cannot see it, and says so by accepting it.
    assert verify_evaluation_run_structure(forged) == forged.run_fingerprint
    with pytest.raises(EvaluationBindingError, match="absent from dataset"):
        verify_evaluation_run(forged, dataset)
    with pytest.raises(EvaluationBindingError, match="absent from dataset"):
        assert_universe_against_dataset(forged.universe, dataset)
    with pytest.raises(EvaluationBindingError, match="absent from dataset"):
        build_evaluation_run(
            dataset=dataset,
            universe=forged.universe,
            ranking=forged.ranking,
            coverage=coverage_of({1: 2}, dataset),
            evidence_class=EvidenceClass.DIAGNOSTIC_CALIBRATION,
        )


def test_a_ranking_naming_an_absent_opportunity_is_refused_though_re_digested(
    dataset,
):
    """The same for the ranking, with the universe widened to keep it in scope.

    A universe that holds 999 cannot be built — which is the point — so the test
    forges one too, precisely so that `ranking ⊆ universe` still holds and the
    only thing left to refuse the pair is the snapshot.
    """
    run = run_over(dataset)
    ranked = run.ranking.opportunity_ids[:-1] + (999,)
    members = canonical_opportunity_ids(set(ranked) | {1, 2, 3})
    widened = resealed_universe(
        run,
        kind=EvaluationUniverseKind.FIXED_BENCHMARK_POOL,
        opportunity_ids=members,
        size=len(members),
    )
    forged = resealed_ranking(
        widened,
        entries=run.ranking.entries[:-1]
        + (
            replace(
                run.ranking.entries[-1],
                opportunity_id=999,
                source_rank_position=None,
            ),
        ),
    )
    # Structurally impeccable, and `ranking ⊆ universe` holds.
    assert verify_evaluation_run_structure(forged) == forged.run_fingerprint
    with pytest.raises(EvaluationBindingError, match="absent from dataset"):
        assert_ranking_against_dataset(forged.ranking, dataset)
    with pytest.raises(EvaluationBindingError, match="absent from dataset"):
        verify_evaluation_run(forged, dataset)
    with pytest.raises(EvaluationBindingError, match="absent from dataset"):
        build_evaluation_run(
            dataset=dataset,
            universe=forged.universe,
            ranking=forged.ranking,
            coverage=coverage_of({1: 2}, dataset),
            evidence_class=EvidenceClass.DIAGNOSTIC_CALIBRATION,
        )


def test_a_universe_built_for_another_snapshot_is_refused_by_the_shared_check(
    dataset,
):
    other = ranked_dataset(content_fingerprint="e" * 64)
    with pytest.raises(EvaluationBindingError, match="content fingerprint"):
        assert_universe_against_dataset(
            build_evaluation_universe(other), dataset
        )
    with pytest.raises(EvaluationBindingError, match="content fingerprint"):
        assert_ranking_against_dataset(
            ranking_from_frozen_dataset(other), dataset
        )


# --------------------------------------------------------------------------
# an availability gate cannot be reached with an unverified run
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forge",
    [
        lambda run: resealed_universe(
            run, opportunity_ids=(1, 1, 2), size=3
        ),
        lambda run: resealed_ranking(run, entries=entries((1, 5), (1, 6))),
        lambda run: resealed_universe(
            run,
            kind=EvaluationUniverseKind.FIXED_BENCHMARK_POOL,
            opportunity_ids=(999,),
            size=1,
        ),
    ],
)
def test_a_gate_cannot_be_reached_with_a_re_sealed_invalid_run(dataset, forge):
    """No verifier is called by the test: the gate's argument cannot be built.

    The gates take a `MetricRunContext`, and the only way to obtain one runs the
    full dataset-bound verification. So a run that was reconstructed invalid and
    then carefully re-digested fails where the context is assembled — not later,
    and not only if somebody remembered to check.
    """
    coverage = coverage_of(dict.fromkeys(range(1, 21), 2), dataset)
    forged = forge(run_over(dataset, coverage=coverage))
    with pytest.raises(EvaluationMetricsError):
        build_metric_run_context(
            dataset=dataset, run=forged, coverage=coverage
        )


def test_a_gate_refuses_anything_that_is_not_a_context(dataset):
    """Refused by the gate's own verifier, in this package's own vocabulary."""
    coverage = coverage_of(dict.fromkeys(range(1, 21), 2), dataset)
    run = run_over(dataset, coverage=coverage)
    for gate in (
        precision_at_k_availability,
        recall_at_k_availability,
        ndcg_at_k_availability,
    ):
        assert list(inspect.signature(gate).parameters) == ["context", "k"]
        with pytest.raises(EvaluationBindingError, match="not a metric run context"):
            gate(run, 10)
        with pytest.raises(EvaluationBindingError, match="not a metric run context"):
            gate(None, 10)


# --------------------------------------------------------------------------
# a hand-built context is allowed to exist, and proves nothing
# --------------------------------------------------------------------------


def test_the_context_carries_no_caller_supplied_verdict():
    """There is no boolean to set, and nothing to overwrite after the fact.

    `labelset_matches` is a derived property, not a constructor field: the shape
    that used to let a forged context assert its own conclusion is gone, and the
    gates take the value the verifier returns rather than reading the property
    at all.
    """
    parameters = list(inspect.signature(MetricRunContext).parameters)
    assert parameters == ["dataset", "run", "coverage"]
    assert "labelset_matches" not in parameters
    assert isinstance(MetricRunContext.__dict__["labelset_matches"], property)


def test_a_hand_built_context_around_an_invalid_run_is_refused_by_every_gate(
    dataset,
):
    """Blocker: `build_metric_run_context` is bypassed entirely.

    The run is forged into an invalid structure and every fingerprint — the
    universe's and the enclosing run's — is recomputed around it. The context is
    then constructed directly. No verifier is called by this test; the gates
    call one, which is the whole point.
    """
    coverage = coverage_of(dict.fromkeys(range(1, 21), 2), dataset)
    run = run_over(dataset, coverage=coverage)
    forged = resealed_universe(run, opportunity_ids=(1, 1, 2), size=3)
    context = MetricRunContext(dataset=dataset, run=forged, coverage=coverage)

    for gate in (
        precision_at_k_availability,
        recall_at_k_availability,
        ndcg_at_k_availability,
    ):
        with pytest.raises(EvaluationMetricsError, match="repeats opportunity ids"):
            gate(context, 10)


def test_a_hand_built_context_around_an_absent_opportunity_is_refused(dataset):
    """The same, for the membership hole only the snapshot can see."""
    coverage = coverage_of(dict.fromkeys(range(1, 21), 2), dataset)
    run = run_over(dataset, coverage=coverage)
    members = run.universe.opportunity_ids[:-1] + (999,)
    forged = resealed_universe(
        run,
        kind=EvaluationUniverseKind.FIXED_BENCHMARK_POOL,
        opportunity_ids=members,
        size=len(members),
    )
    context = MetricRunContext(dataset=dataset, run=forged, coverage=coverage)
    for gate in (
        precision_at_k_availability,
        recall_at_k_availability,
        ndcg_at_k_availability,
    ):
        with pytest.raises(EvaluationBindingError, match="absent from dataset"):
            gate(context, 10)


def test_a_hand_built_context_cannot_override_a_labelset_mismatch(dataset):
    """The second half of the blocker: no flag can make a foreign labelset fit.

    The run is bound to labelset A; the coverage is a perfectly valid labelset B
    over the same dataset and profile. The context is built by hand, and the
    conclusion is still `N_A / LABELSET_BINDING_MISMATCH` — because the gates use
    what the verifier derives from the two artefacts, and there is no field to
    contradict it with.
    """
    grades = dict.fromkeys(range(1, 21), 3)
    run = run_over(dataset, coverage=coverage_of(grades, dataset))
    other_lot = coverage_of(
        grades,
        dataset,
        selection=select_calibration_sample(dataset, sample_size=6),
    )
    assert other_lot.labelset_fingerprint != run.labelset_fingerprint

    context = MetricRunContext(dataset=dataset, run=run, coverage=other_lot)
    assert not context.labelset_matches
    assert verify_metric_run_context(context) is False
    for gate in (
        precision_at_k_availability,
        recall_at_k_availability,
        ndcg_at_k_availability,
    ):
        availability = gate(context, 10)
        assert not availability.available
        assert availability.reason is MetricUnavailableReason.LABELSET_BINDING_MISMATCH

    # And the property cannot be overwritten to say otherwise either.
    with pytest.raises((AttributeError, FrozenInstanceError)):
        context.labelset_matches = True


def test_a_hand_built_context_for_another_dataset_is_refused(dataset):
    """Direct construction does not bypass the dataset and profile bindings."""
    coverage = coverage_of(dict.fromkeys(range(1, 21), 2), dataset)
    run = run_over(dataset, coverage=coverage)
    other = ranked_dataset(dataset_id="evaluation-dataset-v3-" + "c" * 16)

    with pytest.raises(EvaluationDatasetError):
        precision_at_k_availability(
            MetricRunContext(dataset=other, run=run, coverage=coverage), 10
        )
    other_coverage = coverage_of(dict.fromkeys(range(1, 21), 2), other)
    with pytest.raises(EvaluationDatasetError):
        ndcg_at_k_availability(
            MetricRunContext(dataset=dataset, run=run, coverage=other_coverage), 10
        )
    profile_other = ranked_dataset(profile_context_fingerprint="4" * 64)
    with pytest.raises(EvaluationDatasetError):
        recall_at_k_availability(
            MetricRunContext(
                dataset=profile_other,
                run=run,
                coverage=coverage_of({1: 2}, profile_other),
            ),
            10,
        )


def test_a_hand_built_context_around_verified_artefacts_behaves_exactly_as_built(
    dataset,
):
    """Symmetry: the builder is a convenience, not a privilege.

    A context assembled by hand around artefacts that *are* sound decides
    exactly what the builder's context decides. Verification is what differs
    between contexts, not provenance.
    """
    grades = {index: (3 if index <= 4 else 0) for index in range(1, 21)}
    coverage = coverage_of(grades, dataset)
    run = run_over(dataset, coverage=coverage)
    built = build_metric_run_context(dataset=dataset, run=run, coverage=coverage)
    by_hand = MetricRunContext(dataset=dataset, run=run, coverage=coverage)

    for gate in (
        precision_at_k_availability,
        recall_at_k_availability,
        ndcg_at_k_availability,
    ):
        assert gate(built, 10) == gate(by_hand, 10)
        assert gate(built, 10).available


def test_the_builders_and_the_verifier_hold_one_contract(dataset):
    """The validators are shared, so neither side can be lenient alone."""
    run = run_over(dataset)
    assert validate_evaluation_universe_structure(run.universe) is run.universe
    assert validate_evaluation_ranking_structure(run.ranking) is run.ranking
    with pytest.raises(EvaluationBindingError):
        validate_evaluation_universe_structure(run.ranking)
    with pytest.raises(EvaluationBindingError):
        validate_evaluation_ranking_structure(run.universe)


def test_a_run_that_is_not_a_run_is_refused():
    with pytest.raises(EvaluationBindingError, match="not an evaluation run"):
        verify_evaluation_run_structure(
            {"run_schema_version": EVALUATION_RUN_SCHEMA_VERSION}
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
    grades = dict.fromkeys(range(1, 13), 2)
    del grades[7]
    coverage = coverage_of(grades, dataset)
    availability = precision_at_k_availability(context_of(dataset, coverage), 10)
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
    coverage = coverage_of(dict.fromkeys(range(1, 11), 2), dataset)
    context = context_of(dataset, coverage)
    assert precision_at_k_availability(context, 10).available

    ndcg = ndcg_at_k_availability(context, 10)
    assert not ndcg.available
    assert ndcg.reason is MetricUnavailableReason.EVALUATION_UNIVERSE_NOT_FULLY_JUDGED

    recall = recall_at_k_availability(context, 10)
    assert not recall.available
    assert recall.reason is MetricUnavailableReason.RECALL_DENOMINATOR_UNKNOWN
    assert recall.support.relevant_count is None


def test_4_a_fully_judged_universe_with_nothing_relevant_has_no_recall(dataset):
    """The denominator is known, and it is zero. That is not a recall of 0.0."""
    coverage = coverage_of(dict.fromkeys(range(1, 21), 1), dataset)
    recall = recall_at_k_availability(context_of(dataset, coverage), 10)
    assert not recall.available
    assert recall.reason is MetricUnavailableReason.NO_RELEVANT_ITEMS
    assert recall.support.relevant_count == 0
    assert recall.support.universe_size == 20
    assert recall.support.judged_count == 20


def test_5_a_fully_judged_universe_with_relevant_items_opens_every_gate(dataset):
    grades = {index: (3 if index <= 4 else 0) for index in range(1, 21)}
    coverage = coverage_of(grades, dataset)
    context = context_of(dataset, coverage)
    availabilities = [
        precision_at_k_availability(context, 10),
        recall_at_k_availability(context, 10),
        ndcg_at_k_availability(context, 10),
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
    coverage = coverage_of(dict.fromkeys(range(1, 21), 2), dataset)
    context = context_of(dataset, coverage)
    for gate in (
        precision_at_k_availability,
        recall_at_k_availability,
        ndcg_at_k_availability,
    ):
        with pytest.raises(MetricArgumentError, match="positive integer"):
            gate(context, True)


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
