"""Phase 11.2A: read-only evidence about the persisted operational state.

This module answers one question about an existing SQLite file: *is the state
that is already stored intact and coherent, according to the owners that stored
it?* It never asks whether that state is fresh against today's sources, profile
or geography, and it never produces a decision of its own.

Every business statement below is quoted from an official owner:

    Matching        persistence audit + current read model   (Phase 4)
    Recommendation  persistence audit + current read model   (Phase 9, ranking authority)
    Eligibility     the Phase 3.6 audit of stored decisions
    Priority        current audit + current read model       (auxiliary evidence)
    Portfolio       current audit + current read model       (auxiliary evidence)

Nothing is synchronized, recomputed, classified, resolved, repaired, migrated or
written. The file is opened only through the project's `mode=ro` helper, the
connection is confirmed `query_only`, every read happens inside one deferred
`BEGIN` snapshot released with `ROLLBACK`, and the file and its possible SQLite
side files are fingerprinted before opening and again after closing. A single
changed byte, size, timestamp or side-file appearance fails the evidence.

The evidence is JSON-compatible and deterministic: no clock, no absolute path,
no exception text, no free-text owner message, and no synthetic score. Each
check keeps its own status; the only aggregate is PASS when every critical
check passed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from services.collector.database.connection import connect_readonly_database
from services.collector.matching.persistence_audit import (
    MatchingPersistenceAuditError,
    audit_matching_profile_history,
)
from services.collector.matching.read_model import MatchingReadError, read_current_matching
from services.eligibility.audit import audit_eligibility
from services.portfolio.audit import audit_current_portfolio
from services.portfolio.read_model import PortfolioReadError, read_current_portfolio
from services.priority.audit import audit_current_priority
from services.priority.read_model import PriorityReadError, read_current_priority
from services.recommendation.persistence_audit import (
    RecommendationPersistenceAuditError,
    audit_recommendation_profile_history,
)
from services.recommendation.read_model import (
    RecommendationReadError,
    read_current_recommendation,
)


SCHEMA_VERSION = "phase11.2a-operational-state-v1"

#: Possible SQLite companions of a database file. Absence is valid; what matters
#: is that validation leaves each exactly as it found it.
SIDE_FILE_SUFFIXES = ("-wal", "-shm", "-journal")

PASS = "PASS"
FAIL = "FAIL"
UNAVAILABLE = "UNAVAILABLE"

MATCH = "MATCH"
MISMATCH = "MISMATCH"
NOT_APPLICABLE = "NOT_APPLICABLE"

CHECK_ORDER = (
    "SQLITE_QUERY_ONLY",
    "SQLITE_INTEGRITY",
    "SQLITE_FOREIGN_KEYS",
    "MATCHING_AUDIT",
    "RECOMMENDATION_AUDIT",
    "RECOMMENDATION_READY",
    "CURRENT_MATCHING_RECOMMENDATION_CHAIN",
    "ELIGIBILITY_AUDIT",
    "PRIORITY_AUDIT",
    "PORTFOLIO_AUDIT",
    "DATABASE_UNCHANGED",
)

#: How many `integrity_check` lines are quoted. The count is always reported.
INTEGRITY_LINES_REPORTED = 20

_HASH_CHUNK = 1024 * 1024
_PROFILE_ID_TEXT = re.compile(r"[1-9][0-9]*")


class OperationalStateError(RuntimeError):
    """Validation could not be executed safely. `code` is a stable public value."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# --------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------


def require_profile_id(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise OperationalStateError("INVALID_PROFILE_ID")
    return value


def parse_profile_id(raw: object) -> int:
    """A profile id from configuration text: digits only, no sign, no padding."""
    if not isinstance(raw, str) or _PROFILE_ID_TEXT.fullmatch(raw) is None:
        raise OperationalStateError("INVALID_PROFILE_ID")
    return require_profile_id(int(raw))


# --------------------------------------------------------------------------
# filesystem fingerprint
# --------------------------------------------------------------------------


def fingerprint_file(path: Path) -> dict[str, Any]:
    """Existence, size, nanosecond mtime and SHA-256 of one file; never creates it."""
    if not path.exists():
        return {"exists": False}
    if not path.is_file():
        return {"exists": True, "is_file": False}
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    after = path.stat()
    return {
        "exists": True,
        "is_file": True,
        "size": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "sha256": digest.hexdigest(),
        # A file that changed while it was being hashed is not a stable identity.
        "stable_during_hash": (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
    }


def fingerprint_database(path: Path) -> dict[str, Any]:
    return {
        "main": fingerprint_file(path),
        "side_files": {suffix: fingerprint_file(Path(f"{path}{suffix}")) for suffix in SIDE_FILE_SUFFIXES},
    }


def fingerprint_is_stable(fingerprint: dict[str, Any]) -> bool:
    """Whether a database fingerprint is a trustworthy identity at all.

    The main file must exist, be a regular file and not have changed while it
    was hashed. A side file may be absent; if its path exists it must be a
    regular file that was stable while hashed. A path that exists but is not a
    regular file is never read as "absent".
    """
    main = fingerprint.get("main", {})
    if not (main.get("exists") is True and main.get("is_file") is True and main.get("stable_during_hash") is True):
        return False
    side_files = fingerprint.get("side_files", {})
    if set(side_files) != set(SIDE_FILE_SUFFIXES):
        return False
    for side in side_files.values():
        if side.get("exists") is False:
            continue
        if not (side.get("exists") is True and side.get("is_file") is True and side.get("stable_during_hash") is True):
            return False
    return True


def database_unchanged(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Both fingerprints must be stable, and identical."""
    before_stable = fingerprint_is_stable(before)
    after_stable = fingerprint_is_stable(after)
    identical = before == after
    return {
        "before_stable": before_stable,
        "after_stable": after_stable,
        "identical": identical,
        "unchanged": before_stable and after_stable and identical,
    }


# --------------------------------------------------------------------------
# official owners, quoted
# --------------------------------------------------------------------------


def _call(owner: Callable[..., Any], errors: tuple[type[BaseException], ...], *arguments: Any) -> tuple[Any, str | None]:
    """Run one owner. Its declared refusal becomes an error *code*, never its text."""
    try:
        return owner(*arguments), None
    except errors as error:
        return None, type(error).__name__


def _value(item: Any) -> Any:
    """An enum member as its persisted value; anything else, including None, as is."""
    return getattr(item, "value", item)


def _unavailable(error_code: str | None) -> dict[str, Any]:
    return {"available": False, "error_code": error_code}


def matching_evidence(connection: sqlite3.Connection, profile_id: int) -> dict[str, Any]:
    audit, audit_error = _call(
        audit_matching_profile_history, (MatchingPersistenceAuditError, sqlite3.Error), connection, profile_id
    )
    current, read_error = _call(read_current_matching, (MatchingReadError, sqlite3.Error), connection, profile_id)
    audit_part = _unavailable(audit_error) if audit is None else {
        "available": True,
        "error_code": None,
        "audit_version": audit.audit_version,
        "status": audit.status,
        "current_run_id": audit.current_run_id,
        "run_count": audit.run_count,
        "audited_run_count": audit.audited_run_count,
        "assessment_count": audit.assessment_count,
        "ok": audit.ok,
        "issues": [
            {"code": issue.code, "run_id": issue.run_id, "opportunity_id": issue.opportunity_id}
            for issue in audit.issues
        ],
        "audit_fingerprint": audit.audit_fingerprint,
    }
    read_part = _unavailable(read_error) if current is None else {
        "available": True,
        "error_code": None,
        "status": current.status,
        "current_run_id": current.current_run_id,
        "history_count": current.history_count,
        "persistence_version": current.persistence_version,
        "selection_version": current.selection_version,
        "current_run": None if current.current_run is None else {
            "run_id": current.current_run.run_id,
            "run_fingerprint": current.current_run.run_fingerprint,
            "batch_fingerprint": current.current_run.batch_fingerprint,
            "assessment_count": current.current_run.assessment_count,
        },
    }
    return {"audit": audit_part, "current": read_part}


def recommendation_evidence(connection: sqlite3.Connection, profile_id: int) -> dict[str, Any]:
    audit, audit_error = _call(
        audit_recommendation_profile_history, (RecommendationPersistenceAuditError, sqlite3.Error), connection, profile_id
    )
    current, read_error = _call(
        read_current_recommendation, (RecommendationReadError, sqlite3.Error), connection, profile_id
    )
    audit_part = _unavailable(audit_error) if audit is None else {
        "available": True,
        "error_code": None,
        "audit_version": audit.audit_version,
        "status": audit.status,
        "current_run_id": audit.current_run_id,
        "run_count": audit.run_count,
        "audited_run_count": audit.audited_run_count,
        "assessment_count": audit.assessment_count,
        "ok": audit.ok,
        "issues": [
            {"scope": issue.scope, "code": issue.code, "run_id": issue.run_id, "opportunity_id": issue.opportunity_id}
            for issue in audit.issues
        ],
        "audit_fingerprint": audit.audit_fingerprint,
    }
    read_part = _unavailable(read_error) if current is None else {
        "available": True,
        "error_code": None,
        "status": current.status,
        "current_run_id": current.current_run_id,
        "history_count": current.history_count,
        "persistence_version": current.persistence_version,
        "input_assembly_version": current.input_assembly_version,
        "readiness_issues": [
            {"code": _value(issue.code), "opportunity_id": issue.opportunity_id}
            for issue in current.readiness_issues
        ],
        "current_run": None if current.current_run is None else {
            "run_id": current.current_run.run_id,
            "source_matching_run_id": current.current_run.source_matching_run_id,
            "source_matching_run_fingerprint": current.current_run.source_matching_run_fingerprint,
            "run_fingerprint": current.current_run.run_fingerprint,
            "batch_fingerprint": current.current_run.batch_fingerprint,
            "assessment_count": current.current_run.assessment_count,
            "recommendation_engine_version": current.current_run.recommendation_engine_version,
            "recommendation_rules_version": current.current_run.recommendation_rules_version,
        },
    }
    return {"ranking_authority": True, "audit": audit_part, "current": read_part}


def eligibility_evidence(connection: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    audit, error = _call(audit_eligibility, (sqlite3.Error,), connection, user_id)
    if audit is None:
        return _unavailable(error)
    # Counts and stable reason vocabularies only; no reason sentence is quoted.
    return {
        "available": True,
        "error_code": None,
        "decisions": audit.decisions,
        "verdicts": dict(sorted(audit.verdicts.items())),
        "rule_statuses": dict(sorted(audit.rule_statuses.items())),
        "blocker_dimensions": dict(sorted(audit.blocker_dimensions.items())),
        "unknown_dimensions": dict(sorted(audit.unknown_dimensions.items())),
        "invariant_violations": dict(sorted(audit.invariant_violations.items())),
    }


def _auxiliary_issues(issues: Any) -> list[dict[str, Any]]:
    return [
        {"code": _value(issue.code), "run_id": issue.run_id, "opportunity_id": issue.opportunity_id}
        for issue in issues
    ]


def priority_evidence(connection: sqlite3.Connection, profile_id: int) -> dict[str, Any]:
    audit, audit_error = _call(audit_current_priority, (PriorityReadError, sqlite3.Error), connection, profile_id)
    current, read_error = _call(read_current_priority, (PriorityReadError, sqlite3.Error), connection, profile_id)
    audit_part = _unavailable(audit_error) if audit is None else {
        "available": True,
        "error_code": None,
        "status": _value(audit.status),
        "current_run_id": audit.current_run_id,
        "history_count": audit.history_count,
        "run_audit_status": None if audit.run_audit is None else _value(audit.run_audit.status),
        "issues": _auxiliary_issues(audit.issues),
    }
    read_part = _unavailable(read_error) if current is None else {
        "available": True,
        "error_code": None,
        "status": _value(current.status),
        "current_run_id": current.current_run_id,
        "history_count": current.history_count,
        "current_run": None if current.current_run is None else {
            "run_id": current.current_run.run_id,
            "matching_run_id": current.current_run.matching_run_id,
            "matching_run_fingerprint": current.current_run.matching_run_fingerprint,
            "run_fingerprint": current.current_run.run_fingerprint,
            "assessment_count": current.current_run.assessment_count,
            "evaluation_date": current.current_run.evaluation_date.isoformat(),
        },
    }
    return {"role": "AUXILIARY_NOT_RANKING_AUTHORITY", "audit": audit_part, "current": read_part}


def portfolio_evidence(connection: sqlite3.Connection, profile_id: int) -> dict[str, Any]:
    audit, audit_error = _call(audit_current_portfolio, (PortfolioReadError, sqlite3.Error), connection, profile_id)
    current, read_error = _call(read_current_portfolio, (PortfolioReadError, sqlite3.Error), connection, profile_id)
    audit_part = _unavailable(audit_error) if audit is None else {
        "available": True,
        "error_code": None,
        "status": _value(audit.status),
        "current_run_id": audit.current_run_id,
        "history_count": audit.history_count,
        "run_audit_status": None if audit.run_audit is None else _value(audit.run_audit.status),
        "issues": _auxiliary_issues(audit.issues),
    }
    read_part = _unavailable(read_error) if current is None else {
        "available": True,
        "error_code": None,
        "status": _value(current.status),
        "current_run_id": current.current_run_id,
        "history_count": current.history_count,
        "current_run": None if current.current_run is None else {
            "run_id": current.current_run.run_id,
            "priority_run_id": current.current_run.priority_run_id,
            "matching_run_id": current.current_run.matching_run_id,
            "run_fingerprint": current.current_run.run_fingerprint,
            "assessment_count": current.current_run.assessment_count,
            "included_count": current.current_run.included_count,
            "excluded_count": current.current_run.excluded_count,
        },
    }
    return {"role": "AUXILIARY_NOT_RANKING_AUTHORITY", "audit": audit_part, "current": read_part}


# --------------------------------------------------------------------------
# current Matching -> Recommendation chain
# --------------------------------------------------------------------------


def _compare(left: Any, right: Any) -> str:
    if left is None or right is None:
        return UNAVAILABLE
    return MATCH if left == right else MISMATCH


def compare_current_chain(matching: dict[str, Any], recommendation: dict[str, Any]) -> dict[str, Any]:
    """Does the current Recommendation cite the current Matching run?

    Only persisted identities exposed by the two read models are compared. A
    MATCH says the stored pointers agree; it says nothing about whether that
    Matching run is fresh against today's opportunities, profile or geography.
    """
    recommendation_run = recommendation["current"].get("current_run") if recommendation["current"]["available"] else None
    matching_run = matching["current"].get("current_run") if matching["current"]["available"] else None
    if recommendation["current"]["available"] and recommendation["current"]["status"] != "READY":
        run_ids = fingerprints = NOT_APPLICABLE
    else:
        run_ids = _compare(
            None if recommendation_run is None else recommendation_run["source_matching_run_id"],
            None if matching_run is None else matching_run["run_id"],
        )
        fingerprints = _compare(
            None if recommendation_run is None else recommendation_run["source_matching_run_fingerprint"],
            None if matching_run is None else matching_run["run_fingerprint"],
        )
    return {
        "recommendation_source_matching_run_id": None if recommendation_run is None else recommendation_run["source_matching_run_id"],
        "matching_current_run_id": None if matching_run is None else matching_run["run_id"],
        "run_id_comparison": run_ids,
        "run_fingerprint_comparison": fingerprints,
        "freshness_claim": "NOT_ASSESSED",
    }


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------


def _check(name: str, status: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "critical": True, "status": status, "evidence": evidence}


def _auxiliary_check(name: str, component: dict[str, Any]) -> dict[str, Any]:
    audit, current = component["audit"], component["current"]
    if not audit["available"]:
        return _check(name, UNAVAILABLE, {"audit_error_code": audit["error_code"]})
    if audit["status"] == "CORRUPT":
        return _check(name, FAIL, {"audit_status": audit["status"]})
    if not current["available"]:
        return _check(name, UNAVAILABLE, {"audit_status": audit["status"], "read_error_code": current["error_code"]})
    agrees = (audit["status"], audit["current_run_id"]) == (current["status"], current["current_run_id"])
    return _check(
        name,
        PASS if agrees else FAIL,
        {"audit_status": audit["status"], "read_status": current["status"], "audit_agrees_with_read_model": agrees},
    )


def build_checks(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    sqlite_part = evidence["database"]["sqlite"]
    matching = evidence["matching"]
    recommendation = evidence["recommendation"]
    chain = evidence["current_chain"]
    eligibility = evidence["eligibility"]
    checks = []

    checks.append(_check("SQLITE_QUERY_ONLY", PASS if sqlite_part["query_only"] == 1 else FAIL,
                         {"query_only": sqlite_part["query_only"]}))
    integrity_ok = sqlite_part["integrity_check"] == ["ok"]
    checks.append(_check("SQLITE_INTEGRITY", PASS if integrity_ok else FAIL,
                         {"line_count": sqlite_part["integrity_check_line_count"]}))
    fk_count = sqlite_part["foreign_key_violation_count"]
    checks.append(_check("SQLITE_FOREIGN_KEYS", PASS if fk_count == 0 else FAIL, {"violation_count": fk_count}))

    # Both Matching owners read the same persisted `matching_profile_state` in
    # the same NOT_SYNCED / EMPTY / READY vocabulary, so status and current run
    # are compared directly; no translation between vocabularies is made.
    audit, current = matching["audit"], matching["current"]
    audit_available, read_available = audit["available"], current["available"]
    matching_agrees = (
        audit_available
        and read_available
        and (audit["status"], audit["current_run_id"]) == (current["status"], current["current_run_id"])
    )
    checks.append(_check(
        "MATCHING_AUDIT",
        UNAVAILABLE if not (audit_available and read_available)
        else PASS if audit["ok"] and matching_agrees
        else FAIL,
        {
            "audit_available": audit_available,
            "read_available": read_available,
            "audit_ok": audit.get("ok"),
            "audit_status": audit.get("status"),
            "read_status": current.get("status"),
            "audit_current_run_id": audit.get("current_run_id"),
            "read_current_run_id": current.get("current_run_id"),
            "audit_agrees_with_read_model": matching_agrees,
        },
    ))

    audit, current = recommendation["audit"], recommendation["current"]
    if not audit["available"] or not current["available"]:
        checks.append(_check("RECOMMENDATION_AUDIT", UNAVAILABLE,
                             {"audit_available": audit["available"], "read_available": current["available"]}))
    else:
        agrees = (audit["status"], audit["current_run_id"]) == (current["status"], current["current_run_id"])
        checks.append(_check(
            "RECOMMENDATION_AUDIT",
            PASS if audit["ok"] and agrees else FAIL,
            {"ok": audit["ok"], "audit_agrees_with_read_model": agrees},
        ))

    if not current["available"]:
        checks.append(_check("RECOMMENDATION_READY", UNAVAILABLE, {"read_error_code": current["error_code"]}))
    else:
        ready = current["status"] == "READY" and current["current_run"] is not None
        checks.append(_check("RECOMMENDATION_READY", PASS if ready else FAIL,
                             {"status": current["status"], "current_run_id": current["current_run_id"]}))

    comparisons = (chain["run_id_comparison"], chain["run_fingerprint_comparison"])
    chain_status = (
        FAIL if MISMATCH in comparisons
        else PASS if comparisons == (MATCH, MATCH)
        else UNAVAILABLE
    )
    checks.append(_check("CURRENT_MATCHING_RECOMMENDATION_CHAIN", chain_status, {
        "run_id_comparison": chain["run_id_comparison"],
        "run_fingerprint_comparison": chain["run_fingerprint_comparison"],
        "freshness_claim": chain["freshness_claim"],
    }))

    if not eligibility["available"]:
        checks.append(_check("ELIGIBILITY_AUDIT", UNAVAILABLE, {"error_code": eligibility["error_code"]}))
    else:
        violated = {name: count for name, count in eligibility["invariant_violations"].items() if count}
        checks.append(_check("ELIGIBILITY_AUDIT", FAIL if violated else PASS, {"violated_invariants": dict(sorted(violated.items()))}))

    checks.append(_auxiliary_check("PRIORITY_AUDIT", evidence["priority"]))
    checks.append(_auxiliary_check("PORTFOLIO_AUDIT", evidence["portfolio"]))

    database = evidence["database"]
    checks.append(_check("DATABASE_UNCHANGED", PASS if database["unchanged"] else FAIL, {
        "before_stable": database["before_stable"],
        "after_stable": database["after_stable"],
        "identical": database["identical"],
        "unchanged": database["unchanged"],
    }))
    return checks


# --------------------------------------------------------------------------
# the validation
# --------------------------------------------------------------------------


def _sqlite_health(connection: sqlite3.Connection) -> dict[str, Any]:
    integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()]
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    by_table: dict[str, int] = {}
    for row in violations:
        by_table[str(row[0])] = by_table.get(str(row[0]), 0) + 1
    return {
        "integrity_check": integrity[:INTEGRITY_LINES_REPORTED],
        "integrity_check_line_count": len(integrity),
        "foreign_key_violation_count": len(violations),
        "foreign_key_violations_by_table": dict(sorted(by_table.items())),
    }


def _read_evidence(connection: sqlite3.Connection, profile_id: int) -> dict[str, Any]:
    """Everything read inside the one snapshot the caller holds open."""
    owner = connection.execute("SELECT user_id FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    if owner is None:
        raise OperationalStateError("PROFILE_NOT_FOUND")
    matching = matching_evidence(connection, profile_id)
    recommendation = recommendation_evidence(connection, profile_id)
    return {
        "sqlite": _sqlite_health(connection),
        "matching": matching,
        "recommendation": recommendation,
        "current_chain": compare_current_chain(matching, recommendation),
        "eligibility": eligibility_evidence(connection, int(owner[0])),
        "priority": priority_evidence(connection, profile_id),
        "portfolio": portfolio_evidence(connection, profile_id),
    }


def _release(connection: sqlite3.Connection) -> bool:
    """End the snapshot with ROLLBACK if one is open, then close. True when both succeeded."""
    released = True
    try:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
    except sqlite3.Error:
        released = False
    try:
        connection.close()
    except sqlite3.Error:
        released = False
    return released


@dataclass(frozen=True)
class _ReadOutcome:
    read: dict[str, Any] | None
    query_only: int | None
    #: The exception the open/read phase ended with, kept to be raised only after
    #: the after-fingerprint: a safe `OperationalStateError`, or an unexpected
    #: `Exception` that will be re-raised as the very same object.
    pending: Exception | None
    release_failed: bool


def _read_once(path: Path, profile_id: int) -> _ReadOutcome:
    """Open, confirm, snapshot, read, then always release and close.

    Nothing that is an `Exception` escapes from here: it is carried back so the
    caller can fingerprint the file first. `BaseException`s such as
    `KeyboardInterrupt` are not caught; the connection is still released and
    closed on the way out.
    """
    connection = None
    pending: Exception | None = None
    read: dict[str, Any] | None = None
    query_only_value: int | None = None
    released = True
    try:
        try:
            connection = connect_readonly_database(path)
            connection.execute("PRAGMA query_only = ON")
            query_only = connection.execute("PRAGMA query_only").fetchone()
        except sqlite3.Error:
            raise OperationalStateError("DATABASE_OPEN_FAILED") from None
        if query_only is None or query_only[0] != 1:
            raise OperationalStateError("QUERY_ONLY_NOT_CONFIRMED")
        if connection.in_transaction:
            raise OperationalStateError("UNEXPECTED_OPEN_TRANSACTION")
        try:
            connection.execute("BEGIN")
        except sqlite3.Error:
            raise OperationalStateError("SNAPSHOT_OPEN_FAILED") from None
        try:
            read = _read_evidence(connection, profile_id)
        except sqlite3.Error:
            raise OperationalStateError("SNAPSHOT_READ_FAILED") from None
        query_only_value = int(query_only[0])
    except Exception as error:  # carried, never translated
        pending, read, query_only_value = error, None, None
    finally:
        if connection is not None:
            released = _release(connection)
    return _ReadOutcome(read, query_only_value, pending, not released)


def validate_operational_state(database_path: str | os.PathLike[str], profile_id: int) -> dict[str, Any]:
    """Evidence about one existing SQLite file and one profile, strictly read-only.

    Raises `OperationalStateError` when validation cannot run safely: an invalid
    profile id, a missing or non-file database, an unconfirmed `query_only`, a
    snapshot that cannot be opened or released, or a profile that does not
    exist. Anything the owners report about the stored state becomes evidence
    instead.

    Once the file has been fingerprinted, any failure — safe or unexpected —
    first releases the snapshot and closes the connection, then the file is
    fingerprinted again, and only then is something raised, in this order:

    1. `DATABASE_CHANGED_DURING_VALIDATION` if the file did not stay stably
       identical;
    2. `SNAPSHOT_RELEASE_FAILED` if the ROLLBACK or close failed, because the
       snapshot can then no longer be proven released;
    3. the original safe `OperationalStateError`;
    4. the original unexpected exception, the same object, untranslated.
    """
    profile_id = require_profile_id(profile_id)
    path = Path(database_path)
    if not path.exists():
        raise OperationalStateError("DATABASE_NOT_FOUND")
    if not path.is_file():
        raise OperationalStateError("DATABASE_NOT_A_FILE")

    before = fingerprint_database(path)
    outcome = _read_once(path, profile_id)
    after = fingerprint_database(path)
    database = database_unchanged(before, after)
    if outcome.pending is not None or outcome.release_failed:
        if not database["unchanged"]:
            raise OperationalStateError("DATABASE_CHANGED_DURING_VALIDATION") from None
        if outcome.release_failed:
            raise OperationalStateError("SNAPSHOT_RELEASE_FAILED") from None
        raise outcome.pending
    read = outcome.read
    read["sqlite"]["query_only"] = outcome.query_only

    evidence = {
        "schema_version": SCHEMA_VERSION,
        "profile_id": profile_id,
        "database": {
            "file_name": path.name,
            "before": before,
            "after": after,
            **database,
            "sqlite": read["sqlite"],
        },
        "matching": read["matching"],
        "recommendation": read["recommendation"],
        "current_chain": read["current_chain"],
        "eligibility": read["eligibility"],
        "priority": read["priority"],
        "portfolio": read["portfolio"],
    }
    checks = build_checks(evidence)
    evidence["checks"] = checks
    evidence["result"] = PASS if all(check["status"] == PASS for check in checks if check["critical"]) else FAIL
    return evidence


def serialize_evidence(evidence: dict[str, Any]) -> str:
    """Deterministic JSON: sorted keys, fixed separators, stable check order."""
    return json.dumps(evidence, sort_keys=True, indent=2, ensure_ascii=False)
