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

import inspect
import sqlite3
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
    CalibrationSelection,
    FrozenEvaluationDataset,
    HumanLabelError,
    append_human_label,
    canonical_label_order,
    labelset_fingerprint,
    build_labelset_report,
    read_frozen_dataset,
    read_validated_label_history,
    select_calibration_sample,
)
from evaluation.metrics import (
    EvaluationBindingError,
    EvaluationMetricsError,
    EvaluationRunProvenance,
    EvaluationUniverseKind,
    EvidenceClass,
    EvidenceClassError,
    MetricRunContext,
    MetricStatus,
    MetricUnavailableReason,
    UnjudgedOpportunityError,
    build_evaluation_run,
    build_evaluation_universe,
    build_label_coverage,
    build_metric_run_context,
    evaluation_run_fingerprint,
    evaluation_universe_fingerprint,
    ndcg_at_k_availability,
    precision_at_k_availability,
    ranking_from_frozen_dataset,
    recall_at_k_availability,
    require_evidence_class,
    universe_judged_coverage,
    verify_evaluation_run,
    verify_metric_run_context,
    verify_evaluation_run_structure,
    verify_label_coverage,
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


def lot_of(
    dataset: FrozenEvaluationDataset, sample_size: int = COHORT_SIZE
) -> CalibrationSelection:
    """A real calibration lot, drawn by Phase 10.2's own deterministic selector.

    The default covers the whole cohort. A smaller lot is stratified rather than
    chosen, so a test that wants to control *which* postings are judged selects
    everything and then judges a subset — which is the state a real round is in
    for most of its life.
    """
    return select_calibration_sample(dataset, sample_size=sample_size)


def coverage_from_disk(
    dataset: FrozenEvaluationDataset,
    root: Path,
    selection: CalibrationSelection,
    *,
    expected_labelset_fingerprint: str | None = None,
):
    """The labelset of one lot, read off disk and digested by recomputation."""
    return build_label_coverage(
        dataset,
        read_validated_label_history(dataset, root),
        selection=selection,
        expected_labelset_fingerprint=expected_labelset_fingerprint,
    )


def run_for(
    dataset: FrozenEvaluationDataset,
    coverage,
    *,
    universe_ids=None,
    **kwargs,
):
    return build_evaluation_run(
        dataset=dataset,
        universe=build_evaluation_universe(dataset, universe_ids),
        ranking=ranking_from_frozen_dataset(dataset),
        coverage=coverage,
        evidence_class=EvidenceClass.DIAGNOSTIC_CALIBRATION,
        **kwargs,
    )


def context_for(
    dataset: FrozenEvaluationDataset, coverage, **kwargs
) -> MetricRunContext:
    """The verified context a gate takes, built from real files."""
    return build_metric_run_context(
        dataset=dataset,
        run=run_for(dataset, coverage, **kwargs),
        coverage=coverage,
    )


# --------------------------------------------------------------------------
# the run, assembled from real artefacts
# --------------------------------------------------------------------------


def test_a_run_binds_a_real_dataset_a_real_ranking_and_a_real_labelset(
    frozen, labels_root
):
    record_labels(frozen, labels_root, {1: 3, 2: 2, 3: 0})
    lot = lot_of(frozen)
    coverage = coverage_from_disk(frozen, labels_root, lot)
    run = run_for(frozen, coverage)

    assert run.dataset_id == frozen.dataset_id
    assert run.dataset_content_fingerprint == frozen.content_fingerprint
    assert run.profile_context_fingerprint == frozen.profile_context_fingerprint
    assert run.universe.size == COHORT_SIZE
    assert run.ranking.length == RANKED_COUNT
    assert run.ranking.opportunity_ids == tuple(range(1, RANKED_COUNT + 1))
    assert run.labelset_fingerprint == coverage.labelset_fingerprint
    # And the digest the run carries is Phase 10.2's own, over the same lot.
    assert run.labelset_fingerprint == build_labelset_report(
        frozen, lot, labels_root
    ).labelset_fingerprint
    assert verify_evaluation_run(run, frozen) == run.run_fingerprint


def test_the_universe_is_the_cohort_and_not_the_ranked_or_labelled_subset(
    frozen, labels_root
):
    """Three different sets, and confusing any two of them breaks a metric."""
    record_labels(frozen, labels_root, {1: 3, 2: 2})
    coverage = coverage_from_disk(frozen, labels_root, lot_of(frozen))
    run = run_for(frozen, coverage)

    assert run.universe.size == COHORT_SIZE
    assert run.ranking.length == RANKED_COUNT
    assert len(coverage.grades) == 2
    judged = universe_judged_coverage(run.universe, coverage)
    assert (judged.judged_count, judged.total_count) == (2, COHORT_SIZE)


def test_a_rerun_over_unchanged_artefacts_produces_the_same_run_fingerprint(
    frozen, labels_root, tmp_path
):
    record_labels(frozen, labels_root, {1: 3})
    coverage = coverage_from_disk(frozen, labels_root, lot_of(frozen))
    first = run_for(
        frozen,
        coverage,
        provenance=EvaluationRunProvenance(
            generated_at="2026-09-11T06:00:00+00:00",
            dataset_directory=str(frozen.directory),
        ),
    )
    second = run_for(
        frozen,
        coverage,
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
    coverage = coverage_from_disk(frozen, labels_root, lot_of(frozen))
    assert coverage.grade(5) == 3
    # Both rows are still in the audit trail; only the effective one is read.
    assert len(read_validated_label_history(frozen, labels_root)) == 2


# --------------------------------------------------------------------------
# the gates, over real judgements
# --------------------------------------------------------------------------


def test_a_judged_top_ten_opens_precision_only(frozen, labels_root):
    """Regression 3, end to end: precision is local, recall and NDCG are not."""
    record_labels(frozen, labels_root, dict.fromkeys(range(1, 11), 2))
    coverage = coverage_from_disk(frozen, labels_root, lot_of(frozen))
    context = context_for(frozen, coverage)

    precision = precision_at_k_availability(context, 10)
    assert precision.available
    assert precision.support.judged_in_top_k == 10

    recall = recall_at_k_availability(context, 10)
    assert recall.reason is MetricUnavailableReason.RECALL_DENOMINATOR_UNKNOWN

    ndcg = ndcg_at_k_availability(context, 10)
    assert ndcg.reason is MetricUnavailableReason.EVALUATION_UNIVERSE_NOT_FULLY_JUDGED
    assert ndcg.as_result().status is MetricStatus.N_A
    assert ndcg.as_result().value is None


def test_one_unjudged_posting_in_the_top_ten_closes_precision(frozen, labels_root):
    grades = dict.fromkeys(range(1, 11), 2)
    del grades[4]
    record_labels(frozen, labels_root, grades)
    coverage = coverage_from_disk(frozen, labels_root, lot_of(frozen))
    precision = precision_at_k_availability(context_for(frozen, coverage), 10)
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
    coverage = coverage_from_disk(frozen, labels_root, lot_of(frozen))
    context = context_for(frozen, coverage)

    for gate in (
        precision_at_k_availability,
        recall_at_k_availability,
        ndcg_at_k_availability,
    ):
        availability = gate(context, 10)
        assert availability.available, availability.reason
        assert availability.support.numerator is None
        assert availability.support.denominator is None
    assert recall_at_k_availability(context, 10).support.relevant_count == 5


def test_a_fully_judged_cohort_with_nothing_relevant_has_no_recall(
    frozen, labels_root
):
    record_labels(
        frozen, labels_root, dict.fromkeys(range(1, COHORT_SIZE + 1), 1)
    )
    coverage = coverage_from_disk(frozen, labels_root, lot_of(frozen))
    context = context_for(frozen, coverage)

    recall = recall_at_k_availability(context, 10)
    assert recall.reason is MetricUnavailableReason.NO_RELEVANT_ITEMS
    assert recall.support.relevant_count == 0
    # Known-and-zero is not unknown, and the other two gates say so.
    assert ndcg_at_k_availability(context, 10).available
    assert precision_at_k_availability(context, 10).available


def test_k_beyond_the_ranking_is_capped_against_real_artefacts(frozen, labels_root):
    record_labels(
        frozen, labels_root, dict.fromkeys(range(1, RANKED_COUNT + 1), 2)
    )
    coverage = coverage_from_disk(frozen, labels_root, lot_of(frozen))
    precision = precision_at_k_availability(context_for(frozen, coverage), 100)
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
    history = read_validated_label_history(frozen, labels_root)
    # Phase 10.2's own binding check, reused rather than restated — which is
    # why its own error surfaces. Both errors descend from the Phase 10.1
    # `EvaluationDatasetError`, so one `except` still catches the layer.
    with pytest.raises(HumanLabelError):
        build_label_coverage(
            other_frozen, history, selection=lot_of(other_frozen)
        )
    assert issubclass(HumanLabelError, EvaluationDatasetError)
    assert issubclass(EvaluationBindingError, EvaluationDatasetError)


def test_a_labelset_from_another_lot_makes_every_metric_unavailable(
    frozen, labels_root
):
    record_labels(frozen, labels_root, dict.fromkeys(range(1, COHORT_SIZE + 1), 2))
    run = run_for(frozen, coverage_from_disk(frozen, labels_root, lot_of(frozen)))
    coverage = coverage_from_disk(frozen, labels_root, lot_of(frozen, 4))
    assert coverage.labelset_fingerprint != run.labelset_fingerprint
    context = build_metric_run_context(
        dataset=frozen, run=run, coverage=coverage
    )
    assert not context.labelset_matches
    availability = precision_at_k_availability(context, 10)
    assert availability.reason is MetricUnavailableReason.LABELSET_BINDING_MISMATCH


def test_a_real_calibration_labelset_cannot_be_declared_a_benchmark(
    frozen, labels_root
):
    """Regression 12, over a labelset that was actually written to disk."""
    record_labels(frozen, labels_root, {1: 3, 2: 2})
    coverage = coverage_from_disk(frozen, labels_root, lot_of(frozen))
    with pytest.raises(EvidenceClassError, match="independent"):
        build_evaluation_run(
            dataset=frozen,
            universe=build_evaluation_universe(frozen),
            ranking=ranking_from_frozen_dataset(frozen),
            coverage=coverage,
            evidence_class=EvidenceClass.INDEPENDENT_BENCHMARK,
        )


def test_the_recorded_calibration_round_is_refused_by_its_own_digest():
    """The one real labelset this repository knows of, refused by name.

    It is refused at the guard rather than at the builder, because the builder
    can no longer be handed a digest at all: a run is bound to a `LabelCoverage`
    whose identity was recomputed from real judgements, and the recorded v0
    round's labels exist only on the operator's machine. Both halves matter —
    the guard refuses the known round, and there is no argument through which it
    could have been claimed anyway.
    """
    with pytest.raises(EvidenceClassError, match="calibration round"):
        require_evidence_class(
            EvidenceClass.INDEPENDENT_BENCHMARK,
            label_protocol_version=CALIBRATION_V0_PROVENANCE.protocol_version,
            labelset_fingerprint=CALIBRATION_V0_PROVENANCE.labelset_fingerprint,
        )
    assert "labelset_fingerprint" not in inspect.signature(
        build_evaluation_run
    ).parameters


# --------------------------------------------------------------------------
# the labelset is the lot's answers, recomputed
# --------------------------------------------------------------------------


def test_the_recomputed_labelset_digest_is_the_one_phase_10_2_reports(
    frozen, labels_root
):
    """Our recomputation and Phase 10.2's report agree over the same lot."""
    record_labels(frozen, labels_root, {1: 3, 2: 2, 5: 0, 9: 1})
    for sample_size in (3, 7, COHORT_SIZE):
        lot = lot_of(frozen, sample_size)
        report = build_labelset_report(frozen, lot, labels_root)
        if report.judged_count == 0:
            continue
        coverage = coverage_from_disk(frozen, labels_root, lot)
        assert coverage.labelset_fingerprint == report.labelset_fingerprint
        assert coverage.judged_opportunity_ids == tuple(
            sorted(set(lot.opportunity_ids) & {1, 2, 5, 9})
        )
        assert coverage.judged_outside_selection == report.judged_outside_selection


def test_judgements_outside_the_lot_stay_in_the_history_and_out_of_the_labelset(
    frozen, labels_root
):
    """The defect this slice was corrected for, over real files.

    Phase 10.2's report leaves out-of-lot judgements out of the digest and names
    them separately; the coverage does exactly the same, so no grade reaches a
    gate that the run's labelset fingerprint does not cover.
    """
    record_labels(frozen, labels_root, dict.fromkeys(range(1, COHORT_SIZE + 1), 2))
    small = lot_of(frozen, 4)
    inside = set(small.opportunity_ids)
    outside = tuple(sorted(set(range(1, COHORT_SIZE + 1)) - inside))

    coverage = coverage_from_disk(frozen, labels_root, small)
    assert set(coverage.grades) == inside
    assert coverage.judged_outside_selection == outside
    for opportunity_id in outside:
        with pytest.raises(UnjudgedOpportunityError):
            coverage.grade(opportunity_id)

    # Nothing was deleted: the history still holds every judgement.
    history = read_validated_label_history(frozen, labels_root)
    assert len(history) == COHORT_SIZE
    # And a run bound to this lot sees only the lot's answers, so a cohort-wide
    # universe is not fully judged however many labels the file holds.
    context = context_for(frozen, coverage)
    judged = universe_judged_coverage(context.universe, coverage)
    assert judged.judged_count == len(inside)
    assert not judged.fully_judged
    assert not ndcg_at_k_availability(context, 10).available


def test_an_expected_labelset_fingerprint_is_checked_against_the_files(
    frozen, labels_root
):
    record_labels(frozen, labels_root, {1: 3, 2: 2})
    lot = lot_of(frozen)
    report = build_labelset_report(frozen, lot, labels_root)
    coverage = coverage_from_disk(
        frozen,
        labels_root,
        lot,
        expected_labelset_fingerprint=report.labelset_fingerprint,
    )
    assert coverage.labelset_fingerprint == report.labelset_fingerprint

    # One more judgement, and the stored manifest no longer describes the file.
    record_labels(frozen, labels_root, {3: 1})
    with pytest.raises(EvaluationBindingError, match="expected to be"):
        coverage_from_disk(
            frozen,
            labels_root,
            lot,
            expected_labelset_fingerprint=report.labelset_fingerprint,
        )


# --------------------------------------------------------------------------
# the trust boundary, over real artefacts
# --------------------------------------------------------------------------


def test_a_label_outside_the_real_lot_cannot_be_smuggled_into_its_labelset(
    frozen, labels_root
):
    """Real files, real lot, every digest recomputed — and still refused."""
    record_labels(frozen, labels_root, dict.fromkeys(range(1, COHORT_SIZE + 1), 2))
    small = lot_of(frozen, 4)
    honest = coverage_from_disk(frozen, labels_root, small)
    intruder = next(
        opportunity_id
        for opportunity_id in range(1, COHORT_SIZE + 1)
        if opportunity_id not in set(small.opportunity_ids)
    )
    history = read_validated_label_history(frozen, labels_root)
    extra = next(
        item for item in history if item.opportunity_id == intruder
    )
    labels = canonical_label_order(honest.labels + (extra,))
    smuggled = replace(
        honest,
        labels=labels,
        labelset_fingerprint=labelset_fingerprint(
            label_schema_version=honest.label_schema_version,
            protocol_version=honest.label_protocol_version,
            dataset_id=honest.dataset_id,
            dataset_content_fingerprint=honest.dataset_content_fingerprint,
            profile_id=honest.profile_id,
            profile_context_fingerprint=honest.profile_context_fingerprint,
            selector_version=honest.selector_version,
            selection_fingerprint=honest.selection_fingerprint,
            labels=labels,
        ),
    )
    assert smuggled.labelset_fingerprint != honest.labelset_fingerprint
    with pytest.raises(EvaluationBindingError, match="not in lot"):
        verify_label_coverage(smuggled, frozen)
    with pytest.raises(EvaluationBindingError, match="not in lot"):
        run_for(frozen, smuggled)


def test_a_universe_naming_a_posting_the_snapshot_does_not_hold_is_refused(
    frozen, labels_root
):
    """`999` is structurally perfect and genuinely absent from the real file."""
    record_labels(frozen, labels_root, {1: 3})
    coverage = coverage_from_disk(frozen, labels_root, lot_of(frozen))
    run = run_for(frozen, coverage)

    members = run.universe.opportunity_ids[:-1] + (999,)
    universe = replace(
        run.universe,
        kind=EvaluationUniverseKind.FIXED_BENCHMARK_POOL,
        opportunity_ids=members,
        size=len(members),
    )
    universe = replace(
        universe, fingerprint=evaluation_universe_fingerprint(universe)
    )
    forged = replace(run, universe=universe)
    forged = replace(
        forged, run_fingerprint=evaluation_run_fingerprint(forged)
    )

    assert 999 not in frozen.opportunity_ids
    # Self-consistent, and refused the moment the snapshot is consulted.
    assert verify_evaluation_run_structure(forged) == forged.run_fingerprint
    with pytest.raises(EvaluationBindingError, match="absent from dataset"):
        verify_evaluation_run(forged, frozen)
    with pytest.raises(EvaluationBindingError, match="absent from dataset"):
        build_metric_run_context(dataset=frozen, run=forged, coverage=coverage)
    with pytest.raises(EvaluationBindingError, match="absent from dataset"):
        build_evaluation_run(
            dataset=frozen,
            universe=universe,
            ranking=run.ranking,
            coverage=coverage,
            evidence_class=EvidenceClass.DIAGNOSTIC_CALIBRATION,
        )


def test_a_hand_built_context_is_verified_by_the_gate_itself(
    frozen, labels_root
):
    """Real files, `build_metric_run_context` bypassed, gate still refuses.

    `MetricRunContext` is a public dataclass, so the test constructs one
    directly around a re-digested invalid run. Nothing in this test calls a
    verifier; the gate does, which is what makes the verification unavoidable.
    """
    record_labels(frozen, labels_root, dict.fromkeys(range(1, COHORT_SIZE + 1), 2))
    coverage = coverage_from_disk(frozen, labels_root, lot_of(frozen))
    run = run_for(frozen, coverage)

    members = run.universe.opportunity_ids[:-1] + (999,)
    universe = replace(
        run.universe,
        kind=EvaluationUniverseKind.FIXED_BENCHMARK_POOL,
        opportunity_ids=members,
        size=len(members),
    )
    universe = replace(
        universe, fingerprint=evaluation_universe_fingerprint(universe)
    )
    forged = replace(run, universe=universe)
    forged = replace(forged, run_fingerprint=evaluation_run_fingerprint(forged))

    context = MetricRunContext(dataset=frozen, run=forged, coverage=coverage)
    for gate in (
        precision_at_k_availability,
        recall_at_k_availability,
        ndcg_at_k_availability,
    ):
        with pytest.raises(EvaluationBindingError, match="absent from dataset"):
            gate(context, 10)


def test_a_hand_built_context_cannot_override_a_real_labelset_mismatch(
    frozen, labels_root
):
    """Two real labelsets over one dataset, bundled by hand: still N_A."""
    record_labels(frozen, labels_root, dict.fromkeys(range(1, COHORT_SIZE + 1), 2))
    run = run_for(frozen, coverage_from_disk(frozen, labels_root, lot_of(frozen)))
    other_lot = coverage_from_disk(frozen, labels_root, lot_of(frozen, 4))
    assert other_lot.labelset_fingerprint != run.labelset_fingerprint

    context = MetricRunContext(dataset=frozen, run=run, coverage=other_lot)
    assert verify_metric_run_context(context) is False
    for gate in (
        precision_at_k_availability,
        recall_at_k_availability,
        ndcg_at_k_availability,
    ):
        availability = gate(context, 10)
        assert not availability.available
        assert availability.reason is MetricUnavailableReason.LABELSET_BINDING_MISMATCH


def test_a_gate_cannot_be_reached_with_a_re_sealed_invalid_run(
    frozen, labels_root
):
    """The builder refuses the same forgery, before any gate is reached."""
    record_labels(frozen, labels_root, dict.fromkeys(range(1, COHORT_SIZE + 1), 2))
    coverage = coverage_from_disk(frozen, labels_root, lot_of(frozen))
    run = run_for(frozen, coverage)

    duplicated = replace(
        run.universe,
        opportunity_ids=(run.universe.opportunity_ids[0],)
        + run.universe.opportunity_ids,
        size=run.universe.size + 1,
    )
    duplicated = replace(
        duplicated, fingerprint=evaluation_universe_fingerprint(duplicated)
    )
    forged = replace(run, universe=duplicated)
    forged = replace(
        forged, run_fingerprint=evaluation_run_fingerprint(forged)
    )
    with pytest.raises(EvaluationMetricsError, match="repeats opportunity ids"):
        build_metric_run_context(dataset=frozen, run=forged, coverage=coverage)


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
    labels_file = labels_root / frozen.dataset_id / "labels.jsonl"
    before = labels_file.read_bytes()

    def refuse(*args, **kwargs):  # pragma: no cover - the point is that it is unused
        raise AssertionError("the metrics layer opened the operational database")

    monkeypatch.setattr(sqlite3, "connect", refuse)

    reread = read_frozen_dataset(frozen.directory)
    coverage = coverage_from_disk(reread, labels_root, lot_of(reread))
    context = context_for(reread, coverage)
    assert precision_at_k_availability(context, 10).available
    assert not recall_at_k_availability(context, 10).available
    assert not ndcg_at_k_availability(context, 10).available

    assert labels_file.read_bytes() == before
    assert not list(tmp_path.rglob("*.db"))
    assert not list(tmp_path.rglob("*.sqlite*"))
