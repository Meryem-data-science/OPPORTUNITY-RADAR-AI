from datetime import date
import hashlib

import pytest

from services.collector.database.connection import connect_readonly_database
from services.priority import (
    PriorityProfileReadStatus,
    PriorityReadError,
    audit_current_priority,
    audit_priority_run,
    list_priority_runs,
    read_current_priority,
    read_priority_run,
    sync_priority,
)
from tests.integration.test_priority_persistence_sqlite import priority_fixture


DAY = date(2026, 9, 2)


def test_not_synced_profile_has_empty_history(tmp_path):
    connection, identity, _, _ = priority_fixture(tmp_path)
    result = read_current_priority(connection, identity.profile_id)
    assert result.status is PriorityProfileReadStatus.NOT_SYNCED
    assert (result.history_count, result.current_run_id, result.current_run) == (
        0,
        None,
        None,
    )
    assert list_priority_runs(connection, identity.profile_id) == ()


def test_ready_read_model_is_typed_ordered_and_recursively_frozen(tmp_path):
    connection, identity, _, _ = priority_fixture(tmp_path)
    synced = sync_priority(connection, identity.profile_id, DAY)
    result = read_current_priority(connection, identity.profile_id)
    assert result.status is PriorityProfileReadStatus.READY
    assert result.current_run_id == synced.run_id
    assert result.current_run.evaluation_date == DAY
    assert [item.opportunity_id for item in result.current_run.assessments] == sorted(
        item.opportunity_id for item in result.current_run.assessments
    )
    with pytest.raises(TypeError):
        result.current_run.run_payload["profile_id"] = 99
    with pytest.raises(TypeError):
        result.current_run.assessments[0].assessment_payload["result"][
            "priority_score"
        ] = 0
    assert isinstance(result.current_run.run_payload["assessments"], tuple)


def test_current_pointer_not_maximum_run_id(tmp_path):
    connection, identity, _, _ = priority_fixture(tmp_path)
    first = sync_priority(connection, identity.profile_id, DAY)
    second = sync_priority(connection, identity.profile_id, date(2026, 9, 3))
    connection.execute(
        "UPDATE priority_profile_state SET current_run_id=? WHERE profile_id=?",
        (first.run_id, identity.profile_id),
    )
    connection.commit()
    assert (
        read_current_priority(connection, identity.profile_id).current_run_id
        == first.run_id
    )
    history = list_priority_runs(connection, identity.profile_id)
    assert [item.run_id for item in history] == [second.run_id, first.run_id]
    assert [item.is_current for item in history] == [False, True]


@pytest.mark.parametrize("value", [True, 0, -1, "1", None])
def test_invalid_ids_are_rejected(tmp_path, value):
    connection, identity, _, _ = priority_fixture(tmp_path)
    with pytest.raises(PriorityReadError, match="positive integer"):
        read_priority_run(connection, value)
    with pytest.raises(PriorityReadError, match="positive integer"):
        read_current_priority(connection, value)
    assert identity.profile_id > 0


@pytest.mark.parametrize(
    "table,column",
    [
        ("priority_runs", "run_payload_json"),
        ("priority_assessments", "assessment_payload_json"),
    ],
)
def test_invalid_json_is_a_read_error(tmp_path, table, column):
    connection, identity, _, _ = priority_fixture(tmp_path)
    run = sync_priority(connection, identity.profile_id, DAY)
    where = "id=?" if table == "priority_runs" else "run_id=?"
    connection.execute(
        f"UPDATE {table} SET {column}='not-json' WHERE {where}", (run.run_id,)
    )
    connection.commit()
    with pytest.raises(PriorityReadError, match="invalid .* JSON"):
        read_priority_run(connection, run.run_id)


def test_read_only_calls_leave_database_hash_unchanged(tmp_path):
    connection, identity, _, arguments = priority_fixture(tmp_path)
    run = sync_priority(connection, identity.profile_id, DAY)
    database = tmp_path / "priority-persistence.db"
    connection.close()
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    readonly = connect_readonly_database(database)
    read_current_priority(readonly, identity.profile_id)
    list_priority_runs(readonly, identity.profile_id)
    read_priority_run(readonly, run.run_id)
    audit_priority_run(readonly, run.run_id)
    audit_current_priority(readonly, identity.profile_id)
    readonly.close()
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
