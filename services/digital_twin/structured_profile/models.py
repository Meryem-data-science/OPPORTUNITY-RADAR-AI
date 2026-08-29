"""Typed, immutable vocabulary of the Phase 3.4B1 structured projection.

A `StructuredExperience` and a `StructuredProject` are the whole explanation of
one reading: what came in, the named rule that applied, the version of the
contract that decided it, and the fragments the rule was willing to name. Every
fragment is optional, and that is the point of the slice: **an absence stays
`None`**, because a guess stored once is believed forever.

Nothing here carries a level, a seniority, a duration, a computed date, a
confidence or a score. A CV states that somebody wrote "Stage" next to a
company name; it does not state that the person is a junior, how many months
they stayed, or which calendar dates a school year covers. None of those is
derived here or anywhere downstream of here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

#: The version of the whole structuring contract: the two closed rules below,
#: the temporal forms they recognise, and the fallback. Any change to what this
#: package produces from a given fact moves this version, because it is what a
#: projected row records to explain how it was produced — and moving it is what
#: makes the next synchronization rewrite the rows it explains.
STRUCTURED_PROFILE_VERSION = "structured-profile-v1"


class StructuringRule(StrEnum):
    """Why one fact was read the way it was.

    The rules are named after the punctuation they require, not after what the
    text is about: `EXPERIENCE_PIPE_HEADER_V1` says the document itself wrote
    pipe-delimited segments, never that a sentence "looks like" a job.
    """

    #: The first line is written `segment | segment | segment`, with at least
    #: three segments that all hold text, and exactly one segment after the
    #: first two is an explicit temporal form.
    EXPERIENCE_PIPE_HEADER_V1 = "EXPERIENCE_PIPE_HEADER_V1"
    #: The first line carries an explicit `:` separator with text on both
    #: sides, optionally opened by a single list marker.
    PROJECT_BULLET_COLON_V1 = "PROJECT_BULLET_COLON_V1"
    #: No closed rule recognised the wording. The fact is still projected, and
    #: every fragment stays `None`: nothing is guessed to fill the row.
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
