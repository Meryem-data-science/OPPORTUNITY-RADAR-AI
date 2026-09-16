"""Phase 11.2A: unit coverage for the read-only operational state validator.

The owners are replaced by fakes returning shapes like their own result types,
so each test is about the validator alone: that it quotes what an owner says
without translating it, compares only persisted identities, fails closed, keeps
paths and exception text out of the evidence, and computes no score. The real
owners and a migrated SQLite file are exercised in the integration test.
"""

import ast
from datetime import date
import hashlib
from enum import StrEnum
import io
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from services.final_validation import cli
from services.final_validation import operational_state as state
from services.final_validation.operational_state import (
    FAIL,
    MATCH,
    MISMATCH,
    NOT_APPLICABLE,
    PASS,
    UNAVAILABLE,
    OperationalStateError,
)

MODULE_PATH = Path(state.__file__)
SECRET = "TEST-ONLY-secret-detail"


class Code(StrEnum):
    SOME_ISSUE = "SOME_ISSUE"


# --------------------------------------------------------------------------
# fakes shaped like the owners' result types
# --------------------------------------------------------------------------


def matching_audit(status="READY", current_run_id=4, ok=True, issues=()):
    return SimpleNamespace(
        audit_version="TEST-ONLY-matching-audit", status=status, current_run_id=current_run_id,
        run_count=1, audited_run_count=1, assessment_count=3, ok=ok, issues=tuple(issues),
        audit_fingerprint="a" * 64,
    )


def matching_current(status="READY", run_id=4, run_fingerprint="m" * 64):
    run = None if run_id is None else SimpleNamespace(
        run_id=run_id, run_fingerprint=run_fingerprint, batch_fingerprint="b" * 64, assessment_count=3
    )
    return SimpleNamespace(
        status=status, current_run_id=run_id, history_count=0 if run_id is None else 1,
        persistence_version=None if run_id is None else "TEST-ONLY-p", selection_version=None, current_run=run,
    )


def recommendation_audit(status="READY", current_run_id=9, ok=True, issues=()):
    return SimpleNamespace(
        audit_version="TEST-ONLY-recommendation-audit", status=status, current_run_id=current_run_id,
        run_count=1, audited_run_count=1, assessment_count=3, ok=ok, issues=tuple(issues),
        audit_fingerprint="c" * 64,
    )


def recommendation_current(status="READY", run_id=9, source_run=4, source_fingerprint="m" * 64, readiness=()):
    run = None if run_id is None else SimpleNamespace(
        run_id=run_id, source_matching_run_id=source_run, source_matching_run_fingerprint=source_fingerprint,
        run_fingerprint="r" * 64, batch_fingerprint="d" * 64, assessment_count=3,
        recommendation_engine_version="TEST-ONLY-engine", recommendation_rules_version="TEST-ONLY-rules",
    )
    return SimpleNamespace(
        status=status, current_run_id=run_id, history_count=1, persistence_version="TEST-ONLY-p",
        input_assembly_version="TEST-ONLY-i", readiness_issues=tuple(readiness), current_run=run,
    )


def eligibility_audit(unknown_dimensions=None, invariant_violations=None):
    return SimpleNamespace(
        decisions=3, verdicts={"UNKNOWN": 1, "ELIGIBLE": 2}, rule_statuses={"UNKNOWN": 1, "NOT_EVALUATED": 4},
        blocker_dimensions={}, unknown_dimensions=unknown_dimensions or {"LANGUAGE": 1},
        invariant_violations=invariant_violations or {"skill_rule_blocking": 0},
    )


def auxiliary_audit(status="NOT_SYNCED", current_run_id=None, issues=()):
    return SimpleNamespace(status=status, current_run_id=current_run_id, history_count=0, run_audit=None, issues=tuple(issues))


def auxiliary_current(status="NOT_SYNCED", current_run_id=None):
    return SimpleNamespace(status=status, current_run_id=current_run_id, history_count=0, current_run=None)


@pytest.fixture
def owners(monkeypatch):
    """Healthy fakes for every owner; a test replaces the one it is about."""
    fakes = {
        "audit_matching_profile_history": lambda connection, profile_id: matching_audit(),
        "read_current_matching": lambda connection, profile_id: matching_current(),
        "audit_recommendation_profile_history": lambda connection, profile_id: recommendation_audit(),
        "read_current_recommendation": lambda connection, profile_id: recommendation_current(),
        "audit_eligibility": lambda connection, user_id: eligibility_audit(),
        "audit_current_priority": lambda connection, profile_id: auxiliary_audit(),
        "read_current_priority": lambda connection, profile_id: auxiliary_current(),
        "audit_current_portfolio": lambda connection, profile_id: auxiliary_audit(),
        "read_current_portfolio": lambda connection, profile_id: auxiliary_current(),
    }
    for name, fake in fakes.items():
        monkeypatch.setattr(state, name, fake)

    def replace(name, fake):
        monkeypatch.setattr(state, name, fake)

    return replace


@pytest.fixture
def tiny_database(tmp_path):
    """A plain SQLite file with just a profile row; the owners are faked."""
    path = tmp_path / "tiny-operational.db"
    connection = sqlite3.connect(path)
    connection.executescript("CREATE TABLE profiles (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL);"
                             "INSERT INTO profiles VALUES (7, 3);")
    connection.commit()
    connection.close()
    return path


def check(evidence, name):
    return next(item for item in evidence["checks"] if item["name"] == name)


# --------------------------------------------------------------------------
# inputs and configuration
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, -1, True, False, 1.0, "1", None])
def test_invalid_profile_ids_are_rejected(value):
    with pytest.raises(OperationalStateError) as raised:
        state.require_profile_id(value)
    assert raised.value.code == "INVALID_PROFILE_ID"


@pytest.mark.parametrize("raw", ["", "0", "01", "-1", "+1", " 1", "1 ", "1.0", "one", "1e3"])
def test_malformed_profile_text_is_rejected(raw):
    with pytest.raises(OperationalStateError):
        state.parse_profile_id(raw)
    assert state.parse_profile_id("12") == 12


def test_a_missing_database_is_rejected_and_nothing_is_created(tmp_path):
    missing = tmp_path / "absent" / "operational.db"
    with pytest.raises(OperationalStateError) as raised:
        state.validate_operational_state(missing, 1)
    assert raised.value.code == "DATABASE_NOT_FOUND"
    assert not missing.exists() and not missing.parent.exists()


def test_a_directory_is_not_a_database(tmp_path):
    with pytest.raises(OperationalStateError) as raised:
        state.validate_operational_state(tmp_path, 1)
    assert raised.value.code == "DATABASE_NOT_A_FILE"


def run_cli(monkeypatch, environment, validate=None):
    for name in ("DATABASE_BACKEND", "SQLITE_DATABASE_PATH", "OPPORTUNITY_RADAR_PROFILE_ID", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPPORTUNITY_RADAR_ENV", "test")
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    stdout, stderr = io.StringIO(), io.StringIO()
    arguments = {} if validate is None else {"validate": validate}
    code = cli.main(stdout=stdout, stderr=stderr, **arguments)
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.mark.parametrize(
    ("environment", "error_code"),
    [
        ({"DATABASE_BACKEND": "turso", "TURSO_DATABASE_URL": "libsql://x.invalid", "TURSO_AUTH_TOKEN": SECRET,
          "SQLITE_DATABASE_PATH": "x.db", "OPPORTUNITY_RADAR_PROFILE_ID": "1"}, "SQLITE_BACKEND_REQUIRED"),
        ({"SQLITE_DATABASE_PATH": "x.db", "OPPORTUNITY_RADAR_PROFILE_ID": "1"}, "SQLITE_BACKEND_REQUIRED"),
        ({"DATABASE_BACKEND": "sqlite", "OPPORTUNITY_RADAR_PROFILE_ID": "1"}, "SQLITE_DATABASE_PATH_REQUIRED"),
        ({"DATABASE_BACKEND": "sqlite", "SQLITE_DATABASE_PATH": "x.db"}, "PROFILE_ID_REQUIRED"),
        ({"DATABASE_BACKEND": "sqlite", "SQLITE_DATABASE_PATH": "x.db", "OPPORTUNITY_RADAR_PROFILE_ID": "01"}, "INVALID_PROFILE_ID"),
        ({"DATABASE_BACKEND": "sqlite", "SQLITE_DATABASE_PATH": "x.db", "OPPORTUNITY_RADAR_PROFILE_ID": "abc"}, "INVALID_PROFILE_ID"),
    ],
)
def test_the_cli_refuses_incomplete_or_non_sqlite_configuration(monkeypatch, environment, error_code):
    called = []
    code, stdout, stderr = run_cli(monkeypatch, environment, validate=lambda *a: called.append(a))
    assert code == cli.EXIT_ERROR
    assert stdout == ""
    assert json.loads(stderr) == {"error_code": error_code, "result": "ERROR", "schema_version": state.SCHEMA_VERSION}
    assert called == []
    assert SECRET not in stderr and "Traceback" not in stderr


def test_the_cli_reports_a_missing_database_without_its_path(monkeypatch, tmp_path):
    missing = tmp_path / "nowhere" / "operational.db"
    code, stdout, stderr = run_cli(monkeypatch, {
        "DATABASE_BACKEND": "sqlite", "SQLITE_DATABASE_PATH": str(missing), "OPPORTUNITY_RADAR_PROFILE_ID": "5"})
    assert code == cli.EXIT_ERROR and stdout == ""
    assert json.loads(stderr)["error_code"] == "DATABASE_NOT_FOUND"
    assert str(tmp_path) not in stderr and "nowhere" not in stderr
    assert not missing.parent.exists()


@pytest.mark.parametrize(("result", "exit_code"), [(PASS, 0), (FAIL, 1)])
def test_the_cli_exit_code_follows_the_evidence_result(monkeypatch, result, exit_code):
    seen = []

    def validate(path, profile_id):
        seen.append(profile_id)
        return {"result": result, "schema_version": state.SCHEMA_VERSION}

    code, stdout, stderr = run_cli(monkeypatch, {
        "DATABASE_BACKEND": "sqlite", "SQLITE_DATABASE_PATH": "x.db", "OPPORTUNITY_RADAR_PROFILE_ID": "23"}, validate)
    assert (code, stderr, seen) == (exit_code, "", [23])
    assert json.loads(stdout)["result"] == result


# --------------------------------------------------------------------------
# filesystem fingerprint
# --------------------------------------------------------------------------


def test_absent_side_files_are_recorded_as_absent_and_nothing_is_created(tiny_database):
    fingerprint = state.fingerprint_database(tiny_database)
    assert fingerprint["side_files"] == {suffix: {"exists": False} for suffix in ("-wal", "-shm", "-journal")}
    assert fingerprint["main"]["exists"] and len(fingerprint["main"]["sha256"]) == 64
    assert not any(Path(f"{tiny_database}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal"))


def test_present_side_files_are_tracked_by_content_and_left_in_place(tiny_database):
    side = Path(f"{tiny_database}-shm")
    side.write_bytes(b"TEST-ONLY side file")
    first = state.fingerprint_database(tiny_database)
    assert first["side_files"]["-shm"]["exists"] is True
    assert first["side_files"]["-shm"]["size"] == len(b"TEST-ONLY side file")
    assert side.read_bytes() == b"TEST-ONLY side file"
    side.write_bytes(b"TEST-ONLY side filE")
    assert state.fingerprint_database(tiny_database)["side_files"]["-shm"] != first["side_files"]["-shm"]


def test_a_before_after_difference_fails_database_unchanged(owners, tiny_database, monkeypatch):
    real = state.fingerprint_database
    calls = []

    def drifting(path):
        calls.append(path)
        fingerprint = real(path)
        if len(calls) == 2:
            fingerprint["side_files"]["-journal"] = {"exists": True, "is_file": True, "size": 1, "mtime_ns": 1, "sha256": "0" * 64, "stable_during_hash": True}
        return fingerprint

    monkeypatch.setattr(state, "fingerprint_database", drifting)
    evidence = state.validate_operational_state(tiny_database, 7)
    assert evidence["database"]["unchanged"] is False
    assert check(evidence, "DATABASE_UNCHANGED")["status"] == FAIL
    assert evidence["result"] == FAIL


# --------------------------------------------------------------------------
# evidence shape
# --------------------------------------------------------------------------


def test_healthy_fakes_pass_and_the_evidence_is_deterministic(owners, tiny_database):
    first = state.validate_operational_state(tiny_database, 7)
    second = state.validate_operational_state(tiny_database, 7)
    assert state.serialize_evidence(first) == state.serialize_evidence(second)
    assert first["result"] == PASS
    assert [item["name"] for item in first["checks"]] == list(state.CHECK_ORDER)
    assert all(item["status"] == PASS and item["critical"] for item in first["checks"])
    assert first["schema_version"] == "phase11.2a-operational-state-v1"
    assert first["database"]["sqlite"]["query_only"] == 1
    assert first["database"]["sqlite"]["integrity_check"] == ["ok"]


def test_the_evidence_carries_no_absolute_path_no_clock_and_no_score(owners, tiny_database):
    serialized = state.serialize_evidence(state.validate_operational_state(tiny_database, 7))
    assert str(tiny_database) not in serialized
    assert str(tiny_database.parent) not in serialized
    assert json.loads(serialized)["database"]["file_name"] == "tiny-operational.db"
    keys = set()

    def walk(value):
        if isinstance(value, dict):
            keys.update(value)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(json.loads(serialized))
    assert not {key for key in keys if "score" in key or "quality" in key or key in {"generated_at", "timestamp", "now"}}


def test_the_eligibility_audit_is_asked_about_the_profile_owner_not_the_profile(owners, tiny_database):
    seen = []
    owners("audit_eligibility", lambda connection, user_id: seen.append(user_id) or eligibility_audit())
    state.validate_operational_state(tiny_database, 7)
    assert seen == [3]


def test_an_unknown_profile_cannot_be_validated(owners, tiny_database):
    with pytest.raises(OperationalStateError) as raised:
        state.validate_operational_state(tiny_database, 8)
    assert raised.value.code == "PROFILE_NOT_FOUND"


def test_owner_vocabulary_is_quoted_not_normalized(owners, tiny_database):
    owners("audit_matching_profile_history", lambda c, p: matching_audit(status="NOT_SYNCED", current_run_id=None))
    owners("read_current_matching", lambda c, p: matching_current(status="NOT_SYNCED", run_id=None))
    owners("read_current_recommendation", lambda c, p: recommendation_current(
        status="INCOMPLETE", run_id=None, readiness=[SimpleNamespace(code=Code.SOME_ISSUE, message=SECRET, opportunity_id=None)]))
    owners("audit_recommendation_profile_history", lambda c, p: recommendation_audit(status="INCOMPLETE", current_run_id=None))
    owners("audit_eligibility", lambda c, u: eligibility_audit(unknown_dimensions={"LANGUAGE": 2}))
    evidence = state.validate_operational_state(tiny_database, 7)
    assert evidence["matching"]["current"]["status"] == "NOT_SYNCED"
    assert evidence["matching"]["current"]["current_run_id"] is None
    assert evidence["matching"]["current"]["current_run"] is None
    assert evidence["recommendation"]["current"]["status"] == "INCOMPLETE"
    assert evidence["recommendation"]["current"]["readiness_issues"] == [{"code": "SOME_ISSUE", "opportunity_id": None}]
    assert evidence["eligibility"]["verdicts"] == {"ELIGIBLE": 2, "UNKNOWN": 1}
    assert evidence["eligibility"]["unknown_dimensions"] == {"LANGUAGE": 2}
    assert evidence["priority"]["audit"]["status"] == "NOT_SYNCED"
    assert evidence["priority"]["current"]["current_run"] is None
    assert SECRET not in state.serialize_evidence(evidence)


def test_owner_issue_codes_are_exposed_without_their_messages(owners, tiny_database):
    owners("audit_recommendation_profile_history", lambda c, p: recommendation_audit(ok=False, issues=[
        SimpleNamespace(scope="RUN", code="RUN_FINGERPRINT_MISMATCH", run_id=9, opportunity_id=None, detail=SECRET)]))
    owners("audit_current_priority", lambda c, p: auxiliary_audit(status="CORRUPT", issues=[
        SimpleNamespace(code=Code.SOME_ISSUE, message=SECRET, run_id=2, opportunity_id=5)]))
    evidence = state.validate_operational_state(tiny_database, 7)
    assert evidence["recommendation"]["audit"]["issues"] == [
        {"scope": "RUN", "code": "RUN_FINGERPRINT_MISMATCH", "run_id": 9, "opportunity_id": None}]
    assert evidence["priority"]["audit"]["issues"] == [{"code": "SOME_ISSUE", "run_id": 2, "opportunity_id": 5}]
    assert check(evidence, "RECOMMENDATION_AUDIT")["status"] == FAIL
    assert check(evidence, "PRIORITY_AUDIT")["status"] == FAIL
    assert SECRET not in state.serialize_evidence(evidence)


def test_an_owner_refusal_becomes_an_error_code_not_its_message(owners, tiny_database):
    def refuse(connection, profile_id):
        raise state.RecommendationReadError(f"{SECRET} at C:\\secret\\path")

    owners("read_current_recommendation", refuse)
    evidence = state.validate_operational_state(tiny_database, 7)
    assert evidence["recommendation"]["current"] == {"available": False, "error_code": "RecommendationReadError"}
    assert check(evidence, "RECOMMENDATION_READY")["status"] == UNAVAILABLE
    assert check(evidence, "CURRENT_MATCHING_RECOMMENDATION_CHAIN")["status"] == UNAVAILABLE
    assert evidence["result"] == FAIL
    serialized = state.serialize_evidence(evidence)
    assert SECRET not in serialized and "secret" not in serialized


@pytest.mark.parametrize("status", ["NOT_SYNCED", "INCOMPLETE"])
def test_a_recommendation_that_is_not_ready_fails_the_operational_check(owners, tiny_database, status):
    owners("read_current_recommendation", lambda c, p: recommendation_current(status=status, run_id=None))
    owners("audit_recommendation_profile_history", lambda c, p: recommendation_audit(status=status, current_run_id=None))
    evidence = state.validate_operational_state(tiny_database, 7)
    assert check(evidence, "RECOMMENDATION_READY") == {
        "name": "RECOMMENDATION_READY", "critical": True, "status": FAIL,
        "evidence": {"status": status, "current_run_id": None}}
    assert check(evidence, "RECOMMENDATION_AUDIT")["status"] == PASS
    assert evidence["current_chain"]["run_id_comparison"] == NOT_APPLICABLE
    assert check(evidence, "CURRENT_MATCHING_RECOMMENDATION_CHAIN")["status"] == UNAVAILABLE
    assert evidence["result"] == FAIL


def test_an_audit_that_disagrees_with_the_read_model_fails(owners, tiny_database):
    owners("audit_recommendation_profile_history", lambda c, p: recommendation_audit(current_run_id=10))
    assert check(state.validate_operational_state(tiny_database, 7), "RECOMMENDATION_AUDIT")["status"] == FAIL


def test_eligibility_invariant_violations_fail_by_name(owners, tiny_database):
    owners("audit_eligibility", lambda c, u: eligibility_audit(invariant_violations={"skill_rule_blocking": 2, "ambiguity_rule_blocking": 0}))
    item = check(state.validate_operational_state(tiny_database, 7), "ELIGIBILITY_AUDIT")
    assert item["status"] == FAIL
    assert item["evidence"] == {"violated_invariants": {"skill_rule_blocking": 2}}


def test_an_auxiliary_owner_not_synced_is_preserved_and_does_not_fail(owners, tiny_database):
    evidence = state.validate_operational_state(tiny_database, 7)
    assert evidence["priority"]["role"] == evidence["portfolio"]["role"] == "AUXILIARY_NOT_RANKING_AUTHORITY"
    assert evidence["recommendation"]["ranking_authority"] is True
    assert check(evidence, "PORTFOLIO_AUDIT")["evidence"]["audit_status"] == "NOT_SYNCED"
    assert check(evidence, "PORTFOLIO_AUDIT")["status"] == PASS


# --------------------------------------------------------------------------
# current chain
# --------------------------------------------------------------------------


def chain(recommendation_read, matching_read):
    wrap = lambda part: {"current": part}  # noqa: E731
    return state.compare_current_chain(wrap(matching_read), wrap(recommendation_read))


def read_part(current_run, status="READY", available=True):
    return {"available": available, "status": status, "current_run": current_run}


def test_the_current_chain_matches_on_persisted_ids_and_fingerprints():
    result = chain(
        read_part({"source_matching_run_id": 4, "source_matching_run_fingerprint": "f" * 64}),
        read_part({"run_id": 4, "run_fingerprint": "f" * 64}),
    )
    assert result == {
        "recommendation_source_matching_run_id": 4, "matching_current_run_id": 4,
        "run_id_comparison": MATCH, "run_fingerprint_comparison": MATCH, "freshness_claim": "NOT_ASSESSED",
    }


def test_the_current_chain_reports_an_id_mismatch_and_fails(owners, tiny_database):
    result = chain(
        read_part({"source_matching_run_id": 3, "source_matching_run_fingerprint": "f" * 64}),
        read_part({"run_id": 4, "run_fingerprint": "f" * 64}),
    )
    assert (result["run_id_comparison"], result["run_fingerprint_comparison"]) == (MISMATCH, MATCH)
    owners("read_current_recommendation", lambda c, p: recommendation_current(source_run=3))
    evidence = state.validate_operational_state(tiny_database, 7)
    assert check(evidence, "CURRENT_MATCHING_RECOMMENDATION_CHAIN")["status"] == FAIL
    assert evidence["result"] == FAIL


def test_the_current_chain_reports_a_fingerprint_mismatch(owners, tiny_database):
    owners("read_current_recommendation", lambda c, p: recommendation_current(source_fingerprint="e" * 64))
    evidence = state.validate_operational_state(tiny_database, 7)
    assert evidence["current_chain"]["run_id_comparison"] == MATCH
    assert evidence["current_chain"]["run_fingerprint_comparison"] == MISMATCH
    assert check(evidence, "CURRENT_MATCHING_RECOMMENDATION_CHAIN")["status"] == FAIL


def test_unavailable_provenance_never_becomes_match(owners, tiny_database):
    result = chain(
        read_part({"source_matching_run_id": 4, "source_matching_run_fingerprint": "f" * 64}),
        {"available": False, "error_code": "MatchingReadError"},
    )
    assert (result["run_id_comparison"], result["run_fingerprint_comparison"]) == (UNAVAILABLE, UNAVAILABLE)
    owners("read_current_matching", lambda c, p: matching_current(status="NOT_SYNCED", run_id=None))
    evidence = state.validate_operational_state(tiny_database, 7)
    assert evidence["current_chain"]["run_id_comparison"] == UNAVAILABLE
    assert check(evidence, "CURRENT_MATCHING_RECOMMENDATION_CHAIN")["status"] == UNAVAILABLE
    assert evidence["result"] == FAIL


def test_sqlite_health_results_drive_their_checks():
    evidence = {
        "database": {"unchanged": True, "before_stable": True, "after_stable": True, "identical": True, "sqlite": {
            "query_only": 1, "integrity_check": ["*** in database main ***", "row 3 missing from index"],
            "integrity_check_line_count": 2, "foreign_key_violation_count": 2}},
        "matching": {"audit": {"available": True, "ok": True, "status": "READY", "current_run_id": 4},
                     "current": {"available": True, "status": "READY", "current_run_id": 4}},
        "recommendation": {
            "audit": {"available": True, "ok": True, "status": "READY", "current_run_id": 1},
            "current": {"available": True, "status": "READY", "current_run_id": 1, "current_run": {}}},
        "current_chain": {"run_id_comparison": MATCH, "run_fingerprint_comparison": MATCH, "freshness_claim": "NOT_ASSESSED"},
        "eligibility": {"available": True, "invariant_violations": {}},
        "priority": {"audit": {"available": True, "status": "NOT_SYNCED", "current_run_id": None},
                     "current": {"available": True, "status": "NOT_SYNCED", "current_run_id": None}},
        "portfolio": {"audit": {"available": True, "status": "NOT_SYNCED", "current_run_id": None},
                      "current": {"available": True, "status": "NOT_SYNCED", "current_run_id": None}},
    }
    checks = {item["name"]: item for item in state.build_checks(evidence)}
    assert checks["SQLITE_INTEGRITY"]["status"] == FAIL
    assert checks["SQLITE_INTEGRITY"]["evidence"] == {"line_count": 2}
    assert checks["SQLITE_FOREIGN_KEYS"] == {"name": "SQLITE_FOREIGN_KEYS", "critical": True, "status": FAIL, "evidence": {"violation_count": 2}}
    assert checks["SQLITE_QUERY_ONLY"]["status"] == PASS
    assert all(set(item) == {"name", "critical", "status", "evidence"} for item in checks.values())


def test_auxiliary_evidence_serializes_dates_as_iso_text(owners, tiny_database):
    run = SimpleNamespace(run_id=2, matching_run_id=4, matching_run_fingerprint="m" * 64, run_fingerprint="p" * 64,
                          assessment_count=3, evaluation_date=date(2026, 6, 15))
    owners("audit_current_priority", lambda c, p: auxiliary_audit(status="READY", current_run_id=2))
    owners("read_current_priority", lambda c, p: SimpleNamespace(status="READY", current_run_id=2, history_count=1, current_run=run))
    evidence = state.validate_operational_state(tiny_database, 7)
    assert evidence["priority"]["current"]["current_run"]["evaluation_date"] == "2026-06-15"
    assert check(evidence, "PRIORITY_AUDIT")["status"] == PASS


# --------------------------------------------------------------------------
# blocker 1: the Matching audit must agree with the Matching read model
# --------------------------------------------------------------------------


def test_a_healthy_matching_audit_that_agrees_with_the_read_model_passes(owners, tiny_database):
    item = check(state.validate_operational_state(tiny_database, 7), "MATCHING_AUDIT")
    assert item == {"name": "MATCHING_AUDIT", "critical": True, "status": PASS, "evidence": {
        "audit_available": True, "read_available": True, "audit_ok": True,
        "audit_status": "READY", "read_status": "READY",
        "audit_current_run_id": 4, "read_current_run_id": 4, "audit_agrees_with_read_model": True}}


def test_a_matching_audit_and_read_model_on_different_current_runs_fail(owners, tiny_database):
    owners("read_current_matching", lambda c, p: matching_current(run_id=5))
    evidence = state.validate_operational_state(tiny_database, 7)
    item = check(evidence, "MATCHING_AUDIT")
    assert item["status"] == FAIL
    assert (item["evidence"]["audit_current_run_id"], item["evidence"]["read_current_run_id"]) == (4, 5)
    assert item["evidence"]["audit_agrees_with_read_model"] is False
    assert evidence["result"] == FAIL


def test_a_matching_status_disagreement_fails_without_translating_vocabularies(owners, tiny_database):
    owners("audit_matching_profile_history", lambda c, p: matching_audit(status="EMPTY", current_run_id=None))
    owners("read_current_matching", lambda c, p: matching_current(status="NOT_SYNCED", run_id=None))
    item = check(state.validate_operational_state(tiny_database, 7), "MATCHING_AUDIT")
    assert item["status"] == FAIL
    assert (item["evidence"]["audit_status"], item["evidence"]["read_status"]) == ("EMPTY", "NOT_SYNCED")


def test_an_unhealthy_matching_audit_fails_even_when_it_agrees(owners, tiny_database):
    owners("audit_matching_profile_history", lambda c, p: matching_audit(ok=False, issues=[
        SimpleNamespace(code="STATE_RUN_MISMATCH", run_id=4, opportunity_id=None, detail=SECRET)]))
    evidence = state.validate_operational_state(tiny_database, 7)
    assert check(evidence, "MATCHING_AUDIT")["status"] == FAIL
    assert SECRET not in state.serialize_evidence(evidence)


def test_an_unavailable_matching_read_model_is_unavailable_and_fails_overall(owners, tiny_database):
    def refuse(connection, profile_id):
        raise state.MatchingReadError(SECRET)

    owners("read_current_matching", refuse)
    evidence = state.validate_operational_state(tiny_database, 7)
    item = check(evidence, "MATCHING_AUDIT")
    assert item["status"] == UNAVAILABLE
    assert item["evidence"]["read_available"] is False and item["evidence"]["audit_agrees_with_read_model"] is False
    assert evidence["result"] == FAIL
    assert SECRET not in state.serialize_evidence(evidence)


# --------------------------------------------------------------------------
# blocker 2: an unstable fingerprint can never pass
# --------------------------------------------------------------------------


def stable_file(sha="a" * 64):
    return {"exists": True, "is_file": True, "size": 10, "mtime_ns": 1, "sha256": sha, "stable_during_hash": True}


def stable_fingerprint(**side_files):
    sides = {suffix: {"exists": False} for suffix in ("-wal", "-shm", "-journal")}
    sides.update(side_files)
    return {"main": stable_file(), "side_files": sides}


def test_a_stable_identical_fingerprint_passes():
    assert state.database_unchanged(stable_fingerprint(), stable_fingerprint()) == {
        "before_stable": True, "after_stable": True, "identical": True, "unchanged": True}
    with_side = stable_fingerprint(**{"-shm": stable_file("b" * 64)})
    assert state.database_unchanged(with_side, with_side)["unchanged"] is True


@pytest.mark.parametrize("which", ["before", "after"])
def test_an_unstable_main_file_fails_even_when_identical(which):
    unstable = stable_fingerprint()
    unstable["main"]["stable_during_hash"] = False
    before, after = (unstable, unstable) if which == "before" else (stable_fingerprint(), unstable)
    result = state.database_unchanged(before, after)
    assert result["unchanged"] is False
    assert result[f"{which}_stable"] is False


@pytest.mark.parametrize("main", [{"exists": False}, {"exists": True, "is_file": False}])
def test_a_missing_or_non_file_main_database_is_never_stable(main):
    fingerprint = stable_fingerprint()
    fingerprint["main"] = main
    assert state.fingerprint_is_stable(fingerprint) is False
    assert state.database_unchanged(fingerprint, fingerprint)["unchanged"] is False


def test_an_unstable_existing_side_file_fails():
    side = stable_file("c" * 64)
    side["stable_during_hash"] = False
    fingerprint = stable_fingerprint(**{"-wal": side})
    assert state.fingerprint_is_stable(fingerprint) is False
    assert state.database_unchanged(fingerprint, fingerprint)["unchanged"] is False


def test_a_side_path_that_is_not_a_regular_file_fails_and_is_not_read_as_absent(owners, tiny_database):
    # A -shm path is ignored by SQLite for a rollback-journal database, so a
    # directory there cannot disturb the read; the validator must still refuse it.
    side = Path(f"{tiny_database}-shm")
    side.mkdir()
    (side / "TEST-ONLY-marker").write_text("TEST ONLY", encoding="utf-8")
    fingerprint = state.fingerprint_database(tiny_database)
    assert fingerprint["side_files"]["-shm"] == {"exists": True, "is_file": False}
    assert state.fingerprint_is_stable(fingerprint) is False

    evidence = state.validate_operational_state(tiny_database, 7)

    assert check(evidence, "DATABASE_UNCHANGED")["status"] == FAIL
    assert check(evidence, "DATABASE_UNCHANGED")["evidence"] == {
        "before_stable": False, "after_stable": False, "identical": True, "unchanged": False}
    assert evidence["result"] == FAIL
    assert side.is_dir() and (side / "TEST-ONLY-marker").read_text(encoding="utf-8") == "TEST ONLY"


def test_an_unstable_fingerprint_during_validation_fails_database_unchanged(owners, tiny_database, monkeypatch):
    real = state.fingerprint_database

    def unstable(path):
        fingerprint = real(path)
        fingerprint["main"]["stable_during_hash"] = False
        return fingerprint

    monkeypatch.setattr(state, "fingerprint_database", unstable)
    evidence = state.validate_operational_state(tiny_database, 7)
    assert check(evidence, "DATABASE_UNCHANGED")["status"] == FAIL
    assert evidence["database"]["identical"] is True and evidence["database"]["unchanged"] is False
    assert evidence["result"] == FAIL


# --------------------------------------------------------------------------
# blocker 3: the after-fingerprint is taken on failure too
# --------------------------------------------------------------------------


def test_a_failed_validation_on_an_unchanged_file_keeps_its_original_error(owners, tiny_database, monkeypatch):
    real = state.fingerprint_database
    calls = []
    monkeypatch.setattr(state, "fingerprint_database", lambda path: calls.append(path) or real(path))
    with pytest.raises(OperationalStateError) as raised:
        state.validate_operational_state(tiny_database, 8)
    assert raised.value.code == "PROFILE_NOT_FOUND"
    assert len(calls) == 2


@pytest.mark.parametrize("drift", ["content", "unstable"])
def test_a_file_that_changed_during_a_failed_validation_reports_that_instead(owners, tiny_database, monkeypatch, drift):
    real = state.fingerprint_database
    calls = []

    def drifting(path):
        calls.append(path)
        fingerprint = real(path)
        if len(calls) == 2:
            if drift == "content":
                fingerprint["main"]["sha256"] = "0" * 64
            else:
                fingerprint["main"]["stable_during_hash"] = False
        return fingerprint

    monkeypatch.setattr(state, "fingerprint_database", drifting)
    with pytest.raises(OperationalStateError) as raised:
        state.validate_operational_state(tiny_database, 8)
    assert raised.value.code == "DATABASE_CHANGED_DURING_VALIDATION"
    assert raised.value.__cause__ is None and len(calls) == 2


def test_a_programmer_error_is_not_turned_into_evidence_and_still_closes_the_connection(owners, tiny_database, monkeypatch):
    closed = []
    real = state.connect_readonly_database

    class Tracking:
        def __init__(self, connection):
            self._connection = connection

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def close(self):
            closed.append(True)
            self._connection.close()

    def broken(connection, profile_id):
        raise KeyError("TEST-ONLY programmer error")

    monkeypatch.setattr(state, "connect_readonly_database", lambda path: Tracking(real(path)))
    owners("read_current_recommendation", broken)
    with pytest.raises(KeyError):
        state.validate_operational_state(tiny_database, 7)
    assert closed == [True]


# --------------------------------------------------------------------------
# residual: unexpected errors and failed releases, in precedence order
# --------------------------------------------------------------------------


class Harness:
    """Records statements, releases, closes and fingerprints; can fail ROLLBACK or drift."""

    def __init__(self, monkeypatch, *, fail_rollback=False, drift_after=False):
        self.statements, self.closed, self.fingerprints = [], [], []
        real_connect, real_fingerprint = state.connect_readonly_database, state.fingerprint_database
        harness = self

        class Wrapped:
            def __init__(self, connection):
                self._connection = connection

            def __getattr__(self, name):
                return getattr(self._connection, name)

            def execute(self, statement, *parameters):
                harness.statements.append(statement)
                if fail_rollback and statement == "ROLLBACK":
                    raise sqlite3.OperationalError("TEST-ONLY rollback refused")
                return self._connection.execute(statement, *parameters)

            def close(self):
                harness.closed.append(len(harness.fingerprints))
                self._connection.close()

        def fingerprint(path):
            harness.fingerprints.append(path)
            result = real_fingerprint(path)
            if drift_after and len(harness.fingerprints) == 2:
                result["main"]["sha256"] = "0" * 64
            return result

        monkeypatch.setattr(state, "connect_readonly_database", lambda path: Wrapped(real_connect(path)))
        monkeypatch.setattr(state, "fingerprint_database", fingerprint)

    def assert_released_closed_then_fingerprinted(self, *, rollback_attempted=True):
        assert ("ROLLBACK" in self.statements) is rollback_attempted
        assert self.closed == [1], "closed once, after the before-fingerprint and before the after-fingerprint"
        assert len(self.fingerprints) == 2


def raw_fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size, path.stat().st_mtime_ns


def exploding_owner(error):
    def owner(connection, profile_id):
        raise error

    return owner


def test_an_unexpected_owner_error_is_released_closed_fingerprinted_and_re_raised_unchanged(owners, tiny_database, monkeypatch):
    harness = Harness(monkeypatch)
    error = RuntimeError("TEST-ONLY unexpected")
    owners("audit_current_portfolio", exploding_owner(error))
    before = raw_fingerprint(tiny_database)

    with pytest.raises(RuntimeError) as raised:
        state.validate_operational_state(tiny_database, 7)

    assert raised.value is error and type(raised.value) is RuntimeError
    harness.assert_released_closed_then_fingerprinted()
    assert raw_fingerprint(tiny_database) == before


def test_an_unexpected_error_on_a_changed_file_reports_the_change(owners, tiny_database, monkeypatch):
    harness = Harness(monkeypatch, drift_after=True)
    owners("audit_current_portfolio", exploding_owner(RuntimeError("TEST-ONLY unexpected")))
    with pytest.raises(OperationalStateError) as raised:
        state.validate_operational_state(tiny_database, 7)
    assert raised.value.code == "DATABASE_CHANGED_DURING_VALIDATION"
    harness.assert_released_closed_then_fingerprinted()


def test_a_safe_error_with_a_successful_release_keeps_its_code(owners, tiny_database, monkeypatch):
    harness = Harness(monkeypatch)
    before = raw_fingerprint(tiny_database)
    with pytest.raises(OperationalStateError) as raised:
        state.validate_operational_state(tiny_database, 8)
    assert raised.value.code == "PROFILE_NOT_FOUND"
    assert harness.statements[-1] == "ROLLBACK"
    harness.assert_released_closed_then_fingerprinted()
    assert raw_fingerprint(tiny_database) == before


def test_a_failed_rollback_outranks_the_original_safe_error(owners, tiny_database, monkeypatch):
    harness = Harness(monkeypatch, fail_rollback=True)
    before = raw_fingerprint(tiny_database)
    with pytest.raises(OperationalStateError) as raised:
        state.validate_operational_state(tiny_database, 8)
    assert raised.value.code == "SNAPSHOT_RELEASE_FAILED"
    harness.assert_released_closed_then_fingerprinted()
    assert raw_fingerprint(tiny_database) == before


def test_a_failed_rollback_on_a_changed_file_reports_the_change(owners, tiny_database, monkeypatch):
    harness = Harness(monkeypatch, fail_rollback=True, drift_after=True)
    with pytest.raises(OperationalStateError) as raised:
        state.validate_operational_state(tiny_database, 8)
    assert raised.value.code == "DATABASE_CHANGED_DURING_VALIDATION"
    harness.assert_released_closed_then_fingerprinted()


def test_a_failed_rollback_after_a_successful_read_is_not_evidence(owners, tiny_database, monkeypatch):
    harness = Harness(monkeypatch, fail_rollback=True)
    with pytest.raises(OperationalStateError) as raised:
        state.validate_operational_state(tiny_database, 7)
    assert raised.value.code == "SNAPSHOT_RELEASE_FAILED"
    harness.assert_released_closed_then_fingerprinted()


def test_base_exceptions_are_not_captured_but_the_connection_is_still_closed(owners, tiny_database, monkeypatch):
    harness = Harness(monkeypatch)
    owners("audit_current_portfolio", exploding_owner(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        state.validate_operational_state(tiny_database, 7)
    assert "ROLLBACK" in harness.statements
    assert harness.closed == [1] and len(harness.fingerprints) == 1


# --------------------------------------------------------------------------
# static architecture guards
# --------------------------------------------------------------------------


def tree(path=MODULE_PATH):
    return ast.parse(path.read_text(encoding="utf-8"))


def production_sources():
    return [MODULE_PATH, MODULE_PATH.parent / "cli.py", MODULE_PATH.parent / "__init__.py"]


FORBIDDEN_MODULE_PREFIXES = (
    "services.recommendation.sync", "services.recommendation.engine", "services.recommendation.input_assembly",
    "services.recommendation.persistence.", "services.collector.matching.sync", "services.collector.matching.engine",
    "services.priority.sync", "services.priority.engine", "services.priority.dry_run",
    "services.portfolio.sync", "services.portfolio.engine", "services.portfolio.dry_run",
    "services.collector.qualification.classifier", "services.collector.qualification.fine_classifier",
    "services.collector.qualification.persistence", "services.geography", "services.eligibility.engine",
    "services.eligibility.service", "services.collector.agent", "services.collector.collectors",
    "services.collector.cli", "services.collector.extractors", "services.collector.deduplication",
    "evaluation",
)

FORBIDDEN_NAMES = {
    "sync_matching", "sync_recommendations", "sync_priority", "sync_portfolio", "sync_notification_policy",
    "build_matching_assessments", "build_recommendation_batch", "build_recommendation_assessment",
    "rank_recommendation_assessments", "build_priority_assessment", "build_portfolio_assessments",
    "classify_opportunity", "classify_fine_categories", "persist_qualifications", "resolve_segment",
    "synchronize_location_resolutions", "evaluate_eligibility", "synchronize_eligibility", "RadarAgent",
    "run_once", "apply_migrations", "connect_database", "connect_configured_database",
}


def test_production_modules_import_no_computing_or_writing_owner():
    for source in production_sources():
        for node in ast.walk(tree(source)):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not module.startswith(FORBIDDEN_MODULE_PREFIXES), (source.name, module)
                assert not {alias.name for alias in node.names} & FORBIDDEN_NAMES, (source.name, module)
            elif isinstance(node, ast.Import):
                assert not [a.name for a in node.names if a.name.startswith(FORBIDDEN_MODULE_PREFIXES)]


def test_production_modules_call_no_computing_function_and_open_sqlite_only_read_only():
    for source in production_sources():
        for node in ast.walk(tree(source)):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                assert name not in FORBIDDEN_NAMES, (source.name, name)
                if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                    assert (node.func.value.id, node.func.attr) != ("sqlite3", "connect")
    calls = [node for node in ast.walk(tree()) if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "connect_readonly_database"]
    assert len(calls) == 1


def executed_sql():
    statements = []
    for node in ast.walk(tree()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute":
            first = node.args[0] if node.args else None
            assert isinstance(first, ast.Constant) and isinstance(first.value, str), "every executed statement is a literal"
            statements.append(" ".join(first.value.upper().split()))
    return statements


def test_every_statement_the_validator_executes_is_a_read():
    statements = executed_sql()
    assert set(statements) == {
        "PRAGMA QUERY_ONLY = ON", "PRAGMA QUERY_ONLY", "BEGIN", "ROLLBACK",
        "PRAGMA INTEGRITY_CHECK", "PRAGMA FOREIGN_KEY_CHECK", "SELECT USER_ID FROM PROFILES WHERE ID = ?",
    }
    for statement in statements:
        words = set(statement.replace("(", " ").replace(",", " ").split())
        assert not words & {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER", "COMMIT", "VACUUM", "ATTACH"}


def test_nothing_is_named_after_the_forbidden_identifier():
    forbidden = "D" + "56"
    for source in production_sources() + [Path(__file__), Path(__file__).parents[1] / "integration" / "test_final_operational_state_sqlite.py"]:
        if source.exists():
            assert forbidden not in source.read_text(encoding="utf-8")
            assert forbidden not in source.name


def test_production_code_hardcodes_no_profile_and_no_country():
    for source in production_sources():
        text = source.read_text(encoding="utf-8")
        module = tree(source)
        assert "morocco" not in text.lower() and "maroc" not in text.lower()
        for node in ast.walk(module):
            if isinstance(node, ast.keyword) and node.arg == "profile_id":
                assert not isinstance(node.value, ast.Constant)
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") in {"validate_operational_state", "validate"}:
                assert not any(isinstance(arg, ast.Constant) and isinstance(arg.value, int) for arg in node.args)
