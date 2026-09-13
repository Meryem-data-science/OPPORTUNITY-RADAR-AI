"""One immutable content-addressed document per experiment run.

These tests are about `storage.py`, and they are deliberately the same shape as
Phase 10.4's storage tests: the hardening there was arrived at by getting it
wrong twice, and the lessons — a semantic identity projection rather than
"document minus provenance", `os.link` rather than `os.replace`, best-effort
cleanup that cannot fail a publication, the stored object returned on UNCHANGED,
strict key sets, no repair, no overwrite — are re-established here rather than
assumed to carry over.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from evaluation.experiments import (
    DEFAULT_EXPERIMENT_RUN_ROOT,
    ExperimentBindingError,
    ExperimentContractError,
    ExperimentRunProvenance,
    ExperimentRunWriteStatus,
    ExperimentsError,
    build_experiment_run,
    experiment_run_fingerprint,
    read_experiment_run,
    verify_experiment_run,
    verify_experiment_run_structure,
    write_experiment_run,
)
from evaluation.experiments.storage import RUN_FILENAME

from tests.unit.experiment_fixtures import experiment_context, ranked_records


@pytest.fixture()
def run_and_context():
    context = experiment_context(ranked_records(size=10, ranked=6))
    return build_experiment_run(context), context


def stored_document(root: Path, fingerprint: str) -> dict:
    return json.loads((root / fingerprint / RUN_FILENAME).read_text("utf-8"))


# ====================================================================
# where a run lands
# ====================================================================


def test_the_default_root_is_under_the_local_data_space() -> None:
    assert DEFAULT_EXPERIMENT_RUN_ROOT == Path(
        "data/evaluation/experiment_runs"
    )


def test_the_store_is_content_addressed_by_the_run_fingerprint(
    tmp_path, run_and_context
) -> None:
    run, context = run_and_context
    result = write_experiment_run(run, context, root=tmp_path)
    assert result.directory == tmp_path / run.run_fingerprint
    assert result.run_file == result.directory / RUN_FILENAME
    assert result.run_file.is_file()


def test_there_is_exactly_one_document(tmp_path, run_and_context) -> None:
    run, context = run_and_context
    result = write_experiment_run(run, context, root=tmp_path)
    assert [path.name for path in sorted(result.directory.iterdir())] == [
        RUN_FILENAME
    ]


def test_the_rendering_is_stable_utf8_sorted_and_newline_terminated(
    tmp_path, run_and_context
) -> None:
    run, context = run_and_context
    result = write_experiment_run(run, context, root=tmp_path)
    text = result.run_file.read_text("utf-8")
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    payload = json.loads(text)
    # `sort_keys=True` and `indent=2`.
    assert text == json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"


# ====================================================================
# CREATED and UNCHANGED
# ====================================================================


def test_a_new_run_is_created(tmp_path, run_and_context) -> None:
    run, context = run_and_context
    result = write_experiment_run(run, context, root=tmp_path)
    assert result.status is ExperimentRunWriteStatus.CREATED
    assert result.run is run


def test_writing_the_same_run_twice_is_unchanged(tmp_path, run_and_context) -> None:
    run, context = run_and_context
    write_experiment_run(run, context, root=tmp_path)
    before = stored_document(tmp_path, run.run_fingerprint)
    result = write_experiment_run(run, context, root=tmp_path)
    assert result.status is ExperimentRunWriteStatus.UNCHANGED
    assert stored_document(tmp_path, run.run_fingerprint) == before


def test_unchanged_returns_the_run_that_is_actually_stored(
    tmp_path, run_and_context
) -> None:
    """Not the caller's own object with a status attached."""
    run, context = run_and_context
    first = ExperimentRunProvenance(generated_at="2026-01-01T00:00:00+00:00")
    second = ExperimentRunProvenance(generated_at="2026-06-06T06:06:06+00:00")
    write_experiment_run(
        build_experiment_run(context, provenance=first), context, root=tmp_path
    )
    later = build_experiment_run(context, provenance=second)
    result = write_experiment_run(later, context, root=tmp_path)
    assert result.status is ExperimentRunWriteStatus.UNCHANGED
    # The provenance on disk is the original one.
    assert result.run.provenance.generated_at == first.generated_at
    assert result.run is not later


def test_a_provenance_only_difference_is_the_same_run(
    tmp_path, run_and_context
) -> None:
    """The semantic identity projection, not the document minus a block."""
    run, context = run_and_context
    moved = build_experiment_run(
        context,
        provenance=ExperimentRunProvenance(
            generated_at="2030-03-03T03:03:03+00:00",
            dataset_directory="/another/checkout/datasets/x",
            business_metric_run_path="/another/checkout/runs/y",
            label_root="/another/checkout/labels",
            benchmark_records_path="/another/checkout/gold.jsonl",
        ),
    )
    assert moved.run_fingerprint == run.run_fingerprint
    write_experiment_run(run, context, root=tmp_path)
    result = write_experiment_run(moved, context, root=tmp_path)
    assert result.status is ExperimentRunWriteStatus.UNCHANGED


def test_two_different_runs_land_in_two_directories(tmp_path) -> None:
    first_context = experiment_context(ranked_records(size=10, ranked=6))
    second_context = experiment_context(ranked_records(size=10, ranked=5))
    first = write_experiment_run(
        build_experiment_run(first_context), first_context, root=tmp_path
    )
    second = write_experiment_run(
        build_experiment_run(second_context), second_context, root=tmp_path
    )
    assert first.directory != second.directory
    assert first.status is second.status is ExperimentRunWriteStatus.CREATED


# ====================================================================
# full verification happens before any official I/O
# ====================================================================


def test_a_run_that_would_not_verify_is_never_written(
    tmp_path, run_and_context
) -> None:
    run, context = run_and_context
    forged = replace(
        run,
        projections=(
            replace(
                run.projections[4],
                included_opportunity_ids=run.projections[4].included_ids[:-1],
            ),
            *run.projections[:4],
        ),
    )
    forged = replace(forged, run_fingerprint=experiment_run_fingerprint(forged))
    with pytest.raises(ExperimentsError):
        write_experiment_run(forged, context, root=tmp_path)
    # Nothing at all was created.
    assert list(tmp_path.iterdir()) == []


def test_a_run_verified_against_the_wrong_context_is_never_written(
    tmp_path, run_and_context
) -> None:
    run, _ = run_and_context
    other = experiment_context(ranked_records(size=9, ranked=5))
    with pytest.raises(ExperimentBindingError):
        write_experiment_run(run, other, root=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_the_writer_requires_a_context_rather_than_accepting_none(
    run_and_context,
) -> None:
    """A structural check alone cannot establish a membership."""
    import inspect

    signature = inspect.signature(write_experiment_run)
    assert signature.parameters["context"].default is inspect.Parameter.empty


# ====================================================================
# an existing destination is verified, never overwritten
# ====================================================================


def test_a_divergent_document_is_a_hard_error_and_nothing_changes(
    tmp_path, run_and_context
) -> None:
    run, context = run_and_context
    write_experiment_run(run, context, root=tmp_path)
    # Put another run's document under this run's fingerprint.
    other_context = experiment_context(ranked_records(size=10, ranked=5))
    other = build_experiment_run(other_context)
    path = tmp_path / run.run_fingerprint / RUN_FILENAME
    from evaluation.experiments import experiment_run_payload

    path.write_text(
        json.dumps(experiment_run_payload(other), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ExperimentBindingError):
        write_experiment_run(run, context, root=tmp_path)


def test_there_is_no_conflict_status(run_and_context) -> None:
    assert [str(item) for item in ExperimentRunWriteStatus] == [
        "CREATED",
        "UNCHANGED",
    ]


def test_corrupt_json_is_refused_rather_than_repaired(
    tmp_path, run_and_context
) -> None:
    run, context = run_and_context
    write_experiment_run(run, context, root=tmp_path)
    path = tmp_path / run.run_fingerprint / RUN_FILENAME
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ExperimentBindingError, match="not valid JSON"):
        read_experiment_run(run.run_fingerprint, root=tmp_path)
    with pytest.raises(ExperimentBindingError, match="not valid JSON"):
        write_experiment_run(run, context, root=tmp_path)
    # And the corrupt bytes are still there: nothing was repaired.
    assert path.read_text("utf-8") == "{not json"


def test_an_unknown_field_is_refused(tmp_path, run_and_context) -> None:
    run, context = run_and_context
    write_experiment_run(run, context, root=tmp_path)
    path = tmp_path / run.run_fingerprint / RUN_FILENAME
    payload = json.loads(path.read_text("utf-8"))
    payload["confidence"] = 0.9
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ExperimentBindingError, match="unexpected"):
        read_experiment_run(run.run_fingerprint, root=tmp_path)


def test_a_missing_field_is_refused(tmp_path, run_and_context) -> None:
    run, context = run_and_context
    write_experiment_run(run, context, root=tmp_path)
    path = tmp_path / run.run_fingerprint / RUN_FILENAME
    payload = json.loads(path.read_text("utf-8"))
    del payload["ranking"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ExperimentBindingError, match="missing"):
        read_experiment_run(run.run_fingerprint, root=tmp_path)


def test_a_missing_nested_field_is_refused(tmp_path, run_and_context) -> None:
    run, context = run_and_context
    write_experiment_run(run, context, root=tmp_path)
    path = tmp_path / run.run_fingerprint / RUN_FILENAME
    payload = json.loads(path.read_text("utf-8"))
    del payload["projections"][0]["cohort_share"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ExperimentBindingError, match="missing"):
        read_experiment_run(run.run_fingerprint, root=tmp_path)


def test_an_edited_value_whose_digest_was_left_alone_is_refused(
    tmp_path, run_and_context
) -> None:
    run, context = run_and_context
    write_experiment_run(run, context, root=tmp_path)
    path = tmp_path / run.run_fingerprint / RUN_FILENAME
    payload = json.loads(path.read_text("utf-8"))
    for block in payload["projections"]:
        if block["projection"] == "RECOMMENDATION_OBSERVED":
            block["included_count"] = block["included_count"] + 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ExperimentBindingError):
        read_experiment_run(run.run_fingerprint, root=tmp_path)


def test_an_incomplete_directory_is_refused(tmp_path, run_and_context) -> None:
    run, context = run_and_context
    (tmp_path / run.run_fingerprint).mkdir(parents=True)
    with pytest.raises(ExperimentBindingError, match="incomplete"):
        read_experiment_run(run.run_fingerprint, root=tmp_path)
    with pytest.raises(ExperimentBindingError, match="incomplete"):
        write_experiment_run(run, context, root=tmp_path)


def test_a_document_moved_into_another_directory_is_refused(
    tmp_path, run_and_context
) -> None:
    run, context = run_and_context
    result = write_experiment_run(run, context, root=tmp_path)
    elsewhere = tmp_path / ("f" * 64)
    elsewhere.mkdir()
    (elsewhere / RUN_FILENAME).write_text(
        result.run_file.read_text("utf-8"), encoding="utf-8"
    )
    with pytest.raises(ExperimentBindingError, match="its own content names"):
        read_experiment_run("f" * 64, root=tmp_path)


def test_an_existing_publication_cannot_be_raced_over(
    tmp_path, run_and_context, monkeypatch
) -> None:
    """`os.link` refuses the destination rather than overwriting it."""
    run, context = run_and_context
    directory = tmp_path / run.run_fingerprint
    directory.mkdir(parents=True)
    (directory / RUN_FILENAME).write_text("{}", encoding="utf-8")
    with pytest.raises(ExperimentBindingError):
        write_experiment_run(run, context, root=tmp_path)


def test_publication_uses_os_link_and_never_os_replace() -> None:
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("evaluation/experiments/storage.py").read_text())
    calls = {
        f"{node.func.value.id}.{node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
    }
    assert "os.link" in calls
    assert "os.replace" not in calls
    assert "os.rename" not in calls


def test_a_cleanup_failure_after_publish_does_not_fail_the_write(
    tmp_path, run_and_context, monkeypatch
) -> None:
    """The run is frozen from the moment `run.json` exists."""
    run, context = run_and_context
    import evaluation.experiments.storage as module

    original = Path.unlink

    def exploding(self, *args, **kwargs):
        if self.suffix == ".tmp":
            raise OSError("cannot unlink the temporary name")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", exploding)
    result = module.write_experiment_run(run, context, root=tmp_path)
    assert result.status is ExperimentRunWriteStatus.CREATED
    assert result.run_file.is_file()


def test_no_repair_or_overwrite_path_exists_in_the_module() -> None:
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("evaluation/experiments/storage.py").read_text())
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for forbidden in ("repair", "overwrite", "migrate", "fix"):
        assert not any(forbidden in name for name in names), forbidden


# ====================================================================
# reading back
# ====================================================================


def test_a_stored_run_reads_back_and_verifies(tmp_path, run_and_context) -> None:
    run, context = run_and_context
    write_experiment_run(run, context, root=tmp_path)
    back = read_experiment_run(run.run_fingerprint, root=tmp_path)
    assert back.run_fingerprint == run.run_fingerprint
    assert verify_experiment_run_structure(back) == run.run_fingerprint
    assert verify_experiment_run(back, context) == run.run_fingerprint


def test_the_readback_holds_the_same_twelve_blocks(tmp_path, run_and_context) -> None:
    run, context = run_and_context
    write_experiment_run(run, context, root=tmp_path)
    back = read_experiment_run(run.run_fingerprint, root=tmp_path)
    assert [item.projection for item in back.projections] == [
        item.projection for item in run.projections
    ]
    assert [item.pair for item in back.overlaps] == [
        item.pair for item in run.overlaps
    ]
    assert back.ranking.result_fingerprint == run.ranking.result_fingerprint
    assert [
        (entry.metric, entry.k_requested, entry.result.value)
        for entry in back.ranking.metric_results
    ] == [
        (entry.metric, entry.k_requested, entry.result.value)
        for entry in run.ranking.metric_results
    ]


def test_an_na_block_survives_the_round_trip(tmp_path) -> None:
    context = experiment_context(
        ranked_records(size=8, ranked=5), target_country=None
    )
    run = build_experiment_run(context)
    write_experiment_run(run, context, root=tmp_path)
    back = read_experiment_run(run.run_fingerprint, root=tmp_path)
    from evaluation.experiments import (
        CohortProjection,
        OverlapPair,
        OverlapStatus,
        ProjectionStatus,
        ProjectionUnavailableReason,
    )

    geo = back.projection(CohortProjection.GEO_NOT_EXPLICITLY_OUT_OF_TARGET)
    assert geo.status is ProjectionStatus.N_A
    assert geo.unavailable_reason is (
        ProjectionUnavailableReason.TARGET_COUNTRY_UNAVAILABLE
    )
    assert geo.included_opportunity_ids is None
    assert back.overlap(OverlapPair.GEO_AND_MATCHING).status is OverlapStatus.N_A
    verify_experiment_run(back, context)


def test_an_na_ranking_block_survives_the_round_trip(tmp_path) -> None:
    context = experiment_context(
        ranked_records(size=6, ranked=0), with_ranking=False
    )
    run = build_experiment_run(context)
    write_experiment_run(run, context, root=tmp_path)
    back = read_experiment_run(run.run_fingerprint, root=tmp_path)
    from evaluation.experiments import RankingExperimentStatus

    assert back.ranking.status is RankingExperimentStatus.N_A
    assert back.ranking.metric_results == ()
    assert back.ranking.evidence_class is None
    assert back.context.ranking_evaluation_run_fingerprint is None
    verify_experiment_run(back, context)


def test_a_malformed_requested_fingerprint_is_refused(tmp_path) -> None:
    with pytest.raises(ExperimentBindingError, match="not a SHA-256"):
        read_experiment_run("not-a-digest", root=tmp_path)


def test_an_absent_run_is_refused(tmp_path) -> None:
    with pytest.raises(ExperimentBindingError, match="no experiment run"):
        read_experiment_run("a" * 64, root=tmp_path)


def test_reading_performs_the_structural_verification_only() -> None:
    """It is not given the artefacts, and does not pretend to have them."""
    import inspect

    signature = inspect.signature(read_experiment_run)
    assert list(signature.parameters) == ["run_fingerprint", "root"]
