"""Typed, immutable vocabulary of the Phase 3.4C explicit profile input.

Everything in this module describes something a **person typed**. Nothing here
is read out of a CV, out of a postal address, out of a nationality or out of
the clock, and nothing here has a rule that could produce a value from any of
those. That is the whole point of the slice: what somebody has done is not what
somebody wants next, and the two must never be confused.

So there is no inference in this package, in either direction:

* an availability date is a date the person typed. A CV period, a diploma year
  and `date.today()` are not availability, and none of the three is consulted —
  this module imports no clock at all;
* a mobility is a list the person typed. A city on a CV, a country of
  residence and a past employer's address are not mobility, and no geocoding,
  no country lookup and no region expansion happens anywhere below;
* a work mode is a choice the person made. A remote job in the past is not a
  preference for remote in the future;
* a preferred domain is a domain the person named. A skill, a project title
  and a CV section heading are not domains, and nothing here reads them;
* a convention status is an answer the person gave. Being a student is not an
  answer, and neither is being enrolled somewhere;
* a visa sponsorship need is an answer the person gave. A nationality and a
  location are not answers;
* a career objective is a sentence the person wrote. The professional title on
  a CV is not an objective.

Absence is `UNKNOWN`, and `UNKNOWN` is represented by there being no value at
all — no object of these classes, no fact, no projected row. The two fields
that *do* carry a literal `UNKNOWN` member, `ConventionStatus` and
`VisaSponsorshipRequired`, are questions asked inside a preference the person
did state, where "I don't know yet" is itself the answer they gave.

Nothing here carries a confidence, a score, a weight, a priority or a rank, and
nothing here compares a profile to an opportunity: eligibility and matching are
Phases 3.5/3.6 and are not implemented, here or anywhere downstream of here.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

#: The version of the whole explicit-input contract: the closed registries
#: below, the shapes `codec.py` encodes, the normalization applied to free text
#: and the validations refused. Any change to what this package produces **from
#: a given input** moves this version, because it is what a projected row
#: records to explain how it was encoded.
EXPLICIT_PROFILE_INPUT_VERSION = "explicit-profile-input-v1"

#: `YYYY-MM-DD` and nothing else. `date.fromisoformat` alone is too permissive
#: on Python 3.11+ — it accepts `20260901` and week dates — and a stored date
#: has to be one shape, so the shape is checked before the calendar is.
_ISO_DATE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")


class ExplicitProfileInputError(ValueError):
    """Raised when explicit input is malformed, before anything is written.

    Refusing is the honest answer: the alternative would be to complete what
    somebody typed with a plausible value, and a guess stored once is believed
    forever.
    """


class AvailabilityStatus(StrEnum):
    """When the person said they can start. Two forms, both stated."""

    #: Available already. Carries no date, and never gets given one.
    AVAILABLE_NOW = "AVAILABLE_NOW"
    #: Available from a date the person typed. Requires that date.
    AVAILABLE_FROM = "AVAILABLE_FROM"


class MobilityScope(StrEnum):
    """How wide the person said they are willing to go."""

    #: No restriction stated. Not "everywhere on earth" — simply no list.
    OPEN = "OPEN"
    #: Restricted to the locations the person named, and to those only.
    RESTRICTED = "RESTRICTED"


class OpportunityType(StrEnum):
    """The closed registry v1 of what somebody can be looking for.

    Closed on purpose: a free-text opportunity type would be compared to an
    offer by guessing later. Declaration order is the canonical order — see
    `normalize_registry`.
    """

    PFA = "PFA"
    PFE = "PFE"
    SUMMER_INTERNSHIP = "SUMMER_INTERNSHIP"
    PRE_HIRE_INTERNSHIP = "PRE_HIRE_INTERNSHIP"
    ALTERNANCE = "ALTERNANCE"
    INTERNSHIP = "INTERNSHIP"
    FIRST_JOB = "FIRST_JOB"
    JUNIOR_ROLE = "JUNIOR_ROLE"


class WorkMode(StrEnum):
    """The closed registry of how somebody is willing to work."""

    ON_SITE = "ON_SITE"
    HYBRID = "HYBRID"
    REMOTE = "REMOTE"


class ConventionStatus(StrEnum):
    """Whether the person said they can obtain an internship agreement.

    `UNKNOWN` is a real answer here, and the default one: nobody's student
    status, school or enrolment is read as an answer to this question.
    """

    UNKNOWN = "UNKNOWN"
    AVAILABLE = "AVAILABLE"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class VisaSponsorshipRequired(StrEnum):
    """Whether the person said they need sponsorship.

    `UNKNOWN` is a real answer here, and the default one: no nationality, no
    country of residence and no address is ever read as an answer to this
    question.
    """

    UNKNOWN = "UNKNOWN"
    YES = "YES"
    NO = "NO"


def normalize_text(value: object, label: str) -> str:
    """One free-text entry, trimmed. Case and wording are the person's own.

    Trimming is the only thing done to it: no capitalization, no case folding,
    no accent stripping, no spell correction and no expansion of an
    abbreviation. A value that is empty once trimmed is refused rather than
    dropped silently, because a caller passing `""` meant something and this
    module does not get to decide what.
    """
    if not isinstance(value, str):
        raise ExplicitProfileInputError(f"{label} must be text")
    trimmed = value.strip()
    if trimmed == "":
        raise ExplicitProfileInputError(f"{label} must not be empty")
    return trimmed


def normalize_text_list(values: Sequence[object] | None, label: str) -> tuple[str, ...]:
    """Trim every entry and drop later exact duplicates, keeping the first.

    Deduplication is **exact** and case-sensitive, so "Data" and "data" are two
    entries: deciding they are one would be fuzzy matching, which this project
    does not do. Order is the order the person typed, because a list somebody
    wrote is theirs to order; sorting it alphabetically would replace their
    ranking with one nobody chose. First-occurrence dedup on a stable order is
    deterministic, which is all canonical encoding needs.
    """
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        raise ExplicitProfileInputError(f"{label} must be a list, not a single text")
    kept: list[str] = []
    seen: set[str] = set()
    for value in values:
        trimmed = normalize_text(value, label)
        if trimmed in seen:
            continue
        seen.add(trimmed)
        kept.append(trimmed)
    return tuple(kept)


def normalize_registry(
    values: Sequence[object] | None, registry: type[StrEnum], label: str
) -> tuple[StrEnum, ...]:
    """Read every entry against a closed registry, dedup, and order canonically.

    Unlike free text, a closed registry has a canonical order of its own — the
    order its members are declared in — so the same set typed in two orders
    encodes to the same bytes and restating a preference differently ordered is
    a no-op rather than a correction. A value outside the registry is refused;
    it is never mapped to the nearest member.
    """
    if values is None:
        raise ExplicitProfileInputError(f"{label} must not be empty")
    if isinstance(values, (str, bytes)):
        raise ExplicitProfileInputError(f"{label} must be a list, not a single text")
    members: set[StrEnum] = set()
    for value in values:
        if isinstance(value, registry):
            members.add(value)
            continue
        if not isinstance(value, str):
            raise ExplicitProfileInputError(f"{label} must hold {registry.__name__} values")
        try:
            members.add(registry(value))
        except ValueError as error:
            known = ", ".join(member.value for member in registry)
            raise ExplicitProfileInputError(
                f"{label} does not accept that value; the registry is: {known}"
            ) from error
    if not members:
        raise ExplicitProfileInputError(f"{label} must name at least one value")
    return tuple(member for member in registry if member in members)


def normalize_choice(
    value: object, registry: type[StrEnum], label: str
) -> StrEnum:
    """Read one entry against a closed registry. Never guessed from anything."""
    if isinstance(value, registry):
        return value
    if not isinstance(value, str):
        raise ExplicitProfileInputError(f"{label} must be a {registry.__name__} value")
    try:
        return registry(value)
    except ValueError as error:
        known = ", ".join(member.value for member in registry)
        raise ExplicitProfileInputError(
            f"{label} does not accept that value; the registry is: {known}"
        ) from error


def normalize_iso_date(value: object, label: str) -> str:
    """One `YYYY-MM-DD` date the person typed, checked against the calendar.

    The shape is checked first and the calendar second, so `2026-9-1`,
    `20260901` and `01/09/2026` are all refused rather than reshaped, and
    `2026-02-30` is refused rather than rolled forward. Nothing is compared to
    today: this module does not read the clock, so a past date is the person's
    statement and not this package's business.
    """
    if not isinstance(value, str):
        raise ExplicitProfileInputError(f"{label} must be a YYYY-MM-DD date")
    if _ISO_DATE.match(value) is None:
        raise ExplicitProfileInputError(f"{label} must be written YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ExplicitProfileInputError(f"{label} is not a real calendar date") from error
    return value


@dataclass(frozen=True)
class AvailabilityPreference:
    """When the person said they can start, and nothing more.

    The two forms are exclusive and neither is ever completed: `AVAILABLE_NOW`
    with a date would be a date nobody stated, and `AVAILABLE_FROM` without one
    would be an availability nobody could act on.
    """

    status: AvailabilityStatus
    #: `YYYY-MM-DD`, and only with `AVAILABLE_FROM`.
    available_from: str | None = None

    def __post_init__(self) -> None:
        status = normalize_choice(self.status, AvailabilityStatus, "status")
        object.__setattr__(self, "status", status)
        if status is AvailabilityStatus.AVAILABLE_NOW:
            if self.available_from is not None:
                raise ExplicitProfileInputError(
                    "AVAILABLE_NOW carries no date; it is available now"
                )
            return
        if self.available_from is None:
            raise ExplicitProfileInputError(
                "AVAILABLE_FROM requires the date the person typed"
            )
        object.__setattr__(
            self, "available_from", normalize_iso_date(self.available_from, "available_from")
        )


@dataclass(frozen=True)
class MobilityPreference:
    """Where the person said they are willing to go, in their own words.

    `RESTRICTED` naming nowhere is refused: it would restrict nothing while
    claiming to restrict something. `OPEN` naming nowhere is the ordinary form,
    and `OPEN` naming somewhere is allowed too — "open, and here is where I'd
    rather be" is a statement somebody can make.
    """

    scope: MobilityScope
    locations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        scope = normalize_choice(self.scope, MobilityScope, "scope")
        object.__setattr__(self, "scope", scope)
        locations = normalize_text_list(self.locations, "locations")
        if scope is MobilityScope.RESTRICTED and not locations:
            raise ExplicitProfileInputError(
                "RESTRICTED mobility requires at least one location the person named"
            )
        object.__setattr__(self, "locations", locations)


@dataclass(frozen=True)
class OpportunityPreferences:
    """What the person said they are looking for, and under what conditions.

    Two fields are required because a preference without them says nothing:
    somebody looking for no kind of opportunity, or willing to work in no mode
    at all, has not stated a preference. The four others may be empty or
    `UNKNOWN`, and an `UNKNOWN` is never quietly turned into a `NO`.
    """

    opportunity_types: tuple[OpportunityType, ...]
    work_modes: tuple[WorkMode, ...]
    preferred_domains: tuple[str, ...] = ()
    convention_status: ConventionStatus = ConventionStatus.UNKNOWN
    visa_sponsorship_required: VisaSponsorshipRequired = VisaSponsorshipRequired.UNKNOWN
    constraints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "opportunity_types",
            normalize_registry(
                self.opportunity_types, OpportunityType, "opportunity_types"
            ),
        )
        object.__setattr__(
            self, "work_modes", normalize_registry(self.work_modes, WorkMode, "work_modes")
        )
        object.__setattr__(
            self,
            "preferred_domains",
            normalize_text_list(self.preferred_domains, "preferred_domains"),
        )
        object.__setattr__(
            self,
            "convention_status",
            normalize_choice(self.convention_status, ConventionStatus, "convention_status"),
        )
        object.__setattr__(
            self,
            "visa_sponsorship_required",
            normalize_choice(
                self.visa_sponsorship_required,
                VisaSponsorshipRequired,
                "visa_sponsorship_required",
            ),
        )
        object.__setattr__(
            self, "constraints", normalize_text_list(self.constraints, "constraints")
        )


@dataclass(frozen=True)
class CareerObjectives:
    """What the person said they are aiming for, in their own sentences.

    At least one, because an empty list of objectives is not "no objectives" —
    it is UNKNOWN, and UNKNOWN is represented by there being no fact and no row
    at all rather than by an empty array somebody could read as an answer.
    """

    objectives: tuple[str, ...]

    def __post_init__(self) -> None:
        objectives = normalize_text_list(self.objectives, "objectives")
        if not objectives:
            raise ExplicitProfileInputError(
                "career objectives require at least one objective the person wrote"
            )
        object.__setattr__(self, "objectives", objectives)
