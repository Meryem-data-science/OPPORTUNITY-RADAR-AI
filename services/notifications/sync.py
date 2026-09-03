"""Atomic, fail-closed advance of the notification policy cursor.

What a sync can honestly know
-----------------------------
Portfolio runs are append-only *rows*, but the profile's current pointer is
not monotonic: a run is reused whenever the Portfolio comes back to a snapshot
it already stored, so the pointer can jump back to a lower run id. That leaves
this service with exactly two sources of truth about what happened, and no
third:

* the pointer positions it observed itself, one per sync — the cursor is the
  last of them; and
* the Portfolio runs appended since the last sync, which are the run ids above
  the high-water mark.

Anything else is a history nobody recorded. Between two syncs the pointer may
have visited any number of already-stored snapshots, and no row anywhere says
which — so this service never invents that path. It replays the runs that were
genuinely new, and otherwise reports the one movement it can actually attest
to: cursor to current.

That is why the state carries two pointers. ``last_processed`` is the snapshot
last observed; ``highest_seen`` is the greatest run id ever walked, and it
never moves backwards. From them each sync takes one of three shapes:

* ``current == cursor`` — nothing moved, and nothing is written.
* ``current <= highest_seen`` — the Portfolio came back to a snapshot already
  walked. That is one direct transition, cursor to current; the runs numbered
  in between were processed in an earlier sync and are never replayed as if
  they had just happened.
* ``current > highest_seen`` — runs were appended since the last sync. Those,
  and only those, are replayed in ascending order: cursor to the first new run,
  then new run to new run, up to current.

The first sync of a profile is deliberately silent: the Portfolio snapshot the
profile already has becomes the baseline, both pointers are planted on it, and
no event is produced. A user who turns notifications on does not get told about
every opportunity they already know about.

Everything is one transaction. A corrupt Portfolio run, a broken provenance, or
any other failure rolls the whole sync back: no event, no outbox row, and
pointers that have not moved. Nothing here sends anything.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from services.collector.matching.fingerprint import canonical_json
from services.portfolio.audit import (
    PortfolioAuditStatus,
    PortfolioProfileAuditStatus,
    audit_current_portfolio,
    audit_portfolio_run,
)
from services.portfolio.read_model import read_portfolio_run

from .fingerprint import (
    canonical_notification_event_payload,
    notification_event_fingerprint,
)
from .opportunity_metadata import read_opportunity_metadata
from .persistence import (
    NotificationEventRecord,
    store_notification_events,
)
from .policy import (
    NOTIFICATION_POLICY_VERSION,
    NotificationTransition,
    PortfolioPosition,
    evaluate_transitions,
)

REQUIRED_MIGRATION_VERSION = "0019"
_TABLES = frozenset(
    {"notification_policy_state", "notification_events", "notification_outbox"}
)


class NotificationSyncError(RuntimeError):
    """Raised when notification synchronization cannot proceed safely."""


class NotificationSyncStatus(StrEnum):
    NOT_SYNCED = "NOT_SYNCED"
    BASELINE_INITIALIZED = "BASELINE_INITIALIZED"
    UP_TO_DATE = "UP_TO_DATE"
    PROCESSED = "PROCESSED"


@dataclass(frozen=True)
class NotificationSyncResult:
    profile_id: int
    status: NotificationSyncStatus
    policy_version: str
    baseline_portfolio_run_id: int | None
    last_processed_portfolio_run_id: int | None
    highest_seen_portfolio_run_id: int | None
    processed_run_ids: tuple[int, ...]
    event_ids: tuple[int, ...]
    event_count: int
    outbox_count: int
    duplicate_count: int
    state_changed: bool


def _preflight(connection: sqlite3.Connection) -> None:
    try:
        migrated = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?",
            (REQUIRED_MIGRATION_VERSION,),
        ).fetchone()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN"
                " ('notification_policy_state','notification_events','notification_outbox')"
            )
        }
    except sqlite3.Error as error:
        raise NotificationSyncError(
            "notification persistence schema is not ready;"
            f" migrate through {REQUIRED_MIGRATION_VERSION} before sync"
        ) from error
    if migrated is None or tables != _TABLES:
        raise NotificationSyncError(
            "notification persistence schema is not ready;"
            f" migrate through {REQUIRED_MIGRATION_VERSION} before sync"
        )


def _idle(
    connection: sqlite3.Connection,
    profile_id: int,
    status: NotificationSyncStatus,
    baseline: int | None,
    cursor: int | None,
    highest_seen: int | None,
) -> NotificationSyncResult:
    """Leave the database byte-identical and describe why nothing happened."""
    connection.execute("ROLLBACK")
    return NotificationSyncResult(
        profile_id,
        status,
        NOTIFICATION_POLICY_VERSION,
        baseline,
        cursor,
        highest_seen,
        (),
        (),
        0,
        0,
        0,
        False,
    )


def _audited_run(connection: sqlite3.Connection, run_id: int, profile_id: int):
    """Return one Portfolio run only when it audits clean for this profile."""
    audit = audit_portfolio_run(connection, run_id)
    if audit.status is not PortfolioAuditStatus.OK or audit.profile_id != profile_id:
        raise NotificationSyncError(
            f"Portfolio run {run_id} is corrupt; refusing to derive notifications"
        )
    run = read_portfolio_run(connection, run_id)
    if run.profile_id != profile_id:
        raise NotificationSyncError(
            f"Portfolio run {run_id} belongs to another profile"
        )
    return run


def _positions(run) -> dict[int, PortfolioPosition]:
    """Read one run's per-opportunity positions from its persisted payloads."""
    positions: dict[int, PortfolioPosition] = {}
    for assessment in run.assessments:
        try:
            reason_codes = assessment.assessment_payload["result"]["reason_codes"]
        except (KeyError, TypeError) as error:
            raise NotificationSyncError(
                f"Portfolio run {run.run_id} has an unreadable assessment payload"
            ) from error
        if not isinstance(reason_codes, tuple) or not all(
            isinstance(code, str) for code in reason_codes
        ):
            raise NotificationSyncError(
                f"Portfolio run {run.run_id} has invalid persisted reason codes"
            )
        positions[assessment.opportunity_id] = PortfolioPosition(
            assessment.opportunity_id,
            assessment.disposition,
            assessment.bucket,
            assessment.priority_category,
            tuple(reason_codes),
        )
    return positions


def _chain(
    connection: sqlite3.Connection,
    profile_id: int,
    highest_seen: int,
    current: int,
) -> tuple[int, ...]:
    """Return the runs to step through from the cursor, oldest first.

    Only run ids above the high-water mark are genuinely new, so only those are
    replayed. Anything at or below it was already walked in an earlier sync;
    landing back on one of those snapshots is a single direct movement from the
    cursor, not a re-run of the ids numbered in between. The walk always starts
    at the cursor, so the first step is cursor to the first id returned here.
    """
    if current > highest_seen:
        rows = connection.execute(
            "SELECT id FROM portfolio_runs WHERE profile_id=? AND id>? AND id<=?"
            " ORDER BY id ASC",
            (profile_id, highest_seen, current),
        ).fetchall()
        chain = tuple(int(row[0]) for row in rows)
        if not chain or chain[-1] != current:
            raise NotificationSyncError(
                "appended Portfolio runs are inconsistent with this profile's history"
            )
        return chain
    return (current,)


def _records(
    connection: sqlite3.Connection,
    profile_id: int,
    steps: tuple[tuple[int, str, int, str, tuple[NotificationTransition, ...]], ...],
) -> tuple[NotificationEventRecord, ...]:
    """Turn every announced movement into one storable, fingerprinted event."""
    opportunity_ids = {
        transition.opportunity_id for _, _, _, _, batch in steps for transition in batch
    }
    metadata = read_opportunity_metadata(connection, sorted(opportunity_ids))
    records: list[NotificationEventRecord] = []
    seen: set[str] = set()
    for previous_id, previous_fingerprint, run_id, run_fingerprint, batch in steps:
        for transition in batch:
            payload = canonical_notification_event_payload(
                transition, metadata[transition.opportunity_id]
            )
            fingerprint = notification_event_fingerprint(
                profile_id=profile_id,
                previous_portfolio_run_fingerprint=previous_fingerprint,
                portfolio_run_fingerprint=run_fingerprint,
                payload=payload,
            )
            # One walk can revisit a movement it already announced a few runs
            # earlier; the fingerprint says so, and it stays a single event.
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            records.append(
                NotificationEventRecord(
                    profile_id,
                    transition.event_type,
                    transition.opportunity_id,
                    previous_id,
                    run_id,
                    fingerprint,
                    canonical_json(payload),
                )
            )
    return tuple(records)


def sync_notification_policy(
    connection: sqlite3.Connection, profile_id: int
) -> NotificationSyncResult:
    """Advance this profile's notification cursor over its audited Portfolio."""
    if (
        isinstance(profile_id, bool)
        or not isinstance(profile_id, int)
        or profile_id <= 0
    ):
        raise NotificationSyncError("profile_id must be a positive integer")
    if connection.in_transaction:
        raise NotificationSyncError(
            "sync_notification_policy requires a connection without an active"
            " transaction"
        )
    connection.execute("BEGIN IMMEDIATE")
    try:
        _preflight(connection)
        state = connection.execute(
            """SELECT baseline_portfolio_run_id,last_processed_portfolio_run_id,
            highest_seen_portfolio_run_id,policy_version
            FROM notification_policy_state WHERE profile_id=?""",
            (profile_id,),
        ).fetchone()
        if state is not None and state[3] != NOTIFICATION_POLICY_VERSION:
            raise NotificationSyncError(
                "stored notification policy version does not match this policy"
            )
        profile_audit = audit_current_portfolio(connection, profile_id)
        if profile_audit.status is PortfolioProfileAuditStatus.CORRUPT:
            raise NotificationSyncError(
                "Portfolio state is corrupt; refusing to derive notifications"
            )
        if profile_audit.status is PortfolioProfileAuditStatus.NOT_SYNCED:
            if state is not None:
                raise NotificationSyncError(
                    "notification state exists without a current Portfolio snapshot"
                )
            return _idle(
                connection,
                profile_id,
                NotificationSyncStatus.NOT_SYNCED,
                None,
                None,
                None,
            )
        current_run_id = profile_audit.current_run_id
        if current_run_id is None:
            raise NotificationSyncError("audited Portfolio state has no current run")
        if state is None:
            # First activation: today's audited snapshot becomes the baseline,
            # and everything already inside it counts as already known.
            connection.execute(
                """INSERT INTO notification_policy_state
                (profile_id,baseline_portfolio_run_id,last_processed_portfolio_run_id,
                 highest_seen_portfolio_run_id,policy_version) VALUES (?,?,?,?,?)""",
                (
                    profile_id,
                    current_run_id,
                    current_run_id,
                    current_run_id,
                    NOTIFICATION_POLICY_VERSION,
                ),
            )
            connection.execute("COMMIT")
            return NotificationSyncResult(
                profile_id,
                NotificationSyncStatus.BASELINE_INITIALIZED,
                NOTIFICATION_POLICY_VERSION,
                current_run_id,
                current_run_id,
                current_run_id,
                (),
                (),
                0,
                0,
                0,
                True,
            )
        baseline, cursor, highest_seen = (int(value) for value in state[:3])
        if highest_seen < cursor or highest_seen < baseline:
            raise NotificationSyncError(
                "notification high-water mark is behind the pointers it bounds"
            )
        if cursor == current_run_id:
            return _idle(
                connection,
                profile_id,
                NotificationSyncStatus.UP_TO_DATE,
                baseline,
                cursor,
                highest_seen,
            )
        previous = _audited_run(connection, cursor, profile_id)
        previous_positions = _positions(previous)
        chain = _chain(connection, profile_id, highest_seen, current_run_id)
        steps = []
        for run_id in chain:
            run = _audited_run(connection, run_id, profile_id)
            positions = _positions(run)
            steps.append(
                (
                    previous.run_id,
                    previous.run_fingerprint,
                    run.run_id,
                    run.run_fingerprint,
                    evaluate_transitions(previous_positions, positions),
                )
            )
            previous, previous_positions = run, positions
        stored = store_notification_events(
            connection, _records(connection, profile_id, tuple(steps))
        )
        # The cursor follows the pointer wherever it went; the high-water mark
        # only ever rises, so a snapshot already walked is never new again.
        highest_seen = max(highest_seen, current_run_id)
        connection.execute(
            """UPDATE notification_policy_state
            SET last_processed_portfolio_run_id=?,highest_seen_portfolio_run_id=?,
            updated_at=CURRENT_TIMESTAMP WHERE profile_id=?""",
            (current_run_id, highest_seen, profile_id),
        )
        connection.execute("COMMIT")
        return NotificationSyncResult(
            profile_id,
            NotificationSyncStatus.PROCESSED,
            NOTIFICATION_POLICY_VERSION,
            baseline,
            current_run_id,
            highest_seen,
            chain,
            stored.event_ids,
            stored.created_count,
            stored.created_count,
            stored.duplicate_count,
            True,
        )
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
