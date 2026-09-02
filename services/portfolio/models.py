"""Immutable contracts for the pure Phase 5.2A portfolio classifier.

The product's SPONTANEOUS concept is deliberately deferred to its future
networking/application phase; it is not an active Portfolio v1 bucket.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from services.collector.matching import MatchLane
from services.eligibility import GlobalStatus
from services.priority import PriorityCategory

PORTFOLIO_ENGINE_VERSION = "portfolio-engine-v1"
PORTFOLIO_RULES_VERSION = "portfolio-rules-v1"


class PortfolioInputError(ValueError):
    """Raised when upstream snapshots cannot be classified safely."""


class PortfolioBucket(StrEnum):
    SAFE = "SAFE"
    TARGET = "TARGET"
    AMBITIOUS = "AMBITIOUS"


class PortfolioDisposition(StrEnum):
    INCLUDED = "INCLUDED"
    EXCLUDED = "EXCLUDED"


class PortfolioReasonCode(StrEnum):
    PRIORITY_NOT_PORTFOLIO_CANDIDATE = "PRIORITY_NOT_PORTFOLIO_CANDIDATE"
    ELIGIBILITY_INELIGIBLE = "ELIGIBILITY_INELIGIBLE"
    MATCHING_OUTSIDE_PREFERENCES = "MATCHING_OUTSIDE_PREFERENCES"
    REQUIRED_SKILLS_FULL_COVERAGE = "REQUIRED_SKILLS_FULL_COVERAGE"
    REQUIRED_SKILLS_TARGET_COVERAGE = "REQUIRED_SKILLS_TARGET_COVERAGE"
    REQUIRED_SKILLS_AMBITIOUS_GAP = "REQUIRED_SKILLS_AMBITIOUS_GAP"
    REQUIRED_SKILL_EVIDENCE_MISSING = "REQUIRED_SKILL_EVIDENCE_MISSING"
    SAFE_CAP_ELIGIBILITY_UNKNOWN = "SAFE_CAP_ELIGIBILITY_UNKNOWN"
    SAFE_CAP_MATCHING_UNCERTAIN = "SAFE_CAP_MATCHING_UNCERTAIN"


@dataclass(frozen=True)
class PortfolioInput:
    profile_id: int
    opportunity_id: int
    priority_category: PriorityCategory | None
    eligibility_status: GlobalStatus
    matching_lane: MatchLane
    required_skill_score: float | None
    required_skill_matched_count: int
    required_skill_total_count: int
    priority_assessment_fingerprint: str
    matching_assessment_fingerprint: str
    priority_engine_version: str
    priority_rules_version: str
    priority_freshness_version: str
    priority_quality_version: str
    matching_engine_version: str
    matching_rules_version: str
    semantic_percentile_version: str


@dataclass(frozen=True)
class PortfolioAssessment:
    profile_id: int
    opportunity_id: int
    disposition: PortfolioDisposition
    bucket: PortfolioBucket | None
    priority_category: PriorityCategory | None
    eligibility_status: GlobalStatus
    matching_lane: MatchLane
    required_skill_score: float | None
    required_skill_matched_count: int
    required_skill_total_count: int
    safe_cap_applied: bool
    reason_codes: tuple[PortfolioReasonCode, ...]
    priority_assessment_fingerprint: str
    matching_assessment_fingerprint: str
    priority_engine_version: str
    priority_rules_version: str
    priority_freshness_version: str
    priority_quality_version: str
    matching_engine_version: str
    matching_rules_version: str
    semantic_percentile_version: str
    portfolio_engine_version: str = PORTFOLIO_ENGINE_VERSION
    portfolio_rules_version: str = PORTFOLIO_RULES_VERSION
    assessment_fingerprint: str = ""
