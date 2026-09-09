"""Deterministic, batch-first matching engine assembled from Phase 4 signals."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Mapping, Sequence

from .engine_fingerprint import (
    matching_assessment_fingerprint,
    matching_batch_fingerprint,
)
from .models import (
    MATCHING_INPUT_VERSION,
    MatchingInput,
    MatchingOpportunityInput,
    MatchingProfileInput,
)
from .role_domain_preferences import (
    AlignmentReason,
    AlignmentStatus,
    RoleDomainPreferencesResult,
    build_role_domain_preference_signals,
)
from .role_domain_preferences_fingerprint import role_domain_preferences_fingerprint
from .skill_fit import SkillFitResult, build_skill_fit
from .skill_fit_fingerprint import skill_fit_fingerprint
from .skill_signals import build_opportunity_skill_signals
from .tfidf_fingerprint import (
    SEMANTIC_BINDING_VERSION,
    semantic_binding_fingerprint,
    semantic_similarity_fingerprint,
)
from .tfidf_similarity import (
    SemanticSimilarityResult,
    SemanticSimilarityStatus,
    fit_tfidf_corpus,
    score_profile_against_tfidf_corpus,
)

MATCHING_ENGINE_VERSION = "matching-engine-v1"
MATCHING_RULES_VERSION = "matching-rules-v1"
SEMANTIC_PERCENTILE_VERSION = "semantic-percentile-v1"

REQUIRED_SKILL_WEIGHT = 0.50
SEMANTIC_WEIGHT = 0.30
DOMAIN_WEIGHT = 0.20

if REQUIRED_SKILL_WEIGHT + SEMANTIC_WEIGHT + DOMAIN_WEIGHT != 1.0:
    raise RuntimeError("matching engine weights must total exactly 1.0")


class MatchingEngineInputError(ValueError):
    """Raised when component snapshots cannot safely be assembled."""


class MatchLane(StrEnum):
    PRIMARY = "PRIMARY"
    UNCERTAIN = "UNCERTAIN"
    OUTSIDE_PREFERENCES = "OUTSIDE_PREFERENCES"


@dataclass(frozen=True)
class MatchingScoreComponent:
    normalized_score: float | None
    matched_count: int
    total_count: int
    base_weight: float
    upstream_fingerprint: str


@dataclass(frozen=True)
class SemanticScoreComponent:
    status: SemanticSimilarityStatus
    raw_similarity: float | None
    percentile: float | None
    base_weight: float
    semantic_similarity_fingerprint: str
    corpus_fingerprint: str
    model_fingerprint: str
    semantic_percentile_version: str = SEMANTIC_PERCENTILE_VERSION


@dataclass(frozen=True)
class DomainScoreComponent:
    status: AlignmentStatus
    reason: AlignmentReason
    preferred_rank: int | None
    normalized_score: float | None
    base_weight: float
    upstream_fingerprint: str


@dataclass(frozen=True)
class OpportunityTypeCompatibility:
    status: AlignmentStatus
    reason: AlignmentReason


@dataclass(frozen=True)
class SupportingSkillEvidence:
    ratio: float | None
    matched_count: int
    total_count: int


@dataclass(frozen=True)
class MatchingAssessment:
    profile_id: int
    opportunity_id: int
    lane: MatchLane
    match_quality: float | None
    evidence_coverage: float
    required_skill: MatchingScoreComponent
    semantic: SemanticScoreComponent
    domain: DomainScoreComponent
    opportunity_type: OpportunityTypeCompatibility
    preferred_skill: SupportingSkillEvidence
    context_skill: SupportingSkillEvidence
    matching_engine_version: str = MATCHING_ENGINE_VERSION
    matching_rules_version: str = MATCHING_RULES_VERSION
    semantic_percentile_version: str = SEMANTIC_PERCENTILE_VERSION
    assessment_fingerprint: str = ""


@dataclass(frozen=True)
class MatchingBatchResult:
    """One profile's whole cohort, plus the provenance of the corpus behind it.

    `corpus_fingerprint` and `semantic_binding_fingerprint` are two different
    statements about the same documents and both are carried: the first says
    *this is the same corpus content*, the second says *each document is still
    attached to the same posting*. Only the first can be derived from the
    fitted model, which is why the second exists.

    Both binding fields are optional and default to `None`. That is the legacy
    shape — a batch assembled before this provenance existed — and it is
    preserved so historical runs stay readable and auditable rather than being
    retroactively declared corrupt.
    """

    assessments: tuple[MatchingAssessment, ...]
    corpus_fingerprint: str
    tfidf_model_fingerprint: str
    assessment_count: int
    matching_engine_version: str = MATCHING_ENGINE_VERSION
    matching_rules_version: str = MATCHING_RULES_VERSION
    semantic_percentile_version: str = SEMANTIC_PERCENTILE_VERSION
    semantic_binding_version: str | None = None
    semantic_binding_fingerprint: str | None = None
    batch_fingerprint: str = ""


def _stable_ratio(value: float, label: str) -> float:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise MatchingEngineInputError(f"{label} must be finite and within [0, 1]")
    return round(value, 12)


def semantic_percentiles(
    results: Sequence[SemanticSimilarityResult],
) -> dict[int, float | None]:
    """Return whole-cohort mid-rank percentiles keyed by opportunity ID."""
    ids = [item.opportunity_id for item in results]
    if len(ids) != len(set(ids)):
        raise MatchingEngineInputError("duplicate semantic opportunity_id")
    available: list[float] = []
    for result in results:
        if result.status is SemanticSimilarityStatus.AVAILABLE:
            if result.similarity is None:
                raise MatchingEngineInputError(
                    "AVAILABLE semantic result needs similarity"
                )
            available.append(_stable_ratio(result.similarity, "semantic similarity"))
        elif result.similarity is not None:
            raise MatchingEngineInputError("unavailable semantic result has similarity")
    n = len(available)
    output: dict[int, float | None] = {}
    for result in results:
        if result.status is not SemanticSimilarityStatus.AVAILABLE:
            output[result.opportunity_id] = None
            continue
        value = _stable_ratio(float(result.similarity), "semantic similarity")
        percentile = (
            0.5
            if n == 1
            else (
                sum(candidate < value for candidate in available)
                + (sum(candidate == value for candidate in available) - 1) / 2
            )
            / (n - 1)
        )
        output[result.opportunity_id] = _stable_ratio(percentile, "semantic percentile")
    return output


def _lane(status: AlignmentStatus) -> MatchLane:
    return {
        AlignmentStatus.MATCH: MatchLane.PRIMARY,
        AlignmentStatus.UNKNOWN: MatchLane.UNCERTAIN,
        AlignmentStatus.MISMATCH: MatchLane.OUTSIDE_PREFERENCES,
    }[status]


def _validate_pair(result: object, profile_id: int, opportunity_id: int) -> None:
    if (
        getattr(result, "profile_id", None),
        getattr(result, "opportunity_id", None),
    ) != (
        profile_id,
        opportunity_id,
    ):
        raise MatchingEngineInputError("component profile/opportunity ID mismatch")


def _domain_score(
    profile: MatchingProfileInput, result: RoleDomainPreferencesResult
) -> float | None:
    domain = result.domain
    if domain.status is AlignmentStatus.MISMATCH:
        return 0.0
    if domain.status is AlignmentStatus.UNKNOWN:
        return None
    preferences = profile.preferences
    count = 0 if preferences is None else len(preferences.preferred_domains)
    rank = domain.preferred_rank
    if count <= 0 or rank is None or not 1 <= rank <= count:
        raise MatchingEngineInputError("domain MATCH requires a valid preferred rank")
    return _stable_ratio((count - rank + 1) / count, "domain score")


def build_matching_assessment(
    profile: MatchingProfileInput,
    opportunity: MatchingOpportunityInput,
    skill_fit: SkillFitResult,
    semantic_result: SemanticSimilarityResult,
    role_domain_preferences: RoleDomainPreferencesResult,
    semantic_percentile: float | None,
) -> MatchingAssessment:
    """Assemble already-computed upstream results, with strict pair validation."""
    expected = (profile.profile_id, opportunity.opportunity_id)
    for result in (skill_fit, semantic_result, role_domain_preferences):
        _validate_pair(result, *expected)
    versions = (
        skill_fit.matching_input_version,
        skill_fit.skill_signal_version,
        skill_fit.skill_fit_version,
        semantic_result.semantic_document_version,
        semantic_result.tfidf_version,
        semantic_result.sklearn_version,
        semantic_result.corpus_fingerprint,
        semantic_result.model_fingerprint,
        role_domain_preferences.matching_input_version,
        role_domain_preferences.role_domain_preferences_version,
        role_domain_preferences.opportunity_type_bridge_version,
        role_domain_preferences.domain_preference_bridge_version,
        role_domain_preferences.work_mode_bridge_version,
    )
    if any(not value for value in versions):
        raise MatchingEngineInputError(
            "upstream versions and fingerprints must be non-empty"
        )

    required_ratio = skill_fit.required_coverage.ratio
    required_score = (
        None
        if required_ratio is None
        else _stable_ratio(required_ratio, "required skill score")
    )
    if semantic_result.status is SemanticSimilarityStatus.AVAILABLE:
        if semantic_result.similarity is None:
            raise MatchingEngineInputError(
                "AVAILABLE semantic result needs raw similarity"
            )
        _stable_ratio(semantic_result.similarity, "semantic similarity")
        if semantic_percentile is None:
            raise MatchingEngineInputError("AVAILABLE semantic result needs percentile")
        semantic_score = _stable_ratio(semantic_percentile, "semantic percentile")
    else:
        if semantic_result.similarity is not None:
            raise MatchingEngineInputError(
                "unavailable semantic result has raw similarity"
            )
        if semantic_percentile is not None:
            raise MatchingEngineInputError("unavailable semantic result has percentile")
        semantic_score = None
    domain_score = _domain_score(profile, role_domain_preferences)

    components = (
        (required_score, REQUIRED_SKILL_WEIGHT),
        (semantic_score, SEMANTIC_WEIGHT),
        (domain_score, DOMAIN_WEIGHT),
    )
    available_weight = sum(weight for score, weight in components if score is not None)
    weighted_sum = sum(
        score * weight for score, weight in components if score is not None
    )
    coverage = _stable_ratio(available_weight, "evidence coverage")
    quality = (
        None
        if available_weight == 0
        else _stable_ratio(weighted_sum / available_weight, "match quality")
    )
    structured_fp = role_domain_preferences_fingerprint(role_domain_preferences)
    assessment = MatchingAssessment(
        profile_id=profile.profile_id,
        opportunity_id=opportunity.opportunity_id,
        lane=_lane(role_domain_preferences.opportunity_type.status),
        match_quality=quality,
        evidence_coverage=coverage,
        required_skill=MatchingScoreComponent(
            required_score,
            skill_fit.required_coverage.matched_count,
            skill_fit.required_coverage.total_count,
            REQUIRED_SKILL_WEIGHT,
            skill_fit_fingerprint(skill_fit),
        ),
        semantic=SemanticScoreComponent(
            semantic_result.status,
            semantic_result.similarity,
            semantic_score,
            SEMANTIC_WEIGHT,
            semantic_similarity_fingerprint(semantic_result),
            semantic_result.corpus_fingerprint,
            semantic_result.model_fingerprint,
        ),
        domain=DomainScoreComponent(
            role_domain_preferences.domain.status,
            role_domain_preferences.domain.reason,
            role_domain_preferences.domain.preferred_rank,
            domain_score,
            DOMAIN_WEIGHT,
            structured_fp,
        ),
        opportunity_type=OpportunityTypeCompatibility(
            role_domain_preferences.opportunity_type.status,
            role_domain_preferences.opportunity_type.reason,
        ),
        preferred_skill=SupportingSkillEvidence(
            skill_fit.preferred_coverage.ratio,
            skill_fit.preferred_coverage.matched_count,
            skill_fit.preferred_coverage.total_count,
        ),
        context_skill=SupportingSkillEvidence(
            skill_fit.context_overlap.ratio,
            skill_fit.context_overlap.matched_count,
            skill_fit.context_overlap.total_count,
        ),
    )
    return replace(
        assessment,
        assessment_fingerprint=matching_assessment_fingerprint(assessment),
    )


def build_matching_assessments(
    profile: MatchingProfileInput,
    opportunities: Sequence[MatchingOpportunityInput],
) -> MatchingBatchResult:
    """Build deterministic assessments using exactly one corpus fit and score pass."""
    if not opportunities:
        raise MatchingEngineInputError("opportunity corpus must not be empty")
    ordered = tuple(sorted(opportunities, key=lambda item: item.opportunity_id))
    ids = [item.opportunity_id for item in ordered]
    if len(ids) != len(set(ids)):
        raise MatchingEngineInputError("duplicate opportunity_id")

    corpus = fit_tfidf_corpus(ordered)
    semantic_results = score_profile_against_tfidf_corpus(profile, corpus)
    if any(
        item.corpus_fingerprint != corpus.corpus_fingerprint
        or item.model_fingerprint != corpus.model_fingerprint
        for item in semantic_results
    ):
        raise MatchingEngineInputError(
            "semantic results do not share the fitted corpus/model fingerprints"
        )
    semantic_by_id: Mapping[int, SemanticSimilarityResult] = {
        item.opportunity_id: item for item in semantic_results
    }
    if set(semantic_by_id) != set(ids):
        raise MatchingEngineInputError(
            "semantic results do not match opportunity cohort"
        )
    percentiles = semantic_percentiles(semantic_results)
    assessments: list[MatchingAssessment] = []
    for opportunity in ordered:
        matching_input = MatchingInput(profile, opportunity, MATCHING_INPUT_VERSION)
        skill_fit = build_skill_fit(
            matching_input, build_opportunity_skill_signals(opportunity)
        )
        structured = build_role_domain_preference_signals(matching_input)
        assessments.append(
            build_matching_assessment(
                profile,
                opportunity,
                skill_fit,
                semantic_by_id[opportunity.opportunity_id],
                structured,
                percentiles[opportunity.opportunity_id],
            )
        )
    result = MatchingBatchResult(
        assessments=tuple(assessments),
        corpus_fingerprint=corpus.corpus_fingerprint,
        tfidf_model_fingerprint=corpus.model_fingerprint,
        assessment_count=len(assessments),
        semantic_binding_version=SEMANTIC_BINDING_VERSION,
        # The documents the corpus was fitted over, reused: no second fit, no
        # second normalization, and no second pass over the postings.
        semantic_binding_fingerprint=semantic_binding_fingerprint(corpus.documents),
    )
    # The batch fingerprint stays identity-independent and therefore does not
    # see either binding field. The binding is protected by the *run*
    # fingerprint, which is the layer that already owns operational ids.
    return replace(result, batch_fingerprint=matching_batch_fingerprint(result))
