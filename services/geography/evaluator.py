"""The target evaluator: one posting, one target country, three answers.

A pure function over values. It takes the country a profile is looking in and
the resolutions of one posting, and returns `MATCH`, `OUT_OF_TARGET` or
`UNKNOWN`. It touches no database, reads no profile and writes nothing —
**the verdict is never persisted**, because its left-hand side is a declared
mobility that can change this afternoon, and a stored verdict would go on
answering for a profile that no longer exists.

The three rules, in order:

1. **MATCH** — at least one segment RESOLVED to the target country. A posting
   in Casablanca *and* Paris is in Casablanca; a second city does not undo the
   first.
2. **OUT_OF_TARGET** — no such segment, the posting has segments, and every one
   of them RESOLVED to some other country. This is the only branch that
   contradicts, and it demands positive evidence for every segment.
3. **UNKNOWN** — everything else: no target, no locations, or any segment this
   package could not place.

`UNKNOWN` is not a weak `OUT_OF_TARGET`. One AMBIGUOUS segment beside a French
one is enough to withhold the refusal, because `APAC` may well contain Morocco
and a posting is not excluded by a string nobody could read.
"""

from __future__ import annotations

from collections.abc import Sequence

from services.geography.models import (
    GeographicError,
    LocationResolution,
    ResolutionStatus,
    TargetAssessment,
    TargetVerdict,
)

MATCH_RULE = "target-country-present-v1"
OUT_OF_TARGET_RULE = "every-location-resolved-elsewhere-v1"
NO_TARGET_RULE = "no-target-country-v1"
NO_LOCATION_RULE = "no-location-resolved-v1"
UNRESOLVED_LOCATION_RULE = "unresolved-location-present-v1"

VERDICT_RULE_IDS = (
    MATCH_RULE,
    OUT_OF_TARGET_RULE,
    NO_TARGET_RULE,
    NO_LOCATION_RULE,
    UNRESOLVED_LOCATION_RULE,
)

__all__ = [
    "MATCH_RULE",
    "NO_LOCATION_RULE",
    "NO_TARGET_RULE",
    "OUT_OF_TARGET_RULE",
    "UNRESOLVED_LOCATION_RULE",
    "VERDICT_RULE_IDS",
    "evaluate_target",
]


def evaluate_target(
    target_country: str | None,
    resolutions: Sequence[LocationResolution],
) -> TargetAssessment:
    """Judge one posting against one target country, and say which rule did it.

    `target_country` is `None` when the profile declared no single country the
    registry recognises; that is `UNKNOWN`, and it is answered before the
    posting is even looked at — a comparison with nothing has no result.
    """
    if target_country is not None and (
        len(target_country) != 2 or target_country != target_country.upper()
    ):
        raise GeographicError("target country is ISO 3166-1 alpha-2, upper case")

    segments = len(resolutions)
    resolved = [item for item in resolutions if item.status is ResolutionStatus.RESOLVED]
    matching = [item for item in resolved if item.country_code == target_country]

    if target_country is None:
        return TargetAssessment(
            verdict=TargetVerdict.UNKNOWN,
            rule_id=NO_TARGET_RULE,
            segments=segments,
            resolved_segments=len(resolved),
            matching_segments=0,
        )
    if matching:
        return TargetAssessment(
            verdict=TargetVerdict.MATCH,
            rule_id=MATCH_RULE,
            segments=segments,
            resolved_segments=len(resolved),
            matching_segments=len(matching),
        )
    if segments == 0:
        return TargetAssessment(
            verdict=TargetVerdict.UNKNOWN,
            rule_id=NO_LOCATION_RULE,
            segments=0,
            resolved_segments=0,
            matching_segments=0,
        )
    if len(resolved) == segments:
        return TargetAssessment(
            verdict=TargetVerdict.OUT_OF_TARGET,
            rule_id=OUT_OF_TARGET_RULE,
            segments=segments,
            resolved_segments=len(resolved),
            matching_segments=0,
        )
    return TargetAssessment(
        verdict=TargetVerdict.UNKNOWN,
        rule_id=UNRESOLVED_LOCATION_RULE,
        segments=segments,
        resolved_segments=len(resolved),
        matching_segments=0,
    )
