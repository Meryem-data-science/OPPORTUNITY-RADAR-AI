"""The atomic twelve-block run, and what each of the two verifiers establishes.

The forgery tests are the point of this file. A self-consistent but wrong block
is the artefact the structural verifier cannot catch and the full verifier must,
and every one of the six forgeries below is built to pass as much of the chain
as it possibly can.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from evaluation.experiments import (
    EXPERIMENT_CONTRACT_VERSION,
    EXPERIMENT_RUN_SCHEMA_VERSION,
    OVERLAP_PAIR_ORDER,
    PROJECTION_ORDER,
    CohortProjection,
    ExperimentBindingError,
    ExperimentContractError,
    ExperimentRun,
    ExperimentRunContext,
    ExperimentRunProvenance,
    OverlapPair,
    OverlapStatus,
    ProjectionStatus,
    RankingExperimentStatus,
    build_experiment_run,
    compute_overlap,
    compute_projection,
    experiment_run_fingerprint,
    overlap_pair_projections,
    projection_result_fingerprint,
    ranking_experiment_result_fingerprint,
    validate_experiment_run_structure,
    verify_experiment_run,
    verify_experiment_run_structure,
)
from evaluation.metrics import MetricName

from tests.unit.experiment_fixtures import (
    business_run,
    dataset_of,
    experiment_context,
    fully_judged,
    manifest_payload,
    ranked_records,
)


def run_of(records=None, **kwargs):
    context = experiment_context(records, **kwargs)
    return build_experiment_run(context), context


def reseal(run: ExperimentRun) -> ExperimentRun:
    """Re-digest a run after editing a block, so its own identity is valid.

    Every forgery below goes through this: a forged run whose `run_fingerprint`
    was left stale is caught by arithmetic nobody needs a verifier for, and
    proves nothing about whether the verification works.
    """
    return replace(run, run_fingerprint=experiment_run_fingerprint(run))


# ====================================================================
# the run is all twelve blocks
# ====================================================================


def test_a_run_holds_exactly_five_six_and_one() -> None:
    run, _ = run_of()
    assert len(run.projections) == 5
    assert len(run.overlaps) == 6
    assert run.ranking is not None
    assert len(run.projections) + len(run.overlaps) + 1 == 12


def test_the_blocks_are_in_the_canonical_order() -> None:
    run, _ = run_of()
    assert tuple(result.projection for result in run.projections) == PROJECTION_ORDER
    assert tuple(result.pair for result in run.overlaps) == OVERLAP_PAIR_ORDER


def test_an_na_block_is_present_and_counts_towards_the_twelve() -> None:
    run, _ = run_of(ranked_records(size=8, ranked=5), target_country=None)
    assert len(run.projections) == 5
    assert len(run.overlaps) == 6
    geo = run.projection(CohortProjection.GEO_NOT_EXPLICITLY_OUT_OF_TARGET)
    assert geo.status is ProjectionStatus.N_A
    assert run.overlap(OverlapPair.GEO_AND_MATCHING).status is OverlapStatus.N_A


def test_the_run_carries_no_global_status_or_tally() -> None:
    """D48: nothing derivable is duplicated, and there is no overall score."""
    fields = set(ExperimentRun.__dataclass_fields__)
    assert fields == {
        "run_schema_version",
        "contract_version",
        "context",
        "projections",
        "overlaps",
        "ranking",
        "run_fingerprint",
        "provenance",
    }
    for forbidden in (
        "status",
        "computed_count",
        "na_count",
        "total_score",
        "quality_score",
        "dataset_id",
        "context_fingerprint",
    ):
        assert forbidden not in fields, forbidden


def test_a_hard_error_leaves_no_run_at_all() -> None:
    """The arithmetic happens before the assembly, so there is no partial run."""
    records = ranked_records(size=8, ranked=5)
    run = business_run(records)
    forged = replace(
        run, results=(replace(run.results[0], value=0.5), *run.results[1:])
    )
    with pytest.raises(Exception):
        build_experiment_run(
            ExperimentRunContext(
                frozen_dataset=dataset_of(records),
                manifest=manifest_payload(records),
                business_metric_run=forged,
            )
        )


def test_the_builder_reaches_every_block_through_the_public_authority() -> None:
    """A run is assembled exactly the way any other caller would assemble one."""
    run, context = run_of()
    for result in run.projections:
        assert result.result_fingerprint == (
            compute_projection(context, result.projection).result_fingerprint
        )
    for result in run.overlaps:
        assert result.result_fingerprint == (
            compute_overlap(context, result.pair).result_fingerprint
        )


def test_the_versions_are_the_frozen_ones() -> None:
    run, _ = run_of()
    assert run.run_schema_version == EXPERIMENT_RUN_SCHEMA_VERSION
    assert run.contract_version == EXPERIMENT_CONTRACT_VERSION


# ====================================================================
# structural verification
# ====================================================================


def test_a_built_run_survives_both_verifiers() -> None:
    run, context = run_of()
    assert verify_experiment_run_structure(run) == run.run_fingerprint
    assert verify_experiment_run(run, context) == run.run_fingerprint


def test_neither_verifier_takes_a_weakening_argument() -> None:
    """D50: no `strict=False`, no `repair=True`, no `allow_partial`."""
    import inspect

    for function in (verify_experiment_run_structure, verify_experiment_run):
        names = set(inspect.signature(function).parameters)
        for forbidden in (
            "strict",
            "repair",
            "allow_partial",
            "skip_records",
            "tolerate",
        ):
            assert not any(forbidden in name for name in names), function


def test_a_missing_projection_is_refused() -> None:
    run, _ = run_of()
    with pytest.raises(ExperimentContractError, match="exactly"):
        verify_experiment_run_structure(
            reseal(replace(run, projections=run.projections[:4]))
        )


def test_an_extra_projection_is_refused() -> None:
    run, _ = run_of()
    with pytest.raises(ExperimentContractError, match="exactly"):
        verify_experiment_run_structure(
            reseal(
                replace(run, projections=run.projections + (run.projections[0],))
            )
        )


def test_a_missing_overlap_is_refused() -> None:
    run, _ = run_of()
    with pytest.raises(ExperimentContractError, match="exactly"):
        verify_experiment_run_structure(
            reseal(replace(run, overlaps=run.overlaps[:5]))
        )


def test_a_reordered_projection_list_is_refused() -> None:
    run, _ = run_of()
    reordered = (run.projections[1], run.projections[0], *run.projections[2:])
    with pytest.raises(ExperimentContractError, match="canonical order"):
        verify_experiment_run_structure(reseal(replace(run, projections=reordered)))


def test_a_reordered_overlap_list_is_refused() -> None:
    run, _ = run_of()
    reordered = (run.overlaps[1], run.overlaps[0], *run.overlaps[2:])
    with pytest.raises(ExperimentContractError, match="canonical order"):
        verify_experiment_run_structure(reseal(replace(run, overlaps=reordered)))


def test_a_child_digest_is_recomputed_before_the_parent() -> None:
    """A forged block with an edited digest field cannot mint a valid run."""
    run, _ = run_of()
    target = run.projections[4]
    forged_block = replace(
        target,
        included_opportunity_ids=target.included_opportunity_ids[:-1],
        included_count=target.included_count - 1,
        excluded_count=target.excluded_count + 1,
        cohort_share=(target.included_count - 1) / target.cohort_size,
    )
    # Re-digest the block itself, then the run: both identities are internally
    # perfect, and the membership is still not the one the records imply.
    forged_block = replace(
        forged_block,
        result_fingerprint=projection_result_fingerprint(forged_block),
    )
    forged = reseal(
        replace(run, projections=(*run.projections[:4], forged_block))
    )
    # The block's own digest survives; the overlaps that rest on it do not.
    with pytest.raises(ExperimentBindingError):
        verify_experiment_run_structure(forged)


def test_a_block_about_another_dataset_is_refused() -> None:
    run, _ = run_of()
    other = "b" * 64
    forged_block = run.projections[0]
    forged_block = replace(forged_block, dataset_content_fingerprint=other)
    forged_block = replace(
        forged_block, result_fingerprint=projection_result_fingerprint(forged_block)
    )
    forged = reseal(
        replace(run, projections=(forged_block, *run.projections[1:]))
    )
    with pytest.raises(ExperimentBindingError, match="dataset content"):
        verify_experiment_run_structure(forged)


def test_a_run_stating_another_contract_version_is_refused() -> None:
    run, _ = run_of()
    with pytest.raises(ExperimentContractError):
        validate_experiment_run_structure(
            replace(run, contract_version="evaluation-experiment-contract-v2")
        )


def test_an_edited_run_fingerprint_is_refused() -> None:
    run, _ = run_of()
    with pytest.raises(ExperimentBindingError, match="not what it says it is"):
        verify_experiment_run_structure(replace(run, run_fingerprint="c" * 64))


# ====================================================================
# N_A propagation and ranking coherence, without the records
# ====================================================================


def test_an_na_projection_with_computed_overlaps_is_refused() -> None:
    """Structural: the run's own blocks must agree about availability."""
    run, _ = run_of(ranked_records(size=8, ranked=5), target_country=None)
    healthy, _ = run_of(ranked_records(size=8, ranked=5))
    # Take the N_A geography projection, and a COMPUTED overlap that rests on
    # a geography projection from the healthy run.
    forged = reseal(
        replace(
            run,
            overlaps=(
                healthy.overlap(OverlapPair.GEO_AND_DATA_AI),
                *run.overlaps[1:],
            ),
        )
    )
    with pytest.raises(ExperimentBindingError):
        verify_experiment_run_structure(forged)


def test_a_computed_ranking_over_an_empty_recommendation_is_refused() -> None:
    empty, _ = run_of(ranked_records(size=6, ranked=0), with_ranking=False)
    healthy, _ = run_of(ranked_records(size=6, ranked=4))
    forged = reseal(replace(empty, ranking=healthy.ranking))
    with pytest.raises(ExperimentBindingError):
        verify_experiment_run_structure(forged)


def test_an_na_ranking_over_a_non_empty_recommendation_is_refused() -> None:
    healthy, _ = run_of(ranked_records(size=6, ranked=4))
    empty, _ = run_of(ranked_records(size=6, ranked=0), with_ranking=False)
    forged = reseal(replace(healthy, ranking=empty.ranking))
    with pytest.raises(ExperimentBindingError):
        verify_experiment_run_structure(forged)


def test_the_ranking_must_name_this_runs_own_recommendation_projection() -> None:
    run, _ = run_of(ranked_records(size=10, ranked=6))
    other, _ = run_of(ranked_records(size=10, ranked=7))
    forged_ranking = replace(
        run.ranking,
        recommendation_projection_result_fingerprint=(
            other.projection(
                CohortProjection.RECOMMENDATION_OBSERVED
            ).result_fingerprint
        ),
    )
    forged_ranking = replace(
        forged_ranking,
        result_fingerprint=ranking_experiment_result_fingerprint(forged_ranking),
    )
    forged = reseal(replace(run, ranking=forged_ranking))
    with pytest.raises(ExperimentBindingError, match="rests on projection"):
        verify_experiment_run_structure(forged)


# ====================================================================
# provenance is outside identity
# ====================================================================


def test_a_provenance_only_difference_does_not_move_the_run_fingerprint() -> None:
    context = experiment_context()
    bare = build_experiment_run(context)
    annotated = build_experiment_run(
        context,
        provenance=ExperimentRunProvenance(
            generated_at="2026-09-13T12:00:00+00:00",
            dataset_directory="/elsewhere/datasets/x",
            business_metric_run_path="/elsewhere/runs/y",
            label_root="/elsewhere/labels",
            benchmark_records_path="/elsewhere/gold.jsonl",
        ),
    )
    assert bare.run_fingerprint == annotated.run_fingerprint
    assert bare.provenance != annotated.provenance
    verify_experiment_run(annotated, context)


def test_the_identity_domain_excludes_provenance_entirely() -> None:
    from evaluation.experiments import canonical_experiment_run_payload

    run, _ = run_of()
    domain = canonical_experiment_run_payload(run)
    assert set(domain) == {
        "run_schema_version",
        "contract_version",
        "context_fingerprint",
        "projections",
        "overlaps",
        "ranking_result_fingerprint",
    }
    assert len(domain["projections"]) == 5
    assert len(domain["overlaps"]) == 6


# ====================================================================
# the six forgeries — self-consistent, and wrong
# ====================================================================


def test_forgery_1_a_self_consistent_but_wrong_projection() -> None:
    """Counts add up, share matches, digest valid — membership is not the records'."""
    run, context = run_of(ranked_records(size=10, ranked=6))
    target = run.projection(CohortProjection.RECOMMENDATION_OBSERVED)
    # Drop one ranked posting and add one unranked, keeping the count identical.
    forged_ids = tuple(sorted(set(target.included_ids) - {6} | {7}))
    forged_block = replace(target, included_opportunity_ids=forged_ids)
    forged_block = replace(
        forged_block, result_fingerprint=projection_result_fingerprint(forged_block)
    )
    from evaluation.experiments import validate_projection_result_structure

    # The block is structurally impeccable on its own.
    validate_projection_result_structure(forged_block)
    assert forged_block.included_count == target.included_count
    assert forged_block.cohort_share == target.cohort_share

    forged = reseal(
        replace(run, projections=(*run.projections[:4], forged_block))
    )
    # Full verification recomputes the membership and refuses.
    with pytest.raises(ExperimentBindingError):
        verify_experiment_run(forged, context)


def test_forgery_2_a_wrong_overlap_membership() -> None:
    run, context = run_of(ranked_records(size=10, ranked=6))
    target = run.overlap(OverlapPair.MATCHING_AND_RECOMMENDATION)
    both = target.both_opportunity_ids
    left_only = target.left_only_opportunity_ids
    # Move one posting from `both` into `left_only` and one back, so the four
    # partitions stay disjoint, exhaustive, and identically counted.
    forged_block = replace(
        target,
        both_opportunity_ids=tuple(sorted(set(both) - {both[0]} | {left_only[0]})),
        left_only_opportunity_ids=tuple(
            sorted(set(left_only) - {left_only[0]} | {both[0]})
        ),
    )
    from evaluation.experiments import validate_overlap_result_structure
    from evaluation.experiments import overlap_result_fingerprint

    validate_overlap_result_structure(forged_block)
    forged_block = replace(
        forged_block, result_fingerprint=overlap_result_fingerprint(forged_block)
    )
    position = OVERLAP_PAIR_ORDER.index(OverlapPair.MATCHING_AND_RECOMMENDATION)
    overlaps = list(run.overlaps)
    overlaps[position] = forged_block
    forged = reseal(replace(run, overlaps=tuple(overlaps)))
    # Caught even by the structural verifier, because the partitions are
    # re-derived from the run's own projections.
    with pytest.raises(ExperimentBindingError, match="two projections imply"):
        verify_experiment_run_structure(forged)
    with pytest.raises(ExperimentBindingError):
        verify_experiment_run(forged, context)


def test_forgery_3_a_wrong_ranking_metric_payload() -> None:
    """A value no formula produced, with a perfectly valid digest around it."""
    run, context = run_of(ranked_records(size=10, ranked=6))
    entry = run.ranking.metric_results[0]
    forged_entry = replace(entry, result=replace(entry.result, value=0.999))
    forged_ranking = replace(
        run.ranking,
        metric_results=(forged_entry, *run.ranking.metric_results[1:]),
    )
    forged_ranking = replace(
        forged_ranking,
        result_fingerprint=ranking_experiment_result_fingerprint(forged_ranking),
    )
    forged = reseal(replace(run, ranking=forged_ranking))
    # The structural verifier cannot tell: the payload is self-consistent.
    verify_experiment_run_structure(forged)
    with pytest.raises(ExperimentBindingError, match="differ in"):
        verify_experiment_run(forged, context)


def test_forgery_4_a_wrong_business_metric_run_binding() -> None:
    run, context = run_of(ranked_records(size=10, ranked=6))
    forged_binding = replace(
        run.context, business_metric_run_fingerprint="d" * 64
    )
    from evaluation.experiments import experiment_run_context_fingerprint

    forged_binding = replace(
        forged_binding,
        context_fingerprint=experiment_run_context_fingerprint(forged_binding),
    )
    forged = reseal(replace(run, context=forged_binding))
    # Internally perfect: the binding's digest covers its own eight fields.
    verify_experiment_run_structure(forged)
    with pytest.raises(ExperimentBindingError, match="these artefacts assemble"):
        verify_experiment_run(forged, context)


def test_forgery_5_a_missing_and_an_extra_result() -> None:
    run, context = run_of()
    for broken in (
        replace(run, projections=run.projections[:4]),
        replace(run, overlaps=run.overlaps + (run.overlaps[0],)),
    ):
        forged = reseal(broken)
        with pytest.raises(ExperimentContractError):
            verify_experiment_run_structure(forged)
        with pytest.raises(ExperimentContractError):
            verify_experiment_run(forged, context)


def test_forgery_6_a_provenance_only_change_is_not_a_forgery_at_all() -> None:
    """It must not move the identity, and must still verify."""
    run, context = run_of()
    moved = replace(
        run,
        provenance=ExperimentRunProvenance(
            generated_at="2030-01-01T00:00:00+00:00",
            dataset_directory="/somewhere/else",
        ),
    )
    assert moved.run_fingerprint == run.run_fingerprint
    assert verify_experiment_run_structure(moved) == run.run_fingerprint
    assert verify_experiment_run(moved, context) == run.run_fingerprint


# ====================================================================
# full verification against the artefacts
# ====================================================================


def test_full_verification_refuses_a_run_over_another_cohort() -> None:
    run, _ = run_of(ranked_records(size=10, ranked=6))
    other = experiment_context(ranked_records(size=9, ranked=5))
    with pytest.raises(ExperimentBindingError):
        verify_experiment_run(run, other)


def test_full_verification_recomputes_every_one_of_the_twelve_blocks() -> None:
    """Asserted by construction: each block equals its public recomputation."""
    run, context = run_of(ranked_records(size=10, ranked=6, out_of_target=(9,)))
    verify_experiment_run(run, context)
    for stored in run.projections:
        assert stored.result_fingerprint == (
            compute_projection(context, stored.projection).result_fingerprint
        )
    for stored in run.overlaps:
        assert stored.result_fingerprint == (
            compute_overlap(context, stored.pair).result_fingerprint
        )


def test_a_run_is_deterministic_across_two_builds() -> None:
    context = experiment_context(ranked_records(size=11, ranked=7))
    assert (
        build_experiment_run(context).run_fingerprint
        == build_experiment_run(context).run_fingerprint
    )


def test_two_different_cohorts_are_two_different_runs() -> None:
    first, _ = run_of(ranked_records(size=11, ranked=7))
    second, _ = run_of(ranked_records(size=11, ranked=6))
    assert first.run_fingerprint != second.run_fingerprint


# ====================================================================
# the D42 cross-checks
# ====================================================================


def test_the_cross_checks_hold_the_projections_against_phase_10_4() -> None:
    from evaluation.business_metrics import BusinessMetricName

    records = ranked_records(
        size=12, ranked=8, out_of_target=(11,), out_of_scope=(12,), unplaced=(10,)
    )
    run, context = run_of(records)
    business = context.business_metric_run
    cohort = business.context.frozen_cohort_binding

    frozen = run.projection(CohortProjection.FROZEN_COHORT)
    assert frozen.included_count == cohort.cohort_size

    geo = run.projection(CohortProjection.GEO_NOT_EXPLICITLY_OUT_OF_TARGET)
    verdicts = business.result_for(
        next(
            key
            for key in business.keys
            if key.metric is BusinessMetricName.TARGET_COUNTRY_MATCH_RATE
        )
    ).support.target_verdict_breakdown
    assert geo.included_count == (
        verdicts.match_count + verdicts.unknown_target_verdict_count
    )
    assert geo.excluded_count == verdicts.out_of_target_count

    data_ai = run.projection(
        CohortProjection.DATA_AI_NOT_EXPLICITLY_OUT_OF_SCOPE
    )
    breakdown = business.result_for(
        next(
            key
            for key in business.keys
            if key.metric is BusinessMetricName.DATA_AI_RATE
        )
    ).support.data_ai_breakdown
    assert data_ai.included_count == (
        cohort.cohort_size - breakdown.out_of_scope_count
    )
    assert data_ai.excluded_count == breakdown.out_of_scope_count


def test_an_unknown_target_makes_both_phases_refuse_together() -> None:
    """A legitimate Phase 10.4 N_A coinciding with the projection's own."""
    run, context = run_of(ranked_records(size=8, ranked=5), target_country=None)
    geo = run.projection(CohortProjection.GEO_NOT_EXPLICITLY_OUT_OF_TARGET)
    assert geo.status is ProjectionStatus.N_A
    verify_experiment_run(run, context)


def test_the_run_derives_no_projection_from_a_phase_10_4_aggregate() -> None:
    """D42: the projections come from the records, never from the KPIs."""
    import ast
    import pathlib

    tree = ast.parse(
        pathlib.Path("evaluation/experiments/projections.py").read_text()
    )
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any("business_metrics" in name for name in modules)


# ====================================================================
# the run's accessors
# ====================================================================


def test_a_run_answers_for_each_projection_and_pair() -> None:
    run, _ = run_of()
    for projection in PROJECTION_ORDER:
        assert run.projection(projection).projection is projection
    for pair in OVERLAP_PAIR_ORDER:
        assert run.overlap(pair).pair is pair


def test_asking_for_something_that_is_not_a_projection_is_refused() -> None:
    run, _ = run_of()
    with pytest.raises(ExperimentContractError):
        run.projection("FROZEN_COHORT")
    with pytest.raises(ExperimentContractError):
        run.overlap("GEO_AND_MATCHING")


def test_the_ranking_block_is_reachable_by_question() -> None:
    run, _ = run_of()
    assert run.ranking.status is RankingExperimentStatus.COMPUTED
    assert run.ranking.entry(MetricName.NDCG_AT_K, 10).k_requested == 10
    with pytest.raises(ExperimentBindingError, match="no entry"):
        run.ranking.entry(MetricName.NDCG_AT_K, 5)


# ====================================================================
# provenance is re-established by the structural verifier
# ====================================================================
#
# Being outside `run_fingerprint` is not a reason to be unchecked. A provenance
# built through `object.__new__` never ran `__post_init__`, so a verifier that
# only checked its *type* would accept whatever it happened to hold.


def _unchecked_provenance(**overrides):
    forged = object.__new__(ExperimentRunProvenance)
    fields = {
        "generated_at": None,
        "dataset_directory": None,
        "business_metric_run_path": None,
        "label_root": None,
        "benchmark_records_path": None,
    }
    fields.update(overrides)
    for name, value in fields.items():
        object.__setattr__(forged, name, value)
    return forged


def test_a_provenance_that_bypassed_post_init_is_refused_by_the_verifier() -> None:
    run, _ = run_of()
    forged = _unchecked_provenance(generated_at="not-a-timestamp")
    # It really did skip the constructor's guarantee.
    assert forged.generated_at == "not-a-timestamp"
    moved = reseal(replace(run, provenance=forged))
    with pytest.raises(ExperimentBindingError, match="RFC 3339"):
        verify_experiment_run_structure(moved)


def test_a_padded_path_that_bypassed_post_init_is_refused() -> None:
    run, _ = run_of()
    forged = _unchecked_provenance(label_root=" /padded/labels ")
    moved = reseal(replace(run, provenance=forged))
    with pytest.raises(ExperimentBindingError, match="padded"):
        verify_experiment_run_structure(moved)


def test_a_provenance_that_is_not_a_provenance_is_refused() -> None:
    run, _ = run_of()
    moved = reseal(replace(run, provenance={"generated_at": None}))
    with pytest.raises(ExperimentContractError, match="not an experiment run"):
        verify_experiment_run_structure(moved)


def test_a_forged_provenance_never_reaches_the_full_verifier_either() -> None:
    run, context = run_of()
    moved = reseal(
        replace(run, provenance=_unchecked_provenance(generated_at="yesterday"))
    )
    with pytest.raises(ExperimentBindingError):
        verify_experiment_run(moved, context)


def test_validating_the_provenance_did_not_move_it_into_identity() -> None:
    """It is checked *and* still outside `run_fingerprint`. Both, not either."""
    context = experiment_context()
    bare = build_experiment_run(context)
    stamped = build_experiment_run(
        context,
        provenance=ExperimentRunProvenance(
            generated_at="2027-07-07T07:07:07+00:00",
            dataset_directory="/yet/another/place",
        ),
    )
    assert bare.run_fingerprint == stamped.run_fingerprint
    assert verify_experiment_run(stamped, context) == bare.run_fingerprint
    from evaluation.experiments import canonical_experiment_run_payload

    assert "provenance" not in canonical_experiment_run_payload(stamped)


def test_two_valid_but_different_provenances_are_still_one_run(tmp_path) -> None:
    """The storage consequence of the line above, restated where it bites."""
    from evaluation.experiments import (
        ExperimentRunWriteStatus,
        write_experiment_run,
    )

    context = experiment_context()
    first = build_experiment_run(
        context,
        provenance=ExperimentRunProvenance(
            generated_at="2026-01-01T00:00:00Z", label_root="/a/labels"
        ),
    )
    second = build_experiment_run(
        context,
        provenance=ExperimentRunProvenance(
            generated_at="2029-09-09T09:09:09-03:00", label_root="/b/labels"
        ),
    )
    assert first.run_fingerprint == second.run_fingerprint
    assert (
        write_experiment_run(first, context, root=tmp_path).status
        is ExperimentRunWriteStatus.CREATED
    )
    again = write_experiment_run(second, context, root=tmp_path)
    assert again.status is ExperimentRunWriteStatus.UNCHANGED
    assert again.run.provenance.generated_at == "2026-01-01T00:00:00Z"
