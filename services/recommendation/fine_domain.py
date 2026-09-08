"""The fine-domain fit: a Phase 8 category read against declared preferences.

Phase 8 assigns one of eleven fine categories; the Digital Twin holds a person's
`preferred_domains` as free text. Nothing may compare those two directly — there
is no fuzzy matching, no embedding, no similarity and no invented confidence
anywhere in this module. The comparison goes through the bridge Phase 4 already
owns:

    fine category  ->  canonical `Domain` family  ->  recognised preference

The first arrow is the table below and is the only new mapping this slice adds.
The second arrow is **not reimplemented**: the alignment is delegated to
`build_role_domain_preference_signals`, so the label catalogue, the
normalisation, the rank, and the MATCH / MISMATCH / UNKNOWN semantics are
literally `role-domain-preferences-v2`'s, applied to a fine-derived domain
instead of the coarse one. A second copy of that catalogue would start
disagreeing with the first the day either moved.

**Three categories are deliberately unbridged.**

    NLP, COMPUTER_VISION   the canonical `Domain` vocabulary has no family for
                           them. Widening either one into `MACHINE_LEARNING_AI`
                           would let a preference the person never expressed
                           decide their ranking; the honest answer is that this
                           system cannot read the fine category against these
                           preferences, so the coarse component answers instead.
    OTHER                  `OTHER` states "demonstrably Data/AI, no supported
                           sub-domain evidenced". It is a value, not an absence
                           — and precisely because it asserts no sub-domain, it
                           can neither match nor contradict a domain preference.

An unbridged category is `UNAVAILABLE`, which routes the caller to the coarse
domain component of the matching snapshot. It is never a MISMATCH: failing to
read something is not evidence against it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from services.collector.matching import (
    AlignmentReason,
    AlignmentStatus,
    build_role_domain_preference_signals,
)
from services.collector.matching.models import (
    MatchingInput,
    MatchingOpportunityInput,
    MatchingPreferences,
    MatchingProfileInput,
    MatchingQualification,
)
from services.collector.qualification.fine_taxonomy import FineCategory
from services.collector.qualification.taxonomy import Domain, OpportunityType, Qualification

from .models import RecommendationFineClassification, RecommendationInputError

#: The fine -> canonical family table, and the list of what it refuses to map.
#: Moving either moves this version, and this version is part of every
#: fingerprint whose domain component came from the fine half.
FINE_DOMAIN_BRIDGE_VERSION = "fine-domain-bridge-v1"

__all__ = [
    "FINE_DOMAIN_BRIDGE",
    "FINE_DOMAIN_BRIDGE_VERSION",
    "UNBRIDGED_FINE_CATEGORIES",
    "FineDomainAvailability",
    "FineDomainFit",
    "FineDomainReason",
    "build_fine_domain_fit",
    "preferred_rank_score",
]


class FineDomainAvailability(StrEnum):
    """Whether the fine half could be read against the preferences at all."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class FineDomainReason(StrEnum):
    """Why the fine half was used, or why it was not.

    The three `FINE_*` reasons are the unavailable cases; the rest are the
    Phase 4 domain reasons restated, so a reader who knows
    `role-domain-preferences-v2` reads these without a second vocabulary.
    """

    #: Legacy row: migration 0025 reached it, the fine classifier never did.
    FINE_CLASSIFICATION_ABSENT = "FINE_CLASSIFICATION_ABSENT"
    #: Classified, and deliberately assigned no category.
    FINE_CATEGORY_ABSENT = "FINE_CATEGORY_ABSENT"
    #: NLP, COMPUTER_VISION or OTHER: no honest canonical family exists.
    FINE_CATEGORY_NOT_BRIDGED = "FINE_CATEGORY_NOT_BRIDGED"
    PROFILE_PREFERENCES_ABSENT = "PROFILE_PREFERENCES_ABSENT"
    DOMAIN_PREFERENCES_EMPTY = "DOMAIN_PREFERENCES_EMPTY"
    DOMAIN_PREFERENCE_MATCHED = "DOMAIN_PREFERENCE_MATCHED"
    DOMAIN_PREFERENCE_NOT_LISTED = "DOMAIN_PREFERENCE_NOT_LISTED"
    DOMAIN_PREFERENCES_PARTIALLY_UNMAPPED = "DOMAIN_PREFERENCES_PARTIALLY_UNMAPPED"


#: Only semantically evident correspondences. `ARTIFICIAL_INTELLIGENCE` joins
#: `MACHINE_LEARNING` under `MACHINE_LEARNING_AI` because that family already
#: names both in its own label catalogue ("ML/AI", "Artificial Intelligence"),
#: and `DATA_ANALYTICS` joins `BUSINESS_INTELLIGENCE` under `BI_ANALYTICS` for
#: the same reason ("BI/Analytics", "Data Analytics / Business Intelligence").
FINE_DOMAIN_BRIDGE: dict[FineCategory, Domain] = {
    FineCategory.DATA_SCIENCE: Domain.DATA_SCIENCE,
    FineCategory.DATA_ANALYTICS: Domain.BI_ANALYTICS,
    FineCategory.BUSINESS_INTELLIGENCE: Domain.BI_ANALYTICS,
    FineCategory.DATA_ENGINEERING: Domain.DATA_ENGINEERING,
    FineCategory.MACHINE_LEARNING: Domain.MACHINE_LEARNING_AI,
    FineCategory.ARTIFICIAL_INTELLIGENCE: Domain.MACHINE_LEARNING_AI,
    FineCategory.GENERATIVE_AI: Domain.GENAI_LLM,
    FineCategory.MLOPS: Domain.MLOPS_ML_PLATFORM,
}

#: Stated explicitly rather than derived by subtraction, so that adding a fine
#: category without deciding what it means here fails at import time instead of
#: quietly falling into one branch or the other.
UNBRIDGED_FINE_CATEGORIES = (
    FineCategory.NLP,
    FineCategory.COMPUTER_VISION,
    FineCategory.OTHER,
)

if set(FINE_DOMAIN_BRIDGE) | set(UNBRIDGED_FINE_CATEGORIES) != set(FineCategory):
    raise RuntimeError("every fine category must be bridged or explicitly unbridged")
if set(FINE_DOMAIN_BRIDGE) & set(UNBRIDGED_FINE_CATEGORIES):
    raise RuntimeError("a fine category cannot be both bridged and unbridged")


@dataclass(frozen=True)
class FineDomainFit:
    """What the fine half said, or why it said nothing.

    `status` and `normalized_score` are `None` exactly when `availability` is
    `UNAVAILABLE`: an unread category produces no alignment at all rather than a
    neutral one, so the caller has to make the fallback decision explicitly.
    """

    availability: FineDomainAvailability
    reason: FineDomainReason
    status: AlignmentStatus | None
    normalized_score: float | None
    fine_primary_category: FineCategory | None
    bridged_domain: Domain | None
    preferred_rank: int | None
    mapped_preference_count: int
    unmapped_preference_count: int
    fine_domain_bridge_version: str = FINE_DOMAIN_BRIDGE_VERSION


def preferred_rank_score(rank: int | None, preference_count: int) -> float:
    """Score a matched preference by its declared rank, best rank scoring 1.0.

    This is deliberately the same deterministic rule the Phase 4 engine applies
    to a coarse domain MATCH — `(count - rank + 1) / count` — so that switching
    the domain component from the coarse to the fine reading changes *which*
    domain was compared and never *how* a preference rank is worth. A unit test
    pins the two against each other.
    """
    if preference_count <= 0 or rank is None or not 1 <= rank <= preference_count:
        raise RecommendationInputError(
            "a domain MATCH requires a valid rank within the declared preferences"
        )
    return round((preference_count - rank + 1) / preference_count, 12)


#: The reasons `role-domain-preferences-v2` can return once a domain and a set of
#: preferences are both present. Anything else means its contract moved and this
#: module must be revisited rather than guess.
_DOMAIN_REASONS = {
    AlignmentReason.PROFILE_PREFERENCES_ABSENT: (
        FineDomainReason.PROFILE_PREFERENCES_ABSENT
    ),
    AlignmentReason.DOMAIN_PREFERENCES_EMPTY: FineDomainReason.DOMAIN_PREFERENCES_EMPTY,
    AlignmentReason.DOMAIN_PREFERENCE_MATCHED: (
        FineDomainReason.DOMAIN_PREFERENCE_MATCHED
    ),
    AlignmentReason.DOMAIN_PREFERENCE_NOT_LISTED: (
        FineDomainReason.DOMAIN_PREFERENCE_NOT_LISTED
    ),
    AlignmentReason.DOMAIN_PREFERENCES_PARTIALLY_UNMAPPED: (
        FineDomainReason.DOMAIN_PREFERENCES_PARTIALLY_UNMAPPED
    ),
}


def _unavailable(reason: FineDomainReason, category: FineCategory | None) -> FineDomainFit:
    return FineDomainFit(
        availability=FineDomainAvailability.UNAVAILABLE,
        reason=reason,
        status=None,
        normalized_score=None,
        fine_primary_category=category,
        bridged_domain=None,
        preferred_rank=None,
        mapped_preference_count=0,
        unmapped_preference_count=0,
    )


def build_fine_domain_fit(
    fine: RecommendationFineClassification,
    preferences: MatchingPreferences | None,
) -> FineDomainFit:
    """Read one persisted fine category against one set of declared preferences.

    Pure, and it classifies nothing: `fine` is what Phase 8 wrote, read as
    written. The alignment is produced by Phase 4's own signal builder over a
    value-only input whose primary domain is the bridged family — the profile
    side is the real preferences, and the fields that builder reads for its other
    two signals are left empty because only its domain answer is used here.
    """
    if fine.classifier_version is None:
        # State A. The row predates fine classification; nothing was decided, so
        # the coarse component keeps answering. This is not an inconsistency.
        if fine.primary_category is not None:
            raise RecommendationInputError(
                "a fine primary category cannot exist without a classifier version"
            )
        return _unavailable(FineDomainReason.FINE_CLASSIFICATION_ABSENT, None)
    if fine.primary_category is None:
        # State B. The classifier ran and named no sub-domain, which is what it
        # does for an unqualified opportunity. Never read as OTHER.
        return _unavailable(FineDomainReason.FINE_CATEGORY_ABSENT, None)
    domain = FINE_DOMAIN_BRIDGE.get(fine.primary_category)
    if domain is None:
        return _unavailable(
            FineDomainReason.FINE_CATEGORY_NOT_BRIDGED, fine.primary_category
        )

    probe = MatchingInput(
        profile=MatchingProfileInput(profile_id=0, preferences=preferences),
        opportunity=MatchingOpportunityInput(
            opportunity_id=0,
            canonical_title="",
            description=None,
            remote_type=None,
            qualification=MatchingQualification(
                qualification=Qualification.CORE_TARGET.value,
                primary_domain=domain.value,
                opportunity_type=OpportunityType.UNKNOWN.value,
                classifier_version=fine.classifier_version,
            ),
        ),
    )
    alignment = build_role_domain_preference_signals(probe).domain
    reason = _DOMAIN_REASONS.get(alignment.reason)
    if reason is None:
        raise RecommendationInputError(
            f"unsupported domain alignment reason: {alignment.reason.value}"
        )
    if alignment.status is AlignmentStatus.MATCH:
        count = 0 if preferences is None else len(preferences.preferred_domains)
        score: float | None = preferred_rank_score(alignment.preferred_rank, count)
    elif alignment.status is AlignmentStatus.MISMATCH:
        score = 0.0
    else:
        score = None
    return FineDomainFit(
        availability=FineDomainAvailability.AVAILABLE,
        reason=reason,
        status=alignment.status,
        normalized_score=score,
        fine_primary_category=fine.primary_category,
        bridged_domain=domain,
        preferred_rank=alignment.preferred_rank,
        mapped_preference_count=alignment.mapped_preference_count,
        unmapped_preference_count=alignment.unmapped_preference_count,
    )
