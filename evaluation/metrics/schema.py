"""The versioned vocabulary of an offline metric run — Phase 10.3a.

This module is the *contract* and nothing else: the objects a future ranking
metric will be computed against, the statuses and reason codes a result may
carry, and the validation that decides whether an artefact is well formed. It
opens no file, reads no database, computes no digest and — this is the point of
the whole slice — computes no metric.

Four rules shape everything below, and three of them are inherited.

* **UNJUDGED is not a zero.** Phase 10.2 says an opportunity nobody judged has
  no label row; this slice says the same thing one layer up. `LabelCoverage`
  below has no default grade, no `.get(id, 0)` and no fallback: asking for the
  grade of an unjudged opportunity raises. An evaluation universe is a set of
  questions, and the ones nobody answered stay unanswered.
* **The evaluation universe is not the labelled subset.** A universe is
  declared — the frozen cohort, or a fixed benchmark pool — and the labels are
  then measured *against* it. Defining the universe as "the ids that happen to
  have labels" would make every metric fully available by construction and
  every coverage gate a tautology.
* **UNKNOWN is a value, not an absence.** A metric that cannot be computed is
  `N_A` with a stable reason code, never a silently approximated number and
  never a free-form string a caller has to parse.
* **The payload bound is the payload fingerprinted.** Each contract object has
  exactly one canonical payload function here, and `fingerprint.py` digests
  that same function's output, so "what the object says" and "what its digest
  covers" cannot drift apart.

**No metric formula lives in this package.** No Precision@K, no Recall@K, no
DCG, no IDCG, no NDCG, no baseline, no business KPI. Phase 10.3a builds the
gate; 10.3b walks through it.

The dependency direction, unchanged since Phase 10.1:

    production ranking
        -> frozen evaluation dataset (Phase 10.1)
            -> human labels (Phase 10.2)
                -> offline metric runs (here)

and never the reverse. Nothing in `services/` imports this package, and nothing
here opens SQLite: an evaluation run is decided from a frozen directory and a
label file, on a machine that has those two things and nothing else.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from evaluation.dataset import EvaluationDatasetError
from evaluation.labeling import (
    CALIBRATION_V0_PROVENANCE,
    RELEVANCE_GRADE_NAMES,
    SUPPORTED_PROTOCOL_VERSIONS,
)

__all__ = [
    "BINARY_RELEVANCE_GRADE_NAME",
    "EVALUATION_RANKING_VERSION",
    "EVALUATION_RUN_SCHEMA_VERSION",
    "EVALUATION_UNIVERSE_VERSION",
    "INDEPENDENT_BENCHMARK_PROTOCOL_VERSIONS",
    "METRIC_CONTRACT_VERSION",
    "RELEVANT_GRADE_THRESHOLD",
    "SUPPORTED_METRIC_CONTRACT_VERSIONS",
    "EvaluationBindingError",
    "EvaluationMetricsError",
    "EvaluationRanking",
    "EvaluationRankingEntry",
    "EvaluationRunContract",
    "EvaluationRunProvenance",
    "EvaluationUniverse",
    "EvaluationUniverseKind",
    "EvidenceClass",
    "EvidenceClassError",
    "JudgedCoverage",
    "LabelCoverage",
    "MetricArgumentError",
    "MetricAvailability",
    "MetricAvailabilityDecision",
    "MetricContractError",
    "MetricName",
    "MetricResult",
    "MetricStatus",
    "MetricSupport",
    "MetricUnavailableReason",
    "RankingSource",
    "UnjudgedOpportunityError",
    "canonical_opportunity_ids",
    "evaluation_ranking_payload",
    "evaluation_run_payload",
    "evaluation_universe_payload",
    "is_relevant_grade",
    "metric_result_payload",
    "metric_support_payload",
    "require_evidence_class",
    "require_supported_metric_contract_version",
    "validate_fingerprint",
    "validate_k",
    "validate_opportunity_id",
    "validate_rank_position",
]


# --------------------------------------------------------------------------
# versions
# --------------------------------------------------------------------------

#: The *shape* of a metric run contract — which fields bind a run, and what a
#: result may say. It moves whenever that shape changes, so a stored run
#: artefact written under another contract is refused rather than read field by
#: field as though its absent keys were absent facts.
EVALUATION_RUN_SCHEMA_VERSION = "evaluation-run-v1"

#: The *metric contract* version: what the metrics themselves are defined to
#: mean and under which availability rules they may be reported at all.
#: Deliberately separate from the run shape — two runs can share every field and
#: still not be comparable, because the rule that decided whether a number was
#: reportable changed underneath them. It is inside the run fingerprint for
#: exactly that reason.
METRIC_CONTRACT_VERSION = "evaluation-metric-contract-v1"

#: The contract versions this build is able to interpret. Exactly one, and that
#: is policy rather than oversight: a run recorded under a contract whose
#: availability rules this build does not implement cannot be re-decided here.
SUPPORTED_METRIC_CONTRACT_VERSIONS: tuple[str, ...] = (METRIC_CONTRACT_VERSION,)

#: The shape of an evaluation universe.
EVALUATION_UNIVERSE_VERSION = "evaluation-universe-v1"

#: The shape of a frozen evaluation ranking.
EVALUATION_RANKING_VERSION = "evaluation-ranking-v1"


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class EvaluationMetricsError(EvaluationDatasetError):
    """Raised when an offline metric run cannot be built or decided safely.

    A subclass of the Phase 10.1 error for the same reason `HumanLabelError` is:
    a caller that already refuses an untrustworthy evaluation artefact refuses
    an untrustworthy metric run without learning a second exception hierarchy,
    and the two failures are the same kind of failure. Everything this package
    raises is one of the four subclasses below; no low-level `KeyError`,
    `ValueError` or `TypeError` is allowed to escape a public function.
    """


class MetricContractError(EvaluationMetricsError):
    """The artefact was produced under a contract this build cannot interpret."""


class EvaluationBindingError(EvaluationMetricsError):
    """Two artefacts of a run do not agree about what is being evaluated.

    A dataset fingerprint that moved, a universe built for another snapshot, a
    ranking naming an opportunity the universe does not contain, a self-declared
    fingerprint that does not survive recomputation. Nothing is repaired: a run
    assembled from artefacts that disagree would produce a number about nothing
    in particular.
    """


class EvidenceClassError(EvaluationMetricsError):
    """The evidence this run is built on cannot support the class it claims."""


class MetricArgumentError(EvaluationMetricsError):
    """A caller asked a malformed question — a `K` that is not a rank cut-off."""


class UnjudgedOpportunityError(EvaluationMetricsError):
    """The effective grade of an opportunity nobody judged was asked for.

    The single most important refusal in this package. Returning `0` here would
    be the whole failure mode Phase 10 exists to prevent, written as a
    convenience: an unanswered question reported as a negative answer, and every
    metric downstream quietly counting it as a mistake the pipeline made.
    """


# --------------------------------------------------------------------------
# relevance threshold — reused, never redefined
# --------------------------------------------------------------------------

#: The rubric grade that *names* binary relevance. The numeric threshold is
#: derived from Phase 10.2's frozen grade table rather than written down a
#: second time: `relevant iff grade >= 2` is a fact about the rubric, and a
#: copy of the number here would be free to disagree with it.
BINARY_RELEVANCE_GRADE_NAME = "RELEVANT"

_GRADE_BY_NAME: Mapping[str, int] = {
    name: grade for grade, name in RELEVANCE_GRADE_NAMES.items()
}

RELEVANT_GRADE_THRESHOLD: int = _GRADE_BY_NAME[BINARY_RELEVANCE_GRADE_NAME]

#: Stated as import-time assertions because a silent divergence — a rubric that
#: renamed a grade, or reordered the scale — would move the relevance threshold
#: under every future metric without a single test failing for the right reason.
assert RELEVANT_GRADE_THRESHOLD == 2
assert _GRADE_BY_NAME["VERY_RELEVANT"] > RELEVANT_GRADE_THRESHOLD
assert _GRADE_BY_NAME["WEAKLY_RELEVANT"] < RELEVANT_GRADE_THRESHOLD
assert _GRADE_BY_NAME["OUT_OF_TARGET"] < RELEVANT_GRADE_THRESHOLD


def is_relevant_grade(grade: int) -> bool:
    """Is this **recorded** grade relevant under the frozen binary contract?

    Takes a grade, never an opportunity id: there is deliberately no overload
    that accepts "the grade of opportunity 7, whatever that is", because such a
    function would have to decide what to do about an opportunity nobody judged
    and the only correct answer — refuse — is not expressible as a `bool`.
    """
    if isinstance(grade, bool) or not isinstance(grade, int):
        raise MetricArgumentError(
            f"a relevance grade is an integer, not {grade!r} "
            f"({type(grade).__name__})"
        )
    return grade >= RELEVANT_GRADE_THRESHOLD


# --------------------------------------------------------------------------
# evidence class
# --------------------------------------------------------------------------


class EvidenceClass(StrEnum):
    """What a metric run's evidence is, as a declaration rather than an inference.

    Never derived from the number of labels. Twelve judgements are not a
    benchmark because they are twelve; twelve judgements produced by a model and
    validated by a person would not be a benchmark if there were twelve thousand
    of them. The class states how the evidence was produced and therefore what a
    number computed from it is allowed to claim, and it is inside the run
    fingerprint so the same arithmetic reported under two classes cannot share
    one identity.
    """

    #: Evidence good enough to sharpen a rubric, a pipeline or a metric
    #: implementation, and not good enough to publish a performance claim.
    #: The existing `human-relevance-calibration-v0` round is this, by its own
    #: recorded provenance: AI-assisted reading with final human validation.
    DIAGNOSTIC_CALIBRATION = "DIAGNOSTIC_CALIBRATION"

    #: Evidence produced independently of the system being measured, under the
    #: frozen semantic contract, on a pool fixed in advance. No labelset in this
    #: repository is this, and `require_evidence_class` below fails closed
    #: rather than letting one claim it.
    INDEPENDENT_BENCHMARK = "INDEPENDENT_BENCHMARK"


#: The label protocol versions whose *provenance* could establish an independent
#: benchmark. Empty, and empty on purpose.
#:
#: `human-relevance-calibration-v0` cannot: its own recorded provenance says the
#: round was AI-assisted with final human validation and lists, in
#: `CALIBRATION_V0_PROVENANCE.not_a`, exactly the three things it is not.
#: `human-relevance-v1` is a frozen *definition* under which no judgement has
#: been made and which Phase 10.2's reader still refuses, so no artefact can
#: carry it either.
#:
#: A generic labelset object in this repository therefore exposes nothing that
#: could *prove* independence — not who judged, not whether a model proposed the
#: grade first, not whether the pool was fixed before the ranking was read. The
#: honest response to missing provenance is to fail closed, not to invent a
#: field that would be trusted precisely because nothing ever checks it. Adding
#: a version here is a deliberate act that must arrive with the provenance that
#: justifies it.
INDEPENDENT_BENCHMARK_PROTOCOL_VERSIONS: tuple[str, ...] = ()


def require_evidence_class(
    value: Any, *, label_protocol_version: str, labelset_fingerprint: str
) -> EvidenceClass:
    """Refuse an evidence class the labels behind it cannot support.

    The smallest guard the current metadata can actually enforce, in three
    steps, most specific first:

    1. the **known calibration round** — a labelset whose digest is the one
       `CALIBRATION_V0_PROVENANCE` records may never be an independent
       benchmark, and the refusal quotes that round's own statement of what it
       is not;
    2. the **protocol** — an independent benchmark needs a protocol whose
       provenance could establish independence, and no such protocol exists in
       this build;
    3. the **diagnostic** class — which requires only a protocol this build can
       interpret at all, since a diagnostic run is allowed to be made of
       calibration evidence. That is what it is for.
    """
    try:
        evidence_class = EvidenceClass(value)
    except ValueError as error:
        raise EvidenceClassError(
            f"{value!r} is not an evaluation evidence class; this build knows "
            f"{[str(item) for item in EvidenceClass]}"
        ) from error

    if evidence_class is EvidenceClass.DIAGNOSTIC_CALIBRATION:
        if label_protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise EvidenceClassError(
                f"a {evidence_class} run needs labels made under a protocol "
                f"this build interprets ({list(SUPPORTED_PROTOCOL_VERSIONS)}), "
                f"not {label_protocol_version!r}"
            )
        return evidence_class

    if labelset_fingerprint == CALIBRATION_V0_PROVENANCE.labelset_fingerprint:
        raise EvidenceClassError(
            f"labelset {labelset_fingerprint} is the recorded "
            f"{CALIBRATION_V0_PROVENANCE.protocol_version} calibration round "
            f"({CALIBRATION_V0_PROVENANCE.method}), which its own provenance "
            f"states is not {', not '.join(CALIBRATION_V0_PROVENANCE.not_a)}; "
            f"it cannot be classified {EvidenceClass.INDEPENDENT_BENCHMARK}"
        )
    raise EvidenceClassError(
        f"no label protocol in this build can establish "
        f"{EvidenceClass.INDEPENDENT_BENCHMARK} evidence "
        f"(protocol {label_protocol_version!r}); an independent benchmark needs "
        "provenance this repository does not yet record, and inventing that "
        "metadata here would make the claim unfalsifiable"
    )


def require_supported_metric_contract_version(value: Any) -> str:
    """Refuse a run recorded under metric rules this build does not implement."""
    if value not in SUPPORTED_METRIC_CONTRACT_VERSIONS:
        raise MetricContractError(
            f"unsupported metric contract version: {value!r} (this build "
            f"implements {list(SUPPORTED_METRIC_CONTRACT_VERSIONS)})"
        )
    return str(value)


# --------------------------------------------------------------------------
# primitive validation
# --------------------------------------------------------------------------

_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def validate_fingerprint(value: Any, *, subject: str) -> str:
    """A SHA-256 digest as every other Phase 10 artefact spells one."""
    if not isinstance(value, str) or not _FINGERPRINT_PATTERN.match(value):
        raise EvaluationBindingError(
            f"{subject} is not a SHA-256 fingerprint: {value!r}"
        )
    return value


def validate_opportunity_id(value: Any, *, subject: str) -> int:
    """A positive integer opportunity id, with `bool` refused first.

    `isinstance(True, int)` is true in Python, so a `True` that arrived through
    a JSON round trip or a careless caller would otherwise be read as
    opportunity 1 — a real posting, in every dataset this project has.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvaluationBindingError(
            f"{subject} is not an integer opportunity id: {value!r} "
            f"({type(value).__name__})"
        )
    if value <= 0:
        raise EvaluationBindingError(
            f"{subject} is not a positive opportunity id: {value!r}"
        )
    return value


def validate_rank_position(value: Any, *, subject: str) -> int:
    """A positive integer rank position, with `bool` refused first.

    Phase 9A's own constraint, from migration `0026`:
    `rank_position INTEGER NOT NULL CHECK (rank_position > 0)`. A `True` read as
    rank 1 would place a posting at the top of the ranking, which is the single
    position where a silent error costs the most.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvaluationBindingError(
            f"{subject} is not an integer rank position: {value!r} "
            f"({type(value).__name__})"
        )
    if value <= 0:
        raise EvaluationBindingError(
            f"{subject} is not a positive rank position: {value!r}"
        )
    return value


def validate_k(value: Any) -> int:
    """A rank cut-off: a positive integer, and `bool` is not one.

    `K=True` is the ugliest of the three: it would evaluate the top 1 of a
    ranking and report it as the answer to whatever question the caller thought
    they were asking. There is no reading of a boolean as a cut-off that is
    better than stopping.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise MetricArgumentError(
            f"K must be a positive integer, not {value!r} "
            f"({type(value).__name__})"
        )
    if value < 1:
        raise MetricArgumentError(f"K must be at least 1, not {value!r}")
    return value


# --------------------------------------------------------------------------
# the evaluation universe
# --------------------------------------------------------------------------


class EvaluationUniverseKind(StrEnum):
    """What kind of universe a ranking is being evaluated against.

    The kind changes what a metric *means* without changing any arithmetic —
    a recall over a whole frozen cohort and a recall over a benchmark pool are
    different statements — so it is inside the universe fingerprint.
    """

    #: Every opportunity in the frozen Phase 10.1 dataset: the full cohort the
    #: snapshot was taken over.
    FROZEN_DATASET_COHORT = "FROZEN_DATASET_COHORT"

    #: A pool fixed in advance, a strict subset of the frozen dataset. The shape
    #: a future benchmark takes, representable today so that arriving at one
    #: needs no change to the metric mathematics.
    FIXED_BENCHMARK_POOL = "FIXED_BENCHMARK_POOL"


@dataclass(frozen=True)
class EvaluationUniverse:
    """The closed set of opportunities a ranking is evaluated against.

    **Not "the ids that have labels".** The universe is declared first and the
    labels are measured against it; that asymmetry is what makes
    `EVALUATION_UNIVERSE_NOT_FULLY_JUDGED` a fact a gate can discover rather
    than a state that cannot arise. Define the universe from the labels instead
    and every coverage is 100%, every denominator is known, and every metric is
    available — which is exactly how incomplete evidence becomes a benchmark
    claim.

    `opportunity_ids` is held in Phase 10.1's canonical order (`opportunity_id`
    ASC) because a universe is a *set*: nothing about it depends on the order it
    was assembled in, so it is stored in the one order two equal universes are
    guaranteed to agree on. Order matters in a ranking, and only there.

    Built through `run.build_evaluation_universe`, which is the only place the
    invariants below are established; the dataclass is immutable so they cannot
    later be undone.
    """

    universe_version: str
    kind: EvaluationUniverseKind
    dataset_id: str
    dataset_content_fingerprint: str
    opportunity_ids: tuple[int, ...]
    size: int
    fingerprint: str

    def __contains__(self, opportunity_id: object) -> bool:
        return opportunity_id in set(self.opportunity_ids)


def evaluation_universe_payload(universe: EvaluationUniverse) -> dict[str, Any]:
    """The one canonical shape of a universe: what is digested, and what is read."""
    return {
        "universe_version": universe.universe_version,
        "kind": str(universe.kind),
        "dataset_id": universe.dataset_id,
        "dataset_content_fingerprint": universe.dataset_content_fingerprint,
        "opportunity_ids": list(universe.opportunity_ids),
        "size": universe.size,
    }


# --------------------------------------------------------------------------
# the ranking under evaluation
# --------------------------------------------------------------------------


class RankingSource(StrEnum):
    """Where the ordered list being evaluated came from.

    Both members name something *frozen*. There is deliberately no member for
    "the live Recommendation Engine": a metric run reads a snapshot, never a
    production table, and a source enum that could express the latter would be
    the first step towards a metric that quietly re-ran the pipeline it was
    supposed to be measuring.
    """

    #: A deterministic projection of the `recommendation` block Phase 10.1 froze
    #: on each record — the production ranking as it stood when the snapshot was
    #: taken, read out of the file and nowhere else.
    FROZEN_DATASET_RECOMMENDATION = "FROZEN_DATASET_RECOMMENDATION"

    #: An ordering stated directly in evaluation rank positions — a baseline, a
    #: replayed ranking, a fixture. Named so that such an ordering can never be
    #: mistaken for the production one in a later comparison.
    DECLARED_OFFLINE_RANKING = "DECLARED_OFFLINE_RANKING"


@dataclass(frozen=True)
class EvaluationRankingEntry:
    """One ranked opportunity: where it sits, and where it sat upstream.

    `rank_position` is the *evaluation* position and is contiguous from 1, which
    is what makes "the top K" mean the same thing in every run.

    `source_rank_position` is the position the upstream artefact stated, kept
    because the two can legitimately differ and losing the difference would be a
    silent repair. A frozen cohort excludes postings that later went inactive or
    were merged away, so the production rank positions surviving into a snapshot
    can have holes in them — 1, 2, 4, 7. The projection orders by those
    positions and numbers the result 1..N; the original numbers stay here, in
    the digest, so the derivation is auditable rather than assumed. `None` means
    the ranking was stated directly in evaluation positions and there is no
    upstream position to record — "not stated", as everywhere else in Phase 10.
    """

    rank_position: int
    opportunity_id: int
    source_rank_position: int | None = None


@dataclass(frozen=True)
class EvaluationRanking:
    """The frozen ordered list a metric run is about.

    Bound to the dataset *and* to the profile context, because a ranking is
    personalised: the same postings ordered for two different profile states are
    two different rankings, and a judgement made for one of them says nothing
    about the other.

    Invariants, all established in `run.build_evaluation_ranking` and none of
    them recoverable afterwards because the object is frozen: at least one
    entry; unique opportunity ids; unique, positive, integer rank positions;
    positions contiguous from 1 and stored in ascending order.
    """

    ranking_version: str
    source: RankingSource
    dataset_id: str
    dataset_content_fingerprint: str
    profile_id: int
    profile_context_fingerprint: str
    entries: tuple[EvaluationRankingEntry, ...]
    fingerprint: str

    @property
    def opportunity_ids(self) -> tuple[int, ...]:
        """The ranked ids, in rank order. The order *is* the statement."""
        return tuple(entry.opportunity_id for entry in self.entries)

    @property
    def length(self) -> int:
        return len(self.entries)

    def top(self, k: int) -> tuple[int, ...]:
        """The first `k` ranked ids, refusing a malformed cut-off.

        Never pads. Asking for the top 50 of a ranking of 12 returns twelve ids,
        and it is `availability.effective_k` that records the difference between
        what was asked and what exists.
        """
        return self.opportunity_ids[: validate_k(k)]


def evaluation_ranking_payload(ranking: EvaluationRanking) -> dict[str, Any]:
    """The one canonical shape of a ranking: what is digested, and what is read."""
    return {
        "ranking_version": ranking.ranking_version,
        "source": str(ranking.source),
        "dataset_id": ranking.dataset_id,
        "dataset_content_fingerprint": ranking.dataset_content_fingerprint,
        "profile_id": ranking.profile_id,
        "profile_context_fingerprint": ranking.profile_context_fingerprint,
        # In rank order, never sorted: reordering a ranking is the one change
        # that must always move its digest.
        "entries": [
            {
                "rank_position": entry.rank_position,
                "opportunity_id": entry.opportunity_id,
                "source_rank_position": entry.source_rank_position,
            }
            for entry in ranking.entries
        ],
    }


# --------------------------------------------------------------------------
# the label coverage view
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelCoverage:
    """Which opportunities carry an effective judgement, and which grade.

    A read-only projection of Phase 10.2's label history, built by
    `run.build_label_coverage` through that package's own
    `validate_label_history` and `resolve_effective_labels`. There is no second
    interpretation of what "the current judgement" means: the append-only
    revision chain is Phase 10.2's contract and this slice reads it, it does not
    restate it.

    `grades` holds **only judged opportunities**. There is no entry for an
    unjudged one — not a `None`, not a `0`, not a sentinel — so the difference
    between "judged 0" and "nobody looked" is a difference in the keys of a
    mapping rather than a convention somebody has to remember.
    """

    label_schema_version: str
    label_protocol_version: str
    dataset_id: str
    dataset_content_fingerprint: str
    profile_id: int
    profile_context_fingerprint: str
    labelset_fingerprint: str
    grades: Mapping[int, int]

    def is_judged(self, opportunity_id: int) -> bool:
        return opportunity_id in self.grades

    def grade(self, opportunity_id: int) -> int:
        """The effective grade of a judged opportunity, or a refusal.

        The refusal is the feature. Every convenience this method could offer —
        a default, an `Optional`, a `0` — is a way for an unanswered question to
        reach a metric as an answer.
        """
        try:
            return self.grades[opportunity_id]
        except KeyError:
            raise UnjudgedOpportunityError(
                f"opportunity {opportunity_id} carries no judgement in labelset "
                f"{self.labelset_fingerprint}; an unjudged opportunity has no "
                "grade, and it is emphatically not a 0"
            ) from None

    @property
    def judged_opportunity_ids(self) -> tuple[int, ...]:
        """Judged ids in Phase 10.1's canonical order."""
        return tuple(sorted(self.grades))


@dataclass(frozen=True)
class JudgedCoverage:
    """How much of some set of opportunities carries a judgement.

    A support statistic, not a ranking metric: it counts answered questions and
    says nothing about whether the answers agree with the pipeline. Computing it
    in this slice is therefore not a metric formula sneaking in — it is the
    input the gates below decide on.
    """

    judged_count: int
    total_count: int
    unjudged_opportunity_ids: tuple[int, ...]

    @property
    def fully_judged(self) -> bool:
        return not self.unjudged_opportunity_ids

    @property
    def ratio(self) -> float:
        """Judged fraction, and `0.0` for an empty set rather than a crash.

        An empty set has nothing unjudged in it, so `fully_judged` is true while
        `ratio` is 0.0. That pair is not a contradiction: no gate in this
        package reads `ratio`, which exists for reporting, and every gate reads
        the explicit counts.
        """
        if self.total_count == 0:
            return 0.0
        return self.judged_count / self.total_count


# --------------------------------------------------------------------------
# metric results and their availability
# --------------------------------------------------------------------------


class MetricName(StrEnum):
    """The metrics whose availability this slice decides — and computes none of."""

    PRECISION_AT_K = "PRECISION_AT_K"
    RECALL_AT_K = "RECALL_AT_K"
    NDCG_AT_K = "NDCG_AT_K"


class MetricStatus(StrEnum):
    """What a metric result is: a number, or a stated refusal to produce one."""

    COMPUTED = "COMPUTED"
    N_A = "N_A"


class MetricUnavailableReason(StrEnum):
    """Why a metric is `N_A`, as a closed vocabulary.

    Closed on purpose. A free-form string would be written once, parsed never,
    and would let the next reason code be invented at a call site by whoever was
    in a hurry — which is how "not enough labels" and "insufficient coverage"
    end up being two different names for the same missing denominator.
    """

    #: Some position in the effective top K carries no judgement. Precision over
    #: a partially judged cut-off would be a fraction of an unknown numerator.
    TOP_K_NOT_FULLY_JUDGED = "TOP_K_NOT_FULLY_JUDGED"

    #: The declared universe is not fully judged. NDCG needs this and not merely
    #: a judged top K: the ideal ordering is drawn from the best grades anywhere
    #: in the comparison universe, so an unjudged tail can only lower the IDCG
    #: this build would compute and inflate the ratio.
    EVALUATION_UNIVERSE_NOT_FULLY_JUDGED = "EVALUATION_UNIVERSE_NOT_FULLY_JUDGED"

    #: The total number of relevant items in the universe is not knowable from
    #: the labels, so recall has no denominator. Distinct from the reason above
    #: because it is about a *count* rather than about an ideal ordering, and
    #: the two will stop coinciding the moment a closed-universe rule other than
    #: "fully judged" is justified.
    RECALL_DENOMINATOR_UNKNOWN = "RECALL_DENOMINATOR_UNKNOWN"

    #: The universe is fully judged and holds no relevant item at all. The
    #: denominator is known and it is zero, which is a different fact from not
    #: knowing it — and reporting recall as 0.0 here would describe the
    #: evaluation set, not the ranking.
    NO_RELEVANT_ITEMS = "NO_RELEVANT_ITEMS"

    #: The labels offered are not the labels this run is bound to. The question
    #: is well formed and this evidence cannot answer it.
    LABELSET_BINDING_MISMATCH = "LABELSET_BINDING_MISMATCH"


class MetricAvailabilityDecision(StrEnum):
    """Whether a metric *could* be computed — never whether it *was*.

    Deliberately not `MetricStatus`. `COMPUTED` asserts that a number exists,
    and this slice produces no numbers: a gate that returned `COMPUTED` would be
    claiming something it has no way to back up, and the first caller to read
    that as a score would be reasonable to do so.
    """

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class MetricSupport:
    """The evidence behind an availability decision, in numbers a person can read.

    Everything here is either counted from the artefacts or echoed from the
    request. `numerator` and `denominator` exist because a computed result will
    need somewhere to state them and a reader should not have to learn a second
    shape when 10.3b arrives — and they are `None` throughout this slice,
    because no formula exists to fill them and a fabricated denominator is worse
    than an absent one.
    """

    k_requested: int | None = None
    k_effective: int | None = None
    ranking_length: int | None = None
    universe_size: int | None = None
    judged_count: int | None = None
    judged_in_top_k: int | None = None
    unjudged_in_top_k: tuple[int, ...] = ()
    unjudged_in_universe: tuple[int, ...] = ()
    relevant_count: int | None = None
    numerator: float | None = None
    denominator: float | None = None


def metric_support_payload(support: MetricSupport) -> dict[str, Any]:
    """The one canonical shape of the support block."""
    return {
        "k_requested": support.k_requested,
        "k_effective": support.k_effective,
        "ranking_length": support.ranking_length,
        "universe_size": support.universe_size,
        "judged_count": support.judged_count,
        "judged_in_top_k": support.judged_in_top_k,
        "unjudged_in_top_k": list(support.unjudged_in_top_k),
        "unjudged_in_universe": list(support.unjudged_in_universe),
        "relevant_count": support.relevant_count,
        "numerator": support.numerator,
        "denominator": support.denominator,
    }


@dataclass(frozen=True)
class MetricAvailability:
    """One gate's decision about one metric, with the numbers behind it."""

    metric: MetricName
    decision: MetricAvailabilityDecision
    support: MetricSupport
    reason: MetricUnavailableReason | None = None

    def __post_init__(self) -> None:
        unavailable = self.decision is MetricAvailabilityDecision.UNAVAILABLE
        if unavailable and self.reason is None:
            raise MetricContractError(
                f"{self.metric} is unavailable without a stated reason code"
            )
        if not unavailable and self.reason is not None:
            raise MetricContractError(
                f"{self.metric} is available and states reason {self.reason}"
            )

    @property
    def available(self) -> bool:
        return self.decision is MetricAvailabilityDecision.AVAILABLE

    def as_result(self) -> MetricResult:
        """The `N_A` result this decision implies, when it implies one.

        Only ever one direction. An unavailable metric is already a complete
        result — `N_A`, a reason, the support that explains it — so it converts.
        An *available* one is not a result at all until somebody computes it,
        and this package never will, so asking converts nothing and raises.
        """
        if self.available:
            raise MetricContractError(
                f"{self.metric} is available but no value has been computed; "
                "Phase 10.3a decides availability and computes no metric"
            )
        return MetricResult(
            metric=self.metric,
            status=MetricStatus.N_A,
            value=None,
            reason=self.reason,
            support=self.support,
        )


@dataclass(frozen=True)
class MetricResult:
    """A metric run's answer about one metric: a number, or `N_A` and why.

    The invariants are enforced rather than documented, and they are what make
    `N_A` impossible to soften into a zero: a `COMPUTED` result must carry a
    value and no reason, an `N_A` result must carry a reason and no value.
    """

    metric: MetricName
    status: MetricStatus
    support: MetricSupport
    value: float | None = None
    reason: MetricUnavailableReason | None = None

    def __post_init__(self) -> None:
        if self.status is MetricStatus.COMPUTED:
            if self.value is None:
                raise MetricContractError(
                    f"{self.metric} is COMPUTED but states no value"
                )
            if self.reason is not None:
                raise MetricContractError(
                    f"{self.metric} is COMPUTED and states an N_A reason "
                    f"({self.reason})"
                )
            return
        if self.value is not None:
            raise MetricContractError(
                f"{self.metric} is N_A and states value {self.value!r}; an "
                "unavailable metric has no number, not even a zero"
            )
        if self.reason is None:
            raise MetricContractError(
                f"{self.metric} is N_A without a stated reason code"
            )


def metric_result_payload(result: MetricResult) -> dict[str, Any]:
    """The one canonical shape of a metric result."""
    return {
        "metric": str(result.metric),
        "status": str(result.status),
        "value": result.value,
        "reason": None if result.reason is None else str(result.reason),
        "support": metric_support_payload(result.support),
    }


# --------------------------------------------------------------------------
# the evaluation run contract
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EvaluationRunProvenance:
    """Where and when a run was assembled. **Outside every fingerprint.**

    Phase 10.1's rule, applied one layer up: a digest that moved when the clock
    or the checkout did would describe the run instead of what the run is about.
    Two operators deciding the same availability from the same artefacts, a
    week and a directory apart, must agree.
    """

    generated_at: str | None = None
    dataset_directory: str | None = None
    label_root: str | None = None


@dataclass(frozen=True)
class EvaluationRunContract:
    """One future metrics run, and everything it is bound to.

    A metric number means nothing on its own. It is a measurement of *this*
    ranking, over *this* universe, against *these* judgements, made under *this*
    metric contract, and claiming *this* class of evidence. Change any of them
    and the number may still be arithmetically correct while answering a
    different question — so all of them are bound here and all of them are in
    `run_fingerprint`.

    `provenance` is the one block outside that digest, and it holds nothing a
    measurement can see.

    Assembled by `run.build_evaluation_run`, which establishes every binding,
    and re-checked by `run.verify_evaluation_run`, which trusts none of them:
    following Phase 10.1, a stored object's self-declared fingerprint is
    recomputed from the canonical semantic projection before it is believed.
    """

    run_schema_version: str
    metric_contract_version: str
    evidence_class: EvidenceClass
    dataset_id: str
    dataset_content_fingerprint: str
    profile_id: int
    profile_context_fingerprint: str
    universe: EvaluationUniverse
    ranking: EvaluationRanking
    label_protocol_version: str
    labelset_fingerprint: str
    run_fingerprint: str
    provenance: EvaluationRunProvenance = EvaluationRunProvenance()


def evaluation_run_payload(
    run: EvaluationRunContract, *, include_provenance: bool = True
) -> dict[str, Any]:
    """The run as a structure: the semantic block, and optionally the rest.

    `include_provenance=False` yields exactly what `fingerprint.py` digests,
    which is why the two cannot drift: there is one layout, and the digest is
    the layout minus a block that is named here rather than remembered.
    """
    payload: dict[str, Any] = {
        "run_schema_version": run.run_schema_version,
        "metric_contract_version": run.metric_contract_version,
        "evidence_class": str(run.evidence_class),
        "dataset_id": run.dataset_id,
        "dataset_content_fingerprint": run.dataset_content_fingerprint,
        "profile_id": run.profile_id,
        "profile_context_fingerprint": run.profile_context_fingerprint,
        # The nested artefacts enter by digest rather than by contents: each is
        # verified against its own canonical projection before a run is built,
        # so the digest is the whole of what it says.
        "evaluation_universe_fingerprint": run.universe.fingerprint,
        "ranking_fingerprint": run.ranking.fingerprint,
        "label_protocol_version": run.label_protocol_version,
        "labelset_fingerprint": run.labelset_fingerprint,
    }
    if include_provenance:
        payload["run_fingerprint"] = run.run_fingerprint
        payload["provenance"] = {
            "generated_at": run.provenance.generated_at,
            "dataset_directory": run.provenance.dataset_directory,
            "label_root": run.provenance.label_root,
        }
    return payload


def canonical_opportunity_ids(values: Sequence[int]) -> tuple[int, ...]:
    """Ids in Phase 10.1's canonical order, reused rather than re-decided."""
    return tuple(sorted(values))
