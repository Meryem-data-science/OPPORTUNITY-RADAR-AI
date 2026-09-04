"""Phase 7A.1 — where a posting is, and whether that is where you are looking.

The product this radar serves has narrowed to **Morocco only**, and this slice
implements the geography of that and nothing else. It answers one question:

    does this opportunity sit in the country this profile is targeting?

    MATCH          at least one of its locations resolves to that country
    OUT_OF_TARGET  every one of its locations resolves, all to other countries
    UNKNOWN        anything else

Two sides, joined only at the last step:

    profile_mobility                 -> profile target resolver -> country code
    opportunity_constraint_locations -> geographic resolver -> resolutions
                                                          \\-> target evaluator

The projection in the middle — `opportunity_location_resolutions`, added by
`0024` — is derived from the employer's own words and is stored. The verdict is
derived from a person's declared mobility and is **not**: a mobility can change
this afternoon, and a stored verdict would go on answering for a profile that
no longer exists.

What this package deliberately does not do:

* it does not geocode. The registry is a closed, hand-written table of aliases
  in `registry.py`; there is no network call, no geocoding API, no model and no
  fuzzy matching anywhere in the package, and a string it does not recognise is
  answered UNKNOWN rather than approximately;
* it does not rewrite its sources. `opportunity_constraint_locations` keeps the
  raw strings, `profile_mobility` keeps the person's own words — `["Maroc"]`
  stays `["Maroc"]`, and `MA` is derived for the length of one comparison;
* it does not read a work mode. `opportunity_constraints.work_mode` already
  answers remote / hybrid / on-site, and an address is not evidence of
  attendance;
* it does not score, rank or decide eligibility, and it says nothing about
  PFE/internship targeting or Data & AI classification. Those are other slices.

    models.py          the vocabulary: three statuses, three verdicts
    registry.py        the closed catalogues, and why they are closed
    resolver.py        segmentation and the rules, pure
    profile_target.py  the country a declared mobility restricts one to
    evaluator.py       the three-answer comparison, pure
    repository.py      transactional persistence of the projection
    service.py         idempotent synchronization, and the targeting audit
    cli.py             status, sync, audit
"""

from services.geography.models import (
    RESOLVER_VERSION,
    LocationResolution,
    ProfileTarget,
    ResolutionStatus,
    ResolvedSegment,
    TargetAssessment,
    TargetVerdict,
)

__all__ = [
    "LocationResolution",
    "ProfileTarget",
    "RESOLVER_VERSION",
    "ResolutionStatus",
    "ResolvedSegment",
    "TargetAssessment",
    "TargetVerdict",
]
