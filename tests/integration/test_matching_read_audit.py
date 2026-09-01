from dataclasses import replace
import hashlib
import json

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
    matching_run_fingerprint,
    read_current_matching,
    read_matching_run,
    set_matching_state_empty,
    store_matching_batch,
)
from services.collector.matching.engine import (
    MATCHING_ENGINE_VERSION,
    MATCHING_RULES_VERSION,
    SEMANTIC_PERCENTILE_VERSION,
)
from services.collector.matching.fingerprint import canonical_json
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
    read_matching_run(database, first.run_id)
    assert [item.run_id for item in history] == [second.run_id, first.run_id]
    assert [item.is_current for item in history] == [False, True]
    audit = audit_matching_profile_history(database, profile_id)
    assert audit.ok and not audit.issues
    assert (
        audit.audit_fingerprint
        == audit_matching_profile_history(database, profile_id).audit_fingerprint
    )
    assert database.total_changes == before


def test_orphan_history_fails_read_closed_and_is_reported_by_audit(database):
    profile_id = ensure_user_profile(database, "orphan@example.invalid").profile_id
    opportunity_id = add_opportunity(database, "orphan")
    database.commit()
    stored = store_matching_batch(
        database,
        profile_id,
        make_batch(profile_id, [opportunity_id]),
        selection_version="selection-test-v1",
    )
    database.execute(
        "DELETE FROM matching_profile_state WHERE profile_id=?", (profile_id,)
    )
    database.commit()
    with pytest.raises(MatchingReadError, match="without profile state"):
        read_current_matching(database, profile_id)
    report = audit_matching_profile_history(database, profile_id)
    assert not report.ok and report.audited_run_count == 1
    assert "STATE_RUN_MISMATCH" in {issue.code for issue in report.issues}
    assert report.runs[0].run_id == stored.run_id


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


def test_read_fails_closed_for_invalid_or_non_object_batch_json(database):
    profile_id = ensure_user_profile(database, "batch-read@example.invalid").profile_id
    opportunity_id = add_opportunity(database, "batch-read")
    database.commit()
    stored = store_matching_batch(
        database,
        profile_id,
        make_batch(profile_id, [opportunity_id]),
        selection_version="selection-test-v1",
    )
    for payload in ("not-json", "[]"):
        database.execute(
            "UPDATE matching_runs SET batch_payload_json=? WHERE id=?",
            (payload, stored.run_id),
        )
        database.commit()
        with pytest.raises(MatchingReadError, match="batch"):
            read_matching_run(database, stored.run_id)


@pytest.mark.parametrize(
    ("corruption", "expected_code"),
    (
        ("assessment_noncanonical", "ASSESSMENT_PAYLOAD_NOT_CANONICAL"),
        ("assessment_fingerprint", "ASSESSMENT_FINGERPRINT_MISMATCH"),
        ("lane", "ASSESSMENT_LANE_MISMATCH"),
        ("match_quality", "ASSESSMENT_MATCH_QUALITY_MISMATCH"),
        ("evidence_coverage", "ASSESSMENT_EVIDENCE_COVERAGE_MISMATCH"),
        ("assessment_version", "ASSESSMENT_VERSION_MISMATCH"),
        ("corpus", "ASSESSMENT_CORPUS_FINGERPRINT_MISMATCH"),
        ("model", "ASSESSMENT_MODEL_FINGERPRINT_MISMATCH"),
        ("batch_noncanonical", "BATCH_PAYLOAD_NOT_CANONICAL"),
        ("batch_content", "BATCH_CONTENT_MISMATCH"),
        ("batch_fingerprint", "BATCH_FINGERPRINT_MISMATCH"),
        ("run_fingerprint", "RUN_FINGERPRINT_MISMATCH"),
        ("assessment_count", "ASSESSMENT_COUNT_MISMATCH"),
    ),
)
def test_audit_reports_each_supported_corruption(database, corruption, expected_code):
    profile_id = ensure_user_profile(
        database, f"{corruption}@example.invalid"
    ).profile_id
    opportunity_id = add_opportunity(database, corruption)
    database.commit()
    stored = store_matching_batch(
        database,
        profile_id,
        make_batch(profile_id, [opportunity_id]),
        selection_version="selection-test-v1",
    )
    assessment_json = database.execute(
        "SELECT assessment_payload_json FROM matching_assessments WHERE run_id=?",
        (stored.run_id,),
    ).fetchone()[0]
    batch_json = database.execute(
        "SELECT batch_payload_json FROM matching_runs WHERE id=?", (stored.run_id,)
    ).fetchone()[0]
    if corruption == "assessment_noncanonical":
        database.execute(
            "UPDATE matching_assessments SET assessment_payload_json=? WHERE run_id=?",
            (json.dumps(json.loads(assessment_json), indent=2), stored.run_id),
        )
    elif corruption == "assessment_fingerprint":
        database.execute(
            "UPDATE matching_assessments SET assessment_fingerprint=? WHERE run_id=?",
            ("f" * 64, stored.run_id),
        )
    elif corruption in {"lane", "match_quality", "evidence_coverage"}:
        assignments = {
            "lane": ("lane", "UNCERTAIN"),
            "match_quality": ("match_quality", 0.25),
            "evidence_coverage": ("evidence_coverage", 0.75),
        }
        column, value = assignments[corruption]
        database.execute(
            f"UPDATE matching_assessments SET {column}=? WHERE run_id=?",
            (value, stored.run_id),
        )
    elif corruption in {"assessment_version", "corpus", "model"}:
        payload = json.loads(assessment_json)
        if corruption == "assessment_version":
            payload["matching_engine_version"] = "changed-version"
        elif corruption == "corpus":
            payload["semantic"]["corpus_fingerprint"] = "c" * 64
        else:
            payload["semantic"]["model_fingerprint"] = "d" * 64
        database.execute(
            "UPDATE matching_assessments SET assessment_payload_json=? WHERE run_id=?",
            (canonical_json(payload), stored.run_id),
        )
    elif corruption == "batch_noncanonical":
        database.execute(
            "UPDATE matching_runs SET batch_payload_json=? WHERE id=?",
            (json.dumps(json.loads(batch_json), indent=2), stored.run_id),
        )
    elif corruption == "batch_content":
        payload = json.loads(batch_json)
        payload["matching_engine_version"] = "changed-version"
        database.execute(
            "UPDATE matching_runs SET batch_payload_json=? WHERE id=?",
            (canonical_json(payload), stored.run_id),
        )
    elif corruption == "batch_fingerprint":
        database.execute(
            "UPDATE matching_runs SET batch_fingerprint=? WHERE id=?",
            ("e" * 64, stored.run_id),
        )
    elif corruption == "run_fingerprint":
        database.execute(
            "UPDATE matching_runs SET run_fingerprint=? WHERE id=?",
            ("d" * 64, stored.run_id),
        )
    else:
        database.execute(
            "UPDATE matching_runs SET assessment_count=2 WHERE id=?", (stored.run_id,)
        )
    database.commit()
    codes = {
        issue.code
        for issue in audit_matching_profile_history(database, profile_id).issues
    }
    assert expected_code in codes


def test_coherent_historical_versions_are_valid(database):
    profile_id = ensure_user_profile(database, "historical@example.invalid").profile_id
    opportunity_id = add_opportunity(database, "historical")
    database.commit()
    stored = store_matching_batch(
        database,
        profile_id,
        make_batch(profile_id, [opportunity_id]),
        selection_version="selection-test-v1",
    )
    versions = (
        "matching-engine-historical",
        "matching-rules-historical",
        "semantic-percentile-historical",
    )
    assert versions != (
        MATCHING_ENGINE_VERSION,
        MATCHING_RULES_VERSION,
        SEMANTIC_PERCENTILE_VERSION,
    )
    payload = json.loads(
        database.execute(
            "SELECT assessment_payload_json FROM matching_assessments WHERE run_id=?",
            (stored.run_id,),
        ).fetchone()[0]
    )
    (
        payload["matching_engine_version"],
        payload["matching_rules_version"],
        payload["semantic_percentile_version"],
    ) = versions
    payload["semantic"]["semantic_percentile_version"] = versions[2]
    assessment_json = canonical_json(payload)
    assessment_fingerprint = hashlib.sha256(assessment_json.encode()).hexdigest()
    batch = {
        "matching_engine_version": versions[0],
        "matching_rules_version": versions[1],
        "semantic_percentile_version": versions[2],
        "corpus_fingerprint": "a" * 64,
        "tfidf_model_fingerprint": "b" * 64,
        "assessment_count": 1,
        "assessment_fingerprints": [assessment_fingerprint],
    }
    batch_json = canonical_json(batch)
    batch_fingerprint = hashlib.sha256(batch_json.encode()).hexdigest()
    run_fingerprint = matching_run_fingerprint(
        profile_id=profile_id,
        selection_version="selection-test-v1",
        matching_engine_version=versions[0],
        matching_rules_version=versions[1],
        semantic_percentile_version=versions[2],
        corpus_fingerprint="a" * 64,
        tfidf_model_fingerprint="b" * 64,
        batch_fingerprint=batch_fingerprint,
        assessments=((opportunity_id, assessment_fingerprint),),
    )
    database.execute(
        """UPDATE matching_assessments SET assessment_payload_json=?,
        assessment_fingerprint=? WHERE run_id=?""",
        (assessment_json, assessment_fingerprint, stored.run_id),
    )
    database.execute(
        """UPDATE matching_runs SET matching_engine_version=?,matching_rules_version=?,
        semantic_percentile_version=?,batch_payload_json=?,batch_fingerprint=?,
        run_fingerprint=? WHERE id=?""",
        (*versions, batch_json, batch_fingerprint, run_fingerprint, stored.run_id),
    )
    database.commit()
    report = audit_matching_profile_history(database, profile_id)
    assert report.ok and report.issues == ()
