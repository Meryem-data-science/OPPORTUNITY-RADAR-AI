"""Read-only integrity and historical-provenance audit for Portfolio snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from .models import PortfolioReasonCode
from .read_model import PortfolioReadError, _positive, _require_profile


class PortfolioAuditStatus(StrEnum):
    OK = "OK"
    CORRUPT = "CORRUPT"


class PortfolioProfileAuditStatus(StrEnum):
    NOT_SYNCED = "NOT_SYNCED"
    READY = "READY"
    CORRUPT = "CORRUPT"


class PortfolioAuditIssueCode(StrEnum):
    INVALID_RUN_JSON = "INVALID_RUN_JSON"
    NON_CANONICAL_RUN_JSON = "NON_CANONICAL_RUN_JSON"
    RUN_FINGERPRINT_MISMATCH = "RUN_FINGERPRINT_MISMATCH"
    RUN_METADATA_MISMATCH = "RUN_METADATA_MISMATCH"
    ASSESSMENT_COUNT_MISMATCH = "ASSESSMENT_COUNT_MISMATCH"
    RUN_COUNTS_MISMATCH = "RUN_COUNTS_MISMATCH"
    RUN_COHORT_MISMATCH = "RUN_COHORT_MISMATCH"
    INVALID_ASSESSMENT_JSON = "INVALID_ASSESSMENT_JSON"
    NON_CANONICAL_ASSESSMENT_JSON = "NON_CANONICAL_ASSESSMENT_JSON"
    ASSESSMENT_FINGERPRINT_MISMATCH = "ASSESSMENT_FINGERPRINT_MISMATCH"
    ASSESSMENT_COLUMNS_MISMATCH = "ASSESSMENT_COLUMNS_MISMATCH"
    PROFILE_MISSING = "PROFILE_MISSING"
    PRIORITY_RUN_MISSING = "PRIORITY_RUN_MISSING"
    PRIORITY_PROFILE_MISMATCH = "PRIORITY_PROFILE_MISMATCH"
    PRIORITY_FINGERPRINT_MISMATCH = "PRIORITY_FINGERPRINT_MISMATCH"
    MATCHING_RUN_MISSING = "MATCHING_RUN_MISSING"
    MATCHING_PROFILE_MISMATCH = "MATCHING_PROFILE_MISMATCH"
    MATCHING_FINGERPRINT_MISMATCH = "MATCHING_FINGERPRINT_MISMATCH"
    PRIORITY_MATCHING_PROVENANCE_MISMATCH = "PRIORITY_MATCHING_PROVENANCE_MISMATCH"
    PRIORITY_ASSESSMENT_PROVENANCE_MISMATCH = "PRIORITY_ASSESSMENT_PROVENANCE_MISMATCH"
    MATCHING_ASSESSMENT_PROVENANCE_MISMATCH = "MATCHING_ASSESSMENT_PROVENANCE_MISMATCH"
    STATE_MISSING_WITH_HISTORY = "STATE_MISSING_WITH_HISTORY"
    STATE_RUN_MISSING = "STATE_RUN_MISSING"
    STATE_RUN_PROFILE_MISMATCH = "STATE_RUN_PROFILE_MISMATCH"
    STATE_VERSION_MISMATCH = "STATE_VERSION_MISMATCH"


@dataclass(frozen=True)
class PortfolioAuditIssue:
    code: PortfolioAuditIssueCode
    message: str
    run_id: int | None
    opportunity_id: int | None


@dataclass(frozen=True)
class PortfolioRunAuditResult:
    run_id: int
    profile_id: int | None
    status: PortfolioAuditStatus
    issues: tuple[PortfolioAuditIssue, ...]


@dataclass(frozen=True)
class PortfolioProfileAuditResult:
    profile_id: int
    status: PortfolioProfileAuditStatus
    current_run_id: int | None
    history_count: int
    run_audit: PortfolioRunAuditResult | None
    issues: tuple[PortfolioAuditIssue, ...]


def _issue(
    code: PortfolioAuditIssueCode, run_id: int | None, opportunity_id: int | None = None
) -> PortfolioAuditIssue:
    target = "Portfolio profile state" if run_id is None else f"Portfolio run {run_id}"
    if opportunity_id is not None:
        target += f", opportunity {opportunity_id}"
    return PortfolioAuditIssue(
        code,
        f"{target}: {code.value.lower().replace('_', ' ')}",
        run_id,
        opportunity_id,
    )


def _ordered(items: list[PortfolioAuditIssue]) -> tuple[PortfolioAuditIssue, ...]:
    return tuple(
        sorted(
            items,
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


def audit_portfolio_run(
    connection: sqlite3.Connection, run_id: int
) -> PortfolioRunAuditResult:
    run_id = _positive(run_id, "run_id")
    row = connection.execute(
        """SELECT id,profile_id,priority_run_id,matching_run_id,persistence_version,
        input_assembly_version,portfolio_engine_version,portfolio_rules_version,
        priority_run_fingerprint,matching_run_fingerprint,run_fingerprint,assessment_count,
        included_count,excluded_count,safe_count,target_count,ambitious_count,run_payload_json
        FROM portfolio_runs WHERE id=?""",
        (run_id,),
    ).fetchone()
    if row is None:
        raise PortfolioReadError(f"portfolio run {run_id} does not exist")
    issues: list[PortfolioAuditIssue] = []
    payload, canonical = _object(row[17])
    if payload is None:
        issues.append(_issue(PortfolioAuditIssueCode.INVALID_RUN_JSON, run_id))
    else:
        if row[17] != canonical:
            issues.append(
                _issue(PortfolioAuditIssueCode.NON_CANONICAL_RUN_JSON, run_id)
            )
        if hashlib.sha256(canonical.encode()).hexdigest() != row[10]:
            issues.append(
                _issue(PortfolioAuditIssueCode.RUN_FINGERPRINT_MISMATCH, run_id)
            )
        metadata = {
            "persistence_version": row[4],
            "input_assembly_version": row[5],
            "profile_id": row[1],
            "priority_run_fingerprint": row[8],
            "matching_run_fingerprint": row[9],
            "portfolio_engine_version": row[6],
            "portfolio_rules_version": row[7],
        }
        if any(payload.get(key) != value for key, value in metadata.items()):
            issues.append(_issue(PortfolioAuditIssueCode.RUN_METADATA_MISMATCH, run_id))
    rows = connection.execute(
        """SELECT opportunity_id,disposition,bucket,priority_category,
        eligibility_status,matching_lane,required_skill_score,required_skill_matched_count,
        required_skill_total_count,safe_cap_applied,assessment_fingerprint,assessment_payload_json
        FROM portfolio_assessments WHERE run_id=? ORDER BY opportunity_id ASC""",
        (run_id,),
    ).fetchall()
    if len(rows) != row[11] or (
        payload is not None and payload.get("assessment_count") != len(rows)
    ):
        issues.append(_issue(PortfolioAuditIssueCode.ASSESSMENT_COUNT_MISMATCH, run_id))
    dispositions = Counter(item[1] for item in rows)
    buckets = Counter(item[2] for item in rows)
    actual_counts = (
        dispositions["INCLUDED"],
        dispositions["EXCLUDED"],
        buckets["SAFE"],
        buckets["TARGET"],
        buckets["AMBITIOUS"],
    )
    stored_counts = tuple(row[12:17])
    payload_counts = (
        None
        if payload is None
        else (
            payload.get("included_count"),
            payload.get("excluded_count"),
            *(
                (payload.get("bucket_counts") or {}).get(key)
                if isinstance(payload.get("bucket_counts"), dict)
                else None
                for key in ("SAFE", "TARGET", "AMBITIOUS")
            ),
        )
    )
    if actual_counts != stored_counts or (
        payload is not None and payload_counts != stored_counts
    ):
        issues.append(_issue(PortfolioAuditIssueCode.RUN_COUNTS_MISMATCH, run_id))
    cohort = [
        {"opportunity_id": item[0], "assessment_fingerprint": item[10]} for item in rows
    ]
    if payload is not None and payload.get("assessments") != cohort:
        issues.append(_issue(PortfolioAuditIssueCode.RUN_COHORT_MISMATCH, run_id))
    priority_assessments = dict(
        connection.execute(
            "SELECT opportunity_id,assessment_fingerprint FROM priority_assessments WHERE run_id=?",
            (row[2],),
        ).fetchall()
    )
    matching_assessments = dict(
        connection.execute(
            "SELECT opportunity_id,assessment_fingerprint FROM matching_assessments WHERE run_id=?",
            (row[3],),
        ).fetchall()
    )
    for item in rows:
        oid = item[0]
        assessment, item_canonical = _object(item[11])
        if assessment is None:
            issues.append(
                _issue(PortfolioAuditIssueCode.INVALID_ASSESSMENT_JSON, run_id, oid)
            )
            continue
        if item[11] != item_canonical:
            issues.append(
                _issue(
                    PortfolioAuditIssueCode.NON_CANONICAL_ASSESSMENT_JSON, run_id, oid
                )
            )
        if hashlib.sha256(item_canonical.encode()).hexdigest() != item[10]:
            issues.append(
                _issue(
                    PortfolioAuditIssueCode.ASSESSMENT_FINGERPRINT_MISMATCH, run_id, oid
                )
            )
        try:
            upstream, result = assessment["upstream"], assessment["result"]
            skill = upstream["required_skill"]
            reason_codes = result["reason_codes"]
            columns = (
                result["disposition"],
                result["bucket"],
                upstream["priority_category"],
                upstream["eligibility_status"],
                upstream["matching_lane"],
                skill["score"],
                skill["matched_count"],
                skill["total_count"],
                result["safe_cap_applied"],
            )
            reasons_valid = isinstance(reason_codes, list) and all(
                not isinstance(value, bool) and PortfolioReasonCode(value)
                for value in reason_codes
            )
        except (KeyError, TypeError, ValueError):
            upstream, columns, reasons_valid = {}, object(), False
        persisted = (*item[1:9], bool(item[9]) if item[9] in (0, 1) else item[9])
        if columns != persisted or not reasons_valid:
            issues.append(
                _issue(PortfolioAuditIssueCode.ASSESSMENT_COLUMNS_MISMATCH, run_id, oid)
            )
        if upstream.get("priority_assessment_fingerprint") != priority_assessments.get(
            oid
        ):
            issues.append(
                _issue(
                    PortfolioAuditIssueCode.PRIORITY_ASSESSMENT_PROVENANCE_MISMATCH,
                    run_id,
                    oid,
                )
            )
        if upstream.get("matching_assessment_fingerprint") != matching_assessments.get(
            oid
        ):
            issues.append(
                _issue(
                    PortfolioAuditIssueCode.MATCHING_ASSESSMENT_PROVENANCE_MISMATCH,
                    run_id,
                    oid,
                )
            )
    if (
        connection.execute("SELECT 1 FROM profiles WHERE id=?", (row[1],)).fetchone()
        is None
    ):
        issues.append(_issue(PortfolioAuditIssueCode.PROFILE_MISSING, run_id))
    priority = connection.execute(
        "SELECT profile_id,matching_run_id,matching_run_fingerprint,run_fingerprint FROM priority_runs WHERE id=?",
        (row[2],),
    ).fetchone()
    if priority is None:
        issues.append(_issue(PortfolioAuditIssueCode.PRIORITY_RUN_MISSING, run_id))
    else:
        if priority[0] != row[1]:
            issues.append(
                _issue(PortfolioAuditIssueCode.PRIORITY_PROFILE_MISMATCH, run_id)
            )
        if priority[3] != row[8]:
            issues.append(
                _issue(PortfolioAuditIssueCode.PRIORITY_FINGERPRINT_MISMATCH, run_id)
            )
        if (priority[1], priority[2]) != (row[3], row[9]):
            issues.append(
                _issue(
                    PortfolioAuditIssueCode.PRIORITY_MATCHING_PROVENANCE_MISMATCH,
                    run_id,
                )
            )
    matching = connection.execute(
        "SELECT profile_id,run_fingerprint FROM matching_runs WHERE id=?", (row[3],)
    ).fetchone()
    if matching is None:
        issues.append(_issue(PortfolioAuditIssueCode.MATCHING_RUN_MISSING, run_id))
    else:
        if matching[0] != row[1]:
            issues.append(
                _issue(PortfolioAuditIssueCode.MATCHING_PROFILE_MISMATCH, run_id)
            )
        if matching[1] != row[9]:
            issues.append(
                _issue(PortfolioAuditIssueCode.MATCHING_FINGERPRINT_MISMATCH, run_id)
            )
    ordered = _ordered(issues)
    return PortfolioRunAuditResult(
        run_id,
        row[1],
        PortfolioAuditStatus.OK if not ordered else PortfolioAuditStatus.CORRUPT,
        ordered,
    )


def audit_current_portfolio(
    connection: sqlite3.Connection, profile_id: int
) -> PortfolioProfileAuditResult:
    profile_id = _positive(profile_id, "profile_id")
    _require_profile(connection, profile_id)
    history = connection.execute(
        "SELECT COUNT(*) FROM portfolio_runs WHERE profile_id=?", (profile_id,)
    ).fetchone()[0]
    state = connection.execute(
        "SELECT current_run_id,persistence_version,input_assembly_version FROM portfolio_profile_state WHERE profile_id=?",
        (profile_id,),
    ).fetchone()
    if state is None:
        if not history:
            return PortfolioProfileAuditResult(
                profile_id, PortfolioProfileAuditStatus.NOT_SYNCED, None, 0, None, ()
            )
        issues = (_issue(PortfolioAuditIssueCode.STATE_MISSING_WITH_HISTORY, None),)
        return PortfolioProfileAuditResult(
            profile_id, PortfolioProfileAuditStatus.CORRUPT, None, history, None, issues
        )
    current = state[0]
    run = connection.execute(
        "SELECT profile_id,persistence_version,input_assembly_version FROM portfolio_runs WHERE id=?",
        (current,),
    ).fetchone()
    issues: list[PortfolioAuditIssue] = []
    if run is None:
        issues.append(_issue(PortfolioAuditIssueCode.STATE_RUN_MISSING, current))
    else:
        if run[0] != profile_id:
            issues.append(
                _issue(PortfolioAuditIssueCode.STATE_RUN_PROFILE_MISMATCH, current)
            )
        if (state[1], state[2]) != (run[1], run[2]):
            issues.append(
                _issue(PortfolioAuditIssueCode.STATE_VERSION_MISMATCH, current)
            )
    if issues:
        ordered = _ordered(issues)
        return PortfolioProfileAuditResult(
            profile_id,
            PortfolioProfileAuditStatus.CORRUPT,
            current,
            history,
            None,
            ordered,
        )
    run_audit = audit_portfolio_run(connection, current)
    status = (
        PortfolioProfileAuditStatus.READY
        if run_audit.status is PortfolioAuditStatus.OK
        else PortfolioProfileAuditStatus.CORRUPT
    )
    return PortfolioProfileAuditResult(
        profile_id, status, current, history, run_audit, run_audit.issues
    )
