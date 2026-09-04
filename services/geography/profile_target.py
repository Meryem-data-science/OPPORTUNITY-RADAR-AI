"""The profile target resolver: which country this person is looking in.

`profile_mobility` is the source of truth and stays exactly as the person typed
it. A profile that stated `["Maroc"]` keeps `["Maroc"]` in the database: this
module **derives** `MA` for the length of one comparison and writes nothing
back. There is no second preferences table, no `target_country` column and no
new persisted truth anywhere in Phase 7A.1 — a derived value stored beside its
source is a value that can disagree with it.

The rules are the ones the rest of this package uses, applied to the person's
side of the database:

    mobility-restricted-country-v1  RESTRICTED, and the locations name one
                                    country the registry knows -> that country
    mobility-absent-v1              no accepted MOBILITY fact -> UNKNOWN
    mobility-open-v1                OPEN -> UNKNOWN
    mobility-unresolved-v1          the locations named nothing the registry
                                    knows -> UNKNOWN
    mobility-multiple-countries-v1  the locations named several -> UNKNOWN

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
    """
    if scope is MobilityScope.OPEN:
        return ProfileTarget(
            profile_id=profile_id, country_code=None, rule_id=MOBILITY_OPEN_RULE
        )
    countries = {
        segment.country_code
        for location in locations
        for segment in resolve_location_text(location)
        if segment.status is ResolutionStatus.RESOLVED
    }
    if not countries:
        return ProfileTarget(
            profile_id=profile_id, country_code=None, rule_id=MOBILITY_UNRESOLVED_RULE
        )
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
