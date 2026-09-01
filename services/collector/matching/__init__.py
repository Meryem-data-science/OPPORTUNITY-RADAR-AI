"""Public Phase 4.1 matching input contract."""

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

__all__ = [
    "MATCHING_INPUT_VERSION",
    "MatchingInput",
    "MatchingInputError",
    "MatchingOpportunityInput",
    "MatchingProfileInput",
    "OpportunitySkillSignal",
    "OpportunitySkillSignals",
    "RequirementsState",
    "SKILL_SIGNAL_VERSION",
    "SkillSignalKind",
    "SkillSignalSource",
    "build_opportunity_skill_signals",
    "canonical_matching_payload",
    "load_matching_input",
    "load_opportunity_matching_input",
    "load_profile_matching_input",
    "matching_input_fingerprint",
]
