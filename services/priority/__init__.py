"""Public contracts for the pure Phase 5.1A priority engine."""

from .engine import (
    FRESHNESS_WEIGHT,
    HIGH_MINIMUM_COVERAGE,
    MATCH_QUALITY_WEIGHT,
    OPPORTUNITY_QUALITY_WEIGHT,
    build_priority_assessment,
    freshness_score,
)
from .fingerprint import (
    canonical_priority_assessment_payload,
    priority_assessment_fingerprint,
)
from .input_assembly import (
    PRIORITY_INPUT_ASSEMBLY_VERSION,
    PriorityInputAssemblyResult,
    PriorityInputRecord,
    PriorityOpportunityContext,
    PriorityReadinessIssue,
    PriorityReadinessIssueCode,
    PriorityReadinessStatus,
    assemble_priority_inputs,
)
from .models import (
    PRIORITY_ENGINE_VERSION,
    PRIORITY_FRESHNESS_VERSION,
    PRIORITY_QUALITY_VERSION,
    PRIORITY_RULES_VERSION,
    ComponentStatus,
    DeadlineStatus,
    PriorityAssessment,
    PriorityCategory,
    PriorityInput,
    PriorityInputError,
    PriorityEligibilitySnapshot,
    PriorityMatchingSnapshot,
    PriorityReasonCode,
)
from .persistence import (
    PriorityPersistenceError,
    PriorityStoreResult,
    store_priority_batch,
)
from .persistence_fingerprint import (
    PRIORITY_PERSISTENCE_VERSION,
    canonical_priority_run_payload,
    priority_run_fingerprint,
)
from .sync import PrioritySyncError, PrioritySyncResult, sync_priority

__all__ = [
    "FRESHNESS_WEIGHT",
    "HIGH_MINIMUM_COVERAGE",
    "MATCH_QUALITY_WEIGHT",
    "OPPORTUNITY_QUALITY_WEIGHT",
    "PRIORITY_ENGINE_VERSION",
    "PRIORITY_FRESHNESS_VERSION",
    "PRIORITY_QUALITY_VERSION",
    "PRIORITY_RULES_VERSION",
    "PRIORITY_INPUT_ASSEMBLY_VERSION",
    "PRIORITY_PERSISTENCE_VERSION",
    "ComponentStatus",
    "DeadlineStatus",
    "PriorityAssessment",
    "PriorityCategory",
    "PriorityInput",
    "PriorityInputError",
    "PriorityInputAssemblyResult",
    "PriorityInputRecord",
    "PriorityOpportunityContext",
    "PriorityReadinessIssue",
    "PriorityReadinessIssueCode",
    "PriorityReadinessStatus",
    "PriorityEligibilitySnapshot",
    "PriorityMatchingSnapshot",
    "PriorityReasonCode",
    "PriorityPersistenceError",
    "PriorityStoreResult",
    "PrioritySyncError",
    "PrioritySyncResult",
    "build_priority_assessment",
    "assemble_priority_inputs",
    "canonical_priority_assessment_payload",
    "freshness_score",
    "priority_assessment_fingerprint",
    "canonical_priority_run_payload",
    "priority_run_fingerprint",
    "store_priority_batch",
    "sync_priority",
]
