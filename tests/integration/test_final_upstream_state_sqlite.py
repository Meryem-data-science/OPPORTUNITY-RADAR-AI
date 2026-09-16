"""Phase 11.2B: the upstream chain validator over a disposable migrated SQLite file.

The world is the Phase 9A corpus, built through each phase's own entry points
(Digital Twin commands, qualification persistence, geography, eligibility and
Matching synchronization), plus the collection state that corpus lacks: a
TEST-ONLY source registry, source runs through the run owner, source links, and
provenance for the facts the corpus seeds directly. All of it happens during
*setup*, on a throwaway file. Every test then copies that file and validates
the copy. **The operational database is never opened.**

Negative fixtures damage only a test's own copy, and only to prove the
validator reports what is broken instead of repairing it.
"""

import hashlib
import importlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from services.collector.database.connection import connect_database, connect_readonly_database
from services.collector.database.source_runs import finalize_successful_source_run, start_source_run
from services.collector.deduplication.decisions import confirm_pair
from services.collector.deduplication.merges import apply_merge
from services.collector.models.source_run import SourceRunMetrics
from services.collector.sources import SourceConfig
from services.final_validation import upstream_cli as cli
from services.final_validation import upstream_state as upstream
from services.final_validation.operational_state import FAIL, PASS, OperationalStateError
from services.final_validation.upstream_state import (
    DEMONSTRATED,
    NEVER_RUN,
    NOT_ASSESSED,
    NOT_DEMONSTRATED,
    READY,
    STALE,
)
from tests.integration.test_recommendation_sqlite import TEST_ONLY_EMAIL, build_corpus

SIDE_SUFFIXES = ("-wal", "-shm", "-journal")
TEST_ONLY_CV_SHA256 = hashlib.sha256(b"TEST-ONLY cv document").hexdigest()

REGISTRY = """
sources:
  - id: test_only_board_a
    type: greenhouse
    enabled: true
    category: jobs
    country: null
    organization: TEST ONLY Org A
    board_token: test-only-a
  - id: test_only_board_b
    type: greenhouse
    enabled: true
    category: jobs
    country: null
    organization: TEST ONLY Org B
    board_token: test-only-b
  - id: test_only_never_run
    type: stage_ma_html
    enabled: true
    category: jobs
    country: ZZ
    detail_page_limit: 1
"""

#: Entry points that compute, synchronize, classify, resolve, collect or merge.
COMPUTING_ENTRY_POINTS = (
    ("services.collector.matching.sync", "sync_matching"),
    ("services.collector.matching", "sync_matching"),
    ("services.collector.matching.selection", "select_matching_opportunity_ids"),
    ("services.collector.matching.engine", "build_matching_assessments"),
    ("services.recommendation.sync", "sync_recommendations"),
    ("services.recommendation.input_assembly", "assemble_recommendation_inputs"),
    ("services.collector.qualification.classifier", "classify_opportunity"),
    ("services.collector.qualification.fine_classifier", "classify_fine_categories"),
    ("services.collector.qualification.persistence", "persist_qualifications"),
    ("services.collector.qualification.audit", "audit_database"),
    ("services.geography.service", "synchronize_location_resolutions"),
    ("services.geography.service", "audit_geographic_targeting"),
    ("services.eligibility.service", "synchronize_eligibility"),
    ("services.collector.deduplication.audit", "audit_opportunities"),
    ("services.collector.deduplication.merges", "apply_merge"),
    ("services.collector.database.source_runs", "start_source_run"),
    ("services.collector.database.opportunities", "persist_opportunities"),
    ("services.digital_twin.preferences.repository", "synchronize_profile_preferences"),
    ("services.digital_twin.skills.repository", "synchronize_profile_skills"),
    ("services.collector.agent", "RadarAgent"),
)


def _source(source_id):
    return SourceConfig(
        id=source_id, type="greenhouse", enabled=True, organization="TEST ONLY Org", board_token="test-only",
        category="jobs",
    )


def _complete_collection_and_provenance(connection, ids):
    """Give the corpus what real collection and the fact cycle would have left behind."""
    for source_id, clock in (("test_only_board_a", "2099-01-01T00:00:00+00:00"), ("test_only_board_b", "2099-01-02T00:00:00+00:00")):
        attempt = start_source_run(connection, _source(source_id), clock=lambda value=clock: value)
        finalize_successful_source_run(
            connection, attempt, metrics=SourceRunMetrics(items_found=len(ids)), clock=lambda value=clock: value
        )
    for opportunity_id, source_url in connection.execute("SELECT id, source_url FROM opportunities").fetchall():
        source_id = "test_only_board_b" if opportunity_id == ids["out_of_target"] else "test_only_board_a"
        connection.execute(
            """INSERT INTO opportunity_sources (opportunity_id, source_id, source_url, discovered_at)
               VALUES (?, ?, ?, '2099-01-01')""",
            (opportunity_id, source_id, source_url),
        )
    unproven = connection.execute(
        """SELECT id FROM profile_facts AS f
            WHERE NOT EXISTS (SELECT 1 FROM profile_fact_provenance AS p WHERE p.fact_id = f.id)"""
    ).fetchall()
    for index, (fact_id,) in enumerate(unproven):
        connection.execute(
            """INSERT INTO profile_fact_provenance (fact_id, source_type, provenance_key, cv_sha256,
                                                    parser_version, extractor_version)
               VALUES (?, 'CV', ?, ?, 'TEST-ONLY-parser', 'TEST-ONLY-extractor')""",
            (fact_id, f"test-only-cv-{index}", TEST_ONLY_CV_SHA256),
        )
    connection.commit()


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    directory = tmp_path_factory.mktemp("final-upstream-world")
    connection, path, identity, ids, matching = build_corpus(directory)
    try:
        assert matching.state == "READY"
        _complete_collection_and_provenance(connection, ids)
    finally:
        connection.close()
    registry = directory / "TEST-ONLY-sources.yaml"
    registry.write_text(REGISTRY, encoding="utf-8")
    return {"path": path, "profile_id": identity.profile_id, "ids": ids, "registry": registry}


@pytest.fixture
def copy(world, tmp_path):
    target = tmp_path / "upstream-copy.db"
    shutil.copy2(world["path"], target)
    return target


def validate(world, path, **kwargs):
    return upstream.validate_upstream_state(path, world["profile_id"], source_registry_path=world["registry"], **kwargs)


def check(evidence, name):
    return next(item for item in evidence["checks"] if item["name"] == name)


def damage(path, *statements):
    """TEST ONLY corruption of a private copy, foreign keys deliberately off."""
    connection = sqlite3.connect(path)
    try:
        for statement, parameters in statements:
            connection.execute(statement, parameters)
        connection.commit()
    finally:
        connection.close()


def independent_fingerprint(path: Path):
    def one(file: Path):
        if not file.exists():
            return None
        stat = file.stat()
        return (hashlib.sha256(file.read_bytes()).hexdigest(), stat.st_size, stat.st_mtime_ns)

    return one(path), {suffix: one(Path(f"{path}{suffix}")) for suffix in SIDE_SUFFIXES}


def reader(path):
    return connect_readonly_database(path)


# --------------------------------------------------------------------------
# the healthy chain, against independent reads
# --------------------------------------------------------------------------


def test_a_coherent_upstream_chain_passes_with_values_read_from_the_owners(world, copy):
    evidence = validate(world, copy)

    assert evidence["integrity_result"] == PASS, [item for item in evidence["checks"] if item["integrity"] != PASS]
    assert [item["name"] for item in evidence["checks"]] == list(upstream.CHECK_ORDER)
    assert evidence["boundary"] == "STOPS_AT_CURRENT_MATCHING_RUN"

    connection = reader(copy)
    try:
        scope = connection.execute(
            "SELECT COUNT(*) FROM opportunities WHERE is_active = 1 AND status != 'merged_duplicate'"
        ).fetchone()[0]
        qualified = connection.execute("SELECT COUNT(*) FROM opportunity_qualifications").fetchone()[0]
        run_id, count = connection.execute(
            """SELECT r.id, r.assessment_count FROM matching_profile_state AS s
                JOIN matching_runs AS r ON r.id = s.current_run_id WHERE s.profile_id = ?""",
            (world["profile_id"],),
        ).fetchone()
        statuses = dict(connection.execute(
            "SELECT status, COUNT(*) FROM profile_facts WHERE profile_id = ? GROUP BY status", (world["profile_id"],)
        ).fetchall())
    finally:
        connection.close()

    assert evidence["phase8"]["coverage"]["in_scope_opportunity_count"] == scope
    assert evidence["phase8"]["coverage"]["qualification_row_count"] == qualified
    assert evidence["phase8"]["currency"]["input_fingerprint_drift_count"] == 0
    assert check(evidence, "PHASE8_INPUT_CURRENCY")["operational_state"] == READY
    assert evidence["digital_twin"]["fact_status_counts"] == statuses
    assert evidence["digital_twin"]["distinct_cv_fingerprint_count"] == 1

    boundary = evidence["matching_boundary"]
    assert (boundary["current_run_id"], boundary["matched_opportunity_count"]) == (run_id, count)
    assert check(evidence, "MATCHING_UPSTREAM_BOUNDARY")["demonstrability"] == DEMONSTRATED
    assert boundary["cohort_membership_freshness"] == NOT_ASSESSED

    # Read from what the corpus stated through the Digital Twin commands, not from a constant.
    targeting = evidence["targeting"]
    assert targeting["persisted_target_exists"] is False
    assert (targeting["geography"]["country_code"], targeting["geography"]["rule_id"]) == ("MA", "mobility-restricted-country-v1")
    assert targeting["opportunity_type"] == {
        "known": True, "opportunity_types": ["INTERNSHIP"], "rule_id": "declared-opportunity-types-v1",
    }


def test_a_configured_source_that_never_ran_is_reported_as_never_run_and_passes(world, copy):
    evidence = validate(world, copy)

    collection = evidence["collection"]
    by_id = {item["source_id"]: item for item in collection["sources"]}
    assert collection["configured_never_run_source_ids"] == ["test_only_never_run"]
    assert by_id["test_only_never_run"] == {
        "source_id": "test_only_never_run", "configured": True, "persisted": False, "enabled": True,
        "type": "stage_ma_html", "country": "ZZ", "run_count": 0, "run_status_counts": {},
        "latest_run_status": None, "latest_run_started_at": None, "latest_items_found": None,
        "anomaly_code": None, "operational_state": NEVER_RUN,
    }
    assert by_id["test_only_board_a"]["operational_state"] == "SUCCESS"
    assert collection["newest_run_timestamp_raw"] == "2099-01-02T00:00:00+00:00"
    sources = check(evidence, "COLLECTION_SOURCES")
    assert (sources["integrity"], sources["demonstrability"], sources["severity"]) == (PASS, DEMONSTRATED, "WARNING")


def test_no_persisted_merge_is_not_demonstrated_and_passes(world, copy):
    evidence = validate(world, copy)

    dedup = check(evidence, "DEDUP_PERSISTED_STATE")
    assert (dedup["integrity"], dedup["demonstrability"]) == (PASS, NOT_DEMONSTRATED)
    assert evidence["dedup"]["end_to_end_merge_claim"] == NOT_DEMONSTRATED


def test_a_merge_applied_by_the_owner_is_demonstrated(world, copy):
    a, b = sorted((world["ids"]["strong"], world["ids"]["out_of_target"]))
    connection = connect_database(copy)
    try:
        connection.execute(
            """INSERT INTO deduplication_decisions (
                   opportunity_a_id, opportunity_b_id, status, audit_classification, title_similarity,
                   organization_similarity, title_normalized_exact, organization_normalized_exact,
                   location_signal, shared_source_url, shared_application_url, shared_canonical_url,
                   reasons_json, first_detected_at, last_detected_at)
               VALUES (?, ?, 'POSSIBLE_DUPLICATE', 'STRONG_CANDIDATE', 0.9, 1.0, 0, 1, 'TEST_ONLY', 0, 0, 0,
                       '["TEST ONLY"]', '2099-01-01', '2099-01-01')""",
            (a, b),
        )
        connection.commit()
        confirm_pair(connection, a, b)
        apply_merge(connection, a, b, world["ids"]["strong"])
    finally:
        connection.close()

    connection = reader(copy)
    try:
        absorbed_was_matched = connection.execute(
            """SELECT COUNT(*) FROM matching_assessments AS a
                JOIN matching_profile_state AS s ON s.current_run_id = a.run_id
               WHERE s.profile_id = ? AND a.opportunity_id = ?""",
            (world["profile_id"], world["ids"]["out_of_target"]),
        ).fetchone()[0]
    finally:
        connection.close()

    evidence = validate(world, copy)

    dedup = check(evidence, "DEDUP_PERSISTED_STATE")
    assert (dedup["integrity"], dedup["demonstrability"]) == (PASS, DEMONSTRATED)
    assert evidence["dedup"]["applied_merge_count"] == 1
    assert evidence["dedup"]["merge_source_move_count"] == 1
    # The absorbed posting left the scope after Matching ran: reported, and not a membership verdict.
    assert evidence["integrity_result"] == PASS
    assert evidence["matching_boundary"]["matched_outside_current_scope_count"] == absorbed_was_matched


def test_the_evidence_carries_no_private_value_url_hash_or_error_text(world, copy):
    damage(copy, (
        "INSERT INTO source_runs (source_id, started_at, finished_at, status, error_type, error_message) "
        "VALUES ('test_only_board_a', '2099-01-03T00:00:00+00:00', '2099-01-03T00:00:01+00:00', 'FAILED', "
        "'TestOnlyError', 'TEST-ONLY-secret-error-message')", ()),
    )

    serialized = upstream.serialize_evidence(validate(world, copy))

    for forbidden in (TEST_ONLY_EMAIL, TEST_ONLY_CV_SHA256, "TEST-ONLY-secret-error-message", "https://",
                      "example.invalid", "TEST ONLY Org", "test-only-a", "Maroc", str(copy.parent)):
        assert forbidden not in serialized, forbidden


# --------------------------------------------------------------------------
# read-only guarantees
# --------------------------------------------------------------------------


def test_the_connection_is_read_only_and_every_read_happens_inside_one_rollback_snapshot(world, copy, monkeypatch):
    statements = []
    real = connect_readonly_database
    opened = []

    class Recording:
        def __init__(self, connection):
            self._connection = connection

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def execute(self, statement, *parameters):
            normalized = " ".join(statement.split())
            statements.append(normalized)
            if normalized.upper().startswith("SELECT"):
                assert self._connection.execute("PRAGMA query_only").fetchone()[0] == 1
                assert self._connection.in_transaction, normalized
            return self._connection.execute(statement, *parameters)

    def recording(path):
        connection = real(path)
        opened.append(connection)
        return Recording(connection)

    monkeypatch.setattr(upstream, "connect_readonly_database", recording)
    evidence = validate(world, copy)

    assert evidence["integrity_result"] == PASS
    assert len(opened) == 1
    assert statements[:3] == ["PRAGMA query_only = ON", "PRAGMA query_only", "BEGIN"]
    assert statements[-1] == "ROLLBACK"
    assert statements.count("BEGIN") == 1 and statements.count("ROLLBACK") == 1
    for statement in statements:
        upper = statement.upper()
        assert upper.startswith(("SELECT", "WITH", "PRAGMA QUERY_ONLY", "BEGIN", "ROLLBACK")), statement
        words = set(upper.replace("(", " ").replace(",", " ").split())
        assert not words & {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER", "COMMIT"}, statement
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


def test_no_computing_collecting_merging_or_synchronizing_entry_point_is_reached(world, copy, monkeypatch):
    def tripwire(*_args, **_kwargs):
        raise AssertionError("the upstream validator reached a computing entry point")

    for module_name, attribute in COMPUTING_ENTRY_POINTS:
        monkeypatch.setattr(importlib.import_module(module_name), attribute, tripwire)
    assert validate(world, copy)["integrity_result"] == PASS


def test_the_file_is_unchanged_and_no_side_file_appears(world, copy):
    before = independent_fingerprint(copy)
    assert before[1] == {suffix: None for suffix in SIDE_SUFFIXES}

    evidence = validate(world, copy)

    assert independent_fingerprint(copy) == before
    assert evidence["database"]["before"] == evidence["database"]["after"]
    assert evidence["database"]["after"]["side_files"] == {suffix: {"exists": False} for suffix in SIDE_SUFFIXES}
    assert check(evidence, "DATABASE_UNCHANGED")["integrity"] == PASS


def test_a_missing_profile_is_refused_and_the_file_is_left_as_it_was(world, copy):
    before = independent_fingerprint(copy)

    with pytest.raises(OperationalStateError) as refused:
        upstream.validate_upstream_state(copy, 999_999, source_registry_path=world["registry"])

    assert refused.value.code == "PROFILE_NOT_FOUND"
    assert independent_fingerprint(copy) == before


# --------------------------------------------------------------------------
# corruption and drift, reported and never repaired
# --------------------------------------------------------------------------


def test_a_malformed_persisted_classification_fails_decoding_and_the_matching_boundary(world, copy):
    matched = world["ids"]["strong"]
    damage(copy, ("UPDATE opportunity_qualifications SET qualification = 'OUT_OF_SCOPE', fine_primary_category = 'NLP' "
                  "WHERE opportunity_id = ?", (matched,)))
    before = independent_fingerprint(copy)

    evidence = validate(world, copy)

    assert evidence["integrity_result"] == FAIL
    assert check(evidence, "PHASE8_PERSISTED_DECODING")["evidence"]["malformed_codes"] == {"INVALID_FINE_CLASSIFICATION": 1}
    assert evidence["phase8"]["decoding"]["malformed_opportunity_ids"] == [matched]
    assert evidence["matching_boundary"]["matched_with_malformed_qualification_count"] == 1
    assert check(evidence, "MATCHING_UPSTREAM_BOUNDARY")["integrity"] == FAIL
    assert independent_fingerprint(copy) == before


def test_input_drift_is_stale_and_passes(world, copy):
    damage(copy, ("UPDATE opportunities SET description = description || ' TEST ONLY edit' WHERE id = ?",
                  (world["ids"]["strong"],)))

    evidence = validate(world, copy)

    currency = check(evidence, "PHASE8_INPUT_CURRENCY")
    assert (currency["integrity"], currency["operational_state"]) == (PASS, STALE)
    assert currency["evidence"]["input_fingerprint_drift_count"] == 1
    assert evidence["matching_boundary"]["matched_with_not_current_qualification_count"] == 1
    assert evidence["integrity_result"] == PASS


def test_a_matched_posting_without_its_qualification_row_fails_the_boundary(world, copy):
    matched = world["ids"]["strong"]
    damage(copy, ("DELETE FROM opportunity_qualifications WHERE opportunity_id = ?", (matched,)))

    evidence = validate(world, copy)

    boundary = check(evidence, "MATCHING_UPSTREAM_BOUNDARY")
    assert (boundary["integrity"], boundary["evidence"]["matched_without_qualification_row_count"]) == (FAIL, 1)
    coverage = check(evidence, "PHASE8_QUALIFICATION_COVERAGE")
    assert (coverage["integrity"], coverage["operational_state"]) == (PASS, STALE)


def test_a_matching_reference_to_a_missing_opportunity_is_an_orphan(world, copy):
    matched = world["ids"]["strong"]
    damage(copy, ("DELETE FROM opportunities WHERE id = ?", (matched,)))

    evidence = validate(world, copy)

    boundary = check(evidence, "MATCHING_UPSTREAM_BOUNDARY")
    assert (boundary["integrity"], boundary["evidence"]["matched_orphan_count"]) == (FAIL, 1)
    assert evidence["matching_boundary"]["matched_orphan_ids"] == [matched]
    assert evidence["integrity_result"] == FAIL


def test_an_untraced_opportunity_fails_collection_traceability(world, copy):
    damage(copy, ("DELETE FROM opportunity_sources WHERE opportunity_id = ?", (world["ids"]["blocked"],)))

    check_ = check(validate(world, copy), "COLLECTION_OPPORTUNITY_TRACEABILITY")

    assert (check_["integrity"], check_["evidence"]["in_scope_without_source_link_count"]) == (FAIL, 1)


def test_a_tombstone_without_an_applied_merge_fails_dedup(world, copy):
    damage(copy, ("UPDATE opportunities SET status = 'merged_duplicate', is_active = 0 WHERE id = ?",
                  (world["ids"]["blocked"],)))

    dedup = check(validate(world, copy), "DEDUP_PERSISTED_STATE")

    assert (dedup["integrity"], dedup["evidence"]["tombstones_without_applied_merge"]) == (FAIL, 1)


def test_an_accepted_fact_without_provenance_fails_the_digital_twin(world, copy):
    damage(copy, (
        """DELETE FROM profile_fact_provenance WHERE fact_id = (
               SELECT id FROM profile_facts WHERE profile_id = ? AND status = 'ACCEPTED' ORDER BY id LIMIT 1)""",
        (world["profile_id"],),
    ))

    twin = check(validate(world, copy), "DIGITAL_TWIN_FACT_PROVENANCE")

    assert (twin["integrity"], twin["evidence"]["accepted_fact_provenance_consistent"]) == (FAIL, False)


def test_an_unsynchronized_projection_is_stale_and_passes(world, copy):
    damage(copy, (
        """UPDATE profile_facts SET status = 'REJECTED' WHERE id = (
               SELECT fact_id FROM profile_mobility WHERE profile_id = ?)""",
        (world["profile_id"],),
    ))

    evidence = validate(world, copy)

    projections = check(evidence, "DIGITAL_TWIN_PROJECTIONS")
    assert (projections["integrity"], projections["operational_state"]) == (PASS, STALE)
    assert evidence["projections"]["explicit_input"]["mobility"]["backed_by_accepted_user_input_fact"] is False


# --------------------------------------------------------------------------
# the CLI
# --------------------------------------------------------------------------


def test_the_cli_prints_deterministic_evidence_and_exits_by_integrity_only(world, copy, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(copy))
    monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", str(world["profile_id"]))

    def validate_with_registry(path, profile_id):
        return upstream.validate_upstream_state(path, profile_id, source_registry_path=world["registry"])

    first = cli.main(validate=validate_with_registry)
    first_output = capsys.readouterr().out
    second = cli.main(validate=validate_with_registry)
    second_output = capsys.readouterr().out

    # NEVER_RUN and NOT_DEMONSTRATED are present, and the exit code is still zero.
    assert (first, second) == (cli.EXIT_PASS, cli.EXIT_PASS)
    assert first_output == second_output
    parsed = json.loads(first_output)
    assert parsed["collection"]["configured_never_run_source_ids"] == ["test_only_never_run"]
    assert "DEDUP_PERSISTED_STATE" in parsed["demonstrability"][NOT_DEMONSTRATED]

    damage(copy, ("DELETE FROM opportunity_sources WHERE opportunity_id = ?", (world["ids"]["blocked"],)))
    assert cli.main(validate=validate_with_registry) == cli.EXIT_FAIL
