"""Integration coverage for Phase 5.3C1 Web Push delivery over real SQLite.

Every test here mocks the transport. Nothing in this module reaches a push
service, and two tests prove it: one forbids sockets outright, and one asserts
that a half-configured VAPID identity stops the run before any request object
is even built.
"""

import base64
import io
import json
import logging
import socket
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from services.collector import logging_config
from services.notifications import (
    CLAIM_LEASE_SECONDS,
    MAX_DELIVERY_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
    DeliveryBatchStatus,
    DeliveryOutcome,
    DeliveryTargetStatus,
    NotificationDeliveryError,
    NotificationPayloadError,
    NotificationSyncStatus,
    WebPushDeliverySender,
    WebPushResponse,
    WebPushTransportError,
    build_vapid_configuration,
    claim_due_targets,
    classify_status_code,
    drain_notification_deliveries,
    encode_push_payload,
    load_vapid_configuration,
    materialize_delivery_batches,
    new_claim_token,
    read_delivery_status,
    record_delivery_attempt,
    recover_stale_claims,
    release_delivery_claim,
    subscribe_push_subscription,
    sync_notification_policy,
    transport_failure,
    unsubscribe_push_subscription,
)
from services.notifications import delivery_cli, delivery_persistence
from tests.integration.test_notification_policy_sync_sqlite import (
    URGENT,
    baseline,
    excluded,
    fixture,
    included,
    store_run,
)
from tests.integration.test_push_subscriptions_sqlite import P256DH

AUTH = "c" * 22
SCALAR = bytes(range(1, 33))


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


PRIVATE_KEY = encode(SCALAR)
PUBLIC_KEY = encode(
    ec.derive_private_key(int.from_bytes(SCALAR, "big"), ec.SECP256R1())
    .public_key()
    .public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
)
SUBJECT = "mailto:ops@example.invalid"
VAPID_ENVIRONMENT = {
    "WEB_PUSH_VAPID_PUBLIC_KEY": PUBLIC_KEY,
    "WEB_PUSH_VAPID_PRIVATE_KEY": PRIVATE_KEY,
    "WEB_PUSH_VAPID_SUBJECT": SUBJECT,
}

UPSTREAM_TABLES = (
    "portfolio_runs",
    "portfolio_assessments",
    "priority_runs",
    "priority_assessments",
    "matching_runs",
    "matching_assessments",
    "opportunities",
    "opportunity_eligibilities",
)


def endpoint(number: int) -> str:
    return f"https://push.example.invalid/subscription/{number}"


def subscribe(connection, profile_id, number, *, auth=AUTH):
    return subscribe_push_subscription(
        connection,
        profile_id=profile_id,
        endpoint=endpoint(number),
        p256dh=P256DH,
        auth=auth,
    ).subscription_id


def one_event(tmp_path):
    """A migrated database whose outbox holds exactly one real 5.3B event."""
    connection, identity, ids = fixture(tmp_path)
    baseline(connection, identity, {ids[0]: excluded()})
    store_run(connection, identity.profile_id, {ids[0]: included(priority=URGENT)})
    result = sync_notification_policy(connection, identity.profile_id)
    assert result.status is NotificationSyncStatus.PROCESSED and result.event_count == 1
    assert outbox_ids(connection)
    return connection, identity, ids


def outbox_ids(connection):
    return [
        int(row[0])
        for row in connection.execute("SELECT id FROM notification_outbox ORDER BY id")
    ]


def batches(connection):
    return connection.execute(
        "SELECT id,outbox_id,status,target_count,completed_at IS NOT NULL"
        " FROM notification_delivery_batches ORDER BY id"
    ).fetchall()


def targets(connection):
    return connection.execute(
        """SELECT id,batch_id,subscription_id,status,attempt_count,next_attempt_at,
        last_attempt_at,delivered_at,last_error_code,last_error_category,
        claim_token,claimed_at
        FROM notification_delivery_targets ORDER BY id"""
    ).fetchall()


def claim(connection, profile_id, *, token=None, now=None, limit=None):
    """Claim the due targets the way a drain does, and hand back the token."""
    token = new_claim_token() if token is None else token
    return token, claim_due_targets(
        connection, claim_token=token, profile_id=profile_id, now=now, limit=limit
    )


def subscriptions(connection):
    return connection.execute(
        "SELECT id,status,revoked_at IS NOT NULL FROM push_subscriptions ORDER BY id"
    ).fetchall()


class Transport:
    """A Web Push transport that answers from a script and records the calls."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        answer = self.answers.pop(0) if self.answers else 201
        if isinstance(answer, Exception):
            raise answer
        return WebPushResponse(answer)


def sender(*answers):
    transport = Transport(*answers)
    built = WebPushDeliverySender(
        build_vapid_configuration(
            public_key=PUBLIC_KEY, private_key=PRIVATE_KEY, subject=SUBJECT
        ),
        transport,
    )
    return built, transport


def outcomes(*answers):
    """A sender that skips encryption entirely and just returns outcomes."""
    scripted = list(answers)
    calls = []

    def send(target, payload):
        calls.append((target.target_id, payload))
        answer = scripted.pop(0) if scripted else 201
        return (
            transport_failure()
            if answer is None
            else classify_status_code(answer)
            if isinstance(answer, int)
            else answer
        )

    return send, calls


def test_materialization_with_no_active_subscription_is_terminal_immediately(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        result = materialize_delivery_batches(
            connection, profile_id=identity.profile_id
        )

        assert (
            result.created_batches,
            result.created_targets,
            result.empty_batches,
        ) == (
            1,
            0,
            1,
        )
        assert batches(connection) == [
            (1, outbox_ids(connection)[0], "NO_ACTIVE_SUBSCRIPTIONS", 0, 1)
        ]
        assert targets(connection) == []
        report = read_delivery_status(connection, profile_id=identity.profile_id)
        assert report.batches_without_subscriptions == 1
        assert (report.unmaterialized_outbox, report.targets_due) == (0, 0)
    finally:
        connection.close()


def test_materialization_fans_out_to_every_active_subscription_and_no_other(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        first = subscribe(connection, identity.profile_id, 1)
        second = subscribe(connection, identity.profile_id, 2)
        third = subscribe(connection, identity.profile_id, 3)
        unsubscribe_push_subscription(
            connection, profile_id=identity.profile_id, endpoint=endpoint(3)
        )

        result = materialize_delivery_batches(
            connection, profile_id=identity.profile_id
        )

        assert (
            result.created_batches,
            result.created_targets,
            result.empty_batches,
        ) == (
            1,
            2,
            0,
        )
        assert [(row[2], row[3]) for row in targets(connection)] == [
            (first, "PENDING"),
            (second, "PENDING"),
        ]
        assert third not in {row[2] for row in targets(connection)}
        assert batches(connection) == [(1, outbox_ids(connection)[0], "PENDING", 2, 0)]
    finally:
        connection.close()


def test_a_single_active_subscription_produces_exactly_one_target(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscription_id = subscribe(connection, identity.profile_id, 1)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)

        rows = targets(connection)
        assert len(rows) == 1
        assert rows[0][2] == subscription_id and rows[0][3] == "PENDING"
        assert rows[0][4] == 0 and rows[0][5] is not None
        assert rows[0][6] is None and rows[0][7] is None
    finally:
        connection.close()


def test_a_subscription_created_after_materialization_never_joins_that_batch(tmp_path):
    """The recipient snapshot is frozen: a new device is not told old news."""
    connection, identity, _ = one_event(tmp_path)
    try:
        first = subscribe(connection, identity.profile_id, 1)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)
        before = targets(connection)

        latecomer = subscribe(connection, identity.profile_id, 2)
        again = materialize_delivery_batches(connection, profile_id=identity.profile_id)

        assert again.created_batches == 0 and again.created_targets == 0
        assert targets(connection) == before
        assert {row[2] for row in targets(connection)} == {first}
        assert latecomer not in {row[2] for row in targets(connection)}

        # It does receive the *next* event, which is materialized after it.
        store_run(connection, identity.profile_id, {})
        second_event = one_more_event(connection, identity)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)
        assert second_event
        assert {
            row[2] for row in targets(connection) if row[1] == len(batches(connection))
        } == {first, latecomer}
    finally:
        connection.close()


def one_more_event(connection, identity):
    """Produce one further real event by moving a second opportunity in."""
    ids = [
        int(row[0])
        for row in connection.execute("SELECT id FROM opportunities ORDER BY id")
    ]
    before = len(outbox_ids(connection))
    store_run(connection, identity.profile_id, {ids[1]: included(priority=URGENT)})
    sync_notification_policy(connection, identity.profile_id)
    return len(outbox_ids(connection)) > before


def test_repeated_materialization_is_a_no_op_and_never_replaces_a_target(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        first = materialize_delivery_batches(connection, profile_id=identity.profile_id)
        snapshot = (batches(connection), targets(connection))

        for _ in range(3):
            again = materialize_delivery_batches(
                connection, profile_id=identity.profile_id
            )
            assert (again.created_batches, again.created_targets) == (0, 0)
        assert (batches(connection), targets(connection)) == snapshot
        assert first.created_batches == 1

        # The same outbox row cannot acquire a second batch, ever.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO notification_delivery_batches"
                " (outbox_id,status,target_count) VALUES (?,'PENDING',1)",
                (outbox_ids(connection)[0],),
            )
        connection.rollback()
    finally:
        connection.close()


def test_a_success_marks_the_target_sent_and_completes_its_batch(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        send, transport = sender(201)

        result = drain_notification_deliveries(
            connection, send, profile_id=identity.profile_id
        )

        assert (result.attempted, result.sent, result.retried) == (1, 1, 0)
        row = targets(connection)[0]
        assert row[3] == "SENT" and row[4] == 1
        assert row[5] is None and row[6] is not None and row[7] is not None
        assert row[8] is None and row[9] is None
        assert batches(connection)[0][2] == DeliveryBatchStatus.COMPLETED.value
        assert batches(connection)[0][4] == 1
        assert len(transport.requests) == 1
        assert transport.requests[0].endpoint == endpoint(1)
        assert subscriptions(connection) == [(1, "ACTIVE", 0)]
    finally:
        connection.close()


@pytest.mark.parametrize("status_code", (404, 410))
def test_a_gone_endpoint_ends_the_target_and_revokes_the_subscription(
    tmp_path, status_code
):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscription_id = subscribe(connection, identity.profile_id, 1)
        send, _ = outcomes(status_code)

        result = drain_notification_deliveries(
            connection, send, profile_id=identity.profile_id
        )

        assert (result.expired, result.revoked_subscriptions) == (1, 1)
        row = targets(connection)[0]
        assert row[3] == "EXPIRED" and row[4] == 1 and row[5] is None
        assert row[8] == f"HTTP_{status_code}" and row[9] == "EXPIRED"
        # Revocation follows the 5.3A invariants exactly.
        assert subscriptions(connection) == [(subscription_id, "REVOKED", 1)]
        assert batches(connection)[0][2] == DeliveryBatchStatus.COMPLETED.value
    finally:
        connection.close()


@pytest.mark.parametrize("answer", (429, 500, 503, None))
def test_a_transient_failure_schedules_a_retry_rather_than_ending_the_target(
    tmp_path, answer
):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        send, _ = outcomes(answer)

        result = drain_notification_deliveries(
            connection, send, profile_id=identity.profile_id
        )

        assert (result.retried, result.sent, result.failed) == (1, 0, 0)
        row = targets(connection)[0]
        assert row[3] == "PENDING" and row[4] == 1
        assert row[5] is not None and row[5] > row[6]
        assert row[9] == "RETRYABLE"
        assert row[8] == ("TRANSPORT_ERROR" if answer is None else f"HTTP_{answer}")
        # A retried target is nobody's terminal state, so the batch waits.
        assert batches(connection)[0][2] == DeliveryBatchStatus.PENDING.value
        # And the subscription is untouched: a busy push service is not a
        # reason to lose a user's device.
        assert subscriptions(connection) == [(1, "ACTIVE", 0)]
    finally:
        connection.close()


def test_a_target_that_is_not_due_yet_is_never_attempted(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        send, calls = outcomes(503)
        drain_notification_deliveries(connection, send, profile_id=identity.profile_id)
        scheduled = targets(connection)[0][5]

        again, second_calls = outcomes(201)
        result = drain_notification_deliveries(
            connection, again, profile_id=identity.profile_id
        )

        assert second_calls == [] and result.attempted == 0
        assert result.claimed == 0
        assert claim(connection, identity.profile_id)[1] == ()
        assert targets(connection)[0][3] == "PENDING"
        assert targets(connection)[0][4] == 1
        report = read_delivery_status(connection, profile_id=identity.profile_id)
        assert (report.targets_scheduled, report.targets_due) == (1, 0)
        assert len(calls) == 1 and scheduled is not None
    finally:
        connection.close()


def test_retries_follow_the_bounded_schedule_and_end_in_a_terminal_failure(tmp_path):
    """Five attempts in total, then FAILED — and never a sixth."""
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)
        target_id = targets(connection)[0][0]
        for attempt in range(1, MAX_DELIVERY_ATTEMPTS + 1):
            # Each attempt is a fresh claim, and each retry releases it. The
            # clock is moved forward so the scheduled retry is due again.
            token, held = claim(
                connection,
                identity.profile_id,
                now=datetime.now(timezone.utc) + timedelta(days=attempt),
            )
            assert [target.target_id for target in held] == [target_id]
            result = record_delivery_attempt(
                connection,
                target_id=target_id,
                claim_token=token,
                outcome=classify_status_code(503),
            )
            row = targets(connection)[0]
            assert row[10] is None and row[11] is None
            assert row[4] == attempt
            if attempt < MAX_DELIVERY_ATTEMPTS:
                assert result.status is DeliveryTargetStatus.PENDING
                assert row[3] == "PENDING" and row[5] is not None
            else:
                assert result.status is DeliveryTargetStatus.FAILED
                assert row[3] == "FAILED" and row[5] is None
                assert row[8] == "HTTP_503" and row[9] == "RETRYABLE"

        assert len(RETRY_BACKOFF_SECONDS) == MAX_DELIVERY_ATTEMPTS - 1
        assert batches(connection)[0][2] == DeliveryBatchStatus.COMPLETED.value
        assert subscriptions(connection) == [(1, "ACTIVE", 0)]

        # A spent target is terminal: it cannot even be claimed again.
        assert claim(connection, identity.profile_id)[1] == ()
        again = record_delivery_attempt(
            connection,
            target_id=target_id,
            claim_token=token,
            outcome=classify_status_code(201),
        )
        assert again.applied is False and targets(connection)[0][3] == "FAILED"
        assert read_delivery_status(connection).targets_failed == 1
    finally:
        connection.close()


@pytest.mark.parametrize("status_code", (400, 401, 403, 413))
def test_any_other_4xx_fails_permanently_without_revoking_the_subscription(
    tmp_path, status_code
):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscription_id = subscribe(connection, identity.profile_id, 1)
        send, _ = outcomes(status_code)

        result = drain_notification_deliveries(
            connection, send, profile_id=identity.profile_id
        )

        assert (result.permanent_failures, result.revoked_subscriptions) == (1, 0)
        row = targets(connection)[0]
        assert row[3] == "PERMANENT_FAILURE" and row[5] is None
        assert row[8] == f"HTTP_{status_code}" and row[9] == "PERMANENT"
        assert subscriptions(connection) == [(subscription_id, "ACTIVE", 0)]
    finally:
        connection.close()


def test_a_delivered_target_is_never_sent_again_however_often_the_drain_runs(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        send, calls = outcomes(201)
        drain_notification_deliveries(connection, send, profile_id=identity.profile_id)
        after = (batches(connection), targets(connection), subscriptions(connection))

        for _ in range(3):
            again, more = outcomes(201)
            result = drain_notification_deliveries(
                connection, again, profile_id=identity.profile_id
            )
            assert (result.attempted, result.sent) == (0, 0)
            assert more == []
        assert (
            batches(connection),
            targets(connection),
            subscriptions(connection),
        ) == after
        assert len(calls) == 1

        # Even asked directly, under a fresh claim token, a SENT target
        # refuses a second attempt.
        applied = record_delivery_attempt(
            connection,
            target_id=targets(connection)[0][0],
            claim_token=new_claim_token(),
            outcome=classify_status_code(500),
        )
        assert applied.applied is False
        assert targets(connection)[0][3] == "SENT"
    finally:
        connection.close()


def test_the_orchestrator_is_idempotent_across_repeated_runs(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        subscribe(connection, identity.profile_id, 2)
        send, calls = outcomes(201, 201)
        first = drain_notification_deliveries(
            connection, send, profile_id=identity.profile_id
        )
        settled = (batches(connection), targets(connection), subscriptions(connection))

        for _ in range(3):
            again, more = outcomes()
            repeat = drain_notification_deliveries(
                connection, again, profile_id=identity.profile_id
            )
            assert repeat.attempted == 0 and repeat.materialized_batches == 0
            assert more == []

        assert first.sent == 2 and len(calls) == 2
        assert (
            batches(connection),
            targets(connection),
            subscriptions(connection),
        ) == settled
        assert outbox_ids(connection) == [1]
    finally:
        connection.close()


def test_the_payload_sent_is_the_persisted_event_projected_onto_an_internal_target(
    tmp_path,
):
    connection, identity, ids = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        stored = connection.execute(
            "SELECT payload_json FROM notification_events"
        ).fetchone()[0]
        captured = []

        def send(target, payload):
            captured.append(payload)
            return DeliveryOutcome(True)

        drain_notification_deliveries(connection, send, profile_id=identity.profile_id)

        assert len(captured) == 1
        assert captured[0] == encode_push_payload(stored)
        payload = json.loads(captured[0])
        assert payload["url"] == "/portfolio"
        assert payload["url"].startswith("/") and "://" not in payload["url"]
        assert payload["opportunity_id"] == ids[0]
        assert payload["event_type"] == "NEW_ACTIONABLE_OPPORTUNITY"
        # The title comes from the event, not from anything recomputed here.
        assert payload["title"] == json.loads(stored)["opportunity"]["title"]
        assert (
            json.loads(stored)["opportunity"]["original_url"]
            not in captured[0].decode()
        )
    finally:
        connection.close()


def test_delivery_never_writes_to_portfolio_priority_matching_or_the_outbox(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        upstream = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in UPSTREAM_TABLES
        }
        notifications = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in ("notification_events", "notification_outbox")
        }
        policy = connection.execute(
            "SELECT * FROM notification_policy_state ORDER BY profile_id"
        ).fetchall()
        send, _ = outcomes(201)

        drain_notification_deliveries(connection, send, profile_id=identity.profile_id)

        assert {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in UPSTREAM_TABLES
        } == upstream
        assert {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in notifications
        } == notifications
        assert (
            connection.execute(
                "SELECT * FROM notification_policy_state ORDER BY profile_id"
            ).fetchall()
            == policy
        )
    finally:
        connection.close()


def test_a_subscription_revoked_after_materialization_is_closed_without_a_request(
    tmp_path,
):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        subscribe(connection, identity.profile_id, 2)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)
        unsubscribe_push_subscription(
            connection, profile_id=identity.profile_id, endpoint=endpoint(1)
        )
        send, calls = outcomes(201)

        result = drain_notification_deliveries(
            connection, send, profile_id=identity.profile_id
        )

        assert result.closed_revoked_targets == 1
        assert (result.attempted, result.sent) == (1, 1)
        assert [target_id for target_id, _ in calls] == [targets(connection)[1][0]]
        closed = targets(connection)[0]
        assert closed[3] == "PERMANENT_FAILURE"
        assert closed[8] == "SUBSCRIPTION_REVOKED" and closed[9] == "PERMANENT"
        # The batch can finish even though one recipient walked away.
        assert batches(connection)[0][2] == DeliveryBatchStatus.COMPLETED.value
    finally:
        connection.close()


def test_a_failure_while_recording_rolls_the_whole_attempt_back(tmp_path, monkeypatch):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)
        token, held = claim(connection, identity.profile_id)
        before = (batches(connection), targets(connection), subscriptions(connection))
        target_id = held[0].target_id

        def explode(*arguments, **keywords):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(delivery_persistence, "_complete_batch_if_settled", explode)
        with pytest.raises(sqlite3.OperationalError):
            record_delivery_attempt(
                connection,
                target_id=target_id,
                claim_token=token,
                outcome=classify_status_code(410),
            )

        assert connection.in_transaction is False
        # Neither the attempt nor the revocation it implied survived.
        assert (
            batches(connection),
            targets(connection),
            subscriptions(connection),
        ) == before
    finally:
        connection.close()


def test_a_failure_while_materializing_leaves_no_partial_snapshot(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        subscribe(connection, identity.profile_id, 2)

        class Fragile:
            """A connection whose third write fails, mid-snapshot."""

            def __init__(self, wrapped):
                self.wrapped = wrapped
                self.writes = 0

            def execute(self, statement, parameters=()):
                if statement.lstrip().upper().startswith(("INSERT", "UPDATE")):
                    self.writes += 1
                    if self.writes == 2:
                        raise sqlite3.OperationalError("disk I/O error")
                return self.wrapped.execute(statement, parameters)

            @property
            def in_transaction(self):
                return self.wrapped.in_transaction

        with pytest.raises(sqlite3.OperationalError):
            materialize_delivery_batches(
                Fragile(connection), profile_id=identity.profile_id
            )

        assert connection.in_transaction is False
        assert batches(connection) == [] and targets(connection) == []
        assert outbox_ids(connection) == [1]
    finally:
        connection.close()


def test_no_sql_transaction_is_ever_open_while_a_message_is_being_sent(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        subscribe(connection, identity.profile_id, 2)
        seen = []

        def send(target, payload):
            seen.append(connection.in_transaction)
            return DeliveryOutcome(True)

        drain_notification_deliveries(connection, send, profile_id=identity.profile_id)

        assert seen == [False, False]
        assert [row[3] for row in targets(connection)] == ["SENT", "SENT"]
    finally:
        connection.close()


def test_an_incomplete_or_mismatched_vapid_identity_sends_nothing(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)
        before = targets(connection)

        class Forbidden:
            def __call__(self, request):  # pragma: no cover - must never run
                raise AssertionError("a misconfigured sender must not send anything")

        for environment in (
            {},
            {"WEB_PUSH_VAPID_PUBLIC_KEY": PUBLIC_KEY},
            VAPID_ENVIRONMENT | {"WEB_PUSH_VAPID_PRIVATE_KEY": ""},
            VAPID_ENVIRONMENT | {"WEB_PUSH_VAPID_SUBJECT": "ops@example.invalid"},
            VAPID_ENVIRONMENT
            | {"WEB_PUSH_VAPID_PRIVATE_KEY": encode(bytes(range(2, 34)))},
        ):
            with pytest.raises(Exception):
                WebPushDeliverySender(
                    load_vapid_configuration(environment), Forbidden()
                )

        assert targets(connection) == before
        assert [row[4] for row in targets(connection)] == [0]
    finally:
        connection.close()


def test_a_transport_error_is_a_retry_and_never_escapes_the_drain(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        send, transport = sender(
            WebPushTransportError("push request failed: ReadTimeout")
        )

        result = drain_notification_deliveries(
            connection, send, profile_id=identity.profile_id
        )

        assert result.retried == 1
        assert targets(connection)[0][8] == "TRANSPORT_ERROR"
        assert len(transport.requests) == 1
    finally:
        connection.close()


def test_the_drain_never_opens_a_socket_and_never_logs_a_credential(
    tmp_path, monkeypatch
):
    connection, identity, _ = one_event(tmp_path)
    stream = io.StringIO()
    logging.getLogger(logging_config.LOGGER_NAME).handlers.clear()
    logging_config.configure_logging(stream)
    try:
        secret_auth = "d" * 22
        subscribe(connection, identity.profile_id, 1, auth=secret_auth)

        def refuse(*arguments, **keywords):
            raise AssertionError("delivery must not reach the network in tests")

        monkeypatch.setattr(socket, "socket", refuse)
        monkeypatch.setattr(socket, "create_connection", refuse)
        send, _ = sender(410)
        result = drain_notification_deliveries(
            connection, send, profile_id=identity.profile_id
        )

        assert result.expired == 1 and result.revoked_subscriptions == 1
        logged = stream.getvalue()
        assert logged.strip()
        for credential in (endpoint(1), P256DH, secret_auth, PRIVATE_KEY):
            assert credential not in logged
        for record in (json.loads(line) for line in logged.splitlines()):
            assert "push.example.invalid" not in json.dumps(record)
    finally:
        logging.getLogger(logging_config.LOGGER_NAME).handlers.clear()
        connection.close()


def test_a_claimed_target_never_renders_the_credential_it_carries(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)

        _, held = claim(connection, identity.profile_id)

        assert len(held) == 1 and held[0].endpoint == endpoint(1)
        for rendering in (repr(held[0]), str(held[0]), f"{held[0]}"):
            assert endpoint(1) not in rendering
            assert P256DH not in rendering and AUTH not in rendering
            assert "redacted" in rendering
    finally:
        connection.close()


def test_delivery_refuses_an_unmigrated_database_and_a_borrowed_transaction(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        connection.execute("DELETE FROM schema_migrations WHERE version='0020'")
        connection.commit()
        with pytest.raises(NotificationDeliveryError, match="schema is not ready"):
            materialize_delivery_batches(connection, profile_id=identity.profile_id)
        assert connection.in_transaction is False
        connection.execute("INSERT INTO schema_migrations (version) VALUES ('0020')")
        connection.commit()

        send, _ = outcomes(201)
        connection.execute("BEGIN IMMEDIATE")
        for call in (
            lambda: materialize_delivery_batches(connection),
            lambda: recover_stale_claims(connection),
            lambda: claim_due_targets(connection, claim_token=new_claim_token()),
            lambda: record_delivery_attempt(
                connection,
                target_id=1,
                claim_token=new_claim_token(),
                outcome=DeliveryOutcome(True),
            ),
            lambda: drain_notification_deliveries(connection, send),
        ):
            with pytest.raises(NotificationDeliveryError, match="active transaction"):
                call()
        connection.execute("ROLLBACK")

        for profile_id in (0, -1, True, "1"):
            with pytest.raises(NotificationDeliveryError, match="positive integer"):
                materialize_delivery_batches(connection, profile_id=profile_id)
        with pytest.raises(NotificationDeliveryError, match="callable"):
            drain_notification_deliveries(connection, None)
    finally:
        connection.close()


def test_the_status_report_distinguishes_every_stage_of_the_backlog(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        report = read_delivery_status(connection, profile_id=identity.profile_id)
        assert report.unmaterialized_outbox == 1
        assert (report.batches_pending, report.targets_due) == (0, 0)

        subscribe(connection, identity.profile_id, 1)
        subscribe(connection, identity.profile_id, 2)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)
        report = read_delivery_status(connection, profile_id=identity.profile_id)
        assert (report.unmaterialized_outbox, report.batches_pending) == (0, 1)
        assert (report.targets_due, report.targets_scheduled) == (2, 0)

        send, _ = outcomes(201, 503)
        drain_notification_deliveries(connection, send, profile_id=identity.profile_id)
        report = read_delivery_status(connection, profile_id=identity.profile_id)
        assert (report.targets_sent, report.targets_scheduled) == (1, 1)
        assert (report.targets_due, report.batches_pending) == (0, 1)
        assert report.batches_completed == 0

        # Another profile's backlog is never counted in this one's.
        assert read_delivery_status(
            connection, profile_id=identity.profile_id + 99
        ) == (type(report)(identity.profile_id + 99, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
    finally:
        connection.close()


def database_path(connection):
    return connection.execute("PRAGMA database_list").fetchall()[0][2]


def test_the_local_entry_point_delivers_once_and_reports_what_it_did(
    tmp_path, monkeypatch, capsys
):
    connection, identity, _ = one_event(tmp_path)
    path = database_path(connection)
    try:
        subscribe(connection, identity.profile_id, 1)
        connection.commit()
        connection.close()
        transport = Transport(201)
        monkeypatch.setattr(
            delivery_cli, "HttpxWebPushTransport", lambda *a, **k: transport
        )
        for variable, value in VAPID_ENVIRONMENT.items():
            monkeypatch.setenv(variable, value)

        assert (
            delivery_cli.main(
                ["--database", str(path), "--profile-id", str(identity.profile_id)]
            )
            == 0
        )

        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "deliver" and payload["sync_status"] is None
        assert (payload["attempted"], payload["sent"]) == (1, 1)
        assert payload["materialized_batches"] == 1
        assert len(transport.requests) == 1
        connection = sqlite3.connect(path)
        assert [row[3] for row in targets(connection)] == ["SENT"]
    finally:
        connection.close()


def test_the_local_entry_point_fails_closed_without_a_vapid_identity(
    tmp_path, monkeypatch, capsys
):
    connection, identity, _ = one_event(tmp_path)
    path = database_path(connection)
    try:
        subscribe(connection, identity.profile_id, 1)
        connection.commit()

        def forbidden(*arguments, **keywords):  # pragma: no cover - must not run
            raise AssertionError("a misconfigured entry point must not send anything")

        monkeypatch.setattr(delivery_cli, "HttpxWebPushTransport", forbidden)
        monkeypatch.setattr(socket, "socket", forbidden)
        for variable in VAPID_ENVIRONMENT:
            monkeypatch.delenv(variable, raising=False)

        assert (
            delivery_cli.main(
                ["--database", str(path), "--profile-id", str(identity.profile_id)]
            )
            == 1
        )

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.strip() == (
            "notification delivery failed: WebPushConfigurationError"
        )
        # Nothing was materialized, so nothing is waiting to be sent later.
        assert batches(connection) == [] and targets(connection) == []
    finally:
        connection.close()


def test_the_local_entry_point_dry_run_writes_nothing_and_needs_no_vapid(
    tmp_path, monkeypatch, capsys
):
    connection, identity, _ = one_event(tmp_path)
    path = database_path(connection)
    try:
        subscribe(connection, identity.profile_id, 1)
        connection.commit()
        before = (batches(connection), targets(connection), subscriptions(connection))
        connection.close()

        def forbidden(*arguments, **keywords):  # pragma: no cover - must not run
            raise AssertionError("a dry run must not send anything")

        monkeypatch.setattr(delivery_cli, "HttpxWebPushTransport", forbidden)
        monkeypatch.setattr(socket, "socket", forbidden)
        for variable in VAPID_ENVIRONMENT:
            monkeypatch.delenv(variable, raising=False)

        assert (
            delivery_cli.main(
                [
                    "--database",
                    str(path),
                    "--profile-id",
                    str(identity.profile_id),
                    "--dry-run",
                ]
            )
            == 0
        )

        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "dry-run"
        assert payload["unmaterialized_outbox"] == 1
        assert payload["targets_due"] == 0
        connection = sqlite3.connect(path)
        assert (
            batches(connection),
            targets(connection),
            subscriptions(connection),
        ) == before
    finally:
        connection.close()


def test_the_local_entry_point_can_advance_the_policy_before_delivering(
    tmp_path, monkeypatch, capsys
):
    connection, identity, ids = one_event(tmp_path)
    path = database_path(connection)
    try:
        subscribe(connection, identity.profile_id, 1)
        # A movement the policy has not turned into an event yet.
        store_run(connection, identity.profile_id, {ids[1]: included(priority=URGENT)})
        connection.commit()
        connection.close()
        transport = Transport(201, 201)
        monkeypatch.setattr(
            delivery_cli, "HttpxWebPushTransport", lambda *a, **k: transport
        )
        for variable, value in VAPID_ENVIRONMENT.items():
            monkeypatch.setenv(variable, value)

        assert (
            delivery_cli.main(
                [
                    "--database",
                    str(path),
                    "--profile-id",
                    str(identity.profile_id),
                    "--sync",
                ]
            )
            == 0
        )

        payload = json.loads(capsys.readouterr().out)
        assert payload["sync_status"] == NotificationSyncStatus.PROCESSED.value
        assert payload["materialized_batches"] == 2 and payload["sent"] == 2
        connection = sqlite3.connect(path)
        assert len(outbox_ids(connection)) == 2
    finally:
        connection.close()


def test_two_connections_claiming_the_same_backlog_get_disjoint_sets(tmp_path):
    """The whole point of the claim: two drains partition the work."""
    connection, identity, _ = one_event(tmp_path)
    path = database_path(connection)
    try:
        for number in (1, 2, 3):
            subscribe(connection, identity.profile_id, number)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)
        connection.commit()
        everything = {row[0] for row in targets(connection)}
        assert len(everything) == 3

        other = sqlite3.connect(path)
        other.execute("PRAGMA foreign_keys = ON")
        try:
            first_token, first = claim(connection, identity.profile_id, limit=2)
            second_token, second = claim(other, identity.profile_id)

            mine = {target.target_id for target in first}
            theirs = {target.target_id for target in second}
            assert len(mine) == 2 and len(theirs) == 1
            assert mine.isdisjoint(theirs)
            assert mine | theirs == everything
            assert first_token != second_token
            # Every row is claimed exactly once, by exactly one token.
            held = {row[0]: (row[3], row[10]) for row in targets(connection)}
            assert all(status == "IN_FLIGHT" for status, _ in held.values())
            assert {token for _, token in held.values()} == {first_token, second_token}
            for target_id in mine:
                assert held[target_id][1] == first_token
        finally:
            other.close()
    finally:
        connection.close()


def test_a_second_drain_sends_nothing_while_the_targets_are_in_flight(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)
        token, held = claim(connection, identity.profile_id)
        assert len(held) == 1

        send, calls = outcomes(201)
        result = drain_notification_deliveries(
            connection, send, profile_id=identity.profile_id
        )

        assert calls == []
        assert (result.claimed, result.attempted, result.sent) == (0, 0, 0)
        row = targets(connection)[0]
        assert row[3] == "IN_FLIGHT" and row[10] == token
        # And the batch stays open while a claim is outstanding.
        assert batches(connection)[0][2] == DeliveryBatchStatus.PENDING.value
        report = read_delivery_status(connection, profile_id=identity.profile_id)
        assert (report.targets_in_flight, report.targets_due) == (1, 0)
    finally:
        connection.close()


def test_a_result_recorded_under_the_wrong_claim_is_refused_and_changes_nothing(
    tmp_path,
):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)
        token, held = claim(connection, identity.profile_id)
        target_id = held[0].target_id
        before = targets(connection)

        for wrong in (new_claim_token(), "0" * 32):
            outcome = record_delivery_attempt(
                connection,
                target_id=target_id,
                claim_token=wrong,
                outcome=classify_status_code(201),
            )
            assert outcome.applied is False
            assert outcome.status is DeliveryTargetStatus.IN_FLIGHT
        assert targets(connection) == before

        # A token that is not a token at all is refused before any write.
        for malformed in ("", "not-hex", token.upper(), token + "0", None, 7):
            with pytest.raises(NotificationDeliveryError, match="claim_token"):
                record_delivery_attempt(
                    connection,
                    target_id=target_id,
                    claim_token=malformed,
                    outcome=classify_status_code(201),
                )
        assert targets(connection) == before
        assert connection.in_transaction is False

        # The real holder still settles it.
        applied = record_delivery_attempt(
            connection,
            target_id=target_id,
            claim_token=token,
            outcome=classify_status_code(201),
        )
        assert applied.applied is True and targets(connection)[0][3] == "SENT"
    finally:
        connection.close()


def test_a_retry_releases_the_claim_and_a_success_ends_it(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)
        token, held = claim(connection, identity.profile_id)
        target_id = held[0].target_id

        record_delivery_attempt(
            connection,
            target_id=target_id,
            claim_token=token,
            outcome=classify_status_code(503),
        )

        row = targets(connection)[0]
        assert row[3] == "PENDING" and row[4] == 1
        assert row[5] is not None and row[10] is None and row[11] is None
        assert batches(connection)[0][2] == DeliveryBatchStatus.PENDING.value

        later = datetime.now(timezone.utc) + timedelta(hours=2)
        second_token, again = claim(connection, identity.profile_id, now=later)
        assert [target.target_id for target in again] == [target_id]
        assert second_token != token
        assert targets(connection)[0][3] == "IN_FLIGHT"

        record_delivery_attempt(
            connection,
            target_id=target_id,
            claim_token=second_token,
            outcome=DeliveryOutcome(True),
        )

        row = targets(connection)[0]
        assert row[3] == "SENT" and row[4] == 2 and row[7] is not None
        assert row[5] is None and row[10] is None and row[11] is None
        assert batches(connection)[0][2] == DeliveryBatchStatus.COMPLETED.value
    finally:
        connection.close()


def test_an_abandoned_claim_is_recovered_only_once_its_lease_has_expired(tmp_path):
    """The one window where this delivery is at-least-once, made explicit."""
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)
        claimed_at = datetime.now(timezone.utc)
        token, held = claim(connection, identity.profile_id, now=claimed_at)
        target_id = held[0].target_id

        # A live claim is nobody else's to take, however often it is checked.
        for offset in (0, 1, CLAIM_LEASE_SECONDS - 1):
            moment = claimed_at + timedelta(seconds=offset)
            assert (
                recover_stale_claims(
                    connection, profile_id=identity.profile_id, now=moment
                )
                == ()
            )
            assert claim(connection, identity.profile_id, now=moment)[1] == ()
        row = targets(connection)[0]
        assert row[3] == "IN_FLIGHT" and row[10] == token

        expired = claimed_at + timedelta(seconds=CLAIM_LEASE_SECONDS)
        recovered = recover_stale_claims(
            connection, profile_id=identity.profile_id, now=expired
        )

        assert recovered == (target_id,)
        row = targets(connection)[0]
        assert row[3] == "PENDING" and row[5] is not None
        assert row[10] is None and row[11] is None
        # A claim nobody settled is not evidence of an attempt.
        assert row[4] == 0 and row[6] is None
        # And the abandoned holder can no longer settle it.
        assert (
            record_delivery_attempt(
                connection,
                target_id=target_id,
                claim_token=token,
                outcome=DeliveryOutcome(True),
            ).applied
            is False
        )
        assert targets(connection)[0][3] == "PENDING"

        # It is claimable again, which is exactly the redelivery risk.
        third, again = claim(connection, identity.profile_id, now=expired)
        assert [target.target_id for target in again] == [target_id]
        assert third != token
    finally:
        connection.close()


def test_a_drain_recovers_a_stale_claim_and_delivers_it(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        long_ago = datetime.now(timezone.utc) - timedelta(
            seconds=CLAIM_LEASE_SECONDS + 60
        )
        # A drain that claimed this target well over a lease ago, and died.
        materialize_delivery_batches(
            connection, profile_id=identity.profile_id, now=long_ago
        )
        claim(connection, identity.profile_id, now=long_ago)
        assert targets(connection)[0][3] == "IN_FLIGHT"

        send, calls = outcomes(201)
        result = drain_notification_deliveries(
            connection, send, profile_id=identity.profile_id
        )

        assert result.recovered_claims == 1
        assert (result.claimed, result.sent) == (1, 1)
        assert len(calls) == 1
        assert targets(connection)[0][3] == "SENT"
        assert batches(connection)[0][2] == DeliveryBatchStatus.COMPLETED.value
    finally:
        connection.close()


def test_a_batch_completes_only_once_every_target_is_terminal(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        for number in (1, 2, 3):
            subscribe(connection, identity.profile_id, number)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)
        token, held = claim(connection, identity.profile_id)
        assert len(held) == 3

        # One delivered, one going back for a retry, one still in flight.
        record_delivery_attempt(
            connection,
            target_id=held[0].target_id,
            claim_token=token,
            outcome=DeliveryOutcome(True),
        )
        assert batches(connection)[0][2] == DeliveryBatchStatus.PENDING.value
        record_delivery_attempt(
            connection,
            target_id=held[1].target_id,
            claim_token=token,
            outcome=classify_status_code(503),
        )
        assert batches(connection)[0][2] == DeliveryBatchStatus.PENDING.value

        settled = record_delivery_attempt(
            connection,
            target_id=held[2].target_id,
            claim_token=token,
            outcome=classify_status_code(410),
        )
        # The retrying target still holds the batch open.
        assert settled.batch_completed is False
        assert batches(connection)[0][2] == DeliveryBatchStatus.PENDING.value

        later = datetime.now(timezone.utc) + timedelta(hours=2)
        last_token, last = claim(connection, identity.profile_id, now=later)
        assert [target.target_id for target in last] == [held[1].target_id]
        assert batches(connection)[0][2] == DeliveryBatchStatus.PENDING.value
        final = record_delivery_attempt(
            connection,
            target_id=held[1].target_id,
            claim_token=last_token,
            outcome=DeliveryOutcome(True),
            now=later,
        )

        assert final.batch_completed is True
        assert batches(connection)[0][2] == DeliveryBatchStatus.COMPLETED.value
        assert batches(connection)[0][4] == 1
        assert sorted(row[3] for row in targets(connection)) == [
            "EXPIRED",
            "SENT",
            "SENT",
        ]
    finally:
        connection.close()


def test_no_sql_transaction_is_open_during_the_claim_or_the_send(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        subscribe(connection, identity.profile_id, 2)
        seen = []

        def send(target, payload):
            # The claim is already committed and released by the time the
            # first byte would go on the wire.
            seen.append((connection.in_transaction, targets(connection)[0][3]))
            return DeliveryOutcome(True)

        drain_notification_deliveries(connection, send, profile_id=identity.profile_id)

        assert seen == [(False, "IN_FLIGHT"), (False, "SENT")]
        assert [row[3] for row in targets(connection)] == ["SENT", "SENT"]
        assert all(row[10] is None and row[11] is None for row in targets(connection))
    finally:
        connection.close()


def test_a_long_backlog_is_never_claimed_ahead_of_being_sent(tmp_path):
    """One claim per message, taken when that message's turn actually comes.

    Claiming the whole backlog up front would let the last targets age past
    their lease while the drain is still on the first, so another drain could
    legitimately recover a row this one was about to send.
    """
    connection, identity, _ = one_event(tmp_path)
    try:
        for number in (1, 2, 3, 4):
            subscribe(connection, identity.profile_id, number)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)
        connection.commit()
        seen = []

        def send(target, payload):
            # Only this target is claimed; every other one is still waiting.
            seen.append([(row[0], row[3]) for row in targets(connection)])
            return DeliveryOutcome(True)

        result = drain_notification_deliveries(
            connection, send, profile_id=identity.profile_id
        )

        assert result.claimed == 4 and result.sent == 4
        assert len(seen) == 4
        for index, snapshot in enumerate(seen):
            statuses = [status for _, status in snapshot]
            # Everything before this one is already delivered, this one is the
            # only claim outstanding, and nothing after it has been touched.
            assert statuses[:index] == ["SENT"] * index
            assert statuses[index] == "IN_FLIGHT"
            assert statuses[index + 1 :] == ["PENDING"] * (len(statuses) - index - 1)
            assert statuses.count("IN_FLIGHT") == 1
        assert [row[3] for row in targets(connection)] == ["SENT"] * 4
        assert all(row[10] is None and row[11] is None for row in targets(connection))
    finally:
        connection.close()


def test_a_second_drain_takes_a_different_target_but_never_the_one_in_flight(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    path = database_path(connection)
    try:
        for number in (1, 2, 3):
            subscribe(connection, identity.profile_id, number)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)
        connection.commit()
        other = sqlite3.connect(path)
        other.execute("PRAGMA foreign_keys = ON")
        taken = []

        def send(target, payload):
            # A rival drain runs while this message is on the wire. It may
            # take work — just never this target.
            stolen, held = claim(other, identity.profile_id)
            taken.append((target.target_id, [item.target_id for item in held]))
            assert target.target_id not in [item.target_id for item in held]
            assert stolen != target.claim_token
            return DeliveryOutcome(True)

        try:
            result = drain_notification_deliveries(
                connection, send, profile_id=identity.profile_id
            )
        finally:
            other.close()

        # The rival took the two the first drain had not reached yet, so the
        # first drain sent exactly one and then found nothing due.
        assert len(taken) == 1
        mine, theirs = taken[0]
        assert len(theirs) == 2 and mine not in theirs
        assert (result.claimed, result.sent) == (1, 1)
        statuses = {row[0]: row[3] for row in targets(connection)}
        assert statuses[mine] == "SENT"
        assert all(statuses[target_id] == "IN_FLIGHT" for target_id in theirs)
    finally:
        connection.close()


def test_limit_bounds_the_attempts_and_leaves_nothing_claimed_behind(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        for number in (1, 2, 3, 4, 5):
            subscribe(connection, identity.profile_id, number)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)
        send, calls = outcomes(201, 201)

        result = drain_notification_deliveries(
            connection, send, profile_id=identity.profile_id, limit=2
        )

        assert (result.claimed, result.attempted, result.sent) == (2, 2, 2)
        assert len(calls) == 2
        statuses = [row[3] for row in targets(connection)]
        assert statuses == ["SENT", "SENT", "PENDING", "PENDING", "PENDING"]
        # Nothing is left claimed, so no rival drain has to wait out a lease.
        assert not any(row[3] == "IN_FLIGHT" for row in targets(connection))
        assert all(row[10] is None and row[11] is None for row in targets(connection))
        report = read_delivery_status(connection, profile_id=identity.profile_id)
        assert (report.targets_in_flight, report.targets_due) == (0, 3)
        assert batches(connection)[0][2] == DeliveryBatchStatus.PENDING.value

        # The next pass picks up exactly where this one stopped.
        again, more = outcomes(201, 201, 201)
        assert (
            drain_notification_deliveries(
                connection, again, profile_id=identity.profile_id
            ).sent
            == 3
        )
        assert len(more) == 3
        assert [row[3] for row in targets(connection)] == ["SENT"] * 5
    finally:
        connection.close()


def test_a_drain_that_cannot_build_a_payload_gives_the_claim_back(tmp_path):
    """Fail closed, and without stranding the claim for a whole lease."""
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        # A stored event whose click target is not internal: nothing may be
        # sent from it, and nothing may be invented for it either.
        broken = json.loads(
            connection.execute(
                "SELECT payload_json FROM notification_events"
            ).fetchone()[0]
        )
        broken["opportunity"]["target_url"] = "https://evil.example.invalid/x"
        connection.execute(
            "UPDATE notification_events SET payload_json=?",
            (json.dumps(broken, separators=(",", ":"), sort_keys=True),),
        )
        connection.commit()
        send, calls = outcomes(201)

        with pytest.raises(NotificationPayloadError):
            drain_notification_deliveries(
                connection, send, profile_id=identity.profile_id
            )

        assert calls == []
        assert connection.in_transaction is False
        row = targets(connection)[0]
        assert row[3] == "PENDING" and row[4] == 0
        assert row[10] is None and row[11] is None
        assert row[8] is None and row[9] is None
    finally:
        connection.close()


def test_a_sender_that_raises_gives_the_claim_back_before_the_error_escapes(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)

        def explode(target, payload):
            raise RuntimeError("sender blew up")

        with pytest.raises(RuntimeError, match="sender blew up"):
            drain_notification_deliveries(
                connection, explode, profile_id=identity.profile_id
            )

        row = targets(connection)[0]
        assert row[3] == "PENDING" and row[4] == 0
        assert row[10] is None and row[11] is None

        # A sender that answers with nonsense is treated the same way.
        with pytest.raises(NotificationDeliveryError, match="unusable outcome"):
            drain_notification_deliveries(
                connection,
                lambda target, payload: "delivered, honest",
                profile_id=identity.profile_id,
            )
        row = targets(connection)[0]
        assert row[3] == "PENDING" and row[4] == 0 and row[10] is None

        # And the target is still perfectly deliverable afterwards.
        send, _ = outcomes(201)
        assert (
            drain_notification_deliveries(
                connection, send, profile_id=identity.profile_id
            ).sent
            == 1
        )
    finally:
        connection.close()


def test_releasing_a_claim_needs_the_token_and_changes_nothing_else(tmp_path):
    connection, identity, _ = one_event(tmp_path)
    try:
        subscribe(connection, identity.profile_id, 1)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)
        token, held = claim(connection, identity.profile_id)
        target_id = held[0].target_id
        claimed_row = targets(connection)[0]

        assert (
            release_delivery_claim(
                connection, target_id=target_id, claim_token=new_claim_token()
            )
            is False
        )
        assert targets(connection) == [claimed_row]

        assert (
            release_delivery_claim(connection, target_id=target_id, claim_token=token)
            is True
        )
        row = targets(connection)[0]
        assert row[3] == "PENDING" and row[5] is not None
        assert row[10] is None and row[11] is None
        # Nothing about the attempt history moved.
        history = (4, 6, 7, 8, 9)
        assert tuple(row[index] for index in history) == tuple(
            claimed_row[index] for index in history
        )
        # Releasing twice is a no-op, not an error.
        assert (
            release_delivery_claim(connection, target_id=target_id, claim_token=token)
            is False
        )
    finally:
        connection.close()
