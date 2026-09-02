"""Pure, deterministic Phase 5.1A priority policies."""

from __future__ import annotations

import math
from dataclasses import replace

from services.collector.matching import MatchLane
from services.collector.qualification.taxonomy import ListingQuality
from services.eligibility import GlobalStatus

from .fingerprint import priority_assessment_fingerprint
from .models import (
    ComponentStatus,
    DeadlineStatus,
    FreshnessPriorityComponent,
    MatchPriorityComponent,
    PriorityAssessment,
    PriorityCategory,
    PriorityInput,
    PriorityInputError,
    PriorityReasonCode,
    QualityPriorityComponent,
)

MATCH_QUALITY_WEIGHT = 0.50
FRESHNESS_WEIGHT = 0.30
OPPORTUNITY_QUALITY_WEIGHT = 0.20
HIGH_MINIMUM_COVERAGE = 0.70

if MATCH_QUALITY_WEIGHT + FRESHNESS_WEIGHT + OPPORTUNITY_QUALITY_WEIGHT != 1.0:
    raise RuntimeError("priority engine weights must total exactly 1.0")

QUALITY_SCORES = {
    ListingQuality.NORMAL_LISTING: 1.00,
    ListingQuality.INSUFFICIENT_CONTENT: 0.50,
    ListingQuality.POSSIBLE_NON_JOB_PAGE: 0.25,
}


def _stable_ratio(value: float, label: str) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise PriorityInputError(f"{label} must be finite and within [0, 1]")
    return round(value, 12)


def freshness_score(published_at, evaluation_date) -> tuple[float | None, int | None]:
    """Score only explicit publication age; never consult a clock or fallback field."""
    if published_at is None:
        return None, None
    age_days = (evaluation_date - published_at).days
    if age_days < 0:
        raise PriorityInputError("published_at cannot be after evaluation_date")
    if age_days <= 2:
        return 1.0, age_days
    if age_days <= 7:
        return 0.85, age_days
    if age_days <= 14:
        return 0.65, age_days
    if age_days <= 30:
        return 0.40, age_days
    if age_days <= 60:
        return 0.20, age_days
    return 0.10, age_days


def _base_category(score: float | None) -> PriorityCategory | None:
    if score is None:
        return None
    if score >= 0.75:
        return PriorityCategory.HIGH
    if score >= 0.50:
        return PriorityCategory.MEDIUM
    return PriorityCategory.LOW


def _component_status(score: float | None) -> ComponentStatus:
    return ComponentStatus.MISSING if score is None else ComponentStatus.AVAILABLE


def build_priority_assessment(inputs: PriorityInput) -> PriorityAssessment:
    """Aggregate upstream assessments without recalculating either one."""
    matching, eligibility = inputs.matching, inputs.eligibility
    expected = (matching.profile_id, matching.opportunity_id)
    if (eligibility.profile_id, eligibility.opportunity_id) != expected:
        raise PriorityInputError("matching and eligibility identity mismatch")
    if not matching.assessment_fingerprint:
        raise PriorityInputError("matching assessment fingerprint must be non-empty")
    matching_versions = (
        matching.matching_engine_version,
        matching.matching_rules_version,
        matching.semantic_percentile_version,
    )
    if any(not version.strip() for version in matching_versions):
        raise PriorityInputError("matching provenance versions must be non-empty")
    if not eligibility.input_fingerprint or not eligibility.engine_version:
        raise PriorityInputError("eligibility provenance must be non-empty")

    match_score = (
        None
        if matching.match_quality is None
        else _stable_ratio(matching.match_quality, "match_quality")
    )
    matching_coverage = _stable_ratio(
        matching.evidence_coverage, "matching evidence_coverage"
    )
    fresh_score, age_days = freshness_score(inputs.published_at, inputs.evaluation_date)
    quality_score = (
        None
        if inputs.publication_quality is None
        else QUALITY_SCORES[inputs.publication_quality]
    )
    components = (
        (match_score, MATCH_QUALITY_WEIGHT),
        (fresh_score, FRESHNESS_WEIGHT),
        (quality_score, OPPORTUNITY_QUALITY_WEIGHT),
    )
    available_weight = _stable_ratio(
        sum(weight for score, weight in components if score is not None),
        "priority evidence coverage",
    )
    weighted_sum = sum(
        score * weight for score, weight in components if score is not None
    )
    score = (
        None
        if available_weight == 0
        else _stable_ratio(weighted_sum / available_weight, "priority score")
    )
    category = _base_category(score)
    reasons = [
        PriorityReasonCode.MATCH_MISSING
        if match_score is None
        else PriorityReasonCode.MATCH_AVAILABLE,
        PriorityReasonCode.FRESHNESS_MISSING
        if fresh_score is None
        else PriorityReasonCode.FRESHNESS_AVAILABLE,
        PriorityReasonCode.OPPORTUNITY_QUALITY_MISSING
        if quality_score is None
        else PriorityReasonCode.OPPORTUNITY_QUALITY_AVAILABLE,
        {
            GlobalStatus.ELIGIBLE: PriorityReasonCode.ELIGIBILITY_ELIGIBLE,
            GlobalStatus.UNKNOWN: PriorityReasonCode.ELIGIBILITY_UNKNOWN,
            GlobalStatus.INELIGIBLE: PriorityReasonCode.KNOWN_ELIGIBILITY_BLOCKER,
        }[eligibility.status],
        {
            MatchLane.PRIMARY: PriorityReasonCode.MATCHING_LANE_PRIMARY,
            MatchLane.UNCERTAIN: PriorityReasonCode.MATCHING_LANE_UNCERTAIN,
            MatchLane.OUTSIDE_PREFERENCES: (
                PriorityReasonCode.MATCHING_LANE_OUTSIDE_PREFERENCES
            ),
        }[matching.lane],
    ]
    if score is None:
        reasons.append(PriorityReasonCode.NO_NUMERIC_EVIDENCE)

    coverage_guardrail = (
        category is PriorityCategory.HIGH and available_weight < HIGH_MINIMUM_COVERAGE
    )
    if coverage_guardrail:
        category = PriorityCategory.MEDIUM
        reasons.append(PriorityReasonCode.COVERAGE_HIGH_GUARDRAIL)

    days_to_deadline = (
        None
        if inputs.deadline is None
        else (inputs.deadline - inputs.evaluation_date).days
    )
    if days_to_deadline is None:
        deadline_status = DeadlineStatus.MISSING
        reasons.append(PriorityReasonCode.DEADLINE_MISSING)
    elif days_to_deadline < 0:
        deadline_status = DeadlineStatus.EXPIRED
        reasons.append(PriorityReasonCode.DEADLINE_EXPIRED_BLOCKER)
    elif days_to_deadline <= 3:
        deadline_status = DeadlineStatus.IMMINENT
        reasons.append(PriorityReasonCode.DEADLINE_IMMINENT)
    else:
        deadline_status = DeadlineStatus.OPEN
        reasons.append(PriorityReasonCode.DEADLINE_OPEN)

    hard_blocker = None
    if eligibility.status is GlobalStatus.INELIGIBLE:
        hard_blocker = PriorityReasonCode.KNOWN_ELIGIBILITY_BLOCKER
        category = PriorityCategory.IGNORE
    if deadline_status is DeadlineStatus.EXPIRED:
        hard_blocker = PriorityReasonCode.DEADLINE_EXPIRED_BLOCKER
        category = PriorityCategory.IGNORE

    outside_cap = (
        hard_blocker is None
        and matching.lane is MatchLane.OUTSIDE_PREFERENCES
        and category in (PriorityCategory.MEDIUM, PriorityCategory.HIGH)
    )
    if outside_cap:
        category = PriorityCategory.LOW
        reasons.append(PriorityReasonCode.OUTSIDE_PREFERENCES_CAP)

    urgent = (
        hard_blocker is None
        and matching.lane is not MatchLane.OUTSIDE_PREFERENCES
        and deadline_status is DeadlineStatus.IMMINENT
        and category in (PriorityCategory.MEDIUM, PriorityCategory.HIGH)
    )
    if urgent:
        category = PriorityCategory.URGENT
        reasons.append(PriorityReasonCode.URGENT_PROMOTION)

    assessment = PriorityAssessment(
        profile_id=matching.profile_id,
        opportunity_id=matching.opportunity_id,
        priority_score=score,
        priority_evidence_coverage=available_weight,
        priority_category=category,
        match=MatchPriorityComponent(
            _component_status(match_score),
            match_score,
            MATCH_QUALITY_WEIGHT,
            matching_coverage,
            matching.lane,
            matching.assessment_fingerprint,
            matching.matching_engine_version,
            matching.matching_rules_version,
            matching.semantic_percentile_version,
        ),
        freshness=FreshnessPriorityComponent(
            _component_status(fresh_score),
            fresh_score,
            FRESHNESS_WEIGHT,
            inputs.published_at,
            age_days,
        ),
        opportunity_quality=QualityPriorityComponent(
            _component_status(quality_score),
            quality_score,
            OPPORTUNITY_QUALITY_WEIGHT,
            inputs.publication_quality,
        ),
        eligibility_status=eligibility.status,
        matching_lane=matching.lane,
        published_at=inputs.published_at,
        deadline=inputs.deadline,
        evaluation_date=inputs.evaluation_date,
        deadline_status=deadline_status,
        coverage_guardrail_applied=coverage_guardrail,
        hard_blocker=hard_blocker,
        outside_preferences_cap_applied=outside_cap,
        urgent_promotion_applied=urgent,
        reason_codes=tuple(reasons),
        matching_upstream_fingerprint=matching.assessment_fingerprint,
        eligibility_upstream_fingerprint=eligibility.input_fingerprint,
        eligibility_engine_version=eligibility.engine_version,
    )
    return replace(
        assessment,
        assessment_fingerprint=priority_assessment_fingerprint(assessment),
    )
