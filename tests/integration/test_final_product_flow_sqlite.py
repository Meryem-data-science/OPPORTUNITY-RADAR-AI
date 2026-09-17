"""Phase 11.2C: the product flow validator over a disposable migrated SQLite file.

The world is the Phase 9A corpus, built through each phase's own entry points,
then carried to the product the way the product itself gets there: Recommendation,
Priority and Portfolio synchronization, TEST-ONLY source runs and observations,
one candidature through the Application service, one browser subscription, the
notification policy baseline, and one frozen (never sent) digest through the
digest persistence owner. All of it happens during *setup*, on a throwaway file.
Every test then copies that file and validates the copy. **The operational
database is never opened.**

Negative fixtures damage only a test's own copy, with foreign keys and triggers
left as SQLite leaves them for a plain connection, to prove the validator reports
what is broken instead of repairing it.
"""

from datetime import date
import hashlib
import importlib
import json
from pathlib import Path
import shutil
import socket
import sqlite3

import pytest

from services.applications.models import ApplicationAction, ApplicationStatus, TrackingUpdate
from services.applications.service import apply_action, change_status, update_tracking
from services.collector.database.connection import connect_database, connect_readonly_database
from services.final_validation import product_flow as flow
from services.final_validation import product_flow_cli as cli
from services.final_validation.operational_state import FAIL, PASS, OperationalStateError
from services.final_validation.upstream_state import (
    DEMONSTRATED,
    NEVER_RUN,
    NOT_ASSESSED,
    NOT_DEMONSTRATED,
    READY,
    STALE,
)
from services.gmail_digest.models import DIGEST_VERSION
from services.gmail_digest.persistence import insert_frozen_digest
from services.notifications.push_subscriptions import subscribe_push_subscription
from services.notifications.sync import sync_notification_policy
from services.portfolio.sync import sync_portfolio
from services.priority.sync import sync_priority
from services.recommendation import sync_recommendations
from tests.integration.test_final_upstream_state_sqlite import _complete_collection_and_provenance
from tests.integration.test_push_subscriptions_sqlite import P256DH
from tests.integration.test_recommendation_sqlite import TEST_ONLY_EMAIL, build_corpus

SIDE_SUFFIXES = ("-wal", "-shm", "-journal")
TEST_ONLY_NOTE = "TEST-ONLY private note about an interview"
TEST_ONLY_NEXT_ACTION = "TEST-ONLY call the recruiter"
TEST_ONLY_ENDPOINT = "https://push.example.invalid/subscription/test-only"
TEST_ONLY_AUTH = "c" * 22
TEST_ONLY_SUBJECT = "TEST-ONLY digest subject"
TEST_ONLY_BODY = "TEST-ONLY digest body"
TEST_ONLY_RECIPIENT = hashlib.sha256(b"TEST-ONLY recipient").hexdigest()

#: Entry points that compute, synchronize, classify, send, write or connect.
COMPUTING_ENTRY_POINTS = (
    ("services.recommendation.sync", "sync_recommendations"),
    ("services.recommendation", "sync_recommendations"),
    ("services.recommendation.engine", "build_recommendation_batch"),
    ("services.recommendation.input_assembly", "assemble_recommendation_inputs"),
    ("services.collector.matching.sync", "sync_matching"),
    ("services.collector.matching.engine", "build_matching_assessments"),
    ("services.collector.matching.selection", "select_matching_opportunity_ids"),
    ("services.collector.qualification.classifier", "classify_opportunity"),
    ("services.collector.qualification.fine_classifier", "classify_fine_categories"),
    ("services.collector.qualification.persistence", "persist_qualifications"),
    ("services.geography.service", "synchronize_location_resolutions"),
    ("services.priority.sync", "sync_priority"),
    ("services.portfolio.sync", "sync_portfolio"),
    ("services.notifications.sync", "sync_notification_policy"),
    ("services.notifications.delivery", "drain_notification_deliveries"),
    ("services.notifications.delivery", "materialize_notification_deliveries"),
    ("services.notifications.push_subscriptions", "subscribe_push_subscription"),
    ("services.gmail_digest.materialize", "materialize_daily_digest"),
    ("services.gmail_digest.delivery", "drain_gmail_digests"),
    ("services.gmail_digest.persistence", "insert_frozen_digest"),
    ("services.applications.service", "apply_action"),
    ("services.applications.service", "change_status"),
    ("services.applications.service", "update_tracking"),
    ("services.applications.repository", "insert_event"),
    ("services.applications.repository", "update_status"),
    ("services.collector.database.connection", "connect_database"),
    ("urllib.request", "urlopen"),
    ("socket", "create_connection"),
)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    directory = tmp_path_factory.mktemp("final-product-flow-world")
    connection, path, identity, ids, _ = build_corpus(directory)
    profile_id = identity.profile_id
    try:
        # Collection publishes postings as `visible`; the Phase 9A corpus inserts them as `new`.
        connection.execute("UPDATE opportunities SET status = 'visible'")
        connection.commit()
        _complete_collection_and_provenance(connection, ids)
        assert sync_recommendations(connection, profile_id).state.value == "READY"
        sync_priority(connection, profile_id, date(2026, 6, 15))
        sync_portfolio(connection, profile_id)
        sync_notification_policy(connection, profile_id)
        subscribe_push_subscription(
            connection, profile_id=profile_id, endpoint=TEST_ONLY_ENDPOINT, p256dh=P256DH, auth=TEST_ONLY_AUTH,
        )
        saved = apply_action(connection, profile_id=profile_id, opportunity_id=ids["strong"], action=ApplicationAction.SAVE)
        change_status(connection, profile_id=profile_id, application_id=saved.application.id, status=ApplicationStatus.SUBMITTED)
        update_tracking(
            connection, profile_id=profile_id, application_id=saved.application.id,
            update=TrackingUpdate(notes=(TEST_ONLY_NOTE,), next_action=(TEST_ONLY_NEXT_ACTION,)),
        )
        run_id, run_fingerprint = connection.execute(
            """SELECT r.id, r.run_fingerprint FROM portfolio_profile_state AS s
                JOIN portfolio_runs AS r ON r.id = s.current_run_id WHERE s.profile_id = ?""",
            (profile_id,),
        ).fetchone()
        connection.execute("BEGIN")
        insert_frozen_digest(
            connection, profile_id=profile_id, digest_date="2099-01-01", timezone="UTC",
            portfolio_run_id=run_id, portfolio_run_fingerprint=run_fingerprint, digest_version=DIGEST_VERSION,
            content_fingerprint=hashlib.sha256(b"TEST-ONLY content").hexdigest(),
            recipient_fingerprint=TEST_ONLY_RECIPIENT, item_count=1, subject=TEST_ONLY_SUBJECT,
            body_text=TEST_ONLY_BODY, body_html=f"<p>{TEST_ONLY_BODY}</p>", now="2099-01-01T00:00:00+00:00",
        )
        connection.execute("COMMIT")
        user_id = connection.execute("SELECT user_id FROM profiles WHERE id = ?", (profile_id,)).fetchone()[0]
        decoy_user = connection.execute("SELECT id FROM users WHERE id != ? ORDER BY id LIMIT 1", (user_id,)).fetchone()[0]
    finally:
        connection.close()
    return {"path": path, "profile_id": profile_id, "ids": ids, "decoy_user": decoy_user}


@pytest.fixture
def copy(world, tmp_path):
    target = tmp_path / "product-copy.db"
    shutil.copy2(world["path"], target)
    return target


def check(evidence, name):
    return next(item for item in evidence["checks"] if item["name"] == name)


def damage(path, *statements):
    """TEST ONLY corruption of a private copy, through a plain connection."""
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


def recommended_ids(path, profile_id):
    connection = connect_readonly_database(path)
    try:
        return [row[0] for row in connection.execute(
            """SELECT a.opportunity_id FROM recommendation_assessments AS a
                JOIN recommendation_profile_state AS s ON s.current_run_id = a.run_id
               WHERE s.profile_id = ? ORDER BY a.rank_position""",
            (profile_id,),
        ).fetchall()]
    finally:
        connection.close()


# --------------------------------------------------------------------------
# the healthy product flow, against independent reads
# --------------------------------------------------------------------------


def test_a_coherent_product_flow_passes_with_every_area_demonstrated(world, copy):
    evidence = flow.validate_product_flow(copy, world["profile_id"])

    assert evidence["integrity_result"] == PASS, [item for item in evidence["checks"] if item["integrity"] != PASS]
    assert [item["name"] for item in evidence["checks"]] == list(flow.CHECK_ORDER)
    for name in ("PRODUCT_RECOMMENDATION_READ_PATH", "PRODUCT_EXPLANATION_EVIDENCE", "PRODUCT_REAL_URL_EVIDENCE",
                 "EXPLORER_PRODUCT_READ_PATH", "APPLICATION_TRACKING_EVIDENCE", "PROACTIVITY_REUSE_STATE"):
        assert check(evidence, name)["demonstrability"] == DEMONSTRATED, check(evidence, name)
    assert check(evidence, "PROACTIVITY_REUSE_STATE")["operational_state"] == READY


def test_the_product_order_is_the_persisted_recommendation_order(world, copy):
    evidence = flow.validate_product_flow(copy, world["profile_id"])

    surface = evidence["recommendation"]["surface"]
    ordered = recommended_ids(copy, world["profile_id"])
    assert surface["item_count"] == len(ordered) > 0
    assert surface["order_matches_canonical"] and surface["rank_positions_contiguous"]
    assert surface["ranking_recomputed"] is False
    assert sum(surface["disposition_counts"].values()) == len(ordered)


def test_explanations_urls_explorer_and_applications_are_the_persisted_ones(world, copy):
    evidence = flow.validate_product_flow(copy, world["profile_id"])

    explanations = evidence["recommendation"]["explanations"]
    assert explanations["explanation_not_persisted_payload_count"] == 0
    assert explanations["items_with_reason"]["unknowns"] >= 0
    for surface in ("recommendation", "explorer", "applications"):
        part = evidence[surface]["urls"]
        assert part["exposed_count"] > 0
        assert part["network_liveness"] == NOT_ASSESSED
        assert (part["missing_count"], part["malformed_count"], part["not_a_persisted_link_count"]) == (0, 0, 0)

    connection = connect_readonly_database(copy)
    try:
        explorer_total = connection.execute(
            """SELECT COUNT(*) FROM opportunities AS o JOIN opportunity_qualifications AS q ON q.opportunity_id = o.id
                WHERE o.status = 'visible' AND o.is_active = 1 AND q.qualification IN ('CORE_TARGET', 'ADJACENT_TARGET')"""
        ).fetchone()[0]
    finally:
        connection.close()
    explorer = evidence["explorer"]["surface"]
    assert explorer["total"] == explorer["items_read"] == explorer_total
    applications = evidence["applications"]["surface"]
    assert (applications["application_count"], applications["status_counts"]) == (1, {"SUBMITTED": 1})
    assert applications["event_type_counts"] == {"APPLICATION_CREATED": 1, "STATUS_CHANGED": 1, "TRACKING_UPDATED": 1}


def test_the_evidence_carries_no_private_value_secret_title_or_url(world, copy):
    serialized = flow.serialize_evidence(flow.validate_product_flow(copy, world["profile_id"]))

    for forbidden in (TEST_ONLY_EMAIL, TEST_ONLY_NOTE, TEST_ONLY_NEXT_ACTION, TEST_ONLY_ENDPOINT, P256DH,
                      TEST_ONLY_AUTH, TEST_ONLY_SUBJECT, TEST_ONLY_BODY, TEST_ONLY_RECIPIENT, "https://",
                      "example.invalid", "TEST ONLY Data Scientist", "TEST ONLY Org", str(copy.parent)):
        assert forbidden not in serialized, forbidden


# --------------------------------------------------------------------------
# read-only guarantees
# --------------------------------------------------------------------------


def test_the_connection_is_read_only_and_every_read_happens_inside_one_rollback_snapshot(world, copy, monkeypatch):
    import services.final_validation.upstream_state as upstream

    statements = []
    opened = []
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
                assert self._connection.execute("PRAGMA query_only").fetchone()[0] == 1
                assert self._connection.in_transaction, normalized
            return self._connection.execute(statement, *parameters)

    def recording(path):
        connection = real(path)
        opened.append(connection)
        return Recording(connection)

    monkeypatch.setattr(upstream, "connect_readonly_database", recording)
    evidence = flow.validate_product_flow(copy, world["profile_id"])

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


def test_no_computing_sending_writing_or_network_entry_point_is_reached(world, copy, monkeypatch):
    def tripwire(*_args, **_kwargs):
        raise AssertionError("the product flow validator reached a computing, writing or network entry point")

    for module_name, attribute in COMPUTING_ENTRY_POINTS:
        monkeypatch.setattr(importlib.import_module(module_name), attribute, tripwire)
    monkeypatch.setattr(socket.socket, "connect", tripwire)

    assert flow.validate_product_flow(copy, world["profile_id"])["integrity_result"] == PASS


def test_the_file_is_unchanged_and_no_side_file_appears(world, copy):
    before = independent_fingerprint(copy)
    assert before[1] == {suffix: None for suffix in SIDE_SUFFIXES}

    evidence = flow.validate_product_flow(copy, world["profile_id"])

    assert independent_fingerprint(copy) == before
    assert evidence["database"]["before"] == evidence["database"]["after"]
    assert evidence["database"]["after"]["side_files"] == {suffix: {"exists": False} for suffix in SIDE_SUFFIXES}
    assert check(evidence, "DATABASE_UNCHANGED")["integrity"] == PASS


def test_a_missing_profile_is_refused_and_the_file_is_left_as_it_was(world, copy):
    before = independent_fingerprint(copy)

    with pytest.raises(OperationalStateError) as refused:
        flow.validate_product_flow(copy, 999_999)

    assert refused.value.code == "PROFILE_NOT_FOUND"
    assert independent_fingerprint(copy) == before


# --------------------------------------------------------------------------
# honest absences
# --------------------------------------------------------------------------


def test_a_profile_with_nothing_persisted_is_not_demonstrated_and_passes(world, copy):
    damage(copy, ("INSERT INTO profiles (user_id) VALUES (?)", (world["decoy_user"],)))
    connection = connect_readonly_database(copy)
    try:
        empty_profile = connection.execute("SELECT id FROM profiles WHERE user_id = ?", (world["decoy_user"],)).fetchone()[0]
    finally:
        connection.close()

    evidence = flow.validate_product_flow(copy, empty_profile)

    assert evidence["integrity_result"] == PASS, [item for item in evidence["checks"] if item["integrity"] != PASS]
    read_path = check(evidence, "PRODUCT_RECOMMENDATION_READ_PATH")
    assert (read_path["demonstrability"], read_path["operational_state"]) == (NOT_DEMONSTRATED, NEVER_RUN)
    assert check(evidence, "PRODUCT_EXPLANATION_EVIDENCE")["demonstrability"] == NOT_ASSESSED
    applications = check(evidence, "APPLICATION_TRACKING_EVIDENCE")
    assert (applications["demonstrability"], applications["evidence"]["application_count"]) == (NOT_DEMONSTRATED, 0)
    proactivity = check(evidence, "PROACTIVITY_REUSE_STATE")
    assert (proactivity["demonstrability"], proactivity["operational_state"]) == (NOT_DEMONSTRATED, NEVER_RUN)
    # The Explorer is profile-independent and stays demonstrated.
    assert check(evidence, "EXPLORER_PRODUCT_READ_PATH")["demonstrability"] == DEMONSTRATED


def test_no_policy_state_and_no_digest_is_never_run(world, copy):
    damage(copy, ("DELETE FROM notification_policy_state", ()), ("DELETE FROM gmail_digest_outbox", ()))

    evidence = flow.validate_product_flow(copy, world["profile_id"])

    proactivity = check(evidence, "PROACTIVITY_REUSE_STATE")
    assert (proactivity["integrity"], proactivity["operational_state"]) == (PASS, NEVER_RUN)
    assert evidence["integrity_result"] == PASS


def test_proactivity_built_from_an_older_matching_run_is_stale_and_passes(world, copy):
    from services.collector.matching import sync_matching

    damage(copy, ("UPDATE opportunities SET description = description || ' TEST ONLY newer text' WHERE id = ?",
                  (world["ids"]["strong"],)))
    connection = connect_database(copy)
    try:
        assert sync_matching(connection, world["profile_id"]).state == "READY"
    finally:
        connection.close()

    evidence = flow.validate_product_flow(copy, world["profile_id"])

    proactivity = check(evidence, "PROACTIVITY_REUSE_STATE")
    assert (proactivity["integrity"], proactivity["demonstrability"], proactivity["operational_state"]) == (
        PASS, NOT_DEMONSTRATED, STALE)
    assert proactivity["evidence"]["comparisons"]["portfolio_built_from_current_matching_run"] is False
    assert proactivity["evidence"]["comparisons"]["notification_cursor_is_current_portfolio_run"] is True
    assert evidence["integrity_result"] == PASS


# --------------------------------------------------------------------------
# corruption, reported and never repaired
# --------------------------------------------------------------------------


def test_an_explanation_code_in_the_wrong_partition_fails(world, copy):
    connection = connect_readonly_database(copy)
    try:
        row_id, payload = connection.execute(
            """SELECT a.id, a.assessment_payload_json FROM recommendation_assessments AS a
                JOIN recommendation_profile_state AS s ON s.current_run_id = a.run_id
               WHERE s.profile_id = ? ORDER BY a.rank_position LIMIT 1""",
            (world["profile_id"],),
        ).fetchone()
    finally:
        connection.close()
    decoded = json.loads(payload)
    decoded["result"]["strengths"] = list(decoded["result"]["strengths"]) + ["GEOGRAPHY_OUT_OF_TARGET"]
    damage(copy, ("UPDATE recommendation_assessments SET assessment_payload_json = ? WHERE id = ?",
                  (json.dumps(decoded, separators=(",", ":"), sort_keys=True), row_id)))
    before = independent_fingerprint(copy)

    evidence = flow.validate_product_flow(copy, world["profile_id"])

    assert evidence["integrity_result"] == FAIL
    failed = {item["name"] for item in evidence["checks"] if item["integrity"] == FAIL}
    assert failed & {"PRODUCT_EXPLANATION_EVIDENCE", "PRODUCT_RECOMMENDATION_READ_PATH"}
    assert independent_fingerprint(copy) == before


def test_a_recommended_opportunity_that_disappeared_fails_the_read_path(world, copy):
    damage(copy, ("DELETE FROM opportunities WHERE id = ?", (recommended_ids(copy, world["profile_id"])[0],)))

    evidence = flow.validate_product_flow(copy, world["profile_id"])

    read_path = check(evidence, "PRODUCT_RECOMMENDATION_READ_PATH")
    assert (read_path["integrity"], read_path["demonstrability"]) == (FAIL, NOT_ASSESSED)
    assert evidence["integrity_result"] == FAIL


@pytest.mark.parametrize("value, field", [("not a url", "malformed_count"), ("", "missing_count")])
def test_a_malformed_or_missing_exposed_link_fails_url_evidence(world, copy, value, field):
    target = recommended_ids(copy, world["profile_id"])[0]
    damage(
        copy,
        ("DELETE FROM opportunity_sources WHERE opportunity_id = ?", (target,)),
        ("UPDATE opportunities SET source_url = ?, application_url = NULL, canonical_url = NULL WHERE id = ?",
         (value, target)),
    )

    evidence = flow.validate_product_flow(copy, world["profile_id"])

    urls = check(evidence, "PRODUCT_REAL_URL_EVIDENCE")
    assert urls["integrity"] == FAIL
    assert urls["evidence"]["network_liveness"] == NOT_ASSESSED
    assert evidence["recommendation"]["urls"][field] == 1
    assert target in evidence["recommendation"]["urls"]["malformed_or_missing_opportunity_ids"]


def test_a_malformed_persisted_phase8_row_fails_the_explorer_read_path(world, copy):
    damage(copy, ("UPDATE opportunity_qualifications SET fine_primary_category = NULL WHERE opportunity_id = ?",
                  (world["ids"]["strong"],)))

    evidence = flow.validate_product_flow(copy, world["profile_id"])

    explorer = check(evidence, "EXPLORER_PRODUCT_READ_PATH")
    assert explorer["integrity"] == FAIL
    assert evidence["integrity_result"] == FAIL


def test_an_application_whose_status_disagrees_with_its_history_fails(world, copy):
    damage(copy, ("UPDATE applications SET status = 'REJECTED' WHERE opportunity_id = ?", (world["ids"]["strong"],)))

    evidence = flow.validate_product_flow(copy, world["profile_id"])

    applications = check(evidence, "APPLICATION_TRACKING_EVIDENCE")
    assert (applications["integrity"], applications["evidence"]["history_incoherent_count"]) == (FAIL, 1)


def test_a_notification_cursor_on_a_missing_portfolio_run_fails(world, copy):
    damage(copy, ("UPDATE notification_policy_state SET baseline_portfolio_run_id = 999999, "
                  "last_processed_portfolio_run_id = 999999, highest_seen_portfolio_run_id = 999999", ()))

    proactivity = check(flow.validate_product_flow(copy, world["profile_id"]), "PROACTIVITY_REUSE_STATE")

    assert proactivity["integrity"] == FAIL


def test_a_stored_policy_version_the_owner_would_refuse_fails_and_is_not_stale(world, copy):
    damage(copy, ("UPDATE notification_policy_state SET policy_version = 'TEST-ONLY-other-policy'", ()))

    evidence = flow.validate_product_flow(copy, world["profile_id"])

    proactivity = check(evidence, "PROACTIVITY_REUSE_STATE")
    assert (proactivity["integrity"], proactivity["operational_state"]) == (FAIL, None)
    assert proactivity["evidence"]["violated_notification_policy_invariants"] == ["policy_version_matches_running_policy"]
    assert evidence["integrity_result"] == FAIL


@pytest.mark.parametrize("pointer, invariant", [
    ("baseline_portfolio_run_id", "high_water_not_behind_baseline"),
    ("last_processed_portfolio_run_id", "high_water_not_behind_cursor"),
])
def test_a_high_water_mark_behind_the_pointers_it_bounds_fails(world, copy, pointer, invariant):
    # The schema's CHECK constraints forbid this row; they are switched off on
    # this disposable copy only, to prove the validator does not trust them.
    connection = sqlite3.connect(copy)
    try:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(f"UPDATE notification_policy_state SET {pointer} = highest_seen_portfolio_run_id + 1")
        connection.commit()
        lowered = connection.execute(
            f"SELECT highest_seen_portfolio_run_id, {pointer} FROM notification_policy_state"
        ).fetchone()
    finally:
        connection.close()
    assert lowered[0] < lowered[1]

    evidence = flow.validate_product_flow(copy, world["profile_id"])

    proactivity = check(evidence, "PROACTIVITY_REUSE_STATE")
    assert (proactivity["integrity"], proactivity["operational_state"]) == (FAIL, None)
    # The raised pointer names no stored run as well, which the owner's foreign key would also refuse.
    assert proactivity["evidence"]["violated_notification_policy_invariants"] == [
        "pointers_reference_existing_runs", invariant,
    ]


# --------------------------------------------------------------------------
# the CLI
# --------------------------------------------------------------------------


def test_the_cli_prints_deterministic_evidence_and_exits_by_integrity_only(world, copy, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(copy))
    monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", str(world["profile_id"]))
    damage(copy, ("DELETE FROM gmail_digest_outbox", ()), ("DELETE FROM notification_policy_state", ()))

    first = cli.main()
    first_output = capsys.readouterr().out
    second = cli.main()
    second_output = capsys.readouterr().out

    # NEVER_RUN proactivity is visible and the exit code is still zero.
    assert (first, second) == (cli.EXIT_PASS, cli.EXIT_PASS)
    assert first_output == second_output
    parsed = json.loads(first_output)
    assert check(parsed, "PROACTIVITY_REUSE_STATE")["operational_state"] == NEVER_RUN

    damage(copy, ("UPDATE applications SET status = 'REJECTED'", ()))
    assert cli.main() == cli.EXIT_FAIL
