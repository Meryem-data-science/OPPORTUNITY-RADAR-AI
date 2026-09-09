from dataclasses import replace
import hashlib
import json

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.matching import (
    SEMANTIC_BINDING_VERSION,
    MatchingBatchResult,
    MatchingPersistenceError,
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


# --------------------------------------------------------------------------
# semantic binding provenance: legacy runs stay valid, new runs are protected
# --------------------------------------------------------------------------


def bound_batch(profile_id, opportunity_ids, fingerprint="1" * 64):
    """A batch carrying the provenance `build_matching_assessments` computes."""
    raw = replace(
        make_batch(profile_id, opportunity_ids),
        semantic_binding_version=SEMANTIC_BINDING_VERSION,
        semantic_binding_fingerprint=fingerprint,
        batch_fingerprint="",
    )
    return replace(raw, batch_fingerprint=matching_batch_fingerprint(raw))


def store(connection, profile_id, batch):
    """Commit the fixture's pending inserts first, as every test here does."""
    if connection.in_transaction:
        connection.commit()
    return store_matching_batch(
        connection, profile_id, batch, selection_version="selection-test-v1"
    )


def stored_payload(connection, run_id):
    return json.loads(
        connection.execute(
            "SELECT batch_payload_json FROM matching_runs WHERE id=?", (run_id,)
        ).fetchone()[0]
    )


def test_a_new_run_stores_both_binding_fields_and_stays_idempotent(database):
    profile_id = ensure_user_profile(database, "binding@example.invalid").profile_id
    opportunity_id = add_opportunity(database, "binding")
    batch = bound_batch(profile_id, (opportunity_id,))

    first = store(database, profile_id, batch)
    payload = stored_payload(database, first.run_id)
    assert payload["semantic_binding_version"] == SEMANTIC_BINDING_VERSION
    assert payload["semantic_binding_fingerprint"] == "1" * 64
    # The content half is untouched by the addition.
    assert payload["corpus_fingerprint"] == "a" * 64
    assert payload["assessment_count"] == 1

    second = store(database, profile_id, batch)
    assert (second.run_id, second.created) == (first.run_id, False)
    assert database.execute("SELECT COUNT(*) FROM matching_runs").fetchone()[0] == 1
    assert audit_matching_profile_history(database, profile_id).ok is True


def test_a_legacy_run_without_binding_fields_still_passes_the_audit(database):
    """History is not retroactively corrupt because a new field exists."""
    profile_id = ensure_user_profile(database, "legacy@example.invalid").profile_id
    opportunity_id = add_opportunity(database, "legacy")
    legacy = make_batch(profile_id, (opportunity_id,))
    assert legacy.semantic_binding_version is None

    result = store(database, profile_id, legacy)
    payload = stored_payload(database, result.run_id)
    assert "semantic_binding_version" not in payload
    assert "semantic_binding_fingerprint" not in payload

    report = audit_matching_profile_history(database, profile_id)
    assert report.ok is True and report.issues == ()
    assert read_current_matching(database, profile_id).status == "READY"


def test_a_legacy_run_reads_back_as_carrying_no_binding(database):
    profile_id = ensure_user_profile(database, "legacyread@example.invalid").profile_id
    opportunity_id = add_opportunity(database, "legacyread")
    store(database, profile_id, make_batch(profile_id, (opportunity_id,)))
    run = read_current_matching(database, profile_id).current_run
    assert run.semantic_binding_version is None
    assert run.semantic_binding_fingerprint is None


def test_a_new_run_reads_back_carrying_its_binding(database):
    profile_id = ensure_user_profile(database, "newread@example.invalid").profile_id
    opportunity_id = add_opportunity(database, "newread")
    store(database, profile_id, bound_batch(profile_id, (opportunity_id,)))
    run = read_current_matching(database, profile_id).current_run
    assert run.semantic_binding_version == SEMANTIC_BINDING_VERSION
    assert run.semantic_binding_fingerprint == "1" * 64


def test_a_tampered_binding_is_caught_by_the_run_fingerprint(database):
    """The binding is protected by the operational identity, not the content one.

    Editing it in the stored payload without recomputing the run fingerprint is
    exactly the corruption the run layer exists to notice — and the batch
    fingerprint, being a content digest, correctly does not move.
    """
    profile_id = ensure_user_profile(database, "tamper@example.invalid").profile_id
    opportunity_id = add_opportunity(database, "tamper")
    result = store(database, profile_id, bound_batch(profile_id, (opportunity_id,)))
    assert audit_matching_profile_history(database, profile_id).ok is True

    payload = stored_payload(database, result.run_id)
    payload["semantic_binding_fingerprint"] = "9" * 64
    database.execute(
        "UPDATE matching_runs SET batch_payload_json=? WHERE id=?",
        (canonical_json(payload), result.run_id),
    )
    database.commit()

    report = audit_matching_profile_history(database, profile_id)
    assert report.ok is False
    codes = {issue.code for issue in report.issues}
    assert "RUN_FINGERPRINT_MISMATCH" in codes
    # The content digest is untouched, which is the point of the boundary.
    assert "BATCH_FINGERPRINT_MISMATCH" not in codes
    assert "BATCH_CONTENT_MISMATCH" not in codes


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"semantic_binding_fingerprint": None}, "SEMANTIC_BINDING_PARTIAL"),
        ({"semantic_binding_version": None}, "SEMANTIC_BINDING_PARTIAL"),
        (
            {"semantic_binding_version": "semantic-binding-v0"},
            "SEMANTIC_BINDING_VERSION_UNSUPPORTED",
        ),
        (
            {"semantic_binding_fingerprint": "not-a-digest"},
            "SEMANTIC_BINDING_FINGERPRINT_INVALID",
        ),
    ],
)
def test_malformed_binding_provenance_is_an_explicit_audit_issue(
    database, mutation, expected
):
    profile_id = ensure_user_profile(database, "broken@example.invalid").profile_id
    opportunity_id = add_opportunity(database, "broken")
    result = store(database, profile_id, bound_batch(profile_id, (opportunity_id,)))
    payload = stored_payload(database, result.run_id)
    for key, value in mutation.items():
        if value is None:
            payload.pop(key)
        else:
            payload[key] = value
    database.execute(
        "UPDATE matching_runs SET batch_payload_json=? WHERE id=?",
        (canonical_json(payload), result.run_id),
    )
    database.commit()

    report = audit_matching_profile_history(database, profile_id)
    assert report.ok is False
    assert expected in {issue.code for issue in report.issues}


def test_a_different_binding_is_a_different_operational_run(database):
    """Same content identity, different binding, different run identity."""
    profile_id = ensure_user_profile(database, "twobind@example.invalid").profile_id
    opportunity_id = add_opportunity(database, "twobind")
    first = store(database, profile_id, bound_batch(profile_id, (opportunity_id,)))
    second = store(
        database, profile_id, bound_batch(profile_id, (opportunity_id,), "2" * 64)
    )
    assert second.created is True
    assert second.run_id != first.run_id
    assert second.run_fingerprint != first.run_fingerprint
    # The content-oriented batch fingerprint is identical in both runs.
    batch_fingerprints = {
        row[0]
        for row in database.execute("SELECT batch_fingerprint FROM matching_runs")
    }
    assert len(batch_fingerprints) == 1
    assert audit_matching_profile_history(database, profile_id).ok is True


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ({"semantic_binding_fingerprint": None}, "both a version and a fingerprint"),
        ({"semantic_binding_version": "semantic-binding-v0"}, "unsupported semantic"),
        ({"semantic_binding_fingerprint": "nope"}, "invalid semantic binding"),
    ],
)
def test_persistence_refuses_to_store_incoherent_binding_provenance(
    database, mutation, match
):
    profile_id = ensure_user_profile(database, "refuse@example.invalid").profile_id
    opportunity_id = add_opportunity(database, "refuse")
    batch = replace(bound_batch(profile_id, (opportunity_id,)), **mutation)
    with pytest.raises(MatchingPersistenceError, match=match):
        store(database, profile_id, batch)
    assert database.execute("SELECT COUNT(*) FROM matching_runs").fetchone()[0] == 0
