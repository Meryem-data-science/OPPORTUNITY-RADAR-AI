"""The five projections of a frozen cohort, semantic by semantic.

These tests are about `projections.py`: what each projection includes, what it
excludes, where it refuses, and what its identity covers. The `UNKNOWN != FALSE`
rule is most of it.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from evaluation.dataset import EvaluationDatasetError
from evaluation.business_metrics import (
    BusinessMetricName,
    BusinessMetricStatus,
    BusinessMetricUnavailableReason,
)
from evaluation.experiments import (
    PROJECTION_ORDER,
    CohortProjection,
    ExperimentBindingError,
    ExperimentContractError,
    ProjectionStatus,
    ProjectionUnavailableReason,
    compute_projection,
    projection_result_fingerprint,
    verify_projection_result_fingerprint,
)

from tests.unit.experiment_fixtures import (
    OTHER_COUNTRY,
    TARGET_COUNTRY,
    business_run,
    dataset_of,
    experiment_context,
    manifest_payload,
    matching,
    qualification,
    ranked_records,
    record,
    recommendation,
    segment,
    source,
)


def project(which: CohortProjection, records=None, **kwargs):
    return compute_projection(experiment_context(records, **kwargs), which)


def included(which: CohortProjection, records=None, **kwargs) -> tuple[int, ...]:
    return project(which, records, **kwargs).included_ids


GEO = CohortProjection.GEO_NOT_EXPLICITLY_OUT_OF_TARGET
DATA_AI = CohortProjection.DATA_AI_NOT_EXPLICITLY_OUT_OF_SCOPE
MATCHING = CohortProjection.MATCHING_OBSERVED
RECOMMENDATION = CohortProjection.RECOMMENDATION_OBSERVED
COHORT = CohortProjection.FROZEN_COHORT


def cohort_of(*records):
    """A snapshot with no recommendation anywhere, for the stage projections.

    Every test below that is about G, A or M uses this: those three say nothing
    about the ranking, and building a coherent Phase 10.3 run for each of them
    would test the ranking adapter instead of the projection.
    """
    return tuple(records)


# ====================================================================
# the authority itself
# ====================================================================


def test_compute_projection_is_the_only_public_way_to_get_a_result() -> None:
    """No `make_projection_result`, and no sealer taking a membership."""
    import evaluation.experiments.projections as module

    assert module.__all__ == ["compute_projection"]
    for forbidden in (
        "make_projection_result",
        "seal_projection",
        "projection_result",
    ):
        assert not hasattr(module, forbidden)


def test_the_registry_is_closed_and_exhaustive() -> None:
    from evaluation.experiments.projections import _PROJECTION_RESOLVERS

    assert set(_PROJECTION_RESOLVERS) == set(CohortProjection)
    assert tuple(_PROJECTION_RESOLVERS) == PROJECTION_ORDER


def test_something_that_is_not_a_projection_is_refused() -> None:
    with pytest.raises(ExperimentContractError, match="not a cohort projection"):
        compute_projection(experiment_context(), "FROZEN_COHORT")


def test_the_context_is_reverified_on_every_call() -> None:
    """A forged Phase 10.4 run cannot be walked past by holding a context."""
    from evaluation.experiments import ExperimentRunContext

    records = ranked_records(size=6, ranked=0)
    run = business_run(records)
    forged = replace(
        run, results=(replace(run.results[0], value=0.999), *run.results[1:])
    )
    with pytest.raises(Exception):
        compute_projection(
            ExperimentRunContext(
                frozen_dataset=dataset_of(records),
                manifest=manifest_payload(records),
                business_metric_run=forged,
            ),
            COHORT,
        )


# ====================================================================
# 1. FROZEN_COHORT
# ====================================================================


def test_the_frozen_cohort_includes_every_record() -> None:
    records = ranked_records(size=12, ranked=8)
    result = project(COHORT, records)
    assert result.status is ProjectionStatus.COMPUTED
    assert result.included_count == 12
    assert result.excluded_count == 0
    assert result.cohort_share == 1.0
    assert result.included_ids == tuple(range(1, 13))


def test_the_cohort_membership_is_serialized_ascending_not_ranked() -> None:
    """`opportunity_id ASC` is a serialization; it is never a ranking."""
    records = cohort_of(
        record(3, geography_segments=(segment("RESOLVED"),), recommendation=recommendation(1)),
        record(7, geography_segments=(segment("RESOLVED"),), recommendation=recommendation(2)),
        record(9, geography_segments=(segment("RESOLVED"),), recommendation=recommendation(3)),
    )
    result = project(COHORT, records)
    assert result.included_ids == (3, 7, 9)


# ====================================================================
# 2. GEO_NOT_EXPLICITLY_OUT_OF_TARGET — the UNKNOWN != FALSE rule
# ====================================================================


def test_a_match_is_included() -> None:
    records = cohort_of(record(1, geography_segments=(segment("RESOLVED"),)))
    assert included(GEO, records, with_ranking=False) == (1,)


def test_an_unknown_location_is_included() -> None:
    """Nobody established it was elsewhere."""
    records = cohort_of(record(1, geography_segments=(segment("UNKNOWN"),)))
    assert included(GEO, records, with_ranking=False) == (1,)


def test_an_ambiguous_location_is_included_and_is_not_out_of_target() -> None:
    records = cohort_of(record(1, geography_segments=(segment("AMBIGUOUS"),)))
    assert included(GEO, records, with_ranking=False) == (1,)


def test_a_record_with_no_geography_segment_is_included() -> None:
    records = cohort_of(record(1, geography_segments=()))
    assert included(GEO, records, with_ranking=False) == (1,)


def test_only_an_explicit_other_country_is_excluded() -> None:
    records = cohort_of(
        record(1, geography_segments=(segment("RESOLVED"),)),
        record(2, geography_segments=(segment("RESOLVED", country_code=OTHER_COUNTRY),)),
        record(3, geography_segments=(segment("UNKNOWN"),)),
        record(4, geography_segments=(segment("AMBIGUOUS"),)),
        record(5, geography_segments=()),
    )
    result = project(GEO, records, with_ranking=False)
    assert result.included_ids == (1, 3, 4, 5)
    assert result.excluded_count == 1
    assert result.cohort_share == 4 / 5


def test_no_target_country_is_na_and_never_a_fallback() -> None:
    records = cohort_of(record(1, geography_segments=(segment("RESOLVED"),)))
    result = project(GEO, records, with_ranking=False, target_country=None)
    assert result.status is ProjectionStatus.N_A
    assert result.unavailable_reason is (
        ProjectionUnavailableReason.TARGET_COUNTRY_UNAVAILABLE
    )
    assert result.included_opportunity_ids is None
    assert result.included_count is None
    assert result.cohort_share is None
    # The cohort size is still a fact about the snapshot.
    assert result.cohort_size == 1


def test_no_country_is_hardcoded_in_the_geography_projection() -> None:
    """The same cohort against two bound targets."""
    records = cohort_of(
        record(1, geography_segments=(segment("RESOLVED", country_code="FR"),))
    )
    assert included(GEO, records, with_ranking=False, target_country="FR") == (1,)
    assert (
        project(GEO, records, with_ranking=False, target_country="MA").included_ids
        == ()
    )


def test_the_projection_never_reads_the_location_text() -> None:
    records = cohort_of(
        record(
            1,
            location="Casablanca, Morocco",
            country=TARGET_COUNTRY,
            geography_segments=(segment("RESOLVED", country_code=OTHER_COUNTRY),),
        )
    )
    # The frozen segment places it elsewhere; the text says otherwise and is
    # not consulted.
    assert included(GEO, records, with_ranking=False) == ()


def test_an_incoherent_segment_is_a_hard_error_not_an_exclusion() -> None:
    """Refused, and never quietly excluded from the projection.

    The refusal arrives from the **first** boundary that reads the record, which
    for a geography segment is the bound Phase 10.4 run being re-verified over
    these very records — so the error type is that package's. Both are
    subclasses of Phase 10.1's error, and what matters here is that no
    projection is ever produced over evidence that contradicts itself. That the
    Phase 10.5 resolver refuses it in *its* own vocabulary is established in
    `test_frozen_facts.py`, where the primitive is called directly.
    """
    records = cohort_of(
        record(
            1,
            geography_segments=({**segment("RESOLVED"), "country_code": None},),
        )
    )
    with pytest.raises(EvaluationDatasetError):
        project(GEO, records, with_ranking=False)


# ====================================================================
# 3. DATA_AI_NOT_EXPLICITLY_OUT_OF_SCOPE
# ====================================================================


@pytest.mark.parametrize(
    "value", ["CORE_TARGET", "ADJACENT_TARGET", "UNCERTAIN"]
)
def test_every_non_negative_classification_is_included(value) -> None:
    records = cohort_of(record(1, qualification=qualification(value=value)))
    assert included(DATA_AI, records, with_ranking=False) == (1,)


def test_an_unclassified_posting_is_included_and_never_out_of_scope() -> None:
    """`qualification is None` means the classifier never ruled it out."""
    records = cohort_of(record(1, qualification=None))
    result = project(DATA_AI, records, with_ranking=False)
    assert result.included_ids == (1,)
    assert result.excluded_count == 0


def test_only_out_of_scope_is_excluded() -> None:
    records = cohort_of(
        record(1, qualification=qualification()),
        record(2, qualification=qualification(value="OUT_OF_SCOPE")),
        record(3, qualification=qualification(value="UNCERTAIN")),
        record(4, qualification=None),
        record(5, qualification=qualification(value="ADJACENT_TARGET")),
    )
    result = project(DATA_AI, records, with_ranking=False)
    assert result.included_ids == (1, 3, 4, 5)
    assert result.excluded_count == 1


def test_the_data_ai_projection_has_no_unavailable_path() -> None:
    """Answerable from the frozen records alone, always."""
    records = cohort_of(record(1, qualification=None))
    for target in (TARGET_COUNTRY, None):
        result = project(
            DATA_AI, records, with_ranking=False, target_country=target
        )
        assert result.status is ProjectionStatus.COMPUTED


def test_an_unknown_qualification_value_is_a_hard_error() -> None:
    """Never folded into `UNCLASSIFIED`, and never a silent exclusion.

    As above, the first boundary to read the qualification is the bound Phase
    10.4 run, so the refusal carries that package's type. The message is the
    shared primitive's either way, which is the point of `frozen_facts`.
    """
    records = cohort_of(
        record(1, qualification=qualification(value="PROBABLY_FINE"))
    )
    with pytest.raises(EvaluationDatasetError, match="does not define"):
        project(DATA_AI, records, with_ranking=False)


def test_the_projection_never_guesses_from_the_title_or_description() -> None:
    records = cohort_of(
        record(
            1,
            canonical_title="Machine Learning Engineer",
            description="deep learning, data science, AI",
            qualification=qualification(value="OUT_OF_SCOPE"),
        )
    )
    assert included(DATA_AI, records, with_ranking=False) == ()


# ====================================================================
# 4. MATCHING_OBSERVED — presence only
# ====================================================================


def test_matching_presence_is_the_whole_rule() -> None:
    records = cohort_of(
        record(1, matching=matching()),
        record(2, matching=None),
        record(3, matching=matching()),
    )
    result = project(MATCHING, records, with_ranking=False)
    assert result.included_ids == (1, 3)
    assert result.excluded_count == 1


@pytest.mark.parametrize("lane", ["STRONG", "WEAK", "UNCERTAIN", "EXCLUDED"])
def test_no_lane_is_filtered_out(lane) -> None:
    records = cohort_of(record(1, matching=matching(lane=lane)))
    assert included(MATCHING, records, with_ranking=False) == (1,)


def test_a_matching_that_measured_nothing_is_still_observed() -> None:
    """Phase 4's `match_quality is None` with `evidence_coverage == 0.0`."""
    records = cohort_of(
        record(1, matching=matching(match_quality=None, evidence_coverage=0.0))
    )
    assert included(MATCHING, records, with_ranking=False) == (1,)


def test_a_low_match_quality_is_still_observed() -> None:
    records = cohort_of(record(1, matching=matching(match_quality=0.0)))
    assert included(MATCHING, records, with_ranking=False) == (1,)


def test_a_matching_block_that_is_not_an_object_is_a_hard_error() -> None:
    records = cohort_of(record(1, matching="STRONG"))
    with pytest.raises(ExperimentBindingError, match="optional block"):
        project(MATCHING, records, with_ranking=False)


# ====================================================================
# 5. RECOMMENDATION_OBSERVED — presence only
# ====================================================================


def test_recommendation_presence_is_the_whole_rule() -> None:
    records = ranked_records(size=10, ranked=6)
    result = project(RECOMMENDATION, records)
    assert result.included_ids == (1, 2, 3, 4, 5, 6)
    assert result.excluded_count == 4
    assert result.cohort_share == 0.6


@pytest.mark.parametrize(
    "disposition", ["RECOMMENDED", "UNCERTAIN", "NOT_RECOMMENDED"]
)
def test_no_disposition_is_filtered_out(disposition) -> None:
    records = cohort_of(
        record(
            1,
            geography_segments=(segment("RESOLVED"),),
            recommendation=recommendation(1, disposition=disposition),
        )
    )
    assert included(RECOMMENDATION, records) == (1,)


def test_no_score_threshold_is_applied() -> None:
    records = cohort_of(
        record(
            1,
            geography_segments=(segment("RESOLVED"),),
            recommendation=recommendation(1, recommendation_score=0.0),
        ),
        record(
            2,
            geography_segments=(segment("RESOLVED"),),
            recommendation=recommendation(2, recommendation_score=None),
        ),
    )
    assert included(RECOMMENDATION, records) == (1, 2)


def test_a_snapshot_with_no_recommendation_is_computed_and_empty() -> None:
    """An empty *available* projection is a fact, not an absence."""
    records = ranked_records(size=5, ranked=0)
    result = project(RECOMMENDATION, records, with_ranking=False)
    assert result.status is ProjectionStatus.COMPUTED
    assert result.included_ids == ()
    assert result.included_count == 0
    assert result.excluded_count == 5
    assert result.cohort_share == 0.0


def test_a_recommendation_block_that_is_not_an_object_is_a_hard_error() -> None:
    records = cohort_of(
        record(1, geography_segments=(segment("RESOLVED"),), recommendation=7)
    )
    with pytest.raises(ExperimentBindingError):
        project(RECOMMENDATION, records, with_ranking=False)


# ====================================================================
# independence — none of the five composes with another
# ====================================================================


def test_the_projections_are_independent_and_not_a_funnel() -> None:
    """M is every matched posting, not "what survived G"."""
    records = cohort_of(
        record(
            1,
            geography_segments=(segment("RESOLVED", country_code=OTHER_COUNTRY),),
            qualification=qualification(value="OUT_OF_SCOPE"),
            matching=matching(),
            recommendation=recommendation(1),
        ),
        record(
            2,
            geography_segments=(segment("RESOLVED"),),
            qualification=qualification(),
            matching=None,
            recommendation=None,
        ),
    )
    # Posting 1 is out of target *and* out of scope, yet it is observed by both
    # M and R: a funnel reading would have removed it.
    assert included(MATCHING, records) == (1,)
    assert included(RECOMMENDATION, records) == (1,)
    assert included(GEO, records) == (2,)
    assert included(DATA_AI, records) == (2,)


# ====================================================================
# identity
# ====================================================================


def test_every_projection_result_survives_its_own_verifier() -> None:
    context = experiment_context()
    for which in PROJECTION_ORDER:
        result = compute_projection(context, which)
        assert verify_projection_result_fingerprint(result) == (
            result.result_fingerprint
        )


def test_the_result_is_deterministic_across_two_computations() -> None:
    context = experiment_context()
    first = compute_projection(context, RECOMMENDATION)
    second = compute_projection(context, RECOMMENDATION)
    assert first.result_fingerprint == second.result_fingerprint


def test_a_different_membership_of_the_same_size_is_a_different_result() -> None:
    left = project(
        RECOMMENDATION,
        cohort_of(
            record(1, geography_segments=(segment("RESOLVED"),), recommendation=recommendation(1)),
            record(2, geography_segments=(segment("RESOLVED"),), recommendation=None),
        ),
    )
    right = project(
        RECOMMENDATION,
        cohort_of(
            record(1, geography_segments=(segment("RESOLVED"),), recommendation=None),
            record(2, geography_segments=(segment("RESOLVED"),), recommendation=recommendation(1)),
        ),
    )
    assert left.included_count == right.included_count == 1
    assert left.cohort_share == right.cohort_share
    assert left.result_fingerprint != right.result_fingerprint


def test_an_edited_count_no_longer_matches_the_declared_digest() -> None:
    result = project(RECOMMENDATION, ranked_records(size=10, ranked=6))
    forged = replace(result, included_count=7, excluded_count=3)
    with pytest.raises(ExperimentBindingError):
        verify_projection_result_fingerprint(forged)


# ====================================================================
# agreement with Phase 10.4 — two derivations of one cohort
# ====================================================================


def test_the_geography_projection_agrees_with_the_bound_target_verdicts() -> None:
    records = ranked_records(size=10, ranked=6, out_of_target=(2, 5), unplaced=(7,))
    payload = manifest_payload(records)
    run = business_run(records, manifest=payload)
    context = experiment_context(records)
    geo = compute_projection(context, GEO)
    key = next(
        item
        for item in run.keys
        if item.metric is BusinessMetricName.TARGET_COUNTRY_MATCH_RATE
    )
    verdicts = run.result_for(key).support.target_verdict_breakdown
    assert geo.included_count == (
        verdicts.match_count + verdicts.unknown_target_verdict_count
    )
    assert geo.excluded_count == verdicts.out_of_target_count


def test_the_data_ai_projection_agrees_with_the_bound_breakdown() -> None:
    records = ranked_records(
        size=10, ranked=6, out_of_scope=(3, 4), unclassified=(8,)
    )
    run = business_run(records)
    context = experiment_context(records)
    data_ai = compute_projection(context, DATA_AI)
    key = next(
        item
        for item in run.keys
        if item.metric is BusinessMetricName.DATA_AI_RATE
    )
    breakdown = run.result_for(key).support.data_ai_breakdown
    assert data_ai.excluded_count == breakdown.out_of_scope_count
    assert data_ai.included_count == 10 - breakdown.out_of_scope_count
    # And the unclassified posting is *included*.
    assert 8 in data_ai.included_ids


def test_an_unknown_target_in_phase_10_4_coincides_with_the_na_projection() -> None:
    records = ranked_records(size=6, ranked=4)
    run = business_run(records, target_country=None)
    key = next(
        item
        for item in run.keys
        if item.metric is BusinessMetricName.TARGET_COUNTRY_MATCH_RATE
    )
    result = run.result_for(key)
    assert result.status is BusinessMetricStatus.N_A
    assert result.reason is BusinessMetricUnavailableReason.TARGET_COUNTRY_UNKNOWN
    projection = project(GEO, records, target_country=None)
    assert projection.status is ProjectionStatus.N_A
