"""The three offline ranking metrics — Phase 10.3b.

This module computes Precision@K, Recall@K and NDCG@K. It is the only module in
this package that produces ranking metric values — `schema.py` defines the
contract's own numbers, the gain and the discount, and `availability.py` counts
support statistics — and it produces one **only where a Phase 10.3a gate has
already said the question is answerable**.

## The one rule that shapes everything here

Every public function starts by calling its own gate:

    availability = precision_at_k_availability(context, k)
    if not availability.available:
        return availability.as_result()
    ... only now is anything computed ...

That is not a politeness. The gate is what re-verifies the whole context — the
run and the evidence re-established against the frozen dataset, structure,
digests, dataset membership, the calibration lot redrawn — and what decides
whether the judgements a formula would need actually exist. A formula that read
`context.coverage.grade(...)` before its gate had run would be a second, weaker
door into the same house: a forged context that the gate refuses would be
computed from instead. So there is no such path, no re-implemented check that
might be laxer, and no way to ask for a number the gate did not authorise.

The gate is also what makes **UNJUDGED != 0** hold here. Nothing below reaches
for a default grade: every grade is read through `LabelCoverage.grade`, which
raises for an opportunity nobody judged, and the gate has already established
that the positions each formula touches are judged. There is no `.get(id, 0)`
in this file, and there is no `None` that later becomes a zero.

## The frozen definitions

**Precision@K** — over the effective top K, which is `min(K, ranking_length)`:

    numerator   = ranked items in the effective top K whose grade is relevant
    denominator = k_effective
    value       = numerator / denominator

Relevance is binary and is Phase 10.2's: `relevant iff grade >= 2`, through
`is_relevant_grade`. Grade 1 is not relevant; grades 2 and 3 are.

**Recall@K** — the numerator is local, the denominator is not:

    numerator   = relevant items found in the effective top K
    denominator = relevant items in the whole declared evaluation universe
    value       = numerator / denominator

The denominator is the count over the `EvaluationUniverse`, which the gate has
established is closed and fully judged. It is not the number of labels, not the
length of the ranking, not the relevant items of the top K, and not the
calibration lot's own relevant count unless that lot *is* the declared universe.
A relevant posting the ranking never surfaced still sits in the denominator,
which is the entire point of measuring recall against a universe.

**NDCG@K** — graded, not binary:

    gain(grade)      = 2**grade - 1          0 -> 0, 1 -> 1, 2 -> 3, 3 -> 7
    discount(rank)   = log2(rank + 1)        rank 1 -> 1.0, 2 -> 1.585, 3 -> 2.0
    DCG@K            = Σ gain(grade_r) / discount(r)  for r in 1..k_effective
    ideal grades     = the best k_effective grades of the whole universe
    IDCG@K           = the same sum over those ideal grades
    value            = DCG@K / IDCG@K

`gain` and `discount` are not defined here — they are `schema.relevance_gain`
and `schema.rank_discount`, the frozen contract both this module and the NDCG
gate consult, so the gate's decision and the formula's arithmetic cannot come
to disagree about what an ideal ordering is worth.

The ideal is built from **every** grade in the declared universe, best first,
cut at `k_effective`. Not from the ranked items, not from the judged subset of
them, not from the relevant ones: an ideal assembled out of what the ranking
surfaced is an ideal the ranking cannot fail to match, and the ratio would stop
measuring anything. A posting graded 3 that the ranking never showed belongs in
the ideal, and lowers the score — correctly.

## IDCG = 0

Phase 10.3a left this open; it is decided here. A fully judged universe whose
best `k_effective` grades are all 0 has an ideal ordering worth nothing, so
NDCG's denominator is zero and the ratio is undefined. The result is
`N_A / ZERO_IDEAL_DCG` — never 0.0, never 1.0, never NaN, never an exception.
The NDCG gate now refuses that case itself, so a formula never meets it; the
same decision is repeated below as a backstop rather than a division.

`ZERO_IDEAL_DCG` is deliberately not `NO_RELEVANT_ITEMS`. Relevance is binary at
`grade >= 2` while NDCG is graded: a universe of nothing but grade 1 has no
relevant item at all — Recall is `N_A / NO_RELEVANT_ITEMS` there — and an
entirely positive IDCG, because `gain(1) == 1`. The two conditions are different
facts and they get different codes.

## What this module does not do

No rounding, no clamping, no smoothing, no default. A value outside `[0, 1]`
would be a bug and must look like one, so nothing here quietly pulls a result
back into range. No clock, no randomness, no file, no database: given the same
context and the same K, these functions return the same numbers forever.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from .availability import (
    _ideal_grades_at_cutoff,
    ndcg_at_k_availability,
    precision_at_k_availability,
    recall_at_k_availability,
)
from .schema import (
    MetricContractError,
    MetricName,
    MetricResult,
    MetricRunContext,
    MetricStatus,
    MetricSupport,
    MetricUnavailableReason,
    is_relevant_grade,
    rank_discount,
    relevance_gain,
)

#: The whole public surface of this module. The helpers below are private on
#: purpose: a caller who could reach a DCG sum or an ideal cut-off directly
#: would have a way to compute over verified grades without a gate having
#: decided that the metric is knowable, which is the one boundary this package
#: exists to hold. The three metrics are the contract; everything else is how
#: they are made.
__all__ = [
    "ndcg_at_k",
    "precision_at_k",
    "recall_at_k",
]


def _discounted_cumulative_gain(grades: Sequence[int]) -> float:
    """The DCG of a sequence of grades read in rank order, first position 1.

    The one summation in this package, used for the ranked cut-off and for the
    ideal ordering alike — an IDCG that used a second implementation could
    disagree with its own numerator and nobody would see it in the ratio.

    Every grade goes through the frozen `relevance_gain`, so a value outside
    Phase 10.2's 0-3 scale is refused rather than given a gain nobody defined.
    """
    return sum(
        relevance_gain(grade) / rank_discount(position)
        for position, grade in enumerate(grades, start=1)
    )


def _computed(
    *,
    metric: MetricName,
    value: float,
    support: MetricSupport,
    numerator: float,
    denominator: float,
) -> MetricResult:
    """One COMPUTED result, with the fraction that produced it kept beside it.

    The support is the gate's own — the coverage counts, `k_requested`,
    `k_effective`, `ranking_length` — with only the numerator and denominator
    added, so a reader can check the arithmetic without rerunning it and a
    result can never quote support that describes a different decision.
    """
    return MetricResult(
        metric=metric,
        status=MetricStatus.COMPUTED,
        value=value,
        support=replace(support, numerator=numerator, denominator=denominator),
    )


def _relevant_in_top_k(context: MetricRunContext, k_effective: int) -> int:
    """Relevant items among the effective top K, read one judgement at a time.

    `coverage.grade` raises for an unjudged opportunity, and that is the
    intended behaviour rather than a risk to guard against: the caller's gate
    has already established that every position here is judged, so a raise would
    mean the gate and the coverage disagree — which is a bug to surface, not a
    zero to substitute.
    """
    return sum(
        1
        for opportunity_id in context.ranking.opportunity_ids[:k_effective]
        if is_relevant_grade(context.coverage.grade(opportunity_id))
    )


def precision_at_k(context: MetricRunContext, k: int) -> MetricResult:
    """Precision@K over the effective top K, or the gate's `N_A` result.

    `numerator / k_effective`, where the numerator counts the ranked items in
    the effective top K whose recorded grade is relevant under Phase 10.2's
    binary contract. Available only when every one of those positions is judged;
    a single unjudged item makes the metric `N_A / TOP_K_NOT_FULLY_JUDGED`,
    because an unjudged item can be neither counted as a hit nor counted as a
    miss.
    """
    availability = precision_at_k_availability(context, k)
    if not availability.available:
        return availability.as_result()

    support = availability.support
    k_effective = support.k_effective
    if not k_effective:
        # Unreachable through the gate — a ranking holds at least one entry and
        # K is at least 1 — and refused rather than divided by, because a
        # backstop that returns a number is not a backstop.
        raise MetricContractError(
            "Precision@K has no denominator: the effective cut-off is 0"
        )
    numerator = _relevant_in_top_k(context, k_effective)
    return _computed(
        metric=MetricName.PRECISION_AT_K,
        value=numerator / k_effective,
        support=support,
        numerator=float(numerator),
        denominator=float(k_effective),
    )


def recall_at_k(context: MetricRunContext, k: int) -> MetricResult:
    """Recall@K against the declared universe, or the gate's `N_A` result.

    `relevant found in the effective top K / relevant in the whole universe`.
    The denominator comes from the gate's own `relevant_count`, counted over the
    closed, fully judged `EvaluationUniverse` — so a relevant posting the
    ranking never surfaced is in the denominator and not in the numerator, which
    is exactly what recall is supposed to notice.

    `N_A / RECALL_DENOMINATOR_UNKNOWN` when the universe is not fully judged,
    and `N_A / NO_RELEVANT_ITEMS` when it is and holds nothing relevant: a known
    denominator of zero is a different fact from an unknown one, and reporting
    0.0 for either would describe the evaluation set rather than the ranking.
    """
    availability = recall_at_k_availability(context, k)
    if not availability.available:
        return availability.as_result()

    support = availability.support
    denominator = support.relevant_count
    if denominator is None or denominator <= 0:
        # Unreachable: the gate returns NO_RELEVANT_ITEMS for zero and counts
        # the universe itself. Refused rather than repaired, for the same
        # reason as above.
        raise MetricContractError(
            f"Recall@K has no usable denominator: the gate reported "
            f"relevant_count {denominator!r} and still called the metric "
            "available"
        )
    numerator = _relevant_in_top_k(context, support.k_effective)
    return _computed(
        metric=MetricName.RECALL_AT_K,
        value=numerator / denominator,
        support=support,
        numerator=float(numerator),
        denominator=float(denominator),
    )


def ndcg_at_k(context: MetricRunContext, k: int) -> MetricResult:
    """NDCG@K against the universe's ideal ordering, or the gate's `N_A` result.

    `DCG@K / IDCG@K`, both through the one DCG sum below, with the ideal
    drawn from the best `k_effective` grades of the **whole declared universe**
    — the gate has established it is fully judged, which is why NDCG needs more
    than a judged top K.

    `N_A / EVALUATION_UNIVERSE_NOT_FULLY_JUDGED` when it is not, and
    `N_A / ZERO_IDEAL_DCG` when the ideal cut-off carries no gain at all. No
    rounding and no clamping: the ratio is returned as computed.
    """
    availability = ndcg_at_k_availability(context, k)
    if not availability.available:
        return availability.as_result()

    support = availability.support
    k_effective = support.k_effective
    ranked_grades = [
        context.coverage.grade(opportunity_id)
        for opportunity_id in context.ranking.opportunity_ids[:k_effective]
    ]
    ideal_grades = _ideal_grades_at_cutoff(
        context.universe, context.coverage, k_effective
    )
    dcg = _discounted_cumulative_gain(ranked_grades)
    idcg = _discounted_cumulative_gain(ideal_grades)
    if idcg == 0.0:
        # The gate refuses this case already, so reaching it would mean the two
        # had come to disagree. The answer is the same one the gate gives —
        # never a division, never a substituted 0.0 or 1.0.
        return MetricResult(
            metric=MetricName.NDCG_AT_K,
            status=MetricStatus.N_A,
            value=None,
            reason=MetricUnavailableReason.ZERO_IDEAL_DCG,
            support=support,
        )
    return _computed(
        metric=MetricName.NDCG_AT_K,
        value=dcg / idcg,
        support=support,
        numerator=dcg,
        denominator=idcg,
    )
