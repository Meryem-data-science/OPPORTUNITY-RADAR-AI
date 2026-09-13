"""The five projections of a frozen cohort. One public function, one registry.

`compute_projection(context, projection)` is the **only** way to obtain a
`ProjectionResult`. Every projection reaches it through one closed registry, the
resolvers are private, and there is deliberately no public
`make_projection_result(...)` and no sealer taking a membership, a count or a
share: a caller who could reach either would have a second way to publish a
projection — one that had not re-verified the context and had not gone through
the registry.

## The trust boundary is re-established on every call

`ExperimentRunContext` is an ordinary dataclass, so holding one proves nothing.
`compute_projection` re-verifies it **every time**, before reading a record:
the frozen dataset's cohort, the bound Phase 10.4 run recomputed in full over
these very records, D42's five bindings, and the ranking inputs held against the
presence of an observed recommendation. Only then do the records become
readable.

## Every value is derived, and nothing else can be

One path produces every `COMPUTED` result:

    verified records -> the resolver's included id set
                     -> canonical order -> included/excluded counts
                     -> cohort_share = included / cohort_size
                     -> the private sealer

The membership is the resolver's, the counts are derived from it, and the share
is derived from the counts. No public function accepts a membership, a count, a
share, a status or a reason, so a projection nobody computed cannot be published
with a valid digest around it. No rounding anywhere: a share is the quotient.

## Where each rule comes from, and where it does not

The two *not explicitly out* questions read `evaluation.frozen_facts`, the
module Phase 10.4's own geography and Data/AI rates read. That is the whole
point of D45: the rule "an unplaced posting is not out of target" and the rule
"an unread posting is not out of scope" exist **once**, and neither phase owns a
copy. Phase 10.4's private helpers are not imported here and not reimplemented
here; both phases call the same primitives.

The two presence questions read the frozen block's presence and nothing else. No
lane, no `match_quality`, no `evidence_coverage`, no score, no disposition, no
threshold. A projection that applied a quality bar would be measuring the bar.

Nothing here consults `record["country"]`, `record["location"]`, a live
geography resolver, a live classifier, a database, a clock or a default country.
The target comes from the verified `ProfileTargetBindingEvidence` of the bound
Phase 10.4 run; with none, the answer is `N_A / TARGET_COUNTRY_UNAVAILABLE` and
never a fallback.

## Independent projections, never a funnel

Each resolver is handed the **whole verified cohort**. None of them sees another
projection's output, and none of them could: `MATCHING_OBSERVED` is every record
whose matching block is present, not "what survived the geography question". The
five are five readings of one cohort, and reading them as stages would turn four
independent membership facts into a causal story the data does not support.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Callable

from evaluation.frozen_facts import (
    FrozenDataAiState,
    FrozenTargetVerdict,
    data_ai_state_of,
    frozen_optional_block,
    target_verdict_of,
)

from .context import ExperimentRunContext, _VerifiedExperimentContext, _verified
from .fingerprint import (
    EXPERIMENT_PLACEHOLDER_FINGERPRINT,
    projection_result_fingerprint,
    verify_projection_result_fingerprint,
)
from .schema import (
    EXPERIMENT_CONTRACT_VERSION,
    PROJECTION_ORDER,
    PROJECTION_RESULT_SCHEMA_VERSION,
    CohortProjection,
    ExperimentBindingError,
    ExperimentContractError,
    ProjectionResult,
    ProjectionStatus,
    ProjectionUnavailableReason,
    canonical_experiment_opportunity_ids,
    require_supported_experiment_contract_version,
    validate_projection_result_structure,
)

__all__ = ["compute_projection"]


def _record_subject(position: int) -> str:
    """How this package names one record in a refusal. Spelled once."""
    return f"frozen record {position}"


# --------------------------------------------------------------------------
# the sealer — private, and the one place a result is assembled
# --------------------------------------------------------------------------


def _computed(
    verified: _VerifiedExperimentContext,
    projection: CohortProjection,
    included: tuple[int, ...],
) -> ProjectionResult:
    """One COMPUTED projection result, with its counts and share derived.

    **Private on purpose.** A public function taking a membership would be an
    API for publishing a projection nobody computed, with a perfectly valid
    fingerprint and a complete-looking set of counts around it.

    The membership is put into Phase 10.1's canonical order here rather than
    trusted from the resolver, and every id is required to be in the cohort: a
    projection naming a posting the snapshot does not hold is a binding failure,
    not a wider projection.
    """
    ordered = canonical_experiment_opportunity_ids(included)
    if len(set(ordered)) != len(ordered):
        raise ExperimentBindingError(
            f"{projection} included an opportunity twice; a membership holds "
            "each posting once"
        )
    cohort = set(verified.cohort_ids)
    stray = sorted(set(ordered) - cohort)
    if stray:
        raise ExperimentBindingError(
            f"{projection} includes {stray}, which dataset "
            f"{verified.dataset.dataset_id} does not hold; a projection is a "
            "subset of the cohort it projects"
        )
    included_count = len(ordered)
    cohort_size = verified.cohort_size
    return _sealed(
        verified,
        projection,
        status=ProjectionStatus.COMPUTED,
        included_opportunity_ids=ordered,
        included_count=included_count,
        excluded_count=cohort_size - included_count,
        # Derived, never passed in, and never rounded: a share pulled to two
        # decimals is a number nobody can re-derive.
        cohort_share=included_count / cohort_size,
    )


def _unavailable(
    verified: _VerifiedExperimentContext,
    projection: CohortProjection,
    reason: ProjectionUnavailableReason,
) -> ProjectionResult:
    """One `N_A` projection result: a reason, and no fabricated membership.

    Every membership and count field stays absent. An empty included set would
    assert that the pipeline included nobody, which is a measurement; the point
    of the refusal is that no measurement was possible.
    """
    return _sealed(
        verified, projection, status=ProjectionStatus.N_A, unavailable_reason=reason
    )


def _sealed(
    verified: _VerifiedExperimentContext,
    projection: CohortProjection,
    **fields: Any,
) -> ProjectionResult:
    """Assemble, structurally validate, digest and re-verify. The one exit."""
    draft = ProjectionResult(
        result_schema_version=PROJECTION_RESULT_SCHEMA_VERSION,
        contract_version=require_supported_experiment_contract_version(
            EXPERIMENT_CONTRACT_VERSION
        ),
        projection=projection,
        dataset_id=verified.dataset.dataset_id,
        dataset_content_fingerprint=verified.dataset.content_fingerprint,
        cohort_size=verified.cohort_size,
        result_fingerprint=EXPERIMENT_PLACEHOLDER_FINGERPRINT,
        **fields,
    )
    validate_projection_result_structure(draft)
    result = replace(
        draft, result_fingerprint=projection_result_fingerprint(draft)
    )
    # Re-established through the same verifier a stored result faces, so nothing
    # leaves here that would fail verification the moment it was read back.
    verify_projection_result_fingerprint(result)
    return result


# --------------------------------------------------------------------------
# the five resolvers — private, one per projection
# --------------------------------------------------------------------------


def _resolve_frozen_cohort(
    verified: _VerifiedExperimentContext,
) -> ProjectionResult:
    """Every record in the snapshot. The reference projection.

    Its `opportunity_id ASC` order is a **serialization** and is never a
    ranking: this projection is the set every share is taken of and every
    overlap's `neither` partition is completed against, and there is no
    Precision@K over it because a set has no order to be precise about.
    """
    return _computed(
        verified, CohortProjection.FROZEN_COHORT, verified.cohort_ids
    )


def _resolve_geo_not_explicitly_out_of_target(
    verified: _VerifiedExperimentContext,
) -> ProjectionResult:
    """The postings the pipeline did not explicitly place outside the target.

        MATCH         -> INCLUDE
        UNKNOWN       -> INCLUDE
        OUT_OF_TARGET -> EXCLUDE

    The asymmetry is the `UNKNOWN != FALSE` rule in its sharpest form. A posting
    the resolver could not place, or placed ambiguously, **is included**: nobody
    established it was elsewhere, and excluding it would read uncertainty as a
    negative verdict. The excluded set is exactly the set the resolver made an
    explicit negative statement about — segments present, every one resolved,
    none to the target.

    With no target country there is no question to ask, and the answer is
    `N_A / TARGET_COUNTRY_UNAVAILABLE`. **No fallback and no hardcoded country**:
    not `record["country"]`, not a location string, not a source map's scope,
    not "MA".
    """
    target = verified.target_country
    if target is None:
        return _unavailable(
            verified,
            CohortProjection.GEO_NOT_EXPLICITLY_OUT_OF_TARGET,
            ProjectionUnavailableReason.TARGET_COUNTRY_UNAVAILABLE,
        )
    included = [
        int(record["opportunity_id"])
        for position, record in enumerate(verified.records, start=1)
        if target_verdict_of(
            record,
            target,
            record_subject=_record_subject(position),
            refuse=ExperimentBindingError,
        )
        is not FrozenTargetVerdict.OUT_OF_TARGET
    ]
    return _computed(
        verified, CohortProjection.GEO_NOT_EXPLICITLY_OUT_OF_TARGET, tuple(included)
    )


def _resolve_data_ai_not_explicitly_out_of_scope(
    verified: _VerifiedExperimentContext,
) -> ProjectionResult:
    """The postings the Data/AI classifier did not explicitly rule out.

        CORE_TARGET     -> INCLUDE
        ADJACENT_TARGET -> INCLUDE
        UNCERTAIN       -> INCLUDE
        UNCLASSIFIED    -> INCLUDE
        OUT_OF_SCOPE    -> EXCLUDE

    `qualification is None` is `UNCLASSIFIED` and is therefore **included**: the
    classifier never read the posting, so it never ruled it out. Folding that
    into `OUT_OF_SCOPE` would report how far the pipeline has run as a verdict
    about the market, which is the single most consequential confusion available
    here.

    This projection has no `N_A`. The question is answerable from the frozen
    records alone, and a posting with no qualification block answers it.
    """
    included = [
        int(record["opportunity_id"])
        for position, record in enumerate(verified.records, start=1)
        if data_ai_state_of(
            record,
            record_subject=_record_subject(position),
            refuse=ExperimentBindingError,
        )
        is not FrozenDataAiState.OUT_OF_SCOPE
    ]
    return _computed(
        verified,
        CohortProjection.DATA_AI_NOT_EXPLICITLY_OUT_OF_SCOPE,
        tuple(included),
    )


def _present_block_ids(
    verified: _VerifiedExperimentContext, field: str
) -> tuple[int, ...]:
    """The ids whose frozen `field` block is present. **Presence, never quality.**

    The one derivation both presence projections use, so `MATCHING_OBSERVED` and
    `RECOMMENDATION_OBSERVED` cannot come to mean two different things by
    "observed". A block that is present but is not an object is a hard error —
    the snapshot states something this build cannot read, which is a
    contradiction rather than an absence.
    """
    return tuple(
        int(record["opportunity_id"])
        for position, record in enumerate(verified.records, start=1)
        if frozen_optional_block(
            record,
            field,
            record_subject=_record_subject(position),
            refuse=ExperimentBindingError,
        )
        is not None
    )


def _resolve_matching_observed(
    verified: _VerifiedExperimentContext,
) -> ProjectionResult:
    """The postings the frozen Matching run assessed at all.

        matching is not None -> INCLUDE
        matching is None     -> EXCLUDE

    **Presence only.** No filter on `lane`, none on `match_quality` and none on
    `evidence_coverage` — including the Phase 4 state where `match_quality is
    None` with `evidence_coverage == 0.0`, which is an assessment that measured
    nothing and is still an assessment. A projection that dropped it would be
    measuring a quality bar nobody declared.
    """
    return _computed(
        verified,
        CohortProjection.MATCHING_OBSERVED,
        _present_block_ids(verified, "matching"),
    )


def _resolve_recommendation_observed(
    verified: _VerifiedExperimentContext,
) -> ProjectionResult:
    """The postings the frozen Recommendation run assessed at all.

        recommendation is not None -> INCLUDE
        recommendation is None     -> EXCLUDE

    **Presence only.** No score, no disposition and no threshold: an
    `UNCERTAIN` or a rejected recommendation is still a recommendation the run
    produced. A posting the run never covered is excluded — it is the candidate
    false negative the snapshot exists to preserve, and it is emphatically not
    given a rank at the bottom of a list.

    The membership derived here is the same tuple the verified context already
    holds, which is what lets the ranking block require
    `set(ranking.opportunity_ids) == set(included)` and mean it.
    """
    included = _present_block_ids(verified, "recommendation")
    if included != verified.observed_recommendation_ids:
        # Two derivations of one fact, held against each other. Reaching this
        # would mean the context's reading and this resolver's had diverged,
        # which is a bug to surface rather than a discrepancy to pick a side in.
        raise ExperimentBindingError(
            f"the verified context observed {len(verified.observed_recommendation_ids)} "
            f"recommendation assessment(s) and this projection derives "
            f"{len(included)}"
        )
    return _computed(
        verified, CohortProjection.RECOMMENDATION_OBSERVED, included
    )


#: The closed registry, exhaustive over `CohortProjection` by construction and
#: by the assertion below. A projection with no resolver would be a member of
#: the enum that no code answers, which is how an "optional projection" gets in.
_PROJECTION_RESOLVERS: Mapping[
    CohortProjection, Callable[[_VerifiedExperimentContext], ProjectionResult]
] = {
    CohortProjection.FROZEN_COHORT: _resolve_frozen_cohort,
    CohortProjection.GEO_NOT_EXPLICITLY_OUT_OF_TARGET: (
        _resolve_geo_not_explicitly_out_of_target
    ),
    CohortProjection.DATA_AI_NOT_EXPLICITLY_OUT_OF_SCOPE: (
        _resolve_data_ai_not_explicitly_out_of_scope
    ),
    CohortProjection.MATCHING_OBSERVED: _resolve_matching_observed,
    CohortProjection.RECOMMENDATION_OBSERVED: _resolve_recommendation_observed,
}

# Stated at import time, because a missing resolver is a contract hole rather
# than a runtime accident: it would surface as a `KeyError` on one projection of
# one run, long after the member was added.
assert set(_PROJECTION_RESOLVERS) == set(CohortProjection), (
    "every cohort projection needs exactly one resolver"
)
assert tuple(_PROJECTION_RESOLVERS) == PROJECTION_ORDER, (
    "the resolver registry is held in the canonical projection order"
)


# --------------------------------------------------------------------------
# the one public surface
# --------------------------------------------------------------------------


def compute_projection(
    context: ExperimentRunContext, projection: CohortProjection
) -> ProjectionResult:
    """Project one frozen cohort onto one membership question, or refuse.

    The single public way to obtain a `ProjectionResult`. It re-verifies the
    whole context on **every** call — the cohort, the bound Phase 10.4 run
    recomputed over these records, D42's bindings, the ranking inputs — and only
    then dispatches through the closed registry.

    Re-verifying per projection costs five full Phase 10.4 verifications per
    run, which is the intended trade: recomputing twenty-eight metrics over a
    few hundred records is cheap, and a projection that silently trusted its
    inputs is not. It is also what makes `build_experiment_run` honest — it
    reaches every block through this exact function rather than through a
    privileged internal path, so a run is assembled the way any other caller
    would assemble one.
    """
    if not isinstance(projection, CohortProjection):
        raise ExperimentContractError(
            f"{projection!r} is not a cohort projection; this contract defines "
            f"{[str(item) for item in CohortProjection]}"
        )
    verified = _verified(context)
    resolver = _PROJECTION_RESOLVERS[projection]
    result = resolver(verified)
    if not isinstance(result, ProjectionResult):
        raise ExperimentContractError(
            f"resolving {projection} produced {result!r}, which is not a "
            "projection result"
        )
    if result.projection is not projection:
        raise ExperimentBindingError(
            f"{projection} was asked and the result is about {result.projection}"
        )
    return result
