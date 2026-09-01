from dataclasses import replace

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.matching import (
    MatchingBatchResult,
    MatchingPersistenceAuditError,
    MatchingReadError,
    audit_matching_profile_history,
    list_matching_runs,
    matching_assessment_fingerprint,
    matching_batch_fingerprint,
    read_current_matching,
    read_matching_run,
    set_matching_state_empty,
    store_matching_batch,
)
from services.digital_twin.repository import ensure_user_profile
from tests.unit.test_matching_engine import assessment


@pytest.fixture
def database(tmp_path):
    connection = connect_database(tmp_path / "read-audit.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def add_opportunity(connection, suffix):
    return int(
        connection.execute(
            """INSERT INTO opportunities
        (canonical_title,organization,description,discovered_at,first_seen_at,last_seen_at,source_url,status)
        VALUES ('TEST','TEST','TEST','t','t','t',?,'new') RETURNING id""",
            (f"https://example.invalid/read-{suffix}",),
        ).fetchone()[0]
    )


def make_batch(profile_id, opportunity_ids):
    items = []
    for opportunity_id in opportunity_ids:
        original = assessment()
        raw = replace(
            original,
            profile_id=profile_id,
            opportunity_id=opportunity_id,
            semantic=replace(
                original.semantic,
                corpus_fingerprint="a" * 64,
                model_fingerprint="b" * 64,
            ),
            assessment_fingerprint="",
        )
        items.append(
            replace(raw, assessment_fingerprint=matching_assessment_fingerprint(raw))
        )
    raw = MatchingBatchResult(tuple(items), "a" * 64, "b" * 64, len(items))
    return replace(raw, batch_fingerprint=matching_batch_fingerprint(raw))


def test_states_validation_and_read_only(database):
    profile_id = ensure_user_profile(database, "read@example.invalid").profile_id
    database.commit()
    before = database.total_changes
    model = read_current_matching(database, profile_id)
    assert model.status == "NOT_SYNCED" and model.current_run is None
    assert database.total_changes == before
    set_matching_state_empty(
        database, profile_id, selection_version="selection-test-v1"
    )
    assert read_current_matching(database, profile_id).status == "EMPTY"
    for invalid in (True, 0, -1, "1"):
        with pytest.raises(MatchingReadError):
            read_current_matching(database, invalid)
        with pytest.raises(MatchingPersistenceAuditError):
            audit_matching_profile_history(database, invalid)
    with pytest.raises(MatchingReadError, match="does not exist"):
        read_current_matching(database, 99999)


def test_current_uses_state_not_max_history_is_stable_and_audit_is_deterministic(
    database,
):
    profile_id = ensure_user_profile(database, "history@example.invalid").profile_id
    ids = [add_opportunity(database, value) for value in range(3)]
    database.commit()
    first = store_matching_batch(
        database,
        profile_id,
        make_batch(profile_id, ids[:2]),
        selection_version="selection-test-v1",
    )
    second = store_matching_batch(
        database,
        profile_id,
        make_batch(profile_id, ids[1:]),
        selection_version="selection-test-v1",
    )
    database.execute(
        "UPDATE matching_profile_state SET current_run_id=? WHERE profile_id=?",
        (first.run_id, profile_id),
    )
    database.commit()
    before = database.total_changes
    current = read_current_matching(database, profile_id)
    assert current.current_run_id == first.run_id != second.run_id
    assert [item.opportunity_id for item in current.current_run.assessments] == sorted(
        ids[:2]
    )
    history = list_matching_runs(database, profile_id)
    assert [item.run_id for item in history] == [second.run_id, first.run_id]
    assert [item.is_current for item in history] == [False, True]
    audit = audit_matching_profile_history(database, profile_id)
    assert audit.ok and not audit.issues
    assert (
        audit.audit_fingerprint
        == audit_matching_profile_history(database, profile_id).audit_fingerprint
    )
    assert database.total_changes == before


def test_read_fails_closed_and_audit_reports_corruption(database):
    profile_id = ensure_user_profile(database, "corruption@example.invalid").profile_id
    opportunity_id = add_opportunity(database, "corrupt")
    database.commit()
    stored = store_matching_batch(
        database,
        profile_id,
        make_batch(profile_id, [opportunity_id]),
        selection_version="selection-test-v1",
    )
    database.execute(
        "UPDATE matching_assessments SET assessment_payload_json='not-json' WHERE run_id=?",
        (stored.run_id,),
    )
    database.commit()
    with pytest.raises(MatchingReadError, match="invalid assessment JSON"):
        read_matching_run(database, stored.run_id)
    report = audit_matching_profile_history(database, profile_id)
    assert not report.ok
    assert "ASSESSMENT_PAYLOAD_INVALID_JSON" in {issue.code for issue in report.issues}
