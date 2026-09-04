"""The geographic resolver: location text in, country codes out, no guesses.

Pure, local, deterministic and versioned. It reads a string, splits it into the
places it names, and asks the closed registry of `registry.py` which country —
if any — each of them is. It contacts nothing, learns nothing and remembers
nothing between calls, so the same string under the same `RESOLVER_VERSION`
always produces the same rows.

**Segmentation.** One collected string may name several places, because
employers write them that way:

    Doha, Qatar; London, UK; Dubai, UAE

`;` is the separator that means "and another place", and it is the only one
this resolver treats as such (a newline too, which is the same thing typed
differently). A comma is *not* a place separator — `Casablanca, Maroc` is one
place written in two parts — so commas split a segment into its **parts**,
which is a different operation with a different meaning. Order is preserved and
carried into `segment_position`; it is the order the employer wrote, never a
ranking.

**Matching.** Each part is normalised and compared for **equality** against the
catalogues. There is no substring search, and that is what keeps
`France Telecom, Casablanca` out of France: the part `france telecom` is not
`france`, so no country alias fires, the city catalogue answers `MA`, and the
row is right. A substring search would have found `france` inside a company
name and placed the posting in Europe.

**Rules, and the id each row carries.** Every row names the rule that produced
it, including the rows that produced no country — a segment that cannot say why
it is unresolved is not auditable.

    country-alias-v1        exactly one country alias appeared in the parts
    city-catalogue-v1       no country alias, and one catalogued city did
    multiple-countries-v1   parts named two different countries -> AMBIGUOUS
    multiple-cities-v1      cities of two different countries -> AMBIGUOUS
    ambiguous-region-v1     a real area that is not one country -> AMBIGUOUS
    no-geographic-signal-v1 nothing the registry knows -> UNKNOWN

Two countries in one segment are AMBIGUOUS rather than a pick: choosing one
would turn a defect in the string into a fact about the posting, the same way
`0012` records a contradiction instead of resolving it.

**Nothing here reads a work mode.** An address is not evidence of attendance,
and `opportunity_constraints.work_mode` already answers that question. There is
no ON_SITE, HYBRID or REMOTE anywhere in this module.
"""

from __future__ import annotations

import hashlib
import json
import re

from services.geography.models import (
    GeographicError,
    ResolutionStatus,
    ResolvedSegment,
)
from services.geography.registry import (
    country_for_city,
    country_for_text,
    is_ambiguous_region,
    normalize_geographic_text,
)

#: "and another place". Deliberately short: `;` is the only separator the
#: corpus uses to mean it, and a newline is that same key pressed differently.
_SEGMENT_SEPARATORS = re.compile(r"[;\n\r]+")

#: The parts *inside* one place: `Casablanca, Maroc`, `Casablanca / Maroc`,
#: `Casablanca - Maroc`. A dash counts only when it is spaced, so
#: `Aix-en-Provence` stays one part rather than three.
_PART_SEPARATORS = re.compile(r"\s*[,/|()]\s*|\s+[-–—]\s+")

COUNTRY_ALIAS_RULE = "country-alias-v1"
CITY_CATALOGUE_RULE = "city-catalogue-v1"
MULTIPLE_COUNTRIES_RULE = "multiple-countries-v1"
MULTIPLE_CITIES_RULE = "multiple-cities-v1"
AMBIGUOUS_REGION_RULE = "ambiguous-region-v1"
NO_SIGNAL_RULE = "no-geographic-signal-v1"

RULE_IDS = (
    COUNTRY_ALIAS_RULE,
    CITY_CATALOGUE_RULE,
    MULTIPLE_COUNTRIES_RULE,
    MULTIPLE_CITIES_RULE,
    AMBIGUOUS_REGION_RULE,
    NO_SIGNAL_RULE,
)

__all__ = [
    "AMBIGUOUS_REGION_RULE",
    "CITY_CATALOGUE_RULE",
    "COUNTRY_ALIAS_RULE",
    "MULTIPLE_CITIES_RULE",
    "MULTIPLE_COUNTRIES_RULE",
    "NO_SIGNAL_RULE",
    "RULE_IDS",
    "location_fingerprint",
    "resolve_location_text",
    "resolve_segment",
    "split_segments",
]


def split_segments(location_text: str) -> tuple[str, ...]:
    """The places one collected string names, in the order it named them.

    A string that separates nothing is one segment. A string whose separators
    leave nothing behind — `";"` on its own — falls back to the trimmed string
    itself rather than resolving to no rows at all: every source row must
    produce at least one reading, or "already resolved" and "never read" would
    look the same to the next synchronization.
    """
    if not isinstance(location_text, str):
        raise GeographicError("location text must be a string")
    segments = tuple(
        part.strip() for part in _SEGMENT_SEPARATORS.split(location_text) if part.strip()
    )
    if segments:
        return segments
    fallback = location_text.strip()
    if not fallback:
        raise GeographicError("location text must hold something")
    return (fallback,)


def _parts(segment: str) -> tuple[str, ...]:
    """The normalised parts of one segment, plus the segment as a whole.

    The whole is included because a one-part segment — `Morocco` — is the
    common case, and because `Kingdom of Morocco` is one alias containing a
    space rather than three tokens.
    """
    pieces = [
        normalize_geographic_text(piece)
        for piece in _PART_SEPARATORS.split(segment)
    ]
    pieces.append(normalize_geographic_text(segment))
    return tuple(piece for piece in pieces if piece)


def resolve_segment(segment: str) -> ResolvedSegment:
    """Read one place, and say which country it is — or say that it cannot.

    The order of the rules is the order of the evidence: an explicit country
    beats a city that only implies one, and both beat a region that names no
    country at all. Nothing invents a country from a string the registry does
    not hold.
    """
    raw = segment.strip()
    if not raw:
        raise GeographicError("a segment must hold text")
    parts = _parts(raw)

    countries = {
        code for code in (country_for_text(part) for part in parts) if code is not None
    }
    if len(countries) > 1:
        return ResolvedSegment(
            raw_segment=raw,
            status=ResolutionStatus.AMBIGUOUS,
            rule_id=MULTIPLE_COUNTRIES_RULE,
        )

    cities = {
        part: code
        for part, code in ((part, country_for_city(part)) for part in parts)
        if code is not None
    }
    if len(countries) == 1:
        country = next(iter(countries))
        # A city is recorded only when it belongs to the country that was
        # actually resolved: `Casablanca, France` states two things, and the
        # explicit country is the one the employer wrote about the posting.
        city_key = next(
            (part for part, code in cities.items() if code == country), None
        )
        return ResolvedSegment(
            raw_segment=raw,
            status=ResolutionStatus.RESOLVED,
            rule_id=COUNTRY_ALIAS_RULE,
            country_code=country,
            city_key=city_key,
        )

    city_countries = set(cities.values())
    if len(city_countries) > 1:
        return ResolvedSegment(
            raw_segment=raw,
            status=ResolutionStatus.AMBIGUOUS,
            rule_id=MULTIPLE_CITIES_RULE,
        )
    if len(city_countries) == 1:
        city_key, country = next(iter(cities.items()))
        return ResolvedSegment(
            raw_segment=raw,
            status=ResolutionStatus.RESOLVED,
            rule_id=CITY_CATALOGUE_RULE,
            country_code=country,
            city_key=city_key,
        )

    if any(is_ambiguous_region(part) for part in parts):
        return ResolvedSegment(
            raw_segment=raw,
            status=ResolutionStatus.AMBIGUOUS,
            rule_id=AMBIGUOUS_REGION_RULE,
        )
    return ResolvedSegment(
        raw_segment=raw,
        status=ResolutionStatus.UNKNOWN,
        rule_id=NO_SIGNAL_RULE,
    )


def resolve_location_text(location_text: str) -> tuple[ResolvedSegment, ...]:
    """Every place one collected string names, read in order."""
    return tuple(resolve_segment(segment) for segment in split_segments(location_text))


def location_fingerprint(location_text: str) -> str:
    """The SHA-256 of the canonical JSON of exactly what the resolver reads.

    One field, because one field is all it reads. The source row's id is
    excluded on purpose, the same way `0012`'s fingerprint excludes the posting
    id: the identity of a row is not part of its text, and two rows holding the
    same string are the same input.
    """
    serialized = json.dumps(
        {"location_text": location_text},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()
