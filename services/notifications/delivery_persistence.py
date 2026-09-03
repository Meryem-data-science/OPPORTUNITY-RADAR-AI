"""Persistence for Web Push deliveries: frozen recipients, attempts, results.

Two tables, and one idea behind both.

``notification_delivery_batches`` is the delivery counterpart of one
``notification_outbox`` row: exactly one batch per outbox row, forever, keyed
by a UNIQUE ``outbox_id``. Materializing an outbox row that already has a
batch is a no-op, which is what makes re-running the orchestrator safe.

``notification_delivery_targets`` is the *snapshot* of who that event goes to.
The recipients are frozen at the instant the batch is materialized: every
subscription that was ACTIVE for that profile right then, and no other, ever.
A subscription registered a minute later is a new device that never saw this
movement and must not be told about it retroactively, and a target already
written is never replaced, re-pointed, or added to. A profile with no active
subscription at that instant gets a batch that is terminal on the spot —
``NO_ACTIVE_SUBSCRIPTIONS`` — rather than a batch that waits forever for a
recipient it will never acquire.

Nothing here opens a socket. The functions are deliberately small so a caller
can hold a SQLite write transaction for exactly one of them and hold none of
them across a network call.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from services.collector.logging_config import get_logger

from .push_subscriptions import (
    PushSubscriptionStatus,
    revoke_push_subscription_by_id,
)

LOGGER = get_logger("services.collector.notifications.delivery")

REQUIRED_MIGRATION_VERSION = "0020"

#: How many times one target is ever attempted, in total. The fifth failure of
#: a retryable error is the last: there is no sixth.
MAX_DELIVERY_ATTEMPTS = 5

#: The wait after attempt 1, 2, 3, and 4. Deterministic and bounded — no
#: jitter, no unbounded growth, and one entry fewer than the attempt limit
#: because the last attempt is never followed by a wait.
RETRY_BACKOFF_SECONDS: tuple[int, ...] = (60, 300, 900, 3600)

#: SQLite's own ``CURRENT_TIMESTAMP`` format, in UTC, so a timestamp this
#: module writes and one the database writes compare and sort identically.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

_TABLES = frozenset({"notification_delivery_batches", "notification_delivery_targets"})


class NotificationDeliveryError(RuntimeError):
    """Raised when a delivery cannot be materialized or recorded safely."""


class DeliveryBatchStatus(StrEnum):
    PENDING = "PENDING"
    NO_ACTIVE_SUBSCRIPTIONS = "NO_ACTIVE_SUBSCRIPTIONS"
    COMPLETED = "COMPLETED"


class DeliveryTargetStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    EXPIRED = "EXPIRED"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"
    FAILED = "FAILED"


class DeliveryErrorCategory(StrEnum):
    RETRYABLE = "RETRYABLE"
    EXPIRED = "EXPIRED"
    PERMANENT = "PERMANENT"


TERMINAL_TARGET_STATUSES = frozenset(
    {
        DeliveryTargetStatus.SENT,
        DeliveryTargetStatus.EXPIRED,
        DeliveryTargetStatus.PERMANENT_FAILURE,
        DeliveryTargetStatus.FAILED,
    }
)

#: Code recorded when a subscription was revoked between materialization and
#: the attempt. It is not a network result: nothing was sent.
SUBSCRIPTION_REVOKED_CODE = "SUBSCRIPTION_REVOKED"
#: Code recorded when the request never produced a status code at all.
TRANSPORT_ERROR_CODE = "TRANSPORT_ERROR"


@dataclass(frozen=True)
class DeliveryOutcome:
    """What one attempt established, in delivery vocabulary rather than HTTP."""

    delivered: bool
    category: DeliveryErrorCategory | None = None
    code: str | None = None

    def __post_init__(self) -> None:
        if self.delivered and (self.category is not None or self.code is not None):
            raise NotificationDeliveryError("a delivered attempt carries no error")
        if not self.delivered and (self.category is None or not self.code):
            raise NotificationDeliveryError("a failed attempt must name its error")


@dataclass(frozen=True)
class DueTarget:
    """One target ready to be attempted, with the credential it needs.

    The endpoint and the two subscription keys are a credential, so this object
    refuses to render itself: it can travel through a traceback, a pytest diff,
    or a log call without ever printing what it carries.
    """

    target_id: int
    batch_id: int
    outbox_id: int
    event_id: int
    profile_id: int
    subscription_id: int
    attempt_count: int
    endpoint: str
    p256dh: str
    auth: str
    payload_json: str

    def __repr__(self) -> str:
        return (
            f"DueTarget(target_id={self.target_id}, batch_id={self.batch_id},"
            f" subscription_id={self.subscription_id},"
            f" attempt_count={self.attempt_count}, credentials=<redacted>)"
        )

    __str__ = __repr__


@dataclass(frozen=True)
class MaterializationResult:
    """What one materialization pass created, described without re-reading it."""

    batch_ids: tuple[int, ...]
    created_batches: int
    created_targets: int
    empty_batches: int


@dataclass(frozen=True)
class AttemptResult:
    """The persisted consequence of one attempt on one target."""

    target_id: int
    applied: bool
    status: DeliveryTargetStatus
    attempt_count: int
    next_attempt_at: str | None
    subscription_revoked: bool
    batch_completed: bool


def now_timestamp(moment: datetime | None = None) -> str:
    """Render an instant the way SQLite's ``CURRENT_TIMESTAMP`` renders it."""
    value = datetime.now(timezone.utc) if moment is None else moment
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.strftime(TIMESTAMP_FORMAT)


def retry_delay_seconds(attempt_count: int) -> int | None:
    """Return the wait after ``attempt_count`` attempts, or None when spent."""
    if attempt_count < 1 or attempt_count >= MAX_DELIVERY_ATTEMPTS:
        return None
    return RETRY_BACKOFF_SECONDS[min(attempt_count - 1, len(RETRY_BACKOFF_SECONDS) - 1)]


def classify_status_code(status_code: int) -> DeliveryOutcome:
    """Turn one push service status code into a delivery outcome.

    A gone endpoint is the only result that says anything about the
    subscription itself. Every other 4xx is this message's problem — a bad
    header, an oversized body — and must never cost a user their device.
    """
    if not isinstance(status_code, int) or isinstance(status_code, bool):
        raise NotificationDeliveryError("push status code must be an integer")
    if 200 <= status_code < 300:
        return DeliveryOutcome(True)
    if status_code in (404, 410):
        return DeliveryOutcome(
            False, DeliveryErrorCategory.EXPIRED, f"HTTP_{status_code}"
        )
    if status_code == 429 or 500 <= status_code < 600:
        return DeliveryOutcome(
            False, DeliveryErrorCategory.RETRYABLE, f"HTTP_{status_code}"
        )
    return DeliveryOutcome(
        False, DeliveryErrorCategory.PERMANENT, f"HTTP_{status_code}"
    )


def transport_failure() -> DeliveryOutcome:
    """A request that produced no status code at all is worth retrying."""
    return DeliveryOutcome(False, DeliveryErrorCategory.RETRYABLE, TRANSPORT_ERROR_CODE)


def preflight(connection: sqlite3.Connection) -> None:
    """Refuse to touch delivery persistence that has not been migrated."""
    try:
        migrated = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?",
            (REQUIRED_MIGRATION_VERSION,),
        ).fetchone()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN"
                " ('notification_delivery_batches','notification_delivery_targets')"
            )
        }
    except sqlite3.Error as error:
        raise NotificationDeliveryError(
            "notification delivery schema is not ready;"
            f" migrate through {REQUIRED_MIGRATION_VERSION} before delivery"
        ) from error
    if migrated is None or tables != _TABLES:
        raise NotificationDeliveryError(
            "notification delivery schema is not ready;"
            f" migrate through {REQUIRED_MIGRATION_VERSION} before delivery"
        )


def _profile_filter(profile_id: int | None) -> tuple[str, tuple[int, ...]]:
    if profile_id is None:
        return "", ()
    if (
        isinstance(profile_id, bool)
        or not isinstance(profile_id, int)
        or profile_id <= 0
    ):
        raise NotificationDeliveryError("profile_id must be a positive integer")
    return " AND notification_events.profile_id=?", (profile_id,)


def materialize_delivery_batches(
    connection: sqlite3.Connection,
    *,
    profile_id: int | None = None,
    now: datetime | None = None,
) -> MaterializationResult:
    """Freeze the recipients of every outbox row that does not have a batch yet.

    One transaction covers discovery, the batch row, and the whole recipient
    snapshot, so a materialization either exists completely or not at all —
    and a second call finds the batch already there and adds nothing.
    """
    if connection.in_transaction:
        raise NotificationDeliveryError(
            "materialize_delivery_batches requires a connection without an"
            " active transaction"
        )
    clause, parameters = _profile_filter(profile_id)
    moment = now_timestamp(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        preflight(connection)
        pending = connection.execute(
            f"""SELECT notification_outbox.id,notification_events.profile_id
            FROM notification_outbox
            JOIN notification_events
              ON notification_events.id=notification_outbox.event_id
            LEFT JOIN notification_delivery_batches
              ON notification_delivery_batches.outbox_id=notification_outbox.id
            WHERE notification_delivery_batches.id IS NULL{clause}
            ORDER BY notification_outbox.id ASC""",
            parameters,
        ).fetchall()
        batch_ids: list[int] = []
        created_targets = empty_batches = 0
        for outbox_id, event_profile_id in pending:
            outbox_id, event_profile_id = int(outbox_id), int(event_profile_id)
            # The snapshot: exactly the subscriptions active at this instant.
            subscription_ids = [
                int(row[0])
                for row in connection.execute(
                    "SELECT id FROM push_subscriptions WHERE profile_id=?"
                    " AND status=? ORDER BY id ASC",
                    (event_profile_id, PushSubscriptionStatus.ACTIVE.value),
                )
            ]
            if subscription_ids:
                status, completed_at = DeliveryBatchStatus.PENDING.value, None
            else:
                status, completed_at = (
                    DeliveryBatchStatus.NO_ACTIVE_SUBSCRIPTIONS.value,
                    moment,
                )
                empty_batches += 1
            inserted = connection.execute(
                """INSERT INTO notification_delivery_batches
                (outbox_id,status,target_count,materialized_at,updated_at,completed_at)
                VALUES (?,?,?,?,?,?) RETURNING id""",
                (
                    outbox_id,
                    status,
                    len(subscription_ids),
                    moment,
                    moment,
                    completed_at,
                ),
            ).fetchone()
            if inserted is None:
                raise NotificationDeliveryError("delivery batch insert returned no row")
            batch_id = int(inserted[0])
            for subscription_id in subscription_ids:
                connection.execute(
                    """INSERT INTO notification_delivery_targets
                    (batch_id,subscription_id,status,attempt_count,next_attempt_at,
                     created_at,updated_at)
                    VALUES (?,?,?,0,?,?,?)""",
                    (
                        batch_id,
                        subscription_id,
                        DeliveryTargetStatus.PENDING.value,
                        moment,
                        moment,
                        moment,
                    ),
                )
            created_targets += len(subscription_ids)
            batch_ids.append(batch_id)
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    if batch_ids:
        LOGGER.info(
            "Notification delivery batches materialized.",
            extra={
                "event": "notification_delivery_materialized",
                "batch_count": len(batch_ids),
                "target_count": created_targets,
                "empty_batch_count": empty_batches,
            },
        )
    return MaterializationResult(
        tuple(batch_ids), len(batch_ids), created_targets, empty_batches
    )


def close_targets_of_revoked_subscriptions(
    connection: sqlite3.Connection,
    *,
    profile_id: int | None = None,
    now: datetime | None = None,
) -> tuple[int, ...]:
    """Close pending targets whose subscription the user has since revoked.

    Nothing is sent to a revoked endpoint, and a batch cannot wait on one
    forever, so such a target is closed without any attempt: no request is
    made, and ``attempt_count`` still moves, because refusing to send *is* the
    disposition of this target.
    """
    if connection.in_transaction:
        raise NotificationDeliveryError(
            "close_targets_of_revoked_subscriptions requires a connection"
            " without an active transaction"
        )
    clause, parameters = _profile_filter(profile_id)
    moment = now_timestamp(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        preflight(connection)
        rows = connection.execute(
            f"""SELECT notification_delivery_targets.id,
            notification_delivery_targets.batch_id,
            notification_delivery_targets.attempt_count
            FROM notification_delivery_targets
            JOIN push_subscriptions
              ON push_subscriptions.id=notification_delivery_targets.subscription_id
            JOIN notification_delivery_batches
              ON notification_delivery_batches.id=notification_delivery_targets.batch_id
            JOIN notification_outbox
              ON notification_outbox.id=notification_delivery_batches.outbox_id
            JOIN notification_events
              ON notification_events.id=notification_outbox.event_id
            WHERE notification_delivery_targets.status=?
              AND push_subscriptions.status<>?{clause}
            ORDER BY notification_delivery_targets.id ASC""",
            (
                DeliveryTargetStatus.PENDING.value,
                PushSubscriptionStatus.ACTIVE.value,
                *parameters,
            ),
        ).fetchall()
        closed: list[int] = []
        for target_id, batch_id, attempt_count in rows:
            connection.execute(
                """UPDATE notification_delivery_targets
                SET status=?,attempt_count=?,next_attempt_at=NULL,last_attempt_at=?,
                    last_error_code=?,last_error_category=?,updated_at=?
                WHERE id=?""",
                (
                    DeliveryTargetStatus.PERMANENT_FAILURE.value,
                    int(attempt_count) + 1,
                    moment,
                    SUBSCRIPTION_REVOKED_CODE,
                    DeliveryErrorCategory.PERMANENT.value,
                    moment,
                    int(target_id),
                ),
            )
            _complete_batch_if_settled(connection, int(batch_id), moment)
            closed.append(int(target_id))
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return tuple(closed)


def read_due_targets(
    connection: sqlite3.Connection,
    *,
    profile_id: int | None = None,
    now: datetime | None = None,
    limit: int | None = None,
) -> tuple[DueTarget, ...]:
    """Read every target that is pending, due, and still on an ACTIVE endpoint.

    This is a read, taken inside one short transaction so the batch of work is
    a consistent snapshot, and released before anything is sent.
    """
    if connection.in_transaction:
        raise NotificationDeliveryError(
            "read_due_targets requires a connection without an active transaction"
        )
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
    ):
        raise NotificationDeliveryError("limit must be a positive integer")
    clause, parameters = _profile_filter(profile_id)
    moment = now_timestamp(now)
    connection.execute("BEGIN")
    try:
        preflight(connection)
        rows = connection.execute(
            f"""SELECT notification_delivery_targets.id,
            notification_delivery_targets.batch_id,
            notification_delivery_batches.outbox_id,notification_events.id,
            notification_events.profile_id,notification_delivery_targets.subscription_id,
            notification_delivery_targets.attempt_count,push_subscriptions.endpoint,
            push_subscriptions.p256dh,push_subscriptions.auth,
            notification_events.payload_json
            FROM notification_delivery_targets
            JOIN notification_delivery_batches
              ON notification_delivery_batches.id=notification_delivery_targets.batch_id
            JOIN notification_outbox
              ON notification_outbox.id=notification_delivery_batches.outbox_id
            JOIN notification_events
              ON notification_events.id=notification_outbox.event_id
            JOIN push_subscriptions
              ON push_subscriptions.id=notification_delivery_targets.subscription_id
            WHERE notification_delivery_targets.status=?
              AND notification_delivery_targets.next_attempt_at<=?
              AND push_subscriptions.status=?{clause}
            ORDER BY notification_delivery_targets.next_attempt_at ASC,
                     notification_delivery_targets.id ASC"""
            + ("" if limit is None else " LIMIT ?"),
            (
                DeliveryTargetStatus.PENDING.value,
                moment,
                PushSubscriptionStatus.ACTIVE.value,
                *parameters,
                *(() if limit is None else (limit,)),
            ),
        ).fetchall()
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return tuple(
        DueTarget(
            int(row[0]),
            int(row[1]),
            int(row[2]),
            int(row[3]),
            int(row[4]),
            int(row[5]),
            int(row[6]),
            row[7],
            row[8],
            row[9],
            row[10],
        )
        for row in rows
    )


def _complete_batch_if_settled(
    connection: sqlite3.Connection, batch_id: int, moment: str
) -> bool:
    """Close a batch once none of its targets is pending any more."""
    remaining = connection.execute(
        "SELECT COUNT(*) FROM notification_delivery_targets"
        " WHERE batch_id=? AND status=?",
        (batch_id, DeliveryTargetStatus.PENDING.value),
    ).fetchone()[0]
    if remaining:
        connection.execute(
            "UPDATE notification_delivery_batches SET updated_at=? WHERE id=?",
            (moment, batch_id),
        )
        return False
    connection.execute(
        """UPDATE notification_delivery_batches
        SET status=?,completed_at=COALESCE(completed_at,?),updated_at=?
        WHERE id=? AND status=?""",
        (
            DeliveryBatchStatus.COMPLETED.value,
            moment,
            moment,
            batch_id,
            DeliveryBatchStatus.PENDING.value,
        ),
    )
    return True


def record_delivery_attempt(
    connection: sqlite3.Connection,
    *,
    target_id: int,
    outcome: DeliveryOutcome,
    now: datetime | None = None,
) -> AttemptResult:
    """Persist the result of one attempt, in one short transaction of its own.

    A target that is no longer pending — already delivered, already terminal —
    is left untouched and reported as not applied. That is what makes a
    re-drain, a crash between the request and this call, or two orchestrators
    racing each other safe: a delivered target is never resent and never
    downgraded.
    """
    if connection.in_transaction:
        raise NotificationDeliveryError(
            "record_delivery_attempt requires a connection without an active"
            " transaction"
        )
    if isinstance(target_id, bool) or not isinstance(target_id, int) or target_id <= 0:
        raise NotificationDeliveryError("target_id must be a positive integer")
    if not isinstance(outcome, DeliveryOutcome):
        raise NotificationDeliveryError("outcome must be a DeliveryOutcome")
    moment = now_timestamp(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        preflight(connection)
        row = connection.execute(
            "SELECT batch_id,subscription_id,status,attempt_count"
            " FROM notification_delivery_targets WHERE id=?",
            (target_id,),
        ).fetchone()
        if row is None:
            raise NotificationDeliveryError("delivery target does not exist")
        batch_id, subscription_id, status, attempt_count = (
            int(row[0]),
            int(row[1]),
            DeliveryTargetStatus(row[2]),
            int(row[3]),
        )
        if status is not DeliveryTargetStatus.PENDING:
            connection.execute("ROLLBACK")
            return AttemptResult(
                target_id, False, status, attempt_count, None, False, False
            )
        attempt_count += 1
        revoked = False
        if outcome.delivered:
            new_status: DeliveryTargetStatus = DeliveryTargetStatus.SENT
            next_attempt_at: str | None = None
        elif outcome.category is DeliveryErrorCategory.EXPIRED:
            new_status, next_attempt_at = DeliveryTargetStatus.EXPIRED, None
            revoked = revoke_push_subscription_by_id(connection, subscription_id)
        elif outcome.category is DeliveryErrorCategory.PERMANENT:
            new_status, next_attempt_at = DeliveryTargetStatus.PERMANENT_FAILURE, None
        else:
            delay = retry_delay_seconds(attempt_count)
            if delay is None:
                new_status, next_attempt_at = DeliveryTargetStatus.FAILED, None
            else:
                new_status = DeliveryTargetStatus.PENDING
                next_attempt_at = now_timestamp(
                    (datetime.now(timezone.utc) if now is None else now)
                    + timedelta(seconds=delay)
                )
        connection.execute(
            """UPDATE notification_delivery_targets
            SET status=?,attempt_count=?,next_attempt_at=?,last_attempt_at=?,
                delivered_at=?,last_error_code=?,last_error_category=?,updated_at=?
            WHERE id=? AND status=?""",
            (
                new_status.value,
                attempt_count,
                next_attempt_at,
                moment,
                moment if new_status is DeliveryTargetStatus.SENT else None,
                outcome.code,
                None if outcome.category is None else outcome.category.value,
                moment,
                target_id,
                DeliveryTargetStatus.PENDING.value,
            ),
        )
        completed = _complete_batch_if_settled(connection, batch_id, moment)
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    LOGGER.info(
        "Notification delivery attempt recorded.",
        extra={
            "event": "notification_delivery_attempt",
            "target_id": target_id,
            "batch_id": batch_id,
            "subscription_id": subscription_id,
            "delivery_status": new_status.value,
            "attempt_count": attempt_count,
            "delivery_error_code": outcome.code,
            "delivery_error_category": (
                None if outcome.category is None else outcome.category.value
            ),
            "subscription_revoked": revoked,
        },
    )
    return AttemptResult(
        target_id, True, new_status, attempt_count, next_attempt_at, revoked, completed
    )


@dataclass(frozen=True)
class DeliveryStatusReport:
    """A read-only census of where every outbox row currently stands.

    Every counter here answers one of the questions delivery has to be able to
    answer without guessing: what has not been materialized yet, what is due,
    what is waiting on a retry, what is done, what had nobody to go to, and
    what will never be delivered.
    """

    profile_id: int | None
    unmaterialized_outbox: int
    batches_pending: int
    batches_completed: int
    batches_without_subscriptions: int
    targets_due: int
    targets_scheduled: int
    targets_sent: int
    targets_expired: int
    targets_permanent_failure: int
    targets_failed: int


def read_delivery_status(
    connection: sqlite3.Connection,
    *,
    profile_id: int | None = None,
    now: datetime | None = None,
) -> DeliveryStatusReport:
    """Describe the delivery backlog without writing anything at all.

    This runs happily on a ``mode=ro`` connection, which is what makes it safe
    to point at an operational database.
    """
    preflight(connection)
    clause, parameters = _profile_filter(profile_id)
    moment = now_timestamp(now)
    unmaterialized = connection.execute(
        f"""SELECT COUNT(*) FROM notification_outbox
        JOIN notification_events
          ON notification_events.id=notification_outbox.event_id
        LEFT JOIN notification_delivery_batches
          ON notification_delivery_batches.outbox_id=notification_outbox.id
        WHERE notification_delivery_batches.id IS NULL{clause}""",
        parameters,
    ).fetchone()[0]
    batches = dict(
        connection.execute(
            f"""SELECT notification_delivery_batches.status,COUNT(*)
            FROM notification_delivery_batches
            JOIN notification_outbox
              ON notification_outbox.id=notification_delivery_batches.outbox_id
            JOIN notification_events
              ON notification_events.id=notification_outbox.event_id
            WHERE 1=1{clause}
            GROUP BY notification_delivery_batches.status""",
            parameters,
        ).fetchall()
    )
    targets = dict(
        connection.execute(
            f"""SELECT notification_delivery_targets.status
            || CASE WHEN notification_delivery_targets.status=? AND
                 notification_delivery_targets.next_attempt_at>? THEN ':SCHEDULED'
               ELSE '' END,COUNT(*)
            FROM notification_delivery_targets
            JOIN notification_delivery_batches
              ON notification_delivery_batches.id=notification_delivery_targets.batch_id
            JOIN notification_outbox
              ON notification_outbox.id=notification_delivery_batches.outbox_id
            JOIN notification_events
              ON notification_events.id=notification_outbox.event_id
            WHERE 1=1{clause}
            GROUP BY 1""",
            (DeliveryTargetStatus.PENDING.value, moment, *parameters),
        ).fetchall()
    )
    return DeliveryStatusReport(
        profile_id,
        int(unmaterialized),
        int(batches.get(DeliveryBatchStatus.PENDING.value, 0)),
        int(batches.get(DeliveryBatchStatus.COMPLETED.value, 0)),
        int(batches.get(DeliveryBatchStatus.NO_ACTIVE_SUBSCRIPTIONS.value, 0)),
        int(targets.get(DeliveryTargetStatus.PENDING.value, 0)),
        int(targets.get(f"{DeliveryTargetStatus.PENDING.value}:SCHEDULED", 0)),
        int(targets.get(DeliveryTargetStatus.SENT.value, 0)),
        int(targets.get(DeliveryTargetStatus.EXPIRED.value, 0)),
        int(targets.get(DeliveryTargetStatus.PERMANENT_FAILURE.value, 0)),
        int(targets.get(DeliveryTargetStatus.FAILED.value, 0)),
    )
