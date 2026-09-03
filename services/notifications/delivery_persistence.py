"""Persistence for Web Push deliveries: frozen recipients, attempts, results.

Two tables, and one idea behind both.

``notification_delivery_batches`` is the delivery counterpart of one
``notification_outbox`` row: exactly one batch per outbox row, forever, keyed
by a UNIQUE ``outbox_id``. Materializing an outbox row that already has a
batch is a no-op, which is what makes re-running the orchestrator safe.

``notification_delivery_targets`` is the *snapshot* of who that event goes to.
The recipients are frozen at the instant the batch is materialized: every
subscription that was ACTIVE for that profile right then **and already active
when the event happened**, and no other, ever. A subscription registered a
minute later is a new device that never saw this movement and must not be told
about it retroactively, and a target already written is never replaced,
re-pointed, or added to.

Freezing alone is not enough for that promise, because materialization can run
long after the event: a device that opted in in between would be ACTIVE at the
instant of the snapshot and would still be receiving history. So eligibility is
decided against the subscription's own activation watermark (migration 0021)
rather than against the moment the snapshot is taken — a subscription is a
recipient of an event only when ``notification_events.id`` is strictly above
the watermark it recorded when it became active. The comparison is between two
ids in one transaction, so it holds however late materialization runs.

A batch with no recipient at that instant is terminal on the spot —
``NO_ACTIVE_SUBSCRIPTIONS`` — rather than a batch that waits forever for a
recipient it will never acquire, and ``empty_reason`` records which of the two
emptinesses it was: nobody was subscribed at all, or everybody who was
subscribed activated too late for this event.

Sending is a claim, not a read. A drain takes a target by moving it to
``IN_FLIGHT`` under a token of its own, inside one ``BEGIN IMMEDIATE``, and
only then goes to the network. Two drains running at the same time therefore
partition the work: SQLite serializes the two transactions, the second sees
the rows the first already claimed, and no target is ever handed to both.

A claim is taken for the target about to be worked on, not for a backlog. A
drain that claimed a hundred targets up front would still be on the tenth when
the ninetieth aged past its lease, and a second drain would then legitimately
recover a row the first was about to send — the exact overlap the claim is
meant to prevent. So ``claimed_at`` is always the moment that target is
actually picked up, and the lease measures the work on one message rather than
on a queue.

What that does *not* buy is exactly-once. An HTTP request to a push service
and a SQLite write cannot be one atomic act, so a process that dies between a
2xx and the row that records it leaves a claim nobody will ever settle. Those
claims are recovered by lease — an ``IN_FLIGHT`` target older than
:data:`CLAIM_LEASE_SECONDS` goes back to ``PENDING`` and can be claimed again —
and that recovery is exactly where a message can be sent twice. So the honest
semantics are:

* concurrent drains never collide, because a claim is exclusive;
* a target that reached ``SENT`` is never sent again, by any drain, ever;
* but after a crash in that one ambiguous window, delivery is *at-least-once*,
  with a redelivery risk bounded by the lease and by the attempt limit.

A lost claim costs no attempt: ``attempt_count`` moves only when a real send
result is recorded, so a crash cannot silently burn a target's retries.

Nothing here opens a socket. The functions are deliberately small so a caller
can hold a SQLite write transaction for exactly one of them and hold none of
them across a network call.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from services.collector.logging_config import get_logger

from .push_subscriptions import (
    WATERMARK_COLUMN,
    PushSubscriptionStatus,
    revoke_push_subscription_by_id,
)

LOGGER = get_logger("services.collector.notifications.delivery")

REQUIRED_MIGRATION_VERSION = "0021"

#: How many times one target is ever attempted, in total. The fifth failure of
#: a retryable error is the last: there is no sixth.
MAX_DELIVERY_ATTEMPTS = 5

#: The wait after attempt 1, 2, 3, and 4. Deterministic and bounded — no
#: jitter, no unbounded growth, and one entry fewer than the attempt limit
#: because the last attempt is never followed by a wait.
RETRY_BACKOFF_SECONDS: tuple[int, ...] = (60, 300, 900, 3600)

#: How long one drain's claim on a target is honoured. A claim older than this
#: belonged to a drain that died, so it is released and the target becomes
#: claimable again. Deterministic and bounded: it is the whole of the window
#: in which a crash can cause a redelivery, so it is generous enough that a
#: live drain never loses a claim it is still working on, and short enough
#: that a dead one does not strand a notification.
CLAIM_LEASE_SECONDS = 300

#: SQLite's own ``CURRENT_TIMESTAMP`` format, in UTC, so a timestamp this
#: module writes and one the database writes compare and sort identically.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

_TABLES = frozenset({"notification_delivery_batches", "notification_delivery_targets"})

#: The column 0021 adds to the batch table, checked by the preflight so a
#: database that stops at 0020 is refused by name instead of by "no such
#: column" from the middle of a materialization.
BATCH_EMPTY_REASON_COLUMN = "empty_reason"


class NotificationDeliveryError(RuntimeError):
    """Raised when a delivery cannot be materialized or recorded safely."""


class DeliveryBatchStatus(StrEnum):
    PENDING = "PENDING"
    NO_ACTIVE_SUBSCRIPTIONS = "NO_ACTIVE_SUBSCRIPTIONS"
    COMPLETED = "COMPLETED"


class DeliveryBatchEmptyReason(StrEnum):
    """Why a batch was born with no recipient, and terminal because of it.

    ``NO_ACTIVE_SUBSCRIPTIONS`` is 0020's original case: the profile had no
    active subscription at all. ``NO_ELIGIBLE_SUBSCRIPTIONS`` is the one 5.3C2
    introduces: there were active subscriptions, and every one of them became
    active at or after this event, so none of them may be told about it. The
    two are very different operationally — one is a user with no device, the
    other a user whose device is working exactly as intended — and an audit
    that could not tell them apart would read the second as a fault.
    """

    NO_ACTIVE_SUBSCRIPTIONS = "NO_ACTIVE_SUBSCRIPTIONS"
    NO_ELIGIBLE_SUBSCRIPTIONS = "NO_ELIGIBLE_SUBSCRIPTIONS"


class DeliveryTargetStatus(StrEnum):
    PENDING = "PENDING"
    IN_FLIGHT = "IN_FLIGHT"
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
class ClaimedTarget:
    """One target this drain holds a claim on, with the credential it needs.

    Holding the claim is what makes it safe to send: no other drain can be
    looking at this row. The endpoint and the two subscription keys are a
    credential, so this object refuses to render itself — it can travel
    through a traceback, a pytest diff, or a log call without ever printing
    what it carries.
    """

    target_id: int
    batch_id: int
    outbox_id: int
    event_id: int
    profile_id: int
    subscription_id: int
    attempt_count: int
    claim_token: str
    endpoint: str
    p256dh: str
    auth: str
    payload_json: str

    def __repr__(self) -> str:
        return (
            f"ClaimedTarget(target_id={self.target_id}, batch_id={self.batch_id},"
            f" subscription_id={self.subscription_id},"
            f" attempt_count={self.attempt_count}, credentials=<redacted>)"
        )

    __str__ = __repr__


@dataclass(frozen=True)
class MaterializationResult:
    """What one materialization pass created, described without re-reading it.

    ``ineligible_batches`` is the subset of ``empty_batches`` that had active
    subscriptions and excluded every one of them for activating too late. It
    is counted separately because it is the expected shape of a first opt-in,
    not a deployment with nobody subscribed.
    """

    batch_ids: tuple[int, ...]
    created_batches: int
    created_targets: int
    empty_batches: int
    ineligible_batches: int


@dataclass(frozen=True)
class AttemptResult:
    """The persisted consequence of one attempt on one target.

    ``applied`` is false when the row was not this caller's to settle: the
    claim had expired and been recovered, another drain owns it now, or the
    target is already terminal. Nothing is written in that case.
    """

    target_id: int
    applied: bool
    status: DeliveryTargetStatus
    attempt_count: int
    next_attempt_at: str | None
    subscription_revoked: bool
    batch_completed: bool


def new_claim_token() -> str:
    """Mint one drain's claim token: unguessable, and 32 hex characters."""
    return secrets.token_hex(16)


def _claim_token(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 32
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise NotificationDeliveryError("claim_token must be 32 hexadecimal characters")
    return value


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
    """Refuse to touch delivery persistence that has not been migrated.

    Delivery now decides eligibility from two columns 0021 adds, so both are
    checked as well as the recorded version. A 0020 database is a database
    whose subscriptions carry no activation boundary at all: reading it with
    this code would either crash halfway through a materialization or, worse,
    have to guess a boundary. It is refused whole instead.
    """
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
        subscription_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(push_subscriptions)")
        }
        batch_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(notification_delivery_batches)"
            )
        }
    except sqlite3.Error as error:
        raise NotificationDeliveryError(
            "notification delivery schema is not ready;"
            f" migrate through {REQUIRED_MIGRATION_VERSION} before delivery"
        ) from error
    if (
        migrated is None
        or tables != _TABLES
        or WATERMARK_COLUMN not in subscription_columns
        or BATCH_EMPTY_REASON_COLUMN not in batch_columns
    ):
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
            f"""SELECT notification_outbox.id,notification_events.profile_id,
            notification_events.id
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
        created_targets = empty_batches = ineligible_batches = 0
        for outbox_id, event_profile_id, event_id in pending:
            outbox_id, event_profile_id, event_id = (
                int(outbox_id),
                int(event_profile_id),
                int(event_id),
            )
            # The snapshot: the subscriptions active at this instant that were
            # already active when this event happened. Strictly above the
            # watermark — a subscription whose watermark *is* this event id
            # activated after it and must never learn of it.
            subscription_ids = [
                int(row[0])
                for row in connection.execute(
                    f"""SELECT id FROM push_subscriptions WHERE profile_id=?
                    AND status=? AND {WATERMARK_COLUMN}<? ORDER BY id ASC""",
                    (event_profile_id, PushSubscriptionStatus.ACTIVE.value, event_id),
                )
            ]
            if subscription_ids:
                status, completed_at = DeliveryBatchStatus.PENDING.value, None
                empty_reason: str | None = None
            else:
                status, completed_at = (
                    DeliveryBatchStatus.NO_ACTIVE_SUBSCRIPTIONS.value,
                    moment,
                )
                # Distinguish "nobody is subscribed" from "everybody subscribed
                # after this happened": one is a user without a device, the
                # other is the boundary working exactly as designed.
                active = connection.execute(
                    "SELECT COUNT(*) FROM push_subscriptions WHERE profile_id=?"
                    " AND status=?",
                    (event_profile_id, PushSubscriptionStatus.ACTIVE.value),
                ).fetchone()[0]
                if active:
                    empty_reason = (
                        DeliveryBatchEmptyReason.NO_ELIGIBLE_SUBSCRIPTIONS.value
                    )
                    ineligible_batches += 1
                else:
                    empty_reason = (
                        DeliveryBatchEmptyReason.NO_ACTIVE_SUBSCRIPTIONS.value
                    )
                empty_batches += 1
            inserted = connection.execute(
                """INSERT INTO notification_delivery_batches
                (outbox_id,status,target_count,materialized_at,updated_at,
                 completed_at,empty_reason)
                VALUES (?,?,?,?,?,?,?) RETURNING id""",
                (
                    outbox_id,
                    status,
                    len(subscription_ids),
                    moment,
                    moment,
                    completed_at,
                    empty_reason,
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
                "ineligible_batch_count": ineligible_batches,
            },
        )
    return MaterializationResult(
        tuple(batch_ids),
        len(batch_ids),
        created_targets,
        empty_batches,
        ineligible_batches,
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
    made, and ``attempt_count`` stays where it is, because nothing was tried.
    Only ``PENDING`` targets are closed — one already claimed belongs to a
    drain that is mid-flight, and is left for that drain to settle.
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
            notification_delivery_targets.batch_id
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
        for target_id, batch_id in rows:
            connection.execute(
                """UPDATE notification_delivery_targets
                SET status=?,next_attempt_at=NULL,last_error_code=?,
                    last_error_category=?,updated_at=?
                WHERE id=? AND status=?""",
                (
                    DeliveryTargetStatus.PERMANENT_FAILURE.value,
                    SUBSCRIPTION_REVOKED_CODE,
                    DeliveryErrorCategory.PERMANENT.value,
                    moment,
                    int(target_id),
                    DeliveryTargetStatus.PENDING.value,
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


def recover_stale_claims(
    connection: sqlite3.Connection,
    *,
    profile_id: int | None = None,
    now: datetime | None = None,
    lease_seconds: int = CLAIM_LEASE_SECONDS,
) -> tuple[int, ...]:
    """Release claims older than the lease, so a dead drain strands nothing.

    This is the one place a message can end up delivered twice: the drain that
    held the claim may have got its 2xx and died before writing it down. The
    alternative — leaving the target claimed forever — loses the notification
    outright, so the lease is deliberate, bounded, and the reason this delivery
    is at-least-once rather than exactly-once.

    A recovered target keeps its ``attempt_count``: a claim nobody settled is
    not evidence that anything was attempted.
    """
    if connection.in_transaction:
        raise NotificationDeliveryError(
            "recover_stale_claims requires a connection without an active transaction"
        )
    if (
        isinstance(lease_seconds, bool)
        or not isinstance(lease_seconds, int)
        or lease_seconds <= 0
    ):
        raise NotificationDeliveryError("lease_seconds must be a positive integer")
    clause, parameters = _profile_filter(profile_id)
    moment = datetime.now(timezone.utc) if now is None else now
    stamp = now_timestamp(moment)
    expiry = now_timestamp(moment - timedelta(seconds=lease_seconds))
    connection.execute("BEGIN IMMEDIATE")
    try:
        preflight(connection)
        rows = connection.execute(
            f"""SELECT notification_delivery_targets.id
            FROM notification_delivery_targets
            JOIN notification_delivery_batches
              ON notification_delivery_batches.id=notification_delivery_targets.batch_id
            JOIN notification_outbox
              ON notification_outbox.id=notification_delivery_batches.outbox_id
            JOIN notification_events
              ON notification_events.id=notification_outbox.event_id
            WHERE notification_delivery_targets.status=?
              AND notification_delivery_targets.claimed_at<=?{clause}
            ORDER BY notification_delivery_targets.id ASC""",
            (DeliveryTargetStatus.IN_FLIGHT.value, expiry, *parameters),
        ).fetchall()
        recovered = [int(row[0]) for row in rows]
        for target_id in recovered:
            connection.execute(
                """UPDATE notification_delivery_targets
                SET status=?,next_attempt_at=?,claim_token=NULL,claimed_at=NULL,
                    updated_at=? WHERE id=? AND status=?""",
                (
                    DeliveryTargetStatus.PENDING.value,
                    stamp,
                    stamp,
                    target_id,
                    DeliveryTargetStatus.IN_FLIGHT.value,
                ),
            )
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    if recovered:
        LOGGER.warning(
            "Recovered abandoned notification delivery claims.",
            extra={
                "event": "notification_delivery_claims_recovered",
                "target_count": len(recovered),
                "lease_seconds": lease_seconds,
            },
        )
    return tuple(recovered)


def release_delivery_claim(
    connection: sqlite3.Connection,
    *,
    target_id: int,
    claim_token: str,
    now: datetime | None = None,
) -> bool:
    """Hand one claim back unused, leaving the target exactly as it was found.

    A drain that cannot even build the message — a stored event it refuses to
    project — has nothing to record: no request was made, so no attempt
    happened. Releasing puts the target straight back in the queue instead of
    stranding a claim for a whole lease over a failure that is immediate and
    knowable.

    Only the holder of the claim can release it, and nothing else about the row
    changes: not ``attempt_count``, not the last error, not the delivery.
    """
    if connection.in_transaction:
        raise NotificationDeliveryError(
            "release_delivery_claim requires a connection without an active transaction"
        )
    if isinstance(target_id, bool) or not isinstance(target_id, int) or target_id <= 0:
        raise NotificationDeliveryError("target_id must be a positive integer")
    token = _claim_token(claim_token)
    moment = now_timestamp(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        preflight(connection)
        released = connection.execute(
            """UPDATE notification_delivery_targets
            SET status=?,next_attempt_at=?,claim_token=NULL,claimed_at=NULL,
                updated_at=? WHERE id=? AND status=? AND claim_token=?""",
            (
                DeliveryTargetStatus.PENDING.value,
                moment,
                moment,
                target_id,
                DeliveryTargetStatus.IN_FLIGHT.value,
                token,
            ),
        ).rowcount
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return released == 1


def claim_due_targets(
    connection: sqlite3.Connection,
    *,
    claim_token: str,
    profile_id: int | None = None,
    now: datetime | None = None,
    limit: int | None = None,
) -> tuple[ClaimedTarget, ...]:
    """Take exclusive ownership of the targets that are due, then let go.

    Selecting and claiming happen inside one ``BEGIN IMMEDIATE``, so a second
    drain either waits for this transaction or sees the rows already
    ``IN_FLIGHT``; either way it gets a disjoint set. The transaction is
    committed before the caller sends anything, so no SQLite lock is ever held
    across a network request.

    Only the rows this call actually moved to ``IN_FLIGHT`` are returned. A
    drain claims one target at a time — see :func:`claim_next_due_target` —
    because a claim is a promise to work on that message now; ``limit`` exists
    so a caller can say how many at once and mean it.
    """
    if connection.in_transaction:
        raise NotificationDeliveryError(
            "claim_due_targets requires a connection without an active transaction"
        )
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
    ):
        raise NotificationDeliveryError("limit must be a positive integer")
    token = _claim_token(claim_token)
    clause, parameters = _profile_filter(profile_id)
    moment = now_timestamp(now)
    connection.execute("BEGIN IMMEDIATE")
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
        claimed = []
        for row in rows:
            # Guarded by status so the claim is the write that decides
            # ownership, not the read that preceded it.
            changed = connection.execute(
                """UPDATE notification_delivery_targets
                SET status=?,next_attempt_at=NULL,claim_token=?,claimed_at=?,
                    updated_at=? WHERE id=? AND status=?""",
                (
                    DeliveryTargetStatus.IN_FLIGHT.value,
                    token,
                    moment,
                    moment,
                    int(row[0]),
                    DeliveryTargetStatus.PENDING.value,
                ),
            ).rowcount
            if changed == 1:
                claimed.append(row)
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return tuple(
        ClaimedTarget(
            int(row[0]),
            int(row[1]),
            int(row[2]),
            int(row[3]),
            int(row[4]),
            int(row[5]),
            int(row[6]),
            token,
            row[7],
            row[8],
            row[9],
            row[10],
        )
        for row in claimed
    )


def claim_next_due_target(
    connection: sqlite3.Connection,
    *,
    claim_token: str,
    profile_id: int | None = None,
    now: datetime | None = None,
) -> ClaimedTarget | None:
    """Claim the single oldest due target, or return None when none is due.

    This is what a drain loops on. Claiming one message at a time is what keeps
    ``claimed_at`` honest: the lease starts when the work on that message
    starts, so a long queue cannot leave a target aging under a claim while the
    drain is busy elsewhere.
    """
    claimed = claim_due_targets(
        connection, claim_token=claim_token, profile_id=profile_id, now=now, limit=1
    )
    return claimed[0] if claimed else None


def _complete_batch_if_settled(
    connection: sqlite3.Connection, batch_id: int, moment: str
) -> bool:
    """Close a batch once every one of its targets has reached a terminal state.

    A target that is merely claimed is still unfinished, so an ``IN_FLIGHT``
    row holds the batch open exactly as a ``PENDING`` one does.
    """
    remaining = connection.execute(
        "SELECT COUNT(*) FROM notification_delivery_targets"
        " WHERE batch_id=? AND status IN (?,?)",
        (
            batch_id,
            DeliveryTargetStatus.PENDING.value,
            DeliveryTargetStatus.IN_FLIGHT.value,
        ),
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
    claim_token: str,
    outcome: DeliveryOutcome,
    now: datetime | None = None,
) -> AttemptResult:
    """Persist the result of one attempt, in one short transaction of its own.

    Only the drain that holds the claim may settle a target: the row must be
    ``IN_FLIGHT`` *and* carry this exact token. Anything else — a target that
    is already terminal, one whose claim expired and was recovered, one another
    drain now owns — is left completely untouched and reported as not applied.
    That is what makes a re-drain, a recovered claim, or two orchestrators
    racing each other safe: a delivered target is never resent and never
    downgraded, and a stale result never overwrites a fresher one.
    """
    if connection.in_transaction:
        raise NotificationDeliveryError(
            "record_delivery_attempt requires a connection without an active"
            " transaction"
        )
    if isinstance(target_id, bool) or not isinstance(target_id, int) or target_id <= 0:
        raise NotificationDeliveryError("target_id must be a positive integer")
    token = _claim_token(claim_token)
    if not isinstance(outcome, DeliveryOutcome):
        raise NotificationDeliveryError("outcome must be a DeliveryOutcome")
    moment = now_timestamp(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        preflight(connection)
        row = connection.execute(
            "SELECT batch_id,subscription_id,status,attempt_count,claim_token"
            " FROM notification_delivery_targets WHERE id=?",
            (target_id,),
        ).fetchone()
        if row is None:
            raise NotificationDeliveryError("delivery target does not exist")
        batch_id, subscription_id, status, attempt_count, held = (
            int(row[0]),
            int(row[1]),
            DeliveryTargetStatus(row[2]),
            int(row[3]),
            row[4],
        )
        if status is not DeliveryTargetStatus.IN_FLIGHT or held != token:
            # Fail closed: this result is not ours to write down.
            connection.execute("ROLLBACK")
            LOGGER.warning(
                "Discarded a delivery result whose claim no longer holds.",
                extra={
                    "event": "notification_delivery_claim_lost",
                    "target_id": target_id,
                    "delivery_status": status.value,
                },
            )
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
        # Settling always releases the claim, whether the target is terminal
        # or going back into the queue for a later retry.
        connection.execute(
            """UPDATE notification_delivery_targets
            SET status=?,attempt_count=?,next_attempt_at=?,claim_token=NULL,
                claimed_at=NULL,last_attempt_at=?,delivered_at=?,last_error_code=?,
                last_error_category=?,updated_at=?
            WHERE id=? AND status=? AND claim_token=?""",
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
                DeliveryTargetStatus.IN_FLIGHT.value,
                token,
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
    what a drain currently holds a claim on, what is waiting on a retry, what
    is done, what had nobody to go to, and what will never be delivered.

    The two empty-batch counters are disjoint and together are every empty
    batch. ``batches_without_subscriptions`` is a profile with no device;
    ``batches_without_eligible_subscriptions`` is a profile whose devices all
    opted in after the event, which is the boundary doing its job rather than a
    delivery that went missing. Migration 0021 gave every batch that predates
    it the only reason it could have had, so nothing falls between the two.
    """

    profile_id: int | None
    unmaterialized_outbox: int
    batches_pending: int
    batches_completed: int
    batches_without_subscriptions: int
    batches_without_eligible_subscriptions: int
    targets_due: int
    targets_scheduled: int
    targets_in_flight: int
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
            f"""SELECT notification_delivery_batches.status
            || CASE WHEN notification_delivery_batches.empty_reason=? THEN ':INELIGIBLE'
               ELSE '' END,COUNT(*)
            FROM notification_delivery_batches
            JOIN notification_outbox
              ON notification_outbox.id=notification_delivery_batches.outbox_id
            JOIN notification_events
              ON notification_events.id=notification_outbox.event_id
            WHERE 1=1{clause}
            GROUP BY 1""",
            (DeliveryBatchEmptyReason.NO_ELIGIBLE_SUBSCRIPTIONS.value, *parameters),
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
        int(
            batches.get(
                f"{DeliveryBatchStatus.NO_ACTIVE_SUBSCRIPTIONS.value}:INELIGIBLE", 0
            )
        ),
        int(targets.get(DeliveryTargetStatus.PENDING.value, 0)),
        int(targets.get(f"{DeliveryTargetStatus.PENDING.value}:SCHEDULED", 0)),
        int(targets.get(DeliveryTargetStatus.IN_FLIGHT.value, 0)),
        int(targets.get(DeliveryTargetStatus.SENT.value, 0)),
        int(targets.get(DeliveryTargetStatus.EXPIRED.value, 0)),
        int(targets.get(DeliveryTargetStatus.PERMANENT_FAILURE.value, 0)),
        int(targets.get(DeliveryTargetStatus.FAILED.value, 0)),
    )
