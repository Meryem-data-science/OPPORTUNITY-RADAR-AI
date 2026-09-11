"""Deterministic semantic fingerprints for an offline metric run — Phase 10.3a.

Same primitive as every phase before this one — the project's single
`canonical_json` (sorted keys, no insignificant whitespace, UTF-8), digested
with SHA-256. Nothing is invented here; only the three domains are new, and each
is stated in full, because a digest whose domain is implicit is a digest nobody
can reason about.

**The universe fingerprint** answers "which closed set of opportunities is this,
and what is it a set inside". Its exact domain:

    universe_version              the shape of a universe
    kind                          full frozen cohort, or fixed benchmark pool
    dataset_id                    which snapshot the ids are ids in
    dataset_content_fingerprint   and what that snapshot actually contained
    opportunity_ids               the members, in canonical id order
    size                          the declared size, which must equal the members

**The ranking fingerprint** answers "which ordered list is being evaluated, and
for whom". Its exact domain:

    ranking_version               the shape of a ranking
    source                        frozen projection, or declared offline ordering
    dataset_id / dataset_content_fingerprint
    profile_id / profile_context_fingerprint   a ranking is personalised
    entries[]                     rank position, opportunity id and the upstream
                                  position each entry was derived from, **in
                                  rank order**

The entries are not sorted before digesting, and that is the whole point of this
digest: reordering a ranking must always change it.

**The run fingerprint** answers "which measurement is this". Its exact domain:

    run_schema_version            the shape of the run contract
    metric_contract_version       the rules that decide what may be reported
    evidence_class                what the evidence is allowed to claim
    dataset_id / dataset_content_fingerprint
    profile_id / profile_context_fingerprint
    evaluation_universe_fingerprint
    ranking_fingerprint
    label_protocol_version        which rubric the judgements answer
    labelset_fingerprint          which judgements

Nested artefacts enter by digest rather than by contents. That is safe only
because `run.py` verifies each nested digest by recomputation before a run is
assembled, which it does.

**What is outside the run domain, and why** — as much a part of the contract as
what is inside:

* `generated_at` — the clock. Two operators deciding the same availability from
  the same artefacts, a week apart, must agree;
* the frozen dataset **directory** and the label **root** — where the files were
  read from, not what they say. Copying a dataset to a second path must not
  produce a second measurement;
* `run_fingerprint` itself — derived from the domain, so including it would be
  circular;
* presentation: how a report is formatted, rounded or ordered on a screen.

The rule the lists obey: **two runs carrying the same `run_fingerprint` ask the
same question of the same evidence, and any metric contract implemented over
them must return the same answer.**

Following Phase 10.1, a stored object's self-declared fingerprint is never
trusted: `verify_*_fingerprint` below recomputes it from the canonical semantic
projection, and `run.py` calls them before a run is built rather than after a
number has been reported.
"""

from __future__ import annotations

import hashlib
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from .schema import (
    EvaluationBindingError,
    EvaluationRanking,
    EvaluationRunContract,
    EvaluationUniverse,
    evaluation_ranking_payload,
    evaluation_run_payload,
    evaluation_universe_payload,
)

__all__ = [
    "canonical_evaluation_ranking_payload",
    "canonical_evaluation_run_payload",
    "canonical_evaluation_universe_payload",
    "evaluation_ranking_fingerprint",
    "evaluation_run_fingerprint",
    "evaluation_universe_fingerprint",
    "verify_evaluation_ranking_fingerprint",
    "verify_evaluation_run_fingerprint",
    "verify_evaluation_universe_fingerprint",
]


def _digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# the evaluation universe
# --------------------------------------------------------------------------


def canonical_evaluation_universe_payload(
    universe: EvaluationUniverse,
) -> dict[str, Any]:
    """The universe digest domain, as a structure a test can read and assert on.

    Identical to the universe's one canonical payload: a universe carries no
    execution metadata, so there is nothing to project away and no second
    layout to keep in step with the first.
    """
    return evaluation_universe_payload(universe)


def evaluation_universe_fingerprint(universe: EvaluationUniverse) -> str:
    """SHA-256 of the canonical JSON of everything a universe asserts."""
    return _digest(canonical_evaluation_universe_payload(universe))


def verify_evaluation_universe_fingerprint(universe: EvaluationUniverse) -> str:
    """Recompute the digest and refuse a universe that misstates its own."""
    recomputed = evaluation_universe_fingerprint(universe)
    if recomputed != universe.fingerprint:
        raise EvaluationBindingError(
            f"evaluation universe claims fingerprint {universe.fingerprint} but "
            f"its members and bindings digest to {recomputed}; refusing an "
            "artefact that is not what it says it is"
        )
    return recomputed


# --------------------------------------------------------------------------
# the ranking
# --------------------------------------------------------------------------


def canonical_evaluation_ranking_payload(
    ranking: EvaluationRanking,
) -> dict[str, Any]:
    """The ranking digest domain, in rank order and never sorted."""
    return evaluation_ranking_payload(ranking)


def evaluation_ranking_fingerprint(ranking: EvaluationRanking) -> str:
    """SHA-256 of the canonical JSON of everything a ranking asserts."""
    return _digest(canonical_evaluation_ranking_payload(ranking))


def verify_evaluation_ranking_fingerprint(ranking: EvaluationRanking) -> str:
    """Recompute the digest and refuse a ranking that misstates its own."""
    recomputed = evaluation_ranking_fingerprint(ranking)
    if recomputed != ranking.fingerprint:
        raise EvaluationBindingError(
            f"evaluation ranking claims fingerprint {ranking.fingerprint} but "
            f"its order and bindings digest to {recomputed}; refusing an "
            "artefact that is not what it says it is"
        )
    return recomputed


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------


def canonical_evaluation_run_payload(
    run: EvaluationRunContract,
) -> dict[str, Any]:
    """The run digest domain: the contract minus its provenance and own digest.

    Built by asking the run for its one payload with `include_provenance=False`,
    so "what is written" and "what is digested" are one layout differing by a
    block that is named rather than remembered.
    """
    return evaluation_run_payload(run, include_provenance=False)


def evaluation_run_fingerprint(run: EvaluationRunContract) -> str:
    """SHA-256 of the canonical JSON of everything a metric run is about."""
    return _digest(canonical_evaluation_run_payload(run))


def verify_evaluation_run_fingerprint(run: EvaluationRunContract) -> str:
    """Recompute the digest and refuse a run that misstates its own.

    Phase 10.1's principle, one layer up: a forged `run_fingerprint` is exactly
    how two different measurements come to be filed under one identity, and the
    only defence that works is never reading the field except to compare it.
    """
    recomputed = evaluation_run_fingerprint(run)
    if recomputed != run.run_fingerprint:
        raise EvaluationBindingError(
            f"evaluation run claims fingerprint {run.run_fingerprint} but its "
            f"bindings digest to {recomputed}; refusing a run that is not what "
            "it says it is"
        )
    return recomputed
