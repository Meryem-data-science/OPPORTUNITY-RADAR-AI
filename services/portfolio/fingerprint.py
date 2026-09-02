"""Canonical, identity-independent Portfolio assessment fingerprints."""

from __future__ import annotations

import hashlib
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from .models import PortfolioAssessment


def canonical_portfolio_assessment_payload(
    assessment: PortfolioAssessment,
) -> dict[str, Any]:
    """Return business meaning, excluding operational identities and metadata."""
    return {
        "versions": {
            "portfolio_engine": assessment.portfolio_engine_version,
            "portfolio_rules": assessment.portfolio_rules_version,
            "priority_engine": assessment.priority_engine_version,
            "priority_rules": assessment.priority_rules_version,
            "priority_freshness": assessment.priority_freshness_version,
            "priority_quality": assessment.priority_quality_version,
            "matching_engine": assessment.matching_engine_version,
            "matching_rules": assessment.matching_rules_version,
            "semantic_percentile": assessment.semantic_percentile_version,
        },
        "upstream": {
            "priority_assessment_fingerprint": assessment.priority_assessment_fingerprint,
            "matching_assessment_fingerprint": assessment.matching_assessment_fingerprint,
            "priority_category": (
                None
                if assessment.priority_category is None
                else assessment.priority_category.value
            ),
            "eligibility_status": assessment.eligibility_status.value,
            "matching_lane": assessment.matching_lane.value,
            "required_skill": {
                "score": assessment.required_skill_score,
                "matched_count": assessment.required_skill_matched_count,
                "total_count": assessment.required_skill_total_count,
            },
        },
        "result": {
            "disposition": assessment.disposition.value,
            "bucket": None if assessment.bucket is None else assessment.bucket.value,
            "safe_cap_applied": assessment.safe_cap_applied,
            "reason_codes": [code.value for code in assessment.reason_codes],
        },
    }


def portfolio_assessment_fingerprint(assessment: PortfolioAssessment) -> str:
    payload = canonical_portfolio_assessment_payload(assessment)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
