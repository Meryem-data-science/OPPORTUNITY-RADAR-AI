"""Assembling an offline metric run, and refusing one that does not add up.

This module is where the contract in `schema.py` is *established*. Every
invariant those frozen dataclasses claim is created here and nowhere else, so a
caller holding one of them is holding an artefact that has already passed the
gate — the same discipline Phase 10.2 uses for `FrozenEvaluationDataset`.

What is built, in the order a run needs it:

    the evaluation universe   from the frozen dataset, whole or as a fixed pool
    the ranking               projected from the frozen dataset, or declared
    the label coverage        the answers of one verified calibration lot, with
                              Phase 10.2's labelset digest recomputed over them
    the run contract          all of the above, bound and digested

**Nothing declares its own identity.** Every fingerprint an artefact carries is
recomputed here before it is believed, and so is the *structure* it claims:
`schema.validate_evaluation_universe_structure` and
`schema.validate_evaluation_ranking_structure` are applied by the builders and
by the verifiers alike, so an artefact this package sealed and an artefact
somebody reconstructed are held to one contract. A digest recomputed over an
invalid structure is a valid digest, which is why the digest alone was never
enough. The same rule reaches the evidence: a `LabelCoverage` holds the labels
its digest covers, `verify_label_coverage` recomputes that digest from them, and
`build_evaluation_run` takes such a coverage rather than a protocol string and a
fingerprint that might describe nothing.

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
    SUPPORTED_SELECTOR_VERSIONS,
    CalibrationSelection,
    FrozenEvaluationDataset,
    HumanRelevanceLabel,
    assert_selection_bindings,
    canonical_label_order,
    labelset_fingerprint,
    require_supported_protocol_version,
    resolve_effective_labels,
    validate_label_history,
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
    validate_evaluation_ranking_structure,
    validate_evaluation_universe_structure,
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
    "verify_label_coverage",
]


# --------------------------------------------------------------------------
# the evaluation universe
# --------------------------------------------------------------------------


#: A syntactically valid digest used only to structurally validate a draft
#: whose real fingerprint has not been computed yet. It never leaves a builder:
#: the object returned always carries the digest of its own semantic payload.
_PLACEHOLDER_FINGERPRINT = "0" * 64


def _sealed_universe(draft: EvaluationUniverse) -> EvaluationUniverse:
    """Validate a drafted universe structurally, then seal it with its digest.

    The invariants are `schema.validate_evaluation_universe_structure`'s, not a
    second copy of them living in this builder: the verifier re-establishes
    exactly the same ones on any universe it is later handed, so an artefact
    this package built and an artefact somebody reconstructed are held to one
    contract.
    """
    validate_evaluation_universe_structure(
        replace(draft, fingerprint=_PLACEHOLDER_FINGERPRINT)
    )
    return replace(draft, fingerprint=evaluation_universe_fingerprint(draft))


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

    The generic invariants — non-empty, well-formed ids, no duplicate, canonical
    order, a size that equals the membership — belong to
    `validate_evaluation_universe_structure`, applied by `_sealed_universe`.
    What stays here is what needs the *dataset*: that every member is actually
    in the frozen snapshot, and that a universe calling itself the whole cohort
    is one.
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

    # Each id is validated before anything sorts them: a list holding a string
    # or a `None` cannot be ordered at all, and the error a caller deserves is
    # about the value they passed rather than about comparing `str` to `int`.
    validated = [
        validate_opportunity_id(value, subject="an evaluation universe member")
        for value in members
    ]
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

    return _sealed_universe(
        EvaluationUniverse(
            universe_version=EVALUATION_UNIVERSE_VERSION,
            kind=resolved_kind,
            dataset_id=dataset.dataset_id,
            dataset_content_fingerprint=dataset.content_fingerprint,
            opportunity_ids=ordered,
            size=size,
            fingerprint="",
        )
    )


# --------------------------------------------------------------------------
# the ranking
# --------------------------------------------------------------------------


def _sealed_ranking(
    *,
    source: RankingSource,
    dataset: FrozenEvaluationDataset,
    entries: tuple[EvaluationRankingEntry, ...],
) -> EvaluationRanking:
    """Validate a drafted ranking structurally and against the dataset, then seal it.

    The generic invariants — non-empty, unique well-formed ids, unique positive
    positions contiguous from 1 in stored order, a valid source and profile —
    belong to `schema.validate_evaluation_ranking_structure`, which the verifier
    applies to any ranking it is later handed. What is added here is the one
    check that needs the snapshot: every ranked posting is in it.

    Contiguity from 1 is the chosen contract rather than an accident of the
    data: "the top K" has to mean the same thing in every run, and it does not
    when positions can have holes in them. A ranking whose *upstream* positions
    do have holes is still representable — `ranking_from_frozen_dataset` numbers
    the evaluation positions and keeps the upstream ones on each entry — but a
    caller stating positions directly states contiguous ones.
    """
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
    validate_evaluation_ranking_structure(
        replace(draft, fingerprint=_PLACEHOLDER_FINGERPRINT)
    )
    held = set(dataset.opportunity_ids)
    missing = sorted(
        entry.opportunity_id
        for entry in entries
        if entry.opportunity_id not in held
    )
    if missing:
        raise EvaluationBindingError(
            f"the ranking names opportunities absent from dataset "
            f"{dataset.dataset_id}: {missing}"
        )
    return replace(draft, fingerprint=evaluation_ranking_fingerprint(draft))


def build_evaluation_ranking(
    dataset: FrozenEvaluationDataset,
    entries: Sequence[EvaluationRankingEntry],
    *,
    source: RankingSource = RankingSource.DECLARED_OFFLINE_RANKING,
) -> EvaluationRanking:
    """A ranking stated directly in evaluation positions, or a refusal."""
    return _sealed_ranking(
        source=source, dataset=dataset, entries=tuple(entries)
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
    seen: set[int] = set()
    repeated: set[int] = set()
    for position, _ in ranked:
        if position in seen:
            repeated.add(position)
        seen.add(position)
    if repeated:
        duplicates = sorted(repeated)
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
    return _sealed_ranking(
        source=RankingSource.FROZEN_DATASET_RECOMMENDATION,
        dataset=dataset,
        entries=entries,
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
    selection: CalibrationSelection,
    expected_labelset_fingerprint: str | None = None,
) -> LabelCoverage:
    """The judgements of one calibration lot, with their digest **recomputed**.

    The lot is not decoration. Phase 10.2's labelset fingerprint is a digest of
    the effective judgements *of one selection* — `build_labelset_report` leaves
    judgements recorded outside the lot out of the digest on purpose, reporting
    them separately — so a labelset is identified by a lot and its answers
    together. A coverage built from every effective label while carrying a
    digest computed over one lot would hand the gates grades that the run's
    labelset fingerprint does not cover, which is the Phase 10 rule inverted:
    the artefact's declared identity would be trusted instead of recomputed.

    So the identity is derived here rather than accepted, in Phase 10.2's own
    terms and through Phase 10.2's own primitives:

    1. `assert_selection_bindings` — the lot is verified against this frozen
       dataset, which includes **redrawing** it: a file can name any postings it
       likes, and only redrawing shows whether the declared selector would have
       named them;
    2. `validate_label_history` — the whole history is refused unless every row
       is about this dataset and every revision chain is intact;
    3. `resolve_effective_labels` — the current judgement of each opportunity,
       by Phase 10.2's definition and not a second one;
    4. the in-lot judgements are separated from the rest;
    5. `labelset_fingerprint` — recomputed over exactly those, with the schema
       and protocol the rows themselves state, the dataset and profile bindings
       of the frozen snapshot, and the lot's selector version and digest.

    `expected_labelset_fingerprint` is what a caller *believed* the labelset to
    be — from a stored labelset manifest, a report, a command line. It is
    compared against the recomputed value and never substituted for it: a
    mismatch fails closed.

    Nothing is written and nothing is discarded. Judgements outside the lot stay
    in Phase 10.2's history exactly as they are; they are named in
    `judged_outside_selection` and they do not enter this labelset.
    """
    assert_selection_bindings(selection, dataset)
    validate_label_history(history, dataset)
    effective = resolve_effective_labels(history)

    selected = selection.opportunity_ids
    selected_set = set(selected)
    # In the lot's own order, exactly as `build_labelset_report` walks it. The
    # digest canonicalises by opportunity id, so the order cannot move it; the
    # agreement is kept anyway, because two functions computing one digest
    # should not differ even in ways that happen not to matter.
    in_lot = tuple(
        effective[opportunity_id]
        for opportunity_id in selected
        if opportunity_id in effective
    )
    outside = tuple(
        sorted(
            opportunity_id
            for opportunity_id in effective
            if opportunity_id not in selected_set
        )
    )
    if not in_lot:
        # An empty labelset states no rubric, so binding a run to one would mean
        # inventing the protocol its judgements were made under — and a run
        # bound to an invented protocol is a run whose evidence class means
        # nothing. A lot with nothing judged yet is a real state; it is simply
        # not a state a metric run can be assembled from.
        raise EvaluationBindingError(
            f"no judgement in this history belongs to lot "
            f"{selection.selection_fingerprint}, so the labelset holds nothing "
            "and states no label protocol; a metric run cannot be bound to "
            f"evidence that does not exist ({len(outside)} judgement(s) were "
            "recorded outside the lot and are not part of it)"
        )

    protocols = {item.protocol_version for item in in_lot}
    schemas = {item.label_schema_version for item in in_lot}
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

    recomputed = labelset_fingerprint(
        label_schema_version=schema_version,
        protocol_version=protocol_version,
        dataset_id=dataset.dataset_id,
        dataset_content_fingerprint=dataset.content_fingerprint,
        profile_id=dataset.profile_id,
        profile_context_fingerprint=dataset.profile_context_fingerprint,
        selector_version=selection.selector_version,
        selection_fingerprint=selection.selection_fingerprint,
        labels=in_lot,
    )
    if expected_labelset_fingerprint is not None:
        validate_fingerprint(
            expected_labelset_fingerprint,
            subject="the expected labelset fingerprint",
        )
        if expected_labelset_fingerprint != recomputed:
            raise EvaluationBindingError(
                f"the labelset was expected to be "
                f"{expected_labelset_fingerprint} but the judgements of lot "
                f"{selection.selection_fingerprint} digest to {recomputed}; "
                "refusing to measure against a labelset that is not what it "
                "says it is"
            )

    return LabelCoverage(
        label_schema_version=schema_version,
        label_protocol_version=protocol_version,
        dataset_id=dataset.dataset_id,
        dataset_content_fingerprint=dataset.content_fingerprint,
        profile_id=dataset.profile_id,
        profile_context_fingerprint=dataset.profile_context_fingerprint,
        selector_version=selection.selector_version,
        selection_fingerprint=selection.selection_fingerprint,
        labels=canonical_label_order(in_lot),
        judged_outside_selection=outside,
        labelset_fingerprint=recomputed,
    )


def verify_label_coverage(coverage: LabelCoverage) -> str:
    """Recompute a coverage's labelset digest from its own labels, or refuse it.

    The same principle the universe and the ranking are held to, applied to the
    evidence: a `LabelCoverage` that somebody reconstructed — from a stored
    artefact, by hand, by editing one grade — carries a fingerprint field like
    any other, and a fingerprint field that is only ever read is not a check.

    Called by the run builder and by every availability gate, so no path reads a
    grade out of a coverage whose declared identity has not been re-established
    from the judgements it actually holds.
    """
    if not isinstance(coverage, LabelCoverage):
        raise EvaluationBindingError(f"{coverage!r} is not a label coverage")
    require_supported_protocol_version(
        coverage.label_protocol_version, subject="the labelset"
    )
    validate_fingerprint(
        coverage.labelset_fingerprint, subject="the labelset fingerprint"
    )
    validate_fingerprint(
        coverage.selection_fingerprint, subject="the selection fingerprint"
    )
    if coverage.selector_version not in SUPPORTED_SELECTOR_VERSIONS:
        raise MetricContractError(
            f"the labelset was drawn by selector "
            f"{coverage.selector_version!r}; this build reads "
            f"{list(SUPPORTED_SELECTOR_VERSIONS)}"
        )
    if not coverage.labels:
        raise EvaluationBindingError(
            "the labelset holds no judgement; a metric run cannot be bound to "
            "evidence that does not exist"
        )
    overlap = sorted(
        set(coverage.judged_outside_selection) & set(coverage.grades)
    )
    if overlap:
        raise EvaluationBindingError(
            f"opportunities {overlap} are reported both inside and outside the "
            "lot; a judgement is in one labelset or in none"
        )
    recomputed = labelset_fingerprint(
        label_schema_version=coverage.label_schema_version,
        protocol_version=coverage.label_protocol_version,
        dataset_id=coverage.dataset_id,
        dataset_content_fingerprint=coverage.dataset_content_fingerprint,
        profile_id=coverage.profile_id,
        profile_context_fingerprint=coverage.profile_context_fingerprint,
        selector_version=coverage.selector_version,
        selection_fingerprint=coverage.selection_fingerprint,
        labels=coverage.labels,
    )
    if recomputed != coverage.labelset_fingerprint:
        raise EvaluationBindingError(
            f"the label coverage claims labelset {coverage.labelset_fingerprint} "
            f"but the judgements it holds digest to {recomputed}; refusing a "
            "labelset that is not what it says it is"
        )
    return recomputed


# --------------------------------------------------------------------------
# the run contract
# --------------------------------------------------------------------------


def build_evaluation_run(
    *,
    dataset: FrozenEvaluationDataset,
    universe: EvaluationUniverse,
    ranking: EvaluationRanking,
    coverage: LabelCoverage,
    evidence_class: EvidenceClass | str,
    metric_contract_version: str = METRIC_CONTRACT_VERSION,
    provenance: EvaluationRunProvenance | None = None,
) -> EvaluationRunContract:
    """Bind one future metrics run, or refuse to.

    Every check is here rather than spread over the callers, and each one
    corresponds to a way a measurement can be quietly wrong:

    * the **metric contract version** — rules this build does not implement;
    * the **labelset** — recomputed from the judgements the coverage holds, so
      the run's `labelset_fingerprint` always names evidence that exists. The
      lot the labelset was drawn from is bound transitively: Phase 10.2's
      labelset digest already covers the selector version and the selection
      fingerprint, so naming the labelset names the question it answers;
    * the **evidence class** — a claim the labels behind it cannot support,
      decided about that verified labelset identity;
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
    # The labelset identity is *derived from verified evidence*, never accepted
    # as an argument. There is deliberately no parameter here that would let a
    # caller bind a run to a syntactically valid digest with no judgements
    # behind it: `verify_label_coverage` recomputes the digest from the labels
    # the coverage holds, and the protocol and fingerprint below are read off
    # the object that survived it.
    resolved_labelset_fingerprint = verify_label_coverage(coverage)
    protocol_version = require_supported_protocol_version(
        coverage.label_protocol_version, subject="the metric run"
    )
    # ...and the evidence class is therefore decided about a labelset that is
    # known to exist and known to be the one named.
    resolved_class = require_evidence_class(
        evidence_class,
        label_protocol_version=protocol_version,
        labelset_fingerprint=resolved_labelset_fingerprint,
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
    if coverage.dataset_id != dataset.dataset_id:
        raise EvaluationBindingError(
            f"the labels were made against dataset {coverage.dataset_id}, not "
            f"{dataset.dataset_id}"
        )
    if coverage.dataset_content_fingerprint != dataset.content_fingerprint:
        raise EvaluationBindingError(
            "the labels were made against content fingerprint "
            f"{coverage.dataset_content_fingerprint}, not "
            f"{dataset.content_fingerprint}"
        )
    if coverage.profile_id != dataset.profile_id:
        raise EvaluationBindingError(
            f"the labels were made for profile {coverage.profile_id}, not "
            f"{dataset.profile_id}"
        )
    if coverage.profile_context_fingerprint != dataset.profile_context_fingerprint:
        raise EvaluationBindingError(
            "the labels were made against profile context "
            f"{coverage.profile_context_fingerprint}, not "
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
        labelset_fingerprint=resolved_labelset_fingerprint,
        run_fingerprint="",
        provenance=provenance or EvaluationRunProvenance(),
    )
    return replace(draft, run_fingerprint=evaluation_run_fingerprint(draft))


def verify_evaluation_run(run: EvaluationRunContract) -> str:
    """Re-establish every binding of a run from the run alone, or refuse it.

    For a run that arrived from somewhere else — a stored artefact, another
    process, a caller that built the dataclass by hand. It repeats the checks
    `build_evaluation_run` made, without the frozen dataset: the contract
    versions, the evidence class, the nested artefacts **re-established
    structurally and re-digested**, the internal agreement between the run and
    those artefacts, `ranking ⊆ universe`, and finally the run's own digest.

    "Re-established structurally" is the part that a digest check alone does not
    give. Recomputing a fingerprint over an artefact only proves it has not
    moved since somebody digested it — and an artefact that was invalid when it
    was digested, or that was edited and then re-digested, has a perfectly valid
    fingerprint. So `verify_evaluation_universe_fingerprint` and
    `verify_evaluation_ranking_fingerprint` re-check the invariants first, and
    a universe with a repeated member or a ranking with two firsts is refused
    here however carefully its digests were recomputed.

    What this cannot re-establish is the labelset: a run carries its digest and
    its protocol, not the judgements themselves. That binding is made once, at
    build time, from a `LabelCoverage` whose digest `verify_label_coverage`
    recomputed from the labels it holds — which is why there is no builder path
    that accepts a bare fingerprint.
    """
    if not isinstance(run, EvaluationRunContract):
        raise EvaluationBindingError(f"{run!r} is not an evaluation run")
    require_supported_metric_contract_version(run.metric_contract_version)
    if run.run_schema_version != EVALUATION_RUN_SCHEMA_VERSION:
        raise MetricContractError(
            f"unsupported evaluation run schema version: "
            f"{run.run_schema_version!r} (this build reads "
            f"{EVALUATION_RUN_SCHEMA_VERSION!r})"
        )
    if not isinstance(run.dataset_id, str) or not run.dataset_id:
        raise EvaluationBindingError("the evaluation run states no dataset_id")
    validate_fingerprint(
        run.dataset_content_fingerprint,
        subject="the run's dataset content fingerprint",
    )
    validate_fingerprint(
        run.profile_context_fingerprint,
        subject="the run's profile context fingerprint",
    )
    if isinstance(run.profile_id, bool) or not isinstance(run.profile_id, int):
        raise EvaluationBindingError(
            f"the evaluation run states a non-integer profile_id "
            f"{run.profile_id!r}"
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
