"""One pass of Gmail delivery: recover, claim, send, settle, repeat.

The network boundary is the whole design, and it is the same one Web Push
delivery already proved:

1. one transaction releases claims abandoned by a process that died;
2. then, one digest at a time — one short transaction **claims** the single
   oldest due digest under this pass's token and commits;
3. **no transaction at all** while Gmail is called;
4. one short transaction settles exactly that digest, under exactly that
   claim.

Claiming, rather than reading, is what makes two passes safe together: SQLite
serializes step 2, so a second pass either waits or sees the digest already
``IN_FLIGHT``, and the two get disjoint work. A digest is never handed to both,
and one that reached ``SENT`` is never handed to anybody again.

What no amount of care buys is exactly-once. Gmail accepting a message and
SQLite recording ``SENT`` cannot be made one atomic act, so a process that dies
between them leaves a claim it will never settle; the lease releases that claim
and the digest is sent again. Stated plainly:

* concurrent passes never collide, because a claim is exclusive;
* a digest that reached ``SENT`` is never re-sent, by anyone, ever;
* after a crash in that one ambiguous window, delivery is *at-least-once*,
  with a duplicate-email risk bounded by the lease and by the attempt limit.

Nothing here rebuilds a digest. The subject and both bodies come from the
frozen row and go to Gmail unchanged: this pass never reads a Portfolio, never
re-renders, never re-orders, and never recomputes Eligibility, Matching,
Priority or Portfolio.

A digest frozen for a different mailbox is not sent and not touched, and that
holds for *every* step of the pass, recovery included. A stale claim belonging
to another recipient stays exactly as it was found — still ``IN_FLIGHT``, same
token, same ``claimed_at``, same ``updated_at`` — because releasing it would be
a write to a digest this run may not send. It is counted and reported instead,
since a due message for an address that is no longer configured is something an
operator has to be told; only a run configured for that recipient recovers it.

There is no scheduler, no daemon and no deployment here: something outside this
process decides when a pass happens.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from services.collector.logging_config import get_logger

from .delivery_persistence import (
    CLAIM_LEASE_SECONDS,
    ClaimedDigest,
    GmailDeliveryError,
    GmailDeliveryErrorCategory,
    GmailSendOutcome,
    claim_due_digest,
    count_recipient_mismatched_due,
    new_claim_token,
    record_delivery_failure,
    record_delivery_success,
    recover_stale_claims,
    release_delivery_claim,
)
from .fingerprint import recipient_fingerprint
from .models import DIGEST_VERSION, GmailDigestStatus

LOGGER = get_logger("services.collector.gmail_digest.delivery")


@dataclass(frozen=True)
class GmailDeliveryDrainResult:
    """What one pass did, counted rather than re-read.

    ``recipient_mismatched`` is the count of due digests frozen for another
    mailbox: none of them was claimed, sent or modified, and reporting the
    number is how the mismatch stays explicit instead of looking like an empty
    queue.
    """

    profile_id: int
    digest_version: str
    recipient_fingerprint: str
    recovered_claims: int
    recipient_mismatched: int
    claimed: int
    attempted: int
    sent: int
    retried: int
    permanent_failures: int
    skipped: int


def drain_gmail_digests(
    connection: sqlite3.Connection,
    sender,
    *,
    profile_id: int,
    recipient: str,
    digest_version: str = DIGEST_VERSION,
    now: datetime | None = None,
    limit: int | None = None,
    lease_seconds: int = CLAIM_LEASE_SECONDS,
) -> GmailDeliveryDrainResult:
    """Work the due digests one message at a time: claim, send, settle, repeat.

    ``limit`` bounds how many digests this pass attempts, not how many it
    claims ahead of time — a claim is a promise to send that message now, and
    the pass never leaves one claimed behind it.
    """
    if connection.in_transaction:
        raise GmailDeliveryError(
            "drain_gmail_digests requires a connection without an active transaction"
        )
    if not callable(sender):
        raise GmailDeliveryError("sender must be callable")
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
    ):
        raise GmailDeliveryError("limit must be a positive integer")
    # The configured address becomes a fingerprint before any row is looked at,
    # so a malformed recipient fails here rather than halfway through a queue.
    fingerprint = recipient_fingerprint(recipient)
    # Recovery is scoped to the configured mailbox for the same reason the
    # claim is: a run configured for one recipient may not write to a digest
    # frozen for another, and releasing somebody else's stale claim is a write.
    recovered = recover_stale_claims(
        connection,
        profile_id=profile_id,
        recipient_fingerprint=fingerprint,
        digest_version=digest_version,
        now=now,
        lease_seconds=lease_seconds,
    )
    mismatched = count_recipient_mismatched_due(
        connection,
        profile_id=profile_id,
        recipient_fingerprint=fingerprint,
        digest_version=digest_version,
        now=now,
    )
    if mismatched:
        LOGGER.warning(
            "Due Gmail digests are frozen for another recipient and were not sent.",
            extra={
                "event": "gmail_digest_recipient_mismatch",
                "profile_id": profile_id,
                "digest_count": mismatched,
                "recipient_fingerprint": fingerprint,
            },
        )
    # One token for this pass. Everything it claims, only it may settle.
    claim_token = new_claim_token()
    counts = dict.fromkeys(
        ("claimed", "attempted", "sent", "retried", "permanent", "skipped"), 0
    )
    while limit is None or counts["attempted"] < limit:
        claim = claim_due_digest(
            connection,
            claim_token=claim_token,
            profile_id=profile_id,
            recipient_fingerprint=fingerprint,
            digest_version=digest_version,
            now=now,
        )
        if claim is None:
            break
        counts["claimed"] += 1
        outcome = _send(connection, sender, claim, now=now)
        counts["attempted"] += 1
        if outcome.sent:
            settlement = record_delivery_success(
                connection,
                outbox_id=claim.outbox_id,
                claim_token=claim.claim_token,
                gmail_message_id=outcome.message_id,
                now=now,
            )
        else:
            settlement = record_delivery_failure(
                connection,
                outbox_id=claim.outbox_id,
                claim_token=claim.claim_token,
                outcome=outcome,
                now=now,
            )
        if not settlement.applied:
            # The claim was recovered or taken while the message was in flight.
            # The result is not ours to write down, and the row is left alone.
            counts["skipped"] += 1
            continue
        if settlement.status is GmailDigestStatus.SENT:
            counts["sent"] += 1
        elif settlement.status is GmailDigestStatus.PERMANENT_FAILURE:
            counts["permanent"] += 1
        else:
            counts["retried"] += 1

    result = GmailDeliveryDrainResult(
        profile_id,
        digest_version,
        fingerprint,
        len(recovered),
        mismatched,
        counts["claimed"],
        counts["attempted"],
        counts["sent"],
        counts["retried"],
        counts["permanent"],
        counts["skipped"],
    )
    LOGGER.info(
        "Gmail digest delivery pass finished.",
        extra={
            "event": "gmail_digest_delivery_drain",
            "profile_id": profile_id,
            "recovered_claim_count": result.recovered_claims,
            "recipient_mismatched_count": result.recipient_mismatched,
            "claimed_count": result.claimed,
            "attempted_count": result.attempted,
            "sent_count": result.sent,
            "retried_count": result.retried,
            "permanent_failure_count": result.permanent_failures,
            "skipped_count": result.skipped,
        },
    )
    return result


def _send(
    connection: sqlite3.Connection,
    sender,
    claim: ClaimedDigest,
    *,
    now: datetime | None,
) -> GmailSendOutcome:
    """Call the sender with no transaction open, and never strand a claim.

    A sender that raises something the classifier does not recognise is a
    defect, not a delivery result: the claim goes back unused — costing the
    digest no attempt — and the exception propagates instead of being recorded
    as a failure the user's retry budget pays for.
    """
    try:
        outcome = sender(claim)
    except BaseException:
        release_delivery_claim(
            connection,
            outbox_id=claim.outbox_id,
            claim_token=claim.claim_token,
            now=now,
        )
        raise
    if not isinstance(outcome, GmailSendOutcome):
        release_delivery_claim(
            connection,
            outbox_id=claim.outbox_id,
            claim_token=claim.claim_token,
            now=now,
        )
        raise GmailDeliveryError("sender returned an unusable outcome")
    if not outcome.sent and outcome.category not in tuple(GmailDeliveryErrorCategory):
        release_delivery_claim(
            connection,
            outbox_id=claim.outbox_id,
            claim_token=claim.claim_token,
            now=now,
        )
        raise GmailDeliveryError("sender returned an unusable error category")
    return outcome
