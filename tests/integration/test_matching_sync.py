import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.matching import MatchingSyncError, sync_matching
from services.collector.matching import sync as matching_sync
from services.collector.matching.selection import select_matching_opportunity_ids
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
    stored_lane_counts = {
        lane: count
        for lane, count in connection.execute(
            "SELECT lane, COUNT(*) FROM matching_assessments "
            "WHERE run_id=? GROUP BY lane",
            (first.run_id,),
        ).fetchall()
    }
    reported_nonzero_counts = {
        lane: count for lane, count in first.lane_counts.items() if count
    }
    assert stored_lane_counts == reported_nonzero_counts
    assert sum(first.lane_counts.values()) == first.selected_count
    assert sum(stored_lane_counts.values()) == first.selected_count


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


def test_dry_run_does_not_require_0015_marker_or_tables(database):
    connection, profile_id = database
    _add_opportunity(connection, "d")
    connection.execute("DELETE FROM schema_migrations WHERE version='0015'")
    connection.execute("DROP TABLE matching_profile_state")
    connection.execute("DROP TABLE matching_assessments")
    connection.execute("DROP TABLE matching_runs")
    connection.commit()
    dry = sync_matching(connection, profile_id, persist=False)
    assert dry.state == "READY"
    assert dry.batch_fingerprint
    assert dry.corpus_fingerprint
    assert dry.tfidf_model_fingerprint
    assert sum(dry.lane_counts.values()) == dry.selected_count == 1
    with pytest.raises(MatchingSyncError, match="migrate.*0015"):
        sync_matching(connection, profile_id)


def _stored_snapshot(connection, profile_id):
    state = connection.execute(
        "SELECT state,current_run_id FROM matching_profile_state WHERE profile_id=?",
        (profile_id,),
    ).fetchone()
    run_count = connection.execute("SELECT COUNT(*) FROM matching_runs").fetchone()[0]
    assessment_count = connection.execute(
        "SELECT COUNT(*) FROM matching_assessments"
    ).fetchone()[0]
    return state, run_count, assessment_count


def test_engine_failure_preserves_last_good_state(database, monkeypatch):
    connection, profile_id = database
    _add_opportunity(connection, "e")
    connection.commit()
    ready = sync_matching(connection, profile_id)
    before = _stored_snapshot(connection, profile_id)

    def fail_engine(*args, **kwargs):
        raise RuntimeError("injected engine failure")

    monkeypatch.setattr(matching_sync, "build_matching_assessments", fail_engine)
    with pytest.raises(RuntimeError, match="engine failure"):
        sync_matching(connection, profile_id)

    assert before == (("READY", ready.run_id), 1, 1)
    assert _stored_snapshot(connection, profile_id) == before


def test_persistence_failure_preserves_last_good_state(database, monkeypatch):
    connection, profile_id = database
    _add_opportunity(connection, "f")
    connection.commit()
    ready = sync_matching(connection, profile_id)
    before = _stored_snapshot(connection, profile_id)

    def fail_persistence(*args, **kwargs):
        raise RuntimeError("injected persistence failure")

    monkeypatch.setattr(
        matching_sync, "store_matching_batch_in_transaction", fail_persistence
    )
    with pytest.raises(RuntimeError, match="persistence failure"):
        sync_matching(connection, profile_id)

    assert before == (("READY", ready.run_id), 1, 1)
    assert _stored_snapshot(connection, profile_id) == before


@pytest.mark.parametrize("persist", [False, True])
def test_empty_cohort_does_not_call_engine(database, monkeypatch, persist):
    connection, profile_id = database

    def fail_if_called(*args, **kwargs):
        raise AssertionError("engine must not run for an empty cohort")

    monkeypatch.setattr(matching_sync, "build_matching_assessments", fail_if_called)
    assert sync_matching(connection, profile_id, persist=persist).state == "EMPTY"


def test_non_empty_cohort_calls_engine_exactly_once(database, monkeypatch):
    connection, profile_id = database
    _add_opportunity(connection, "a-engine-count")
    connection.commit()
    real_engine = matching_sync.build_matching_assessments
    calls = 0

    def counting_engine(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_engine(*args, **kwargs)

    monkeypatch.setattr(matching_sync, "build_matching_assessments", counting_engine)
    assert sync_matching(connection, profile_id, persist=False).state == "READY"
    assert calls == 1


def _determinism_database(path, opportunity_order, qualification_order):
    connection = connect_database(path)
    apply_migrations(connection)
    profile_id = ensure_user_profile(
        connection, f"determinism-{path.name}@example.invalid"
    ).profile_id
    opportunities = {
        10: ("Python Engineer", "Build Python data systems", "ten"),
        20: ("Analytics Engineer", "Model reliable analytics", "twenty"),
    }
    for opportunity_id in opportunity_order:
        title, description, suffix = opportunities[opportunity_id]
        connection.execute(
            """INSERT INTO opportunities
               (id,canonical_title,organization,description,discovered_at,
                first_seen_at,last_seen_at,source_url,status)
               VALUES (?,?,'TEST',?,'t','t','t',?,'new')""",
            (opportunity_id, title, description, f"https://example.invalid/{suffix}"),
        )
    for opportunity_id in qualification_order:
        connection.execute(
            """INSERT INTO opportunity_qualifications
               (opportunity_id,qualification,primary_domain,opportunity_type,
                employment_type,listing_quality,matched_domains_json,
                matched_title_signals_json,matched_description_signals_json,
                matched_exclusion_signals_json,reasons_json,classifier_version,
                input_fingerprint,classified_at)
               VALUES (?,'CORE_TARGET','DATA_ENGINEERING','JOB','UNKNOWN',
                       'NORMAL_LISTING','[]','[]','[]','[]','[]','classifier-v1',?,'t')""",
            (opportunity_id, str(opportunity_id)[0] * 64),
        )
    connection.commit()
    return connection, profile_id


def test_determinism_across_nonsemantic_insertion_order(tmp_path):
    first_connection, first_profile = _determinism_database(
        tmp_path / "first.db", (20, 10), (10, 20)
    )
    second_connection, second_profile = _determinism_database(
        tmp_path / "second.db", (10, 20), (20, 10)
    )
    try:
        assert select_matching_opportunity_ids(first_connection) == (10, 20)
        assert select_matching_opportunity_ids(second_connection) == (10, 20)
        first = sync_matching(first_connection, first_profile, persist=False)
        second = sync_matching(second_connection, second_profile, persist=False)
        assert first.selected_count == second.selected_count == 2
        assert first.lane_counts == second.lane_counts
        assert first.batch_fingerprint == second.batch_fingerprint
        assert first.corpus_fingerprint == second.corpus_fingerprint
        assert first.tfidf_model_fingerprint == second.tfidf_model_fingerprint
    finally:
        first_connection.close()
        second_connection.close()
