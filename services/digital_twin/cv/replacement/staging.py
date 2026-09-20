"""Recording what a human answered, and holding it.

Every decision here is stored and nothing else happens. No fact is created, no
status moves, no evidence is attached and no projection is recomputed — a
person can answer the whole review, close their laptop, come back, change an
answer and cancel the lot, and `profile_facts`, `profile_fact_provenance`,
`profile_preferences`, `profile_availability` and `profile_mobility` will be
byte for byte what they were before any of it.

A correction is staged the same way: the typed value is kept as a value, on the
decision row. No replacement fact is created and no fact is marked `CORRECTED`
while a review is open, because doing either would apply half a review.

Two properties are worth stating plainly, because both are about what happens
*between* two moments rather than inside one:

**One transaction per answer.** `record_review_decision` opens `BEGIN
IMMEDIATE` before it reads the attempt's lifecycle, before it builds the plan
and before it checks the target, and closes it at the single `COMMIT`. A
deferred transaction would take no write lock until the write itself, so
another connection could cancel the attempt, decide a fact or edit the manifest
after the validation and before the row lands — and the answer would be stored
against a state that had already stopped being true.

**An answer is bound to the state it answered.** Completeness is not decided
from target ids: each decision stores the plan entry's `state_digest`, and
`mark_ready_to_activate` recomputes the plan and compares. A fact that changed
status, or that gained evidence no CV produced, moves that digest, and the
review fails closed with `StaleReviewDecisionError` naming the entry to look at
again. Both checks and the `READY_TO_ACTIVATE` write share one transaction, so
the claim "a complete, current review exists" cannot be made about a state that
changed while it was being verified.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from services.digital_twin.cv.replacement.models import (
    BLOCKED_DIFFERENCES,
    CvStagingError,
    DecisionNotPermittedError,
    DecisionRole,
    EXISTING_DECISIONS,
    INCOMING_DECISIONS,
    OPEN_REPLACEMENT_LIFECYCLES,
    PROTECTED_DIFFERENCES,
    PlanEntry,
    Replacement,
    ReplacementClosedError,
    ReplacementLifecycle,
    ReplacementNotFoundError,
    ReplacementPlan,
    ReviewDecision,
    ReviewIncompleteError,
    StagedDecision,
    StaleReviewDecisionError,
    TerminalFactError,
)
from services.digital_twin.cv.replacement.planning import (
    compute_replacement_plan,
    plan_entry_for,
)
from services.digital_twin.cv.replacement.repository import (
    get_replacement,
    list_staged_decisions,
)

__all__ = [
    "mark_ready_to_activate",
    "mark_ready_to_activate_in_transaction",
    "record_review_decision",
    "review_progress",
]

_REPLACEMENT_COLUMNS = "id, profile_id, extraction_id, baseline_document_id, lifecycle"


def _require_open(replacement: Replacement | None, replacement_id: int, profile_id: int):
    if replacement is None:
        raise ReplacementNotFoundError(
            f"replacement {replacement_id} does not belong to profile {profile_id}"
        )
    if replacement.lifecycle not in OPEN_REPLACEMENT_LIFECYCLES:
        raise ReplacementClosedError(
            f"replacement {replacement_id} is {replacement.lifecycle.value}"
        )
    return replacement


def _check_decision_against_plan(
    entry: PlanEntry, decision: ReviewDecision, staged_value: str | None
) -> None:
    """Refuse anything the plan, as the database states it now, does not allow."""
    if decision is ReviewDecision.UNDECIDED:
        raise DecisionNotPermittedError("UNDECIDED is the absence of an answer")
    if entry.role is DecisionRole.INCOMING and decision not in INCOMING_DECISIONS:
        raise DecisionNotPermittedError(
            f"{decision.value} is not an answer about an incoming reading"
        )
    if entry.role is DecisionRole.EXISTING and decision not in EXISTING_DECISIONS:
        raise DecisionNotPermittedError(
            f"{decision.value} is not an answer about an existing fact"
        )
    if entry.difference in BLOCKED_DIFFERENCES:
        if decision is not ReviewDecision.SKIP_BLOCKED:
            # Reopening a terminal decision is a workflow of its own, and it
            # does not exist. Failing closed is the only answer that does not
            # invent one.
            raise TerminalFactError(
                f"a {entry.difference.value} reading is only ever skipped"
            )
    elif decision is ReviewDecision.SKIP_BLOCKED:
        raise DecisionNotPermittedError("only a blocked reading is skipped")
    if decision is ReviewDecision.RETIRE and entry.difference in PROTECTED_DIFFERENCES:
        raise DecisionNotPermittedError(
            f"a {entry.difference.value} fact is not this document's to retire"
        )
    if decision is ReviewDecision.CORRECT and (
        staged_value is None or staged_value.strip() == ""
    ):
        raise DecisionNotPermittedError("a correction stages the value it proposes")
    if decision is not ReviewDecision.CORRECT and staged_value is not None:
        raise DecisionNotPermittedError("only a correction stages a value")


def record_review_decision(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    replacement_id: int,
    decision: ReviewDecision,
    candidate_id: int | None = None,
    fact_id: int | None = None,
    staged_value: str | None = None,
    staged_normalized_value: str | None = None,
    after_plan: Callable[[], None] | None = None,
) -> StagedDecision:
    """Record one answer, under one `BEGIN IMMEDIATE`. Writes no fact.

    Exactly one target: an INCOMING answer names a manifest candidate, which has
    no fact and gets none here; an EXISTING answer names a fact this profile
    already holds. The schema refuses both and neither, and so does this.

    `after_plan` is a test seam invoked inside the transaction, once the plan
    has been built and the answer validated and before anything is written;
    production callers leave it unset.
    """
    if (candidate_id is None) == (fact_id is None):
        raise DecisionNotPermittedError(
            "a decision names a manifest candidate or an existing fact, not both"
        )
    if connection.in_transaction:
        raise sqlite3.ProgrammingError(
            "record_review_decision owns its transaction and borrows none"
        )
    # IMMEDIATE, and before the first business read: the lifecycle, the plan and
    # the write have to describe one moment. A deferred transaction would let
    # another connection cancel the attempt between the check and the row.
    connection.execute("BEGIN IMMEDIATE")
    try:
        replacement = _require_open(
            get_replacement(
                connection, profile_id=profile_id, replacement_id=replacement_id
            ),
            replacement_id,
            profile_id,
        )
        plan = compute_replacement_plan(
            connection, profile_id=profile_id, replacement_id=replacement_id
        )
        entry = plan_entry_for(plan, candidate_id=candidate_id, fact_id=fact_id)
        if entry is None:
            raise DecisionNotPermittedError(
                "that target is not part of this replacement's plan"
            )
        _check_decision_against_plan(entry, decision, staged_value)
        if after_plan is not None:
            after_plan()

        # A lookup and one statement rather than a multi-target upsert: the two
        # uniqueness rules are on different columns, and an explicit read says
        # plainly which of the two this answer replaces.
        existing = connection.execute(
            """SELECT id FROM profile_cv_replacement_decisions
                WHERE replacement_id = ? AND profile_id = ?
                  AND candidate_id IS ? AND fact_id IS ?""",
            (replacement_id, profile_id, candidate_id, fact_id),
        ).fetchone()
        if existing is None:
            connection.execute(
                """INSERT INTO profile_cv_replacement_decisions (
                       replacement_id, profile_id, role, candidate_id, fact_id,
                       difference, decision, staged_value, staged_normalized_value,
                       fact_status_at_decision, review_state_digest, decided_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (
                    replacement_id,
                    profile_id,
                    entry.role.value,
                    candidate_id,
                    fact_id,
                    entry.difference.value,
                    decision.value,
                    staged_value,
                    staged_normalized_value,
                    entry.fact_status,
                    entry.state_digest,
                ),
            )
        else:
            connection.execute(
                """UPDATE profile_cv_replacement_decisions
                      SET difference = ?, decision = ?, staged_value = ?,
                          staged_normalized_value = ?, fact_status_at_decision = ?,
                          review_state_digest = ?, decided_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND profile_id = ?""",
                (
                    entry.difference.value,
                    decision.value,
                    staged_value,
                    staged_normalized_value,
                    entry.fact_status,
                    entry.state_digest,
                    existing[0],
                    profile_id,
                ),
            )
        if replacement.lifecycle is not ReplacementLifecycle.REVIEWING:
            # PREPARED becomes REVIEWING, and READY_TO_ACTIVATE goes back to it:
            # a completed review must not keep claiming to cover an answer it
            # never saw.
            connection.execute(
                """UPDATE profile_cv_replacements
                      SET lifecycle = 'REVIEWING', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND profile_id = ?
                      AND lifecycle IN ('PREPARED', 'READY_TO_ACTIVATE')""",
                (replacement_id, profile_id),
            )
        stored = [
            row
            for row in list_staged_decisions(
                connection, profile_id=profile_id, replacement_id=replacement_id
            )
            if row.candidate_id == candidate_id and row.fact_id == fact_id
        ]
        if not stored:
            raise CvStagingError("the decision was not stored")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return stored[0]


def _readiness(
    plan: ReplacementPlan, decisions: tuple[StagedDecision, ...]
) -> dict[str, object]:
    """Compare the plan with the answers, by state and not by id alone."""
    answered = {
        (decision.candidate_id, decision.fact_id): decision
        for decision in decisions
        if decision.decision is not ReviewDecision.UNDECIDED
    }
    expected = {(entry.candidate_id, entry.fact_id): entry for entry in plan.entries}
    unanswered = [key for key in expected if key not in answered]
    outside = [key for key in answered if key not in expected]
    stale = [
        key
        for key, entry in expected.items()
        if key in answered and answered[key].state_digest != entry.state_digest
    ]
    return {
        "replacement_id": plan.replacement_id,
        "plan_entries": len(plan.entries),
        "answered": len(expected) - len(unanswered),
        "unanswered": len(unanswered),
        "decisions_outside_plan": len(outside),
        "stale_decisions": len(stale),
        "stale_targets": tuple(sorted(stale, key=lambda key: (key[0] or 0, key[1] or 0))),
        "counts_by_difference": plan.counts_by_difference(),
    }


def review_progress(
    connection: sqlite3.Connection, *, profile_id: int, replacement_id: int
) -> dict[str, object]:
    """How much of the plan is answered, and how much of it has gone stale.

    Counters, ids and canonical names. Never a reading, never a value.
    """
    plan = compute_replacement_plan(
        connection, profile_id=profile_id, replacement_id=replacement_id
    )
    decisions = list_staged_decisions(
        connection, profile_id=profile_id, replacement_id=replacement_id
    )
    return _readiness(plan, decisions)


def mark_ready_to_activate_in_transaction(
    connection: sqlite3.Connection, *, profile_id: int, replacement_id: int
) -> Replacement:
    """Verify the review and publish READY_TO_ACTIVATE, in one transaction.

    This is the **only** place in the package that writes that lifecycle, and
    the verification is inside it rather than in front of it. There is no entry
    point that writes it after checking `in_transaction` alone, and no caller
    can reach the write by promising to have checked first: the check is the
    first thing this function does, and it is the same check
    `review_progress` reports, computed by the same `_readiness`.

    It opens no transaction and commits none. The caller owns both, which is
    what makes the verification and the claim land together.

    Three ways to fail, all closed:

    * an entry nobody answered;
    * an answer about something the plan no longer contains;
    * an answer whose `state_digest` no longer matches — the fact changed
      status, or gained evidence no CV produced, or the manifest reading moved.
    """
    if not connection.in_transaction:
        raise CvStagingError(
            "marking a replacement ready requires the caller's transaction"
        )
    _require_open(
        get_replacement(
            connection, profile_id=profile_id, replacement_id=replacement_id
        ),
        replacement_id,
        profile_id,
    )
    plan = compute_replacement_plan(
        connection, profile_id=profile_id, replacement_id=replacement_id
    )
    progress = _readiness(
        plan,
        list_staged_decisions(
            connection, profile_id=profile_id, replacement_id=replacement_id
        ),
    )
    if progress["unanswered"]:
        raise ReviewIncompleteError(
            f"{progress['unanswered']} of {progress['plan_entries']} entries "
            f"are still unanswered"
        )
    if progress["decisions_outside_plan"]:
        raise ReviewIncompleteError(
            f"{progress['decisions_outside_plan']} decisions no longer match "
            f"the plan; recompute the review"
        )
    if progress["stale_decisions"]:
        raise StaleReviewDecisionError(
            f"{progress['stale_decisions']} decisions were taken against a "
            f"state the database no longer holds "
            f"(candidate_id, fact_id): {progress['stale_targets']}"
        )
    row = connection.execute(
        f"""UPDATE profile_cv_replacements
               SET lifecycle = 'READY_TO_ACTIVATE', updated_at = CURRENT_TIMESTAMP
             WHERE id = ? AND profile_id = ?
               AND lifecycle IN ('PREPARED', 'REVIEWING', 'READY_TO_ACTIVATE')
         RETURNING {_REPLACEMENT_COLUMNS}""",
        (replacement_id, profile_id),
    ).fetchone()
    if row is None:
        raise ReplacementNotFoundError(
            f"replacement {replacement_id} of profile {profile_id} is not open"
        )
    return Replacement(
        id=row[0],
        profile_id=row[1],
        extraction_id=row[2],
        baseline_document_id=row[3],
        lifecycle=ReplacementLifecycle(row[4]),
    )


def mark_ready_to_activate(
    connection: sqlite3.Connection, *, profile_id: int, replacement_id: int
) -> Replacement:
    """Say that a complete, still-current review exists. It activates nothing.

    In this slice `READY_TO_ACTIVATE` is a statement about the review and
    nothing else: no fact changes, no CV becomes active, and no downstream
    phase is told anything. Activation is a separate, later, separately
    approved piece of work.

    This is the ordinary entry point: it owns one `BEGIN IMMEDIATE` and hands
    it to the function above, which verifies and writes inside it.
    """
    if connection.in_transaction:
        raise sqlite3.ProgrammingError(
            "mark_ready_to_activate owns its transaction and borrows none"
        )
    connection.execute("BEGIN IMMEDIATE")
    try:
        ready = mark_ready_to_activate_in_transaction(
            connection, profile_id=profile_id, replacement_id=replacement_id
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return ready
