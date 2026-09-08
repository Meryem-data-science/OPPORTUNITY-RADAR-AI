"""Phase 9A — the personalized recommendation engine, v1.

Phase 8 answers *what is this opportunity*. This package answers the other half:

    among the Data & AI opportunities that are relevant at all, which ones fit
    **this** profile, its CV-derived facts and its declared preferences?

It is an additive layer and it owns no new truth. Everything it reads was
established upstream and is read exactly as persisted:

    Phase 4 matching snapshot   required-skill fit, semantic percentile, coarse
                                domain alignment, opportunity-type signal
    Phase 8 fine classification which Data/AI sub-domain, read, never recomputed
    Phase 7 geography           the posting's stored location resolutions, and
                                the country the profile's mobility restricts to
    Phase 3.6 eligibility       the stored decision for this user and posting
    Phase 3 Digital Twin        the declared preferences, through Matching's own
                                read-only loader

and it produces one score, one route, one deterministic ranking and a set of
machine-readable reasons. There is no LLM anywhere in it, no embedding, no
similarity of its own, no generated sentence and no invented confidence.

What 9A deliberately does **not** do: it persists nothing, migrates nothing,
exposes no HTTP route and changes no page. It also leaves Matching v1 exactly as
it was — same weights, same versions, same snapshots — because the recommendation
is a controlled evolution of that baseline rather than a second layer of bonuses
stacked on top of `match_quality`.

    models.py          the vocabulary: versions, enums, immutable values
    fine_domain.py     the fine -> canonical-family bridge, and the fit
    engine.py          the score, the disposition, the ranking, pure
    fingerprint.py     canonical payloads and their SHA-256 digests
    input_assembly.py  read-only assembly out of SQLite, and its readiness
"""

from services.recommendation.engine import (
    build_domain_component,
    build_geography_signal,
    build_recommendation_assessment,
    build_recommendation_batch,
    eligibility_signal_status,
    rank_recommendation_assessments,
    recommendation_disposition,
)
from services.recommendation.fine_domain import (
    FINE_DOMAIN_BRIDGE,
    FINE_DOMAIN_BRIDGE_VERSION,
    UNBRIDGED_FINE_CATEGORIES,
    FineDomainAvailability,
    FineDomainFit,
    FineDomainReason,
    build_fine_domain_fit,
    preferred_rank_score,
)
from services.recommendation.fingerprint import (
    canonical_recommendation_assessment_payload,
    canonical_recommendation_batch_payload,
    recommendation_assessment_fingerprint,
    recommendation_batch_fingerprint,
)
from services.recommendation.input_assembly import (
    RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
    RecommendationInputAssemblyResult,
    RecommendationInputRecord,
    RecommendationOpportunityContext,
    RecommendationReadinessIssue,
    RecommendationReadinessIssueCode,
    RecommendationReadinessStatus,
    assemble_recommendation_inputs,
)
from services.recommendation.models import (
    DISPOSITION_PRECEDENCE,
    DISPOSITION_RANK,
    DOMAIN_WEIGHT,
    RECOMMENDATION_ENGINE_VERSION,
    RECOMMENDATION_RULES_VERSION,
    REQUIRED_SKILL_WEIGHT,
    SEMANTIC_WEIGHT,
    CONFIRMED_GAP_CODES,
    STRENGTH_CODES,
    UNKNOWN_CODES,
    ComponentStatus,
    DomainComponent,
    DomainFitSource,
    EligibilitySignal,
    EligibilitySignalStatus,
    GeographySignal,
    GeographyState,
    OpportunityTypeSignal,
    RecommendationAssessment,
    RecommendationBatchResult,
    RecommendationDisposition,
    RecommendationEligibilityInput,
    RecommendationFineClassification,
    RecommendationGeographyInput,
    RecommendationInput,
    RecommendationInputError,
    RecommendationMatchingSnapshot,
    RecommendationReasonCode,
    RequiredSkillComponent,
    SemanticComponent,
    WorkModeSignal,
)

__all__ = [
    "CONFIRMED_GAP_CODES",
    "DISPOSITION_PRECEDENCE",
    "DISPOSITION_RANK",
    "DOMAIN_WEIGHT",
    "FINE_DOMAIN_BRIDGE",
    "FINE_DOMAIN_BRIDGE_VERSION",
    "RECOMMENDATION_ENGINE_VERSION",
    "RECOMMENDATION_INPUT_ASSEMBLY_VERSION",
    "RECOMMENDATION_RULES_VERSION",
    "REQUIRED_SKILL_WEIGHT",
    "SEMANTIC_WEIGHT",
    "STRENGTH_CODES",
    "UNBRIDGED_FINE_CATEGORIES",
    "UNKNOWN_CODES",
    "ComponentStatus",
    "DomainComponent",
    "DomainFitSource",
    "EligibilitySignal",
    "EligibilitySignalStatus",
    "FineDomainAvailability",
    "FineDomainFit",
    "FineDomainReason",
    "GeographySignal",
    "GeographyState",
    "OpportunityTypeSignal",
    "RecommendationAssessment",
    "RecommendationBatchResult",
    "RecommendationDisposition",
    "RecommendationEligibilityInput",
    "RecommendationFineClassification",
    "RecommendationGeographyInput",
    "RecommendationInput",
    "RecommendationInputAssemblyResult",
    "RecommendationInputError",
    "RecommendationInputRecord",
    "RecommendationMatchingSnapshot",
    "RecommendationOpportunityContext",
    "RecommendationReadinessIssue",
    "RecommendationReadinessIssueCode",
    "RecommendationReadinessStatus",
    "RecommendationReasonCode",
    "RequiredSkillComponent",
    "SemanticComponent",
    "WorkModeSignal",
    "assemble_recommendation_inputs",
    "build_domain_component",
    "build_fine_domain_fit",
    "build_geography_signal",
    "build_recommendation_assessment",
    "build_recommendation_batch",
    "canonical_recommendation_assessment_payload",
    "canonical_recommendation_batch_payload",
    "eligibility_signal_status",
    "preferred_rank_score",
    "rank_recommendation_assessments",
    "recommendation_assessment_fingerprint",
    "recommendation_batch_fingerprint",
    "recommendation_disposition",
]
