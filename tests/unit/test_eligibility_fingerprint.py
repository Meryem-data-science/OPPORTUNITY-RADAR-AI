"""What the digest covers, and — more importantly — what it does not.

The digest is the whole of idempotence: a second synchronization writes nothing
because every posting's digest still matches. So these tests come in two halves.
One says a change that matters changes it. The other says a change that does not
matter leaves it alone, because a digest that moved on an edited telephone
number would rewrite three hundred decisions to store the same three hundred
verdicts.
"""

from __future__ import annotations

import dataclasses

from services.eligibility.fingerprint import (
    canonical_eligibility_payload,
    eligibility_fingerprint,
)
from services.eligibility.models import (
    ConventionCapability,
    EducationRequirementInput,
    EligibilityInput,
    ExperienceRequirementInput,
    LanguageRequirementInput,
    OpportunityEligibilityInput,
    ProfileEligibilityInput,
    ProfileLanguageInput,
    RequirementAmbiguityInput,
    RequirementKind,
    SkillRequirementInput,
    SponsorshipNeed,
)


def offer(**overrides) -> OpportunityEligibilityInput:
    base = {
        "opportunity_id": 1,
        "constraints_extractor_version": "opportunity-constraints-v1",
        "requirements_extractor_version": "opportunity-requirements-v3",
        "education": (EducationRequirementInput("BAC_PLUS_5", "MINIMUM"),),
        "experience": (
            ExperienceRequirementInput(24, None, RequirementKind.REQUIRED),
        ),
        "languages": (
            LanguageRequirementInput("english", RequirementKind.REQUIRED, "B2"),
            LanguageRequirementInput("french", RequirementKind.PREFERRED, None),
        ),
        "skills": (
            SkillRequirementInput("python", RequirementKind.REQUIRED),
            SkillRequirementInput("sql", RequirementKind.PREFERRED),
        ),
        "ambiguities": (
            RequirementAmbiguityInput("SKILL", "ALTERNATIVE_GROUP_UNSUPPORTED"),
        ),
        "visa_sponsorship": "NOT_AVAILABLE",
        "work_authorization": "REQUIRED",
        "convention": "REQUIRED",
    }
    base.update(overrides)
    return OpportunityEligibilityInput(**base)


def person(**overrides) -> ProfileEligibilityInput:
    base = {
        "profile_id": 7,
        "education_levels": ("BAC_PLUS_5",),
        "languages": (
            ProfileLanguageInput("english", "C1"),
            ProfileLanguageInput("french", "courant"),
        ),
        "skills": ("python", "sql"),
        "sponsorship_need": SponsorshipNeed.NO,
        "convention_capability": ConventionCapability.AVAILABLE,
    }
    base.update(overrides)
    return ProfileEligibilityInput(**base)


def digest(opportunity=None, profile=None, **overrides) -> str:
    return eligibility_fingerprint(
        EligibilityInput(opportunity or offer(), profile or person(), **overrides)
    )


def test_the_same_inputs_give_the_same_digest():
    assert digest() == digest()


def test_reordering_equivalent_collections_gives_the_same_digest():
    """A set of demands is the same set however it happens to be listed."""
    reordered = offer(
        languages=(
            LanguageRequirementInput("french", RequirementKind.PREFERRED, None),
            LanguageRequirementInput("english", RequirementKind.REQUIRED, "B2"),
        ),
        skills=(
            SkillRequirementInput("sql", RequirementKind.PREFERRED),
            SkillRequirementInput("python", RequirementKind.REQUIRED),
        ),
    )
    shuffled = person(
        languages=(
            ProfileLanguageInput("french", "courant"),
            ProfileLanguageInput("english", "C1"),
        ),
        skills=("sql", "python"),
    )
    assert digest(reordered, shuffled) == digest()


def test_mixed_bounded_and_unbounded_experience_is_canonicalized():
    """Nullable bounds and kinds remain JSON values, but never compare directly."""
    mixed = (
        ExperienceRequirementInput(24, 48, RequirementKind.PREFERRED),
        ExperienceRequirementInput(12, None, None),
        ExperienceRequirementInput(None, 24, RequirementKind.REQUIRED),
    )

    payload = canonical_eligibility_payload(
        EligibilityInput(offer(experience=mixed), person())
    )

    assert payload["opportunity"]["experience"] == [
        [None, 24, "REQUIRED"],
        [12, None, None],
        [24, 48, "PREFERRED"],
    ]
    assert len(digest(offer(experience=mixed))) == 64


def test_reordering_mixed_experience_preserves_payload_and_fingerprint():
    mixed = (
        ExperienceRequirementInput(None, 24, RequirementKind.REQUIRED),
        ExperienceRequirementInput(12, None, None),
        ExperienceRequirementInput(24, 48, RequirementKind.PREFERRED),
    )
    reversed_mixed = tuple(reversed(mixed))
    first = EligibilityInput(offer(experience=mixed), person())
    second = EligibilityInput(offer(experience=reversed_mixed), person())

    assert canonical_eligibility_payload(first) == canonical_eligibility_payload(second)
    assert eligibility_fingerprint(first) == eligibility_fingerprint(second)


def test_nullable_opportunity_language_levels_sort_deterministically():
    languages = (
        LanguageRequirementInput("english", RequirementKind.REQUIRED, "B2"),
        LanguageRequirementInput("english", RequirementKind.REQUIRED, None),
    )
    reversed_languages = tuple(reversed(languages))

    first = EligibilityInput(offer(languages=languages), person())
    second = EligibilityInput(offer(languages=reversed_languages), person())

    assert canonical_eligibility_payload(first)["opportunity"]["languages"] == [
        ["english", "REQUIRED", None],
        ["english", "REQUIRED", "B2"],
    ]
    assert canonical_eligibility_payload(first) == canonical_eligibility_payload(second)
    assert eligibility_fingerprint(first) == eligibility_fingerprint(second)


def test_nullable_profile_language_levels_sort_deterministically():
    languages = (
        ProfileLanguageInput("english", "C1"),
        ProfileLanguageInput("english", None),
    )
    reversed_languages = tuple(reversed(languages))

    first = EligibilityInput(offer(), person(languages=languages))
    second = EligibilityInput(offer(), person(languages=reversed_languages))

    assert canonical_eligibility_payload(first)["profile"]["languages"] == [
        ["english", None],
        ["english", "C1"],
    ]
    assert canonical_eligibility_payload(first) == canonical_eligibility_payload(second)
    assert eligibility_fingerprint(first) == eligibility_fingerprint(second)


def test_a_new_engine_version_changes_the_digest():
    """A change of rules must recompute every stored verdict."""
    assert digest(engine_version="eligibility-rules-v2") != digest()


def test_a_changed_education_requirement_changes_the_digest():
    assert (
        digest(offer(education=(EducationRequirementInput("BAC_PLUS_3", "MINIMUM"),)))
        != digest()
    )


def test_a_changed_required_language_level_changes_the_digest():
    assert (
        digest(
            offer(
                languages=(
                    LanguageRequirementInput("english", RequirementKind.REQUIRED, "C2"),
                    LanguageRequirementInput("french", RequirementKind.PREFERRED, None),
                )
            )
        )
        != digest()
    )


def test_a_changed_profile_language_level_changes_the_digest():
    assert (
        digest(
            profile=person(
                languages=(
                    ProfileLanguageInput("english", "A2"),
                    ProfileLanguageInput("french", "courant"),
                )
            )
        )
        != digest()
    )


def test_a_newly_stated_sponsorship_need_changes_the_digest():
    assert digest(profile=person(sponsorship_need=SponsorshipNeed.YES)) != digest()


def test_a_changed_convention_capability_changes_the_digest():
    assert (
        digest(
            profile=person(convention_capability=ConventionCapability.NOT_AVAILABLE)
        )
        != digest()
    )


def test_a_gained_skill_changes_the_digest():
    assert digest(profile=person(skills=("python", "sql", "spark"))) != digest()


def test_a_new_ambiguity_changes_the_digest():
    """It cannot move the verdict, but it is a row the decision must store."""
    assert (
        digest(
            offer(
                ambiguities=(
                    RequirementAmbiguityInput("SKILL", "ALTERNATIVE_GROUP_UNSUPPORTED"),
                    RequirementAmbiguityInput(
                        "LANGUAGE", "CONFLICTING_LANGUAGE_PROFICIENCY"
                    ),
                )
            )
        )
        != digest()
    )


def test_a_new_extractor_version_changes_the_digest():
    """The same words read under new rules are a new statement of the demand."""
    assert (
        digest(offer(requirements_extractor_version="opportunity-requirements-v4"))
        != digest()
    )


def test_the_row_identities_are_not_in_the_digest():
    """Two identical postings are two identical inputs; the id names the row."""
    assert digest(offer(opportunity_id=999)) == digest()
    assert digest(profile=person(profile_id=999)) == digest()


def test_the_digest_domain_is_exactly_the_two_input_contracts():
    """The property that makes "in the digest" and "read by a rule" one set.

    Every field of both dataclasses appears in the payload, except the two row
    identities the test above pins as deliberately excluded. A field added to an
    input without being serialized would break idempotence silently; this fails
    loudly instead.
    """
    payload = canonical_eligibility_payload(EligibilityInput(offer(), person()))
    opportunity_fields = set(
        OpportunityEligibilityInput.__dataclass_fields__
    ) - {"opportunity_id"}
    profile_fields = set(ProfileEligibilityInput.__dataclass_fields__) - {"profile_id"}
    assert set(payload["opportunity"]) == opportunity_fields
    assert set(payload["profile"]) == profile_fields


def test_the_digest_is_sixty_four_hexadecimal_characters():
    value = digest()
    assert len(value) == 64
    assert set(value) <= set("0123456789abcdef")


def test_nothing_irrelevant_can_reach_the_digest_at_all():
    """A telephone number, a GitHub URL, a portfolio, an availability date and a
    mobility are not fields of the profile contract, so no edit to one can
    recompute a decision. The absence is the guarantee; asserting it here keeps
    a future field from being added without this argument being made again.
    """
    forbidden = {
        "phone",
        "phone_number",
        "github_url",
        "portfolio_url",
        "linkedin_url",
        "email",
        "full_name",
        "availability",
        "available_from",
        "mobility",
        "mobility_scope",
        "preferred_domains",
        "opportunity_types",
        "work_modes",
        "career_objectives",
        "certifications",
        "projects",
    }
    assert not forbidden & set(ProfileEligibilityInput.__dataclass_fields__)
    payload = canonical_eligibility_payload(EligibilityInput(offer(), person()))
    assert not forbidden & set(payload["profile"])


def test_nothing_volatile_is_in_the_digest():
    """No clock, no timestamp, no row order. Two runs a week apart agree."""
    payload = canonical_eligibility_payload(EligibilityInput(offer(), person()))
    rendered = repr(payload)
    for volatile in ("evaluated_at", "created_at", "updated_at", "uuid"):
        assert volatile not in rendered


def test_the_digest_is_stable_across_an_equal_but_rebuilt_input():
    rebuilt = dataclasses.replace(offer())
    assert digest(rebuilt) == digest()
