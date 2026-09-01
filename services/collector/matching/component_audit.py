"""Pure component observations and read-only SQLite audit orchestration."""

from __future__ import annotations

import math
import statistics
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence

from services.collector.database.connection import connect_readonly_database

from .component_audit_fingerprint import (
    component_audit_fingerprint,
    component_observation_fingerprint,
)
from .inputs import load_opportunity_matching_input, load_profile_matching_input
from .models import MatchingInput, MatchingOpportunityInput, MatchingProfileInput
from .role_domain_preferences import (
    AlignmentStatus,
    build_role_domain_preference_signals,
)
from .role_domain_preferences_fingerprint import role_domain_preferences_fingerprint
from .skill_fit import build_skill_fit
from .skill_fit_fingerprint import skill_fit_fingerprint
from .skill_signals import RequirementsState, build_opportunity_skill_signals
from .tfidf_fingerprint import semantic_similarity_fingerprint
from .tfidf_similarity import (
    SemanticSimilarityStatus,
    fit_tfidf_corpus,
    score_profile_against_tfidf_corpus,
)

COMPONENT_AUDIT_VERSION = "component-audit-v1"
COMPONENT_AUDIT_SELECTION_VERSION = "component-audit-selection-v1"
SELECTION_QUALIFICATION_VALUES = ("ADJACENT_TARGET", "CORE_TARGET")


class ComponentAuditInputError(ValueError):
    """Raised when component snapshots cannot safely be combined."""


def _stable(value: float) -> float:
    if not math.isfinite(value):
        raise ArithmeticError("audit floats must be finite")
    return round(value, 12)


@dataclass(frozen=True)
class NumericDistribution:
    count: int
    zero_count: int
    partial_count: int
    full_count: int
    distinct_value_count: int
    minimum: float | None
    p25: float | None
    median: float | None
    p75: float | None
    p90: float | None
    maximum: float | None
    mean: float | None
    standard_deviation: float | None


def numeric_distribution(
    values: Iterable[float | int | None], *, ratios: bool = True
) -> NumericDistribution:
    items = sorted(_stable(float(value)) for value in values if value is not None)
    if not items:
        return NumericDistribution(
            0, 0, 0, 0, 0, None, None, None, None, None, None, None, None
        )

    def percentile(p: float) -> float:
        return items[max(0, math.ceil(p * len(items)) - 1)]

    return NumericDistribution(
        len(items),
        sum(v == 0 for v in items),
        sum(0 < v < 1 for v in items) if ratios else 0,
        sum(v == 1 for v in items) if ratios else 0,
        len(set(items)),
        items[0],
        percentile(0.25),
        _stable(statistics.median(items)),
        percentile(0.75),
        percentile(0.9),
        items[-1],
        _stable(statistics.fmean(items)),
        _stable(statistics.pstdev(items)),
    )


@dataclass(frozen=True)
class Correlation:
    sample_count: int
    pearson_r: float | None


def pearson_correlation(
    pairs: Iterable[tuple[float | None, float | None]],
) -> Correlation:
    rows = [(float(x), float(y)) for x, y in pairs if x is not None and y is not None]
    if len(rows) < 2:
        return Correlation(len(rows), None)
    xs, ys = zip(*rows)
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    dx, dy = [x - mx for x in xs], [y - my for y in ys]
    denominator = math.sqrt(sum(x * x for x in dx) * sum(y * y for y in dy))
    return Correlation(
        len(rows),
        None
        if denominator == 0
        else _stable(sum(x * y for x, y in zip(dx, dy, strict=True)) / denominator),
    )


@dataclass(frozen=True)
class MatchingComponentObservation:
    profile_id: int
    opportunity_id: int
    requirements_state: str
    required_matched: int
    required_total: int
    required_coverage: float | None
    preferred_matched: int
    preferred_total: int
    preferred_coverage: float | None
    context_matched: int
    context_total: int
    context_overlap: float | None
    semantic_status: str
    semantic_similarity: float | None
    shared_term_count: int
    opportunity_type_status: str
    opportunity_type_reason: str
    domain_status: str
    domain_reason: str
    preferred_domain_rank: int | None
    work_mode_status: str
    work_mode_reason: str
    skill_fit_fingerprint: str
    semantic_similarity_fingerprint: str
    role_domain_preferences_fingerprint: str


@dataclass(frozen=True)
class SkillSummary:
    requirements_unknown: int
    requirements_extracted: int
    required_denominator_zero: int
    required_denominator_positive: int
    required_skills_total: int
    required_skills_matched: int
    required_skills_missing: int
    preferred_denominator_zero: int
    preferred_denominator_positive: int
    preferred_skills_total: int
    preferred_skills_matched: int
    preferred_skills_missing: int
    context_denominator_zero: int
    context_denominator_positive: int
    context_skills_total: int
    context_skills_matched: int
    context_skills_unmatched: int
    required_coverage: NumericDistribution
    preferred_coverage: NumericDistribution
    context_overlap: NumericDistribution


@dataclass(frozen=True)
class SemanticSummary:
    available: int
    empty_profile_document: int
    empty_opportunity_document: int
    similarity_zero: int
    similarity_positive: int
    similarity: NumericDistribution
    shared_term_count: NumericDistribution


@dataclass(frozen=True)
class AlignmentSummary:
    status_counts: tuple[tuple[str, int], ...]
    reason_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class StructuredSummary:
    opportunity_type: AlignmentSummary
    domain: AlignmentSummary
    work_mode: AlignmentSummary
    domain_rank_1: int
    domain_rank_2: int
    domain_rank_3_plus: int
    domain_rank: NumericDistribution


@dataclass(frozen=True)
class ComponentAuditReport:
    component_audit_version: str
    selection_version: str
    selection_qualification_values: tuple[str, ...]
    observation_count: int
    corpus_fingerprint: str
    tfidf_model_fingerprint: str
    tfidf_version: str
    sklearn_version: str
    skill_summary: SkillSummary
    semantic_summary: SemanticSummary
    structured_summary: StructuredSummary
    evidence_availability_histogram: tuple[tuple[int, int], ...]
    pairwise_correlations: tuple[tuple[str, Correlation], ...]
    observations: tuple[MatchingComponentObservation, ...]
    audit_fingerprint: str
    total_changes_before: int = 0
    total_changes_after: int = 0


def _alignment(
    observations: Sequence[MatchingComponentObservation], prefix: str
) -> AlignmentSummary:
    statuses = Counter(getattr(item, f"{prefix}_status") for item in observations)
    reasons = Counter(getattr(item, f"{prefix}_reason") for item in observations)
    return AlignmentSummary(
        tuple((s, statuses[s]) for s in ("MATCH", "MISMATCH", "UNKNOWN")),
        tuple(sorted(reasons.items())),
    )


def audit_matching_components(
    profile: MatchingProfileInput, opportunities: Sequence[MatchingOpportunityInput]
) -> ComponentAuditReport:
    ordered = tuple(sorted(opportunities, key=lambda item: item.opportunity_id))
    ids = [item.opportunity_id for item in ordered]
    if len(ids) != len(set(ids)):
        raise ComponentAuditInputError("duplicate opportunity_id")
    corpus = fit_tfidf_corpus(ordered)
    semantics = {
        item.opportunity_id: item
        for item in score_profile_against_tfidf_corpus(profile, corpus)
    }
    observations = []
    for opportunity in ordered:
        matching = MatchingInput(profile, opportunity)
        fit = build_skill_fit(matching, build_opportunity_skill_signals(opportunity))
        semantic = semantics[opportunity.opportunity_id]
        structured = build_role_domain_preference_signals(matching)
        expected = (profile.profile_id, opportunity.opportunity_id)
        if any(
            (item.profile_id, item.opportunity_id) != expected
            for item in (fit, semantic, structured)
        ):
            raise ComponentAuditInputError("component profile/opportunity ID mismatch")
        observations.append(
            MatchingComponentObservation(
                *expected,
                fit.requirements_state.value,
                fit.required_coverage.matched_count,
                fit.required_coverage.total_count,
                fit.required_coverage.ratio,
                fit.preferred_coverage.matched_count,
                fit.preferred_coverage.total_count,
                fit.preferred_coverage.ratio,
                fit.context_overlap.matched_count,
                fit.context_overlap.total_count,
                fit.context_overlap.ratio,
                semantic.status.value,
                semantic.similarity,
                semantic.shared_term_count,
                structured.opportunity_type.status.value,
                structured.opportunity_type.reason.value,
                structured.domain.status.value,
                structured.domain.reason.value,
                structured.domain.preferred_rank,
                structured.work_mode.status.value,
                structured.work_mode.reason.value,
                skill_fit_fingerprint(fit),
                semantic_similarity_fingerprint(semantic),
                role_domain_preferences_fingerprint(structured),
            )
        )
    obs = tuple(observations)

    def totals(prefix: str) -> tuple[int, int, int, int, int]:
        total = sum(getattr(o, f"{prefix}_total") for o in obs)
        matched = sum(getattr(o, f"{prefix}_matched") for o in obs)
        return (
            sum(getattr(o, f"{prefix}_total") == 0 for o in obs),
            sum(getattr(o, f"{prefix}_total") > 0 for o in obs),
            total,
            matched,
            total - matched,
        )

    req, pref, context = totals("required"), totals("preferred"), totals("context")
    skill = SkillSummary(
        sum(o.requirements_state == RequirementsState.UNKNOWN.value for o in obs),
        sum(o.requirements_state == RequirementsState.EXTRACTED.value for o in obs),
        *req,
        *pref,
        *context,
        numeric_distribution(o.required_coverage for o in obs),
        numeric_distribution(o.preferred_coverage for o in obs),
        numeric_distribution(o.context_overlap for o in obs),
    )
    semantic_values = [o.semantic_similarity for o in obs]
    semantic = SemanticSummary(
        sum(o.semantic_status == SemanticSimilarityStatus.AVAILABLE.value for o in obs),
        sum(
            o.semantic_status == SemanticSimilarityStatus.EMPTY_PROFILE_DOCUMENT.value
            for o in obs
        ),
        sum(
            o.semantic_status
            == SemanticSimilarityStatus.EMPTY_OPPORTUNITY_DOCUMENT.value
            for o in obs
        ),
        sum(v == 0 for v in semantic_values if v is not None),
        sum(v > 0 for v in semantic_values if v is not None),
        numeric_distribution(semantic_values),
        numeric_distribution((o.shared_term_count for o in obs), ratios=False),
    )
    ranks = [
        o.preferred_domain_rank
        for o in obs
        if o.domain_status == AlignmentStatus.MATCH.value
    ]
    structured = StructuredSummary(
        _alignment(obs, "opportunity_type"),
        _alignment(obs, "domain"),
        _alignment(obs, "work_mode"),
        sum(r == 1 for r in ranks),
        sum(r == 2 for r in ranks),
        sum(r is not None and r >= 3 for r in ranks),
        numeric_distribution(ranks, ratios=False),
    )
    histogram = Counter(
        sum(
            (
                o.required_coverage is not None,
                o.preferred_coverage is not None,
                o.context_overlap is not None,
                o.semantic_status == SemanticSimilarityStatus.AVAILABLE.value,
                o.opportunity_type_status != AlignmentStatus.UNKNOWN.value,
                o.domain_status != AlignmentStatus.UNKNOWN.value,
                o.work_mode_status != AlignmentStatus.UNKNOWN.value,
            )
        )
        for o in obs
    )
    pairs = (
        ("required_vs_semantic", "required_coverage", "semantic_similarity"),
        ("preferred_vs_semantic", "preferred_coverage", "semantic_similarity"),
        ("context_vs_semantic", "context_overlap", "semantic_similarity"),
        ("required_vs_context", "required_coverage", "context_overlap"),
    )
    correlations = tuple(
        (name, pearson_correlation((getattr(o, left), getattr(o, right)) for o in obs))
        for name, left, right in pairs
    )
    fingerprints = [component_observation_fingerprint(o) for o in obs]
    audit_fp = component_audit_fingerprint(
        corpus_fingerprint=corpus.corpus_fingerprint,
        model_fingerprint=corpus.model_fingerprint,
        observation_fingerprints=fingerprints,
    )
    return ComponentAuditReport(
        COMPONENT_AUDIT_VERSION,
        COMPONENT_AUDIT_SELECTION_VERSION,
        SELECTION_QUALIFICATION_VALUES,
        len(obs),
        corpus.corpus_fingerprint,
        corpus.model_fingerprint,
        corpus.tfidf_version,
        corpus.sklearn_version,
        skill,
        semantic,
        structured,
        tuple((i, histogram[i]) for i in range(8)),
        correlations,
        obs,
        audit_fp,
    )


def audit_matching_database(
    database: str | Path, profile_id: int
) -> ComponentAuditReport:
    connection = connect_readonly_database(database)
    try:
        connection.execute("PRAGMA query_only = ON")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise ComponentAuditInputError("query_only mode is required")
        before = connection.total_changes
        profile = load_profile_matching_input(connection, profile_id)
        rows = connection.execute(
            """SELECT o.id FROM opportunities o JOIN opportunity_qualifications q ON q.opportunity_id=o.id WHERE o.is_active=1 AND o.status!='merged_duplicate' AND q.qualification IN ('CORE_TARGET','ADJACENT_TARGET') ORDER BY o.id"""
        ).fetchall()
        report = audit_matching_components(
            profile,
            tuple(
                load_opportunity_matching_input(connection, int(row[0])) for row in rows
            ),
        )
        after = connection.total_changes
        if after != before:
            raise ComponentAuditInputError("read-only audit changed the database")
        return replace(report, total_changes_before=before, total_changes_after=after)
    finally:
        connection.close()
