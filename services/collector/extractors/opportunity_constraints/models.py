"""Typed, immutable vocabulary of the Phase 3.5A opportunity constraints.

This package answers exactly one question: **what does this posting itself
require?** It never asks what any person offers, so nothing here mentions a
profile, a candidate, a fit, a score or a decision. Eligibility is Phase 3.6
and matching is Phase 4; neither is implemented, here or downstream of here.

Two registries are **imported** from `services.digital_twin.preferences.models`
rather than redefined: `OpportunityType` and `WorkMode`. That is deliberate and
it is the point. Phase 3.6 will have to compare "what the person wants" with
"what the posting is", and two registries that merely look alike are a bug
waiting for the day they drift apart. The import direction is safe: that module
imports only the standard library, so nothing cycles. A test pins the two names
to the same objects, so a future divergence fails loudly instead of silently.

Absence is the default answer everywhere. A field is `None` — read as UNKNOWN —
unless a closed rule found explicit evidence for it, and three of the enums
carry an `UNKNOWN` member so the extractor's output is total: every field has a
value, and "we did not find anything" is one of them rather than a hole. The
repository stores that member as `NULL`, so the database has exactly one
representation of "not asserted"; see `repository.py`.

**Absence is never FALSE.** A posting that says nothing about visas is not a
posting that refuses to sponsor, a posting with an address is not a posting
that requires attendance, and a posting written in English does not require
English. None of those inferences exists in this package.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

# The shared registries. Imported, never redefined — see the module docstring.
from services.digital_twin.preferences.models import OpportunityType, WorkMode

#: The version of the whole extraction contract: the closed rules, the
#: registries they compare against, the normalization applied to text and the
#: shape of everything below. Any change to what this package produces **from a
#: given posting** moves this version, and moving it is what makes the next
#: synchronization recompute the rows it explains.
EXTRACTOR_VERSION = "opportunity-constraints-v1"

#: The longest evidence fragment stored for one rule match. Evidence is a
#: pointer to why a value was asserted, not a copy of the posting: storing a
#: whole description would duplicate the source, bloat the projection and make
#: every audit read personal-scale text it does not need.
MAX_EVIDENCE_LENGTH = 200

__all__ = [
    "EXTRACTOR_VERSION",
    "MAX_EVIDENCE_LENGTH",
    "ConstraintConflict",
    "ConstraintEvidence",
    "ConstraintKind",
    "ConventionRequirement",
    "DurationRequirement",
    "EducationLevel",
    "EducationRequirement",
    "EducationRequirementMode",
    "ExperienceObligation",
    "ExperienceRequirement",
    "ExtractedConstraints",
    "OpportunityConstraintError",
    "OpportunitySource",
    "OpportunityType",
    "SLOT_KINDS",
    "Slot",
    "SourceField",
    "StartRequirement",
    "StartPrecision",
    "VisaSponsorship",
    "WorkAuthorization",
    "WorkMode",
]


class OpportunityConstraintError(ValueError):
    """Raised when an extracted constraint is malformed, before any write."""


class ConstraintKind(StrEnum):
    """What a value, a piece of evidence or a conflict is about.

    One kind per thing a posting can require. There is no `ELIGIBILITY` kind
    and no `MATCH` kind, because a constraint is a property of the posting and
    a decision is a property of nobody yet.
    """

    OPPORTUNITY_TYPE = "OPPORTUNITY_TYPE"
    EDUCATION = "EDUCATION"
    EXPERIENCE = "EXPERIENCE"
    DURATION = "DURATION"
    START = "START"
    LOCATION = "LOCATION"
    WORK_MODE = "WORK_MODE"
    VISA_SPONSORSHIP = "VISA_SPONSORSHIP"
    WORK_AUTHORIZATION = "WORK_AUTHORIZATION"
    CONVENTION = "CONVENTION"


class Slot(StrEnum):
    """The single place a value lands, and the unit a contradiction is judged in.

    A *kind* is the category a reader browses by; a *slot* is the thing that can
    actually disagree with itself, and the two are not the same. `EXPERIENCE`
    holds two independent slots: a posting may state how much experience it
    wants in one sentence and how badly it wants it in another, and those two
    statements do not compete — so they can also contradict themselves
    separately, and one posting can legitimately carry both an
    `EXPERIENCE_BOUNDS` conflict and an `EXPERIENCE_OBLIGATION` one. Recording
    them under one key would either lose one of them or merge `36-` and
    `REQUIRED` into a single list of "conflicting values", which are two
    different contradictions and one meaningless row.

    `EDUCATION` and `LOCATION` are here because rules land in them, but they are
    multi-valued: several levels or several places are several answers, never a
    disagreement, so neither is ever conflict-resolved.
    """

    OPPORTUNITY_TYPE = "OPPORTUNITY_TYPE"
    #: Multi-valued. Never conflict-resolved.
    EDUCATION = "EDUCATION"
    EXPERIENCE_BOUNDS = "EXPERIENCE_BOUNDS"
    EXPERIENCE_OBLIGATION = "EXPERIENCE_OBLIGATION"
    DURATION = "DURATION"
    START = "START"
    #: Multi-valued. Never conflict-resolved.
    LOCATION = "LOCATION"
    WORK_MODE = "WORK_MODE"
    VISA_SPONSORSHIP = "VISA_SPONSORSHIP"
    WORK_AUTHORIZATION = "WORK_AUTHORIZATION"
    CONVENTION = "CONVENTION"


class SourceField(StrEnum):
    """Where a piece of evidence was read.

    A field name, never a document: the extractor is handed already-collected
    values and reads nothing else. `QUALIFICATION_TYPE` is the one derived
    input — `opportunity_qualifications.opportunity_type`, produced by the
    Phase 2 classifier — and it is named separately precisely so an audit can
    tell "the posting said so" from "another versioned classifier said so".
    """

    TITLE = "TITLE"
    DESCRIPTION = "DESCRIPTION"
    LOCATION = "LOCATION"
    COUNTRY = "COUNTRY"
    REMOTE_TYPE = "REMOTE_TYPE"
    QUALIFICATION_TYPE = "QUALIFICATION_TYPE"


class EducationLevel(StrEnum):
    """A level a posting named explicitly, normalized but never translated.

    `BAC_PLUS_5` and `MASTER` are two members, not one. They are written by
    different postings in different countries and a reader who wants to treat
    them alike can; this package will not decide that for them, because
    "Bac+5 required" and "Master required" are not the same sentence and the
    difference is not ours to erase.
    """

    BAC_PLUS_2 = "BAC_PLUS_2"
    BAC_PLUS_3 = "BAC_PLUS_3"
    BAC_PLUS_4 = "BAC_PLUS_4"
    BAC_PLUS_5 = "BAC_PLUS_5"
    BACHELOR = "BACHELOR"
    MASTER = "MASTER"
    ENGINEERING_DEGREE = "ENGINEERING_DEGREE"
    PHD = "PHD"


class EducationRequirementMode(StrEnum):
    """Whether a named level is a floor or the level itself."""

    #: "Bac+3 minimum", "at least a Bachelor" — this level or above.
    MINIMUM = "MINIMUM"
    #: "Master's degree", with nothing making it a floor.
    EXACT = "EXACT"


class ExperienceObligation(StrEnum):
    """How hard a posting made its experience requirement."""

    REQUIRED = "REQUIRED"
    PREFERRED = "PREFERRED"
    #: Nothing explicit was found. A job title is not a quantity.
    UNKNOWN = "UNKNOWN"


class ConventionRequirement(StrEnum):
    """Whether the posting demands a school internship agreement.

    Never inferred from the posting being an internship, from the word
    student, from a school being mentioned or from a country.
    """

    REQUIRED = "REQUIRED"
    NOT_REQUIRED = "NOT_REQUIRED"
    UNKNOWN = "UNKNOWN"


class VisaSponsorship(StrEnum):
    """Whether the **employer** offers to sponsor a visa.

    This is a statement by the company about what it will do. It is not
    `WorkAuthorization`, which is a statement about what the applicant must
    already hold, and it is not the person's own need, which is Phase 3.4C's
    `visa_sponsorship_required` on the profile side. Three different claims,
    three separate fields, and no rule in this package derives any of them
    from another.
    """

    AVAILABLE = "AVAILABLE"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    UNKNOWN = "UNKNOWN"


class WorkAuthorization(StrEnum):
    """Whether the applicant must already be authorized to work there.

    "Must be authorized to work in the US" is `REQUIRED`. It is **not**
    `VisaSponsorship.NOT_AVAILABLE`: a company can require existing
    authorization and still sponsor, or say neither. Turning one into the
    other would invent a refusal nobody wrote.
    """

    REQUIRED = "REQUIRED"
    NOT_REQUIRED = "NOT_REQUIRED"
    UNKNOWN = "UNKNOWN"


class StartPrecision(StrEnum):
    """How precisely a posting stated when the work begins.

    The precision is stored because a missing part is never completed. "Starting
    in February" is `MONTH` with no year — not February of the current year,
    not February of next year. Nothing in this package reads a clock.
    """

    DATE = "DATE"
    MONTH = "MONTH"
    YEAR = "YEAR"


#: Which category each slot reports under. Both experience slots map to
#: `EXPERIENCE`: they are one subject a reader browses by, and two things that
#: can disagree independently.
SLOT_KINDS: dict[Slot, ConstraintKind] = {
    Slot.OPPORTUNITY_TYPE: ConstraintKind.OPPORTUNITY_TYPE,
    Slot.EDUCATION: ConstraintKind.EDUCATION,
    Slot.EXPERIENCE_BOUNDS: ConstraintKind.EXPERIENCE,
    Slot.EXPERIENCE_OBLIGATION: ConstraintKind.EXPERIENCE,
    Slot.DURATION: ConstraintKind.DURATION,
    Slot.START: ConstraintKind.START,
    Slot.LOCATION: ConstraintKind.LOCATION,
    Slot.WORK_MODE: ConstraintKind.WORK_MODE,
    Slot.VISA_SPONSORSHIP: ConstraintKind.VISA_SPONSORSHIP,
    Slot.WORK_AUTHORIZATION: ConstraintKind.WORK_AUTHORIZATION,
    Slot.CONVENTION: ConstraintKind.CONVENTION,
}


@dataclass(frozen=True)
class OpportunitySource:
    """Exactly the already-collected values the extractor is allowed to read.

    It is a plain value object, so the pure extractor never opens a database,
    never issues a query and never sees a column it was not handed. The
    fingerprint in `extractor.py` is computed over these fields and no others,
    which is what makes "the source changed" a decidable question.

    `qualification_type` is the Phase 2 classifier's own reading, passed in as
    text exactly as that table stores it; `None` when the opportunity has not
    been qualified.
    """

    opportunity_id: int
    canonical_title: str | None = None
    description: str | None = None
    location: str | None = None
    country: str | None = None
    remote_type: str | None = None
    qualification_type: str | None = None


@dataclass(frozen=True)
class ConstraintEvidence:
    """Why one value was asserted: the rule, the field, and the words.

    `text` is the minimal fragment the rule matched, taken from the normalized
    text and capped at `MAX_EVIDENCE_LENGTH`. It exists so a human can check
    the extractor rather than trust it.
    """

    kind: ConstraintKind
    source_field: SourceField
    rule_id: str
    text: str
    #: The value this evidence supports, as the text form the projection
    #: stores. Kept so two pieces of evidence for one kind can be compared
    #: without re-running the rules.
    normalized_value: str | None = None

    def __post_init__(self) -> None:
        if not self.rule_id.strip():
            raise OpportunityConstraintError("evidence must name the rule that fired")
        if not self.text.strip():
            raise OpportunityConstraintError("evidence must quote what it matched")
        if len(self.text) > MAX_EVIDENCE_LENGTH:
            raise OpportunityConstraintError(
                f"evidence must be a fragment, not a copy of the posting "
                f"(over {MAX_EVIDENCE_LENGTH} characters)"
            )


@dataclass(frozen=True)
class ConstraintConflict:
    """Two rules read the same posting and disagreed, so nothing was asserted.

    A conflict is recorded rather than resolved. "Fully remote" three lines
    above "this role is fully on-site" is a defect in the posting, and picking
    one of them — the first, the last, the longest match — would be this
    package deciding what an employer meant. The field stays UNKNOWN and this
    row says why, which is the only answer that stays true.

    The identity of a conflict is its **slot**, not its kind. One posting can
    contradict itself about how much experience it wants *and* about whether it
    insists, and those are two contradictions with two answers; keying them by
    `EXPERIENCE` would collide. `kind` stays for browsing, and is derived from
    the slot rather than passed in, so the two can never disagree.
    """

    slot: Slot
    #: The incompatible values, as text, sorted so the row is deterministic.
    values: tuple[str, ...]
    #: The rules that produced them, sorted, deduplicated.
    rule_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.slot, Slot):
            raise OpportunityConstraintError("a conflict names the slot that disagreed")
        if self.slot in (Slot.EDUCATION, Slot.LOCATION):
            raise OpportunityConstraintError(
                f"{self.slot.value} is multi-valued and cannot contradict itself"
            )
        if len(self.values) < 2:
            raise OpportunityConstraintError("a conflict needs two values to conflict")
        if tuple(sorted(self.values)) != self.values:
            raise OpportunityConstraintError("conflict values must be sorted")
        if tuple(sorted(self.rule_ids)) != self.rule_ids:
            raise OpportunityConstraintError("conflict rule ids must be sorted")

    @property
    def kind(self) -> ConstraintKind:
        """The category this contradiction belongs to, derived from the slot."""
        return SLOT_KINDS[self.slot]


@dataclass(frozen=True)
class EducationRequirement:
    """One level a posting named, and whether it named it as a floor."""

    level: EducationLevel
    mode: EducationRequirementMode


@dataclass(frozen=True)
class ExperienceRequirement:
    """How much prior experience the posting asked for, in months.

    Months rather than years because postings write both, and one unit is one
    comparison later. A bound is `None` when the posting did not give it:
    "3+ years" has a floor and no ceiling, and inventing a ceiling would be
    inventing a rejection.
    """

    min_months: int | None = None
    max_months: int | None = None
    obligation: ExperienceObligation = ExperienceObligation.UNKNOWN

    def __post_init__(self) -> None:
        for bound in (self.min_months, self.max_months):
            if bound is not None and (isinstance(bound, bool) or bound < 0):
                raise OpportunityConstraintError("experience months must not be negative")
        if (
            self.min_months is not None
            and self.max_months is not None
            and self.min_months > self.max_months
        ):
            raise OpportunityConstraintError("experience min must not exceed max")

    @property
    def known(self) -> bool:
        return self.min_months is not None or self.max_months is not None


@dataclass(frozen=True)
class DurationRequirement:
    """How long the posting says the work lasts, in months.

    Never derived from the kind of posting: a six-month internship and a
    four-month internship are both internships, and the word does not carry a
    number.
    """

    min_months: int | None = None
    max_months: int | None = None

    def __post_init__(self) -> None:
        for bound in (self.min_months, self.max_months):
            if bound is not None and (isinstance(bound, bool) or bound < 1):
                raise OpportunityConstraintError("duration months must be positive")
        if (
            self.min_months is not None
            and self.max_months is not None
            and self.min_months > self.max_months
        ):
            raise OpportunityConstraintError("duration min must not exceed max")

    @property
    def known(self) -> bool:
        return self.min_months is not None or self.max_months is not None


@dataclass(frozen=True)
class StartRequirement:
    """When the posting says the work begins, at the precision it was written.

    A missing part stays missing. `MONTH` precision means the year is `None`
    and will not be filled from the clock, from the posting date or from
    anything else.
    """

    year: int | None = None
    month: int | None = None
    day: int | None = None
    precision: StartPrecision | None = None

    def __post_init__(self) -> None:
        if self.precision is None:
            if self.year is not None or self.month is not None or self.day is not None:
                raise OpportunityConstraintError("a start without precision holds no part")
            return
        if self.month is not None and not 1 <= self.month <= 12:
            raise OpportunityConstraintError("start month is outside the calendar")
        if self.precision is StartPrecision.YEAR and (
            self.year is None or self.month is not None or self.day is not None
        ):
            raise OpportunityConstraintError("YEAR precision holds a year and nothing else")
        if self.precision is StartPrecision.MONTH and (
            self.month is None or self.day is not None
        ):
            raise OpportunityConstraintError("MONTH precision holds a month and no day")
        if self.precision is StartPrecision.DATE:
            if self.year is None or self.month is None or self.day is None:
                raise OpportunityConstraintError("DATE precision holds a whole date")
            # The calendar decides, not the digits: 2027-02-30 is refused
            # rather than rolled forward into March.
            try:
                date(self.year, self.month, self.day)
            except ValueError as error:
                raise OpportunityConstraintError(
                    "the start date is not a real calendar date"
                ) from error

    @property
    def known(self) -> bool:
        return self.precision is not None


@dataclass(frozen=True)
class ExtractedConstraints:
    """Everything one posting was found to require, and why.

    The object is **total**: every scalar has a value, and UNKNOWN — `None`, or
    the `UNKNOWN` member where the enum has one — is a value like any other. A
    caller never has to ask whether a field was computed.

    Nothing here is a judgement about a person, and nothing here is a number
    meant to be compared to one: there is no score, no weight, no rank and no
    confidence. A closed rule either found explicit evidence or it did not.
    """

    opportunity_id: int
    source_fingerprint: str
    extractor_version: str = EXTRACTOR_VERSION
    opportunity_type: OpportunityType | None = None
    education: tuple[EducationRequirement, ...] = ()
    experience: ExperienceRequirement = field(default_factory=ExperienceRequirement)
    duration: DurationRequirement = field(default_factory=DurationRequirement)
    start: StartRequirement = field(default_factory=StartRequirement)
    locations: tuple[str, ...] = ()
    work_mode: WorkMode | None = None
    visa_sponsorship: VisaSponsorship = VisaSponsorship.UNKNOWN
    work_authorization: WorkAuthorization = WorkAuthorization.UNKNOWN
    convention: ConventionRequirement = ConventionRequirement.UNKNOWN
    evidence: tuple[ConstraintEvidence, ...] = ()
    conflicts: tuple[ConstraintConflict, ...] = ()

    def __post_init__(self) -> None:
        if len(self.source_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.source_fingerprint
        ):
            raise OpportunityConstraintError(
                "source_fingerprint must be a lowercase sha256 digest"
            )
        seen: set[tuple[str, str]] = set()
        for requirement in self.education:
            key = (requirement.level.value, requirement.mode.value)
            if key in seen:
                raise OpportunityConstraintError("an education level is listed twice")
            seen.add(key)

    @property
    def conflicting_kinds(self) -> frozenset[ConstraintKind]:
        return frozenset(conflict.kind for conflict in self.conflicts)

    @property
    def conflicting_slots(self) -> frozenset[Slot]:
        return frozenset(conflict.slot for conflict in self.conflicts)

    def evidence_for(self, kind: ConstraintKind) -> tuple[ConstraintEvidence, ...]:
        return tuple(item for item in self.evidence if item.kind is kind)


def deduplicate_texts(values: Sequence[str]) -> tuple[str, ...]:
    """Trim, drop blanks, and keep the first of each exact duplicate.

    Exact and case-sensitive: deciding that two spellings are one place would
    be fuzzy matching, and this project does none. Order is the order the
    posting used, because a list somebody wrote is theirs to order.
    """
    kept: list[str] = []
    seen: set[str] = set()
    for value in values:
        trimmed = "" if value is None else str(value).strip()
        if not trimmed or trimmed in seen:
            continue
        seen.add(trimmed)
        kept.append(trimmed)
    return tuple(kept)
