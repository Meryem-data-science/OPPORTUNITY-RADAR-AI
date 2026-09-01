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

__all__ = [
    "MATCHING_INPUT_VERSION",
    "MatchingInput",
    "MatchingInputError",
    "MatchingOpportunityInput",
    "MatchingProfileInput",
    "canonical_matching_payload",
    "load_matching_input",
    "load_opportunity_matching_input",
    "load_profile_matching_input",
    "matching_input_fingerprint",
]
