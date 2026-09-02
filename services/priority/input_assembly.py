"""Read-only assembly and full-corpus preflight for Priority v1 inputs."""

from __future__ import annotations

import math
import numbers
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from services.collector.matching import (
    MATCHING_ENGINE_VERSION,
    MATCHING_PERSISTENCE_VERSION,
    MATCHING_RULES_VERSION,
    MATCHING_SELECTION_VERSION,
    SEMANTIC_PERCENTILE_VERSION,
    MatchLane,
    MatchingReadError,
    read_current_matching,
    select_matching_opportunity_ids,
)
from services.collector.qualification.taxonomy import ListingQuality
from services.eligibility import ELIGIBILITY_ENGINE_VERSION
from services.eligibility.repository import read_eligibility

from .models import PriorityEligibilitySnapshot, PriorityInput, PriorityMatchingSnapshot

PRIORITY_INPUT_ASSEMBLY_VERSION = "priority-input-assembly-v1"


class PriorityReadinessStatus(StrEnum):
    READY = "READY"
    INCOMPLETE = "INCOMPLETE"


class PriorityReadinessIssueCode(StrEnum):
    PROFILE_NOT_FOUND = "PROFILE_NOT_FOUND"
    PROFILE_OWNER_INVALID = "PROFILE_OWNER_INVALID"
    MATCHING_NOT_READY = "MATCHING_NOT_READY"
    MATCHING_VERSION_STALE = "MATCHING_VERSION_STALE"
    STALE_MATCHING_COHORT = "STALE_MATCHING_COHORT"
    OPPORTUNITY_MISSING = "OPPORTUNITY_MISSING"
    QUALIFICATION_SNAPSHOT_MISSING = "QUALIFICATION_SNAPSHOT_MISSING"
    ELIGIBILITY_SNAPSHOT_MISSING = "ELIGIBILITY_SNAPSHOT_MISSING"
    ELIGIBILITY_VERSION_STALE = "ELIGIBILITY_VERSION_STALE"
    INVALID_PUBLISHED_AT = "INVALID_PUBLISHED_AT"
    INVALID_DEADLINE = "INVALID_DEADLINE"
    INVALID_UPSTREAM_VALUE = "INVALID_UPSTREAM_VALUE"


@dataclass(frozen=True)
class PriorityReadinessIssue:
    code: PriorityReadinessIssueCode
    message: str
    opportunity_id: int | None = None


@dataclass(frozen=True)
class PriorityOpportunityContext:
    opportunity_id: int
    canonical_title: str
    organization: str
    source_url: str
    application_url: str | None
    canonical_url: str | None


@dataclass(frozen=True)
class PriorityInputRecord:
    priority_input: PriorityInput
    context: PriorityOpportunityContext


@dataclass(frozen=True)
class PriorityInputAssemblyResult:
    assembly_version: str
    status: PriorityReadinessStatus
    profile_id: int
    user_id: int | None
    matching_run_id: int | None
    evaluation_date: date
    issues: tuple[PriorityReadinessIssue, ...]
    records: tuple[PriorityInputRecord, ...]


def parse_persisted_date(value: object) -> date | None:
    """Parse an explicit ISO date/datetime, normalizing aware datetimes to UTC."""
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("value is not a non-empty trimmed ISO string")
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("value is not a valid ISO date or datetime") from error
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC)
    return parsed.date()


def _issue(code, message, opportunity_id=None):
    return PriorityReadinessIssue(code, message, opportunity_id)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_matching_numbers(match_quality: object, evidence_coverage: object) -> bool:
    if (
        isinstance(evidence_coverage, bool)
        or not isinstance(evidence_coverage, numbers.Real)
        or not math.isfinite(evidence_coverage)
        or not 0.0 <= evidence_coverage <= 1.0
    ):
        return False
    if match_quality is not None and (
        isinstance(match_quality, bool)
        or not isinstance(match_quality, numbers.Real)
        or not math.isfinite(match_quality)
        or not 0.0 <= match_quality <= 1.0
    ):
        return False
    return (evidence_coverage == 0.0) == (match_quality is None)


def _incomplete(profile_id, evaluation_date, issues, user_id=None, run_id=None):
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
    return PriorityInputAssemblyResult(
        PRIORITY_INPUT_ASSEMBLY_VERSION,
        PriorityReadinessStatus.INCOMPLETE,
        profile_id,
        user_id,
        run_id,
        evaluation_date,
        ordered,
        (),
    )


def assemble_priority_inputs(
    connection: sqlite3.Connection, profile_id: int, evaluation_date: date
) -> PriorityInputAssemblyResult:
    """Read and validate the complete current Matching cohort without scoring it."""
    owner = connection.execute(
        "SELECT user_id FROM profiles WHERE id = ?", (profile_id,)
    ).fetchone()
    if owner is None:
        return _incomplete(
            profile_id,
            evaluation_date,
            [
                _issue(
                    PriorityReadinessIssueCode.PROFILE_NOT_FOUND,
                    f"profile {profile_id} was not found",
                )
            ],
        )
    user_id = owner[0]
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        return _incomplete(
            profile_id,
            evaluation_date,
            [
                _issue(
                    PriorityReadinessIssueCode.PROFILE_OWNER_INVALID,
                    f"profile {profile_id} has an invalid owner",
                )
            ],
        )
    try:
        matching = read_current_matching(connection, profile_id)
    except MatchingReadError as error:
        return _incomplete(
            profile_id,
            evaluation_date,
            [
                _issue(
                    PriorityReadinessIssueCode.MATCHING_NOT_READY,
                    f"matching snapshot is not readable: {error}",
                )
            ],
            user_id,
        )
    run = matching.current_run
    if matching.status != "READY" or run is None:
        return _incomplete(
            profile_id,
            evaluation_date,
            [
                _issue(
                    PriorityReadinessIssueCode.MATCHING_NOT_READY,
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
            evaluation_date,
            [
                _issue(
                    PriorityReadinessIssueCode.MATCHING_VERSION_STALE,
                    "matching run has unsupported versions: " + ", ".join(stale),
                )
            ],
            user_id,
            run.run_id,
        )
    persisted_ids = tuple(item.opportunity_id for item in run.assessments)
    selected_ids = select_matching_opportunity_ids(connection)
    if persisted_ids != selected_ids:
        return _incomplete(
            profile_id,
            evaluation_date,
            [
                _issue(
                    PriorityReadinessIssueCode.STALE_MATCHING_COHORT,
                    "matching cohort is stale; synchronize Matching first",
                )
            ],
            user_id,
            run.run_id,
        )

    issues: list[PriorityReadinessIssue] = []
    records: list[PriorityInputRecord] = []
    for assessment in run.assessments:
        opportunity_id = assessment.opportunity_id
        row = connection.execute(
            """SELECT o.canonical_title,o.organization,o.published_at,o.deadline,
                      o.source_url,o.application_url,o.canonical_url,q.listing_quality
                 FROM opportunities AS o
                 LEFT JOIN opportunity_qualifications AS q ON q.opportunity_id=o.id
                WHERE o.id=?""",
            (opportunity_id,),
        ).fetchone()
        if row is None:
            issues.append(
                _issue(
                    PriorityReadinessIssueCode.OPPORTUNITY_MISSING,
                    f"opportunity {opportunity_id} was not found",
                    opportunity_id,
                )
            )
            continue
        if row[7] is None:
            issues.append(
                _issue(
                    PriorityReadinessIssueCode.QUALIFICATION_SNAPSHOT_MISSING,
                    f"opportunity {opportunity_id} has no qualification snapshot",
                    opportunity_id,
                )
            )
            quality = None
        else:
            try:
                quality = ListingQuality(row[7])
            except (ValueError, TypeError):
                quality = None
                issues.append(
                    _issue(
                        PriorityReadinessIssueCode.INVALID_UPSTREAM_VALUE,
                        f"opportunity {opportunity_id} has invalid listing_quality",
                        opportunity_id,
                    )
                )
        stored = None
        eligibility_invalid = False
        try:
            stored = read_eligibility(connection, user_id, opportunity_id)
        except (ValueError, TypeError):
            eligibility_invalid = True
            issues.append(
                _issue(
                    PriorityReadinessIssueCode.INVALID_UPSTREAM_VALUE,
                    f"opportunity {opportunity_id} has invalid eligibility status",
                    opportunity_id,
                )
            )
        if stored is None and not eligibility_invalid:
            issues.append(
                _issue(
                    PriorityReadinessIssueCode.ELIGIBILITY_SNAPSHOT_MISSING,
                    f"opportunity {opportunity_id} has no eligibility snapshot for user {user_id}",
                    opportunity_id,
                )
            )
        elif stored is not None and stored.engine_version != ELIGIBILITY_ENGINE_VERSION:
            issues.append(
                _issue(
                    PriorityReadinessIssueCode.ELIGIBILITY_VERSION_STALE,
                    f"opportunity {opportunity_id} has stale eligibility engine version",
                    opportunity_id,
                )
            )
        try:
            lane = MatchLane(assessment.lane)
            if not _is_sha256(
                assessment.assessment_fingerprint
            ) or not _valid_matching_numbers(
                assessment.match_quality, assessment.evidence_coverage
            ):
                raise ValueError
        except (ValueError, TypeError):
            lane = None
            issues.append(
                _issue(
                    PriorityReadinessIssueCode.INVALID_UPSTREAM_VALUE,
                    f"opportunity {opportunity_id} has invalid Matching values",
                    opportunity_id,
                )
            )
        dates = []
        for raw, code, label in (
            (row[2], PriorityReadinessIssueCode.INVALID_PUBLISHED_AT, "published_at"),
            (row[3], PriorityReadinessIssueCode.INVALID_DEADLINE, "deadline"),
        ):
            try:
                parsed = parse_persisted_date(raw)
                if (
                    label == "published_at"
                    and parsed is not None
                    and parsed > evaluation_date
                ):
                    raise ValueError
                dates.append(parsed)
            except ValueError:
                dates.append(None)
                issues.append(
                    _issue(
                        code,
                        f"opportunity {opportunity_id} has invalid {label}",
                        opportunity_id,
                    )
                )
        if stored is None or quality is None or lane is None:
            continue
        if not _is_sha256(stored.input_fingerprint) or not stored.engine_version:
            issues.append(
                _issue(
                    PriorityReadinessIssueCode.INVALID_UPSTREAM_VALUE,
                    f"opportunity {opportunity_id} has invalid eligibility provenance",
                    opportunity_id,
                )
            )
            continue
        matching_snapshot = PriorityMatchingSnapshot(
            profile_id,
            opportunity_id,
            assessment.match_quality,
            assessment.evidence_coverage,
            lane,
            assessment.assessment_fingerprint,
            run.matching_engine_version,
            run.matching_rules_version,
            run.semantic_percentile_version,
        )
        eligibility_snapshot = PriorityEligibilitySnapshot(
            profile_id,
            opportunity_id,
            stored.status,
            stored.input_fingerprint,
            stored.engine_version,
        )
        records.append(
            PriorityInputRecord(
                PriorityInput(
                    matching_snapshot,
                    eligibility_snapshot,
                    quality,
                    dates[0],
                    dates[1],
                    evaluation_date,
                ),
                PriorityOpportunityContext(
                    opportunity_id, row[0], row[1], row[4], row[5], row[6]
                ),
            )
        )
    if issues:
        return _incomplete(profile_id, evaluation_date, issues, user_id, run.run_id)
    return PriorityInputAssemblyResult(
        PRIORITY_INPUT_ASSEMBLY_VERSION,
        PriorityReadinessStatus.READY,
        profile_id,
        user_id,
        run.run_id,
        evaluation_date,
        (),
        tuple(records),
    )
