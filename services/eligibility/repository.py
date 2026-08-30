"""Transactional persistence for Phase 3.6 decisions.

Same style as every repository before it: plain SQLite, no ORM, no second
connection factory, no second migration runner, and one explicit `BEGIN
IMMEDIATE` / `COMMIT` / `ROLLBACK` around a whole decision.

Four rules are enforced here rather than trusted to callers.

* **Nothing upstream is ever written.** No `INSERT`, `UPDATE` or `DELETE`
  against `opportunities`, `users`, `profiles`, `profile_facts`, any
  `profile_*` projection, `opportunity_constraints` or any 3.5B table appears
  below, and a test reads this file to keep it so. Phase 3.6 is a reader of both
  sides and a writer of its own two tables;
* **`opportunities.eligibility_score` is not touched.** It is a column from an
  earlier design with no current writer, and a categorical verdict is not a
  number. Repurposing it would put a Phase 3.6 answer in a place nothing in
  Phase 3.6 documents;
* **a decision is replaced whole or not at all.** Writing one means deleting the
  previous decision and every rule result hanging off it, then inserting the new
  summary and every new result, inside one transaction. A summary whose counters
  describe results that were not written explains nothing, and a decision with
  half its reasons is worse than the stale one it replaced;
* **the counters are derived, never passed in.** They are computed from the rule
  results being written, in this module, so a summary cannot disagree with the
  rows beneath it. `0014` then re-checks the aggregation itself, so a wrong
  verdict is refused by SQLite even if it reaches here.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from services.eligibility.models import (
    Dimension,
    EligibilityDecision,
    GlobalStatus,
    ReasonCode,
    RequirementKind,
    RuleResult,
    RuleStatus,
)

__all__ = [
    "EligibilityRepositoryError",
    "StoredEligibility",
    "count_rule_statuses",
    "delete_eligibility",
    "read_eligibility",
    "read_rule_results",
    "store_eligibility",
    "stored_eligibility_signature",
]


class EligibilityRepositoryError(RuntimeError):
    """Raised when a decision cannot be read or written safely."""


def stored_eligibility_signature(
    connection: sqlite3.Connection, user_id: int, opportunity_id: int
) -> tuple[str, str] | None:
    """The `(fingerprint, engine_version)` already stored for this pair, or None.

    This is the whole idempotence check, and it reads the decision row rather
    than counting rule results — the same argument 3.5B's state table makes. A
    decision always has results, but a caller asking "has this been decided?"
    should not have to know that.
    """
    row = connection.execute(
        "SELECT input_fingerprint, engine_version FROM opportunity_eligibilities "
        "WHERE user_id = ? AND opportunity_id = ?",
        (user_id, opportunity_id),
    ).fetchone()
    return None if row is None else (str(row[0]), str(row[1]))


def delete_eligibility(
    connection: sqlite3.Connection, user_id: int, opportunity_id: int
) -> None:
    """Remove one pair's decision and its reasons. Touches no source table.

    Children first, so the order alone leaves no orphan even when foreign keys
    are off.
    """
    connection.execute(
        "DELETE FROM eligibility_rule_results WHERE eligibility_id IN "
        "(SELECT id FROM opportunity_eligibilities "
        " WHERE user_id = ? AND opportunity_id = ?)",
        (user_id, opportunity_id),
    )
    connection.execute(
        "DELETE FROM opportunity_eligibilities "
        "WHERE user_id = ? AND opportunity_id = ?",
        (user_id, opportunity_id),
    )


def count_rule_statuses(results: tuple[RuleResult, ...]) -> dict[str, int]:
    """The five tallies plus the blocking-unknown subset.

    Counts of outcomes, and nothing that could be read as a score. There is no
    total here to divide by on purpose: the size of a rule set depends on how
    many skills a posting listed, so a ratio over it would move when the posting
    got wordier.
    """
    counts = {
        "satisfied_count": 0,
        "violated_count": 0,
        "unknown_count": 0,
        "not_applicable_count": 0,
        "not_evaluated_count": 0,
        "blocking_unknown_count": 0,
    }
    names = {
        RuleStatus.SATISFIED: "satisfied_count",
        RuleStatus.VIOLATED: "violated_count",
        RuleStatus.UNKNOWN: "unknown_count",
        RuleStatus.NOT_APPLICABLE: "not_applicable_count",
        RuleStatus.NOT_EVALUATED: "not_evaluated_count",
    }
    for result in results:
        counts[names[result.status]] += 1
        if result.is_blocking and result.status is RuleStatus.UNKNOWN:
            counts["blocking_unknown_count"] += 1
    return counts


def _insert(
    connection: sqlite3.Connection,
    user_id: int,
    decision: EligibilityDecision,
    evaluated_at: str,
) -> int:
    counts = count_rule_statuses(decision.results)
    row = connection.execute(
        """INSERT INTO opportunity_eligibilities (
               user_id, opportunity_id, status, engine_version, input_fingerprint,
               satisfied_count, violated_count, unknown_count,
               not_applicable_count, not_evaluated_count, blocking_unknown_count,
               evaluated_at, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           RETURNING id""",
        (
            user_id,
            decision.opportunity_id,
            decision.status.value,
            decision.engine_version,
            decision.input_fingerprint,
            counts["satisfied_count"],
            counts["violated_count"],
            counts["unknown_count"],
            counts["not_applicable_count"],
            counts["not_evaluated_count"],
            counts["blocking_unknown_count"],
            evaluated_at,
            evaluated_at,
            evaluated_at,
        ),
    ).fetchone()
    if row is None:
        raise EligibilityRepositoryError("eligibility insert returned no row")
    eligibility_id = int(row[0])

    connection.executemany(
        """INSERT INTO eligibility_rule_results (
               eligibility_id, position, dimension, rule_code, status,
               is_blocking, requirement_kind, reason_code, explanation,
               requirement_ref, profile_ref, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                eligibility_id,
                position,
                result.dimension.value,
                result.rule_code,
                result.status.value,
                1 if result.is_blocking else 0,
                None
                if result.requirement_kind is None
                else result.requirement_kind.value,
                result.reason_code.value,
                result.explanation,
                result.requirement_ref,
                result.profile_ref,
                evaluated_at,
            )
            for position, result in enumerate(decision.results)
        ],
    )
    return eligibility_id


def store_eligibility(
    connection: sqlite3.Connection,
    user_id: int,
    decision: EligibilityDecision,
    *,
    evaluated_at: str | None = None,
    after_delete=None,
) -> int:
    """Replace one pair's decision, whole, in one transaction.

    Returns the stored decision's id. The caller supplies an already-evaluated
    decision, so nothing is computed here and nothing is decided here. A failure
    at any point — a CHECK the decision violates, a foreign key, an interruption
    — rolls back the delete along with the inserts, so the previous decision
    survives intact rather than being replaced by a partial one.

    `after_delete` is a test seam invoked once the old rows are gone and before
    the new ones are written; production callers leave it unset.
    """
    if not isinstance(decision, EligibilityDecision):
        raise EligibilityRepositoryError("decision must be an EligibilityDecision")
    timestamp = evaluated_at or datetime.now(UTC).isoformat(timespec="microseconds")
    connection.execute("BEGIN IMMEDIATE")
    try:
        if connection.execute(
            "SELECT 1 FROM users WHERE id = ?", (user_id,)
        ).fetchone() is None:
            raise EligibilityRepositoryError(f"user {user_id} does not exist")
        if connection.execute(
            "SELECT 1 FROM opportunities WHERE id = ?", (decision.opportunity_id,)
        ).fetchone() is None:
            raise EligibilityRepositoryError(
                f"opportunity {decision.opportunity_id} does not exist"
            )
        delete_eligibility(connection, user_id, decision.opportunity_id)
        if after_delete is not None:
            after_delete()
        eligibility_id = _insert(connection, user_id, decision, timestamp)
        connection.execute("COMMIT")
        return eligibility_id
    except BaseException:
        connection.execute("ROLLBACK")
        raise


class StoredEligibility:
    """One persisted decision, as it was read back. A plain record, no logic."""

    __slots__ = (
        "id",
        "user_id",
        "opportunity_id",
        "status",
        "engine_version",
        "input_fingerprint",
        "satisfied_count",
        "violated_count",
        "unknown_count",
        "not_applicable_count",
        "not_evaluated_count",
        "blocking_unknown_count",
        "evaluated_at",
        "created_at",
        "updated_at",
    )

    def __init__(self, row: tuple) -> None:
        (
            self.id,
            self.user_id,
            self.opportunity_id,
            status,
            self.engine_version,
            self.input_fingerprint,
            self.satisfied_count,
            self.violated_count,
            self.unknown_count,
            self.not_applicable_count,
            self.not_evaluated_count,
            self.blocking_unknown_count,
            self.evaluated_at,
            self.created_at,
            self.updated_at,
        ) = row
        self.status = GlobalStatus(str(status))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"StoredEligibility(user_id={self.user_id}, "
            f"opportunity_id={self.opportunity_id}, status={self.status})"
        )


_DECISION_COLUMNS = (
    "id, user_id, opportunity_id, status, engine_version, input_fingerprint, "
    "satisfied_count, violated_count, unknown_count, not_applicable_count, "
    "not_evaluated_count, blocking_unknown_count, evaluated_at, created_at, "
    "updated_at"
)


def read_eligibility(
    connection: sqlite3.Connection, user_id: int, opportunity_id: int
) -> StoredEligibility | None:
    """The stored decision for this pair, or None when there is none."""
    row = connection.execute(
        f"SELECT {_DECISION_COLUMNS} FROM opportunity_eligibilities "
        "WHERE user_id = ? AND opportunity_id = ?",
        (user_id, opportunity_id),
    ).fetchone()
    return None if row is None else StoredEligibility(row)


def read_rule_results(
    connection: sqlite3.Connection, eligibility_id: int
) -> tuple[RuleResult, ...]:
    """One decision's reasons, in the order the engine produced them."""
    rows = connection.execute(
        """SELECT dimension, rule_code, status, is_blocking, requirement_kind,
                  reason_code, explanation, requirement_ref, profile_ref
             FROM eligibility_rule_results
            WHERE eligibility_id = ?
            ORDER BY position""",
        (eligibility_id,),
    ).fetchall()
    return tuple(
        RuleResult(
            dimension=Dimension(str(row[0])),
            rule_code=str(row[1]),
            status=RuleStatus(str(row[2])),
            is_blocking=bool(row[3]),
            requirement_kind=(
                None if row[4] is None else RequirementKind(str(row[4]))
            ),
            reason_code=ReasonCode(str(row[5])),
            explanation=str(row[6]),
            requirement_ref=None if row[7] is None else str(row[7]),
            profile_ref=None if row[8] is None else str(row[8]),
        )
        for row in rows
    )
