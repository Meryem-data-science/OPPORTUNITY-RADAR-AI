"""Phase 10.4b persistence: one document per run, written once, never rewritten.

What is asserted here is mostly what *cannot* happen. A stored run is immutable,
so the interesting cases are the refusals: an incomplete directory, a corrupted
document, a forged fingerprint, a schema this build does not read, a key that is
missing or one too many, and a second run that would land on an existing one.

The two behaviours that are not refusals matter just as much:

* **UNCHANGED is byte-exact.** Re-writing the same measurement rewrites nothing,
  and a run differing only in its provenance is the same measurement;
* **the result reports what is on disk**, not what the caller was holding.

Every test writes into a `tmp_path`; nothing here touches
`data/evaluation/business_metric_runs`.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from evaluation.business_metrics import (
    DEFAULT_BUSINESS_METRIC_RUN_ROOT,
    BusinessMetricBindingError,
    BusinessMetricContractError,
    BusinessMetricName,
    BusinessMetricRunProvenance,
    BusinessMetricRunWriteStatus,
    build_business_metric_run,
    read_business_metric_run,
    verify_business_metric_run,
    verify_business_metric_run_structure,
    write_business_metric_run,
)
from evaluation.business_metrics.storage import RUN_FILENAME
from tests.unit.business_metric_fixtures import (
    DEFAULT_RECORDS,
    computation,
    manifest_payload,
    qualification,
    record,
    source,
)


def a_run(records=DEFAULT_RECORDS, **kwargs):
    provenance = kwargs.pop("provenance", None)
    return build_business_metric_run(
        computation(tuple(records), **kwargs), provenance=provenance
    )


def stored_document(root, run) -> dict:
    return json.loads(
        (root / run.run_fingerprint / RUN_FILENAME).read_text(encoding="utf-8")
    )


def rewrite(root, run, document) -> None:
    """Put an edited document back on disk, exactly as a hand edit would."""
    path = root / run.run_fingerprint / RUN_FILENAME
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# ====================================================================
# where a run lives
# ====================================================================


def test_the_default_root_is_the_contracted_path() -> None:
    assert str(DEFAULT_BUSINESS_METRIC_RUN_ROOT).replace("\\", "/") == (
        "data/evaluation/business_metric_runs"
    )


def test_a_run_is_one_document_named_after_its_fingerprint(tmp_path) -> None:
    run = a_run()
    result = write_business_metric_run(run, tmp_path)
    assert result.directory == tmp_path / run.run_fingerprint
    assert result.run_file.name == RUN_FILENAME
    assert result.run_file.is_file()
    # One authoritative document, and nothing beside it.
    assert [path.name for path in result.directory.iterdir()] == [RUN_FILENAME]


# ====================================================================
# CREATED and UNCHANGED
# ====================================================================


def test_the_first_write_creates(tmp_path) -> None:
    run = a_run()
    result = write_business_metric_run(run, tmp_path)
    assert result.status is BusinessMetricRunWriteStatus.CREATED
    assert result.run == run


def test_a_second_identical_write_is_unchanged(tmp_path) -> None:
    run = a_run()
    write_business_metric_run(run, tmp_path)
    before = (tmp_path / run.run_fingerprint / RUN_FILENAME).read_bytes()
    result = write_business_metric_run(run, tmp_path)
    assert result.status is BusinessMetricRunWriteStatus.UNCHANGED
    assert (tmp_path / run.run_fingerprint / RUN_FILENAME).read_bytes() == before


def test_a_provenance_only_difference_is_unchanged_and_rewrites_nothing(
    tmp_path,
) -> None:
    context = computation()
    first = build_business_metric_run(
        context,
        provenance=BusinessMetricRunProvenance(
            generated_at="2026-05-05T05:05:05Z", dataset_directory="first/place"
        ),
    )
    second = build_business_metric_run(
        context,
        provenance=BusinessMetricRunProvenance(
            generated_at="2026-08-08T08:08:08Z", dataset_directory="second/place"
        ),
    )
    assert first.run_fingerprint == second.run_fingerprint
    assert first.provenance != second.provenance

    write_business_metric_run(first, tmp_path)
    path = tmp_path / first.run_fingerprint / RUN_FILENAME
    before = path.read_bytes()
    result = write_business_metric_run(second, tmp_path)
    assert result.status is BusinessMetricRunWriteStatus.UNCHANGED
    assert path.read_bytes() == before


def test_unchanged_returns_the_run_that_is_actually_stored(tmp_path) -> None:
    """The caller learns what the store holds, not what it was holding."""
    context = computation()
    first = build_business_metric_run(
        context,
        provenance=BusinessMetricRunProvenance(generated_at="2026-05-05T05:05:05Z"),
    )
    second = build_business_metric_run(
        context,
        provenance=BusinessMetricRunProvenance(generated_at="2026-08-08T08:08:08Z"),
    )
    write_business_metric_run(first, tmp_path)
    result = write_business_metric_run(second, tmp_path)
    assert result.run.provenance.generated_at == "2026-05-05T05:05:05Z"
    assert result.run == first
    assert result.run != second


def test_the_bytes_are_stable(tmp_path) -> None:
    run = a_run()
    write_business_metric_run(run, tmp_path / "a")
    write_business_metric_run(a_run(), tmp_path / "b")
    first = (tmp_path / "a" / run.run_fingerprint / RUN_FILENAME).read_bytes()
    second = (tmp_path / "b" / run.run_fingerprint / RUN_FILENAME).read_bytes()
    assert first == second
    text = first.decode("utf-8")
    assert text.endswith("\n")
    assert text.startswith("{\n  ")


def test_the_run_fingerprint_is_not_the_digest_of_the_file(tmp_path) -> None:
    import hashlib

    run = a_run(
        provenance=BusinessMetricRunProvenance(generated_at="2026-05-05T05:05:05Z")
    )
    result = write_business_metric_run(run, tmp_path)
    file_digest = hashlib.sha256(result.run_file.read_bytes()).hexdigest()
    assert file_digest != run.run_fingerprint


# ====================================================================
# reading back
# ====================================================================


def test_a_stored_run_round_trips(tmp_path) -> None:
    run = a_run()
    write_business_metric_run(run, tmp_path)
    assert read_business_metric_run(run.run_fingerprint, tmp_path) == run


def test_a_round_trip_survives_a_run_with_every_member_and_a_source_key(
    tmp_path,
) -> None:
    records = (
        record(
            1,
            application_url="https://alpha.example/1",
            absorbed=(11,),
            sources=(source("alpha"), source("beta")),
            published_at="2026-02-27T09:00:00+00:00",
            description="text",
            qualification=qualification(),
        ),
        record(2, source_url="https://beta.example/2", sources=(source("alpha"),)),
    )
    run = a_run(records)
    assert any(
        result.key.metric is BusinessMetricName.OBSERVED_SOURCE_CONTRIBUTION
        for result in run.results
    )
    write_business_metric_run(run, tmp_path)
    restored = read_business_metric_run(run.run_fingerprint, tmp_path)
    assert restored == run
    assert (
        verify_business_metric_run(restored, records, manifest_payload(records))
        == run.run_fingerprint
    )


def test_reading_verifies_rather_than_trusting(tmp_path) -> None:
    run = a_run()
    write_business_metric_run(run, tmp_path)
    restored = read_business_metric_run(run.run_fingerprint, tmp_path)
    assert verify_business_metric_run_structure(restored) == run.run_fingerprint


def test_reading_an_absent_run_is_refused(tmp_path) -> None:
    with pytest.raises(BusinessMetricBindingError, match="no business metric run"):
        read_business_metric_run("a" * 64, tmp_path)


def test_reading_refuses_something_that_is_not_a_fingerprint(tmp_path) -> None:
    with pytest.raises(BusinessMetricBindingError, match="SHA-256"):
        read_business_metric_run("not-a-fingerprint", tmp_path)


def test_a_directory_with_no_document_is_incomplete(tmp_path) -> None:
    run = a_run()
    (tmp_path / run.run_fingerprint).mkdir(parents=True)
    with pytest.raises(BusinessMetricBindingError, match="incomplete"):
        read_business_metric_run(run.run_fingerprint, tmp_path)
    # And a write into it refuses too, rather than repairing it.
    with pytest.raises(BusinessMetricBindingError, match="incomplete"):
        write_business_metric_run(run, tmp_path)


def test_malformed_json_is_refused(tmp_path) -> None:
    run = a_run()
    write_business_metric_run(run, tmp_path)
    (tmp_path / run.run_fingerprint / RUN_FILENAME).write_text(
        "{not json", encoding="utf-8"
    )
    with pytest.raises(BusinessMetricBindingError, match="not valid JSON"):
        read_business_metric_run(run.run_fingerprint, tmp_path)


def test_a_document_that_is_not_an_object_is_refused(tmp_path) -> None:
    run = a_run()
    write_business_metric_run(run, tmp_path)
    (tmp_path / run.run_fingerprint / RUN_FILENAME).write_text(
        "[1, 2, 3]", encoding="utf-8"
    )
    with pytest.raises(BusinessMetricBindingError, match="not a JSON object"):
        read_business_metric_run(run.run_fingerprint, tmp_path)


# ====================================================================
# strict deserialization
# ====================================================================


def test_an_unexpected_key_is_refused(tmp_path) -> None:
    run = a_run()
    write_business_metric_run(run, tmp_path)
    document = stored_document(tmp_path, run)
    document["something_new"] = True
    rewrite(tmp_path, run, document)
    with pytest.raises(BusinessMetricBindingError, match="unexpected"):
        read_business_metric_run(run.run_fingerprint, tmp_path)


def test_a_missing_key_is_refused(tmp_path) -> None:
    run = a_run()
    write_business_metric_run(run, tmp_path)
    document = stored_document(tmp_path, run)
    del document["provenance"]
    rewrite(tmp_path, run, document)
    with pytest.raises(BusinessMetricBindingError, match="missing"):
        read_business_metric_run(run.run_fingerprint, tmp_path)


def test_an_unexpected_key_deep_in_the_document_is_refused(tmp_path) -> None:
    run = a_run()
    write_business_metric_run(run, tmp_path)
    document = stored_document(tmp_path, run)
    document["context"]["frozen_cohort_binding"]["extra"] = 1
    rewrite(tmp_path, run, document)
    with pytest.raises(BusinessMetricBindingError, match="unexpected"):
        read_business_metric_run(run.run_fingerprint, tmp_path)


def test_an_unexpected_key_in_a_result_is_refused(tmp_path) -> None:
    run = a_run()
    write_business_metric_run(run, tmp_path)
    document = stored_document(tmp_path, run)
    document["results"][0]["support"]["extra_block"] = None
    rewrite(tmp_path, run, document)
    with pytest.raises(BusinessMetricBindingError, match="unexpected"):
        read_business_metric_run(run.run_fingerprint, tmp_path)


def test_an_unknown_run_schema_version_is_refused(tmp_path) -> None:
    run = a_run()
    write_business_metric_run(run, tmp_path)
    document = stored_document(tmp_path, run)
    document["run_schema_version"] = "business-metric-run-v99"
    rewrite(tmp_path, run, document)
    with pytest.raises(BusinessMetricContractError, match="run schema version"):
        read_business_metric_run(run.run_fingerprint, tmp_path)


def test_an_unknown_contract_version_is_refused(tmp_path) -> None:
    run = a_run()
    write_business_metric_run(run, tmp_path)
    document = stored_document(tmp_path, run)
    document["contract_version"] = "business-data-quality-metric-contract-v99"
    rewrite(tmp_path, run, document)
    with pytest.raises(BusinessMetricContractError, match="contract version"):
        read_business_metric_run(run.run_fingerprint, tmp_path)


def test_an_unknown_enum_member_is_refused_and_never_coerced(tmp_path) -> None:
    run = a_run()
    write_business_metric_run(run, tmp_path)
    document = stored_document(tmp_path, run)
    document["results"][0]["status"] = "PROBABLY"
    rewrite(tmp_path, run, document)
    with pytest.raises(BusinessMetricContractError, match="not a member of"):
        read_business_metric_run(run.run_fingerprint, tmp_path)


def test_a_manually_forged_stored_fingerprint_is_refused(tmp_path) -> None:
    run = a_run()
    write_business_metric_run(run, tmp_path)
    document = stored_document(tmp_path, run)
    document["run_fingerprint"] = "b" * 64
    rewrite(tmp_path, run, document)
    with pytest.raises(BusinessMetricBindingError):
        read_business_metric_run(run.run_fingerprint, tmp_path)


def test_an_edited_value_is_refused_even_with_its_digest_left_alone(
    tmp_path,
) -> None:
    """A hand edit that keeps every stated digest still fails: they are recomputed."""
    run = a_run()
    write_business_metric_run(run, tmp_path)
    document = stored_document(tmp_path, run)
    for result in document["results"]:
        if result["status"] == "COMPUTED":
            result["value"] = 0.123456
            break
    rewrite(tmp_path, run, document)
    with pytest.raises(BusinessMetricBindingError):
        read_business_metric_run(run.run_fingerprint, tmp_path)


def test_a_document_in_the_wrong_directory_is_refused(tmp_path) -> None:
    run = a_run()
    write_business_metric_run(run, tmp_path)
    elsewhere = tmp_path / ("c" * 64)
    elsewhere.mkdir()
    (elsewhere / RUN_FILENAME).write_text(
        (tmp_path / run.run_fingerprint / RUN_FILENAME).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(BusinessMetricBindingError, match="stored under the directory"):
        read_business_metric_run("c" * 64, tmp_path)


def test_the_storage_module_uses_no_pickle_and_no_eval() -> None:
    """Read from the syntax tree, because the module *documents* what it refuses."""
    import ast
    from pathlib import Path

    source_path = (
        Path(__file__).resolve().parents[2]
        / "evaluation/business_metrics/storage.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any("pickle" in module for module in imported), imported
    called = {
        getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "eval" not in called
    assert "exec" not in called


# ====================================================================
# never an overwrite
# ====================================================================


def test_two_different_runs_are_stored_side_by_side(tmp_path) -> None:
    """Content addressing means different measurements cannot collide at all."""
    first = a_run()
    second = a_run((record(1, description="different"), record(2)))
    assert first.run_fingerprint != second.run_fingerprint
    one = write_business_metric_run(first, tmp_path)
    two = write_business_metric_run(second, tmp_path)
    assert one.status is two.status is BusinessMetricRunWriteStatus.CREATED
    assert one.directory != two.directory
    assert read_business_metric_run(first.run_fingerprint, tmp_path) == first
    assert read_business_metric_run(second.run_fingerprint, tmp_path) == second


def test_writing_over_a_divergent_document_is_refused(tmp_path) -> None:
    """A collision is forced, since content addressing prevents a natural one.

    One run's document is placed under another's fingerprint by hand — what a
    partial restore or a careless copy produces — and the write refuses rather
    than replacing it, leaving the bytes exactly as they were.
    """
    first = a_run()
    second = a_run((record(1, description="different"), record(2)))
    write_business_metric_run(first, tmp_path)
    write_business_metric_run(second, tmp_path)

    victim = tmp_path / first.run_fingerprint / RUN_FILENAME
    intruder = (tmp_path / second.run_fingerprint / RUN_FILENAME).read_text(
        encoding="utf-8"
    )
    victim.write_text(intruder, encoding="utf-8")

    with pytest.raises(BusinessMetricBindingError):
        write_business_metric_run(first, tmp_path)
    assert victim.read_text(encoding="utf-8") == intruder


def test_a_failed_publication_leaves_no_trusted_run(tmp_path, monkeypatch) -> None:
    """An interrupted write never produces a readable run.

    `os.replace` is made to fail at the moment of publication — after the
    temporary file is complete and the directory exists — which is the narrowest
    window there is. What is left behind must be refused rather than read.
    """
    import evaluation.business_metrics.storage as storage_module

    run = a_run()

    def explode(*args, **kwargs):
        raise OSError("the disk went away")

    monkeypatch.setattr(storage_module.os, "replace", explode)
    with pytest.raises(BusinessMetricBindingError, match="cannot write"):
        write_business_metric_run(run, tmp_path)

    # No document, so nothing can be read as a run...
    with pytest.raises(BusinessMetricBindingError, match="incomplete"):
        read_business_metric_run(run.run_fingerprint, tmp_path)
    # ...and no temporary file was left lying about under the root.
    leftovers = [path.name for path in tmp_path.iterdir() if path.is_file()]
    assert leftovers == []


def test_the_temporary_file_is_never_the_official_destination(tmp_path) -> None:
    run = a_run()
    result = write_business_metric_run(run, tmp_path)
    assert result.run_file.name == RUN_FILENAME
    assert not any(
        path.name.endswith(".tmp") for path in tmp_path.rglob("*")
    )


def test_writing_verifies_the_run_before_touching_the_disk(tmp_path) -> None:
    run = a_run()
    broken = replace(run, run_fingerprint="d" * 64)
    with pytest.raises(BusinessMetricBindingError):
        write_business_metric_run(broken, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_the_storage_result_reports_what_it_did(tmp_path) -> None:
    run = a_run()
    created = write_business_metric_run(run, tmp_path)
    unchanged = write_business_metric_run(run, tmp_path)
    assert created.status is BusinessMetricRunWriteStatus.CREATED
    assert unchanged.status is BusinessMetricRunWriteStatus.UNCHANGED
    assert created.directory == unchanged.directory
    assert created.run_file == unchanged.run_file
    assert set(BusinessMetricRunWriteStatus) == {
        BusinessMetricRunWriteStatus.CREATED,
        BusinessMetricRunWriteStatus.UNCHANGED,
    }
