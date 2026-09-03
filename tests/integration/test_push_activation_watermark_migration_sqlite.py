"""Integration tests for migration 0021: the push activation watermark.

The upgrade a real operational database takes is 0020, then 0021, and the one
thing 0021 must not do on the way is hand the profile's whole history to a
device that is already registered. So every subscription that exists when it
runs — active or revoked — is initialized to its profile's current highest
event id, which is exactly the boundary a fresh opt-in would have taken.
"""

import sqlite3

import pytest

from services.collector.database.migrations import apply_migrations
from services.digital_twin.repository import ensure_user_profile
from tests.integration.test_notification_policy_migration_sqlite import (
    event_arguments,
    insert_event,
    two_runs,
)
from tests.integration.test_push_subscriptions_sqlite import P256DH

ENDPOINT = "https://push.example.invalid/subscription/abc"
OTHER_ENDPOINT = "https://push.example.invalid/subscription/def"
AUTH = "c" * 22
WATERMARK = "notification_event_watermark"


def rewind_to_0020(connection):
    """Turn a fully migrated database back into a genuine 0020 one.

    The upgrade under test needs a database that already holds real 0018-0020
    data — subscriptions, events, an outbox, batches — and does not yet have
    the watermark. Building that fixture needs the whole upstream stack, which
    only exists on a fully migrated database, so the boundary is removed again
    afterwards rather than the fixture being rebuilt by hand. What is left is
    the schema 0020 produces and the rows an operator would really have.
    """
    for trigger in (
        "push_subscriptions_watermark_covers_existing_events",
        "push_subscriptions_reactivation_refreshes_watermark",
        "notification_delivery_batches_empty_reason_is_stated",
    ):
        connection.execute(f"DROP TRIGGER {trigger}")
    connection.execute("DROP INDEX idx_push_subscriptions_profile_status_watermark")
    connection.execute(f"ALTER TABLE push_subscriptions DROP COLUMN {WATERMARK}")
    connection.execute(
        "ALTER TABLE notification_delivery_batches DROP COLUMN empty_reason"
    )
    connection.execute("DELETE FROM schema_migrations WHERE version='0021'")
    connection.commit()
    assert WATERMARK not in {
        row[1] for row in connection.execute("PRAGMA table_info(push_subscriptions)")
    }


def insert_subscription(connection, profile_id, endpoint, *, status="ACTIVE", **extra):
    columns = {
        "profile_id": profile_id,
        "endpoint": endpoint,
        "p256dh": P256DH,
        "auth": AUTH,
        "status": status,
        "revoked_at": None if status == "ACTIVE" else "2026-01-01 00:00:00",
    } | extra
    names = ",".join(columns)
    placeholders = ",".join("?" for _ in columns)
    return connection.execute(
        f"INSERT INTO push_subscriptions ({names}) VALUES ({placeholders})"
        " RETURNING id",
        tuple(columns.values()),
    ).fetchone()[0]


def watermarks(connection):
    return connection.execute(
        f"SELECT id,status,{WATERMARK} FROM push_subscriptions ORDER BY id"
    ).fetchall()


def announce(connection, identity, opportunity_id, first, second, fingerprint):
    return insert_event(
        connection,
        event_arguments(
            identity.profile_id,
            opportunity_id,
            first,
            second,
            event_fingerprint=fingerprint,
        ),
    )


def test_0021_backfills_every_existing_subscription_to_its_profiles_last_event(
    tmp_path,
):
    """The upgrade path a real operational database takes: 0020, then 0021.

    Nobody who was already subscribed learns anything about the past, and a
    profile that has no event at all starts at zero rather than at somebody
    else's history.
    """
    connection, identity, opportunity_ids, first, second = two_runs(tmp_path)
    try:
        other = ensure_user_profile(connection, "watermark-other@example.invalid")
        mine = [
            announce(connection, identity, opportunity_ids[0], first, second, "a" * 64),
            announce(connection, identity, opportunity_ids[1], first, second, "b" * 64),
        ]
        outbox_id = connection.execute(
            "INSERT INTO notification_outbox (event_id) VALUES (?) RETURNING id",
            (mine[0],),
        ).fetchone()[0]
        connection.commit()
        rewind_to_0020(connection)

        active = insert_subscription(connection, identity.profile_id, ENDPOINT)
        revoked = insert_subscription(
            connection, identity.profile_id, OTHER_ENDPOINT, status="REVOKED"
        )
        eventless = insert_subscription(
            connection, other.profile.id, "https://push.example.invalid/other"
        )
        empty_batch = connection.execute(
            """INSERT INTO notification_delivery_batches
            (outbox_id,status,target_count,completed_at)
            VALUES (?,'NO_ACTIVE_SUBSCRIPTIONS',0,'2026-01-01 00:00:00')
            RETURNING id""",
            (outbox_id,),
        ).fetchone()[0]
        connection.commit()
        before = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in (
                "profiles",
                "push_subscriptions",
                "notification_events",
                "notification_outbox",
            )
        }

        assert apply_migrations(connection) == ["0021"]

        # Active or revoked makes no difference: both are already-known devices
        # and neither may be handed the two events that predate the upgrade.
        assert watermarks(connection) == [
            (active, "ACTIVE", max(mine)),
            (revoked, "REVOKED", max(mine)),
            # Another profile's events are not this profile's history.
            (eventless, "ACTIVE", 0),
        ]
        # Every 0018-0020 column is exactly as it was: the other tables row for
        # row, and push_subscriptions once the appended watermark is trimmed.
        assert {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in before
            if table != "push_subscriptions"
        } == {
            table: rows
            for table, rows in before.items()
            if table != "push_subscriptions"
        }
        assert [
            row[:-1]
            for row in connection.execute(
                "SELECT * FROM push_subscriptions ORDER BY id"
            )
        ] == before["push_subscriptions"]
        # A batch that was empty before 0021 keeps the only reason it can have.
        assert connection.execute(
            "SELECT id,status,empty_reason FROM notification_delivery_batches"
        ).fetchall() == [
            (empty_batch, "NO_ACTIVE_SUBSCRIPTIONS", "NO_ACTIVE_SUBSCRIPTIONS")
        ]
        assert list(connection.execute("PRAGMA integrity_check")) == [("ok",)]
        assert list(connection.execute("PRAGMA foreign_key_check")) == []
        assert apply_migrations(connection) == []
    finally:
        connection.close()


def test_0021_leaves_a_database_with_no_subscription_at_all_untouched(tmp_path):
    connection, identity, opportunity_ids, first, second = two_runs(tmp_path)
    try:
        announce(connection, identity, opportunity_ids[0], first, second, "a" * 64)
        connection.commit()
        rewind_to_0020(connection)

        assert apply_migrations(connection) == ["0021"]

        assert watermarks(connection) == []
        assert list(connection.execute("PRAGMA integrity_check")) == [("ok",)]
    finally:
        connection.close()


def test_the_watermark_column_is_a_non_negative_integer_that_must_be_stated(tmp_path):
    connection, identity, opportunity_ids, first, second = two_runs(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            insert_subscription(
                connection, identity.profile_id, ENDPOINT, **{WATERMARK: -1}
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            insert_subscription(
                connection, identity.profile_id, ENDPOINT, **{WATERMARK: None}
            )
        connection.rollback()
        subscription_id = insert_subscription(
            connection, identity.profile_id, ENDPOINT, **{WATERMARK: 0}
        )
        connection.commit()
        assert watermarks(connection) == [(subscription_id, "ACTIVE", 0)]
        assert opportunity_ids and first != second
    finally:
        connection.close()


def test_no_row_may_activate_behind_the_event_stream_whoever_writes_it(tmp_path):
    """The column default is 0; the triggers are what make that unusable.

    SQLite can only add a column with a constant default, and 0 is the value
    that would deliver a profile's whole history. These two triggers are the
    reason a hand-written INSERT or reactivation cannot quietly take it.
    """
    connection, identity, opportunity_ids, first, second = two_runs(tmp_path)
    try:
        event_id = announce(
            connection, identity, opportunity_ids[0], first, second, "a" * 64
        )
        connection.commit()

        # An insert that relies on the default, or names anything below the
        # stream, is refused outright.
        for watermark in ({}, {WATERMARK: 0}, {WATERMARK: event_id - 1}):
            with pytest.raises(sqlite3.IntegrityError, match="predates"):
                insert_subscription(
                    connection, identity.profile_id, ENDPOINT, **watermark
                )
            connection.rollback()

        subscription_id = insert_subscription(
            connection, identity.profile_id, ENDPOINT, **{WATERMARK: event_id}
        )
        connection.execute(
            "UPDATE push_subscriptions SET status='REVOKED',"
            " revoked_at='2026-01-01 00:00:00' WHERE id=?",
            (subscription_id,),
        )
        connection.commit()
        later = announce(
            connection, identity, opportunity_ids[1], first, second, "b" * 64
        )
        connection.commit()

        # Coming back with the boundary it had before is coming back to the
        # past: reactivation has to take the current one.
        with pytest.raises(sqlite3.IntegrityError, match="predates"):
            connection.execute(
                "UPDATE push_subscriptions SET status='ACTIVE',revoked_at=NULL"
                " WHERE id=?",
                (subscription_id,),
            )
        connection.rollback()
        connection.execute(
            f"UPDATE push_subscriptions SET status='ACTIVE',revoked_at=NULL,"
            f" {WATERMARK}=? WHERE id=?",
            (later, subscription_id),
        )
        connection.commit()
        assert watermarks(connection) == [(subscription_id, "ACTIVE", later)]
    finally:
        connection.close()


def test_a_live_subscription_keeps_its_watermark_through_a_key_rotation(tmp_path):
    """Only REVOKED -> ACTIVE is a new activation; nothing else moves the line."""
    connection, identity, opportunity_ids, first, second = two_runs(tmp_path)
    try:
        subscription_id = insert_subscription(
            connection, identity.profile_id, ENDPOINT, **{WATERMARK: 0}
        )
        connection.commit()
        announce(connection, identity, opportunity_ids[0], first, second, "a" * 64)
        connection.commit()

        # A rotation touches the credential, not the status, so the trigger
        # that guards activation does not fire and the boundary holds.
        connection.execute(
            "UPDATE push_subscriptions SET p256dh=?,auth=? WHERE id=?",
            (P256DH, "d" * 22, subscription_id),
        )
        # Revoking is not an activation either.
        connection.execute(
            "UPDATE push_subscriptions SET status='REVOKED',"
            " revoked_at='2026-01-01 00:00:00' WHERE id=?",
            (subscription_id,),
        )
        connection.commit()
        assert watermarks(connection) == [(subscription_id, "REVOKED", 0)]
    finally:
        connection.close()


def test_0021_lets_an_empty_batch_say_which_emptiness_it_was(tmp_path):
    connection, identity, opportunity_ids, first, second = two_runs(tmp_path)
    try:
        event_id = announce(
            connection, identity, opportunity_ids[0], first, second, "a" * 64
        )
        outbox_id = connection.execute(
            "INSERT INTO notification_outbox (event_id) VALUES (?) RETURNING id",
            (event_id,),
        ).fetchone()[0]
        connection.commit()

        def insert_batch(**changes):
            arguments = {
                "outbox_id": outbox_id,
                "status": "NO_ACTIVE_SUBSCRIPTIONS",
                "target_count": 0,
                "completed_at": "2026-01-01 00:00:00",
                "empty_reason": "NO_ELIGIBLE_SUBSCRIPTIONS",
            } | changes
            names = ",".join(arguments)
            placeholders = ",".join("?" for _ in arguments)
            return connection.execute(
                f"INSERT INTO notification_delivery_batches ({names})"
                f" VALUES ({placeholders}) RETURNING id",
                tuple(arguments.values()),
            ).fetchone()[0]

        for arguments in (
            # A reason only an empty batch may carry...
            {"status": "PENDING", "target_count": 1, "completed_at": None},
            # ...a reason it must carry...
            {"empty_reason": None},
            # ...and only one of the two the vocabulary allows.
            {"empty_reason": "NOBODY_HOME"},
        ):
            with pytest.raises(sqlite3.IntegrityError):
                insert_batch(**arguments)
            connection.rollback()

        batch_id = insert_batch()
        connection.commit()
        assert connection.execute(
            "SELECT id,status,empty_reason FROM notification_delivery_batches"
        ).fetchall() == [
            (batch_id, "NO_ACTIVE_SUBSCRIPTIONS", "NO_ELIGIBLE_SUBSCRIPTIONS")
        ]
    finally:
        connection.close()
