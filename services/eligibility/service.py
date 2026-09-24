"""Orchestration: select the postings, load both sides, decide, reconcile.

The only module that puts the pure engine and the repository together, and
deliberately thin. It selects the postings the same way every phase before it
does, hands each pair of inputs to `evaluate_eligibility` as plain value
objects, and asks the repository to store the result — one posting, one
transaction.

**Phase 3.5 is a prerequisite, and the failure is loud.** A verdict computed
over a posting whose requirements nobody has read would be a verdict over
silence, and silence and "requires nothing" are exactly the two things `0013`'s
state table exists to tell apart. Rather than skip such a posting quietly and
report a coverage the database does not have, the run refuses before writing
anything and says which synchronization to run first. It does not run 3.5
itself: two phases that trigger each other are two phases nobody can reason
about separately.

**The person must already exist.** `synchronize_eligibility` takes a user and a
profile that were created by the Digital Twin's own commands. Bringing a user
into existence as a side effect of an eligibility run would create an empty
profile and then compute a page of UNKNOWNs about it, which reads like an answer
and is an accident.

The selection mirrors 3.5B's, which mirrors 3.5A's, which mirrors the
qualification pipeline: active postings that are not merged duplicates, oldest
id first. "In scope" is a collection filter and has never been a judgement about
whether anybody could apply — that judgement is this phase's output, not its
input.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from services.eligibility.engine import evaluate_eligibility
from services.eligibility.inputs import load_opportunity_input, load_profile_input
from services.eligibility.models import (
    ELIGIBILITY_ENGINE_VERSION,
    EligibilityInput,
    GlobalStatus,
    RuleStatus,
)
from services.eligibility.repository import (
    store_eligibility,
    stored_eligibility_signature,
)
from services.profile_revision.watermark import (
    SyncPhase,
    active_profile_revision,
    advance_sync_watermark_if_unchanged,
    read_sync_watermark,
    required_phases,
)

__all__ = [
    "MISSING_REQUIREMENTS_ERROR",
    "EligibilityServiceError",
    "EligibilitySyncSummary",
    "evaluate_one_eligibility",
    "in_scope_opportunity_ids",
    "synchronize_eligibility",
]


class EligibilityServiceError(RuntimeError):
    """Raised when eligibility synchronization cannot safely proceed."""


MISSING_REQUIREMENTS_ERROR = (
    "some in-scope postings have no Phase 3.5 requirement reading; "
    "run the Phase 3.5A constraint sync and the Phase 3.5B requirement sync first"
)

#: Every posting the decision covers, and whether Phase 3.5 has been through it.
#: The 3.5B state row is what says "this posting was read", so a posting that
#: genuinely demands nothing is in scope and a posting nobody has read is not.
_SCOPE_SQL = """
    SELECT o.id, s.opportunity_id IS NOT NULL
      FROM opportunities AS o
      LEFT JOIN opportunity_requirement_extraction_state AS s
             ON s.opportunity_id = o.id
     WHERE o.is_active = 1 AND o.status != 'merged_duplicate'
"""


def _require_schema(connection: sqlite3.Connection) -> None:
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='opportunity_eligibilities'"
    ).fetchone() is None:
        raise EligibilityServiceError(
            "migration 0014 is required; explicitly apply migrations first"
        )


def in_scope_opportunity_ids(
    connection: sqlite3.Connection, *, limit: int | None = None
) -> tuple[tuple[int, bool], ...]:
    """Every in-scope posting's id and whether Phase 3.5 has read it."""
    if limit is not None and (isinstance(limit, bool) or limit < 1):
        raise ValueError("limit must be positive")
    sql = _SCOPE_SQL + " ORDER BY o.id"
    parameters: tuple[int, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        parameters = (limit,)
    return tuple(
        (int(row[0]), bool(row[1]))
        for row in connection.execute(sql, parameters).fetchall()
    )


@dataclass(frozen=True)
class EligibilitySyncSummary:
    """What one run did, and how the corpus came out. Counters, never a score.

    `unchanged` is the interesting one: it counts the postings whose stored
    fingerprint already matched, which were therefore not rewritten — not even a
    timestamp. A second run over unchanged data reports every posting there and
    `changed=false`.
    """

    processed: int
    unchanged: int
    created: int
    replaced: int
    eligible: int
    ineligible: int
    unknown: int
    satisfied_rules: int
    violated_rules: int
    unknown_rules: int
    not_applicable_rules: int
    not_evaluated_rules: int
    engine_version: str = ELIGIBILITY_ENGINE_VERSION

    @property
    def changed(self) -> bool:
        return bool(self.created or self.replaced)

    def as_dict(self) -> dict[str, object]:
        return {
            "engine_version": self.engine_version,
            "processed": self.processed,
            "created": self.created,
            "replaced": self.replaced,
            "unchanged": self.unchanged,
            "eligible": self.eligible,
            "unknown": self.unknown,
            "ineligible": self.ineligible,
            "satisfied_rules": self.satisfied_rules,
            "violated_rules": self.violated_rules,
            "unknown_rules": self.unknown_rules,
            "not_applicable_rules": self.not_applicable_rules,
            "not_evaluated_rules": self.not_evaluated_rules,
            "changed": self.changed,
        }


def evaluate_one_eligibility(
    connection: sqlite3.Connection,
    user_id: int,
    profile_id: int,
    opportunity_id: int,
    *,
    evaluated_at: str | None = None,
):
    """Decide one posting for one person, and store it if anything changed.

    Returns `(decision, written)`. `False` means the stored fingerprint and
    engine version already matched, so nothing was rewritten.

    There is no `engine_version` argument, here or in `synchronize_eligibility`,
    and that is deliberate — the same argument 3.5B's service makes.
    `ELIGIBILITY_ENGINE_VERSION` is the version of *these rules*, so it is the
    only honest label for what they produced. A caller allowed to pass another
    could ask for a recomputation under a version it invented, and the row would
    then claim rules that never ran. Making a new version means editing the
    constant.
    """
    _require_schema(connection)
    opportunity = load_opportunity_input(connection, opportunity_id)
    if opportunity is None:
        raise EligibilityServiceError(
            f"opportunity {opportunity_id} has no Phase 3.5 requirement reading; "
            "run the Phase 3.5A and 3.5B synchronizations first"
        )
    profile = load_profile_input(connection, profile_id)
    decision = evaluate_eligibility(EligibilityInput(opportunity, profile))
    signature = stored_eligibility_signature(connection, user_id, opportunity_id)
    if signature == (decision.input_fingerprint, decision.engine_version):
        return decision, False
    store_eligibility(connection, user_id, decision, evaluated_at=evaluated_at)
    return decision, True


def synchronize_eligibility(
    connection: sqlite3.Connection,
    user_id: int,
    profile_id: int,
    *,
    limit: int | None = None,
    evaluated_at: str | None = None,
    before_store: Callable[[int], None] | None = None,
) -> EligibilitySyncSummary:
    """Bring every in-scope posting's decision for this person up to date.

    The Phase 3.5 prerequisite is checked for the **whole** selection before
    anything is written, so a half-read corpus fails as one refusal rather than
    as a partly-written run.

    The person's side is loaded **once**, before the loop. It is the same for
    every posting, so re-reading it per posting would be several hundred
    identical queries — and, worse, would let a concurrent edit change the
    profile halfway through a run and produce a corpus decided against two
    different people.

    Each posting is then its own transaction, so one failure cannot undo the
    decisions already reconciled — but the failure is raised rather than
    swallowed, because a run that silently skipped postings would report a
    coverage it does not have.

    A posting whose stored `(fingerprint, engine version)` already matches is
    left exactly as it is: no delete, no insert, no timestamp moved.

    `before_store` is a test seam invoked with each opportunity id that is about
    to be written; production callers leave it unset.
    """
    _require_schema(connection)
    if connection.execute(
        "SELECT 1 FROM users WHERE id = ?", (user_id,)
    ).fetchone() is None:
        raise EligibilityServiceError(f"user {user_id} does not exist")
    if connection.execute(
        "SELECT 1 FROM profiles WHERE id = ? AND user_id = ?", (profile_id, user_id)
    ).fetchone() is None:
        raise EligibilityServiceError(
            f"profile {profile_id} does not belong to user {user_id}"
        )

    # Captured before anything else, and before the person's side is loaded:
    # this is the revision the whole run will be computed against. The question
    # it answers is about *this profile*, so it is asked before the corpus-wide
    # prerequisite below — a run against skills that still describe the previous
    # CV has nothing to gain from reading the postings first.
    revision = active_profile_revision(connection, profile_id)
    if revision:
        behind = [
            phase.value
            for phase in required_phases(SyncPhase.ELIGIBILITY)
            if read_sync_watermark(connection, profile_id, phase) != revision
        ]
        if behind:
            raise EligibilityServiceError(
                f"eligibility cannot be synchronized for revision {revision} of "
                f"profile {profile_id}: {', '.join(behind)} "
                f"{'is' if len(behind) == 1 else 'are'} not synchronized yet"
            )

    timestamp = evaluated_at or datetime.now(UTC).isoformat(timespec="microseconds")
    scope = in_scope_opportunity_ids(connection, limit=limit)
    if any(not read for _, read in scope):
        raise EligibilityServiceError(MISSING_REQUIREMENTS_ERROR)

    profile = load_profile_input(connection, profile_id)
    verdicts = {status: 0 for status in GlobalStatus}
    rules = {status: 0 for status in RuleStatus}
    unchanged = created = replaced = 0

    for opportunity_id, _ in scope:
        opportunity = load_opportunity_input(connection, opportunity_id)
        if opportunity is None:
            # The 3.5B state row said this posting was read, and its 3.5A
            # projection or its 3.5B rows are not there. That is a broken
            # invariant upstream, not a posting to skip.
            raise EligibilityServiceError(
                f"opportunity {opportunity_id} has a Phase 3.5B state row but no "
                "readable requirement projection"
            )
        decision = evaluate_eligibility(EligibilityInput(opportunity, profile))
        verdicts[decision.status] += 1
        for result in decision.results:
            rules[result.status] += 1

        signature = stored_eligibility_signature(connection, user_id, opportunity_id)
        if signature == (decision.input_fingerprint, decision.engine_version):
            unchanged += 1
            continue
        if before_store is not None:
            before_store(opportunity_id)
        store_eligibility(connection, user_id, decision, evaluated_at=timestamp)
        if signature is None:
            created += 1
        else:
            replaced += 1

    # Every posting was its own transaction, so there is no single write for
    # the claim to sit beside. It is made here instead, once the whole loop has
    # succeeded, under one `BEGIN IMMEDIATE` that reads the revision again: an
    # activation that landed mid-run leaves the watermark where it was, because
    # the corpus then describes two different people. A posting that raised
    # never reaches this line.
    advance_sync_watermark_if_unchanged(
        connection,
        profile_id=profile_id,
        phase=SyncPhase.ELIGIBILITY,
        expected_revision=revision,
    )

    return EligibilitySyncSummary(
        processed=len(scope),
        unchanged=unchanged,
        created=created,
        replaced=replaced,
        eligible=verdicts[GlobalStatus.ELIGIBLE],
        ineligible=verdicts[GlobalStatus.INELIGIBLE],
        unknown=verdicts[GlobalStatus.UNKNOWN],
        satisfied_rules=rules[RuleStatus.SATISFIED],
        violated_rules=rules[RuleStatus.VIOLATED],
        unknown_rules=rules[RuleStatus.UNKNOWN],
        not_applicable_rules=rules[RuleStatus.NOT_APPLICABLE],
        not_evaluated_rules=rules[RuleStatus.NOT_EVALUATED],
    )
