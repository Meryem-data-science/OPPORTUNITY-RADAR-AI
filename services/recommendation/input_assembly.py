"""Read-only assembly and full-cohort preflight for Phase 9A inputs.

Six persisted sources are read here and **nothing is written, classified,
geocoded or repaired**:

    1. the profile and the user that owns it        `profiles`
    2. the current Matching snapshot                Phase 4, `read_current_matching`
    3. the profile's declared preferences           Phase 3, via Matching's loader
    4. the persisted fine classification            Phase 8, decoded as written
    5. the persisted location resolutions           Phase 7, plus the profile target
    6. the stored eligibility decision              Phase 3.6

The shape follows `services/priority/input_assembly.py` — a readiness status, a
list of explicit issues, a stable order, and no partial output — because an
operator who understands that preflight should understand this one. Two of its
rules are deliberately different, and both come from Phase 9's own contract:

* **a missing eligibility decision is not an issue.** Priority refuses to
  prioritize a pair it has no decision for; recommendation carries the absence
  through as `EligibilitySignalStatus.MISSING`, which routes the opportunity to
  UNCERTAIN. Refusing the whole cohort because one posting was never evaluated
  would hide the other ninety-nine, and turning the absence into a verdict is
  exactly what Phase 3.6 forbids. A *stale* decision is still an issue: reading
  another engine's verdict as if it were this one's is not an absence;
* **a legacy fine classification is not an issue either.** A row migration
  `0025` reached and the fine classifier never did is a documented state, and it
  routes to the coarse domain component. What *is* refused is a row whose
  persisted fine half contradicts its coarse half — that is a real inconsistency
  and it must not be papered over with an invented category.

When any issue is found the result is `INCOMPLETE` and carries **no records at
all**: a half-assembled cohort would rank an opportunity against a corpus that
is missing its competitors.
"""

from __future__ import annotations

import math
import numbers
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from services.api.fine_classification import (
    FineClassificationDecodeError,
    decode_fine_classification,
)
from services.collector.matching import (
    MATCHING_ENGINE_VERSION,
    MATCHING_PERSISTENCE_VERSION,
    MATCHING_RULES_VERSION,
    MATCHING_SELECTION_VERSION,
    SEMANTIC_PERCENTILE_VERSION,
    AlignmentReason,
    AlignmentStatus,
    MatchingInput,
    MatchingInputError,
    MatchingReadError,
    MatchLane,
    SemanticSimilarityStatus,
    build_role_domain_preference_signals,
    load_opportunity_matching_input,
    load_profile_matching_input,
    read_current_matching,
    select_matching_opportunity_ids,
)
from services.collector.matching.role_domain_preferences import (
    RoleDomainPreferencesInputError,
)
from services.collector.matching.role_domain_preferences_fingerprint import (
    role_domain_preferences_fingerprint,
)
from services.eligibility import ELIGIBILITY_ENGINE_VERSION
from services.eligibility.repository import read_eligibility
from services.geography.profile_target import resolve_profile_target
from services.geography.repository import read_opportunity_resolutions

from .models import (
    RecommendationEligibilityInput,
    RecommendationFineClassification,
    RecommendationGeographyInput,
    RecommendationInput,
    RecommendationMatchingSnapshot,
)

RECOMMENDATION_INPUT_ASSEMBLY_VERSION = "recommendation-input-assembly-v1"

__all__ = [
    "RECOMMENDATION_INPUT_ASSEMBLY_VERSION",
    "RecommendationInputAssemblyResult",
    "RecommendationInputRecord",
    "RecommendationOpportunityContext",
    "RecommendationReadinessIssue",
    "RecommendationReadinessIssueCode",
    "RecommendationReadinessStatus",
    "assemble_recommendation_inputs",
]


class RecommendationReadinessStatus(StrEnum):
    READY = "READY"
    INCOMPLETE = "INCOMPLETE"


class RecommendationReadinessIssueCode(StrEnum):
    PROFILE_NOT_FOUND = "PROFILE_NOT_FOUND"
    PROFILE_OWNER_INVALID = "PROFILE_OWNER_INVALID"
    MATCHING_NOT_READY = "MATCHING_NOT_READY"
    MATCHING_VERSION_STALE = "MATCHING_VERSION_STALE"
    STALE_MATCHING_COHORT = "STALE_MATCHING_COHORT"
    STALE_MATCHING_SNAPSHOT = "STALE_MATCHING_SNAPSHOT"
    OPPORTUNITY_MISSING = "OPPORTUNITY_MISSING"
    INVALID_MATCHING_PAYLOAD = "INVALID_MATCHING_PAYLOAD"
    FINE_CLASSIFICATION_INVALID = "FINE_CLASSIFICATION_INVALID"
    ELIGIBILITY_VERSION_STALE = "ELIGIBILITY_VERSION_STALE"
    INVALID_UPSTREAM_VALUE = "INVALID_UPSTREAM_VALUE"


@dataclass(frozen=True)
class RecommendationReadinessIssue:
    code: RecommendationReadinessIssueCode
    message: str
    opportunity_id: int | None = None


@dataclass(frozen=True)
class RecommendationOpportunityContext:
    """Presentation-only identity, carried beside the input and never scored."""

    opportunity_id: int
    canonical_title: str
    organization: str
    source_url: str
    application_url: str | None
    canonical_url: str | None


@dataclass(frozen=True)
class RecommendationInputRecord:
    recommendation_input: RecommendationInput
    context: RecommendationOpportunityContext


@dataclass(frozen=True)
class RecommendationInputAssemblyResult:
    assembly_version: str
    status: RecommendationReadinessStatus
    profile_id: int
    user_id: int | None
    matching_run_id: int | None
    issues: tuple[RecommendationReadinessIssue, ...]
    records: tuple[RecommendationInputRecord, ...]


def _issue(code, message, opportunity_id=None) -> RecommendationReadinessIssue:
    return RecommendationReadinessIssue(code, message, opportunity_id)


def _incomplete(profile_id, issues, user_id=None, run_id=None):
    ordered = tuple(
        sorted(
            issues,
            key=lambda item: (
                item.opportunity_id is not None,
                item.opportunity_id or 0,
                item.code.value,
            ),
        )
    )
    return RecommendationInputAssemblyResult(
        RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
        RecommendationReadinessStatus.INCOMPLETE,
        profile_id,
        user_id,
        run_id,
        ordered,
        (),
    )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _ratio(value: object, *, optional: bool = True) -> float | None:
    """Accept a persisted `[0, 1]` number, or `None` when one is allowed."""
    if value is None:
        if not optional:
            raise ValueError("a required ratio is missing")
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, numbers.Real)
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        raise ValueError("value is not a ratio within [0, 1]")
    return float(value)


def _count(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("value is not a non-negative integer")
    return value


def _section(payload: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    section = payload.get(name)
    if not isinstance(section, Mapping):
        raise ValueError(f"persisted assessment has no {name} section")
    return section


def _snapshot(
    profile_id: int, assessment, run
) -> RecommendationMatchingSnapshot:
    """Read one persisted matching assessment payload, strictly, as written.

    Every value is validated against the contract `matching-engine-v1` wrote it
    under. Nothing is defaulted: a payload this cannot read is an issue for the
    caller to report, never a component quietly treated as unavailable.
    """
    payload = assessment.assessment_payload
    if not isinstance(payload, Mapping):
        raise ValueError("persisted assessment payload is not an object")
    required = _section(payload, "required_skill")
    semantic = _section(payload, "semantic")
    domain = _section(payload, "domain")
    opportunity_type = _section(payload, "opportunity_type")
    if not _is_sha256(assessment.assessment_fingerprint):
        raise ValueError("persisted assessment fingerprint is not a SHA-256 digest")
    if not _is_sha256(domain.get("upstream_fingerprint")):
        raise ValueError("persisted domain upstream fingerprint is not a SHA-256 digest")
    coverage = _ratio(assessment.evidence_coverage, optional=False)
    quality = _ratio(assessment.match_quality)
    if (coverage == 0.0) != (quality is None):
        raise ValueError("match quality and evidence coverage disagree")
    semantic_status = SemanticSimilarityStatus(semantic.get("status"))
    percentile = _ratio(semantic.get("percentile"))
    if (semantic_status is SemanticSimilarityStatus.AVAILABLE) != (
        percentile is not None
    ):
        raise ValueError("semantic percentile and status disagree")
    return RecommendationMatchingSnapshot(
        profile_id=profile_id,
        opportunity_id=assessment.opportunity_id,
        lane=MatchLane(assessment.lane),
        match_quality=quality,
        evidence_coverage=coverage,
        assessment_fingerprint=assessment.assessment_fingerprint,
        required_skill_score=_ratio(required.get("normalized_score")),
        required_skill_matched_count=_count(required.get("matched_count")),
        required_skill_total_count=_count(required.get("total_count")),
        semantic_status=semantic_status,
        semantic_percentile=percentile,
        domain_status=AlignmentStatus(domain.get("status")),
        domain_reason=AlignmentReason(domain.get("reason")),
        domain_preferred_rank=(
            None
            if domain.get("preferred_rank") is None
            else _count(domain.get("preferred_rank"))
        ),
        domain_normalized_score=_ratio(domain.get("normalized_score")),
        opportunity_type_status=AlignmentStatus(opportunity_type.get("status")),
        opportunity_type_reason=AlignmentReason(opportunity_type.get("reason")),
        structured_upstream_fingerprint=str(domain.get("upstream_fingerprint")),
        matching_engine_version=run.matching_engine_version,
        matching_rules_version=run.matching_rules_version,
        semantic_percentile_version=run.semantic_percentile_version,
    )


def _fine_classification(
    connection: sqlite3.Connection, opportunity_id: int
) -> RecommendationFineClassification:
    """Decode the persisted Phase 8 half with the API's own strict decoder.

    Only two of its five public values are read. The other three are decoded all
    the same, because the decoder's coherence rules — `OTHER` is never evidenced,
    an unqualified row carries no category, a qualified one carries exactly one —
    are what makes reading the two safe.
    """
    row = connection.execute(
        """SELECT q.qualification, q.fine_primary_category,
                  q.fine_secondary_categories_json, q.fine_category_evidence_json,
                  q.fine_reasons_json, q.fine_classifier_version
             FROM opportunities AS o
             LEFT JOIN opportunity_qualifications AS q ON q.opportunity_id = o.id
            WHERE o.id = ?""",
        (opportunity_id,),
    ).fetchone()
    if row is None:
        raise FineClassificationDecodeError(f"opportunity {opportunity_id} disappeared")
    decoded = decode_fine_classification(*row)
    return RecommendationFineClassification(
        primary_category=decoded.primary_category,
        classifier_version=decoded.classifier_version,
    )


def assemble_recommendation_inputs(
    connection: sqlite3.Connection, profile_id: int
) -> RecommendationInputAssemblyResult:
    """Read and validate the complete current Matching cohort without scoring it.

    Read-only from beginning to end: every statement below is a `SELECT`, and
    every upstream is consulted through the package that owns it.
    """
    owner = connection.execute(
        "SELECT user_id FROM profiles WHERE id = ?", (profile_id,)
    ).fetchone()
    if owner is None:
        return _incomplete(
            profile_id,
            [
                _issue(
                    RecommendationReadinessIssueCode.PROFILE_NOT_FOUND,
                    f"profile {profile_id} was not found",
                )
            ],
        )
    user_id = owner[0]
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        return _incomplete(
            profile_id,
            [
                _issue(
                    RecommendationReadinessIssueCode.PROFILE_OWNER_INVALID,
                    f"profile {profile_id} has an invalid owner",
                )
            ],
        )
    try:
        matching = read_current_matching(connection, profile_id)
    except MatchingReadError as error:
        return _incomplete(
            profile_id,
            [
                _issue(
                    RecommendationReadinessIssueCode.MATCHING_NOT_READY,
                    f"matching snapshot is not readable: {error}",
                )
            ],
            user_id,
        )
    run = matching.current_run
    if matching.status != "READY" or run is None:
        return _incomplete(
            profile_id,
            [
                _issue(
                    RecommendationReadinessIssueCode.MATCHING_NOT_READY,
                    f"matching status is {matching.status}; synchronize Matching first",
                )
            ],
            user_id,
        )
    expected_versions = (
        ("persistence_version", run.persistence_version, MATCHING_PERSISTENCE_VERSION),
        ("selection_version", run.selection_version, MATCHING_SELECTION_VERSION),
        (
            "matching_engine_version",
            run.matching_engine_version,
            MATCHING_ENGINE_VERSION,
        ),
        ("matching_rules_version", run.matching_rules_version, MATCHING_RULES_VERSION),
        (
            "semantic_percentile_version",
            run.semantic_percentile_version,
            SEMANTIC_PERCENTILE_VERSION,
        ),
    )
    stale = [name for name, actual, expected in expected_versions if actual != expected]
    if stale:
        return _incomplete(
            profile_id,
            [
                _issue(
                    RecommendationReadinessIssueCode.MATCHING_VERSION_STALE,
                    "matching run has unsupported versions: " + ", ".join(stale),
                )
            ],
            user_id,
            run.run_id,
        )
    persisted_ids = tuple(item.opportunity_id for item in run.assessments)
    if persisted_ids != select_matching_opportunity_ids(connection):
        return _incomplete(
            profile_id,
            [
                _issue(
                    RecommendationReadinessIssueCode.STALE_MATCHING_COHORT,
                    "matching cohort is stale; synchronize Matching first",
                )
            ],
            user_id,
            run.run_id,
        )
    try:
        profile_input = load_profile_matching_input(connection, profile_id)
    except MatchingInputError as error:
        return _incomplete(
            profile_id,
            [
                _issue(
                    RecommendationReadinessIssueCode.INVALID_UPSTREAM_VALUE,
                    f"profile projections are not readable: {error}",
                )
            ],
            user_id,
            run.run_id,
        )
    profile_target = resolve_profile_target(connection, profile_id)

    issues: list[RecommendationReadinessIssue] = []
    records: list[RecommendationInputRecord] = []
    for assessment in run.assessments:
        opportunity_id = assessment.opportunity_id
        row = connection.execute(
            """SELECT canonical_title, organization, source_url, application_url,
                      canonical_url FROM opportunities WHERE id = ?""",
            (opportunity_id,),
        ).fetchone()
        if row is None:
            issues.append(
                _issue(
                    RecommendationReadinessIssueCode.OPPORTUNITY_MISSING,
                    f"opportunity {opportunity_id} was not found",
                    opportunity_id,
                )
            )
            continue
        try:
            snapshot = _snapshot(profile_id, assessment, run)
        except (ValueError, TypeError) as error:
            issues.append(
                _issue(
                    RecommendationReadinessIssueCode.INVALID_MATCHING_PAYLOAD,
                    f"opportunity {opportunity_id} has an unreadable matching "
                    f"assessment: {error}",
                    opportunity_id,
                )
            )
            continue
        try:
            opportunity_input = load_opportunity_matching_input(
                connection, opportunity_id
            )
            structured = build_role_domain_preference_signals(
                MatchingInput(profile_input, opportunity_input)
            )
        except (MatchingInputError, RoleDomainPreferencesInputError) as error:
            issues.append(
                _issue(
                    RecommendationReadinessIssueCode.INVALID_UPSTREAM_VALUE,
                    f"opportunity {opportunity_id} has unreadable structured "
                    f"signals: {error}",
                    opportunity_id,
                )
            )
            continue
        # The recomputed alignment is only usable while it still describes the
        # snapshot it will be mixed with. A disagreement means the persisted
        # matching predates a change to the qualification, the preferences or
        # the posting, and Phase 9 repairs nothing.
        if (
            role_domain_preferences_fingerprint(structured)
            != snapshot.structured_upstream_fingerprint
        ):
            issues.append(
                _issue(
                    RecommendationReadinessIssueCode.STALE_MATCHING_SNAPSHOT,
                    f"opportunity {opportunity_id} has structured signals that "
                    f"no longer match its persisted assessment; synchronize "
                    f"Matching first",
                    opportunity_id,
                )
            )
            continue
        try:
            fine = _fine_classification(connection, opportunity_id)
        except FineClassificationDecodeError as error:
            issues.append(
                _issue(
                    RecommendationReadinessIssueCode.FINE_CLASSIFICATION_INVALID,
                    f"opportunity {opportunity_id} has an unreadable fine "
                    f"classification: {error}",
                    opportunity_id,
                )
            )
            continue
        try:
            stored = read_eligibility(connection, user_id, opportunity_id)
        except (ValueError, TypeError) as error:
            issues.append(
                _issue(
                    RecommendationReadinessIssueCode.INVALID_UPSTREAM_VALUE,
                    f"opportunity {opportunity_id} has an invalid eligibility "
                    f"decision: {error}",
                    opportunity_id,
                )
            )
            continue
        if stored is not None and (
            stored.engine_version != ELIGIBILITY_ENGINE_VERSION
            or not _is_sha256(stored.input_fingerprint)
        ):
            issues.append(
                _issue(
                    RecommendationReadinessIssueCode.ELIGIBILITY_VERSION_STALE,
                    f"opportunity {opportunity_id} has a stale or unusable "
                    f"eligibility decision",
                    opportunity_id,
                )
            )
            continue
        records.append(
            RecommendationInputRecord(
                RecommendationInput(
                    matching=snapshot,
                    preferences=profile_input.preferences,
                    structured=structured,
                    fine=fine,
                    geography=RecommendationGeographyInput(
                        profile_target=profile_target,
                        resolutions=read_opportunity_resolutions(
                            connection, opportunity_id
                        ),
                    ),
                    eligibility=RecommendationEligibilityInput(
                        status=None if stored is None else stored.status,
                        input_fingerprint=(
                            None if stored is None else stored.input_fingerprint
                        ),
                        engine_version=(
                            None if stored is None else stored.engine_version
                        ),
                    ),
                ),
                RecommendationOpportunityContext(opportunity_id, *row),
            )
        )
    if issues:
        return _incomplete(profile_id, issues, user_id, run.run_id)
    return RecommendationInputAssemblyResult(
        RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
        RecommendationReadinessStatus.READY,
        profile_id,
        user_id,
        run.run_id,
        (),
        tuple(records),
    )
