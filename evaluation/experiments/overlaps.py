"""How two projections of one frozen cohort coincide. Six pairs, one function.

`compute_overlap(context, pair)` is the **only** way to obtain an
`OverlapResult`. The six pairs are a closed enum with a closed registry, the
partition derivation is private and shared with the verifier, and there is
deliberately no public `make_overlap_result(...)`.

## It consumes projection results, never records

Every overlap is computed from the two official `ProjectionResult`s — obtained
through `projections.compute_projection`, which re-verifies the whole context —
and from the `FROZEN_COHORT` projection that completes the `neither` partition.
This module reads no `record["qualification"]`, no geography segment and no
frozen block of any kind. It **never reinterprets the business records**: if it
did, there would be two readings of "out of target" in Phase 10.5 alone, and the
overlap could disagree with the projection it claims to be about.

## The four partitions

For the pair's left projection P and right projection Q, over the frozen cohort
C:

    both       = P ∩ Q
    left_only  = P \\ Q
    right_only = Q \\ P
    neither    = C \\ (P ∪ Q)

Pairwise disjoint, exhaustive over C, each holding its exact ids in Phase 10.1's
canonical order. `neither` is completed against the **cohort** rather than
against `P ∪ Q`, which is why the reference projection exists: "in neither" is a
statement about every posting in the snapshot, and a union has nothing outside
it.

## Two rates, both stated, neither averaged

    right_among_left = |P ∩ Q| / |P|
    left_among_right = |P ∩ Q| / |Q|

Asymmetric, and neither implies the other. A single "overlap rate" — or a mean
of the two — would hide which side the asymmetry is on, which is usually the
only interesting thing about it.

A rate whose reference projection is **empty** is
`N_A / EMPTY_REFERENCE_PROJECTION` with `numerator=0`, `denominator=0` and no
value. The overlap itself stays `COMPUTED`: its four partitions are perfectly
well defined over an empty projection, and `0/0` reported as `0.0` would be a
measurement of nothing.

## An empty projection is not an unavailable one

The distinction this module turns on. A `COMPUTED` projection that includes
nobody is a **fact** — the pipeline included nothing — and its overlaps are
computable, with the two rates refusing only where they would divide by zero. An
`N_A` projection is the **absence** of a fact, and then the whole overlap is
`N_A / INPUT_PROJECTION_UNAVAILABLE` and states no partition at all. Fabricating
four empty sets there would assert that nobody is in either projection, which is
exactly the measurement that could not be made.

## No causal vocabulary

Nothing here names a conversion, a drop-off, a retention, a funnel stage or an
improvement caused by anything. The six pairs are six statements about how two
independent readings of one cohort coincide, and that is the whole of what an
overlap can say.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from .context import ExperimentRunContext, _VerifiedExperimentContext, _verified
from .fingerprint import (
    EXPERIMENT_PLACEHOLDER_FINGERPRINT,
    overlap_result_fingerprint,
    partitions_against,
    verify_overlap_against_projections,
    verify_overlap_result_fingerprint,
)
from .projections import compute_projection
from .schema import (
    EXPERIMENT_CONTRACT_VERSION,
    OVERLAP_PAIR_ORDER,
    OVERLAP_PAIR_PROJECTIONS,
    OVERLAP_RESULT_SCHEMA_VERSION,
    CohortProjection,
    DirectionalRate,
    DirectionalRateName,
    ExperimentBindingError,
    ExperimentContractError,
    OverlapPair,
    OverlapResult,
    OverlapStatus,
    OverlapUnavailableReason,
    ProjectionResult,
    ProjectionStatus,
    overlap_pair_projections,
    require_supported_experiment_contract_version,
    validate_overlap_result_structure,
)

__all__ = ["compute_overlap"]


# --------------------------------------------------------------------------
# the directional rates — private, derived, never supplied
# --------------------------------------------------------------------------


def _directional_rate(
    name: DirectionalRateName, *, numerator: int, denominator: int
) -> DirectionalRate:
    """One direction of one overlap, from the partition's own two counts.

    A zero denominator is the reference projection being empty, and that is a
    stated refusal rather than a division: `numerator=0`, `denominator=0`,
    `value=None`. Both counts are *facts* there — an intersection with an empty
    set really is empty — which is what distinguishes this from a projection
    whose membership could not be computed at all.
    """
    if denominator == 0:
        return DirectionalRate(
            name=name,
            status=OverlapStatus.N_A,
            unavailable_reason=(
                OverlapUnavailableReason.EMPTY_REFERENCE_PROJECTION
            ),
            numerator=0,
            denominator=0,
            value=None,
        )
    return DirectionalRate(
        name=name,
        status=OverlapStatus.COMPUTED,
        numerator=numerator,
        denominator=denominator,
        # Derived, never passed in, and never rounded.
        value=numerator / denominator,
    )


# --------------------------------------------------------------------------
# the sealer — private, and the one place an overlap is assembled
# --------------------------------------------------------------------------


def _sealed(
    verified: _VerifiedExperimentContext,
    pair: OverlapPair,
    left: ProjectionResult,
    right: ProjectionResult,
    **fields: Any,
) -> OverlapResult:
    """Assemble, structurally validate, digest and re-verify. The one exit."""
    left_projection, right_projection = overlap_pair_projections(pair)
    draft = OverlapResult(
        result_schema_version=OVERLAP_RESULT_SCHEMA_VERSION,
        contract_version=require_supported_experiment_contract_version(
            EXPERIMENT_CONTRACT_VERSION
        ),
        pair=pair,
        dataset_id=verified.dataset.dataset_id,
        dataset_content_fingerprint=verified.dataset.content_fingerprint,
        cohort_size=verified.cohort_size,
        left_projection=left_projection,
        right_projection=right_projection,
        # The inputs enter by digest, as nested artefacts do throughout Phase
        # 10, and each has been recomputed by `compute_projection` before
        # arriving here.
        left_projection_result_fingerprint=left.result_fingerprint,
        right_projection_result_fingerprint=right.result_fingerprint,
        result_fingerprint=EXPERIMENT_PLACEHOLDER_FINGERPRINT,
        **fields,
    )
    validate_overlap_result_structure(draft)
    result = replace(draft, result_fingerprint=overlap_result_fingerprint(draft))
    # Re-established through the same two verifiers a stored overlap faces: its
    # own digest, and its partitions re-derived from the two projections.
    verify_overlap_result_fingerprint(result)
    verify_overlap_against_projections(result, left, right, verified.cohort_ids)
    return result


def _computed(
    verified: _VerifiedExperimentContext,
    pair: OverlapPair,
    left: ProjectionResult,
    right: ProjectionResult,
) -> OverlapResult:
    """The four partitions and the two rates of two available memberships.

    The partitions come from `partitions_against`, the **one** derivation, which
    the structural verifier also calls. A second implementation here is exactly
    the drift that would let the verifier certify its own mistake.
    """
    partitions = partitions_against(verified.cohort_ids, left, right)
    both = partitions["both"]
    left_only = partitions["left_only"]
    right_only = partitions["right_only"]
    neither = partitions["neither"]
    return _sealed(
        verified,
        pair,
        left,
        right,
        status=OverlapStatus.COMPUTED,
        both_opportunity_ids=both,
        left_only_opportunity_ids=left_only,
        right_only_opportunity_ids=right_only,
        neither_opportunity_ids=neither,
        both_count=len(both),
        left_only_count=len(left_only),
        right_only_count=len(right_only),
        neither_count=len(neither),
        right_among_left=_directional_rate(
            DirectionalRateName.RIGHT_AMONG_LEFT,
            numerator=len(both),
            denominator=len(both) + len(left_only),
        ),
        left_among_right=_directional_rate(
            DirectionalRateName.LEFT_AMONG_RIGHT,
            numerator=len(both),
            denominator=len(both) + len(right_only),
        ),
    )


def _unavailable(
    verified: _VerifiedExperimentContext,
    pair: OverlapPair,
    left: ProjectionResult,
    right: ProjectionResult,
) -> OverlapResult:
    """One `N_A` overlap: a reason, and no fabricated partition.

    Every partition, every count and both rates stay absent. Four empty sets
    would assert that nobody is in either projection — a measurement — and the
    point of the refusal is that at least one of the two memberships does not
    exist to be intersected.
    """
    return _sealed(
        verified,
        pair,
        left,
        right,
        status=OverlapStatus.N_A,
        unavailable_reason=(
            OverlapUnavailableReason.INPUT_PROJECTION_UNAVAILABLE
        ),
    )


#: The closed registry of the six pairs. Not a mapping to resolvers — every
#: overlap is computed by the same two functions above, because an overlap is
#: pure set arithmetic over two memberships and a pair-specific resolver would
#: be a place for one pair to mean something slightly different. What the
#: registry fixes is *which* projections each pair is of, and in which order.
_OVERLAP_INPUTS: Mapping[
    OverlapPair, tuple[CohortProjection, CohortProjection]
] = OVERLAP_PAIR_PROJECTIONS

assert set(_OVERLAP_INPUTS) == set(OverlapPair), (
    "every overlap pair needs exactly one pair of projections"
)
assert tuple(OVERLAP_PAIR_ORDER) == tuple(OverlapPair), (
    "the canonical overlap order covers the closed enum exactly once"
)


# --------------------------------------------------------------------------
# the one public surface
# --------------------------------------------------------------------------


def compute_overlap(
    context: ExperimentRunContext, pair: OverlapPair
) -> OverlapResult:
    """Compare two projections of one frozen cohort, or refuse.

    The single public way to obtain an `OverlapResult`. It re-verifies the whole
    context, obtains the pair's two projections and the reference cohort through
    the **public** `compute_projection` — so an overlap is never computed from a
    membership that did not go through the projection authority — and then
    performs pure set arithmetic over them.

    An `N_A` input makes the whole overlap `N_A`; an *empty but available* input
    does not. That distinction is the reason this function reads statuses rather
    than lengths.
    """
    if not isinstance(pair, OverlapPair):
        raise ExperimentContractError(
            f"{pair!r} is not an overlap pair; this contract defines "
            f"{[str(item) for item in OverlapPair]}"
        )
    verified = _verified(context)
    left_projection, right_projection = overlap_pair_projections(pair)
    left = compute_projection(context, left_projection)
    right = compute_projection(context, right_projection)
    reference = compute_projection(context, CohortProjection.FROZEN_COHORT)

    # The reference projection is the cohort, and it is checked rather than
    # assumed: a `FROZEN_COHORT` that had somehow lost a record would silently
    # shrink every `neither` partition in the run.
    if not reference.computed:
        raise ExperimentContractError(
            f"the reference projection {CohortProjection.FROZEN_COHORT} is "
            f"{reference.status}; it is the whole frozen cohort and is always "
            "computable"
        )
    if reference.included_ids != verified.cohort_ids:
        raise ExperimentBindingError(
            f"the reference projection holds {reference.included_count} "
            f"opportunity id(s) and the verified cohort holds "
            f"{verified.cohort_size}"
        )

    if (
        left.status is ProjectionStatus.N_A
        or right.status is ProjectionStatus.N_A
    ):
        return _unavailable(verified, pair, left, right)
    return _computed(verified, pair, left, right)
