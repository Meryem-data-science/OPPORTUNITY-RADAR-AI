"""Public deterministic Phase 4 matching contracts."""

from .fingerprint import canonical_matching_payload, matching_input_fingerprint
from .inputs import (
    MatchingInputError,
    load_matching_input,
    load_opportunity_matching_input,
    load_profile_matching_input,
)
from .models import (
    MATCHING_INPUT_VERSION,
    MatchingInput,
    MatchingOpportunityInput,
    MatchingProfileInput,
)
from .skill_signals import (
    SKILL_SIGNAL_VERSION,
    OpportunitySkillSignal,
    OpportunitySkillSignals,
    RequirementsState,
    SkillSignalKind,
    SkillSignalSource,
    build_opportunity_skill_signals,
)
from .skill_fit import (
    SKILL_FIT_VERSION,
    SkillCoverage,
    SkillFitEvaluation,
    SkillFitInputError,
    SkillFitResult,
    build_skill_fit,
)
from .skill_fit_fingerprint import canonical_skill_fit_payload, skill_fit_fingerprint

__all__ = [
    "MATCHING_INPUT_VERSION",
    "MatchingInput",
    "MatchingInputError",
    "MatchingOpportunityInput",
    "MatchingProfileInput",
    "OpportunitySkillSignal",
    "OpportunitySkillSignals",
    "RequirementsState",
    "SKILL_FIT_VERSION",
    "SKILL_SIGNAL_VERSION",
    "SkillSignalKind",
    "SkillSignalSource",
    "SkillCoverage",
    "SkillFitEvaluation",
    "SkillFitInputError",
    "SkillFitResult",
    "build_opportunity_skill_signals",
    "build_skill_fit",
    "canonical_matching_payload",
    "canonical_skill_fit_payload",
    "load_matching_input",
    "load_opportunity_matching_input",
    "load_profile_matching_input",
    "matching_input_fingerprint",
    "skill_fit_fingerprint",
]
