"""Canonical SHA-256 fingerprints for priority assessments."""

from __future__ import annotations

import hashlib
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from .models import PriorityAssessment


def canonical_priority_assessment_payload(
    assessment: PriorityAssessment,
) -> dict[str, Any]:
    """Return all business-relevant content and no runtime/persistence metadata."""
    return {
        "versions": {
            "priority_engine": assessment.priority_engine_version,
            "priority_rules": assessment.priority_rules_version,
            "freshness": assessment.freshness_version,
            "quality": assessment.quality_version,
        },
        "dates": {
            "evaluation_date": assessment.evaluation_date.isoformat(),
            "published_at": (
                None
                if assessment.published_at is None
                else assessment.published_at.isoformat()
            ),
            "deadline": (
                None if assessment.deadline is None else assessment.deadline.isoformat()
            ),
        },
        "match": {
            "status": assessment.match.status.value,
            "score": assessment.match.score,
            "base_weight": assessment.match.base_weight,
            "evidence_coverage": assessment.match.evidence_coverage,
            "lane": assessment.match.lane.value,
            "upstream_fingerprint": assessment.match.upstream_fingerprint,
            "matching_engine_version": assessment.match.matching_engine_version,
            "matching_rules_version": assessment.match.matching_rules_version,
            "semantic_percentile_version": assessment.match.semantic_percentile_version,
        },
        "freshness": {
            "status": assessment.freshness.status.value,
            "score": assessment.freshness.score,
            "base_weight": assessment.freshness.base_weight,
            "age_days": assessment.freshness.age_days,
            "version": assessment.freshness.version,
        },
        "opportunity_quality": {
            "status": assessment.opportunity_quality.status.value,
            "score": assessment.opportunity_quality.score,
            "base_weight": assessment.opportunity_quality.base_weight,
            "publication_quality": (
                None
                if assessment.opportunity_quality.publication_quality is None
                else assessment.opportunity_quality.publication_quality.value
            ),
            "version": assessment.opportunity_quality.version,
        },
        "eligibility": {
            "status": assessment.eligibility_status.value,
            "input_fingerprint": assessment.eligibility_upstream_fingerprint,
            "engine_version": assessment.eligibility_engine_version,
        },
        "result": {
            "priority_score": assessment.priority_score,
            "priority_evidence_coverage": assessment.priority_evidence_coverage,
            "priority_category": (
                None
                if assessment.priority_category is None
                else assessment.priority_category.value
            ),
            "deadline_status": assessment.deadline_status.value,
            "coverage_guardrail_applied": assessment.coverage_guardrail_applied,
            "hard_blocker": (
                None
                if assessment.hard_blocker is None
                else assessment.hard_blocker.value
            ),
            "outside_preferences_cap_applied": (
                assessment.outside_preferences_cap_applied
            ),
            "urgent_promotion_applied": assessment.urgent_promotion_applied,
            "reason_codes": sorted(code.value for code in assessment.reason_codes),
        },
    }


def priority_assessment_fingerprint(assessment: PriorityAssessment) -> str:
    payload = canonical_priority_assessment_payload(assessment)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
