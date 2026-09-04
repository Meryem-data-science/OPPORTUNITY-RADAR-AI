"""Integration coverage for migration 0023: the shape of a tracked candidature.

These tests talk to SQLite and not to the service, because everything asserted
here has to hold whoever is writing — the service, a future phase, a CLI, or
somebody with a sqlite3 prompt. If any of it lived only in Python it would be
a convention rather than an invariant.
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

CREATED_AT = "2026-02-01 09:00:00"


def migrations_below(tmp_path, version):
    """A migrations directory holding every migration before ``version``."""
    directory = tmp_path / f"migrations-below-{version}"
    directory.mkdir()
    for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY):
        if migration.version < version:
            (directory / migration.path.name).write_text(
                migration.path.read_text(encoding="utf-8"), encoding="utf-8"
            )
    return directory


def opportunity(connection, url="https://boards.example.invalid/jobs/1"):
    """One persisted opportunity, shaped exactly like a collected one."""
    row = connection.execute(
        """INSERT INTO opportunities
           (canonical_title, organization, location, discovered_at, first_seen_at,
            last_seen_at, source_url, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'visible') RETURNING id""",
        ("Data Engineer", "Example Org", "Paris", CREATED_AT, CREATED_AT, CREATED_AT, url),
    ).fetchone()
    # Committed so the domain, which opens its own BEGIN IMMEDIATE, is never
    # handed a connection that is already inside a transaction.
    connection.commit()
    return int(row[0])


def tracking_fixture(tmp_path, name="application-tracking.db"):
    """A migrated database with two profiles and one real opportunity."""
    connection = connect_database(tmp_path / name)
    apply_migrations(connection)
    owner = ensure_user_profile(connection, "application-owner@example.invalid")
    other = ensure_user_profile(connection, "application-other@example.invalid")
    return connection, owner.profile.id, other.profile.id, opportunity(connection)


def application(connection, profile_id, opportunity_id, status="SAVED", submitted_at=None):
    row = connection.execute(
        """INSERT INTO applications
           (profile_id, opportunity_id, status, submitted_at, last_status_change,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id""",
        (
            profile_id,
            opportunity_id,
            status,
            submitted_at,
            CREATED_AT,
            CREATED_AT,
            CREATED_AT,
        ),
    ).fetchone()
    return int(row[0])


def event(connection, application_id, event_type, from_status, to_status):
    return connection.execute(
        """INSERT INTO application_events
           (application_id, event_type, from_status, to_status, actor_type, occurred_at)
           VALUES (?, ?, ?, ?, 'USER', ?) RETURNING id""",
        (application_id, event_type, from_status, to_status, CREATED_AT),
    ).fetchone()[0]


def test_migration_0023_creates_both_tables_and_their_guards(tmp_path):
    connection, _, _, _ = tracking_fixture(tmp_path)
    try:
        objects = {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT type, name FROM sqlite_master WHERE type IN ('table','trigger','index')"
            )
        }
        assert ("table", "applications") in objects
        assert ("table", "application_events") in objects
        for trigger in (
            "application_events_are_append_only_on_update",
            "application_events_are_append_only_on_delete",
            "application_events_agree_with_the_application",
        ):
            assert ("trigger", trigger) in objects
        for index in (
            "idx_applications_profile_status",
            "idx_applications_profile_last_status_change",
            "idx_applications_opportunity",
            "idx_applications_profile_followup",
            "idx_application_events_application",
        ):
            assert ("index", index) in objects
        applied = {
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
        assert "0023" in applied
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_a_database_below_0023_has_neither_table(tmp_path):
    connection = connect_database(tmp_path / "below.db")
    try:
        apply_migrations(connection, migrations_below(tmp_path, "0023"))
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "applications" not in names and "application_events" not in names
    finally:
        connection.close()


def test_the_whole_official_status_vocabulary_is_accepted(tmp_path):
    connection, profile_id, _, _ = tracking_fixture(tmp_path)
    try:
        for index, status in enumerate(
            (
                "DISCOVERED",
                "SAVED",
                "PREPARING",
                "READY",
                "SUBMITTED",
                "CONFIRMED",
                "ASSESSMENT",
                "INTERVIEW",
                "REJECTED",
                "OFFER",
                "WITHDRAWN",
            )
        ):
            submitted = None if status in {
                "DISCOVERED",
                "SAVED",
                "PREPARING",
                "READY",
                "WITHDRAWN",
            } else CREATED_AT
            new_opportunity = opportunity(
                connection, f"https://boards.example.invalid/jobs/v{index}"
            )
            application(connection, profile_id, new_opportunity, status, submitted)
    finally:
        connection.close()


def test_an_invented_status_is_refused(tmp_path):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            application(connection, profile_id, opportunity_id, "APPLIED")
    finally:
        connection.close()


@pytest.mark.parametrize(
    "status, submitted_at",
    [
        ("SAVED", CREATED_AT),
        ("PREPARING", CREATED_AT),
        ("READY", CREATED_AT),
        ("DISCOVERED", CREATED_AT),
        ("SUBMITTED", None),
        ("CONFIRMED", None),
        ("ASSESSMENT", None),
        ("INTERVIEW", None),
        ("REJECTED", None),
        ("OFFER", None),
    ],
)
def test_a_status_that_disagrees_with_the_submission_instant_is_refused(
    tmp_path, status, submitted_at
):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            application(connection, profile_id, opportunity_id, status, submitted_at)
    finally:
        connection.close()


def test_withdrawn_is_the_one_status_that_sits_on_both_sides(tmp_path):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        application(connection, profile_id, opportunity_id, "WITHDRAWN", None)
        second = opportunity(connection, "https://boards.example.invalid/jobs/2")
        application(connection, profile_id, second, "WITHDRAWN", CREATED_AT)
    finally:
        connection.close()


def test_one_candidature_per_profile_and_opportunity(tmp_path):
    connection, profile_id, other_profile_id, opportunity_id = tracking_fixture(tmp_path)
    try:
        application(connection, profile_id, opportunity_id)
        with pytest.raises(sqlite3.IntegrityError):
            application(connection, profile_id, opportunity_id, "PREPARING")
        # The constraint is per profile: another profile tracking the same
        # opportunity is a different candidature, not a duplicate.
        application(connection, other_profile_id, opportunity_id)
    finally:
        connection.close()


def test_both_foreign_keys_are_real(tmp_path):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            application(connection, 9999, opportunity_id)
        with pytest.raises(sqlite3.IntegrityError):
            application(connection, profile_id, 9999)
        with pytest.raises(sqlite3.IntegrityError):
            event(connection, 9999, "APPLICATION_CREATED", None, "SAVED")
    finally:
        connection.close()


def test_an_opportunity_somebody_applied_to_cannot_be_deleted(tmp_path):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        application(connection, profile_id, opportunity_id)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM opportunities WHERE id = ?", (opportunity_id,)
            )
    finally:
        connection.close()


def test_application_events_cannot_be_updated_or_deleted(tmp_path):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        application_id = application(connection, profile_id, opportunity_id)
        event_id = event(connection, application_id, "APPLICATION_CREATED", None, "SAVED")
        for statement, parameters in (
            (
                "UPDATE application_events SET to_status = 'OFFER' WHERE id = ?",
                (event_id,),
            ),
            ("UPDATE application_events SET occurred_at = ?", ("2020-01-01 00:00:00",)),
            ("DELETE FROM application_events WHERE id = ?", (event_id,)),
            ("DELETE FROM application_events", ()),
        ):
            with pytest.raises(sqlite3.IntegrityError) as refusal:
                connection.execute(statement, parameters)
            assert "append-only" in str(refusal.value)
        stored = connection.execute(
            "SELECT event_type, from_status, to_status, occurred_at"
            " FROM application_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        assert stored == ("APPLICATION_CREATED", None, "SAVED", CREATED_AT)
    finally:
        connection.close()


def test_a_candidature_with_a_history_cannot_be_deleted(tmp_path):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        application_id = application(connection, profile_id, opportunity_id)
        event(connection, application_id, "APPLICATION_CREATED", None, "SAVED")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM applications WHERE id = ?", (application_id,)
            )
    finally:
        connection.close()


def test_an_event_cannot_describe_a_state_the_application_is_not_in(tmp_path):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        application_id = application(connection, profile_id, opportunity_id)
        # The application is SAVED, so an event claiming it became PREPARING
        # is refused until the row itself says PREPARING. That is what forces
        # the state and its history into one transaction, in that order.
        with pytest.raises(sqlite3.IntegrityError) as refusal:
            event(connection, application_id, "STATUS_CHANGED", "SAVED", "PREPARING")
        assert "current application state" in str(refusal.value)
        connection.execute(
            "UPDATE applications SET status = 'PREPARING' WHERE id = ?",
            (application_id,),
        )
        event(connection, application_id, "STATUS_CHANGED", "SAVED", "PREPARING")
    finally:
        connection.close()


@pytest.mark.parametrize(
    "event_type, from_status, to_status",
    [
        ("APPLICATION_CREATED", "SAVED", "SAVED"),
        ("APPLICATION_CREATED", None, None),
        ("STATUS_CHANGED", None, "SAVED"),
        ("STATUS_CHANGED", "SAVED", None),
        ("STATUS_CHANGED", "SAVED", "SAVED"),
        ("TRACKING_UPDATED", None, "SAVED"),
        ("TRACKING_UPDATED", "SAVED", None),
        ("CV_GENERATED", None, "SAVED"),
        ("GMAIL_INTERVIEW_DETECTED", "SAVED", "INTERVIEW"),
    ],
)
def test_an_event_has_to_mean_what_its_type_says(
    tmp_path, event_type, from_status, to_status
):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        application_id = application(connection, profile_id, opportunity_id)
        with pytest.raises(sqlite3.IntegrityError):
            event(connection, application_id, event_type, from_status, to_status)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "column, value",
    [
        ("followup_date", "2026-13-01"),
        ("followup_date", "2026-02-30"),
        ("followup_date", "01/02/2026"),
        ("followup_date", "2026-2-1"),
        ("notes", ""),
        ("notes", "  padded  "),
        ("notes", "n" * 4001),
        ("next_action", ""),
        ("next_action", "a" * 501),
        ("last_status_change", "not-a-timestamp"),
    ],
)
def test_tracking_columns_refuse_values_the_domain_never_writes(
    tmp_path, column, value
):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        application_id = application(connection, profile_id, opportunity_id)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"UPDATE applications SET {column} = ? WHERE id = ?",
                (value, application_id),
            )
    finally:
        connection.close()


def test_a_submitted_candidature_refuses_an_impossible_submission_instant(tmp_path):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        application_id = application(
            connection, profile_id, opportunity_id, "SUBMITTED", CREATED_AT
        )
        for impossible in ("2026-02-30 09:00:00", "yesterday", "2026-02-01"):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE applications SET submitted_at = ? WHERE id = ?",
                    (impossible, application_id),
                )
    finally:
        connection.close()
