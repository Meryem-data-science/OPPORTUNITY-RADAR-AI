"""Which ranking metrics may be reported at all — and none of their values.

This module answers one question per metric: *given this universe, this
ranking and these judgements, is the metric knowable?* It never answers *what
is it*. There is no Precision@K value here, no Recall@K value, no DCG and no
IDCG sum: the gates decide, `formulas.py` computes, and each formula asks its
gate before touching a grade.

The gates are pure and deterministic: same artefacts in, same decision out, no
file read, no clock, no database, no randomness.

**They verify their own inputs, every time.** A gate takes a
`MetricRunContext` — the frozen dataset, the run and the label coverage
together — and its first act is `verify_metric_run_context`, which re-establishes
the run and the evidence against that dataset from scratch: structure, digests,
dataset membership, the calibration lot redrawn. Nothing is inferred from the
type of the argument, because `MetricRunContext` is a public dataclass anybody
can construct; a context assembled by hand around a re-digested invalid run is
refused by the gate itself, and there is no caller-supplied flag that can make a
foreign labelset look like the run's.

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
ranking relevance is scored with it here; scoring is `formulas.py`'s job, and
it reuses the same `is_relevant_grade` rather than restating the threshold.

## The zero ideal, decided

A fully judged universe whose best grades at the cut-off are all 0 has an ideal
ordering worth nothing, so NDCG's denominator is zero and the ratio is
undefined. Phase 10.3a left this open because deciding it needed the gain
function; the gain function is now frozen in `schema.relevance_gain`, so the
NDCG gate settles it here: `N_A / ZERO_IDEAL_DCG`, never an AVAILABLE that
hands the formula a division it cannot perform.

The gate decides it from gains rather than by summing a DCG it has no business
computing. Every discount is `log2(rank + 1)`, which is at least 1.0 and always
finite and positive, so a sum of non-negative discounted gains is zero exactly
when every gain in it is zero — and "is any ideal gain positive?" is a question
about the contract, not a score.

`ZERO_IDEAL_DCG` is not `NO_RELEVANT_ITEMS`. Relevance is binary at `grade >= 2`
while NDCG is graded: a universe of nothing but grade 1 has no relevant item —
Recall is `N_A / NO_RELEVANT_ITEMS` — and a strictly positive IDCG, because
`gain(1) == 1`. Two different facts, two different codes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from .run import verify_metric_run_context
from .schema import (
    EvaluationBindingError,
    EvaluationRanking,
    EvaluationUniverse,
    JudgedCoverage,
    LabelCoverage,
    MetricAvailability,
    MetricAvailabilityDecision,
    MetricName,
    MetricRunContext,
    MetricSupport,
    MetricUnavailableReason,
    is_relevant_grade,
    relevance_gain,
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


def _ideal_grades_at_cutoff(
    universe: EvaluationUniverse, coverage: LabelCoverage, k: int
) -> tuple[int, ...]:
    """The best `k` grades the declared universe can offer, best first.

    Private, and shared with `formulas.py` by explicit import rather than
    through the package's public surface. It is an ingredient of NDCG, not a
    metric: exposing it would offer a way to read verified grades in ranked
    order without any gate having decided that reading them is legitimate, and
    the availability -> formula boundary is the only door this package has.

    The ideal ordering NDCG is measured against, and it is drawn from the
    **whole declared universe** rather than from the ranked items, the judged
    items or the relevant ones. An ideal built from what the ranking happened to
    surface would be an ideal the ranking cannot fail to match.

    Refuses unless the universe is fully judged, for the same reason
    `relevant_count_in_universe` does: the best grades of the judged part are an
    understatement of the best grades of the whole, and an understated ideal
    inflates every ratio taken against it.

    Fewer than `k` grades come back when the universe is smaller than the
    cut-off. Nothing is padded — a universe of 3 has 3 grades, not 3 grades and
    seven zeros — because a padded zero is an opinion nobody expressed.
    """
    judged = universe_judged_coverage(universe, coverage)
    if not judged.fully_judged:
        raise EvaluationBindingError(
            f"{len(judged.unjudged_opportunity_ids)} of the universe's "
            f"{judged.total_count} opportunities carry no judgement; the ideal "
            "ordering is not knowable, and one built from the judged part would "
            "understate the ideal and inflate every ratio taken against it"
        )
    grades = sorted(
        (coverage.grade(opportunity_id) for opportunity_id in universe.opportunity_ids),
        reverse=True,
    )
    return tuple(grades[: validate_k(k)])


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
# the gates
# --------------------------------------------------------------------------
#
# Every gate begins with `verify_metric_run_context`, and that call — not the
# type of the argument — is what makes verification unavoidable.
#
# A `MetricRunContext` is a public dataclass: `MetricRunContext(...)` is a
# constructor call anybody can make, so holding one proves nothing and the gates
# infer nothing from it. What the context *is* is a bundle carrying the frozen
# dataset alongside the run and the coverage, which is exactly what is needed to
# re-establish both from scratch. So the first thing every gate does is redo the
# full dataset-bound verification of the run and of the evidence, and the
# labelset-match state it then uses is the value that verifier **returns** —
# derived from two verified artefacts, never a boolean a caller could set.


def _support(
    context: MetricRunContext, *, k_requested: int, k_effective: int
) -> MetricSupport:
    """The numbers behind a decision, counted once and shared by the gates."""
    universe = universe_judged_coverage(context.universe, context.coverage)
    top = judged_coverage_of(
        context.ranking.opportunity_ids[:k_effective], context.coverage
    )
    return MetricSupport(
        k_requested=k_requested,
        k_effective=k_effective,
        ranking_length=context.ranking.length,
        universe_size=context.universe.size,
        judged_count=universe.judged_count,
        judged_in_top_k=top.judged_count,
        unjudged_in_top_k=top.unjudged_opportunity_ids,
        unjudged_in_universe=universe.unjudged_opportunity_ids,
    )


def _binding_mismatch_availability(
    metric: MetricName, context: MetricRunContext, k: int
) -> MetricAvailability:
    """The one decision that can be made without reading a single judgement."""
    return MetricAvailability(
        metric=metric,
        decision=MetricAvailabilityDecision.UNAVAILABLE,
        reason=MetricUnavailableReason.LABELSET_BINDING_MISMATCH,
        support=MetricSupport(
            k_requested=k,
            k_effective=min(k, context.ranking.length),
            ranking_length=context.ranking.length,
            universe_size=context.universe.size,
        ),
    )


def precision_at_k_availability(
    context: MetricRunContext, k: int
) -> MetricAvailability:
    """May Precision@K be reported for this run? **Its value is not computed.**

    Available only when every position of the effective top K carries a
    judgement. An unjudged item there cannot be counted as a hit and must not be
    counted as a miss, and there is no third option that is honest.

    The context is fully re-verified first, however it was obtained.
    """
    k_requested = validate_k(k)
    if not verify_metric_run_context(context):
        return _binding_mismatch_availability(
            MetricName.PRECISION_AT_K, context, k_requested
        )
    k_used = min(k_requested, context.ranking.length)
    support = _support(context, k_requested=k_requested, k_effective=k_used)
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
    context: MetricRunContext, k: int
) -> MetricAvailability:
    """May Recall@K be reported for this run? **Its value is not computed.**

    Available only when the declared universe is closed and fully judged, so the
    total number of relevant items is a fact rather than a lower bound, and only
    when that total is not zero.

    The context is fully re-verified first, however it was obtained.
    """
    k_requested = validate_k(k)
    if not verify_metric_run_context(context):
        return _binding_mismatch_availability(
            MetricName.RECALL_AT_K, context, k_requested
        )
    k_used = min(k_requested, context.ranking.length)
    support = _support(context, k_requested=k_requested, k_effective=k_used)
    if support.unjudged_in_universe:
        return MetricAvailability(
            metric=MetricName.RECALL_AT_K,
            decision=MetricAvailabilityDecision.UNAVAILABLE,
            reason=MetricUnavailableReason.RECALL_DENOMINATOR_UNKNOWN,
            support=support,
        )
    # The denominator is knowable, so it is stated. It is *not* a metric value:
    # it is a count of the evidence, and no ranking is measured by it anywhere
    # in this slice.
    relevant = relevant_count_in_universe(context.universe, context.coverage)
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
    context: MetricRunContext, k: int
) -> MetricAvailability:
    """May NDCG@K be reported for this run? **Its value is not computed.**

    Available only when the whole evaluation universe is fully judged. A judged
    top K is deliberately **not** sufficient: the ideal ordering is drawn from
    the best grades anywhere in the comparison universe, so an unjudged tail
    understates the IDCG and therefore overstates the ratio.

    The context is fully re-verified first, however it was obtained.
    """
    k_requested = validate_k(k)
    if not verify_metric_run_context(context):
        return _binding_mismatch_availability(
            MetricName.NDCG_AT_K, context, k_requested
        )
    k_used = min(k_requested, context.ranking.length)
    support = _support(context, k_requested=k_requested, k_effective=k_used)
    if support.unjudged_in_universe:
        return MetricAvailability(
            metric=MetricName.NDCG_AT_K,
            decision=MetricAvailabilityDecision.UNAVAILABLE,
            reason=MetricUnavailableReason.EVALUATION_UNIVERSE_NOT_FULLY_JUDGED,
            support=support,
        )
    # The gate has to be honest about the denominator as well as the evidence.
    # A universe that is fully judged and graded 0 throughout has an ideal
    # ordering worth nothing, so NDCG's denominator is zero and the ratio is
    # undefined — announcing AVAILABLE there would hand the formula a division
    # it cannot perform.
    #
    # Decided from gains, not from a DCG: every discount is at least 1.0 and
    # therefore finite and positive, so a sum of non-negative discounted gains
    # is zero exactly when every gain in it is zero. The gate consults the one
    # frozen `relevance_gain`; summing it is the formula's job, not a gate's.
    if not any(
        relevance_gain(grade) > 0
        for grade in _ideal_grades_at_cutoff(
            context.universe, context.coverage, k_used
        )
    ):
        return MetricAvailability(
            metric=MetricName.NDCG_AT_K,
            decision=MetricAvailabilityDecision.UNAVAILABLE,
            reason=MetricUnavailableReason.ZERO_IDEAL_DCG,
            support=support,
        )
    return MetricAvailability(
        metric=MetricName.NDCG_AT_K,
        decision=MetricAvailabilityDecision.AVAILABLE,
        support=support,
    )
