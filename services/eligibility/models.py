"""The vocabulary of Phase 3.6: statuses, dimensions, reasons, and the two
canonical inputs the engine is allowed to look at.

Nothing here opens a database, reads a clock or touches the network. These are
value objects, and that is what makes the engine in `engine.py` a pure function
of them — the same inputs give the same verdict, the same reasons and the same
fingerprint, on any machine, in any order, forever.

**The two input dataclasses are the contract of the whole phase.** A rule may
read a field of `OpportunityEligibilityInput` or `ProfileEligibilityInput` and
nothing else. There is no connection to reach through, no lazy loader and no
`extra` dict, so a rule cannot quietly start depending on a nationality, an
address, a telephone number or a preference — and the fingerprint, which is
computed over exactly these two objects plus the engine version, therefore
covers exactly what was read. A field added to a rule without being added here
does not compile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "ELIGIBILITY_ENGINE_VERSION",
    "HARD_DIMENSIONS",
    "ConventionCapability",
    "Dimension",
    "EducationRequirementInput",
    "EligibilityDecision",
    "EligibilityInput",
    "EligibilityModelError",
    "ExperienceRequirementInput",
    "GlobalStatus",
    "LanguageRequirementInput",
    "OpportunityEligibilityInput",
    "ProfileEligibilityInput",
    "ProfileLanguageInput",
    "ReasonCode",
    "RequirementAmbiguityInput",
    "RequirementKind",
    "RuleResult",
    "RuleStatus",
    "SkillRequirementInput",
    "SponsorshipNeed",
]

#: The version of the *rules*, not of the code around them. It participates in
#: the fingerprint, so moving it recomputes every decision — which is exactly
#: what a change of business meaning deserves. Adding a reason code, changing a
#: truth table, widening a hard dimension: all of those move this string. A
#: refactor that cannot change any verdict does not.
ELIGIBILITY_ENGINE_VERSION = "eligibility-rules-v1"


class EligibilityModelError(ValueError):
    """Raised when an eligibility value object would be self-contradictory."""


class GlobalStatus(StrEnum):
    """The verdict, and there are three of them.

    `ELIGIBLE` does **not** mean the employer will accept the candidate. It
    means: no hard blocker known to `eligibility-rules-v1` was contradicted,
    and no hard requirement it supports was left unanswered. A posting that
    demanded nothing this engine can check is `ELIGIBLE`, because nothing is
    known to stand in the way — turning silence into a doubt would make every
    thin advertisement look like a problem.

    `UNKNOWN` is a question: something was genuinely required and the facts to
    answer it are missing. It is never a soft `INELIGIBLE`, and no caller may
    treat it as one.
    """

    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    UNKNOWN = "UNKNOWN"


class RuleStatus(StrEnum):
    """What one rule concluded.

    `UNKNOWN` and `NOT_EVALUATED` are not synonyms and the difference decides
    the verdict. `UNKNOWN` says "this posting requires something and I cannot
    tell whether you meet it" — a hard rule saying that makes the decision
    UNKNOWN. `NOT_EVALUATED` says "this version declines to decide this at all"
    — an ambiguity, a skill requirement, a deferred dimension — and it changes
    nothing.
    """

    SATISFIED = "SATISFIED"
    VIOLATED = "VIOLATED"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_EVALUATED = "NOT_EVALUATED"


class Dimension(StrEnum):
    """What a rule is about."""

    EDUCATION = "EDUCATION"
    ENROLLMENT = "ENROLLMENT"
    EXPERIENCE = "EXPERIENCE"
    LANGUAGE = "LANGUAGE"
    WORK_AUTHORIZATION = "WORK_AUTHORIZATION"
    CONVENTION = "CONVENTION"
    #: Advisory. A skill requirement is reported and never decides.
    SKILL = "SKILL"
    #: Advisory. A demand 3.5B deliberately refused to represent.
    AMBIGUITY = "AMBIGUITY"
    #: Deferred by v1. The data exists; the comparison is not v1's business.
    MOBILITY = "MOBILITY"
    LOCATION = "LOCATION"
    AVAILABILITY = "AVAILABILITY"
    DURATION = "DURATION"
    START_DATE = "START_DATE"
    WORK_MODE = "WORK_MODE"


#: The only dimensions a `VIOLATED` may ever come from, and the only ones whose
#: `UNKNOWN` degrades the verdict. Widening this set is a change of business
#: meaning and moves `ELIGIBILITY_ENGINE_VERSION`.
HARD_DIMENSIONS: frozenset[Dimension] = frozenset(
    {
        Dimension.EDUCATION,
        Dimension.ENROLLMENT,
        Dimension.EXPERIENCE,
        Dimension.LANGUAGE,
        Dimension.WORK_AUTHORIZATION,
        Dimension.CONVENTION,
    }
)

#: Dimensions this version reports and never decides on, in a fixed order so a
#: decision's rule list is deterministic.
DEFERRED_DIMENSIONS: tuple[Dimension, ...] = (
    Dimension.MOBILITY,
    Dimension.LOCATION,
    Dimension.AVAILABILITY,
    Dimension.DURATION,
    Dimension.START_DATE,
    Dimension.WORK_MODE,
)


class RequirementKind(StrEnum):
    """How hard the posting made a requirement. A PREFERRED one never blocks."""

    REQUIRED = "REQUIRED"
    PREFERRED = "PREFERRED"


class ReasonCode(StrEnum):
    """The stable, machine-readable business truth of a rule result.

    A caller reads this, never the rendered `explanation`. Codes are added, not
    repurposed: changing what one means would silently rewrite the meaning of
    every row already stored under it.
    """

    EDUCATION_REQUIREMENT_ABSENT = "EDUCATION_REQUIREMENT_ABSENT"
    EDUCATION_REQUIREMENT_SATISFIED = "EDUCATION_REQUIREMENT_SATISFIED"
    EDUCATION_REQUIREMENT_VIOLATED = "EDUCATION_REQUIREMENT_VIOLATED"
    EDUCATION_PROFILE_UNKNOWN = "EDUCATION_PROFILE_UNKNOWN"
    EDUCATION_NOT_COMPARABLE = "EDUCATION_NOT_COMPARABLE"

    ENROLLMENT_REQUIREMENT_ABSENT = "ENROLLMENT_REQUIREMENT_ABSENT"
    ENROLLMENT_REQUIREMENT_SATISFIED = "ENROLLMENT_REQUIREMENT_SATISFIED"
    ENROLLMENT_REQUIREMENT_VIOLATED = "ENROLLMENT_REQUIREMENT_VIOLATED"
    ENROLLMENT_STATUS_UNKNOWN = "ENROLLMENT_STATUS_UNKNOWN"

    EXPERIENCE_REQUIREMENT_ABSENT = "EXPERIENCE_REQUIREMENT_ABSENT"
    EXPERIENCE_MIN_SATISFIED = "EXPERIENCE_MIN_SATISFIED"
    EXPERIENCE_MIN_VIOLATED = "EXPERIENCE_MIN_VIOLATED"
    EXPERIENCE_MAX_SATISFIED = "EXPERIENCE_MAX_SATISFIED"
    EXPERIENCE_MAX_VIOLATED = "EXPERIENCE_MAX_VIOLATED"
    EXPERIENCE_RANGE_SATISFIED = "EXPERIENCE_RANGE_SATISFIED"
    EXPERIENCE_NOT_COMPARABLE = "EXPERIENCE_NOT_COMPARABLE"
    EXPERIENCE_PREFERRED_NOT_BLOCKING = "EXPERIENCE_PREFERRED_NOT_BLOCKING"
    EXPERIENCE_OBLIGATION_UNSTATED_NOT_BLOCKING = (
        "EXPERIENCE_OBLIGATION_UNSTATED_NOT_BLOCKING"
    )

    LANGUAGE_REQUIREMENT_ABSENT = "LANGUAGE_REQUIREMENT_ABSENT"
    LANGUAGE_REQUIRED_SATISFIED = "LANGUAGE_REQUIRED_SATISFIED"
    LANGUAGE_REQUIRED_LEVEL_VIOLATED = "LANGUAGE_REQUIRED_LEVEL_VIOLATED"
    LANGUAGE_PROFILE_UNKNOWN = "LANGUAGE_PROFILE_UNKNOWN"
    LANGUAGE_LEVEL_NOT_COMPARABLE = "LANGUAGE_LEVEL_NOT_COMPARABLE"
    LANGUAGE_PREFERRED_NOT_BLOCKING = "LANGUAGE_PREFERRED_NOT_BLOCKING"

    WORK_AUTHORIZATION_REQUIREMENT_ABSENT = "WORK_AUTHORIZATION_REQUIREMENT_ABSENT"
    WORK_AUTHORIZATION_SATISFIED = "WORK_AUTHORIZATION_SATISFIED"
    WORK_AUTHORIZATION_VIOLATED = "WORK_AUTHORIZATION_VIOLATED"
    WORK_AUTHORIZATION_UNKNOWN = "WORK_AUTHORIZATION_UNKNOWN"
    WORK_AUTHORIZATION_SPONSORSHIP_OFFERED = "WORK_AUTHORIZATION_SPONSORSHIP_OFFERED"

    CONVENTION_REQUIREMENT_ABSENT = "CONVENTION_REQUIREMENT_ABSENT"
    CONVENTION_SATISFIED = "CONVENTION_SATISFIED"
    CONVENTION_VIOLATED = "CONVENTION_VIOLATED"
    CONVENTION_STATUS_UNKNOWN = "CONVENTION_STATUS_UNKNOWN"

    REQUIRED_SKILL_VERIFIED = "REQUIRED_SKILL_VERIFIED"
    REQUIRED_SKILL_NOT_VERIFIED_NON_BLOCKING = (
        "REQUIRED_SKILL_NOT_VERIFIED_NON_BLOCKING"
    )
    PREFERRED_SKILL_VERIFIED = "PREFERRED_SKILL_VERIFIED"
    PREFERRED_SKILL_NOT_BLOCKING = "PREFERRED_SKILL_NOT_BLOCKING"

    AMBIGUOUS_REQUIREMENT_NOT_BLOCKING = "AMBIGUOUS_REQUIREMENT_NOT_BLOCKING"

    MOBILITY_NOT_EVALUATED_V1 = "MOBILITY_NOT_EVALUATED_V1"
    LOCATION_NOT_EVALUATED_V1 = "LOCATION_NOT_EVALUATED_V1"
    AVAILABILITY_NOT_EVALUATED_V1 = "AVAILABILITY_NOT_EVALUATED_V1"
    DURATION_NOT_EVALUATED_V1 = "DURATION_NOT_EVALUATED_V1"
    START_DATE_NOT_EVALUATED_V1 = "START_DATE_NOT_EVALUATED_V1"
    WORK_MODE_NOT_EVALUATED_V1 = "WORK_MODE_NOT_EVALUATED_V1"


class SponsorshipNeed(StrEnum):
    """Whether this person needs an employer to sponsor them.

    The three members mirror `profile_preferences.visa_sponsorship_required`
    exactly, `UNKNOWN` included. Nothing computes this from a nationality, a
    country of residence, an address or a posting's location, here or anywhere
    downstream: it is what the person stated, or it is UNKNOWN.
    """

    YES = "YES"
    NO = "NO"
    UNKNOWN = "UNKNOWN"


class ConventionCapability(StrEnum):
    """Whether this person can supply a school internship agreement.

    Mirrors `profile_preferences.convention_status`. Being a student does not
    imply `AVAILABLE`: a school that will not sign is a real and common case,
    and the person is asked the question separately for that reason.
    """

    AVAILABLE = "AVAILABLE"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, order=True)
class EducationRequirementInput:
    """One education level a posting will accept, and whether it is a floor.

    `level` and `mode` carry 3.5A's own spellings — `BAC_PLUS_5`, `MASTER`,
    `MINIMUM`, `EXACT` — as plain strings rather than as an imported enum, so
    this package holds no opinion about which levels exist. Several of these on
    one posting are several **accepted** levels, exactly as `0012` says: "Bac+5
    or Master" is a choice the employer offered, not a contradiction.
    """

    level: str
    mode: str


@dataclass(frozen=True, order=True)
class ExperienceRequirementInput:
    """One experience demand: its bounds in months, and how it was stated.

    A bound is `None` when the posting gave none. `kind` is `None` when the
    posting named a quantity without saying whether it was an obligation —
    3.5A's `ExperienceObligation.UNKNOWN` — and this version refuses to guess.
    """

    min_months: int | None
    max_months: int | None
    kind: RequirementKind | None


@dataclass(frozen=True, order=True)
class LanguageRequirementInput:
    """One language a posting asked for, at the level it wrote, if it wrote one.

    `proficiency_text` is the posting's own words, untranslated: `B2` stays
    `B2` and `Fluent` stays `Fluent`. Turning the second into a CEFR level would
    be this package inventing an equivalence the employer never stated, and
    `0013` refuses it on the offer side for the same reason.
    """

    language_key: str
    kind: RequirementKind
    proficiency_text: str | None = None


@dataclass(frozen=True, order=True)
class SkillRequirementInput:
    """One skill a posting asked for, by the shared canonical key of `0008`."""

    canonical_key: str
    kind: RequirementKind


@dataclass(frozen=True, order=True)
class RequirementAmbiguityInput:
    """One demand 3.5B understood and deliberately declined to store.

    Only the kind and the reason travel here. The refused group's terms were
    never stored — storing half a requirement is worse than storing none — so
    there is nothing for a rule to compare and nothing this phase could do with
    it but report that the refusal happened.
    """

    kind: str
    reason: str


@dataclass(frozen=True)
class OpportunityEligibilityInput:
    """Everything about one posting that any rule of v1 is allowed to read.

    Deliberately absent, and absent from the fingerprint with it: the title,
    the description, the company, the locations, the work mode, the duration,
    the start date and the opportunity type. v1 evaluates none of them, so an
    edit to any of them must not recompute a verdict that could not change.

    `enrollment_required` is `None` for every posting the current schema can
    produce: Phase 3.5 has no representation for "must currently be enrolled",
    and inventing one from the word *student*, from `convention_requirement` or
    from the opportunity type would be this phase manufacturing a demand nobody
    wrote. The field exists so the rule that would evaluate it is written,
    tested and ready rather than absent — see `ENROLLMENT_LIMITATION` in
    `inputs.py`.
    """

    opportunity_id: int
    constraints_extractor_version: str
    requirements_extractor_version: str
    education: tuple[EducationRequirementInput, ...] = ()
    enrollment_required: bool | None = None
    experience: tuple[ExperienceRequirementInput, ...] = ()
    languages: tuple[LanguageRequirementInput, ...] = ()
    skills: tuple[SkillRequirementInput, ...] = ()
    ambiguities: tuple[RequirementAmbiguityInput, ...] = ()
    #: 3.5A's `visa_sponsorship`: what the employer offers. `UNKNOWN` when the
    #: posting said nothing.
    visa_sponsorship: str = "UNKNOWN"
    #: 3.5A's `work_authorization`: what the applicant must already hold.
    work_authorization: str = "UNKNOWN"
    #: 3.5A's `convention_requirement`.
    convention: str = "UNKNOWN"


@dataclass(frozen=True, order=True)
class ProfileLanguageInput:
    """One language this person's Digital Twin holds, at the level it recorded.

    `proficiency_text` is again verbatim. `courant` stays `courant`, and
    `0010`'s structurer refuses to turn it into `C1` for exactly the reason this
    engine refuses to compare it: nobody stated that equivalence.
    """

    language_key: str
    proficiency_text: str | None = None


@dataclass(frozen=True)
class ProfileEligibilityInput:
    """Everything about one person that any rule of v1 is allowed to read.

    Deliberately absent, and absent from the fingerprint with it: the name, the
    telephone number, the GitHub URL, the portfolio, the address, the
    nationality, the certifications, the projects, the career objectives, the
    availability, the mobility and the preferred opportunity types and work
    modes. None of them is read by a rule; editing any of them recomputes
    nothing.

    Three fields are empty for every profile the current schema can produce,
    and each is empty for a stated reason rather than by omission:

    * `education_levels` — Phase 3.4 stores an education as institution,
      programme and period, all verbatim, and says in `0010` that there is "no
      `degree_level`, no `bac_plus` and no `graduation_year`". An empty tuple
      here therefore means **unknown**, never "this person has no degree", and
      the education rule answers UNKNOWN rather than contradicting anything;
    * `currently_enrolled` — no fact in Phase 3.4 states it. A finished Bac+5
      does not imply a current student, and a period ending in a future year is
      a string somebody wrote on a CV, not an enrolment;
    * `comparable_experience_months` — Phase 3.4 stores a period as the text the
      CV wrote. There is no total, and this engine will not build one: summing
      every month a person ever worked to answer "3 years of data engineering"
      would answer a different question, and role similarity is Phase 4.
    """

    profile_id: int
    education_levels: tuple[str, ...] = ()
    currently_enrolled: bool | None = None
    comparable_experience_months: int | None = None
    languages: tuple[ProfileLanguageInput, ...] = ()
    skills: tuple[str, ...] = ()
    sponsorship_need: SponsorshipNeed = SponsorshipNeed.UNKNOWN
    convention_capability: ConventionCapability = ConventionCapability.UNKNOWN


@dataclass(frozen=True)
class EligibilityInput:
    """One posting, one person, one engine version: the whole digest domain."""

    opportunity: OpportunityEligibilityInput
    profile: ProfileEligibilityInput
    engine_version: str = ELIGIBILITY_ENGINE_VERSION


@dataclass(frozen=True)
class RuleResult:
    """What one rule concluded, and everything needed to audit it.

    The constructor refuses a self-contradictory result rather than leaving it
    to SQLite: an engine bug should fail where the bug is, in the rule that
    built the object, not three layers later in an `INSERT`. The schema keeps
    the same CHECKs anyway, because a guarantee worth having is worth having
    twice.
    """

    dimension: Dimension
    rule_code: str
    status: RuleStatus
    reason_code: ReasonCode
    explanation: str
    is_blocking: bool = False
    requirement_kind: RequirementKind | None = None
    requirement_ref: str | None = None
    profile_ref: str | None = None

    def __post_init__(self) -> None:
        if self.is_blocking and self.dimension not in HARD_DIMENSIONS:
            raise EligibilityModelError(
                f"{self.dimension} is not a hard dimension and cannot block"
            )
        if self.is_blocking and self.requirement_kind is not RequirementKind.REQUIRED:
            raise EligibilityModelError(
                "only an explicitly REQUIRED requirement can block"
            )
        if self.status is RuleStatus.VIOLATED and not self.is_blocking:
            raise EligibilityModelError(
                "a non-blocking rule cannot be VIOLATED; a gap is not a contradiction"
            )
        if not self.explanation.strip():
            raise EligibilityModelError("a rule result must explain itself")


@dataclass(frozen=True)
class EligibilityDecision:
    """One verdict, its reasons, and the digest of what produced it.

    There is no score and no ratio, and the counters below cannot be made into
    one honestly: they count rule outcomes over a rule set whose size depends on
    how many skills a posting happened to list. "Four of five satisfied" is not
    eighty percent of anything.
    """

    opportunity_id: int
    profile_id: int
    status: GlobalStatus
    input_fingerprint: str
    results: tuple[RuleResult, ...] = ()
    engine_version: str = ELIGIBILITY_ENGINE_VERSION

    def _count(self, status: RuleStatus) -> int:
        return sum(1 for result in self.results if result.status is status)

    @property
    def satisfied_count(self) -> int:
        return self._count(RuleStatus.SATISFIED)

    @property
    def violated_count(self) -> int:
        return self._count(RuleStatus.VIOLATED)

    @property
    def unknown_count(self) -> int:
        return self._count(RuleStatus.UNKNOWN)

    @property
    def not_applicable_count(self) -> int:
        return self._count(RuleStatus.NOT_APPLICABLE)

    @property
    def not_evaluated_count(self) -> int:
        return self._count(RuleStatus.NOT_EVALUATED)

    @property
    def blocking_unknown_count(self) -> int:
        """The UNKNOWNs that actually made the verdict UNKNOWN."""
        return sum(
            1
            for result in self.results
            if result.is_blocking and result.status is RuleStatus.UNKNOWN
        )

    @property
    def blocking_results(self) -> tuple[RuleResult, ...]:
        """Every rule that could have decided the verdict, in evaluation order."""
        return tuple(result for result in self.results if result.is_blocking)
