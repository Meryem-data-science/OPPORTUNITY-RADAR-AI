"""Read-only, fail-closed assembly of Portfolio inputs from audited snapshots."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from services.collector.matching import (
    MatchLane,
    MatchingReadError,
    audit_matching_profile_history,
    read_matching_run,
)
from services.priority import PriorityProfileAuditStatus, audit_current_priority
from services.priority.read_model import read_current_priority

from .models import PortfolioInput

PORTFOLIO_INPUT_ASSEMBLY_VERSION = "portfolio-input-assembly-v1"


class PortfolioAssemblyStatus(StrEnum):
    READY = "READY"
    INCOMPLETE = "INCOMPLETE"


class PortfolioAssemblyIssueCode(StrEnum):
    PRIORITY_NOT_SYNCED = "PRIORITY_NOT_SYNCED"
    PRIORITY_AUDIT_CORRUPT = "PRIORITY_AUDIT_CORRUPT"
    MATCHING_RUN_MISSING = "MATCHING_RUN_MISSING"
    MATCHING_PROFILE_MISMATCH = "MATCHING_PROFILE_MISMATCH"
    MATCHING_RUN_FINGERPRINT_MISMATCH = "MATCHING_RUN_FINGERPRINT_MISMATCH"
    MATCHING_RUN_AUDIT_FAILED = "MATCHING_RUN_AUDIT_FAILED"
    PORTFOLIO_COHORT_MISMATCH = "PORTFOLIO_COHORT_MISMATCH"
    MATCHING_ASSESSMENT_PROVENANCE_MISMATCH = "MATCHING_ASSESSMENT_PROVENANCE_MISMATCH"
    MATCHING_LANE_PROVENANCE_MISMATCH = "MATCHING_LANE_PROVENANCE_MISMATCH"
    MATCHING_VERSION_PROVENANCE_MISMATCH = "MATCHING_VERSION_PROVENANCE_MISMATCH"
    REQUIRED_SKILL_SNAPSHOT_INVALID = "REQUIRED_SKILL_SNAPSHOT_INVALID"


@dataclass(frozen=True)
class PortfolioAssemblyIssue:
    code: PortfolioAssemblyIssueCode
    opportunity_id: int | None
    detail: str


@dataclass(frozen=True)
class PortfolioAssemblyResult:
    profile_id: int
    status: PortfolioAssemblyStatus
    input_assembly_version: str
    priority_run_id: int | None
    priority_run_fingerprint: str | None
    matching_run_id: int | None
    matching_run_fingerprint: str | None
    assessment_count: int
    inputs: tuple[PortfolioInput, ...]
    issues: tuple[PortfolioAssemblyIssue, ...]


def _issue(code: PortfolioAssemblyIssueCode, detail: str, opportunity_id=None):
    return PortfolioAssemblyIssue(code, opportunity_id, detail)


def _incomplete(
    profile_id: int,
    issues: list[PortfolioAssemblyIssue],
    *,
    priority_run=None,
    matching_run=None,
) -> PortfolioAssemblyResult:
    ordered = tuple(
        sorted(
            issues,
            key=lambda item: (
                item.opportunity_id is not None,
                item.opportunity_id or 0,
                item.code.value,
                item.detail,
            ),
        )
    )
    return PortfolioAssemblyResult(
        profile_id,
        PortfolioAssemblyStatus.INCOMPLETE,
        PORTFOLIO_INPUT_ASSEMBLY_VERSION,
        None if priority_run is None else priority_run.run_id,
        None if priority_run is None else priority_run.run_fingerprint,
        None if priority_run is None else priority_run.matching_run_id,
        None if matching_run is None else matching_run.run_fingerprint,
        0,
        (),
        ordered,
    )


def _required_skill(payload: Mapping[str, Any]) -> tuple[float | None, int, int] | None:
    value = payload.get("required_skill")
    if not isinstance(value, Mapping):
        return None
    score = value.get("normalized_score")
    matched = value.get("matched_count")
    total = value.get("total_count")
    if (
        isinstance(matched, bool)
        or not isinstance(matched, int)
        or matched < 0
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total < 0
        or matched > total
    ):
        return None
    if total == 0:
        return (None, matched, total) if matched == 0 and score is None else None
    if (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
        or not 0 <= score <= 1
    ):
        return None
    return float(score), matched, total


def assemble_portfolio_inputs(
    connection: sqlite3.Connection, profile_id: int
) -> PortfolioAssemblyResult:
    """Assemble the whole current Priority cohort, without writes or scoring."""
    priority_audit = audit_current_priority(connection, profile_id)
    if priority_audit.status is PriorityProfileAuditStatus.NOT_SYNCED:
        return _incomplete(
            profile_id,
            [
                _issue(
                    PortfolioAssemblyIssueCode.PRIORITY_NOT_SYNCED,
                    "Priority is not synced",
                )
            ],
        )
    if priority_audit.status is not PriorityProfileAuditStatus.READY:
        return _incomplete(
            profile_id,
            [
                _issue(
                    PortfolioAssemblyIssueCode.PRIORITY_AUDIT_CORRUPT,
                    "current Priority snapshot failed audit",
                )
            ],
        )

    priority = read_current_priority(connection, profile_id).current_run
    assert priority is not None  # guaranteed by the successful current-run audit
    try:
        matching = read_matching_run(connection, priority.matching_run_id)
    except MatchingReadError:
        return _incomplete(
            profile_id,
            [
                _issue(
                    PortfolioAssemblyIssueCode.MATCHING_RUN_MISSING,
                    f"matching run {priority.matching_run_id} is unavailable",
                )
            ],
            priority_run=priority,
        )
    structural: list[PortfolioAssemblyIssue] = []
    if matching.profile_id != profile_id:
        structural.append(
            _issue(
                PortfolioAssemblyIssueCode.MATCHING_PROFILE_MISMATCH,
                "referenced Matching run belongs to another profile",
            )
        )
    if matching.run_fingerprint != priority.matching_run_fingerprint:
        structural.append(
            _issue(
                PortfolioAssemblyIssueCode.MATCHING_RUN_FINGERPRINT_MISMATCH,
                "Priority and Matching run fingerprints differ",
            )
        )
    if structural:
        return _incomplete(
            profile_id, structural, priority_run=priority, matching_run=matching
        )

    report = audit_matching_profile_history(connection, profile_id)
    run_audit = next(
        (item for item in report.runs if item.run_id == priority.matching_run_id), None
    )
    if run_audit is None or not run_audit.ok:
        return _incomplete(
            profile_id,
            [
                _issue(
                    PortfolioAssemblyIssueCode.MATCHING_RUN_AUDIT_FAILED,
                    "referenced Matching run failed or was absent from its audit",
                )
            ],
            priority_run=priority,
            matching_run=matching,
        )

    priority_by_id = {item.opportunity_id: item for item in priority.assessments}
    matching_by_id = {item.opportunity_id: item for item in matching.assessments}
    if set(priority_by_id) != set(matching_by_id):
        return _incomplete(
            profile_id,
            [
                _issue(
                    PortfolioAssemblyIssueCode.PORTFOLIO_COHORT_MISMATCH,
                    "Priority and referenced Matching cohorts differ",
                )
            ],
            priority_run=priority,
            matching_run=matching,
        )

    issues: list[PortfolioAssemblyIssue] = []
    inputs: list[PortfolioInput] = []
    for opportunity_id in sorted(priority_by_id):
        priority_item = priority_by_id[opportunity_id]
        matching_item = matching_by_id[opportunity_id]
        match_payload = priority_item.assessment_payload.get("match")
        if (
            not isinstance(match_payload, Mapping)
            or match_payload.get("upstream_fingerprint")
            != matching_item.assessment_fingerprint
        ):
            issues.append(
                _issue(
                    PortfolioAssemblyIssueCode.MATCHING_ASSESSMENT_PROVENANCE_MISMATCH,
                    "Priority does not reference this Matching assessment",
                    opportunity_id,
                )
            )
        payload_lane = (
            match_payload.get("lane") if isinstance(match_payload, Mapping) else None
        )
        if not (
            priority_item.matching_lane.value == payload_lane == matching_item.lane
        ):
            issues.append(
                _issue(
                    PortfolioAssemblyIssueCode.MATCHING_LANE_PROVENANCE_MISMATCH,
                    "persisted Matching lanes differ",
                    opportunity_id,
                )
            )
        versions = (
            ("matching_engine_version", matching.matching_engine_version),
            ("matching_rules_version", matching.matching_rules_version),
            ("semantic_percentile_version", matching.semantic_percentile_version),
        )
        if not isinstance(match_payload, Mapping) or any(
            match_payload.get(name) != expected for name, expected in versions
        ):
            issues.append(
                _issue(
                    PortfolioAssemblyIssueCode.MATCHING_VERSION_PROVENANCE_MISMATCH,
                    "Priority Matching versions differ from the referenced run",
                    opportunity_id,
                )
            )
        required = _required_skill(matching_item.assessment_payload)
        if required is None:
            issues.append(
                _issue(
                    PortfolioAssemblyIssueCode.REQUIRED_SKILL_SNAPSHOT_INVALID,
                    "persisted required-skill snapshot is invalid",
                    opportunity_id,
                )
            )
            continue
        score, matched, total = required
        inputs.append(
            PortfolioInput(
                profile_id=profile_id,
                opportunity_id=opportunity_id,
                priority_category=priority_item.priority_category,
                eligibility_status=priority_item.eligibility_status,
                matching_lane=MatchLane(matching_item.lane),
                required_skill_score=score,
                required_skill_matched_count=matched,
                required_skill_total_count=total,
                priority_assessment_fingerprint=priority_item.assessment_fingerprint,
                matching_assessment_fingerprint=matching_item.assessment_fingerprint,
                priority_engine_version=priority.priority_engine_version,
                priority_rules_version=priority.priority_rules_version,
                priority_freshness_version=priority.freshness_version,
                priority_quality_version=priority.quality_version,
                matching_engine_version=matching.matching_engine_version,
                matching_rules_version=matching.matching_rules_version,
                semantic_percentile_version=matching.semantic_percentile_version,
            )
        )
    if issues or len(inputs) != priority.assessment_count:
        return _incomplete(
            profile_id, issues, priority_run=priority, matching_run=matching
        )
    return PortfolioAssemblyResult(
        profile_id,
        PortfolioAssemblyStatus.READY,
        PORTFOLIO_INPUT_ASSEMBLY_VERSION,
        priority.run_id,
        priority.run_fingerprint,
        matching.run_id,
        matching.run_fingerprint,
        len(inputs),
        tuple(inputs),
        (),
    )
