"""Local orchestration of Web Push delivery: materialize, drain, record.

The network boundary is the whole design. A SQLite write transaction is never
open while a request is in flight, so one drain is a sequence of short, closed
transactions with the slow part strictly between them:

1. one transaction materializes the recipients of any new outbox row;
2. one transaction closes targets whose subscription has since been revoked;
3. one transaction reads the targets that are due, and commits;
4. **no transaction at all** while each message is encrypted and sent;
5. one short transaction per result, recording exactly that one attempt.

Consequences worth stating: a crash anywhere loses at most the record of the
attempts in flight, and re-running the drain is safe because a target that is
already terminal is never re-attempted and never overwritten. The delivery
never recomputes Portfolio, Priority, Matching, or Eligibility, never rewrites
what an event means, and never reads anything outside the outbox, the event it
points at, and the subscriptions frozen for it.

There is no scheduler, no daemon, and no deployment here: something outside
this process decides when a drain happens.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from services.collector.logging_config import get_logger

from .delivery_payload import encode_push_payload
from .delivery_persistence import (
    DeliveryOutcome,
    DeliveryTargetStatus,
    DueTarget,
    MaterializationResult,
    NotificationDeliveryError,
    classify_status_code,
    close_targets_of_revoked_subscriptions,
    materialize_delivery_batches,
    read_due_targets,
    record_delivery_attempt,
    transport_failure,
)
from .web_push import (
    DEFAULT_TTL_SECONDS,
    PushTarget,
    VapidConfiguration,
    WebPushTransport,
    WebPushTransportError,
    build_web_push_request,
)

LOGGER = get_logger("services.collector.notifications.delivery")


@dataclass(frozen=True)
class DeliveryDrainResult:
    """What one drain did, counted rather than re-read."""

    profile_id: int | None
    materialized_batches: int
    materialized_targets: int
    empty_batches: int
    closed_revoked_targets: int
    attempted: int
    sent: int
    retried: int
    expired: int
    permanent_failures: int
    failed: int
    skipped: int
    revoked_subscriptions: int
    completed_batches: int


class WebPushDeliverySender:
    """Encrypts and sends one message, and reports only a delivery outcome.

    The VAPID identity lives here and nowhere else in the delivery path, and
    the transport is injected: a test supplies one that never opens a socket,
    while production supplies :class:`~services.notifications.web_push.HttpxWebPushTransport`.
    """

    def __init__(
        self,
        configuration: VapidConfiguration,
        transport: WebPushTransport,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._configuration = configuration
        self._transport = transport
        self._ttl_seconds = ttl_seconds

    def __call__(self, target: DueTarget, payload: bytes) -> DeliveryOutcome:
        request = build_web_push_request(
            self._configuration,
            PushTarget(target.endpoint, target.p256dh, target.auth),
            payload,
            ttl_seconds=self._ttl_seconds,
        )
        try:
            response = self._transport(request)
        except WebPushTransportError:
            # The error carries no endpoint and no key, and is not re-raised:
            # a push service that is unreachable is a retry, not a crash.
            return transport_failure()
        return classify_status_code(response.status_code)


def materialize_notification_deliveries(
    connection: sqlite3.Connection,
    *,
    profile_id: int | None = None,
    now: datetime | None = None,
) -> MaterializationResult:
    """Freeze recipients for every outbox row that does not have a batch yet."""
    return materialize_delivery_batches(connection, profile_id=profile_id, now=now)


def drain_notification_deliveries(
    connection: sqlite3.Connection,
    sender,
    *,
    profile_id: int | None = None,
    now: datetime | None = None,
    limit: int | None = None,
    materialize: bool = True,
) -> DeliveryDrainResult:
    """Send every due target once, recording each result as it comes back."""
    if connection.in_transaction:
        raise NotificationDeliveryError(
            "drain_notification_deliveries requires a connection without an"
            " active transaction"
        )
    if not callable(sender):
        raise NotificationDeliveryError("sender must be callable")
    materialized = (
        materialize_delivery_batches(connection, profile_id=profile_id, now=now)
        if materialize
        else MaterializationResult((), 0, 0, 0)
    )
    closed = close_targets_of_revoked_subscriptions(
        connection, profile_id=profile_id, now=now
    )
    due = read_due_targets(connection, profile_id=profile_id, now=now, limit=limit)
    # Every payload is built before the first request, so a corrupt stored
    # event fails the drain closed rather than half way through it.
    work = tuple((target, encode_push_payload(target.payload_json)) for target in due)

    counts = dict.fromkeys(
        (
            "attempted",
            "sent",
            "retried",
            "expired",
            "permanent",
            "failed",
            "skipped",
            "revoked",
            "completed",
        ),
        0,
    )
    for target, payload in work:
        outcome = sender(target, payload)
        if not isinstance(outcome, DeliveryOutcome):
            raise NotificationDeliveryError("sender returned an unusable outcome")
        counts["attempted"] += 1
        result = record_delivery_attempt(
            connection, target_id=target.target_id, outcome=outcome, now=now
        )
        if not result.applied:
            counts["skipped"] += 1
            continue
        counts["revoked"] += int(result.subscription_revoked)
        counts["completed"] += int(result.batch_completed)
        counts[
            {
                DeliveryTargetStatus.SENT: "sent",
                DeliveryTargetStatus.PENDING: "retried",
                DeliveryTargetStatus.EXPIRED: "expired",
                DeliveryTargetStatus.PERMANENT_FAILURE: "permanent",
                DeliveryTargetStatus.FAILED: "failed",
            }[result.status]
        ] += 1

    result = DeliveryDrainResult(
        profile_id,
        materialized.created_batches,
        materialized.created_targets,
        materialized.empty_batches,
        len(closed),
        counts["attempted"],
        counts["sent"],
        counts["retried"],
        counts["expired"],
        counts["permanent"],
        counts["failed"],
        counts["skipped"],
        counts["revoked"],
        counts["completed"],
    )
    LOGGER.info(
        "Notification delivery drain finished.",
        extra={
            "event": "notification_delivery_drain",
            "profile_id": profile_id,
            "attempted_count": result.attempted,
            "sent_count": result.sent,
            "retried_count": result.retried,
            "expired_count": result.expired,
            "permanent_failure_count": result.permanent_failures,
            "failed_count": result.failed,
        },
    )
    return result
