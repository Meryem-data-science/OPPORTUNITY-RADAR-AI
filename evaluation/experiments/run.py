"""One experiment run: twelve blocks, all of them, or no run at all.

Five projections, six overlaps, one ranking experiment. `build_experiment_run`
computes every one of them or raises, and the two verifiers say different things
about what a run establishes.

## All twelve or nothing

A hard error anywhere leaves **no run**. That is why the arithmetic is done
before the assembly rather than block by block into a growing document: there is
no state in which a partial run exists to be stored, reported on, or mistaken
for a complete one. There is no partial run, no stored failed run, no selected
subset and no optional projection.

`N_A` is a **present** block and counts towards the twelve. A run whose
geography projection could not be computed is a complete run that says so.

## The two verifications are different claims

`verify_experiment_run_structure(run)` — what a run can establish **about
itself**, with no artefacts to hand. The versions, the provenance's shape, the
context binding and its recomputed digest, exactly five projections and six
overlaps in canonical order, exactly one ranking block, every child digest
recomputed, one dataset and one profile throughout, the overlap partitions
re-derived from the run's **own stored projections**, `N_A` propagation, the
coherence between the ranking block and `RECOMMENDATION_OBSERVED`, and the run's
own digest over all of it.

It deliberately proves **no membership**. A `GEO_NOT_EXPLICITLY_OUT_OF_TARGET`
that includes a posting the resolver placed in another country is internally
impeccable — its counts add up, its share matches, its digest is valid — and
only the records show the difference.

`verify_experiment_run(run, context)` — the structural verification first, then
everything that needs the artefacts: the context re-established (which fully
re-verifies the bound Phase 10.4 run over these very records), the binding
rebuilt and compared, **all twelve blocks recomputed** through the same public
authorities any other caller uses, D42's cross-checks against Phase 10.4's own
counts, and exact semantic equality between stored and recomputed.

A forged value fails there even though its own digest is perfect, because the
recomputation produces a different payload and therefore a different digest.

## No weakening switches

There is no `strict=False`, no `repair=True`, no `allow_partial`, no
`skip_records` and no `tolerate_*`. A verification with an escape hatch is a
verification whose result nobody can interpret.

## The D42 cross-checks

Phase 10.5 recomputes none of Phase 10.4's KPIs and derives no projection from
their aggregates — the projections come from the records through
`evaluation.frozen_facts`. What the full verifier does is hold the two
**independent** derivations against each other:

    |C|             == BusinessMetricRun frozen cohort size
    |G|             == match_count + unknown_target_verdict_count
    excluded(G)     == out_of_target_count
    |A|             == cohort_size - out_of_scope_count
    excluded(A)     == out_of_scope_count

The geography identities apply only where Phase 10.4 actually computed the
target verdicts — a legitimate `N_A / TARGET_COUNTRY_UNKNOWN` there is
acceptable, and the projection is then `N_A` too, which is itself the agreement.
Any *mismatch* is a hard error: it means two readings of one frozen cohort
disagree, and that is a contradiction rather than a question nobody can answer.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from evaluation.business_metrics import (
    BusinessMetricName,
    BusinessMetricRun,
    BusinessMetricStatus,
    BusinessMetricUnavailableReason,
)

from .context import (
    ExperimentRunContext,
    _VerifiedExperimentContext,
    _verified,
    experiment_run_context_binding,
)
from .fingerprint import (
    EXPERIMENT_PLACEHOLDER_FINGERPRINT,
    experiment_run_fingerprint,
    verify_experiment_run_context_fingerprint,
    verify_experiment_run_fingerprint,
    verify_overlap_against_projections,
    verify_overlap_result_fingerprint,
    verify_projection_result_fingerprint,
    verify_ranking_experiment_result_fingerprint,
)
from .overlaps import compute_overlap
from .projections import compute_projection
from .ranking import compute_ranking_experiment
from .schema import (
    EXPERIMENT_CONTRACT_VERSION,
    EXPERIMENT_RUN_SCHEMA_VERSION,
    OVERLAP_PAIR_ORDER,
    PROJECTION_ORDER,
    CohortProjection,
    ExperimentBindingError,
    ExperimentContractError,
    ExperimentRun,
    ExperimentRunProvenance,
    ProjectionResult,
    RankingExperimentResult,
    RankingExperimentStatus,
    RankingExperimentUnavailableReason,
    overlap_pair_projections,
    overlap_result_payload,
    projection_result_payload,
    ranking_experiment_result_payload,
    require_supported_experiment_contract_version,
    require_supported_experiment_run_schema_version,
    validate_experiment_run_structure,
)

__all__ = [
    "build_experiment_run",
    "verify_experiment_run",
    "verify_experiment_run_structure",
]


# --------------------------------------------------------------------------
# building a run
# --------------------------------------------------------------------------


def build_experiment_run(
    context: ExperimentRunContext,
    *,
    provenance: ExperimentRunProvenance | None = None,
) -> ExperimentRun:
    """Compute all twelve blocks over one verified context, or produce nothing.

    In order, and each step refuses rather than reports:

    1. the context is verified — the cohort, the bound Phase 10.4 run recomputed
       in full over these records, D42's five bindings, the ranking inputs held
       against the presence of an observed recommendation;
    2. the persistent context binding is derived from that verified bundle and
       its own digest sealed;
    3. the five projections, the six overlaps and the ranking block are computed
       through the **public** authorities — `compute_projection`,
       `compute_overlap`, `compute_ranking_experiment`. The private resolvers
       are never called from here: a run must be reachable by exactly the path
       any other caller has, or it would be testing different functions than the
       ones it ships;
    4. every block's digest is recomputed and the overlaps are re-derived from
       the projections, before the parent identity exists;
    5. the D42 cross-checks hold the projections against Phase 10.4's own counts;
    6. only then is the run's fingerprint computed and the artefact assembled.
    """
    verified = _verified(context)
    binding = experiment_run_context_binding(verified)
    contract_version = require_supported_experiment_contract_version(
        EXPERIMENT_CONTRACT_VERSION
    )

    projections = tuple(
        compute_projection(context, projection) for projection in PROJECTION_ORDER
    )
    by_projection = {result.projection: result for result in projections}
    overlaps = tuple(compute_overlap(context, pair) for pair in OVERLAP_PAIR_ORDER)
    ranking = compute_ranking_experiment(context)

    # Re-established through the same verifiers a stored run faces, so a run
    # holds nothing that would fail verification the moment it was read back.
    for result in projections:
        verify_projection_result_fingerprint(result)
    for result in overlaps:
        left, right = overlap_pair_projections(result.pair)
        verify_overlap_result_fingerprint(result)
        verify_overlap_against_projections(
            result, by_projection[left], by_projection[right], verified.cohort_ids
        )
    verify_ranking_experiment_result_fingerprint(ranking)
    _require_ranking_coherence(
        ranking, by_projection[CohortProjection.RECOMMENDATION_OBSERVED]
    )
    _require_business_metric_agreement(verified, by_projection)

    draft = ExperimentRun(
        run_schema_version=require_supported_experiment_run_schema_version(
            EXPERIMENT_RUN_SCHEMA_VERSION
        ),
        contract_version=contract_version,
        context=binding,
        projections=projections,
        overlaps=overlaps,
        ranking=ranking,
        run_fingerprint=EXPERIMENT_PLACEHOLDER_FINGERPRINT,
        provenance=(
            ExperimentRunProvenance() if provenance is None else provenance
        ),
    )
    validate_experiment_run_structure(draft)
    return replace(draft, run_fingerprint=experiment_run_fingerprint(draft))


# --------------------------------------------------------------------------
# coherence rules shared by the builder and the verifiers
# --------------------------------------------------------------------------


def _require_ranking_coherence(
    ranking: RankingExperimentResult, recommendation: ProjectionResult
) -> None:
    """The ranking block and `RECOMMENDATION_OBSERVED` say the same thing.

    Establishable without the records, which is why it belongs to the structural
    verification: both objects are in the run, and their agreement is a property
    of the run itself.

    Three rules, in both directions:

    * the block names *this* projection, by digest;
    * `N_A / NO_OBSERVED_RECOMMENDATION_RANKING` requires an empty observed
       projection — a refusal over a non-empty one would be a ranking that
       exists and was not evaluated;
    * a `COMPUTED` block requires a non-empty one — four metric results over a
       ranking of nothing would be numbers about an ordering that does not
       exist.
    """
    if ranking.recommendation_projection_result_fingerprint != (
        recommendation.result_fingerprint
    ):
        raise ExperimentBindingError(
            f"the ranking block rests on projection "
            f"{ranking.recommendation_projection_result_fingerprint} and this "
            f"run's {recommendation.projection} states "
            f"{recommendation.result_fingerprint}"
        )
    if not recommendation.computed:  # pragma: no cover - presence has no N_A
        raise ExperimentContractError(
            f"{recommendation.projection} is {recommendation.status}; a presence "
            "projection is always computable from the frozen records"
        )
    observed = recommendation.included_count
    if ranking.status is RankingExperimentStatus.N_A:
        if observed != 0:
            raise ExperimentBindingError(
                f"the ranking block is N_A / "
                f"{RankingExperimentUnavailableReason.NO_OBSERVED_RECOMMENDATION_RANKING}"
                f" and {recommendation.projection} holds {observed} posting(s); "
                "that reason names a snapshot with no observed ranking, and this "
                "snapshot has one"
            )
        return
    if observed == 0:
        raise ExperimentBindingError(
            f"the ranking block is COMPUTED and {recommendation.projection} is "
            "empty; there is no observed ordering for those four numbers to be "
            "about"
        )


def _require_business_metric_agreement(
    verified: _VerifiedExperimentContext,
    by_projection: dict[CohortProjection, ProjectionResult],
) -> None:
    """D42's cross-checks: two independent readings of one cohort must agree.

    Phase 10.5 derived these projections from the records through
    `evaluation.frozen_facts`; Phase 10.4 derived its counts from the same
    records through the same primitives but into *its* arithmetic — five counts
    for the Data/AI breakdown, three for the target verdicts. Neither is read
    off the other, which is what makes this a check rather than a tautology.

    The Data/AI identities always apply: `DATA_AI_RATE` is computable from the
    frozen records alone, so a cohort always has an `out_of_scope_count`.

    The geography identities apply **only where Phase 10.4 computed the target
    verdicts**. A legitimate `N_A / TARGET_COUNTRY_UNKNOWN` or
    `TARGET_SCOPE_BINDING_MISSING` there is acceptable, and the projection is
    then `N_A / TARGET_COUNTRY_UNAVAILABLE` too — which is itself the agreement,
    and is checked as such in both directions.

    Any mismatch is a **hard error**.
    """
    run = verified.business_metric_run
    cohort = run.context.frozen_cohort_binding
    frozen = by_projection[CohortProjection.FROZEN_COHORT]
    if frozen.included_count != cohort.cohort_size:
        raise ExperimentBindingError(
            f"{frozen.projection} holds {frozen.included_count} posting(s) and "
            f"the bound business metric run states a frozen cohort of "
            f"{cohort.cohort_size}"
        )

    data_ai = by_projection[CohortProjection.DATA_AI_NOT_EXPLICITLY_OUT_OF_SCOPE]
    breakdown = _support_block(run, BusinessMetricName.DATA_AI_RATE, "data_ai_breakdown")
    if breakdown is None:
        raise ExperimentBindingError(
            f"the bound business metric run states no Data/AI breakdown for "
            f"{BusinessMetricName.DATA_AI_RATE}; that metric is computable from "
            "the frozen records alone, so its absence means the run and this "
            "cohort are not describing the same snapshot"
        )
    out_of_scope = breakdown.out_of_scope_count
    if not data_ai.computed:  # pragma: no cover - this projection has no N_A
        raise ExperimentContractError(
            f"{data_ai.projection} is {data_ai.status}; it is computable from "
            "the frozen records alone"
        )
    if data_ai.included_count != cohort.cohort_size - out_of_scope:
        raise ExperimentBindingError(
            f"{data_ai.projection} includes {data_ai.included_count} and the "
            f"bound business metric run counts {out_of_scope} OUT_OF_SCOPE in a "
            f"cohort of {cohort.cohort_size}, implying "
            f"{cohort.cohort_size - out_of_scope}"
        )
    if data_ai.excluded_count != out_of_scope:
        raise ExperimentBindingError(
            f"{data_ai.projection} excludes {data_ai.excluded_count} and the "
            f"bound business metric run counts {out_of_scope} OUT_OF_SCOPE; the "
            "excluded set of this projection is exactly the postings the "
            "classifier ruled out"
        )

    geo = by_projection[CohortProjection.GEO_NOT_EXPLICITLY_OUT_OF_TARGET]
    verdicts = _support_block(
        run, BusinessMetricName.TARGET_COUNTRY_MATCH_RATE, "target_verdict_breakdown"
    )
    if verdicts is None:
        # Phase 10.4 could not compute the target verdicts. That is legitimate —
        # an UNKNOWN target is coherent evidence there — and it must coincide
        # with this projection's own refusal. Checked in both directions: an
        # available projection beside an unavailable rate would mean Phase 10.5
        # found a target Phase 10.4 did not.
        if geo.computed:
            raise ExperimentBindingError(
                f"{geo.projection} is COMPUTED and the bound business metric run "
                f"states no target verdict breakdown for "
                f"{BusinessMetricName.TARGET_COUNTRY_MATCH_RATE}; both read the "
                "target from the same binding, so one cannot have found a "
                "country the other did not"
            )
        return
    if not geo.computed:
        raise ExperimentBindingError(
            f"{geo.projection} is {geo.status} / {geo.unavailable_reason} and the "
            f"bound business metric run computed its target verdicts; both read "
            "the target from the same binding"
        )
    expected_included = (
        verdicts.match_count + verdicts.unknown_target_verdict_count
    )
    if geo.included_count != expected_included:
        raise ExperimentBindingError(
            f"{geo.projection} includes {geo.included_count} and the bound "
            f"business metric run counts {verdicts.match_count} MATCH plus "
            f"{verdicts.unknown_target_verdict_count} UNKNOWN, implying "
            f"{expected_included}"
        )
    if geo.excluded_count != verdicts.out_of_target_count:
        raise ExperimentBindingError(
            f"{geo.projection} excludes {geo.excluded_count} and the bound "
            f"business metric run counts {verdicts.out_of_target_count} "
            "OUT_OF_TARGET; the excluded set of this projection is exactly the "
            "postings the resolver placed elsewhere"
        )


def _support_block(
    run: BusinessMetricRun, metric: BusinessMetricName, attribute: str
) -> Any:
    """One typed support block of one Phase 10.4 metric, or `None`.

    `None` covers both "the metric is `N_A`" and "it states no such block",
    which the caller distinguishes per metric: a `DATA_AI_RATE` without its
    breakdown is a contradiction, while a `TARGET_COUNTRY_MATCH_RATE` without
    its verdicts is the legitimate UNKNOWN-target case.

    An `N_A` whose reason is not one this contract expects is a hard error
    rather than a silently accepted absence — a `DATA_AI_RATE` refused for a
    reason about a URL audit would mean the bound run is not the run this check
    assumes.
    """
    result = run.result_for(
        next(
            key
            for key in run.keys
            if key.metric is metric and key.dimension_value is None
        )
    )
    if result.status is BusinessMetricStatus.N_A:
        permitted = {
            BusinessMetricUnavailableReason.TARGET_COUNTRY_UNKNOWN,
            BusinessMetricUnavailableReason.TARGET_SCOPE_BINDING_MISSING,
        }
        if result.reason not in permitted:
            raise ExperimentBindingError(
                f"the bound business metric run states {metric} is N_A / "
                f"{result.reason}; the only refusals this cross-check can "
                f"interpret are {sorted(str(item) for item in permitted)}"
            )
        return None
    return getattr(result.support, attribute, None)


# --------------------------------------------------------------------------
# structural verification — everything establishable without the artefacts
# --------------------------------------------------------------------------


def verify_experiment_run_structure(run: ExperimentRun) -> str:
    """Re-establish a run from itself, and return its recomputed fingerprint.

    What it establishes: the versions, the provenance's shape, the context
    binding and its recomputed digest, exactly five projections and six overlaps
    in canonical order, exactly one ranking block, every child digest
    recomputed, one dataset and one profile across all twelve blocks, the
    overlap partitions **re-derived from this run's own stored projections**,
    `N_A` propagation from an unavailable projection into its overlaps, and the
    coherence between the ranking block and `RECOMMENDATION_OBSERVED`.

    What it deliberately does **not** claim, and the name says so: it proves no
    membership. A projection whose included set is not the set the frozen
    records imply passes here — its counts add up, its share matches, its digest
    is valid — and only `verify_experiment_run` has the records that show the
    difference.
    """
    validate_experiment_run_structure(run)
    verify_experiment_run_context_fingerprint(run.context)

    by_projection: dict[CohortProjection, ProjectionResult] = {}
    for result in run.projections:
        verify_projection_result_fingerprint(result)
        by_projection[result.projection] = result

    cohort = by_projection[CohortProjection.FROZEN_COHORT]
    if not cohort.computed:  # pragma: no cover - refused by the validator
        raise ExperimentContractError(
            "the reference projection of this run is not COMPUTED"
        )
    cohort_ids = cohort.included_ids

    for result in run.overlaps:
        verify_overlap_result_fingerprint(result)
        left, right = overlap_pair_projections(result.pair)
        # The partitions re-derived from the run's own projections. This is what
        # catches an overlap that is internally impeccable — disjoint, covering,
        # rates matching — while describing memberships the run does not state.
        verify_overlap_against_projections(
            result, by_projection[left], by_projection[right], cohort_ids
        )

    verify_ranking_experiment_result_fingerprint(run.ranking)
    _require_ranking_coherence(
        run.ranking, by_projection[CohortProjection.RECOMMENDATION_OBSERVED]
    )
    return verify_experiment_run_fingerprint(run)


# --------------------------------------------------------------------------
# full verification — the run against the artefacts it claims to be about
# --------------------------------------------------------------------------


def verify_experiment_run(
    run: ExperimentRun, context: ExperimentRunContext
) -> str:
    """Recompute a whole run from its artefacts and require it to be what it says.

    **The only function here that certifies the memberships.** The structural
    verifier can establish that a projection is self-consistent and can never
    establish that its included set is the set the frozen records imply, because
    a SHA-256 identifies content it does not hold. This does:

    1. the structure and every digest, through the structural verifier;
    2. the context re-established — which fully re-verifies the bound Phase 10.4
       run over these very records, recomputing all of its metrics, and holds
       D42's five bindings;
    3. the context binding **rebuilt** from those verified artefacts and
       required to be the one the run stores, by digest and field by field;
    4. **all twelve blocks recomputed** through the public authorities and
       required to equal the stored ones semantically — status, reason, exact
       membership, every partition, every rate, all four Phase 10.3 metric
       payloads, and every nested fingerprint;
    5. D42's cross-checks against Phase 10.4's own counts;
    6. the run fingerprint recomputed over the blocks that survived.

    A forged membership fails at (4) even though its own digest is perfectly
    valid, because recomputation produces a different payload. Altered records
    or an altered manifest fail at (2): they derive a different cohort binding,
    and Phase 10.4's own verification refuses them first.

    There is no argument that weakens any of this.
    """
    structural = verify_experiment_run_structure(run)
    verified = _verified(context)

    rebuilt = experiment_run_context_binding(verified)
    if rebuilt.context_fingerprint != run.context.context_fingerprint:
        raise ExperimentBindingError(
            f"this run rests on context {run.context.context_fingerprint} and "
            f"these artefacts assemble {rebuilt.context_fingerprint}"
        )
    for name in (
        "run_context_schema_version",
        "experiment_contract_version",
        "dataset_id",
        "dataset_content_fingerprint",
        "profile_id",
        "profile_context_fingerprint",
        "business_metric_run_fingerprint",
        "ranking_evaluation_run_fingerprint",
    ):
        derived = getattr(rebuilt, name)
        stated = getattr(run.context, name)
        if derived != stated:
            # The digest above would already have caught this. The field
            # comparison is what makes the failure legible.
            raise ExperimentBindingError(
                f"these artefacts derive {name} {derived!r} and this run's "
                f"context states {stated!r}"
            )

    by_projection: dict[CohortProjection, ProjectionResult] = {}
    for stored in run.projections:
        recomputed = compute_projection(context, stored.projection)
        _require_identical(
            f"projection {stored.projection}",
            projection_result_payload(stored),
            projection_result_payload(recomputed),
            stored.result_fingerprint,
            recomputed.result_fingerprint,
        )
        by_projection[stored.projection] = stored

    for stored in run.overlaps:
        recomputed_overlap = compute_overlap(context, stored.pair)
        _require_identical(
            f"overlap {stored.pair}",
            overlap_result_payload(stored),
            overlap_result_payload(recomputed_overlap),
            stored.result_fingerprint,
            recomputed_overlap.result_fingerprint,
        )

    recomputed_ranking = compute_ranking_experiment(context)
    _require_identical(
        "the ranking experiment",
        ranking_experiment_result_payload(run.ranking),
        ranking_experiment_result_payload(recomputed_ranking),
        run.ranking.result_fingerprint,
        recomputed_ranking.result_fingerprint,
    )

    _require_business_metric_agreement(verified, by_projection)

    recomputed_run = experiment_run_fingerprint(run)
    if recomputed_run != run.run_fingerprint:
        raise ExperimentBindingError(
            f"the run claims fingerprint {run.run_fingerprint} and its verified "
            f"blocks digest to {recomputed_run}"
        )
    return structural


def _require_identical(
    subject: str,
    stored_payload: dict[str, Any],
    recomputed_payload: dict[str, Any],
    stored_fingerprint: str,
    recomputed_fingerprint: str,
) -> None:
    """Stored and recomputed must be the same block, field for field.

    Compared through the canonical payload rather than by `==` so that a
    mismatch can say *which* field moved — and compared in full, including every
    id of every membership and every number of every support block, because a
    stored block that agrees on the counts while disagreeing about which
    postings they are is still not this experiment.

    This is the check that makes a forged membership visible. The structural
    verifier passes such a block: it is internally impeccable. Only
    recomputation distinguishes it.
    """
    if canonical_json(stored_payload) == canonical_json(recomputed_payload):
        if stored_fingerprint != recomputed_fingerprint:
            # Two identical payloads digesting differently would mean one of the
            # two fingerprints was not computed from the payload beside it.
            raise ExperimentBindingError(
                f"{subject} is stored as {stored_fingerprint} and recomputes to "
                f"{recomputed_fingerprint} over an identical payload"
            )
        return
    differing = sorted(
        field
        for field in set(stored_payload) | set(recomputed_payload)
        if canonical_json(stored_payload.get(field))
        != canonical_json(recomputed_payload.get(field))
    )
    raise ExperimentBindingError(
        f"{subject} is stored as {stored_fingerprint} and recomputing it over "
        f"these artefacts yields {recomputed_fingerprint}; the stored and "
        f"recomputed blocks differ in {differing}. A block whose digest is valid "
        "over a membership no projection produced is exactly what this "
        "verification exists to catch"
    )
