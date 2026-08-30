"""Phase 3.4C: what a person states about availability, mobility and wants.

Four things a CV cannot say, and which this package therefore never reads from
one: when somebody can start, where they will go, what kind of opportunity and
way of working they want (with the convention and visa answers that go with
it), and what they are aiming for. All four arrive as explicit `USER_INPUT`,
are recorded as `ACCEPTED` facts in `profile_facts`, and are projected onto the
four singleton tables migration `0011` adds.

    explicit user input
        -> profile_facts (ACCEPTED, USER_INPUT provenance)
             +-> profile_availability
             +-> profile_mobility
             +-> profile_preferences
             +-> profile_career_objectives

No accepted fact means no row, and no row means **UNKNOWN** — never `FALSE`,
never a default and never a plausible value.

The package holds no matching, no eligibility, no scoring and no ranking: it is
the profile side of Phases 3.5/3.6, and those are not implemented.
"""
