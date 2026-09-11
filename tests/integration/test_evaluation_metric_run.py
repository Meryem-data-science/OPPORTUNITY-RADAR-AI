"""Phase 10.3a against real files: a frozen dataset, real labels, real gates.

Every dataset here is written by Phase 10.1's own `write_evaluation_dataset`
and every judgement by Phase 10.2's own `append_human_label`, into a
`tmp_path`. That matters: the availability gates are exercised against genuine
Phase 10 artefacts rather than against a hand-rolled approximation of them, so
a disagreement between the writers and this reader shows up here instead of on
the operator's machine.

Nothing in this module opens the operational database — one test proves it by
making every `sqlite3.connect` in the process raise — and no real opportunity
and no real judgement appears anywhere in it.
"""

import sqlite3
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
    EvaluationOpportunityRecord,
    EvaluationProfileContext,
    EvaluationRecommendationRecord,
    EvaluationSourceIdentity,
    EvaluationSourceRecord,
    EvaluationDatasetError,
    EvaluationUpstreamProvenance,
    evaluation_content_fingerprint,
    write_evaluation_dataset,
)
from evaluation.labeling import (
    CALIBRATION_V0_PROVENANCE,
    HUMAN_LABEL_PROTOCOL_VERSION,
    FrozenEvaluationDataset,
    HumanLabelError,
    append_human_label,
    build_labelset_report,
    read_frozen_dataset,
    read_validated_label_history,
    select_calibration_sample,
)
from evaluation.metrics import (
    EvaluationBindingError,
    EvaluationRunProvenance,
    EvidenceClass,
    EvidenceClassError,
    MetricStatus,
    MetricUnavailableReason,
    UnjudgedOpportunityError,
    build_evaluation_run,
    build_evaluation_universe,
    build_label_coverage,
    ndcg_at_k_availability,
    precision_at_k_availability,
    ranking_from_frozen_dataset,
    recall_at_k_availability,
    universe_judged_coverage,
    verify_evaluation_run,
)

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

#: Sixteen invented postings, twelve of them ranked by the invented
#: Recommendation run. The four unranked ones are the point of the cohort: they
#: are in the universe and they are not in the ranking.
COHORT_SIZE = 16
RANKED_COUNT = 12


def opportunity(opportunity_id: int) -> EvaluationOpportunityRecord:
    recommendation = None
    if opportunity_id <= RANKED_COUNT:
        recommendation = EvaluationRecommendationRecord(
            rank_position=opportunity_id,
            disposition="RECOMMENDED",
            recommendation_score=0.9 - opportunity_id / 100,
            evidence_coverage=0.8,
            assessment_fingerprint="f" * 64,
        )
    return EvaluationOpportunityRecord(
        opportunity_id=opportunity_id,
        canonical_title=f"Invented posting {opportunity_id}",
        organization=f"Invented Org {opportunity_id}",
        opportunity_type="PFE",
        employment_type=None,
        location="Casablanca, Maroc",
        country="MA",
        remote_type="ONSITE",
        description=f"Invented description {opportunity_id}.",
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
                source_id="greenhouse",
                source_type="greenhouse",
                source_url=f"https://example.invalid/offers/{opportunity_id}",
                application_url=None,
                canonical_url=f"https://example.invalid/offers/{opportunity_id}",
                discovered_at="2026-01-01T00:00:00+00:00",
            ),
        ),
        qualification=None,
        geography_segments=(),
        eligibility=None,
        matching=None,
        recommendation=recommendation,
    )


def build_dataset(count: int = COHORT_SIZE) -> EvaluationDataset:
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
def frozen(tmp_path: Path) -> FrozenEvaluationDataset:
    """A real frozen dataset, written by Phase 10.1 and verified by Phase 10.2."""
    paths = write_evaluation_dataset(build_dataset(), tmp_path / "datasets")
    return read_frozen_dataset(paths.directory)


@pytest.fixture
def labels_root(tmp_path: Path) -> Path:
    return tmp_path / "labels"


def record_labels(
    dataset: FrozenEvaluationDataset,
    root: Path,
    grades: dict[int, int],
) -> None:
    """Real judgements, written through Phase 10.2's append-only writer."""
    for opportunity_id, grade in sorted(grades.items()):
        append_human_label(
            dataset,
            opportunity_id=opportunity_id,
            relevance_grade=grade,
            root=root,
        )


def coverage_from_disk(
    dataset: FrozenEvaluationDataset, root: Path, labelset_fingerprint: str
):
    return build_label_coverage(
        dataset,
        read_validated_label_history(dataset, root),
        labelset_fingerprint=labelset_fingerprint,
    )


def labelset_fingerprint_of(
    dataset: FrozenEvaluationDataset, root: Path, sample_size: int
) -> str:
    """A real labelset digest, from Phase 10.2's own report over a real lot."""
    selection = select_calibration_sample(dataset, sample_size=sample_size)
    return build_labelset_report(dataset, selection, root).labelset_fingerprint


def run_for(
    dataset: FrozenEvaluationDataset,
    labelset_fingerprint: str,
    *,
    universe_ids=None,
    **kwargs,
):
    return build_evaluation_run(
        dataset=dataset,
        universe=build_evaluation_universe(dataset, universe_ids),
        ranking=ranking_from_frozen_dataset(dataset),
        evidence_class=EvidenceClass.DIAGNOSTIC_CALIBRATION,
        label_protocol_version=HUMAN_LABEL_PROTOCOL_VERSION,
        labelset_fingerprint=labelset_fingerprint,
        **kwargs,
    )


# --------------------------------------------------------------------------
# the run, assembled from real artefacts
# --------------------------------------------------------------------------


def test_a_run_binds_a_real_dataset_a_real_ranking_and_a_real_labelset(
    frozen, labels_root
):
    record_labels(frozen, labels_root, {1: 3, 2: 2, 3: 0})
    fingerprint = labelset_fingerprint_of(frozen, labels_root, 3)
    run = run_for(frozen, fingerprint)

    assert run.dataset_id == frozen.dataset_id
    assert run.dataset_content_fingerprint == frozen.content_fingerprint
    assert run.profile_context_fingerprint == frozen.profile_context_fingerprint
    assert run.universe.size == COHORT_SIZE
    assert run.ranking.length == RANKED_COUNT
    assert run.ranking.opportunity_ids == tuple(range(1, RANKED_COUNT + 1))
    assert run.labelset_fingerprint == fingerprint
    assert verify_evaluation_run(run) == run.run_fingerprint


def test_the_universe_is_the_cohort_and_not_the_ranked_or_labelled_subset(
    frozen, labels_root
):
    """Three different sets, and confusing any two of them breaks a metric."""
    record_labels(frozen, labels_root, {1: 3, 2: 2})
    fingerprint = labelset_fingerprint_of(frozen, labels_root, 2)
    run = run_for(frozen, fingerprint)
    coverage = coverage_from_disk(frozen, labels_root, fingerprint)

    assert run.universe.size == COHORT_SIZE
    assert run.ranking.length == RANKED_COUNT
    assert len(coverage.grades) == 2
    judged = universe_judged_coverage(run.universe, coverage)
    assert (judged.judged_count, judged.total_count) == (2, COHORT_SIZE)


def test_a_rerun_over_unchanged_artefacts_produces_the_same_run_fingerprint(
    frozen, labels_root, tmp_path
):
    record_labels(frozen, labels_root, {1: 3})
    fingerprint = labelset_fingerprint_of(frozen, labels_root, 1)
    first = run_for(
        frozen,
        fingerprint,
        provenance=EvaluationRunProvenance(
            generated_at="2026-09-11T06:00:00+00:00",
            dataset_directory=str(frozen.directory),
        ),
    )
    second = run_for(
        frozen,
        fingerprint,
        provenance=EvaluationRunProvenance(
            generated_at="2026-09-12T21:30:00+00:00",
            dataset_directory=str(tmp_path / "a-second-checkout"),
        ),
    )
    assert first.run_fingerprint == second.run_fingerprint


def test_a_run_over_a_relabelled_opportunity_reads_the_latest_revision(
    frozen, labels_root
):
    """Phase 10.2's append-only correction, read through Phase 10.2's own resolver."""
    record_labels(frozen, labels_root, {5: 0})
    append_human_label(
        frozen,
        opportunity_id=5,
        relevance_grade=3,
        root=labels_root,
        relabel=True,
        relabel_reason="the rubric was clarified",
    )
    fingerprint = labelset_fingerprint_of(frozen, labels_root, 1)
    coverage = coverage_from_disk(frozen, labels_root, fingerprint)
    assert coverage.grade(5) == 3
    # Both rows are still in the audit trail; only the effective one is read.
    assert len(read_validated_label_history(frozen, labels_root)) == 2


# --------------------------------------------------------------------------
# the gates, over real judgements
# --------------------------------------------------------------------------


def test_a_judged_top_ten_opens_precision_only(frozen, labels_root):
    """Regression 3, end to end: precision is local, recall and NDCG are not."""
    record_labels(frozen, labels_root, dict.fromkeys(range(1, 11), 2))
    fingerprint = labelset_fingerprint_of(frozen, labels_root, 10)
    run = run_for(frozen, fingerprint)
    coverage = coverage_from_disk(frozen, labels_root, fingerprint)

    precision = precision_at_k_availability(run, coverage, 10)
    assert precision.available
    assert precision.support.judged_in_top_k == 10

    recall = recall_at_k_availability(run, coverage, 10)
    assert recall.reason is MetricUnavailableReason.RECALL_DENOMINATOR_UNKNOWN

    ndcg = ndcg_at_k_availability(run, coverage, 10)
    assert ndcg.reason is MetricUnavailableReason.EVALUATION_UNIVERSE_NOT_FULLY_JUDGED
    assert ndcg.as_result().status is MetricStatus.N_A
    assert ndcg.as_result().value is None


def test_one_unjudged_posting_in_the_top_ten_closes_precision(frozen, labels_root):
    grades = dict.fromkeys(range(1, 11), 2)
    del grades[4]
    record_labels(frozen, labels_root, grades)
    fingerprint = labelset_fingerprint_of(frozen, labels_root, 10)
    run = run_for(frozen, fingerprint)
    coverage = coverage_from_disk(frozen, labels_root, fingerprint)

    precision = precision_at_k_availability(run, coverage, 10)
    assert not precision.available
    assert precision.reason is MetricUnavailableReason.TOP_K_NOT_FULLY_JUDGED
    assert precision.support.unjudged_in_top_k == (4,)
    with pytest.raises(UnjudgedOpportunityError):
        coverage.grade(4)


def test_a_fully_judged_cohort_opens_every_gate_and_still_yields_no_number(
    frozen, labels_root
):
    grades = {index: (3 if index <= 5 else 0) for index in range(1, COHORT_SIZE + 1)}
    record_labels(frozen, labels_root, grades)
    fingerprint = labelset_fingerprint_of(frozen, labels_root, COHORT_SIZE)
    run = run_for(frozen, fingerprint)
    coverage = coverage_from_disk(frozen, labels_root, fingerprint)

    for gate in (
        precision_at_k_availability,
        recall_at_k_availability,
        ndcg_at_k_availability,
    ):
        availability = gate(run, coverage, 10)
        assert availability.available, availability.reason
        assert availability.support.numerator is None
        assert availability.support.denominator is None
    assert recall_at_k_availability(run, coverage, 10).support.relevant_count == 5


def test_a_fully_judged_cohort_with_nothing_relevant_has_no_recall(
    frozen, labels_root
):
    record_labels(
        frozen, labels_root, dict.fromkeys(range(1, COHORT_SIZE + 1), 1)
    )
    fingerprint = labelset_fingerprint_of(frozen, labels_root, COHORT_SIZE)
    run = run_for(frozen, fingerprint)
    coverage = coverage_from_disk(frozen, labels_root, fingerprint)

    recall = recall_at_k_availability(run, coverage, 10)
    assert recall.reason is MetricUnavailableReason.NO_RELEVANT_ITEMS
    assert recall.support.relevant_count == 0
    # Known-and-zero is not unknown, and the other two gates say so.
    assert ndcg_at_k_availability(run, coverage, 10).available
    assert precision_at_k_availability(run, coverage, 10).available


def test_k_beyond_the_ranking_is_capped_against_real_artefacts(frozen, labels_root):
    record_labels(
        frozen, labels_root, dict.fromkeys(range(1, RANKED_COUNT + 1), 2)
    )
    fingerprint = labelset_fingerprint_of(frozen, labels_root, RANKED_COUNT)
    run = run_for(frozen, fingerprint)
    coverage = coverage_from_disk(frozen, labels_root, fingerprint)

    precision = precision_at_k_availability(run, coverage, 100)
    assert precision.support.k_requested == 100
    assert precision.support.k_effective == RANKED_COUNT
    assert precision.available


# --------------------------------------------------------------------------
# fail-closed bindings, over real artefacts
# --------------------------------------------------------------------------


def test_labels_from_another_frozen_dataset_are_refused(
    frozen, labels_root, tmp_path
):
    """A second snapshot, one posting different — and its labels do not transfer."""
    other_records = tuple(opportunity(index) for index in range(1, COHORT_SIZE))
    other_fingerprint = evaluation_content_fingerprint(
        schema_version=EVALUATION_DATASET_SCHEMA_VERSION,
        cohort=COHORT,
        profile_context=PROFILE_CONTEXT,
        upstream=UPSTREAM,
        records=other_records,
    )
    other = EvaluationDataset(
        manifest=EvaluationDatasetManifest(
            schema_version=EVALUATION_DATASET_SCHEMA_VERSION,
            dataset_id=(
                f"{EVALUATION_DATASET_SCHEMA_VERSION}-{other_fingerprint[:16]}"
            ),
            generated_at="2026-03-02T00:00:00+00:00",
            git_commit=None,
            record_count=len(other_records),
            profile_context=PROFILE_CONTEXT,
            source=SOURCE_IDENTITY,
            cohort=COHORT,
            upstream=UPSTREAM,
            content_fingerprint=other_fingerprint,
        ),
        records=other_records,
    )
    other_paths = write_evaluation_dataset(other, tmp_path / "datasets")
    other_frozen = read_frozen_dataset(other_paths.directory)

    record_labels(frozen, labels_root, {1: 3})
    fingerprint = labelset_fingerprint_of(frozen, labels_root, 1)
    history = read_validated_label_history(frozen, labels_root)
    # Phase 10.2's own binding check, reused rather than restated — which is
    # why its own error surfaces. Both errors descend from the Phase 10.1
    # `EvaluationDatasetError`, so one `except` still catches the layer.
    with pytest.raises(HumanLabelError, match="belongs to dataset"):
        build_label_coverage(
            other_frozen, history, labelset_fingerprint=fingerprint
        )
    assert issubclass(HumanLabelError, EvaluationDatasetError)
    assert issubclass(EvaluationBindingError, EvaluationDatasetError)


def test_a_labelset_from_another_lot_makes_every_metric_unavailable(
    frozen, labels_root
):
    record_labels(frozen, labels_root, dict.fromkeys(range(1, 11), 2))
    run = run_for(frozen, labelset_fingerprint_of(frozen, labels_root, 4))
    coverage = coverage_from_disk(
        frozen, labels_root, labelset_fingerprint_of(frozen, labels_root, 10)
    )
    assert coverage.labelset_fingerprint != run.labelset_fingerprint
    availability = precision_at_k_availability(run, coverage, 10)
    assert availability.reason is MetricUnavailableReason.LABELSET_BINDING_MISMATCH


def test_a_real_calibration_labelset_cannot_be_declared_a_benchmark(
    frozen, labels_root
):
    """Regression 12, over a labelset that was actually written to disk."""
    record_labels(frozen, labels_root, {1: 3, 2: 2})
    fingerprint = labelset_fingerprint_of(frozen, labels_root, 2)
    with pytest.raises(EvidenceClassError, match="independent"):
        build_evaluation_run(
            dataset=frozen,
            universe=build_evaluation_universe(frozen),
            ranking=ranking_from_frozen_dataset(frozen),
            evidence_class=EvidenceClass.INDEPENDENT_BENCHMARK,
            label_protocol_version=HUMAN_LABEL_PROTOCOL_VERSION,
            labelset_fingerprint=fingerprint,
        )


def test_the_recorded_calibration_round_is_refused_by_its_own_digest(frozen):
    """The one real labelset this repository knows of, refused by name."""
    with pytest.raises(EvidenceClassError, match="calibration round"):
        build_evaluation_run(
            dataset=frozen,
            universe=build_evaluation_universe(frozen),
            ranking=ranking_from_frozen_dataset(frozen),
            evidence_class=EvidenceClass.INDEPENDENT_BENCHMARK,
            label_protocol_version=CALIBRATION_V0_PROVENANCE.protocol_version,
            labelset_fingerprint=CALIBRATION_V0_PROVENANCE.labelset_fingerprint,
        )


# --------------------------------------------------------------------------
# the layering, proved rather than described
# --------------------------------------------------------------------------


def test_deciding_availability_opens_no_database_and_writes_no_label(
    frozen, labels_root, tmp_path, monkeypatch
):
    """Regression 14: with `sqlite3.connect` disabled, the whole path still runs.

    The gate needs a frozen directory and a label file. Nothing else — and the
    label file is not touched either, which is checked by digesting it before
    and after.
    """
    record_labels(frozen, labels_root, dict.fromkeys(range(1, 11), 2))
    fingerprint = labelset_fingerprint_of(frozen, labels_root, 10)
    labels_file = labels_root / frozen.dataset_id / "labels.jsonl"
    before = labels_file.read_bytes()

    def refuse(*args, **kwargs):  # pragma: no cover - the point is that it is unused
        raise AssertionError("the metrics layer opened the operational database")

    monkeypatch.setattr(sqlite3, "connect", refuse)

    reread = read_frozen_dataset(frozen.directory)
    run = run_for(reread, fingerprint)
    coverage = coverage_from_disk(reread, labels_root, fingerprint)
    assert precision_at_k_availability(run, coverage, 10).available
    assert not recall_at_k_availability(run, coverage, 10).available
    assert not ndcg_at_k_availability(run, coverage, 10).available

    assert labels_file.read_bytes() == before
    assert not list(tmp_path.rglob("*.db"))
    assert not list(tmp_path.rglob("*.sqlite*"))
