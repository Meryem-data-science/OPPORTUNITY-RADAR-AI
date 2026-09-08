"""Immutable vocabulary of Phase 9A: how well one opportunity fits one profile.

Phase 8 answers *what is this opportunity*. Phase 9 answers *how well does this
opportunity fit this profile*, and nothing here re-opens the first question: the
coarse qualification and the fine category are read exactly as Phase 4 and
Phase 8 persisted them, and no classifier runs in this package.

The recommendation is an **additive** layer over the Phase 4 matching snapshot.
It keeps that engine's three weights, its renormalization, and its separation of
a score from the evidence that produced it; it changes exactly one thing, the
source of the single `domain` component, and it adds four non-numeric signals
that route an opportunity rather than score it.

    recommendation_score              [0, 1] or None, weighted over available
                                      components only
    recommendation_evidence_coverage  [0, 1], the weight that was available
    disposition                       RECOMMENDED / UNCERTAIN /
                                      OUTSIDE_PREFERENCES / KNOWN_BLOCKER

`UNKNOWN` is never a refusal on either side. A required skill the Digital Twin
does not confirm is not a skill the person lacks; a work mode nobody could read
is not a work mode that was refused; a location nobody could place is not a
location outside the target. Every such case reaches the output as an *unknown*,
never as a confirmed gap, and the disposition rules below say so explicitly.

Nothing here carries a timestamp. The engine is a pure function of persisted
content plus the versions below, so the same inputs fingerprint identically
tomorrow.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from services.collector.matching import (
    DOMAIN_WEIGHT,
    REQUIRED_SKILL_WEIGHT,
    SEMANTIC_WEIGHT,
    AlignmentReason,
    AlignmentStatus,
    MatchLane,
    RoleDomainPreferencesResult,
    SemanticSimilarityStatus,
)
from services.collector.matching.models import MatchingPreferences
from services.collector.qualification.fine_taxonomy import FineCategory
from services.eligibility import GlobalStatus
from services.geography.models import LocationResolution, ProfileTarget

#: The assembly of the rules in `engine.py`: the components, the disposition
#: precedence and the ranking. Moving any of them moves this version.
RECOMMENDATION_ENGINE_VERSION = "recommendation-engine-v1"
#: The policy the engine applies: which signal contradicts a preference, which
#: one is merely unknown, and which reason codes a result carries.
RECOMMENDATION_RULES_VERSION = "recommendation-rules-v1"

__all__ = [
    "CONFIRMED_GAP_CODES",
    "DISPOSITION_PRECEDENCE",
    "DISPOSITION_RANK",
    "DOMAIN_WEIGHT",
    "REQUIRED_SKILL_WEIGHT",
    "RECOMMENDATION_ENGINE_VERSION",
    "RECOMMENDATION_RULES_VERSION",
    "SEMANTIC_WEIGHT",
    "STRENGTH_CODES",
    "UNKNOWN_CODES",
    "ComponentStatus",
    "DomainComponent",
    "DomainFitSource",
    "EligibilitySignal",
    "EligibilitySignalStatus",
    "GeographySignal",
    "GeographyState",
    "OpportunityTypeSignal",
    "RecommendationAssessment",
    "RecommendationBatchResult",
    "RecommendationDisposition",
    "RecommendationEligibilityInput",
    "RecommendationFineClassification",
    "RecommendationGeographyInput",
    "RecommendationInput",
    "RecommendationInputError",
    "RecommendationMatchingSnapshot",
    "RecommendationReasonCode",
    "RequiredSkillComponent",
    "SemanticComponent",
    "WorkModeSignal",
]


class RecommendationInputError(ValueError):
    """Raised when upstream snapshots cannot be recommended over safely."""


class RecommendationDisposition(StrEnum):
    """Where an opportunity is routed, independently of how it scored.

    A high score does not move an opportunity out of `KNOWN_BLOCKER`, and a low
    one does not keep it out of `RECOMMENDED`: the score answers *how well does
    this fit*, the disposition answers *is there anything known standing in the
    way*. They are deliberately separate axes.
    """

    RECOMMENDED = "RECOMMENDED"
    UNCERTAIN = "UNCERTAIN"
    OUTSIDE_PREFERENCES = "OUTSIDE_PREFERENCES"
    KNOWN_BLOCKER = "KNOWN_BLOCKER"


#: Precedence, worst first. A blocker outranks a contradicted preference, which
#: outranks an unknown, which outranks a clean result.
DISPOSITION_PRECEDENCE = (
    RecommendationDisposition.KNOWN_BLOCKER,
    RecommendationDisposition.OUTSIDE_PREFERENCES,
    RecommendationDisposition.UNCERTAIN,
    RecommendationDisposition.RECOMMENDED,
)

#: Ranking order, best first — the reverse of the precedence above.
DISPOSITION_RANK = {
    disposition: index
    for index, disposition in enumerate(reversed(DISPOSITION_PRECEDENCE))
}


class ComponentStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    MISSING = "MISSING"


class DomainFitSource(StrEnum):
    """Which of the two persisted classifications the domain component used.

    Exactly one of them contributes, ever. `FINE` means the Phase 8 category was
    present and could be bridged honestly to a domain family the preference
    vocabulary already knows; `COARSE` is the Phase 4 domain component read
    straight out of the matching snapshot, used whenever the fine half cannot be
    read that way.
    """

    FINE = "FINE"
    COARSE = "COARSE"


class GeographyState(StrEnum):
    """A Phase 7 verdict, plus the one state a verdict cannot express.

    `NOT_APPLICABLE` is an OPEN mobility: the person stated no geographic
    restriction, so there is nothing for a posting to contradict and nothing
    unknown about it either. Reading OPEN as `UNKNOWN` would put an uncertainty
    on the record where the person gave a clear answer.
    """

    MATCH = "MATCH"
    OUT_OF_TARGET = "OUT_OF_TARGET"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EligibilitySignalStatus(StrEnum):
    """The three Phase 3.6 verdicts, plus "no decision was ever recorded".

    `MISSING` is kept apart from `UNKNOWN` because they are different facts —
    the engine never ran for this pair, versus it ran and found a question it
    could not answer — and folding either into `INELIGIBLE` would invent a
    blocker out of an absence.
    """

    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    UNKNOWN = "UNKNOWN"
    MISSING = "MISSING"


class RecommendationReasonCode(StrEnum):
    """Stable, machine-readable explanations, partitioned by the engine.

    Every code lands in exactly one of `strengths`, `confirmed_gaps` and
    `unknowns`, and the partition is part of the rules version. No sentence is
    generated anywhere in this package.
    """

    # strengths — positively established fits
    REQUIRED_SKILLS_ALL_CONFIRMED = "REQUIRED_SKILLS_ALL_CONFIRMED"
    FINE_DOMAIN_PREFERRED = "FINE_DOMAIN_PREFERRED"
    COARSE_DOMAIN_PREFERRED = "COARSE_DOMAIN_PREFERRED"
    OPPORTUNITY_TYPE_ALLOWED = "OPPORTUNITY_TYPE_ALLOWED"
    WORK_MODE_ALLOWED = "WORK_MODE_ALLOWED"
    GEOGRAPHY_MATCHED = "GEOGRAPHY_MATCHED"
    ELIGIBILITY_NO_KNOWN_BLOCKER = "ELIGIBILITY_NO_KNOWN_BLOCKER"

    # confirmed gaps — a preference or a rule was positively contradicted
    OPPORTUNITY_TYPE_OUTSIDE_PREFERENCES = "OPPORTUNITY_TYPE_OUTSIDE_PREFERENCES"
    WORK_MODE_OUTSIDE_PREFERENCES = "WORK_MODE_OUTSIDE_PREFERENCES"
    GEOGRAPHY_OUT_OF_TARGET = "GEOGRAPHY_OUT_OF_TARGET"
    DOMAIN_OUTSIDE_PREFERENCES = "DOMAIN_OUTSIDE_PREFERENCES"
    ELIGIBILITY_KNOWN_BLOCKER = "ELIGIBILITY_KNOWN_BLOCKER"

    # unknowns — questions, never soft refusals
    REQUIRED_SKILLS_NOT_ALL_CONFIRMED = "REQUIRED_SKILLS_NOT_ALL_CONFIRMED"
    REQUIRED_SKILLS_UNAVAILABLE = "REQUIRED_SKILLS_UNAVAILABLE"
    SEMANTIC_EVIDENCE_UNAVAILABLE = "SEMANTIC_EVIDENCE_UNAVAILABLE"
    DOMAIN_FIT_UNKNOWN = "DOMAIN_FIT_UNKNOWN"
    FINE_DOMAIN_UNAVAILABLE_USING_COARSE_FALLBACK = (
        "FINE_DOMAIN_UNAVAILABLE_USING_COARSE_FALLBACK"
    )
    OPPORTUNITY_TYPE_UNKNOWN = "OPPORTUNITY_TYPE_UNKNOWN"
    WORK_MODE_UNKNOWN = "WORK_MODE_UNKNOWN"
    GEOGRAPHY_UNKNOWN = "GEOGRAPHY_UNKNOWN"
    ELIGIBILITY_UNKNOWN = "ELIGIBILITY_UNKNOWN"
    ELIGIBILITY_SNAPSHOT_MISSING = "ELIGIBILITY_SNAPSHOT_MISSING"
    NO_NUMERIC_EVIDENCE = "NO_NUMERIC_EVIDENCE"


#: The partition itself, stated once so the engine cannot drift from it and a
#: test can assert that every code is classified exactly once.
STRENGTH_CODES = frozenset(
    {
        RecommendationReasonCode.REQUIRED_SKILLS_ALL_CONFIRMED,
        RecommendationReasonCode.FINE_DOMAIN_PREFERRED,
        RecommendationReasonCode.COARSE_DOMAIN_PREFERRED,
        RecommendationReasonCode.OPPORTUNITY_TYPE_ALLOWED,
        RecommendationReasonCode.WORK_MODE_ALLOWED,
        RecommendationReasonCode.GEOGRAPHY_MATCHED,
        RecommendationReasonCode.ELIGIBILITY_NO_KNOWN_BLOCKER,
    }
)
CONFIRMED_GAP_CODES = frozenset(
    {
        RecommendationReasonCode.OPPORTUNITY_TYPE_OUTSIDE_PREFERENCES,
        RecommendationReasonCode.WORK_MODE_OUTSIDE_PREFERENCES,
        RecommendationReasonCode.GEOGRAPHY_OUT_OF_TARGET,
        RecommendationReasonCode.DOMAIN_OUTSIDE_PREFERENCES,
        RecommendationReasonCode.ELIGIBILITY_KNOWN_BLOCKER,
    }
)
UNKNOWN_CODES = frozenset(RecommendationReasonCode) - STRENGTH_CODES - CONFIRMED_GAP_CODES


@dataclass(frozen=True)
class RecommendationMatchingSnapshot:
    """The Phase 4 assessment this recommendation is built on, as persisted.

    Every numeric field below was computed by `matching-engine-v1` and written by
    `matching-persistence-v1`; nothing in Phase 9 recomputes TF-IDF, skill fit or
    the coarse domain alignment. `structured_upstream_fingerprint` is the
    persisted `domain.upstream_fingerprint`, which is the fingerprint of the
    whole role/domain/preferences result the snapshot was derived from — the
    engine uses it to refuse a recomputed alignment that no longer agrees with
    the snapshot instead of silently recommending over stale evidence.
    """

    profile_id: int
    opportunity_id: int
    lane: MatchLane
    match_quality: float | None
    evidence_coverage: float
    assessment_fingerprint: str
    required_skill_score: float | None
    required_skill_matched_count: int
    required_skill_total_count: int
    semantic_status: SemanticSimilarityStatus
    semantic_percentile: float | None
    domain_status: AlignmentStatus
    domain_reason: AlignmentReason
    domain_preferred_rank: int | None
    domain_normalized_score: float | None
    opportunity_type_status: AlignmentStatus
    opportunity_type_reason: AlignmentReason
    structured_upstream_fingerprint: str
    matching_engine_version: str
    matching_rules_version: str
    semantic_percentile_version: str


@dataclass(frozen=True)
class RecommendationFineClassification:
    """The two persisted Phase 8 values Phase 9 is allowed to read.

    `classifier_version is None` is the legacy state: migration `0025` reached
    the row and the fine classifier never did. `primary_category is None` with a
    version present is the classifier's deliberate refusal to name a sub-domain.
    `OTHER` is a third, distinct value and is never either of those.
    """

    primary_category: FineCategory | None
    classifier_version: str | None


@dataclass(frozen=True)
class RecommendationGeographyInput:
    """The Phase 7 halves, joined only inside the engine.

    `profile_target` is derived on demand from the person's declared mobility
    and is stored nowhere; `resolutions` are the persisted readings of the
    posting's own location strings. The verdict between them is produced by
    Phase 7's own `evaluate_target`, never by a rule of this package.
    """

    profile_target: ProfileTarget
    resolutions: tuple[LocationResolution, ...]


@dataclass(frozen=True)
class RecommendationEligibilityInput:
    """The stored Phase 3.6 decision for this pair, or its absence.

    `status is None` means no decision row exists. It is carried as an absence
    all the way to `EligibilitySignalStatus.MISSING`; it never becomes
    `INELIGIBLE` and never becomes `ELIGIBLE`.
    """

    status: GlobalStatus | None
    input_fingerprint: str | None
    engine_version: str | None


@dataclass(frozen=True)
class RecommendationInput:
    """Everything one assessment reads, already loaded and already validated.

    `structured` is the Phase 4 role/domain/preferences result recomputed from
    the same persisted rows the snapshot was built from. It exists for exactly
    one reason: the work-mode alignment `role-domain-preferences-v2` computes is
    not part of the persisted matching assessment, and Phase 9 reuses that logic
    rather than writing a second parser of `remote` / `hybrid` / `on-site`. Its
    fingerprint must equal the snapshot's, which is what makes reusing it safe.
    """

    matching: RecommendationMatchingSnapshot
    preferences: MatchingPreferences | None
    structured: RoleDomainPreferencesResult
    fine: RecommendationFineClassification
    geography: RecommendationGeographyInput
    eligibility: RecommendationEligibilityInput


@dataclass(frozen=True)
class RequiredSkillComponent:
    """The Phase 4 required-skill ratio, reused verbatim.

    `matched_count < total_count` states that the Digital Twin does not *confirm*
    the remaining requirements. It never states that the person lacks them, and
    the reason code it produces is an unknown, not a gap.
    """

    status: ComponentStatus
    score: float | None
    base_weight: float
    matched_count: int
    total_count: int


@dataclass(frozen=True)
class SemanticComponent:
    """The Phase 4 cohort percentile, reused verbatim. No corpus is refitted."""

    status: ComponentStatus
    score: float | None
    base_weight: float
    semantic_status: SemanticSimilarityStatus
    semantic_percentile_version: str


@dataclass(frozen=True)
class DomainComponent:
    """The single domain component, and which classification produced it.

    There is exactly one of these, carrying exactly `DOMAIN_WEIGHT`. The fine and
    the coarse readings are alternatives, never addends: `source` names the one
    that was used and the other contributes nothing at all.
    """

    status: ComponentStatus
    score: float | None
    base_weight: float
    source: DomainFitSource
    fit_status: AlignmentStatus
    fit_reason: str
    preferred_rank: int | None
    fine_primary_category: FineCategory | None
    fine_classifier_version: str | None
    fine_domain_bridge_version: str | None


@dataclass(frozen=True)
class OpportunityTypeSignal:
    """A routing signal read from the matching snapshot. It carries no weight."""

    status: AlignmentStatus
    reason: AlignmentReason


@dataclass(frozen=True)
class WorkModeSignal:
    """A routing signal, computed by `role-domain-preferences-v2`. No weight."""

    status: AlignmentStatus
    reason: AlignmentReason
    opportunity_mode: str | None


@dataclass(frozen=True)
class GeographySignal:
    """A routing signal, decided by Phase 7's evaluator. It carries no weight.

    Both rule ids are kept: the one that decided what the profile is targeting,
    and the one that decided the posting against it. `verdict_rule_id` is `None`
    when no comparison was made at all, which is the OPEN case.
    """

    state: GeographyState
    target_country: str | None
    mobility_rule_id: str
    verdict_rule_id: str | None
    segments: int
    resolved_segments: int
    matching_segments: int


@dataclass(frozen=True)
class EligibilitySignal:
    """A routing signal read from Phase 3.6. It carries no weight either."""

    status: EligibilitySignalStatus
    upstream_fingerprint: str | None
    engine_version: str | None


@dataclass(frozen=True)
class RecommendationAssessment:
    """One profile against one opportunity: a score, a route, and the reasons."""

    profile_id: int
    opportunity_id: int
    recommendation_score: float | None
    recommendation_evidence_coverage: float
    disposition: RecommendationDisposition
    required_skill: RequiredSkillComponent
    semantic: SemanticComponent
    domain: DomainComponent
    opportunity_type: OpportunityTypeSignal
    work_mode: WorkModeSignal
    geography: GeographySignal
    eligibility: EligibilitySignal
    baseline_match_quality: float | None
    baseline_evidence_coverage: float
    baseline_matching_lane: MatchLane
    baseline_matching_assessment_fingerprint: str
    strengths: tuple[RecommendationReasonCode, ...]
    confirmed_gaps: tuple[RecommendationReasonCode, ...]
    unknowns: tuple[RecommendationReasonCode, ...]
    matching_engine_version: str
    matching_rules_version: str
    semantic_percentile_version: str
    role_domain_preferences_version: str
    geographic_resolver_version: str
    eligibility_engine_version: str | None
    recommendation_engine_version: str = RECOMMENDATION_ENGINE_VERSION
    recommendation_rules_version: str = RECOMMENDATION_RULES_VERSION
    assessment_fingerprint: str = ""


@dataclass(frozen=True)
class RecommendationBatchResult:
    """One profile's whole cohort, already ranked. The order is the product."""

    profile_id: int
    assessments: tuple[RecommendationAssessment, ...]
    assessment_count: int
    recommendation_engine_version: str = RECOMMENDATION_ENGINE_VERSION
    recommendation_rules_version: str = RECOMMENDATION_RULES_VERSION
    batch_fingerprint: str = ""
