"""What one experiment run has to work with, and how that is established.

These tests are about `context.py`: the trust boundary, D42's binding to exactly
one Phase 10.4 run, and D43/D55's rule that the ranking inputs are present
exactly when there is a ranking — with a missing input a **precondition
failure** rather than a new `N_A`.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from evaluation.business_metrics import BusinessMetricBindingError
from evaluation.experiments import (
    EXPERIMENT_CONTRACT_VERSION,
    EXPERIMENT_RUN_CONTEXT_SCHEMA_VERSION,
    ExperimentBindingError,
    ExperimentContractError,
    ExperimentRunContext,
    RankingEvaluationInputs,
    build_experiment_run_context,
    verify_experiment_run_context,
)
from evaluation.metrics import (
    EvaluationUniverseKind,
    EvidenceClass,
    RankingSource,
    build_evaluation_ranking,
    build_evaluation_run,
    build_evaluation_universe,
    ranking_from_frozen_dataset,
)
from evaluation.metrics.schema import EvaluationRankingEntry

from tests.unit.experiment_fixtures import (
    TARGET_COUNTRY,
    business_run,
    coverage_of,
    dataset_of,
    evaluation_run_over,
    experiment_context,
    fully_judged,
    manifest_payload,
    ranked_records,
)


def context_of(records, **kwargs):
    return experiment_context(records, **kwargs)


# ====================================================================
# the trust boundary
# ====================================================================


def test_a_verified_context_derives_its_persistent_binding() -> None:
    records = ranked_records()
    payload = manifest_payload(records)
    context = context_of(records)
    binding = verify_experiment_run_context(context)
    assert binding.run_context_schema_version == (
        EXPERIMENT_RUN_CONTEXT_SCHEMA_VERSION
    )
    assert binding.experiment_contract_version == EXPERIMENT_CONTRACT_VERSION
    assert binding.dataset_id == payload["dataset_id"]
    assert binding.dataset_content_fingerprint == payload["content_fingerprint"]
    assert binding.profile_id == payload["profile_context"]["profile_id"]
    assert binding.business_metric_run_fingerprint == (
        context.business_metric_run.run_fingerprint
    )
    assert binding.ranking_evaluation_run_fingerprint == (
        context.ranking_evaluation_inputs.evaluation_run.run_fingerprint
    )


def test_the_context_is_a_public_dataclass_and_proves_nothing() -> None:
    """Constructing one by hand is allowed; it establishes nothing at all."""
    records = ranked_records()
    forged = ExperimentRunContext(
        frozen_dataset=dataset_of(records),
        manifest=manifest_payload(records),
        business_metric_run=business_run(ranked_records(size=6)),
    )
    # The Phase 10.4 run is about another cohort, so verification refuses it.
    with pytest.raises((ExperimentBindingError, BusinessMetricBindingError)):
        verify_experiment_run_context(forged)


def test_a_context_that_is_not_a_context_is_refused() -> None:
    with pytest.raises(ExperimentContractError, match="not an experiment run"):
        verify_experiment_run_context({"frozen_dataset": None})


def test_a_bare_record_list_is_not_a_frozen_dataset() -> None:
    """Phase 10.5 reads a snapshot that passed Phase 10.2's integrity gate."""
    records = ranked_records()
    with pytest.raises(ExperimentContractError, match="integrity gate"):
        verify_experiment_run_context(
            ExperimentRunContext(
                frozen_dataset=list(records),
                manifest=manifest_payload(records),
                business_metric_run=business_run(records),
            )
        )


def test_something_that_is_not_a_business_metric_run_is_refused() -> None:
    records = ranked_records()
    with pytest.raises(ExperimentContractError, match="bound to exactly one"):
        verify_experiment_run_context(
            ExperimentRunContext(
                frozen_dataset=dataset_of(records),
                manifest=manifest_payload(records),
                business_metric_run={"run_fingerprint": "a" * 64},
            )
        )


def test_an_empty_cohort_is_refused() -> None:
    records = ranked_records(size=1, ranked=0)
    dataset = dataset_of(records)
    empty = replace(dataset, records=(), record_count=0)
    with pytest.raises(ExperimentBindingError, match="not a cohort"):
        verify_experiment_run_context(
            ExperimentRunContext(
                frozen_dataset=empty,
                manifest=manifest_payload(records),
                business_metric_run=business_run(records),
            )
        )


# ====================================================================
# D42 — one Phase 10.4 run, over the same everything
# ====================================================================


def test_the_bound_business_metric_run_is_fully_reverified() -> None:
    """Not a fingerprint comparison: the whole arithmetic, over these records."""
    records = ranked_records()
    run = business_run(records)
    forged = replace(
        run,
        results=(
            replace(run.results[0], value=0.123456),
            *run.results[1:],
        ),
    )
    with pytest.raises((ExperimentBindingError, BusinessMetricBindingError)):
        verify_experiment_run_context(
            ExperimentRunContext(
                frozen_dataset=dataset_of(records),
                manifest=manifest_payload(records),
                business_metric_run=forged,
            )
        )


def test_a_business_metric_run_over_another_cohort_is_refused() -> None:
    records = ranked_records(size=12)
    other = ranked_records(size=9)
    with pytest.raises((ExperimentBindingError, BusinessMetricBindingError)):
        verify_experiment_run_context(
            ExperimentRunContext(
                frozen_dataset=dataset_of(records),
                manifest=manifest_payload(records),
                business_metric_run=business_run(other),
            )
        )


def test_an_altered_manifest_is_refused() -> None:
    records = ranked_records()
    payload = dict(manifest_payload(records))
    payload["record_count"] = 99
    with pytest.raises((ExperimentBindingError, BusinessMetricBindingError)):
        verify_experiment_run_context(
            ExperimentRunContext(
                frozen_dataset=dataset_of(records),
                manifest=payload,
                business_metric_run=business_run(records),
            )
        )


def test_a_manifest_that_is_not_a_mapping_is_refused() -> None:
    records = ranked_records()
    with pytest.raises(ExperimentContractError, match="manifest"):
        verify_experiment_run_context(
            ExperimentRunContext(
                frozen_dataset=dataset_of(records),
                manifest="manifest.json",
                business_metric_run=business_run(records),
            )
        )


# ====================================================================
# D43 / D55 — the ranking inputs, in both directions
# ====================================================================


def test_a_snapshot_with_no_recommendation_takes_no_ranking_inputs() -> None:
    context = context_of(ranked_records(size=5, ranked=0), with_ranking=False)
    binding = verify_experiment_run_context(context)
    assert context.ranking_evaluation_inputs is None
    assert binding.ranking_evaluation_run_fingerprint is None


def test_ranking_inputs_for_a_snapshot_with_no_ranking_are_refused() -> None:
    """There is no observed ordering for them to be about."""
    records = ranked_records(size=5, ranked=0)
    dataset = dataset_of(records)
    ranked = ranked_records(size=5, ranked=3)
    ranked_dataset = dataset_of(ranked)
    coverage = fully_judged(ranked_dataset)
    with pytest.raises(ExperimentBindingError, match="froze no recommendation"):
        verify_experiment_run_context(
            ExperimentRunContext(
                frozen_dataset=dataset,
                manifest=manifest_payload(records),
                business_metric_run=business_run(records),
                ranking_evaluation_inputs=RankingEvaluationInputs(
                    evaluation_run=evaluation_run_over(ranked_dataset, coverage),
                    label_coverage=coverage,
                ),
            )
        )


def test_a_ranking_with_no_inputs_is_a_precondition_failure_not_an_na() -> None:
    """D55. A missing input must not look like a measured absence of evidence."""
    records = ranked_records(size=8, ranked=5)
    with pytest.raises(
        ExperimentBindingError, match="requires the Phase 10.3 evaluation run"
    ):
        verify_experiment_run_context(
            ExperimentRunContext(
                frozen_dataset=dataset_of(records),
                manifest=manifest_payload(records),
                business_metric_run=business_run(records),
                ranking_evaluation_inputs=None,
            )
        )


def test_the_precondition_failure_message_explains_why_it_is_not_an_na() -> None:
    records = ranked_records(size=8, ranked=5)
    with pytest.raises(ExperimentBindingError) as error:
        build_experiment_run_context(
            frozen_dataset=dataset_of(records),
            manifest=manifest_payload(records),
            business_metric_run=business_run(records),
        )
    assert "measured absence of evidence" in str(error.value)


def test_inputs_that_are_not_the_bundle_are_refused() -> None:
    records = ranked_records(size=8, ranked=5)
    with pytest.raises(ExperimentContractError, match="ranking evaluation inputs"):
        verify_experiment_run_context(
            ExperimentRunContext(
                frozen_dataset=dataset_of(records),
                manifest=manifest_payload(records),
                business_metric_run=business_run(records),
                ranking_evaluation_inputs={"evaluation_run": None},
            )
        )


def test_a_partially_judged_labelset_is_perfectly_legal() -> None:
    """It produces Phase 10.3's own N_A results, not a refusal here."""
    records = ranked_records(size=10, ranked=6)
    dataset = dataset_of(records)
    coverage = coverage_of({1: 3, 2: 1}, dataset)
    context = context_of(records, coverage=coverage)
    binding = verify_experiment_run_context(context)
    assert binding.ranking_evaluation_run_fingerprint is not None


# ====================================================================
# what Phase 10.5 may evaluate
# ====================================================================


def test_a_universe_that_is_not_the_whole_cohort_is_refused() -> None:
    """A recall over the ranked set could not see a posting the ranking missed."""
    records = ranked_records(size=10, ranked=6)
    dataset = dataset_of(records)
    coverage = fully_judged(dataset)
    pool_run = evaluation_run_over(
        dataset, coverage, universe_ids=[1, 2, 3, 4, 5, 6]
    )
    assert pool_run.universe.kind is EvaluationUniverseKind.FIXED_BENCHMARK_POOL
    with pytest.raises(ExperimentBindingError, match="FROZEN_DATASET_COHORT"):
        verify_experiment_run_context(
            ExperimentRunContext(
                frozen_dataset=dataset,
                manifest=manifest_payload(records),
                business_metric_run=business_run(records),
                ranking_evaluation_inputs=RankingEvaluationInputs(
                    evaluation_run=pool_run, label_coverage=coverage
                ),
            )
        )


def test_a_declared_offline_ranking_is_refused() -> None:
    """Only the ordering production actually produced may be evaluated."""
    records = ranked_records(size=10, ranked=6)
    dataset = dataset_of(records)
    coverage = fully_judged(dataset)
    declared = build_evaluation_ranking(
        dataset,
        [
            EvaluationRankingEntry(rank_position=1, opportunity_id=2),
            EvaluationRankingEntry(rank_position=2, opportunity_id=1),
        ],
        source=RankingSource.DECLARED_OFFLINE_RANKING,
    )
    run = build_evaluation_run(
        dataset=dataset,
        universe=build_evaluation_universe(dataset),
        ranking=declared,
        coverage=coverage,
        evidence_class=EvidenceClass.DIAGNOSTIC_CALIBRATION,
    )
    with pytest.raises(
        ExperimentBindingError, match="FROZEN_DATASET_RECOMMENDATION"
    ):
        verify_experiment_run_context(
            ExperimentRunContext(
                frozen_dataset=dataset,
                manifest=manifest_payload(records),
                business_metric_run=business_run(records),
                ranking_evaluation_inputs=RankingEvaluationInputs(
                    evaluation_run=run, label_coverage=coverage
                ),
            )
        )


def test_the_frozen_ranking_source_is_what_the_fixture_actually_builds() -> None:
    records = ranked_records()
    dataset = dataset_of(records)
    assert (
        ranking_from_frozen_dataset(dataset).source
        is RankingSource.FROZEN_DATASET_RECOMMENDATION
    )


# ====================================================================
# the D44 fingerprint domain, in behaviour
# ====================================================================


def test_the_context_fingerprint_ignores_the_benchmark_rows() -> None:
    """Transient evidence for verifying somebody else's binding."""
    records = ranked_records()
    payload = manifest_payload(records)
    dataset = dataset_of(records, manifest=payload)
    coverage = fully_judged(dataset)
    inputs = RankingEvaluationInputs(
        evaluation_run=evaluation_run_over(dataset, coverage),
        label_coverage=coverage,
    )
    run = business_run(records, manifest=payload)
    plain = build_experiment_run_context(
        frozen_dataset=dataset,
        manifest=payload,
        business_metric_run=run,
        ranking_evaluation_inputs=inputs,
    )
    assert (
        verify_experiment_run_context(plain).context_fingerprint
        == verify_experiment_run_context(plain).context_fingerprint
    )


def test_the_context_fingerprint_is_stable_across_two_assemblies() -> None:
    records = ranked_records()
    first = verify_experiment_run_context(context_of(records))
    second = verify_experiment_run_context(context_of(records))
    assert first.context_fingerprint == second.context_fingerprint


def test_a_different_cohort_is_a_different_context() -> None:
    first = verify_experiment_run_context(context_of(ranked_records(size=12)))
    second = verify_experiment_run_context(context_of(ranked_records(size=11)))
    assert first.context_fingerprint != second.context_fingerprint


def test_an_unknown_target_is_a_different_context() -> None:
    """The bound Phase 10.4 run differs, so the context does."""
    records = ranked_records()
    with_target = verify_experiment_run_context(
        context_of(records, target_country=TARGET_COUNTRY)
    )
    without = verify_experiment_run_context(
        context_of(records, target_country=None)
    )
    assert with_target.context_fingerprint != without.context_fingerprint


def test_the_binding_cannot_be_minted_from_strings() -> None:
    """`experiment_run_context_binding` takes only the private verified bundle."""
    from evaluation.experiments import experiment_run_context_binding

    with pytest.raises(ExperimentContractError, match="never assembled from"):
        experiment_run_context_binding({"dataset_id": "x"})
