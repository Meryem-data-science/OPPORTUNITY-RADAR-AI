"""Phase 7B.1 — what kind of thing a posting is, and whether you want that kind.

The product this radar serves has narrowed to **PFE / internship** work, and
this slice implements the type side of that and nothing else. It answers one
question:

    is this opportunity's structured type one of the types this profile
    explicitly said it is looking for?

    MATCH          its type is one the profile named
    OUT_OF_TARGET  its type is known, and is not one of them
    UNKNOWN        its type is unknown, or the profile named no type

Two sides, joined only at the last step, and neither of them written to:

    profile_preferences                       -> profile type target resolver
                                                        \\
    opportunity_constraints.opportunity_type   ------------> type target evaluator

Both sides already exist. The types a person is looking for are Phase 3.4C's
`profile_preferences.opportunity_types_json`; the kind a posting is, is Phase
3.5A's `opportunity_constraints.opportunity_type`; and both speak the one
closed registry declared in `services/digital_twin/preferences/models.py`. This
slice adds **no migration, no table, no column and no third taxonomy** — the
verdict is derived from a stated preference, so it is computed on demand and
stored nowhere.

What this package deliberately does not do:

* it does not classify. `opportunity_constraints.opportunity_type` is read back
  exactly as Phase 3.5A stored it; no title, description or qualification is
  re-read here, and a stored type that looks wrong is reported, never repaired
  on the way past — `explain` exists so such a reading can be inspected;
* it does not widen a declared set. `PFE` and `INTERNSHIP` do not imply
  `ALTERNANCE`, `FIRST_JOB`, `JUNIOR_ROLE` or the specific internship kinds,
  and the generic type does not imply the specific ones or the reverse;
* it does not name a type. The evaluator's signature takes the target set, so
  the profile stays the only statement of what is being looked for;
* it does not read geography. Whether a posting is in Morocco is Phase 7A.1's
  answer, in `services/geography/`, and composing the two verdicts into one
  view is a later slice;
* it does not classify Data & AI, does not match a CV, does not score, does not
  rank, does not decide eligibility and does not look at how old a posting is.
  Those are other slices.

    models.py          the vocabulary: the shared registry, three verdicts
    profile_target.py  the types a stated preference names
    evaluator.py       the three-answer comparison, pure
    service.py         the audit, and the single-posting explanation
    cli.py             audit, explain
"""

from services.targeting.opportunity_type.models import (
    TARGETING_VERSION,
    OpportunityType,
    ProfileTypeTarget,
    TargetVerdict,
    TypeTargetAssessment,
    TypeTargetingError,
)

__all__ = [
    "OpportunityType",
    "ProfileTypeTarget",
    "TARGETING_VERSION",
    "TargetVerdict",
    "TypeTargetAssessment",
    "TypeTargetingError",
]
