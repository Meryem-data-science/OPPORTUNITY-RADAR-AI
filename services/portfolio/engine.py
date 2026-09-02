"""Pure, deterministic Portfolio v1 classification policy."""

from __future__ import annotations

import math
import re
from dataclasses import replace
from typing import Iterable

from services.collector.matching import MatchLane
from services.eligibility import GlobalStatus
from services.priority import PriorityCategory

from .fingerprint import portfolio_assessment_fingerprint
from .models import (
    PortfolioAssessment,
    PortfolioBucket,
    PortfolioDisposition,
    PortfolioInput,
    PortfolioInputError,
    PortfolioReasonCode,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CANDIDATES = frozenset(
    {PriorityCategory.URGENT, PriorityCategory.HIGH, PriorityCategory.MEDIUM}
)


def _validate(value: PortfolioInput) -> None:
    for name in ("profile_id", "opportunity_id"):
        item = getattr(value, name)
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise PortfolioInputError(f"{name} must be a positive integer")
    for name in ("required_skill_matched_count", "required_skill_total_count"):
        item = getattr(value, name)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise PortfolioInputError(f"{name} must be a non-negative integer")
    matched = value.required_skill_matched_count
    total = value.required_skill_total_count
    if matched > total:
        raise PortfolioInputError("required skill matched count exceeds total count")
    score = value.required_skill_score
    if total == 0:
        if matched != 0 or score is not None:
            raise PortfolioInputError(
                "missing required-skill evidence must be 0/0 and None"
            )
    else:
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise PortfolioInputError("required skill score is required with evidence")
        stable_score = float(score)
        if not math.isfinite(stable_score) or not 0.0 <= stable_score <= 1.0:
            raise PortfolioInputError(
                "required skill score must be finite within [0, 1]"
            )
        if round(stable_score, 12) != round(matched / total, 12):
            raise PortfolioInputError(
                "required skill score is inconsistent with counts"
            )
    if value.priority_category is not None and not isinstance(
        value.priority_category, PriorityCategory
    ):
        raise PortfolioInputError("invalid priority category")
    if not isinstance(value.eligibility_status, GlobalStatus):
        raise PortfolioInputError("invalid eligibility status")
    if not isinstance(value.matching_lane, MatchLane):
        raise PortfolioInputError("invalid matching lane")
    for name in (
        "priority_engine_version",
        "priority_rules_version",
        "priority_freshness_version",
        "priority_quality_version",
        "matching_engine_version",
        "matching_rules_version",
        "semantic_percentile_version",
    ):
        if (
            not isinstance(getattr(value, name), str)
            or not getattr(value, name).strip()
        ):
            raise PortfolioInputError(f"{name} must be non-empty")
    for name in (
        "priority_assessment_fingerprint",
        "matching_assessment_fingerprint",
    ):
        fingerprint = getattr(value, name)
        if not isinstance(fingerprint, str) or _SHA256.fullmatch(fingerprint) is None:
            raise PortfolioInputError(f"{name} must be a lowercase SHA-256")


def _exclusion_reasons(value: PortfolioInput) -> list[PortfolioReasonCode]:
    reasons = []
    if value.priority_category not in _CANDIDATES:
        reasons.append(PortfolioReasonCode.PRIORITY_NOT_PORTFOLIO_CANDIDATE)
    if value.eligibility_status is GlobalStatus.INELIGIBLE:
        reasons.append(PortfolioReasonCode.ELIGIBILITY_INELIGIBLE)
    if value.matching_lane is MatchLane.OUTSIDE_PREFERENCES:
        reasons.append(PortfolioReasonCode.MATCHING_OUTSIDE_PREFERENCES)
    return reasons


def build_portfolio_assessment(value: PortfolioInput) -> PortfolioAssessment:
    """Classify one validated upstream snapshot without I/O or ambient state."""
    _validate(value)
    reasons = _exclusion_reasons(value)
    disposition = (
        PortfolioDisposition.EXCLUDED if reasons else PortfolioDisposition.INCLUDED
    )
    bucket = None
    safe_cap_applied = False
    if not reasons:
        matched = value.required_skill_matched_count
        total = value.required_skill_total_count
        if total == 0:
            bucket = PortfolioBucket.TARGET
            reasons.append(PortfolioReasonCode.REQUIRED_SKILL_EVIDENCE_MISSING)
        elif matched == total:
            bucket = PortfolioBucket.SAFE
            reasons.append(PortfolioReasonCode.REQUIRED_SKILLS_FULL_COVERAGE)
        elif matched * 3 >= total * 2:
            bucket = PortfolioBucket.TARGET
            reasons.append(PortfolioReasonCode.REQUIRED_SKILLS_TARGET_COVERAGE)
        else:
            bucket = PortfolioBucket.AMBITIOUS
            reasons.append(PortfolioReasonCode.REQUIRED_SKILLS_AMBITIOUS_GAP)
        if bucket is PortfolioBucket.SAFE:
            if value.eligibility_status is GlobalStatus.UNKNOWN:
                reasons.append(PortfolioReasonCode.SAFE_CAP_ELIGIBILITY_UNKNOWN)
            if value.matching_lane is MatchLane.UNCERTAIN:
                reasons.append(PortfolioReasonCode.SAFE_CAP_MATCHING_UNCERTAIN)
            if len(reasons) > 1:
                bucket = PortfolioBucket.TARGET
                safe_cap_applied = True
    assessment_values = value.__dict__ | {
        "required_skill_score": (
            None
            if value.required_skill_score is None
            else float(value.required_skill_score)
        )
    }
    assessment = PortfolioAssessment(
        **assessment_values,
        disposition=disposition,
        bucket=bucket,
        safe_cap_applied=safe_cap_applied,
        reason_codes=tuple(reasons),
    )
    return replace(
        assessment,
        assessment_fingerprint=portfolio_assessment_fingerprint(assessment),
    )


def build_portfolio_assessments(
    inputs: Iterable[PortfolioInput],
) -> tuple[PortfolioAssessment, ...]:
    """Classify a non-empty, single-profile batch in opportunity-ID order."""
    values = tuple(inputs)
    if not values:
        raise PortfolioInputError("portfolio batch must not be empty")
    for value in values:
        _validate(value)
    if len({value.profile_id for value in values}) != 1:
        raise PortfolioInputError("portfolio batch must contain one profile_id")
    if len({value.opportunity_id for value in values}) != len(values):
        raise PortfolioInputError("duplicate opportunity_id in portfolio batch")
    return tuple(
        build_portfolio_assessment(value)
        for value in sorted(values, key=lambda item: item.opportunity_id)
    )
