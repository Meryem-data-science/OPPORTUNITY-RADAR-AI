"""Read-only internal-integrity audit of persisted matching history."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .fingerprint import canonical_json
from .persistence_audit_fingerprint import matching_persistence_audit_fingerprint
from .persistence_fingerprint import matching_run_fingerprint

MATCHING_PERSISTENCE_AUDIT_VERSION = "matching-persistence-audit-v1"


class MatchingPersistenceAuditError(RuntimeError):
    """Raised when an audit cannot be performed."""


@dataclass(frozen=True, order=True)
class MatchingPersistenceAuditIssue:
    code: str
    run_id: int | None
    opportunity_id: int | None
    detail: str


@dataclass(frozen=True)
class MatchingRunAuditResult:
    run_id: int
    run_fingerprint: str
    batch_fingerprint: str
    assessment_fingerprints: tuple[tuple[int, str], ...]
    assessment_count: int
    ok: bool
    issues: tuple[MatchingPersistenceAuditIssue, ...]


@dataclass(frozen=True)
class MatchingPersistenceAuditReport:
    audit_version: str
    profile_id: int
    status: str
    current_run_id: int | None
    run_count: int
    audited_run_count: int
    assessment_count: int
    ok: bool
    issues: tuple[MatchingPersistenceAuditIssue, ...]
    runs: tuple[MatchingRunAuditResult, ...]
    audit_fingerprint: str


def _issue(code: str, run_id: int | None, opportunity_id: int | None, detail: str):
    return MatchingPersistenceAuditIssue(code, run_id, opportunity_id, detail)


def _digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _assessment_issues(row: tuple, run: tuple) -> list[MatchingPersistenceAuditIssue]:
    opportunity_id, lane, quality, coverage, fingerprint, stored = row
    run_id = run[0]
    try:
        payload = json.loads(stored)
    except (json.JSONDecodeError, TypeError):
        return [
            _issue(
                "ASSESSMENT_PAYLOAD_INVALID_JSON",
                run_id,
                opportunity_id,
                "assessment payload is invalid JSON",
            )
        ]
    issues = []
    if not isinstance(payload, dict):
        return [
            _issue(
                "ASSESSMENT_PAYLOAD_INVALID_JSON",
                run_id,
                opportunity_id,
                "assessment payload is not an object",
            )
        ]
    canonical = canonical_json(payload)
    if canonical != stored:
        issues.append(
            _issue(
                "ASSESSMENT_PAYLOAD_NOT_CANONICAL",
                run_id,
                opportunity_id,
                "assessment payload is not canonical JSON",
            )
        )
    if _digest(payload) != fingerprint:
        issues.append(
            _issue(
                "ASSESSMENT_FINGERPRINT_MISMATCH",
                run_id,
                opportunity_id,
                "assessment fingerprint differs from payload",
            )
        )
    for code, key, actual in (
        ("ASSESSMENT_LANE_MISMATCH", "lane", lane),
        ("ASSESSMENT_MATCH_QUALITY_MISMATCH", "match_quality", quality),
        ("ASSESSMENT_EVIDENCE_COVERAGE_MISMATCH", "evidence_coverage", coverage),
    ):
        if payload.get(key) != actual:
            issues.append(
                _issue(
                    code, run_id, opportunity_id, f"column {key} differs from payload"
                )
            )
    versions = (run[4], run[5], run[6])
    payload_versions = tuple(
        payload.get(key)
        for key in (
            "matching_engine_version",
            "matching_rules_version",
            "semantic_percentile_version",
        )
    )
    semantic = payload.get("semantic")
    if (
        payload_versions != versions
        or not isinstance(semantic, dict)
        or semantic.get("semantic_percentile_version") != run[6]
    ):
        issues.append(
            _issue(
                "ASSESSMENT_VERSION_MISMATCH",
                run_id,
                opportunity_id,
                "assessment versions differ from run",
            )
        )
    if not isinstance(semantic, dict) or semantic.get("corpus_fingerprint") != run[7]:
        issues.append(
            _issue(
                "ASSESSMENT_CORPUS_FINGERPRINT_MISMATCH",
                run_id,
                opportunity_id,
                "assessment corpus fingerprint differs from run",
            )
        )
    if not isinstance(semantic, dict) or semantic.get("model_fingerprint") != run[8]:
        issues.append(
            _issue(
                "ASSESSMENT_MODEL_FINGERPRINT_MISMATCH",
                run_id,
                opportunity_id,
                "assessment model fingerprint differs from run",
            )
        )
    return issues


def _audit_run(connection: sqlite3.Connection, run: tuple) -> MatchingRunAuditResult:
    run_id = run[0]
    rows = connection.execute(
        """SELECT opportunity_id,lane,match_quality,evidence_coverage,
        assessment_fingerprint,assessment_payload_json FROM matching_assessments
        WHERE run_id=? ORDER BY opportunity_id ASC""",
        (run_id,),
    ).fetchall()
    issues = []
    if len(rows) != run[11]:
        issues.append(
            _issue(
                "ASSESSMENT_COUNT_MISMATCH",
                run_id,
                None,
                f"stored count {run[11]} differs from row count {len(rows)}",
            )
        )
    for row in rows:
        issues.extend(_assessment_issues(row, run))
    try:
        batch = json.loads(run[12])
    except (json.JSONDecodeError, TypeError):
        batch = None
        issues.append(
            _issue(
                "BATCH_PAYLOAD_INVALID_JSON",
                run_id,
                None,
                "batch payload is invalid JSON",
            )
        )
    if isinstance(batch, dict):
        if canonical_json(batch) != run[12]:
            issues.append(
                _issue(
                    "BATCH_PAYLOAD_NOT_CANONICAL",
                    run_id,
                    None,
                    "batch payload is not canonical JSON",
                )
            )
        expected = {
            "matching_engine_version": run[4],
            "matching_rules_version": run[5],
            "semantic_percentile_version": run[6],
            "corpus_fingerprint": run[7],
            "tfidf_model_fingerprint": run[8],
            "assessment_count": run[11],
            "assessment_fingerprints": sorted(row[4] for row in rows),
        }
        if batch != expected:
            issues.append(
                _issue(
                    "BATCH_CONTENT_MISMATCH",
                    run_id,
                    None,
                    "batch payload differs from run and assessment rows",
                )
            )
        if _digest(batch) != run[9]:
            issues.append(
                _issue(
                    "BATCH_FINGERPRINT_MISMATCH",
                    run_id,
                    None,
                    "batch fingerprint differs from payload",
                )
            )
    elif batch is not None:
        issues.append(
            _issue(
                "BATCH_PAYLOAD_INVALID_JSON",
                run_id,
                None,
                "batch payload is not an object",
            )
        )
    assessments = tuple((row[0], row[4]) for row in rows)
    calculated_run = matching_run_fingerprint(
        profile_id=run[1],
        persistence_version=run[2],
        selection_version=run[3],
        matching_engine_version=run[4],
        matching_rules_version=run[5],
        semantic_percentile_version=run[6],
        corpus_fingerprint=run[7],
        tfidf_model_fingerprint=run[8],
        batch_fingerprint=run[9],
        assessments=assessments,
    )
    if calculated_run != run[10]:
        issues.append(
            _issue(
                "RUN_FINGERPRINT_MISMATCH",
                run_id,
                None,
                "run fingerprint differs from stored run content",
            )
        )
    ordered = tuple(sorted(issues))
    return MatchingRunAuditResult(
        run_id, run[10], run[9], assessments, len(rows), not ordered, ordered
    )


def audit_matching_profile_history(
    connection: sqlite3.Connection, profile_id: int
) -> MatchingPersistenceAuditReport:
    if (
        not isinstance(profile_id, int)
        or isinstance(profile_id, bool)
        or profile_id <= 0
    ):
        raise MatchingPersistenceAuditError("profile_id must be a positive integer")
    try:
        if (
            connection.execute(
                "SELECT 1 FROM profiles WHERE id=?", (profile_id,)
            ).fetchone()
            is None
        ):
            raise MatchingPersistenceAuditError(f"profile {profile_id} does not exist")
        state = connection.execute(
            "SELECT state,current_run_id FROM matching_profile_state WHERE profile_id=?",
            (profile_id,),
        ).fetchone()
        status, current_run_id = ("NOT_SYNCED", None) if state is None else state
        runs_raw = connection.execute(
            """SELECT id,profile_id,persistence_version,selection_version,matching_engine_version,
            matching_rules_version,semantic_percentile_version,corpus_fingerprint,
            tfidf_model_fingerprint,batch_fingerprint,run_fingerprint,assessment_count,
            batch_payload_json FROM matching_runs WHERE profile_id=? ORDER BY id DESC""",
            (profile_id,),
        ).fetchall()
    except sqlite3.Error as error:
        raise MatchingPersistenceAuditError(
            f"matching persistence query failed: {error}"
        ) from error
    issues = []
    run_ids = {row[0] for row in runs_raw}
    if (
        (status == "EMPTY" and current_run_id is not None)
        or (status == "READY" and current_run_id not in run_ids)
        or status not in {"NOT_SYNCED", "EMPTY", "READY"}
    ):
        issues.append(
            _issue(
                "STATE_RUN_MISMATCH",
                current_run_id,
                None,
                "profile state does not reference a run owned by the profile",
            )
        )
    runs = tuple(_audit_run(connection, row) for row in runs_raw)
    issues.extend(issue for run in runs for issue in run.issues)
    ordered = tuple(sorted(issues))
    stable = dict(
        audit_version=MATCHING_PERSISTENCE_AUDIT_VERSION,
        profile_id=profile_id,
        status=status,
        current_run_id=current_run_id,
        runs=[
            {
                "run_id": run.run_id,
                "run_fingerprint": run.run_fingerprint,
                "batch_fingerprint": run.batch_fingerprint,
                "assessments": list(run.assessment_fingerprints),
            }
            for run in runs
        ],
        issues=[
            {
                "code": item.code,
                "run_id": item.run_id,
                "opportunity_id": item.opportunity_id,
                "detail": item.detail,
            }
            for item in ordered
        ],
    )
    fingerprint = matching_persistence_audit_fingerprint(**stable)
    return MatchingPersistenceAuditReport(
        MATCHING_PERSISTENCE_AUDIT_VERSION,
        profile_id,
        status,
        current_run_id,
        len(runs),
        len(runs),
        sum(run.assessment_count for run in runs),
        not ordered,
        ordered,
        runs,
        fingerprint,
    )
