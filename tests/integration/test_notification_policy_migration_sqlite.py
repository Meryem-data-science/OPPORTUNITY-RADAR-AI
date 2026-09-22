"""Integration tests for migration 0019: notification policy state and outbox."""

import sqlite3

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import (
    DEFAULT_MIGRATIONS_DIRECTORY,
    apply_migrations,
    discover_migrations,
)
from services.digital_twin.repository import ensure_user_profile
from services.notifications import NOTIFICATION_POLICY_VERSION
from tests.integration.test_portfolio_persistence_sqlite import (
    persist,
    portfolio_fixture,
)

TABLES = ("notification_policy_state", "notification_events", "notification_outbox")


def two_runs(tmp_path):
    """A migrated database holding one stored Portfolio run and a clone of it.

    The clone is a second, distinct run row for the same profile, which is what
    an event needs: an event always spans two different Portfolio runs.
    """
    connection, identity, opportunity_ids, arguments = portfolio_fixture(tmp_path)
    stored = persist(connection, arguments)
    second = stored.run_id + 1
    connection.execute(
        """INSERT INTO portfolio_runs SELECT ?,profile_id,priority_run_id,matching_run_id,
        persistence_version,input_assembly_version,portfolio_engine_version,
        portfolio_rules_version,priority_run_fingerprint,matching_run_fingerprint,?,
        assessment_count,included_count,excluded_count,safe_count,target_count,
        ambitious_count,run_payload_json,created_at FROM portfolio_runs WHERE id=?""",
        (second, "c" * 64, stored.run_id),
    )
    connection.commit()
    return connection, identity, opportunity_ids, stored.run_id, second


def event_arguments(profile_id, opportunity_id, previous_run_id, run_id, **changes):
    return {
        "profile_id": profile_id,
        "event_type": "NEW_ACTIONABLE_OPPORTUNITY",
        "opportunity_id": opportunity_id,
        "previous_portfolio_run_id": previous_run_id,
        "portfolio_run_id": run_id,
        "policy_version": NOTIFICATION_POLICY_VERSION,
        "event_fingerprint": "a" * 64,
        "payload_json": '{"event_type":"NEW_ACTIONABLE_OPPORTUNITY"}',
    } | changes


def insert_event(connection, arguments):
    columns = ",".join(arguments)
    placeholders = ",".join("?" for _ in arguments)
    return connection.execute(
        f"INSERT INTO notification_events ({columns}) VALUES ({placeholders})"
        " RETURNING id",
        tuple(arguments.values()),
    ).fetchone()[0]


def counts(connection):
    return tuple(
        connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in TABLES
    )


def test_0019_creates_the_three_notification_tables_empty_and_is_idempotent(tmp_path):
    connection = connect_database(tmp_path / "notification-migration.db")
    try:
        assert "0019" in apply_migrations(connection)
        assert apply_migrations(connection) == []
        assert connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version='0019'"
        ).fetchone() == (1,)
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert set(TABLES) <= names
        assert counts(connection) == (0, 0, 0)
    finally:
        connection.close()


def test_0019_upgrades_an_existing_database_without_touching_its_data(tmp_path):
    """The upgrade path a real operational database takes: 0018, then 0019."""
    connection = connect_database(tmp_path / "upgrade.db")
    try:
        directory = tmp_path / "migrations-before-0019"
        directory.mkdir()
        for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY):
            if migration.version < "0019":
                (directory / migration.path.name).write_text(
                    migration.path.read_text(encoding="utf-8"), encoding="utf-8"
                )
        applied = apply_migrations(connection, directory)
        assert "0019" not in applied and applied[-1] == "0018"
        owner = ensure_user_profile(connection, "notify-upgrade@example.invalid")
        before = connection.execute(
            "SELECT id, user_id FROM profiles ORDER BY id"
        ).fetchall()

        assert apply_migrations(connection) == [
            "0019", "0020", "0021", "0022", "0023", "0024", "0025", "0026", "0027", "0028",
        ]

        assert (
            connection.execute(
                "SELECT id, user_id FROM profiles ORDER BY id"
            ).fetchall()
            == before
        )
        assert owner.profile_id > 0
        assert counts(connection) == (0, 0, 0)
    finally:
        connection.close()


def test_state_is_one_row_per_profile_pinned_to_that_profiles_portfolio_runs(tmp_path):
    connection, identity, _, first, second = two_runs(tmp_path)
    state = (
        identity.profile_id,
        second,
        second,
        second,
        NOTIFICATION_POLICY_VERSION,
    )
    insert = """INSERT INTO notification_policy_state
        (profile_id,baseline_portfolio_run_id,last_processed_portfolio_run_id,
         highest_seen_portfolio_run_id,policy_version) VALUES (?,?,?,?,?)"""
    connection.execute(insert, state)
    connection.commit()

    for statement, parameters in (
        (insert, state),
        (
            "UPDATE notification_policy_state SET last_processed_portfolio_run_id=?",
            (9999,),
        ),
        (
            "UPDATE notification_policy_state SET highest_seen_portfolio_run_id=?",
            (9999,),
        ),
        ("UPDATE notification_policy_state SET policy_version=?", ("   ",)),
        ("UPDATE notification_policy_state SET baseline_portfolio_run_id=?", (0,)),
        # The high-water mark can never sit below the pointers it bounds, so it
        # cannot be walked back to an earlier run even though that run exists.
        (
            "UPDATE notification_policy_state SET highest_seen_portfolio_run_id=?",
            (first,),
        ),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement, parameters)
        connection.rollback()

    assert connection.execute(
        """SELECT profile_id,baseline_portfolio_run_id,last_processed_portfolio_run_id,
        highest_seen_portfolio_run_id,policy_version FROM notification_policy_state"""
    ).fetchall() == [state]


def test_events_reject_unknown_types_bad_fingerprints_and_single_run_spans(tmp_path):
    connection, identity, opportunity_ids, first, second = two_runs(tmp_path)
    base = event_arguments(identity.profile_id, opportunity_ids[0], first, second)

    for arguments in (
        base | {"event_type": "SPONTANEOUS"},
        base | {"event_type": ""},
        base | {"event_fingerprint": "z" * 64},
        base | {"event_fingerprint": "a" * 63},
        base | {"payload_json": "   "},
        base | {"payload_json": ' {"a":1}'},
        base | {"policy_version": ""},
        base | {"portfolio_run_id": first},
        base | {"opportunity_id": 9999},
    ):
        with pytest.raises(sqlite3.IntegrityError):
            insert_event(connection, arguments)
        connection.rollback()

    assert counts(connection) == (0, 0, 0)


def test_events_are_unique_by_fingerprint_and_the_outbox_is_one_row_per_event(
    tmp_path,
):
    connection, identity, opportunity_ids, first, second = two_runs(tmp_path)
    arguments = event_arguments(identity.profile_id, opportunity_ids[0], first, second)
    event_id = insert_event(connection, arguments)
    connection.execute(
        "INSERT INTO notification_outbox (event_id) VALUES (?)", (event_id,)
    )
    connection.commit()

    for statement, parameters in (
        (None, arguments | {"opportunity_id": opportunity_ids[1]}),
        ("INSERT INTO notification_outbox (event_id) VALUES (?)", (event_id,)),
        ("INSERT INTO notification_outbox (event_id) VALUES (?)", (9999,)),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            if statement is None:
                insert_event(connection, parameters)
            else:
                connection.execute(statement, parameters)
        connection.rollback()

    assert counts(connection) == (0, 1, 1)


def test_deleting_an_event_removes_its_outbox_row_but_portfolio_runs_are_restricted(
    tmp_path,
):
    connection, identity, opportunity_ids, first, second = two_runs(tmp_path)
    event_id = insert_event(
        connection,
        event_arguments(identity.profile_id, opportunity_ids[0], first, second),
    )
    connection.execute(
        "INSERT INTO notification_outbox (event_id) VALUES (?)", (event_id,)
    )
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM portfolio_runs WHERE id=?", (second,))
    connection.rollback()
    connection.execute("DELETE FROM notification_events WHERE id=?", (event_id,))
    connection.commit()

    assert counts(connection) == (0, 0, 0)


def test_only_one_new_actionable_event_per_profile_and_opportunity(tmp_path):
    """The partial unique index is the database guard for the policy v1 rule.

    It constrains NEW_ACTIONABLE_OPPORTUNITY alone: an opportunity can escalate
    as often as it legitimately climbs, but it is new exactly once.
    """
    connection, identity, opportunity_ids, first, second = two_runs(tmp_path)
    partial = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        ("idx_notification_events_new_actionable_once",),
    ).fetchone()
    assert (
        partial is not None
        and "WHERE event_type = 'NEW_ACTIONABLE_OPPORTUNITY'" in (partial[0])
    )

    base = event_arguments(identity.profile_id, opportunity_ids[0], first, second)
    insert_event(connection, base)
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError):
        insert_event(connection, base | {"event_fingerprint": "b" * 64})
    connection.rollback()

    # A different opportunity, and an escalation of the same one, both fit.
    insert_event(
        connection,
        base | {"opportunity_id": opportunity_ids[1], "event_fingerprint": "b" * 64},
    )
    insert_event(
        connection,
        base | {"event_type": "ATTENTION_ESCALATED", "event_fingerprint": "c" * 64},
    )
    insert_event(
        connection,
        base | {"event_type": "ATTENTION_ESCALATED", "event_fingerprint": "d" * 64},
    )
    connection.commit()

    assert connection.execute(
        "SELECT event_type,COUNT(*) FROM notification_events GROUP BY event_type"
        " ORDER BY event_type"
    ).fetchall() == [("ATTENTION_ESCALATED", 2), ("NEW_ACTIONABLE_OPPORTUNITY", 2)]
