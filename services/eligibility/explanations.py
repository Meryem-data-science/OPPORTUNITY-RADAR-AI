"""Deterministic English renderings of a reason code.

No language model, no template engine, no translation table loaded from
anywhere: a dict of format strings and `str.format`. The same reason code and
the same details produce the same sentence on any machine, which is what makes a
stored `explanation` reproducible from the row beside it.

**The reason code is the business truth; this is a courtesy.** Nothing
downstream should parse these sentences, and changing the wording of one is not
a change of meaning — which is why the wording is *not* in the fingerprint and a
rewording does not recompute a single decision. Changing what a *code* means
would be, and that moves the engine version instead.

The sentences name levels, months, canonical skill keys and language keys. They
never quote a posting's words or a person's: the evidence fragments live in
`opportunity_skill_requirement_evidence`, `opportunity_language_requirement_evidence`
and `profile_facts`, which is where an auditor should read them, under whatever
access those tables have. Copying them into a column that a routine command
prints would spread a candidate's CV and an employer's advertisement across a
third place with no reason to hold them.
"""

from __future__ import annotations

from services.eligibility.models import ReasonCode

__all__ = ["EXPLANATION_TEMPLATES", "ExplanationError", "explain"]


class ExplanationError(KeyError):
    """Raised when a reason code has no template, or a template lacks a detail.

    Loud on purpose. A reason code added without a sentence would otherwise
    reach the database as an empty explanation, and a missing detail would
    silently produce a sentence with a hole in it.
    """


#: One sentence per reason code. Every code in `ReasonCode` appears exactly
#: once; a test walks the enum and fails if one does not.
EXPLANATION_TEMPLATES: dict[ReasonCode, str] = {
    ReasonCode.EDUCATION_REQUIREMENT_ABSENT: (
        "The posting states no education requirement."
    ),
    ReasonCode.EDUCATION_REQUIREMENT_SATISFIED: (
        "The posting accepts {accepted}, and the candidate's verified education "
        "includes {held}."
    ),
    ReasonCode.EDUCATION_REQUIREMENT_VIOLATED: (
        "The posting accepts {accepted}, and the candidate's verified education "
        "({held}) meets none of them."
    ),
    ReasonCode.EDUCATION_PROFILE_UNKNOWN: (
        "The posting accepts {accepted}, but the candidate's education level is "
        "not recorded in a comparable form, so this cannot be answered."
    ),
    ReasonCode.EDUCATION_NOT_COMPARABLE: (
        "The posting accepts {accepted} and the candidate holds {held}, but "
        "those levels sit on different scales and are not comparable."
    ),
    ReasonCode.ENROLLMENT_REQUIREMENT_ABSENT: (
        "The posting states no current-enrolment requirement."
    ),
    ReasonCode.ENROLLMENT_REQUIREMENT_SATISFIED: (
        "The posting requires a currently enrolled student, and the candidate is "
        "verified as currently enrolled."
    ),
    ReasonCode.ENROLLMENT_REQUIREMENT_VIOLATED: (
        "The posting requires a currently enrolled student, and the candidate is "
        "verified as not currently enrolled."
    ),
    ReasonCode.ENROLLMENT_STATUS_UNKNOWN: (
        "The posting requires a currently enrolled student, but the candidate's "
        "current enrolment is unknown."
    ),
    ReasonCode.EXPERIENCE_REQUIREMENT_ABSENT: (
        "The posting states no experience requirement."
    ),
    ReasonCode.EXPERIENCE_MIN_SATISFIED: (
        "The posting requires {bounds} of experience, and the candidate's "
        "comparable experience is {months} months."
    ),
    ReasonCode.EXPERIENCE_MIN_VIOLATED: (
        "The posting requires {bounds} of experience, and the candidate's "
        "comparable experience is {months} months."
    ),
    ReasonCode.EXPERIENCE_MAX_SATISFIED: (
        "The posting allows {bounds} of experience, and the candidate's "
        "comparable experience is {months} months."
    ),
    ReasonCode.EXPERIENCE_MAX_VIOLATED: (
        "The posting allows {bounds} of experience, and the candidate's "
        "comparable experience is {months} months."
    ),
    ReasonCode.EXPERIENCE_RANGE_SATISFIED: (
        "The posting requires {bounds} of experience, and the candidate's "
        "comparable experience is {months} months."
    ),
    ReasonCode.EXPERIENCE_NOT_COMPARABLE: (
        "The posting requires {bounds} of experience, but the candidate has no "
        "experience recorded in a form comparable to that requirement."
    ),
    ReasonCode.EXPERIENCE_PREFERRED_NOT_BLOCKING: (
        "The posting prefers {bounds} of experience. A preference never blocks."
    ),
    ReasonCode.EXPERIENCE_OBLIGATION_UNSTATED_NOT_BLOCKING: (
        "The posting mentions {bounds} of experience without stating it as a "
        "condition, so this version does not decide on it."
    ),
    ReasonCode.LANGUAGE_REQUIREMENT_ABSENT: (
        "The posting states no language requirement."
    ),
    ReasonCode.LANGUAGE_REQUIRED_SATISFIED: (
        "{language} is required at {level}, and the candidate's verified "
        "{language} meets it."
    ),
    ReasonCode.LANGUAGE_REQUIRED_LEVEL_VIOLATED: (
        "{language} is required at {level}, and the candidate's verified "
        "{language} level is {held}."
    ),
    ReasonCode.LANGUAGE_PROFILE_UNKNOWN: (
        "{language} is required at {level}, but the candidate's verified "
        "{language} is unknown. An unlisted language is not a language the "
        "candidate lacks."
    ),
    ReasonCode.LANGUAGE_LEVEL_NOT_COMPARABLE: (
        "{language} is required at {level}, and the candidate states a "
        "{language} level that is not expressed on that scale."
    ),
    ReasonCode.LANGUAGE_PREFERRED_NOT_BLOCKING: (
        "{language} is preferred, not required. A preference never blocks."
    ),
    ReasonCode.WORK_AUTHORIZATION_REQUIREMENT_ABSENT: (
        "The posting states no work-authorization requirement."
    ),
    ReasonCode.WORK_AUTHORIZATION_SATISFIED: (
        "The posting requires existing work authorization, and the candidate "
        "stated they do not need visa sponsorship."
    ),
    ReasonCode.WORK_AUTHORIZATION_VIOLATED: (
        "The posting requires existing work authorization and offers no "
        "sponsorship, and the candidate stated they need visa sponsorship."
    ),
    ReasonCode.WORK_AUTHORIZATION_UNKNOWN: (
        "The posting requires existing work authorization, but the candidate has "
        "stated nothing about needing visa sponsorship."
    ),
    ReasonCode.WORK_AUTHORIZATION_SPONSORSHIP_OFFERED: (
        "The posting states that visa sponsorship is available, so existing work "
        "authorization is not a blocker."
    ),
    ReasonCode.CONVENTION_REQUIREMENT_ABSENT: (
        "The posting states no internship-agreement requirement."
    ),
    ReasonCode.CONVENTION_SATISFIED: (
        "The posting requires a school internship agreement, and the candidate "
        "stated they can provide one."
    ),
    ReasonCode.CONVENTION_VIOLATED: (
        "The posting requires a school internship agreement, and the candidate "
        "stated they cannot provide one."
    ),
    ReasonCode.CONVENTION_STATUS_UNKNOWN: (
        "The posting requires a school internship agreement, but the candidate "
        "has stated nothing about being able to provide one."
    ),
    ReasonCode.REQUIRED_SKILL_VERIFIED: (
        "The posting requires {skill}, and the candidate's profile holds it."
    ),
    ReasonCode.REQUIRED_SKILL_NOT_VERIFIED_NON_BLOCKING: (
        "The posting requires {skill} and the candidate's profile does not hold "
        "it. This version never blocks on a skill, because a posting's "
        "alternatives are not yet represented without loss."
    ),
    ReasonCode.PREFERRED_SKILL_VERIFIED: (
        "The posting prefers {skill}, and the candidate's profile holds it."
    ),
    ReasonCode.PREFERRED_SKILL_NOT_BLOCKING: (
        "The posting prefers {skill}. A preference never blocks."
    ),
    ReasonCode.AMBIGUOUS_REQUIREMENT_NOT_BLOCKING: (
        "Phase 3.5B recorded a {kind} requirement it declined to represent "
        "({reason}). An unrepresented demand is never treated as a requirement."
    ),
    ReasonCode.MOBILITY_NOT_EVALUATED_V1: (
        "Mobility is not evaluated by eligibility-rules-v1."
    ),
    ReasonCode.LOCATION_NOT_EVALUATED_V1: (
        "Location compatibility is not evaluated by eligibility-rules-v1."
    ),
    ReasonCode.AVAILABILITY_NOT_EVALUATED_V1: (
        "Availability is not evaluated by eligibility-rules-v1."
    ),
    ReasonCode.DURATION_NOT_EVALUATED_V1: (
        "Duration compatibility is not evaluated by eligibility-rules-v1."
    ),
    ReasonCode.START_DATE_NOT_EVALUATED_V1: (
        "Start-date compatibility is not evaluated by eligibility-rules-v1."
    ),
    ReasonCode.WORK_MODE_NOT_EVALUATED_V1: (
        "Work-mode compatibility is not evaluated by eligibility-rules-v1."
    ),
}

#: What the templates say when a detail is genuinely absent — a posting that
#: required a language and named no level, for instance. `None` must not reach
#: `str.format`, which would render the word "None" inside a sentence.
_ABSENT = "an unspecified level"

#: The longest sentence the schema accepts. A detail long enough to overflow is
#: truncated rather than allowed to fail an insert: an explanation is a
#: convenience and must never be the reason a correct verdict cannot be stored.
MAX_EXPLANATION_LENGTH = 400


def explain(reason_code: ReasonCode, **detail: object) -> str:
    """The sentence for this reason code, filled from these details."""
    try:
        template = EXPLANATION_TEMPLATES[reason_code]
    except KeyError as error:
        raise ExplanationError(
            f"{reason_code} has no explanation template"
        ) from error
    values = {
        key: _ABSENT if value is None else value for key, value in detail.items()
    }
    try:
        rendered = template.format(**values)
    except KeyError as error:
        raise ExplanationError(
            f"{reason_code} needs detail {error.args[0]!r}"
        ) from error
    if len(rendered) > MAX_EXPLANATION_LENGTH:
        return rendered[: MAX_EXPLANATION_LENGTH - 1] + "…"
    return rendered
