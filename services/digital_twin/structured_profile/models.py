"""Typed, immutable vocabulary of the Phase 3.4B structured projection.

A `StructuredExperience`, `StructuredProject`, `StructuredEducation`,
`StructuredCertification` or `StructuredLanguage` is the whole explanation of
one reading: what came in, the named rule that applied, the version of the
contract that decided it, and the fragments the rule was willing to name. Every
fragment is optional, and that is the point of the slice: **an absence stays
`None`**, because a guess stored once is believed forever.

Nothing here carries a level, a seniority, a duration, a computed date, a
confidence or a score. A CV states that somebody wrote "Stage" next to a
company name; it does not state that the person is a junior, how many months
they stayed, or which calendar dates a school year covers. It states that
somebody wrote "Master"; it does not state `Bac+5`. It states that somebody
wrote "courant"; it does not state `C1`. None of those is derived here or
anywhere downstream of here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

#: The version of the whole structuring contract: the closed rules below, the
#: temporal forms they recognise, the closed registries they compare against,
#: and the per-type fallbacks. Any change to what this package produces **from
#: a given fact** moves this version, because it is what a projected row
#: records to explain how it was produced — and moving it is what makes the
#: next synchronization rewrite the rows it explains.
#:
#: Phase 3.4B2 adds three fact types and leaves it at `v1` deliberately.
#: Adding a type is backward-compatible by construction: `EXPERIENCE` and
#: `PROJECT` are read by the same two rules, over the same temporal grammar,
#: and produce byte-for-byte the same fragments as before, so rewriting their
#: eight existing rows would be churn with no reading behind it. A version is a
#: statement about *how a row was produced*; the rows produced by
#: `EXPERIENCE_PIPE_HEADER_V1`, `PROJECT_BULLET_COLON_V1` and `UNPARSED_V1`
#: were produced exactly as `structured-profile-v1` says they were. The day one
#: of those readings changes, this string moves and every row is rewritten.
STRUCTURED_PROFILE_VERSION = "structured-profile-v1"


class StructuringRule(StrEnum):
    """Why one fact was read the way it was.

    The rules are named after the punctuation and the closed registries they
    require, not after what the text is about: `EXPERIENCE_PIPE_HEADER_V1` says
    the document itself wrote pipe-delimited segments, never that a sentence
    "looks like" a job, and `EDUCATION_PIPE_EXPLICIT_V1` says a closed marker
    told a school from a programme, never that segment 1 "is usually" a
    diploma.
    """

    #: The first line is written `segment | segment | segment`, with at least
    #: three segments that all hold text, and exactly one segment after the
    #: first two is an explicit temporal form.
    EXPERIENCE_PIPE_HEADER_V1 = "EXPERIENCE_PIPE_HEADER_V1"
    #: The first line carries an explicit `:` separator with text on both
    #: sides, optionally opened by a single list marker.
    PROJECT_BULLET_COLON_V1 = "PROJECT_BULLET_COLON_V1"
    #: A pipe header holding exactly one explicit period, exactly two other
    #: segments, and exactly one of those two carrying a closed institution
    #: marker. The marked one is the institution, the other the programme.
    EDUCATION_PIPE_EXPLICIT_V1 = "EDUCATION_PIPE_EXPLICIT_V1"
    #: A pipe header holding exactly one explicit period, whose other segments
    #: cannot be told apart honestly. The period is kept verbatim; the
    #: institution and the programme stay `None` rather than being guessed.
    EDUCATION_PIPE_PERIOD_ONLY_V1 = "EDUCATION_PIPE_PERIOD_ONLY_V1"
    #: No closed rule recognised the education wording. Every fragment `None`.
    EDUCATION_UNPARSED_V1 = "EDUCATION_UNPARSED_V1"
    #: Every segment of the first line is either an explicit `label: value`
    #: whose label belongs to the closed registry, or a whole explicit period.
    #: The certification label is present exactly once, no label repeats, and
    #: no word of the closed intention registry appears anywhere in the line.
    CERTIFICATION_EXPLICIT_V1 = "CERTIFICATION_EXPLICIT_V1"
    #: No closed rule recognised the certification wording. Every fragment
    #: `None` — in particular, nothing is recorded as obtained.
    CERTIFICATION_UNPARSED_V1 = "CERTIFICATION_UNPARSED_V1"
    #: The line carries one explicit separator, and everything on its right is
    #: a single form of the closed proficiency registry. Both sides are stored
    #: verbatim; the registry is used to compare, never to translate.
    LANGUAGE_EXPLICIT_PROFICIENCY_V1 = "LANGUAGE_EXPLICIT_PROFICIENCY_V1"
    #: No closed rule recognised the language wording. Every fragment `None`.
    LANGUAGE_UNPARSED_V1 = "LANGUAGE_UNPARSED_V1"
    #: No closed rule recognised the wording of an experience or a project. The
    #: fact is still projected, and every fragment stays `None`: nothing is
    #: guessed to fill the row. It keeps its Phase 3.4B1 name and its Phase
    #: 3.4B1 meaning; the three types added by Phase 3.4B2 each carry their own
    #: fallback above, so a tally can say which reading declined.
    UNPARSED_V1 = "UNPARSED_V1"


class StructuredProfileError(RuntimeError):
    """Raised when the structured projection cannot be read or written safely."""


class StructuredProfileNotFoundError(StructuredProfileError):
    """Raised when the profile the projection was asked about does not exist.

    Synchronizing is not a way to bring a profile into existence: an absent
    profile is an absence, and this package creates no user and no profile.
    """


@dataclass(frozen=True)
class StructuredExperience:
    """One EXPERIENCE fact, as the closed rules read it. Fragments may be None."""

    #: The technical input: the fact's own value, unchanged.
    source_value: str
    role_text: str | None
    organization_text: str | None
    period_text: str | None
    description_text: str | None
    structurer_version: str
    structuring_rule_id: StructuringRule


@dataclass(frozen=True)
class StructuredProject:
    """One PROJECT fact, as the closed rules read it. Fragments may be None."""

    source_value: str
    title_text: str | None
    period_text: str | None
    description_text: str | None
    structurer_version: str
    structuring_rule_id: StructuringRule


@dataclass(frozen=True)
class StructuredEducation:
    """One EDUCATION fact, as the closed rules read it. Fragments may be None.

    There is no `degree_level`, no `bac_plus` and no `graduation_year`: the
    word "Master" is a word the document wrote, not a level, and a school year
    is not a pair of calendar dates.
    """

    source_value: str
    institution_text: str | None
    program_text: str | None
    period_text: str | None
    description_text: str | None
    structurer_version: str
    structuring_rule_id: StructuringRule


@dataclass(frozen=True)
class StructuredCertification:
    """One CERTIFICATION fact, as the closed rules read it.

    There is no `obtained`, no `obtained_at` and no `expires_at`: naming a
    certification is not holding one, and no rule here turns a stated intention
    into an achievement.
    """

    source_value: str
    certification_text: str | None
    issuer_text: str | None
    period_text: str | None
    description_text: str | None
    structurer_version: str
    structuring_rule_id: StructuringRule


@dataclass(frozen=True)
class StructuredLanguage:
    """One LANGUAGE fact, as the closed rules read it.

    `proficiency_text` is the wording the document used, verbatim. There is no
    CEFR field and no mapping onto one: "courant" stays "courant".
    """

    source_value: str
    language_text: str | None
    proficiency_text: str | None
    structurer_version: str
    structuring_rule_id: StructuringRule


@dataclass(frozen=True)
class ProfileExperience:
    """One persisted experience row.

    It carries no provenance of its own: `fact_id` is the way to the fact, and
    the fact is the way to `profile_fact_provenance`. It carries no seniority,
    no duration and no level either, because nothing proves one.
    """

    id: int
    profile_id: int
    fact_id: int
    role_text: str | None
    organization_text: str | None
    period_text: str | None
    description_text: str | None
    structurer_version: str
    structuring_rule_id: str
    created_at: str


@dataclass(frozen=True)
class ProfileProject:
    """One persisted project row. Same chain, same refusals."""

    id: int
    profile_id: int
    fact_id: int
    title_text: str | None
    period_text: str | None
    description_text: str | None
    structurer_version: str
    structuring_rule_id: str
    created_at: str


@dataclass(frozen=True)
class ProfileEducation:
    """One persisted education row. Same chain, same refusals."""

    id: int
    profile_id: int
    fact_id: int
    institution_text: str | None
    program_text: str | None
    period_text: str | None
    description_text: str | None
    structurer_version: str
    structuring_rule_id: str
    created_at: str


@dataclass(frozen=True)
class ProfileCertification:
    """One persisted certification row. Same chain, same refusals."""

    id: int
    profile_id: int
    fact_id: int
    certification_text: str | None
    issuer_text: str | None
    period_text: str | None
    description_text: str | None
    structurer_version: str
    structuring_rule_id: str
    created_at: str


@dataclass(frozen=True)
class ProfileLanguage:
    """One persisted language row. Same chain, same refusals."""

    id: int
    profile_id: int
    fact_id: int
    language_text: str | None
    proficiency_text: str | None
    structurer_version: str
    structuring_rule_id: str
    created_at: str


#: Every reading this package can produce, for the reconciliation to hold one
#: of without caring which.
StructuredEntry = (
    StructuredExperience
    | StructuredProject
    | StructuredEducation
    | StructuredCertification
    | StructuredLanguage
)
