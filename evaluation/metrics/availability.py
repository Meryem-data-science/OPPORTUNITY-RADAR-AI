"""Which ranking metrics may be reported at all — and none of their values.

This module answers one question per metric: *given this universe, this
ranking and these judgements, is the metric knowable?* It never answers *what
is it*. There is no Precision@K here, no Recall@K, no DCG, no IDCG and no NDCG;
Phase 10.3a builds the gate and Phase 10.3b walks through it.

The gates are pure and deterministic: same artefacts in, same decision out, no
file read, no clock, no database, no randomness.

## What is counted, and why counting it is not a metric

Judged coverage *is* computed here — how many opportunities of a set carry an
effective judgement, and which do not. That is a support statistic about the
evidence, not a measurement of the ranking: it says how many questions were
answered, never whether the answers agreed with the pipeline. Deferring it
would leave the gates with nothing to decide on.

## The three rules

**Precision@K** needs every position in the effective top K to be judged.
One unjudged item there and the numerator is a count of an unknown, so the
metric is `N_A / TOP_K_NOT_FULLY_JUDGED`. Nothing about the rest of the
universe matters: precision asks only about what was shown.

**Recall@K** needs the total number of relevant items in the declared universe,
and that number is knowable only if the universe is closed *and* judged. The
safe rule this contract adopts is the whole universe fully judged; anything
less is `N_A / RECALL_DENOMINATOR_UNKNOWN`. A fully judged universe holding no
relevant item is a different fact — the denominator is known and it is zero —
and reporting a recall of 0.0 there would describe the evaluation set rather
than the ranking, so it is `N_A / NO_RELEVANT_ITEMS`.

**NDCG@K** needs more than a judged top K, and this is the rule most likely to
be weakened by somebody in a hurry. NDCG is a ratio against the *ideal*
ordering, and the ideal ordering is built from the best grades available in the
comparison universe. An unjudged tail cannot lower a DCG — the numerator only
ever sees ranked, judged items — but it silently lowers the IDCG this build
would be able to construct, and a ratio with an understated denominator is an
overstated score. So a judged top K is not enough: the universe must be fully
judged, or the metric is `N_A / EVALUATION_UNIVERSE_NOT_FULLY_JUDGED`.

## K

Strict, and `bool` refused before `int`, because `K=True` would evaluate the
top 1 and report it as the answer to whatever question the caller believed they
were asking.

`K` larger than the ranking is not an error and is not padded: the effective
cut-off is `min(K, ranking_length)`, both numbers are recorded in the support
block, and the gates decide on the effective one. Pretending positions 13 to 50
of a ranking of 12 exist would invent unjudged items and make every metric
unavailable for a reason that is not true.

## The relevance threshold

Reused from the frozen Phase 10.2 rubric — `relevant iff grade >= 2` — and used
here for exactly one purpose: telling "the universe is fully judged and holds
no relevant item" apart from "the universe is fully judged and holds some". No
ranking relevance is scored with it, because no ranking relevance is scored
here at all.

## One open question, recorded rather than decided

A fully judged universe in which *every* grade is 0 has an IDCG of zero, and a
ratio against zero is undefined. That is a different condition from
`NO_RELEVANT_ITEMS` as this contract uses it (grade >= 2), and deciding it
needs the gain function NDCG will actually use — which is a 10.3b question.
This slice does not guess: such a universe passes the NDCG gate here, and the
formula slice must decide the case explicitly rather than divide by zero.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from .schema import (
    EvaluationBindingError,
    EvaluationRanking,
    EvaluationRunContract,
    EvaluationUniverse,
    JudgedCoverage,
    LabelCoverage,
    MetricAvailability,
    MetricAvailabilityDecision,
    MetricName,
    MetricSupport,
    MetricUnavailableReason,
    is_relevant_grade,
    validate_k,
)

__all__ = [
    "effective_k",
    "judged_coverage_of",
    "judged_count_in_universe",
    "ndcg_at_k_availability",
    "precision_at_k_availability",
    "recall_at_k_availability",
    "relevant_count_in_universe",
    "top_k_judged_coverage",
    "universe_judged_coverage",
    "validate_k",
]


# --------------------------------------------------------------------------
# judged coverage — support statistics, not metrics
# --------------------------------------------------------------------------


def judged_coverage_of(
    opportunity_ids: Sequence[int], coverage: LabelCoverage
) -> JudgedCoverage:
    """How much of an arbitrary set of opportunities carries a judgement.

    The unjudged ids are reported, not merely counted: a gate that says "the top
    10 is not fully judged" is an instruction to go and judge something, and it
    is a much better instruction when it names what.
    """
    unjudged = tuple(
        opportunity_id
        for opportunity_id in opportunity_ids
        if not coverage.is_judged(opportunity_id)
    )
    return JudgedCoverage(
        judged_count=len(opportunity_ids) - len(unjudged),
        total_count=len(opportunity_ids),
        unjudged_opportunity_ids=unjudged,
    )


def universe_judged_coverage(
    universe: EvaluationUniverse, coverage: LabelCoverage
) -> JudgedCoverage:
    """Judged coverage of the declared universe, in canonical id order.

    Of the **universe**, which is the whole point: coverage of "the ids that
    have labels" is always 100% and tells nobody anything.
    """
    return judged_coverage_of(universe.opportunity_ids, coverage)


def judged_count_in_universe(
    universe: EvaluationUniverse, coverage: LabelCoverage
) -> int:
    """How many of the universe's opportunities carry an effective judgement."""
    return universe_judged_coverage(universe, coverage).judged_count


def relevant_count_in_universe(
    universe: EvaluationUniverse, coverage: LabelCoverage
) -> int:
    """How many judged members of the universe are relevant, `grade >= 2`.

    Refuses outright unless the universe is fully judged. A count over a partly
    judged universe would be a lower bound presented as a total, and a lower
    bound in a recall denominator overstates recall — which is precisely the
    failure `RECALL_DENOMINATOR_UNKNOWN` exists to prevent, so it is not
    available as a convenience here either.
    """
    judged = universe_judged_coverage(universe, coverage)
    if not judged.fully_judged:
        raise EvaluationBindingError(
            f"{len(judged.unjudged_opportunity_ids)} of the universe's "
            f"{judged.total_count} opportunities carry no judgement; the "
            "number of relevant items in it is not knowable, and a count over "
            "the judged part would be a lower bound reported as a total"
        )
    return sum(
        1
        for opportunity_id in universe.opportunity_ids
        if is_relevant_grade(coverage.grade(opportunity_id))
    )


def effective_k(k: int, ranking: EvaluationRanking) -> int:
    """`min(K, ranking length)`, after validating K.

    The explicit rule of this contract. A ranking of 12 asked for its top 50 has
    an effective cut-off of 12, and the difference between the two numbers is
    recorded in every support block rather than resolved silently in either
    direction.
    """
    return min(validate_k(k), ranking.length)


def top_k_judged_coverage(
    ranking: EvaluationRanking, coverage: LabelCoverage, k: int
) -> JudgedCoverage:
    """Judged coverage of the effective top K, in rank order."""
    return judged_coverage_of(
        ranking.opportunity_ids[: effective_k(k, ranking)], coverage
    )


# --------------------------------------------------------------------------
# binding, checked before any gate decides
# --------------------------------------------------------------------------


def _check_label_bindings(
    run: EvaluationRunContract, coverage: LabelCoverage
) -> bool:
    """Refuse structurally wrong labels; report a merely different labelset.

    Returns whether the labels are a *different labelset* from the one the run
    declares — and raises before returning at all if they are not even about
    the same thing.

    Two layers, deliberately different in kind. A coverage built over another
    dataset or another profile state is a **structurally wrong artefact** and
    raises: the question itself does not typecheck. A coverage over the right
    dataset whose labelset digest or protocol is not the one the run declares is
    a **well-formed question this evidence cannot answer**, so it is reported as
    `N_A / LABELSET_BINDING_MISMATCH` rather than as a crash — an unavailable
    metric is a legitimate answer, and this is one.
    """
    if coverage.dataset_id != run.dataset_id:
        raise EvaluationBindingError(
            f"the labels were made against dataset {coverage.dataset_id}, not "
            f"{run.dataset_id}"
        )
    if coverage.dataset_content_fingerprint != run.dataset_content_fingerprint:
        raise EvaluationBindingError(
            "the labels were made against content fingerprint "
            f"{coverage.dataset_content_fingerprint}, not "
            f"{run.dataset_content_fingerprint}"
        )
    if coverage.profile_id != run.profile_id:
        raise EvaluationBindingError(
            f"the labels were made for profile {coverage.profile_id}, not "
            f"{run.profile_id}"
        )
    if coverage.profile_context_fingerprint != run.profile_context_fingerprint:
        raise EvaluationBindingError(
            "the labels were made against profile context "
            f"{coverage.profile_context_fingerprint}, not "
            f"{run.profile_context_fingerprint}"
        )
    return (
        coverage.labelset_fingerprint != run.labelset_fingerprint
        or coverage.label_protocol_version != run.label_protocol_version
    )


def _support(
    run: EvaluationRunContract,
    coverage: LabelCoverage,
    *,
    k_requested: int,
    k_effective: int,
) -> MetricSupport:
    """The numbers behind a decision, counted once and shared by the gates."""
    universe = universe_judged_coverage(run.universe, coverage)
    top = judged_coverage_of(
        run.ranking.opportunity_ids[:k_effective], coverage
    )
    return MetricSupport(
        k_requested=k_requested,
        k_effective=k_effective,
        ranking_length=run.ranking.length,
        universe_size=run.universe.size,
        judged_count=universe.judged_count,
        judged_in_top_k=top.judged_count,
        unjudged_in_top_k=top.unjudged_opportunity_ids,
        unjudged_in_universe=universe.unjudged_opportunity_ids,
    )


def _binding_mismatch_availability(
    metric: MetricName, run: EvaluationRunContract, k: int
) -> MetricAvailability:
    """The one decision that can be made without reading a single judgement."""
    return MetricAvailability(
        metric=metric,
        decision=MetricAvailabilityDecision.UNAVAILABLE,
        reason=MetricUnavailableReason.LABELSET_BINDING_MISMATCH,
        support=MetricSupport(
            k_requested=validate_k(k),
            k_effective=min(validate_k(k), run.ranking.length),
            ranking_length=run.ranking.length,
            universe_size=run.universe.size,
        ),
    )


# --------------------------------------------------------------------------
# the gates
# --------------------------------------------------------------------------


def precision_at_k_availability(
    run: EvaluationRunContract, coverage: LabelCoverage, k: int
) -> MetricAvailability:
    """May Precision@K be reported for this run? **Its value is not computed.**

    Available only when every position of the effective top K carries a
    judgement. An unjudged item there cannot be counted as a hit and must not be
    counted as a miss, and there is no third option that is honest.
    """
    k_requested = validate_k(k)
    if _check_label_bindings(run, coverage):
        return _binding_mismatch_availability(
            MetricName.PRECISION_AT_K, run, k_requested
        )
    k_used = min(k_requested, run.ranking.length)
    support = _support(
        run, coverage, k_requested=k_requested, k_effective=k_used
    )
    if support.unjudged_in_top_k:
        return MetricAvailability(
            metric=MetricName.PRECISION_AT_K,
            decision=MetricAvailabilityDecision.UNAVAILABLE,
            reason=MetricUnavailableReason.TOP_K_NOT_FULLY_JUDGED,
            support=support,
        )
    return MetricAvailability(
        metric=MetricName.PRECISION_AT_K,
        decision=MetricAvailabilityDecision.AVAILABLE,
        support=support,
    )


def recall_at_k_availability(
    run: EvaluationRunContract, coverage: LabelCoverage, k: int
) -> MetricAvailability:
    """May Recall@K be reported for this run? **Its value is not computed.**

    Available only when the declared universe is closed and fully judged, so the
    total number of relevant items is a fact rather than a lower bound, and only
    when that total is not zero.
    """
    k_requested = validate_k(k)
    if _check_label_bindings(run, coverage):
        return _binding_mismatch_availability(
            MetricName.RECALL_AT_K, run, k_requested
        )
    k_used = min(k_requested, run.ranking.length)
    support = _support(
        run, coverage, k_requested=k_requested, k_effective=k_used
    )
    if support.unjudged_in_universe:
        return MetricAvailability(
            metric=MetricName.RECALL_AT_K,
            decision=MetricAvailabilityDecision.UNAVAILABLE,
            reason=MetricUnavailableReason.RECALL_DENOMINATOR_UNKNOWN,
            support=support,
        )
    # The denominator is knowable, so it is stated. It is *not* a metric
    # value: it is a count of the evidence, and no ranking is measured by it
    # anywhere in this slice.
    relevant = relevant_count_in_universe(run.universe, coverage)
    support = replace(support, relevant_count=relevant)
    if relevant == 0:
        return MetricAvailability(
            metric=MetricName.RECALL_AT_K,
            decision=MetricAvailabilityDecision.UNAVAILABLE,
            reason=MetricUnavailableReason.NO_RELEVANT_ITEMS,
            support=support,
        )
    return MetricAvailability(
        metric=MetricName.RECALL_AT_K,
        decision=MetricAvailabilityDecision.AVAILABLE,
        support=support,
    )


def ndcg_at_k_availability(
    run: EvaluationRunContract, coverage: LabelCoverage, k: int
) -> MetricAvailability:
    """May NDCG@K be reported for this run? **Its value is not computed.**

    Available only when the whole evaluation universe is fully judged. A judged
    top K is deliberately **not** sufficient: the ideal ordering is drawn from
    the best grades anywhere in the comparison universe, so an unjudged tail
    understates the IDCG and therefore overstates the ratio.
    """
    k_requested = validate_k(k)
    if _check_label_bindings(run, coverage):
        return _binding_mismatch_availability(
            MetricName.NDCG_AT_K, run, k_requested
        )
    k_used = min(k_requested, run.ranking.length)
    support = _support(
        run, coverage, k_requested=k_requested, k_effective=k_used
    )
    if support.unjudged_in_universe:
        return MetricAvailability(
            metric=MetricName.NDCG_AT_K,
            decision=MetricAvailabilityDecision.UNAVAILABLE,
            reason=MetricUnavailableReason.EVALUATION_UNIVERSE_NOT_FULLY_JUDGED,
            support=support,
        )
    return MetricAvailability(
        metric=MetricName.NDCG_AT_K,
        decision=MetricAvailabilityDecision.AVAILABLE,
        support=support,
    )
