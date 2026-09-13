"""The Phase 10.4b run: which keys, computed together, sealed under one identity.

Three properties are the point of this file:

* **the key set is derived, not chosen.** Every scalar metric plus one key per
  `source_id` the records were actually seen at — no key for a source that was
  merely declared, no synthetic UNKNOWN, and no way for a caller to ask for a
  subset;
* **all or nothing.** A hard error on one metric leaves no run at all, so a
  partial run cannot be mistaken for a complete one;
* **the fingerprint follows D9 exactly.** It moves when a result moves and it
  does not move when the provenance does — which is what lets the store
  recognise a re-run of the same measurement.

And the one that closes Phase 10.4a's documented gap: the full verifier
recomputes every number, so a forged value whose own digest is perfectly valid is
caught, which no amount of structural verification could do.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from evaluation.business_metrics import (
    BUSINESS_METRIC_CONTRACT_VERSION,
    BUSINESS_METRIC_RUN_SCHEMA_VERSION,
    DIMENSIONAL_BUSINESS_METRICS,
    BusinessMetricBindingError,
    BusinessMetricContractError,
    BusinessMetricDimension,
    BusinessMetricKey,
    BusinessMetricName,
    BusinessMetricRunProvenance,
    BusinessMetricStatus,
    BusinessMetricsError,
    build_business_metric_run,
    business_metric_key_sort_key,
    business_metric_run_fingerprint,
    canonical_business_metric_run_payload,
    compute_business_metric,
    verify_business_metric_run,
    verify_business_metric_run_structure,
)
from tests.unit.business_metric_fixtures import (
    DEFAULT_RECORDS,
    FakeSourceEntry,
    benchmark_rows,
    computation,
    declared_universe,
    manifest_payload,
    qualification,
    ready_benchmark,
    record,
    source,
)

SOURCE_ID = BusinessMetricDimension.SOURCE_ID
SCALAR_METRICS = tuple(
    metric
    for metric in BusinessMetricName
    if metric not in DIMENSIONAL_BUSINESS_METRICS
)


def run_over(records=DEFAULT_RECORDS, **kwargs):
    """A complete run over `records`, with everything assembled."""
    provenance = kwargs.pop("provenance", None)
    return build_business_metric_run(
        computation(tuple(records), **kwargs), provenance=provenance
    )


def source_keys(run):
    return tuple(
        result.key.dimension_value
        for result in run.results
        if result.key.metric is BusinessMetricName.OBSERVED_SOURCE_CONTRIBUTION
    )


# ====================================================================
# the key set of a complete run — D7
# ====================================================================


def test_a_run_answers_every_scalar_metric_exactly_once() -> None:
    run = run_over()
    scalar = [
        result.key.metric
        for result in run.results
        if result.key.dimension_kind is BusinessMetricDimension.NONE
    ]
    assert sorted(scalar, key=str) == sorted(SCALAR_METRICS, key=str)
    assert len(scalar) == len(set(scalar)) == 26


def test_a_run_holds_one_key_per_observed_source() -> None:
    records = (
        record(1, sources=(source("alpha"), source("beta"))),
        record(2, sources=(source("alpha"),)),
        record(3, sources=()),
    )
    run = run_over(records)
    assert source_keys(run) == ("alpha", "beta")
    alpha = run.result_for(
        BusinessMetricKey(
            BusinessMetricName.OBSERVED_SOURCE_CONTRIBUTION,
            dimension_kind=SOURCE_ID,
            dimension_value="alpha",
        )
    )
    assert alpha.support.numerator == 2
    assert alpha.value == pytest.approx(2 / 3)


def test_a_source_repeated_anywhere_still_yields_one_key() -> None:
    records = (
        record(1, sources=(source("alpha"), source("alpha"))),
        record(2, sources=(source("alpha"),)),
    )
    assert source_keys(run_over(records)) == ("alpha",)


def test_a_declared_but_unobserved_source_gets_no_key() -> None:
    records = (record(1, sources=(source("alpha"),)),)
    run = run_over(
        records,
        declared_source_universe=declared_universe(
            (
                FakeSourceEntry("alpha"),
                FakeSourceEntry("never_seen", integration_status="CANDIDATE"),
            )
        ),
    )
    assert source_keys(run) == ("alpha",)
    assert "never_seen" not in source_keys(run)


def test_no_synthetic_unknown_source_key_is_invented() -> None:
    records = (record(1, sources=()), record(2, sources=()))
    run = run_over(records)
    assert source_keys(run) == ()


def test_a_cohort_with_no_sources_is_a_valid_complete_run() -> None:
    run = run_over((record(1), record(2)))
    assert len(run.results) == len(SCALAR_METRICS)
    assert verify_business_metric_run_structure(run) == run.run_fingerprint


def test_the_results_are_in_canonical_order() -> None:
    records = (
        record(1, sources=(source("zeta"), source("alpha"))),
        record(2, sources=(source("mu"),)),
    )
    run = run_over(records)
    identities = [business_metric_key_sort_key(result.key) for result in run.results]
    assert identities == sorted(identities)
    assert source_keys(run) == ("alpha", "mu", "zeta")


def test_a_caller_cannot_choose_a_subset() -> None:
    import inspect

    parameters = set(inspect.signature(build_business_metric_run).parameters)
    assert parameters == {"context", "provenance"}


# ====================================================================
# all of it, or none of it
# ====================================================================


def test_one_hard_error_prevents_the_whole_run() -> None:
    """An evaluation-ready benchmark makes discovery recall raise, so no run
    exists — not a run of twenty-six results with one omitted."""
    context = computation(
        benchmark_binding=ready_benchmark(3), benchmark_records=benchmark_rows(3)
    )
    with pytest.raises(BusinessMetricContractError, match="no discovery evidence"):
        build_business_metric_run(context)


def test_an_incoherent_record_prevents_the_whole_run() -> None:
    records = (record(1, qualification=qualification(value="NOT_A_VALUE")),)
    with pytest.raises(BusinessMetricBindingError):
        run_over(records)


def test_every_result_of_a_run_is_verifiable() -> None:
    run = run_over()
    for result in run.results:
        assert result.contract_version == BUSINESS_METRIC_CONTRACT_VERSION
        assert result.status in (
            BusinessMetricStatus.COMPUTED,
            BusinessMetricStatus.N_A,
        )
        if result.status is BusinessMetricStatus.COMPUTED:
            assert result.value == (
                result.support.numerator / result.support.denominator
            )


def test_the_run_states_its_versions_and_duplicates_nothing() -> None:
    run = run_over()
    assert run.run_schema_version == BUSINESS_METRIC_RUN_SCHEMA_VERSION
    assert run.contract_version == BUSINESS_METRIC_CONTRACT_VERSION
    assert run.run_schema_version != run.contract_version
    fields = {field.name for field in __import__("dataclasses").fields(run)}
    assert fields == {
        "run_schema_version",
        "contract_version",
        "context",
        "results",
        "run_fingerprint",
        "provenance",
    }
    for absent in (
        "metric_count",
        "computed_count",
        "na_count",
        "values_by_metric",
        "dataset_id",
        "run_context_fingerprint",
    ):
        assert absent not in fields


def test_the_run_is_built_through_the_public_compute_function() -> None:
    """Each stored result equals what the public dispatcher returns."""
    context = computation()
    run = build_business_metric_run(context)
    for result in run.results:
        assert compute_business_metric(context, result.key) == result


# ====================================================================
# the run fingerprint — D9
# ====================================================================


def test_the_run_fingerprint_is_deterministic() -> None:
    context = computation()
    first = build_business_metric_run(context)
    second = build_business_metric_run(computation())
    assert first.run_fingerprint == second.run_fingerprint


def test_provenance_does_not_move_the_run_fingerprint() -> None:
    context = computation()
    bare = build_business_metric_run(context)
    annotated = build_business_metric_run(
        context,
        provenance=BusinessMetricRunProvenance(
            generated_at="2026-07-07T07:07:07Z",
            dataset_directory="data/evaluation/datasets/anything",
            benchmark_records_path="somewhere/gold.jsonl",
        ),
    )
    assert annotated.run_fingerprint == bare.run_fingerprint
    assert annotated.provenance != bare.provenance


def test_a_changed_result_moves_the_run_fingerprint() -> None:
    run = run_over()
    other = run_over(
        (
            record(1, qualification=qualification(value="CORE_TARGET")),
            record(2, qualification=qualification(value="CORE_TARGET")),
            record(3),
        )
    )
    assert other.run_fingerprint != run.run_fingerprint


def test_the_fingerprint_domain_is_exactly_the_contract_projection() -> None:
    run = run_over()
    payload = canonical_business_metric_run_payload(run)
    assert set(payload) == {
        "run_schema_version",
        "contract_version",
        "run_context_fingerprint",
        "results",
    }
    assert payload["run_context_fingerprint"] == (
        run.context.run_context_fingerprint
    )
    assert len(payload["results"]) == len(run.results)
    for entry, result in zip(payload["results"], run.results):
        assert set(entry) == {"key", "result_fingerprint"}
        assert entry["result_fingerprint"] == result.result_fingerprint
        # The key enters by its canonical payload, never by `repr()`.
        assert set(entry["key"]) == {"metric", "dimension_kind", "dimension_value"}
        assert entry["key"]["metric"] == str(result.key.metric)
    rendered = str(payload)
    assert "BusinessMetricKey(" not in rendered


def test_the_fingerprint_excludes_provenance_paths_and_the_clock() -> None:
    run = run_over(
        provenance=BusinessMetricRunProvenance(
            generated_at="2026-07-07T07:07:07Z", dataset_directory="/tmp/whatever"
        )
    )
    rendered = str(canonical_business_metric_run_payload(run))
    for absent in ("2026-07-07", "/tmp/whatever", "provenance", "generated_at"):
        assert absent not in rendered


def test_an_unknown_run_schema_version_is_refused() -> None:
    run = run_over()
    with pytest.raises(BusinessMetricContractError, match="run schema version"):
        verify_business_metric_run_structure(
            replace(run, run_schema_version="business-metric-run-v2")
        )


# ====================================================================
# structural verification
# ====================================================================


def test_structural_verification_accepts_a_sealed_run() -> None:
    run = run_over()
    assert verify_business_metric_run_structure(run) == run.run_fingerprint


def test_structural_verification_refuses_a_forged_run_fingerprint() -> None:
    run = run_over()
    with pytest.raises(BusinessMetricBindingError, match="refusing an artefact"):
        verify_business_metric_run_structure(replace(run, run_fingerprint="a" * 64))


def test_structural_verification_refuses_a_duplicate_key() -> None:
    run = run_over()
    doubled = replace(run, results=(run.results[0], *run.results))
    with pytest.raises(BusinessMetricBindingError, match="more than one result"):
        verify_business_metric_run_structure(doubled)


def test_structural_verification_refuses_results_out_of_order() -> None:
    run = run_over()
    shuffled = replace(run, results=tuple(reversed(run.results)))
    with pytest.raises(BusinessMetricBindingError, match="canonical key order"):
        verify_business_metric_run_structure(shuffled)


def test_structural_verification_refuses_a_mixed_contract_version() -> None:
    run = run_over()
    mixed = replace(
        run,
        results=(
            replace(run.results[0], contract_version="something-else-v1"),
            *run.results[1:],
        ),
    )
    with pytest.raises(BusinessMetricContractError):
        verify_business_metric_run_structure(mixed)


def test_structural_verification_refuses_an_empty_run() -> None:
    run = run_over()
    with pytest.raises(BusinessMetricContractError, match="at least one result"):
        verify_business_metric_run_structure(replace(run, results=()))


def resealed(run):
    """The same run with its fingerprint recomputed over whatever it now holds.

    What a tamperer would do, and the reason none of the tests below can be
    satisfied by the run's own digest: it is made valid on purpose.
    """
    return replace(run, run_fingerprint=business_metric_run_fingerprint(run))


def test_structural_verification_rebinds_every_result_to_the_run_context() -> None:
    """A result sealed against *other* evidence cannot be dropped into a run.

    Both runs measure the same records; their contexts differ only in the git
    commit the declared source map was read at, which is inside that binding's
    content fingerprint. So the two `ACTIVE_DECLARED_SOURCE_RATE` results carry
    the same value and *different* evidence identities — and the foreign one,
    re-sealed into a run whose fingerprint is then recomputed, is structurally
    impeccable in every way except the one that matters.
    """
    records = DEFAULT_RECORDS
    mine = run_over(records, declared_source_universe=declared_universe())
    theirs = run_over(
        records, declared_source_universe=declared_universe(git_commit="d" * 40)
    )
    key = BusinessMetricKey(BusinessMetricName.ACTIVE_DECLARED_SOURCE_RATE)
    foreign = theirs.result_for(key)
    ours = mine.result_for(key)
    # Same number, and two different identities for the question and the proof.
    assert foreign.value == ours.value
    assert foreign.scope_fingerprint != ours.scope_fingerprint
    assert foreign.evidence_fingerprint != ours.evidence_fingerprint

    spliced = resealed(
        replace(
            mine,
            results=tuple(
                foreign if result.key == key else result for result in mine.results
            ),
        )
    )
    # Its own digest is valid, and so is the run's.
    assert business_metric_run_fingerprint(spliced) == spliced.run_fingerprint
    with pytest.raises(
        BusinessMetricBindingError, match="this context asks"
    ) as error:
        verify_business_metric_run_structure(spliced)
    assert str(key.metric) in str(error.value)


def test_structural_verification_requires_every_scalar_metric() -> None:
    """A dropped scalar metric is structural, and is refused without the records.

    Which metrics the contract defines is a fact this verifier holds. A run that
    simply omits one, re-fingerprinted over what remained, would otherwise report
    twenty-five measurements under a complete run's name.
    """
    run = run_over()
    for missing in (
        BusinessMetricName.DESCRIPTION_PRESENCE_RATE,
        BusinessMetricName.BROKEN_URL_RATE,
        BusinessMetricName.OPPORTUNITY_SKILL_COVERAGE,
    ):
        trimmed = resealed(
            replace(
                run,
                results=tuple(
                    result for result in run.results if result.key.metric is not missing
                ),
            )
        )
        assert business_metric_run_fingerprint(trimmed) == trimmed.run_fingerprint
        with pytest.raises(BusinessMetricBindingError, match="scalar metric"):
            verify_business_metric_run_structure(trimmed)


def test_a_forged_provenance_is_refused_by_the_structural_verifier() -> None:
    """A dataclass is not a proof token, so the block is validated again.

    `object.__new__` skips `__init__`, so `__post_init__` never runs and the
    block reaches the run unchecked. The provenance is outside `run_fingerprint`
    by design, so the digest cannot catch it either — only revalidation can.
    """
    run = run_over()
    for field, value in (
        ("generated_at", "not-a-timestamp"),
        ("generated_at", "2026-03-01T09:15:00"),  # no offset
        ("dataset_directory", "  padded/path  "),
        ("dataset_directory", ""),
        ("benchmark_records_path", 7),
    ):
        forged = object.__new__(BusinessMetricRunProvenance)
        for name in ("generated_at", "dataset_directory", "benchmark_records_path"):
            object.__setattr__(forged, name, None)
        object.__setattr__(forged, field, value)

        tampered = replace(run, provenance=forged)
        # The run's own digest is untouched, because provenance is outside it.
        assert tampered.run_fingerprint == run.run_fingerprint
        with pytest.raises(BusinessMetricsError):
            verify_business_metric_run_structure(tampered)


def test_a_well_formed_provenance_still_passes() -> None:
    run = run_over(
        provenance=BusinessMetricRunProvenance(
            generated_at="2026-07-07T07:07:07Z",
            dataset_directory="data/evaluation/datasets/x",
            benchmark_records_path="evaluation/benchmarks/gold.yaml",
        )
    )
    assert verify_business_metric_run_structure(run) == run.run_fingerprint


def test_structural_verification_does_not_claim_to_check_the_arithmetic() -> None:
    """A run missing a dynamic source key passes structurally and fails fully.

    This is the boundary the two verifiers are separated along, asserted rather
    than merely documented.
    """
    records = (record(1, sources=(source("alpha"),)),)
    manifest = manifest_payload(records)
    run = run_over(records)
    trimmed_results = tuple(
        result
        for result in run.results
        if result.key.metric is not BusinessMetricName.OBSERVED_SOURCE_CONTRIBUTION
    )
    trimmed = replace(run, results=trimmed_results)
    trimmed = replace(
        trimmed, run_fingerprint=business_metric_run_fingerprint(trimmed)
    )
    # Structurally impeccable...
    assert verify_business_metric_run_structure(trimmed) == trimmed.run_fingerprint
    # ...and not a complete run over these records.
    with pytest.raises(BusinessMetricBindingError, match="missing"):
        verify_business_metric_run(trimmed, records, manifest)


# ====================================================================
# full verification
# ====================================================================


def test_full_verification_accepts_a_sealed_run() -> None:
    records = DEFAULT_RECORDS
    run = run_over(records)
    assert (
        verify_business_metric_run(run, records, manifest_payload(records))
        == run.run_fingerprint
    )


def test_full_verification_catches_a_forged_numeric_result() -> None:
    """The gap Phase 10.4a documented and could not close.

    The forged result is re-sealed so that its own `result_fingerprint` is valid
    over its content, and the run's fingerprint is recomputed over it. Every
    digest is impeccable; only recomputing the metric reveals the number.
    """
    from evaluation.business_metrics import business_metric_result_fingerprint

    records = DEFAULT_RECORDS
    manifest = manifest_payload(records)
    run = run_over(records)
    # A metric with no required partition beside it, so nothing in the contract
    # can contradict the forgery: one of three records states a description, and
    # the forged result claims all three do — internally consistent, with
    # `value` still the exact quotient of its own fraction.
    target = run.result_for(
        BusinessMetricKey(BusinessMetricName.DESCRIPTION_PRESENCE_RATE)
    )
    assert target.value == pytest.approx(1 / 3)
    forged = replace(
        target,
        value=1.0,
        support=replace(target.support, numerator=3, denominator=3),
    )
    forged = replace(
        forged, result_fingerprint=business_metric_result_fingerprint(forged)
    )
    results = tuple(
        forged if result.key == target.key else result for result in run.results
    )
    tampered = replace(run, results=results)
    tampered = replace(
        tampered, run_fingerprint=business_metric_run_fingerprint(tampered)
    )
    # Structurally valid — which is exactly the danger.
    assert verify_business_metric_run_structure(tampered) == tampered.run_fingerprint
    with pytest.raises(BusinessMetricBindingError, match="recomputing it"):
        verify_business_metric_run(tampered, records, manifest)


def test_full_verification_catches_an_extra_key() -> None:
    records = (record(1, sources=(source("alpha"),)),)
    manifest = manifest_payload(records)
    run = run_over(records)
    duplicate = run.results[0]
    extra = replace(
        run,
        results=tuple(
            sorted(
                (*run.results, replace(duplicate, key=duplicate.key)),
                key=lambda item: business_metric_key_sort_key(item.key),
            )
        ),
    )
    with pytest.raises(BusinessMetricBindingError):
        verify_business_metric_run(extra, records, manifest)


def test_full_verification_catches_altered_records() -> None:
    records = DEFAULT_RECORDS
    run = run_over(records)
    altered = (
        replace_record(records[0], description="edited after the fact"),
        *records[1:],
    )
    with pytest.raises(BusinessMetricBindingError):
        verify_business_metric_run(run, altered, manifest_payload(altered))


def replace_record(record_mapping, **overrides):
    return {**record_mapping, **overrides}


def test_full_verification_catches_an_altered_manifest() -> None:
    records = DEFAULT_RECORDS
    run = run_over(records)
    manifest = dict(manifest_payload(records))
    manifest["generated_at"] = "2026-04-01T09:15:00+00:00"
    with pytest.raises(BusinessMetricBindingError):
        verify_business_metric_run(run, records, manifest)


def test_full_verification_catches_a_benchmark_mismatch() -> None:
    """Rows that are not the bound benchmark's rows are refused on re-verification.

    The run itself binds a benchmark that is not evaluation-ready — the state of
    the world today — so it builds normally. Handing the verifier a different set
    of gold rows must still fail: they do not digest to what the binding names.
    """
    records = DEFAULT_RECORDS
    manifest = manifest_payload(records)
    run = run_over(records)
    binding = run.context.benchmark_binding
    assert binding is not None

    # The wrong number of rows.
    with pytest.raises(BusinessMetricBindingError, match="row"):
        verify_business_metric_run(
            run, records, manifest, benchmark_records=benchmark_rows(5)
        )
    # And the right number of the wrong rows, which only the digest catches.
    right_count = benchmark_rows(binding.record_count)
    tampered = (*right_count[:-1], {**right_count[-1], "title": "edited"})
    with pytest.raises(BusinessMetricBindingError, match="digest to"):
        verify_business_metric_run(
            run, records, manifest, benchmark_records=tampered
        )


def test_full_verification_recomputes_the_exact_outputs() -> None:
    records = DEFAULT_RECORDS
    manifest = manifest_payload(records)
    run = run_over(records)
    context = computation(records)
    for result in run.results:
        assert compute_business_metric(context, result.key) == result
    assert verify_business_metric_run(run, records, manifest) == run.run_fingerprint


def test_full_verification_refuses_a_run_built_over_other_evidence() -> None:
    records = DEFAULT_RECORDS
    manifest = manifest_payload(records)
    run = run_over(records)
    # The same records, but a run context that assembled less evidence: the
    # stored run rests on an assembly these inputs do not reproduce.
    with pytest.raises(BusinessMetricBindingError):
        verify_business_metric_run(
            replace(run, context=computation(records, full=False).run_context),
            records,
            manifest,
        )


def test_a_run_is_not_a_business_metric_run() -> None:
    with pytest.raises(BusinessMetricContractError, match="not a business metric run"):
        verify_business_metric_run_structure(object())


def test_result_for_refuses_a_key_the_run_does_not_hold() -> None:
    run = run_over((record(1),))
    with pytest.raises(BusinessMetricBindingError, match="no result for"):
        run.result_for(
            BusinessMetricKey(
                BusinessMetricName.OBSERVED_SOURCE_CONTRIBUTION,
                dimension_kind=SOURCE_ID,
                dimension_value="nobody",
            )
        )
