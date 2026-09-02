from dataclasses import replace
from datetime import date
import sqlite3

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.matching import (
    MATCHING_SELECTION_VERSION,
    MatchingBatchResult,
    matching_batch_fingerprint,
    store_matching_batch,
)
from services.collector.qualification.persistence import persist_qualifications
from services.digital_twin.repository import ensure_user_profile
from services.eligibility import ELIGIBILITY_ENGINE_VERSION
from services.priority import (
    PriorityPersistenceError,
    assemble_priority_inputs,
    build_priority_assessment,
    store_priority_batch,
)
from tests.integration.test_priority_dry_run_sqlite import _batch

DAY = date(2026, 9, 2)


def priority_fixture(tmp_path, *, opportunities=2):
    connection = connect_database(tmp_path / "priority-persistence.db")
    apply_migrations(connection)
    connection.execute("INSERT INTO users(email) VALUES ('other@example.invalid')")
    connection.commit()
    identity = ensure_user_profile(connection, "priority-owner@example.invalid")
    opportunity_ids = []
    for number in range(opportunities):
        opportunity_ids.append(
            connection.execute(
                """INSERT INTO opportunities
                (canonical_title,organization,published_at,deadline,discovered_at,
                 first_seen_at,last_seen_at,source_url,status)
                VALUES (?,?,NULL,NULL,'2099-01-01','2099-01-01','2099-01-01',?,'new')
                RETURNING id""",
                (f"Data Engineer {number}", "Org", f"https://example.invalid/{number}"),
            ).fetchone()[0]
        )
    connection.commit()
    persist_qualifications(connection)
    for opportunity_id in opportunity_ids:
        connection.execute(
            """INSERT INTO opportunity_eligibilities
            (user_id,opportunity_id,status,engine_version,input_fingerprint,satisfied_count,
             violated_count,unknown_count,not_applicable_count,not_evaluated_count,
             blocking_unknown_count,evaluated_at) VALUES (?,?,?,?,?,0,0,1,0,0,1,?)""",
            (
                identity.user_id,
                opportunity_id,
                "UNKNOWN",
                ELIGIBILITY_ENGINE_VERSION,
                f"{opportunity_id:x}".zfill(64),
                "2026-09-01",
            ),
        )
    connection.commit()
    items = tuple(
        _batch(identity.profile_id, item).assessments[0] for item in opportunity_ids
    )
    batch = MatchingBatchResult(items, "a" * 64, "b" * 64, len(items))
    batch = replace(batch, batch_fingerprint=matching_batch_fingerprint(batch))
    matching = store_matching_batch(
        connection,
        identity.profile_id,
        batch,
        selection_version=MATCHING_SELECTION_VERSION,
    )
    assembly = assemble_priority_inputs(connection, identity.profile_id, DAY)
    assessments = tuple(
        build_priority_assessment(record.priority_input) for record in assembly.records
    )
    arguments = dict(
        profile_id=identity.profile_id,
        user_id=identity.user_id,
        matching_run_id=matching.run_id,
        matching_run_fingerprint=matching.run_fingerprint,
        evaluation_date=DAY,
        assessments=assessments,
    )
    return connection, identity, opportunity_ids, arguments


def store(connection, arguments):
    connection.execute("BEGIN IMMEDIATE")
    try:
        result = store_priority_batch(connection, **arguments)
        connection.execute("COMMIT")
        return result
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def priority_snapshot(connection):
    return (
        connection.execute("SELECT COUNT(*) FROM priority_runs").fetchone()[0],
        connection.execute("SELECT COUNT(*) FROM priority_assessments").fetchone()[0],
        connection.execute(
            "SELECT * FROM priority_profile_state ORDER BY profile_id"
        ).fetchall(),
    )


def test_first_store_and_identical_reuse_are_idempotent(tmp_path):
    connection, _, _, arguments = priority_fixture(tmp_path)
    first = store(connection, arguments)
    first_rows = tuple(connection.iterdump())
    second = store(connection, arguments)
    assert first.created is True and second.created is False
    assert (first.run_id, first.run_fingerprint) == (
        second.run_id,
        second.run_fingerprint,
    )
    assert connection.execute("SELECT COUNT(*) FROM priority_runs").fetchone()[0] == 1
    assert (
        connection.execute("SELECT COUNT(*) FROM priority_assessments").fetchone()[0]
        == 2
    )
    assert tuple(connection.iterdump()) == first_rows


@pytest.mark.parametrize(
    "column,value", [("evaluation_date", "2026-09-03"), ("run_payload_json", "{}")]
)
def test_existing_run_metadata_corruption_is_rejected(tmp_path, column, value):
    connection, _, _, arguments = priority_fixture(tmp_path)
    result = store(connection, arguments)
    connection.execute(
        f"UPDATE priority_runs SET {column}=? WHERE id=?", (value, result.run_id)
    )
    connection.commit()
    with pytest.raises(PriorityPersistenceError, match="metadata is corrupt"):
        store(connection, arguments)


def test_existing_assessment_corruption_is_rejected(tmp_path):
    connection, _, _, arguments = priority_fixture(tmp_path)
    result = store(connection, arguments)
    connection.execute(
        "UPDATE priority_assessments SET assessment_payload_json='{}' WHERE run_id=? LIMIT 1",
        (result.run_id,),
    )
    connection.commit()
    with pytest.raises(PriorityPersistenceError, match="assessments are corrupt"):
        store(connection, arguments)


def test_stored_assessment_count_mismatch_is_rejected(tmp_path):
    connection, _, _, arguments = priority_fixture(tmp_path)
    result = store(connection, arguments)
    connection.execute(
        "DELETE FROM priority_assessments WHERE run_id=? AND id=(SELECT max(id) FROM priority_assessments WHERE run_id=?)",
        (result.run_id, result.run_id),
    )
    connection.commit()
    with pytest.raises(PriorityPersistenceError, match="assessments are corrupt"):
        store(connection, arguments)


def test_invalid_assessment_fingerprint_is_rejected_before_writes(tmp_path):
    connection, _, _, arguments = priority_fixture(tmp_path)
    arguments["assessments"] = (
        replace(arguments["assessments"][0], assessment_fingerprint="0" * 64),
    )
    connection.execute("BEGIN IMMEDIATE")
    with pytest.raises(PriorityPersistenceError, match="fingerprint"):
        store_priority_batch(connection, **arguments)
    connection.rollback()
    assert connection.execute("SELECT COUNT(*) FROM priority_runs").fetchone()[0] == 0


def test_store_requires_active_transaction(tmp_path):
    connection, _, _, arguments = priority_fixture(tmp_path)
    with pytest.raises(PriorityPersistenceError, match="active transaction"):
        store_priority_batch(connection, **arguments)


def test_profile_owner_provenance_supports_distinct_ids_and_rejects_wrong_user(
    tmp_path,
):
    connection, identity, _, arguments = priority_fixture(tmp_path)
    assert identity.profile_id != identity.user_id
    correct = store(connection, arguments)
    assert correct.created is True

    before = priority_snapshot(connection)
    other_user_id = connection.execute(
        "SELECT id FROM users WHERE id != ? ORDER BY id LIMIT 1", (identity.user_id,)
    ).fetchone()[0]
    with pytest.raises(
        PriorityPersistenceError, match="profile/user provenance is inconsistent"
    ):
        store(connection, arguments | {"user_id": other_user_id})
    assert priority_snapshot(connection) == before


@pytest.mark.parametrize(
    "override",
    (
        {"matching_run_fingerprint": "f" * 64},
        {"matching_run_id": 999_999},
    ),
    ids=("valid-but-wrong-fingerprint", "missing-run"),
)
def test_matching_run_provenance_mismatch_is_rejected_without_mutation(
    tmp_path, override
):
    connection, _, _, arguments = priority_fixture(tmp_path)
    before = priority_snapshot(connection)
    with pytest.raises(
        PriorityPersistenceError, match="matching run provenance is inconsistent"
    ):
        store(connection, arguments | override)
    assert priority_snapshot(connection) == before


def test_matching_run_from_another_profile_is_rejected_without_mutation(tmp_path):
    connection, _, opportunity_ids, arguments = priority_fixture(tmp_path)
    second = ensure_user_profile(connection, "second-owner@example.invalid")
    item = _batch(second.profile_id, opportunity_ids[0]).assessments[0]
    batch = MatchingBatchResult((item,), "a" * 64, "b" * 64, 1)
    batch = replace(batch, batch_fingerprint=matching_batch_fingerprint(batch))
    foreign_run = store_matching_batch(
        connection,
        second.profile_id,
        batch,
        selection_version=MATCHING_SELECTION_VERSION,
    )
    before = priority_snapshot(connection)
    with pytest.raises(
        PriorityPersistenceError, match="matching run provenance is inconsistent"
    ):
        store(
            connection,
            arguments
            | {
                "matching_run_id": foreign_run.run_id,
                "matching_run_fingerprint": foreign_run.run_fingerprint,
            },
        )
    assert priority_snapshot(connection) == before


def test_new_snapshot_never_updates_existing_runs_or_assessments(tmp_path):
    connection, _, _, arguments = priority_fixture(tmp_path)
    first = store(connection, arguments)
    old_run = connection.execute(
        "SELECT * FROM priority_runs WHERE id=?", (first.run_id,)
    ).fetchone()
    old_assessments = connection.execute(
        "SELECT * FROM priority_assessments WHERE run_id=? ORDER BY id", (first.run_id,)
    ).fetchall()
    second = store(connection, arguments | {"evaluation_date": date(2026, 9, 3)})
    assert second.created is True and second.run_id != first.run_id
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
        == old_assessments
    )
    assert connection.execute("SELECT COUNT(*) FROM priority_runs").fetchone()[0] == 2


def test_migration_constraints_and_cross_profile_state(tmp_path):
    connection, identity, opportunity_ids, arguments = priority_fixture(
        tmp_path, opportunities=1
    )
    result = store(connection, arguments)
    assert connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version='0016'"
    ).fetchone()
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'priority_%'"
        )
    }
    assert tables == {"priority_runs", "priority_assessments", "priority_profile_state"}
    for column, value in (
        ("priority_category", "BAD"),
        ("eligibility_status", "BAD"),
        ("matching_lane", "BAD"),
        ("priority_score", 2),
        ("priority_evidence_coverage", -1),
        ("assessment_fingerprint", "BAD"),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"UPDATE priority_assessments SET {column}=? WHERE run_id=?",
                (value, result.run_id),
            )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO priority_assessments (run_id,opportunity_id,priority_evidence_coverage,eligibility_status,matching_lane,assessment_fingerprint,assessment_payload_json) VALUES (?,?,?,?,?,?,?)",
            (
                result.run_id,
                opportunity_ids[0],
                1,
                "UNKNOWN",
                "PRIMARY",
                "a" * 64,
                "{}",
            ),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """INSERT INTO priority_runs
            (profile_id,user_id,matching_run_id,persistence_version,input_assembly_version,
             priority_engine_version,priority_rules_version,freshness_version,quality_version,
             evaluation_date,matching_run_fingerprint,run_fingerprint,assessment_count,run_payload_json)
            SELECT profile_id,user_id,matching_run_id,persistence_version,input_assembly_version,
             priority_engine_version,priority_rules_version,freshness_version,quality_version,
             evaluation_date,matching_run_fingerprint,run_fingerprint,assessment_count,run_payload_json
            FROM priority_runs WHERE id=?""",
            (result.run_id,),
        )
    connection.rollback()
    second = ensure_user_profile(connection, "second@example.invalid")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO priority_profile_state(profile_id,current_run_id,persistence_version,input_assembly_version) VALUES (?,?,?,?)",
            (
                second.profile_id,
                result.run_id,
                "priority-persistence-v1",
                "priority-input-assembly-v1",
            ),
        )
