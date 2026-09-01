"""The truth tables of `eligibility-rules-v1`, and the promises around them.

Two kinds of test live here. The first walks each rule's table cell by cell.
The second asserts the properties the whole phase is built on — that a gap never
becomes a refusal, that a preference never blocks, that a skill never blocks,
that the six deferred dimensions never move a verdict — and those are written
against the aggregated decision rather than against one rule, because that is
where a mistake would actually cost somebody an opportunity.
"""

from __future__ import annotations

import pytest

from services.eligibility.engine import aggregate, evaluate_eligibility
from services.eligibility.models import (
    ConventionCapability,
    Dimension,
    EducationRequirementInput,
    EligibilityInput,
    EligibilityModelError,
    ExperienceRequirementInput,
    GlobalStatus,
    LanguageRequirementInput,
    OpportunityEligibilityInput,
    ProfileEligibilityInput,
    ProfileLanguageInput,
    ReasonCode,
    RequirementAmbiguityInput,
    RequirementKind,
    RuleResult,
    RuleStatus,
    SkillRequirementInput,
    SponsorshipNeed,
)

CONSTRAINTS_VERSION = "opportunity-constraints-v1"
REQUIREMENTS_VERSION = "opportunity-requirements-v3"


def offer(**overrides) -> OpportunityEligibilityInput:
    """A posting demanding nothing, plus whatever the test states explicitly."""
    return OpportunityEligibilityInput(
        opportunity_id=overrides.pop("opportunity_id", 1),
        constraints_extractor_version=CONSTRAINTS_VERSION,
        requirements_extractor_version=REQUIREMENTS_VERSION,
        **overrides,
    )


def person(**overrides) -> ProfileEligibilityInput:
    """A Digital Twin stating nothing, plus whatever the test states."""
    return ProfileEligibilityInput(profile_id=overrides.pop("profile_id", 7), **overrides)


def decide(opportunity, profile):
    return evaluate_eligibility(EligibilityInput(opportunity, profile))


def results_for(decision, dimension: Dimension) -> tuple[RuleResult, ...]:
    return tuple(item for item in decision.results if item.dimension is dimension)


def only(decision, dimension: Dimension) -> RuleResult:
    found = results_for(decision, dimension)
    assert len(found) == 1
    return found[0]


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def test_a_posting_demanding_nothing_is_eligible():
    """Silence is not a doubt. A thin advertisement blocks nobody."""
    decision = decide(offer(), person())
    assert decision.status is GlobalStatus.ELIGIBLE
    assert decision.violated_count == 0
    assert decision.blocking_unknown_count == 0


def test_one_verified_contradiction_makes_the_verdict_ineligible():
    decision = decide(
        offer(convention="REQUIRED"),
        person(convention_capability=ConventionCapability.NOT_AVAILABLE),
    )
    assert decision.status is GlobalStatus.INELIGIBLE


def test_an_unanswered_hard_requirement_makes_the_verdict_unknown():
    decision = decide(offer(convention="REQUIRED"), person())
    assert decision.status is GlobalStatus.UNKNOWN
    assert decision.violated_count == 0
    assert decision.blocking_unknown_count == 1


def test_a_contradiction_outranks_an_open_question():
    """VIOLATED > UNKNOWN > SATISFIED, and both reasons are kept."""
    decision = decide(
        offer(
            convention="REQUIRED",
            languages=(
                LanguageRequirementInput("english", RequirementKind.REQUIRED, "B2"),
            ),
        ),
        person(convention_capability=ConventionCapability.NOT_AVAILABLE),
    )
    assert decision.status is GlobalStatus.INELIGIBLE
    assert decision.violated_count == 1
    assert decision.blocking_unknown_count == 1


def test_every_blocker_is_preserved_rather_than_short_circuited():
    """An audit needs all the reasons, not the first one that decided."""
    decision = decide(
        offer(
            convention="REQUIRED",
            work_authorization="REQUIRED",
            languages=(
                LanguageRequirementInput("french", RequirementKind.REQUIRED, "C1"),
            ),
        ),
        person(
            convention_capability=ConventionCapability.NOT_AVAILABLE,
            sponsorship_need=SponsorshipNeed.YES,
            languages=(ProfileLanguageInput("french", "A2"),),
        ),
    )
    assert decision.status is GlobalStatus.INELIGIBLE
    assert decision.violated_count == 3
    dimensions = {
        result.dimension
        for result in decision.results
        if result.status is RuleStatus.VIOLATED
    }
    assert dimensions == {
        Dimension.CONVENTION,
        Dimension.WORK_AUTHORIZATION,
        Dimension.LANGUAGE,
    }


def test_aggregate_reads_only_the_blocking_rules():
    assert aggregate(()) is GlobalStatus.ELIGIBLE


# --------------------------------------------------------------------------
# Education
# --------------------------------------------------------------------------


def test_education_requirement_absent_is_not_applicable():
    result = only(decide(offer(), person()), Dimension.EDUCATION)
    assert result.status is RuleStatus.NOT_APPLICABLE
    assert result.reason_code is ReasonCode.EDUCATION_REQUIREMENT_ABSENT
    assert result.is_blocking is False


def test_education_minimum_met_by_a_higher_rung_of_the_same_ladder():
    result = only(
        decide(
            offer(education=(EducationRequirementInput("BAC_PLUS_3", "MINIMUM"),)),
            person(education_levels=("BAC_PLUS_5",)),
        ),
        Dimension.EDUCATION,
    )
    assert result.status is RuleStatus.SATISFIED
    assert result.reason_code is ReasonCode.EDUCATION_REQUIREMENT_SATISFIED


def test_education_minimum_contradicted_by_a_lower_rung():
    result = only(
        decide(
            offer(education=(EducationRequirementInput("BAC_PLUS_5", "MINIMUM"),)),
            person(education_levels=("BAC_PLUS_3",)),
        ),
        Dimension.EDUCATION,
    )
    assert result.status is RuleStatus.VIOLATED
    assert result.reason_code is ReasonCode.EDUCATION_REQUIREMENT_VIOLATED


def test_education_unknown_profile_is_unknown_and_never_violated():
    """An empty level set is 'we do not know', never 'they have no degree'."""
    result = only(
        decide(
            offer(education=(EducationRequirementInput("BAC_PLUS_5", "MINIMUM"),)),
            person(),
        ),
        Dimension.EDUCATION,
    )
    assert result.status is RuleStatus.UNKNOWN
    assert result.reason_code is ReasonCode.EDUCATION_PROFILE_UNKNOWN


def test_education_across_two_ladders_is_not_comparable_rather_than_violated():
    """`0012` refuses to equate BAC_PLUS_5 and MASTER; so does this engine."""
    result = only(
        decide(
            offer(education=(EducationRequirementInput("BAC_PLUS_5", "MINIMUM"),)),
            person(education_levels=("MASTER",)),
        ),
        Dimension.EDUCATION,
    )
    assert result.status is RuleStatus.UNKNOWN
    assert result.reason_code is ReasonCode.EDUCATION_NOT_COMPARABLE


def test_several_education_rows_are_accepted_alternatives_not_obligations():
    """"Bac+5 or Master" is satisfied by either, and contradicted by neither."""
    decision = decide(
        offer(
            education=(
                EducationRequirementInput("BAC_PLUS_5", "MINIMUM"),
                EducationRequirementInput("MASTER", "MINIMUM"),
            )
        ),
        person(education_levels=("MASTER",)),
    )
    assert only(decision, Dimension.EDUCATION).status is RuleStatus.SATISFIED


def test_an_incomparable_alternative_prevents_a_contradiction():
    """One route this engine cannot check is one it cannot rule out."""
    result = only(
        decide(
            offer(
                education=(
                    EducationRequirementInput("BAC_PLUS_5", "MINIMUM"),
                    EducationRequirementInput("ENGINEERING_DEGREE", "EXACT"),
                )
            ),
            person(education_levels=("BAC_PLUS_3",)),
        ),
        Dimension.EDUCATION,
    )
    assert result.status is RuleStatus.UNKNOWN


def test_an_exact_education_demand_is_not_met_by_a_higher_degree():
    """Holding a PhD is not a row in this database saying one holds a Master."""
    result = only(
        decide(
            offer(education=(EducationRequirementInput("MASTER", "EXACT"),)),
            person(education_levels=("PHD",)),
        ),
        Dimension.EDUCATION,
    )
    assert result.status is RuleStatus.VIOLATED


# --------------------------------------------------------------------------
# Enrolment
# --------------------------------------------------------------------------


def test_enrolment_requirement_absent_is_not_applicable():
    result = only(decide(offer(), person()), Dimension.ENROLLMENT)
    assert result.status is RuleStatus.NOT_APPLICABLE


def test_enrolment_required_and_verified_is_satisfied():
    result = only(
        decide(offer(enrollment_required=True), person(currently_enrolled=True)),
        Dimension.ENROLLMENT,
    )
    assert result.status is RuleStatus.SATISFIED


def test_enrolment_required_and_verified_absent_is_violated():
    result = only(
        decide(offer(enrollment_required=True), person(currently_enrolled=False)),
        Dimension.ENROLLMENT,
    )
    assert result.status is RuleStatus.VIOLATED


def test_enrolment_required_and_unknown_is_unknown():
    result = only(
        decide(offer(enrollment_required=True), person()), Dimension.ENROLLMENT
    )
    assert result.status is RuleStatus.UNKNOWN
    assert result.reason_code is ReasonCode.ENROLLMENT_STATUS_UNKNOWN


def test_a_completed_degree_never_implies_current_enrolment():
    """Two facts, and the engine has no path from the first to the second."""
    decision = decide(
        offer(enrollment_required=True), person(education_levels=("BAC_PLUS_5",))
    )
    assert only(decision, Dimension.ENROLLMENT).status is RuleStatus.UNKNOWN
    assert decision.status is GlobalStatus.UNKNOWN


# --------------------------------------------------------------------------
# Experience
# --------------------------------------------------------------------------


def test_experience_requirement_absent_is_not_applicable():
    assert only(decide(offer(), person()), Dimension.EXPERIENCE).status is (
        RuleStatus.NOT_APPLICABLE
    )


def test_experience_minimum_met_is_satisfied():
    result = only(
        decide(
            offer(
                experience=(
                    ExperienceRequirementInput(24, None, RequirementKind.REQUIRED),
                )
            ),
            person(comparable_experience_months=36),
        ),
        Dimension.EXPERIENCE,
    )
    assert result.status is RuleStatus.SATISFIED
    assert result.reason_code is ReasonCode.EXPERIENCE_MIN_SATISFIED


def test_experience_minimum_unmet_is_violated():
    result = only(
        decide(
            offer(
                experience=(
                    ExperienceRequirementInput(24, None, RequirementKind.REQUIRED),
                )
            ),
            person(comparable_experience_months=12),
        ),
        Dimension.EXPERIENCE,
    )
    assert result.status is RuleStatus.VIOLATED
    assert result.reason_code is ReasonCode.EXPERIENCE_MIN_VIOLATED


def test_experience_maximum_exceeded_is_violated():
    result = only(
        decide(
            offer(
                experience=(
                    ExperienceRequirementInput(None, 24, RequirementKind.REQUIRED),
                )
            ),
            person(comparable_experience_months=60),
        ),
        Dimension.EXPERIENCE,
    )
    assert result.status is RuleStatus.VIOLATED
    assert result.reason_code is ReasonCode.EXPERIENCE_MAX_VIOLATED


def test_a_non_comparable_experience_produces_neither_verdict():
    """No total means no arithmetic, and no arithmetic means no contradiction."""
    result = only(
        decide(
            offer(
                experience=(
                    ExperienceRequirementInput(36, None, RequirementKind.REQUIRED),
                )
            ),
            person(),
        ),
        Dimension.EXPERIENCE,
    )
    assert result.status is RuleStatus.UNKNOWN
    assert result.reason_code is ReasonCode.EXPERIENCE_NOT_COMPARABLE


def test_a_preferred_experience_never_blocks():
    result = only(
        decide(
            offer(
                experience=(
                    ExperienceRequirementInput(60, None, RequirementKind.PREFERRED),
                )
            ),
            person(comparable_experience_months=1),
        ),
        Dimension.EXPERIENCE,
    )
    assert result.status is RuleStatus.NOT_EVALUATED
    assert result.is_blocking is False


def test_an_unstated_obligation_is_not_evaluated():
    """A number is not a condition. "3+ years" with no word making it one is
    reported and not decided."""
    result = only(
        decide(
            offer(experience=(ExperienceRequirementInput(36, None, None),)),
            person(comparable_experience_months=1),
        ),
        Dimension.EXPERIENCE,
    )
    assert result.status is RuleStatus.NOT_EVALUATED
    assert result.reason_code is (
        ReasonCode.EXPERIENCE_OBLIGATION_UNSTATED_NOT_BLOCKING
    )


def test_two_experience_demands_produce_two_results():
    decision = decide(
        offer(
            experience=(
                ExperienceRequirementInput(84, None, RequirementKind.REQUIRED),
                ExperienceRequirementInput(24, None, RequirementKind.REQUIRED),
            )
        ),
        person(comparable_experience_months=36),
    )
    statuses = [item.status for item in results_for(decision, Dimension.EXPERIENCE)]
    assert statuses == [RuleStatus.VIOLATED, RuleStatus.SATISFIED]


def test_an_unquantified_required_experience_is_unknown():
    result = only(
        decide(
            offer(
                experience=(
                    ExperienceRequirementInput(None, None, RequirementKind.REQUIRED),
                )
            ),
            person(comparable_experience_months=120),
        ),
        Dimension.EXPERIENCE,
    )
    assert result.status is RuleStatus.UNKNOWN


# --------------------------------------------------------------------------
# Languages
# --------------------------------------------------------------------------


def test_language_requirement_absent_is_not_applicable():
    assert only(decide(offer(), person()), Dimension.LANGUAGE).status is (
        RuleStatus.NOT_APPLICABLE
    )


def test_a_higher_cefr_level_satisfies_a_required_one():
    result = only(
        decide(
            offer(
                languages=(
                    LanguageRequirementInput("english", RequirementKind.REQUIRED, "B2"),
                )
            ),
            person(languages=(ProfileLanguageInput("english", "C1"),)),
        ),
        Dimension.LANGUAGE,
    )
    assert result.status is RuleStatus.SATISFIED


def test_a_lower_cefr_level_contradicts_a_required_one():
    result = only(
        decide(
            offer(
                languages=(
                    LanguageRequirementInput("english", RequirementKind.REQUIRED, "B2"),
                )
            ),
            person(languages=(ProfileLanguageInput("english", "A2"),)),
        ),
        Dimension.LANGUAGE,
    )
    assert result.status is RuleStatus.VIOLATED
    assert result.reason_code is ReasonCode.LANGUAGE_REQUIRED_LEVEL_VIOLATED


def test_a_language_the_profile_never_names_is_unknown_not_violated():
    """A CV that does not list Spanish has not said its author lacks Spanish."""
    result = only(
        decide(
            offer(
                languages=(
                    LanguageRequirementInput("spanish", RequirementKind.REQUIRED, "B2"),
                )
            ),
            person(languages=(ProfileLanguageInput("english", "C1"),)),
        ),
        Dimension.LANGUAGE,
    )
    assert result.status is RuleStatus.UNKNOWN
    assert result.reason_code is ReasonCode.LANGUAGE_PROFILE_UNKNOWN


@pytest.mark.parametrize("stated", ["courant", "fluent", "native", "bilingue", None])
def test_a_level_that_is_not_cefr_is_not_compared(stated):
    """No mapping turns "fluent" into C1, here or anywhere in this project."""
    result = only(
        decide(
            offer(
                languages=(
                    LanguageRequirementInput("english", RequirementKind.REQUIRED, "C2"),
                )
            ),
            person(languages=(ProfileLanguageInput("english", stated),)),
        ),
        Dimension.LANGUAGE,
    )
    assert result.status is RuleStatus.UNKNOWN
    assert result.reason_code is ReasonCode.LANGUAGE_LEVEL_NOT_COMPARABLE


def test_a_required_language_with_no_readable_level_asks_only_for_the_language():
    result = only(
        decide(
            offer(
                languages=(
                    LanguageRequirementInput(
                        "french", RequirementKind.REQUIRED, "Courant"
                    ),
                )
            ),
            person(languages=(ProfileLanguageInput("french", "débutant"),)),
        ),
        Dimension.LANGUAGE,
    )
    assert result.status is RuleStatus.SATISFIED


def test_a_preferred_language_never_blocks_and_never_asks():
    """It does not contradict, and it does not make the verdict UNKNOWN either."""
    decision = decide(
        offer(
            languages=(
                LanguageRequirementInput("german", RequirementKind.PREFERRED, "C2"),
            )
        ),
        person(),
    )
    result = only(decision, Dimension.LANGUAGE)
    assert result.status is RuleStatus.NOT_EVALUATED
    assert result.is_blocking is False
    assert decision.status is GlobalStatus.ELIGIBLE


# --------------------------------------------------------------------------
# Work authorization
# --------------------------------------------------------------------------


def test_no_authorization_statement_is_not_applicable():
    assert only(decide(offer(), person()), Dimension.WORK_AUTHORIZATION).status is (
        RuleStatus.NOT_APPLICABLE
    )


def test_needing_sponsorship_where_none_is_offered_is_a_contradiction():
    result = only(
        decide(
            offer(visa_sponsorship="NOT_AVAILABLE"),
            person(sponsorship_need=SponsorshipNeed.YES),
        ),
        Dimension.WORK_AUTHORIZATION,
    )
    assert result.status is RuleStatus.VIOLATED


def test_not_needing_sponsorship_satisfies_a_demand_for_existing_authorization():
    result = only(
        decide(
            offer(work_authorization="REQUIRED"),
            person(sponsorship_need=SponsorshipNeed.NO),
        ),
        Dimension.WORK_AUTHORIZATION,
    )
    assert result.status is RuleStatus.SATISFIED


def test_an_unstated_sponsorship_need_is_unknown():
    result = only(
        decide(offer(work_authorization="REQUIRED"), person()),
        Dimension.WORK_AUTHORIZATION,
    )
    assert result.status is RuleStatus.UNKNOWN


def test_an_offered_sponsorship_relieves_the_constraint():
    """An employer's offer of help must never count against the applicant."""
    result = only(
        decide(
            offer(visa_sponsorship="AVAILABLE", work_authorization="REQUIRED"),
            person(sponsorship_need=SponsorshipNeed.YES),
        ),
        Dimension.WORK_AUTHORIZATION,
    )
    assert result.status is RuleStatus.SATISFIED
    assert result.reason_code is ReasonCode.WORK_AUTHORIZATION_SPONSORSHIP_OFFERED


def test_no_visa_conclusion_is_reachable_from_a_nationality_or_a_country():
    """The engine cannot make that inference: neither datum is an input at all.

    `ProfileEligibilityInput` has no nationality, no country and no address, and
    `OpportunityEligibilityInput` has no location. The test asserts the shape,
    because a rule cannot read what the contract does not carry.
    """
    forbidden = {"nationality", "country", "city", "address", "location", "postal_code"}
    assert not forbidden & set(ProfileEligibilityInput.__dataclass_fields__)
    assert not forbidden & set(OpportunityEligibilityInput.__dataclass_fields__)


# --------------------------------------------------------------------------
# Internship agreement
# --------------------------------------------------------------------------


def test_no_agreement_requirement_is_not_applicable():
    assert only(decide(offer(), person()), Dimension.CONVENTION).status is (
        RuleStatus.NOT_APPLICABLE
    )


def test_a_stated_inability_to_provide_an_agreement_is_a_contradiction():
    result = only(
        decide(
            offer(convention="REQUIRED"),
            person(convention_capability=ConventionCapability.NOT_AVAILABLE),
        ),
        Dimension.CONVENTION,
    )
    assert result.status is RuleStatus.VIOLATED


def test_a_stated_ability_satisfies_the_agreement_requirement():
    result = only(
        decide(
            offer(convention="REQUIRED"),
            person(convention_capability=ConventionCapability.AVAILABLE),
        ),
        Dimension.CONVENTION,
    )
    assert result.status is RuleStatus.SATISFIED


def test_being_a_student_does_not_imply_an_available_agreement():
    """Two separate facts. A school that will not sign is a real, common case."""
    decision = decide(
        offer(convention="REQUIRED", enrollment_required=True),
        person(currently_enrolled=True),
    )
    assert only(decision, Dimension.ENROLLMENT).status is RuleStatus.SATISFIED
    assert only(decision, Dimension.CONVENTION).status is RuleStatus.UNKNOWN
    assert decision.status is GlobalStatus.UNKNOWN


def test_a_posting_saying_no_agreement_is_needed_is_not_applicable():
    result = only(
        decide(
            offer(convention="NOT_REQUIRED"),
            person(convention_capability=ConventionCapability.NOT_AVAILABLE),
        ),
        Dimension.CONVENTION,
    )
    assert result.status is RuleStatus.NOT_APPLICABLE


# --------------------------------------------------------------------------
# Skills — advisory in v1, and never anything else
# --------------------------------------------------------------------------


def test_a_missing_required_skill_never_makes_a_verdict_ineligible():
    decision = decide(
        offer(
            skills=(
                SkillRequirementInput("python", RequirementKind.REQUIRED),
                SkillRequirementInput("spark", RequirementKind.REQUIRED),
            )
        ),
        person(),
    )
    assert decision.status is GlobalStatus.ELIGIBLE
    for result in results_for(decision, Dimension.SKILL):
        assert result.is_blocking is False
        assert result.status is RuleStatus.NOT_EVALUATED
        assert result.reason_code is (
            ReasonCode.REQUIRED_SKILL_NOT_VERIFIED_NON_BLOCKING
        )


def test_a_held_required_skill_is_recorded_as_positive_evidence_only():
    decision = decide(
        offer(skills=(SkillRequirementInput("python", RequirementKind.REQUIRED),)),
        person(skills=("python",)),
    )
    result = only(decision, Dimension.SKILL)
    assert result.status is RuleStatus.SATISFIED
    assert result.reason_code is ReasonCode.REQUIRED_SKILL_VERIFIED
    assert result.is_blocking is False
    assert decision.status is GlobalStatus.ELIGIBLE


def test_a_flattened_alternative_cannot_reject_anybody():
    """"AWS, Azure or GCP" written as three bullets is three REQUIRED rows.

    Holding one of them is what the posting asked for, and the two it did not
    ask for must not count against the candidate. Nothing in the stored shape
    tells that apart from three genuine obligations, which is exactly why no
    skill blocks in v1.
    """
    decision = decide(
        offer(
            skills=(
                SkillRequirementInput("aws", RequirementKind.REQUIRED),
                SkillRequirementInput("azure", RequirementKind.REQUIRED),
                SkillRequirementInput("gcp", RequirementKind.REQUIRED),
            )
        ),
        person(skills=("aws",)),
    )
    assert decision.status is GlobalStatus.ELIGIBLE
    assert decision.violated_count == 0
    assert decision.blocking_unknown_count == 0


def test_preferred_skills_carry_no_penalty_either_way():
    decision = decide(
        offer(
            skills=(
                SkillRequirementInput("tableau", RequirementKind.PREFERRED),
                SkillRequirementInput("looker", RequirementKind.PREFERRED),
            )
        ),
        person(skills=("tableau",)),
    )
    assert decision.status is GlobalStatus.ELIGIBLE
    reasons = {result.reason_code for result in results_for(decision, Dimension.SKILL)}
    assert reasons == {
        ReasonCode.PREFERRED_SKILL_VERIFIED,
        ReasonCode.PREFERRED_SKILL_NOT_BLOCKING,
    }


# --------------------------------------------------------------------------
# Ambiguities kept by 3.5B
# --------------------------------------------------------------------------


def test_an_ambiguity_is_reported_and_decides_nothing():
    decision = decide(
        offer(
            ambiguities=(
                RequirementAmbiguityInput("SKILL", "ALTERNATIVE_GROUP_UNSUPPORTED"),
            )
        ),
        person(),
    )
    result = only(decision, Dimension.AMBIGUITY)
    assert result.status is RuleStatus.NOT_EVALUATED
    assert result.reason_code is ReasonCode.AMBIGUOUS_REQUIREMENT_NOT_BLOCKING
    assert result.is_blocking is False


def test_an_ambiguity_never_downgrades_the_verdict():
    """Not to INELIGIBLE, and not to UNKNOWN either. It is an audit trail."""
    decision = decide(
        offer(
            ambiguities=(
                RequirementAmbiguityInput("SKILL", "ALTERNATIVE_GROUP_UNSUPPORTED"),
                RequirementAmbiguityInput(
                    "LANGUAGE", "CONFLICTING_LANGUAGE_PROFICIENCY"
                ),
                RequirementAmbiguityInput(
                    "SKILL", "COMPOUND_SKILL_EXPRESSION_UNSUPPORTED"
                ),
            )
        ),
        person(),
    )
    assert decision.status is GlobalStatus.ELIGIBLE
    assert decision.violated_count == 0
    assert decision.blocking_unknown_count == 0


# --------------------------------------------------------------------------
# Dimensions this version defers
# --------------------------------------------------------------------------


DEFERRED = (
    Dimension.MOBILITY,
    Dimension.LOCATION,
    Dimension.AVAILABILITY,
    Dimension.DURATION,
    Dimension.START_DATE,
    Dimension.WORK_MODE,
)


@pytest.mark.parametrize("dimension", DEFERRED)
def test_a_deferred_dimension_reports_and_never_decides(dimension):
    decision = decide(offer(), person())
    result = only(decision, dimension)
    assert result.status is RuleStatus.NOT_EVALUATED
    assert result.is_blocking is False
    assert decision.status is GlobalStatus.ELIGIBLE


def test_every_dimension_answers_on_every_posting():
    """Zero is an answer, and a uniform rule list is one an audit can query."""
    decision = decide(offer(), person())
    dimensions = [result.dimension for result in decision.results]
    for dimension in (
        Dimension.EDUCATION,
        Dimension.ENROLLMENT,
        Dimension.EXPERIENCE,
        Dimension.LANGUAGE,
        Dimension.WORK_AUTHORIZATION,
        Dimension.CONVENTION,
        *DEFERRED,
    ):
        assert dimensions.count(dimension) == 1


# --------------------------------------------------------------------------
# The value object refuses to be self-contradictory
# --------------------------------------------------------------------------


def test_a_non_hard_dimension_cannot_be_marked_blocking():
    with pytest.raises(EligibilityModelError):
        RuleResult(
            dimension=Dimension.SKILL,
            rule_code="X",
            status=RuleStatus.SATISFIED,
            reason_code=ReasonCode.REQUIRED_SKILL_VERIFIED,
            explanation="x",
            is_blocking=True,
            requirement_kind=RequirementKind.REQUIRED,
        )


def test_a_preferred_requirement_cannot_be_marked_blocking():
    with pytest.raises(EligibilityModelError):
        RuleResult(
            dimension=Dimension.LANGUAGE,
            rule_code="X",
            status=RuleStatus.SATISFIED,
            reason_code=ReasonCode.LANGUAGE_PREFERRED_NOT_BLOCKING,
            explanation="x",
            is_blocking=True,
            requirement_kind=RequirementKind.PREFERRED,
        )


def test_a_non_blocking_rule_cannot_be_violated():
    with pytest.raises(EligibilityModelError):
        RuleResult(
            dimension=Dimension.LANGUAGE,
            rule_code="X",
            status=RuleStatus.VIOLATED,
            reason_code=ReasonCode.LANGUAGE_REQUIRED_LEVEL_VIOLATED,
            explanation="x",
        )
