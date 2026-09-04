"""The profile target resolver: which country this person is looking in.

`profile_mobility` is the source of truth and stays exactly as the person typed
it. A profile that stated `["Maroc"]` keeps `["Maroc"]` in the database: this
module **derives** `MA` for the length of one comparison and writes nothing
back. There is no second preferences table, no `target_country` column and no
new persisted truth anywhere in Phase 7A.1 — a derived value stored beside its
source is a value that can disagree with it.

The rules are the ones the rest of this package uses, applied to the person's
side of the database:

    mobility-restricted-country-v1  RESTRICTED, and **every** location the
                                    person named resolves to the same country
                                    -> that country
    mobility-absent-v1              no accepted MOBILITY fact -> UNKNOWN
    mobility-open-v1                OPEN -> UNKNOWN
    mobility-unresolved-v1          at least one location this package could
                                    not place -> UNKNOWN
    mobility-multiple-countries-v1  every location placed, in several
                                    countries -> UNKNOWN

**A target country is unanimous or it does not exist.** Every declared location
must resolve, and all of them to one country; a single AMBIGUOUS or UNKNOWN
entry withholds the target rather than being skipped. `["Maroc", "Atlantis"]`
is UNKNOWN, not `MA`: ignoring the entry nobody could place would read absence
of evidence as agreement, quietly narrow a restriction the person wrote wider
than one country, and let the evaluator answer OUT_OF_TARGET about a place they
may well have meant to include. `UNKNOWN` is not `FALSE` on this side of the
database either.

`["Maroc", "Casablanca"]` is `MA` all the same: several entries naming one
country are unanimous, not multiple.

`OPEN` is UNKNOWN and not a target, however the locations read. `OPEN` means
"I will go anywhere", possibly with a preference attached; deriving a target
country from it would let this package answer OUT_OF_TARGET about a posting the
person explicitly said they were open to. A target country is what a
**restriction** names.

Several countries is UNKNOWN and never a pick. The profile audited before this
slice still said `["Maroc", "France"]`; the honest reading of that is "this
person has not restricted themselves to one country", not "Morocco, because it
was written first". Narrowing the search to Morocco is something the person
does by editing their mobility through the Digital Twin commands — not
something this resolver does on their behalf.
"""

from __future__ import annotations

import sqlite3

from services.digital_twin.preferences.models import MobilityScope
from services.digital_twin.preferences.repository import get_profile_mobility
from services.geography.models import ProfileTarget, ResolutionStatus
from services.geography.resolver import resolve_location_text

RESTRICTED_COUNTRY_RULE = "mobility-restricted-country-v1"
MOBILITY_ABSENT_RULE = "mobility-absent-v1"
MOBILITY_OPEN_RULE = "mobility-open-v1"
MOBILITY_UNRESOLVED_RULE = "mobility-unresolved-v1"
MOBILITY_MULTIPLE_COUNTRIES_RULE = "mobility-multiple-countries-v1"

TARGET_RULE_IDS = (
    RESTRICTED_COUNTRY_RULE,
    MOBILITY_ABSENT_RULE,
    MOBILITY_OPEN_RULE,
    MOBILITY_UNRESOLVED_RULE,
    MOBILITY_MULTIPLE_COUNTRIES_RULE,
)

__all__ = [
    "MOBILITY_ABSENT_RULE",
    "MOBILITY_MULTIPLE_COUNTRIES_RULE",
    "MOBILITY_OPEN_RULE",
    "MOBILITY_UNRESOLVED_RULE",
    "RESTRICTED_COUNTRY_RULE",
    "TARGET_RULE_IDS",
    "resolve_declared_target",
    "resolve_profile_target",
]


def resolve_declared_target(
    profile_id: int, scope: MobilityScope, locations: tuple[str, ...]
) -> ProfileTarget:
    """The target a declared mobility names, without touching the database.

    The pure half of this module: the same rules, over values a caller already
    holds. Every location the person typed is read by the ordinary geographic
    resolver — the same registry, the same normalisation, the same rule ids —
    so `Maroc` on the profile side and `Maroc` on the posting side can never be
    read two different ways.

    A target is unanimous or absent: every segment of every declared location
    must resolve, and all to one country. Nothing here drops the segments it
    could not read.
    """
    if scope is MobilityScope.OPEN:
        return ProfileTarget(
            profile_id=profile_id, country_code=None, rule_id=MOBILITY_OPEN_RULE
        )
    segments = tuple(
        segment
        for location in locations
        for segment in resolve_location_text(location)
    )
    # An entry nobody could place is not an entry that can be skipped. One
    # AMBIGUOUS or UNKNOWN segment — and the empty case, which resolves
    # nothing at all — withholds the target.
    if not segments or any(
        segment.status is not ResolutionStatus.RESOLVED for segment in segments
    ):
        return ProfileTarget(
            profile_id=profile_id, country_code=None, rule_id=MOBILITY_UNRESOLVED_RULE
        )
    countries = {segment.country_code for segment in segments}
    if len(countries) > 1:
        return ProfileTarget(
            profile_id=profile_id,
            country_code=None,
            rule_id=MOBILITY_MULTIPLE_COUNTRIES_RULE,
        )
    return ProfileTarget(
        profile_id=profile_id,
        country_code=next(iter(countries)),
        rule_id=RESTRICTED_COUNTRY_RULE,
    )


def resolve_profile_target(
    connection: sqlite3.Connection, profile_id: int
) -> ProfileTarget:
    """The country this profile restricts itself to, read from what it stated.

    Reads `profile_mobility` through the Digital Twin's own repository — the
    projection stays the only reader of its own tables — and writes nothing.
    An absent row is UNKNOWN: a person who stated no mobility has not stated
    that they will go nowhere.
    """
    stored = get_profile_mobility(connection, profile_id)
    if stored is None:
        return ProfileTarget(
            profile_id=profile_id, country_code=None, rule_id=MOBILITY_ABSENT_RULE
        )
    mobility = stored.value
    return resolve_declared_target(profile_id, mobility.scope, mobility.locations)
