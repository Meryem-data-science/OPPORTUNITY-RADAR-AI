"""Value builders shared by the Phase 9A unit tests.

Everything here is built from the **real** upstream types and the **real**
upstream pure functions — Phase 4's signal builder, Phase 7's resolver and
target resolver — rather than from hand-written look-alikes, so a test can never
pass against a parallel geography or a parallel preference bridge that the
production code does not use.
"""

from __future__ import annotations

from services.collector.matching import (
    MATCHING_ENGINE_VERSION,
    MATCHING_RULES_VERSION,
    SEMANTIC_PERCENTILE_VERSION,
    AlignmentStatus,
    MatchLane,
    RoleDomainPreferencesResult,
    SemanticSimilarityStatus,
    build_role_domain_preference_signals,
)
from services.collector.matching.models import (
    MatchingInput,
    MatchingOpportunityInput,
    MatchingPreferences,
    MatchingProfileInput,
    MatchingQualification,
)
from services.collector.matching.role_domain_preferences_fingerprint import (
    role_domain_preferences_fingerprint,
)
from services.collector.qualification.fine_taxonomy import FineCategory
from services.collector.qualification.taxonomy import Domain, OpportunityType, Qualification
from services.digital_twin.preferences.models import MobilityScope
from services.eligibility import ELIGIBILITY_ENGINE_VERSION, GlobalStatus
from services.geography.models import RESOLVER_VERSION, LocationResolution
from services.geography.profile_target import resolve_declared_target
from services.geography.resolver import resolve_location_text
from services.recommendation import (
    RecommendationEligibilityInput,
    RecommendationFineClassification,
    RecommendationGeographyInput,
    RecommendationInput,
    RecommendationMatchingSnapshot,
    preferred_rank_score,
)

PROFILE_ID = 11
OPPORTUNITY_ID = 7
CLASSIFIER_VERSION = "qualification-rules-v2"
FINE_CLASSIFIER_VERSION = "fine-data-ai-rules-v2"
#: Four recognised labels, so a rank is meaningful and the arithmetic is exact.
PREFERRED_DOMAINS = ("Data Science", "GenAI/LLM", "MLOps/ML Platform", "BI/Analytics")

#: Explicit "use the default", so that passing `None` means "the profile stated
#: no preferences at all" rather than "I did not override this argument".
DEFAULT = object()


def preferences(
    opportunity_types=("INTERNSHIP",),
    work_modes=("REMOTE",),
    preferred_domains=PREFERRED_DOMAINS,
) -> MatchingPreferences:
    return MatchingPreferences(
        opportunity_types, work_modes, preferred_domains, "preferences-v1"
    )


def profile(prefs=None) -> MatchingProfileInput:
    return MatchingProfileInput(PROFILE_ID, preferences=prefs)


def opportunity(
    opportunity_id=OPPORTUNITY_ID,
    domain=Domain.DATA_SCIENCE,
    opportunity_type=OpportunityType.INTERNSHIP,
    remote_type="remote",
    qualification=Qualification.CORE_TARGET,
) -> MatchingOpportunityInput:
    return MatchingOpportunityInput(
        opportunity_id=opportunity_id,
        canonical_title="Data Scientist Internship",
        description="python sql machine learning",
        remote_type=remote_type,
        qualification=MatchingQualification(
            qualification=qualification.value,
            primary_domain=domain.value,
            opportunity_type=opportunity_type.value,
            classifier_version=CLASSIFIER_VERSION,
        ),
    )


def structured(prefs=None, opp=None) -> RoleDomainPreferencesResult:
    """The real Phase 4 alignment, which is what the snapshot below describes."""
    return build_role_domain_preference_signals(
        MatchingInput(profile(prefs), opp or opportunity())
    )


def coarse_domain_score(alignment, prefs) -> float | None:
    """What Matching v1 persisted for this alignment, restated for the fixture."""
    if alignment.status is AlignmentStatus.MISMATCH:
        return 0.0
    if alignment.status is AlignmentStatus.UNKNOWN:
        return None
    return preferred_rank_score(
        alignment.preferred_rank, len(prefs.preferred_domains)
    )


def snapshot(
    signals,
    prefs,
    *,
    opportunity_id=OPPORTUNITY_ID,
    required=0.8,
    required_matched=4,
    required_total=5,
    semantic=0.7,
    coarse_score="derive",
    match_quality=0.75,
    evidence_coverage=1.0,
    lane=MatchLane.PRIMARY,
    fingerprint="a" * 64,
) -> RecommendationMatchingSnapshot:
    """A persisted Phase 4 assessment, coherent with `signals` by construction."""
    if coarse_score == "derive":
        coarse_score = coarse_domain_score(signals.domain, prefs)
    return RecommendationMatchingSnapshot(
        profile_id=PROFILE_ID,
        opportunity_id=opportunity_id,
        lane=lane,
        match_quality=match_quality,
        evidence_coverage=evidence_coverage,
        assessment_fingerprint=fingerprint,
        required_skill_score=required,
        required_skill_matched_count=required_matched,
        required_skill_total_count=required_total,
        semantic_status=(
            SemanticSimilarityStatus.AVAILABLE
            if semantic is not None
            else SemanticSimilarityStatus.EMPTY_OPPORTUNITY_DOCUMENT
        ),
        semantic_percentile=semantic,
        domain_status=signals.domain.status,
        domain_reason=signals.domain.reason,
        domain_preferred_rank=signals.domain.preferred_rank,
        domain_normalized_score=coarse_score,
        opportunity_type_status=signals.opportunity_type.status,
        opportunity_type_reason=signals.opportunity_type.reason,
        structured_upstream_fingerprint=role_domain_preferences_fingerprint(signals),
        matching_engine_version=MATCHING_ENGINE_VERSION,
        matching_rules_version=MATCHING_RULES_VERSION,
        semantic_percentile_version=SEMANTIC_PERCENTILE_VERSION,
    )


def fine(category=FineCategory.DATA_SCIENCE, version=FINE_CLASSIFIER_VERSION):
    return RecommendationFineClassification(category, version)


LEGACY_FINE = RecommendationFineClassification(None, None)


def geography(
    *,
    scope=MobilityScope.RESTRICTED,
    declared=("Maroc",),
    locations=("Casablanca",),
    opportunity_id=OPPORTUNITY_ID,
) -> RecommendationGeographyInput:
    """Both Phase 7 halves, produced by Phase 7's own pure functions."""
    resolutions: list[LocationResolution] = []
    for source_id, text in enumerate(locations, 1):
        for position, segment in enumerate(resolve_location_text(text)):
            resolutions.append(
                LocationResolution(
                    opportunity_id=opportunity_id,
                    source_location_id=source_id,
                    segment_position=position,
                    raw_segment=segment.raw_segment,
                    status=segment.status,
                    rule_id=segment.rule_id,
                    country_code=segment.country_code,
                    city_key=segment.city_key,
                    resolver_version=RESOLVER_VERSION,
                    source_fingerprint="c" * 64,
                )
            )
    return RecommendationGeographyInput(
        profile_target=resolve_declared_target(PROFILE_ID, scope, declared),
        resolutions=tuple(resolutions),
    )


def eligibility(status=GlobalStatus.ELIGIBLE) -> RecommendationEligibilityInput:
    if status is None:
        return RecommendationEligibilityInput(None, None, None)
    return RecommendationEligibilityInput(
        status, "e" * 64, ELIGIBILITY_ENGINE_VERSION
    )


def recommendation_input(
    *,
    prefs=DEFAULT,
    opp=None,
    fine_classification=None,
    geo=None,
    eligible=GlobalStatus.ELIGIBLE,
    **snapshot_overrides,
) -> RecommendationInput:
    """One fully coherent input; every keyword reaches exactly one upstream."""
    prefs = preferences() if prefs is DEFAULT else prefs
    opp = opportunity() if opp is None else opp
    signals = structured(prefs, opp)
    return RecommendationInput(
        matching=snapshot(
            signals, prefs, opportunity_id=opp.opportunity_id, **snapshot_overrides
        ),
        preferences=prefs,
        structured=signals,
        fine=fine() if fine_classification is None else fine_classification,
        geography=(
            geography(opportunity_id=opp.opportunity_id) if geo is None else geo
        ),
        eligibility=eligibility(eligible),
    )
