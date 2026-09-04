"""The profile target resolver: which kinds of opportunity this person wants.

`profile_preferences` is the source of truth and stays exactly as the person
stated it. This module **derives** the target set for the length of one
comparison and writes nothing back. Phase 7B.1 creates no
`target_preferences`, no `search_preferences`, no `pfe_preferences`, no
`stage_preferences` and no column anywhere: a second place to state what
somebody is looking for is a second place for it to be wrong.

The rules:

    declared-opportunity-types-v1   an accepted PREFERENCE fact names one or
                                    more types -> that set, canonically ordered
    preferences-absent-v1           no accepted PREFERENCE fact -> UNKNOWN
    preferences-no-opportunity-type-v1
                                    the declared set names nothing -> UNKNOWN

**The declared set is the target, exactly.** Nothing here widens it. A profile
that named `PFE` and `INTERNSHIP` is not thereby looking for `ALTERNANCE`,
`FIRST_JOB`, `JUNIOR_ROLE`, `PFA`, `SUMMER_INTERNSHIP` or
`PRE_HIRE_INTERNSHIP`, and no rule in this package treats the generic
`INTERNSHIP` as covering the specific kinds or the other way round. Reading one
as the other would answer MATCH about a posting the person never asked for, or
OUT_OF_TARGET about one they did.

Nothing here is inferred. A CV, a diploma year, a skill, a project, a career
objective, an availability and a mobility are all silent on this question, and
this module reads none of them.

**Nothing here names PFE or INTERNSHIP.** The profile the product currently
serves happens to target those two, but that is a fact about one row of one
database, not about this code: a constant here would be a second, independent
statement of what the person is looking for, and the two would drift apart the
first time they edited their preferences.

An `opportunity_types_json` that no longer decodes is refused out loud by the
Digital Twin's own repository, exactly as a corrupt mobility is on the
geographic side. That is not UNKNOWN: UNKNOWN is the absence of a statement,
and a row that stopped being readable is a defect to fix, not a person who said
nothing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from services.digital_twin.preferences.models import (
    OpportunityType,
    normalize_registry,
)
from services.digital_twin.preferences.repository import get_profile_preferences
from services.targeting.opportunity_type.models import ProfileTypeTarget

DECLARED_TYPES_RULE = "declared-opportunity-types-v1"
PREFERENCES_ABSENT_RULE = "preferences-absent-v1"
NO_DECLARED_TYPE_RULE = "preferences-no-opportunity-type-v1"

TARGET_RULE_IDS = (
    DECLARED_TYPES_RULE,
    PREFERENCES_ABSENT_RULE,
    NO_DECLARED_TYPE_RULE,
)

__all__ = [
    "DECLARED_TYPES_RULE",
    "NO_DECLARED_TYPE_RULE",
    "PREFERENCES_ABSENT_RULE",
    "TARGET_RULE_IDS",
    "resolve_declared_type_target",
    "resolve_profile_type_target",
]


def resolve_declared_type_target(
    profile_id: int, opportunity_types: Sequence[object] | None
) -> ProfileTypeTarget:
    """The target a declared set of types names, without touching a database.

    The pure half of this module: the same rules, over values a caller already
    holds. Absence — `None`, or a set naming nothing — is UNKNOWN. A value
    outside the shared registry is **refused**, never mapped to the nearest
    member and never quietly dropped, because dropping it would narrow a target
    the person wrote wider and let the evaluator answer OUT_OF_TARGET about a
    posting they had asked for.

    The order is the registry's own declaration order, through the same
    `normalize_registry` the preferences themselves are canonicalised by, so
    one set has one representation here and in the database alike.
    """
    if not opportunity_types:
        return ProfileTypeTarget(
            profile_id=profile_id,
            opportunity_types=(),
            rule_id=NO_DECLARED_TYPE_RULE,
        )
    declared = normalize_registry(
        opportunity_types, OpportunityType, "opportunity_types"
    )
    return ProfileTypeTarget(
        profile_id=profile_id,
        opportunity_types=tuple(declared),
        rule_id=DECLARED_TYPES_RULE,
    )


def resolve_profile_type_target(
    connection: sqlite3.Connection, profile_id: int
) -> ProfileTypeTarget:
    """The kinds this profile is looking for, read from what it stated.

    Reads `profile_preferences` through the Digital Twin's own repository — the
    projection stays the only reader of its own tables — and writes nothing.
    An absent row is UNKNOWN: a person who stated no preference has not stated
    that they are looking for nothing.
    """
    stored = get_profile_preferences(connection, profile_id)
    if stored is None:
        return ProfileTypeTarget(
            profile_id=profile_id,
            opportunity_types=(),
            rule_id=PREFERENCES_ABSENT_RULE,
        )
    return resolve_declared_type_target(
        profile_id, stored.value.opportunity_types
    )
