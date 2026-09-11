"""Assembling an offline metric run, and refusing one that does not add up.

This module is where the contract in `schema.py` is *established*. Every
invariant those frozen dataclasses claim is created here and nowhere else, so a
caller holding one of them is holding an artefact that has already passed the
gate — the same discipline Phase 10.2 uses for `FrozenEvaluationDataset`.

What is built, in the order a run needs it:

    the evaluation universe   from the frozen dataset, whole or as a fixed pool
    the ranking               projected from the frozen dataset, or declared
    the label coverage        from Phase 10.2's own validated label history
    the run contract          all of the above, bound and digested

**Fail closed, never repair.** A universe naming a posting the dataset does not
hold, a ranking whose positions repeat, a labelset made for another profile, a
self-declared fingerprint that does not survive recomputation, an evidence class
the labels cannot support, a metric contract version this build does not
implement — each is refused where it is found. Nothing is dropped, renumbered,
deduplicated or coerced: a measurement assembled from artefacts that disagree
would be a number about nothing in particular, and it would look exactly like a
number about something.

**One exception hierarchy, and Phase 10.2's is part of it.** Everything raised
here is an `EvaluationMetricsError`, except where a reused Phase 10.2 check
refuses first — a label bound to another dataset, a broken revision chain — and
raises its own `HumanLabelError`. That is deliberate: both descend from the
Phase 10.1 `EvaluationDatasetError`, so one `except` still catches the layer,
and re-wrapping Phase 10.2's message would replace a precise diagnosis with a
vaguer one.

**Nothing here opens the operational database.** A run is assembled from a
frozen directory and a label file. The production ranking is read only as the
`recommendation` block Phase 10.1 already froze on each record — a deterministic
projection of a snapshot, never a live query, and never a re-run of the engine
being measured.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from evaluation.labeling import (
    FrozenEvaluationDataset,
    HumanRelevanceLabel,
    require_supported_protocol_version,
    resolve_effective_labels,
    validate_label_history,
    validate_relevance_grade,
)

from .fingerprint import (
    evaluation_ranking_fingerprint,
    evaluation_run_fingerprint,
    evaluation_universe_fingerprint,
    verify_evaluation_ranking_fingerprint,
    verify_evaluation_run_fingerprint,
    verify_evaluation_universe_fingerprint,
)
from .schema import (
    EVALUATION_RANKING_VERSION,
    EVALUATION_RUN_SCHEMA_VERSION,
    EVALUATION_UNIVERSE_VERSION,
    METRIC_CONTRACT_VERSION,
    EvaluationBindingError,
    EvaluationRanking,
    EvaluationRankingEntry,
    EvaluationRunContract,
    EvaluationRunProvenance,
    EvaluationUniverse,
    EvaluationUniverseKind,
    EvidenceClass,
    LabelCoverage,
    MetricContractError,
    RankingSource,
    canonical_opportunity_ids,
    require_evidence_class,
    require_supported_metric_contract_version,
    validate_fingerprint,
    validate_opportunity_id,
    validate_rank_position,
)

__all__ = [
    "assert_ranking_within_universe",
    "build_evaluation_ranking",
    "build_evaluation_run",
    "build_evaluation_universe",
    "build_label_coverage",
    "ranking_from_frozen_dataset",
    "verify_evaluation_run",
]


# --------------------------------------------------------------------------
# the evaluation universe
# --------------------------------------------------------------------------


def _duplicates(values: Sequence[int]) -> list[int]:
    """The repeated values, in one pass and in ascending order."""
    seen: set[int] = set()
    repeated: set[int] = set()
    for value in values:
        if value in seen:
            repeated.add(value)
        seen.add(value)
    return sorted(repeated)


def build_evaluation_universe(
    dataset: FrozenEvaluationDataset,
    opportunity_ids: Sequence[int] | None = None,
    *,
    kind: EvaluationUniverseKind | None = None,
    declared_size: int | None = None,
) -> EvaluationUniverse:
    """The closed set a ranking will be evaluated against, or a refusal.

    `opportunity_ids is None` means the whole frozen cohort, which is the
    common case and the only one that needs no argument: a universe is a
    deliberate declaration, and declaring "everything in this snapshot" should
    be the easy thing to say.

    A subset is a `FIXED_BENCHMARK_POOL` — the shape a future benchmark takes.
    It is representable today precisely so that arriving at one changes no
    metric mathematics: the gates below read a universe, not a cohort.

    `declared_size` exists so that a size stated elsewhere — in a stored
    artefact, in a report — can be checked against the members rather than
    believed. A mismatch is refused; it is never the members that get adjusted.

    Refused, each with its own message: a non-integer or non-positive id, a
    duplicate, an id the frozen dataset does not hold, an empty pool, and a
    declared size that disagrees with the membership.
    """
    if opportunity_ids is None:
        members = dataset.opportunity_ids
        resolved_kind = (
            EvaluationUniverseKind.FROZEN_DATASET_COHORT if kind is None else kind
        )
    else:
        members = tuple(opportunity_ids)
        resolved_kind = (
            EvaluationUniverseKind.FIXED_BENCHMARK_POOL if kind is None else kind
        )

    if not isinstance(resolved_kind, EvaluationUniverseKind):
        raise EvaluationBindingError(
            f"{resolved_kind!r} is not an evaluation universe kind"
        )

    validated = [
        validate_opportunity_id(value, subject="an evaluation universe member")
        for value in members
    ]
    if not validated:
        raise EvaluationBindingError(
            "an evaluation universe holds at least one opportunity; an empty "
            "universe cannot be the denominator of anything"
        )
    duplicates = _duplicates(validated)
    if duplicates:
        raise EvaluationBindingError(
            f"the evaluation universe repeats opportunity ids: {duplicates}; "
            "a duplicated member would be counted twice in every denominator"
        )
    held = set(dataset.opportunity_ids)
    missing = sorted(value for value in validated if value not in held)
    if missing:
        raise EvaluationBindingError(
            f"the evaluation universe names opportunities absent from dataset "
            f"{dataset.dataset_id}: {missing}"
        )

    ordered = canonical_opportunity_ids(validated)
    size = len(ordered)
    if declared_size is not None and declared_size != size:
        raise EvaluationBindingError(
            f"the evaluation universe declares size {declared_size!r} and holds "
            f"{size} opportunities"
        )
    if (
        resolved_kind is EvaluationUniverseKind.FROZEN_DATASET_COHORT
        and ordered != canonical_opportunity_ids(dataset.opportunity_ids)
    ):
        raise EvaluationBindingError(
            f"a {resolved_kind} universe is the whole frozen cohort of "
            f"{dataset.dataset_id} ({dataset.record_count} opportunities), not "
            f"a {size}-opportunity subset of it"
        )

    draft = EvaluationUniverse(
        universe_version=EVALUATION_UNIVERSE_VERSION,
        kind=resolved_kind,
        dataset_id=dataset.dataset_id,
        dataset_content_fingerprint=dataset.content_fingerprint,
        opportunity_ids=ordered,
        size=size,
        # Computed from the draft one line below. The empty string is never
        # observable: the draft does not leave this function.
        fingerprint="",
    )
    return replace(draft, fingerprint=evaluation_universe_fingerprint(draft))


# --------------------------------------------------------------------------
# the ranking
# --------------------------------------------------------------------------


def _ranking_with_fingerprint(
    *,
    source: RankingSource,
    dataset: FrozenEvaluationDataset,
    entries: tuple[EvaluationRankingEntry, ...],
) -> EvaluationRanking:
    draft = EvaluationRanking(
        ranking_version=EVALUATION_RANKING_VERSION,
        source=source,
        dataset_id=dataset.dataset_id,
        dataset_content_fingerprint=dataset.content_fingerprint,
        profile_id=dataset.profile_id,
        profile_context_fingerprint=dataset.profile_context_fingerprint,
        entries=entries,
        fingerprint="",
    )
    return replace(draft, fingerprint=evaluation_ranking_fingerprint(draft))


def _validated_entries(
    entries: Sequence[EvaluationRankingEntry], dataset: FrozenEvaluationDataset
) -> tuple[EvaluationRankingEntry, ...]:
    """The ranking invariants, established once.

    Contiguity from 1 is the chosen contract rather than an accident of the
    data: "the top K" has to mean the same thing in every run, and it does not
    when positions can have holes in them. A ranking whose upstream positions
    *do* have holes is still representable — `ranking_from_frozen_dataset`
    numbers the evaluation positions and keeps the upstream ones on each entry
    — but a caller stating positions directly states contiguous ones.
    """
    if not entries:
        raise EvaluationBindingError(
            "an evaluation ranking holds at least one ranked opportunity; "
            "there is no top K of an empty list"
        )
    positions: list[int] = []
    ids: list[int] = []
    for entry in entries:
        positions.append(
            validate_rank_position(
                entry.rank_position, subject="an evaluation ranking position"
            )
        )
        ids.append(
            validate_opportunity_id(
                entry.opportunity_id, subject="a ranked opportunity"
            )
        )
        if entry.source_rank_position is not None:
            validate_rank_position(
                entry.source_rank_position,
                subject="an upstream rank position",
            )
    duplicate_positions = _duplicates(positions)
    if duplicate_positions:
        raise EvaluationBindingError(
            f"the ranking repeats rank positions: {duplicate_positions}; two "
            "postings cannot both be nth"
        )
    duplicate_ids = _duplicates(ids)
    if duplicate_ids:
        raise EvaluationBindingError(
            f"the ranking repeats opportunity ids: {duplicate_ids}; one posting "
            "occupies one position"
        )
    if positions != list(range(1, len(positions) + 1)):
        raise EvaluationBindingError(
            f"the ranking states positions {positions}, not the canonical "
            f"contiguous {list(range(1, len(positions) + 1))} in order"
        )
    held = set(dataset.opportunity_ids)
    missing = sorted(value for value in ids if value not in held)
    if missing:
        raise EvaluationBindingError(
            f"the ranking names opportunities absent from dataset "
            f"{dataset.dataset_id}: {missing}"
        )
    return tuple(entries)


def build_evaluation_ranking(
    dataset: FrozenEvaluationDataset,
    entries: Sequence[EvaluationRankingEntry],
    *,
    source: RankingSource = RankingSource.DECLARED_OFFLINE_RANKING,
) -> EvaluationRanking:
    """A ranking stated directly in evaluation positions, or a refusal."""
    if not isinstance(source, RankingSource):
        raise EvaluationBindingError(f"{source!r} is not a ranking source")
    return _ranking_with_fingerprint(
        source=source,
        dataset=dataset,
        entries=_validated_entries(tuple(entries), dataset),
    )


def _frozen_rank_position(record: Mapping[str, Any]) -> int | None:
    """This record's production rank, read out of the frozen block or `None`.

    `recommendation is None` is Phase 10.1's own statement that the
    Recommendation run never covered this posting. It is not rank 0, not last,
    and not an error: it is the candidate false negative the snapshot exists to
    preserve, and it simply is not in the ranking.
    """
    recommendation = record.get("recommendation")
    if recommendation is None:
        return None
    if not isinstance(recommendation, Mapping):
        raise EvaluationBindingError(
            f"opportunity {record.get('opportunity_id')!r} carries a "
            "recommendation block that is not an object"
        )
    return validate_rank_position(
        recommendation.get("rank_position"),
        subject=(
            f"the frozen rank of opportunity {record.get('opportunity_id')!r}"
        ),
    )


def ranking_from_frozen_dataset(
    dataset: FrozenEvaluationDataset,
) -> EvaluationRanking:
    """The production ranking as the snapshot froze it — a projection, not a run.

    Reads the `recommendation.rank_position` Phase 10.1 already persisted on
    each record, orders by it, and numbers the result `1..N`. The upstream
    position is kept on every entry, because the two can differ: a frozen cohort
    excludes postings that later went inactive or were merged away, so the
    production positions surviving into a snapshot can have holes. Numbering the
    evaluation positions is a declared projection; discarding the originals
    would be a silent repair, and this function does not do that.

    No production module is imported, no engine is re-run and no database is
    opened. Recommendation is not redesigned, consulted or told that any of this
    exists.
    """
    ranked: list[tuple[int, int]] = []
    for record in dataset.records:
        position = _frozen_rank_position(record)
        if position is None:
            continue
        opportunity_id = validate_opportunity_id(
            record.get("opportunity_id"), subject="a ranked opportunity"
        )
        ranked.append((position, opportunity_id))
    if not ranked:
        raise EvaluationBindingError(
            f"dataset {dataset.dataset_id} froze no recommendation position; "
            "there is no ranking in it to evaluate"
        )
    source_positions = [position for position, _ in ranked]
    duplicates = _duplicates(source_positions)
    if duplicates:
        raise EvaluationBindingError(
            f"dataset {dataset.dataset_id} froze duplicate recommendation rank "
            f"positions {duplicates}; Phase 9A makes them unique within a run, "
            "so this snapshot mixes runs or was edited"
        )
    ranked.sort()
    entries = tuple(
        EvaluationRankingEntry(
            rank_position=index,
            opportunity_id=opportunity_id,
            source_rank_position=position,
        )
        for index, (position, opportunity_id) in enumerate(ranked, start=1)
    )
    return _ranking_with_fingerprint(
        source=RankingSource.FROZEN_DATASET_RECOMMENDATION,
        dataset=dataset,
        entries=_validated_entries(entries, dataset),
    )


def assert_ranking_within_universe(
    ranking: EvaluationRanking, universe: EvaluationUniverse
) -> None:
    """Refuse a ranking that leaves the universe it is being measured in.

    A ranked posting outside the universe has no denominator to belong to: it
    can be counted in a precision numerator and can never be counted in a
    recall denominator, so every metric over the pair would be internally
    inconsistent while remaining arithmetically fine.
    """
    members = set(universe.opportunity_ids)
    outside = sorted(
        opportunity_id
        for opportunity_id in ranking.opportunity_ids
        if opportunity_id not in members
    )
    if outside:
        raise EvaluationBindingError(
            f"the ranking names opportunities outside the evaluation universe: "
            f"{outside}; a ranked item that is not in the universe cannot be "
            "measured against it"
        )


# --------------------------------------------------------------------------
# the label coverage view
# --------------------------------------------------------------------------


def build_label_coverage(
    dataset: FrozenEvaluationDataset,
    history: Sequence[HumanRelevanceLabel],
    *,
    labelset_fingerprint: str,
) -> LabelCoverage:
    """Which opportunities carry an effective judgement, read through Phase 10.2.

    The revision chain is **not** reinterpreted here. `validate_label_history`
    refuses a history that is not entirely about this dataset or whose revisions
    do not run `1, 2, 3, ...`, and `resolve_effective_labels` decides which row
    is current. Both are Phase 10.2's, and a second opinion about what "the
    latest valid judgement" means is precisely the kind of divergence that ends
    with two slices disagreeing about a number neither can explain.

    Nothing is written. No label is created, converted, migrated or graded, and
    an opportunity with no row simply does not appear in the result.
    """
    validate_fingerprint(labelset_fingerprint, subject="the labelset fingerprint")
    validate_label_history(history, dataset)
    effective = resolve_effective_labels(history)

    protocols = {label.protocol_version for label in effective.values()}
    schemas = {label.label_schema_version for label in effective.values()}
    if not protocols:
        # An empty labelset states no rubric, so binding a run to one would
        # mean inventing the protocol its judgements were made under — and a
        # run bound to an invented protocol is a run whose evidence class means
        # nothing. A round with nothing judged yet is a real state; it is
        # simply not a state a metric run can be assembled from.
        raise EvaluationBindingError(
            "the labelset holds no judgement, so it states no label protocol; "
            "a metric run cannot be bound to evidence that does not exist"
        )
    if len(protocols) > 1:
        raise EvaluationBindingError(
            f"the labelset mixes label protocols {sorted(protocols)}; "
            "judgements made under two rubrics are answers to two different "
            "questions and are never one labelset"
        )
    if len(schemas) > 1:
        raise EvaluationBindingError(
            f"the labelset mixes label schema versions {sorted(schemas)}"
        )
    protocol_version = require_supported_protocol_version(
        next(iter(protocols)), subject="the labelset"
    )
    schema_version = next(iter(schemas))

    grades = {
        opportunity_id: validate_relevance_grade(label.relevance_grade)
        for opportunity_id, label in sorted(effective.items())
    }
    return LabelCoverage(
        label_schema_version=schema_version,
        label_protocol_version=protocol_version,
        dataset_id=dataset.dataset_id,
        dataset_content_fingerprint=dataset.content_fingerprint,
        profile_id=dataset.profile_id,
        profile_context_fingerprint=dataset.profile_context_fingerprint,
        labelset_fingerprint=labelset_fingerprint,
        grades=grades,
    )


# --------------------------------------------------------------------------
# the run contract
# --------------------------------------------------------------------------


def build_evaluation_run(
    *,
    dataset: FrozenEvaluationDataset,
    universe: EvaluationUniverse,
    ranking: EvaluationRanking,
    evidence_class: EvidenceClass | str,
    label_protocol_version: str,
    labelset_fingerprint: str,
    metric_contract_version: str = METRIC_CONTRACT_VERSION,
    provenance: EvaluationRunProvenance | None = None,
) -> EvaluationRunContract:
    """Bind one future metrics run, or refuse to.

    Every check is here rather than spread over the callers, and each one
    corresponds to a way a measurement can be quietly wrong:

    * the **metric contract version** — rules this build does not implement;
    * the **evidence class** — a claim the labels behind it cannot support;
    * the **label protocol** — a rubric this build cannot interpret;
    * the **nested fingerprints** — recomputed, never believed;
    * the **dataset binding** of the universe and of the ranking — a universe or
      an ordering built over another snapshot;
    * the **profile binding** of the ranking — a personalised ordering judged
      against another profile state;
    * **ranking ⊆ universe** — a ranked item with no denominator to belong to.

    The run's own fingerprint is computed last, from the canonical semantic
    projection, so it can only ever describe artefacts that already agree.
    """
    contract_version = require_supported_metric_contract_version(
        metric_contract_version
    )
    validate_fingerprint(labelset_fingerprint, subject="the labelset fingerprint")
    protocol_version = require_supported_protocol_version(
        label_protocol_version, subject="the metric run"
    )
    resolved_class = require_evidence_class(
        evidence_class,
        label_protocol_version=protocol_version,
        labelset_fingerprint=labelset_fingerprint,
    )

    verify_evaluation_universe_fingerprint(universe)
    verify_evaluation_ranking_fingerprint(ranking)

    if universe.dataset_id != dataset.dataset_id:
        raise EvaluationBindingError(
            f"the evaluation universe was built for dataset "
            f"{universe.dataset_id}, not {dataset.dataset_id}"
        )
    if universe.dataset_content_fingerprint != dataset.content_fingerprint:
        raise EvaluationBindingError(
            "the evaluation universe was built against content fingerprint "
            f"{universe.dataset_content_fingerprint}, not "
            f"{dataset.content_fingerprint}"
        )
    if ranking.dataset_id != dataset.dataset_id:
        raise EvaluationBindingError(
            f"the ranking was frozen in dataset {ranking.dataset_id}, not "
            f"{dataset.dataset_id}"
        )
    if ranking.dataset_content_fingerprint != dataset.content_fingerprint:
        raise EvaluationBindingError(
            "the ranking was frozen against content fingerprint "
            f"{ranking.dataset_content_fingerprint}, not "
            f"{dataset.content_fingerprint}"
        )
    if ranking.profile_id != dataset.profile_id:
        raise EvaluationBindingError(
            f"the ranking was produced for profile {ranking.profile_id}, not "
            f"{dataset.profile_id}"
        )
    if ranking.profile_context_fingerprint != dataset.profile_context_fingerprint:
        raise EvaluationBindingError(
            "the ranking was produced against profile context "
            f"{ranking.profile_context_fingerprint}, not "
            f"{dataset.profile_context_fingerprint}"
        )
    assert_ranking_within_universe(ranking, universe)

    draft = EvaluationRunContract(
        run_schema_version=EVALUATION_RUN_SCHEMA_VERSION,
        metric_contract_version=contract_version,
        evidence_class=resolved_class,
        dataset_id=dataset.dataset_id,
        dataset_content_fingerprint=dataset.content_fingerprint,
        profile_id=dataset.profile_id,
        profile_context_fingerprint=dataset.profile_context_fingerprint,
        universe=universe,
        ranking=ranking,
        label_protocol_version=protocol_version,
        labelset_fingerprint=labelset_fingerprint,
        run_fingerprint="",
        provenance=provenance or EvaluationRunProvenance(),
    )
    return replace(draft, run_fingerprint=evaluation_run_fingerprint(draft))


def verify_evaluation_run(run: EvaluationRunContract) -> str:
    """Re-establish every binding of a run from the run alone, or refuse it.

    For a run that arrived from somewhere else — a stored artefact, another
    process, a caller that built the dataclass by hand. It repeats the checks
    `build_evaluation_run` made, without the frozen dataset: the contract
    versions, the evidence class, the nested digests recomputed, the internal
    agreement between the run and its nested artefacts, `ranking ⊆ universe`,
    and finally the run's own digest.
    """
    require_supported_metric_contract_version(run.metric_contract_version)
    if run.run_schema_version != EVALUATION_RUN_SCHEMA_VERSION:
        raise MetricContractError(
            f"unsupported evaluation run schema version: "
            f"{run.run_schema_version!r} (this build reads "
            f"{EVALUATION_RUN_SCHEMA_VERSION!r})"
        )
    validate_fingerprint(
        run.labelset_fingerprint, subject="the labelset fingerprint"
    )
    protocol_version = require_supported_protocol_version(
        run.label_protocol_version, subject="the metric run"
    )
    require_evidence_class(
        run.evidence_class,
        label_protocol_version=protocol_version,
        labelset_fingerprint=run.labelset_fingerprint,
    )
    verify_evaluation_universe_fingerprint(run.universe)
    verify_evaluation_ranking_fingerprint(run.ranking)

    for name, stated, nested in (
        ("dataset_id", run.dataset_id, run.universe.dataset_id),
        (
            "dataset_content_fingerprint",
            run.dataset_content_fingerprint,
            run.universe.dataset_content_fingerprint,
        ),
        ("dataset_id", run.dataset_id, run.ranking.dataset_id),
        (
            "dataset_content_fingerprint",
            run.dataset_content_fingerprint,
            run.ranking.dataset_content_fingerprint,
        ),
        (
            "profile_context_fingerprint",
            run.profile_context_fingerprint,
            run.ranking.profile_context_fingerprint,
        ),
    ):
        if stated != nested:
            raise EvaluationBindingError(
                f"the run states {name} {stated!r} and one of its artefacts "
                f"states {nested!r}"
            )
    if run.profile_id != run.ranking.profile_id:
        raise EvaluationBindingError(
            f"the run states profile {run.profile_id} and its ranking states "
            f"{run.ranking.profile_id}"
        )
    assert_ranking_within_universe(run.ranking, run.universe)
    return verify_evaluation_run_fingerprint(run)
