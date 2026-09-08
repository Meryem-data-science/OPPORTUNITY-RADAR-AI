"""The pure Phase 9A rules: one score, one route, one deterministic ranking.

Three numeric components, and the weights are Matching v1's, imported rather
than restated:

    required skill fit  0.50   the Phase 4 ratio, read from the snapshot
    semantic fit        0.30   the Phase 4 cohort percentile, read as persisted
    domain fit          0.20   **one** component, fine or coarse, never both

    available_weight      = the weights of the components that are available
    recommendation_score  = weighted_sum / available_weight
    recommendation_evidence_coverage = available_weight

A component that is unavailable is left out of both sums; it never becomes a
zero. With nothing available the score is `None` and the coverage is `0.0`, and
the ranking below places those results deterministically rather than treating
them as the worst possible score.

**Anti double-counting.** The coarse domain and the fine domain answer the same
question — *is this the kind of work the person asked for* — so they are two
readings of one component, not two components. `build_domain_component` picks
exactly one: the fine reading when Phase 8 wrote a category this system can
bridge honestly, and otherwise the coarse component the matching snapshot
already carries. The one that is not picked contributes nothing, and the chosen
`DomainFitSource` is on the assessment and in its fingerprint.

**Four routing signals, none of which carries weight.** Opportunity type, work
mode, geography and eligibility decide the disposition and never touch the
score, so a Casablanca posting and an otherwise identical Paris one score the
same and are routed differently. Their precedence is:

    1. KNOWN_BLOCKER        an authoritative upstream says no: Eligibility
                            INELIGIBLE, and nothing else in this version
    2. OUTSIDE_PREFERENCES  a preference was positively contradicted
    3. UNCERTAIN            no contradiction, but a critical signal is unknown
    4. RECOMMENDED          nothing known stands in the way

A domain that is not among the preferred ones is **not** a routing blocker: it
is already priced into the domain component, and blocking on it as well would
count it twice.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Sequence

from services.collector.matching import (
    DOMAIN_WEIGHT,
    REQUIRED_SKILL_WEIGHT,
    SEMANTIC_WEIGHT,
    AlignmentStatus,
    SemanticSimilarityStatus,
)
from services.collector.matching.role_domain_preferences_fingerprint import (
    role_domain_preferences_fingerprint,
)
from services.collector.matching.skill_fit_fingerprint import skill_fit_fingerprint
from services.eligibility import GlobalStatus
from services.geography.evaluator import evaluate_target
from services.geography.models import (
    RESOLVER_VERSION,
    ResolutionStatus,
    TargetVerdict,
)
from services.geography.profile_target import MOBILITY_OPEN_RULE

from .fine_domain import (
    FineDomainAvailability,
    FineDomainFit,
    build_fine_domain_fit,
)
from .fingerprint import (
    recommendation_assessment_fingerprint,
    recommendation_batch_fingerprint,
)
from .models import (
    CONFIRMED_GAP_CODES,
    DISPOSITION_RANK,
    STRENGTH_CODES,
    ComponentStatus,
    DomainComponent,
    DomainFitSource,
    EligibilitySignal,
    EligibilitySignalStatus,
    GeographySignal,
    GeographyState,
    OpportunityTypeSignal,
    RecommendationAssessment,
    RecommendationBatchResult,
    RecommendationDisposition,
    RecommendationEligibilityEvidence,
    RecommendationGeographyInput,
    RecommendationInput,
    RecommendationInputError,
    RecommendationReasonCode,
    RecommendationSkillEvidence,
    RequiredSkillComponent,
    SemanticComponent,
    WorkModeSignal,
)

__all__ = [
    "build_domain_component",
    "build_eligibility_evidence",
    "build_geography_signal",
    "build_recommendation_assessment",
    "build_recommendation_batch",
    "build_skill_evidence",
    "eligibility_signal_status",
    "rank_recommendation_assessments",
    "recommendation_disposition",
]


def _stable_ratio(value: float, label: str) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise RecommendationInputError(f"{label} must be finite and within [0, 1]")
    return round(value, 12)


def _component_status(score: float | None) -> ComponentStatus:
    return ComponentStatus.MISSING if score is None else ComponentStatus.AVAILABLE


def build_domain_component(
    snapshot_status: AlignmentStatus,
    snapshot_reason: str,
    snapshot_rank: int | None,
    snapshot_score: float | None,
    fine: FineDomainFit,
    fine_classifier_version: str | None,
) -> DomainComponent:
    """Return the one domain component, from the fine half or the coarse half.

    The choice is total and exclusive. When the fine reading is `AVAILABLE` it
    supplies the status, the rank and the score, and the coarse values are
    ignored entirely; when it is not, the coarse component of the matching
    snapshot is used exactly as persisted — Phase 9 does not recompute it.
    """
    if fine.availability is FineDomainAvailability.AVAILABLE:
        if fine.status is None:
            raise RecommendationInputError(
                "an available fine domain fit must carry an alignment status"
            )
        return DomainComponent(
            status=_component_status(fine.normalized_score),
            score=fine.normalized_score,
            base_weight=DOMAIN_WEIGHT,
            source=DomainFitSource.FINE,
            fit_status=fine.status,
            fit_reason=fine.reason.value,
            preferred_rank=fine.preferred_rank,
            fine_primary_category=fine.fine_primary_category,
            fine_classifier_version=fine_classifier_version,
            fine_domain_bridge_version=fine.fine_domain_bridge_version,
        )
    return DomainComponent(
        status=_component_status(snapshot_score),
        score=snapshot_score,
        base_weight=DOMAIN_WEIGHT,
        source=DomainFitSource.COARSE,
        fit_status=snapshot_status,
        fit_reason=snapshot_reason,
        preferred_rank=snapshot_rank,
        fine_primary_category=fine.fine_primary_category,
        fine_classifier_version=fine_classifier_version,
        fine_domain_bridge_version=None,
    )


def build_geography_signal(
    geography: RecommendationGeographyInput, opportunity_id: int
) -> GeographySignal:
    """Judge one posting with Phase 7's own evaluator, plus the OPEN case.

    An OPEN mobility is answered before the posting is looked at: the person
    stated no restriction, so there is no target to compare against and no
    uncertainty to report. Every other case — no mobility stated, a mobility
    that resolves to no single country, several countries — reaches
    `evaluate_target` with `None` and comes back UNKNOWN, which is the honest
    answer and never `OUT_OF_TARGET`.
    """
    target = geography.profile_target
    for resolution in geography.resolutions:
        if resolution.opportunity_id != opportunity_id:
            raise RecommendationInputError(
                "location resolutions belong to another opportunity"
            )
    countries = tuple(
        sorted(
            {
                item.country_code
                for item in geography.resolutions
                if item.status is ResolutionStatus.RESOLVED
                and item.country_code is not None
            }
        )
    )
    verdict = evaluate_target(target.country_code, geography.resolutions)
    if target.rule_id == MOBILITY_OPEN_RULE:
        # The counters are facts about the posting and stay; the verdict does
        # not, because no comparison was made — `verdict_rule_id` is None so a
        # reader cannot mistake this for a Phase 7 answer.
        return GeographySignal(
            state=GeographyState.NOT_APPLICABLE,
            target_country=None,
            mobility_rule_id=target.rule_id,
            verdict_rule_id=None,
            segments=verdict.segments,
            resolved_segments=verdict.resolved_segments,
            matching_segments=0,
            resolved_countries=countries,
        )
    state = {
        TargetVerdict.MATCH: GeographyState.MATCH,
        TargetVerdict.OUT_OF_TARGET: GeographyState.OUT_OF_TARGET,
        TargetVerdict.UNKNOWN: GeographyState.UNKNOWN,
    }[verdict.verdict]
    return GeographySignal(
        state=state,
        target_country=target.country_code,
        mobility_rule_id=target.rule_id,
        verdict_rule_id=verdict.rule_id,
        segments=verdict.segments,
        resolved_segments=verdict.resolved_segments,
        matching_segments=verdict.matching_segments,
        resolved_countries=countries,
    )


def build_skill_evidence(
    skill_fit,
) -> tuple[RecommendationSkillEvidence, ...]:
    """Project the Phase 4 per-skill evaluations into the explanation contract.

    Order is the upstream's, which is deterministic; `kind` and `sources` are
    the upstream enums. `matched` becomes `confirmed_in_profile`, renamed
    because the two words say different things to a reader: a skill that is not
    confirmed is a question about the profile, not a statement about the person.
    """
    return tuple(
        RecommendationSkillEvidence(
            canonical_key=item.canonical_key,
            canonical_name=item.canonical_name,
            kind=item.kind,
            sources=item.sources,
            confirmed_in_profile=item.matched,
            profile_normalizer_versions=item.profile_normalizer_versions,
        )
        for item in skill_fit.evaluations
    )


def build_eligibility_evidence(
    eligibility,
) -> tuple[RecommendationEligibilityEvidence, ...]:
    """Project the persisted Phase 3.6 rule results. Nothing is re-evaluated."""
    return tuple(
        RecommendationEligibilityEvidence.of(result) for result in eligibility.results
    )


def eligibility_signal_status(status: GlobalStatus | None) -> EligibilitySignalStatus:
    """Map the stored verdict, keeping "never decided" apart from "unknown"."""
    if status is None:
        return EligibilitySignalStatus.MISSING
    return {
        GlobalStatus.ELIGIBLE: EligibilitySignalStatus.ELIGIBLE,
        GlobalStatus.INELIGIBLE: EligibilitySignalStatus.INELIGIBLE,
        GlobalStatus.UNKNOWN: EligibilitySignalStatus.UNKNOWN,
    }[status]


def recommendation_disposition(
    opportunity_type: OpportunityTypeSignal,
    work_mode: WorkModeSignal,
    geography: GeographySignal,
    eligibility: EligibilitySignal,
) -> RecommendationDisposition:
    """Apply the four-way precedence over the routing signals only.

    The score is not consulted, and neither is the domain fit: a perfectly
    scored opportunity whose eligibility is INELIGIBLE is a `KNOWN_BLOCKER`, and
    one whose geography is `OUT_OF_TARGET` is `OUTSIDE_PREFERENCES`, however
    well it scored.
    """
    if eligibility.status is EligibilitySignalStatus.INELIGIBLE:
        return RecommendationDisposition.KNOWN_BLOCKER
    contradicted = (
        opportunity_type.status is AlignmentStatus.MISMATCH
        or work_mode.status is AlignmentStatus.MISMATCH
        or geography.state is GeographyState.OUT_OF_TARGET
    )
    if contradicted:
        return RecommendationDisposition.OUTSIDE_PREFERENCES
    uncertain = (
        opportunity_type.status is AlignmentStatus.UNKNOWN
        or work_mode.status is AlignmentStatus.UNKNOWN
        or geography.state is GeographyState.UNKNOWN
        or eligibility.status
        in (EligibilitySignalStatus.UNKNOWN, EligibilitySignalStatus.MISSING)
    )
    if uncertain:
        return RecommendationDisposition.UNCERTAIN
    return RecommendationDisposition.RECOMMENDED


def _skill_codes(component: RequiredSkillComponent) -> RecommendationReasonCode:
    if component.total_count == 0:
        return RecommendationReasonCode.REQUIRED_SKILLS_UNAVAILABLE
    if component.matched_count == component.total_count:
        return RecommendationReasonCode.REQUIRED_SKILLS_ALL_CONFIRMED
    # Absence of confirmation, never a statement that the person lacks a skill.
    return RecommendationReasonCode.REQUIRED_SKILLS_NOT_ALL_CONFIRMED


def _domain_codes(component: DomainComponent) -> list[RecommendationReasonCode]:
    codes: list[RecommendationReasonCode] = []
    if component.source is DomainFitSource.COARSE:
        codes.append(
            RecommendationReasonCode.FINE_DOMAIN_UNAVAILABLE_USING_COARSE_FALLBACK
        )
    if component.fit_status is AlignmentStatus.MATCH:
        codes.append(
            RecommendationReasonCode.FINE_DOMAIN_PREFERRED
            if component.source is DomainFitSource.FINE
            else RecommendationReasonCode.COARSE_DOMAIN_PREFERRED
        )
    elif component.fit_status is AlignmentStatus.MISMATCH:
        codes.append(RecommendationReasonCode.DOMAIN_OUTSIDE_PREFERENCES)
    else:
        codes.append(RecommendationReasonCode.DOMAIN_FIT_UNKNOWN)
    return codes


_TYPE_CODES = {
    AlignmentStatus.MATCH: RecommendationReasonCode.OPPORTUNITY_TYPE_ALLOWED,
    AlignmentStatus.MISMATCH: (
        RecommendationReasonCode.OPPORTUNITY_TYPE_OUTSIDE_PREFERENCES
    ),
    AlignmentStatus.UNKNOWN: RecommendationReasonCode.OPPORTUNITY_TYPE_UNKNOWN,
}
_WORK_MODE_CODES = {
    AlignmentStatus.MATCH: RecommendationReasonCode.WORK_MODE_ALLOWED,
    AlignmentStatus.MISMATCH: RecommendationReasonCode.WORK_MODE_OUTSIDE_PREFERENCES,
    AlignmentStatus.UNKNOWN: RecommendationReasonCode.WORK_MODE_UNKNOWN,
}
#: `NOT_APPLICABLE` is deliberately absent: an OPEN mobility is neither a
#: strength of the posting, a contradicted preference nor an open question, so
#: it produces no reason code. The `geography` signal states it in full.
_GEOGRAPHY_CODES = {
    GeographyState.MATCH: RecommendationReasonCode.GEOGRAPHY_MATCHED,
    GeographyState.OUT_OF_TARGET: RecommendationReasonCode.GEOGRAPHY_OUT_OF_TARGET,
    GeographyState.UNKNOWN: RecommendationReasonCode.GEOGRAPHY_UNKNOWN,
}
_ELIGIBILITY_CODES = {
    EligibilitySignalStatus.ELIGIBLE: (
        RecommendationReasonCode.ELIGIBILITY_NO_KNOWN_BLOCKER
    ),
    EligibilitySignalStatus.INELIGIBLE: (
        RecommendationReasonCode.ELIGIBILITY_KNOWN_BLOCKER
    ),
    EligibilitySignalStatus.UNKNOWN: RecommendationReasonCode.ELIGIBILITY_UNKNOWN,
    EligibilitySignalStatus.MISSING: (
        RecommendationReasonCode.ELIGIBILITY_SNAPSHOT_MISSING
    ),
}


def _validate(inputs: RecommendationInput) -> None:
    matching = inputs.matching
    expected = (matching.profile_id, matching.opportunity_id)
    structured = inputs.structured
    eligibility = inputs.eligibility
    if (structured.profile_id, structured.opportunity_id) != expected:
        raise RecommendationInputError("structured signals identity mismatch")
    if inputs.geography.profile_target.profile_id != matching.profile_id:
        raise RecommendationInputError("profile target identity mismatch")
    if not matching.assessment_fingerprint:
        raise RecommendationInputError("matching assessment fingerprint is required")
    versions = (
        matching.matching_engine_version,
        matching.matching_rules_version,
        matching.semantic_percentile_version,
        structured.role_domain_preferences_version,
    )
    if any(not version.strip() for version in versions):
        raise RecommendationInputError("upstream versions must be non-empty")
    # The work-mode alignment below is recomputed rather than persisted, so the
    # snapshot has to agree that it describes the same reading. A disagreement is
    # a stale snapshot, and repairing it silently is exactly what this phase must
    # not do.
    if (
        role_domain_preferences_fingerprint(structured)
        != matching.structured_upstream_fingerprint
    ):
        raise RecommendationInputError(
            "recomputed role/domain/preference signals disagree with the "
            "matching snapshot; synchronize Matching first"
        )
    skill_fit = inputs.skill_fit
    if (skill_fit.profile_id, skill_fit.opportunity_id) != expected:
        raise RecommendationInputError("skill fit identity mismatch")
    # The per-skill detail explains the persisted ratio, so it has to be the
    # detail of *that* ratio. The recomputed fit is used for the explanation
    # only; the number always comes from the snapshot.
    if skill_fit_fingerprint(skill_fit) != matching.required_skill_upstream_fingerprint:
        raise RecommendationInputError(
            "recomputed skill fit disagrees with the matching snapshot; "
            "synchronize Matching first"
        )
    if eligibility.results and eligibility.status is None:
        raise RecommendationInputError(
            "eligibility rule results without a stored decision"
        )


def build_recommendation_assessment(
    inputs: RecommendationInput,
) -> RecommendationAssessment:
    """Score, route and explain one pair, without recomputing any upstream."""
    _validate(inputs)
    matching = inputs.matching

    required_score = (
        None
        if matching.required_skill_score is None
        else _stable_ratio(matching.required_skill_score, "required skill score")
    )
    if (matching.semantic_status is SemanticSimilarityStatus.AVAILABLE) != (
        matching.semantic_percentile is not None
    ):
        raise RecommendationInputError(
            "semantic percentile must be present exactly when the status is AVAILABLE"
        )
    semantic_score = (
        None
        if matching.semantic_percentile is None
        else _stable_ratio(matching.semantic_percentile, "semantic percentile")
    )
    fine = build_fine_domain_fit(inputs.fine, inputs.preferences)
    domain = build_domain_component(
        matching.domain_status,
        matching.domain_reason.value,
        matching.domain_preferred_rank,
        matching.domain_normalized_score,
        fine,
        inputs.fine.classifier_version,
    )

    components = (
        (required_score, REQUIRED_SKILL_WEIGHT),
        (semantic_score, SEMANTIC_WEIGHT),
        (domain.score, DOMAIN_WEIGHT),
    )
    available_weight = _stable_ratio(
        sum(weight for score, weight in components if score is not None),
        "recommendation evidence coverage",
    )
    weighted_sum = sum(
        score * weight for score, weight in components if score is not None
    )
    score = (
        None
        if available_weight == 0
        else _stable_ratio(weighted_sum / available_weight, "recommendation score")
    )

    required_skill = RequiredSkillComponent(
        _component_status(required_score),
        required_score,
        REQUIRED_SKILL_WEIGHT,
        matching.required_skill_matched_count,
        matching.required_skill_total_count,
        matching.required_skill_upstream_fingerprint,
    )
    semantic = SemanticComponent(
        _component_status(semantic_score),
        semantic_score,
        SEMANTIC_WEIGHT,
        matching.semantic_status,
        matching.semantic_percentile_version,
    )
    opportunity_type = OpportunityTypeSignal(
        matching.opportunity_type_status, matching.opportunity_type_reason
    )
    work_mode = WorkModeSignal(
        inputs.structured.work_mode.status,
        inputs.structured.work_mode.reason,
        inputs.structured.work_mode.opportunity_mode,
    )
    geography = build_geography_signal(inputs.geography, matching.opportunity_id)
    eligibility = EligibilitySignal(
        eligibility_signal_status(inputs.eligibility.status),
        inputs.eligibility.input_fingerprint,
        inputs.eligibility.engine_version,
    )

    codes = [
        _skill_codes(required_skill),
        *_domain_codes(domain),
        _TYPE_CODES[opportunity_type.status],
        _WORK_MODE_CODES[work_mode.status],
        _ELIGIBILITY_CODES[eligibility.status],
    ]
    geography_code = _GEOGRAPHY_CODES.get(geography.state)
    if geography_code is not None:
        codes.append(geography_code)
    if semantic.status is ComponentStatus.MISSING:
        codes.append(RecommendationReasonCode.SEMANTIC_EVIDENCE_UNAVAILABLE)
    if inputs.declared_constraints:
        # Read and shown, never interpreted. No parser, no heuristic, no weight
        # and no routing: the person is told this engine did not check them.
        codes.append(
            RecommendationReasonCode.USER_CONSTRAINTS_NOT_AUTOMATICALLY_EVALUATED
        )
    if score is None:
        codes.append(RecommendationReasonCode.NO_NUMERIC_EVIDENCE)

    assessment = RecommendationAssessment(
        profile_id=matching.profile_id,
        opportunity_id=matching.opportunity_id,
        recommendation_score=score,
        recommendation_evidence_coverage=available_weight,
        disposition=recommendation_disposition(
            opportunity_type, work_mode, geography, eligibility
        ),
        required_skill=required_skill,
        semantic=semantic,
        domain=domain,
        opportunity_type=opportunity_type,
        work_mode=work_mode,
        geography=geography,
        eligibility=eligibility,
        baseline_match_quality=matching.match_quality,
        baseline_evidence_coverage=_stable_ratio(
            matching.evidence_coverage, "baseline evidence coverage"
        ),
        baseline_matching_lane=matching.lane,
        baseline_matching_assessment_fingerprint=matching.assessment_fingerprint,
        strengths=tuple(code for code in codes if code in STRENGTH_CODES),
        confirmed_gaps=tuple(code for code in codes if code in CONFIRMED_GAP_CODES),
        unknowns=tuple(
            code
            for code in codes
            if code not in STRENGTH_CODES and code not in CONFIRMED_GAP_CODES
        ),
        skill_evidence=build_skill_evidence(inputs.skill_fit),
        eligibility_evidence=build_eligibility_evidence(inputs.eligibility),
        declared_constraints=inputs.declared_constraints,
        matching_engine_version=matching.matching_engine_version,
        matching_rules_version=matching.matching_rules_version,
        semantic_percentile_version=matching.semantic_percentile_version,
        role_domain_preferences_version=(
            inputs.structured.role_domain_preferences_version
        ),
        geographic_resolver_version=RESOLVER_VERSION,
        eligibility_engine_version=inputs.eligibility.engine_version,
    )
    return replace(
        assessment,
        assessment_fingerprint=recommendation_assessment_fingerprint(assessment),
    )


def _ranking_key(assessment: RecommendationAssessment) -> tuple:
    """Disposition first, then score, then coverage, then the opportunity id.

    An assessment with no numeric evidence sorts **after** every scored one
    inside its own disposition — it is not the worst fit, it is the one nothing
    is known about, and placing it below a 0.0 keeps it from displacing results
    that were actually measured. The final key is the opportunity id, so the
    order is total and no tie is resolved by the input sequence.
    """
    score = assessment.recommendation_score
    return (
        DISPOSITION_RANK[assessment.disposition],
        score is None,
        -(0.0 if score is None else score),
        -assessment.recommendation_evidence_coverage,
        assessment.opportunity_id,
    )


def rank_recommendation_assessments(
    assessments: Sequence[RecommendationAssessment],
) -> tuple[RecommendationAssessment, ...]:
    """Return the ranking, which is a pure function of the assessments alone."""
    return tuple(sorted(assessments, key=_ranking_key))


def build_recommendation_batch(
    profile_id: int, inputs: Sequence[RecommendationInput]
) -> RecommendationBatchResult:
    """Assess a whole cohort and rank it. The input order is not significant."""
    seen: set[int] = set()
    for item in inputs:
        if item.matching.profile_id != profile_id:
            raise RecommendationInputError("input belongs to another profile")
        if item.matching.opportunity_id in seen:
            raise RecommendationInputError("duplicate opportunity_id")
        seen.add(item.matching.opportunity_id)
    assessments = rank_recommendation_assessments(
        [build_recommendation_assessment(item) for item in inputs]
    )
    result = RecommendationBatchResult(
        profile_id=profile_id,
        assessments=assessments,
        assessment_count=len(assessments),
    )
    return replace(result, batch_fingerprint=recommendation_batch_fingerprint(result))
