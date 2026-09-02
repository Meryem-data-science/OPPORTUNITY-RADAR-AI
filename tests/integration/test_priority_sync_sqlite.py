from datetime import date
import sqlite3

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.priority import PriorityReadinessStatus, PrioritySyncError, sync_priority
from tests.integration.test_priority_persistence_sqlite import DAY, priority_fixture


def test_priority_sync_is_atomic_append_only_and_idempotent(tmp_path):
    connection, identity, _, arguments = priority_fixture(tmp_path)

    first = sync_priority(connection, identity.profile_id, date(2026, 9, 2))
    state_before = connection.execute("SELECT * FROM priority_profile_state").fetchone()
    counts_before = (
        connection.execute("SELECT COUNT(*) FROM priority_runs").fetchone()[0],
        connection.execute("SELECT COUNT(*) FROM priority_assessments").fetchone()[0],
    )
    second = sync_priority(connection, identity.profile_id, date(2026, 9, 2))

    assert identity.profile_id != identity.user_id
    assert first.status is PriorityReadinessStatus.READY
    assert first.created is True and first.state_changed is True
    assert second.created is False and second.state_changed is False
    assert (
        first.run_id == second.run_id
        and first.run_fingerprint == second.run_fingerprint
    )
    assert first.matching_run_id == arguments["matching_run_id"]
    persisted_ids = connection.execute(
        "SELECT profile_id,user_id FROM priority_runs"
    ).fetchone()
    assert persisted_ids == (identity.profile_id, identity.user_id)
    assert counts_before == (1, 2)
    assert counts_before == (
        connection.execute("SELECT COUNT(*) FROM priority_runs").fetchone()[0],
        connection.execute("SELECT COUNT(*) FROM priority_assessments").fetchone()[0],
    )
    assert (
        connection.execute("SELECT * FROM priority_profile_state").fetchone()
        == state_before
    )


def test_incomplete_keeps_previous_ready_snapshot_unchanged(tmp_path):
    connection, identity, opportunity_ids, _ = priority_fixture(tmp_path)
    ready = sync_priority(connection, identity.profile_id, DAY)
    before = (
        connection.execute("SELECT COUNT(*) FROM priority_runs").fetchone()[0],
        connection.execute("SELECT COUNT(*) FROM priority_assessments").fetchone()[0],
        connection.execute("SELECT * FROM priority_profile_state").fetchone(),
    )
    connection.execute(
        "DELETE FROM opportunity_eligibilities WHERE opportunity_id=?",
        (opportunity_ids[-1],),
    )
    connection.commit()
    incomplete = sync_priority(connection, identity.profile_id, DAY)
    assert incomplete.status is PriorityReadinessStatus.INCOMPLETE
    assert (incomplete.persisted, incomplete.created, incomplete.state_changed) == (
        False,
        None,
        False,
    )
    assert incomplete.run_id is None and incomplete.run_fingerprint is None
    after = (
        connection.execute("SELECT COUNT(*) FROM priority_runs").fetchone()[0],
        connection.execute("SELECT COUNT(*) FROM priority_assessments").fetchone()[0],
        connection.execute("SELECT * FROM priority_profile_state").fetchone(),
    )
    assert after == before and after[2][1] == ready.run_id


def test_evaluation_date_change_appends_and_old_snapshot_can_be_reused(tmp_path):
    connection, identity, _, _ = priority_fixture(tmp_path)
    first = sync_priority(connection, identity.profile_id, DAY)
    old_run = connection.execute(
        "SELECT * FROM priority_runs WHERE id=?", (first.run_id,)
    ).fetchone()
    old_items = connection.execute(
        "SELECT * FROM priority_assessments WHERE run_id=? ORDER BY id", (first.run_id,)
    ).fetchall()
    second = sync_priority(connection, identity.profile_id, date(2026, 9, 3))
    reused = sync_priority(connection, identity.profile_id, DAY)
    assert second.created is True and second.state_changed is True
    assert (
        second.run_id != first.run_id
        and second.run_fingerprint != first.run_fingerprint
    )
    assert (reused.run_id, reused.run_fingerprint) == (
        first.run_id,
        first.run_fingerprint,
    )
    assert reused.created is False and reused.state_changed is True
    assert connection.execute("SELECT COUNT(*) FROM priority_runs").fetchone()[0] == 2
    assert (
        connection.execute("SELECT COUNT(*) FROM priority_assessments").fetchone()[0]
        == 4
    )
    assert (
        connection.execute(
            "SELECT * FROM priority_runs WHERE id=?", (first.run_id,)
        ).fetchone()
        == old_run
    )
    assert (
        connection.execute(
            "SELECT * FROM priority_assessments WHERE run_id=? ORDER BY id",
            (first.run_id,),
        ).fetchall()
        == old_items
    )


def test_legitimate_eligibility_snapshot_change_creates_new_run(tmp_path):
    connection, identity, opportunity_ids, _ = priority_fixture(tmp_path)
    first = sync_priority(connection, identity.profile_id, DAY)
    connection.execute(
        "UPDATE opportunity_eligibilities SET input_fingerprint=? WHERE opportunity_id=?",
        ("f" * 64, opportunity_ids[0]),
    )
    connection.commit()
    second = sync_priority(connection, identity.profile_id, DAY)
    assert second.created is True and second.run_id != first.run_id


def test_middle_assessment_failure_rolls_back_run_items_and_state(tmp_path):
    connection, identity, opportunity_ids, _ = priority_fixture(tmp_path)
    ready = sync_priority(connection, identity.profile_id, DAY)
    before = (
        connection.execute("SELECT COUNT(*) FROM priority_runs").fetchone()[0],
        connection.execute("SELECT COUNT(*) FROM priority_assessments").fetchone()[0],
        connection.execute("SELECT * FROM priority_profile_state").fetchone(),
    )
    connection.execute(f"""CREATE TEMP TRIGGER fail_second BEFORE INSERT ON priority_assessments
        WHEN NEW.opportunity_id={opportunity_ids[1]} BEGIN
        SELECT RAISE(ABORT, 'forced test failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="forced test failure"):
        sync_priority(connection, identity.profile_id, date(2026, 9, 3))
    after = (
        connection.execute("SELECT COUNT(*) FROM priority_runs").fetchone()[0],
        connection.execute("SELECT COUNT(*) FROM priority_assessments").fetchone()[0],
        connection.execute("SELECT * FROM priority_profile_state").fetchone(),
    )
    assert after == before and after[2][1] == ready.run_id
    assert connection.in_transaction is False


def test_sync_refuses_schema_without_0016_and_does_not_mutate(tmp_path):
    connection = connect_database(tmp_path / "old-schema.db")
    apply_migrations(connection)
    connection.execute("DELETE FROM schema_migrations WHERE version='0016'")
    connection.commit()
    before = tuple(connection.iterdump())
    with pytest.raises(PrioritySyncError, match="0016"):
        sync_priority(connection, 1, DAY)
    assert tuple(connection.iterdump()) == before
