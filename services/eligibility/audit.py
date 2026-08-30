"""Reading back what was decided, so a verdict can be checked rather than
trusted.

Two questions this answers, and both are about the stored rows rather than about
the engine that wrote them — an audit that re-ran the rules would only prove the
rules agree with themselves.

* **Why is anything INELIGIBLE?** Every refusal is listed with the dimension
  that produced it, its reason code, and the two logical references that say
  which requirement and which projected fact were compared. Any refusal that
  cannot be traced to a real contradiction on one of the six hard dimensions is
  a defect, and `blocker_dimensions` is where it would show up;

* **Did anything that must not block, block?** `invariant_violations` runs the
  promises of this phase as SQL over the stored rows and returns the offending
  counts. It should always come back empty, on any corpus, forever. It is
  written to be run against the operational database, not only against a
  fixture: the schema's CHECKs make most of these unstorable, and this is the
  second lock on the same door.

Nothing here prints a posting's words or a person's. The references are stable
logical pointers — `LANGUAGE#english`, `profile_preferences#convention_status` —
and an operator who needs the evidence itself reads Phase 3.4's and Phase 3.5B's
own evidence tables, which is where it lives.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from services.eligibility.models import Dimension, GlobalStatus, HARD_DIMENSIONS

__all__ = [
    "BlockingReason",
    "EligibilityAudit",
    "audit_eligibility",
    "invariant_violations",
]

#: The dimensions that must never appear on a blocking row, and must never carry
#: a VIOLATED. Advisory first, then the six this version defers.
NON_BLOCKING_DIMENSIONS: tuple[str, ...] = (
    Dimension.SKILL.value,
    Dimension.AMBIGUITY.value,
    Dimension.MOBILITY.value,
    Dimension.LOCATION.value,
    Dimension.AVAILABILITY.value,
    Dimension.DURATION.value,
    Dimension.START_DATE.value,
    Dimension.WORK_MODE.value,
)


@dataclass(frozen=True)
class BlockingReason:
    """One stored reason that decided, or failed to decide, one verdict."""

    opportunity_id: int
    status: GlobalStatus
    dimension: str
    reason_code: str
    requirement_ref: str | None
    profile_ref: str | None
    explanation: str


@dataclass(frozen=True)
class EligibilityAudit:
    """One person's whole corpus, as counts and as the reasons behind them."""

    user_id: int
    decisions: int
    verdicts: dict[str, int]
    rule_statuses: dict[str, int]
    blocker_dimensions: dict[str, int]
    unknown_dimensions: dict[str, int]
    ineligible_reasons: tuple[BlockingReason, ...]
    unknown_reasons: tuple[BlockingReason, ...]
    invariant_violations: dict[str, int]

    def as_dict(self) -> dict[str, object]:
        return {
            "decisions": self.decisions,
            "verdicts": dict(sorted(self.verdicts.items())),
            "rule_statuses": dict(sorted(self.rule_statuses.items())),
            "blocker_dimensions": dict(sorted(self.blocker_dimensions.items())),
            "unknown_dimensions": dict(sorted(self.unknown_dimensions.items())),
            "ineligible_decisions": len(
                {reason.opportunity_id for reason in self.ineligible_reasons}
            ),
            "invariant_violations": dict(sorted(self.invariant_violations.items())),
        }


def _counts(connection: sqlite3.Connection, sql: str, parameters: tuple) -> dict[str, int]:
    return {
        str(row[0]): int(row[1])
        for row in connection.execute(sql, parameters).fetchall()
    }


def _reasons(
    connection: sqlite3.Connection, user_id: int, status: GlobalStatus, rule_status: str
) -> tuple[BlockingReason, ...]:
    rows = connection.execute(
        """SELECT e.opportunity_id, e.status, r.dimension, r.reason_code,
                  r.requirement_ref, r.profile_ref, r.explanation
             FROM opportunity_eligibilities AS e
             JOIN eligibility_rule_results AS r ON r.eligibility_id = e.id
            WHERE e.user_id = ? AND e.status = ?
              AND r.is_blocking = 1 AND r.status = ?
            ORDER BY e.opportunity_id, r.position""",
        (user_id, status.value, rule_status),
    ).fetchall()
    return tuple(
        BlockingReason(
            opportunity_id=int(row[0]),
            status=GlobalStatus(str(row[1])),
            dimension=str(row[2]),
            reason_code=str(row[3]),
            requirement_ref=None if row[4] is None else str(row[4]),
            profile_ref=None if row[5] is None else str(row[5]),
            explanation=str(row[6]),
        )
        for row in rows
    )


def invariant_violations(
    connection: sqlite3.Connection, user_id: int
) -> dict[str, int]:
    """The promises of Phase 3.6, checked against the rows actually stored.

    Every count here must be zero. Each name is one sentence from the phase's
    contract, and a non-zero value names precisely which sentence was broken.
    """
    placeholders = ", ".join("?" * len(NON_BLOCKING_DIMENSIONS))
    scoped = (
        "FROM eligibility_rule_results AS r "
        "JOIN opportunity_eligibilities AS e ON e.id = r.eligibility_id "
        "WHERE e.user_id = ? "
    )
    checks: dict[str, tuple[str, tuple]] = {
        "non_blocking_rule_violated": (
            f"SELECT COUNT(*) {scoped} AND r.status = 'VIOLATED' AND r.is_blocking = 0",
            (user_id,),
        ),
        "preferred_requirement_blocking": (
            f"SELECT COUNT(*) {scoped} AND r.requirement_kind = 'PREFERRED' "
            "AND r.is_blocking = 1",
            (user_id,),
        ),
        "skill_rule_blocking": (
            f"SELECT COUNT(*) {scoped} AND r.dimension = 'SKILL' AND r.is_blocking = 1",
            (user_id,),
        ),
        "ambiguity_rule_blocking": (
            f"SELECT COUNT(*) {scoped} AND r.dimension = 'AMBIGUITY' "
            "AND r.is_blocking = 1",
            (user_id,),
        ),
        "deferred_or_advisory_dimension_blocking": (
            f"SELECT COUNT(*) {scoped} AND r.dimension IN ({placeholders}) "
            "AND r.is_blocking = 1",
            (user_id, *NON_BLOCKING_DIMENSIONS),
        ),
        "deferred_or_advisory_dimension_violated": (
            f"SELECT COUNT(*) {scoped} AND r.dimension IN ({placeholders}) "
            "AND r.status = 'VIOLATED'",
            (user_id, *NON_BLOCKING_DIMENSIONS),
        ),
        "blocking_rule_outside_hard_dimensions": (
            f"SELECT COUNT(*) {scoped} AND r.is_blocking = 1 AND r.dimension NOT IN "
            "(" + ", ".join("?" * len(HARD_DIMENSIONS)) + ")",
            (user_id, *sorted(dimension.value for dimension in HARD_DIMENSIONS)),
        ),
        # A refusal must come from a contradiction. An UNKNOWN, however many
        # there are, must never produce one.
        "ineligible_without_violation": (
            "SELECT COUNT(*) FROM opportunity_eligibilities "
            "WHERE user_id = ? AND status = 'INELIGIBLE' AND violated_count = 0",
            (user_id,),
        ),
        "violation_without_ineligible": (
            "SELECT COUNT(*) FROM opportunity_eligibilities "
            "WHERE user_id = ? AND status != 'INELIGIBLE' AND violated_count > 0",
            (user_id,),
        ),
        "unknown_without_blocking_unknown": (
            "SELECT COUNT(*) FROM opportunity_eligibilities "
            "WHERE user_id = ? AND status = 'UNKNOWN' AND blocking_unknown_count = 0",
            (user_id,),
        ),
        "eligible_with_open_hard_rule": (
            "SELECT COUNT(*) FROM opportunity_eligibilities "
            "WHERE user_id = ? AND status = 'ELIGIBLE' "
            "AND (violated_count > 0 OR blocking_unknown_count > 0)",
            (user_id,),
        ),
        # The summary must describe the rows beneath it.
        "counter_disagrees_with_results": (
            "SELECT COUNT(*) FROM opportunity_eligibilities AS e WHERE e.user_id = ? "
            "AND (e.violated_count != (SELECT COUNT(*) FROM eligibility_rule_results "
            "  WHERE eligibility_id = e.id AND status = 'VIOLATED') "
            "  OR e.satisfied_count != (SELECT COUNT(*) FROM eligibility_rule_results "
            "  WHERE eligibility_id = e.id AND status = 'SATISFIED') "
            "  OR e.unknown_count != (SELECT COUNT(*) FROM eligibility_rule_results "
            "  WHERE eligibility_id = e.id AND status = 'UNKNOWN') "
            "  OR e.not_applicable_count != (SELECT COUNT(*) FROM eligibility_rule_results "
            "  WHERE eligibility_id = e.id AND status = 'NOT_APPLICABLE') "
            "  OR e.not_evaluated_count != (SELECT COUNT(*) FROM eligibility_rule_results "
            "  WHERE eligibility_id = e.id AND status = 'NOT_EVALUATED'))",
            (user_id,),
        ),
    }
    return {
        name: int(connection.execute(sql, parameters).fetchone()[0])
        for name, (sql, parameters) in checks.items()
    }


def audit_eligibility(
    connection: sqlite3.Connection, user_id: int
) -> EligibilityAudit:
    """Everything an operator needs to check one person's stored decisions."""
    decisions = int(
        connection.execute(
            "SELECT COUNT(*) FROM opportunity_eligibilities WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]
    )
    verdicts = _counts(
        connection,
        "SELECT status, COUNT(*) FROM opportunity_eligibilities "
        "WHERE user_id = ? GROUP BY status",
        (user_id,),
    )
    rule_statuses = _counts(
        connection,
        "SELECT r.status, COUNT(*) FROM eligibility_rule_results AS r "
        "JOIN opportunity_eligibilities AS e ON e.id = r.eligibility_id "
        "WHERE e.user_id = ? GROUP BY r.status",
        (user_id,),
    )
    blocker_dimensions = _counts(
        connection,
        "SELECT r.dimension, COUNT(*) FROM eligibility_rule_results AS r "
        "JOIN opportunity_eligibilities AS e ON e.id = r.eligibility_id "
        "WHERE e.user_id = ? AND r.status = 'VIOLATED' GROUP BY r.dimension",
        (user_id,),
    )
    unknown_dimensions = _counts(
        connection,
        "SELECT r.dimension, COUNT(*) FROM eligibility_rule_results AS r "
        "JOIN opportunity_eligibilities AS e ON e.id = r.eligibility_id "
        "WHERE e.user_id = ? AND r.status = 'UNKNOWN' AND r.is_blocking = 1 "
        "GROUP BY r.dimension",
        (user_id,),
    )
    return EligibilityAudit(
        user_id=user_id,
        decisions=decisions,
        verdicts=verdicts,
        rule_statuses=rule_statuses,
        blocker_dimensions=blocker_dimensions,
        unknown_dimensions=unknown_dimensions,
        ineligible_reasons=_reasons(
            connection, user_id, GlobalStatus.INELIGIBLE, "VIOLATED"
        ),
        unknown_reasons=_reasons(connection, user_id, GlobalStatus.UNKNOWN, "UNKNOWN"),
        invariant_violations=invariant_violations(connection, user_id),
    )
