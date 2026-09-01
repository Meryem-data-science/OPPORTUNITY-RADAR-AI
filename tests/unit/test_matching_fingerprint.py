from dataclasses import replace

import pytest

from services.collector.matching.fingerprint import (
    canonical_matching_payload,
    matching_input_fingerprint,
)
from services.collector.matching.models import (
    MatchingCareerObjectives,
    MatchingEducation,
    MatchingExperience,
    MatchingInput,
    MatchingOpportunityInput,
    MatchingPreferences,
    MatchingProfileInput,
    MatchingProfileSkill,
    MatchingProject,
    MatchingQualification,
    MatchingRequiredSkill,
    MatchingRequirementAmbiguity,
    MatchingRequirements,
)


def sample() -> MatchingInput:
    return MatchingInput(
        profile=MatchingProfileInput(
            profile_id=1,
            skills=(
                MatchingProfileSkill("python", "Python", ("normalizer-v1",)),
                MatchingProfileSkill("sql", "SQL", ("normalizer-v1",)),
            ),
            experiences=(
                MatchingExperience(None, "Built pipelines", "structurer-v1"),
                MatchingExperience("Analyst", None, "structurer-v1"),
            ),
            projects=(MatchingProject("Radar", None, "structurer-v1"),),
            educations=(MatchingEducation(None, "Data programme", "structurer-v1"),),
            preferences=MatchingPreferences(
                ("INTERNSHIP", "PFE"),
                ("REMOTE", "HYBRID"),
                ("Data", "AI"),
                "preferences-v1",
            ),
            career_objectives=MatchingCareerObjectives(
                ("Build reliable data products", "Learn MLOps"), "preferences-v1"
            ),
        ),
        opportunity=MatchingOpportunityInput(
            opportunity_id=2,
            canonical_title="Data Engineer",
            description="Build pipelines",
            remote_type=None,
            qualification=MatchingQualification(
                "CORE_TARGET", "DATA_ENGINEERING", "INTERNSHIP", "classifier-v1"
            ),
            requirements=MatchingRequirements(
                "extractor-v1",
                (
                    MatchingRequiredSkill("sql", "SQL", "PREFERRED"),
                    MatchingRequiredSkill("python", "Python", "REQUIRED"),
                ),
                (MatchingRequirementAmbiguity("SKILL", "ALTERNATIVE"),),
            ),
        ),
    )


def test_same_content_has_same_fingerprint_and_ids_are_excluded():
    inputs = sample()
    other_ids = replace(
        inputs,
        profile=replace(inputs.profile, profile_id=99),
        opportunity=replace(inputs.opportunity, opportunity_id=100),
    )
    assert matching_input_fingerprint(inputs) == matching_input_fingerprint(sample())
    assert matching_input_fingerprint(inputs) == matching_input_fingerprint(other_ids)
    payload = canonical_matching_payload(inputs)
    assert "profile_id" not in str(payload)
    assert "opportunity_id" not in str(payload)


def test_semantic_collections_are_order_independent_with_nullable_values():
    inputs = sample()
    reordered = replace(
        inputs,
        profile=replace(
            inputs.profile,
            skills=tuple(reversed(inputs.profile.skills)),
            experiences=tuple(reversed(inputs.profile.experiences)),
            preferences=replace(
                inputs.profile.preferences,
                opportunity_types=tuple(
                    reversed(inputs.profile.preferences.opportunity_types)
                ),
                work_modes=tuple(reversed(inputs.profile.preferences.work_modes)),
            ),
        ),
        opportunity=replace(
            inputs.opportunity,
            requirements=replace(
                inputs.opportunity.requirements,
                skills=tuple(reversed(inputs.opportunity.requirements.skills)),
            ),
        ),
    )
    assert matching_input_fingerprint(inputs) == matching_input_fingerprint(reordered)


def test_free_text_preference_order_is_preserved_and_changes_fingerprint():
    inputs = sample()
    reordered_domains = replace(
        inputs,
        profile=replace(
            inputs.profile,
            preferences=replace(
                inputs.profile.preferences,
                preferred_domains=tuple(
                    reversed(inputs.profile.preferences.preferred_domains)
                ),
            ),
        ),
    )
    reordered_objectives = replace(
        inputs,
        profile=replace(
            inputs.profile,
            career_objectives=replace(
                inputs.profile.career_objectives,
                objectives=tuple(
                    reversed(inputs.profile.career_objectives.objectives)
                ),
            ),
        ),
    )

    payload = canonical_matching_payload(inputs)["profile"]
    assert payload["preferences"]["preferred_domains"] == ["Data", "AI"]
    assert payload["career_objectives"]["objectives"] == [
        "Build reliable data products",
        "Learn MLOps",
    ]
    assert matching_input_fingerprint(inputs) != matching_input_fingerprint(
        reordered_domains
    )
    assert matching_input_fingerprint(inputs) != matching_input_fingerprint(
        reordered_objectives
    )


@pytest.mark.parametrize(
    "changed",
    [
        lambda value: replace(
            value, profile=replace(value.profile, skills=value.profile.skills[:-1])
        ),
        lambda value: replace(
            value,
            profile=replace(
                value.profile,
                career_objectives=replace(
                    value.profile.career_objectives, objectives=("Different",)
                ),
            ),
        ),
        lambda value: replace(
            value, opportunity=replace(value.opportunity, canonical_title="ML Engineer")
        ),
        lambda value: replace(
            value, opportunity=replace(value.opportunity, description="Different")
        ),
        lambda value: replace(
            value,
            opportunity=replace(
                value.opportunity,
                requirements=replace(
                    value.opportunity.requirements,
                    skills=(
                        replace(
                            value.opportunity.requirements.skills[0],
                            requirement="REQUIRED",
                        ),
                    )
                    + value.opportunity.requirements.skills[1:],
                ),
            ),
        ),
    ],
)
def test_business_content_changes_fingerprint(changed):
    inputs = sample()
    assert matching_input_fingerprint(inputs) != matching_input_fingerprint(
        changed(inputs)
    )


def test_unknown_is_different_from_known_empty_extraction_and_preferences():
    inputs = sample()
    no_requirements = replace(
        inputs, opportunity=replace(inputs.opportunity, requirements=None)
    )
    empty_requirements = replace(
        inputs,
        opportunity=replace(
            inputs.opportunity,
            requirements=MatchingRequirements("extractor-v1", (), ()),
        ),
    )
    assert matching_input_fingerprint(no_requirements) != matching_input_fingerprint(
        empty_requirements
    )

    no_preferences = replace(inputs, profile=replace(inputs.profile, preferences=None))
    empty_domains = replace(
        inputs,
        profile=replace(
            inputs.profile,
            preferences=replace(inputs.profile.preferences, preferred_domains=()),
        ),
    )
    assert matching_input_fingerprint(no_preferences) != matching_input_fingerprint(
        empty_domains
    )
