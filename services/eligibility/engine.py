"""The rules of `eligibility-rules-v1`, and the aggregator over them.

Pure. Nothing in this module opens a database, reads a clock, touches the
network or looks at anything but the `EligibilityInput` it is handed — which is
what lets the whole truth table be tested in memory, and what makes the
fingerprint in `fingerprint.py` a promise rather than a hope: the same input
gives the same verdict, the same reasons in the same order, forever.

Three rules govern everything below.

**A contradiction is not a gap.** `VIOLATED` requires all six of: an explicit
REQUIRED demand on the offer side; that demand not being one 3.5B refused to
represent; the dimension being one of the six this version supports; the
person's side holding the fact; the two being genuinely comparable; and the
comparison coming out against. Miss any one and the answer is UNKNOWN,
NOT_APPLICABLE or NOT_EVALUATED. `RuleResult` enforces the parts of that it
can, and `migrations/0014` enforces them again in the schema.

**Nothing stops at the first blocker.** A posting contradicting three
requirements produces three VIOLATED results, not one and an early return. The
verdict needs the first; the person reading it needs all three.

**Every dimension answers, on every posting.** A posting silent about
languages produces a NOT_APPLICABLE language result rather than no result at
all, and the six deferred dimensions produce a NOT_EVALUATED result each. Zero
is an answer, and a decision whose rule list is uniform is one an audit can
query without knowing which rules happened to run.
"""

from __future__ import annotations

from services.eligibility.comparison import (
    compare_cefr,
    compare_education_levels,
    parse_cefr,
)
from services.eligibility.explanations import explain
from services.eligibility.fingerprint import eligibility_fingerprint
from services.eligibility.models import (
    DEFERRED_DIMENSIONS,
    ConventionCapability,
    Dimension,
    EligibilityDecision,
    EligibilityInput,
    ExperienceRequirementInput,
    GlobalStatus,
    LanguageRequirementInput,
    ProfileEligibilityInput,
    ReasonCode,
    RequirementKind,
    RuleResult,
    RuleStatus,
    SponsorshipNeed,
)

__all__ = ["DEFERRED_REASONS", "aggregate", "evaluate_eligibility"]

#: Which reason each deferred dimension reports. Deferring is a decision this
#: version makes on purpose, so it is stated per dimension rather than
#: generated from the name: the day one of them is implemented, its entry
#: leaves this table and the rule takes its place.
DEFERRED_REASONS: dict[Dimension, ReasonCode] = {
    Dimension.MOBILITY: ReasonCode.MOBILITY_NOT_EVALUATED_V1,
    Dimension.LOCATION: ReasonCode.LOCATION_NOT_EVALUATED_V1,
    Dimension.AVAILABILITY: ReasonCode.AVAILABILITY_NOT_EVALUATED_V1,
    Dimension.DURATION: ReasonCode.DURATION_NOT_EVALUATED_V1,
    Dimension.START_DATE: ReasonCode.START_DATE_NOT_EVALUATED_V1,
    Dimension.WORK_MODE: ReasonCode.WORK_MODE_NOT_EVALUATED_V1,
}


def _result(
    dimension: Dimension,
    rule_code: str,
    status: RuleStatus,
    reason_code: ReasonCode,
    *,
    is_blocking: bool = False,
    requirement_kind: RequirementKind | None = None,
    requirement_ref: str | None = None,
    profile_ref: str | None = None,
    **detail: object,
) -> RuleResult:
    """One result, with its explanation rendered from the same reason and detail."""
    return RuleResult(
        dimension=dimension,
        rule_code=rule_code,
        status=status,
        reason_code=reason_code,
        explanation=explain(reason_code, **detail),
        is_blocking=is_blocking,
        requirement_kind=requirement_kind,
        requirement_ref=requirement_ref,
        profile_ref=profile_ref,
    )


def _education_results(
    opportunity, profile: ProfileEligibilityInput
) -> tuple[RuleResult, ...]:
    """One result for the education demand as a whole.

    The several rows `0012` stores are several **accepted** levels — "Bac+5 or
    Master" — so they are read as a disjunction: holding any one of them
    satisfies the demand. The consequence for the other direction matters more:
    a contradiction needs *every* accepted level to be both comparable and
    unmet, because one level this engine cannot compare is one route to the job
    it cannot rule out.
    """
    if not opportunity.education:
        return (
            _result(
                Dimension.EDUCATION,
                "EDUCATION_V1",
                RuleStatus.NOT_APPLICABLE,
                ReasonCode.EDUCATION_REQUIREMENT_ABSENT,
            ),
        )

    accepted = ", ".join(
        f"{item.level}({item.mode})" for item in opportunity.education
    )
    requirement_ref = "EDUCATION#" + "|".join(
        f"{item.level}:{item.mode}" for item in opportunity.education
    )
    if not profile.education_levels:
        # Empty is UNKNOWN, never "no degree": Phase 3.4 stores no level at all,
        # so there is nothing here that could contradict anything.
        return (
            _result(
                Dimension.EDUCATION,
                "EDUCATION_V1",
                RuleStatus.UNKNOWN,
                ReasonCode.EDUCATION_PROFILE_UNKNOWN,
                is_blocking=True,
                requirement_kind=RequirementKind.REQUIRED,
                requirement_ref=requirement_ref,
                accepted=accepted,
            ),
        )

    incomparable = False
    for requirement in opportunity.education:
        minimum = requirement.mode == "MINIMUM"
        for held in profile.education_levels:
            verdict = compare_education_levels(held, requirement.level, minimum=minimum)
            if verdict is None:
                incomparable = True
            elif verdict:
                return (
                    _result(
                        Dimension.EDUCATION,
                        "EDUCATION_V1",
                        RuleStatus.SATISFIED,
                        ReasonCode.EDUCATION_REQUIREMENT_SATISFIED,
                        is_blocking=True,
                        requirement_kind=RequirementKind.REQUIRED,
                        requirement_ref=requirement_ref,
                        profile_ref="profile_educations",
                        accepted=accepted,
                        held=held,
                    ),
                )

    if incomparable:
        return (
            _result(
                Dimension.EDUCATION,
                "EDUCATION_V1",
                RuleStatus.UNKNOWN,
                ReasonCode.EDUCATION_NOT_COMPARABLE,
                is_blocking=True,
                requirement_kind=RequirementKind.REQUIRED,
                requirement_ref=requirement_ref,
                profile_ref="profile_educations",
                accepted=accepted,
                held=", ".join(profile.education_levels),
            ),
        )
    return (
        _result(
            Dimension.EDUCATION,
            "EDUCATION_V1",
            RuleStatus.VIOLATED,
            ReasonCode.EDUCATION_REQUIREMENT_VIOLATED,
            is_blocking=True,
            requirement_kind=RequirementKind.REQUIRED,
            requirement_ref=requirement_ref,
            profile_ref="profile_educations",
            accepted=accepted,
            held=", ".join(profile.education_levels),
        ),
    )


def _enrollment_results(
    opportunity, profile: ProfileEligibilityInput
) -> tuple[RuleResult, ...]:
    """Whether the posting demands a current student, and whether this is one.

    Current enrolment is not an education level and is never derived from one:
    a finished Bac+5 says the person graduated, which is the opposite of a
    current registration as often as it is a sign of one.
    """
    if opportunity.enrollment_required is not True:
        return (
            _result(
                Dimension.ENROLLMENT,
                "ENROLLMENT_V1",
                RuleStatus.NOT_APPLICABLE,
                ReasonCode.ENROLLMENT_REQUIREMENT_ABSENT,
            ),
        )
    if profile.currently_enrolled is None:
        return (
            _result(
                Dimension.ENROLLMENT,
                "ENROLLMENT_V1",
                RuleStatus.UNKNOWN,
                ReasonCode.ENROLLMENT_STATUS_UNKNOWN,
                is_blocking=True,
                requirement_kind=RequirementKind.REQUIRED,
                requirement_ref="ENROLLMENT#REQUIRED",
            ),
        )
    satisfied = profile.currently_enrolled
    return (
        _result(
            Dimension.ENROLLMENT,
            "ENROLLMENT_V1",
            RuleStatus.SATISFIED if satisfied else RuleStatus.VIOLATED,
            ReasonCode.ENROLLMENT_REQUIREMENT_SATISFIED
            if satisfied
            else ReasonCode.ENROLLMENT_REQUIREMENT_VIOLATED,
            is_blocking=True,
            requirement_kind=RequirementKind.REQUIRED,
            requirement_ref="ENROLLMENT#REQUIRED",
            profile_ref="profile_enrollment",
        ),
    )


def _experience_reference(requirement: ExperienceRequirementInput) -> str:
    low = "" if requirement.min_months is None else str(requirement.min_months)
    high = "" if requirement.max_months is None else str(requirement.max_months)
    return f"EXPERIENCE#{low}-{high}"


def _experience_results(
    opportunity, profile: ProfileEligibilityInput
) -> tuple[RuleResult, ...]:
    """One result per experience demand the posting made.

    `0012` stores several rows because a posting asking for "7+ years of
    engineering" and "2+ years of ML" is asking for two things. Each gets its
    own result here, for the same reason.

    Only a demand the posting marked REQUIRED can block. A PREFERRED one is
    reported and never decides; one whose obligation 3.5A could not read —
    a bare "3+ years" with no word making it a condition — is NOT_EVALUATED,
    because the strict test for a contradiction starts with an explicit
    obligation and a number alone is not one.

    A quantity is compared only when the caller supplied one it asserts is
    comparable. The loader never does, and the docstring of
    `ProfileEligibilityInput` says why: a posting's "3 years of data
    engineering" is not answered by a total of every month a person has worked,
    and this version will not pretend otherwise.
    """
    if not opportunity.experience:
        return (
            _result(
                Dimension.EXPERIENCE,
                "EXPERIENCE_V1",
                RuleStatus.NOT_APPLICABLE,
                ReasonCode.EXPERIENCE_REQUIREMENT_ABSENT,
            ),
        )

    results: list[RuleResult] = []
    for requirement in opportunity.experience:
        reference = _experience_reference(requirement)
        bounds = _bounds_text(requirement)
        if requirement.kind is RequirementKind.PREFERRED:
            results.append(
                _result(
                    Dimension.EXPERIENCE,
                    "EXPERIENCE_V1",
                    RuleStatus.NOT_EVALUATED,
                    ReasonCode.EXPERIENCE_PREFERRED_NOT_BLOCKING,
                    requirement_kind=RequirementKind.PREFERRED,
                    requirement_ref=reference,
                    bounds=bounds,
                )
            )
            continue
        if requirement.kind is None:
            results.append(
                _result(
                    Dimension.EXPERIENCE,
                    "EXPERIENCE_V1",
                    RuleStatus.NOT_EVALUATED,
                    ReasonCode.EXPERIENCE_OBLIGATION_UNSTATED_NOT_BLOCKING,
                    requirement_ref=reference,
                    bounds=bounds,
                )
            )
            continue
        if requirement.min_months is None and requirement.max_months is None:
            # "Experience is required", with no quantity. There is nothing to
            # compare, so there is nothing that could contradict it.
            results.append(
                _result(
                    Dimension.EXPERIENCE,
                    "EXPERIENCE_V1",
                    RuleStatus.UNKNOWN,
                    ReasonCode.EXPERIENCE_NOT_COMPARABLE,
                    is_blocking=True,
                    requirement_kind=RequirementKind.REQUIRED,
                    requirement_ref=reference,
                    bounds=bounds,
                )
            )
            continue
        months = profile.comparable_experience_months
        if months is None:
            results.append(
                _result(
                    Dimension.EXPERIENCE,
                    "EXPERIENCE_V1",
                    RuleStatus.UNKNOWN,
                    ReasonCode.EXPERIENCE_NOT_COMPARABLE,
                    is_blocking=True,
                    requirement_kind=RequirementKind.REQUIRED,
                    requirement_ref=reference,
                    profile_ref="profile_experiences",
                    bounds=bounds,
                )
            )
            continue
        results.append(
            _compare_experience(requirement, months, reference, bounds)
        )
    return tuple(results)


def _bounds_text(requirement: ExperienceRequirementInput) -> str:
    if requirement.min_months is not None and requirement.max_months is not None:
        return f"between {requirement.min_months} and {requirement.max_months} months"
    if requirement.min_months is not None:
        return f"at least {requirement.min_months} months"
    if requirement.max_months is not None:
        return f"at most {requirement.max_months} months"
    return "an unquantified amount"


def _compare_experience(
    requirement: ExperienceRequirementInput,
    months: int,
    reference: str,
    bounds: str,
) -> RuleResult:
    """The arithmetic, once both sides are known to be comparable quantities."""
    below = requirement.min_months is not None and months < requirement.min_months
    above = requirement.max_months is not None and months > requirement.max_months
    if below or above:
        reason = (
            ReasonCode.EXPERIENCE_MIN_VIOLATED
            if below
            else ReasonCode.EXPERIENCE_MAX_VIOLATED
        )
        status = RuleStatus.VIOLATED
    else:
        if requirement.min_months is not None and requirement.max_months is not None:
            reason = ReasonCode.EXPERIENCE_RANGE_SATISFIED
        elif requirement.min_months is not None:
            reason = ReasonCode.EXPERIENCE_MIN_SATISFIED
        else:
            reason = ReasonCode.EXPERIENCE_MAX_SATISFIED
        status = RuleStatus.SATISFIED
    return _result(
        Dimension.EXPERIENCE,
        "EXPERIENCE_V1",
        status,
        reason,
        is_blocking=True,
        requirement_kind=RequirementKind.REQUIRED,
        requirement_ref=reference,
        profile_ref="profile_experiences",
        bounds=bounds,
        months=months,
    )


def _language_results(
    opportunity, profile: ProfileEligibilityInput
) -> tuple[RuleResult, ...]:
    """One result per language the posting named.

    The one case worth stating twice: a language the person's Digital Twin never
    mentions produces UNKNOWN, never VIOLATED. A CV that does not list Spanish
    has not said its author cannot speak Spanish, and a posting requiring
    Spanish is a question to put to them, not grounds to drop the posting.
    """
    if not opportunity.languages:
        return (
            _result(
                Dimension.LANGUAGE,
                "LANGUAGE_V1",
                RuleStatus.NOT_APPLICABLE,
                ReasonCode.LANGUAGE_REQUIREMENT_ABSENT,
            ),
        )
    held = {item.language_key: item for item in profile.languages}
    return tuple(
        _language_result(requirement, held.get(requirement.language_key))
        for requirement in opportunity.languages
    )


def _language_result(
    requirement: LanguageRequirementInput, held
) -> RuleResult:
    reference = f"LANGUAGE#{requirement.language_key}"
    profile_reference = (
        None if held is None else f"profile_languages#{requirement.language_key}"
    )
    if requirement.kind is RequirementKind.PREFERRED:
        return _result(
            Dimension.LANGUAGE,
            "LANGUAGE_V1",
            RuleStatus.NOT_EVALUATED,
            ReasonCode.LANGUAGE_PREFERRED_NOT_BLOCKING,
            requirement_kind=RequirementKind.PREFERRED,
            requirement_ref=reference,
            profile_ref=profile_reference,
            language=requirement.language_key,
        )

    required_level = parse_cefr(requirement.proficiency_text)
    if held is None:
        return _result(
            Dimension.LANGUAGE,
            "LANGUAGE_V1",
            RuleStatus.UNKNOWN,
            ReasonCode.LANGUAGE_PROFILE_UNKNOWN,
            is_blocking=True,
            requirement_kind=RequirementKind.REQUIRED,
            requirement_ref=reference,
            language=requirement.language_key,
            level=required_level or requirement.proficiency_text,
        )
    if required_level is None:
        # The posting asked for the language and named no level this engine can
        # read. Holding the language is the whole of what was demanded.
        return _result(
            Dimension.LANGUAGE,
            "LANGUAGE_V1",
            RuleStatus.SATISFIED,
            ReasonCode.LANGUAGE_REQUIRED_SATISFIED,
            is_blocking=True,
            requirement_kind=RequirementKind.REQUIRED,
            requirement_ref=reference,
            profile_ref=profile_reference,
            language=requirement.language_key,
            level=requirement.proficiency_text,
        )
    held_level = parse_cefr(held.proficiency_text)
    if held_level is None:
        return _result(
            Dimension.LANGUAGE,
            "LANGUAGE_V1",
            RuleStatus.UNKNOWN,
            ReasonCode.LANGUAGE_LEVEL_NOT_COMPARABLE,
            is_blocking=True,
            requirement_kind=RequirementKind.REQUIRED,
            requirement_ref=reference,
            profile_ref=profile_reference,
            language=requirement.language_key,
            level=required_level,
        )
    reaches = compare_cefr(held_level, required_level)
    return _result(
        Dimension.LANGUAGE,
        "LANGUAGE_V1",
        RuleStatus.SATISFIED if reaches else RuleStatus.VIOLATED,
        ReasonCode.LANGUAGE_REQUIRED_SATISFIED
        if reaches
        else ReasonCode.LANGUAGE_REQUIRED_LEVEL_VIOLATED,
        is_blocking=True,
        requirement_kind=RequirementKind.REQUIRED,
        requirement_ref=reference,
        profile_ref=profile_reference,
        language=requirement.language_key,
        level=required_level,
        held=held_level,
    )


def _work_authorization_results(
    opportunity, profile: ProfileEligibilityInput
) -> tuple[RuleResult, ...]:
    """Whether the posting's stated authorization terms and this person's stated
    need can both be true.

    Only two explicit statements are read, both of them written by the employer:
    `work_authorization`, what the applicant must already hold, and
    `visa_sponsorship`, what the company will do. On the other side, exactly one:
    `profile_preferences.visa_sponsorship_required`, what the person said they
    need. `0012` names all three and says no rule derives any of them from
    another; this rule adds none.

    Nothing here reads a nationality, a country of residence, an address, a
    posting's location or a name — none of those is in `ProfileEligibilityInput`
    at all, so the inference cannot be made even by accident. "Moroccan
    national, job in France, therefore a visa is needed" is precisely the
    reasoning this engine refuses.

    A company offering sponsorship relieves the constraint even when it also
    says authorization is required. Reading that combination as a blocker would
    make an employer's own offer of help count against the applicant.
    """
    sponsorship_offered = opportunity.visa_sponsorship == "AVAILABLE"
    demands_existing = (
        opportunity.work_authorization == "REQUIRED"
        or opportunity.visa_sponsorship == "NOT_AVAILABLE"
    )
    reference = (
        f"WORK_AUTHORIZATION#{opportunity.work_authorization}"
        f"/SPONSORSHIP#{opportunity.visa_sponsorship}"
    )
    if sponsorship_offered:
        return (
            _result(
                Dimension.WORK_AUTHORIZATION,
                "WORK_AUTHORIZATION_V1",
                RuleStatus.SATISFIED,
                ReasonCode.WORK_AUTHORIZATION_SPONSORSHIP_OFFERED,
                is_blocking=True,
                requirement_kind=RequirementKind.REQUIRED,
                requirement_ref=reference,
            ),
        )
    if not demands_existing:
        return (
            _result(
                Dimension.WORK_AUTHORIZATION,
                "WORK_AUTHORIZATION_V1",
                RuleStatus.NOT_APPLICABLE,
                ReasonCode.WORK_AUTHORIZATION_REQUIREMENT_ABSENT,
            ),
        )
    profile_reference = "profile_preferences#visa_sponsorship_required"
    if profile.sponsorship_need is SponsorshipNeed.UNKNOWN:
        return (
            _result(
                Dimension.WORK_AUTHORIZATION,
                "WORK_AUTHORIZATION_V1",
                RuleStatus.UNKNOWN,
                ReasonCode.WORK_AUTHORIZATION_UNKNOWN,
                is_blocking=True,
                requirement_kind=RequirementKind.REQUIRED,
                requirement_ref=reference,
            ),
        )
    needs_sponsorship = profile.sponsorship_need is SponsorshipNeed.YES
    return (
        _result(
            Dimension.WORK_AUTHORIZATION,
            "WORK_AUTHORIZATION_V1",
            RuleStatus.VIOLATED if needs_sponsorship else RuleStatus.SATISFIED,
            ReasonCode.WORK_AUTHORIZATION_VIOLATED
            if needs_sponsorship
            else ReasonCode.WORK_AUTHORIZATION_SATISFIED,
            is_blocking=True,
            requirement_kind=RequirementKind.REQUIRED,
            requirement_ref=reference,
            profile_ref=profile_reference,
        ),
    )


def _convention_results(
    opportunity, profile: ProfileEligibilityInput
) -> tuple[RuleResult, ...]:
    """Whether a demanded school agreement is one this person can supply.

    Two separate facts, and the rule keeps them separate: being a student does
    not mean a school will sign, so `convention_capability` is what the person
    stated about the agreement and nothing else stands in for it.
    """
    if opportunity.convention != "REQUIRED":
        return (
            _result(
                Dimension.CONVENTION,
                "CONVENTION_V1",
                RuleStatus.NOT_APPLICABLE,
                ReasonCode.CONVENTION_REQUIREMENT_ABSENT,
            ),
        )
    reference = "CONVENTION#REQUIRED"
    if profile.convention_capability is ConventionCapability.UNKNOWN:
        return (
            _result(
                Dimension.CONVENTION,
                "CONVENTION_V1",
                RuleStatus.UNKNOWN,
                ReasonCode.CONVENTION_STATUS_UNKNOWN,
                is_blocking=True,
                requirement_kind=RequirementKind.REQUIRED,
                requirement_ref=reference,
            ),
        )
    available = profile.convention_capability is ConventionCapability.AVAILABLE
    return (
        _result(
            Dimension.CONVENTION,
            "CONVENTION_V1",
            RuleStatus.SATISFIED if available else RuleStatus.VIOLATED,
            ReasonCode.CONVENTION_SATISFIED
            if available
            else ReasonCode.CONVENTION_VIOLATED,
            is_blocking=True,
            requirement_kind=RequirementKind.REQUIRED,
            requirement_ref=reference,
            profile_ref="profile_preferences#convention_status",
        ),
    )


def _skill_results(
    opportunity, profile: ProfileEligibilityInput
) -> tuple[RuleResult, ...]:
    """One advisory result per skill the posting named. None of them can block.

    This is the deliberate limitation of v1, and it is a limitation on the side
    of not rejecting anybody. `0013` stores "Python or R required" as **no**
    skill row and an ambiguity, but an employer who wrote "AWS, Azure or GCP" in
    three separate bullets leaves three REQUIRED rows that look like three
    obligations and are one choice. Nothing in the stored shape tells the two
    apart, so treating a missing REQUIRED skill as a contradiction would reject
    candidates the posting would have accepted. Until alternative groups are
    representable without loss, a skill is evidence and never a verdict.

    A verified skill is still worth recording: it is the positive half of the
    same evidence, and Phase 4 will want it.
    """
    results: list[RuleResult] = []
    held = frozenset(profile.skills)
    for requirement in opportunity.skills:
        verified = requirement.canonical_key in held
        required = requirement.kind is RequirementKind.REQUIRED
        if required:
            reason = (
                ReasonCode.REQUIRED_SKILL_VERIFIED
                if verified
                else ReasonCode.REQUIRED_SKILL_NOT_VERIFIED_NON_BLOCKING
            )
        else:
            reason = (
                ReasonCode.PREFERRED_SKILL_VERIFIED
                if verified
                else ReasonCode.PREFERRED_SKILL_NOT_BLOCKING
            )
        results.append(
            _result(
                Dimension.SKILL,
                "SKILL_ADVISORY_V1",
                RuleStatus.SATISFIED if verified else RuleStatus.NOT_EVALUATED,
                reason,
                requirement_kind=requirement.kind,
                requirement_ref=f"SKILL#{requirement.canonical_key}",
                profile_ref=(
                    f"profile_skills#{requirement.canonical_key}" if verified else None
                ),
                skill=requirement.canonical_key,
            )
        )
    return tuple(results)


def _ambiguity_results(opportunity) -> tuple[RuleResult, ...]:
    """One result per demand 3.5B refused to represent.

    A refusal is not a weak requirement and must never be promoted into one.
    3.5B recorded that a posting asked for something it could not store without
    corrupting it — "Python or R", one language demanded at two incompatible
    levels — and the honest thing for this phase to do with that is to say so
    and decide nothing. It does not make the verdict UNKNOWN either: an
    unrepresentable demand is not the same as a real requirement whose answer is
    missing, and treating it as one would make an audit trail degrade every
    posting that has one.
    """
    return tuple(
        _result(
            Dimension.AMBIGUITY,
            "AMBIGUITY_V1",
            RuleStatus.NOT_EVALUATED,
            ReasonCode.AMBIGUOUS_REQUIREMENT_NOT_BLOCKING,
            requirement_ref=f"AMBIGUITY#{item.kind}:{item.reason}",
            kind=item.kind,
            reason=item.reason,
        )
        for item in opportunity.ambiguities
    )


def _deferred_results() -> tuple[RuleResult, ...]:
    """The six dimensions this version reports and refuses to decide.

    They are emitted for every posting, including postings that state nothing
    about them, so that "v1 did not evaluate this" is a row an audit can find
    rather than a silence it has to infer. None of them can block: the schema
    forbids it as well as this code.
    """
    return tuple(
        _result(
            dimension,
            f"{dimension.value}_DEFERRED_V1",
            RuleStatus.NOT_EVALUATED,
            DEFERRED_REASONS[dimension],
        )
        for dimension in DEFERRED_DIMENSIONS
    )


def aggregate(results: tuple[RuleResult, ...]) -> GlobalStatus:
    """The verdict, from the blocking rules only.

        a hard rule was contradicted   -> INELIGIBLE
        a hard rule went unanswered    -> UNKNOWN
        neither                        -> ELIGIBLE

    Nothing else votes. A posting with a hundred unverified skills, a dozen
    ambiguities and six deferred dimensions is ELIGIBLE if no hard rule was
    contradicted and none was left unanswered — because nothing known stands in
    the way, and that is all this verdict claims.
    """
    blocking = [result for result in results if result.is_blocking]
    if any(result.status is RuleStatus.VIOLATED for result in blocking):
        return GlobalStatus.INELIGIBLE
    if any(result.status is RuleStatus.UNKNOWN for result in blocking):
        return GlobalStatus.UNKNOWN
    return GlobalStatus.ELIGIBLE


def evaluate_eligibility(inputs: EligibilityInput) -> EligibilityDecision:
    """Run every rule over one posting and one person, and aggregate.

    The rule order is fixed — the six hard dimensions, then skills, then
    ambiguities, then the deferred six — so a decision's stored `position`
    column means the same thing on every row of every run, and two evaluations
    of one unchanged input are identical down to the row order.
    """
    opportunity = inputs.opportunity
    profile = inputs.profile
    results: tuple[RuleResult, ...] = (
        *_education_results(opportunity, profile),
        *_enrollment_results(opportunity, profile),
        *_experience_results(opportunity, profile),
        *_language_results(opportunity, profile),
        *_work_authorization_results(opportunity, profile),
        *_convention_results(opportunity, profile),
        *_skill_results(opportunity, profile),
        *_ambiguity_results(opportunity),
        *_deferred_results(),
    )
    return EligibilityDecision(
        opportunity_id=opportunity.opportunity_id,
        profile_id=profile.profile_id,
        status=aggregate(results),
        input_fingerprint=eligibility_fingerprint(inputs),
        results=results,
        engine_version=inputs.engine_version,
    )
