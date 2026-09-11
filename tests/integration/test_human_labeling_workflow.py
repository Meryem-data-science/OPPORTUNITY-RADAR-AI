"""Phase 10.2 against real files: the integrity gate, the label store, the CLI.

Every dataset here is written by Phase 10.1's own `write_evaluation_dataset`,
from invented opportunities, into a `tmp_path`. That matters: the integrity gate
is checked against genuine Phase 10.1 output rather than against a hand-rolled
approximation of it, so a disagreement between the writer and this reader shows
up here instead of on the operator's machine.

Nothing in this module opens the operational database, and no real opportunity
appears anywhere in it.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from evaluation.dataset import (
    EVALUATION_CANONICAL_ORDER,
    EVALUATION_COHORT_CRITERIA,
    EVALUATION_COHORT_VERSION,
    EVALUATION_DATASET_SCHEMA_VERSION,
    EvaluationCohortDefinition,
    EvaluationDataset,
    EvaluationDatasetManifest,
    EvaluationGeographySegmentRecord,
    EvaluationMatchingRecord,
    EvaluationOpportunityRecord,
    EvaluationProfileContext,
    EvaluationQualificationRecord,
    EvaluationRecommendationRecord,
    EvaluationSourceIdentity,
    EvaluationSourceRecord,
    EvaluationUpstreamProvenance,
    evaluation_content_fingerprint,
    write_evaluation_dataset,
)
from evaluation.labeling import (
    CALIBRATION_SELECTOR_VERSION,
    FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION,
    HUMAN_LABEL_PROTOCOL_VERSION,
    HUMAN_LABEL_SCHEMA_VERSION,
    RELEVANCE_GRADE_NAMES,
    WITHHELD_RECORD_FIELDS,
    DataAiJudgment,
    FrozenDatasetIntegrityError,
    GeoJudgment,
    HumanLabelError,
    LabelDiagnostics,
    append_human_label,
    assert_selection_bindings,
    calibration_selection_fingerprint,
    build_labelset_report,
    load_calibration_selection,
    read_frozen_dataset,
    read_label_history,
    read_validated_label_history,
    resolve_effective_labels,
    select_calibration_sample,
    write_calibration_selection,
    write_labelset_manifest,
)
from evaluation.labeling.cli import main

PROFILE_CONTEXT = EvaluationProfileContext(
    profile_id=1, user_id=1, fingerprint="9" * 64
)

COHORT = EvaluationCohortDefinition(
    version=EVALUATION_COHORT_VERSION,
    criteria=EVALUATION_COHORT_CRITERIA,
    ordering=EVALUATION_CANONICAL_ORDER,
    excluded_counts={"merged_duplicate": 0, "inactive": 0},
)

UPSTREAM = EvaluationUpstreamProvenance(
    matching_status="READY",
    matching_run_id=3,
    matching_run_fingerprint="b" * 64,
    matching_engine_version="matching-engine-v1",
    matching_rules_version="matching-rules-v1",
    matching_selection_version="matching-selection-v1",
    recommendation_status="READY",
    recommendation_run_id=1,
    recommendation_run_fingerprint="c" * 64,
    recommendation_engine_version="recommendation-engine-v1",
    recommendation_rules_version="recommendation-rules-v1",
)

SOURCE_IDENTITY = EvaluationSourceIdentity(
    database_path="data/invented.db",
    database_bytes=1024,
    database_sha256="d" * 64,
    wal_present=False,
    wal_bytes=None,
    shm_present=False,
    applied_migrations=("0001_invented",),
)


def opportunity(opportunity_id: int) -> EvaluationOpportunityRecord:
    """One invented posting, varied by id so a lot can span several strata."""
    qualifications = ("CORE_TARGET", "ADJACENT_TARGET", "UNCERTAIN", "OUT_OF_SCOPE")
    qualification = None
    if opportunity_id % 5:
        qualification = EvaluationQualificationRecord(
            qualification=qualifications[opportunity_id % 4],
            primary_domain="DATA_ENGINEERING",
            opportunity_type="PFE",
            employment_type="UNKNOWN",
            listing_quality="NORMAL_LISTING",
            classifier_version="qualification-rules-v2",
            input_fingerprint="a" * 64,
            classified_at="2026-01-01T00:00:00+00:00",
            fine_primary_category="DATA_ENGINEERING",
            fine_secondary_categories=(),
            fine_classifier_version="fine-data-ai-rules-v2",
        )
    recommendation = None
    if opportunity_id % 3:
        recommendation = EvaluationRecommendationRecord(
            rank_position=opportunity_id,
            disposition="RECOMMENDED",
            recommendation_score=0.9 - opportunity_id / 100,
            evidence_coverage=0.8,
            assessment_fingerprint="f" * 64,
        )
    segments: tuple[EvaluationGeographySegmentRecord, ...] = ()
    if opportunity_id % 4:
        segments = (
            EvaluationGeographySegmentRecord(
                segment_position=0,
                raw_segment="Casablanca" if opportunity_id % 2 else "Remote",
                status="RESOLVED" if opportunity_id % 2 else "UNRESOLVED_LOCATION_RULE",
                rule_id="city_ma" if opportunity_id % 2 else "none",
                country_code="MA" if opportunity_id % 2 else None,
                city_key="casablanca" if opportunity_id % 2 else None,
                resolver_version="geography-resolver-v1",
            ),
        )
    return EvaluationOpportunityRecord(
        opportunity_id=opportunity_id,
        canonical_title=f"Invented posting {opportunity_id}",
        organization=f"Invented Org {opportunity_id}",
        opportunity_type=("PFE", "INTERNSHIP", None)[opportunity_id % 3],
        employment_type=None,
        location="Casablanca, Maroc",
        country="MA",
        remote_type="ONSITE",
        description=(
            f"Invented description {opportunity_id}.\n"
            "  Deux espaces et un retour à la ligne conservés tels quels."
        ),
        source_url=f"https://example.invalid/offers/{opportunity_id}",
        application_url=None,
        canonical_url=f"https://example.invalid/offers/{opportunity_id}",
        status="active",
        is_active=True,
        published_at=None,
        deadline=None,
        discovered_at="2026-01-01T00:00:00+00:00",
        first_seen_at="2026-01-01T00:00:00+00:00",
        last_seen_at="2026-01-02T00:00:00+00:00",
        absorbed_duplicate_ids=(),
        sources=(
            EvaluationSourceRecord(
                source_id=("greenhouse", "linkedin", "stagiaires_ma")[
                    opportunity_id % 3
                ],
                source_type=("greenhouse", "linkedin", "stagiaires_ma")[
                    opportunity_id % 3
                ],
                source_url=f"https://example.invalid/offers/{opportunity_id}",
                application_url=None,
                canonical_url=f"https://example.invalid/offers/{opportunity_id}",
                discovered_at="2026-01-01T00:00:00+00:00",
            ),
        ),
        qualification=qualification,
        geography_segments=segments,
        eligibility=None,
        matching=(
            EvaluationMatchingRecord(
                lane="PRIMARY",
                match_quality=0.7,
                evidence_coverage=0.8,
                assessment_fingerprint="e" * 64,
            )
            if opportunity_id % 2
            else None
        ),
        recommendation=recommendation,
    )


def build_dataset(count: int = 12) -> EvaluationDataset:
    records = tuple(opportunity(index) for index in range(1, count + 1))
    fingerprint = evaluation_content_fingerprint(
        schema_version=EVALUATION_DATASET_SCHEMA_VERSION,
        cohort=COHORT,
        profile_context=PROFILE_CONTEXT,
        upstream=UPSTREAM,
        records=records,
    )
    manifest = EvaluationDatasetManifest(
        schema_version=EVALUATION_DATASET_SCHEMA_VERSION,
        dataset_id=f"{EVALUATION_DATASET_SCHEMA_VERSION}-{fingerprint[:16]}",
        generated_at="2026-03-01T00:00:00+00:00",
        git_commit=None,
        record_count=len(records),
        profile_context=PROFILE_CONTEXT,
        source=SOURCE_IDENTITY,
        cohort=COHORT,
        upstream=UPSTREAM,
        content_fingerprint=fingerprint,
    )
    return EvaluationDataset(manifest=manifest, records=records)


@pytest.fixture
def frozen(tmp_path: Path) -> Path:
    """A real frozen dataset directory, written by Phase 10.1's own writer."""
    dataset = build_dataset()
    paths = write_evaluation_dataset(dataset, tmp_path / "datasets")
    return paths.directory


@pytest.fixture
def labels_root(tmp_path: Path) -> Path:
    return tmp_path / "labels"


# --------------------------------------------------------------------------
# the integrity gate
# --------------------------------------------------------------------------


def test_a_frozen_dataset_verifies_and_reports_its_own_identity(frozen: Path):
    dataset = read_frozen_dataset(frozen)
    assert dataset.dataset_id == frozen.name
    assert dataset.record_count == 12
    assert len(dataset.records) == 12
    assert dataset.profile_id == 1
    assert dataset.profile_context_fingerprint == "9" * 64
    assert dataset.opportunity_ids == tuple(range(1, 13))
    assert dataset.dataset_id.startswith(EVALUATION_DATASET_SCHEMA_VERSION)


def test_the_gate_needs_no_database(frozen: Path, tmp_path: Path):
    """Two files are enough; there is no SQLite anywhere near this path."""
    assert sorted(path.name for path in frozen.iterdir()) == [
        "manifest.json",
        "opportunities.jsonl",
    ]
    assert not list(tmp_path.rglob("*.db"))
    read_frozen_dataset(frozen)


def test_a_corrupt_manifest_is_refused(frozen: Path):
    (frozen / "manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(FrozenDatasetIntegrityError, match="cannot be read"):
        read_frozen_dataset(frozen)


def test_a_missing_manifest_is_refused(frozen: Path):
    (frozen / "manifest.json").unlink()
    with pytest.raises(FrozenDatasetIntegrityError, match="manifest.json"):
        read_frozen_dataset(frozen)


def test_an_edited_manifest_that_keeps_its_digest_is_refused(frozen: Path):
    """The digest is recomputed, never believed.

    Here the profile binding is rewritten while the stored `content_fingerprint`
    is left intact — the shape of a hand-edited or partially restored manifest.
    A reader that trusted the stored digest would hand this to an annotator as
    though it described the dataset it names.
    """
    path = frozen / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["profile_context"]["fingerprint"] = "0" * 64
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(FrozenDatasetIntegrityError, match="digest to"):
        read_frozen_dataset(frozen)


def test_a_wrong_content_fingerprint_is_refused(frozen: Path):
    path = frozen / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["content_fingerprint"] = "0" * 64
    manifest["dataset_id"] = f"{EVALUATION_DATASET_SCHEMA_VERSION}-{'0' * 16}"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(FrozenDatasetIntegrityError):
        read_frozen_dataset(frozen)


def test_a_wrong_record_count_is_refused(frozen: Path):
    path = frozen / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["record_count"] = 11
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(FrozenDatasetIntegrityError, match="record_count"):
        read_frozen_dataset(frozen)


def test_a_corrupt_jsonl_line_is_refused(frozen: Path):
    path = frozen / "opportunities.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[3] = "{oops"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(FrozenDatasetIntegrityError, match="not valid JSON"):
        read_frozen_dataset(frozen)


def test_a_truncated_jsonl_is_refused(frozen: Path):
    path = frozen / "opportunities.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()[:-1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(FrozenDatasetIntegrityError, match="record_count"):
        read_frozen_dataset(frozen)


def test_a_record_missing_a_contract_field_is_refused(frozen: Path):
    path = frozen / "opportunities.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    del record["description"]
    lines[0] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(FrozenDatasetIntegrityError, match="record contract"):
        read_frozen_dataset(frozen)


def test_an_edited_record_is_refused(frozen: Path):
    """One character of one description, and the dataset stops verifying."""
    path = frozen / "opportunities.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[2])
    record["description"] = (record["description"] or "") + "."
    lines[2] = json.dumps(record, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(FrozenDatasetIntegrityError, match="digest to"):
        read_frozen_dataset(frozen)


def test_reordered_records_are_refused(frozen: Path):
    path = frozen / "opportunities.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[0], lines[1] = lines[1], lines[0]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(FrozenDatasetIntegrityError, match="canonical order"):
        read_frozen_dataset(frozen)


def test_duplicated_opportunity_ids_are_refused(frozen: Path):
    path = frozen / "opportunities.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[0]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(FrozenDatasetIntegrityError, match="repeats opportunity ids"):
        read_frozen_dataset(frozen)


def test_a_renamed_directory_is_refused(frozen: Path):
    renamed = frozen.parent / "evaluation-dataset-v3-deadbeefdeadbeef"
    frozen.rename(renamed)
    with pytest.raises(FrozenDatasetIntegrityError, match="dataset_id"):
        read_frozen_dataset(renamed)


def test_an_unsupported_schema_version_is_refused(frozen: Path):
    path = frozen / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["schema_version"] = "evaluation-dataset-v99"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(Exception, match="unsupported evaluation dataset schema"):
        read_frozen_dataset(frozen)


# --------------------------------------------------------------------------
# selection artefacts
# --------------------------------------------------------------------------


def test_a_selection_is_written_once_and_then_verified(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    selection = select_calibration_sample(dataset, 5)
    first = write_calibration_selection(selection, labels_root)
    assert first.status == "CREATED"
    second = write_calibration_selection(selection, labels_root)
    assert second.status == "UNCHANGED"
    assert first.path == second.path
    stored = load_calibration_selection(first.path)
    assert stored.opportunity_ids == selection.opportunity_ids
    assert stored.selection_fingerprint == selection.selection_fingerprint


def test_two_lot_sizes_do_not_overwrite_each_other(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    small = write_calibration_selection(
        select_calibration_sample(dataset, 3), labels_root
    )
    large = write_calibration_selection(
        select_calibration_sample(dataset, 7), labels_root
    )
    assert small.path != large.path
    assert small.path.is_file() and large.path.is_file()


def test_an_edited_selection_is_refused(frozen: Path, labels_root: Path):
    dataset = read_frozen_dataset(frozen)
    result = write_calibration_selection(
        select_calibration_sample(dataset, 4), labels_root
    )
    payload = json.loads(result.path.read_text(encoding="utf-8"))
    payload["items"][0]["opportunity_id"] = 999
    result.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(HumanLabelError, match="digest to"):
        load_calibration_selection(result.path)


def test_the_frozen_dataset_directory_is_never_written_to(
    frozen: Path, labels_root: Path
):
    """Phase 10.1 froze that directory. Phase 10.2 does not touch it."""
    before = {path.name: path.read_bytes() for path in frozen.iterdir()}
    dataset = read_frozen_dataset(frozen)
    selection = select_calibration_sample(dataset, 3)
    write_calibration_selection(selection, labels_root)
    append_human_label(
        dataset,
        opportunity_id=selection.opportunity_ids[0],
        relevance_grade=2,
        root=labels_root,
    )
    after = {path.name: path.read_bytes() for path in frozen.iterdir()}
    assert before == after


# --------------------------------------------------------------------------
# writing labels
# --------------------------------------------------------------------------


def test_a_label_is_bound_to_the_dataset_it_was_made_against(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    label = append_human_label(
        dataset,
        opportunity_id=3,
        relevance_grade=3,
        diagnostics=LabelDiagnostics(
            geo_judgment=GeoJudgment.TARGET_GEOGRAPHY,
            data_ai_judgment=DataAiJudgment.UNKNOWN_DATA_AI,
        ),
        reason_tags=["Strong-Fit", "strong-fit"],
        note="  clear PFE in the target city  ",
        root=labels_root,
    )
    assert label.dataset_id == dataset.dataset_id
    assert label.dataset_content_fingerprint == dataset.content_fingerprint
    assert label.profile_id == dataset.profile_id
    assert label.profile_context_fingerprint == dataset.profile_context_fingerprint
    assert label.label_schema_version == HUMAN_LABEL_SCHEMA_VERSION
    assert label.protocol_version == HUMAN_LABEL_PROTOCOL_VERSION
    assert label.reason_tags == ("strong-fit",)
    assert label.note == "clear PFE in the target city"
    assert label.revision == 1

    stored = read_label_history(dataset.dataset_id, labels_root)
    assert len(stored) == 1
    assert stored[0] == label


def test_a_label_carries_no_personal_data(frozen: Path, labels_root: Path):
    """The dataset already holds the profile binding; a label reuses it."""
    dataset = read_frozen_dataset(frozen)
    append_human_label(
        dataset, opportunity_id=1, relevance_grade=1, root=labels_root
    )
    path = labels_root / dataset.dataset_id / "labels.jsonl"
    payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert set(payload) == {
        "label_schema_version",
        "protocol_version",
        "dataset_id",
        "dataset_content_fingerprint",
        "profile_id",
        "profile_context_fingerprint",
        "opportunity_id",
        "relevance_grade",
        "relevance_grade_name",
        "diagnostics",
        "reason_tags",
        "note",
        "labeled_at",
        "revision",
        "relabel_reason",
    }


@pytest.mark.parametrize("grade", [0, 1, 2, 3])
def test_every_rubric_grade_can_be_recorded(
    frozen: Path, labels_root: Path, grade: int
):
    dataset = read_frozen_dataset(frozen)
    label = append_human_label(
        dataset, opportunity_id=2, relevance_grade=grade, root=labels_root
    )
    assert label.relevance_grade == grade


@pytest.mark.parametrize("grade", [-1, 4, True, "2"])
def test_a_grade_outside_the_rubric_is_never_stored(
    frozen: Path, labels_root: Path, grade
):
    dataset = read_frozen_dataset(frozen)
    with pytest.raises(HumanLabelError, match="relevance_grade"):
        append_human_label(
            dataset, opportunity_id=2, relevance_grade=grade, root=labels_root
        )
    assert read_label_history(dataset.dataset_id, labels_root) == ()


def test_an_opportunity_outside_the_dataset_cannot_be_labelled(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    with pytest.raises(HumanLabelError, match="not in dataset"):
        append_human_label(
            dataset, opportunity_id=9999, relevance_grade=2, root=labels_root
        )


def test_a_second_label_is_refused_by_default(frozen: Path, labels_root: Path):
    """No silent overwrite. The refusal names the grade already on file."""
    dataset = read_frozen_dataset(frozen)
    append_human_label(
        dataset, opportunity_id=4, relevance_grade=3, root=labels_root
    )
    with pytest.raises(HumanLabelError, match="already labelled 3"):
        append_human_label(
            dataset, opportunity_id=4, relevance_grade=1, root=labels_root
        )
    history = read_label_history(dataset.dataset_id, labels_root)
    assert [item.relevance_grade for item in history] == [3]


def test_a_relabel_needs_an_explicit_reason(frozen: Path, labels_root: Path):
    dataset = read_frozen_dataset(frozen)
    append_human_label(
        dataset, opportunity_id=4, relevance_grade=3, root=labels_root
    )
    with pytest.raises(HumanLabelError, match="must state why"):
        append_human_label(
            dataset,
            opportunity_id=4,
            relevance_grade=1,
            relabel=True,
            root=labels_root,
        )


def test_a_relabel_reason_without_a_relabel_is_refused(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    with pytest.raises(HumanLabelError, match="without asking for a relabel"):
        append_human_label(
            dataset,
            opportunity_id=4,
            relevance_grade=1,
            relabel_reason="because",
            root=labels_root,
        )


def test_an_explicit_relabel_keeps_both_rows(frozen: Path, labels_root: Path):
    """A correction is an event in the file, not a difference between backups."""
    dataset = read_frozen_dataset(frozen)
    append_human_label(
        dataset, opportunity_id=4, relevance_grade=3, root=labels_root
    )
    corrected = append_human_label(
        dataset,
        opportunity_id=4,
        relevance_grade=1,
        relabel=True,
        relabel_reason="misread the deadline on the first pass",
        root=labels_root,
    )
    assert corrected.revision == 2
    history = read_label_history(dataset.dataset_id, labels_root)
    assert [item.relevance_grade for item in history] == [3, 1]
    assert [item.revision for item in history] == [1, 2]
    effective = resolve_effective_labels(history)
    assert effective[4].relevance_grade == 1
    assert effective[4].relabel_reason == "misread the deadline on the first pass"


def test_relabelling_an_unlabelled_opportunity_is_refused(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    with pytest.raises(HumanLabelError, match="no label to correct"):
        append_human_label(
            dataset,
            opportunity_id=4,
            relevance_grade=1,
            relabel=True,
            relabel_reason="there is nothing here",
            root=labels_root,
        )


def _write_foreign_label(dataset, labels_root: Path, **overrides) -> None:
    """A labels file written by hand, as a restore or a copy-paste would."""
    payload = {
        "label_schema_version": HUMAN_LABEL_SCHEMA_VERSION,
        "protocol_version": HUMAN_LABEL_PROTOCOL_VERSION,
        "dataset_id": dataset.dataset_id,
        "dataset_content_fingerprint": dataset.content_fingerprint,
        "profile_id": dataset.profile_id,
        "profile_context_fingerprint": dataset.profile_context_fingerprint,
        "opportunity_id": 1,
        "relevance_grade": 2,
        "relevance_grade_name": "RELEVANT",
        "diagnostics": {
            "geo_judgment": None,
            "data_ai_judgment": None,
            "opportunity_type_judgment": None,
        },
        "reason_tags": [],
        "note": None,
        "labeled_at": "2026-03-01T10:00:00+00:00",
        "revision": 1,
        "relabel_reason": None,
    }
    payload.update(overrides)
    path = labels_root / dataset.dataset_id / "labels.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def test_a_label_made_against_another_dataset_is_refused(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    selection = select_calibration_sample(dataset, 4)
    _write_foreign_label(
        dataset, labels_root, dataset_id="evaluation-dataset-v3-" + "0" * 16
    )
    with pytest.raises(HumanLabelError, match="belongs to dataset"):
        build_labelset_report(dataset, selection, labels_root)


def test_a_label_made_against_other_contents_is_refused(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    selection = select_calibration_sample(dataset, 4)
    _write_foreign_label(dataset, labels_root, dataset_content_fingerprint="0" * 64)
    with pytest.raises(HumanLabelError, match="content fingerprint"):
        build_labelset_report(dataset, selection, labels_root)


def test_a_label_made_for_another_profile_state_is_refused(
    frozen: Path, labels_root: Path
):
    """A personalised judgement does not survive a change of profile."""
    dataset = read_frozen_dataset(frozen)
    selection = select_calibration_sample(dataset, 4)
    _write_foreign_label(dataset, labels_root, profile_context_fingerprint="0" * 64)
    with pytest.raises(HumanLabelError, match="profile context fingerprint"):
        build_labelset_report(dataset, selection, labels_root)


def test_a_label_file_with_two_rows_at_one_revision_is_refused(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    path = labels_root / dataset.dataset_id / "labels.jsonl"
    _write_foreign_label(dataset, labels_root)
    line = path.read_text(encoding="utf-8").strip()
    other = json.loads(line)
    other["relevance_grade"] = 0
    other["relevance_grade_name"] = "OUT_OF_TARGET"
    path.write_text(line + "\n" + json.dumps(other) + "\n", encoding="utf-8")
    with pytest.raises(HumanLabelError, match="two labels at revision"):
        resolve_effective_labels(read_label_history(dataset.dataset_id, labels_root))


# --------------------------------------------------------------------------
# progress and the labelset digest
# --------------------------------------------------------------------------


def test_unjudged_opportunities_are_reported_and_never_counted_as_zero(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    selection = select_calibration_sample(dataset, 6)
    judged = selection.opportunity_ids[:2]
    append_human_label(
        dataset, opportunity_id=judged[0], relevance_grade=3, root=labels_root
    )
    append_human_label(
        dataset, opportunity_id=judged[1], relevance_grade=0, root=labels_root
    )
    report = build_labelset_report(dataset, selection, labels_root)
    assert report.selected_count == 6
    assert report.judged_count == 2
    assert report.unjudged_count == 4
    assert set(report.unjudged_opportunity_ids) == set(
        selection.opportunity_ids[2:]
    )
    # Exactly one zero: the one somebody assigned. The four unjudged postings
    # are not zeros, and the distribution does not pretend otherwise.
    assert report.grade_distribution["OUT_OF_TARGET"] == 1
    assert report.grade_distribution["VERY_RELEVANT"] == 1
    assert sum(report.grade_distribution.values()) == 2


def test_the_labelset_digest_ignores_when_the_judging_happened(
    frozen: Path, tmp_path: Path
):
    """The property the protocol is required to have, checked through the store."""
    dataset = read_frozen_dataset(frozen)
    selection = select_calibration_sample(dataset, 4)
    fingerprints = []
    for index, moment in enumerate(
        ("2026-03-01T09:00:00+00:00", "2026-09-09T23:11:00+00:00")
    ):
        root = tmp_path / f"labels-{index}"
        for offset, opportunity_id in enumerate(selection.opportunity_ids[:2]):
            append_human_label(
                dataset,
                opportunity_id=opportunity_id,
                relevance_grade=3 - offset,
                labeled_at=moment,
                root=root,
            )
        fingerprints.append(
            build_labelset_report(dataset, selection, root).labelset_fingerprint
        )
    assert fingerprints[0] == fingerprints[1]


def test_a_corrected_judgement_digests_like_a_direct_one(
    frozen: Path, tmp_path: Path
):
    dataset = read_frozen_dataset(frozen)
    selection = select_calibration_sample(dataset, 4)
    opportunity_id = selection.opportunity_ids[0]

    direct_root = tmp_path / "direct"
    append_human_label(
        dataset, opportunity_id=opportunity_id, relevance_grade=2, root=direct_root
    )
    direct = build_labelset_report(dataset, selection, direct_root)

    corrected_root = tmp_path / "corrected"
    append_human_label(
        dataset,
        opportunity_id=opportunity_id,
        relevance_grade=0,
        root=corrected_root,
    )
    append_human_label(
        dataset,
        opportunity_id=opportunity_id,
        relevance_grade=2,
        relabel=True,
        relabel_reason="read the description properly the second time",
        root=corrected_root,
    )
    corrected = build_labelset_report(dataset, selection, corrected_root)

    assert direct.labelset_fingerprint == corrected.labelset_fingerprint
    assert direct.revision_count == 1
    assert corrected.revision_count == 2


def test_a_judgement_outside_the_lot_is_reported_not_digested(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    selection = select_calibration_sample(dataset, 3)
    outside = next(
        opportunity_id
        for opportunity_id in dataset.opportunity_ids
        if opportunity_id not in selection.opportunity_ids
    )
    append_human_label(
        dataset, opportunity_id=selection.opportunity_ids[0], relevance_grade=2,
        root=labels_root,
    )
    before = build_labelset_report(dataset, selection, labels_root)
    append_human_label(
        dataset, opportunity_id=outside, relevance_grade=3, root=labels_root
    )
    after = build_labelset_report(dataset, selection, labels_root)
    assert after.judged_outside_selection == (outside,)
    assert after.labelset_fingerprint == before.labelset_fingerprint


def test_a_labelset_manifest_states_the_protocol_is_not_frozen(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    selection = select_calibration_sample(dataset, 3)
    report = build_labelset_report(dataset, selection, labels_root)
    result = write_labelset_manifest(report, labels_root)
    payload = json.loads(result.path.read_text(encoding="utf-8"))
    assert payload["protocol_version"] == "human-relevance-calibration-v0"
    # The manifest says which rubric produced these judgements and that the
    # frozen one did not: a calibration artefact must not read as a benchmark.
    assert "CALIBRATION" in payload["protocol_status"]
    assert "no label carries it" in payload["protocol_status"]
    assert payload["judged_count"] == 0
    assert payload["unjudged_count"] == 3
    assert write_labelset_manifest(report, labels_root).status == "UPDATED"


# --------------------------------------------------------------------------
# protocol and selector versions are never mixed (FIX 2)
# --------------------------------------------------------------------------


def test_a_label_from_another_protocol_is_refused_on_read(
    frozen: Path, labels_root: Path
):
    """Right shape, wrong question: the row parses and is still refused."""
    dataset = read_frozen_dataset(frozen)
    _write_foreign_label(
        dataset, labels_root, protocol_version="human-relevance-v1"
    )
    with pytest.raises(HumanLabelError, match="protocol version"):
        read_label_history(dataset.dataset_id, labels_root)


def test_a_label_from_another_protocol_never_reaches_a_digest(
    frozen: Path, labels_root: Path
):
    """The transition v0 -> v1 must not be able to pool the two silently."""
    dataset = read_frozen_dataset(frozen)
    selection = select_calibration_sample(dataset, 3)
    _write_foreign_label(
        dataset,
        labels_root,
        opportunity_id=selection.opportunity_ids[0],
        protocol_version="human-relevance-v1",
    )
    with pytest.raises(HumanLabelError, match="protocol version"):
        build_labelset_report(dataset, selection, labels_root)


def test_a_label_from_another_protocol_blocks_a_new_judgement(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    _write_foreign_label(
        dataset, labels_root, protocol_version="human-relevance-v1"
    )
    with pytest.raises(HumanLabelError, match="protocol version"):
        append_human_label(
            dataset, opportunity_id=2, relevance_grade=2, root=labels_root
        )


def test_a_selection_from_another_protocol_is_refused_on_read(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    result = write_calibration_selection(
        select_calibration_sample(dataset, 3), labels_root
    )
    payload = json.loads(result.path.read_text(encoding="utf-8"))
    payload["protocol_version"] = "human-relevance-v1"
    result.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(HumanLabelError, match="protocol version"):
        load_calibration_selection(result.path)


def test_a_selection_from_an_unsupported_selector_is_refused_on_read(
    frozen: Path, labels_root: Path
):
    """No compatibility policy in this slice, so no silent acceptance either."""
    dataset = read_frozen_dataset(frozen)
    result = write_calibration_selection(
        select_calibration_sample(dataset, 3), labels_root
    )
    payload = json.loads(result.path.read_text(encoding="utf-8"))
    payload["selector_version"] = "calibration-selector-v9"
    result.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(HumanLabelError, match="selector"):
        load_calibration_selection(result.path)


# --------------------------------------------------------------------------
# selection bindings are checked before anything is shown (FIX 3)
# --------------------------------------------------------------------------


def test_a_selection_pointing_at_another_dataset_is_refused_before_any_view(
    tmp_path: Path, frozen: Path, labels_root: Path, capsys
):
    """`--selection PATH` from another dataset stops at the argument.

    Not at the digest, three commands later: an annotator must never be shown a
    posting under a lot that was drawn against a different snapshot.
    """
    other_dataset = build_dataset(9)
    other = write_evaluation_dataset(other_dataset, tmp_path / "other-datasets")
    other_view = read_frozen_dataset(other.directory)
    foreign = write_calibration_selection(
        select_calibration_sample(other_view, 4), tmp_path / "other-labels"
    )

    common = ["--dataset-dir", str(frozen), "--labels-root", str(labels_root)]
    code, payload = run(
        capsys, "show", *common, "--selection", str(foreign.path), "--next"
    )
    assert code == 2
    assert "drawn from dataset" in payload["error"]
    assert "opportunity" not in payload

    for command in ("progress", "fingerprint"):
        code, payload = run(
            capsys, command, *common, "--selection", str(foreign.path)
        )
        assert code == 2
        assert "drawn from dataset" in payload["error"]


def test_a_selection_for_another_profile_is_refused_by_every_path(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    selection = select_calibration_sample(dataset, 4)
    foreign = replace(selection, profile_id=2)
    with pytest.raises(HumanLabelError, match="drawn for profile"):
        assert_selection_bindings(foreign, dataset)
    with pytest.raises(HumanLabelError, match="drawn for profile"):
        build_labelset_report(dataset, foreign, labels_root)


def test_the_labelset_report_checks_the_whole_binding_not_only_the_id(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    selection = select_calibration_sample(dataset, 4)
    with pytest.raises(HumanLabelError, match="positions"):
        build_labelset_report(
            dataset,
            replace(
                selection,
                items=tuple(
                    replace(item, position=item.position + 1)
                    for item in selection.items
                ),
            ),
            labels_root,
        )


# --------------------------------------------------------------------------
# the existing history is validated before anything is appended (FIX 4)
# --------------------------------------------------------------------------


def test_a_corrupt_history_stops_a_new_judgement_instead_of_gaining_one(
    frozen: Path, labels_root: Path
):
    """A valid row must never be appended behind an invalid one.

    That would make the corruption permanent and give it company: the file would
    then hold one judgement that belongs here and one that does not, and no
    later reader could tell which number came from which.
    """
    dataset = read_frozen_dataset(frozen)
    _write_foreign_label(dataset, labels_root, dataset_content_fingerprint="0" * 64)
    path = labels_root / dataset.dataset_id / "labels.jsonl"
    before = path.read_text(encoding="utf-8")
    with pytest.raises(HumanLabelError, match="content fingerprint"):
        append_human_label(
            dataset, opportunity_id=5, relevance_grade=3, root=labels_root
        )
    assert path.read_text(encoding="utf-8") == before


def test_a_history_judging_an_absent_opportunity_is_refused(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    _write_foreign_label(dataset, labels_root, opportunity_id=9999)
    with pytest.raises(HumanLabelError, match="not in dataset"):
        read_validated_label_history(dataset, labels_root)


def _write_rows(dataset, labels_root: Path, rows: list[dict]) -> Path:
    path = labels_root / dataset.dataset_id / "labels.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    return path


def _row(dataset, **overrides) -> dict:
    row = {
        "label_schema_version": HUMAN_LABEL_SCHEMA_VERSION,
        "protocol_version": HUMAN_LABEL_PROTOCOL_VERSION,
        "dataset_id": dataset.dataset_id,
        "dataset_content_fingerprint": dataset.content_fingerprint,
        "profile_id": dataset.profile_id,
        "profile_context_fingerprint": dataset.profile_context_fingerprint,
        "opportunity_id": 1,
        "relevance_grade": 2,
        "relevance_grade_name": "RELEVANT",
        "diagnostics": {
            "geo_judgment": None,
            "data_ai_judgment": None,
            "opportunity_type_judgment": None,
        },
        "reason_tags": [],
        "note": None,
        "labeled_at": "2026-03-01T10:00:00+00:00",
        "revision": 1,
        "relabel_reason": None,
    }
    row.update(overrides)
    # The name follows the grade unless a test deliberately contradicts it, so a
    # fixture exercising some other rule does not trip the grade-name check
    # first.
    if "relevance_grade_name" not in overrides:
        row["relevance_grade_name"] = RELEVANCE_GRADE_NAMES[row["relevance_grade"]]
    return row


def test_a_first_judgement_claiming_a_later_revision_is_refused(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    _write_rows(dataset, labels_root, [_row(dataset, revision=2, relabel_reason="x")])
    with pytest.raises(HumanLabelError, match="revision 1 was due"):
        read_validated_label_history(dataset, labels_root)


def test_a_gap_in_the_revision_chain_is_refused(frozen: Path, labels_root: Path):
    dataset = read_frozen_dataset(frozen)
    _write_rows(
        dataset,
        labels_root,
        [
            _row(dataset),
            _row(dataset, revision=3, relabel_reason="skipped a step"),
        ],
    )
    with pytest.raises(HumanLabelError, match="revision 2 was due"):
        read_validated_label_history(dataset, labels_root)


def test_a_correction_that_states_no_reason_is_refused(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    _write_rows(
        dataset,
        labels_root,
        [_row(dataset), _row(dataset, revision=2, relevance_grade=0)],
    )
    with pytest.raises(HumanLabelError, match="without stating why"):
        read_validated_label_history(dataset, labels_root)


def test_a_first_judgement_claiming_to_be_a_correction_is_refused(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    _write_rows(
        dataset, labels_root, [_row(dataset, relabel_reason="nothing to correct")]
    )
    with pytest.raises(HumanLabelError, match="states a relabel reason"):
        read_validated_label_history(dataset, labels_root)


def test_a_history_written_by_the_normal_path_always_validates(
    frozen: Path, labels_root: Path
):
    """The invariants hold for every file this package itself produces."""
    dataset = read_frozen_dataset(frozen)
    append_human_label(
        dataset, opportunity_id=1, relevance_grade=3, root=labels_root
    )
    append_human_label(
        dataset, opportunity_id=2, relevance_grade=0, root=labels_root
    )
    append_human_label(
        dataset,
        opportunity_id=1,
        relevance_grade=1,
        relabel=True,
        relabel_reason="reread the description",
        root=labels_root,
    )
    history = read_validated_label_history(dataset, labels_root)
    assert [(item.opportunity_id, item.revision) for item in history] == [
        (1, 1),
        (2, 1),
        (1, 2),
    ]


# --------------------------------------------------------------------------
# a stored row is read strictly and never repaired (FIX A)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "pattern"),
    [
        ("revision", True, "revision"),
        ("revision", "1", "revision"),
        ("revision", 1.0, "revision"),
        ("revision", 0, "not positive"),
        ("profile_id", "1", "profile_id"),
        ("profile_id", True, "profile_id"),
        ("opportunity_id", "1", "opportunity_id"),
        ("opportunity_id", 1.0, "opportunity_id"),
        ("dataset_id", 7, "dataset_id"),
        ("labeled_at", 20260301, "labeled_at"),
        ("labeled_at", "yesterday", "ISO-8601"),
        ("diagnostics", None, "diagnostics"),
        ("diagnostics", [], "diagnostics"),
        ("diagnostics", {"geo_judgment": None}, "diagnostics block"),
        ("diagnostics", {"geo_judgment": 1, "data_ai_judgment": None,
                         "opportunity_type_judgment": None}, "geo_judgment"),
        ("reason_tags", "remote", "reason_tags"),
        ("reason_tags", [3], "reason tag"),
        ("reason_tags", None, "reason_tags"),
        ("note", 7, "note"),
        ("relabel_reason", 7, "relabel_reason"),
    ],
)
def test_a_non_contractual_value_is_refused_rather_than_coerced(
    frozen: Path, labels_root: Path, field, value, pattern
):
    """No `int(...)`, no `str(...)`, no normalization on the way in.

    Each of these used to be quietly converted into something valid — `true`
    into revision 1, `"1"` into 1 — and the converted value would then have been
    written back over the stored row by the next unrelated append.
    """
    dataset = read_frozen_dataset(frozen)
    _write_rows(dataset, labels_root, [_row(dataset, **{field: value})])
    with pytest.raises(HumanLabelError, match=pattern):
        read_label_history(dataset.dataset_id, labels_root)


@pytest.mark.parametrize(
    ("tags", "pattern"),
    [
        (["Remote"], "canonical form"),
        (["remote", "remote"], "canonical form"),
        (["remote", "geo-mismatch"], "canonical form"),
        ([" remote "], "canonical form"),
    ],
)
def test_non_canonical_reason_tags_are_refused_not_normalized(
    frozen: Path, labels_root: Path, tags, pattern
):
    """Uppercase, duplicated, unsorted or padded: all rows this writer never wrote."""
    dataset = read_frozen_dataset(frozen)
    _write_rows(dataset, labels_root, [_row(dataset, reason_tags=tags)])
    with pytest.raises(HumanLabelError, match=pattern):
        read_label_history(dataset.dataset_id, labels_root)


@pytest.mark.parametrize("note", ["  padded  ", "", "   "])
def test_a_non_canonical_note_is_refused_not_trimmed(
    frozen: Path, labels_root: Path, note
):
    dataset = read_frozen_dataset(frozen)
    _write_rows(dataset, labels_root, [_row(dataset, note=note)])
    with pytest.raises(HumanLabelError, match="canonical form"):
        read_label_history(dataset.dataset_id, labels_root)


def test_a_grade_name_contradicting_its_grade_is_refused_not_reconciled(
    frozen: Path, labels_root: Path
):
    """The row says two things about one judgement and they disagree.

    Rewriting the name to match the number would be this code deciding what
    somebody meant, on a record whose entire purpose is to say what they meant.
    """
    dataset = read_frozen_dataset(frozen)
    _write_rows(
        dataset,
        labels_root,
        [_row(dataset, relevance_grade=2, relevance_grade_name="VERY_RELEVANT")],
    )
    with pytest.raises(HumanLabelError, match="contradict each other"):
        read_label_history(dataset.dataset_id, labels_root)
    # And the file is left exactly as it was, name included.
    path = labels_root / dataset.dataset_id / "labels.jsonl"
    assert '"relevance_grade_name": "VERY_RELEVANT"' in path.read_text(
        encoding="utf-8"
    )


def test_a_row_with_an_extra_or_missing_key_is_refused(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    _write_rows(dataset, labels_root, [_row(dataset, labeler="someone")])
    with pytest.raises(HumanLabelError, match="unexpected: labeler"):
        read_label_history(dataset.dataset_id, labels_root)

    row = _row(dataset)
    del row["note"]
    _write_rows(dataset, labels_root, [row])
    with pytest.raises(HumanLabelError, match="missing: note"):
        read_label_history(dataset.dataset_id, labels_root)


def test_the_round_trip_check_compares_structure_and_not_bytes(
    frozen: Path, labels_root: Path
):
    """The second layer: re-serialize what was parsed, compare canonical JSON.

    It does not need to know what a future field is in order to refuse a row
    that would come out different — and it must not mistake formatting for
    meaning, so indentation and key order in the file are irrelevant.
    """
    dataset = read_frozen_dataset(frozen)
    row = _row(dataset)
    row["relevance_grade_name"] = "RELEVANT"
    row["relevance_grade"] = 2
    # A row that does round trip is accepted...
    _write_rows(dataset, labels_root, [row])
    assert len(read_label_history(dataset.dataset_id, labels_root)) == 1
    # ...and the comparison is over structure, not bytes: the same row written
    # with different spacing and key order is the same row.
    path = labels_root / dataset.dataset_id / "labels.jsonl"
    path.write_text(
        json.dumps(dict(reversed(list(row.items()))), indent=None) + "\n",
        encoding="utf-8",
    )
    assert len(read_label_history(dataset.dataset_id, labels_root)) == 1


def test_an_append_leaves_every_earlier_row_byte_identical(
    frozen: Path, labels_root: Path
):
    """Append-only as a property of the bytes, not of a proof about them."""
    dataset = read_frozen_dataset(frozen)
    path = labels_root / dataset.dataset_id / "labels.jsonl"

    append_human_label(
        dataset,
        opportunity_id=1,
        relevance_grade=3,
        reason_tags=["strong-fit"],
        note="clear fit",
        root=labels_root,
    )
    first = path.read_text(encoding="utf-8").splitlines()

    append_human_label(
        dataset, opportunity_id=2, relevance_grade=0, root=labels_root
    )
    append_human_label(
        dataset,
        opportunity_id=1,
        relevance_grade=1,
        relabel=True,
        relabel_reason="reread the description",
        root=labels_root,
    )
    after = path.read_text(encoding="utf-8").splitlines()

    assert after[: len(first)] == first
    assert len(after) == 3


def test_a_refused_append_leaves_the_file_byte_identical(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    path = labels_root / dataset.dataset_id / "labels.jsonl"
    _write_rows(dataset, labels_root, [_row(dataset, reason_tags=["Remote"])])
    before = path.read_bytes()
    with pytest.raises(HumanLabelError, match="canonical form"):
        append_human_label(
            dataset, opportunity_id=2, relevance_grade=2, root=labels_root
        )
    assert path.read_bytes() == before


# --------------------------------------------------------------------------
# a stored lot must be what its selector actually draws (FIX B)
# --------------------------------------------------------------------------


def _rewrite_selection(path: Path, mutate) -> None:
    """Edit a stored selection and recompute its self-declared fingerprint.

    The shape of a hand-assembled file: internally consistent, digest and all.
    Only redrawing the lot can tell that the algorithm it names did not produce
    it.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    payload["selection_fingerprint"] = calibration_selection_fingerprint(
        selector_version=payload["selector_version"],
        dataset_id=payload["dataset_id"],
        dataset_content_fingerprint=payload["dataset_content_fingerprint"],
        sample_size=payload["effective_sample_size"],
        selected_opportunity_ids=[
            item["opportunity_id"] for item in payload["items"]
        ],
    )
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_a_selection_the_selector_would_not_have_drawn_is_refused(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    selection = select_calibration_sample(dataset, 4)
    result = write_calibration_selection(selection, labels_root)
    drawn = set(selection.opportunity_ids)
    stranger = next(
        value for value in dataset.opportunity_ids if value not in drawn
    )

    def swap(payload):
        payload["items"][1]["opportunity_id"] = stranger

    _rewrite_selection(result.path, swap)
    # The file is internally consistent: its own digest matches its contents.
    stored = load_calibration_selection(result.path)
    assert stored.selection_fingerprint
    with pytest.raises(HumanLabelError, match="is not what calibration-selector-v0"):
        assert_selection_bindings(stored, dataset)


def test_a_reordered_selection_is_refused(frozen: Path, labels_root: Path):
    """The draw order is part of the lot; a resorted file is a different lot."""
    dataset = read_frozen_dataset(frozen)
    result = write_calibration_selection(
        select_calibration_sample(dataset, 4), labels_root
    )

    def reverse(payload):
        items = list(reversed(payload["items"]))
        for position, item in enumerate(items, start=1):
            item["position"] = position
        payload["items"] = items

    _rewrite_selection(result.path, reverse)
    with pytest.raises(HumanLabelError, match="is not what calibration-selector-v0"):
        assert_selection_bindings(load_calibration_selection(result.path), dataset)


def test_a_selection_with_an_invented_stratum_is_refused(
    frozen: Path, labels_root: Path
):
    """The stratum claims to explain the draw; an unchecked explanation is decoration."""
    dataset = read_frozen_dataset(frozen)
    result = write_calibration_selection(
        select_calibration_sample(dataset, 4), labels_root
    )

    def relabel_stratum(payload):
        payload["items"][0]["stratum"]["recommendation_band"] = "RECOMMENDED_HIGH"
        payload["items"][0]["stratum"]["qualification"] = "CORE_TARGET"

    _rewrite_selection(result.path, relabel_stratum)
    with pytest.raises(HumanLabelError, match="states stratum"):
        assert_selection_bindings(load_calibration_selection(result.path), dataset)


def test_a_selection_with_an_invented_diversity_key_is_refused(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    result = write_calibration_selection(
        select_calibration_sample(dataset, 4), labels_root
    )

    def relabel_diversity(payload):
        payload["items"][0]["diversity_key"]["source_type"] = "invented"

    _rewrite_selection(result.path, relabel_diversity)
    with pytest.raises(HumanLabelError, match="diversity key"):
        assert_selection_bindings(load_calibration_selection(result.path), dataset)


def test_a_selection_claiming_a_different_size_than_the_selector_draws_is_refused(
    frozen: Path, labels_root: Path
):
    dataset = read_frozen_dataset(frozen)
    result = write_calibration_selection(
        select_calibration_sample(dataset, 4), labels_root
    )

    def drop_one(payload):
        payload["items"] = payload["items"][:3]
        payload["effective_sample_size"] = 3

    _rewrite_selection(result.path, drop_one)
    with pytest.raises(HumanLabelError, match="draws 4 postings"):
        assert_selection_bindings(load_calibration_selection(result.path), dataset)


def test_a_genuinely_drawn_selection_round_trips_and_is_accepted(
    frozen: Path, labels_root: Path
):
    """The positive case: a real lot, stored and reloaded, still verifies."""
    dataset = read_frozen_dataset(frozen)
    selection = select_calibration_sample(dataset, 5)
    result = write_calibration_selection(selection, labels_root)
    stored = load_calibration_selection(result.path)
    assert_selection_bindings(stored, dataset)
    assert stored.opportunity_ids == selection.opportunity_ids
    assert stored.selector_version == CALIBRATION_SELECTOR_VERSION


def test_the_same_dataset_and_size_redraw_identically(frozen: Path):
    """The determinism the re-derivation check depends on, stated separately."""
    dataset = read_frozen_dataset(frozen)
    again = read_frozen_dataset(frozen)
    first = select_calibration_sample(dataset, 7)
    second = select_calibration_sample(again, 7)
    assert first.opportunity_ids == second.opportunity_ids
    assert first.selection_fingerprint == second.selection_fingerprint
    assert [dict(item.stratum) for item in first.items] == [
        dict(item.stratum) for item in second.items
    ]


def test_the_cli_refuses_a_selection_the_selector_would_not_have_drawn(
    frozen: Path, labels_root: Path, capsys
):
    """And it refuses before showing anything, as with every other binding."""
    dataset = read_frozen_dataset(frozen)
    selection = select_calibration_sample(dataset, 4)
    result = write_calibration_selection(selection, labels_root)
    drawn = set(selection.opportunity_ids)
    stranger = next(
        value for value in dataset.opportunity_ids if value not in drawn
    )
    _rewrite_selection(
        result.path,
        lambda payload: payload["items"][2].__setitem__(
            "opportunity_id", stranger
        ),
    )
    code, payload = run(
        capsys,
        "show",
        "--dataset-dir",
        str(frozen),
        "--labels-root",
        str(labels_root),
        "--next",
    )
    assert code == 2
    assert "is not what calibration-selector-v0" in payload["error"]
    assert "opportunity" not in payload


# --------------------------------------------------------------------------
# the frozen v1 contract does not disturb the calibration audit trail
# --------------------------------------------------------------------------


def test_calibration_v0_artefacts_stay_readable_and_auditable(
    frozen: Path, labels_root: Path
):
    """Freezing v1 changed no stored row and no stored reading of one.

    The shape mirrors the real calibration round — a relabel at revision 2
    leaving two rows for one judgement — with invented opportunities. What is
    asserted is that such a history still parses, still validates, still reports
    and still says `calibration-v0` afterwards.
    """
    dataset = read_frozen_dataset(frozen)
    selection = select_calibration_sample(dataset, 3)
    first, second, third = selection.opportunity_ids

    append_human_label(
        dataset, opportunity_id=first, relevance_grade=0, root=labels_root
    )
    append_human_label(
        dataset, opportunity_id=second, relevance_grade=2, root=labels_root
    )
    append_human_label(
        dataset,
        opportunity_id=first,
        relevance_grade=1,
        relabel=True,
        relabel_reason="the rubric was clarified after this judgement",
        root=labels_root,
    )
    append_human_label(
        dataset, opportunity_id=third, relevance_grade=0, root=labels_root
    )

    history = read_validated_label_history(dataset, labels_root)
    assert len(history) == 4
    assert {item.protocol_version for item in history} == {
        HUMAN_LABEL_PROTOCOL_VERSION
    }
    effective = resolve_effective_labels(history)
    assert len(effective) == 3
    assert effective[first].revision == 2
    assert effective[first].relevance_grade == 1

    report = build_labelset_report(dataset, selection, labels_root)
    assert report.protocol_version == HUMAN_LABEL_PROTOCOL_VERSION
    assert report.judged_count == 3
    assert report.unjudged_count == 0
    # One relabel: more audit rows than judgements, exactly as the real round.
    assert report.revision_count == 4


def test_recording_a_new_judgement_never_migrates_an_existing_row(
    frozen: Path, labels_root: Path
):
    """No silent v0 -> v1 rewrite, checked on the bytes.

    The frozen contract is a definition sitting in the source tree. It must not
    reach a stored row by any path, least of all as a side effect of recording
    an unrelated judgement.
    """
    dataset = read_frozen_dataset(frozen)
    path = labels_root / dataset.dataset_id / "labels.jsonl"
    append_human_label(
        dataset, opportunity_id=1, relevance_grade=2, root=labels_root
    )
    before = path.read_text(encoding="utf-8").splitlines()

    append_human_label(
        dataset, opportunity_id=2, relevance_grade=0, root=labels_root
    )
    after = path.read_text(encoding="utf-8").splitlines()

    assert after[: len(before)] == before
    text = path.read_text(encoding="utf-8")
    assert FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION not in text
    assert text.count(HUMAN_LABEL_PROTOCOL_VERSION) == 2


def test_a_v1_row_is_refused_rather_than_accepted_into_a_calibration_history(
    frozen: Path, labels_root: Path
):
    """A row claiming the frozen protocol does not belong in this file."""
    dataset = read_frozen_dataset(frozen)
    _write_rows(
        dataset,
        labels_root,
        [_row(dataset, protocol_version=FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION)],
    )
    with pytest.raises(HumanLabelError, match="another protocol"):
        read_label_history(dataset.dataset_id, labels_root)


def test_a_history_mixing_calibration_and_frozen_rows_is_refused_whole(
    frozen: Path, labels_root: Path
):
    """Two rubrics in one file is not a history, and half of one is not either.

    The v0 row on its own is perfectly good. It is still refused here, because
    what is being read is the file, and this file holds judgements of two
    different questions with nothing to tell them apart.
    """
    dataset = read_frozen_dataset(frozen)
    path = _write_rows(
        dataset,
        labels_root,
        [
            _row(dataset, opportunity_id=1),
            _row(
                dataset,
                opportunity_id=2,
                protocol_version=FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION,
            ),
        ],
    )
    before = path.read_bytes()
    with pytest.raises(HumanLabelError, match="another protocol"):
        read_label_history(dataset.dataset_id, labels_root)
    with pytest.raises(HumanLabelError, match="another protocol"):
        append_human_label(
            dataset, opportunity_id=3, relevance_grade=2, root=labels_root
        )
    # Refused, not repaired: neither row was rewritten, dropped or converted.
    assert path.read_bytes() == before


def test_a_v1_selection_artefact_is_refused_too(frozen: Path, labels_root: Path):
    """The separation is not only about labels: a lot names its protocol too."""
    dataset = read_frozen_dataset(frozen)
    result = write_calibration_selection(
        select_calibration_sample(dataset, 3), labels_root
    )
    payload = json.loads(result.path.read_text(encoding="utf-8"))
    payload["protocol_version"] = FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION
    result.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(HumanLabelError, match="another protocol"):
        load_calibration_selection(result.path)


# --------------------------------------------------------------------------
# the CLI
# --------------------------------------------------------------------------


def run(capsys, *argv) -> tuple[int, dict]:
    code = main(list(argv))
    captured = capsys.readouterr().out
    return code, json.loads(captured)


def test_the_cli_verifies_selects_shows_labels_and_reports(
    frozen: Path, labels_root: Path, capsys
):
    common = ["--dataset-dir", str(frozen), "--labels-root", str(labels_root)]

    code, verified = run(capsys, "verify", *common)
    assert code == 0
    assert verified["integrity"] == "VERIFIED"
    assert verified["protocol_version"] == "human-relevance-calibration-v0"
    assert "CALIBRATION" in verified["protocol_status"]
    assert "no label carries it" in verified["protocol_status"]

    code, selected = run(capsys, "select", *common, "--sample-size", "4")
    assert code == 0
    assert selected["write_status"] == "CREATED"
    assert len(selected["selected_opportunity_ids"]) == 4
    # Aggregate variety, but no posting-to-stratum mapping.
    assert "items" not in selected
    assert selected["stratum_counts"]

    code, shown = run(capsys, "show", *common, "--next")
    assert code == 0
    first = selected["selected_opportunity_ids"][0]
    assert shown["opportunity"]["opportunity_id"] == first
    for field in WITHHELD_RECORD_FIELDS:
        assert field not in shown["opportunity"]
    assert [entry["grade"] for entry in shown["rubric"]] == [3, 2, 1, 0]
    assert "never a 0" in shown["unjudged_is_not_zero"]

    code, recorded = run(
        capsys,
        "label",
        *common,
        "--opportunity-id",
        str(first),
        "--grade",
        "2",
        "--geo-judgment",
        "UNKNOWN_GEOGRAPHY",
        "--reason-tag",
        "borderline",
        "--note",
        "fits, but the location text is ambiguous",
    )
    assert code == 0
    assert recorded["relevance_grade"] == 2
    assert recorded["revision"] == 1

    code, progress = run(capsys, "progress", *common)
    assert code == 0
    assert progress["judged_count"] == 1
    assert progress["unjudged_count"] == 3
    assert progress["grade_distribution"]["OUT_OF_TARGET"] == 0
    assert len(progress["unjudged_opportunity_ids"]) == 3

    code, digest = run(capsys, "fingerprint", *common, "--write-manifest")
    assert code == 0
    assert len(digest["labelset_fingerprint"]) == 64
    assert digest["write_status"] == "CREATED"
    assert Path(digest["path"]).is_file()

    # The second `show --next` moves on rather than repeating a judged posting.
    code, second = run(capsys, "show", *common, "--next")
    assert second["opportunity"]["opportunity_id"] != first


def test_the_cli_never_prints_a_system_verdict(
    frozen: Path, labels_root: Path, capsys
):
    """Not even after the posting has been judged: one command, one behaviour.

    Two separate checks. No *value* the pipeline produced appears anywhere in
    the output, and no verdict *field* appears in the evidence block. The
    payload does name the withheld blocks — under `withheld_until_judged`, so
    the annotator knows what is being kept from them — and naming a block is
    not showing its contents.
    """
    common = ["--dataset-dir", str(frozen), "--labels-root", str(labels_root)]
    run(capsys, "select", *common, "--sample-size", "3")

    def shown(opportunity_id: str) -> tuple[str, dict]:
        main([*["show"], *common, "--opportunity-id", opportunity_id])
        printed = capsys.readouterr().out
        return printed, json.loads(printed)

    printed, payload = shown("1")
    for value in (
        "CORE_TARGET",
        "ADJACENT_TARGET",
        "OUT_OF_SCOPE",
        "RECOMMENDED",
        "PRIMARY",
        "rank_position",
        "recommendation_score",
        "match_quality",
        "assessment_fingerprint",
    ):
        assert value not in printed, value
    evidence = json.dumps(payload["opportunity"])
    for field in WITHHELD_RECORD_FIELDS:
        assert field not in evidence, field
    assert set(payload["withheld_until_judged"]) == set(WITHHELD_RECORD_FIELDS)

    main([*["label"], *common, "--opportunity-id", "1", "--grade", "3"])
    capsys.readouterr()
    after, payload = shown("1")
    for value in ("CORE_TARGET", "rank_position", "recommendation_score"):
        assert value not in after, value
    assert json.dumps(payload["opportunity"]).count("qualification") == 0


def test_the_cli_refuses_a_duplicate_label(frozen: Path, labels_root: Path, capsys):
    common = ["--dataset-dir", str(frozen), "--labels-root", str(labels_root)]
    run(capsys, "label", *common, "--opportunity-id", "2", "--grade", "1")
    code, payload = run(
        capsys, "label", *common, "--opportunity-id", "2", "--grade", "3"
    )
    assert code == 2
    assert "already labelled" in payload["error"]

    code, payload = run(
        capsys,
        "label",
        *common,
        "--opportunity-id",
        "2",
        "--grade",
        "3",
        "--relabel",
        "--relabel-reason",
        "reread the description",
    )
    assert code == 0
    assert payload["revision"] == 2


def test_the_cli_refuses_a_grade_outside_the_rubric(
    frozen: Path, labels_root: Path
):
    common = ["--dataset-dir", str(frozen), "--labels-root", str(labels_root)]
    for grade in ("4", "-1", "true"):
        with pytest.raises(SystemExit):
            main([*["label"], *common, "--opportunity-id", "2", "--grade", grade])


def test_the_cli_refuses_a_corrupt_dataset(frozen: Path, labels_root: Path, capsys):
    (frozen / "manifest.json").write_text("{", encoding="utf-8")
    code, payload = run(
        capsys,
        "verify",
        "--dataset-dir",
        str(frozen),
        "--labels-root",
        str(labels_root),
    )
    assert code == 2
    assert "cannot be read" in payload["error"]


def test_the_cli_dry_run_writes_nothing(frozen: Path, labels_root: Path, capsys):
    code, payload = run(
        capsys,
        "select",
        "--dataset-dir",
        str(frozen),
        "--labels-root",
        str(labels_root),
        "--sample-size",
        "3",
        "--dry-run",
    )
    assert code == 0
    assert payload["written"] is False
    assert not labels_root.exists()


def test_the_cli_will_not_guess_between_two_lots(
    frozen: Path, labels_root: Path, capsys
):
    common = ["--dataset-dir", str(frozen), "--labels-root", str(labels_root)]
    run(capsys, "select", *common, "--sample-size", "3")
    run(capsys, "select", *common, "--sample-size", "5")
    code, payload = run(capsys, "progress", *common)
    assert code == 2
    assert "several calibration selections" in payload["error"]


def test_the_cli_reveals_strata_only_when_asked(
    frozen: Path, labels_root: Path, capsys
):
    common = ["--dataset-dir", str(frozen), "--labels-root", str(labels_root)]
    code, payload = run(
        capsys, "select", *common, "--sample-size", "3", "--reveal-strata"
    )
    assert code == 0
    assert [item["opportunity_id"] for item in payload["items"]] == payload[
        "selected_opportunity_ids"
    ]
    assert all("stratum" in item for item in payload["items"])
