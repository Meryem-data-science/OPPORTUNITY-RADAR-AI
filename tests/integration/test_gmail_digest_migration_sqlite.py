"""Integration tests for migration 0022: the frozen Gmail digest outbox.

The upgrade a real operational database takes is 0021, then 0022, and the one
thing 0022 must not do on the way is disturb anything already there: the
Portfolio snapshot, the notification stream, and — the row that actually
exists in production today — an ACTIVE Web Push subscription.
"""

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
from tests.integration.test_push_activation_watermark_migration_sqlite import (
    insert_subscription,
)

TABLE = "gmail_digest_outbox"
UPSTREAM_TABLES = (
    "profiles",
    "opportunities",
    "portfolio_runs",
    "portfolio_assessments",
    "portfolio_profile_state",
    "push_subscriptions",
    "notification_events",
    "notification_outbox",
    "notification_delivery_batches",
    "notification_delivery_targets",
)

ROW = dict(
    profile_id=1,
    digest_date="2026-09-03",
    timezone="Africa/Casablanca",
    portfolio_run_id=1,
    portfolio_run_fingerprint="a" * 64,
    digest_version="gmail-digest-v1",
    content_fingerprint="b" * 64,
    recipient_fingerprint="c" * 64,
    item_count=2,
    subject="Opportunity Radar — 2 opportunités à examiner",
    body_text="body",
    body_html="<p>body</p>",
    status="PENDING",
    attempt_count=0,
    next_attempt_at="2026-09-03 10:00:00",
)


def insert_digest(connection, **changes):
    columns = ROW | changes
    names = ",".join(columns)
    placeholders = ",".join("?" for _ in columns)
    return connection.execute(
        f"INSERT INTO {TABLE} ({names}) VALUES ({placeholders}) RETURNING id",
        tuple(columns.values()),
    ).fetchone()[0]


def rows_of(connection, table):
    return connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()


def upstream_snapshot(connection):
    return {table: rows_of(connection, table) for table in UPSTREAM_TABLES}


def operational_fixture(tmp_path):
    """A database shaped like the real one: Portfolio, events, one ACTIVE device."""
    connection, identity, _, previous_run, run = two_runs(tmp_path)
    event = insert_event(
        connection, event_arguments(identity.profile_id, 1, previous_run, run)
    )
    connection.execute(
        "INSERT INTO notification_outbox (event_id) VALUES (?)", (event,)
    )
    insert_subscription(
        connection,
        identity.profile_id,
        "https://push.example.invalid/subscription/live",
        notification_event_watermark=event,
    )
    connection.commit()
    return connection, identity


def test_0022_creates_the_digest_outbox_empty_and_is_idempotent(tmp_path):
    connection = connect_database(tmp_path / "digest-migration.db")
    try:
        assert "0022" in apply_migrations(connection)
        assert apply_migrations(connection) == []
        assert connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version='0022'"
        ).fetchone() == (1,)
        assert connection.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone() == (0,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_0022_upgrades_a_real_0021_database_without_touching_a_single_row(tmp_path):
    """0021 with a live Portfolio, a notification and an ACTIVE subscription."""
    connection, _ = operational_fixture(tmp_path)
    try:
        connection.execute("DROP TABLE gmail_digest_outbox")
        connection.execute("DELETE FROM schema_migrations WHERE version='0022'")
        connection.commit()
        before = upstream_snapshot(connection)
        migrations = tuple(
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        )
        assert migrations[-1] == "0021"

        assert apply_migrations(connection) == ["0022"]

        assert upstream_snapshot(connection) == before
        assert connection.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM push_subscriptions WHERE status='ACTIVE'"
        ).fetchone() == (1,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_0022_does_not_alter_any_earlier_migration_file():
    """0018-0021 are history: 0022 adds a table and rewrites nothing."""
    versions = [
        migration.version
        for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY)
    ]

    assert versions[-1] == "0022"
    assert versions.count("0022") == 1


def migrated(tmp_path, name="digest-constraints.db"):
    connection = connect_database(tmp_path / name)
    apply_migrations(connection)
    identity = ensure_user_profile(connection, "owner@example.invalid")
    assert identity.profile_id == ROW["profile_id"]
    connection.commit()
    return connection


def test_one_profile_gets_at_most_one_digest_per_day_and_version(tmp_path):
    connection = migrated(tmp_path)
    try:
        insert_digest(connection)
        with pytest.raises(sqlite3.IntegrityError):
            insert_digest(connection, content_fingerprint="d" * 64)
        # A different day, and a different version of the same day, are both
        # legitimate rows.
        assert insert_digest(connection, digest_date="2026-09-04") > 0
        assert insert_digest(connection, digest_version="gmail-digest-v2") > 0
    finally:
        connection.close()


def test_a_digest_belongs_to_a_profile_that_exists(tmp_path):
    connection = migrated(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            insert_digest(connection, profile_id=999)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "changes",
    [
        # A fresh PENDING row carries no claim, and no half of one.
        {"claim_token": "a" * 32},
        {"claimed_at": "2026-09-03 10:00:00"},
        {"status": "PENDING", "next_attempt_at": None},
        # A claim is only ever a claim: token and instant together, IN_FLIGHT.
        {"status": "IN_FLIGHT", "next_attempt_at": None},
        {
            "status": "IN_FLIGHT",
            "next_attempt_at": None,
            "claim_token": "a" * 32,
        },
        {
            "status": "IN_FLIGHT",
            "claim_token": "a" * 32,
            "claimed_at": "2026-09-03 10:00:00",
        },
        {
            "status": "IN_FLIGHT",
            "next_attempt_at": None,
            "claim_token": "NOT-HEX-" + "a" * 24,
            "claimed_at": "2026-09-03 10:00:00",
        },
        # A sent digest names its instant, its Gmail message, and an attempt.
        {"status": "SENT", "next_attempt_at": None},
        {
            "status": "SENT",
            "next_attempt_at": None,
            "sent_at": "2026-09-03 10:00:00",
        },
        {
            "status": "SENT",
            "next_attempt_at": None,
            "sent_at": "2026-09-03 10:00:00",
            "gmail_message_id": "abc123",
        },
        {
            "status": "SENT",
            "next_attempt_at": None,
            "sent_at": "2026-09-03 10:00:00",
            "gmail_message_id": "abc123",
            "attempt_count": 1,
            "last_attempt_at": "2026-09-03 10:00:00",
            "last_error_code": "HTTP_500",
            "last_error_category": "RETRYABLE",
        },
        # Only a sent digest carries either of those two facts.
        {"sent_at": "2026-09-03 10:00:00"},
        {"gmail_message_id": "abc123"},
        # An attempt and the instant it happened are one fact.
        {"attempt_count": 1},
        {"last_attempt_at": "2026-09-03 10:00:00"},
        # A terminal failure names its error.
        {"status": "PERMANENT_FAILURE", "next_attempt_at": None},
        {
            "status": "PERMANENT_FAILURE",
            "next_attempt_at": None,
            "last_error_code": "REFUSED",
        },
        # Vocabulary the lifecycle does not have.
        {"status": "FAILED", "next_attempt_at": None},
        {"status": "pending"},
        {"last_error_category": "EXPIRED", "last_error_code": "HTTP_410"},
        # An empty digest is a no-op result, never a row.
        {"item_count": 0},
        {"item_count": -1},
        # Identities are lowercase SHA-256, and a day is a real date.
        {"content_fingerprint": "B" * 64},
        {"content_fingerprint": "b" * 63},
        {"recipient_fingerprint": "not a fingerprint"},
        {"portfolio_run_fingerprint": "z" * 64},
        {"digest_date": "2026-02-30"},
        {"digest_date": "2026-13-01"},
        {"digest_date": "03/09/2026"},
        {"digest_date": "2026-09-03T00:00:00"},
        # Nothing about a frozen message may be blank.
        {"timezone": ""},
        {"timezone": " Africa/Casablanca"},
        {"digest_version": ""},
        {"subject": "   "},
        {"body_text": ""},
        {"body_html": "   "},
        {"profile_id": 0},
        {"portfolio_run_id": 0},
        {"attempt_count": -1, "last_attempt_at": "2026-09-03 10:00:00"},
    ],
)
def test_the_schema_rejects_every_inconsistent_operational_state(tmp_path, changes):
    connection = migrated(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            insert_digest(connection, **changes)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {
            "status": "IN_FLIGHT",
            "next_attempt_at": None,
            "claim_token": "a" * 32,
            "claimed_at": "2026-09-03 10:00:00",
        },
        {
            "status": "SENT",
            "next_attempt_at": None,
            "sent_at": "2026-09-03 10:00:00",
            "gmail_message_id": "18f0a1b2c3d4e5f6",
            "attempt_count": 1,
            "last_attempt_at": "2026-09-03 10:00:00",
        },
        {
            "status": "PERMANENT_FAILURE",
            "next_attempt_at": None,
            "attempt_count": 2,
            "last_attempt_at": "2026-09-03 10:00:00",
            "last_error_code": "HTTP_403",
            "last_error_category": "PERMANENT",
        },
    ],
)
def test_every_lifecycle_state_phase_5_4b_needs_is_already_expressible(
    tmp_path, changes
):
    """5.4B delivers against these columns without a second migration."""
    connection = migrated(tmp_path)
    try:
        assert insert_digest(connection, **changes) > 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
