"""Explicit, transactional and reversible merges of human-confirmed duplicates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import sqlite3
from typing import Callable

from services.collector.deduplication.decisions import CONFIRMED_DUPLICATE, canonicalize_pair

APPLIED = "APPLIED"
ROLLED_BACK = "ROLLED_BACK"
MERGED_DUPLICATE_STATUS = "merged_duplicate"
FILL_ONLY_FIELDS = (
    "opportunity_type", "employment_type", "location", "country", "remote_type",
    "description", "published_at", "deadline", "application_url", "canonical_url",
)


class MergeError(ValueError):
    """A safe merge or rollback precondition was not met."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class MergePlan:
    decision_id: int | None
    decision_status: str | None
    canonical_opportunity_id: int
    merged_opportunity_id: int | None
    source_moves_count: int
    sources_to_move: tuple[dict[str, object], ...]
    fields_to_fill: tuple[str, ...]
    observation_conflicts: tuple[dict[str, object], ...]
    warnings: tuple[str, ...]
    merge_allowed: bool
    error_code: str | None = None
    existing_merge_id: int | None = None


@dataclass(frozen=True)
class RollbackPlan:
    merge_id: int
    canonical_opportunity_id: int
    merged_opportunity_id: int
    source_moves_count: int
    fields_to_restore: tuple[str, ...]
    rollback_allowed: bool
    error_code: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _missing(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _opportunity(connection: sqlite3.Connection, opportunity_id: int) -> dict[str, object] | None:
    cursor = connection.execute("SELECT * FROM opportunities WHERE id = ?", (opportunity_id,))
    row = cursor.fetchone()
    return None if row is None else dict(zip((item[0] for item in cursor.description), row))


def _fail_plan(canonical: int, code: str, message: str, **values: object) -> MergePlan:
    warnings = (message,)
    return MergePlan(values.get("decision_id"), values.get("decision_status"), canonical,
                     values.get("merged_id"), 0, (), (), (), warnings, False, code,
                     values.get("existing_merge_id"))


def preflight_merge(connection: sqlite3.Connection, first_id: int, second_id: int, canonical_id: int) -> MergePlan:
    """Read-only validation and exact mutation plan. It never migrates or writes."""
    try:
        first, second = canonicalize_pair(first_id, second_id)
    except ValueError as error:
        return _fail_plan(canonical_id, "INVALID_PAIR", str(error))
    decision = connection.execute(
        "SELECT id, status FROM deduplication_decisions WHERE opportunity_a_id=? AND opportunity_b_id=?",
        (first, second),
    ).fetchone()
    if decision is None:
        return _fail_plan(canonical_id, "DECISION_NOT_FOUND", "no decision exists for this pair")
    decision_id, decision_status = int(decision[0]), str(decision[1])
    if canonical_id not in (first, second):
        return _fail_plan(canonical_id, "CANONICAL_OUTSIDE_PAIR", "canonical must be one member of the decision", decision_id=decision_id, decision_status=decision_status)
    merged_id = second if canonical_id == first else first
    if decision_status != CONFIRMED_DUPLICATE:
        return _fail_plan(canonical_id, "DECISION_NOT_CONFIRMED", "only CONFIRMED_DUPLICATE is mergeable", decision_id=decision_id, decision_status=decision_status, merged_id=merged_id)
    canonical, merged = _opportunity(connection, canonical_id), _opportunity(connection, merged_id)
    if canonical is None or merged is None:
        return _fail_plan(canonical_id, "OPPORTUNITY_NOT_FOUND", "both opportunities must exist", decision_id=decision_id, decision_status=decision_status, merged_id=merged_id)
    active = connection.execute(
        "SELECT id, canonical_opportunity_id, merged_opportunity_id FROM deduplication_merges WHERE status='APPLIED' AND decision_id=?",
        (decision_id,),
    ).fetchone()
    if active is not None:
        if int(active[1]) == canonical_id:
            return _fail_plan(canonical_id, "ALREADY_APPLIED", "this merge is already applied", decision_id=decision_id, decision_status=decision_status, merged_id=merged_id, existing_merge_id=int(active[0]))
        return _fail_plan(canonical_id, "CANONICAL_DIRECTION_CONFLICT", "the pair is already applied with the opposite canonical", decision_id=decision_id, decision_status=decision_status, merged_id=merged_id, existing_merge_id=int(active[0]))
    if connection.execute("SELECT 1 FROM deduplication_merges WHERE status='APPLIED' AND merged_opportunity_id=?", (canonical_id,)).fetchone():
        return _fail_plan(canonical_id, "CANONICAL_ALREADY_ABSORBED", "canonical is an absorbed opportunity", decision_id=decision_id, decision_status=decision_status, merged_id=merged_id)
    if connection.execute("SELECT 1 FROM deduplication_merges WHERE status='APPLIED' AND (canonical_opportunity_id=? OR merged_opportunity_id=?)", (merged_id, merged_id)).fetchone():
        return _fail_plan(canonical_id, "MERGED_IN_ACTIVE_MERGE", "merged opportunity participates in an active merge", decision_id=decision_id, decision_status=decision_status, merged_id=merged_id)
    if not bool(canonical["is_active"]) or canonical["status"] == MERGED_DUPLICATE_STATUS:
        return _fail_plan(canonical_id, "CANONICAL_INACTIVE", "canonical must be active and not a tombstone", decision_id=decision_id, decision_status=decision_status, merged_id=merged_id)
    if not bool(merged["is_active"]) or merged["status"] == MERGED_DUPLICATE_STATUS:
        return _fail_plan(canonical_id, "MERGED_INACTIVE", "merged opportunity must be active and not a tombstone", decision_id=decision_id, decision_status=decision_status, merged_id=merged_id)
    cursor = connection.execute("SELECT id, source_id, source_url, application_url, canonical_url, discovered_at, created_at FROM opportunity_sources WHERE opportunity_id=? ORDER BY id", (merged_id,))
    names = [item[0] for item in cursor.description]
    sources = tuple(dict(zip(names, row)) for row in cursor.fetchall())
    conflicts = tuple(source for source in sources if connection.execute(
        "SELECT 1 FROM opportunity_sources WHERE opportunity_id=? AND source_id=? AND source_url=?",
        (canonical_id, source["source_id"], source["source_url"]),
    ).fetchone())
    fields = tuple(field for field in FILL_ONLY_FIELDS if _missing(canonical[field]) and not _missing(merged[field]))
    return MergePlan(decision_id, decision_status, canonical_id, merged_id, len(sources), sources,
                     fields, conflicts, (), not conflicts,
                     "OBSERVATION_CONFLICT" if conflicts else None)


def apply_merge(connection: sqlite3.Connection, first_id: int, second_id: int, canonical_id: int, *, clock: Callable[[], str] = _now, after_move: Callable[[], None] | None = None) -> dict[str, object]:
    """Validate and apply one merge in one explicit SQLite transaction."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        plan = preflight_merge(connection, first_id, second_id, canonical_id)
        if not plan.merge_allowed:
            raise MergeError(plan.error_code or "MERGE_REFUSED", "; ".join(plan.warnings) or "observation conflict")
        assert plan.merged_opportunity_id is not None and plan.decision_id is not None
        canonical = _opportunity(connection, canonical_id)
        merged = _opportunity(connection, plan.merged_opportunity_id)
        assert canonical is not None and merged is not None
        before = {field: canonical[field] for field in plan.fields_to_fill}
        after = {field: merged[field] for field in plan.fields_to_fill}
        merged_before = {"status": merged["status"], "is_active": merged["is_active"]}
        timestamp = clock()
        cursor = connection.execute(
            """INSERT INTO deduplication_merges (decision_id, canonical_opportunity_id,
               merged_opportunity_id, status, canonical_before_json, canonical_after_json,
               merged_before_json, fields_filled_json, applied_at)
               VALUES (?, ?, ?, 'APPLIED', ?, ?, ?, ?, ?)""",
            (plan.decision_id, canonical_id, plan.merged_opportunity_id,
             json.dumps(before), json.dumps(after), json.dumps(merged_before),
             json.dumps(list(plan.fields_to_fill)), timestamp),
        )
        merge_id = int(cursor.lastrowid)
        for source in plan.sources_to_move:
            connection.execute("UPDATE opportunity_sources SET opportunity_id=? WHERE id=?", (canonical_id, source["id"]))
            connection.execute(
                """INSERT INTO deduplication_merge_source_moves
                   (merge_id, opportunity_source_id, from_opportunity_id, to_opportunity_id, moved_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (merge_id, source["id"], plan.merged_opportunity_id, canonical_id, timestamp),
            )
        if after_move is not None:
            after_move()
        if plan.fields_to_fill:
            assignments = ", ".join(f"{field} = ?" for field in plan.fields_to_fill)
            connection.execute(f"UPDATE opportunities SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE id=?", (*after.values(), canonical_id))
        connection.execute("UPDATE opportunities SET status=?, is_active=0, updated_at=CURRENT_TIMESTAMP WHERE id=?", (MERGED_DUPLICATE_STATUS, plan.merged_opportunity_id))
        connection.execute("COMMIT")
        return {"result": APPLIED, "merge_id": merge_id, **asdict(plan)}
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _merge_row(connection: sqlite3.Connection, merge_id: int) -> dict[str, object] | None:
    cursor = connection.execute("SELECT * FROM deduplication_merges WHERE id=?", (merge_id,))
    row = cursor.fetchone()
    return None if row is None else dict(zip((item[0] for item in cursor.description), row))


def preflight_rollback(connection: sqlite3.Connection, merge_id: int) -> RollbackPlan:
    merge = _merge_row(connection, merge_id)
    if merge is None:
        raise MergeError("MERGE_NOT_FOUND", "merge does not exist")
    canonical_id, merged_id = int(merge["canonical_opportunity_id"]), int(merge["merged_opportunity_id"])
    fields = tuple(json.loads(str(merge["fields_filled_json"])))
    count = int(connection.execute("SELECT COUNT(*) FROM deduplication_merge_source_moves WHERE merge_id=?", (merge_id,)).fetchone()[0])
    if merge["status"] != APPLIED:
        return RollbackPlan(merge_id, canonical_id, merged_id, count, fields, False, "MERGE_NOT_APPLIED")
    if connection.execute("SELECT 1 FROM deduplication_merges WHERE status='APPLIED' AND canonical_opportunity_id=? AND id>?", (canonical_id, merge_id)).fetchone():
        return RollbackPlan(merge_id, canonical_id, merged_id, count, fields, False, "ROLLBACK_NOT_LIFO")
    canonical = _opportunity(connection, canonical_id)
    if canonical is None:
        return RollbackPlan(merge_id, canonical_id, merged_id, count, fields, False, "OPPORTUNITY_NOT_FOUND")
    expected = json.loads(str(merge["canonical_after_json"]))
    if any(canonical[field] != expected[field] for field in fields):
        return RollbackPlan(merge_id, canonical_id, merged_id, count, fields, False, "ROLLBACK_DATA_DRIFT")
    return RollbackPlan(merge_id, canonical_id, merged_id, count, fields, True)


def rollback_merge(connection: sqlite3.Connection, merge_id: int, *, clock: Callable[[], str] = _now, after_move: Callable[[], None] | None = None) -> dict[str, object]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        plan = preflight_rollback(connection, merge_id)
        if not plan.rollback_allowed:
            raise MergeError(plan.error_code or "ROLLBACK_REFUSED", "rollback safety check failed")
        merge = _merge_row(connection, merge_id)
        assert merge is not None
        moves = connection.execute("SELECT opportunity_source_id, from_opportunity_id, to_opportunity_id FROM deduplication_merge_source_moves WHERE merge_id=? ORDER BY id DESC", (merge_id,)).fetchall()
        for source_id, from_id, to_id in moves:
            cursor = connection.execute("UPDATE opportunity_sources SET opportunity_id=? WHERE id=? AND opportunity_id=?", (from_id, source_id, to_id))
            if cursor.rowcount != 1:
                raise MergeError("ROLLBACK_SOURCE_DRIFT", f"source observation {source_id} moved since merge")
        if after_move is not None:
            after_move()
        before = json.loads(str(merge["canonical_before_json"]))
        if plan.fields_to_restore:
            assignments = ", ".join(f"{field}=?" for field in plan.fields_to_restore)
            connection.execute(f"UPDATE opportunities SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE id=?", (*(before[field] for field in plan.fields_to_restore), plan.canonical_opportunity_id))
        merged_before = json.loads(str(merge["merged_before_json"]))
        connection.execute("UPDATE opportunities SET status=?, is_active=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (merged_before["status"], merged_before["is_active"], plan.merged_opportunity_id))
        timestamp = clock()
        connection.execute("UPDATE deduplication_merges SET status='ROLLED_BACK', rolled_back_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (timestamp, merge_id))
        connection.execute("COMMIT")
        return {"result": ROLLED_BACK, **asdict(plan)}
    except Exception:
        connection.execute("ROLLBACK")
        raise


def list_merges(connection: sqlite3.Connection) -> list[dict[str, object]]:
    cursor = connection.execute("SELECT id, decision_id, canonical_opportunity_id, merged_opportunity_id, status, applied_at, rolled_back_at FROM deduplication_merges ORDER BY id")
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]
