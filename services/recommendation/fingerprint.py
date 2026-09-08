"""Canonical SHA-256 fingerprints for recommendation results.

Same shape as Matching, Eligibility and Priority: a canonical payload holding
every value the result actually depends on, serialized with the project's one
`canonical_json`, digested with SHA-256.

What the payload deliberately excludes is as much of the contract as what it
includes. No timestamp, no run id, no database path, no profile or opportunity
id, and no collection whose order is accidental: the reason codes are sorted
because their partition, not their emission order, is the statement. The
ranking, by contrast, *is* a product of this phase, so the batch payload keeps
the ranked fingerprints in rank order — and because the ranking is a pure
function of content, shuffling the batch's input still yields the same digest.
"""

from __future__ import annotations

import hashlib
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from .models import RecommendationAssessment, RecommendationBatchResult

__all__ = [
    "canonical_recommendation_assessment_payload",
    "canonical_recommendation_batch_payload",
    "recommendation_assessment_fingerprint",
    "recommendation_batch_fingerprint",
]


def canonical_recommendation_assessment_payload(
    assessment: RecommendationAssessment,
) -> dict[str, Any]:
    """Return every business-relevant value, and no identity or runtime data."""
    return {
        "versions": {
            "recommendation_engine": assessment.recommendation_engine_version,
            "recommendation_rules": assessment.recommendation_rules_version,
            "matching_engine": assessment.matching_engine_version,
            "matching_rules": assessment.matching_rules_version,
            "semantic_percentile": assessment.semantic_percentile_version,
            "role_domain_preferences": assessment.role_domain_preferences_version,
            "geographic_resolver": assessment.geographic_resolver_version,
            "eligibility_engine": assessment.eligibility_engine_version,
            "fine_domain_bridge": assessment.domain.fine_domain_bridge_version,
        },
        "weights": {
            "required_skill": assessment.required_skill.base_weight,
            "semantic": assessment.semantic.base_weight,
            "domain": assessment.domain.base_weight,
        },
        "required_skill": {
            "status": assessment.required_skill.status.value,
            "score": assessment.required_skill.score,
            "matched_count": assessment.required_skill.matched_count,
            "total_count": assessment.required_skill.total_count,
        },
        "semantic": {
            "status": assessment.semantic.status.value,
            "score": assessment.semantic.score,
            "semantic_status": assessment.semantic.semantic_status.value,
        },
        "domain": {
            "status": assessment.domain.status.value,
            "score": assessment.domain.score,
            "source": assessment.domain.source.value,
            "fit_status": assessment.domain.fit_status.value,
            "fit_reason": assessment.domain.fit_reason,
            "preferred_rank": assessment.domain.preferred_rank,
            "fine_primary_category": (
                None
                if assessment.domain.fine_primary_category is None
                else assessment.domain.fine_primary_category.value
            ),
            "fine_classifier_version": assessment.domain.fine_classifier_version,
        },
        "opportunity_type": {
            "status": assessment.opportunity_type.status.value,
            "reason": assessment.opportunity_type.reason.value,
        },
        "work_mode": {
            "status": assessment.work_mode.status.value,
            "reason": assessment.work_mode.reason.value,
            "opportunity_mode": assessment.work_mode.opportunity_mode,
        },
        "geography": {
            "state": assessment.geography.state.value,
            "target_country": assessment.geography.target_country,
            "mobility_rule_id": assessment.geography.mobility_rule_id,
            "verdict_rule_id": assessment.geography.verdict_rule_id,
            "segments": assessment.geography.segments,
            "resolved_segments": assessment.geography.resolved_segments,
            "matching_segments": assessment.geography.matching_segments,
        },
        "eligibility": {
            "status": assessment.eligibility.status.value,
            "upstream_fingerprint": assessment.eligibility.upstream_fingerprint,
            "engine_version": assessment.eligibility.engine_version,
        },
        "baseline": {
            "match_quality": assessment.baseline_match_quality,
            "evidence_coverage": assessment.baseline_evidence_coverage,
            "lane": assessment.baseline_matching_lane.value,
            "assessment_fingerprint": (
                assessment.baseline_matching_assessment_fingerprint
            ),
        },
        "result": {
            "recommendation_score": assessment.recommendation_score,
            "recommendation_evidence_coverage": (
                assessment.recommendation_evidence_coverage
            ),
            "disposition": assessment.disposition.value,
            "strengths": sorted(code.value for code in assessment.strengths),
            "confirmed_gaps": sorted(code.value for code in assessment.confirmed_gaps),
            "unknowns": sorted(code.value for code in assessment.unknowns),
        },
    }


def recommendation_assessment_fingerprint(
    assessment: RecommendationAssessment,
) -> str:
    payload = canonical_recommendation_assessment_payload(assessment)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def canonical_recommendation_batch_payload(
    batch: RecommendationBatchResult,
) -> dict[str, Any]:
    """Return the batch's own statement: its versions, its size, and its order.

    The fingerprints are listed **in rank order** because the ranking is what
    this phase produces. Two batches assembled from the same content in a
    different sequence rank identically and therefore fingerprint identically;
    two batches that rank differently cannot.
    """
    return {
        "recommendation_engine_version": batch.recommendation_engine_version,
        "recommendation_rules_version": batch.recommendation_rules_version,
        "assessment_count": batch.assessment_count,
        "ranked_assessment_fingerprints": [
            item.assessment_fingerprint for item in batch.assessments
        ],
    }


def recommendation_batch_fingerprint(batch: RecommendationBatchResult) -> str:
    payload = canonical_recommendation_batch_payload(batch)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
