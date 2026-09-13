"""The cryptographic identities of a Phase 10.5 experiment.

Same primitive as every phase before this one — the project's single
`canonical_json` (sorted keys, no insignificant whitespace, UTF-8), digested
with SHA-256. Nothing is invented here; only the domains are new, and each is
stated in full, because a digest whose domain is implicit is a digest nobody can
reason about.

## Four identities, four questions

    projection result   which postings did this projection include?
    overlap result      how do these two projections coincide, exactly?
    ranking result      what did the four questions answer, over which ranking?
    run                 which twelve blocks, over which assembly of artefacts?

and one more that is not a result at all:

    context             which frozen artefacts did this run have to work with?

## Memberships enter by value, nested artefacts by digest

The rule Phase 10 applies everywhere is that a nested artefact enters a domain
by its own digest, recomputed first. Phase 10.5 keeps it — a projection enters an
overlap by digest, and every block enters the run by digest — with one
deliberate exception in each direction.

**Memberships enter in full.** A projection's `included_opportunity_ids` and an
overlap's four partitions are inside their own digests as the complete id lists,
not as counts. That is the point: two projections of the same size over
different postings are different projections, and an identity covering only the
counts would let a membership be edited under a valid digest. So identical
counts with different ids produce different fingerprints, and a test asserts it.

**The four Phase 10.3 metric results enter in full**, through Phase 10.3's own
`metric_result_payload`. A `MetricResult` carries no fingerprint field of its
own, and minting one here would create a Phase 10.5 identity for a Phase 10.3
artefact — a second name for the same thing, free to be computed over a
different domain and free to drift from it. The ranking result's digest
therefore covers the four payloads entire: value, status, reason and support
block.

## The context domain is exactly semantic

    run_context_schema_version
    experiment_contract_version
    dataset_id / dataset_content_fingerprint
    profile_id / profile_context_fingerprint
    business_metric_run_fingerprint
    ranking_evaluation_run_fingerprint | null

**Outside it, and each for a reason**: the frozen records and the raw manifest,
because the dataset enters by the digest that already covers them; the label
coverage's grades, because the evaluation run's own fingerprint already binds
the labelset; the benchmark rows, because they are transient evidence for
verifying somebody else's binding and republishing them would make every run a
copy of a gold file; the provenance and every path, because where a file was
read is not what it says; and every Phase 10.5 output, because a context says
what a run *had*, never what it produced. A context whose digest moved when a
projection did would be unable to answer whether two runs measured the same
thing.

## Nothing declares its own identity

Following Phase 10.1, a stored object's self-declared fingerprint is never
trusted: each `verify_*` below **re-establishes the structure first** and then
recomputes the digest. Both halves, in that order, because either alone is
insufficient — a digest check catches an artefact that changed after somebody
digested it, and a structural check catches one that was already wrong when they
did. An overlap whose partitions share a member, re-digested, has a perfectly
valid fingerprint over an artefact nobody should read.

This module opens no file, opens no database connection and makes no request.
"""

from __future__ import annotations

import hashlib
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from .schema import (
    ExperimentBindingError,
    ExperimentRun,
    ExperimentRunContextBinding,
    OverlapResult,
    ProjectionResult,
    ProjectionStatus,
    RankingExperimentResult,
    experiment_run_context_binding_payload,
    experiment_run_fingerprint_payload,
    overlap_result_payload,
    projection_result_payload,
    ranking_experiment_result_payload,
    validate_experiment_run_structure,
    validate_overlap_result_structure,
    validate_projection_result_structure,
    validate_ranking_experiment_result_structure,
)

__all__ = [
    "EXPERIMENT_PLACEHOLDER_FINGERPRINT",
    "canonical_experiment_run_context_payload",
    "canonical_experiment_run_payload",
    "canonical_overlap_result_payload",
    "canonical_projection_result_payload",
    "canonical_ranking_experiment_result_payload",
    "experiment_run_context_fingerprint",
    "experiment_run_fingerprint",
    "overlap_result_fingerprint",
    "partitions_against",
    "projection_result_fingerprint",
    "ranking_experiment_result_fingerprint",
    "verify_experiment_run_context_fingerprint",
    "verify_experiment_run_fingerprint",
    "verify_overlap_against_projections",
    "verify_overlap_result_fingerprint",
    "verify_projection_result_fingerprint",
    "verify_ranking_experiment_result_fingerprint",
]

#: The value a draft carries in its own fingerprint field before it is sealed.
#: Never a valid digest of anything: sixty-four zeroes is a well-formed SHA-256
#: shape that no content produces, so a draft that escaped unsealed fails
#: verification rather than passing as some other artefact.
EXPERIMENT_PLACEHOLDER_FINGERPRINT = "0" * 64


def _digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _refuse(subject: str, claimed: str, recomputed: str) -> None:
    raise ExperimentBindingError(
        f"{subject} claims fingerprint {claimed} but its content digests to "
        f"{recomputed}; refusing an artefact that is not what it says it is"
    )


# --------------------------------------------------------------------------
# the projection result
# --------------------------------------------------------------------------


def canonical_projection_result_payload(
    result: ProjectionResult,
) -> dict[str, Any]:
    """A projection result's digest domain: the whole of what it states.

    The membership is digested **in full**, not by count — see the module
    docstring. `result_fingerprint` is outside it, because it is derived from
    this domain and including it would be circular.
    """
    return projection_result_payload(result, include_result_fingerprint=False)


def projection_result_fingerprint(result: ProjectionResult) -> str:
    """SHA-256 of the canonical JSON of everything a projection result says."""
    return _digest(canonical_projection_result_payload(result))


def verify_projection_result_fingerprint(result: ProjectionResult) -> str:
    """Re-establish the structure, then recompute the digest, or refuse.

    The structural half re-derives the counts from the membership and the share
    from the counts, so a result somebody edited and re-digested — a share
    adjusted, an id removed, a count left behind — is refused here rather than
    silently entering an overlap and a report.
    """
    validate_projection_result_structure(result)
    recomputed = projection_result_fingerprint(result)
    if recomputed != result.result_fingerprint:
        _refuse(
            f"projection {result.projection}", result.result_fingerprint, recomputed
        )
    return recomputed


# --------------------------------------------------------------------------
# the overlap result
# --------------------------------------------------------------------------


def canonical_overlap_result_payload(result: OverlapResult) -> dict[str, Any]:
    """An overlap result's digest domain: four partitions and two rates.

    The partitions are digested in full and the two input projections enter by
    their own recomputed digests, so an overlap's identity moves when either
    membership moves — even when every count it states stays the same.
    """
    return overlap_result_payload(result, include_result_fingerprint=False)


def overlap_result_fingerprint(result: OverlapResult) -> str:
    """SHA-256 of the canonical JSON of everything an overlap result says."""
    return _digest(canonical_overlap_result_payload(result))


def verify_overlap_result_fingerprint(result: OverlapResult) -> str:
    """Re-establish the structure, then recompute the digest, or refuse."""
    validate_overlap_result_structure(result)
    recomputed = overlap_result_fingerprint(result)
    if recomputed != result.result_fingerprint:
        _refuse(f"overlap {result.pair}", result.result_fingerprint, recomputed)
    return recomputed


def verify_overlap_against_projections(
    result: OverlapResult,
    left: ProjectionResult,
    right: ProjectionResult,
    cohort_ids: tuple[int, ...],
) -> OverlapResult:
    """Re-derive an overlap from two stored projections, or refuse it.

    This is the check that makes a stored overlap more than a self-consistent
    document. `validate_overlap_result_structure` establishes that the four
    partitions are disjoint, cover the cohort and imply the stated rates — all
    of which a forged overlap can satisfy while describing memberships that are
    not the ones the run's own projections state. Here the partitions are
    **recomputed** from those projections and compared exactly.

    `cohort_ids` is the reference set the `neither` partition is completed
    against, and it is a parameter rather than something derived from the two
    memberships for the reason `partitions_against` gives: "in neither" is a
    statement about every posting in the snapshot, and two projections' union
    has nothing outside it. A caller passes the run's own `FROZEN_COHORT`
    membership, which the run has already had verified.

    It needs no records, which is why it belongs to the structural verification:
    the projections are already in the run, and whether *they* are right about
    the snapshot is the full verifier's question.
    """
    verify_projection_result_fingerprint(left)
    verify_projection_result_fingerprint(right)
    validate_overlap_result_structure(result)

    if left.projection is not result.left_projection:
        raise ExperimentBindingError(
            f"overlap {result.pair} names left projection "
            f"{result.left_projection} and was offered {left.projection}"
        )
    if right.projection is not result.right_projection:
        raise ExperimentBindingError(
            f"overlap {result.pair} names right projection "
            f"{result.right_projection} and was offered {right.projection}"
        )
    for side, projection, stated in (
        ("left", left, result.left_projection_result_fingerprint),
        ("right", right, result.right_projection_result_fingerprint),
    ):
        if projection.result_fingerprint != stated:
            raise ExperimentBindingError(
                f"overlap {result.pair} rests on {side} projection {stated} and "
                f"the run's {projection.projection} states "
                f"{projection.result_fingerprint}"
            )

    unavailable = (
        left.status is ProjectionStatus.N_A or right.status is ProjectionStatus.N_A
    )
    if unavailable:
        if result.computed:
            raise ExperimentBindingError(
                f"overlap {result.pair} is COMPUTED and one of its input "
                "projections is N_A; there is no membership to intersect"
            )
        # An N_A overlap states no partition, which the structure validator has
        # already established. There is nothing left to recompute.
        return result
    if not result.computed:
        raise ExperimentBindingError(
            f"overlap {result.pair} is {result.status} / "
            f"{result.unavailable_reason} and both its input projections are "
            "COMPUTED; an overlap of two available memberships is available"
        )

    if left.cohort_size != result.cohort_size or right.cohort_size != (
        result.cohort_size
    ):
        raise ExperimentBindingError(
            f"overlap {result.pair} is over a cohort of {result.cohort_size} and "
            f"its projections state {left.cohort_size} and {right.cohort_size}"
        )
    if len(cohort_ids) != result.cohort_size:
        raise ExperimentBindingError(
            f"overlap {result.pair} is over a cohort of {result.cohort_size} and "
            f"the reference membership offered holds {len(cohort_ids)}"
        )
    expected = partitions_against(cohort_ids, left, right)
    for name, ids in expected.items():
        stored = getattr(result, f"{name}_opportunity_ids")
        if tuple(stored) != ids:
            raise ExperimentBindingError(
                f"the `{name}` partition of {result.pair} holds {list(stored)} "
                f"and its two projections imply {list(ids)}"
            )
    return result


def partitions_against(
    cohort_ids: tuple[int, ...],
    left: ProjectionResult,
    right: ProjectionResult,
) -> dict[str, tuple[int, ...]]:
    """The four partitions two available projections imply over one cohort.

    **One derivation**, shared by `overlaps.compute_overlap` and by the
    structural verifier, so the two cannot come to hold different opinions of
    what `left_only` means — a second implementation is exactly the drift that
    would let a verifier certify its own mistake.

        both       = P ∩ Q
        left_only  = P \\ Q
        right_only = Q \\ P
        neither    = C \\ (P ∪ Q)

    `neither` is completed against the **frozen cohort** rather than against the
    union of the two projections: "in neither" is a statement about every
    posting in the snapshot, and a union has nothing outside it.

    Every partition comes back in Phase 10.1's canonical order, and each input
    membership is required to be a subset of the cohort — a projection naming a
    posting the snapshot does not hold is a binding failure, not a fifth
    partition.
    """
    cohort = set(cohort_ids)
    left_ids = set(left.included_ids)
    right_ids = set(right.included_ids)
    for projection, ids in ((left, left_ids), (right, right_ids)):
        stray = sorted(ids - cohort)
        if stray:
            raise ExperimentBindingError(
                f"{projection.projection} includes {stray}, which the frozen "
                "cohort does not hold; a projection is a subset of the cohort "
                "it projects"
            )
    return {
        "both": tuple(sorted(left_ids & right_ids)),
        "left_only": tuple(sorted(left_ids - right_ids)),
        "right_only": tuple(sorted(right_ids - left_ids)),
        "neither": tuple(sorted(cohort - (left_ids | right_ids))),
    }


# --------------------------------------------------------------------------
# the ranking experiment result
# --------------------------------------------------------------------------


def canonical_ranking_experiment_result_payload(
    result: RankingExperimentResult,
) -> dict[str, Any]:
    """The ranking result's digest domain: the bindings and the four answers.

    The four Phase 10.3 `MetricResult`s enter **in full** rather than by a
    digest of their own — see the module docstring — so this identity moves when
    any value, status, reason or support number moves, and when any of the five
    bindings or the evidence class does.
    """
    return ranking_experiment_result_payload(
        result, include_result_fingerprint=False
    )


def ranking_experiment_result_fingerprint(result: RankingExperimentResult) -> str:
    """SHA-256 of the canonical JSON of the whole ranking experiment result."""
    return _digest(canonical_ranking_experiment_result_payload(result))


def verify_ranking_experiment_result_fingerprint(
    result: RankingExperimentResult,
) -> str:
    """Re-establish the structure, then recompute the digest, or refuse."""
    validate_ranking_experiment_result_structure(result)
    recomputed = ranking_experiment_result_fingerprint(result)
    if recomputed != result.result_fingerprint:
        _refuse(
            "the ranking experiment result", result.result_fingerprint, recomputed
        )
    return recomputed


# --------------------------------------------------------------------------
# the run context binding
# --------------------------------------------------------------------------


def canonical_experiment_run_context_payload(
    binding: ExperimentRunContextBinding,
) -> dict[str, Any]:
    """The context's digest domain: exactly the eight semantic fields.

    Stated as a structure a test can assert on, so that "the domain excludes the
    records" is a property somebody can check rather than a sentence in a
    docstring. `context_fingerprint` is outside it, being derived from it.
    """
    return experiment_run_context_binding_payload(
        binding, include_fingerprint=False
    )


def experiment_run_context_fingerprint(
    binding: ExperimentRunContextBinding,
) -> str:
    """SHA-256 of the canonical JSON of what one run had to work with.

    Moves when the snapshot moves, when the person or their profile state moves,
    when the bound business metric run moves, and when the bound ranking
    evaluation run appears, disappears or changes. It does **not** move when the
    clock does, when the dataset is read from another directory, or when any
    Phase 10.5 output changes.
    """
    return _digest(canonical_experiment_run_context_payload(binding))


def verify_experiment_run_context_fingerprint(
    binding: ExperimentRunContextBinding,
) -> str:
    """Re-establish the binding's structure, then recompute its digest."""
    from .schema import _validate_context_binding_structure

    _validate_context_binding_structure(binding)
    recomputed = experiment_run_context_fingerprint(binding)
    if recomputed != binding.context_fingerprint:
        _refuse(
            "the experiment run context binding",
            binding.context_fingerprint,
            recomputed,
        )
    return recomputed


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------


def canonical_experiment_run_payload(run: ExperimentRun) -> dict[str, Any]:
    """The run's digest domain: its identity, and never its document.

    Four things — the layout version, the contract version, the context's own
    digest, and the twelve child digests in canonical order. Each child enters
    by digest, and every one of those is recomputed before a run is sealed or
    believed.

    Deliberately outside it: the provenance block entire, so a run generated at
    another moment, read from another directory, on another machine, under
    another Python, is the **same run**. That is what makes the
    content-addressed store able to say UNCHANGED and mean it.

    And this is emphatically not the SHA-256 of `run.json`: the file carries the
    provenance and its own formatting, and two files differing in either
    describe one experiment.
    """
    return experiment_run_fingerprint_payload(run)


def experiment_run_fingerprint(run: ExperimentRun) -> str:
    """SHA-256 of the canonical JSON of everything a run *is*."""
    return _digest(canonical_experiment_run_payload(run))


def verify_experiment_run_fingerprint(run: ExperimentRun) -> str:
    """Re-establish the structure, then recompute the digest, or refuse.

    Both halves and in that order, as everywhere else here: a run whose blocks
    were edited after somebody digested them is caught by the recomputation, and
    a run that was already mis-assembled when they did — a missing projection, a
    reordered overlap — is caught by the structure, whose digest would otherwise
    be perfectly valid over an artefact nobody should read.

    **The child digests are recomputed before the parent**, which is the rule
    that makes this more than a checksum over a list of strings: a run's own
    fingerprint is a digest of twelve claimed digests, so believing those claims
    would mean a forged block with an edited fingerprint field produces a
    perfectly valid run identity.
    """
    validate_experiment_run_structure(run)
    for result in run.projections:
        verify_projection_result_fingerprint(result)
    for result in run.overlaps:
        verify_overlap_result_fingerprint(result)
    verify_ranking_experiment_result_fingerprint(run.ranking)
    verify_experiment_run_context_fingerprint(run.context)
    recomputed = experiment_run_fingerprint(run)
    if recomputed != run.run_fingerprint:
        _refuse("the experiment run", run.run_fingerprint, recomputed)
    return recomputed
