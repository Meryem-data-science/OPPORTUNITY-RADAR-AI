"""Durable delivery state for the frozen digest: claim it, settle it, recover it.

Migration 0022 already holds every column this needs — the four statuses, the
attempt counter, the schedule, the claim token and its instant, the Gmail
message id and the bounded error vocabulary. Phase 5.4B adds no migration; it
uses what 5.4A wrote down, and every write here is shaped to satisfy 0022's own
CHECK constraints rather than to work around them.

Nothing in this module sends anything. It does not import a Gmail client, does
not build a MIME message, and does not know a recipient address — only the
fingerprint the row already carries. That separation is what lets the whole
lifecycle be tested against real SQLite with no Google package installed and no
socket in reach.

The transaction discipline, which is the point of the module:

* **claim** — one ``BEGIN IMMEDIATE`` that selects the single oldest due row
  and moves it to ``IN_FLIGHT`` under a fresh token, then commits. SQLite
  serializes two claimers, so the second either waits or sees the row already
  claimed; two drains can never hold the same digest at once.
* **send** — no transaction at all. The network call happens with no lock
  held, which is why a slow or hanging Gmail request cannot block a second
  process, a materialization, or a read.
* **settle** — one ``BEGIN IMMEDIATE`` that writes the result *only* if the
  row is still ``IN_FLIGHT`` under this exact token. A result whose claim was
  recovered in the meantime is discarded rather than applied, so a stale
  outcome can never overwrite a fresher one and a ``SENT`` digest can never be
  downgraded.

Delivery is **at-least-once**, and deliberately so. Gmail accepting a message
and SQLite recording ``SENT`` are two acts that cannot be made one: a process
that dies between them leaves a claim it will never settle, the lease releases
that claim, and the digest is sent again. The alternative — leaving a claim
held forever — loses the email outright. So a crash in that one window can
duplicate a digest, bounded by the lease and by the attempt limit, and nothing
here claims otherwise.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from services.collector.logging_config import get_logger

from .models import DIGEST_VERSION, GmailDigestError, GmailDigestStatus
from .persistence import DIGEST_OUTBOX_TABLE, preflight

LOGGER = get_logger("services.collector.gmail_digest.delivery")

#: How many times one digest is ever attempted, in total. The fifth failure of
#: a retryable error is the last: there is no sixth. Identical to Web Push,
#: because the operational promise is the same one.
MAX_DELIVERY_ATTEMPTS = 5

#: The wait after attempt 1, 2, 3 and 4. Deterministic and bounded — no
#: jitter, no unbounded growth, and one entry fewer than the attempt limit
#: because the last attempt is never followed by a wait.
RETRY_BACKOFF_SECONDS: tuple[int, ...] = (60, 300, 900, 3600)

#: How long one drain's claim on a digest is honoured. A claim older than this
#: belonged to a process that died, so it is released and the digest becomes
#: claimable again. This is the whole of the window in which a crash can cause
#: a duplicate email, which is why it is bounded, deterministic, and generous
#: enough that a live send never loses a claim it is still working on.
CLAIM_LEASE_SECONDS = 300

#: SQLite's own ``CURRENT_TIMESTAMP`` format, in UTC, so an instant this module
#: writes sorts and compares identically to one the database wrote.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 0022's bound on ``last_error_code`` and on ``gmail_message_id``.
MAX_ERROR_CODE_LENGTH = 64
MAX_MESSAGE_ID_LENGTH = 128

#: Recorded when the request never produced an HTTP status at all — a DNS
#: failure, a refused connection, a TLS error, a timeout. Nothing was sent, so
#: it is worth trying again.
TRANSPORT_ERROR_CODE = "TRANSPORT_ERROR"

#: Recorded when authorization failed at send time even though the credential
#: was loaded, refreshed and scope-checked before the digest was claimed. That
#: is a revoked or downgraded grant, not a blip: no number of retries fixes it.
AUTHORIZATION_ERROR_CODE = "AUTHORIZATION_FAILED"

#: Recorded when Gmail accepted a message but named no id for it. The row
#: cannot become SENT without one — 0022 requires the id — and re-sending on a
#: response shape that will recur only mails the user twice, so it is terminal.
MISSING_MESSAGE_ID_CODE = "GMAIL_RESPONSE_WITHOUT_ID"

#: Prefix recorded when the last allowed attempt fails on a retryable cause.
#: The category persisted with it is PERMANENT, because the *decision* is
#: terminal even though the cause was transient; the cause is kept in the code
#: so an operator can still see what was failing.
RETRY_LIMIT_EXHAUSTED_PREFIX = "RETRY_LIMIT_EXHAUSTED_"


class GmailDeliveryError(GmailDigestError):
    """Raised when a delivery cannot be claimed, settled or recovered safely."""


class GmailDeliveryErrorCategory(StrEnum):
    """0022's error vocabulary, and the whole of it."""

    RETRYABLE = "RETRYABLE"
    PERMANENT = "PERMANENT"


#: A digest in one of these states is finished forever. Neither is claimable,
#: and neither can be settled, overwritten or resent by anything.
TERMINAL_STATUSES = frozenset(
    {GmailDigestStatus.SENT, GmailDigestStatus.PERMANENT_FAILURE}
)


def bounded_error_code(code: object) -> str:
    """Normalize one error code into something 0022 will accept and store.

    Codes are a closed, safe vocabulary this repository writes: an HTTP status,
    a named Google reason, a transport failure. Normalizing here — uppercase,
    trimmed, ``[A-Z0-9_]`` only, truncated to the column's 64 characters — is
    the guarantee that no Google response body, message or address can reach
    the database through this column even if a future caller is careless.
    """
    if not isinstance(code, str) or not code.strip():
        raise GmailDeliveryError("error code must be a non-empty string")
    kept: list[str] = []
    for character in code.strip().upper():
        if character.isascii() and character.isalnum():
            kept.append(character)
        elif kept and kept[-1] != "_":
            # Runs of anything unprintable collapse to one separator, so a code
            # stays readable however it was spelled.
            kept.append("_")
    cleaned = "".join(kept).strip("_") or "UNKNOWN"
    return cleaned[:MAX_ERROR_CODE_LENGTH].rstrip("_") or "UNKNOWN"


def exhausted_error_code(code: str) -> str:
    """Name a terminal retry exhaustion without losing what was failing."""
    return bounded_error_code(
        f"{RETRY_LIMIT_EXHAUSTED_PREFIX}{bounded_error_code(code)}"
    )


@dataclass(frozen=True)
class GmailSendOutcome:
    """What one send attempt established, in delivery vocabulary not HTTP.

    A delivered outcome names the Gmail message id and no error; a failed one
    names a bounded code and a category and no id. The constructor refuses
    anything else, so an impossible outcome cannot reach a settlement and turn
    into an impossible row.
    """

    sent: bool
    message_id: str | None = None
    category: GmailDeliveryErrorCategory | None = None
    code: str | None = None

    def __post_init__(self) -> None:
        if self.sent:
            if self.category is not None or self.code is not None:
                raise GmailDeliveryError("a delivered digest carries no error")
            if not isinstance(self.message_id, str) or not self.message_id.strip():
                raise GmailDeliveryError(
                    "a delivered digest must name a Gmail message id"
                )
            if len(self.message_id.strip()) > MAX_MESSAGE_ID_LENGTH:
                raise GmailDeliveryError("Gmail message id is implausibly long")
        else:
            if self.message_id is not None:
                raise GmailDeliveryError("a failed digest names no Gmail message id")
            if self.category is None or not self.code:
                raise GmailDeliveryError("a failed digest must name its error")


@dataclass(frozen=True)
class ClaimedDigest:
    """One frozen digest this drain holds an exclusive claim on.

    It carries the three frozen values a send needs, and refuses to render any
    of them: a subject and two rendered bodies are the user's own opportunity
    shortlist, so this object can travel through a traceback, a pytest diff or
    a log call without printing a word of it. The recipient is not here at all
    — only the fingerprint the row was frozen with — because the address is
    runtime configuration and lives in the sender, never in the queue.
    """

    outbox_id: int
    profile_id: int
    digest_date: str
    digest_version: str
    recipient_fingerprint: str
    content_fingerprint: str
    item_count: int
    attempt_count: int
    claim_token: str
    subject: str
    body_text: str
    body_html: str

    def __repr__(self) -> str:
        return (
            f"ClaimedDigest(outbox_id={self.outbox_id},"
            f" profile_id={self.profile_id}, digest_date={self.digest_date!r},"
            f" item_count={self.item_count},"
            f" attempt_count={self.attempt_count}, message=<redacted>)"
        )

    __str__ = __repr__


@dataclass(frozen=True)
class DeliverySettlement:
    """The persisted consequence of one attempt on one digest.

    ``applied`` is false when the row was not this caller's to settle: the
    claim had expired and been recovered, another drain owns it now, or the
    digest is already terminal. Nothing is written in that case.
    """

    outbox_id: int
    applied: bool
    status: GmailDigestStatus
    attempt_count: int
    next_attempt_at: str | None
    error_code: str | None
    error_category: GmailDeliveryErrorCategory | None


@dataclass(frozen=True)
class GmailDeliveryStatusReport:
    """A read-only census of one profile's digest delivery, safe to print.

    Every counter answers an operational question without disclosing anything:
    what is waiting, what is due now, what some process currently holds, whose
    claim has aged past the lease, what is done, what will never be sent, and
    how many frozen digests are for a mailbox other than the configured one.
    """

    profile_id: int
    digest_version: str
    total: int
    pending: int
    due: int
    scheduled: int
    in_flight: int
    stale_claims: int
    sent: int
    permanent_failure: int
    recipient_matching: int
    recipient_mismatched: int
    due_recipient_mismatched: int


def new_claim_token() -> str:
    """Mint one drain's claim token: unguessable, and 32 hex characters."""
    return secrets.token_hex(16)


def _claim_token(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 32
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise GmailDeliveryError("claim_token must be 32 hexadecimal characters")
    return value


def _positive(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GmailDeliveryError(f"{name} must be a positive integer")
    return value


def _fingerprint(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise GmailDeliveryError(f"{name} must be a 64-character hex digest")
    return value


def _message_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GmailDeliveryError("Gmail message id must be a non-empty string")
    identifier = value.strip()
    if len(identifier) > MAX_MESSAGE_ID_LENGTH:
        raise GmailDeliveryError("Gmail message id is implausibly long")
    return identifier


def now_timestamp(moment: datetime | None = None) -> str:
    """Render an instant the way SQLite's ``CURRENT_TIMESTAMP`` renders it."""
    value = datetime.now(timezone.utc) if moment is None else moment
    if not isinstance(value, datetime):
        raise GmailDeliveryError("now must be a datetime")
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.strftime(TIMESTAMP_FORMAT)


def retry_delay_seconds(attempt_count: int) -> int | None:
    """Return the wait after ``attempt_count`` attempts, or None when spent."""
    if isinstance(attempt_count, bool) or not isinstance(attempt_count, int):
        raise GmailDeliveryError("attempt_count must be an integer")
    if attempt_count < 1 or attempt_count >= MAX_DELIVERY_ATTEMPTS:
        return None
    return RETRY_BACKOFF_SECONDS[min(attempt_count - 1, len(RETRY_BACKOFF_SECONDS) - 1)]


def transport_failure() -> GmailSendOutcome:
    """A request that produced no HTTP status at all is worth retrying."""
    return GmailSendOutcome(
        False,
        category=GmailDeliveryErrorCategory.RETRYABLE,
        code=TRANSPORT_ERROR_CODE,
    )


def authorization_failure() -> GmailSendOutcome:
    """Authorization that failed *after* it was validated will fail again."""
    return GmailSendOutcome(
        False,
        category=GmailDeliveryErrorCategory.PERMANENT,
        code=AUTHORIZATION_ERROR_CODE,
    )


def delivered(message_id: str) -> GmailSendOutcome:
    """Gmail accepted the message and named it."""
    return GmailSendOutcome(True, message_id=_message_id(message_id))


def _profile_and_version(profile_id: int, digest_version: str) -> tuple[int, str]:
    profile = _positive(profile_id, "profile_id")
    if not isinstance(digest_version, str) or not digest_version.strip():
        raise GmailDeliveryError("digest_version must be a non-empty string")
    return profile, digest_version


def recover_stale_claims(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    recipient_fingerprint: str,
    digest_version: str = DIGEST_VERSION,
    now: datetime | None = None,
    lease_seconds: int = CLAIM_LEASE_SECONDS,
) -> tuple[int, ...]:
    """Release this recipient's stale claims, so a dead process strands nothing.

    The recipient fingerprint is required and is part of the query, not a
    filter applied to the result. A delivery run is configured for exactly one
    mailbox, and a digest frozen for a different one is none of its business:
    recovering such a row would write to a digest this run may not send, which
    is the same violation as claiming it. So a mismatched digest is left
    untouched here — still ``IN_FLIGHT``, still holding its claim, still
    carrying the same ``updated_at`` — and is reported instead. Only a run
    configured for *that* recipient may recover it.

    Making the argument required rather than optional is deliberate: an
    accidental omission is a ``TypeError`` at the call site, not a silent
    recovery across every mailbox in the table.

    A recovered digest keeps its ``attempt_count`` exactly as it was. A claim
    nobody settled is not evidence that anything was attempted — the process
    may have died before Gmail was ever called — and charging it an attempt
    would spend a user's retry budget on a crash.

    This is also the one place a digest can end up delivered twice: the process
    that held the claim may have had its message accepted and died before
    writing ``SENT``. That risk is the price of not losing the email, it is
    bounded by the lease and by the attempt limit, and it is why this delivery
    is at-least-once rather than exactly-once.
    """
    profile, version = _profile_and_version(profile_id, digest_version)
    fingerprint = _fingerprint(recipient_fingerprint, "recipient_fingerprint")
    if (
        isinstance(lease_seconds, bool)
        or not isinstance(lease_seconds, int)
        or lease_seconds <= 0
    ):
        raise GmailDeliveryError("lease_seconds must be a positive integer")
    if connection.in_transaction:
        raise GmailDeliveryError(
            "recover_stale_claims requires a connection without an active transaction"
        )
    moment = datetime.now(timezone.utc) if now is None else now
    stamp = now_timestamp(moment)
    expiry = now_timestamp(moment - timedelta(seconds=lease_seconds))
    connection.execute("BEGIN IMMEDIATE")
    try:
        preflight(connection)
        rows = connection.execute(
            f"""SELECT id FROM {DIGEST_OUTBOX_TABLE}
            WHERE profile_id=? AND digest_version=? AND status=? AND claimed_at<=?
              AND recipient_fingerprint=?
            ORDER BY id ASC""",
            (
                profile,
                version,
                GmailDigestStatus.IN_FLIGHT.value,
                expiry,
                fingerprint,
            ),
        ).fetchall()
        recovered = [int(row[0]) for row in rows]
        for outbox_id in recovered:
            # Guarded by the fingerprint as well as by the status, so the
            # write itself — not only the read that chose it — refuses a row
            # belonging to another recipient.
            connection.execute(
                f"""UPDATE {DIGEST_OUTBOX_TABLE}
                SET status=?,next_attempt_at=?,claim_token=NULL,claimed_at=NULL,
                    updated_at=? WHERE id=? AND status=? AND recipient_fingerprint=?""",
                (
                    GmailDigestStatus.PENDING.value,
                    stamp,
                    stamp,
                    outbox_id,
                    GmailDigestStatus.IN_FLIGHT.value,
                    fingerprint,
                ),
            )
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    if recovered:
        LOGGER.warning(
            "Recovered abandoned Gmail digest claims.",
            extra={
                "event": "gmail_digest_claims_recovered",
                "profile_id": profile,
                "recipient_fingerprint": fingerprint,
                "outbox_count": len(recovered),
                "lease_seconds": lease_seconds,
            },
        )
    return tuple(recovered)


def count_recipient_mismatched_due(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    recipient_fingerprint: str,
    digest_version: str = DIGEST_VERSION,
    now: datetime | None = None,
) -> int:
    """Count due digests frozen for a mailbox other than the configured one.

    They are never claimed and never sent, but they are never silently ignored
    either: a due digest that belongs to a different recipient means the
    configured address has changed under a queue that still holds messages for
    the old one, and an operator has to be told that in so many words.
    """
    profile, version = _profile_and_version(profile_id, digest_version)
    fingerprint = _fingerprint(recipient_fingerprint, "recipient_fingerprint")
    moment = now_timestamp(now)
    return int(
        connection.execute(
            f"""SELECT COUNT(*) FROM {DIGEST_OUTBOX_TABLE}
            WHERE profile_id=? AND digest_version=? AND status=?
              AND next_attempt_at<=? AND recipient_fingerprint<>?""",
            (profile, version, GmailDigestStatus.PENDING.value, moment, fingerprint),
        ).fetchone()[0]
    )


def claim_due_digest(
    connection: sqlite3.Connection,
    *,
    claim_token: str,
    profile_id: int,
    recipient_fingerprint: str,
    digest_version: str = DIGEST_VERSION,
    now: datetime | None = None,
) -> ClaimedDigest | None:
    """Take exclusive ownership of the single oldest due digest, then let go.

    The recipient fingerprint is part of the claim predicate, not a check made
    afterwards: a digest frozen for another mailbox is never moved to
    ``IN_FLIGHT`` at all, so a recipient mismatch cannot write a single byte.

    Selecting and claiming happen inside one ``BEGIN IMMEDIATE`` and the
    transaction is committed before this returns, so the caller sends with no
    lock held. One digest at a time, deliberately: a claim is a promise to work
    on that message *now*, and claiming a backlog in advance would leave later
    rows aging past their lease while the first is still in flight — which is
    exactly the situation the lease exists to resolve.
    """
    profile, version = _profile_and_version(profile_id, digest_version)
    fingerprint = _fingerprint(recipient_fingerprint, "recipient_fingerprint")
    token = _claim_token(claim_token)
    if connection.in_transaction:
        raise GmailDeliveryError(
            "claim_due_digest requires a connection without an active transaction"
        )
    moment = now_timestamp(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        preflight(connection)
        row = connection.execute(
            f"""SELECT id,profile_id,digest_date,digest_version,recipient_fingerprint,
            content_fingerprint,item_count,attempt_count,subject,body_text,body_html
            FROM {DIGEST_OUTBOX_TABLE}
            WHERE profile_id=? AND digest_version=? AND status=?
              AND next_attempt_at<=? AND recipient_fingerprint=?
            ORDER BY next_attempt_at ASC,id ASC LIMIT 1""",
            (
                profile,
                version,
                GmailDigestStatus.PENDING.value,
                moment,
                fingerprint,
            ),
        ).fetchone()
        claimed = None
        if row is not None:
            # Guarded by status, so the claim is the write that decides
            # ownership rather than the read that preceded it.
            changed = connection.execute(
                f"""UPDATE {DIGEST_OUTBOX_TABLE}
                SET status=?,next_attempt_at=NULL,claim_token=?,claimed_at=?,
                    updated_at=? WHERE id=? AND status=?""",
                (
                    GmailDigestStatus.IN_FLIGHT.value,
                    token,
                    moment,
                    moment,
                    int(row[0]),
                    GmailDigestStatus.PENDING.value,
                ),
            ).rowcount
            if changed == 1:
                claimed = row
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    if claimed is None:
        return None
    return ClaimedDigest(
        int(claimed[0]),
        int(claimed[1]),
        claimed[2],
        claimed[3],
        claimed[4],
        claimed[5],
        int(claimed[6]),
        int(claimed[7]),
        token,
        claimed[8],
        claimed[9],
        claimed[10],
    )


def release_delivery_claim(
    connection: sqlite3.Connection,
    *,
    outbox_id: int,
    claim_token: str,
    now: datetime | None = None,
) -> bool:
    """Hand one claim back unused, leaving the digest exactly as it was found.

    Nothing else about the row changes — not ``attempt_count``, not the last
    error — because nothing was attempted. This is for the failure that
    happens before any request exists, where stranding the claim for a whole
    lease would be pure delay.
    """
    identifier = _positive(outbox_id, "outbox_id")
    token = _claim_token(claim_token)
    if connection.in_transaction:
        raise GmailDeliveryError(
            "release_delivery_claim requires a connection without an active transaction"
        )
    moment = now_timestamp(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        preflight(connection)
        released = connection.execute(
            f"""UPDATE {DIGEST_OUTBOX_TABLE}
            SET status=?,next_attempt_at=?,claim_token=NULL,claimed_at=NULL,
                updated_at=? WHERE id=? AND status=? AND claim_token=?""",
            (
                GmailDigestStatus.PENDING.value,
                moment,
                moment,
                identifier,
                GmailDigestStatus.IN_FLIGHT.value,
                token,
            ),
        ).rowcount
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return released == 1


def _held_claim(
    connection: sqlite3.Connection, outbox_id: int, token: str
) -> tuple[GmailDigestStatus, int] | None:
    """Return the row's state when this token still owns it, else None."""
    row = connection.execute(
        f"SELECT status,attempt_count,claim_token FROM {DIGEST_OUTBOX_TABLE} WHERE id=?",
        (outbox_id,),
    ).fetchone()
    if row is None:
        raise GmailDeliveryError("digest does not exist")
    try:
        status = GmailDigestStatus(row[0])
    except ValueError as error:
        raise GmailDeliveryError(
            "persisted digest carries an unknown status"
        ) from error
    if status is not GmailDigestStatus.IN_FLIGHT or row[2] != token:
        LOGGER.warning(
            "Discarded a Gmail digest result whose claim no longer holds.",
            extra={
                "event": "gmail_digest_claim_lost",
                "outbox_id": outbox_id,
                "delivery_status": status.value,
            },
        )
        return None
    return status, int(row[1])


def record_delivery_success(
    connection: sqlite3.Connection,
    *,
    outbox_id: int,
    claim_token: str,
    gmail_message_id: str,
    now: datetime | None = None,
) -> DeliverySettlement:
    """Record that Gmail accepted this digest, under this claim and no other.

    A digest that reaches ``SENT`` is finished forever: it carries the instant
    it was sent and the id Gmail gave it, it carries no error, and no later
    pass, recovery or race can claim it again or write over it.
    """
    identifier = _positive(outbox_id, "outbox_id")
    token = _claim_token(claim_token)
    message_id = _message_id(gmail_message_id)
    if connection.in_transaction:
        raise GmailDeliveryError(
            "record_delivery_success requires a connection without an active transaction"
        )
    moment = now_timestamp(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        preflight(connection)
        held = _held_claim(connection, identifier, token)
        if held is None:
            row = connection.execute(
                f"SELECT status,attempt_count FROM {DIGEST_OUTBOX_TABLE} WHERE id=?",
                (identifier,),
            ).fetchone()
            connection.execute("ROLLBACK")
            return DeliverySettlement(
                identifier,
                False,
                GmailDigestStatus(row[0]),
                int(row[1]),
                None,
                None,
                None,
            )
        attempt_count = held[1] + 1
        connection.execute(
            f"""UPDATE {DIGEST_OUTBOX_TABLE}
            SET status=?,attempt_count=?,next_attempt_at=NULL,claim_token=NULL,
                claimed_at=NULL,last_attempt_at=?,sent_at=?,gmail_message_id=?,
                last_error_code=NULL,last_error_category=NULL,updated_at=?
            WHERE id=? AND status=? AND claim_token=?""",
            (
                GmailDigestStatus.SENT.value,
                attempt_count,
                moment,
                moment,
                message_id,
                moment,
                identifier,
                GmailDigestStatus.IN_FLIGHT.value,
                token,
            ),
        )
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    LOGGER.info(
        "Gmail digest sent.",
        extra={
            "event": "gmail_digest_sent",
            "outbox_id": identifier,
            "attempt_count": attempt_count,
        },
    )
    return DeliverySettlement(
        identifier, True, GmailDigestStatus.SENT, attempt_count, None, None, None
    )


def record_delivery_failure(
    connection: sqlite3.Connection,
    *,
    outbox_id: int,
    claim_token: str,
    outcome: GmailSendOutcome,
    now: datetime | None = None,
) -> DeliverySettlement:
    """Record one failed attempt, and decide whether there will be another.

    A permanent cause is terminal on the spot. A retryable one goes back into
    the queue with the next attempt scheduled by the fixed backoff — until the
    attempt budget is spent, at which point the digest becomes
    ``PERMANENT_FAILURE`` and its category becomes ``PERMANENT`` too. That is
    not a re-classification of the cause but a statement about the decision:
    the row now means "this will not be sent", and 0022 refuses to hold a
    terminal row that also says "try again". The original cause survives inside
    the error code, prefixed rather than replaced.
    """
    identifier = _positive(outbox_id, "outbox_id")
    token = _claim_token(claim_token)
    if not isinstance(outcome, GmailSendOutcome) or outcome.sent:
        raise GmailDeliveryError("outcome must be a failed GmailSendOutcome")
    if connection.in_transaction:
        raise GmailDeliveryError(
            "record_delivery_failure requires a connection without an active transaction"
        )
    instant = datetime.now(timezone.utc) if now is None else now
    moment = now_timestamp(instant)
    connection.execute("BEGIN IMMEDIATE")
    try:
        preflight(connection)
        held = _held_claim(connection, identifier, token)
        if held is None:
            row = connection.execute(
                f"SELECT status,attempt_count FROM {DIGEST_OUTBOX_TABLE} WHERE id=?",
                (identifier,),
            ).fetchone()
            connection.execute("ROLLBACK")
            return DeliverySettlement(
                identifier,
                False,
                GmailDigestStatus(row[0]),
                int(row[1]),
                None,
                None,
                None,
            )
        attempt_count = held[1] + 1
        code = bounded_error_code(outcome.code)
        category = outcome.category
        if category is GmailDeliveryErrorCategory.PERMANENT:
            status, next_attempt_at = GmailDigestStatus.PERMANENT_FAILURE, None
        else:
            delay = retry_delay_seconds(attempt_count)
            if delay is None:
                status, next_attempt_at = GmailDigestStatus.PERMANENT_FAILURE, None
                category = GmailDeliveryErrorCategory.PERMANENT
                code = exhausted_error_code(code)
            else:
                status = GmailDigestStatus.PENDING
                next_attempt_at = now_timestamp(instant + timedelta(seconds=delay))
        connection.execute(
            f"""UPDATE {DIGEST_OUTBOX_TABLE}
            SET status=?,attempt_count=?,next_attempt_at=?,claim_token=NULL,
                claimed_at=NULL,last_attempt_at=?,last_error_code=?,
                last_error_category=?,updated_at=?
            WHERE id=? AND status=? AND claim_token=?""",
            (
                status.value,
                attempt_count,
                next_attempt_at,
                moment,
                code,
                category.value,
                moment,
                identifier,
                GmailDigestStatus.IN_FLIGHT.value,
                token,
            ),
        )
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    LOGGER.info(
        "Gmail digest delivery attempt failed.",
        extra={
            "event": "gmail_digest_attempt_failed",
            "outbox_id": identifier,
            "delivery_status": status.value,
            "attempt_count": attempt_count,
            "delivery_error_code": code,
            "delivery_error_category": category.value,
        },
    )
    return DeliverySettlement(
        identifier, True, status, attempt_count, next_attempt_at, code, category
    )


def read_delivery_status(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    recipient_fingerprint: str | None = None,
    digest_version: str = DIGEST_VERSION,
    now: datetime | None = None,
    lease_seconds: int = CLAIM_LEASE_SECONDS,
) -> GmailDeliveryStatusReport:
    """Describe the delivery backlog without writing anything at all.

    This runs unchanged on a ``mode=ro`` connection with ``query_only`` pinned,
    which is what makes it safe to point at a real operational database.
    """
    profile, version = _profile_and_version(profile_id, digest_version)
    fingerprint = (
        None
        if recipient_fingerprint is None
        else _fingerprint(recipient_fingerprint, "recipient_fingerprint")
    )
    if (
        isinstance(lease_seconds, bool)
        or not isinstance(lease_seconds, int)
        or lease_seconds <= 0
    ):
        raise GmailDeliveryError("lease_seconds must be a positive integer")
    preflight(connection)
    instant = datetime.now(timezone.utc) if now is None else now
    moment = now_timestamp(instant)
    expiry = now_timestamp(instant - timedelta(seconds=lease_seconds))
    counts = dict(
        connection.execute(
            f"""SELECT status,COUNT(*) FROM {DIGEST_OUTBOX_TABLE}
            WHERE profile_id=? AND digest_version=? GROUP BY status""",
            (profile, version),
        ).fetchall()
    )
    pending = int(counts.get(GmailDigestStatus.PENDING.value, 0))
    due = int(
        connection.execute(
            f"""SELECT COUNT(*) FROM {DIGEST_OUTBOX_TABLE}
            WHERE profile_id=? AND digest_version=? AND status=? AND next_attempt_at<=?""",
            (profile, version, GmailDigestStatus.PENDING.value, moment),
        ).fetchone()[0]
    )
    stale = int(
        connection.execute(
            f"""SELECT COUNT(*) FROM {DIGEST_OUTBOX_TABLE}
            WHERE profile_id=? AND digest_version=? AND status=? AND claimed_at<=?""",
            (profile, version, GmailDigestStatus.IN_FLIGHT.value, expiry),
        ).fetchone()[0]
    )
    matching = mismatched = due_mismatched = 0
    if fingerprint is not None:
        matching = int(
            connection.execute(
                f"""SELECT COUNT(*) FROM {DIGEST_OUTBOX_TABLE}
                WHERE profile_id=? AND digest_version=? AND recipient_fingerprint=?""",
                (profile, version, fingerprint),
            ).fetchone()[0]
        )
        mismatched = int(
            connection.execute(
                f"""SELECT COUNT(*) FROM {DIGEST_OUTBOX_TABLE}
                WHERE profile_id=? AND digest_version=? AND recipient_fingerprint<>?""",
                (profile, version, fingerprint),
            ).fetchone()[0]
        )
        due_mismatched = count_recipient_mismatched_due(
            connection,
            profile_id=profile,
            recipient_fingerprint=fingerprint,
            digest_version=version,
            now=instant,
        )
    return GmailDeliveryStatusReport(
        profile,
        version,
        sum(int(value) for value in counts.values()),
        pending,
        due,
        pending - due,
        int(counts.get(GmailDigestStatus.IN_FLIGHT.value, 0)),
        stale,
        int(counts.get(GmailDigestStatus.SENT.value, 0)),
        int(counts.get(GmailDigestStatus.PERMANENT_FAILURE.value, 0)),
        matching,
        mismatched,
        due_mismatched,
    )
