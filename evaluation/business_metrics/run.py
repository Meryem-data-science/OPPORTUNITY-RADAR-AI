"""One complete business metric run: every key, once, or no run at all.

Phase 10.4b. `formulas.py` answers one question at a time; this module decides
**which** questions a run asks, asks all of them, and seals the answers under one
identity — or raises and produces nothing.

## The key set is derived, never chosen

`build_business_metric_run` takes no list of metrics. There is no parameter
through which a caller could ask for a subset, because a run that reported
twenty-two of twenty-seven metrics and called itself a run would be a selection
presented as a measurement — and the five it left out would be exactly the five
somebody did not like.

The set is:

* one scalar key for **every** non-dimensional metric name in the contract; and
* one `OBSERVED_SOURCE_CONTRIBUTION / SOURCE_ID=<id>` key for **every distinct
  `source_id` the frozen records were actually seen at**.

A source id repeated across records, or twice within one record, contributes one
key. A source that is *declared* in the source map but never observed gets none:
a contribution of 0.0 for it would assert that it found nothing, which is a
measurement nobody made. There is no synthetic `UNKNOWN` source key. A cohort
whose records carry no source rows at all yields no dimensional keys, and that is
a valid complete run.

## All of it, or none of it

Every key goes through the **public** `compute_business_metric`, so every result
in a run came through the same re-verified trust boundary any other caller would
face; the private resolvers are not reachable from here. Each result must be
either COMPUTED or a contract-valid `N_A`, and each is re-established with
`verify_business_metric_result` against the context before the run is sealed.

A hard error on a single key means **no official run exists**. Not a run with
twenty-six results, not a run with one marked failed: the artefact is not
produced. A partial run is the shape "we measured what worked" takes, and it is
indistinguishable from a complete one once it is written to disk.

## Two verifications, deliberately separate

`verify_business_metric_run_structure` re-establishes a run **from itself**:
versions, structures, canonical order, a duplicate-free key set, every scalar
metric the contract defines, every nested digest and the run's own — and it
rebinds **every result to the run's own context**, so a result sealed against a
different assembly of evidence cannot be dropped into a run and re-fingerprinted.

It does not pretend to more. It cannot re-derive the *dynamic* per-source keys,
because which sources the cohort observed is a fact about records it does not
hold, and it cannot check any arithmetic for the same reason. A run that passes
it is well formed and internally coherent; it is not thereby *true*.

The line between the two is drawn by where the fact lives: which scalar metrics
exist is a property of `BusinessMetricName`, which this module has, so their
completeness is structural. Which `source_id`s exist is a property of the frozen
records, which it does not, so that half waits for the full verifier.

`verify_business_metric_run` has the records. It rebuilds the computation
context, re-derives the exact key set, **recomputes every metric** and requires
semantic equality with what was stored, result by result. It is the only function
in this package that certifies a run arithmetically — and it is what closes the
gap Phase 10.4a documented, where a result's own digest was perfectly valid over
a value no formula had produced.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from .bindings import (
    assert_unique_business_metric_keys,
    verify_business_metric_result,
    verify_business_metric_results,
    verify_business_metric_run_context,
)
from .fingerprint import (
    PLACEHOLDER_FINGERPRINT,
    business_metric_run_fingerprint,
    verify_business_metric_result_fingerprint,
    verify_business_metric_run_context_fingerprint,
    verify_business_metric_run_fingerprint,
)
from .formulas import (
    BusinessMetricComputationContext,
    _observed_source_id_universe,
    _verified,
    build_business_metric_computation_context,
    compute_business_metric,
)
from .schema import (
    BUSINESS_METRIC_RUN_SCHEMA_VERSION,
    DIMENSIONAL_BUSINESS_METRICS,
    BusinessMetricBindingError,
    BusinessMetricContractError,
    BusinessMetricDimension,
    BusinessMetricKey,
    BusinessMetricName,
    BusinessMetricResult,
    BusinessMetricRun,
    BusinessMetricRunProvenance,
    BusinessMetricStatus,
    business_metric_key_payload,
    business_metric_key_sort_key,
    business_metric_result_payload,
    canonical_business_metric_keys,
    validate_business_metric_run_structure,
)

__all__ = [
    "build_business_metric_run",
    "verify_business_metric_run",
    "verify_business_metric_run_structure",
]


# --------------------------------------------------------------------------
# the exact key set of a complete run
# --------------------------------------------------------------------------


def _run_metric_keys(verified: Any) -> tuple[BusinessMetricKey, ...]:
    """Every key a complete run over this cohort answers, canonically ordered.

    Derived from the contract's own metric registry and from the **verified**
    records — never from a caller, and never from the declared source map. The
    dimensional half is the whole reason this cannot be a constant: which sources
    a snapshot observed is a property of that snapshot.
    """
    keys: list[BusinessMetricKey] = [
        BusinessMetricKey(metric)
        for metric in BusinessMetricName
        if metric not in DIMENSIONAL_BUSINESS_METRICS
    ]
    keys.extend(
        BusinessMetricKey(
            BusinessMetricName.OBSERVED_SOURCE_CONTRIBUTION,
            dimension_kind=BusinessMetricDimension.SOURCE_ID,
            dimension_value=source_id,
        )
        for source_id in _observed_source_id_universe(verified)
    )
    # Refuses a repeat and returns them in the contract's order, so a run's
    # results are held in the one order two equal runs agree on.
    return assert_unique_business_metric_keys(keys)


# --------------------------------------------------------------------------
# building a run
# --------------------------------------------------------------------------


def build_business_metric_run(
    context: BusinessMetricComputationContext,
    *,
    provenance: BusinessMetricRunProvenance | None = None,
) -> BusinessMetricRun:
    """Compute every metric over one verified cohort, or produce nothing.

    In order, and each step refuses rather than reports:

    1. the computation context is verified — the cohort rebuilt from the records,
       held against the run context's, every nested digest recomputed, the
       benchmark rows held against their binding;
    2. the exact key set is derived from the contract and from the observed
       sources of those records;
    3. every key is computed through the **public** `compute_business_metric`,
       which re-verifies the context again for each one. The private resolvers
       are never called from here: a run must be reachable by exactly the path
       any other caller has, or it would be testing a different function;
    4. every result is re-established against the run context with
       `verify_business_metric_result`, and its own digest recomputed;
    5. the results are required to cover the derived key set **exactly** — no
       missing key, no extra one;
    6. only then is the run's fingerprint computed and the artefact assembled.

    A hard error at any point leaves no run. That is the point of doing the
    arithmetic before the assembly rather than result by result into a growing
    document.
    """
    verified = _verified(context)
    contract_version = verified.run_context.contract_version
    keys = _run_metric_keys(verified)

    results: list[BusinessMetricResult] = []
    for key in keys:
        result = compute_business_metric(context, key)
        if not isinstance(result, BusinessMetricResult):
            raise BusinessMetricContractError(
                f"computing {key.metric} produced {result!r}, which is not a "
                "business metric result"
            )
        if result.status not in (
            BusinessMetricStatus.COMPUTED,
            BusinessMetricStatus.N_A,
        ):
            raise BusinessMetricContractError(
                f"{key.metric} produced status {result.status!r}; a business "
                "metric is a number or a stated refusal"
            )
        if business_metric_key_sort_key(result.key) != business_metric_key_sort_key(
            key
        ):
            raise BusinessMetricBindingError(
                f"the run asked for {business_metric_key_payload(key)} and the "
                f"result is about {business_metric_key_payload(result.key)}"
            )
        # Re-established through the same verifier a stored result faces, so a
        # run holds nothing that would fail verification the moment it was read
        # back.
        verify_business_metric_result(result, context=verified.run_context)
        verify_business_metric_result_fingerprint(result)
        results.append(result)

    _require_exact_key_coverage(keys, results)

    draft = BusinessMetricRun(
        run_schema_version=BUSINESS_METRIC_RUN_SCHEMA_VERSION,
        contract_version=contract_version,
        context=verified.run_context,
        results=tuple(
            sorted(results, key=lambda item: business_metric_key_sort_key(item.key))
        ),
        run_fingerprint=PLACEHOLDER_FINGERPRINT,
        provenance=(
            BusinessMetricRunProvenance() if provenance is None else provenance
        ),
    )
    validate_business_metric_run_structure(draft)
    return replace(draft, run_fingerprint=business_metric_run_fingerprint(draft))


def _require_exact_key_coverage(
    keys: Sequence[BusinessMetricKey], results: Sequence[BusinessMetricResult]
) -> None:
    """The results answer this key set exactly: nothing missing, nothing extra.

    Both directions, because they fail for different reasons. A missing key is a
    run that is silently incomplete; an extra one is a run answering a question
    the cohort did not pose — a contribution for a source nobody observed, most
    likely, which is precisely the fabricated zero this contract forbids.
    """
    expected = {business_metric_key_sort_key(key) for key in keys}
    produced = {business_metric_key_sort_key(item.key) for item in results}
    missing = sorted(expected - produced)
    extra = sorted(produced - expected)
    if missing or extra:
        raise BusinessMetricBindingError(
            f"this run answers {len(produced)} key(s) and a complete run over "
            f"this cohort answers {len(expected)}; missing {missing}, "
            f"unexpected {extra}. A business metric run is every metric or it is "
            "not a run"
        )
    if len(produced) != len(results):
        raise BusinessMetricBindingError(
            f"the run holds {len(results)} result(s) under {len(produced)} "
            "distinct key(s); one key carries one result"
        )


# --------------------------------------------------------------------------
# structural verification — everything establishable without the records
# --------------------------------------------------------------------------


def verify_business_metric_run_structure(run: BusinessMetricRun) -> str:
    """Re-establish a run from itself, and return its recomputed fingerprint.

    What it establishes: the versions, the run context's structure and its
    recomputed digest, every result's structure and recomputed digest, the
    single contract version across all three levels, one result per key in
    canonical order, and the run's own digest over all of it.

    Crucially it also **rebinds every result to this run's context**, through
    Phase 10.4a's own `verify_business_metric_results`: each result's
    `scope_fingerprint` and `evidence_fingerprint` must be exactly the ones
    re-projected from `run.context`. Verifying a result's own digest is not
    enough and was the hole this closes — a result sealed against *another*
    context is internally impeccable, and dropping it into a run whose
    fingerprint was then recomputed produced a document that passed every check
    and described two different assemblies of evidence at once.

    What it deliberately does **not** claim, and the name says so:

    * it cannot re-derive the dynamic `OBSERVED_SOURCE_CONTRIBUTION` keys. Which
      sources the cohort observed is a fact about records this function does not
      hold, so a run that omits one — or invents one — passes here. The *scalar*
      half of the key set is another matter and is required, by
      `validate_business_metric_run_structure`: which metrics the contract
      defines is not a fact about records;
    * it proves no arithmetic. A SHA-256 identifies content; it does not say that
      a value is the value the metric's formula yields over the frozen records.

    `verify_business_metric_run` has the records and asks both questions.
    """
    validate_business_metric_run_structure(run)
    verify_business_metric_run_context(run.context)
    verify_business_metric_run_context_fingerprint(run.context)
    # Re-establishes each result against this context — its own digest, its
    # structure, the projections it claims, the universe's real size and the
    # condition of any refusal. It recomputes no arithmetic: that needs the
    # records, which this function does not have.
    verify_business_metric_results(run.results, context=run.context)
    return verify_business_metric_run_fingerprint(run)


# --------------------------------------------------------------------------
# full verification — the run against the snapshot it claims to measure
# --------------------------------------------------------------------------


def verify_business_metric_run(
    run: BusinessMetricRun,
    records: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    *,
    benchmark_records: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Recompute a whole run from the snapshot and require it to be what it says.

    **The only function here that certifies the arithmetic.** Phase 10.4a could
    establish that a result was structurally supportable — the right universe,
    evidence of the right class, a support block that adds up against itself —
    and could never establish that `value` was the number the metric's formula
    yields, because it had no formula. This does:

    1. the structure and every digest, through the structural verifier above;
    2. a computation context rebuilt from these original records and manifest,
       which re-establishes the cohort binding and re-applies the benchmark rules;
    3. the run context stored in the run must be the one that context is about;
    4. the exact key set, re-derived from the verified records — which is what
       catches a missing per-source key and an invented one;
    5. **every metric recomputed** through the public `compute_business_metric`,
       and required to equal the stored result semantically — key, status, value,
       reason, universe, evidence class, support block and all three fingerprints;
    6. the run fingerprint recomputed over the results that survived.

    A forged value fails at (5) even though its own digest is perfectly valid,
    because the recomputation produces a different number and therefore a
    different result digest. Altered records or an altered manifest fail at (2)
    or (3): they derive a different cohort binding.
    """
    structural = verify_business_metric_run_structure(run)

    context = build_business_metric_computation_context(
        records,
        manifest,
        run.context,
        benchmark_records=benchmark_records,
    )
    verified = _verified(context)
    if (
        verified.run_context.run_context_fingerprint
        != run.context.run_context_fingerprint
    ):
        raise BusinessMetricBindingError(
            f"this run rests on evidence {run.context.run_context_fingerprint} "
            f"and these inputs assemble {verified.run_context.run_context_fingerprint}"
        )

    expected_keys = _run_metric_keys(verified)
    stored_keys = canonical_business_metric_keys(
        [result.key for result in run.results]
    )
    expected_identities = [business_metric_key_sort_key(key) for key in expected_keys]
    stored_identities = [business_metric_key_sort_key(key) for key in stored_keys]
    if expected_identities != stored_identities:
        missing = sorted(set(expected_identities) - set(stored_identities))
        extra = sorted(set(stored_identities) - set(expected_identities))
        raise BusinessMetricBindingError(
            f"this run states {len(stored_identities)} key(s) and a complete run "
            f"over these records answers {len(expected_identities)}; missing "
            f"{missing}, unexpected {extra}"
        )

    for stored in run.results:
        recomputed = compute_business_metric(context, stored.key)
        _require_identical_results(stored, recomputed)

    recomputed_run = business_metric_run_fingerprint(run)
    if recomputed_run != run.run_fingerprint:
        raise BusinessMetricBindingError(
            f"the run claims fingerprint {run.run_fingerprint} and its verified "
            f"results digest to {recomputed_run}"
        )
    return structural


def _require_identical_results(
    stored: BusinessMetricResult, recomputed: BusinessMetricResult
) -> None:
    """Stored and recomputed must be the same answer, field for field.

    Compared through the canonical payload rather than by `==` so that a mismatch
    can say *which* field moved — and compared in full, including the support
    block and all three fingerprints, because a stored result that agrees on the
    value while disagreeing about its numerator is still not this measurement.

    This is the check that makes a forged value visible. Phase 10.4a's
    `verify_business_metric_result` would pass such a result: it is structurally
    impeccable. Only recomputation distinguishes it.
    """
    stored_payload = business_metric_result_payload(stored)
    recomputed_payload = business_metric_result_payload(recomputed)
    if canonical_json(stored_payload) == canonical_json(recomputed_payload):
        return
    differing = sorted(
        field
        for field in recomputed_payload
        if canonical_json(stored_payload.get(field))
        != canonical_json(recomputed_payload[field])
    )
    raise BusinessMetricBindingError(
        f"{stored.key.metric} is stored as "
        f"{stored.status}/{stored.value!r} and recomputing it over these records "
        f"yields {recomputed.status}/{recomputed.value!r}; the stored and "
        f"recomputed results differ in {differing}. A result whose digest is "
        "valid over a number no formula produced is exactly what this "
        "verification exists to catch"
    )
