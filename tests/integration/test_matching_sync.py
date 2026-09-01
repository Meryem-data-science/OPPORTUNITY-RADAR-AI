import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.matching import MatchingSyncError, sync_matching
from services.digital_twin.repository import ensure_user_profile


def _add_opportunity(connection, suffix: str, qualification="CORE_TARGET"):
    opportunity_id = int(
        connection.execute(
            """INSERT INTO opportunities
               (canonical_title,organization,description,discovered_at,first_seen_at,
                last_seen_at,source_url,status)
               VALUES (?,?,?,'t','t','t',?,'new') RETURNING id""",
            (
                "Python Engineer",
                "TEST",
                "Build Python systems",
                f"https://example.invalid/{suffix}",
            ),
        ).fetchone()[0]
    )
    connection.execute(
        """INSERT INTO opportunity_qualifications
           (opportunity_id,qualification,primary_domain,opportunity_type,
            employment_type,listing_quality,matched_domains_json,
            matched_title_signals_json,matched_description_signals_json,
            matched_exclusion_signals_json,reasons_json,classifier_version,
            input_fingerprint,classified_at)
           VALUES (?,?,'DATA_ENGINEERING','JOB','UNKNOWN','NORMAL_LISTING',
                   '[]','[]','[]','[]','[]','classifier-v1',?,'t')""",
        (opportunity_id, qualification, suffix[0] * 64),
    )
    return opportunity_id


@pytest.fixture
def database(tmp_path):
    connection = connect_database(tmp_path / "sync.db")
    apply_migrations(connection)
    profile_id = ensure_user_profile(connection, "sync@example.invalid").profile_id
    yield connection, profile_id
    connection.close()


def test_ready_dry_run_and_persisted_idempotence(database):
    connection, profile_id = database
    _add_opportunity(connection, "a")
    _add_opportunity(connection, "b", "ADJACENT_TARGET")
    connection.commit()

    before = connection.total_changes
    dry = sync_matching(connection, profile_id, persist=False)
    assert connection.total_changes == before
    assert dry.state == "READY" and not dry.persisted and dry.created is None
    assert dry.selected_count == 2
    assert tuple(dry.lane_counts) == ("PRIMARY", "UNCERTAIN", "OUTSIDE_PREFERENCES")
    assert sum(dry.lane_counts.values()) == 2

    first = sync_matching(connection, profile_id)
    second = sync_matching(connection, profile_id)
    assert first.created is True and second.created is False
    assert first.run_id == second.run_id
    assert first.run_fingerprint == second.run_fingerprint
    assert connection.execute("SELECT COUNT(*) FROM matching_runs").fetchone()[0] == 1
    assert (
        connection.execute("SELECT COUNT(*) FROM matching_assessments").fetchone()[0]
        == 2
    )
    assert connection.execute(
        "SELECT state,current_run_id FROM matching_profile_state WHERE profile_id=?",
        (profile_id,),
    ).fetchone() == ("READY", first.run_id)


def test_input_change_creates_new_run_and_empty_preserves_history(database):
    connection, profile_id = database
    opportunity_id = _add_opportunity(connection, "c")
    connection.commit()
    first = sync_matching(connection, profile_id)
    connection.execute(
        "UPDATE opportunities SET description='A materially different semantic corpus' WHERE id=?",
        (opportunity_id,),
    )
    connection.commit()
    second = sync_matching(connection, profile_id)
    assert second.created and second.run_id != first.run_id
    assert connection.execute("SELECT COUNT(*) FROM matching_runs").fetchone()[0] == 2

    connection.execute("UPDATE opportunities SET is_active=0")
    connection.commit()
    empty = sync_matching(connection, profile_id)
    assert empty.state == "EMPTY" and empty.persisted
    assert connection.execute(
        "SELECT state,current_run_id FROM matching_profile_state WHERE profile_id=?",
        (profile_id,),
    ).fetchone() == ("EMPTY", None)
    assert connection.execute("SELECT COUNT(*) FROM matching_runs").fetchone()[0] == 2


def test_empty_dry_run_and_validation(database):
    connection, profile_id = database
    before = connection.total_changes
    result = sync_matching(connection, profile_id, persist=False)
    assert result.state == "EMPTY" and not result.persisted
    assert connection.total_changes == before
    for invalid in (True, 0, -1, "1"):
        with pytest.raises(MatchingSyncError, match="positive integer"):
            sync_matching(connection, invalid, persist=False)
    with pytest.raises(MatchingSyncError, match="does not exist"):
        sync_matching(connection, 999, persist=False)


def test_persist_requires_0015_but_dry_run_does_not(database):
    connection, profile_id = database
    connection.execute("DELETE FROM schema_migrations WHERE version='0015'")
    connection.commit()
    assert sync_matching(connection, profile_id, persist=False).state == "EMPTY"
    with pytest.raises(MatchingSyncError, match="migrate.*0015"):
        sync_matching(connection, profile_id)
