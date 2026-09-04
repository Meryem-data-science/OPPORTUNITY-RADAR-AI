"""The type target evaluator: one posting, one declared set, three answers.

A pure function over values. It takes the kinds a profile said it is looking
for and the structured type Phase 3.5A already read from one posting, and
returns `MATCH`, `OUT_OF_TARGET` or `UNKNOWN`. It touches no database, opens no
connection, reads no profile and writes nothing — **the verdict is never
persisted**, because its left-hand side is a stated preference that can change
this afternoon, and a stored verdict would go on answering for a profile that
no longer exists.

The four rules, in order:

1. **UNKNOWN** — the profile named no kind at all. Answered before the posting
   is even looked at: a comparison with nothing has no result.
2. **UNKNOWN** — the posting's structured type is `None`. No closed rule of
   Phase 3.5A found an explicit statement of what kind of thing it is, and a
   posting nobody could read is not a posting of another kind.
3. **MATCH** — the type is one the profile explicitly named.
4. **OUT_OF_TARGET** — the type is known and is not one of them. This is the
   only branch that contradicts, and it demands a type on both sides.

`UNKNOWN` is not a weak `OUT_OF_TARGET`, and the order above is what keeps them
apart: the two absences are answered before the comparison rather than falling
through it into a refusal.

**Nothing in this module names a type.** There is no `{"PFE", "INTERNSHIP"}`
here, no Morocco, no `opportunity_id`, no title and no special case for any
particular posting. The signature is `evaluate_type_target(target_types,
opportunity_type)` precisely so that the profile stays the only statement of
what is being looked for; a constant in this file would be a second, silent
one. It also re-classifies nothing: `opportunity_type` is copied from
`opportunity_constraints` and is never re-read from a title, a description or a
qualification here — a posting whose stored type looks wrong is an extraction
question, and this function's job is to make it **visible**, not to correct it
on the way past.
"""

from __future__ import annotations

from collections.abc import Collection

from services.digital_twin.preferences.models import OpportunityType
from services.targeting.opportunity_type.models import (
    TargetVerdict,
    TypeTargetAssessment,
    TypeTargetingError,
)

MATCH_RULE = "declared-type-matched-v1"
OUT_OF_TARGET_RULE = "known-type-outside-target-v1"
NO_TARGET_RULE = "no-target-type-v1"
UNKNOWN_TYPE_RULE = "unknown-opportunity-type-v1"

VERDICT_RULE_IDS = (
    MATCH_RULE,
    OUT_OF_TARGET_RULE,
    NO_TARGET_RULE,
    UNKNOWN_TYPE_RULE,
)

__all__ = [
    "MATCH_RULE",
    "NO_TARGET_RULE",
    "OUT_OF_TARGET_RULE",
    "UNKNOWN_TYPE_RULE",
    "VERDICT_RULE_IDS",
    "evaluate_type_target",
]


def evaluate_type_target(
    target_types: Collection[OpportunityType] | None,
    opportunity_type: OpportunityType | None,
) -> TypeTargetAssessment:
    """Judge one posting's type against one target set, and say which rule did.

    `target_types` is empty or `None` when the profile declared no kind the
    registry recognises; `opportunity_type` is `None` when Phase 3.5A asserted
    none. Either absence is UNKNOWN, and the first is answered first.

    Both sides must speak the shared registry. A plain string is refused rather
    than compared, so a caller cannot get a `MATCH` out of `"pfe"` or an
    `OUT_OF_TARGET` out of a value this project has never defined.
    """
    if opportunity_type is not None and not isinstance(
        opportunity_type, OpportunityType
    ):
        raise TypeTargetingError(
            "opportunity_type is an OpportunityType of the shared registry, or None"
        )
    declared = tuple(target_types or ())
    if any(not isinstance(value, OpportunityType) for value in declared):
        raise TypeTargetingError(
            "target types are OpportunityType members of the shared registry"
        )
    target = frozenset(declared)

    if not target:
        return TypeTargetAssessment(
            verdict=TargetVerdict.UNKNOWN,
            rule_id=NO_TARGET_RULE,
            opportunity_type=opportunity_type,
            target_type_count=0,
        )
    if opportunity_type is None:
        return TypeTargetAssessment(
            verdict=TargetVerdict.UNKNOWN,
            rule_id=UNKNOWN_TYPE_RULE,
            opportunity_type=None,
            target_type_count=len(target),
        )
    if opportunity_type in target:
        return TypeTargetAssessment(
            verdict=TargetVerdict.MATCH,
            rule_id=MATCH_RULE,
            opportunity_type=opportunity_type,
            target_type_count=len(target),
        )
    return TypeTargetAssessment(
        verdict=TargetVerdict.OUT_OF_TARGET,
        rule_id=OUT_OF_TARGET_RULE,
        opportunity_type=opportunity_type,
        target_type_count=len(target),
    )
