"""The observed ranking experiment: four questions, asked of Phase 10.3.

`compute_ranking_experiment(context)` is the **only** way to obtain a
`RankingExperimentResult`. It is an *adapter* and nothing more: it establishes
which ranking may be evaluated, against which universe, over which membership,
and then calls Phase 10.3's own authorities and records what they return.

## Phase 10.5 owns no ranking formula

There is exactly one arithmetic path per question and it is not here:

    precision_at_k(context, 5)
    precision_at_k(context, 10)
    recall_at_k(context, 10)
    ndcg_at_k(context, 10)

No DCG sum, no gain table, no positional discount, no relevance threshold and
no `effective_k` of its own. `effective_k` stays exactly Phase 10.3's
`min(K, ranking.length)`, applied by Phase 10.3's gates, reported in Phase
10.3's support blocks. A second implementation of any of it would be a second
opinion filed under Phase 10.3's name, and the first divergence would be
invisible because each copy would be internally consistent.

The `MetricResult`s are stored **exactly as returned** — value, status, reason
and support block — and Phase 10.5 mints no metric-level fingerprint around
them. See `fingerprint.py` on why.

## The four questions, and no fifth

`RANKING_EXPERIMENT_QUESTIONS` is the contract's. Every cut-off is a decision
about what "the top" means, and a K nobody decided on would be a number in a
report that no definition supports. So there is no `k` parameter here: the
function takes a context and answers the four questions.

## What may be evaluated, and what may not

**The ranking** is `RankingSource.FROZEN_DATASET_RECOMMENDATION` — the ordering
production actually produced, as Phase 10.1 froze it. There is no other option
and no way to state one: not `opportunity_id ASC`, not `match_quality DESC`, not
a new `recommendation_score`, not new weights, and not a rule that appends the
postings the Recommendation run never covered to the end of the list. An
uncovered posting is **not in the ranking**, which is the fact the snapshot
exists to preserve.

**The universe** is `EvaluationUniverseKind.FROZEN_DATASET_COHORT` — the whole
snapshot. Emphatically not `RECOMMENDATION_OBSERVED`: a recall whose universe is
the set of postings the ranking covered cannot see a relevant posting the
ranking missed, which is the one thing recall exists to notice. Both rules are
established in `context.py` when the inputs are verified and re-established
here.

**The membership** must agree exactly:

    set(EvaluationRanking.opportunity_ids)
        == set(RECOMMENDATION_OBSERVED.included_opportunity_ids)

Two independent derivations of one fact — Phase 10.3's projection of the frozen
`recommendation.rank_position`, and this package's presence projection — held
against each other. That is a real check precisely because neither is computed
from the other.

## The one whole-block refusal

`N_A / NO_OBSERVED_RECOMMENDATION_RANKING`, and only when
`RECOMMENDATION_OBSERVED` is `COMPUTED` and **empty**: the snapshot froze no
recommendation at all, so there is no ordering to evaluate. That is a statement
about the snapshot.

Every *other* way a ranking number can be unknowable already has a Phase 10.3
reason code — a partially judged top K, a universe that is not fully judged, no
relevant item, a zero ideal DCG, a labelset that is not the one the run is bound
to — and those arrive as `N_A` **metric entries inside a COMPUTED block**, which
is exactly what a partially judged calibration round looks like. Re-stating any
of them at block level would create a second vocabulary for one fact.

A snapshot that *has* a ranking but whose labels were never supplied is neither:
it is a precondition failure, raised in `context.py`. See D55 there.

## Diagnostic evidence stays diagnostic

`EvidenceClass` is read off the bound Phase 10.3 run, which derived it through
that package's `require_evidence_class` — a gate that fails closed and refuses
`INDEPENDENT_BENCHMARK` by name for every labelset this build can produce. The
existing calibration round is `DIAGNOSTIC_CALIBRATION`, it is recorded as such,
and nothing here promotes it. Phase 10.5 has no parameter that could.
"""

from __future__ import annotations

from dataclasses import replace

from evaluation.metrics import (
    MetricName,
    MetricResult,
    MetricRunContext,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)

from .context import ExperimentRunContext, _VerifiedExperimentContext, _verified
from .fingerprint import (
    EXPERIMENT_PLACEHOLDER_FINGERPRINT,
    ranking_experiment_result_fingerprint,
    verify_ranking_experiment_result_fingerprint,
)
from .projections import compute_projection
from .schema import (
    EXPERIMENT_CONTRACT_VERSION,
    EXPERIMENT_RANKING_SOURCE,
    EXPERIMENT_RANKING_UNIVERSE_KIND,
    RANKING_EXPERIMENT_QUESTIONS,
    RANKING_EXPERIMENT_RESULT_SCHEMA_VERSION,
    CohortProjection,
    ExperimentBindingError,
    ExperimentContractError,
    ProjectionResult,
    RankingExperimentResult,
    RankingExperimentStatus,
    RankingExperimentUnavailableReason,
    RankingMetricEntry,
    require_supported_experiment_contract_version,
    validate_ranking_experiment_result_structure,
)

__all__ = ["compute_ranking_experiment"]


#: The four Phase 10.3 authorities, by metric name. A mapping rather than an
#: `if` chain so that the questions the contract states and the functions that
#: answer them are one table, and so a metric with no authority is an import-time
#: failure rather than a `KeyError` on one run.
_METRIC_AUTHORITIES = {
    MetricName.PRECISION_AT_K: precision_at_k,
    MetricName.RECALL_AT_K: recall_at_k,
    MetricName.NDCG_AT_K: ndcg_at_k,
}

assert {metric for metric, _ in RANKING_EXPERIMENT_QUESTIONS} <= set(
    _METRIC_AUTHORITIES
), "every ranking question needs a Phase 10.3 authority to answer it"


def _sealed(
    verified: _VerifiedExperimentContext,
    recommendation: ProjectionResult,
    **fields: object,
) -> RankingExperimentResult:
    """Assemble, structurally validate, digest and re-verify. The one exit."""
    draft = RankingExperimentResult(
        result_schema_version=RANKING_EXPERIMENT_RESULT_SCHEMA_VERSION,
        contract_version=require_supported_experiment_contract_version(
            EXPERIMENT_CONTRACT_VERSION
        ),
        dataset_id=verified.dataset.dataset_id,
        dataset_content_fingerprint=verified.dataset.content_fingerprint,
        profile_id=verified.dataset.profile_id,
        profile_context_fingerprint=verified.dataset.profile_context_fingerprint,
        # Always stated, `N_A` included: the emptiness of that projection is
        # precisely what licenses the refusal, so the refusal names it.
        recommendation_projection_result_fingerprint=(
            recommendation.result_fingerprint
        ),
        result_fingerprint=EXPERIMENT_PLACEHOLDER_FINGERPRINT,
        **fields,  # type: ignore[arg-type]
    )
    validate_ranking_experiment_result_structure(draft)
    result = replace(
        draft, result_fingerprint=ranking_experiment_result_fingerprint(draft)
    )
    verify_ranking_experiment_result_fingerprint(result)
    return result


def _require_ranking_membership(
    verified: _VerifiedExperimentContext, recommendation: ProjectionResult
) -> None:
    """The frozen ranking's members are the observed recommendation set. Exactly.

    Two independent derivations of one fact, held against each other: Phase
    10.3 projected the frozen `recommendation.rank_position` of each record into
    an ordering, and this package's `RECOMMENDATION_OBSERVED` read the presence
    of the same block. They must name the same postings.

    A mismatch is a **hard error**, not an `N_A`. It means one of two things,
    and both are contradictions rather than absences: a record carrying a
    recommendation block with no readable rank position, or a ranking assembled
    over a snapshot that is not this one.

    Compared as **sets**, deliberately. The ranking's order is its statement and
    the projection's order is a serialization; requiring the two sequences to be
    equal would demand that the production ranking happen to be in
    `opportunity_id ASC` order, which is a coincidence and not a contract.
    """
    inputs = verified.ranking_inputs
    if inputs is None:  # pragma: no cover - guarded by the caller
        raise ExperimentContractError(
            "the ranking membership cannot be checked without ranking inputs"
        )
    ranked = set(inputs.evaluation_run.ranking.opportunity_ids)
    observed = set(recommendation.included_ids)
    if ranked == observed:
        return
    missing = sorted(observed - ranked)
    extra = sorted(ranked - observed)
    raise ExperimentBindingError(
        f"the frozen ranking holds {len(ranked)} posting(s) and "
        f"{CohortProjection.RECOMMENDATION_OBSERVED} holds {len(observed)}; "
        f"ranked but not observed {extra}, observed but not ranked {missing}. "
        "The ranking under evaluation is a projection of the same frozen "
        "recommendation blocks this projection counts, so the two cannot "
        "legitimately differ"
    )


def _require_bound_ranking(verified: _VerifiedExperimentContext) -> None:
    """Re-establish the two Phase 10.5 rules about *what* may be evaluated.

    `context.py` establishes them when the inputs are verified; they are checked
    again here because this is the function that computes the numbers, and a
    rule enforced only at the boundary is a rule that holds until somebody adds
    a second boundary.
    """
    inputs = verified.ranking_inputs
    if inputs is None:  # pragma: no cover - guarded by the caller
        raise ExperimentContractError(
            "there is no bound ranking to establish"
        )
    run = inputs.evaluation_run
    if run.universe.kind is not EXPERIMENT_RANKING_UNIVERSE_KIND:
        raise ExperimentBindingError(
            f"the bound evaluation run measures a {run.universe.kind} universe "
            f"and Phase 10.5 evaluates against {EXPERIMENT_RANKING_UNIVERSE_KIND}"
        )
    if run.ranking.source is not EXPERIMENT_RANKING_SOURCE:
        raise ExperimentBindingError(
            f"the bound evaluation run evaluates a {run.ranking.source} ranking "
            f"and Phase 10.5 evaluates {EXPERIMENT_RANKING_SOURCE}"
        )
    if run.universe.dataset_id != verified.dataset.dataset_id:
        raise ExperimentBindingError(
            f"the bound universe is over dataset {run.universe.dataset_id} and "
            f"this experiment is about {verified.dataset.dataset_id}"
        )
    if run.universe.size != verified.cohort_size:
        raise ExperimentBindingError(
            f"the bound universe holds {run.universe.size} posting(s) and this "
            f"frozen cohort holds {verified.cohort_size}; the universe is the "
            "whole cohort"
        )


def compute_ranking_experiment(
    context: ExperimentRunContext,
) -> RankingExperimentResult:
    """Ask the four ranking questions about the frozen ordering, or refuse.

    The single public way to obtain a `RankingExperimentResult`. In order:

    1. the whole context is re-verified — as in every public computation here;
    2. `RECOMMENDATION_OBSERVED` is obtained through the **public**
       `compute_projection`, so the membership the ranking is held against is
       the official one;
    3. an empty observed set is the one whole-block refusal;
    4. otherwise the bound ranking's source, universe and membership are
       re-established, and the four Phase 10.3 authorities are called in
       contract order over one `MetricRunContext`.

    Each authority runs its own gate, which re-verifies the Phase 10.3 run and
    the coverage against the dataset from scratch — so the numbers here rest on
    artefacts re-established four times over, not on this function's word.
    """
    verified = _verified(context)
    recommendation = compute_projection(
        context, CohortProjection.RECOMMENDATION_OBSERVED
    )
    if not recommendation.computed:  # pragma: no cover - presence has no N_A
        raise ExperimentContractError(
            f"{CohortProjection.RECOMMENDATION_OBSERVED} is "
            f"{recommendation.status}; a presence projection is always "
            "computable from the frozen records"
        )

    if recommendation.included_count == 0:
        # The one legitimate whole-block `N_A`. `context.py` has already
        # established that no ranking inputs were supplied for such a snapshot,
        # so there is nothing to leave unstated by accident.
        return _sealed(
            verified,
            recommendation,
            status=RankingExperimentStatus.N_A,
            unavailable_reason=(
                RankingExperimentUnavailableReason.NO_OBSERVED_RECOMMENDATION_RANKING
            ),
        )

    inputs = verified.ranking_inputs
    if inputs is None:
        # Unreachable through `_verified`, which refuses this combination as a
        # precondition failure (D55). Refused rather than turned into an `N_A`,
        # for exactly the reason it is refused there: a missing input must not
        # look like a measured absence of evidence.
        raise ExperimentContractError(
            f"this snapshot froze {recommendation.included_count} recommendation "
            "assessment(s) and the verified context holds no ranking inputs"
        )
    _require_bound_ranking(verified)
    _require_ranking_membership(verified, recommendation)

    run = inputs.evaluation_run
    metric_context = MetricRunContext(
        dataset=verified.dataset,
        run=run,
        coverage=inputs.label_coverage,
    )
    entries: list[RankingMetricEntry] = []
    for metric, k_requested in RANKING_EXPERIMENT_QUESTIONS:
        authority = _METRIC_AUTHORITIES[metric]
        result = authority(metric_context, k_requested)
        if not isinstance(result, MetricResult):
            raise ExperimentContractError(
                f"asking {metric} at K={k_requested} produced {result!r}, which "
                "is not a Phase 10.3 metric result"
            )
        if result.metric is not metric:
            raise ExperimentBindingError(
                f"{metric} at K={k_requested} was asked and the result is about "
                f"{result.metric}"
            )
        entries.append(
            RankingMetricEntry(
                metric=metric,
                k_requested=k_requested,
                # Stored exactly as Phase 10.3 returned it. Nothing is
                # re-derived, rounded, re-supported or re-sealed.
                result=result,
            )
        )
    return _sealed(
        verified,
        recommendation,
        status=RankingExperimentStatus.COMPUTED,
        evaluation_run_fingerprint=run.run_fingerprint,
        evaluation_universe_fingerprint=run.universe.fingerprint,
        ranking_fingerprint=run.ranking.fingerprint,
        labelset_fingerprint=run.labelset_fingerprint,
        # Read off the bound Phase 10.3 run, which derived it through that
        # package's fail-closed gate. There is no parameter here that could
        # promote a diagnostic calibration to an independent benchmark.
        evidence_class=run.evidence_class,
        metric_results=tuple(entries),
    )
