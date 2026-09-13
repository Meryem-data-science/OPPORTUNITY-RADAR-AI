"""Where the frozen ranking and the human judgements disagree. A diagnostic view.

`build_error_analysis(run, context)` names the individual postings worth looking
at. It is **derived, not persisted**: no fingerprint, no storage, no place in an
`ExperimentRun`, and nothing here can change a stored result. It full-verifies
the run before it says anything, because a diagnostic drawn from an unverified
document is a list of postings nobody should act on.

## It counts nothing

There is no false-positive rate, no false-negative rate, no inversion rate, no
Kendall tau and no agreement coefficient. Those are *metrics*, the four metrics
of this contract are fixed, and a fifth number computed in a diagnostic view
would be a metric that arrived without a definition, without an availability
gate and without a place in any identity.

What this produces is **candidates**: which postings, and why. A person reads
them.

## UNJUDGED is never an error

The rule this module exists to hold. An unjudged posting in the top K is not a
false positive and an unjudged posting outside it is not a false negative — it
is a question nobody answered. `LabelCoverage.grade` raises for one, so every
lookup here is guarded by `is_judged` and there is no default, no `Optional` and
no zero standing in for an unanswered question.

    false-positive candidate   in the effective top 10, JUDGED, grade < 2
    false-negative candidate   grade >= 2, outside the effective top 10
                               — which includes a posting the Recommendation
                                 run never covered at all

The second is why the cohort matters rather than the ranking: a relevant posting
the pipeline never ranked is the most interesting failure available, and it is
invisible from the recommendation output.

`grade >= 2` is Phase 10.2's binary relevance threshold, read through Phase
10.3's `is_relevant_grade` rather than written down again.

## Ranking inversions

For two postings that are **both ranked and both judged**:

    rank(A) < rank(B) and grade(A) < grade(B)  ->  JUDGED_RANKING_INVERSION

Both conditions on both postings, every time. A pair where either is unjudged is
not an inversion, and a pair where either is unranked has no ranks to compare.
Reported as pairs, in canonical order, and **not** counted into a rate.

## Diagnostics that reuse existing truth, and invent none

**Geography.** `AMBIGUOUS_LOCATION` is kept strictly distinct from
`UNKNOWN_LOCATION`: a resolver that found several places the text could mean did
not fail to find one, and merging them would hide which of the two the pipeline
should fix. Both are read through `evaluation.frozen_facts`, so they are the
same two facts Phase 10.4's `UNKNOWN_LOCATION_RATE` and its
`ambiguous_location_count` are about.

**Sources.** Only facts demonstrable from Phase 10.1's frozen records and Phase
10.4's own bound evidence. No new reason code is invented for a URL: where a
URL's state is in question the analysis reports Phase 10.4's own
`UrlAuditOutcome` and `UrlAuditInconclusiveReason` members, and where no audit
is bound it reports Phase 10.4's own `URL_AUDIT_EVIDENCE_MISSING` — the existing
reason for exactly that absence.

**Skills.** `N_A`, with the existing official truth:
`OPPORTUNITY_SKILLS_NOT_FROZEN`. Per-opportunity skill requirements are not in
the Phase 10.1 record contract, so a skill-level diagnostic has no input. No new
reason code is minted for it — not `SKILL_LEVEL_EVIDENCE_NOT_FROZEN`, not
anything else — because Phase 10.4 already names this absence and a second name
for one fact is how two vocabularies start.

## Deterministic, and bound to its evidence

No sampling, no randomness, no clock, no truncation to "the interesting ones".
Every list is in a canonical deterministic order, so two people running this
over one run get the same lists in the same order. The evidence class, the label
protocol version and the labelset fingerprint are carried on the analysis
itself, because every judgement-dependent diagnostic here is a statement about
*that* labelset and would be a different statement about another.

Human observations about *why* something went wrong — a person's causal reading
— belong in none of this: not in an `ExperimentRun`, not in a fingerprint, not
in a label and not in production.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from evaluation.business_metrics import (
    BusinessMetricName,
    BusinessMetricRun,
    BusinessMetricStatus,
    BusinessMetricUnavailableReason,
    UrlAuditOutcome,
    frozen_action_url,
)
from evaluation.frozen_facts import (
    FrozenSegmentStatus,
    record_geography_of,
)
from evaluation.metrics import (
    EvidenceClass,
    LabelCoverage,
    effective_k,
    is_relevant_grade,
)

from .context import ExperimentRunContext, _VerifiedExperimentContext, _verified
from .run import verify_experiment_run
from .schema import (
    CohortProjection,
    ExperimentBindingError,
    ExperimentContractError,
    ExperimentRun,
    RankingExperimentStatus,
)

__all__ = [
    "ERROR_ANALYSIS_EFFECTIVE_K",
    "ErrorAnalysis",
    "FalseNegativeCandidate",
    "FalsePositiveCandidate",
    "GeoDiagnostics",
    "JudgedRankingInversion",
    "RankingDiagnostics",
    "SkillDiagnostics",
    "SourceDiagnostics",
    "build_error_analysis",
]

#: The cut-off the candidate lists are drawn at. Ten, because that is the
#: deepest cut-off the four contract questions ask about — a diagnostic over a
#: cut-off no metric uses would describe a top nobody measured. The *effective*
#: value is `min(10, ranking.length)`, taken from Phase 10.3's own
#: `effective_k` rather than recomputed here.
ERROR_ANALYSIS_EFFECTIVE_K = 10


class SkillDiagnosticStatus(StrEnum):
    """Whether a skill-level diagnostic could be produced. Two members."""

    COMPUTED = "COMPUTED"
    N_A = "N_A"


# --------------------------------------------------------------------------
# the candidate shapes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FalsePositiveCandidate:
    """A posting the ranking surfaced that a person judged not relevant.

    "Candidate" is the honest word. A grade of 0 or 1 in the effective top 10 is
    a disagreement between the pipeline and one labelset under one protocol; it
    is not proof the pipeline is wrong, and this object deliberately carries no
    verdict field. `rank_position` is the evaluation position, so "top 10" means
    the same thing here as it does in Precision@10.
    """

    opportunity_id: int
    rank_position: int
    relevance_grade: int


@dataclass(frozen=True)
class FalseNegativeCandidate:
    """A posting a person judged relevant that the ranking did not surface.

    `rank_position is None` is the case worth the whole module: the
    Recommendation run **never covered** this posting, so it is not in the
    ranking at all. It is not rank 0 and not last — it is absent, which is the
    candidate false negative the frozen snapshot exists to preserve and which
    cannot be seen from the recommendation output.

    A stated `rank_position` means the posting was ranked but below the
    effective cut-off.
    """

    opportunity_id: int
    relevance_grade: int
    rank_position: int | None


@dataclass(frozen=True)
class JudgedRankingInversion:
    """Two ranked, judged postings ordered against their own grades.

    Both ranked and both judged, always — see the module docstring. Reported as
    a pair and never counted into a rate.
    """

    higher_ranked_opportunity_id: int
    higher_rank_position: int
    higher_relevance_grade: int
    lower_ranked_opportunity_id: int
    lower_rank_position: int
    lower_relevance_grade: int


@dataclass(frozen=True)
class RankingDiagnostics:
    """The ranking's disagreements with one labelset, posting by posting.

    `k_effective` is Phase 10.3's `min(K, ranking.length)` and is stated so a
    reader knows what "the top" meant for a short ranking.

    `unjudged_in_top_k` is carried beside the candidates and is **not** among
    them: those are the postings the analysis could say nothing about, and
    knowing how many there are is how a reader calibrates how much the two
    candidate lists are worth.
    """

    k_requested: int
    k_effective: int
    ranking_length: int
    false_positive_candidates: tuple[FalsePositiveCandidate, ...]
    false_negative_candidates: tuple[FalseNegativeCandidate, ...]
    judged_ranking_inversions: tuple[JudgedRankingInversion, ...]
    unjudged_in_top_k: tuple[int, ...]
    judged_count: int


@dataclass(frozen=True)
class GeoDiagnostics:
    """Why the cohort's locations are what they are. Two distinct failures.

    `ambiguous_location_opportunity_ids` and
    `unknown_location_opportunity_ids` are kept apart because they are different
    failures with different fixes — several possible places versus none at all —
    and they may overlap freely: a posting with one AMBIGUOUS segment and one
    UNKNOWN segment is in both lists, honestly.

    `out_of_target_opportunity_ids` is the excluded set of
    `GEO_NOT_EXPLICITLY_OUT_OF_TARGET`, read off the run's own projection rather
    than re-derived, and `None` when that projection is `N_A` — there is no
    target, so there is nothing to be out of.
    """

    target_country: str | None
    ambiguous_location_opportunity_ids: tuple[int, ...]
    unknown_location_opportunity_ids: tuple[int, ...]
    no_geography_segment_opportunity_ids: tuple[int, ...]
    out_of_target_opportunity_ids: tuple[int, ...] | None


@dataclass(frozen=True)
class SourceDiagnostics:
    """What the frozen records and the bound Phase 10.4 evidence say about sources.

    Facts only, and only demonstrable ones. The per-source counts come from the
    frozen `sources` rows; the URL states come from Phase 10.4's bound
    `UrlAuditBinding` **through its own vocabulary**, and
    `url_evidence_reason` carries Phase 10.4's own
    `URL_AUDIT_EVIDENCE_MISSING` when no audit is bound. No reason code is
    invented here for any of it.
    """

    observed_source_ids: tuple[str, ...]
    records_per_source: Mapping[str, int]
    records_with_no_source_row: tuple[int, ...]
    records_with_no_action_url: tuple[int, ...]
    #: `None` when an audit is bound; otherwise Phase 10.4's own reason for the
    #: absence, reused rather than restated.
    url_evidence_reason: BusinessMetricUnavailableReason | None
    #: Per `UrlAuditOutcome`, how many audited URLs the bound audit recorded.
    #: Empty when no audit is bound.
    url_outcome_counts: Mapping[str, int]
    broken_url_opportunity_ids: tuple[int, ...]


@dataclass(frozen=True)
class SkillDiagnostics:
    """Skill-level analysis, which this build cannot produce. `N_A`, by name.

    The reason is Phase 10.4's existing `OPPORTUNITY_SKILLS_NOT_FROZEN` and no
    new code is minted for it: per-opportunity skill requirements are not in the
    Phase 10.1 record contract, that package already names this exact absence,
    and a second name for one fact is how two vocabularies start.
    """

    status: SkillDiagnosticStatus
    unavailable_reason: BusinessMetricUnavailableReason


@dataclass(frozen=True)
class ErrorAnalysis:
    """The whole diagnostic view of one verified run. **Never persisted.**

    Carries the evidence identity — the class, the protocol and the labelset
    fingerprint — because every judgement-dependent diagnostic in it is a
    statement about *that* labelset and would be a different statement about
    another. `None` for all three when the run's ranking block is `N_A`: there
    was no ranking to evaluate, so there is no evidence to identify.

    `ranking` is likewise `None` then. That is not a missing section: a snapshot
    with no observed recommendation has no ranking disagreements, and the
    geography, source and skill diagnostics below still apply to its cohort.
    """

    dataset_id: str
    dataset_content_fingerprint: str
    profile_id: int
    profile_context_fingerprint: str
    run_fingerprint: str
    context_fingerprint: str
    cohort_size: int
    evidence_class: EvidenceClass | None
    label_protocol_version: str | None
    labelset_fingerprint: str | None
    ranking: RankingDiagnostics | None
    geo: GeoDiagnostics
    sources: SourceDiagnostics
    skills: SkillDiagnostics = field(
        default_factory=lambda: SkillDiagnostics(
            status=SkillDiagnosticStatus.N_A,
            unavailable_reason=(
                BusinessMetricUnavailableReason.OPPORTUNITY_SKILLS_NOT_FROZEN
            ),
        )
    )


# --------------------------------------------------------------------------
# the ranking diagnostics
# --------------------------------------------------------------------------


def _ranking_diagnostics(
    verified: _VerifiedExperimentContext, coverage: LabelCoverage
) -> RankingDiagnostics:
    """The two candidate lists and the inversions, over one effective cut-off."""
    inputs = verified.ranking_inputs
    if inputs is None:  # pragma: no cover - guarded by the caller
        raise ExperimentContractError(
            "ranking diagnostics need the bound Phase 10.3 inputs"
        )
    ranking = inputs.evaluation_run.ranking
    # Phase 10.3's own `min(K, ranking.length)`, not a second implementation.
    k_effective = effective_k(ERROR_ANALYSIS_EFFECTIVE_K, ranking)
    ranked_ids = ranking.opportunity_ids
    top = ranked_ids[:k_effective]
    rank_of = {
        entry.opportunity_id: entry.rank_position for entry in ranking.entries
    }

    false_positives: list[FalsePositiveCandidate] = []
    unjudged_in_top: list[int] = []
    for opportunity_id in top:
        if not coverage.is_judged(opportunity_id):
            # Never a candidate. An unanswered question is not a wrong answer.
            unjudged_in_top.append(opportunity_id)
            continue
        grade = coverage.grade(opportunity_id)
        if not is_relevant_grade(grade):
            false_positives.append(
                FalsePositiveCandidate(
                    opportunity_id=opportunity_id,
                    rank_position=rank_of[opportunity_id],
                    relevance_grade=grade,
                )
            )

    top_set = set(top)
    false_negatives: list[FalseNegativeCandidate] = []
    # Over the **whole cohort**, not over the ranking: a relevant posting the
    # Recommendation run never covered is exactly the case this list exists for,
    # and it is invisible from the ranking.
    for opportunity_id in verified.cohort_ids:
        if opportunity_id in top_set:
            continue
        if not coverage.is_judged(opportunity_id):
            continue
        grade = coverage.grade(opportunity_id)
        if is_relevant_grade(grade):
            false_negatives.append(
                FalseNegativeCandidate(
                    opportunity_id=opportunity_id,
                    relevance_grade=grade,
                    # `None` means the run never ranked it at all.
                    rank_position=rank_of.get(opportunity_id),
                )
            )

    inversions: list[JudgedRankingInversion] = []
    judged_ranked = [
        opportunity_id
        for opportunity_id in ranked_ids
        if coverage.is_judged(opportunity_id)
    ]
    # Pairs in rank order, so `higher` is always the better-ranked of the two
    # and the list is deterministic without a sort key that could be re-chosen.
    for index, higher in enumerate(judged_ranked):
        higher_grade = coverage.grade(higher)
        for lower in judged_ranked[index + 1 :]:
            lower_grade = coverage.grade(lower)
            if higher_grade < lower_grade:
                inversions.append(
                    JudgedRankingInversion(
                        higher_ranked_opportunity_id=higher,
                        higher_rank_position=rank_of[higher],
                        higher_relevance_grade=higher_grade,
                        lower_ranked_opportunity_id=lower,
                        lower_rank_position=rank_of[lower],
                        lower_relevance_grade=lower_grade,
                    )
                )
    return RankingDiagnostics(
        k_requested=ERROR_ANALYSIS_EFFECTIVE_K,
        k_effective=k_effective,
        ranking_length=ranking.length,
        false_positive_candidates=tuple(false_positives),
        false_negative_candidates=tuple(false_negatives),
        judged_ranking_inversions=tuple(inversions),
        unjudged_in_top_k=tuple(unjudged_in_top),
        judged_count=len(coverage.judged_opportunity_ids),
    )


# --------------------------------------------------------------------------
# the geography diagnostics
# --------------------------------------------------------------------------


def _geo_diagnostics(
    verified: _VerifiedExperimentContext, run: ExperimentRun
) -> GeoDiagnostics:
    """The two location failures, kept apart, plus the run's own excluded set."""
    ambiguous: list[int] = []
    unknown: list[int] = []
    no_segments: list[int] = []
    for position, record in enumerate(verified.records, start=1):
        opportunity_id = int(record["opportunity_id"])
        geography = record_geography_of(
            record,
            record_subject=f"frozen record {position}",
            refuse=ExperimentBindingError,
        )
        if geography.has_ambiguous_segment:
            ambiguous.append(opportunity_id)
        if not geography.has_segments:
            no_segments.append(opportunity_id)
            unknown.append(opportunity_id)
        elif FrozenSegmentStatus.UNKNOWN in geography.statuses:
            unknown.append(opportunity_id)

    geo = run.projection(CohortProjection.GEO_NOT_EXPLICITLY_OUT_OF_TARGET)
    if geo.computed:
        included = set(geo.included_ids)
        out_of_target: tuple[int, ...] | None = tuple(
            opportunity_id
            for opportunity_id in verified.cohort_ids
            if opportunity_id not in included
        )
    else:
        # No target, so nothing to be out of. `None` rather than an empty tuple:
        # an empty list would read as "nobody is out of target", which is a
        # finding, and no finding was possible.
        out_of_target = None
    return GeoDiagnostics(
        target_country=verified.target_country,
        ambiguous_location_opportunity_ids=tuple(ambiguous),
        unknown_location_opportunity_ids=tuple(unknown),
        no_geography_segment_opportunity_ids=tuple(no_segments),
        out_of_target_opportunity_ids=out_of_target,
    )


# --------------------------------------------------------------------------
# the source diagnostics
# --------------------------------------------------------------------------


def _source_diagnostics(
    verified: _VerifiedExperimentContext,
) -> SourceDiagnostics:
    """Facts about sources and action URLs, from Phase 10.1 and Phase 10.4 only.

    The per-source counts and the two "no row" / "no URL" lists come from the
    frozen records. The URL states come from Phase 10.4's bound audit, reported
    in **its** vocabulary; where no audit is bound the absence is reported as
    Phase 10.4's own `URL_AUDIT_EVIDENCE_MISSING` rather than as a code invented
    here.
    """
    per_source: dict[str, int] = {}
    no_rows: list[int] = []
    no_url: list[int] = []
    url_owners: dict[str, list[int]] = {}
    for position, record in enumerate(verified.records, start=1):
        opportunity_id = int(record["opportunity_id"])
        rows = record["sources"]
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise ExperimentBindingError(
                f"frozen record {position} states sources={rows!r}, which is "
                "not a sequence"
            )
        if not rows:
            no_rows.append(opportunity_id)
        seen: set[str] = set()
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, Mapping):
                raise ExperimentBindingError(
                    f"frozen record {position} states sources[{index}]={row!r}, "
                    "which is not a mapping"
                )
            source_id = row.get("source_id")
            if not isinstance(source_id, str) or not source_id:
                raise ExperimentBindingError(
                    f"source row {index} of frozen record {position} states no "
                    "source_id"
                )
            # Counted once per record: a posting seen twice at one source is one
            # record of that source, not two.
            if source_id not in seen:
                seen.add(source_id)
                per_source[source_id] = per_source.get(source_id, 0) + 1
        # Phase 10.4's own frozen action URL protocol, reused rather than
        # re-deciding which field a posting's action URL comes from.
        url = frozen_action_url(record)
        if url is None:
            no_url.append(opportunity_id)
        else:
            url_owners.setdefault(url, []).append(opportunity_id)

    audit = verified.business_metric_run.context.url_audit_binding
    outcome_counts: dict[str, int] = {}
    broken: list[int] = []
    reason: BusinessMetricUnavailableReason | None = None
    if audit is None:
        reason = BusinessMetricUnavailableReason.URL_AUDIT_EVIDENCE_MISSING
    else:
        for observation in audit.observations:
            key = str(observation.outcome)
            outcome_counts[key] = outcome_counts.get(key, 0) + 1
            if observation.outcome is UrlAuditOutcome.BROKEN:
                broken.extend(url_owners.get(observation.requested_url, ()))
    return SourceDiagnostics(
        observed_source_ids=tuple(sorted(per_source)),
        records_per_source=dict(sorted(per_source.items())),
        records_with_no_source_row=tuple(no_rows),
        records_with_no_action_url=tuple(no_url),
        url_evidence_reason=reason,
        url_outcome_counts=dict(sorted(outcome_counts.items())),
        broken_url_opportunity_ids=tuple(sorted(set(broken))),
    )


def _skill_diagnostics(run: BusinessMetricRun) -> SkillDiagnostics:
    """`N_A / OPPORTUNITY_SKILLS_NOT_FROZEN`, and the bound run agrees.

    The reason is not asserted from this module's belief about the Phase 10.1
    contract — it is **read back** from the bound Phase 10.4 run's own
    `OPPORTUNITY_SKILL_COVERAGE_RATE`, which is permanently `N_A` for exactly
    this reason. So if that ever changes upstream, this diagnostic fails rather
    than quietly reporting a stale truth.
    """
    expected = BusinessMetricUnavailableReason.OPPORTUNITY_SKILLS_NOT_FROZEN
    key = next(
        (
            item
            for item in run.keys
            if item.metric is BusinessMetricName.OPPORTUNITY_SKILL_COVERAGE
            and item.dimension_value is None
        ),
        None,
    )
    if key is None:
        raise ExperimentBindingError(
            "the bound business metric run answers no "
            f"{BusinessMetricName.OPPORTUNITY_SKILL_COVERAGE}; the skill "
            "diagnostic reports that metric's own official truth rather than "
            "asserting one of its own"
        )
    result = run.result_for(key)
    if result.status is not BusinessMetricStatus.N_A or result.reason is not expected:
        raise ExperimentBindingError(
            f"the bound business metric run states "
            f"{BusinessMetricName.OPPORTUNITY_SKILL_COVERAGE} is "
            f"{result.status}/{result.reason}; the skill diagnostic reuses that "
            f"metric's official {expected} and mints no reason code of its own, "
            "so a change upstream must be a failure here rather than a stale "
            "truth repeated"
        )
    return SkillDiagnostics(
        status=SkillDiagnosticStatus.N_A, unavailable_reason=expected
    )


# --------------------------------------------------------------------------
# the one public surface
# --------------------------------------------------------------------------


def build_error_analysis(
    run: ExperimentRun, context: ExperimentRunContext
) -> ErrorAnalysis:
    """The diagnostic view of one verified run, or a refusal.

    **Always full-verifies first.** A diagnostic drawn from an unverified
    document is a list of postings nobody should act on, and the whole value of
    this view is that a person can trust the ids in it enough to go and read
    those postings.

    Produces no rate and no score of any kind — see the module docstring — and
    reuses the existing official reasons for the two absences it reports.
    """
    verify_experiment_run(run, context)
    verified = _verified(context)

    ranking_diagnostics: RankingDiagnostics | None = None
    evidence_class: EvidenceClass | None = None
    protocol_version: str | None = None
    labelset_fingerprint: str | None = None
    if run.ranking.status is RankingExperimentStatus.COMPUTED:
        inputs = verified.ranking_inputs
        if inputs is None:  # pragma: no cover - refused by the verifier
            raise ExperimentContractError(
                "the run's ranking block is COMPUTED and this context holds no "
                "ranking inputs"
            )
        ranking_diagnostics = _ranking_diagnostics(verified, inputs.label_coverage)
        evidence_class = inputs.evaluation_run.evidence_class
        protocol_version = inputs.evaluation_run.label_protocol_version
        labelset_fingerprint = inputs.evaluation_run.labelset_fingerprint

    return ErrorAnalysis(
        dataset_id=verified.dataset.dataset_id,
        dataset_content_fingerprint=verified.dataset.content_fingerprint,
        profile_id=verified.dataset.profile_id,
        profile_context_fingerprint=verified.dataset.profile_context_fingerprint,
        run_fingerprint=run.run_fingerprint,
        context_fingerprint=run.context.context_fingerprint,
        cohort_size=verified.cohort_size,
        evidence_class=evidence_class,
        label_protocol_version=protocol_version,
        labelset_fingerprint=labelset_fingerprint,
        ranking=ranking_diagnostics,
        geo=_geo_diagnostics(verified, run),
        sources=_source_diagnostics(verified),
        skills=_skill_diagnostics(verified.business_metric_run),
    )
