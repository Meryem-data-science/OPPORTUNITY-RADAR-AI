"""Phase 11.2A: the operational state validator over a disposable migrated SQLite file.

The upstream chain is built once, in a temporary directory, through each phase's
own entry points — qualification, constraints, eligibility, Matching,
Recommendation, Priority and Portfolio synchronization all run during *setup*
only, on a throwaway file. Every test then copies that file and validates the
copy. **The operational database is never opened.**

What is proven here is the validator's own contract: a `mode=ro` connection
confirmed `query_only` before any read, one `BEGIN` … `ROLLBACK` snapshot and
nothing else, the real owners consulted, SQLite's own integrity and foreign-key
verdicts consumed, corruption reported but never repaired, and the file and its
side files left byte-for-byte as they were.
"""

from dataclasses import replace
from datetime import date
import hashlib
import importlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from services.collector.database.connection import connect_database, connect_readonly_database
from services.collector.matching.read_model import MatchingReadError
from services.final_validation import cli
from services.final_validation import operational_state as state
from services.final_validation.operational_state import FAIL, MATCH, PASS, OperationalStateError
from services.portfolio.sync import sync_portfolio
from services.priority.sync import sync_priority
from services.recommendation import sync_recommendations
from tests.integration.test_recommendation_sqlite import build_corpus

SIDE_SUFFIXES = ("-wal", "-shm", "-journal")

#: Entry points that compute, synchronize, classify, resolve or collect. The
#: validator reaching any of them would fail the test that sets these.
COMPUTING_ENTRY_POINTS = (
    ("services.collector.matching.sync", "sync_matching"),
    ("services.collector.matching", "sync_matching"),
    ("services.collector.matching.engine", "build_matching_assessments"),
    ("services.recommendation.sync", "sync_recommendations"),
    ("services.recommendation", "sync_recommendations"),
    ("services.recommendation.engine", "build_recommendation_batch"),
    ("services.recommendation.input_assembly", "assemble_recommendation_inputs"),
    ("services.priority.sync", "sync_priority"),
    ("services.priority.engine", "build_priority_assessment"),
    ("services.portfolio.sync", "sync_portfolio"),
    ("services.portfolio.engine", "build_portfolio_assessments"),
    ("services.collector.qualification.classifier", "classify_opportunity"),
    ("services.collector.qualification.fine_classifier", "classify_fine_categories"),
    ("services.geography.resolver", "resolve_segment"),
    ("services.geography.service", "synchronize_location_resolutions"),
    ("services.eligibility.engine", "evaluate_eligibility"),
    ("services.eligibility.service", "synchronize_eligibility"),
    ("services.collector.agent", "RadarAgent"),
)

OWNER_NAMES = (
    "audit_matching_profile_history",
    "read_current_matching",
    "audit_recommendation_profile_history",
    "read_current_recommendation",
    "audit_eligibility",
    "audit_current_priority",
    "read_current_priority",
    "audit_current_portfolio",
    "read_current_portfolio",
)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """A migrated corpus with READY Matching, Recommendation, Priority and Portfolio."""
    connection, path, identity, _ids, _ = build_corpus(tmp_path_factory.mktemp("final-validation-world"))
    try:
        assert sync_recommendations(connection, identity.profile_id).state.value == "READY"
        sync_priority(connection, identity.profile_id, date(2026, 6, 15))
        sync_portfolio(connection, identity.profile_id)
        user_id = connection.execute("SELECT user_id FROM profiles WHERE id=?", (identity.profile_id,)).fetchone()[0]
    finally:
        connection.close()
    assert user_id != identity.profile_id, "the corpus must keep user and profile ids apart"
    return {"path": path, "profile_id": identity.profile_id, "user_id": user_id}


@pytest.fixture
def copy(world, tmp_path):
    """A private copy of the world, so a test may damage its own file."""
    target = tmp_path / "operational-copy.db"
    shutil.copy2(world["path"], target)
    return target


def independent_fingerprint(path: Path):
    def one(file: Path):
        if not file.exists():
            return None
        stat = file.stat()
        return (hashlib.sha256(file.read_bytes()).hexdigest(), stat.st_size, stat.st_mtime_ns)

    return one(path), {suffix: one(Path(f"{path}{suffix}")) for suffix in SIDE_SUFFIXES}


def check(evidence, name):
    return next(item for item in evidence["checks"] if item["name"] == name)


def test_a_healthy_persisted_state_passes_with_the_owners_identities(world, copy):
    evidence = state.validate_operational_state(copy, world["profile_id"])

    assert evidence["result"] == PASS, [item for item in evidence["checks"] if item["status"] != PASS]
    assert [item["name"] for item in evidence["checks"]] == list(state.CHECK_ORDER)
    reader = connect_readonly_database(copy)
    try:
        matching_run_id = reader.execute(
            "SELECT current_run_id FROM matching_profile_state WHERE profile_id=?", (world["profile_id"],)).fetchone()[0]
        matching_fingerprint = reader.execute(
            "SELECT run_fingerprint FROM matching_runs WHERE id=?", (matching_run_id,)).fetchone()[0]
        recommendation_run_id = reader.execute(
            "SELECT current_run_id FROM recommendation_profile_state WHERE profile_id=?", (world["profile_id"],)).fetchone()[0]
        source = reader.execute(
            "SELECT source_matching_run_id, source_matching_run_fingerprint FROM recommendation_runs WHERE id=?",
            (recommendation_run_id,)).fetchone()
        decisions = reader.execute(
            "SELECT COUNT(*) FROM opportunity_eligibilities WHERE user_id=?", (world["user_id"],)).fetchone()[0]
    finally:
        reader.close()

    assert evidence["matching"]["current"]["current_run"]["run_id"] == matching_run_id
    assert evidence["matching"]["current"]["current_run"]["run_fingerprint"] == matching_fingerprint
    recommendation_run = evidence["recommendation"]["current"]["current_run"]
    assert recommendation_run["run_id"] == recommendation_run_id
    assert (recommendation_run["source_matching_run_id"], recommendation_run["source_matching_run_fingerprint"]) == tuple(source)
    assert evidence["current_chain"] == {
        "recommendation_source_matching_run_id": source[0],
        "matching_current_run_id": matching_run_id,
        "run_id_comparison": MATCH,
        "run_fingerprint_comparison": MATCH,
        "freshness_claim": "NOT_ASSESSED",
    }
    assert evidence["eligibility"]["decisions"] == decisions > 0
    assert evidence["database"]["sqlite"]["integrity_check"] == ["ok"]
    assert evidence["database"]["sqlite"]["foreign_key_violation_count"] == 0
    assert evidence["priority"]["audit"]["status"] == evidence["portfolio"]["audit"]["status"] == "READY"
    assert str(copy.parent) not in state.serialize_evidence(evidence)


def test_the_connection_is_read_only_and_every_read_happens_inside_one_rollback_snapshot(world, copy, monkeypatch):
    statements = []
    query_only_at_read = []
    real = connect_readonly_database

    class Recording:
        def __init__(self, connection):
            self._connection = connection

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def execute(self, statement, *parameters):
            normalized = " ".join(statement.split())
            statements.append(normalized)
            if normalized.upper().startswith("SELECT"):
                query_only_at_read.append(self._connection.execute("PRAGMA query_only").fetchone()[0])
                assert self._connection.in_transaction, normalized
            return self._connection.execute(statement, *parameters)

    opened = []

    def recording(path):
        connection = real(path)
        opened.append(connection)
        return Recording(connection)

    monkeypatch.setattr(state, "connect_readonly_database", recording)
    evidence = state.validate_operational_state(copy, world["profile_id"])

    assert evidence["result"] == PASS
    assert len(opened) == 1
    assert statements[:3] == ["PRAGMA query_only = ON", "PRAGMA query_only", "BEGIN"]
    assert statements[-1] == "ROLLBACK"
    assert statements.count("BEGIN") == 1 and statements.count("ROLLBACK") == 1
    assert query_only_at_read and set(query_only_at_read) == {1}
    for statement in statements:
        upper = statement.upper()
        assert upper.startswith(("SELECT", "WITH", "PRAGMA QUERY_ONLY", "PRAGMA INTEGRITY_CHECK",
                                 "PRAGMA FOREIGN_KEY_CHECK", "BEGIN", "ROLLBACK")), statement
        words = set(upper.replace("(", " ").replace(",", " ").split())
        assert not words & {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER", "COMMIT"}, statement
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")  # closed before the after-fingerprint


def test_the_helper_connection_itself_refuses_writes(copy):
    connection = connect_readonly_database(copy)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("UPDATE profiles SET user_id = user_id")
    finally:
        connection.close()


def test_the_official_owners_are_invoked_with_the_profile_and_its_owner(world, copy, monkeypatch):
    calls = []
    for name in OWNER_NAMES:
        original = getattr(state, name)

        def spy(connection, key, _name=name, _original=original):
            calls.append((_name, key))
            return _original(connection, key)

        monkeypatch.setattr(state, name, spy)
    assert state.validate_operational_state(copy, world["profile_id"])["result"] == PASS
    assert sorted(calls) == sorted(
        [(name, world["profile_id"]) for name in OWNER_NAMES if name != "audit_eligibility"]
        + [("audit_eligibility", world["user_id"])]
    )


def test_no_computing_or_synchronizing_entry_point_is_reached(world, copy, monkeypatch):
    def tripwire(*_args, **_kwargs):
        raise AssertionError("the operational state validator reached a computing entry point")

    for module_name, attribute in COMPUTING_ENTRY_POINTS:
        monkeypatch.setattr(importlib.import_module(module_name), attribute, tripwire)
    assert state.validate_operational_state(copy, world["profile_id"])["result"] == PASS


def test_the_file_is_unchanged_and_no_side_file_appears(world, copy):
    before = independent_fingerprint(copy)
    assert before[1] == {suffix: None for suffix in SIDE_SUFFIXES}

    evidence = state.validate_operational_state(copy, world["profile_id"])

    after = independent_fingerprint(copy)
    assert after == before
    main = evidence["database"]["after"]["main"]
    assert (main["sha256"], main["size"], main["mtime_ns"]) == before[0]
    assert evidence["database"]["before"] == evidence["database"]["after"]
    assert evidence["database"]["after"]["side_files"] == {suffix: {"exists": False} for suffix in SIDE_SUFFIXES}
    assert check(evidence, "DATABASE_UNCHANGED")["status"] == PASS


def test_a_pre_existing_side_file_is_tracked_and_left_exactly_in_place(world, copy):
    # A stray -shm beside a rollback-journal database is ignored by SQLite, so
    # it is a safe way to prove the validator neither deletes nor alters it.
    side = Path(f"{copy}-shm")
    side.write_bytes(b"TEST-ONLY pre-existing side file")
    before = independent_fingerprint(copy)

    evidence = state.validate_operational_state(copy, world["profile_id"])

    assert independent_fingerprint(copy) == before
    assert side.read_bytes() == b"TEST-ONLY pre-existing side file"
    tracked = evidence["database"]["after"]["side_files"]["-shm"]
    assert tracked["exists"] is True and tracked["sha256"] == before[1]["-shm"][0]
    assert check(evidence, "DATABASE_UNCHANGED")["status"] == PASS


def test_persisted_corruption_is_reported_and_never_repaired(world, copy):
    writer = connect_database(copy)
    try:
        run_id, opportunity_id, score = writer.execute(
            """SELECT run_id, opportunity_id, recommendation_score FROM recommendation_assessments
            WHERE recommendation_score IS NOT NULL ORDER BY run_id, rank_position LIMIT 1""").fetchone()
        writer.execute(
            "UPDATE recommendation_assessments SET recommendation_score = ? WHERE run_id = ? AND opportunity_id = ?",
            (score / 2 if score else 0.123, run_id, opportunity_id))
        writer.commit()
    finally:
        writer.close()
    tampered = independent_fingerprint(copy)

    evidence = state.validate_operational_state(copy, world["profile_id"])

    assert evidence["result"] == FAIL
    assert check(evidence, "RECOMMENDATION_AUDIT")["status"] == FAIL
    assert evidence["recommendation"]["audit"]["ok"] is False
    assert evidence["recommendation"]["audit"]["issues"]
    assert all(set(issue) == {"scope", "code", "run_id", "opportunity_id"} for issue in evidence["recommendation"]["audit"]["issues"])
    assert independent_fingerprint(copy) == tampered
    reader = connect_readonly_database(copy)
    try:
        still = reader.execute(
            "SELECT recommendation_score FROM recommendation_assessments WHERE run_id = ? AND opportunity_id = ?",
            (run_id, opportunity_id)).fetchone()[0]
    finally:
        reader.close()
    assert still != score


def test_sqlite_foreign_key_violations_are_consumed_and_reported(world, copy):
    writer = sqlite3.connect(copy)
    try:
        writer.execute("PRAGMA foreign_keys = OFF")
        writer.execute(
            """INSERT INTO opportunity_sources (opportunity_id, source_id, source_url, discovered_at)
            VALUES (987654321, 'TEST-ONLY-missing-source', 'https://example.invalid/fk', '2026-01-01')""")
        writer.commit()
    finally:
        writer.close()
    before = independent_fingerprint(copy)

    evidence = state.validate_operational_state(copy, world["profile_id"])

    sqlite_part = evidence["database"]["sqlite"]
    assert sqlite_part["foreign_key_violation_count"] == 2
    assert sqlite_part["foreign_key_violations_by_table"] == {"opportunity_sources": 2}
    assert check(evidence, "SQLITE_FOREIGN_KEYS")["status"] == FAIL
    assert check(evidence, "SQLITE_INTEGRITY")["status"] == PASS
    assert evidence["result"] == FAIL
    assert independent_fingerprint(copy) == before


def test_a_missing_database_or_profile_is_refused_without_creating_anything(world, copy, tmp_path):
    missing = tmp_path / "absent" / "operational.db"
    with pytest.raises(OperationalStateError) as raised:
        state.validate_operational_state(missing, world["profile_id"])
    assert raised.value.code == "DATABASE_NOT_FOUND"
    assert not missing.parent.exists()

    before = independent_fingerprint(copy)
    with pytest.raises(OperationalStateError) as raised:
        state.validate_operational_state(copy, 987654321)
    assert raised.value.code == "PROFILE_NOT_FOUND"
    assert independent_fingerprint(copy) == before


def test_the_matching_audit_agrees_with_the_matching_read_model(world, copy):
    evidence = state.validate_operational_state(copy, world["profile_id"])
    reader = connect_readonly_database(copy)
    try:
        persisted = reader.execute(
            "SELECT state, current_run_id FROM matching_profile_state WHERE profile_id=?", (world["profile_id"],)).fetchone()
    finally:
        reader.close()
    item = check(evidence, "MATCHING_AUDIT")
    assert item["status"] == PASS
    assert item["evidence"] == {
        "audit_available": True, "read_available": True, "audit_ok": True,
        "audit_status": persisted[0], "read_status": persisted[0],
        "audit_current_run_id": persisted[1], "read_current_run_id": persisted[1],
        "audit_agrees_with_read_model": True,
    }


def test_a_matching_read_model_on_another_current_run_fails_the_matching_audit(world, copy, monkeypatch):
    real = state.read_current_matching

    def elsewhere(connection, profile_id):
        current = real(connection, profile_id)
        return replace(current, current_run_id=current.current_run_id + 1000)

    monkeypatch.setattr(state, "read_current_matching", elsewhere)
    before = independent_fingerprint(copy)
    evidence = state.validate_operational_state(copy, world["profile_id"])
    assert check(evidence, "MATCHING_AUDIT")["status"] == FAIL
    assert check(evidence, "MATCHING_AUDIT")["evidence"]["audit_agrees_with_read_model"] is False
    assert evidence["result"] == FAIL
    assert independent_fingerprint(copy) == before


def test_an_unavailable_matching_read_model_is_unavailable(world, copy, monkeypatch):
    def refuse(connection, profile_id):
        raise MatchingReadError("TEST-ONLY refusal")

    monkeypatch.setattr(state, "read_current_matching", refuse)
    evidence = state.validate_operational_state(copy, world["profile_id"])
    assert check(evidence, "MATCHING_AUDIT")["status"] == "UNAVAILABLE"
    assert evidence["matching"]["current"] == {"available": False, "error_code": "MatchingReadError"}
    assert evidence["result"] == FAIL


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [("sqlite_error_inside_snapshot", "SNAPSHOT_READ_FAILED"), ("unknown_profile", "PROFILE_NOT_FOUND")],
)
def test_a_failure_after_opening_still_rolls_back_closes_and_fingerprints_again(
    world, copy, monkeypatch, failure, expected_code
):
    statements, opened, closed, fingerprints = [], [], [], []
    real_connect = connect_readonly_database
    real_fingerprint = state.fingerprint_database

    class Recording:
        def __init__(self, connection):
            self._connection = connection

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def execute(self, statement, *parameters):
            statements.append(" ".join(statement.split()))
            return self._connection.execute(statement, *parameters)

        def close(self):
            closed.append(len(fingerprints))
            self._connection.close()

    def recording_connect(path):
        connection = real_connect(path)
        opened.append(connection)
        return Recording(connection)

    def counting_fingerprint(path):
        fingerprints.append(path)
        return real_fingerprint(path)

    monkeypatch.setattr(state, "connect_readonly_database", recording_connect)
    monkeypatch.setattr(state, "fingerprint_database", counting_fingerprint)
    profile_id = world["profile_id"]
    if failure == "sqlite_error_inside_snapshot":
        def broken_health(connection):
            connection.execute("SELECT 1")
            raise sqlite3.OperationalError("TEST-ONLY induced failure")

        monkeypatch.setattr(state, "_sqlite_health", broken_health)
    else:
        profile_id = 987654321
    before = independent_fingerprint(copy)

    with pytest.raises(OperationalStateError) as raised:
        state.validate_operational_state(copy, profile_id)

    assert raised.value.code == expected_code
    assert statements[:3] == ["PRAGMA query_only = ON", "PRAGMA query_only", "BEGIN"]
    assert statements[-1] == "ROLLBACK" and "COMMIT" not in statements
    assert len(opened) == 1 and closed == [1], "closed after the before-fingerprint, before the after-fingerprint"
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")
    assert len(fingerprints) == 2
    assert independent_fingerprint(copy) == before


def test_an_unexpected_owner_error_is_released_closed_fingerprinted_and_re_raised(world, copy, monkeypatch):
    statements, closed, fingerprints = [], [], []
    real_connect, real_fingerprint = connect_readonly_database, state.fingerprint_database
    error = RuntimeError("TEST-ONLY unexpected owner failure")

    class Recording:
        def __init__(self, connection):
            self._connection = connection

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def execute(self, statement, *parameters):
            statements.append(" ".join(statement.split()))
            return self._connection.execute(statement, *parameters)

        def close(self):
            closed.append(len(fingerprints))
            self._connection.close()

    def explode(connection, profile_id):
        raise error

    monkeypatch.setattr(state, "connect_readonly_database", lambda path: Recording(real_connect(path)))
    monkeypatch.setattr(state, "fingerprint_database", lambda path: fingerprints.append(path) or real_fingerprint(path))
    monkeypatch.setattr(state, "audit_current_priority", explode)
    before = independent_fingerprint(copy)

    with pytest.raises(RuntimeError) as raised:
        state.validate_operational_state(copy, world["profile_id"])

    assert raised.value is error
    assert statements[:3] == ["PRAGMA query_only = ON", "PRAGMA query_only", "BEGIN"]
    assert statements[-1] == "ROLLBACK" and "COMMIT" not in statements
    assert closed == [1] and len(fingerprints) == 2
    assert independent_fingerprint(copy) == before


def test_the_cli_prints_deterministic_evidence_and_exits_zero(world, copy, monkeypatch, capsys):
    monkeypatch.setenv("OPPORTUNITY_RADAR_ENV", "test")
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(copy))
    monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", str(world["profile_id"]))
    before = independent_fingerprint(copy)

    assert cli.main() == cli.EXIT_PASS
    first = capsys.readouterr()
    assert cli.main() == cli.EXIT_PASS
    second = capsys.readouterr()

    assert first.out == second.out and first.err == second.err == ""
    assert json.loads(first.out)["result"] == PASS
    assert str(copy) not in first.out and str(copy.parent) not in first.out
    assert independent_fingerprint(copy) == before
