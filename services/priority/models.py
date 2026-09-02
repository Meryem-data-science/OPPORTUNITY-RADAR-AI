"""Immutable value objects for the pure Phase 5.1A priority engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from services.collector.matching import MatchLane, MatchingAssessment
from services.collector.qualification.taxonomy import ListingQuality
from services.eligibility import EligibilityDecision, GlobalStatus

PRIORITY_ENGINE_VERSION = "priority-engine-v1"
PRIORITY_RULES_VERSION = "priority-rules-v1"
PRIORITY_FRESHNESS_VERSION = "priority-freshness-v1"
PRIORITY_QUALITY_VERSION = "priority-quality-v1"


class PriorityInputError(ValueError):
    """Raised when upstream snapshots cannot be prioritized safely."""


class PriorityCategory(StrEnum):
    URGENT = "URGENT"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    IGNORE = "IGNORE"


class ComponentStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    MISSING = "MISSING"


class DeadlineStatus(StrEnum):
    MISSING = "MISSING"
    EXPIRED = "EXPIRED"
    IMMINENT = "IMMINENT"
    OPEN = "OPEN"


class PriorityReasonCode(StrEnum):
    MATCH_AVAILABLE = "MATCH_AVAILABLE"
    MATCH_MISSING = "MATCH_MISSING"
    FRESHNESS_AVAILABLE = "FRESHNESS_AVAILABLE"
    FRESHNESS_MISSING = "FRESHNESS_MISSING"
    OPPORTUNITY_QUALITY_AVAILABLE = "OPPORTUNITY_QUALITY_AVAILABLE"
    OPPORTUNITY_QUALITY_MISSING = "OPPORTUNITY_QUALITY_MISSING"
    ELIGIBILITY_ELIGIBLE = "ELIGIBILITY_ELIGIBLE"
    ELIGIBILITY_UNKNOWN = "ELIGIBILITY_UNKNOWN"
    KNOWN_ELIGIBILITY_BLOCKER = "KNOWN_ELIGIBILITY_BLOCKER"
    MATCHING_LANE_PRIMARY = "MATCHING_LANE_PRIMARY"
    MATCHING_LANE_UNCERTAIN = "MATCHING_LANE_UNCERTAIN"
    MATCHING_LANE_OUTSIDE_PREFERENCES = "MATCHING_LANE_OUTSIDE_PREFERENCES"
    OUTSIDE_PREFERENCES_CAP = "OUTSIDE_PREFERENCES_CAP"
    COVERAGE_HIGH_GUARDRAIL = "COVERAGE_HIGH_GUARDRAIL"
    DEADLINE_MISSING = "DEADLINE_MISSING"
    DEADLINE_OPEN = "DEADLINE_OPEN"
    DEADLINE_EXPIRED_BLOCKER = "DEADLINE_EXPIRED_BLOCKER"
    DEADLINE_IMMINENT = "DEADLINE_IMMINENT"
    URGENT_PROMOTION = "URGENT_PROMOTION"
    NO_NUMERIC_EVIDENCE = "NO_NUMERIC_EVIDENCE"


@dataclass(frozen=True)
class PriorityInput:
    matching: MatchingAssessment
    eligibility: EligibilityDecision
    publication_quality: ListingQuality | None
    published_at: date | None
    deadline: date | None
    evaluation_date: date


@dataclass(frozen=True)
class NumericComponent:
    status: ComponentStatus
    score: float | None
    base_weight: float


@dataclass(frozen=True)
class MatchPriorityComponent(NumericComponent):
    evidence_coverage: float
    lane: MatchLane
    upstream_fingerprint: str
    matching_engine_version: str
    matching_rules_version: str
    semantic_percentile_version: str


@dataclass(frozen=True)
class FreshnessPriorityComponent(NumericComponent):
    published_at: date | None
    age_days: int | None
    version: str = PRIORITY_FRESHNESS_VERSION


@dataclass(frozen=True)
class QualityPriorityComponent(NumericComponent):
    publication_quality: ListingQuality | None
    version: str = PRIORITY_QUALITY_VERSION


@dataclass(frozen=True)
class PriorityAssessment:
    profile_id: int
    opportunity_id: int
    priority_score: float | None
    priority_evidence_coverage: float
    priority_category: PriorityCategory | None
    match: MatchPriorityComponent
    freshness: FreshnessPriorityComponent
    opportunity_quality: QualityPriorityComponent
    eligibility_status: GlobalStatus
    matching_lane: MatchLane
    published_at: date | None
    deadline: date | None
    evaluation_date: date
    deadline_status: DeadlineStatus
    coverage_guardrail_applied: bool
    hard_blocker: PriorityReasonCode | None
    outside_preferences_cap_applied: bool
    urgent_promotion_applied: bool
    reason_codes: tuple[PriorityReasonCode, ...]
    matching_upstream_fingerprint: str
    eligibility_upstream_fingerprint: str
    eligibility_engine_version: str
    priority_engine_version: str = PRIORITY_ENGINE_VERSION
    priority_rules_version: str = PRIORITY_RULES_VERSION
    freshness_version: str = PRIORITY_FRESHNESS_VERSION
    quality_version: str = PRIORITY_QUALITY_VERSION
    assessment_fingerprint: str = ""
