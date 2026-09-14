"""Phase 11.1A: `GET /api/recommendation` over disposable, fully migrated SQLite.

Every database is built under a temporary directory and thrown away, profiles
use `.invalid` addresses and every posting is invented. **The operational
database is never opened.**

Nothing about Recommendation is faked. The schema is the project's own
migrations; READY and INCOMPLETE states are published by Phase 9B.3's real
`sync_recommendations` over a real upstream corpus; the lighter fixtures store
runs through Phase 9B.1's `store_recommendation_batch`. Corruption is introduced
the only honest way — a valid history first, then one value broken — so each
refusal is provably about that value.

Synchronization runs during *setup* only. The request under test must never run
it, and one test below proves that with tripwires on every computing entry
point.
"""

from dataclasses import dataclass
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

import services.api.recommendation as recommendation_api
from services.api.main import app
from services.api.recommendation import PUBLIC_RECOMMENDATION_ERROR
from services.collector.database.connection import (
    connect_database,
    connect_readonly_database,
)
from services.collector.database.migrations import apply_migrations
from services.collector.matching import store_matching_batch
from services.digital_twin.repository import ensure_user_profile
from services.recommendation import (
    audit_recommendation_profile_history,
    read_current_recommendation,
    sync_recommendations,
)
from tests.integration.test_matching_read_audit import (
    add_opportunity,
    make_batch as make_matching_batch,
)
from tests.integration.test_opportunity_api_fine_classification_sqlite import (
    _insert_qualification,
)
from tests.integration.test_recommendation_persistence import (
    SELECTION_VERSION,
    Fixture,
)
from tests.integration.test_recommendation_sqlite import build_corpus
from tests.integration.test_recommendation_sync import make_stale

# TEST ONLY identities and links; `.invalid` is reserved and never resolves.
TEST_ONLY_EMAIL = "recommendation.api@example.invalid"
OFFICIAL_SOURCE_URL = "https://boards.example.invalid/test-only/jobs/1"
OFFICIAL_APPLY_URL = "https://boards.example.invalid/test-only/jobs/1/apply"
FINE_VERSION = "TEST-ONLY-fine-v0"

RECOMMENDATION_TABLES = (
    "recommendation_runs",
    "recommendation_assessments",
    "recommendation_profile_state",
)
WATCHED_TABLES = RECOMMENDATION_TABLES + (
    "opportunities",
    "opportunity_qualifications",
    "opportunity_sources",
    "sources",
    "matching_runs",
    "matching_assessments",
    "matching_profile_state",
)

#: Every entry point that computes, synchronizes, classifies or stores, at each
#: place it can be looked up from. A GET that reached any of them would fail.
COMPUTING_ENTRY_POINTS = (
    ("services.recommendation.engine", "build_recommendation_assessment"),
    ("services.recommendation.engine", "build_recommendation_batch"),
    ("services.recommendation.engine", "rank_recommendation_assessments"),
    ("services.recommendation.input_assembly", "assemble_recommendation_inputs"),
    ("services.recommendation.persistence", "store_recommendation_batch"),
    ("services.recommendation.sync", "sync_recommendations"),
    ("services.recommendation.sync", "build_recommendation_batch"),
    ("services.recommendation.sync", "assemble_recommendation_inputs"),
    ("services.recommendation", "build_recommendation_assessment"),
    ("services.recommendation", "build_recommendation_batch"),
    ("services.recommendation", "rank_recommendation_assessments"),
    ("services.recommendation", "assemble_recommendation_inputs"),
    ("services.recommendation", "store_recommendation_batch"),
    ("services.recommendation", "sync_recommendations"),
    ("services.collector.qualification.fine_classifier", "classify_fine_categories"),
    ("services.collector.matching", "sync_matching"),
)


@dataclass(frozen=True)
class ReadyWorld:
    path: Path
    profile_id: int
    ids: dict
    run_id: int


def configure(monkeypatch, path, profile_id):
    monkeypatch.setenv("OPPORTUNITY_RADAR_ENV", "test")
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", str(profile_id))


def get(monkeypatch, path, profile_id):
    configure(monkeypatch, path, profile_id)
    return TestClient(app).get("/api/recommendation")


def table_rows(path, tables=WATCHED_TABLES):
    reader = connect_readonly_database(path)
    try:
        return {
            table: reader.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in tables
        }
    finally:
        reader.close()


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def assert_public_503(response):
    assert response.status_code == 503
    assert response.json() == {"detail": PUBLIC_RECOMMENDATION_ERROR}
    for leak in ("Traceback", "sqlite", "SELECT", "FINGERPRINT", ".db"):
        assert leak not in response.text, leak


@pytest.fixture(scope="module")
def ready_world(tmp_path_factory):
    """A real READY recommendation, plus one official ATS observation.

    Built once and only ever *read* by the tests that use it. The observation
    is added after the synchronization on purpose: link priority is metadata
    outside the recommendation snapshot, and the surface must still apply it.
    """
    connection, path, identity, ids, _ = build_corpus(
        tmp_path_factory.mktemp("recommendation-api-ready")
    )
    try:
        result = sync_recommendations(connection, identity.profile_id)
        assert result.state.value == "READY", result.issues
        connection.execute(
            "INSERT INTO sources (id, type, enabled, status)"
            " VALUES ('test_only_greenhouse', 'greenhouse', 1, 'active')"
        )
        connection.execute(
            "INSERT INTO opportunity_sources"
            " (opportunity_id, source_id, source_url, application_url, discovered_at)"
            " VALUES (?, 'test_only_greenhouse', ?, ?, '2099-01-01')",
            (ids["strong"], OFFICIAL_SOURCE_URL, OFFICIAL_APPLY_URL),
        )
        connection.commit()
    finally:
        connection.close()
    return ReadyWorld(path, identity.profile_id, ids, result.run_id)


def light_world(tmp_path):
    """One profile, three postings, their matching run, and nothing stored yet.

    Same pattern as `matching_fixture` in the Recommendation persistence audit
    tests: the Matching batch comes from Phase 4's *audit* fixture, the only one
    carrying the semantic corpus and model fingerprints Phase 4's audit checks,
    so the source Matching run survives the audit the surface depends on.
    """
    path = tmp_path / "recommendation-api-light.db"
    connection = connect_database(path)
    apply_migrations(connection)
    profile_id = ensure_user_profile(connection, TEST_ONLY_EMAIL).profile_id
    ids = tuple(add_opportunity(connection, f"api-{suffix}") for suffix in "abc")
    connection.commit()
    matching = store_matching_batch(
        connection,
        profile_id,
        make_matching_batch(profile_id, ids),
        selection_version=SELECTION_VERSION,
    )
    connection.commit()
    return Fixture(connection, profile_id, ids, matching), path


def decoded(raw):
    return None if raw is None else json.loads(raw)


# --------------------------------------------------------------------------
# READY, exactly as persisted
# --------------------------------------------------------------------------


def test_a_ready_run_is_served_exactly_as_persisted_in_rank_order(
    ready_world, monkeypatch
):
    response = get(monkeypatch, ready_world.path, ready_world.profile_id)

    assert response.status_code == 200
    body = response.json()
    reader = connect_readonly_database(ready_world.path)
    try:
        run_row = reader.execute(
            """SELECT id, created_at, assessment_count, persistence_version,
               input_assembly_version, recommendation_engine_version,
               recommendation_rules_version, source_matching_run_id,
               source_matching_run_fingerprint, batch_fingerprint, run_fingerprint
               FROM recommendation_runs WHERE id=?""",
            (ready_world.run_id,),
        ).fetchone()
        assessments = reader.execute(
            """SELECT opportunity_id, rank_position, disposition, recommendation_score,
               evidence_coverage, assessment_fingerprint, assessment_payload_json
               FROM recommendation_assessments WHERE run_id=? ORDER BY rank_position""",
            (ready_world.run_id,),
        ).fetchall()
        opportunities = {
            row[0]: row
            for row in reader.execute(
                """SELECT id, canonical_title, organization, location, last_seen_at,
                   source_url FROM opportunities"""
            ).fetchall()
        }
        fine = {
            row[0]: row[1:]
            for row in reader.execute(
                """SELECT opportunity_id, fine_primary_category,
                   fine_secondary_categories_json, fine_category_evidence_json,
                   fine_reasons_json, fine_classifier_version
                   FROM opportunity_qualifications"""
            ).fetchall()
        }
        audit = audit_recommendation_profile_history(reader, ready_world.profile_id)
    finally:
        reader.close()

    assert body["profile_id"] == ready_world.profile_id
    assert body["status"] == "READY"
    assert body["history_count"] == 1
    assert body["readiness_issues"] == []
    run = body["current_run"]
    assert [
        run[key]
        for key in (
            "run_id",
            "created_at",
            "assessment_count",
            "persistence_version",
            "input_assembly_version",
            "recommendation_engine_version",
            "recommendation_rules_version",
            "source_matching_run_id",
            "source_matching_run_fingerprint",
            "batch_fingerprint",
            "run_fingerprint",
        )
    ] == list(run_row)
    assert body["persistence_version"] == run_row[3]
    assert body["input_assembly_version"] == run_row[4]

    # The persisted rank order, item for item; nothing dropped, nothing moved.
    assert len(run["items"]) == len(assessments) == run_row[2] == 5
    assert [item["opportunity_id"] for item in run["items"]] == [
        row[0] for row in assessments
    ]
    assert [item["rank_position"] for item in run["items"]] == [1, 2, 3, 4, 5]
    for item, row in zip(run["items"], assessments, strict=True):
        snapshot = item["recommendation"]
        stored_payload = json.loads(row[6])
        assert snapshot["disposition"] == row[2]
        assert snapshot["recommendation_score"] == row[3]
        assert (snapshot["recommendation_score"] is None) == (row[3] is None)
        assert snapshot["evidence_coverage"] == row[4]
        assert snapshot["assessment_fingerprint"] == row[5]
        assert snapshot["explanation"] == stored_payload
        for partition in ("strengths", "confirmed_gaps", "unknowns"):
            assert snapshot[partition] == stored_payload["result"][partition]

        opportunity = item["opportunity"]
        stored = opportunities[item["opportunity_id"]]
        assert opportunity["id"] == stored[0]
        assert opportunity["canonical_title"] == stored[1]
        assert opportunity["organization"] == stored[2]
        assert opportunity["location"] == stored[3]
        assert opportunity["last_seen_at"] == stored[4]
        expected_url = (
            OFFICIAL_APPLY_URL
            if item["opportunity_id"] == ready_world.ids["strong"]
            else stored[5]
        )
        assert opportunity["original_url"] == expected_url

        primary, secondary, evidence, reasons, version = fine[item["opportunity_id"]]
        assert opportunity["fine_primary_category"] == primary
        assert opportunity["fine_secondary_categories"] == decoded(secondary)
        assert opportunity["fine_category_evidence"] == decoded(evidence)
        assert opportunity["fine_reasons"] == decoded(reasons)
        assert opportunity["fine_classifier_version"] == version

    assert audit.ok is True
    assert body["integrity"] == {
        "ok": True,
        "audit_version": audit.audit_version,
        "audit_fingerprint": audit.audit_fingerprint,
    }


def test_the_get_runs_on_a_query_only_connection_that_cannot_write(
    ready_world, monkeypatch
):
    observed = []
    real = recommendation_api.read_current_recommendation

    def probe(connection, profile_id):
        observed.append(connection.execute("PRAGMA query_only").fetchone()[0])
        try:
            connection.execute(
                "UPDATE recommendation_profile_state SET updated_at=updated_at"
                " WHERE profile_id=?",
                (profile_id,),
            )
        except sqlite3.OperationalError as error:
            observed.append(str(error))
        else:
            observed.append("the write went through")
        return real(connection, profile_id)

    monkeypatch.setattr(recommendation_api, "read_current_recommendation", probe)

    response = get(monkeypatch, ready_world.path, ready_world.profile_id)

    assert response.status_code == 200
    assert observed[0] == 1
    assert "readonly" in observed[1], observed


def test_the_get_mutates_no_row_and_no_byte(ready_world, monkeypatch):
    rows_before = table_rows(ready_world.path)
    digest_before = file_digest(ready_world.path)

    for _ in range(2):
        assert get(monkeypatch, ready_world.path, ready_world.profile_id).status_code == 200

    assert table_rows(ready_world.path) == rows_before
    assert file_digest(ready_world.path) == digest_before


def test_two_reads_of_an_unchanged_state_answer_identically(ready_world, monkeypatch):
    first = get(monkeypatch, ready_world.path, ready_world.profile_id)
    second = get(monkeypatch, ready_world.path, ready_world.profile_id)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()


def test_the_get_computes_synchronizes_and_classifies_nothing(
    ready_world, monkeypatch
):
    reached = []

    def tripwire(name):
        def trip(*args, **kwargs):
            reached.append(name)
            raise AssertionError(f"GET /api/recommendation reached {name}")

        return trip

    for module_name, attribute in COMPUTING_ENTRY_POINTS:
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, attribute, tripwire(f"{module_name}.{attribute}"))

    response = get(monkeypatch, ready_world.path, ready_world.profile_id)

    assert reached == []
    assert response.status_code == 200
    assert response.json()["status"] == "READY"


# --------------------------------------------------------------------------
# NOT_SYNCED and INCOMPLETE
# --------------------------------------------------------------------------


def test_a_profile_that_never_synchronized_is_not_synced(tmp_path, monkeypatch):
    path = tmp_path / "recommendation-api-not-synced.db"
    connection = connect_database(path)
    apply_migrations(connection)
    profile_id = ensure_user_profile(connection, TEST_ONLY_EMAIL).profile_id
    connection.commit()
    connection.close()
    rows_before = table_rows(path, RECOMMENDATION_TABLES)

    response = get(monkeypatch, path, profile_id)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "NOT_SYNCED"
    assert body["current_run"] is None
    assert body["readiness_issues"] == []
    assert body["history_count"] == 0
    assert body["persistence_version"] is None
    assert body["integrity"]["ok"] is True
    assert table_rows(path, RECOMMENDATION_TABLES) == rows_before


def test_an_incomplete_state_keeps_its_issues_and_names_no_earlier_run(
    tmp_path, monkeypatch
):
    connection, path, identity, ids, _ = build_corpus(tmp_path)
    try:
        ready = sync_recommendations(connection, identity.profile_id)
        assert ready.state.value == "READY"
        make_stale(connection, ids["strong"])
        stale = sync_recommendations(connection, identity.profile_id)
        assert stale.state.value == "INCOMPLETE"
        connection.commit()
    finally:
        connection.close()
    rows_before = table_rows(path, RECOMMENDATION_TABLES)

    response = get(monkeypatch, path, identity.profile_id)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "INCOMPLETE"
    # A READY run from before is still history; it is not served as current.
    assert body["history_count"] == 1
    assert body["current_run"] is None
    assert body["readiness_issues"] == [
        {"code": issue.code.value, "opportunity_id": issue.opportunity_id}
        for issue in stale.issues
    ]
    assert body["readiness_issues"] == [
        {"code": "GEOGRAPHY_PROJECTION_STALE", "opportunity_id": ids["strong"]}
    ]
    assert body["integrity"]["ok"] is True
    # The stored message is diagnostic text and stays out of the public body.
    assert stale.issues[0].message not in response.text
    assert table_rows(path, RECOMMENDATION_TABLES) == rows_before


# --------------------------------------------------------------------------
# corruption and inconsistency are refused, never partially served
# --------------------------------------------------------------------------


def test_a_tampered_assessment_fingerprint_is_refused_by_the_audit(
    tmp_path, monkeypatch
):
    fixture, path = light_world(tmp_path)
    stored = fixture.store()
    # The history is sound before the corruption, so the refusal is provably it.
    assert audit_recommendation_profile_history(
        fixture.connection, fixture.profile_id
    ).ok
    fixture.connection.execute(
        "UPDATE recommendation_assessments SET assessment_fingerprint=?"
        " WHERE run_id=? AND rank_position=1",
        ("0" * 64, stored.run_id),
    )
    fixture.connection.commit()
    # The digest still has the *form* of one, so the read model reads it; only
    # the audit can tell it no longer describes the stored payload.
    assert read_current_recommendation(fixture.connection, fixture.profile_id).status == (
        "READY"
    )
    assert not audit_recommendation_profile_history(
        fixture.connection, fixture.profile_id
    ).ok
    fixture.connection.close()

    assert_public_503(get(monkeypatch, path, fixture.profile_id))


def test_a_ranking_with_a_hole_is_refused_by_the_read_model(tmp_path, monkeypatch):
    fixture, path = light_world(tmp_path)
    stored = fixture.store()
    fixture.connection.execute(
        "DELETE FROM recommendation_assessments WHERE run_id=? AND rank_position=2",
        (stored.run_id,),
    )
    fixture.connection.execute(
        "UPDATE recommendation_runs SET assessment_count=2 WHERE id=?", (stored.run_id,)
    )
    fixture.connection.commit()
    fixture.connection.close()

    assert_public_503(get(monkeypatch, path, fixture.profile_id))


def test_a_ranked_posting_whose_metadata_is_gone_fails_the_whole_ranking(
    tmp_path, monkeypatch
):
    fixture, path = light_world(tmp_path)
    fixture.store()
    connection = fixture.connection
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DELETE FROM opportunities WHERE id=?", (fixture.opportunity_ids[1],))
    connection.commit()
    connection.execute("PRAGMA foreign_keys = ON")
    # Opportunity metadata is outside the recommendation snapshot, so both the
    # read model and the audit still accept the history: the refusal below is
    # the surface's own, and it refuses the ranking whole.
    assert read_current_recommendation(connection, fixture.profile_id).status == "READY"
    assert audit_recommendation_profile_history(connection, fixture.profile_id).ok
    connection.close()

    response = get(monkeypatch, path, fixture.profile_id)

    assert_public_503(response)
    assert "items" not in response.text


def test_never_classified_classified_without_category_and_other_stay_distinct(
    tmp_path, monkeypatch
):
    fixture, path = light_world(tmp_path)
    fixture.store()
    other, no_category, never = fixture.opportunity_ids
    _insert_qualification(
        fixture.connection,
        other,
        qualification="ADJACENT_TARGET",
        fine_primary_category="OTHER",
        fine_secondary_categories_json="[]",
        fine_category_evidence_json="[]",
        fine_reasons_json='["TEST ONLY other reason"]',
        fine_classifier_version=FINE_VERSION,
    )
    _insert_qualification(
        fixture.connection,
        no_category,
        qualification="UNCERTAIN",
        fine_primary_category=None,
        fine_secondary_categories_json="[]",
        fine_category_evidence_json="[]",
        fine_reasons_json='["TEST ONLY not qualified"]',
        fine_classifier_version=FINE_VERSION,
    )
    fixture.connection.commit()
    fixture.connection.close()

    response = get(monkeypatch, path, fixture.profile_id)

    assert response.status_code == 200
    by_id = {
        item["opportunity_id"]: item["opportunity"]
        for item in response.json()["current_run"]["items"]
    }
    assert set(by_id) == set(fixture.opportunity_ids)

    assert by_id[other]["fine_primary_category"] == "OTHER"
    assert by_id[other]["fine_secondary_categories"] == []
    assert by_id[other]["fine_category_evidence"] == []
    assert by_id[other]["fine_classifier_version"] == FINE_VERSION

    assert by_id[no_category]["fine_primary_category"] is None
    assert by_id[no_category]["fine_secondary_categories"] == []
    assert by_id[no_category]["fine_reasons"] == ["TEST ONLY not qualified"]
    assert by_id[no_category]["fine_classifier_version"] == FINE_VERSION

    for field in (
        "fine_primary_category",
        "fine_secondary_categories",
        "fine_category_evidence",
        "fine_reasons",
        "fine_classifier_version",
    ):
        assert by_id[never][field] is None, field


# --------------------------------------------------------------------------
# configuration and profile failures
# --------------------------------------------------------------------------


def test_bad_profile_or_configuration_is_a_sanitized_503(tmp_path, monkeypatch):
    fixture, path = light_world(tmp_path)
    fixture.store()
    fixture.connection.close()

    for raw in (None, "0", "-1", "01", "abc"):
        configure(monkeypatch, path, fixture.profile_id)
        if raw is None:
            monkeypatch.delenv("OPPORTUNITY_RADAR_PROFILE_ID")
        else:
            monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", raw)
        assert_public_503(TestClient(app).get("/api/recommendation"))

    # A well-formed profile that does not exist is refused, never substituted.
    assert_public_503(get(monkeypatch, path, 9999))

    # A database the migrations never reached.
    unmigrated = tmp_path / "unmigrated.db"
    connect_database(unmigrated).close()
    assert_public_503(get(monkeypatch, unmigrated, fixture.profile_id))

    # SQLite outside development without a configured path.
    configure(monkeypatch, path, fixture.profile_id)
    monkeypatch.delenv("SQLITE_DATABASE_PATH")
    assert_public_503(TestClient(app).get("/api/recommendation"))


def test_a_missing_database_path_is_a_503_and_creates_nothing(tmp_path, monkeypatch):
    """A GET never brings a database, or the directory holding it, into existence."""
    reached = []
    monkeypatch.setattr(
        recommendation_api,
        "read_current_recommendation",
        lambda *arguments: reached.append(arguments),
    )
    missing_parent = tmp_path / "never-created"
    in_missing_parent = missing_parent / "recommendation.db"
    in_existing_parent = tmp_path / "missing-recommendation.db"

    for missing in (in_missing_parent, in_existing_parent):
        assert_public_503(get(monkeypatch, missing, 1))
        assert not missing.exists(), missing

    assert not missing_parent.exists()
    assert reached == []
