from dataclasses import replace
import sqlite3

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.matching import (
    MatchingBatchResult,
    MatchingPersistenceError,
    matching_assessment_fingerprint,
    matching_batch_fingerprint,
    read_matching_profile_state,
    set_matching_state_empty,
    store_matching_batch,
)
from services.digital_twin.repository import ensure_user_profile
from tests.unit.test_matching_engine import assessment


def make_batch(profile_id, opportunity_ids):
    items = []
    for opportunity_id in opportunity_ids:
        raw = replace(
            assessment(),
            profile_id=profile_id,
            opportunity_id=opportunity_id,
            assessment_fingerprint="",
        )
        items.append(
            replace(raw, assessment_fingerprint=matching_assessment_fingerprint(raw))
        )
    raw_batch = MatchingBatchResult(tuple(items), "a" * 64, "b" * 64, len(items))
    return replace(raw_batch, batch_fingerprint=matching_batch_fingerprint(raw_batch))


@pytest.fixture
def database(tmp_path):
    connection = connect_database(tmp_path / "matching-persistence.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def add_opportunity(connection, suffix):
    return int(
        connection.execute(
            """INSERT INTO opportunities (canonical_title,organization,description,discovered_at,first_seen_at,last_seen_at,source_url,status)
           VALUES ('TEST','TEST','TEST','t','t','t',?,'new') RETURNING id""",
            (f"https://example.invalid/{suffix}",),
        ).fetchone()[0]
    )


def test_store_is_idempotent_append_only_and_empty_preserves_history(database):
    profile_id = ensure_user_profile(database, "persistence@example.invalid").profile_id
    ids = (add_opportunity(database, 1), add_opportunity(database, 2))
    database.commit()
    result = store_matching_batch(
        database,
        profile_id,
        make_batch(profile_id, ids),
        selection_version="selection-test-v1",
    )
    repeated = store_matching_batch(
        database,
        profile_id,
        make_batch(profile_id, tuple(reversed(ids))),
        selection_version="selection-test-v1",
    )
    assert result.created and not repeated.created and result.run_id == repeated.run_id
    assert database.execute("SELECT COUNT(*) FROM matching_runs").fetchone()[0] == 1
    assert (
        database.execute("SELECT COUNT(*) FROM matching_assessments").fetchone()[0] == 2
    )
    assert read_matching_profile_state(database, profile_id)[1:3] == (
        "READY",
        result.run_id,
    )
    set_matching_state_empty(
        database, profile_id, selection_version="selection-test-v1"
    )
    assert read_matching_profile_state(database, profile_id)[1:3] == ("EMPTY", None)
    assert database.execute("SELECT COUNT(*) FROM matching_runs").fetchone()[0] == 1
    store_matching_batch(
        database,
        profile_id,
        make_batch(profile_id, ids),
        selection_version="selection-test-v1",
    )
    assert read_matching_profile_state(database, profile_id)[1:3] == (
        "READY",
        result.run_id,
    )


def test_rollback_and_cascades(database):
    profile_id = ensure_user_profile(database, "rollback@example.invalid").profile_id
    ids = (add_opportunity(database, 3), add_opportunity(database, 4))
    database.commit()

    def fail(position):
        if position == 0:
            raise RuntimeError("test interruption")

    with pytest.raises(RuntimeError):
        store_matching_batch(
            database,
            profile_id,
            make_batch(profile_id, ids),
            selection_version="selection-test-v1",
            after_assessment_insert=fail,
        )
    assert database.execute("SELECT COUNT(*) FROM matching_runs").fetchone()[0] == 0
    stored = store_matching_batch(
        database,
        profile_id,
        make_batch(profile_id, ids),
        selection_version="selection-test-v1",
    )
    with pytest.raises(sqlite3.IntegrityError):
        database.execute("DELETE FROM opportunities WHERE id=?", (ids[0],))
    assert (
        database.execute(
            "SELECT COUNT(*) FROM opportunities WHERE id=?", (ids[0],)
        ).fetchone()[0]
        == 1
    )
    assert database.execute(
        "SELECT assessment_count FROM matching_runs WHERE id=?", (stored.run_id,)
    ).fetchone() == (2,)
    assert (
        database.execute(
            "SELECT COUNT(*) FROM matching_assessments WHERE run_id=?", (stored.run_id,)
        ).fetchone()[0]
        == 2
    )
    assert read_matching_profile_state(database, profile_id)[1:3] == (
        "READY",
        stored.run_id,
    )
    database.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
    assert database.execute("SELECT COUNT(*) FROM matching_runs").fetchone()[0] == 0
    assert (
        database.execute("SELECT COUNT(*) FROM matching_assessments").fetchone()[0] == 0
    )
    assert read_matching_profile_state(database, profile_id) is None


def test_missing_profile_and_existing_corruption_fail_closed(database):
    with pytest.raises(MatchingPersistenceError, match="profile"):
        set_matching_state_empty(database, 999, selection_version="selection-test-v1")
    profile_id = ensure_user_profile(database, "corrupt@example.invalid").profile_id
    opportunity_id = add_opportunity(database, 5)
    database.commit()
    batch = make_batch(profile_id, (opportunity_id,))
    result = store_matching_batch(
        database, profile_id, batch, selection_version="selection-test-v1"
    )
    database.execute(
        "UPDATE matching_assessments SET lane='UNCERTAIN' WHERE run_id=?",
        (result.run_id,),
    )
    database.commit()
    with pytest.raises(MatchingPersistenceError, match="corrupt"):
        store_matching_batch(
            database, profile_id, batch, selection_version="selection-test-v1"
        )
