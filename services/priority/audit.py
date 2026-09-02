"""Read-only integrity and provenance audit for persisted Priority snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from .read_model import PriorityReadError, _positive, _require_profile


class PriorityAuditStatus(StrEnum):
    OK = "OK"
    CORRUPT = "CORRUPT"


class PriorityProfileAuditStatus(StrEnum):
    NOT_SYNCED = "NOT_SYNCED"
    READY = "READY"
    CORRUPT = "CORRUPT"


class PriorityAuditIssueCode(StrEnum):
    INVALID_RUN_JSON = "INVALID_RUN_JSON"
    NON_CANONICAL_RUN_JSON = "NON_CANONICAL_RUN_JSON"
    RUN_FINGERPRINT_MISMATCH = "RUN_FINGERPRINT_MISMATCH"
    RUN_METADATA_MISMATCH = "RUN_METADATA_MISMATCH"
    ASSESSMENT_COUNT_MISMATCH = "ASSESSMENT_COUNT_MISMATCH"
    INVALID_ASSESSMENT_JSON = "INVALID_ASSESSMENT_JSON"
    NON_CANONICAL_ASSESSMENT_JSON = "NON_CANONICAL_ASSESSMENT_JSON"
    ASSESSMENT_FINGERPRINT_MISMATCH = "ASSESSMENT_FINGERPRINT_MISMATCH"
    ASSESSMENT_COLUMNS_MISMATCH = "ASSESSMENT_COLUMNS_MISMATCH"
    RUN_COHORT_MISMATCH = "RUN_COHORT_MISMATCH"
    PROFILE_MISSING = "PROFILE_MISSING"
    PROFILE_USER_PROVENANCE_MISMATCH = "PROFILE_USER_PROVENANCE_MISMATCH"
    MATCHING_RUN_MISSING = "MATCHING_RUN_MISSING"
    MATCHING_PROFILE_MISMATCH = "MATCHING_PROFILE_MISMATCH"
    MATCHING_FINGERPRINT_MISMATCH = "MATCHING_FINGERPRINT_MISMATCH"
    STATE_MISSING_WITH_HISTORY = "STATE_MISSING_WITH_HISTORY"
    STATE_RUN_MISSING = "STATE_RUN_MISSING"
    STATE_RUN_PROFILE_MISMATCH = "STATE_RUN_PROFILE_MISMATCH"
    STATE_VERSION_MISMATCH = "STATE_VERSION_MISMATCH"


@dataclass(frozen=True)
class PriorityAuditIssue:
    code: PriorityAuditIssueCode
    message: str
    run_id: int | None
    opportunity_id: int | None


@dataclass(frozen=True)
class PriorityRunAuditResult:
    run_id: int
    profile_id: int | None
    status: PriorityAuditStatus
    issues: tuple[PriorityAuditIssue, ...]


@dataclass(frozen=True)
class PriorityProfileAuditResult:
    profile_id: int
    status: PriorityProfileAuditStatus
    current_run_id: int | None
    history_count: int
    run_audit: PriorityRunAuditResult | None
    issues: tuple[PriorityAuditIssue, ...]


def _issue(
    code: PriorityAuditIssueCode, run_id: int | None, opportunity_id: int | None = None
) -> PriorityAuditIssue:
    target = (
        f"priority run {run_id}" if run_id is not None else "priority profile state"
    )
    if opportunity_id is not None:
        target += f", opportunity {opportunity_id}"
    return PriorityAuditIssue(
        code,
        f"{target}: {code.value.lower().replace('_', ' ')}",
        run_id,
        opportunity_id,
    )


def _ordered(issues: list[PriorityAuditIssue]) -> tuple[PriorityAuditIssue, ...]:
    return tuple(
        sorted(
            issues,
            key=lambda item: (
                item.opportunity_id is not None,
                item.opportunity_id or 0,
                item.code.value,
            ),
        )
    )


def _object(raw: object) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(raw, str):
        return None, None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None, None
    if not isinstance(value, dict):
        return None, None
    return value, canonical_json(value)


def audit_priority_run(
    connection: sqlite3.Connection, run_id: int
) -> PriorityRunAuditResult:
    run_id = _positive(run_id, "run_id")
    row = connection.execute(
        """SELECT id,profile_id,user_id,matching_run_id,persistence_version,input_assembly_version,
        priority_engine_version,priority_rules_version,freshness_version,quality_version,
        evaluation_date,matching_run_fingerprint,run_fingerprint,assessment_count,run_payload_json
        FROM priority_runs WHERE id=?""",
        (run_id,),
    ).fetchone()
    if row is None:
        raise PriorityReadError(f"priority run {run_id} does not exist")
    issues: list[PriorityAuditIssue] = []
    profile_id = row[1]
    payload, canonical = _object(row[14])
    if payload is None:
        issues.append(_issue(PriorityAuditIssueCode.INVALID_RUN_JSON, run_id))
    else:
        if row[14] != canonical:
            issues.append(_issue(PriorityAuditIssueCode.NON_CANONICAL_RUN_JSON, run_id))
        computed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if computed != row[12]:
            issues.append(
                _issue(PriorityAuditIssueCode.RUN_FINGERPRINT_MISMATCH, run_id)
            )
        expected = {
            "persistence_version": row[4],
            "input_assembly_version": row[5],
            "profile_id": row[1],
            "user_id": row[2],
            "evaluation_date": row[10],
            "matching_run_fingerprint": row[11],
            "priority_engine_version": row[6],
            "priority_rules_version": row[7],
            "freshness_version": row[8],
            "quality_version": row[9],
            "assessment_count": row[13],
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            issues.append(_issue(PriorityAuditIssueCode.RUN_METADATA_MISMATCH, run_id))

    assessment_rows = connection.execute(
        """SELECT opportunity_id,priority_score,priority_evidence_coverage,priority_category,
        eligibility_status,matching_lane,assessment_fingerprint,assessment_payload_json
        FROM priority_assessments WHERE run_id=? ORDER BY opportunity_id ASC""",
        (run_id,),
    ).fetchall()
    if len(assessment_rows) != row[13] or (
        payload is not None and payload.get("assessment_count") != len(assessment_rows)
    ):
        issues.append(_issue(PriorityAuditIssueCode.ASSESSMENT_COUNT_MISMATCH, run_id))
    cohort = [
        {"opportunity_id": item[0], "assessment_fingerprint": item[6]}
        for item in assessment_rows
    ]
    if payload is not None and payload.get("assessments") != cohort:
        issues.append(_issue(PriorityAuditIssueCode.RUN_COHORT_MISMATCH, run_id))
    for item in assessment_rows:
        opportunity_id = item[0]
        item_payload, item_canonical = _object(item[7])
        if item_payload is None:
            issues.append(
                _issue(
                    PriorityAuditIssueCode.INVALID_ASSESSMENT_JSON,
                    run_id,
                    opportunity_id,
                )
            )
            continue
        if item[7] != item_canonical:
            issues.append(
                _issue(
                    PriorityAuditIssueCode.NON_CANONICAL_ASSESSMENT_JSON,
                    run_id,
                    opportunity_id,
                )
            )
        if hashlib.sha256(item_canonical.encode("utf-8")).hexdigest() != item[6]:
            issues.append(
                _issue(
                    PriorityAuditIssueCode.ASSESSMENT_FINGERPRINT_MISMATCH,
                    run_id,
                    opportunity_id,
                )
            )
        try:
            columns = (
                item_payload["result"]["priority_score"],
                item_payload["result"]["priority_evidence_coverage"],
                item_payload["result"]["priority_category"],
                item_payload["eligibility"]["status"],
                item_payload["match"]["lane"],
            )
        except (KeyError, TypeError):
            columns = object()
        if columns != tuple(item[1:6]):
            issues.append(
                _issue(
                    PriorityAuditIssueCode.ASSESSMENT_COLUMNS_MISMATCH,
                    run_id,
                    opportunity_id,
                )
            )

    owner = connection.execute(
        "SELECT user_id FROM profiles WHERE id=?", (profile_id,)
    ).fetchone()
    if owner is None:
        issues.append(_issue(PriorityAuditIssueCode.PROFILE_MISSING, run_id))
    elif owner[0] != row[2]:
        issues.append(
            _issue(PriorityAuditIssueCode.PROFILE_USER_PROVENANCE_MISMATCH, run_id)
        )
    matching = connection.execute(
        "SELECT profile_id,run_fingerprint FROM matching_runs WHERE id=?", (row[3],)
    ).fetchone()
    if matching is None:
        issues.append(_issue(PriorityAuditIssueCode.MATCHING_RUN_MISSING, run_id))
    else:
        if matching[0] != profile_id:
            issues.append(
                _issue(PriorityAuditIssueCode.MATCHING_PROFILE_MISMATCH, run_id)
            )
        if matching[1] != row[11]:
            issues.append(
                _issue(PriorityAuditIssueCode.MATCHING_FINGERPRINT_MISMATCH, run_id)
            )
    ordered = _ordered(issues)
    return PriorityRunAuditResult(
        run_id,
        profile_id,
        PriorityAuditStatus.CORRUPT if ordered else PriorityAuditStatus.OK,
        ordered,
    )


def audit_current_priority(
    connection: sqlite3.Connection, profile_id: int
) -> PriorityProfileAuditResult:
    profile_id = _positive(profile_id, "profile_id")
    _require_profile(connection, profile_id)
    history_count = connection.execute(
        "SELECT COUNT(*) FROM priority_runs WHERE profile_id=?", (profile_id,)
    ).fetchone()[0]
    state = connection.execute(
        "SELECT current_run_id,persistence_version,input_assembly_version FROM priority_profile_state WHERE profile_id=?",
        (profile_id,),
    ).fetchone()
    if state is None:
        if not history_count:
            return PriorityProfileAuditResult(
                profile_id, PriorityProfileAuditStatus.NOT_SYNCED, None, 0, None, ()
            )
        issue = _issue(PriorityAuditIssueCode.STATE_MISSING_WITH_HISTORY, None)
        return PriorityProfileAuditResult(
            profile_id,
            PriorityProfileAuditStatus.CORRUPT,
            None,
            history_count,
            None,
            (issue,),
        )
    current_run_id = state[0]
    run = connection.execute(
        "SELECT profile_id,persistence_version,input_assembly_version FROM priority_runs WHERE id=?",
        (current_run_id,),
    ).fetchone()
    if run is None:
        issue = _issue(PriorityAuditIssueCode.STATE_RUN_MISSING, current_run_id)
        return PriorityProfileAuditResult(
            profile_id,
            PriorityProfileAuditStatus.CORRUPT,
            current_run_id,
            history_count,
            None,
            (issue,),
        )
    state_issues = []
    if run[0] != profile_id:
        state_issues.append(
            _issue(PriorityAuditIssueCode.STATE_RUN_PROFILE_MISMATCH, current_run_id)
        )
    if tuple(state[1:]) != tuple(run[1:]):
        state_issues.append(
            _issue(PriorityAuditIssueCode.STATE_VERSION_MISMATCH, current_run_id)
        )
    if state_issues:
        return PriorityProfileAuditResult(
            profile_id,
            PriorityProfileAuditStatus.CORRUPT,
            current_run_id,
            history_count,
            None,
            _ordered(state_issues),
        )
    run_audit = audit_priority_run(connection, current_run_id)
    return PriorityProfileAuditResult(
        profile_id,
        PriorityProfileAuditStatus.READY
        if run_audit.status is PriorityAuditStatus.OK
        else PriorityProfileAuditStatus.CORRUPT,
        current_run_id,
        history_count,
        run_audit,
        run_audit.issues,
    )
