"""What one experiment run has to work with, and how that is established.

An `ExperimentRunContext` is the bundle every Phase 10.5 computation reads: the
frozen Phase 10.1 dataset, its manifest, the fully verified Phase 10.4
`BusinessMetricRun` the experiments are bound to, and — when the snapshot froze a
recommendation at all — the Phase 10.3 evaluation run and label coverage the
four ranking questions are asked through.

## Transient, and never a proof token

It is an ordinary frozen dataclass. Anybody can construct one with any dataset
and any run they like, so **holding one proves nothing**, and every public
computation in this package re-verifies it before reading a record. That is the
same decision Phase 10.3 made for `MetricRunContext` and Phase 10.4 for
`BusinessMetricComputationContext`, for the same reason: Python dataclass
construction is not a capability boundary, and a `verified=True` flag set by a
builder would put the whole guarantee on whoever remembered to use the builder.

`build_experiment_run_context` exists as a convenience and an early refusal. It
is not a credential.

It is also **never persisted whole**. What a stored run keeps is
`ExperimentRunContextBinding` — eight fields naming the artefacts by identity.
A run document that republished the records, the manifest, the grades and the
benchmark rows would be a copy of every artefact it measured.

## What it may not hold

No database handle, no HTTP client, no live profile, no clock and no free policy
knob. There is no parameter here through which a caller could choose a target
country, an as-of date, a relevance threshold or a cut-off: every one of those
is already fixed by an artefact this context binds, and a second place to state
it would be a second answer.

## The one binding per phase

**One Phase 10.4 run, fully verified.** D42's rule: a Phase 10.5 run is bound to
exactly one `BusinessMetricRun`, over the same dataset, the same dataset
fingerprint, the same profile, the same profile fingerprint and the same cohort.
`verify_experiment_run_context` does not merely compare the strings — it calls
Phase 10.4's own `verify_business_metric_run` with these records and this
manifest, which recomputes all twenty-eight of its metrics. A run whose numbers
are not the numbers those records yield is refused here, before a single
projection exists.

Phase 10.5 nonetheless **recomputes none of those KPIs, copies none of them as a
result of its own, and derives no projection from their aggregates**. The
geography and Data/AI projections are computed from the records through
`evaluation.frozen_facts`, exactly as Phase 10.4's own rates are; the
cross-checks in `run.py` then hold the two derivations against each other, which
is only meaningful because neither was read off the other.

**The ranking inputs are present exactly when there is a ranking.** If the
snapshot froze no recommendation assessment at all, `ranking_evaluation_inputs`
**must** be `None` — there is nothing to evaluate, and the ranking block will say
so. If it froze one, they **must** exist:

    R empty      -> inputs MUST be None
    R non-empty  -> inputs MUST exist, with a valid, non-empty LabelCoverage

and the second half is a **hard precondition failure**, not a new `N_A`. That is
the correction D55 makes and it matters: a run that quietly reported "no labels
available" for a snapshot that has a ranking would look like a measured absence
of evidence, when what actually happened is that the operator did not supply the
evidence they have. The distinction between "this snapshot has nothing to
measure" and "you did not give me the labels" must not collapse into one status.

A *partially* judged labelset is a different thing again and is perfectly legal:
it produces Phase 10.3's own `N_A / TOP_K_NOT_FULLY_JUDGED` and friends, inside a
`COMPUTED` ranking block. So is a labelset that is not the one the evaluation run
declares — Phase 10.3 calls that `N_A / LABELSET_BINDING_MISMATCH`, a well-formed
question this evidence cannot answer, and this module deliberately does not
promote it to a refusal of its own.

## The benchmark rows

Carried **only** because Phase 10.4's full verification needs them: a
`BenchmarkBinding` that says `evaluation_ready=True` is identified by the digest
of its rows, so verifying such a run requires the rows in hand. They are
transient, they never enter an experiment result, and they are outside the
context fingerprint entirely.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from evaluation.business_metrics import (
    BusinessMetricRun,
    verify_business_metric_run,
)
from evaluation.labeling import FrozenEvaluationDataset
from evaluation.metrics import (
    EvaluationRunContract,
    LabelCoverage,
    MetricRunContext,
    verify_metric_run_context,
)

from .fingerprint import (
    EXPERIMENT_PLACEHOLDER_FINGERPRINT,
    experiment_run_context_fingerprint,
    verify_experiment_run_context_fingerprint,
)
from .schema import (
    EXPERIMENT_CONTRACT_VERSION,
    EXPERIMENT_RANKING_SOURCE,
    EXPERIMENT_RANKING_UNIVERSE_KIND,
    EXPERIMENT_RUN_CONTEXT_SCHEMA_VERSION,
    ExperimentBindingError,
    ExperimentContractError,
    ExperimentRunContextBinding,
    require_supported_experiment_contract_version,
    validate_cohort_size,
)

__all__ = [
    "ExperimentRunContext",
    "RankingEvaluationInputs",
    "build_experiment_run_context",
    "experiment_run_context_binding",
    "verify_experiment_run_context",
]


# --------------------------------------------------------------------------
# the ranking inputs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RankingEvaluationInputs:
    """The Phase 10.3 artefacts the four ranking questions are asked through.

    Two objects and no third: the bound evaluation run, and the label coverage
    the metrics read grades from. Phase 10.3 owns both shapes, owns their
    verification and owns the gates that decide whether each metric is knowable;
    Phase 10.5 supplies them and reads the answers.

    There is deliberately no `universe`, no `ranking` and no
    `labelset_fingerprint` field here. All three are already on the evaluation
    run, and a second copy beside it is a field that can come to disagree with
    the artefact it names.
    """

    evaluation_run: EvaluationRunContract
    label_coverage: LabelCoverage


# --------------------------------------------------------------------------
# the context
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExperimentRunContext:
    """The frozen artefacts one experiment run is computed from. **Transient.**

    Never persisted whole — see the module docstring — and never a proof that
    anything was verified: every public computation calls
    `verify_experiment_run_context` on whatever it is handed, every time.

    `manifest` is the Phase 10.1 manifest as the file holds it, kept as a
    mapping rather than re-parsed into dataclasses so that Phase 10.4's cohort
    binding can be rebuilt over exactly the bytes that were read.

    `benchmark_records` is the one payload here that is not about the cohort. It
    is the gold benchmark's rows, carried because `BenchmarkBinding` is an
    identity and holds none, and used for exactly one thing: letting Phase
    10.4's own verification establish that the rows in hand are the rows its
    binding names. It reaches no result, no fingerprint and no stored run.
    """

    frozen_dataset: FrozenEvaluationDataset
    manifest: Mapping[str, Any]
    business_metric_run: BusinessMetricRun
    ranking_evaluation_inputs: RankingEvaluationInputs | None = None
    benchmark_records: tuple[Mapping[str, Any], ...] | None = None


@dataclass(frozen=True)
class _VerifiedExperimentContext:
    """What survived verification. **Private, and the only thing resolvers read.**

    Constructed solely by `_verified` below, so a projection resolver cannot be
    handed an unverified dataset: the type it takes does not exist outside the
    verification path.

    `observed_recommendation_ids` is derived here, once, because three separate
    questions depend on it — whether the ranking inputs are required, what the
    `RECOMMENDATION_OBSERVED` projection includes, and whether the frozen
    ranking's membership is that projection's. Deriving it in three places would
    be three chances to disagree.
    """

    dataset: FrozenEvaluationDataset
    manifest: Mapping[str, Any]
    records: tuple[Mapping[str, Any], ...]
    cohort_ids: tuple[int, ...]
    cohort_size: int
    business_metric_run: BusinessMetricRun
    ranking_inputs: RankingEvaluationInputs | None
    benchmark_records: tuple[Mapping[str, Any], ...] | None
    observed_recommendation_ids: tuple[int, ...]
    #: The derived Phase 10.3 labelset-match state, or `None` when there are no
    #: ranking inputs. Phase 10.3's `verify_metric_run_context` computes it from
    #: two verified artefacts; nothing here sets it.
    labelset_matches: bool | None

    @property
    def target_country(self) -> str | None:
        """The bound target country, or `None` for an unavailable one.

        Read from the **verified** `ProfileTargetBindingEvidence` of the bound
        Phase 10.4 run and from nowhere else: not from `record["country"]`, not
        from a location string, not from a declared source map's scope, and not
        from a default. `None` covers both ways it can be unavailable — no
        binding at all, or a binding whose `country_code` is `None`, which is
        Phase 10.4's coherent evidence of an UNKNOWN target — because the
        projection's answer is the same for both and is `N_A`, never a country.
        """
        binding = self.business_metric_run.context.profile_target_binding
        return None if binding is None else binding.country_code


def _require_records(records: Any, *, subject: str) -> tuple[Mapping[str, Any], ...]:
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise ExperimentBindingError(f"{subject} are not a sequence: {records!r}")
    for position, record in enumerate(records, start=1):
        if not isinstance(record, Mapping):
            raise ExperimentBindingError(
                f"{subject}: record {position} is not a mapping: {record!r}"
            )
    return tuple(records)


def _observed_recommendation_ids(
    records: Sequence[Mapping[str, Any]],
) -> tuple[int, ...]:
    """The ids whose frozen `recommendation` block is present. **Presence only.**

    No score, no disposition and no threshold — see `CohortProjection`. A
    `recommendation` that is present but is not an object is a hard error: the
    snapshot states something this build cannot read, which is a contradiction
    rather than an absence.

    `recommendation is None` is Phase 10.1's own statement that the
    Recommendation run never covered the posting. It is not rank 0, not last and
    not an error — it is the candidate false negative the snapshot exists to
    preserve, and it simply is not in this set.
    """
    observed: list[int] = []
    for position, record in enumerate(records, start=1):
        if "recommendation" not in record:
            raise ExperimentBindingError(
                f"frozen record {position} states no 'recommendation' field; "
                "refusing to read a record of another contract"
            )
        block = record["recommendation"]
        if block is None:
            continue
        if not isinstance(block, Mapping):
            raise ExperimentBindingError(
                f"frozen record {position} states recommendation={block!r} "
                f"({type(block).__name__}), which is not the optional block of "
                "the Phase 10.1 record contract"
            )
        observed.append(int(record["opportunity_id"]))
    return tuple(sorted(observed))


def build_experiment_run_context(
    *,
    frozen_dataset: FrozenEvaluationDataset,
    manifest: Mapping[str, Any],
    business_metric_run: BusinessMetricRun,
    ranking_evaluation_inputs: RankingEvaluationInputs | None = None,
    benchmark_records: Sequence[Mapping[str, Any]] | None = None,
) -> ExperimentRunContext:
    """Bundle the frozen artefacts one run is about, verified, or refuse.

    The frontier of Phase 10.5. It assembles the context and immediately runs
    `verify_experiment_run_context` over it, so a caller finds out here rather
    than at the first projection — and the returned object's verification is
    nonetheless performed again on every use, because it establishes nothing.
    """
    context = ExperimentRunContext(
        frozen_dataset=frozen_dataset,
        manifest=manifest,
        business_metric_run=business_metric_run,
        ranking_evaluation_inputs=ranking_evaluation_inputs,
        benchmark_records=(
            None
            if benchmark_records is None
            else _require_records(benchmark_records, subject="the benchmark rows")
        ),
    )
    _verified(context)
    return context


def _verified(context: Any) -> _VerifiedExperimentContext:
    """Re-establish an experiment context from its own inputs. The whole gate.

    In order, and every step refuses rather than repairs:

    1. the object is an experiment run context and its parts are of their types;
    2. the frozen dataset's own records and cohort are read, and the cohort is
       required to be non-empty;
    3. the bound Phase 10.4 run is **fully verified** against these records and
       this manifest — which recomputes every one of its metrics, re-derives its
       cohort binding from the records and re-establishes its whole evidence
       assembly. A run whose numbers are not the numbers these records yield
       fails here;
    4. the Phase 10.4 run's cohort, dataset, dataset fingerprint, profile and
       profile fingerprint are held against the dataset's own — D42's binding,
       checked rather than assumed;
    5. the ranking inputs are held against the presence of an observed
       recommendation, in both directions, and verified through Phase 10.3's own
       `verify_metric_run_context` when they exist.

    There is no path from a bare dataset to a projection that skips it, and no
    cached "verified" flag a second call could trust.
    """
    if not isinstance(context, ExperimentRunContext):
        raise ExperimentContractError(
            f"{context!r} is not an experiment run context"
        )
    dataset = context.frozen_dataset
    if not isinstance(dataset, FrozenEvaluationDataset):
        raise ExperimentContractError(
            f"{dataset!r} is not a verified frozen evaluation dataset; Phase "
            "10.5 reads a snapshot that has been through Phase 10.2's integrity "
            "gate, never a directory or a bare list of records"
        )
    if not isinstance(context.manifest, Mapping):
        raise ExperimentContractError(
            f"{context.manifest!r} is not a Phase 10.1 manifest mapping"
        )
    if not isinstance(context.business_metric_run, BusinessMetricRun):
        raise ExperimentContractError(
            f"{context.business_metric_run!r} is not a business metric run; a "
            "Phase 10.5 run is bound to exactly one verified Phase 10.4 run"
        )

    records = _require_records(dataset.records, subject="the frozen records")
    cohort_ids = tuple(int(record["opportunity_id"]) for record in records)
    cohort_size = validate_cohort_size(
        len(records), subject=f"the cohort of dataset {dataset.dataset_id}"
    )
    if list(cohort_ids) != sorted(cohort_ids) or len(set(cohort_ids)) != len(
        cohort_ids
    ):
        # Phase 10.2's reader already establishes this; re-established here
        # because every projection's canonical membership and every overlap's
        # `neither` partition is derived from this order.
        raise ExperimentBindingError(
            f"dataset {dataset.dataset_id} does not hold its records in the "
            "canonical order (opportunity_id ASC) without repetition"
        )

    benchmark_records = (
        None
        if context.benchmark_records is None
        else _require_records(
            context.benchmark_records, subject="the benchmark rows"
        )
    )

    # **The whole Phase 10.4 run, recomputed over these very records.** Not a
    # fingerprint comparison: the arithmetic. This is what makes the D42
    # cross-checks in `run.py` meaningful — they hold two independent
    # derivations against each other, and that is only a check if neither was
    # read off the other.
    verify_business_metric_run(
        context.business_metric_run,
        records,
        context.manifest,
        benchmark_records=benchmark_records,
    )
    _require_business_metric_binding(context.business_metric_run, dataset, cohort_size)

    observed = _observed_recommendation_ids(records)
    ranking_inputs, labelset_matches = _verified_ranking_inputs(
        context.ranking_evaluation_inputs, dataset, observed
    )
    return _VerifiedExperimentContext(
        dataset=dataset,
        manifest=context.manifest,
        records=records,
        cohort_ids=cohort_ids,
        cohort_size=cohort_size,
        business_metric_run=context.business_metric_run,
        ranking_inputs=ranking_inputs,
        benchmark_records=benchmark_records,
        observed_recommendation_ids=observed,
        labelset_matches=labelset_matches,
    )


def _require_business_metric_binding(
    run: BusinessMetricRun, dataset: FrozenEvaluationDataset, cohort_size: int
) -> None:
    """D42's binding: one Phase 10.4 run, about this snapshot and this person.

    Five equalities, each naming a way a measurement can be quietly wrong: a run
    about another snapshot, about the same snapshot at other content, for another
    person, for the same person in another profile state, or over a cohort of
    another size. A mismatch is a **hard error**, never an `N_A`: the artefacts
    in front of us are not describing the same world, which is a different thing
    from a question this evidence cannot answer.
    """
    cohort = run.context.frozen_cohort_binding
    for name, stated, expected in (
        ("dataset_id", cohort.dataset_id, dataset.dataset_id),
        (
            "dataset fingerprint",
            cohort.dataset_fingerprint,
            dataset.content_fingerprint,
        ),
        ("profile_id", cohort.profile_id, dataset.profile_id),
        (
            "profile fingerprint",
            cohort.profile_fingerprint,
            dataset.profile_context_fingerprint,
        ),
        ("cohort size", cohort.cohort_size, cohort_size),
    ):
        if stated != expected:
            raise ExperimentBindingError(
                f"the bound business metric run states {name} {stated!r} and "
                f"this frozen dataset states {expected!r}; a Phase 10.5 run is "
                "bound to exactly one Phase 10.4 run over the same dataset, the "
                "same content, the same profile state and the same cohort"
            )


def _verified_ranking_inputs(
    inputs: Any,
    dataset: FrozenEvaluationDataset,
    observed: tuple[int, ...],
) -> tuple[RankingEvaluationInputs | None, bool | None]:
    """Hold the ranking inputs against the presence of an observed ranking.

    Both directions, and they fail for different reasons — see the module
    docstring on D55. An absent ranking with inputs supplied is a caller
    measuring a ranking the snapshot does not hold; a present ranking with no
    inputs is a caller who has the evidence and did not supply it, and that is a
    **precondition failure** rather than a new `N_A`.

    When inputs exist they are verified through Phase 10.3's own
    `verify_metric_run_context`, which re-establishes the run and the coverage
    against this dataset from scratch and returns the derived labelset-match
    state. Two further bindings are checked here because they are Phase 10.5's
    own rule rather than Phase 10.3's: the universe must be the whole frozen
    cohort and the ranking must be the frozen production recommendation. Phase
    10.3 can represent other universes and other sources quite legitimately;
    this package may evaluate exactly one of each.
    """
    if not observed:
        if inputs is not None:
            raise ExperimentBindingError(
                "this snapshot froze no recommendation assessment and ranking "
                "evaluation inputs were supplied; there is no observed ranking "
                "for them to be about, and the ranking block states that rather "
                "than measuring an ordering the snapshot does not hold"
            )
        return None, None

    if inputs is None:
        # D55. Emphatically not a new `N_A`: see the module docstring.
        raise ExperimentBindingError(
            f"this snapshot froze {len(observed)} recommendation assessment(s) "
            "and no ranking evaluation inputs were supplied. A Phase 10.5 run "
            "over a snapshot that has an observed ranking requires the Phase "
            "10.3 evaluation run and a valid, non-empty label coverage; "
            "reporting the ranking block as unavailable here would present a "
            "missing input as a measured absence of evidence"
        )
    if not isinstance(inputs, RankingEvaluationInputs):
        raise ExperimentContractError(
            f"{inputs!r} is not a ranking evaluation inputs bundle"
        )
    if not isinstance(inputs.evaluation_run, EvaluationRunContract):
        raise ExperimentContractError(
            f"{inputs.evaluation_run!r} is not a Phase 10.3 evaluation run"
        )
    if not isinstance(inputs.label_coverage, LabelCoverage):
        raise ExperimentContractError(
            f"{inputs.label_coverage!r} is not a Phase 10.3 label coverage"
        )
    if not inputs.label_coverage.labels:
        # Phase 10.2's own builder refuses an empty labelset, so this is a
        # backstop against a hand-built coverage — and it is a precondition
        # failure for the same reason a missing bundle is.
        raise ExperimentBindingError(
            "the supplied label coverage holds no judgement; a Phase 10.5 run "
            "over an observed ranking requires a non-empty labelset, and an "
            "empty one is a missing input rather than a measured absence"
        )

    # Phase 10.3's own verification, in full: the run and the coverage
    # re-established against this dataset, the calibration lot redrawn by Phase
    # 10.2's selector, every nested digest recomputed. The labelset-match state
    # comes back derived from two verified artefacts.
    labelset_matches = verify_metric_run_context(
        MetricRunContext(
            dataset=dataset,
            run=inputs.evaluation_run,
            coverage=inputs.label_coverage,
        )
    )

    run = inputs.evaluation_run
    if run.universe.kind is not EXPERIMENT_RANKING_UNIVERSE_KIND:
        raise ExperimentBindingError(
            f"the bound evaluation run measures a {run.universe.kind} universe "
            f"and Phase 10.5 evaluates against {EXPERIMENT_RANKING_UNIVERSE_KIND} "
            "only; a recall whose universe is the set of postings the ranking "
            "covered cannot see a relevant posting the ranking missed"
        )
    if run.ranking.source is not EXPERIMENT_RANKING_SOURCE:
        raise ExperimentBindingError(
            f"the bound evaluation run evaluates a {run.ranking.source} ranking "
            f"and Phase 10.5 evaluates {EXPERIMENT_RANKING_SOURCE} only; the "
            "observed ranking experiment is about the ordering production "
            "actually produced, and a declared offline ordering is not it"
        )
    return inputs, labelset_matches


def verify_experiment_run_context(
    context: ExperimentRunContext,
) -> ExperimentRunContextBinding:
    """Re-establish a context, and return the persistent binding it derives.

    The public name for the gate above. It returns the binding **derived from
    the artefacts in front of it** rather than one the caller supplied, so a
    caller that uses the return value is using a fact established from verified
    inputs.
    """
    return experiment_run_context_binding(_verified(context))


def experiment_run_context_binding(
    verified: Any,
) -> ExperimentRunContextBinding:
    """The persistent, fingerprinted binding of a verified context.

    Accepts only the private verified bundle, which is what makes it impossible
    to mint a binding for artefacts nobody checked: the type it takes is not
    constructible from outside this module's verification path. Public callers
    reach it through `verify_experiment_run_context`.

    The digest is computed last, over the eight semantic fields alone — D44's
    domain, stated in `fingerprint.py` — and is then re-verified before being
    returned, so the object that leaves here is one that survives the same check
    a stored one faces.
    """
    if not isinstance(verified, _VerifiedExperimentContext):
        raise ExperimentContractError(
            f"{verified!r} is not a verified experiment run context; a context "
            "binding is derived from artefacts that have been re-established, "
            "never assembled from strings a caller holds"
        )
    contract_version = require_supported_experiment_contract_version(
        EXPERIMENT_CONTRACT_VERSION
    )
    dataset = verified.dataset
    inputs = verified.ranking_inputs
    draft = ExperimentRunContextBinding(
        run_context_schema_version=EXPERIMENT_RUN_CONTEXT_SCHEMA_VERSION,
        experiment_contract_version=contract_version,
        dataset_id=dataset.dataset_id,
        dataset_content_fingerprint=dataset.content_fingerprint,
        profile_id=dataset.profile_id,
        profile_context_fingerprint=dataset.profile_context_fingerprint,
        business_metric_run_fingerprint=(
            verified.business_metric_run.run_fingerprint
        ),
        # `None` exactly when the snapshot froze no recommendation at all. The
        # one nullable binding here, and its nullability is the same fact as the
        # ranking block's single `N_A`.
        ranking_evaluation_run_fingerprint=(
            None if inputs is None else inputs.evaluation_run.run_fingerprint
        ),
        context_fingerprint=EXPERIMENT_PLACEHOLDER_FINGERPRINT,
    )
    binding = replace(
        draft, context_fingerprint=experiment_run_context_fingerprint(draft)
    )
    verify_experiment_run_context_fingerprint(binding)
    return binding
