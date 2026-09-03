"""Phase 5.3C2 over real SQLite: opting in never replays what came before it.

Everything here is about one line. A subscription records the highest
notification event id its profile had at the instant it became active, and it
is a recipient of an event only when that event's id is strictly above the
line. Because both sides are ids read inside one transaction, the line holds
however long after the event materialization runs, and a race between an event
and an opt-in is settled by whichever commits first rather than by two clocks.

Nothing here opens a socket: the only delivery these tests drive is the
materialization that decides who a message is for.
"""

import sqlite3

import pytest

from services.digital_twin.repository import ensure_user_profile
from services.notifications import (
    DeliveryBatchEmptyReason,
    DeliveryBatchStatus,
    NotificationDeliveryError,
    NotificationSyncStatus,
    PushSubscriptionError,
    PushSubscriptionOwnershipError,
    materialize_delivery_batches,
    read_delivery_status,
    subscribe_push_subscription,
    sync_notification_policy,
    unsubscribe_push_subscription,
)
from tests.integration.test_notification_policy_sync_sqlite import (
    URGENT,
    baseline,
    excluded,
    fixture,
    included,
    store_run,
)
from tests.integration.test_push_activation_watermark_migration_sqlite import (
    rewind_to_0020,
)
from tests.integration.test_push_subscriptions_sqlite import (
    OTHER_P256DH,
    P256DH,
    ROTATED_P256DH,
)

AUTH = "c" * 22
ROTATED_AUTH = "d" * 22
WATERMARK = "notification_event_watermark"

#: A batch nobody receives is terminal under 0020's single status; 0021's
#: reason is what says which of the two emptinesses produced it.
EMPTY = DeliveryBatchStatus.NO_ACTIVE_SUBSCRIPTIONS.value
NO_DEVICE = DeliveryBatchEmptyReason.NO_ACTIVE_SUBSCRIPTIONS.value
TOO_LATE = DeliveryBatchEmptyReason.NO_ELIGIBLE_SUBSCRIPTIONS.value
PENDING = DeliveryBatchStatus.PENDING.value


def endpoint(number):
    return f"https://push.example.invalid/subscription/{number}"


def opt_in(connection, profile_id, number, *, p256dh=P256DH, auth=AUTH):
    """One browser opt-in, through the real persistence path."""
    return subscribe_push_subscription(
        connection,
        profile_id=profile_id,
        endpoint=endpoint(number),
        p256dh=p256dh,
        auth=auth,
    )


def opt_out(connection, profile_id, number):
    return unsubscribe_push_subscription(
        connection, profile_id=profile_id, endpoint=endpoint(number)
    )


def stored(connection):
    return connection.execute(
        f"SELECT id,status,p256dh,auth,{WATERMARK} FROM push_subscriptions ORDER BY id"
    ).fetchall()


def watermark_of(connection, subscription_id):
    return connection.execute(
        f"SELECT {WATERMARK} FROM push_subscriptions WHERE id=?", (subscription_id,)
    ).fetchone()[0]


class Radar:
    """The real 5.3B policy, driven one movement at a time.

    Each :meth:`move` promotes one more opportunity into the actionable
    Portfolio and leaves every other position exactly where it was, so the
    policy produces exactly one event per call and the test can name the id it
    produced. Nothing here is a fixture event: they are stored by the real
    sync, with real fingerprints and a real outbox row.
    """

    def __init__(self, connection, identity, opportunity_ids):
        self.connection = connection
        self.identity = identity
        self.ids = opportunity_ids
        self._actionable: set[int] = set()

    def move(self, index):
        """Move opportunity ``index`` in, and return the event id it produced."""
        self._actionable.add(self.ids[index])
        store_run(
            self.connection,
            self.identity.profile_id,
            {
                opportunity_id: (
                    included(priority=URGENT)
                    if opportunity_id in self._actionable
                    else excluded()
                )
                for opportunity_id in self.ids
            },
        )
        result = sync_notification_policy(self.connection, self.identity.profile_id)
        assert result.status is NotificationSyncStatus.PROCESSED
        assert result.event_count == 1
        return result.event_ids[0]


def ready(tmp_path, opportunities=3):
    """A migrated database whose Portfolio baseline is set and silent."""
    connection, identity, ids = fixture(tmp_path, opportunities)
    baseline(connection, identity, {opportunity: excluded() for opportunity in ids})
    return connection, identity, Radar(connection, identity, ids)


def max_event_id(connection, profile_id):
    return connection.execute(
        "SELECT COALESCE(MAX(id),0) FROM notification_events WHERE profile_id=?",
        (profile_id,),
    ).fetchone()[0]


def batches(connection):
    return connection.execute(
        "SELECT id,status,target_count,empty_reason FROM notification_delivery_batches"
        " ORDER BY id"
    ).fetchall()


def targets(connection):
    return connection.execute(
        "SELECT batch_id,subscription_id,status FROM notification_delivery_targets"
        " ORDER BY id"
    ).fetchall()


# --------------------------------------------------------------------------
# What an activation records
# --------------------------------------------------------------------------


def test_a_new_subscription_starts_level_with_the_profiles_current_event(tmp_path):
    connection, identity, radar = ready(tmp_path)
    try:
        first = opt_in(connection, identity.profile_id, 1)
        assert first.created and first.notification_event_watermark == 0

        event_id = radar.move(0)
        second = opt_in(connection, identity.profile_id, 2)

        # A device that arrives after the movement is level with it, not behind.
        assert second.created and second.notification_event_watermark == event_id
        assert watermark_of(connection, first.subscription_id) == 0
        assert watermark_of(connection, second.subscription_id) == event_id
    finally:
        connection.close()


def test_a_reactivation_refreshes_the_boundary_to_the_present(tmp_path):
    connection, identity, radar = ready(tmp_path)
    try:
        subscription = opt_in(connection, identity.profile_id, 1)
        first = radar.move(0)
        assert opt_out(connection, identity.profile_id, 1).revoked

        second = radar.move(1)
        again = opt_in(connection, identity.profile_id, 1)

        # Coming back is opting in again: the time away is not owed to anyone.
        assert again.subscription_id == subscription.subscription_id
        assert (again.created, again.reactivated) == (False, True)
        assert again.notification_event_watermark == second
        assert second > first
    finally:
        connection.close()


def test_re_sending_an_identical_subscription_changes_absolutely_nothing(tmp_path):
    connection, identity, radar = ready(tmp_path)
    try:
        opt_in(connection, identity.profile_id, 1)
        radar.move(0)
        before = stored(connection)

        again = opt_in(connection, identity.profile_id, 1)

        assert (again.created, again.reactivated, again.keys_updated) == (
            False,
            False,
            False,
        )
        assert again.changed is False
        assert again.notification_event_watermark == 0
        # A browser that re-posts what the server already has has not opted in
        # again, so nothing about the row moves — the boundary least of all.
        assert stored(connection) == before
    finally:
        connection.close()


def test_a_key_rotation_replaces_the_credential_and_keeps_the_boundary(tmp_path):
    connection, identity, radar = ready(tmp_path)
    try:
        subscription = opt_in(connection, identity.profile_id, 1)
        radar.move(0)

        rotated = opt_in(
            connection,
            identity.profile_id,
            1,
            p256dh=ROTATED_P256DH,
            auth=ROTATED_AUTH,
        )

        assert (rotated.created, rotated.reactivated, rotated.keys_updated) == (
            False,
            False,
            True,
        )
        # It never stopped being subscribed, so it never re-started either.
        assert rotated.notification_event_watermark == 0
        assert stored(connection) == [
            (subscription.subscription_id, "ACTIVE", ROTATED_P256DH, ROTATED_AUTH, 0)
        ]
    finally:
        connection.close()


def test_a_second_endpoint_is_a_second_device_with_a_boundary_of_its_own(tmp_path):
    connection, identity, radar = ready(tmp_path)
    try:
        first = opt_in(connection, identity.profile_id, 1)
        event_id = radar.move(0)
        second = opt_in(connection, identity.profile_id, 2, p256dh=OTHER_P256DH)

        assert first.subscription_id != second.subscription_id
        assert [row[-1] for row in stored(connection)] == [0, event_id]
    finally:
        connection.close()


def test_the_boundary_changes_nothing_about_ownership_or_who_may_subscribe(tmp_path):
    connection, identity, radar = ready(tmp_path)
    try:
        other = ensure_user_profile(connection, "boundary-other@example.invalid")
        opt_in(connection, identity.profile_id, 1)
        radar.move(0)

        # Another profile cannot take over the endpoint, and the refusal still
        # leaves the stored row — boundary included — completely untouched.
        before = stored(connection)
        with pytest.raises(PushSubscriptionOwnershipError):
            opt_in(connection, other.profile.id, 1)
        assert stored(connection) == before

        # Its own endpoint gets its own profile's boundary, which is 0: the
        # events above belong to a different profile entirely.
        mine = opt_in(connection, other.profile.id, 99)
        assert mine.notification_event_watermark == 0

        with pytest.raises(PushSubscriptionError, match="profile does not exist"):
            opt_in(connection, identity.profile_id + 9999, 100)
    finally:
        connection.close()


# --------------------------------------------------------------------------
# What the boundary decides
# --------------------------------------------------------------------------


def test_an_event_at_or_below_the_boundary_is_never_a_target_and_one_above_is(
    tmp_path,
):
    connection, identity, radar = ready(tmp_path)
    try:
        below = radar.move(0)
        at = radar.move(1)
        subscription = opt_in(connection, identity.profile_id, 1)
        assert subscription.notification_event_watermark == at
        above = radar.move(2)
        assert below < at < above

        result = materialize_delivery_batches(
            connection, profile_id=identity.profile_id
        )

        # Three events, three batches, and exactly one recipient among them.
        assert result.created_batches == 3
        assert (result.created_targets, result.empty_batches) == (1, 2)
        assert result.ineligible_batches == 2
        by_event = dict(
            connection.execute(
                """SELECT notification_events.id,notification_delivery_batches.id
                FROM notification_delivery_batches
                JOIN notification_outbox
                  ON notification_outbox.id=notification_delivery_batches.outbox_id
                JOIN notification_events
                  ON notification_events.id=notification_outbox.event_id"""
            )
        )
        assert targets(connection) == [
            (by_event[above], subscription.subscription_id, "PENDING")
        ]
        assert sorted(batches(connection)) == sorted(
            [
                (by_event[below], EMPTY, 0, TOO_LATE),
                (by_event[at], EMPTY, 0, TOO_LATE),
                (by_event[above], PENDING, 1, None),
            ]
        )
    finally:
        connection.close()


def test_several_devices_with_different_boundaries_fan_out_exactly(tmp_path):
    connection, identity, radar = ready(tmp_path)
    try:
        early = opt_in(connection, identity.profile_id, 1)
        first = radar.move(0)
        middle = opt_in(connection, identity.profile_id, 2, p256dh=OTHER_P256DH)
        second = radar.move(1)
        late = opt_in(connection, identity.profile_id, 3, p256dh=ROTATED_P256DH)
        third = radar.move(2)

        materialize_delivery_batches(connection, profile_id=identity.profile_id)

        by_event = dict(
            connection.execute(
                """SELECT notification_events.id,notification_delivery_batches.id
                FROM notification_delivery_batches
                JOIN notification_outbox
                  ON notification_outbox.id=notification_delivery_batches.outbox_id
                JOIN notification_events
                  ON notification_events.id=notification_outbox.event_id"""
            )
        )
        # Each event reaches exactly the devices that already existed for it.
        assert sorted(targets(connection)) == sorted(
            [
                (by_event[first], early.subscription_id, "PENDING"),
                (by_event[second], early.subscription_id, "PENDING"),
                (by_event[second], middle.subscription_id, "PENDING"),
                (by_event[third], early.subscription_id, "PENDING"),
                (by_event[third], middle.subscription_id, "PENDING"),
                (by_event[third], late.subscription_id, "PENDING"),
            ]
        )
    finally:
        connection.close()


def test_a_batch_with_no_eligible_device_is_terminal_at_once_and_says_why(tmp_path):
    connection, identity, radar = ready(tmp_path)
    try:
        # Nobody subscribed at all.
        nobody = radar.move(0)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)
        assert batches(connection) == [(1, EMPTY, 0, NO_DEVICE)]

        # Somebody subscribed, but only after the next event happened.
        second = radar.move(1)
        opt_in(connection, identity.profile_id, 1)
        result = materialize_delivery_batches(
            connection, profile_id=identity.profile_id
        )

        assert (result.empty_batches, result.ineligible_batches) == (1, 1)
        assert batches(connection)[1] == (2, EMPTY, 0, TOO_LATE)
        assert targets(connection) == []
        # Terminal on the spot: both batches are completed, neither is waiting.
        assert [
            row[0]
            for row in connection.execute(
                "SELECT completed_at IS NOT NULL FROM notification_delivery_batches"
            )
        ] == [1, 1]

        report = read_delivery_status(connection, profile_id=identity.profile_id)
        assert report.batches_without_subscriptions == 1
        assert report.batches_without_eligible_subscriptions == 1
        assert (report.batches_pending, report.unmaterialized_outbox) == (0, 0)
        assert nobody < second
    finally:
        connection.close()


def test_a_late_subscription_never_joins_an_old_batch_or_an_old_outbox_row(tmp_path):
    """Neither a materialized batch nor an unmaterialized one is backfilled.

    The frozen snapshot already covered the first case in 5.3C1. The second is
    the case 5.3C2 exists for: an outbox row that nobody has materialized yet
    is still an event from before this device, and materializing it later must
    not quietly turn it into that device's first notification.
    """
    connection, identity, radar = ready(tmp_path)
    try:
        early = opt_in(connection, identity.profile_id, 1)
        materialized = radar.move(0)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)
        before = targets(connection)

        unmaterialized = radar.move(1)
        late = opt_in(connection, identity.profile_id, 2, p256dh=OTHER_P256DH)
        assert late.notification_event_watermark == unmaterialized

        result = materialize_delivery_batches(
            connection, profile_id=identity.profile_id
        )

        # The already frozen batch is untouched, and the one materialized now
        # goes only to the device that existed when its event happened.
        assert result.created_batches == 1 and result.created_targets == 1
        assert targets(connection)[: len(before)] == before
        assert late.subscription_id not in {row[1] for row in targets(connection)}
        assert {row[1] for row in targets(connection)} == {early.subscription_id}

        # And the next event reaches both, because both were there for it.
        radar.move(2)
        materialize_delivery_batches(connection, profile_id=identity.profile_id)
        newest = max(row[0] for row in batches(connection))
        assert {row[1] for row in targets(connection) if row[0] == newest} == {
            early.subscription_id,
            late.subscription_id,
        }
        assert materialized < unmaterialized
    finally:
        connection.close()


def test_a_reactivated_device_rejoins_from_the_present_and_not_from_before(tmp_path):
    connection, identity, radar = ready(tmp_path)
    try:
        subscription = opt_in(connection, identity.profile_id, 1)
        opt_out(connection, identity.profile_id, 1)
        # Everything that happened while it was off is not its to catch up on.
        away = radar.move(0)
        opt_in(connection, identity.profile_id, 1)
        back = radar.move(1)

        materialize_delivery_batches(connection, profile_id=identity.profile_id)

        by_event = dict(
            connection.execute(
                """SELECT notification_events.id,notification_delivery_batches.id
                FROM notification_delivery_batches
                JOIN notification_outbox
                  ON notification_outbox.id=notification_delivery_batches.outbox_id
                JOIN notification_events
                  ON notification_events.id=notification_outbox.event_id"""
            )
        )
        assert targets(connection) == [
            (by_event[back], subscription.subscription_id, "PENDING")
        ]
        assert by_event[away] != by_event[back]
    finally:
        connection.close()


# --------------------------------------------------------------------------
# A database that has not been migrated, and one being written to twice
# --------------------------------------------------------------------------


def test_code_that_needs_the_boundary_refuses_a_database_that_stopped_at_0020(
    tmp_path,
):
    """Fail closed, in a sentence — never with "no such column"."""
    connection, identity, radar = ready(tmp_path)
    try:
        radar.move(0)
        rewind_to_0020(connection)

        with pytest.raises(PushSubscriptionError, match="schema is not ready"):
            opt_in(connection, identity.profile_id, 1)
        assert connection.in_transaction is False
        with pytest.raises(NotificationDeliveryError, match="schema is not ready"):
            materialize_delivery_batches(connection, profile_id=identity.profile_id)
        assert connection.in_transaction is False
        with pytest.raises(NotificationDeliveryError, match="schema is not ready"):
            read_delivery_status(connection, profile_id=identity.profile_id)

        # Nothing was written on the way out.
        assert connection.execute(
            "SELECT COUNT(*) FROM push_subscriptions"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM notification_delivery_batches"
        ).fetchone() == (0,)
    finally:
        connection.close()


def test_the_boundary_is_decided_by_the_commit_order_and_not_by_a_clock(tmp_path):
    """Two connections, one file: an event and an opt-in cannot interleave.

    The event's transaction and the subscription's are both ``BEGIN
    IMMEDIATE``, so SQLite serializes them. Whichever commits first is the one
    the other one sees, which is exactly what makes the comparison of two ids
    an ordering rather than a guess.
    """
    connection, identity, radar = ready(tmp_path)
    path = connection.execute("PRAGMA database_list").fetchall()[0][2]
    # A second connection that refuses to wait, so a collision is visible
    # rather than silently serialized behind a timeout.
    writer = sqlite3.connect(path, timeout=0)
    writer.execute("PRAGMA foreign_keys = ON")
    try:
        opt_in(connection, identity.profile_id, 1)
        first = radar.move(0)

        # An event whose transaction is still open is not yet an event. The
        # escalation is written by hand rather than through the policy because
        # the point is the open transaction, not what put it there.
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO notification_events
            (profile_id,event_type,opportunity_id,previous_portfolio_run_id,
             portfolio_run_id,policy_version,event_fingerprint,payload_json)
            SELECT profile_id,'ATTENTION_ESCALATED',opportunity_id,
             previous_portfolio_run_id,portfolio_run_id,policy_version,?,payload_json
            FROM notification_events WHERE id=?""",
            ("f" * 64, first),
        )
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            subscribe_push_subscription(
                writer,
                profile_id=identity.profile_id,
                endpoint=endpoint(2),
                p256dh=OTHER_P256DH,
                auth=AUTH,
            )
        pending = max_event_id(connection, identity.profile_id)
        connection.execute("COMMIT")

        # Committed before the activation: inside the boundary, never delivered.
        after = subscribe_push_subscription(
            writer,
            profile_id=identity.profile_id,
            endpoint=endpoint(2),
            p256dh=OTHER_P256DH,
            auth=AUTH,
        )
        assert after.notification_event_watermark == pending

        # Committed after the activation: outside the boundary, deliverable.
        later = radar.move(1)
        assert later > after.notification_event_watermark

        materialize_delivery_batches(connection, profile_id=identity.profile_id)
        eligible = {
            row[1]
            for row in connection.execute(
                """SELECT notification_events.id,
                notification_delivery_targets.subscription_id
                FROM notification_delivery_targets
                JOIN notification_delivery_batches
                  ON notification_delivery_batches.id
                     =notification_delivery_targets.batch_id
                JOIN notification_outbox
                  ON notification_outbox.id=notification_delivery_batches.outbox_id
                JOIN notification_events
                  ON notification_events.id=notification_outbox.event_id
                WHERE notification_events.id=?""",
                (later,),
            )
        }
        assert after.subscription_id in eligible
        assert (
            connection.execute(
                """SELECT COUNT(*) FROM notification_delivery_targets
                JOIN notification_delivery_batches
                  ON notification_delivery_batches.id
                     =notification_delivery_targets.batch_id
                JOIN notification_outbox
                  ON notification_outbox.id=notification_delivery_batches.outbox_id
                WHERE notification_outbox.event_id=?
                  AND notification_delivery_targets.subscription_id=?""",
                (pending, after.subscription_id),
            ).fetchone()[0]
            == 0
        )
    finally:
        writer.close()
        connection.close()
