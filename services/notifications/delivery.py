"""Local orchestration of Web Push delivery: materialize, drain, record.

The network boundary is the whole design. A SQLite write transaction is never
open while a request is in flight, so one drain is a sequence of short, closed
transactions with the slow part strictly between them:

1. one transaction materializes the recipients of any new outbox row;
2. one transaction releases claims abandoned by a drain that died;
3. one transaction closes targets whose subscription has since been revoked;

then, repeatedly, one message at a time:

4. one short transaction **claims** the single oldest due target — moving it
   to ``IN_FLIGHT`` under this drain's token — and commits;
5. **no transaction at all** while that message is encrypted and sent;
6. one short transaction settling exactly that target, under that claim.

Claiming, rather than reading, is what makes two drains safe together: SQLite
serializes step 4, so the second drain either waits or sees the row already
claimed, and the two get disjoint work. A target is never handed to both.

Step 4 deliberately takes *one* target rather than the whole backlog. A drain
that claimed a hundred up front would still be sending the tenth long after
the hundredth had aged past its lease, at which point a second drain would
recover a row the first was still about to send — two drains, one message,
which is precisely what the claim exists to prevent. Claiming immediately
before each send keeps the lease measuring the work on that one message.
``limit`` therefore bounds how many targets this pass attempts, not how many
it claims ahead of time.

What no amount of care buys here is exactly-once. An HTTP request and a SQLite
write cannot be made one atomic act, so a drain that dies between a 2xx and
step 6 leaves a claim it will never settle. Such claims are released by lease
(:data:`~services.notifications.delivery_persistence.CLAIM_LEASE_SECONDS`) and
the target is attempted again — which is precisely where the same notification
can be delivered twice. Stated plainly:

* concurrent drains never collide, because a claim is exclusive;
* a target that reached ``SENT`` is never sent again, by anyone, ever;
* after a crash in that one ambiguous window, delivery is *at-least-once*,
  with a redelivery risk bounded by the lease and by the attempt limit.

Re-running the drain is otherwise safe: a terminal target is never
re-attempted and never overwritten. The delivery never recomputes Portfolio,
Priority, Matching, or Eligibility, never rewrites what an event means, and
never reads anything outside the outbox, the event it points at, and the
subscriptions frozen for it.

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
    CLAIM_LEASE_SECONDS,
    ClaimedTarget,
    DeliveryOutcome,
    DeliveryTargetStatus,
    MaterializationResult,
    NotificationDeliveryError,
    claim_next_due_target,
    classify_status_code,
    close_targets_of_revoked_subscriptions,
    materialize_delivery_batches,
    new_claim_token,
    record_delivery_attempt,
    recover_stale_claims,
    release_delivery_claim,
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
    recovered_claims: int
    closed_revoked_targets: int
    claimed: int
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

    def __call__(self, target: ClaimedTarget, payload: bytes) -> DeliveryOutcome:
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
    lease_seconds: int = CLAIM_LEASE_SECONDS,
) -> DeliveryDrainResult:
    """Work the backlog one message at a time: claim, send, settle, repeat.

    ``limit`` bounds how many targets this pass attempts. The pass ends when
    nothing is due any more, or when that bound is reached — and it never
    leaves a target claimed behind it.
    """
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
    recovered = recover_stale_claims(
        connection, profile_id=profile_id, now=now, lease_seconds=lease_seconds
    )
    closed = close_targets_of_revoked_subscriptions(
        connection, profile_id=profile_id, now=now
    )
    # One token for this drain. Everything it claims, only it may settle.
    claim_token = new_claim_token()
    counts = dict.fromkeys(
        (
            "claimed",
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
    while limit is None or counts["attempted"] < limit:
        target = claim_next_due_target(
            connection, claim_token=claim_token, profile_id=profile_id, now=now
        )
        if target is None:
            break
        counts["claimed"] += 1
        try:
            # A corrupt stored event fails closed: nothing is sent from a
            # record we do not trust, and nothing is invented for it either.
            payload = encode_push_payload(target.payload_json)
            outcome = sender(target, payload)
        except BaseException:
            # Give the claim back rather than stranding it for a whole lease
            # over a failure that is immediate and repeatable.
            release_delivery_claim(
                connection,
                target_id=target.target_id,
                claim_token=target.claim_token,
                now=now,
            )
            raise
        if not isinstance(outcome, DeliveryOutcome):
            release_delivery_claim(
                connection,
                target_id=target.target_id,
                claim_token=target.claim_token,
                now=now,
            )
            raise NotificationDeliveryError("sender returned an unusable outcome")
        counts["attempted"] += 1
        result = record_delivery_attempt(
            connection,
            target_id=target.target_id,
            claim_token=target.claim_token,
            outcome=outcome,
            now=now,
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
        len(recovered),
        len(closed),
        counts["claimed"],
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
            "recovered_claim_count": result.recovered_claims,
            "claimed_count": result.claimed,
            "attempted_count": result.attempted,
            "sent_count": result.sent,
            "retried_count": result.retried,
            "expired_count": result.expired,
            "permanent_failure_count": result.permanent_failures,
            "failed_count": result.failed,
        },
    )
    return result
