"""Integration tests for migration 0020: delivery batches and their targets."""

import sqlite3

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import (
    DEFAULT_MIGRATIONS_DIRECTORY,
    apply_migrations,
    discover_migrations,
)
from services.digital_twin.repository import ensure_user_profile
from tests.integration.test_notification_policy_migration_sqlite import (
    event_arguments,
    insert_event,
    two_runs,
)

TABLES = ("notification_delivery_batches", "notification_delivery_targets")
ENDPOINT = "https://push.example.invalid/subscription/abc"
P256DH = "BN" + "a" * 85
AUTH = "c" * 22
LATER = "2099-01-01 00:00:00"


def delivery_fixture(tmp_path):
    """A migrated database holding one outbox row and one live subscription."""
    connection, identity, opportunity_ids, first, second = two_runs(tmp_path)
    event_id = insert_event(
        connection,
        event_arguments(identity.profile_id, opportunity_ids[0], first, second),
    )
    outbox_id = connection.execute(
        "INSERT INTO notification_outbox (event_id) VALUES (?) RETURNING id",
        (event_id,),
    ).fetchone()[0]
    subscription_id = connection.execute(
        """INSERT INTO push_subscriptions (profile_id,endpoint,p256dh,auth,status)
        VALUES (?,?,?,?,'ACTIVE') RETURNING id""",
        (identity.profile_id, ENDPOINT, P256DH, AUTH),
    ).fetchone()[0]
    connection.commit()
    return connection, identity, outbox_id, subscription_id


def batch_arguments(outbox_id, **changes):
    return {
        "outbox_id": outbox_id,
        "status": "PENDING",
        "target_count": 1,
        "completed_at": None,
    } | changes


def target_arguments(batch_id, subscription_id, **changes):
    return {
        "batch_id": batch_id,
        "subscription_id": subscription_id,
        "status": "PENDING",
        "attempt_count": 0,
        "next_attempt_at": LATER,
        "last_attempt_at": None,
        "delivered_at": None,
        "last_error_code": None,
        "last_error_category": None,
    } | changes


def insert(connection, table, arguments):
    columns = ",".join(arguments)
    placeholders = ",".join("?" for _ in arguments)
    return connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders}) RETURNING id",
        tuple(arguments.values()),
    ).fetchone()[0]


def counts(connection):
    return tuple(
        connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in TABLES
    )


def test_0020_creates_both_delivery_tables_empty_and_is_idempotent(tmp_path):
    connection = connect_database(tmp_path / "delivery-migration.db")
    try:
        assert "0020" in apply_migrations(connection)
        assert apply_migrations(connection) == []
        assert connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version='0020'"
        ).fetchone() == (1,)
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert set(TABLES) <= names
        assert counts(connection) == (0, 0)
        assert {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(notification_delivery_targets)"
            )
        } == {
            "id",
            "batch_id",
            "subscription_id",
            "status",
            "attempt_count",
            "next_attempt_at",
            "last_attempt_at",
            "delivered_at",
            "last_error_code",
            "last_error_category",
            "created_at",
            "updated_at",
        }
    finally:
        connection.close()


def test_0020_upgrades_a_0019_database_without_touching_any_of_its_data(tmp_path):
    """The upgrade path a real operational database takes: 0019, then 0020."""
    connection = connect_database(tmp_path / "upgrade.db")
    try:
        directory = tmp_path / "migrations-before-0020"
        directory.mkdir()
        for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY):
            if migration.version != "0020":
                (directory / migration.path.name).write_text(
                    migration.path.read_text(encoding="utf-8"), encoding="utf-8"
                )
        applied = apply_migrations(connection, directory)
        assert "0020" not in applied and applied[-1] == "0019"
        identity = ensure_user_profile(connection, "delivery-upgrade@example.invalid")
        profile_id = identity.profile.id
        connection.execute(
            """INSERT INTO push_subscriptions (profile_id,endpoint,p256dh,auth,status)
            VALUES (?,?,?,?,'ACTIVE')""",
            (profile_id, ENDPOINT, P256DH, AUTH),
        )
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
        policy_before = connection.execute(
            "SELECT * FROM notification_policy_state ORDER BY profile_id"
        ).fetchall()

        assert apply_migrations(connection) == ["0020"]

        assert {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in before
        } == before
        assert (
            connection.execute(
                "SELECT * FROM notification_policy_state ORDER BY profile_id"
            ).fetchall()
            == policy_before
        )
        assert counts(connection) == (0, 0)
    finally:
        connection.close()


def test_a_batch_belongs_to_exactly_one_outbox_row_and_states_its_own_terminality(
    tmp_path,
):
    connection, _, outbox_id, _ = delivery_fixture(tmp_path)
    try:
        batch_id = insert(
            connection, "notification_delivery_batches", batch_arguments(outbox_id)
        )
        connection.commit()

        for arguments in (
            # One outbox row, one batch, forever.
            batch_arguments(outbox_id),
            batch_arguments(9999),
            batch_arguments(outbox_id + 1000),
            # A pending batch is neither empty nor complete.
            batch_arguments(outbox_id + 1, target_count=0),
            batch_arguments(outbox_id + 1, completed_at=LATER),
            # A batch without recipients is terminal on the spot, and empty.
            batch_arguments(
                outbox_id + 1, status="NO_ACTIVE_SUBSCRIPTIONS", completed_at=None
            ),
            batch_arguments(
                outbox_id + 1,
                status="NO_ACTIVE_SUBSCRIPTIONS",
                target_count=1,
                completed_at=LATER,
            ),
            # A completed batch had recipients and says when it finished.
            batch_arguments(outbox_id + 1, status="COMPLETED", completed_at=None),
            batch_arguments(outbox_id + 1, status="DONE", completed_at=LATER),
            batch_arguments(outbox_id + 1, target_count=-1),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                insert(connection, "notification_delivery_batches", arguments)
            connection.rollback()

        assert connection.execute(
            "SELECT id,outbox_id,status,target_count,completed_at"
            " FROM notification_delivery_batches"
        ).fetchall() == [(batch_id, outbox_id, "PENDING", 1, None)]
    finally:
        connection.close()


def test_a_target_is_pending_with_a_schedule_or_terminal_without_one(tmp_path):
    connection, _, outbox_id, subscription_id = delivery_fixture(tmp_path)
    try:
        batch_id = insert(
            connection, "notification_delivery_batches", batch_arguments(outbox_id)
        )
        target_id = insert(
            connection,
            "notification_delivery_targets",
            target_arguments(batch_id, subscription_id),
        )
        connection.commit()

        for statement, parameters in (
            # One target per (batch, subscription) — a fan-out never doubles up.
            (None, target_arguments(batch_id, subscription_id)),
            (None, target_arguments(batch_id, 9999)),
            (None, target_arguments(9999, subscription_id)),
            (None, target_arguments(batch_id, subscription_id, status="QUEUED")),
            # Pending needs a schedule; terminal must not carry one.
            (
                "UPDATE notification_delivery_targets SET next_attempt_at=NULL",
                (),
            ),
            (
                "UPDATE notification_delivery_targets SET status='SENT',"
                "attempt_count=1,last_attempt_at=?,delivered_at=?",
                (LATER, LATER),
            ),
            # A delivery instant belongs to SENT alone.
            (
                "UPDATE notification_delivery_targets SET delivered_at=?",
                (LATER,),
            ),
            # An attempt and the moment it happened are one fact.
            (
                "UPDATE notification_delivery_targets SET attempt_count=1",
                (),
            ),
            (
                "UPDATE notification_delivery_targets SET last_attempt_at=?",
                (LATER,),
            ),
            # A failure names its error; an unknown category is not one.
            (
                "UPDATE notification_delivery_targets SET status='FAILED',"
                "attempt_count=1,last_attempt_at=?,next_attempt_at=NULL",
                (LATER,),
            ),
            (
                "UPDATE notification_delivery_targets SET last_error_category='ODD'",
                (),
            ),
            (
                "UPDATE notification_delivery_targets SET last_error_code='   '",
                (),
            ),
            (
                "UPDATE notification_delivery_targets SET attempt_count=-1",
                (),
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                if statement is None:
                    insert(connection, "notification_delivery_targets", parameters)
                else:
                    connection.execute(statement, parameters)
            connection.rollback()

        # The three legitimate terminal shapes all fit.
        for changes in (
            dict(
                status="SENT",
                attempt_count=1,
                next_attempt_at=None,
                last_attempt_at=LATER,
                delivered_at=LATER,
            ),
            dict(
                status="EXPIRED",
                attempt_count=1,
                next_attempt_at=None,
                last_attempt_at=LATER,
                last_error_code="HTTP_410",
                last_error_category="EXPIRED",
            ),
            dict(
                status="FAILED",
                attempt_count=5,
                next_attempt_at=None,
                last_attempt_at=LATER,
                last_error_code="HTTP_503",
                last_error_category="RETRYABLE",
            ),
        ):
            assignments = ",".join(f"{column}=?" for column in changes)
            connection.execute(
                f"UPDATE notification_delivery_targets SET {assignments} WHERE id=?",
                (*changes.values(), target_id),
            )
            connection.rollback()

        assert counts(connection) == (1, 1)
    finally:
        connection.close()


def test_targets_follow_their_batch_and_their_subscription_out_of_existence(tmp_path):
    connection, identity, outbox_id, subscription_id = delivery_fixture(tmp_path)
    try:
        batch_id = insert(
            connection, "notification_delivery_batches", batch_arguments(outbox_id)
        )
        insert(
            connection,
            "notification_delivery_targets",
            target_arguments(batch_id, subscription_id),
        )
        connection.commit()

        connection.execute(
            "DELETE FROM push_subscriptions WHERE id=?", (subscription_id,)
        )
        assert counts(connection) == (1, 0)
        connection.rollback()

        connection.execute(
            "DELETE FROM notification_delivery_batches WHERE id=?", (batch_id,)
        )
        assert counts(connection) == (0, 0)
        connection.rollback()

        # An outbox row still cannot vanish under a live event, but deleting
        # the event cascades all the way through delivery.
        connection.execute(
            "DELETE FROM notification_events WHERE id="
            "(SELECT event_id FROM notification_outbox WHERE id=?)",
            (outbox_id,),
        )
        assert counts(connection) == (0, 0)
        assert (
            connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0]
            == 0
        )
        connection.rollback()
        assert counts(connection) == (1, 1)
    finally:
        connection.close()
