"""Integration tests for append-only notification event and outbox persistence."""

from dataclasses import replace

import pytest

from services.notifications import (
    NOTIFICATION_POLICY_VERSION,
    NotificationEventRecord,
    NotificationEventType,
    NotificationPersistenceError,
    store_notification_events,
)
from tests.integration.test_notification_policy_migration_sqlite import two_runs

NEW = NotificationEventType.NEW_ACTIONABLE_OPPORTUNITY
ESCALATED = NotificationEventType.ATTENTION_ESCALATED
PAYLOAD = '{"event_type":"NEW_ACTIONABLE_OPPORTUNITY","opportunity_id":1}'


def record(identity, opportunity, previous_run, run, **changes):
    return replace(
        NotificationEventRecord(
            identity.profile_id,
            NEW,
            opportunity,
            previous_run,
            run,
            "a" * 64,
            PAYLOAD,
        ),
        **changes,
    )


def store(connection, records):
    connection.execute("BEGIN IMMEDIATE")
    try:
        result = store_notification_events(connection, records)
        connection.execute("COMMIT")
        return result
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def counts(connection):
    return (
        connection.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0],
        connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0],
    )


def assert_rejected_without_writes(connection, records, match):
    before = counts(connection)
    with pytest.raises(NotificationPersistenceError, match=match):
        store(connection, records)
    assert counts(connection) == before
    assert connection.in_transaction is False


def test_every_event_is_stored_with_exactly_one_outbox_row(tmp_path):
    connection, identity, ids, first, second = two_runs(tmp_path)
    records = (
        record(identity, ids[0], first, second),
        record(
            identity,
            ids[1],
            first,
            second,
            event_type=ESCALATED,
            event_fingerprint="b" * 64,
        ),
    )
    result = store(connection, records)

    assert (result.created_count, result.duplicate_count) == (2, 0)
    assert len(result.event_ids) == 2
    assert counts(connection) == (2, 2)
    assert connection.execute(
        """SELECT notification_events.event_type,notification_events.opportunity_id,
        notification_events.policy_version FROM notification_events
        JOIN notification_outbox ON notification_outbox.event_id=notification_events.id
        ORDER BY notification_events.id"""
    ).fetchall() == [
        ("NEW_ACTIONABLE_OPPORTUNITY", ids[0], NOTIFICATION_POLICY_VERSION),
        ("ATTENTION_ESCALATED", ids[1], NOTIFICATION_POLICY_VERSION),
    ]


def test_restoring_the_same_events_is_a_byte_noop_reported_as_duplicates(tmp_path):
    connection, identity, ids, first, second = two_runs(tmp_path)
    records = (record(identity, ids[0], first, second),)
    created = store(connection, records)
    dump = tuple(connection.iterdump())
    repeated = store(connection, records)

    assert (created.created_count, created.duplicate_count) == (1, 0)
    assert (repeated.created_count, repeated.duplicate_count) == (0, 1)
    assert repeated.event_ids == created.event_ids
    assert tuple(connection.iterdump()) == dump


def test_an_empty_batch_stores_nothing_and_still_needs_a_transaction(tmp_path):
    connection, _, _, _, _ = two_runs(tmp_path)
    with pytest.raises(NotificationPersistenceError, match="active transaction"):
        store_notification_events(connection, ())
    assert store(connection, ()).created_count == 0
    assert counts(connection) == (0, 0)


def test_invalid_records_and_batches_are_refused_without_writing_anything(tmp_path):
    connection, identity, ids, first, second = two_runs(tmp_path)
    valid = record(identity, ids[0], first, second)
    assert_rejected_without_writes(connection, [valid], "must be a tuple")
    assert_rejected_without_writes(
        connection, (valid, valid), "duplicate event fingerprints"
    )
    for changes, match in (
        ({"profile_id": 0}, "profile_id must be a positive integer"),
        ({"opportunity_id": -1}, "opportunity_id must be a positive integer"),
        ({"previous_portfolio_run_id": True}, "must be a positive integer"),
        ({"portfolio_run_id": first}, "single Portfolio run"),
        ({"event_type": "NEW_ACTIONABLE_OPPORTUNITY"}, "invalid notification event"),
        ({"policy_version": "notification-policy-v2"}, "unexpected notification"),
        ({"event_fingerprint": "z" * 64}, "invalid event fingerprint"),
        ({"event_fingerprint": "a" * 63}, "invalid event fingerprint"),
        ({"payload_json": "  "}, "invalid event payload"),
        ({"payload_json": f" {PAYLOAD}"}, "invalid event payload"),
        ({"payload_json": None}, "invalid event payload"),
    ):
        assert_rejected_without_writes(
            connection, (record(identity, ids[0], first, second, **changes),), match
        )


def test_a_stored_event_that_contradicts_its_fingerprint_is_refused(tmp_path):
    connection, identity, ids, first, second = two_runs(tmp_path)
    records = (record(identity, ids[0], first, second),)
    store(connection, records)
    connection.execute(
        "UPDATE notification_events SET payload_json=?", ('{"tampered":true}',)
    )
    connection.commit()

    assert_rejected_without_writes(connection, records, "contradicts its fingerprint")


def test_a_stored_event_without_its_outbox_row_is_refused(tmp_path):
    connection, identity, ids, first, second = two_runs(tmp_path)
    records = (record(identity, ids[0], first, second),)
    store(connection, records)
    connection.execute("DELETE FROM notification_outbox")
    connection.commit()

    assert_rejected_without_writes(connection, records, "no outbox row")
