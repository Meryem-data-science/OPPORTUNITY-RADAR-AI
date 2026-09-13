"""The Phase 10.5 experiment contract: the vocabulary, and nothing else.

This module is the *contract*. It opens no file, computes no projection, reads
no record and takes no digest. What it holds is the closed vocabulary a Phase
10.5 experiment is written in — five projections, six overlaps, four ranking
questions — the shapes their results take, and the validators that re-establish
every invariant those shapes claim.

## What Phase 10.5 is, and what it is not

An experiment is an **offline** layer over artefacts that are already frozen:

    Phase 10.1 frozen dataset
        + Phase 10.2 human evidence
        + Phase 10.3 ranking metrics
        + Phase 10.4 BusinessMetricRun
            -> Phase 10.5 experiments

Nothing is re-run. No Matching run, no Recommendation run, no Qualification
pass, no HTTP request, no SQLite write, no profile change. The Recommendation
ranking this package evaluates is the one Phase 10.1 froze; the numbers it
reports over that ranking are computed by Phase 10.3's own authorities, called
rather than reimplemented.

## The two kinds of experiment, kept strictly apart

**Stage-selection experiments** ask which postings a stage of the pipeline
*included*. That is a membership question, and a membership question has no
Precision@K: a set has no order, and `opportunity_id ASC` is a serialization,
not a ranking.

**The observed ranking experiment** asks how good the frozen Recommendation
ordering is. That is the only place Precision@K, Recall@K and NDCG@K appear, and
the only ranking they may be computed over is
`RankingSource.FROZEN_DATASET_RECOMMENDATION`.

There is deliberately no way to state any other ranking here. No new weights, no
new `recommendation_score`, no `match_quality DESC` ordering, and no rule that
puts a posting the Recommendation run never covered at the bottom of a list — an
uncovered posting is not in the ranking at all, which is the fact Phase 10.1
exists to preserve.

## Five projections, independent, never a funnel

`CohortProjection` is closed at five members and every one of them is a
projection of **the same frozen cohort**. They are not stages of a pipeline and
they do not compose: `MATCHING_OBSERVED` is not "what survived
`GEO_NOT_EXPLICITLY_OUT_OF_TARGET`", it is every record whose frozen matching
block is present. Reading them as a funnel would turn four independent
membership facts into a causal story the data does not support, which is why
the only comparisons this contract can express are membership, non-membership,
cohort share and overlap — and why no field, status or reason code anywhere in
it names a conversion, a drop-off, a retention or an improvement.

Two of the five are *not explicitly out* questions, and their asymmetry is the
whole `UNKNOWN != FALSE` rule:

    GEO_NOT_EXPLICITLY_OUT_OF_TARGET   MATCH -> in, UNKNOWN -> in,
                                       OUT_OF_TARGET -> out
    DATA_AI_NOT_EXPLICITLY_OUT_OF_SCOPE
                                       CORE / ADJACENT / UNCERTAIN /
                                       UNCLASSIFIED -> in, OUT_OF_SCOPE -> out

An unplaced posting is included because nobody established it was elsewhere; an
unread posting is included because nobody established it was out of scope. The
excluded set of each is exactly the set the pipeline made an *explicit negative
statement* about.

The other two are presence questions and nothing more. `MATCHING_OBSERVED`
filters on no lane, no `match_quality` and no `evidence_coverage`;
`RECOMMENDATION_OBSERVED` filters on no score, no disposition and no threshold.
A projection that quietly applied a quality bar would be measuring that bar.

## A status is a result, and `N_A` is one

`COMPUTED` and `N_A` are the only two statuses, and an `N_A` is a **present**
result with a stated reason — never a zero, never an empty set, and never a
missing block. Where a projection is `N_A` its membership and counts are
semantically absent: fabricating an empty included set would say the pipeline
included nothing, which is a measurement, and the point of the `N_A` is that no
measurement was possible.

## Identity covers the exact members

Every result fingerprint's domain includes the exact opportunity ids, not merely
the counts. Two projections of the same size over different postings are
different projections, and a digest that could not tell them apart would let a
membership be edited under a valid identity.

The digests themselves live in `fingerprint.py`; this module states the payloads
they are taken over, so "what a result says" and "what its identity covers"
cannot drift into two spellings.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Any

from evaluation.dataset import EvaluationDatasetError
from evaluation.metrics import (
    EvaluationUniverseKind,
    EvidenceClass,
    MetricContractError,
    MetricName,
    MetricResult,
    RankingSource,
    metric_result_payload,
    validate_metric_result_structure,
)

__all__ = [
    "EXPERIMENT_CONTRACT_VERSION",
    "EXPERIMENT_RUN_CONTEXT_SCHEMA_VERSION",
    "EXPERIMENT_RUN_SCHEMA_VERSION",
    "EXPERIMENT_RANKING_SOURCE",
    "EXPERIMENT_RANKING_UNIVERSE_KIND",
    "OVERLAP_PAIR_ORDER",
    "OVERLAP_PAIR_PROJECTIONS",
    "OVERLAP_RESULT_SCHEMA_VERSION",
    "PROJECTION_ORDER",
    "PROJECTION_RESULT_SCHEMA_VERSION",
    "PROVENANCE_PATH_FIELDS",
    "RANKING_EXPERIMENT_QUESTIONS",
    "RANKING_EXPERIMENT_RESULT_SCHEMA_VERSION",
    "SUPPORTED_EXPERIMENT_CONTRACT_VERSIONS",
    "SUPPORTED_EXPERIMENT_RUN_SCHEMA_VERSIONS",
    "CohortProjection",
    "DirectionalRate",
    "DirectionalRateName",
    "ExperimentArgumentError",
    "ExperimentBindingError",
    "ExperimentContractError",
    "ExperimentRun",
    "ExperimentRunContextBinding",
    "ExperimentRunProvenance",
    "ExperimentsError",
    "OverlapPair",
    "OverlapResult",
    "OverlapStatus",
    "OverlapUnavailableReason",
    "ProjectionResult",
    "ProjectionStatus",
    "ProjectionUnavailableReason",
    "RankingExperimentResult",
    "RankingExperimentStatus",
    "RankingExperimentUnavailableReason",
    "RankingMetricEntry",
    "canonical_experiment_opportunity_ids",
    "directional_rate_payload",
    "experiment_run_context_binding_payload",
    "experiment_run_fingerprint_payload",
    "experiment_run_payload",
    "experiment_run_provenance_payload",
    "overlap_pair_projections",
    "overlap_result_payload",
    "projection_result_payload",
    "ranking_experiment_result_payload",
    "ranking_metric_entry_payload",
    "require_supported_experiment_contract_version",
    "require_supported_experiment_run_schema_version",
    "validate_cohort_size",
    "validate_experiment_count",
    "validate_experiment_fingerprint",
    "validate_experiment_opportunity_id",
    "validate_experiment_run_provenance",
    "validate_experiment_run_structure",
    "validate_overlap_result_structure",
    "validate_projection_result_structure",
    "validate_ranking_experiment_result_structure",
]


# --------------------------------------------------------------------------
# versions
# --------------------------------------------------------------------------

#: What the experiments *mean*: which projections exist, what each includes and
#: excludes, which overlaps are defined, which ranking questions are asked and
#: under which rules an `N_A` may be stated. Inside every result's identity, so
#: two results produced under different definitions can never look like one.
EXPERIMENT_CONTRACT_VERSION = "evaluation-experiment-contract-v1"

#: The shape of the transient run context, and of the persistent binding that
#: records which one a run was assembled from.
EXPERIMENT_RUN_CONTEXT_SCHEMA_VERSION = "experiment-run-context-v1"

#: The shape of a stored experiment run document.
EXPERIMENT_RUN_SCHEMA_VERSION = "experiment-run-v1"

#: The shape of one projection result.
PROJECTION_RESULT_SCHEMA_VERSION = "projection-result-v1"

#: The shape of one overlap result.
OVERLAP_RESULT_SCHEMA_VERSION = "overlap-result-v1"

#: The shape of the ranking experiment result.
RANKING_EXPERIMENT_RESULT_SCHEMA_VERSION = "ranking-experiment-result-v1"

#: Exactly one contract version, and that is policy rather than oversight: a run
#: recorded under definitions this build does not implement cannot be re-decided
#: here, and reading it as though it could would be the one failure this whole
#: package is built to make impossible.
SUPPORTED_EXPERIMENT_CONTRACT_VERSIONS: tuple[str, ...] = (
    EXPERIMENT_CONTRACT_VERSION,
)

#: Likewise for the document shape.
SUPPORTED_EXPERIMENT_RUN_SCHEMA_VERSIONS: tuple[str, ...] = (
    EXPERIMENT_RUN_SCHEMA_VERSION,
)


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class ExperimentsError(EvaluationDatasetError):
    """Raised when an experiment artefact cannot be built or read safely.

    A subclass of the Phase 10.1 error, as Phases 10.2, 10.3 and 10.4 are.
    Everything this package raises is one of the three subclasses below; no raw
    `KeyError`, `ValueError`, `TypeError` or `AttributeError` escapes a public
    function.
    """


class ExperimentContractError(ExperimentsError):
    """The artefact is not something this contract can express.

    An unsupported version, a projection outside the closed five, an overlap
    outside the closed six, a ranking block holding other than the four
    questions, a `COMPUTED` result with no membership.
    """


class ExperimentBindingError(ExperimentsError):
    """Two artefacts do not agree about what is being measured.

    A dataset fingerprint that moved, a business metric run about another
    cohort, an overlap whose partitions do not cover the cohort, a ranking whose
    members are not the observed recommendation set, a digest that does not
    survive recomputation.
    """


class ExperimentArgumentError(ExperimentsError):
    """A caller asked a malformed question — a NaN share, a negative count."""


# --------------------------------------------------------------------------
# primitive validators
# --------------------------------------------------------------------------

_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def validate_experiment_fingerprint(value: Any, *, subject: str) -> str:
    """A lowercase hexadecimal SHA-256, or a refusal. A *shape* check only."""
    if not isinstance(value, str) or not _FINGERPRINT_PATTERN.match(value):
        raise ExperimentBindingError(
            f"{subject} is not a SHA-256 fingerprint: {value!r}"
        )
    return value


def validate_experiment_opportunity_id(value: Any, *, subject: str) -> int:
    """A positive integer opportunity id, and `bool` is not one.

    `bool` is refused first because `isinstance(True, int)` holds in Python, so
    a `True` arriving through a JSON round trip would otherwise be read as the
    opportunity id 1 — a membership in a projection, confirmed by nothing.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExperimentBindingError(
            f"{subject} is not an integer opportunity id: {value!r} "
            f"({type(value).__name__})"
        )
    if value <= 0:
        raise ExperimentBindingError(
            f"{subject} is not a positive opportunity id: {value!r}"
        )
    return value


def validate_experiment_count(value: Any, *, subject: str) -> int:
    """A non-negative integer count, and `bool` is not one."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExperimentBindingError(
            f"{subject} is not an integer count: {value!r} "
            f"({type(value).__name__})"
        )
    if value < 0:
        raise ExperimentBindingError(f"{subject} is a negative count: {value!r}")
    return value


def validate_cohort_size(value: Any, *, subject: str) -> int:
    """A cohort holds at least one record.

    A projection of nothing has no cohort share and no membership to state, so
    an empty cohort is refused here rather than dividing by zero four blocks
    later. Phase 10.1's own reader refuses an empty snapshot for the same
    reason; this is that rule restated where this contract can enforce it.
    """
    size = validate_experiment_count(value, subject=subject)
    if size == 0:
        raise ExperimentBindingError(
            f"{subject} is 0; a cohort with no record in it is not a cohort, "
            "and a projection of it would have no share to state"
        )
    return size


def _validate_share(value: Any, *, subject: str) -> float:
    """A finite fraction in `[0, 1]`. No rounding, no clamping."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExperimentArgumentError(
            f"{subject} is not a number: {value!r} ({type(value).__name__})"
        )
    number = float(value)
    if not math.isfinite(number):
        raise ExperimentArgumentError(f"{subject} is not finite: {value!r}")
    if number < 0.0 or number > 1.0:
        raise ExperimentArgumentError(
            f"{subject} is {number!r}, which is outside [0, 1]; a share of a "
            "cohort cannot be negative or exceed the whole"
        )
    return number


def canonical_experiment_opportunity_ids(values: Sequence[int]) -> tuple[int, ...]:
    """Ids in Phase 10.1's canonical order, reused rather than re-decided.

    `opportunity_id ASC`, which is the order the snapshot itself is written in.
    It is the order a *set* is serialized in and says nothing about rank: a
    projection is a membership, and the only place order carries meaning in this
    package is inside the frozen ranking.
    """
    return tuple(sorted(values))


def _validate_id_set(value: Any, *, subject: str) -> tuple[int, ...]:
    """A canonical, duplicate-free tuple of opportunity ids, or a refusal."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ExperimentBindingError(f"{subject} is not a sequence: {value!r}")
    ids = [
        validate_experiment_opportunity_id(item, subject=f"a member of {subject}")
        for item in value
    ]
    if len(set(ids)) != len(ids):
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        raise ExperimentBindingError(
            f"{subject} repeats opportunity ids {duplicates}; a membership holds "
            "each opportunity once"
        )
    if list(ids) != sorted(ids):
        raise ExperimentBindingError(
            f"{subject} is not in the canonical order (opportunity_id ASC); "
            "refusing a reordered membership"
        )
    return tuple(ids)


def require_supported_experiment_contract_version(value: Any) -> str:
    """Refuse definitions this build does not implement, at the boundary."""
    supported = SUPPORTED_EXPERIMENT_CONTRACT_VERSIONS
    if not isinstance(value, str) or value not in supported:
        raise ExperimentContractError(
            f"unsupported experiment contract version: {value!r} (this build "
            f"reads {list(supported)})"
        )
    return value


def require_supported_experiment_run_schema_version(value: Any) -> str:
    """Refuse a document shape this build cannot read, at the boundary."""
    supported = SUPPORTED_EXPERIMENT_RUN_SCHEMA_VERSIONS
    if not isinstance(value, str) or value not in supported:
        raise ExperimentContractError(
            f"unsupported experiment run schema version: {value!r} (this "
            f"build reads {list(supported)})"
        )
    return value


def _require_member(enum: Any, value: Any, *, subject: str) -> Any:
    """One member of a closed vocabulary. Never a coercion from a string."""
    if not isinstance(value, enum):
        raise ExperimentContractError(
            f"{subject} is {value!r}, which is not a member of "
            f"{enum.__name__}; this build knows {[str(item) for item in enum]}"
        )
    return value


def _require_text(value: Any, *, subject: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperimentBindingError(
            f"{subject} must be a non-empty string, not {value!r} "
            f"({type(value).__name__})"
        )
    if value != value.strip():
        raise ExperimentBindingError(
            f"{subject} must not be padded with whitespace: {value!r}"
        )
    return value


def _require_optional_text(value: Any, *, subject: str) -> str | None:
    """`None` means *not stated* and stays `None`."""
    return None if value is None else _require_text(value, subject=subject)


#: An RFC 3339 instant with an explicit offset, spelled exactly as Phase 10.4
#: spells one. A timestamp with no offset does not identify a moment — it
#: identifies a moment per timezone.
_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d{1,9})?([Zz]|[+-]\d{2}:\d{2})$"
)


def _require_timestamp(value: Any, *, subject: str) -> str:
    """An RFC 3339 instant with an explicit offset, checked against the calendar.

    The repository's existing convention, applied here rather than restated: the
    pattern is Phase 10.4's, and the calendar check behind it is what stops
    `2026-02-30T00:00:00Z` from being a well-shaped string that is not a day.
    """
    if not isinstance(value, str) or not _TIMESTAMP_PATTERN.match(value):
        raise ExperimentBindingError(
            f"{subject} is not an RFC 3339 timestamp with an explicit offset "
            f"(e.g. 2026-03-01T09:00:00Z): {value!r}"
        )
    normalized = value.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ExperimentBindingError(
            f"{subject} is not a real instant: {value!r}"
        ) from error
    if parsed.tzinfo is None:  # pragma: no cover - the pattern requires one
        raise ExperimentBindingError(
            f"{subject} states no UTC offset: {value!r}"
        )
    return value


# --------------------------------------------------------------------------
# the five projections
# --------------------------------------------------------------------------


class CohortProjection(StrEnum):
    """The five independent projections of one frozen cohort. Closed at five.

    Every member is a membership question about the same cohort, answered from
    the frozen record and nothing else. **None of them is a stage of a funnel**
    — see the module docstring — and no member may be added without a decision
    that says what it means and why the existing five do not say it.
    """

    #: Every record in the snapshot. The reference set every share is taken of
    #: and every overlap's `neither` partition is completed against. Its
    #: `opportunity_id ASC` order is a serialization and is **never** a ranking.
    FROZEN_COHORT = "FROZEN_COHORT"

    #: The postings the pipeline did not explicitly place outside the bound
    #: target country: `MATCH` and `UNKNOWN` are in, `OUT_OF_TARGET` is out. The
    #: target comes from the verified profile target binding of the bound
    #: Phase 10.4 run and from nowhere else; with no target there is no
    #: question, and the result is `N_A / TARGET_COUNTRY_UNAVAILABLE` rather
    #: than a fallback to any country.
    GEO_NOT_EXPLICITLY_OUT_OF_TARGET = "GEO_NOT_EXPLICITLY_OUT_OF_TARGET"

    #: The postings the Data/AI classifier did not explicitly rule out:
    #: `CORE_TARGET`, `ADJACENT_TARGET`, `UNCERTAIN` and `UNCLASSIFIED` are in,
    #: `OUT_OF_SCOPE` is out. A posting the classifier never read is
    #: `UNCLASSIFIED` and therefore **included** — it was never ruled out.
    DATA_AI_NOT_EXPLICITLY_OUT_OF_SCOPE = "DATA_AI_NOT_EXPLICITLY_OUT_OF_SCOPE"

    #: The postings the frozen Matching run assessed at all. Presence only: no
    #: lane, no `match_quality`, no `evidence_coverage`.
    MATCHING_OBSERVED = "MATCHING_OBSERVED"

    #: The postings the frozen Recommendation run assessed at all. Presence
    #: only: no score, no disposition, no threshold.
    RECOMMENDATION_OBSERVED = "RECOMMENDATION_OBSERVED"


#: The canonical order of the five projections in a run and in a report. The
#: reference cohort first, then the four questions in the order the overlap
#: pairs below are named: G, A, M, R.
PROJECTION_ORDER: tuple[CohortProjection, ...] = (
    CohortProjection.FROZEN_COHORT,
    CohortProjection.GEO_NOT_EXPLICITLY_OUT_OF_TARGET,
    CohortProjection.DATA_AI_NOT_EXPLICITLY_OUT_OF_SCOPE,
    CohortProjection.MATCHING_OBSERVED,
    CohortProjection.RECOMMENDATION_OBSERVED,
)


class ProjectionStatus(StrEnum):
    """What a projection result is: a membership, or a stated refusal."""

    COMPUTED = "COMPUTED"
    N_A = "N_A"


class ProjectionUnavailableReason(StrEnum):
    """Why a projection is `N_A`, as a closed vocabulary. One member in v1.

    Closed on purpose, and short on purpose. A free-form string would be written
    once and parsed never, and would let the next reason be invented at a call
    site by whoever was in a hurry.
    """

    #: The bound Phase 10.4 run carries no verified target country — either no
    #: `ProfileTargetBindingEvidence` at all, or one whose `country_code` is
    #: `None`, which is that contract's coherent evidence of an UNKNOWN target.
    #: There is nothing to compare a posting's country against, and the honest
    #: answer is that the question cannot be asked — not that every posting is
    #: out of target, and emphatically not a hardcoded country.
    TARGET_COUNTRY_UNAVAILABLE = "TARGET_COUNTRY_UNAVAILABLE"


@dataclass(frozen=True)
class ProjectionResult:
    """One projection of one frozen cohort: who is in it, and how many.

    **The membership is the result.** `included_opportunity_ids` holds the exact
    ids, in canonical order, and it is inside `result_fingerprint`: two
    projections with identical counts over different postings are different
    projections, and an identity that covered only the counts would let a
    membership be edited under a valid digest.

    On `N_A` the membership and the three counts are **semantically absent** —
    `None`, not an empty tuple and not a zero. A projection that could not be
    computed did not include nothing; it included nothing *knowable*, and an
    empty set is a measurement that says the pipeline excluded everything.

    `cohort_size` stays stated either way: it is a fact about the snapshot
    rather than about this projection, and a reader of an `N_A` result still
    needs to know what was not projected.

    Sealed only by `projections.compute_projection`. The dataclass is public so
    a stored document can be parsed back into it, and holding one proves
    nothing: every verifier re-establishes the structure and recomputes the
    digest before reading a member.
    """

    result_schema_version: str
    contract_version: str
    projection: CohortProjection
    status: ProjectionStatus
    dataset_id: str
    dataset_content_fingerprint: str
    cohort_size: int
    result_fingerprint: str
    unavailable_reason: ProjectionUnavailableReason | None = None
    included_opportunity_ids: tuple[int, ...] | None = None
    included_count: int | None = None
    excluded_count: int | None = None
    cohort_share: float | None = None

    @property
    def computed(self) -> bool:
        return self.status is ProjectionStatus.COMPUTED

    @property
    def included_ids(self) -> tuple[int, ...]:
        """The membership, or a refusal naming the reason it is absent.

        The refusal is the feature. Every convenience this could offer — an
        empty tuple, a `None` the caller forgets to check — is a way for "we
        could not ask" to reach an overlap or a report as "nobody was included".
        """
        if self.included_opportunity_ids is None:
            raise ExperimentContractError(
                f"{self.projection} is {self.status}"
                + (
                    ""
                    if self.unavailable_reason is None
                    else f" / {self.unavailable_reason}"
                )
                + " and states no membership; an unavailable projection has no "
                "included set, not even an empty one"
            )
        return self.included_opportunity_ids


def projection_result_payload(
    result: ProjectionResult, *, include_result_fingerprint: bool = True
) -> dict[str, Any]:
    """The projection result as a structure: the semantic block, and its digest.

    One layout, used by the stored document and by the identity, so "what the
    file says" and "what the fingerprint covers" cannot drift apart.
    `include_result_fingerprint=False` yields exactly what `fingerprint.py`
    digests — the block's own digest is derived from this domain, so including
    it there would be circular — and the flag is named here rather than
    remembered at two call sites.
    """
    included = result.included_opportunity_ids
    payload: dict[str, Any] = {
        "result_schema_version": result.result_schema_version,
        "contract_version": result.contract_version,
        "projection": str(result.projection),
        "status": str(result.status),
        "unavailable_reason": (
            None
            if result.unavailable_reason is None
            else str(result.unavailable_reason)
        ),
        "dataset_id": result.dataset_id,
        "dataset_content_fingerprint": result.dataset_content_fingerprint,
        "cohort_size": result.cohort_size,
        # `null`, never `[]`: the absence of a membership is a different
        # statement from a membership of nobody.
        "included_opportunity_ids": None if included is None else list(included),
        "included_count": result.included_count,
        "excluded_count": result.excluded_count,
        "cohort_share": result.cohort_share,
    }
    if include_result_fingerprint:
        payload["result_fingerprint"] = result.result_fingerprint
    return payload


def validate_projection_result_structure(result: Any) -> ProjectionResult:
    """Re-establish every invariant a projection result claims, or refuse it.

    Applied by the builder *and* by every verifier, because the two must not
    drift into separate opinions of the contract — and because a digest taken
    over an invalid artefact is a perfectly valid digest. A result whose counts
    disagree with its membership, or whose share disagrees with its counts, has
    an impeccable fingerprint over arithmetic nobody should read.
    """
    if not isinstance(result, ProjectionResult):
        raise ExperimentContractError(f"{result!r} is not a projection result")
    if result.result_schema_version != PROJECTION_RESULT_SCHEMA_VERSION:
        raise ExperimentContractError(
            f"unsupported projection result version: "
            f"{result.result_schema_version!r} (this build reads "
            f"{PROJECTION_RESULT_SCHEMA_VERSION!r})"
        )
    require_supported_experiment_contract_version(result.contract_version)
    _require_member(
        CohortProjection, result.projection, subject="the projection"
    )
    _require_member(ProjectionStatus, result.status, subject="the status")
    _require_text(result.dataset_id, subject="the projection's dataset id")
    validate_experiment_fingerprint(
        result.dataset_content_fingerprint,
        subject="the projection's dataset content fingerprint",
    )
    cohort_size = validate_cohort_size(
        result.cohort_size, subject="the projected cohort size"
    )
    validate_experiment_fingerprint(
        result.result_fingerprint, subject="the projection result fingerprint"
    )

    if result.status is ProjectionStatus.N_A:
        _require_member(
            ProjectionUnavailableReason,
            result.unavailable_reason,
            subject=f"the reason {result.projection} is unavailable",
        )
        for name in (
            "included_opportunity_ids",
            "included_count",
            "excluded_count",
            "cohort_share",
        ):
            if getattr(result, name) is not None:
                raise ExperimentContractError(
                    f"{result.projection} is N_A and states "
                    f"{name}={getattr(result, name)!r}; an unavailable "
                    "projection has no membership and no count, not even an "
                    "empty one and not even a zero"
                )
        return result

    if result.unavailable_reason is not None:
        raise ExperimentContractError(
            f"{result.projection} is COMPUTED and states unavailable reason "
            f"{result.unavailable_reason}"
        )
    included = _validate_id_set(
        result.included_opportunity_ids,
        subject=f"the membership of {result.projection}",
    )
    included_count = validate_experiment_count(
        result.included_count, subject=f"the included count of {result.projection}"
    )
    excluded_count = validate_experiment_count(
        result.excluded_count, subject=f"the excluded count of {result.projection}"
    )
    if included_count != len(included):
        raise ExperimentBindingError(
            f"{result.projection} states included_count {included_count} and "
            f"holds {len(included)} opportunity id(s)"
        )
    if included_count + excluded_count != cohort_size:
        raise ExperimentBindingError(
            f"{result.projection} includes {included_count} and excludes "
            f"{excluded_count} of a cohort of {cohort_size}; a projection "
            "partitions its cohort, so the two counts sum to it"
        )
    share = _validate_share(
        result.cohort_share, subject=f"the cohort share of {result.projection}"
    )
    # Exact, because both sides are derived from the same two integers by the
    # same division. A tolerance here would be a licence for a share that was
    # stated rather than computed.
    if share != included_count / cohort_size:
        raise ExperimentBindingError(
            f"{result.projection} states cohort share {share!r} and its counts "
            f"imply {included_count / cohort_size!r}"
        )
    if result.projection is CohortProjection.FROZEN_COHORT:
        # The reference projection is the cohort. Checked rather than assumed:
        # a `FROZEN_COHORT` that excluded a record would silently shrink every
        # `neither` partition and every share in the run.
        if included_count != cohort_size:
            raise ExperimentBindingError(
                f"{result.projection} includes {included_count} of "
                f"{cohort_size} record(s); the reference projection is the whole "
                "frozen cohort, and one that excludes a record is not it"
            )
    return result


# --------------------------------------------------------------------------
# the six overlaps
# --------------------------------------------------------------------------


class OverlapPair(StrEnum):
    """The six unordered pairs of the four *question* projections. Closed at six.

    Six because the four questions — G, A, M, R — have exactly six unordered
    pairs, and `FROZEN_COHORT` is not among them: it is the reference set every
    overlap is completed against, so pairing it with anything would ask whether
    a projection overlaps the cohort it is a projection of.

    Each member names its two projections in the canonical order G < A < M < R,
    so "left" and "right" are fixed by the contract rather than by a caller.
    That matters because the two directional rates below are asymmetric: which
    one is which must not depend on the order somebody passed arguments in.
    """

    GEO_AND_DATA_AI = "GEO_AND_DATA_AI"
    GEO_AND_MATCHING = "GEO_AND_MATCHING"
    GEO_AND_RECOMMENDATION = "GEO_AND_RECOMMENDATION"
    DATA_AI_AND_MATCHING = "DATA_AI_AND_MATCHING"
    DATA_AI_AND_RECOMMENDATION = "DATA_AI_AND_RECOMMENDATION"
    MATCHING_AND_RECOMMENDATION = "MATCHING_AND_RECOMMENDATION"


#: Each pair's (left, right) projections, in the canonical order G < A < M < R.
#: One mapping, so no resolver and no verifier spells a pair's members twice.
OVERLAP_PAIR_PROJECTIONS: Mapping[
    OverlapPair, tuple[CohortProjection, CohortProjection]
] = {
    OverlapPair.GEO_AND_DATA_AI: (
        CohortProjection.GEO_NOT_EXPLICITLY_OUT_OF_TARGET,
        CohortProjection.DATA_AI_NOT_EXPLICITLY_OUT_OF_SCOPE,
    ),
    OverlapPair.GEO_AND_MATCHING: (
        CohortProjection.GEO_NOT_EXPLICITLY_OUT_OF_TARGET,
        CohortProjection.MATCHING_OBSERVED,
    ),
    OverlapPair.GEO_AND_RECOMMENDATION: (
        CohortProjection.GEO_NOT_EXPLICITLY_OUT_OF_TARGET,
        CohortProjection.RECOMMENDATION_OBSERVED,
    ),
    OverlapPair.DATA_AI_AND_MATCHING: (
        CohortProjection.DATA_AI_NOT_EXPLICITLY_OUT_OF_SCOPE,
        CohortProjection.MATCHING_OBSERVED,
    ),
    OverlapPair.DATA_AI_AND_RECOMMENDATION: (
        CohortProjection.DATA_AI_NOT_EXPLICITLY_OUT_OF_SCOPE,
        CohortProjection.RECOMMENDATION_OBSERVED,
    ),
    OverlapPair.MATCHING_AND_RECOMMENDATION: (
        CohortProjection.MATCHING_OBSERVED,
        CohortProjection.RECOMMENDATION_OBSERVED,
    ),
}

#: The canonical order of the six overlaps in a run and in a report: the pairs
#: of G < A < M < R, read left to right and top to bottom.
OVERLAP_PAIR_ORDER: tuple[OverlapPair, ...] = (
    OverlapPair.GEO_AND_DATA_AI,
    OverlapPair.GEO_AND_MATCHING,
    OverlapPair.GEO_AND_RECOMMENDATION,
    OverlapPair.DATA_AI_AND_MATCHING,
    OverlapPair.DATA_AI_AND_RECOMMENDATION,
    OverlapPair.MATCHING_AND_RECOMMENDATION,
)


def overlap_pair_projections(
    pair: OverlapPair,
) -> tuple[CohortProjection, CohortProjection]:
    """The pair's two projections, left then right, or a refusal."""
    _require_member(OverlapPair, pair, subject="the overlap pair")
    return OVERLAP_PAIR_PROJECTIONS[pair]


class OverlapStatus(StrEnum):
    """What an overlap result is: four partitions, or a stated refusal."""

    COMPUTED = "COMPUTED"
    N_A = "N_A"


class OverlapUnavailableReason(StrEnum):
    """Why an overlap or one of its rates is `N_A`. Closed vocabulary."""

    #: One of the two input projections is itself `N_A`, so there is no
    #: membership to intersect. The whole overlap is unavailable and states no
    #: partition: fabricating four empty sets would assert that nobody is in
    #: either projection, which is a measurement rather than a refusal.
    INPUT_PROJECTION_UNAVAILABLE = "INPUT_PROJECTION_UNAVAILABLE"

    #: A *directional rate*'s reference projection is empty, so the rate has no
    #: denominator. The overlap itself stays `COMPUTED` — its four partitions
    #: are perfectly well defined over an empty projection — and only the rate
    #: that would divide by zero refuses. An empty computed projection is a
    #: fact; an unavailable one is the absence of one.
    EMPTY_REFERENCE_PROJECTION = "EMPTY_REFERENCE_PROJECTION"


class DirectionalRateName(StrEnum):
    """The two asymmetric rates of one overlap. Both, always, never a mean.

    They answer different questions and neither implies the other: how much of
    the left projection is also in the right, and how much of the right is also
    in the left. Reporting one of them, or an average of the two, would hide
    which side the asymmetry is on — and there is deliberately no third member
    that names a causal reading of either.
    """

    #: `|P ∩ Q| / |P|` — of the left projection, how much is also in the right.
    RIGHT_AMONG_LEFT = "RIGHT_AMONG_LEFT"

    #: `|P ∩ Q| / |Q|` — of the right projection, how much is also in the left.
    LEFT_AMONG_RIGHT = "LEFT_AMONG_RIGHT"


@dataclass(frozen=True)
class DirectionalRate:
    """One direction of one overlap, with the fraction that produced it.

    `numerator` and `denominator` are stated on an `N_A` rate as well, and both
    are `0` there: the reference projection is empty, so the numerator really is
    zero and the denominator really is zero. That is a different statement from
    a projection whose membership could not be computed at all, which is the
    whole-overlap `INPUT_PROJECTION_UNAVAILABLE` above, and it is why the counts
    here are facts rather than placeholders.

    `value` is `None` on `N_A` and a `0/0` is never reported as `0.0`.
    """

    name: DirectionalRateName
    status: OverlapStatus
    numerator: int
    denominator: int
    value: float | None = None
    unavailable_reason: OverlapUnavailableReason | None = None


def directional_rate_payload(rate: DirectionalRate) -> dict[str, Any]:
    """The one canonical shape of a directional rate."""
    return {
        "name": str(rate.name),
        "status": str(rate.status),
        "unavailable_reason": (
            None if rate.unavailable_reason is None else str(rate.unavailable_reason)
        ),
        "numerator": rate.numerator,
        "denominator": rate.denominator,
        "value": rate.value,
    }


def _validate_directional_rate(
    rate: Any, *, expected: DirectionalRateName, subject: str
) -> DirectionalRate:
    """Re-establish one rate: its name, its status, and its own arithmetic."""
    if not isinstance(rate, DirectionalRate):
        raise ExperimentContractError(f"{subject} is not a directional rate")
    _require_member(DirectionalRateName, rate.name, subject=f"the name of {subject}")
    if rate.name is not expected:
        raise ExperimentBindingError(
            f"{subject} is named {rate.name} and this position holds {expected}"
        )
    _require_member(OverlapStatus, rate.status, subject=f"the status of {subject}")
    numerator = validate_experiment_count(
        rate.numerator, subject=f"the numerator of {subject}"
    )
    denominator = validate_experiment_count(
        rate.denominator, subject=f"the denominator of {subject}"
    )
    if numerator > denominator:
        raise ExperimentBindingError(
            f"{subject} states {numerator}/{denominator}; an intersection cannot "
            "hold more members than the projection it is taken of"
        )
    if rate.status is OverlapStatus.N_A:
        empty = OverlapUnavailableReason.EMPTY_REFERENCE_PROJECTION
        if rate.unavailable_reason is not empty:
            raise ExperimentContractError(
                f"{subject} is N_A and states reason {rate.unavailable_reason}; "
                "the only reason a directional rate may refuse is "
                f"{OverlapUnavailableReason.EMPTY_REFERENCE_PROJECTION}"
            )
        if denominator != 0:
            raise ExperimentBindingError(
                f"{subject} is N_A / "
                f"{OverlapUnavailableReason.EMPTY_REFERENCE_PROJECTION} and "
                f"states denominator {denominator}; that reason names an empty "
                "reference projection, and this one is not empty"
            )
        if numerator != 0:
            raise ExperimentBindingError(
                f"{subject} is N_A and states numerator {numerator}; an "
                "intersection with an empty projection is empty"
            )
        if rate.value is not None:
            raise ExperimentBindingError(
                f"{subject} is N_A and states value {rate.value!r}; a rate with "
                "no denominator has no number, not even a zero"
            )
        return rate
    if rate.unavailable_reason is not None:
        raise ExperimentContractError(
            f"{subject} is COMPUTED and states unavailable reason "
            f"{rate.unavailable_reason}"
        )
    if denominator == 0:
        raise ExperimentBindingError(
            f"{subject} is COMPUTED over an empty reference projection; a rate "
            "with no denominator is N_A / "
            f"{OverlapUnavailableReason.EMPTY_REFERENCE_PROJECTION}"
        )
    value = _validate_share(rate.value, subject=f"the value of {subject}")
    if value != numerator / denominator:
        raise ExperimentBindingError(
            f"{subject} states value {value!r} and its fraction "
            f"{numerator}/{denominator} implies {numerator / denominator!r}"
        )
    return rate


@dataclass(frozen=True)
class OverlapResult:
    """How two projections of one cohort sit relative to each other.

    **Four partitions, disjoint and exhaustive over the cohort**: the postings in
    both, the ones only in the left projection, the ones only in the right, and
    the ones in neither. The last is why the frozen cohort is the reference set:
    `neither` is `C \\ (P ∪ Q)`, and without a cohort to complete against, "in
    neither" would name no set at all.

    Each partition holds its exact ids in canonical order and all four are
    inside `result_fingerprint`, for the same reason a projection's membership
    is: two overlaps with identical counts over different postings are different
    overlaps.

    **Two rates, both stated.** `right_among_left` and `left_among_right` are
    asymmetric and neither implies the other; a single "overlap rate" would hide
    which side the asymmetry is on. A rate whose reference projection is empty
    is `N_A / EMPTY_REFERENCE_PROJECTION` while the overlap stays `COMPUTED`.

    On `N_A / INPUT_PROJECTION_UNAVAILABLE` every partition, every count and both
    rates are semantically absent. Nothing is fabricated: an overlap with an
    unavailable input is not an overlap of empty sets.

    Nothing here names a conversion, a drop-off or a cause. The two projections
    are independent readings of one cohort and this object says how they
    coincide, which is the only comparison the contract can express.
    """

    result_schema_version: str
    contract_version: str
    pair: OverlapPair
    status: OverlapStatus
    dataset_id: str
    dataset_content_fingerprint: str
    cohort_size: int
    left_projection: CohortProjection
    right_projection: CohortProjection
    left_projection_result_fingerprint: str
    right_projection_result_fingerprint: str
    result_fingerprint: str
    unavailable_reason: OverlapUnavailableReason | None = None
    both_opportunity_ids: tuple[int, ...] | None = None
    left_only_opportunity_ids: tuple[int, ...] | None = None
    right_only_opportunity_ids: tuple[int, ...] | None = None
    neither_opportunity_ids: tuple[int, ...] | None = None
    both_count: int | None = None
    left_only_count: int | None = None
    right_only_count: int | None = None
    neither_count: int | None = None
    right_among_left: DirectionalRate | None = None
    left_among_right: DirectionalRate | None = None

    @property
    def computed(self) -> bool:
        return self.status is OverlapStatus.COMPUTED


def overlap_result_payload(
    result: OverlapResult, *, include_result_fingerprint: bool = True
) -> dict[str, Any]:
    """The overlap result as a structure: the semantic block, and its digest.

    `include_result_fingerprint=False` yields the digest domain, for the same
    reason as above.
    """

    def ids(value: tuple[int, ...] | None) -> list[int] | None:
        return None if value is None else list(value)

    payload: dict[str, Any] = {
        "result_schema_version": result.result_schema_version,
        "contract_version": result.contract_version,
        "pair": str(result.pair),
        "status": str(result.status),
        "unavailable_reason": (
            None
            if result.unavailable_reason is None
            else str(result.unavailable_reason)
        ),
        "dataset_id": result.dataset_id,
        "dataset_content_fingerprint": result.dataset_content_fingerprint,
        "cohort_size": result.cohort_size,
        "left_projection": str(result.left_projection),
        "right_projection": str(result.right_projection),
        # The inputs enter by digest, as nested artefacts do everywhere in Phase
        # 10: each one is recomputed before an overlap is sealed, so the digest
        # is the whole of what it says.
        "left_projection_result_fingerprint": (
            result.left_projection_result_fingerprint
        ),
        "right_projection_result_fingerprint": (
            result.right_projection_result_fingerprint
        ),
        "both_opportunity_ids": ids(result.both_opportunity_ids),
        "left_only_opportunity_ids": ids(result.left_only_opportunity_ids),
        "right_only_opportunity_ids": ids(result.right_only_opportunity_ids),
        "neither_opportunity_ids": ids(result.neither_opportunity_ids),
        "both_count": result.both_count,
        "left_only_count": result.left_only_count,
        "right_only_count": result.right_only_count,
        "neither_count": result.neither_count,
        "right_among_left": (
            None
            if result.right_among_left is None
            else directional_rate_payload(result.right_among_left)
        ),
        "left_among_right": (
            None
            if result.left_among_right is None
            else directional_rate_payload(result.left_among_right)
        ),
    }
    if include_result_fingerprint:
        payload["result_fingerprint"] = result.result_fingerprint
    return payload


_OVERLAP_ABSENT_ON_N_A = (
    "both_opportunity_ids",
    "left_only_opportunity_ids",
    "right_only_opportunity_ids",
    "neither_opportunity_ids",
    "both_count",
    "left_only_count",
    "right_only_count",
    "neither_count",
    "right_among_left",
    "left_among_right",
)


def validate_overlap_result_structure(result: Any) -> OverlapResult:
    """Re-establish every invariant an overlap result claims, or refuse it.

    The partition rules are the ones worth naming, because they are what a
    forged membership fails: the four sets are pairwise disjoint, their union is
    exactly the cohort by size, `both ∪ left_only` and `both ∪ right_only` are
    the two input memberships as counted, and each rate's fraction is the
    partition's own.

    It does **not** check the partitions against the stored projections — that
    needs the projection results, and `fingerprint.py`'s verifier and
    `run.py`'s do it with them in hand.
    """
    if not isinstance(result, OverlapResult):
        raise ExperimentContractError(f"{result!r} is not an overlap result")
    if result.result_schema_version != OVERLAP_RESULT_SCHEMA_VERSION:
        raise ExperimentContractError(
            f"unsupported overlap result version: "
            f"{result.result_schema_version!r} (this build reads "
            f"{OVERLAP_RESULT_SCHEMA_VERSION!r})"
        )
    require_supported_experiment_contract_version(result.contract_version)
    _require_member(OverlapPair, result.pair, subject="the overlap pair")
    _require_member(OverlapStatus, result.status, subject="the status")
    _require_text(result.dataset_id, subject="the overlap's dataset id")
    validate_experiment_fingerprint(
        result.dataset_content_fingerprint,
        subject="the overlap's dataset content fingerprint",
    )
    cohort_size = validate_cohort_size(
        result.cohort_size, subject="the overlapped cohort size"
    )
    validate_experiment_fingerprint(
        result.result_fingerprint, subject="the overlap result fingerprint"
    )
    left, right = overlap_pair_projections(result.pair)
    if result.left_projection is not left or result.right_projection is not right:
        raise ExperimentBindingError(
            f"{result.pair} is the pair ({left}, {right}) and this result states "
            f"({result.left_projection}, {result.right_projection}); the pair's "
            "members and their canonical order are fixed by the contract"
        )
    validate_experiment_fingerprint(
        result.left_projection_result_fingerprint,
        subject=f"the left projection fingerprint of {result.pair}",
    )
    validate_experiment_fingerprint(
        result.right_projection_result_fingerprint,
        subject=f"the right projection fingerprint of {result.pair}",
    )

    if result.status is OverlapStatus.N_A:
        unavailable_input = OverlapUnavailableReason.INPUT_PROJECTION_UNAVAILABLE
        if result.unavailable_reason is not unavailable_input:
            raise ExperimentContractError(
                f"{result.pair} is N_A and states reason "
                f"{result.unavailable_reason}; the only reason a whole overlap "
                "may refuse is "
                f"{OverlapUnavailableReason.INPUT_PROJECTION_UNAVAILABLE}"
            )
        for name in _OVERLAP_ABSENT_ON_N_A:
            if getattr(result, name) is not None:
                raise ExperimentContractError(
                    f"{result.pair} is N_A and states {name}="
                    f"{getattr(result, name)!r}; an overlap with an unavailable "
                    "input states no partition, no count and no rate — four "
                    "empty sets would assert that nobody is in either "
                    "projection"
                )
        return result

    if result.unavailable_reason is not None:
        raise ExperimentContractError(
            f"{result.pair} is COMPUTED and states unavailable reason "
            f"{result.unavailable_reason}"
        )
    both = _validate_id_set(
        result.both_opportunity_ids, subject=f"the `both` partition of {result.pair}"
    )
    left_only = _validate_id_set(
        result.left_only_opportunity_ids,
        subject=f"the `left_only` partition of {result.pair}",
    )
    right_only = _validate_id_set(
        result.right_only_opportunity_ids,
        subject=f"the `right_only` partition of {result.pair}",
    )
    neither = _validate_id_set(
        result.neither_opportunity_ids,
        subject=f"the `neither` partition of {result.pair}",
    )
    partitions = (
        ("both", both),
        ("left_only", left_only),
        ("right_only", right_only),
        ("neither", neither),
    )
    for name, ids in partitions:
        stated = getattr(result, f"{name}_count")
        counted = validate_experiment_count(
            stated, subject=f"the {name} count of {result.pair}"
        )
        if counted != len(ids):
            raise ExperimentBindingError(
                f"{result.pair} states {name}_count {counted} and its {name} "
                f"partition holds {len(ids)} opportunity id(s)"
            )
    # Pairwise disjoint, and the union exactly the cohort's size. Both halves:
    # four sets can be disjoint and still miss a record, and four sets can cover
    # the right number of slots while sharing a member.
    for (first_name, first), (second_name, second) in (
        (partitions[index], partitions[other])
        for index in range(len(partitions))
        for other in range(index + 1, len(partitions))
    ):
        shared = sorted(set(first) & set(second))
        if shared:
            raise ExperimentBindingError(
                f"{result.pair} holds {shared} in both its {first_name} and its "
                f"{second_name} partition; the four partitions of an overlap are "
                "disjoint"
            )
    union = set(both) | set(left_only) | set(right_only) | set(neither)
    if len(union) != cohort_size:
        raise ExperimentBindingError(
            f"the four partitions of {result.pair} cover {len(union)} "
            f"opportunity id(s) and the cohort holds {cohort_size}; they "
            "partition the whole frozen cohort, so a partition that does not "
            "cover it has lost or double-counted a record"
        )
    _validate_directional_rate(
        result.right_among_left,
        expected=DirectionalRateName.RIGHT_AMONG_LEFT,
        subject=f"the `right among left` rate of {result.pair}",
    )
    _validate_directional_rate(
        result.left_among_right,
        expected=DirectionalRateName.LEFT_AMONG_RIGHT,
        subject=f"the `left among right` rate of {result.pair}",
    )
    # Each rate's fraction is this partition's own, and not a second count of it.
    rate_expectations = (
        (
            result.right_among_left,
            len(both),
            len(both) + len(left_only),
            "the left projection",
        ),
        (
            result.left_among_right,
            len(both),
            len(both) + len(right_only),
            "the right projection",
        ),
    )
    for rate, numerator, denominator, reference in rate_expectations:
        if rate.numerator != numerator or rate.denominator != denominator:
            raise ExperimentBindingError(
                f"{rate.name} of {result.pair} states "
                f"{rate.numerator}/{rate.denominator} and the partitions imply "
                f"{numerator}/{denominator} over {reference}"
            )
    return result


# --------------------------------------------------------------------------
# the observed ranking experiment
# --------------------------------------------------------------------------

#: The only ranking Phase 10.5 may evaluate. Stated as a constant so the rule
#: is one symbol rather than a comparison written in four places, and so a test
#: can assert that it is the frozen production ordering and nothing else.
EXPERIMENT_RANKING_SOURCE: RankingSource = RankingSource.FROZEN_DATASET_RECOMMENDATION

#: The only universe a Phase 10.5 ranking metric may be computed against: the
#: **whole frozen cohort**. Emphatically not `RECOMMENDATION_OBSERVED` — a recall
#: whose universe is the set of postings the ranking covered cannot see a
#: relevant posting the ranking missed, which is the one thing recall exists to
#: notice.
EXPERIMENT_RANKING_UNIVERSE_KIND: EvaluationUniverseKind = (
    EvaluationUniverseKind.FROZEN_DATASET_COHORT
)

#: The four ranking questions of v1, in canonical order. Exactly four, and no
#: other K: each cut-off is a decision about what "the top" means, and a K
#: nobody decided on would be a number in a report that no definition supports.
RANKING_EXPERIMENT_QUESTIONS: tuple[tuple[MetricName, int], ...] = (
    (MetricName.PRECISION_AT_K, 5),
    (MetricName.PRECISION_AT_K, 10),
    (MetricName.RECALL_AT_K, 10),
    (MetricName.NDCG_AT_K, 10),
)


class RankingExperimentStatus(StrEnum):
    """What the ranking block is: four metric results, or a stated refusal."""

    COMPUTED = "COMPUTED"
    N_A = "N_A"


class RankingExperimentUnavailableReason(StrEnum):
    """Why the whole ranking block is `N_A`. Exactly one member in v1.

    One member, and the narrowness is deliberate. Every *other* way a ranking
    number can be unknowable — a partially judged top K, a universe that is not
    fully judged, no relevant item, a zero ideal DCG, a labelset that is not the
    one the run is bound to — is already a Phase 10.3 `MetricResult` with a
    Phase 10.3 reason code, and re-stating any of them here would create a
    second vocabulary for one fact. Those arrive as `N_A` metric entries inside
    a `COMPUTED` block, which is exactly what a partially judged round looks
    like.
    """

    #: `RECOMMENDATION_OBSERVED` is `COMPUTED` and empty: the frozen snapshot
    #: holds no recommendation assessment at all, so there is no observed
    #: ranking to evaluate. The only legitimate whole-block refusal, and it is
    #: a statement about the snapshot rather than about the evidence.
    NO_OBSERVED_RECOMMENDATION_RANKING = "NO_OBSERVED_RECOMMENDATION_RANKING"


@dataclass(frozen=True)
class RankingMetricEntry:
    """One of the four questions, and Phase 10.3's own answer to it.

    `result` is the **exact** `MetricResult` Phase 10.3 returned — its status,
    its value, its reason and its support block, unmodified. Phase 10.5 adds no
    digest of its own around it and no second support shape: a metric entry that
    re-stated a numerator would be a second opinion filed under Phase 10.3's
    name.

    `k_requested` is echoed beside it because the entry identifies a *question*,
    and `Precision@5` and `Precision@10` are two questions with one metric name.
    It is required to equal the K the result's own support records.
    """

    metric: MetricName
    k_requested: int
    result: MetricResult


def ranking_metric_entry_payload(entry: RankingMetricEntry) -> dict[str, Any]:
    """The one canonical shape of a metric entry.

    The nested result enters **in full**, through Phase 10.3's own
    `metric_result_payload`, rather than by a digest of its own. That is the one
    place this package departs from "nested artefacts enter by digest", and
    deliberately: a `MetricResult` carries no fingerprint field, and inventing
    one for it here would create a Phase 10.5 identity for a Phase 10.3 artefact
    — a second name for the same thing, free to be computed over a different
    domain. So the ranking result's own fingerprint covers the four payloads
    entire.
    """
    return {
        "metric": str(entry.metric),
        "k_requested": entry.k_requested,
        "result": metric_result_payload(entry.result),
    }


@dataclass(frozen=True)
class RankingExperimentResult:
    """The observed ranking experiment: four questions about one frozen ordering.

    Bound to everything a ranking number means nothing without — the snapshot,
    the person, the projection that fixed which postings the ranking covers, the
    Phase 10.3 evaluation run, its universe, its ranking, its labelset and the
    class of evidence that labelset can support. All of them are inside
    `result_fingerprint`, because a number that is arithmetically right about
    another ranking, another universe or another labelset is not this
    measurement.

    On `N_A / NO_OBSERVED_RECOMMENDATION_RANKING` the five Phase 10.3 bindings
    and the evidence class are `None` and `metric_results` is empty: there is no
    evaluation run, because there was no ranking to build one over. That is the
    one case, and it is a statement about the snapshot.
    """

    result_schema_version: str
    contract_version: str
    status: RankingExperimentStatus
    dataset_id: str
    dataset_content_fingerprint: str
    profile_id: int
    profile_context_fingerprint: str
    #: Which `RECOMMENDATION_OBSERVED` result fixed the ranking's membership.
    #: Always stated, `N_A` included: the emptiness of that projection is
    #: precisely what licenses the refusal, so the refusal names it.
    recommendation_projection_result_fingerprint: str
    result_fingerprint: str
    unavailable_reason: RankingExperimentUnavailableReason | None = None
    evaluation_run_fingerprint: str | None = None
    evaluation_universe_fingerprint: str | None = None
    ranking_fingerprint: str | None = None
    labelset_fingerprint: str | None = None
    evidence_class: EvidenceClass | None = None
    metric_results: tuple[RankingMetricEntry, ...] = ()

    @property
    def computed(self) -> bool:
        return self.status is RankingExperimentStatus.COMPUTED

    def entry(self, metric: MetricName, k_requested: int) -> RankingMetricEntry:
        """One question's entry, or a refusal. Derived, never indexed twice."""
        for item in self.metric_results:
            if item.metric is metric and item.k_requested == k_requested:
                return item
        raise ExperimentBindingError(
            f"this ranking experiment holds no entry for {metric} at K="
            f"{k_requested}"
        )


def ranking_experiment_result_payload(
    result: RankingExperimentResult, *, include_result_fingerprint: bool = True
) -> dict[str, Any]:
    """The ranking result as a structure: the semantic block, and its digest.

    `include_result_fingerprint=False` yields the digest domain, for the same
    reason as above.
    """
    payload: dict[str, Any] = {
        "result_schema_version": result.result_schema_version,
        "contract_version": result.contract_version,
        "status": str(result.status),
        "unavailable_reason": (
            None
            if result.unavailable_reason is None
            else str(result.unavailable_reason)
        ),
        "dataset_id": result.dataset_id,
        "dataset_content_fingerprint": result.dataset_content_fingerprint,
        "profile_id": result.profile_id,
        "profile_context_fingerprint": result.profile_context_fingerprint,
        "recommendation_projection_result_fingerprint": (
            result.recommendation_projection_result_fingerprint
        ),
        "evaluation_run_fingerprint": result.evaluation_run_fingerprint,
        "evaluation_universe_fingerprint": result.evaluation_universe_fingerprint,
        "ranking_fingerprint": result.ranking_fingerprint,
        "labelset_fingerprint": result.labelset_fingerprint,
        "evidence_class": (
            None if result.evidence_class is None else str(result.evidence_class)
        ),
        # In contract order, never sorted: the four questions are a sequence the
        # contract states, and reordering them would be a different report of
        # the same numbers.
        "metric_results": [
            ranking_metric_entry_payload(entry) for entry in result.metric_results
        ],
    }
    if include_result_fingerprint:
        payload["result_fingerprint"] = result.result_fingerprint
    return payload


_RANKING_BINDINGS = (
    "evaluation_run_fingerprint",
    "evaluation_universe_fingerprint",
    "ranking_fingerprint",
    "labelset_fingerprint",
)


def validate_ranking_experiment_result_structure(
    result: Any,
) -> RankingExperimentResult:
    """Re-establish every invariant the ranking result claims, or refuse it.

    The one worth naming is the question set: a `COMPUTED` block holds exactly
    the four `RANKING_EXPERIMENT_QUESTIONS`, in that order, each entry's metric
    and K matching the contract's and the result's own support agreeing about K.
    A fifth question, a missing one or a reordering is a contract failure, not a
    partial run.
    """
    if not isinstance(result, RankingExperimentResult):
        raise ExperimentContractError(
            f"{result!r} is not a ranking experiment result"
        )
    if result.result_schema_version != RANKING_EXPERIMENT_RESULT_SCHEMA_VERSION:
        raise ExperimentContractError(
            f"unsupported ranking experiment result version: "
            f"{result.result_schema_version!r} (this build reads "
            f"{RANKING_EXPERIMENT_RESULT_SCHEMA_VERSION!r})"
        )
    require_supported_experiment_contract_version(result.contract_version)
    _require_member(
        RankingExperimentStatus, result.status, subject="the ranking status"
    )
    _require_text(result.dataset_id, subject="the ranking's dataset id")
    validate_experiment_fingerprint(
        result.dataset_content_fingerprint,
        subject="the ranking's dataset content fingerprint",
    )
    if isinstance(result.profile_id, bool) or not isinstance(result.profile_id, int):
        raise ExperimentBindingError(
            f"the ranking's profile id is not an integer: {result.profile_id!r}"
        )
    if result.profile_id <= 0:
        raise ExperimentBindingError(
            f"the ranking's profile id is not positive: {result.profile_id!r}"
        )
    validate_experiment_fingerprint(
        result.profile_context_fingerprint,
        subject="the ranking's profile context fingerprint",
    )
    validate_experiment_fingerprint(
        result.recommendation_projection_result_fingerprint,
        subject="the ranking's recommendation projection fingerprint",
    )
    validate_experiment_fingerprint(
        result.result_fingerprint, subject="the ranking experiment fingerprint"
    )

    if result.status is RankingExperimentStatus.N_A:
        no_ranking = (
            RankingExperimentUnavailableReason.NO_OBSERVED_RECOMMENDATION_RANKING
        )
        if result.unavailable_reason is not no_ranking:
            raise ExperimentContractError(
                f"the ranking block is N_A and states reason "
                f"{result.unavailable_reason}; the only whole-block refusal this "
                "contract defines is "
                f"{RankingExperimentUnavailableReason.NO_OBSERVED_RECOMMENDATION_RANKING}"
                " — every other unknowable is a Phase 10.3 metric result with a "
                "Phase 10.3 reason"
            )
        for name in _RANKING_BINDINGS:
            if getattr(result, name) is not None:
                raise ExperimentContractError(
                    f"the ranking block is N_A and states {name}="
                    f"{getattr(result, name)!r}; there was no ranking to build an "
                    "evaluation run over, so there is no run to name"
                )
        if result.evidence_class is not None:
            raise ExperimentContractError(
                f"the ranking block is N_A and claims evidence class "
                f"{result.evidence_class}; a claim about what evidence supports "
                "presupposes a measurement"
            )
        if result.metric_results:
            raise ExperimentContractError(
                f"the ranking block is N_A and holds "
                f"{len(result.metric_results)} metric entr(ies)"
            )
        return result

    if result.unavailable_reason is not None:
        raise ExperimentContractError(
            f"the ranking block is COMPUTED and states unavailable reason "
            f"{result.unavailable_reason}"
        )
    for name in _RANKING_BINDINGS:
        validate_experiment_fingerprint(
            getattr(result, name), subject=f"the ranking block's {name}"
        )
    _require_member(
        EvidenceClass, result.evidence_class, subject="the ranking evidence class"
    )
    if len(result.metric_results) != len(RANKING_EXPERIMENT_QUESTIONS):
        raise ExperimentContractError(
            f"the ranking block holds {len(result.metric_results)} metric "
            f"entr(ies) and this contract asks exactly "
            f"{len(RANKING_EXPERIMENT_QUESTIONS)} questions: "
            f"{[f'{metric}@{k}' for metric, k in RANKING_EXPERIMENT_QUESTIONS]}"
        )
    for position, (entry, (metric, k_requested)) in enumerate(
        zip(result.metric_results, RANKING_EXPERIMENT_QUESTIONS), start=1
    ):
        if not isinstance(entry, RankingMetricEntry):
            raise ExperimentContractError(
                f"ranking entry {position} is not a metric entry: {entry!r}"
            )
        if entry.metric is not metric or entry.k_requested != k_requested:
            raise ExperimentContractError(
                f"ranking entry {position} asks {entry.metric}@"
                f"{entry.k_requested} and this contract's question {position} is "
                f"{metric}@{k_requested}; the four questions and their order are "
                "the contract's"
            )
        # **The nested result is re-established, never taken on trust.** A
        # `MetricResult` reaching this contract was built by Phase 10.3 in every
        # honest case — and `object.__new__(MetricResult)` followed by
        # `object.__setattr__` produces one that never ran `__post_init__` and
        # can claim COMPUTED with no value, `N_A` with one, or a support that is
        # not a support at all. A dataclass is not a proof token, so Phase
        # 10.3's own structural validator runs over each of the four before
        # anything here reads a status, a value or a K.
        #
        # It is Phase 10.3's function deliberately: those invariants belong to
        # the package that defines them, and a second copy of "COMPUTED carries
        # a value" living here would be free to drift from the original.
        try:
            validate_metric_result_structure(entry.result)
        except MetricContractError as error:
            raise ExperimentContractError(
                f"ranking entry {position} ({metric}@{k_requested}) does not "
                f"re-establish as a Phase 10.3 metric result: {error}"
            ) from error
        if entry.result.metric is not metric:
            raise ExperimentBindingError(
                f"ranking entry {position} asks {metric} and its result is about "
                f"{entry.result.metric}"
            )
        if entry.result.support.k_requested != k_requested:
            raise ExperimentBindingError(
                f"ranking entry {position} asks K={k_requested} and its result's "
                f"support records K={entry.result.support.k_requested!r}"
            )
    return result


# --------------------------------------------------------------------------
# the persistent context binding
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExperimentRunContextBinding:
    """Which assembly of frozen artefacts a stored run was computed from.

    The **persistent** half of `context.ExperimentRunContext`, which is transient
    and is never stored whole: a run document that republished the frozen
    records, the manifest, the label coverage and the benchmark rows would be a
    copy of every artefact it measured. What is kept is the identity of each —
    enough for a reader to find them and for a verifier to refuse a different
    one, and not one byte more.

    `ranking_evaluation_run_fingerprint` is `None` exactly when the snapshot
    froze no recommendation at all. It is the one nullable binding here, and its
    nullability is the same fact as the ranking block's single `N_A`.
    """

    run_context_schema_version: str
    experiment_contract_version: str
    dataset_id: str
    dataset_content_fingerprint: str
    profile_id: int
    profile_context_fingerprint: str
    business_metric_run_fingerprint: str
    ranking_evaluation_run_fingerprint: str | None
    context_fingerprint: str


def experiment_run_context_binding_payload(
    binding: ExperimentRunContextBinding, *, include_fingerprint: bool = True
) -> dict[str, Any]:
    """The binding's one layout, optionally without its own digest.

    `include_fingerprint=False` yields exactly the domain `fingerprint.py`
    digests — which is why the two cannot drift: there is one layout, and the
    digest is the layout minus a field that is named here rather than
    remembered.
    """
    payload: dict[str, Any] = {
        "run_context_schema_version": binding.run_context_schema_version,
        "experiment_contract_version": binding.experiment_contract_version,
        "dataset_id": binding.dataset_id,
        "dataset_content_fingerprint": binding.dataset_content_fingerprint,
        "profile_id": binding.profile_id,
        "profile_context_fingerprint": binding.profile_context_fingerprint,
        "business_metric_run_fingerprint": binding.business_metric_run_fingerprint,
        "ranking_evaluation_run_fingerprint": (
            binding.ranking_evaluation_run_fingerprint
        ),
    }
    if include_fingerprint:
        payload["context_fingerprint"] = binding.context_fingerprint
    return payload


def _validate_context_binding_structure(
    binding: Any,
) -> ExperimentRunContextBinding:
    """Re-establish the binding's own shape. Its digest is `fingerprint.py`'s."""
    if not isinstance(binding, ExperimentRunContextBinding):
        raise ExperimentContractError(
            f"{binding!r} is not an experiment run context binding"
        )
    if binding.run_context_schema_version != EXPERIMENT_RUN_CONTEXT_SCHEMA_VERSION:
        raise ExperimentContractError(
            f"unsupported experiment run context version: "
            f"{binding.run_context_schema_version!r} (this build reads "
            f"{EXPERIMENT_RUN_CONTEXT_SCHEMA_VERSION!r})"
        )
    require_supported_experiment_contract_version(
        binding.experiment_contract_version
    )
    _require_text(binding.dataset_id, subject="the context's dataset id")
    validate_experiment_fingerprint(
        binding.dataset_content_fingerprint,
        subject="the context's dataset content fingerprint",
    )
    if isinstance(binding.profile_id, bool) or not isinstance(
        binding.profile_id, int
    ):
        raise ExperimentBindingError(
            f"the context's profile id is not an integer: {binding.profile_id!r}"
        )
    if binding.profile_id <= 0:
        raise ExperimentBindingError(
            f"the context's profile id is not positive: {binding.profile_id!r}"
        )
    validate_experiment_fingerprint(
        binding.profile_context_fingerprint,
        subject="the context's profile context fingerprint",
    )
    validate_experiment_fingerprint(
        binding.business_metric_run_fingerprint,
        subject="the context's business metric run fingerprint",
    )
    if binding.ranking_evaluation_run_fingerprint is not None:
        validate_experiment_fingerprint(
            binding.ranking_evaluation_run_fingerprint,
            subject="the context's ranking evaluation run fingerprint",
        )
    validate_experiment_fingerprint(
        binding.context_fingerprint, subject="the experiment run context fingerprint"
    )
    return binding


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExperimentRunProvenance:
    """Where and when a run was assembled. **Outside `run_fingerprint`.**

    Phase 10.1's rule, applied one layer up again: a digest that moved with the
    clock or with a directory would describe the act of running rather than what
    was run. Two operators computing the same experiments from the same frozen
    artefacts, a week and a filesystem apart, produce one run identity — which is
    what makes the content-addressed store able to say UNCHANGED and mean it.

    The paths are **informative**. They are validated as text and nothing here
    asks the filesystem whether they exist: a path says where somebody read a
    file, and a run that was moved to another machine did not thereby become a
    different experiment.
    """

    generated_at: str | None = None
    dataset_directory: str | None = None
    business_metric_run_path: str | None = None
    label_root: str | None = None
    benchmark_records_path: str | None = None

    def __post_init__(self) -> None:
        # The same authority the structural verifier calls — see
        # `validate_experiment_run_provenance`. Constructing one checks it, and
        # so does reading one somebody else constructed.
        validate_experiment_run_provenance(self)


#: The four provenance fields that are paths. `generated_at` is not among them:
#: it is an instant and is held to the instant rule instead.
PROVENANCE_PATH_FIELDS: tuple[str, ...] = (
    "dataset_directory",
    "business_metric_run_path",
    "label_root",
    "benchmark_records_path",
)


def validate_experiment_run_provenance(
    provenance: Any,
) -> ExperimentRunProvenance:
    """Re-establish everything a provenance block claims, or refuse it.

    **One authority, two callers.** `ExperimentRunProvenance.__post_init__`
    calls it so a constructed block is checked, and
    `validate_experiment_run_structure` calls it so a block this process did
    *not* construct is checked too — `object.__new__(ExperimentRunProvenance)`
    followed by `object.__setattr__` never runs `__post_init__`, and a verifier
    that only checked the block's *type* would accept whatever such an object
    happened to hold. The lesson is Phase 10.4's, arrived at the same way.

    `generated_at` is an **instant**: `None`, or an RFC 3339 timestamp with an
    explicit offset, as every other Phase 10 timestamp is spelled. A
    `"not-a-timestamp"` that used to pass as "optional text" is refused here.

    The four paths stay optional, non-empty, unpadded text — and **nothing asks
    the filesystem whether they exist**. A path says where somebody read a file;
    a run that was moved to another machine did not thereby become a different
    experiment, and a verifier that failed on a path that had since been deleted
    would make a frozen artefact unverifiable by accident.

    None of this enters `run_fingerprint`. Validating the provenance does not
    promote it to identity: it stays outside, which is exactly why two runs
    differing only here are one run.
    """
    if not isinstance(provenance, ExperimentRunProvenance):
        raise ExperimentContractError(
            f"{provenance!r} ({type(provenance).__name__}) is not an experiment "
            "run provenance"
        )
    if provenance.generated_at is not None:
        _require_timestamp(
            provenance.generated_at,
            subject="the run provenance's generated_at",
        )
    stated = {field.name for field in fields(ExperimentRunProvenance)}
    if stated != {"generated_at", *PROVENANCE_PATH_FIELDS}:  # pragma: no cover
        raise ExperimentContractError(
            f"the provenance contract states {sorted(stated)}; this validator "
            "covers a different set and would leave a field unchecked"
        )
    for name in PROVENANCE_PATH_FIELDS:
        _require_optional_text(
            getattr(provenance, name),
            subject=f"the run provenance's {name}",
        )
    return provenance


def experiment_run_provenance_payload(
    provenance: ExperimentRunProvenance,
) -> dict[str, Any]:
    """The provenance block's one layout. Digested nowhere."""
    return {
        "generated_at": provenance.generated_at,
        "dataset_directory": provenance.dataset_directory,
        "business_metric_run_path": provenance.business_metric_run_path,
        "label_root": provenance.label_root,
        "benchmark_records_path": provenance.benchmark_records_path,
    }


@dataclass(frozen=True)
class ExperimentRun:
    """One complete experiment run: twelve blocks, all of them, or no run at all.

    Five projections, six overlaps and one ranking experiment. **Twelve**, and
    "complete" is the load-bearing word: `run.build_experiment_run` computes
    every block or raises, and a hard error anywhere leaves no run rather than a
    partial one. There is no selected subset, no optional projection and no
    stored failure.

    An `N_A` block is a **present** result and counts towards the twelve. A run
    whose geography projection could not be computed is a complete run that says
    so.

    **Nothing is duplicated.** There is no global status, no `computed_count`, no
    `na_count`, no total score, no quality score and no second copy of the
    dataset id or the context's digest. Every one of those is derivable from the
    blocks, and a stored copy is a field that can come to disagree with what it
    summarises — which is how a report ends up claiming five projections over a
    list of four. There is also no overall number *by design*: a run is twelve
    separate statements about one cohort, and a single figure summarising them
    would be a score this contract cannot define and nobody could re-derive.

    `provenance` is the one block outside `run_fingerprint`.
    """

    run_schema_version: str
    contract_version: str
    context: ExperimentRunContextBinding
    projections: tuple[ProjectionResult, ...]
    overlaps: tuple[OverlapResult, ...]
    ranking: RankingExperimentResult
    run_fingerprint: str
    provenance: ExperimentRunProvenance = ExperimentRunProvenance()

    def projection(self, projection: CohortProjection) -> ProjectionResult:
        """One projection's result, or a refusal. Derived rather than indexed."""
        _require_member(CohortProjection, projection, subject="the projection")
        for result in self.projections:
            if result.projection is projection:
                return result
        raise ExperimentBindingError(
            f"this run holds no result for projection {projection}"
        )

    def overlap(self, pair: OverlapPair) -> OverlapResult:
        """One overlap's result, or a refusal."""
        _require_member(OverlapPair, pair, subject="the overlap pair")
        for result in self.overlaps:
            if result.pair is pair:
                return result
        raise ExperimentBindingError(f"this run holds no result for overlap {pair}")


def experiment_run_fingerprint_payload(run: ExperimentRun) -> dict[str, Any]:
    """The run's **identity domain**, and deliberately not its document.

    Four things, and the last three are why a run has an identity at all:

        run_schema_version    the layout
        contract_version      the definitions the blocks were produced under
        context_fingerprint   which assembly of frozen artefacts this run had
        the twelve child digests, in canonical order

    Every child enters **by digest**, the way nested artefacts enter every domain
    in Phase 10 — and every one of those digests is *recomputed* before a run is
    sealed or believed, so the identity is the whole of what it says and a
    changed membership moves a child digest and therefore this one.

    **Outside**: the provenance and everything in it, the bytes of `run.json`,
    the directory it lands in, the machine, the OS and the Python version. This
    is not the SHA-256 of a file; it is the identity of an experiment, and the
    same experiment written twice is one run.
    """
    return {
        "run_schema_version": run.run_schema_version,
        "contract_version": run.contract_version,
        "context_fingerprint": run.context.context_fingerprint,
        "projections": [
            {
                "projection": str(result.projection),
                "result_fingerprint": result.result_fingerprint,
            }
            for result in run.projections
        ],
        "overlaps": [
            {
                "pair": str(result.pair),
                "result_fingerprint": result.result_fingerprint,
            }
            for result in run.overlaps
        ],
        "ranking_result_fingerprint": run.ranking.result_fingerprint,
    }


def experiment_run_payload(
    run: ExperimentRun, *, include_provenance: bool = True
) -> dict[str, Any]:
    """The whole run as a structure — the authoritative document of `run.json`.

    Distinct from the identity domain above, and the difference is not a block
    that was left out: the identity holds each child by digest, while the
    document holds each child in full, so that a stored run can be read back
    without the artefacts it was computed from.
    """
    payload: dict[str, Any] = {
        "run_schema_version": run.run_schema_version,
        "contract_version": run.contract_version,
        "context": experiment_run_context_binding_payload(run.context),
        "projections": [
            projection_result_payload(result) for result in run.projections
        ],
        "overlaps": [overlap_result_payload(result) for result in run.overlaps],
        "ranking": ranking_experiment_result_payload(run.ranking),
        "run_fingerprint": run.run_fingerprint,
    }
    if include_provenance:
        payload["provenance"] = experiment_run_provenance_payload(run.provenance)
    return payload


def validate_experiment_run_structure(run: Any) -> ExperimentRun:
    """Re-establish every invariant a run claims about itself, or refuse it.

    The counting rules are the ones this function exists for: exactly five
    projections in `PROJECTION_ORDER`, exactly six overlaps in
    `OVERLAP_PAIR_ORDER`, exactly one ranking block, one contract version across
    the run and every block in it, and one dataset and profile binding
    throughout. A missing block, an extra one and a reordering are each a
    contract failure rather than a partial run.

    It does not recompute a digest — `fingerprint.py` does — and it proves no
    membership: whether a projection's included set is the set the frozen records
    imply needs the records, and that is `run.verify_experiment_run`'s question.
    """
    if not isinstance(run, ExperimentRun):
        raise ExperimentContractError(f"{run!r} is not an experiment run")
    require_supported_experiment_run_schema_version(run.run_schema_version)
    require_supported_experiment_contract_version(run.contract_version)
    binding = _validate_context_binding_structure(run.context)
    if binding.experiment_contract_version != run.contract_version:
        raise ExperimentBindingError(
            f"this run states contract version {run.contract_version!r} and its "
            f"context binding states {binding.experiment_contract_version!r}"
        )
    # Re-established field by field, not merely type-checked: a provenance
    # built through `object.__new__` never ran `__post_init__`, so its own
    # constructor guarantees say nothing about the object in front of us.
    validate_experiment_run_provenance(run.provenance)
    validate_experiment_fingerprint(
        run.run_fingerprint, subject="the experiment run fingerprint"
    )

    if not isinstance(run.projections, tuple) or len(run.projections) != len(
        PROJECTION_ORDER
    ):
        raise ExperimentContractError(
            f"this run holds {len(run.projections)} projection(s) and a complete "
            f"run holds exactly {len(PROJECTION_ORDER)}: "
            f"{[str(item) for item in PROJECTION_ORDER]}"
        )
    for position, (result, expected) in enumerate(
        zip(run.projections, PROJECTION_ORDER), start=1
    ):
        validate_projection_result_structure(result)
        if result.projection is not expected:
            raise ExperimentContractError(
                f"projection {position} of this run is {result.projection} and "
                f"the canonical order states {expected}"
            )

    if not isinstance(run.overlaps, tuple) or len(run.overlaps) != len(
        OVERLAP_PAIR_ORDER
    ):
        raise ExperimentContractError(
            f"this run holds {len(run.overlaps)} overlap(s) and a complete run "
            f"holds exactly {len(OVERLAP_PAIR_ORDER)}: "
            f"{[str(item) for item in OVERLAP_PAIR_ORDER]}"
        )
    for position, (result, expected) in enumerate(
        zip(run.overlaps, OVERLAP_PAIR_ORDER), start=1
    ):
        validate_overlap_result_structure(result)
        if result.pair is not expected:
            raise ExperimentContractError(
                f"overlap {position} of this run is {result.pair} and the "
                f"canonical order states {expected}"
            )

    validate_ranking_experiment_result_structure(run.ranking)

    # One cohort, one person, one set of definitions, across all twelve blocks.
    # Checked here rather than per block, because each block is individually
    # coherent about whatever snapshot it names: a run holding eleven blocks
    # about one dataset and one about another is exactly the document that passes
    # every local check and describes two measurements at once.
    blocks: tuple[tuple[str, Any], ...] = (
        *((f"projection {result.projection}", result) for result in run.projections),
        *((f"overlap {result.pair}", result) for result in run.overlaps),
        ("the ranking block", run.ranking),
    )
    for name, block in blocks:
        if block.contract_version != run.contract_version:
            raise ExperimentBindingError(
                f"{name} states contract version {block.contract_version!r} and "
                f"the run states {run.contract_version!r}"
            )
        if block.dataset_id != binding.dataset_id:
            raise ExperimentBindingError(
                f"{name} is about dataset {block.dataset_id!r} and this run's "
                f"context binds {binding.dataset_id!r}"
            )
        if block.dataset_content_fingerprint != binding.dataset_content_fingerprint:
            raise ExperimentBindingError(
                f"{name} is about dataset content "
                f"{block.dataset_content_fingerprint} and this run's context "
                f"binds {binding.dataset_content_fingerprint}"
            )
    cohort_sizes = {result.cohort_size for result in run.projections} | {
        result.cohort_size for result in run.overlaps
    }
    if len(cohort_sizes) != 1:
        raise ExperimentBindingError(
            f"the blocks of this run state cohort sizes {sorted(cohort_sizes)}; "
            "every projection and every overlap is about one frozen cohort"
        )
    if run.ranking.profile_id != binding.profile_id:
        raise ExperimentBindingError(
            f"the ranking block measures profile {run.ranking.profile_id} and "
            f"this run's context binds {binding.profile_id}"
        )
    if run.ranking.profile_context_fingerprint != (
        binding.profile_context_fingerprint
    ):
        raise ExperimentBindingError(
            f"the ranking block measures profile context "
            f"{run.ranking.profile_context_fingerprint} and this run's context "
            f"binds {binding.profile_context_fingerprint}"
        )
    return run
