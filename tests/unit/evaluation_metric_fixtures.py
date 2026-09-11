"""Invented Phase 10 artefacts shared by the Phase 10.3 unit tests.

Everything here is built in memory: a `FrozenEvaluationDataset` assembled from
record mappings, labels made by hand, lots drawn by Phase 10.2's own selector.
No file is written, no real opportunity appears, and the operational database is
never opened. The integrity gate that turns two files on disk into a frozen
dataset is Phase 10.2's and is exercised against real files in
`tests/integration`.

Held in one module because the contract tests and the formula tests need the
same fixtures, and two copies of a dataset builder eventually stop being the
same dataset.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from evaluation.labeling import (
    HUMAN_LABEL_PROTOCOL_VERSION,
    HUMAN_LABEL_SCHEMA_VERSION,
    CalibrationSelection,
    FrozenEvaluationDataset,
    HumanRelevanceLabel,
    LabelDiagnostics,
    select_calibration_sample,
)
from evaluation.metrics import (
    EvidenceClass,
    LabelCoverage,
    build_evaluation_run,
    build_evaluation_universe,
    build_label_coverage,
    build_metric_run_context,
    ranking_from_frozen_dataset,
)

DATASET_CONTENT_FINGERPRINT = "a" * 64
DATASET_ID = "evaluation-dataset-v3-" + DATASET_CONTENT_FINGERPRINT[:16]
PROFILE_CONTEXT_FINGERPRINT = "9" * 64


# --------------------------------------------------------------------------
# invented artefacts
# --------------------------------------------------------------------------


def record(opportunity_id: int, rank_position: int | None) -> dict[str, Any]:
    """One frozen record, reduced to the two fields this slice reads.

    A `recommendation` of `None` is Phase 10.1's statement that the
    Recommendation run never covered the posting — the candidate false negative
    the snapshot exists to preserve — and it is exactly what must not become a
    rank of 0 or a place at the bottom of the list.
    """
    recommendation = (
        None
        if rank_position is None
        else {
            "rank_position": rank_position,
            "disposition": "RECOMMENDED",
            "recommendation_score": 0.5,
            "evidence_coverage": 0.8,
            "assessment_fingerprint": "f" * 64,
        }
    )
    return {"opportunity_id": opportunity_id, "recommendation": recommendation}


def frozen_dataset(
    records: Sequence[Mapping[str, Any]],
    *,
    dataset_id: str = DATASET_ID,
    content_fingerprint: str = DATASET_CONTENT_FINGERPRINT,
    profile_id: int = 1,
    profile_context_fingerprint: str = PROFILE_CONTEXT_FINGERPRINT,
) -> FrozenEvaluationDataset:
    return FrozenEvaluationDataset(
        directory=Path("/invented/datasets") / dataset_id,
        schema_version="evaluation-dataset-v3",
        dataset_id=dataset_id,
        content_fingerprint=content_fingerprint,
        record_count=len(records),
        profile_id=profile_id,
        user_id=1,
        profile_context_fingerprint=profile_context_fingerprint,
        ordering="opportunity_id ASC",
        records=tuple(records),
    )


def ranked_dataset(
    size: int = 20, ranked: int = 12, **kwargs: Any
) -> FrozenEvaluationDataset:
    """`size` opportunities, the first `ranked` of which the pipeline ordered."""
    return frozen_dataset(
        [
            record(index, index if index <= ranked else None)
            for index in range(1, size + 1)
        ],
        **kwargs,
    )


def label(
    opportunity_id: int,
    grade: int,
    *,
    dataset: FrozenEvaluationDataset,
    revision: int = 1,
    relabel_reason: str | None = None,
    protocol_version: str = HUMAN_LABEL_PROTOCOL_VERSION,
) -> HumanRelevanceLabel:
    return HumanRelevanceLabel(
        label_schema_version=HUMAN_LABEL_SCHEMA_VERSION,
        protocol_version=protocol_version,
        dataset_id=dataset.dataset_id,
        dataset_content_fingerprint=dataset.content_fingerprint,
        profile_id=dataset.profile_id,
        profile_context_fingerprint=dataset.profile_context_fingerprint,
        opportunity_id=opportunity_id,
        relevance_grade=grade,
        diagnostics=LabelDiagnostics(),
        reason_tags=(),
        note=None,
        labeled_at="2026-04-01T00:00:00+00:00",
        revision=revision,
        relabel_reason=relabel_reason,
    )


def whole_lot(dataset: FrozenEvaluationDataset) -> CalibrationSelection:
    """A calibration lot covering the whole dataset.

    Phase 10.2 draws a *stratified* lot, so a lot smaller than the cohort holds
    postings nobody chose by hand. Most tests here want to decide which postings
    are judged rather than which are selected, so they select everything and
    then judge a subset — which is also the state a real round is in for most of
    its life: a lot drawn, and part of it answered.
    """
    return select_calibration_sample(dataset, sample_size=len(dataset.records))


def coverage_of(
    grades: Mapping[int, int],
    dataset: FrozenEvaluationDataset,
    *,
    selection: CalibrationSelection | None = None,
    expected_labelset_fingerprint: str | None = None,
) -> LabelCoverage:
    return build_label_coverage(
        dataset,
        [
            label(opportunity_id, grade, dataset=dataset)
            for opportunity_id, grade in sorted(grades.items())
        ],
        selection=whole_lot(dataset) if selection is None else selection,
        expected_labelset_fingerprint=expected_labelset_fingerprint,
    )


def run_over(
    dataset: FrozenEvaluationDataset,
    *,
    universe_ids: Sequence[int] | None = None,
    coverage: LabelCoverage | None = None,
    **kwargs: Any,
):
    universe = build_evaluation_universe(dataset, universe_ids)
    ranking = ranking_from_frozen_dataset(dataset)
    return build_evaluation_run(
        dataset=dataset,
        universe=universe,
        ranking=ranking,
        coverage=coverage_of({1: 2}, dataset) if coverage is None else coverage,
        evidence_class=EvidenceClass.DIAGNOSTIC_CALIBRATION,
        **kwargs,
    )


def context_of(
    dataset: FrozenEvaluationDataset,
    coverage: LabelCoverage | None = None,
    **kwargs: Any,
):
    """A verified context: the only thing an availability gate accepts."""
    resolved = coverage_of({1: 2}, dataset) if coverage is None else coverage
    return build_metric_run_context(
        dataset=dataset,
        run=run_over(dataset, coverage=resolved, **kwargs),
        coverage=resolved,
    )
