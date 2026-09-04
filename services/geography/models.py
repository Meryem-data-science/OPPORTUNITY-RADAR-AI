"""The vocabulary of Phase 7A.1: three resolution statuses, three verdicts.

Two questions live in this package and they are kept apart on purpose:

    *what country does this posting name?*      a property of the posting
    *is that the country I am looking in?*      a property of a person, today

The first is derived from the employer's own words, is stable while those words
are, and is therefore projected into `opportunity_location_resolutions`. The
second depends on a declared mobility that can change this afternoon, so it is
computed on demand and **stored nowhere**. Nothing below carries a profile id,
a score, a rank or an eligibility.

`UNKNOWN` is never `FALSE`, on either side. A segment nobody could place is a
question about the posting, not a statement that the posting is somewhere else,
and `TargetVerdict.UNKNOWN` is never a soft `OUT_OF_TARGET`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

#: The version of the whole geographic contract: the alias registry, the city
#: catalogue, the segmentation, the normalisation and the rule ids below. Any
#: change to what this package resolves **from a given string** moves this
#: version, and moving it is what makes the next synchronization recompute the
#: rows it explains. It is deliberately not a caller-supplied argument: see
#: `services/geography/service.py`.
RESOLVER_VERSION = "geographic-resolver-v1"

__all__ = [
    "GeographicError",
    "LocationResolution",
    "LocationSource",
    "ProfileTarget",
    "RESOLVER_VERSION",
    "ResolutionStatus",
    "ResolvedSegment",
    "TargetAssessment",
    "TargetVerdict",
]


class GeographicError(ValueError):
    """Raised when a geographic value is malformed, before any write."""


class ResolutionStatus(StrEnum):
    """What one segment of a location string could be turned into.

    `RESOLVED`   a closed rule named exactly one country for this segment.
    `AMBIGUOUS`  the text does name a place, but not one country — `APAC`,
                 `EMEA`, or a segment naming two countries at once.
    `UNKNOWN`    no geographic signal the registry knows — `Any Office`.

    The last two are different questions, not different amounts of confidence,
    and neither is ever read as "not in the target country".
    """

    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    UNKNOWN = "UNKNOWN"


class TargetVerdict(StrEnum):
    """Whether a posting sits in the country a profile is looking in.

    `MATCH`          at least one segment resolved to the target country.
    `OUT_OF_TARGET`  every segment resolved, and every one to another country.
    `UNKNOWN`        anything else, including a posting with no location at
                     all and a profile that declared no target.

    There is no fourth answer and no number. A posting is not 70% Moroccan.
    """

    MATCH = "MATCH"
    OUT_OF_TARGET = "OUT_OF_TARGET"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ResolvedSegment:
    """One segment of one location string, as the pure resolver read it.

    This is the resolver's whole output for a segment and carries no identity:
    no opportunity, no source row, no position. Those belong to the projection
    (`LocationResolution`), which is assembled by the service from a source row
    and a sequence of these. Keeping them apart is what lets the resolver be
    tested on strings alone.
    """

    raw_segment: str
    status: ResolutionStatus
    rule_id: str
    country_code: str | None = None
    city_key: str | None = None

    def __post_init__(self) -> None:
        if not self.raw_segment.strip():
            raise GeographicError("a segment must hold text")
        if self.raw_segment != self.raw_segment.strip():
            raise GeographicError("a segment is stored trimmed")
        if not self.rule_id.strip() or self.rule_id != self.rule_id.strip():
            raise GeographicError("a resolution names one trimmed rule id")
        resolved = self.status is ResolutionStatus.RESOLVED
        if resolved != (self.country_code is not None):
            raise GeographicError(
                "RESOLVED is exactly 'a country was determined'; the other two "
                "statuses carry no country"
            )
        if self.country_code is not None and (
            len(self.country_code) != 2 or self.country_code != self.country_code.upper()
        ):
            raise GeographicError("country_code is ISO 3166-1 alpha-2, upper case")
        if self.city_key is not None and self.country_code is None:
            raise GeographicError("a city that implied no country implies nothing")


@dataclass(frozen=True)
class LocationSource:
    """One `opportunity_constraint_locations` row, as the resolver reads it.

    `location_text` is the employer's string, untouched. The row's identity
    travels with it so the projection can point back at the evidence, and is
    deliberately **not** part of the fingerprint: the identity of a row is not
    part of what was read.
    """

    opportunity_id: int
    source_location_id: int
    position: int
    location_text: str


@dataclass(frozen=True)
class LocationResolution:
    """One stored row of `opportunity_location_resolutions`.

    A projection row: what the resolver said about one segment, plus which
    source row and which posting it was read from, plus the pair that makes the
    next run idempotent.
    """

    opportunity_id: int
    source_location_id: int
    segment_position: int
    raw_segment: str
    status: ResolutionStatus
    rule_id: str
    country_code: str | None
    city_key: str | None
    resolver_version: str
    source_fingerprint: str

    @property
    def resolved(self) -> bool:
        return self.status is ResolutionStatus.RESOLVED


@dataclass(frozen=True)
class ProfileTarget:
    """The one country a profile's declared mobility restricts it to, or none.

    `country_code` is `None` whenever the profile did not state a single
    country the registry recognises, and `rule_id` says which of those cases it
    was. It is **derived**: `profile_mobility` keeps the person's own words —
    `["Maroc"]` stays `["Maroc"]` — and this value is never written back.
    """

    profile_id: int
    country_code: str | None
    rule_id: str

    @property
    def known(self) -> bool:
        return self.country_code is not None


@dataclass(frozen=True)
class TargetAssessment:
    """One posting judged against one target country, and why.

    Computed on demand and stored nowhere: the left-hand side of this
    comparison is a profile, and a profile changes.
    """

    verdict: TargetVerdict
    rule_id: str
    segments: int
    resolved_segments: int
    matching_segments: int

    def as_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict.value,
            "rule_id": self.rule_id,
            "segments": self.segments,
            "resolved_segments": self.resolved_segments,
            "matching_segments": self.matching_segments,
        }
