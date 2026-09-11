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
    calibration_selection_fingerprint,
    canonical_label_order,
    labelset_fingerprint,
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
    MetricContractError,
    RankingSource,
    LabelCoverage,
    MetricRunContext,
    canonical_opportunity_ids,
    require_evidence_class,
    require_supported_metric_contract_version,
    validate_declared_size,
    validate_evaluation_ranking_structure,
    validate_evaluation_universe_structure,
    validate_fingerprint,
    validate_opportunity_id,
    validate_rank_position,
)

__all__ = [
    "assert_ranking_against_dataset",
    "assert_ranking_within_universe",
    "assert_universe_against_dataset",
    "build_evaluation_ranking",
    "build_evaluation_run",
    "build_evaluation_universe",
    "build_label_coverage",
    "build_metric_run_context",
    "ranking_from_frozen_dataset",
    "verify_evaluation_run",
    "verify_evaluation_run_structure",
    "verify_label_coverage",
    "verify_label_coverage_structure",
    "verify_metric_run_context",
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
    ordered = canonical_opportunity_ids(validated)
    size = len(ordered)
    if declared_size is not None:
        # Validated as a count *before* it is compared, so `declared_size=True`
        # cannot confirm the size of a one-member universe by way of `True == 1`.
        validate_declared_size(
            declared_size, subject="the declared evaluation universe size"
        )
        if declared_size != size:
            raise EvaluationBindingError(
                f"the evaluation universe declares size {declared_size!r} and "
                f"holds {size} opportunities"
            )

    universe = _sealed_universe(
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
    # The same membership check the verifier applies, applied here too: one
    # contract, not a builder-side approximation of it.
    assert_universe_against_dataset(universe, dataset)
    return universe


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
    ranking = replace(draft, fingerprint=evaluation_ranking_fingerprint(draft))
    # The same bindings-and-membership check the verifier applies.
    assert_ranking_against_dataset(ranking, dataset)
    return ranking


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


def assert_universe_against_dataset(
    universe: EvaluationUniverse, dataset: FrozenEvaluationDataset
) -> None:
    """Re-establish a universe's membership in a *verified* frozen dataset.

    Structure is not membership. `validate_evaluation_universe_structure` can
    prove that a universe holds unique, well-formed, canonically ordered ids and
    that its declared size counts them; it cannot prove that those ids name
    postings that exist, because a digest over `999` is exactly as valid as a
    digest over a real opportunity. Only the snapshot can answer that, so every
    path that has the snapshot asks it — the builder, and the full verifier.

    Checked: the dataset id and content fingerprint agree, every member is in
    the frozen cohort, and a universe calling itself the whole cohort is one.
    """
    validate_evaluation_universe_structure(universe)
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
    held = set(dataset.opportunity_ids)
    missing = sorted(
        value for value in universe.opportunity_ids if value not in held
    )
    if missing:
        raise EvaluationBindingError(
            f"the evaluation universe names opportunities absent from dataset "
            f"{dataset.dataset_id}: {missing}"
        )
    if (
        universe.kind is EvaluationUniverseKind.FROZEN_DATASET_COHORT
        and universe.opportunity_ids
        != canonical_opportunity_ids(dataset.opportunity_ids)
    ):
        raise EvaluationBindingError(
            f"a {universe.kind} universe is the whole frozen cohort of "
            f"{dataset.dataset_id} ({dataset.record_count} opportunities), not "
            f"the {universe.size}-opportunity set it holds"
        )


def assert_ranking_against_dataset(
    ranking: EvaluationRanking, dataset: FrozenEvaluationDataset
) -> None:
    """Re-establish a ranking's bindings and membership in a verified dataset.

    The same argument as above, plus the profile: a ranking is personalised, so
    an ordering produced against another profile state is not this run's ranking
    however well formed it is.
    """
    validate_evaluation_ranking_structure(ranking)
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
    held = set(dataset.opportunity_ids)
    missing = sorted(
        entry.opportunity_id
        for entry in ranking.entries
        if entry.opportunity_id not in held
    )
    if missing:
        raise EvaluationBindingError(
            f"the ranking names opportunities absent from dataset "
            f"{dataset.dataset_id}: {missing}"
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
        selection=selection,
        labels=canonical_label_order(in_lot),
        judged_outside_selection=outside,
        labelset_fingerprint=recomputed,
    )


def verify_label_coverage_structure(coverage: LabelCoverage) -> str:
    """Re-establish a coverage from itself: the lot, the labels, the digest.

    Everything that can be checked **without** the frozen dataset, and the
    boundary is drawn there deliberately. What this establishes:

    * the lot's own Phase 10.2 digest, recomputed from its members through
      `calibration_selection_fingerprint`, so `selection_fingerprint` provably
      identifies *these* opportunity ids rather than floating beside them;
    * the lot's dataset and profile bindings agree with the coverage's;
    * every label names an opportunity **in that lot** — the hole this closes:
      a digest recomputed over lot A's fingerprint and answers to postings
      outside lot A is self-consistent and says something false;
    * every label's own bindings — dataset id, content fingerprint, profile id,
      profile context, label schema, protocol — agree with the coverage's, so a
      label smuggled in from another snapshot is refused even when the grade it
      contributes leaves the semantic digest unchanged;
    * every grade is a real Phase 10.2 grade, through `validate_relevance_grade`
      rather than a second opinion about the 0-3 scale;
    * one judgement per opportunity, and nothing counted both inside and outside
      the lot;
    * the labelset digest, recomputed over exactly those verified labels and
      that verified lot.

    What it **cannot** establish: that the lot is the lot Phase 10.2's selector
    would actually draw from the snapshot. That needs the snapshot, and it is
    `verify_label_coverage` below.
    """
    if not isinstance(coverage, LabelCoverage):
        raise EvaluationBindingError(f"{coverage!r} is not a label coverage")
    protocol_version = require_supported_protocol_version(
        coverage.label_protocol_version, subject="the labelset"
    )
    validate_fingerprint(
        coverage.labelset_fingerprint, subject="the labelset fingerprint"
    )
    validate_fingerprint(
        coverage.dataset_content_fingerprint,
        subject="the labelset's dataset content fingerprint",
    )
    validate_fingerprint(
        coverage.profile_context_fingerprint,
        subject="the labelset's profile context fingerprint",
    )

    selection = coverage.selection
    if not isinstance(selection, CalibrationSelection):
        raise EvaluationBindingError(
            f"{selection!r} is not a Phase 10.2 calibration selection"
        )
    if selection.selector_version not in SUPPORTED_SELECTOR_VERSIONS:
        raise MetricContractError(
            f"the labelset was drawn by selector "
            f"{selection.selector_version!r}; this build reads "
            f"{list(SUPPORTED_SELECTOR_VERSIONS)}"
        )
    selected = selection.opportunity_ids
    if len(set(selected)) != len(selected):
        raise EvaluationBindingError(
            "the calibration lot repeats opportunity ids; a posting is drawn "
            "once or not at all"
        )
    # Phase 10.2's own selection digest, recomputed from the lot's members. This
    # is what ties `selection_fingerprint` to the ids the labels must belong to.
    recomputed_selection = calibration_selection_fingerprint(
        selector_version=selection.selector_version,
        dataset_id=selection.dataset_id,
        dataset_content_fingerprint=selection.dataset_content_fingerprint,
        sample_size=selection.effective_sample_size,
        selected_opportunity_ids=selected,
    )
    if recomputed_selection != selection.selection_fingerprint:
        raise EvaluationBindingError(
            f"the calibration lot claims fingerprint "
            f"{selection.selection_fingerprint} but its members digest to "
            f"{recomputed_selection}; refusing a lot that is not what it says "
            "it is"
        )
    for name, stated, on_lot in (
        ("dataset_id", coverage.dataset_id, selection.dataset_id),
        (
            "dataset_content_fingerprint",
            coverage.dataset_content_fingerprint,
            selection.dataset_content_fingerprint,
        ),
        ("profile_id", coverage.profile_id, selection.profile_id),
        (
            "profile_context_fingerprint",
            coverage.profile_context_fingerprint,
            selection.profile_context_fingerprint,
        ),
    ):
        if stated != on_lot:
            raise EvaluationBindingError(
                f"the labelset states {name} {stated!r} and the lot it claims "
                f"to answer states {on_lot!r}"
            )

    if not coverage.labels:
        raise EvaluationBindingError(
            "the labelset holds no judgement; a metric run cannot be bound to "
            "evidence that does not exist"
        )
    selected_set = set(selected)
    seen: set[int] = set()
    for item in coverage.labels:
        if not isinstance(item, HumanRelevanceLabel):
            raise EvaluationBindingError(f"{item!r} is not a human relevance label")
        opportunity_id = validate_opportunity_id(
            item.opportunity_id, subject="a judged opportunity"
        )
        if opportunity_id not in selected_set:
            raise EvaluationBindingError(
                f"the labelset exposes a judgement of opportunity "
                f"{opportunity_id}, which is not in lot "
                f"{selection.selection_fingerprint}; a labelset is the answers "
                "of its lot, and a judgement recorded outside it is not covered "
                "by that lot's digest"
            )
        if opportunity_id in seen:
            raise EvaluationBindingError(
                f"opportunity {opportunity_id} is judged twice in the labelset"
            )
        seen.add(opportunity_id)
        for name, stated, on_label in (
            ("dataset_id", coverage.dataset_id, item.dataset_id),
            (
                "dataset_content_fingerprint",
                coverage.dataset_content_fingerprint,
                item.dataset_content_fingerprint,
            ),
            ("profile_id", coverage.profile_id, item.profile_id),
            (
                "profile_context_fingerprint",
                coverage.profile_context_fingerprint,
                item.profile_context_fingerprint,
            ),
            (
                "label_schema_version",
                coverage.label_schema_version,
                item.label_schema_version,
            ),
            ("protocol_version", protocol_version, item.protocol_version),
        ):
            if stated != on_label:
                raise EvaluationBindingError(
                    f"the judgement of opportunity {opportunity_id} states "
                    f"{name} {on_label!r} and the labelset states {stated!r}"
                )
        # Phase 10.2's domain, not a copy of it — and checked *before* the digest
        # is recomputed, because a grade outside the rubric has no canonical
        # payload at all and would fail with a `KeyError` from inside 10.2.
        validate_relevance_grade(item.relevance_grade)

    overlap = sorted(set(coverage.judged_outside_selection) & seen)
    if overlap:
        raise EvaluationBindingError(
            f"opportunities {overlap} are reported both inside and outside the "
            "lot; a judgement is in one labelset or in none"
        )
    inside_claimed_outside = sorted(
        set(coverage.judged_outside_selection) & selected_set
    )
    if inside_claimed_outside:
        raise EvaluationBindingError(
            f"opportunities {inside_claimed_outside} are reported as outside "
            "the lot and are in it"
        )

    recomputed = labelset_fingerprint(
        label_schema_version=coverage.label_schema_version,
        protocol_version=protocol_version,
        dataset_id=coverage.dataset_id,
        dataset_content_fingerprint=coverage.dataset_content_fingerprint,
        profile_id=coverage.profile_id,
        profile_context_fingerprint=coverage.profile_context_fingerprint,
        selector_version=selection.selector_version,
        selection_fingerprint=selection.selection_fingerprint,
        labels=coverage.labels,
    )
    if recomputed != coverage.labelset_fingerprint:
        raise EvaluationBindingError(
            f"the label coverage claims labelset {coverage.labelset_fingerprint} "
            f"but the judgements it holds digest to {recomputed}; refusing a "
            "labelset that is not what it says it is"
        )
    return recomputed


def verify_label_coverage(
    coverage: LabelCoverage, dataset: FrozenEvaluationDataset
) -> str:
    """Everything above, plus the lot **redrawn** from the frozen dataset.

    The full check, and the only one entitled to say that a coverage answers the
    deterministic Phase 10.2 lot rather than a list of ids that digests to the
    right string. `assert_selection_bindings` redraws the lot from the snapshot
    and compares it id by id, which is a statement two digests cannot make.
    """
    verify_label_coverage_structure(coverage)
    assert_selection_bindings(coverage.selection, dataset)
    for name, stated, on_dataset in (
        ("dataset_id", coverage.dataset_id, dataset.dataset_id),
        (
            "dataset_content_fingerprint",
            coverage.dataset_content_fingerprint,
            dataset.content_fingerprint,
        ),
        ("profile_id", coverage.profile_id, dataset.profile_id),
        (
            "profile_context_fingerprint",
            coverage.profile_context_fingerprint,
            dataset.profile_context_fingerprint,
        ),
    ):
        if stated != on_dataset:
            raise EvaluationBindingError(
                f"the labels were made against {name} {stated!r}, not "
                f"{on_dataset!r}"
            )
    return coverage.labelset_fingerprint


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
    resolved_labelset_fingerprint = verify_label_coverage(coverage, dataset)
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

    # Recomputed, then re-established against the snapshot. A universe or a
    # ranking that arrived from outside this package carries valid fingerprints
    # over whatever it happens to contain, so a digest is never taken as
    # evidence that it came through a builder: `999` digests as well as a real
    # opportunity does, and only the frozen dataset knows the difference.
    verify_evaluation_universe_fingerprint(universe)
    verify_evaluation_ranking_fingerprint(ranking)
    assert_universe_against_dataset(universe, dataset)
    assert_ranking_against_dataset(ranking, dataset)

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


def verify_evaluation_run_structure(run: EvaluationRunContract) -> str:
    """Re-establish a run from itself: contract, structure, digests, agreement.

    Everything that can be checked **without** the frozen dataset, and the name
    says so. What it establishes: the contract versions, the run's own primitive
    shapes, the nested universe and ranking re-validated structurally and
    re-digested, the internal agreement between the run and those artefacts,
    `ranking ⊆ universe`, and the run's own digest.

    What it cannot establish, and what no amount of digesting could: that the
    ids in that universe and that ranking name postings in the snapshot the
    `dataset_content_fingerprint` refers to. A SHA-256 identifies content this
    function does not have. `verify_evaluation_run` below has it, and asks.
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


def verify_evaluation_run(
    run: EvaluationRunContract, dataset: FrozenEvaluationDataset
) -> str:
    """The full verification: everything structural, plus the snapshot's answer.

    The frozen dataset is a required argument rather than an optional one,
    because a verifier that claimed to "re-establish every binding" while
    holding only a digest string would be making a promise it cannot keep — and
    a caller who read that promise would stop looking. With the snapshot, the
    two statements structure alone cannot make are made:
    `assert_universe_against_dataset` and `assert_ranking_against_dataset`, each
    of which asks the file whether those opportunities exist.
    """
    verify_evaluation_run_structure(run)
    if run.dataset_id != dataset.dataset_id:
        raise EvaluationBindingError(
            f"the run measures dataset {run.dataset_id}, not "
            f"{dataset.dataset_id}"
        )
    if run.dataset_content_fingerprint != dataset.content_fingerprint:
        raise EvaluationBindingError(
            f"the run measures content fingerprint "
            f"{run.dataset_content_fingerprint}, not {dataset.content_fingerprint}"
        )
    if run.profile_id != dataset.profile_id:
        raise EvaluationBindingError(
            f"the run measures profile {run.profile_id}, not {dataset.profile_id}"
        )
    if run.profile_context_fingerprint != dataset.profile_context_fingerprint:
        raise EvaluationBindingError(
            "the run measures profile context "
            f"{run.profile_context_fingerprint}, not "
            f"{dataset.profile_context_fingerprint}"
        )
    assert_universe_against_dataset(run.universe, dataset)
    assert_ranking_against_dataset(run.ranking, dataset)
    return run.run_fingerprint


# --------------------------------------------------------------------------
# the context an availability gate decides in, and its verification
# --------------------------------------------------------------------------


def verify_metric_run_context(context: MetricRunContext) -> bool:
    """Re-establish a whole availability decision's inputs, and say what they are.

    Returns the **derived** labelset-match state: whether the coverage is the
    labelset the run declares, decided from the two verified artefacts and never
    read from a field a caller could set.

    This is the function that makes verification unavoidable. A
    `MetricRunContext` is a public dataclass and anybody can construct one — so
    nothing is inferred from the fact that one exists. Every gate calls this
    before reading a ranking, a universe, a grade or a labelset identity, and it
    redoes the work from scratch:

    1. the argument really is a context bundle;
    2. `verify_evaluation_run(run, dataset)` — the full, dataset-bound run
       verification: contract versions, structure, digests, membership;
    3. `verify_label_coverage(coverage, dataset)` — the full, dataset-bound
       evidence verification, lot redrawn by Phase 10.2's own selector;
    4. the run and the coverage agree about the snapshot and the person;
    5. the labelset-match state, derived last, from artefacts that have passed
       everything above.

    It costs a re-verification per gate, which is the intended trade: three
    digests over a few hundred labels is cheap, and a decision that silently
    trusted its inputs is not.
    """
    if not isinstance(context, MetricRunContext):
        raise EvaluationBindingError(
            f"{context!r} is not a metric run context; an availability decision "
            "is made about a frozen dataset, a run and a labelset together"
        )
    verify_evaluation_run(context.run, context.dataset)
    verify_label_coverage(context.coverage, context.dataset)
    run = context.run
    coverage = context.coverage
    for name, on_run, on_coverage in (
        ("dataset_id", run.dataset_id, coverage.dataset_id),
        (
            "dataset_content_fingerprint",
            run.dataset_content_fingerprint,
            coverage.dataset_content_fingerprint,
        ),
        ("profile_id", run.profile_id, coverage.profile_id),
        (
            "profile_context_fingerprint",
            run.profile_context_fingerprint,
            coverage.profile_context_fingerprint,
        ),
    ):
        if on_run != on_coverage:
            raise EvaluationBindingError(
                f"the labels were made against {name} {on_coverage!r} and the "
                f"run measures {on_run!r}"
            )
    # Derived here, from two verified artefacts, and returned rather than
    # stored: there is no boolean anywhere a caller could set to make a
    # different labelset look like the right one.
    return (
        coverage.labelset_fingerprint == run.labelset_fingerprint
        and coverage.label_protocol_version == run.label_protocol_version
    )


def build_metric_run_context(
    *,
    dataset: FrozenEvaluationDataset,
    run: EvaluationRunContract,
    coverage: LabelCoverage,
) -> MetricRunContext:
    """Bundle a run with its evidence, verified, ready for the gates.

    The convenient constructor, and nothing more than that: it assembles the
    context and runs `verify_metric_run_context` over it, so a caller finds out
    here rather than at the first gate. It is **not** a privileged path — the
    gates verify whatever they are given, so a context built by hand is equally
    safe and equally checked.
    """
    context = MetricRunContext(dataset=dataset, run=run, coverage=coverage)
    verify_metric_run_context(context)
    return context
