"""Canonical identity-independent fingerprints for matching engine output."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from .fingerprint import canonical_json

if TYPE_CHECKING:
    from .engine import MatchingAssessment, MatchingBatchResult


def canonical_matching_assessment_payload(
    assessment: MatchingAssessment,
) -> dict[str, Any]:
    """Return semantic assessment content, deliberately excluding technical IDs."""
    return {
        "matching_engine_version": assessment.matching_engine_version,
        "matching_rules_version": assessment.matching_rules_version,
        "semantic_percentile_version": assessment.semantic_percentile_version,
        "weights": {
            "required_skill": assessment.required_skill.base_weight,
            "semantic": assessment.semantic.base_weight,
            "domain": assessment.domain.base_weight,
        },
        "lane": assessment.lane.value,
        "opportunity_type": {
            "status": assessment.opportunity_type.status.value,
            "reason": assessment.opportunity_type.reason.value,
        },
        "required_skill": {
            "normalized_score": assessment.required_skill.normalized_score,
            "matched_count": assessment.required_skill.matched_count,
            "total_count": assessment.required_skill.total_count,
            "base_weight": assessment.required_skill.base_weight,
            "upstream_fingerprint": assessment.required_skill.upstream_fingerprint,
        },
        "semantic": {
            "status": assessment.semantic.status.value,
            "raw_similarity": assessment.semantic.raw_similarity,
            "percentile": assessment.semantic.percentile,
            "base_weight": assessment.semantic.base_weight,
            "semantic_similarity_fingerprint": (
                assessment.semantic.semantic_similarity_fingerprint
            ),
            "corpus_fingerprint": assessment.semantic.corpus_fingerprint,
            "model_fingerprint": assessment.semantic.model_fingerprint,
            "semantic_percentile_version": (
                assessment.semantic.semantic_percentile_version
            ),
        },
        "domain": {
            "status": assessment.domain.status.value,
            "reason": assessment.domain.reason.value,
            "preferred_rank": assessment.domain.preferred_rank,
            "normalized_score": assessment.domain.normalized_score,
            "base_weight": assessment.domain.base_weight,
            "upstream_fingerprint": assessment.domain.upstream_fingerprint,
        },
        "supporting": {
            "preferred": {
                "ratio": assessment.preferred_skill.ratio,
                "matched_count": assessment.preferred_skill.matched_count,
                "total_count": assessment.preferred_skill.total_count,
            },
            "context": {
                "ratio": assessment.context_skill.ratio,
                "matched_count": assessment.context_skill.matched_count,
                "total_count": assessment.context_skill.total_count,
            },
        },
        "match_quality": assessment.match_quality,
        "evidence_coverage": assessment.evidence_coverage,
    }


def matching_assessment_fingerprint(assessment: MatchingAssessment) -> str:
    encoded = canonical_json(canonical_matching_assessment_payload(assessment)).encode()
    return hashlib.sha256(encoded).hexdigest()


def canonical_matching_batch_payload(batch: MatchingBatchResult) -> dict[str, Any]:
    return {
        "matching_engine_version": batch.matching_engine_version,
        "matching_rules_version": batch.matching_rules_version,
        "semantic_percentile_version": batch.semantic_percentile_version,
        "corpus_fingerprint": batch.corpus_fingerprint,
        "tfidf_model_fingerprint": batch.tfidf_model_fingerprint,
        "assessment_count": batch.assessment_count,
        "assessment_fingerprints": sorted(
            item.assessment_fingerprint for item in batch.assessments
        ),
    }


def matching_batch_fingerprint(batch: MatchingBatchResult) -> str:
    encoded = canonical_json(canonical_matching_batch_payload(batch)).encode()
    return hashlib.sha256(encoded).hexdigest()
