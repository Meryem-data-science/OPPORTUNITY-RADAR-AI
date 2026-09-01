from dataclasses import FrozenInstanceError

import pytest

from services.collector.matching.models import (
    MATCHING_INPUT_VERSION,
    MatchingInput,
    MatchingOpportunityInput,
    MatchingProfileInput,
)


def test_matching_contract_is_versioned_and_immutable():
    profile = MatchingProfileInput(profile_id=1)
    opportunity = MatchingOpportunityInput(2, "Data role", None, None)
    inputs = MatchingInput(profile, opportunity)

    assert (
        inputs.matching_input_version == MATCHING_INPUT_VERSION == "matching-input-v1"
    )
    with pytest.raises(FrozenInstanceError):
        profile.profile_id = 3
    with pytest.raises(FrozenInstanceError):
        opportunity.canonical_title = "Changed"
