"""Phase 10.5 against real files: a frozen dataset, real labels, a real store.

Every artefact here is produced by the phase that owns it — the dataset by Phase
10.1's own `write_evaluation_dataset` and read back through Phase 10.2's
integrity gate, the judgements by Phase 10.2's own `append_human_label`, the
business metrics by Phase 10.4's own builder, the run by Phase 10.5's — into a
`tmp_path`. That is what this module adds over the unit suites: those construct
a `FrozenEvaluationDataset` directly, which is legitimate for testing semantics
and does bypass the gate that turns two files on disk into one. Here the gate
runs, so a disagreement between Phase 10.1's writer and Phase 10.5's reader
shows up here rather than on an operator's machine.

The whole operator sequence is walked end to end: read the frozen dataset,
verify the business metric run, rebuild the context, build the experiment run,
full-verify it, write it content-addressed, read it back, structurally verify the
readback, full-verify the readback, build the error analysis, render the report.

Nothing in this module opens the operational database — one test proves it by
making every `sqlite3.connect` in the process raise — and no real opportunity
and no real judgement appears anywhere in it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from evaluation.business_metrics import (
    MERGED_DUPLICATE_EXCLUSION_KEY,
    PLACEHOLDER_FINGERPRINT,
    PROFILE_TARGET_BINDING_VERSION,
    ProfileTargetBindingEvidence,
    build_business_metric_computation_context,
    build_business_metric_run,
    build_business_metric_run_context,
    build_frozen_cohort_binding,
    profile_target_binding_fingerprint,
    verify_business_metric_run,
)
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
from evaluation.experiments import (
    CohortProjection,
    ExperimentRunProvenance,
    ExperimentRunWriteStatus,
    OverlapPair,
    OverlapStatus,
    ProjectionStatus,
    RankingEvaluationInputs,
    RankingExperimentStatus,
    build_error_analysis,
    build_experiment_report,
    build_experiment_run,
    build_experiment_run_context,
    read_experiment_run,
    render_experiment_report_markdown,
    verify_experiment_run,
    verify_experiment_run_structure,
    write_experiment_run,
)
from evaluation.labeling import (
    FrozenEvaluationDataset,
    append_human_label,
    read_frozen_dataset,
    read_validated_label_history,
    select_calibration_sample,
)
from evaluation.metrics import (
    EvidenceClass,
    MetricName,
    MetricStatus,
    build_evaluation_run,
    build_evaluation_universe,
    build_label_coverage,
    ranking_from_frozen_dataset,
)

PROFILE_ID = 1
USER_ID = 7
PROFILE_FINGERPRINT = "9" * 64
TARGET_COUNTRY = "MA"
OTHER_COUNTRY = "FR"
GENERATED_AT = "2026-03-01T09:15:00+00:00"

#: Sixteen invented postings; twelve ranked by the invented Recommendation run.
#: The four unranked ones are the point of the cohort — they are in the universe
#: and not in the ranking — and postings 15 and 16 are additionally out of
#: target and out of scope, so the geography and Data/AI projections are strict
#: subsets rather than the whole cohort.
COHORT_SIZE = 16
RANKED_COUNT = 12
OUT_OF_TARGET = (15,)
OUT_OF_SCOPE = (16,)
UNMATCHED = (13, 14)
UNCLASSIFIED = (14,)

PROFILE_CONTEXT = EvaluationProfileContext(
    profile_id=PROFILE_ID, user_id=USER_ID, fingerprint=PROFILE_FINGERPRINT
)

COHORT = EvaluationCohortDefinition(
    version=EVALUATION_COHORT_VERSION,
    criteria=EVALUATION_COHORT_CRITERIA,
    ordering=EVALUATION_CANONICAL_ORDER,
    excluded_counts={MERGED_DUPLICATE_EXCLUSION_KEY: 0, "inactive": 3},
)

UPSTREAM = EvaluationUpstreamProvenance(
    matching_status="PRESENT",
    matching_run_id=4,
    matching_run_fingerprint="1" * 64,
    matching_engine_version="matching-engine-v1",
    matching_rules_version="matching-rules-v1",
    matching_selection_version="matching-selection-v1",
    recommendation_status="PRESENT",
    recommendation_run_id=5,
    recommendation_run_fingerprint="2" * 64,
    recommendation_engine_version="recommendation-engine-v1",
    recommendation_rules_version="recommendation-rules-v1",
)

SOURCE_IDENTITY = EvaluationSourceIdentity(
    database_path="data/invented.db",
    database_bytes=2048,
    database_sha256="d" * 64,
    wal_present=False,
    wal_bytes=None,
    shm_present=False,
    applied_migrations=("0001_invented",),
)


def opportunity(opportunity_id: int) -> EvaluationOpportunityRecord:
    """One invented posting, carrying the frozen blocks Phase 10.5 reads."""
    country = (
        OTHER_COUNTRY if opportunity_id in OUT_OF_TARGET else TARGET_COUNTRY
    )
    qualification = None
    if opportunity_id not in UNCLASSIFIED:
        qualification = EvaluationQualificationRecord(
            qualification=(
                "OUT_OF_SCOPE"
                if opportunity_id in OUT_OF_SCOPE
                else "CORE_TARGET"
            ),
            primary_domain="DATA_SCIENCE",
            opportunity_type="PFE",
            employment_type="FULL_TIME",
            listing_quality="NORMAL_LISTING",
            classifier_version="qualification-v1",
            input_fingerprint="1" * 64,
            classified_at="2026-02-20T10:00:00+00:00",
            fine_primary_category=None,
            fine_secondary_categories=None,
            fine_classifier_version=None,
        )
    matching = None
    if opportunity_id not in UNMATCHED:
        matching = EvaluationMatchingRecord(
            lane="STRONG",
            match_quality=0.7,
            evidence_coverage=0.8,
            assessment_fingerprint="4" * 64,
        )
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
        country=country,
        remote_type="ONSITE",
        description=f"Invented description {opportunity_id}.",
        source_url=f"https://example.invalid/offers/{opportunity_id}",
        application_url=None,
        canonical_url=f"https://example.invalid/offers/{opportunity_id}",
        status="active",
        is_active=True,
        published_at="2026-02-20T09:00:00+00:00",
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
                canonical_url=(
                    f"https://example.invalid/offers/{opportunity_id}"
                ),
                discovered_at="2026-01-01T00:00:00+00:00",
            ),
        ),
        qualification=qualification,
        geography_segments=(
            EvaluationGeographySegmentRecord(
                segment_position=1,
                raw_segment="Casablanca",
                status="RESOLVED",
                rule_id="city-registry-v1",
                country_code=country,
                city_key=None,
                resolver_version="geography-v1",
            ),
        ),
        eligibility=None,
        matching=matching,
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
        generated_at=GENERATED_AT,
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
    """A real frozen dataset, written by Phase 10.1 and through Phase 10.2's gate."""
    paths = write_evaluation_dataset(build_dataset(), tmp_path / "datasets")
    return read_frozen_dataset(paths.directory)


@pytest.fixture
def manifest(frozen: FrozenEvaluationDataset) -> dict:
    """The manifest as the file on disk holds it, not as the writer built it."""
    return json.loads(
        (frozen.directory / "manifest.json").read_text(encoding="utf-8")
    )


def target_binding(
    *, country_code: str | None = TARGET_COUNTRY
) -> ProfileTargetBindingEvidence:
    draft = ProfileTargetBindingEvidence(
        binding_version=PROFILE_TARGET_BINDING_VERSION,
        profile_id=PROFILE_ID,
        profile_fingerprint=PROFILE_FINGERPRINT,
        country_code=country_code,
        rule_id=(
            "mobility-restricted-country-v1"
            if country_code is not None
            else "mobility-open-v1"
        ),
        binding_fingerprint=PLACEHOLDER_FINGERPRINT,
    )
    from dataclasses import replace

    return replace(
        draft, binding_fingerprint=profile_target_binding_fingerprint(draft)
    )


def business_run_of(
    frozen: FrozenEvaluationDataset,
    manifest: dict,
    *,
    country_code: str | None = TARGET_COUNTRY,
):
    """A real Phase 10.4 run over the records the frozen files hold."""
    binding = build_frozen_cohort_binding(frozen.records, manifest)
    context = build_business_metric_run_context(
        cohort_binding=binding,
        profile_target_binding=target_binding(country_code=country_code),
    )
    return build_business_metric_run(
        build_business_metric_computation_context(
            frozen.records, manifest, context
        )
    )


def labelled(
    frozen: FrozenEvaluationDataset, root: Path, grades: dict[int, int]
):
    """Real judgements, written through Phase 10.2's append-only writer."""
    for opportunity_id, grade in sorted(grades.items()):
        append_human_label(
            frozen,
            opportunity_id=opportunity_id,
            relevance_grade=grade,
            root=root,
        )
    return build_label_coverage(
        frozen,
        read_validated_label_history(frozen, root),
        selection=select_calibration_sample(frozen, sample_size=COHORT_SIZE),
    )


def ranking_inputs(frozen: FrozenEvaluationDataset, coverage):
    return RankingEvaluationInputs(
        evaluation_run=build_evaluation_run(
            dataset=frozen,
            universe=build_evaluation_universe(frozen),
            ranking=ranking_from_frozen_dataset(frozen),
            coverage=coverage,
            evidence_class=EvidenceClass.DIAGNOSTIC_CALIBRATION,
        ),
        label_coverage=coverage,
    )


def context_of(
    frozen: FrozenEvaluationDataset,
    manifest: dict,
    root: Path,
    *,
    grades: dict[int, int] | None = None,
    country_code: str | None = TARGET_COUNTRY,
):
    """The whole four-layer stack, every layer from real files."""
    if grades is None:
        grades = {
            index: (3 if index <= 5 else 1) for index in range(1, COHORT_SIZE + 1)
        }
    coverage = labelled(frozen, root, grades)
    return build_experiment_run_context(
        frozen_dataset=frozen,
        manifest=manifest,
        business_metric_run=business_run_of(
            frozen, manifest, country_code=country_code
        ),
        ranking_evaluation_inputs=ranking_inputs(frozen, coverage),
    )


# --------------------------------------------------------------------------
# the whole operator sequence, end to end
# --------------------------------------------------------------------------


def test_the_whole_sequence_over_real_files(frozen, manifest, tmp_path) -> None:
    """Read, verify, build, verify, write, read back, verify, analyse, report."""
    # 1-2. the frozen dataset is read through the gate; the Phase 10.4 run is
    # verified against the records those files hold.
    business = business_run_of(frozen, manifest)
    verify_business_metric_run(business, frozen.records, manifest)

    # 3. the context, rebuilt and verified.
    context = context_of(frozen, manifest, tmp_path / "labels")

    # 4-5. the run, built and fully verified.
    run = build_experiment_run(
        context,
        provenance=ExperimentRunProvenance(
            generated_at="2026-09-13T00:00:00+00:00",
            dataset_directory=str(frozen.directory),
            label_root=str(tmp_path / "labels"),
        ),
    )
    assert verify_experiment_run(run, context) == run.run_fingerprint

    # 6. written, content-addressed.
    root = tmp_path / "experiment_runs"
    stored = write_experiment_run(run, context, root=root)
    assert stored.status is ExperimentRunWriteStatus.CREATED
    assert stored.directory == root / run.run_fingerprint
    assert stored.run_file.is_file()

    # 7-9. read back, structurally verified, then fully verified.
    back = read_experiment_run(run.run_fingerprint, root=root)
    assert verify_experiment_run_structure(back) == run.run_fingerprint
    assert verify_experiment_run(back, context) == run.run_fingerprint

    # 10-11. the two derived views.
    analysis = build_error_analysis(back, context)
    assert analysis.run_fingerprint == run.run_fingerprint
    markdown = render_experiment_report_markdown(
        build_experiment_report(back, context)
    )
    assert markdown.startswith("# Experiment run report")
    assert run.run_fingerprint in markdown


def test_the_twelve_blocks_are_all_present_over_real_files(
    frozen, manifest, tmp_path
) -> None:
    context = context_of(frozen, manifest, tmp_path / "labels")
    run = build_experiment_run(context)
    assert len(run.projections) == 5
    assert len(run.overlaps) == 6
    assert run.ranking is not None


def test_the_projections_over_real_files_say_what_the_records_say(
    frozen, manifest, tmp_path
) -> None:
    context = context_of(frozen, manifest, tmp_path / "labels")
    run = build_experiment_run(context)

    cohort = run.projection(CohortProjection.FROZEN_COHORT)
    assert cohort.included_count == COHORT_SIZE
    assert cohort.cohort_share == 1.0

    geo = run.projection(CohortProjection.GEO_NOT_EXPLICITLY_OUT_OF_TARGET)
    assert geo.excluded_count == len(OUT_OF_TARGET)
    assert 15 not in geo.included_ids

    data_ai = run.projection(
        CohortProjection.DATA_AI_NOT_EXPLICITLY_OUT_OF_SCOPE
    )
    assert data_ai.excluded_count == len(OUT_OF_SCOPE)
    # The unclassified posting is *included*: nobody ruled it out.
    assert 14 in data_ai.included_ids

    matching = run.projection(CohortProjection.MATCHING_OBSERVED)
    assert matching.excluded_count == len(UNMATCHED)

    recommendation = run.projection(CohortProjection.RECOMMENDATION_OBSERVED)
    assert recommendation.included_count == RANKED_COUNT
    assert recommendation.included_ids == tuple(range(1, RANKED_COUNT + 1))


def test_the_ranking_over_real_files_answers_the_four_questions(
    frozen, manifest, tmp_path
) -> None:
    context = context_of(frozen, manifest, tmp_path / "labels")
    run = build_experiment_run(context)
    assert run.ranking.status is RankingExperimentStatus.COMPUTED
    for entry in run.ranking.metric_results:
        assert entry.result.status is MetricStatus.COMPUTED
        assert entry.result.value is not None
    # The universe is the whole cohort, not the twelve ranked postings.
    for entry in run.ranking.metric_results:
        assert entry.result.support.universe_size == COHORT_SIZE
    assert (
        run.ranking.entry(MetricName.PRECISION_AT_K, 10).result.support.k_effective
        == 10
    )


def test_a_rerun_over_unchanged_artefacts_is_the_same_run(
    frozen, manifest, tmp_path
) -> None:
    """The identity is of the experiment, not of the moment it was run."""
    root = tmp_path / "experiment_runs"
    first_context = context_of(frozen, manifest, tmp_path / "labels")
    first = build_experiment_run(
        first_context,
        provenance=ExperimentRunProvenance(generated_at="2026-01-01T00:00:00Z"),
    )
    write_experiment_run(first, first_context, root=root)

    second = build_experiment_run(
        first_context,
        provenance=ExperimentRunProvenance(
            generated_at="2026-12-31T23:59:59Z",
            dataset_directory="/somewhere/else",
        ),
    )
    assert second.run_fingerprint == first.run_fingerprint
    again = write_experiment_run(second, first_context, root=root)
    assert again.status is ExperimentRunWriteStatus.UNCHANGED
    # And the stored provenance is the original one.
    assert again.run.provenance.generated_at == "2026-01-01T00:00:00Z"


def test_an_unknown_target_makes_the_geography_blocks_na_over_real_files(
    frozen, manifest, tmp_path
) -> None:
    context = context_of(
        frozen, manifest, tmp_path / "labels", country_code=None
    )
    run = build_experiment_run(context)
    geo = run.projection(CohortProjection.GEO_NOT_EXPLICITLY_OUT_OF_TARGET)
    assert geo.status is ProjectionStatus.N_A
    assert geo.included_opportunity_ids is None
    for pair in (
        OverlapPair.GEO_AND_DATA_AI,
        OverlapPair.GEO_AND_MATCHING,
        OverlapPair.GEO_AND_RECOMMENDATION,
    ):
        assert run.overlap(pair).status is OverlapStatus.N_A
    # The other three overlaps and the ranking are unaffected.
    assert (
        run.overlap(OverlapPair.DATA_AI_AND_MATCHING).status
        is OverlapStatus.COMPUTED
    )
    assert run.ranking.status is RankingExperimentStatus.COMPUTED
    verify_experiment_run(run, context)


def test_a_partially_judged_round_yields_phase_10_3s_own_refusals(
    frozen, manifest, tmp_path
) -> None:
    """The state a real calibration round is in for most of its life."""
    context = context_of(
        frozen, manifest, tmp_path / "labels", grades={1: 3, 2: 1, 3: 0}
    )
    run = build_experiment_run(context)
    assert run.ranking.status is RankingExperimentStatus.COMPUTED
    statuses = {
        (entry.metric, entry.k_requested): entry.result.status
        for entry in run.ranking.metric_results
    }
    assert statuses[(MetricName.RECALL_AT_K, 10)] is MetricStatus.N_A
    assert statuses[(MetricName.NDCG_AT_K, 10)] is MetricStatus.N_A
    for entry in run.ranking.metric_results:
        if entry.result.status is MetricStatus.N_A:
            assert entry.result.value is None
            assert entry.result.reason is not None
    verify_experiment_run(run, context)


def test_the_error_analysis_over_real_files_names_real_candidates(
    frozen, manifest, tmp_path
) -> None:
    # Posting 16 is relevant and was never ranked: the candidate false negative
    # the frozen snapshot exists to preserve.
    grades = {index: 0 for index in range(1, COHORT_SIZE + 1)}
    grades[16] = 3
    context = context_of(frozen, manifest, tmp_path / "labels", grades=grades)
    run = build_experiment_run(context)
    analysis = build_error_analysis(run, context)
    assert {
        (item.opportunity_id, item.rank_position)
        for item in analysis.ranking.false_negative_candidates
    } == {(16, None)}
    # And the ten judged-zero postings in the top ten are the candidates.
    assert {
        item.opportunity_id
        for item in analysis.ranking.false_positive_candidates
    } == set(range(1, 11))


def test_a_readback_of_a_run_written_elsewhere_is_refused(
    frozen, manifest, tmp_path
) -> None:
    context = context_of(frozen, manifest, tmp_path / "labels")
    run = build_experiment_run(context)
    root = tmp_path / "experiment_runs"
    write_experiment_run(run, context, root=root)
    other = tmp_path / "moved"
    other.mkdir()
    destination = other / ("e" * 64)
    destination.mkdir()
    (destination / "run.json").write_text(
        (root / run.run_fingerprint / "run.json").read_text("utf-8"),
        encoding="utf-8",
    )
    from evaluation.experiments import ExperimentBindingError

    with pytest.raises(ExperimentBindingError):
        read_experiment_run("e" * 64, root=other)


def test_the_stored_document_is_one_json_file_and_nothing_else(
    frozen, manifest, tmp_path
) -> None:
    context = context_of(frozen, manifest, tmp_path / "labels")
    run = build_experiment_run(context)
    root = tmp_path / "experiment_runs"
    stored = write_experiment_run(run, context, root=root)
    assert sorted(path.name for path in stored.directory.iterdir()) == [
        "run.json"
    ]
    # No stray temporary file was left beside the run directories.
    assert sorted(path.name for path in root.iterdir()) == [run.run_fingerprint]


# --------------------------------------------------------------------------
# the production boundary, proved rather than asserted
# --------------------------------------------------------------------------


def test_no_sqlite_connection_is_opened_anywhere_in_the_sequence(
    frozen, manifest, tmp_path, monkeypatch
) -> None:
    """The whole Phase 10.5 sequence, with every `sqlite3.connect` exploding."""

    def refuse(*args, **kwargs):
        raise AssertionError(
            "Phase 10.5 opened a database connection; it reads frozen files"
        )

    context = context_of(frozen, manifest, tmp_path / "labels")
    monkeypatch.setattr(sqlite3, "connect", refuse)
    run = build_experiment_run(context)
    verify_experiment_run(run, context)
    root = tmp_path / "experiment_runs"
    write_experiment_run(run, context, root=root)
    back = read_experiment_run(run.run_fingerprint, root=root)
    verify_experiment_run(back, context)
    build_error_analysis(back, context)
    render_experiment_report_markdown(build_experiment_report(back, context))


def test_nothing_in_services_imports_the_experiments_package() -> None:
    """`evaluation` never feeds production, and this is the strongest form."""
    import ast

    offenders = []
    for root in ("services", "apps"):
        for path in Path(root).rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):  # pragma: no cover - defensive
                continue
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                for module in modules:
                    if module == "evaluation" or module.startswith("evaluation."):
                        offenders.append(f"{path}: {module}")
    assert offenders == []


def test_the_experiments_package_is_not_re_exported_from_the_root() -> None:
    """`evaluation/__init__.py` is documentation, never a facade.

    Asserted on the module's own source rather than on `hasattr`: importing
    `evaluation.experiments` binds the submodule on its parent package as a
    matter of Python's import mechanics, which is not a re-export and is not
    something this package could prevent. What it can and does prevent is
    importing the subpackage itself, or lifting any of its names up — which is
    what would let something reach `build_experiment_run` through `evaluation`.
    """
    import ast

    import evaluation

    tree = ast.parse(Path(evaluation.__file__).read_text(encoding="utf-8"))
    imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert imports == [], "the root module imports nothing at all"
    assert getattr(evaluation, "__all__", None) is None
    for name in (
        "build_experiment_run",
        "compute_projection",
        "compute_overlap",
        "compute_ranking_experiment",
        "write_experiment_run",
        "ExperimentRun",
    ):
        assert not hasattr(evaluation, name), name
