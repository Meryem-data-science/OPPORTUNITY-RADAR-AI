"""Phase 5.4B Gmail delivery, over real SQLite and against a real frozen digest.

Nothing in this module reaches Gmail. Every send goes through an injected fake
transport, one test forbids sockets outright, and another proves a missing
authorization stops the command before a single row is claimed. What *is* real
is everything else: a genuinely migrated database, a real audited Portfolio, a
digest frozen by the unchanged Phase 5.4A materializer, and every claim,
lease, retry and settlement written through the same code an operator runs.
"""

import base64
import hashlib
import json
import socket
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy

import pytest

from services.collector.database.connection import connect_database
from services.gmail_digest import (
    CLAIM_LEASE_SECONDS,
    MAX_DELIVERY_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
    TRANSPORT_ERROR_CODE,
    GmailDeliveryError,
    GmailDeliveryErrorCategory,
    GmailDigestSender,
    GmailDigestStatus,
    GmailSendOutcome,
    claim_due_digest,
    count_recipient_mismatched_due,
    delivery_cli,
    drain_gmail_digests,
    new_claim_token,
    read_delivery_status,
    record_delivery_failure,
    record_delivery_success,
    recover_stale_claims,
    recipient_fingerprint,
)
from tests.integration.test_gmail_digest_sqlite import (
    DAY_ONE,
    DAY_TWO,
    OTHER_RECIPIENT,
    RECIPIENT,
    TIMEZONE,
    digest_fixture,
    included,
    materialize,
    store_run,
)

RETRYABLE = GmailDeliveryErrorCategory.RETRYABLE
PERMANENT = GmailDeliveryErrorCategory.PERMANENT
MESSAGE_ID = "18f0a1b2c3d4e5f6"


# --------------------------------------------------------------------------
# Fixtures: a real frozen digest, and senders that never open a socket
# --------------------------------------------------------------------------


def frozen_digest(tmp_path, *, recipient=RECIPIENT, now=DAY_ONE):
    """A migrated database holding exactly one frozen PENDING digest."""
    connection, identity, ids = digest_fixture(tmp_path)
    store_run(
        connection,
        identity.profile_id,
        {ids[0]: included(), ids[1]: included(), ids[2]: included()},
    )
    result = materialize(connection, identity.profile_id, recipient=recipient, now=now)
    assert result.created
    return connection, identity.profile_id, result.outbox_id


class RecordingTransport:
    """A Gmail transport that records raw messages and never sends one."""

    def __init__(self, *results):
        self.results = list(results) or [{"id": MESSAGE_ID}]
        self.raw_messages = []

    def __call__(self, raw_message):
        self.raw_messages.append(raw_message)
        result = self.results[min(len(self.raw_messages) - 1, len(self.results) - 1)]
        if isinstance(result, BaseException):
            raise result
        return result


def sender(*results, recipient=RECIPIENT):
    return GmailDigestSender(RecordingTransport(*results), recipient=recipient)


def failing_sender(category, code="HTTP_500"):
    def send(claim):
        return GmailSendOutcome(False, category=category, code=code)

    return send


def row(connection, outbox_id):
    return connection.execute(
        """SELECT status,attempt_count,next_attempt_at,claim_token,claimed_at,
        last_attempt_at,sent_at,gmail_message_id,last_error_code,last_error_category
        FROM gmail_digest_outbox WHERE id=?""",
        (outbox_id,),
    ).fetchone()


def status_of(connection, outbox_id):
    return row(connection, outbox_id)[0]


def database_path(connection):
    return connection.execute("PRAGMA database_list").fetchone()[2]


def file_digest(connection):
    connection.commit()
    with open(database_path(connection), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def drain(connection, profile_id, send, *, recipient=RECIPIENT, **extra):
    return drain_gmail_digests(
        connection, send, profile_id=profile_id, recipient=recipient, **extra
    )


# --------------------------------------------------------------------------
# The message that goes out is the frozen one
# --------------------------------------------------------------------------


def test_the_sent_message_is_exactly_what_5_4a_froze(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)
    subject, body_text, body_html = connection.execute(
        "SELECT subject,body_text,body_html FROM gmail_digest_outbox WHERE id=?",
        (outbox_id,),
    ).fetchone()
    transport = RecordingTransport({"id": MESSAGE_ID})

    drain(connection, profile_id, GmailDigestSender(transport, recipient=RECIPIENT))

    message = message_from_bytes(
        base64.urlsafe_b64decode(transport.raw_messages[0]), policy=policy.default
    )
    parts = {
        part.get_content_type(): part.get_content()
        for part in message.walk()
        if not part.is_multipart()
    }
    assert message.get_content_type() == "multipart/alternative"
    assert message["Subject"] == subject
    assert message["To"] == RECIPIENT
    assert parts["text/plain"].replace("\r\n", "\n") == body_text
    assert parts["text/html"].replace("\r\n", "\n").rstrip("\n") == body_html.rstrip(
        "\n"
    )


def test_delivery_never_touches_the_portfolio_or_rewrites_the_digest(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)
    frozen = connection.execute(
        """SELECT subject,body_text,body_html,content_fingerprint,item_count,
        portfolio_run_id,recipient_fingerprint FROM gmail_digest_outbox WHERE id=?""",
        (outbox_id,),
    ).fetchone()
    portfolio = connection.execute(
        "SELECT id,run_fingerprint FROM portfolio_runs ORDER BY id"
    ).fetchall()

    drain(connection, profile_id, sender())

    assert (
        connection.execute(
            """SELECT subject,body_text,body_html,content_fingerprint,item_count,
            portfolio_run_id,recipient_fingerprint FROM gmail_digest_outbox WHERE id=?""",
            (outbox_id,),
        ).fetchone()
        == frozen
    )
    assert (
        connection.execute(
            "SELECT id,run_fingerprint FROM portfolio_runs ORDER BY id"
        ).fetchall()
        == portfolio
    )


# --------------------------------------------------------------------------
# Recipient identity guard
# --------------------------------------------------------------------------


def test_a_matching_fingerprint_may_be_claimed_and_sent(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)

    result = drain(connection, profile_id, sender())

    assert (result.sent, result.recipient_mismatched) == (1, 0)
    assert status_of(connection, outbox_id) == GmailDigestStatus.SENT.value


def test_a_digest_frozen_for_another_mailbox_is_neither_sent_nor_touched(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(
        tmp_path, recipient=OTHER_RECIPIENT
    )
    before = row(connection, outbox_id)
    transport = RecordingTransport()

    result = drain(
        connection, profile_id, GmailDigestSender(transport, recipient=RECIPIENT)
    )

    assert transport.raw_messages == []
    assert (result.claimed, result.attempted, result.sent) == (0, 0, 0)
    assert row(connection, outbox_id) == before
    assert status_of(connection, outbox_id) == GmailDigestStatus.PENDING.value


def test_a_recipient_mismatch_is_explicit_rather_than_an_empty_queue(tmp_path):
    connection, profile_id, _ = frozen_digest(tmp_path, recipient=OTHER_RECIPIENT)

    result = drain(connection, profile_id, sender())

    assert result.recipient_mismatched == 1
    assert result.recipient_fingerprint == recipient_fingerprint(RECIPIENT)
    assert (
        count_recipient_mismatched_due(
            connection,
            profile_id=profile_id,
            recipient_fingerprint=recipient_fingerprint(RECIPIENT),
        )
        == 1
    )


def test_a_mismatched_digest_cannot_be_claimed_even_directly(tmp_path):
    connection, profile_id, _ = frozen_digest(tmp_path, recipient=OTHER_RECIPIENT)

    claimed = claim_due_digest(
        connection,
        claim_token=new_claim_token(),
        profile_id=profile_id,
        recipient_fingerprint=recipient_fingerprint(RECIPIENT),
    )

    assert claimed is None


def test_the_raw_recipient_is_never_persisted(tmp_path):
    connection, profile_id, _ = frozen_digest(tmp_path)

    drain(connection, profile_id, sender())

    connection.commit()
    with open(database_path(connection), "rb") as handle:
        contents = handle.read()
    assert RECIPIENT.encode("utf-8") not in contents
    assert recipient_fingerprint(RECIPIENT).encode("ascii") in contents


def test_a_malformed_configured_recipient_stops_before_any_row_is_read(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)
    before = row(connection, outbox_id)

    with pytest.raises(Exception):
        drain(connection, profile_id, sender(), recipient="not-an-address")

    assert row(connection, outbox_id) == before


# --------------------------------------------------------------------------
# Claim, transaction boundary, concurrency
# --------------------------------------------------------------------------


def test_a_due_digest_is_claimed_with_a_32_hex_token(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)
    token = new_claim_token()

    claimed = claim_due_digest(
        connection,
        claim_token=token,
        profile_id=profile_id,
        recipient_fingerprint=recipient_fingerprint(RECIPIENT),
    )

    status, _, next_attempt_at, stored_token, claimed_at = row(connection, outbox_id)[
        :5
    ]
    assert claimed.outbox_id == outbox_id
    assert len(token) == 32 and int(token, 16) >= 0
    assert (status, next_attempt_at) == (GmailDigestStatus.IN_FLIGHT.value, None)
    assert stored_token == token and claimed_at is not None


def test_the_network_call_happens_with_no_transaction_open(tmp_path):
    connection, profile_id, _ = frozen_digest(tmp_path)
    observed = []

    def send(claim):
        observed.append(connection.in_transaction)
        return GmailSendOutcome(True, message_id=MESSAGE_ID)

    drain(connection, profile_id, send)

    assert observed == [False]


def test_two_drains_never_claim_the_same_live_digest(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)
    path = database_path(connection)
    connection.commit()
    fingerprint = recipient_fingerprint(RECIPIENT)
    claims = []
    barrier = threading.Barrier(2)

    def claimer():
        with connect_database(path) as other:
            other.execute("PRAGMA busy_timeout = 5000")
            barrier.wait()
            claims.append(
                claim_due_digest(
                    other,
                    claim_token=new_claim_token(),
                    profile_id=profile_id,
                    recipient_fingerprint=fingerprint,
                )
            )

    threads = [threading.Thread(target=claimer) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(claim is None for claim in claims) == [False, True]
    assert status_of(connection, outbox_id) == GmailDigestStatus.IN_FLIGHT.value


def test_two_concurrent_drains_send_one_digest_exactly_once(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)
    path = database_path(connection)
    connection.commit()
    sent = []
    lock = threading.Lock()

    def send(claim):
        with lock:
            sent.append(claim.outbox_id)
        return GmailSendOutcome(True, message_id=MESSAGE_ID)

    def drainer():
        with connect_database(path) as other:
            other.execute("PRAGMA busy_timeout = 5000")
            drain(other, profile_id, send)

    threads = [threading.Thread(target=drainer) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sent == [outbox_id]
    assert status_of(connection, outbox_id) == GmailDigestStatus.SENT.value


@pytest.mark.parametrize(
    "terminal", [GmailDigestStatus.SENT, GmailDigestStatus.PERMANENT_FAILURE]
)
def test_a_terminal_digest_can_never_be_claimed_again(tmp_path, terminal):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)
    if terminal is GmailDigestStatus.SENT:
        drain(connection, profile_id, sender())
    else:
        drain(connection, profile_id, failing_sender(PERMANENT, "HTTP_400"))
    assert status_of(connection, outbox_id) == terminal.value
    before = row(connection, outbox_id)

    result = drain(connection, profile_id, sender())

    assert (result.claimed, result.attempted) == (0, 0)
    assert row(connection, outbox_id) == before


def test_only_the_holder_of_a_claim_may_settle_it(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)
    claim_due_digest(
        connection,
        claim_token=new_claim_token(),
        profile_id=profile_id,
        recipient_fingerprint=recipient_fingerprint(RECIPIENT),
    )

    settlement = record_delivery_success(
        connection,
        outbox_id=outbox_id,
        claim_token="f" * 32,
        gmail_message_id=MESSAGE_ID,
    )

    assert settlement.applied is False
    assert status_of(connection, outbox_id) == GmailDigestStatus.IN_FLIGHT.value


def test_a_failure_cannot_be_settled_without_the_claim_either(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)
    claim_due_digest(
        connection,
        claim_token=new_claim_token(),
        profile_id=profile_id,
        recipient_fingerprint=recipient_fingerprint(RECIPIENT),
    )
    before = row(connection, outbox_id)

    settlement = record_delivery_failure(
        connection,
        outbox_id=outbox_id,
        claim_token="f" * 32,
        outcome=GmailSendOutcome(False, category=RETRYABLE, code="HTTP_503"),
    )

    assert settlement.applied is False
    assert row(connection, outbox_id) == before


def test_a_result_whose_claim_was_recovered_is_discarded_not_applied(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)
    stale = new_claim_token()
    claim_due_digest(
        connection,
        claim_token=stale,
        profile_id=profile_id,
        recipient_fingerprint=recipient_fingerprint(RECIPIENT),
        now=DAY_ONE,
    )
    recover_stale_claims(
        connection,
        profile_id=profile_id,
        now=DAY_ONE + timedelta(seconds=CLAIM_LEASE_SECONDS + 1),
    )

    settlement = record_delivery_success(
        connection, outbox_id=outbox_id, claim_token=stale, gmail_message_id=MESSAGE_ID
    )

    assert settlement.applied is False
    assert status_of(connection, outbox_id) == GmailDigestStatus.PENDING.value


# --------------------------------------------------------------------------
# Stale claim recovery
# --------------------------------------------------------------------------


def test_a_claim_older_than_the_lease_is_recovered(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)
    claim_due_digest(
        connection,
        claim_token=new_claim_token(),
        profile_id=profile_id,
        recipient_fingerprint=recipient_fingerprint(RECIPIENT),
        now=DAY_ONE,
    )

    recovered = recover_stale_claims(
        connection,
        profile_id=profile_id,
        now=DAY_ONE + timedelta(seconds=CLAIM_LEASE_SECONDS + 1),
    )

    status, attempts, next_attempt_at, token, claimed_at = row(connection, outbox_id)[
        :5
    ]
    assert recovered == (outbox_id,)
    assert status == GmailDigestStatus.PENDING.value
    assert (token, claimed_at) == (None, None)
    assert next_attempt_at is not None


def test_a_fresh_claim_is_never_recovered_from_under_a_live_send(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)
    token = new_claim_token()
    claim_due_digest(
        connection,
        claim_token=token,
        profile_id=profile_id,
        recipient_fingerprint=recipient_fingerprint(RECIPIENT),
        now=DAY_ONE,
    )

    recovered = recover_stale_claims(
        connection,
        profile_id=profile_id,
        now=DAY_ONE + timedelta(seconds=CLAIM_LEASE_SECONDS - 1),
    )

    assert recovered == ()
    assert row(connection, outbox_id)[3] == token


def test_recovering_a_crashed_claim_costs_the_digest_no_attempt(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)
    claim_due_digest(
        connection,
        claim_token=new_claim_token(),
        profile_id=profile_id,
        recipient_fingerprint=recipient_fingerprint(RECIPIENT),
        now=DAY_ONE,
    )
    assert row(connection, outbox_id)[1] == 0

    recover_stale_claims(
        connection,
        profile_id=profile_id,
        now=DAY_ONE + timedelta(seconds=CLAIM_LEASE_SECONDS + 1),
    )

    assert row(connection, outbox_id)[1] == 0
    assert row(connection, outbox_id)[5] is None


# --------------------------------------------------------------------------
# At-least-once, stated rather than papered over
# --------------------------------------------------------------------------


def test_a_send_accepted_before_a_crash_can_be_delivered_twice(tmp_path):
    """The ambiguity is real, bounded, and documented — not pretended away.

    Gmail accepting the message and SQLite recording SENT cannot be one atomic
    act. Here the first pass gets its message id and dies before settling; the
    lease later releases the claim and the digest is sent again. That second
    email is the cost of never losing the first one, and it is why this
    delivery is at-least-once rather than exactly-once.
    """
    connection, profile_id, outbox_id = frozen_digest(tmp_path)
    accepted = []

    def crashing_sender(claim):
        accepted.append(claim.outbox_id)
        raise KeyboardInterrupt("process killed after Gmail accepted the message")

    with pytest.raises(KeyboardInterrupt):
        drain(connection, profile_id, crashing_sender, now=DAY_ONE)
    # The claim released on the way out is the *known* failure; simulate the
    # harder one, where the process never got that far.
    claim_due_digest(
        connection,
        claim_token=new_claim_token(),
        profile_id=profile_id,
        recipient_fingerprint=recipient_fingerprint(RECIPIENT),
        now=DAY_ONE,
    )
    assert status_of(connection, outbox_id) == GmailDigestStatus.IN_FLIGHT.value

    later = DAY_ONE + timedelta(seconds=CLAIM_LEASE_SECONDS + 1)
    result = drain(connection, profile_id, sender(), now=later)

    assert (result.recovered_claims, result.sent) == (1, 1)
    assert accepted == [outbox_id]
    assert status_of(connection, outbox_id) == GmailDigestStatus.SENT.value


# --------------------------------------------------------------------------
# Success
# --------------------------------------------------------------------------


def test_a_sent_digest_records_the_instant_and_the_gmail_id(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)

    drain(connection, profile_id, sender(), now=DAY_ONE)

    (
        status,
        attempts,
        next_attempt_at,
        token,
        claimed_at,
        last_attempt_at,
        sent_at,
        message_id,
        error_code,
        error_category,
    ) = row(connection, outbox_id)
    assert status == GmailDigestStatus.SENT.value
    assert attempts == 1
    assert (next_attempt_at, token, claimed_at) == (None, None, None)
    assert last_attempt_at is not None and sent_at is not None
    assert message_id == MESSAGE_ID
    assert (error_code, error_category) == (None, None)


def test_a_sent_digest_is_never_sent_again(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)
    drain(connection, profile_id, sender())
    transport = RecordingTransport()

    result = drain(
        connection, profile_id, GmailDigestSender(transport, recipient=RECIPIENT)
    )

    assert transport.raw_messages == []
    assert (result.claimed, result.sent) == (0, 0)
    assert row(connection, outbox_id)[1] == 1


def test_an_acceptance_without_an_id_never_becomes_sent(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)

    drain(connection, profile_id, sender({"threadId": "abc"}))

    status, _, _, _, _, _, sent_at, message_id, code, category = row(
        connection, outbox_id
    )
    assert status == GmailDigestStatus.PERMANENT_FAILURE.value
    assert (sent_at, message_id) == (None, None)
    assert (code, category) == ("GMAIL_RESPONSE_WITHOUT_ID", PERMANENT.value)


# --------------------------------------------------------------------------
# Retry
# --------------------------------------------------------------------------


def test_a_transport_failure_schedules_the_first_backoff(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)

    result = drain(
        connection,
        profile_id,
        sender(ConnectionResetError("reset")),
        now=DAY_ONE,
    )

    status, attempts, next_attempt_at, token, claimed_at = row(connection, outbox_id)[
        :5
    ]
    assert (result.retried, result.sent) == (1, 0)
    assert (status, attempts) == (GmailDigestStatus.PENDING.value, 1)
    assert next_attempt_at == (DAY_ONE + timedelta(seconds=60)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    assert (token, claimed_at) == (None, None)
    assert row(connection, outbox_id)[8:] == (TRANSPORT_ERROR_CODE, RETRYABLE.value)


def test_the_backoff_schedule_is_60_300_900_3600(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)
    scheduled = []

    moment = DAY_ONE
    for expected in RETRY_BACKOFF_SECONDS:
        drain(connection, profile_id, failing_sender(RETRYABLE, "HTTP_503"), now=moment)
        scheduled.append(row(connection, outbox_id)[2])
        moment = moment + timedelta(seconds=expected)

    assert scheduled == [
        (DAY_ONE + timedelta(seconds=offset)).strftime("%Y-%m-%d %H:%M:%S")
        for offset in (60, 60 + 300, 60 + 300 + 900, 60 + 300 + 900 + 3600)
    ]


def test_a_digest_is_not_due_before_its_backoff_has_elapsed(tmp_path):
    connection, profile_id, _ = frozen_digest(tmp_path)
    drain(connection, profile_id, failing_sender(RETRYABLE, "HTTP_503"), now=DAY_ONE)

    early = drain(connection, profile_id, sender(), now=DAY_ONE + timedelta(seconds=59))
    late = drain(connection, profile_id, sender(), now=DAY_ONE + timedelta(seconds=61))

    assert (early.claimed, late.sent) == (0, 1)


def test_the_fifth_retryable_failure_is_terminal(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)

    moment = DAY_ONE
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        drain(connection, profile_id, failing_sender(RETRYABLE, "HTTP_503"), now=moment)
        moment = moment + timedelta(hours=2)

    status, attempts, next_attempt_at = row(connection, outbox_id)[:3]
    code, category = row(connection, outbox_id)[8:]
    assert status == GmailDigestStatus.PERMANENT_FAILURE.value
    assert (attempts, next_attempt_at) == (MAX_DELIVERY_ATTEMPTS, None)
    assert category == PERMANENT.value
    assert code == "RETRY_LIMIT_EXHAUSTED_HTTP_503"


def test_exhaustion_keeps_the_cause_inside_a_schema_valid_permanent_row(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)

    moment = DAY_ONE
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        drain(connection, profile_id, failing_sender(RETRYABLE, "HTTP_429"), now=moment)
        moment = moment + timedelta(hours=2)

    code, category = row(connection, outbox_id)[8:]
    assert "HTTP_429" in code and len(code) <= 64
    assert category == PERMANENT.value
    # The row satisfies 0022 on its own terms, checked by the database itself.
    connection.execute("PRAGMA integrity_check")


# --------------------------------------------------------------------------
# Permanent failure
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code", ["HTTP_400", "HTTP_401", "HTTP_403_INSUFFICIENT_PERMISSIONS", "HTTP_404"]
)
def test_a_permanent_error_is_terminal_on_the_first_attempt(tmp_path, code):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)

    result = drain(connection, profile_id, failing_sender(PERMANENT, code), now=DAY_ONE)

    status, attempts, next_attempt_at, token, claimed_at = row(connection, outbox_id)[
        :5
    ]
    assert result.permanent_failures == 1
    assert (status, attempts, next_attempt_at) == (
        GmailDigestStatus.PERMANENT_FAILURE.value,
        1,
        None,
    )
    assert (token, claimed_at) == (None, None)
    assert row(connection, outbox_id)[8:] == (code, PERMANENT.value)


def test_no_google_response_body_is_ever_persisted(tmp_path):
    connection, profile_id, _ = frozen_digest(tmp_path)
    leak = "Recipient person@secret.invalid rejected by policy 42"

    drain(connection, profile_id, failing_sender(PERMANENT, leak))

    connection.commit()
    with open(database_path(connection), "rb") as handle:
        contents = handle.read()
    assert b"secret.invalid" not in contents and b"rejected by policy" not in contents


def test_a_sender_returning_nonsense_strands_no_claim(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)

    with pytest.raises(GmailDeliveryError):
        drain(connection, profile_id, lambda claim: "sent, honest")

    status, attempts = row(connection, outbox_id)[:2]
    assert (status, attempts) == (GmailDigestStatus.PENDING.value, 0)


# --------------------------------------------------------------------------
# Status reporting
# --------------------------------------------------------------------------


def test_the_status_report_counts_every_operational_shape(tmp_path):
    connection, profile_id, outbox_id = frozen_digest(tmp_path)
    fingerprint = recipient_fingerprint(RECIPIENT)

    due = read_delivery_status(
        connection, profile_id=profile_id, recipient_fingerprint=fingerprint
    )
    drain(connection, profile_id, sender())
    sent = read_delivery_status(
        connection, profile_id=profile_id, recipient_fingerprint=fingerprint
    )

    assert (due.total, due.pending, due.due, due.sent) == (1, 1, 1, 0)
    assert (sent.sent, sent.pending, sent.due, sent.in_flight) == (1, 0, 0, 0)
    assert (sent.recipient_matching, sent.recipient_mismatched) == (1, 0)


def test_the_status_report_counts_a_stale_claim(tmp_path):
    connection, profile_id, _ = frozen_digest(tmp_path)
    claim_due_digest(
        connection,
        claim_token=new_claim_token(),
        profile_id=profile_id,
        recipient_fingerprint=recipient_fingerprint(RECIPIENT),
        now=DAY_ONE,
    )

    report = read_delivery_status(
        connection,
        profile_id=profile_id,
        recipient_fingerprint=recipient_fingerprint(RECIPIENT),
        now=DAY_ONE + timedelta(seconds=CLAIM_LEASE_SECONDS + 1),
    )

    assert (report.in_flight, report.stale_claims) == (1, 1)


def test_the_status_report_counts_a_mismatched_recipient(tmp_path):
    connection, profile_id, _ = frozen_digest(tmp_path, recipient=OTHER_RECIPIENT)

    report = read_delivery_status(
        connection,
        profile_id=profile_id,
        recipient_fingerprint=recipient_fingerprint(RECIPIENT),
    )

    assert (report.recipient_matching, report.recipient_mismatched) == (0, 1)
    assert report.due_recipient_mismatched == 1


# --------------------------------------------------------------------------
# The one-shot operator command
# --------------------------------------------------------------------------


def cli_args(connection, profile_id, *extra, recipient=RECIPIENT):
    return [
        "--database",
        database_path(connection),
        "--profile-id",
        str(profile_id),
        "--timezone",
        TIMEZONE,
        "--recipient",
        recipient,
        *extra,
    ]


def run_cli(connection, profile_id, *extra, recipient=RECIPIENT):
    connection.commit()
    return delivery_cli.main(
        cli_args(connection, profile_id, *extra, recipient=recipient)
    )


def install_sender(monkeypatch, transport, *, calls=None):
    """Replace credential loading with a fake, and record when it happened."""

    def build(recipient, *, configuration=None, dependencies=None):
        if calls is not None:
            calls.append("sender-built")
        return GmailDigestSender(transport, recipient=recipient)

    monkeypatch.setattr(delivery_cli, "build_gmail_digest_sender", build)


def ready_database(tmp_path):
    """A migrated database with a real Portfolio and no digest yet."""
    connection, identity, ids = digest_fixture(tmp_path)
    store_run(
        connection,
        identity.profile_id,
        {ids[0]: included(), ids[1]: included(), ids[2]: included()},
    )
    connection.commit()
    return connection, identity.profile_id


def test_the_command_materializes_then_drains_in_one_pass(
    tmp_path, monkeypatch, capsys
):
    connection, profile_id = ready_database(tmp_path)
    transport = RecordingTransport()
    install_sender(monkeypatch, transport)

    code = run_cli(connection, profile_id)

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["mode"] == "deliver"
    assert payload["materialization_status"] == "CREATED"
    assert payload["delivery"]["sent"] == 1
    assert len(transport.raw_messages) == 1


def test_the_sender_is_validated_before_any_digest_is_claimed(
    tmp_path, monkeypatch, capsys
):
    connection, profile_id = ready_database(tmp_path)
    before = file_digest(connection)

    def refuse(recipient, *, configuration=None, dependencies=None):
        raise RuntimeError("no Gmail authorization is stored")

    monkeypatch.setattr(delivery_cli, "build_gmail_digest_sender", refuse)

    code = run_cli(connection, profile_id)

    assert code == 1
    assert capsys.readouterr().err.startswith("gmail digest delivery failed:")
    # Nothing was materialized and nothing was claimed: the file is untouched.
    assert file_digest(connection) == before
    assert (
        connection.execute("SELECT COUNT(*) FROM gmail_digest_outbox").fetchone()[0]
        == 0
    )


def test_a_second_run_on_the_same_day_sends_nothing(tmp_path, monkeypatch, capsys):
    connection, profile_id = ready_database(tmp_path)
    transport = RecordingTransport()
    install_sender(monkeypatch, transport)
    run_cli(connection, profile_id)
    capsys.readouterr()

    code = run_cli(connection, profile_id)

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["materialization_status"] == "ALREADY_MATERIALIZED"
    assert payload["delivery"] == payload["delivery"] | {"claimed": 0, "sent": 0}
    assert len(transport.raw_messages) == 1


def test_the_limit_bounds_how_many_digests_one_pass_attempts(
    tmp_path, monkeypatch, capsys
):
    connection, profile_id = ready_database(tmp_path)
    # Two frozen digests, two consecutive local days.
    materialize(connection, profile_id, now=DAY_ONE)
    materialize(connection, profile_id, now=DAY_TWO)
    connection.commit()
    transport = RecordingTransport()
    install_sender(monkeypatch, transport)

    run_cli(connection, profile_id, "--limit", "1")

    payload = json.loads(capsys.readouterr().out)
    assert payload["delivery"]["attempted"] == 1
    assert len(transport.raw_messages) == 1
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM gmail_digest_outbox WHERE status='PENDING'"
        ).fetchone()[0]
        == 1
    )


def test_the_command_never_prints_a_recipient_a_subject_or_a_body(
    tmp_path, monkeypatch, capsys
):
    connection, profile_id = ready_database(tmp_path)
    install_sender(monkeypatch, RecordingTransport())

    run_cli(connection, profile_id)

    printed = capsys.readouterr()
    subject, body_text, body_html = connection.execute(
        "SELECT subject,body_text,body_html FROM gmail_digest_outbox LIMIT 1"
    ).fetchone()
    output = printed.out + printed.err
    assert RECIPIENT not in output
    assert subject not in output
    assert body_text.strip().splitlines()[0] not in output
    assert "<html" not in output and body_html[:40] not in output


def test_the_command_output_is_machine_readable_json(tmp_path, monkeypatch, capsys):
    connection, profile_id = ready_database(tmp_path)
    install_sender(monkeypatch, RecordingTransport())

    run_cli(connection, profile_id)

    payload = json.loads(capsys.readouterr().out)
    assert payload["delivery"]["recipient_fingerprint"] == recipient_fingerprint(
        RECIPIENT
    )
    assert set(payload["delivery"]) >= {
        "claimed",
        "attempted",
        "sent",
        "retried",
        "permanent_failures",
        "recovered_claims",
        "recipient_mismatched",
        "skipped",
    }


# --------------------------------------------------------------------------
# --dry-run: no OAuth, no socket, no byte
# --------------------------------------------------------------------------


def test_a_dry_run_leaves_the_database_byte_identical(tmp_path, capsys):
    connection, profile_id = ready_database(tmp_path)
    materialize(connection, profile_id, now=DAY_ONE)
    before = file_digest(connection)

    code = run_cli(connection, profile_id, "--dry-run")

    assert code == 0
    assert file_digest(connection) == before
    assert json.loads(capsys.readouterr().out)["mode"] == "dry-run"


def test_a_dry_run_needs_no_oauth_file_at_all(tmp_path, monkeypatch, capsys):
    connection, profile_id = ready_database(tmp_path)

    def refuse(*args, **kwargs):
        raise AssertionError("a dry run must never load a credential")

    monkeypatch.setattr(delivery_cli, "build_gmail_digest_sender", refuse)

    assert run_cli(connection, profile_id, "--dry-run") == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "dry-run"


def test_a_dry_run_opens_no_socket(tmp_path, monkeypatch, capsys):
    connection, profile_id = ready_database(tmp_path)
    materialize(connection, profile_id, now=DAY_ONE)
    connection.commit()

    def forbidden(*args, **kwargs):
        raise AssertionError("a dry run must never open a socket")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    assert run_cli(connection, profile_id, "--dry-run") == 0
    capsys.readouterr()


def test_a_dry_run_claims_nothing_and_recovers_nothing(tmp_path, capsys):
    connection, profile_id = ready_database(tmp_path)
    outbox_id = materialize(connection, profile_id, now=DAY_ONE).outbox_id
    claim_due_digest(
        connection,
        claim_token=new_claim_token(),
        profile_id=profile_id,
        recipient_fingerprint=recipient_fingerprint(RECIPIENT),
        now=DAY_ONE,
    )
    before = row(connection, outbox_id)
    digest_before = file_digest(connection)

    run_cli(connection, profile_id, "--dry-run")

    assert row(connection, outbox_id) == before
    assert file_digest(connection) == digest_before
    assert json.loads(capsys.readouterr().out)["delivery"]["in_flight"] == 1


def test_a_dry_run_reports_what_materialization_would_decide(tmp_path, capsys):
    connection, profile_id = ready_database(tmp_path)

    run_cli(connection, profile_id, "--dry-run")
    first = json.loads(capsys.readouterr().out)
    # The dry run asks about *today*, so today is the day this one is frozen for.
    materialize(connection, profile_id, now=datetime.now(timezone.utc))
    connection.commit()
    before = file_digest(connection)
    run_cli(connection, profile_id, "--dry-run")
    second = json.loads(capsys.readouterr().out)

    assert first["would_materialize"] == "CREATED"
    assert first["would_materialize_item_count"] == 3
    assert second["would_materialize"] == "ALREADY_MATERIALIZED"
    assert file_digest(connection) == before


def test_a_dry_run_reports_a_recipient_mismatch_safely(tmp_path, capsys):
    connection, profile_id = ready_database(tmp_path)
    materialize(connection, profile_id, now=DAY_ONE, recipient=OTHER_RECIPIENT)

    run_cli(connection, profile_id, "--dry-run")

    payload = json.loads(capsys.readouterr().out)
    assert payload["delivery"]["recipient_mismatched"] == 1
    assert payload["delivery"]["due_recipient_mismatched"] == 1
    assert payload["delivery"]["recipient_matching"] == 0
    assert OTHER_RECIPIENT not in json.dumps(payload)


def test_a_dry_run_prints_no_subject_body_or_address(tmp_path, capsys):
    connection, profile_id = ready_database(tmp_path)
    materialize(connection, profile_id, now=DAY_ONE)
    connection.commit()

    run_cli(connection, profile_id, "--dry-run")

    printed = capsys.readouterr()
    subject, body_text = connection.execute(
        "SELECT subject,body_text FROM gmail_digest_outbox LIMIT 1"
    ).fetchone()
    output = printed.out + printed.err
    assert RECIPIENT not in output and subject not in output
    assert body_text.strip().splitlines()[0] not in output


def test_a_dry_run_cannot_write_even_if_it_tried(tmp_path):
    connection, profile_id = ready_database(tmp_path)
    connection.commit()
    from services.collector.database.connection import connect_readonly_database

    with connect_readonly_database(database_path(connection)) as readonly:
        readonly.execute("PRAGMA query_only = ON")
        assert readonly.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            readonly.execute("UPDATE gmail_digest_outbox SET status='SENT'")
