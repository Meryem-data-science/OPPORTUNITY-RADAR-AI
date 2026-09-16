"""Phase 11.2B: unit coverage for the read-only upstream chain validator.

`build_checks` is pure over an evidence dictionary, so the status model is
tested directly on shapes like the ones the readers produce. The strict Phase 8
row decoder is tested on raw tuples. Owners are replaced by fakes where the
question is what the validator lets out of them. The real owners and a migrated
SQLite file are exercised in the integration test.
"""

import ast
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.collector.qualification.classifier import CLASSIFIER_VERSION
from services.collector.qualification.fine_classifier import FINE_CLASSIFIER_VERSION
from services.collector.qualification.persistence import input_fingerprint
from services.digital_twin.facts.models import FactSourceType, FactStatus
from services.final_validation import upstream_cli as cli
from services.final_validation import upstream_state as upstream
from services.final_validation.operational_state import FAIL, PASS, OperationalStateError
from services.final_validation.upstream_state import (
    BLOCKER,
    DEMONSTRATED,
    INFORMATIONAL,
    NEVER_RUN,
    NOT_ASSESSED,
    NOT_DEMONSTRATED,
    READY,
    STALE,
    UNKNOWN,
    WARNING,
)

MODULE_PATH = Path(upstream.__file__)
SECRET = "TEST-ONLY-secret-detail"
SECRET_HASH = hashlib.sha256(b"TEST-ONLY cv").hexdigest()


# --------------------------------------------------------------------------
# a healthy evidence dictionary, shaped like the readers' output
# --------------------------------------------------------------------------


def healthy():
    return {
        "database": {"query_only": 1, "before_stable": True, "after_stable": True, "identical": True, "unchanged": True},
        "digital_twin": {
            "available": True, "error_code": None, "owner_user_present": True,
            "fact_status_counts": {"ACCEPTED": 3, "REJECTED": 1},
            "facts_without_provenance_by_status": {},
            "accepted_facts_without_provenance": 0, "accepted_fact_provenance_consistent": True,
            "corrected_facts_without_resolvable_replacement": 0, "distinct_cv_fingerprint_count": 1,
        },
        "projections": {
            "available": True, "error_code": None,
            "structured": {"experiences": {"row_count": 1, "rows_not_backed_by_accepted_fact": 0}},
            "skills": {"skill_count": 2, "evidence_count": 2, "evidence_not_backed_by_accepted_fact": 0},
            "explicit_input": {"mobility": {"stated": True, "backed_by_accepted_user_input_fact": True}},
            "unbacked_projection_row_count": 0,
        },
        "targeting": {
            "available": True, "error_code": None, "persisted_target_exists": False,
            "geography": {"known": True, "country_code": "ZZ", "rule_id": "TEST-ONLY-geo-rule"},
            "opportunity_type": {"known": True, "opportunity_types": ["INTERNSHIP"], "rule_id": "TEST-ONLY-type-rule"},
        },
        "collection": {
            "available": True, "error_code": None, "run_status_counts": {"SUCCESS": 2},
            "configured_never_run_source_ids": [], "runs_without_persisted_source_count": 0,
            "freshness_claim": NOT_ASSESSED,
        },
        "traceability": {
            "in_scope_opportunity_count": 5, "in_scope_without_source_link_count": 0,
            "links_to_unpersisted_source_count": 0, "links_to_missing_opportunity_count": 0,
        },
        "dedup": {
            "available": True, "error_code": None, "decision_count": 1, "merge_count": 1, "applied_merge_count": 1,
            "applied_merges_with_incoherent_tombstone": 0, "tombstones_without_applied_merge": 0,
            "in_scope_duplicate_canonical_url_group_count": 0, "end_to_end_merge_claim": DEMONSTRATED,
        },
        "phase8": {
            "coverage": {
                "in_scope_opportunity_count": 5, "in_scope_with_qualification_row": 5,
                "in_scope_without_qualification_row": 0, "orphan_qualification_rows": 0,
            },
            "decoding": {"decoded_row_count": 5, "malformed_row_count": 0, "malformed_codes": {}},
            "currency": {
                "compared_row_count": 5, "input_fingerprint_drift_count": 0, "classifier_version_drift_count": 0,
                "fine_classifier_version_drift_count": 0, "not_current_count": 0,
            },
        },
        "matching_boundary": {
            "available": True, "error_code": None, "status": "READY", "current_run_id": 7,
            "current_run": {"run_id": 7}, "matched_opportunity_count": 3, "matched_orphan_count": 0,
            "matched_without_qualification_row_count": 0, "matched_with_malformed_qualification_count": 0,
            "matched_outside_current_scope_count": 0, "matched_with_not_current_qualification_count": 0,
            "cohort_membership_freshness": NOT_ASSESSED,
        },
    }


def checks_of(evidence):
    return {item["name"]: item for item in upstream.build_checks(evidence)}


def test_a_healthy_evidence_passes_every_check_in_the_declared_order():
    checks = upstream.build_checks(healthy())

    assert [item["name"] for item in checks] == list(upstream.CHECK_ORDER)
    assert {item["integrity"] for item in checks} == {PASS}
    for item in checks:
        assert set(item) == {"name", "integrity", "demonstrability", "operational_state", "severity", "evidence"}
        assert item["severity"] == INFORMATIONAL, item


def test_every_dimension_keeps_its_own_vocabulary():
    for item in upstream.build_checks(healthy()):
        assert item["integrity"] in (PASS, FAIL)
        assert item["demonstrability"] in (DEMONSTRATED, NOT_DEMONSTRATED, NOT_ASSESSED)
        assert item["severity"] in (BLOCKER, WARNING, INFORMATIONAL)


def test_no_score_or_aggregate_verdict_is_invented():
    source = MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in ("quality_score", "validation_score", "global_score", "overall_score", "confidence"):
        assert forbidden not in source
    serialized = json.dumps(upstream.build_checks(healthy()))
    assert "score" not in serialized


# --------------------------------------------------------------------------
# NOT_DEMONSTRATED, NEVER_RUN, STALE and UNKNOWN are not FAIL
# --------------------------------------------------------------------------


def test_a_configured_source_that_never_ran_is_a_warning_and_not_a_failure():
    evidence = healthy()
    evidence["collection"]["configured_never_run_source_ids"] = ["test_only_never_run"]

    check = checks_of(evidence)["COLLECTION_SOURCES"]

    assert check["integrity"] == PASS
    assert check["severity"] == WARNING
    assert check["evidence"]["configured_never_run_source_ids"] == ["test_only_never_run"]


def test_no_successful_run_at_all_is_not_demonstrated_and_still_passes():
    evidence = healthy()
    evidence["collection"]["run_status_counts"] = {}

    check = checks_of(evidence)["COLLECTION_SOURCES"]

    assert (check["integrity"], check["demonstrability"]) == (PASS, NOT_DEMONSTRATED)


def test_a_run_without_its_persisted_source_fails_collection():
    evidence = healthy()
    evidence["collection"]["runs_without_persisted_source_count"] = 1

    assert checks_of(evidence)["COLLECTION_SOURCES"]["integrity"] == FAIL


def test_no_real_merge_is_not_demonstrated_and_never_a_failure():
    evidence = healthy()
    evidence["dedup"].update(decision_count=0, merge_count=0, applied_merge_count=0, end_to_end_merge_claim=NOT_DEMONSTRATED)

    check = checks_of(evidence)["DEDUP_PERSISTED_STATE"]

    assert (check["integrity"], check["demonstrability"], check["severity"]) == (PASS, NOT_DEMONSTRATED, WARNING)


@pytest.mark.parametrize("field", ["applied_merges_with_incoherent_tombstone", "tombstones_without_applied_merge"])
def test_an_incoherent_persisted_merge_fails_dedup(field):
    evidence = healthy()
    evidence["dedup"][field] = 1

    check = checks_of(evidence)["DEDUP_PERSISTED_STATE"]

    assert (check["integrity"], check["severity"]) == (FAIL, BLOCKER)


def test_an_unknown_target_is_not_demonstrated_and_not_false():
    evidence = healthy()
    evidence["targeting"]["geography"].update(known=False, country_code=None, rule_id="TEST-ONLY-absent")

    check = checks_of(evidence)["PROFILE_TARGETING"]

    assert (check["integrity"], check["demonstrability"], check["operational_state"]) == (PASS, NOT_DEMONSTRATED, UNKNOWN)


def test_an_unsynchronized_projection_is_stale_not_failed():
    evidence = healthy()
    evidence["projections"]["unbacked_projection_row_count"] = 2

    check = checks_of(evidence)["DIGITAL_TWIN_PROJECTIONS"]

    assert (check["integrity"], check["operational_state"]) == (PASS, STALE)


@pytest.mark.parametrize("field, value", [
    ("accepted_facts_without_provenance", 1),
    ("corrected_facts_without_resolvable_replacement", 1),
    ("owner_user_present", False),
])
def test_a_broken_fact_relationship_fails_the_digital_twin(field, value):
    evidence = healthy()
    evidence["digital_twin"][field] = value

    assert checks_of(evidence)["DIGITAL_TWIN_FACT_PROVENANCE"]["integrity"] == FAIL


def test_an_unaccepted_fact_without_provenance_is_flagged_but_does_not_fail():
    evidence = healthy()
    evidence["digital_twin"]["facts_without_provenance_by_status"] = {"REJECTED": 1}

    check = checks_of(evidence)["DIGITAL_TWIN_FACT_PROVENANCE"]

    assert (check["integrity"], check["severity"]) == (PASS, WARNING)


@pytest.mark.parametrize("part, name", [
    ("digital_twin", "DIGITAL_TWIN_FACT_PROVENANCE"),
    ("projections", "DIGITAL_TWIN_PROJECTIONS"),
    ("targeting", "PROFILE_TARGETING"),
    ("collection", "COLLECTION_SOURCES"),
    ("dedup", "DEDUP_PERSISTED_STATE"),
    ("matching_boundary", "MATCHING_UPSTREAM_BOUNDARY"),
])
def test_an_owner_refusal_fails_integrity_and_is_not_assessed(part, name):
    evidence = healthy()
    evidence[part] = {"available": False, "error_code": "TEST_ONLY_ERROR"}

    check = checks_of(evidence)[name]

    assert (check["integrity"], check["demonstrability"]) == (FAIL, NOT_ASSESSED)
    assert check["evidence"] == {"error_code": "TEST_ONLY_ERROR"}


# --------------------------------------------------------------------------
# NOT_ASSESSED
# --------------------------------------------------------------------------


def test_recomputation_dimensions_are_declared_not_assessed():
    checks = upstream.build_checks(healthy())
    summary = upstream.demonstrability_summary(checks)

    names = {name for name, _ in upstream.NOT_ASSESSED_DIMENSIONS}
    assert {
        "MATCHING_COHORT_MEMBERSHIP_FRESHNESS", "MATCHING_ASSESSMENT_RECOMPUTATION",
        "PHASE8_CLASSIFICATION_CORRECTNESS", "DEDUP_CANDIDATE_DETECTION", "COLLECTION_FRESHNESS",
    } <= names
    assert names <= set(summary[NOT_ASSESSED])
    assert checks_of(healthy())["MATCHING_UPSTREAM_BOUNDARY"]["evidence"]["cohort_membership_freshness"] == NOT_ASSESSED
    assert checks_of(healthy())["COLLECTION_SOURCES"]["evidence"]["freshness_claim"] == NOT_ASSESSED


# --------------------------------------------------------------------------
# Phase 8 coverage and currency
# --------------------------------------------------------------------------


def test_an_uncovered_in_scope_opportunity_is_stale_and_not_demonstrated():
    evidence = healthy()
    evidence["phase8"]["coverage"].update(in_scope_with_qualification_row=4, in_scope_without_qualification_row=1)

    check = checks_of(evidence)["PHASE8_QUALIFICATION_COVERAGE"]

    assert (check["integrity"], check["demonstrability"], check["operational_state"]) == (PASS, NOT_DEMONSTRATED, STALE)


def test_an_orphan_qualification_row_fails_coverage():
    evidence = healthy()
    evidence["phase8"]["coverage"]["orphan_qualification_rows"] = 1

    assert checks_of(evidence)["PHASE8_QUALIFICATION_COVERAGE"]["integrity"] == FAIL


def test_a_malformed_persisted_row_fails_decoding():
    evidence = healthy()
    evidence["phase8"]["decoding"].update(malformed_row_count=1, malformed_codes={"INVALID_QUALIFICATION": 1})

    assert checks_of(evidence)["PHASE8_PERSISTED_DECODING"]["integrity"] == FAIL


def test_fingerprint_drift_is_stale_and_never_a_failure():
    evidence = healthy()
    evidence["phase8"]["currency"].update(input_fingerprint_drift_count=2, not_current_count=2)

    check = checks_of(evidence)["PHASE8_INPUT_CURRENCY"]

    assert (check["integrity"], check["demonstrability"], check["operational_state"]) == (PASS, DEMONSTRATED, STALE)


def test_zero_drift_is_ready():
    assert checks_of(healthy())["PHASE8_INPUT_CURRENCY"]["operational_state"] == READY


# --------------------------------------------------------------------------
# Matching boundary
# --------------------------------------------------------------------------


@pytest.mark.parametrize("field", [
    "matched_orphan_count", "matched_without_qualification_row_count", "matched_with_malformed_qualification_count",
])
def test_a_matching_reference_without_its_upstream_identity_fails(field):
    evidence = healthy()
    evidence["matching_boundary"][field] = 1

    check = checks_of(evidence)["MATCHING_UPSTREAM_BOUNDARY"]

    assert (check["integrity"], check["demonstrability"], check["severity"]) == (FAIL, NOT_DEMONSTRATED, BLOCKER)


@pytest.mark.parametrize("field", ["matched_outside_current_scope_count", "matched_with_not_current_qualification_count"])
def test_a_matched_posting_that_moved_upstream_is_a_warning_not_a_membership_judgement(field):
    evidence = healthy()
    evidence["matching_boundary"][field] = 1

    check = checks_of(evidence)["MATCHING_UPSTREAM_BOUNDARY"]

    assert (check["integrity"], check["demonstrability"], check["severity"]) == (PASS, DEMONSTRATED, WARNING)


@pytest.mark.parametrize("status", ["NOT_SYNCED", "EMPTY"])
def test_no_current_matching_run_is_not_demonstrated_and_passes(status):
    evidence = healthy()
    evidence["matching_boundary"].update(status=status, current_run_id=None, current_run=None)

    check = checks_of(evidence)["MATCHING_UPSTREAM_BOUNDARY"]

    assert (check["integrity"], check["demonstrability"], check["operational_state"]) == (PASS, NOT_DEMONSTRATED, status)


def test_database_and_query_only_failures_fail_integrity():
    evidence = healthy()
    evidence["database"].update(query_only=0, unchanged=False, identical=False)

    checks = checks_of(evidence)

    assert checks["SQLITE_QUERY_ONLY"]["integrity"] == FAIL
    assert checks["DATABASE_UNCHANGED"]["integrity"] == FAIL


# --------------------------------------------------------------------------
# strict persisted Phase 8 decoding
# --------------------------------------------------------------------------


INPUTS = ("TEST ONLY title", "TEST ONLY description", "https://example.invalid/1", None, None)


def qualification_row(**overrides):
    values = {
        "present": 1,
        "qualification": "CORE_TARGET", "primary_domain": "DATA_SCIENCE", "opportunity_type": "INTERNSHIP",
        "employment_type": "UNKNOWN", "listing_quality": "NORMAL_LISTING",
        "matched_domains_json": '["DATA_SCIENCE"]', "matched_title_signals_json": "[]",
        "matched_description_signals_json": "[]", "matched_exclusion_signals_json": "[]", "reasons_json": '["r"]',
        "classifier_version": CLASSIFIER_VERSION, "input_fingerprint": input_fingerprint(*INPUTS),
        "fine_primary_category": "OTHER", "fine_secondary_categories_json": "[]",
        "fine_category_evidence_json": "[]", "fine_reasons_json": '["r"]',
        "fine_classifier_version": FINE_CLASSIFIER_VERSION,
    }
    values.update(overrides)
    return (41, *INPUTS, *values.values())


def test_a_persisted_row_is_decoded_exactly_as_written():
    read = upstream.decode_qualification_row(qualification_row())

    assert read.malformed_code is None
    assert (read.qualification, read.primary_domain, read.opportunity_type) == ("CORE_TARGET", "DATA_SCIENCE", "INTERNSHIP")
    assert (read.fine_state, read.fine_primary_category) == ("CLASSIFIED_WITH_CATEGORY", "OTHER")
    assert read.current is True


def test_an_absent_row_is_absent_and_not_malformed():
    read = upstream.decode_qualification_row((41, *INPUTS, 0, *([None] * 17)))

    assert (read.present, read.malformed_code, read.current) == (False, None, None)


def test_a_legacy_row_without_fine_classification_is_unclassified_and_not_current():
    read = upstream.decode_qualification_row(qualification_row(
        fine_primary_category=None, fine_secondary_categories_json=None, fine_category_evidence_json=None,
        fine_reasons_json=None, fine_classifier_version=None,
    ))

    assert (read.malformed_code, read.fine_state, read.current) == (None, "UNCLASSIFIED", False)


@pytest.mark.parametrize("overrides, code", [
    ({"qualification": "TEST_ONLY_NOT_A_QUALIFICATION"}, "INVALID_QUALIFICATION"),
    ({"primary_domain": "TEST_ONLY"}, "INVALID_PRIMARY_DOMAIN"),
    ({"opportunity_type": "TEST_ONLY"}, "INVALID_OPPORTUNITY_TYPE"),
    ({"employment_type": None}, "INVALID_EMPLOYMENT_TYPE"),
    ({"listing_quality": "TEST_ONLY"}, "INVALID_LISTING_QUALITY"),
    ({"matched_domains_json": "{not json"}, "INVALID_MATCHED_DOMAINS_JSON"),
    ({"reasons_json": '{"a": 1}'}, "INVALID_REASONS_JSON"),
    ({"matched_title_signals_json": "[1]"}, "INVALID_MATCHED_TITLE_SIGNALS_JSON"),
    ({"classifier_version": " "}, "INVALID_CLASSIFIER_VERSION"),
    # Phase 8's own coherence rule: an OUT_OF_SCOPE row never carries a category.
    ({"qualification": "OUT_OF_SCOPE", "fine_primary_category": "NLP"}, "INVALID_FINE_CLASSIFICATION"),
    ({"fine_secondary_categories_json": "not json"}, "INVALID_FINE_CLASSIFICATION"),
])
def test_a_malformed_persisted_row_is_refused_with_a_stable_code(overrides, code):
    read = upstream.decode_qualification_row(qualification_row(**overrides))

    assert read.malformed_code == code
    assert read.current is None


def test_input_fingerprint_drift_is_detected_with_the_owner_function():
    read = upstream.decode_qualification_row(qualification_row(input_fingerprint="0" * 64))

    assert (read.fingerprint_current, read.classifier_version_current, read.current) == (False, True, False)


def test_a_version_other_than_the_running_one_is_not_current():
    read = upstream.decode_qualification_row(qualification_row(classifier_version="TEST-ONLY-old-rules"))

    assert (read.malformed_code, read.classifier_version_current, read.current) == (None, False, False)


def test_decoding_never_reaches_a_classifier(monkeypatch):
    def tripwire(*_args, **_kwargs):
        raise AssertionError("the upstream validator classified")

    import services.collector.qualification.classifier as classifier
    import services.collector.qualification.fine_classifier as fine_classifier

    monkeypatch.setattr(classifier, "classify_opportunity", tripwire)
    monkeypatch.setattr(fine_classifier, "classify_fine_categories", tripwire)
    assert upstream.decode_qualification_row(qualification_row()).malformed_code is None


# --------------------------------------------------------------------------
# privacy
# --------------------------------------------------------------------------


def fact(fact_id, status, fact_type="EMAIL", replaced_by=None):
    return SimpleNamespace(id=fact_id, status=status, fact_type=fact_type, value=SECRET, replaced_by_fact_id=replaced_by)


def provenance(source_type, cv_sha256=None):
    return SimpleNamespace(source_type=source_type, cv_sha256=cv_sha256, source_locator=SECRET, provenance_key=SECRET)


class FakeConnection:
    def execute(self, *_args):
        return SimpleNamespace(fetchone=lambda: (1,), fetchall=lambda: [])


def test_digital_twin_evidence_carries_counts_and_never_a_value_or_the_cv_hash(monkeypatch):
    facts = (
        fact(1, FactStatus.ACCEPTED), fact(2, FactStatus.CORRECTED, replaced_by=3),
        fact(3, FactStatus.ACCEPTED, "PHONE"), fact(4, FactStatus.REJECTED),
    )
    rows = {
        1: (provenance(FactSourceType.CV, SECRET_HASH),),
        2: (provenance(FactSourceType.CV, SECRET_HASH),),
        3: (provenance(FactSourceType.USER_INPUT),),
        4: (),
    }
    monkeypatch.setattr(upstream, "list_profile_facts", lambda _c, _p: facts)
    monkeypatch.setattr(upstream, "list_profile_fact_provenance", lambda _c, _p, fact_id: rows[fact_id])

    evidence = upstream.digital_twin_evidence(FakeConnection(), 5)

    serialized = json.dumps(evidence)
    assert SECRET not in serialized and SECRET_HASH not in serialized
    assert evidence["distinct_cv_fingerprint_count"] == 1
    assert evidence["accepted_fact_provenance_consistent"] is True
    assert evidence["facts_without_provenance_by_status"] == {"REJECTED": 1}
    assert evidence["fact_status_counts"] == {"ACCEPTED": 2, "CORRECTED": 1, "REJECTED": 1}
    assert evidence["corrected_facts_without_resolvable_replacement"] == 0


def test_a_corrected_fact_pointing_nowhere_is_counted(monkeypatch):
    monkeypatch.setattr(upstream, "list_profile_facts", lambda _c, _p: (fact(2, FactStatus.CORRECTED, replaced_by=99),))
    monkeypatch.setattr(upstream, "list_profile_fact_provenance", lambda *_a: (provenance(FactSourceType.CV),))

    evidence = upstream.digital_twin_evidence(FakeConnection(), 5)

    assert evidence["corrected_facts_without_resolvable_replacement"] == 1
    assert evidence["cv_provenance_without_fingerprint_count"] == 1


def test_an_owner_refusal_is_reduced_to_its_type_name(monkeypatch):
    def refuse(*_args):
        raise ValueError(SECRET)

    monkeypatch.setattr(upstream, "list_profile_facts", refuse)

    evidence = upstream.digital_twin_evidence(FakeConnection(), 5)

    assert evidence["available"] is False and evidence["error_code"] == "ValueError"
    assert SECRET not in json.dumps(evidence)


class RunConnection:
    def __init__(self, sources, runs):
        self.sources, self.runs = sources, runs

    def execute(self, statement, *_args):
        rows = [(item,) for item in self.sources] if "FROM sources" in statement else self.runs
        return SimpleNamespace(fetchall=lambda: rows)


def health(source_id, status=None, last_run_at=None):
    return SimpleNamespace(
        source_id=source_id, enabled=True, status=status, last_run_at=last_run_at, items_found=None,
        anomaly_code=None, anomaly_message=SECRET, error_type=SECRET, error_message=SECRET,
        has_run=status is not None,
    )


def test_collection_reports_never_run_honestly_and_never_an_error_message(monkeypatch, tmp_path):
    configured = [
        SimpleNamespace(id="test_only_ran", type="greenhouse", country=None, organization=SECRET),
        SimpleNamespace(id="test_only_never_run", type="stage_ma_html", country="ZZ", organization=SECRET),
    ]
    monkeypatch.setattr(upstream, "load_source_registry", lambda _path: configured)
    monkeypatch.setattr(upstream, "read_source_health", lambda _c, configured_sources: [
        health("test_only_never_run"),
        health("test_only_ran", "FAILED", "2099-01-02T00:00:00+00:00"),
    ])
    runs = [
        ("test_only_ran", "SUCCESS", "2099-01-01T00:00:00+00:00", "2099-01-01T00:00:05+00:00"),
        ("test_only_ran", "FAILED", "2099-01-02T00:00:00+00:00", "2099-01-02T00:00:01Z"),
        ("test_only_ran", "RUNNING", "not a timestamp", None),
    ]

    evidence = upstream.collection_evidence(RunConnection(["test_only_ran"], runs), tmp_path / "x.yaml")

    assert SECRET not in json.dumps(evidence)
    by_id = {item["source_id"]: item for item in evidence["sources"]}
    assert by_id["test_only_never_run"]["operational_state"] == NEVER_RUN
    assert by_id["test_only_never_run"]["persisted"] is False
    assert by_id["test_only_ran"]["operational_state"] == "FAILED"
    assert evidence["configured_never_run_source_ids"] == ["test_only_never_run"]
    assert evidence["newest_run_timestamp_raw"] == "2099-01-02T00:00:01Z"
    assert evidence["unparseable_or_naive_run_timestamp_count"] == 1
    assert evidence["freshness_claim"] == NOT_ASSESSED


def test_an_unreadable_source_registry_is_an_owner_refusal(monkeypatch, tmp_path):
    evidence = upstream.collection_evidence(RunConnection([], []), tmp_path / "TEST-ONLY-missing.yaml")

    assert evidence == {"available": False, "error_code": "SourceConfigurationError"}


# --------------------------------------------------------------------------
# static guarantees
# --------------------------------------------------------------------------


FORBIDDEN_NAMES = {
    "classify_opportunity", "classify_fine_categories", "persist_qualifications", "audit_database",
    "sync_matching", "select_matching_opportunity_ids", "build_matching_assessments",
    "sync_recommendations", "assemble_recommendation_inputs", "audit_geographic_targeting",
    "synchronize_location_resolutions", "audit_opportunity_type_targeting", "stage_candidates",
    "audit_opportunities", "apply_merge", "start_source_run", "persist_opportunities",
    "synchronize_profile_preferences", "synchronize_profile_skills", "synchronize_structured_profile_entries",
    "connect_database", "connect_configured_database", "apply_migrations", "resolve_location_text",
}


@pytest.mark.parametrize("path", [MODULE_PATH, Path(cli.__file__)])
def test_no_computing_writing_or_synchronizing_name_is_imported_or_called(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    assert not names & FORBIDDEN_NAMES


def test_no_write_statement_appears_in_the_module():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            words = set(node.value.upper().replace("(", " ").split())
            assert not words & {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "COMMIT", "REPLACE"}, node.value


def test_no_country_or_profile_value_is_hardcoded():
    source = MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in ('"MA"', "'MA'", "Maroc", "Morocco", '"PFE"', '"INTERNSHIP"', "stagiaires_ma", "stage_ma", "Data & AI"):
        assert forbidden not in source


# --------------------------------------------------------------------------
# CLI exit semantics
# --------------------------------------------------------------------------


@pytest.fixture
def environment(monkeypatch, tmp_path):
    database = tmp_path / "TEST-ONLY.db"
    database.write_bytes(b"")
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(database))
    monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", "3")
    return database


def run_cli(evidence=None, error=None):
    calls = []

    def validate(path, profile_id):
        calls.append((path, profile_id))
        if error is not None:
            raise error
        return evidence

    stdout, stderr = io.StringIO(), io.StringIO()
    code = cli.main(stdout=stdout, stderr=stderr, validate=validate)
    return code, stdout.getvalue(), stderr.getvalue(), calls


def evidence_with(checks):
    result = PASS if all(item["integrity"] == PASS for item in checks) else FAIL
    return {"schema_version": upstream.SCHEMA_VERSION, "checks": checks, "integrity_result": result}


def test_the_cli_exits_zero_when_integrity_passes(environment):
    checks = upstream.build_checks(healthy())
    code, stdout, stderr, calls = run_cli(evidence_with(checks))

    assert (code, stderr) == (cli.EXIT_PASS, "")
    assert calls == [(environment, 3)]
    assert json.loads(stdout)["integrity_result"] == PASS


def test_not_demonstrated_never_run_stale_and_unknown_do_not_change_the_exit_code(environment):
    evidence = healthy()
    evidence["collection"]["configured_never_run_source_ids"] = ["test_only_never_run"]
    evidence["collection"]["run_status_counts"] = {}
    evidence["dedup"].update(merge_count=0, applied_merge_count=0, end_to_end_merge_claim=NOT_DEMONSTRATED)
    evidence["targeting"]["geography"]["known"] = False
    evidence["phase8"]["currency"]["not_current_count"] = 3
    evidence["matching_boundary"].update(status="NOT_SYNCED", current_run=None, current_run_id=None)
    checks = upstream.build_checks(evidence)
    assert {item["integrity"] for item in checks} == {PASS}

    code, _stdout, _stderr, _calls = run_cli(evidence_with(checks))

    assert code == cli.EXIT_PASS


def test_the_cli_exits_one_on_an_integrity_failure(environment):
    evidence = healthy()
    evidence["matching_boundary"]["matched_orphan_count"] = 1

    code, stdout, _stderr, _calls = run_cli(evidence_with(upstream.build_checks(evidence)))

    assert code == cli.EXIT_FAIL
    assert json.loads(stdout)["integrity_result"] == FAIL


def test_the_cli_exits_two_with_a_stable_code_when_validation_cannot_run(environment):
    code, stdout, stderr, _calls = run_cli(error=OperationalStateError("PROFILE_NOT_FOUND"))

    assert (code, stdout) == (cli.EXIT_ERROR, "")
    assert json.loads(stderr) == {"error_code": "PROFILE_NOT_FOUND", "result": "ERROR", "schema_version": upstream.SCHEMA_VERSION}


@pytest.mark.parametrize("variable, value, code", [
    ("DATABASE_BACKEND", "turso", "SQLITE_BACKEND_REQUIRED"),
    ("SQLITE_DATABASE_PATH", "", "SQLITE_DATABASE_PATH_REQUIRED"),
    ("OPPORTUNITY_RADAR_PROFILE_ID", None, "PROFILE_ID_REQUIRED"),
    ("OPPORTUNITY_RADAR_PROFILE_ID", "01", "INVALID_PROFILE_ID"),
])
def test_the_cli_refuses_incomplete_configuration_without_validating(environment, monkeypatch, variable, value, code):
    if value is None:
        monkeypatch.delenv(variable)
    else:
        monkeypatch.setenv(variable, value)

    result, _stdout, stderr, calls = run_cli(evidence_with([]))

    assert (result, calls) == (cli.EXIT_ERROR, [])
    assert json.loads(stderr)["error_code"] == code
