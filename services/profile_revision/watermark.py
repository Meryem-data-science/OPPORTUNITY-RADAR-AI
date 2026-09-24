"""Which profile revision each downstream phase has actually caught up with.

Activating a replacement CV does not edit a ranking; it makes every ranking
describe somebody who no longer exists. Migration 0028 gives that fact a place
to live: `profile_cv_replacements.activation_revision` counts the activations a
profile has had, and `profile_downstream_sync_watermark` records, per phase, the
revision that phase last *recomputed and published* for.

Two numbers, and one rule built on them:

    a result is current  <=>  its phase, and every phase it is derived from,
                              have a watermark equal to the active revision

Everything else here exists to keep that rule honest.

**A watermark is a claim about work that happened.** It is never written
because a freshness check passed, because a caller asked, or because a read
found nothing to do. `advance_sync_watermark_in_transaction` refuses to run
outside the caller's transaction precisely so that it cannot be called anywhere
except beside the write it is vouching for — in the same transaction as the
publication, before its single `COMMIT`. If the publication rolls back, so does
the claim.

**A phase cannot outrun what it is built from.** Recomputing Matching from
skills that still describe the previous CV produces a result that is stale
whatever its own watermark says, so advancing refuses when a required phase is
behind. That is what makes the order the activation announces — skills,
structured profile, eligibility, Matching, Recommendation, Priority, Portfolio
— a rule rather than a suggestion.

**A profile that never replaced a CV is untouched.** Its active revision is 0,
every missing watermark reads 0, and advancing at revision 0 writes nothing at
all. No row appears, no behaviour changes, and a database migrated before 0027
— where an activation cannot exist — answers 0 rather than failing.

The dependencies below are read off the code, not off the phase order: each one
is a projection the dependent phase actually loads.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

__all__ = [
    "DIRECT_DEPENDENCIES",
    "DependencyNotSyncedError",
    "ProfileRevisionError",
    "SyncPhase",
    "WatermarkRegressionError",
    "active_profile_revision",
    "advance_sync_watermark_if_unchanged",
    "advance_sync_watermark_in_transaction",
    "phase_is_current",
    "read_sync_watermark",
    "read_sync_watermarks",
    "required_phases",
    "stale_phases",
]


class SyncPhase(StrEnum):
    """The seven phases migration 0028 names, spelled exactly as it spells them.

    The `CHECK` constraint on `profile_downstream_sync_watermark.phase` is the
    authority; this enum mirrors it so that a typo is an `AttributeError` here
    rather than a constraint failure at the end of a synchronization.
    """

    SKILLS = "SKILLS"
    STRUCTURED_PROFILE = "STRUCTURED_PROFILE"
    ELIGIBILITY = "ELIGIBILITY"
    MATCHING = "MATCHING"
    RECOMMENDATION = "RECOMMENDATION"
    PRIORITY = "PRIORITY"
    PORTFOLIO = "PORTFOLIO"


#: What each phase reads from another phase's published projection — taken from
#: the loaders themselves, not from the order the phases happen to run in:
#:
#: * skills and the structured profile are built from verified profile facts,
#:   so a CV activation is upstream of both and of nothing else;
#: * eligibility loads `list_profile_skills` and `list_profile_languages`;
#: * matching loads `list_profile_skills`, `list_profile_experiences`,
#:   `list_profile_projects` and `list_profile_educations`;
#: * recommendation assembles the current matching run and reads the stored
#:   eligibility decisions;
#: * priority requires the current matching snapshot **and** reads the stored
#:   eligibility decision of every posting, carrying its status and its
#:   `input_fingerprint` into `PriorityEligibilitySnapshot`
#:   (`services/priority/input_assembly.py`) — so a decision computed for an
#:   earlier revision would be scored as though it still described the person;
#: * portfolio requires the current priority run and carries the matching one.
DIRECT_DEPENDENCIES: Mapping[SyncPhase, tuple[SyncPhase, ...]] = MappingProxyType(
    {
        SyncPhase.SKILLS: (),
        SyncPhase.STRUCTURED_PROFILE: (),
        SyncPhase.ELIGIBILITY: (SyncPhase.SKILLS, SyncPhase.STRUCTURED_PROFILE),
        SyncPhase.MATCHING: (SyncPhase.SKILLS, SyncPhase.STRUCTURED_PROFILE),
        SyncPhase.RECOMMENDATION: (SyncPhase.MATCHING, SyncPhase.ELIGIBILITY),
        SyncPhase.PRIORITY: (SyncPhase.MATCHING, SyncPhase.ELIGIBILITY),
        SyncPhase.PORTFOLIO: (SyncPhase.PRIORITY, SyncPhase.MATCHING),
    }
)


class ProfileRevisionError(RuntimeError):
    """A revision or watermark operation cannot be carried out safely."""


class DependencyNotSyncedError(ProfileRevisionError):
    """A phase this one is derived from has not caught up with this revision.

    Publishing anyway would record a result computed from a projection that
    still describes the previous CV, and a watermark saying otherwise.
    """


class WatermarkRegressionError(ProfileRevisionError):
    """A watermark would move backwards. Revisions only ever count up."""


def required_phases(phase: SyncPhase) -> tuple[SyncPhase, ...]:
    """Every phase `phase` depends on, directly or through another, sorted.

    The closure rather than the direct edges: Portfolio is built from Priority,
    which is built from Matching, which is built from the skills — so skills
    that still describe the previous CV make the Portfolio stale too, however
    many times Priority has been recomputed in between.
    """
    seen: set[SyncPhase] = set()
    pending = list(DIRECT_DEPENDENCIES[phase])
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(DIRECT_DEPENDENCIES[current])
    return tuple(sorted(seen, key=lambda member: member.value))


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def active_profile_revision(connection: sqlite3.Connection, profile_id: int) -> int:
    """How many CV activations this profile has had. Never `None`, never below 0.

    A profile that has never replaced its CV is at revision 0, and so is every
    profile in a database migrated before 0027 — there, an activation cannot
    have been recorded, so answering 0 is a fact rather than a fallback.
    """
    if not _table_exists(connection, "profile_cv_replacements"):
        return 0
    row = connection.execute(
        """SELECT COALESCE(MAX(activation_revision), 0)
             FROM profile_cv_replacements
            WHERE profile_id = ? AND activated_at IS NOT NULL""",
        (profile_id,),
    ).fetchone()
    return 0 if row is None or row[0] is None else int(row[0])


def read_sync_watermark(
    connection: sqlite3.Connection, profile_id: int, phase: SyncPhase
) -> int:
    """The revision this phase last published for. Absent means 0, as designed."""
    if not _table_exists(connection, "profile_downstream_sync_watermark"):
        return 0
    row = connection.execute(
        """SELECT synced_revision FROM profile_downstream_sync_watermark
            WHERE profile_id = ? AND phase = ?""",
        (profile_id, SyncPhase(phase).value),
    ).fetchone()
    return 0 if row is None else int(row[0])


def read_sync_watermarks(
    connection: sqlite3.Connection, profile_id: int
) -> Mapping[SyncPhase, int]:
    """Every phase's watermark, including the ones that have never been written."""
    stored: dict[SyncPhase, int] = {phase: 0 for phase in SyncPhase}
    if not _table_exists(connection, "profile_downstream_sync_watermark"):
        return MappingProxyType(stored)
    for name, revision in connection.execute(
        """SELECT phase, synced_revision FROM profile_downstream_sync_watermark
            WHERE profile_id = ?""",
        (profile_id,),
    ).fetchall():
        stored[SyncPhase(name)] = int(revision)
    return MappingProxyType(stored)


def stale_phases(
    connection: sqlite3.Connection, profile_id: int, phase: SyncPhase
) -> tuple[SyncPhase, ...]:
    """Which of `phase` and its dependencies are behind the active revision.

    Empty means the result may be presented as current. Canonical names and
    numbers only — nothing here reads a value or a reading.
    """
    phase = SyncPhase(phase)
    revision = active_profile_revision(connection, profile_id)
    if revision == 0:
        # Nothing has ever been replaced, so nothing can be behind.
        return ()
    watermarks = read_sync_watermarks(connection, profile_id)
    return tuple(
        member
        for member in (phase, *required_phases(phase))
        if watermarks[member] != revision
    )


def phase_is_current(
    connection: sqlite3.Connection, profile_id: int, phase: SyncPhase
) -> bool:
    """Whether this phase's published result describes the profile as it is now."""
    return not stale_phases(connection, profile_id, phase)


def _require_transaction(connection: sqlite3.Connection, phase: SyncPhase) -> None:
    if not connection.in_transaction:
        raise ProfileRevisionError(
            f"advancing the {SyncPhase(phase).value} watermark requires the "
            f"transaction that publishes the result it vouches for"
        )


def _advance(
    connection: sqlite3.Connection, *, profile_id: int, phase: SyncPhase, revision: int
) -> None:
    """Write the claim. The caller has already proved it is allowed to."""
    stored = read_sync_watermark(connection, profile_id, phase)
    if stored > revision:
        raise WatermarkRegressionError(
            f"the {phase.value} watermark of profile {profile_id} is at revision "
            f"{stored}; it cannot move back to {revision}"
        )
    behind = [
        member
        for member in required_phases(phase)
        if read_sync_watermark(connection, profile_id, member) != revision
    ]
    if behind:
        raise DependencyNotSyncedError(
            f"{phase.value} cannot be published for revision {revision} of profile "
            f"{profile_id}: {', '.join(member.value for member in behind)} "
            f"{'is' if len(behind) == 1 else 'are'} not synchronized yet"
        )
    connection.execute(
        """INSERT INTO profile_downstream_sync_watermark
               (profile_id, phase, synced_revision, updated_at)
           VALUES (?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT (profile_id, phase)
           DO UPDATE SET synced_revision = excluded.synced_revision,
                         updated_at = CURRENT_TIMESTAMP""",
        (profile_id, phase.value, revision),
    )


def advance_sync_watermark_in_transaction(
    connection: sqlite3.Connection, *, profile_id: int, phase: SyncPhase
) -> int:
    """Record that this phase has published its result for the active revision.

    Called by the owner that just wrote the result, inside the transaction that
    wrote it and before its single `COMMIT`. It opens nothing, commits nothing
    and rolls nothing back, so the claim and the result land together or not at
    all — and a publication that fails afterwards takes the watermark with it.

    The revision is **read here**, under the caller's transaction, rather than
    passed in. An `IMMEDIATE` transaction holds the write lock an activation
    also needs, so the number read cannot change before the `COMMIT`, and no
    caller can name a revision other than the live one.

    Refuses, without writing, when a phase this one is derived from is behind:
    the result would then have been computed from a projection describing the
    previous CV. Returns the revision now recorded.

    At revision 0 it writes nothing. A profile that never replaced a CV keeps
    exactly the rows it had, which is what makes this safe to add to owners that
    have been running since long before any of this existed.
    """
    phase = SyncPhase(phase)
    _require_transaction(connection, phase)
    revision = active_profile_revision(connection, profile_id)
    if revision == 0:
        return 0
    _advance(connection, profile_id=profile_id, phase=phase, revision=revision)
    return revision


def advance_sync_watermark_if_unchanged(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    phase: SyncPhase,
    expected_revision: int,
) -> bool:
    """The same claim, for a phase whose publication is many transactions.

    Eligibility decides one posting per transaction on purpose, so that one
    failure cannot undo the postings already reconciled. There is no single
    write to sit beside, so the honest form is this one: the caller captures
    the revision **before** its loop, and calls this once the whole loop has
    succeeded. Here, under one `BEGIN IMMEDIATE`, the revision is read again
    and the watermark moves only if it is still the one the whole run was
    computed against.

    An activation that landed while the loop was running therefore leaves the
    watermark where it was — the run did describe two different profiles, and
    saying so is the only truthful answer. Returns whether the claim was made.

    It owns its transaction and borrows none: it is called after the loop, not
    inside one of its transactions.
    """
    phase = SyncPhase(phase)
    if connection.in_transaction:
        raise ProfileRevisionError(
            f"advancing the {phase.value} watermark after a multi-transaction "
            f"synchronization owns its transaction and borrows none"
        )
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
        raise ProfileRevisionError("expected_revision must be an integer")
    if expected_revision < 0:
        raise ProfileRevisionError("expected_revision cannot be negative")
    if expected_revision == 0:
        # A profile that had not replaced a CV when the run started has no
        # claim to make, so this opens no transaction at all. If one was
        # activated during the run, the check below would refuse anyway.
        return False
    connection.execute("BEGIN IMMEDIATE")
    try:
        revision = active_profile_revision(connection, profile_id)
        if revision != expected_revision or revision == 0:
            # Either the profile moved under the run, or there is nothing to
            # claim. Both mean: write nothing.
            connection.execute("ROLLBACK")
            return False
        _advance(connection, profile_id=profile_id, phase=phase, revision=revision)
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return True
