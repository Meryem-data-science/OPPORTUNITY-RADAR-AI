"""Invented Phase 10.5 artefacts shared by the Phase 10.5 unit tests.

Phase 10.5 sits on top of four frozen layers, so a fixture for it has to be
coherent across all four at once: a Phase 10.1 snapshot whose manifest digests
to its own `dataset_id`, a Phase 10.4 run computed and verified over exactly
those records, a Phase 10.2 calibration lot drawn by that package's own
selector, and a Phase 10.3 evaluation run bound to all of it. Building that by
hand in every test would be four chances per test to build it slightly
differently.

So the record and manifest builders are **reused** from
`business_metric_fixtures` rather than restated — one snapshot shape across
Phase 10.4's tests and Phase 10.5's, which is the point: the two phases are
verified against the same frozen cohort, and a second record builder here would
eventually stop building the same record.

Everything is built in memory and every snapshot is invented, but invented **in
Phase 10.1's own shape**, so the real verification paths run over these fixtures
exactly as they would over a real snapshot. Nothing here opens the operational
database, writes a file, makes an HTTP request or touches a real opportunity.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from evaluation.business_metrics import (
    build_business_metric_run,
    build_business_metric_computation_context,
    build_frozen_cohort_binding,
)
from evaluation.experiments import (
    ExperimentRunContext,
    RankingEvaluationInputs,
    build_experiment_run_context,
)
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
    ranking_from_frozen_dataset,
)

# The one snapshot vocabulary, borrowed rather than restated.
from tests.unit.business_metric_fixtures import (  # noqa: F401 - re-exported
    ALPHA,
    BETA,
    GAMMA,
    GENERATED_AT,
    OTHER_COUNTRY,
    PROFILE_FINGERPRINT,
    PROFILE_ID,
    TARGET_COUNTRY,
    USER_ID,
    full_run_context,
    manifest_payload,
    qualification,
    record,
    run_context,
    segment,
    source,
    target_binding,
)

__all__ = [
    "ALPHA",
    "BETA",
    "GAMMA",
    "OTHER_COUNTRY",
    "PROFILE_FINGERPRINT",
    "PROFILE_ID",
    "TARGET_COUNTRY",
    "coverage_of",
    "dataset_of",
    "evaluation_run_over",
    "experiment_context",
    "full_run_context",
    "label",
    "manifest_payload",
    "matching",
    "qualification",
    "ranked_records",
    "recommendation",
    "record",
    "run_context",
    "segment",
    "source",
    "target_binding",
    "whole_lot",
]


# --------------------------------------------------------------------------
# the frozen blocks Phase 10.5 reads for presence
# --------------------------------------------------------------------------


def matching(
    *,
    lane: str = "STRONG",
    match_quality: float | None = 0.72,
    evidence_coverage: float = 0.8,
) -> dict[str, Any]:
    """A frozen matching assessment.

    `match_quality=None` with `evidence_coverage=0.0` is Phase 4's own contract
    for "nothing was measured", and `MATCHING_OBSERVED` must include it: the
    projection is about presence, not quality.
    """
    return {
        "lane": lane,
        "match_quality": match_quality,
        "evidence_coverage": evidence_coverage,
        "assessment_fingerprint": "4" * 64,
    }


def recommendation(
    rank_position: int,
    *,
    disposition: str = "RECOMMENDED",
    recommendation_score: float | None = 0.61,
    evidence_coverage: float = 0.75,
) -> dict[str, Any]:
    """A frozen recommendation assessment at one production rank position."""
    return {
        "rank_position": rank_position,
        "disposition": disposition,
        "recommendation_score": recommendation_score,
        "evidence_coverage": evidence_coverage,
        "assessment_fingerprint": "5" * 64,
    }


def ranked_records(
    size: int = 12,
    *,
    ranked: int = 8,
    out_of_target: Sequence[int] = (),
    out_of_scope: Sequence[int] = (),
    unmatched: Sequence[int] = (),
    unclassified: Sequence[int] = (),
    unplaced: Sequence[int] = (),
) -> tuple[dict[str, Any], ...]:
    """`size` postings, the first `ranked` of which the pipeline ordered.

    The keyword arguments carve out the cases the five projections are about,
    each named for the *frozen fact* it creates rather than for the projection
    it happens to exercise:

        out_of_target   a segment RESOLVED to another country -> excluded by G
        unplaced        an UNKNOWN segment -> **included** by G
        out_of_scope    qualification OUT_OF_SCOPE -> excluded by A
        unclassified    no qualification block -> **included** by A
        unmatched       no matching block -> excluded by M

    Everything else carries a RESOLVED segment in the target country, a
    CORE_TARGET qualification and a matching assessment, so a test that names
    nothing gets a cohort where G, A and M are the whole cohort and only R is a
    strict subset.
    """
    records: list[dict[str, Any]] = []
    for index in range(1, size + 1):
        if index in tuple(out_of_target):
            segments = (segment("RESOLVED", country_code=OTHER_COUNTRY),)
        elif index in tuple(unplaced):
            segments = (segment("UNKNOWN"),)
        else:
            segments = (segment("RESOLVED"),)
        if index in tuple(unclassified):
            block = None
        elif index in tuple(out_of_scope):
            block = qualification(value="OUT_OF_SCOPE")
        else:
            block = qualification()
        records.append(
            record(
                index,
                geography_segments=segments,
                qualification=block,
                sources=(source("alpha_board"),),
                matching=None if index in tuple(unmatched) else matching(),
                recommendation=(
                    recommendation(index) if index <= ranked else None
                ),
                canonical_title=f"Invented posting {index}",
                organization=f"Invented Org {index}",
                published_at="2026-02-20T09:00:00+00:00",
            )
        )
    return tuple(records)


# --------------------------------------------------------------------------
# the Phase 10.1 snapshot
# --------------------------------------------------------------------------


def dataset_of(
    records: Sequence[Mapping[str, Any]],
    *,
    manifest: Mapping[str, Any] | None = None,
) -> FrozenEvaluationDataset:
    """A verified-shaped frozen dataset over `records`.

    The identity comes from `manifest_payload`, which digests the manifest
    through Phase 10.1's own fingerprint domain — so `dataset_id` and
    `content_fingerprint` here are the values the real integrity gate would
    compute, and Phase 10.4's `build_frozen_cohort_binding` verifies these
    fixtures exactly as it would verify a snapshot read off disk.

    The dataclass is constructed directly rather than through
    `read_frozen_dataset` because that function takes a directory, and these
    tests write no file. The gate that turns two files into a
    `FrozenEvaluationDataset` is Phase 10.2's and is exercised against real
    files in `tests/integration`.
    """
    payload = manifest_payload(tuple(records)) if manifest is None else manifest
    return FrozenEvaluationDataset(
        directory=Path("/invented/datasets") / payload["dataset_id"],
        schema_version=payload["schema_version"],
        dataset_id=payload["dataset_id"],
        content_fingerprint=payload["content_fingerprint"],
        record_count=len(records),
        profile_id=payload["profile_context"]["profile_id"],
        user_id=payload["profile_context"]["user_id"],
        profile_context_fingerprint=payload["profile_context"]["fingerprint"],
        ordering=payload["cohort"]["ordering"],
        records=tuple(records),
    )


# --------------------------------------------------------------------------
# the Phase 10.2 evidence and the Phase 10.3 run
# --------------------------------------------------------------------------


def label(
    opportunity_id: int,
    grade: int,
    *,
    dataset: FrozenEvaluationDataset,
    revision: int = 1,
) -> HumanRelevanceLabel:
    """One human judgement, bound to this snapshot and this profile state."""
    return HumanRelevanceLabel(
        label_schema_version=HUMAN_LABEL_SCHEMA_VERSION,
        protocol_version=HUMAN_LABEL_PROTOCOL_VERSION,
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
        relabel_reason=None,
    )


def whole_lot(dataset: FrozenEvaluationDataset) -> CalibrationSelection:
    """A calibration lot covering the whole cohort, drawn by Phase 10.2.

    Most tests here want to decide which postings are *judged* rather than which
    are *selected*, so they select everything and judge a subset — which is also
    the state a real round is in for most of its life.
    """
    return select_calibration_sample(dataset, sample_size=len(dataset.records))


def coverage_of(
    grades: Mapping[int, int],
    dataset: FrozenEvaluationDataset,
    *,
    selection: CalibrationSelection | None = None,
) -> LabelCoverage:
    """A label coverage holding exactly `grades`, through the real builder."""
    return build_label_coverage(
        dataset,
        [
            label(opportunity_id, grade, dataset=dataset)
            for opportunity_id, grade in sorted(grades.items())
        ],
        selection=whole_lot(dataset) if selection is None else selection,
    )


def fully_judged(
    dataset: FrozenEvaluationDataset, *, relevant_upto: int = 4
) -> LabelCoverage:
    """Every posting judged, the first `relevant_upto` of them relevant.

    A fully judged universe is what Recall@K and NDCG@K need, so this is the
    coverage a test uses when it wants four `COMPUTED` metric entries rather
    than Phase 10.3's legitimate refusals.
    """
    return coverage_of(
        {
            int(item["opportunity_id"]): (
                3 if int(item["opportunity_id"]) <= relevant_upto else 1
            )
            for item in dataset.records
        },
        dataset,
    )


def evaluation_run_over(
    dataset: FrozenEvaluationDataset,
    coverage: LabelCoverage,
    *,
    universe_ids: Sequence[int] | None = None,
    evidence_class: EvidenceClass = EvidenceClass.DIAGNOSTIC_CALIBRATION,
):
    """A Phase 10.3 run over the whole cohort and the frozen recommendation.

    `universe_ids=None` is the whole frozen cohort, which is the only universe
    Phase 10.5 evaluates against; a test that wants to prove the refusal passes
    a subset and gets a `FIXED_BENCHMARK_POOL`.
    """
    return build_evaluation_run(
        dataset=dataset,
        universe=build_evaluation_universe(dataset, universe_ids),
        ranking=ranking_from_frozen_dataset(dataset),
        coverage=coverage,
        evidence_class=evidence_class,
    )


# --------------------------------------------------------------------------
# the Phase 10.4 run and the Phase 10.5 context
# --------------------------------------------------------------------------


def business_run(
    records: Sequence[Mapping[str, Any]],
    *,
    manifest: Mapping[str, Any] | None = None,
    target_country: str | None = TARGET_COUNTRY,
    full: bool = False,
):
    """A complete Phase 10.4 run over `records`, computed and verified.

    `target_country=None` assembles a run whose profile target binding states an
    UNKNOWN target, which is Phase 10.4's coherent evidence for
    `N_A / TARGET_COUNTRY_UNKNOWN` — and therefore Phase 10.5's
    `N_A / TARGET_COUNTRY_UNAVAILABLE`. Passing `target_country=False` is not a
    thing: to assemble a run with *no* binding at all, call with
    `full=False, target_country=...` and override in the test.
    """
    payload = manifest_payload(tuple(records)) if manifest is None else manifest
    binding = build_frozen_cohort_binding(records, payload)
    build = full_run_context if full else run_context
    kwargs: dict[str, Any] = {}
    if target_country is not None or not full:
        kwargs["profile_target_binding"] = target_binding(
            country_code=target_country,
            profile_id=binding.profile_id,
            profile_fingerprint=binding.profile_fingerprint,
        )
    context = build(binding, **kwargs)
    return build_business_metric_run(
        build_business_metric_computation_context(records, payload, context)
    )


def experiment_context(
    records: Sequence[Mapping[str, Any]] | None = None,
    *,
    coverage: LabelCoverage | None = None,
    with_ranking: bool = True,
    target_country: str | None = TARGET_COUNTRY,
    **record_kwargs: Any,
) -> ExperimentRunContext:
    """A verified Phase 10.5 context over a coherent four-layer stack.

    The one assembly every Phase 10.5 test needs, built through the real
    builders at every layer so that a fixture cannot be more permissive than
    production.

    `with_ranking=False` suppresses the Phase 10.3 inputs, which is only legal
    for a snapshot that froze no recommendation — the contract's `R empty =>
    inputs MUST be None` — so the tests that use it pass records with
    `ranked=0`.
    """
    resolved = ranked_records(**record_kwargs) if records is None else tuple(records)
    payload = manifest_payload(resolved)
    dataset = dataset_of(resolved, manifest=payload)
    inputs = None
    if with_ranking:
        labels = fully_judged(dataset) if coverage is None else coverage
        inputs = RankingEvaluationInputs(
            evaluation_run=evaluation_run_over(dataset, labels),
            label_coverage=labels,
        )
    return build_experiment_run_context(
        frozen_dataset=dataset,
        manifest=payload,
        business_metric_run=business_run(
            resolved, manifest=payload, target_country=target_country
        ),
        ranking_evaluation_inputs=inputs,
    )
