"""Phase 11.2C: unit coverage for the read-only product flow validator.

`build_checks` is pure over an evidence dictionary, so the status model is
tested directly on shapes like the readers produce. URL shape, URL linkage and
application history coherence are tested on plain values. Static tripwires keep
computing, writing, sending and network code out of the module. The real read
paths and a migrated SQLite file are exercised in the integration test.
"""

import ast
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.final_validation import product_flow as flow
from services.final_validation import product_flow_cli as cli
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

MODULE_PATH = Path(flow.__file__)


# --------------------------------------------------------------------------
# a healthy evidence dictionary, shaped like the readers' output
# --------------------------------------------------------------------------


def urls(count=3, **overrides):
    part = {
        "exposed_count": count, "shape_counts": {"WELL_FORMED": count}, "missing_count": 0, "malformed_count": 0,
        "malformed_or_missing_opportunity_ids": [], "not_a_persisted_link_count": 0,
        "not_a_persisted_link_opportunity_ids": [], "scheme_counts": {"https": count}, "distinct_host_count": 1,
        "with_source_observation_count": count, "network_liveness": NOT_ASSESSED,
    }
    part.update(overrides)
    return part


def healthy():
    return {
        "database": {"query_only": 1, "before_stable": True, "after_stable": True, "identical": True, "unchanged": True},
        "recommendation": {
            "canonical": {"available": True, "error_code": None, "status": "READY", "current_run_id": 4, "history_count": 1},
            "surface": {
                "available": True, "error_code": None, "status": "READY", "integrity_ok": True,
                "status_matches_canonical": True, "run_present_in_both": True, "run_id": 4,
                "run_id_matches_canonical": True, "run_fingerprint_matches_canonical": True,
                "source_matching_run_id": 2, "item_count": 3, "canonical_assessment_count": 3,
                "order_matches_canonical": True, "rank_positions_contiguous": True,
                "duplicate_opportunity_count": 0, "orphan_opportunity_count": 0, "orphan_opportunity_ids": [],
                "item_identity_mismatch_count": 0, "assessment_fingerprint_mismatch_count": 0,
                "disposition_mismatch_count": 0,
                "disposition_counts": {"RECOMMENDED": 0, "UNCERTAIN": 1, "OUTSIDE_PREFERENCES": 2, "KNOWN_BLOCKER": 0},
                "ranking_recomputed": False,
            },
            "explanations": {
                "item_count": 3, "reason_code_counts": {"strengths": 1, "confirmed_gaps": 2, "unknowns": 4},
                "items_with_reason": {"strengths": 1, "confirmed_gaps": 2, "unknowns": 3},
                "items_with_code_outside_partition": 0, "explanation_not_persisted_payload_count": 0,
            },
            "urls": urls(),
        },
        "explorer": {
            "surface": {
                "available": True, "error_code": None, "total": 3, "items_read": 3, "pages_read_to_total": True,
                "duplicate_opportunity_count": 0, "orphan_opportunity_count": 0, "without_phase8_row_count": 0,
                "outside_persisted_universe_count": 0, "malformed_phase8_row_count": 0,
                "fine_category_not_persisted_value_count": 0, "opportunity_type_not_persisted_value_count": 0,
                "live_classification": False,
            },
            "urls": urls(),
        },
        "applications": {
            "surface": {
                "available": True, "error_code": None, "application_count": 1, "status_counts": {"SUBMITTED": 1},
                "event_count": 2, "history_incoherent_count": 0, "opportunity_identity_mismatch_count": 0,
                "writes_performed": False,
            },
            "urls": urls(1),
        },
        "proactivity": {
            "available": True, "error_code": None, "basis": "PORTFOLIO_AUXILIARY_NOT_RANKING_AUTHORITY",
            "portfolio_current_run_id": 5, "portfolio_matching_run_id": 2, "matching_current_run_id": 2,
            "notification_policy": {
                "baseline_portfolio_run_id": 5, "last_processed_portfolio_run_id": 5, "highest_seen_portfolio_run_id": 5,
                "pointers_complete": True, "pointers_reference_existing_runs": True,
                "policy_version_matches_running_policy": True,
                "high_water_not_behind_baseline": True, "high_water_not_behind_cursor": True,
            },
            "push_subscription_status_counts": {"ACTIVE": 1}, "push_watermarks_above_event_stream": 0,
            "digest": {"total_count": 1, "status_counts": {"SENT": 1}, "digests_referencing_missing_portfolio_run": 0},
            "comparisons": {
                "notification_cursor_is_current_portfolio_run": True, "latest_digest_is_current_portfolio_run": True,
                "portfolio_built_from_current_matching_run": True, "portfolio_matching_run_is_recommendation_source_run": True,
            },
        },
    }


def checks_of(evidence):
    return {item["name"]: item for item in flow.build_checks(evidence)}


def test_a_healthy_evidence_passes_every_check_in_the_declared_order():
    checks = flow.build_checks(healthy())

    assert [item["name"] for item in checks] == list(flow.CHECK_ORDER)
    assert {item["integrity"] for item in checks} == {PASS}
    for item in checks:
        assert set(item) == {"name", "integrity", "demonstrability", "operational_state", "severity", "evidence"}
        assert item["integrity"] in (PASS, FAIL)
        assert item["demonstrability"] in (DEMONSTRATED, NOT_DEMONSTRATED, NOT_ASSESSED)
        assert item["severity"] == INFORMATIONAL, item


def test_no_score_is_invented_and_ranking_is_never_recomputed():
    source = MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in ("quality_score", "validation_score", "global_score", "overall_score"):
        assert forbidden not in source
    evidence = checks_of(healthy())["PRODUCT_RECOMMENDATION_READ_PATH"]["evidence"]
    assert evidence["ranking_recomputed"] is False
    assert "score" not in json.dumps(flow.build_checks(healthy()))


# --------------------------------------------------------------------------
# Recommendation read path
# --------------------------------------------------------------------------


def test_zero_recommended_outcomes_are_reported_and_not_a_failure():
    check = checks_of(healthy())["PRODUCT_RECOMMENDATION_READ_PATH"]

    assert (check["integrity"], check["demonstrability"]) == (PASS, DEMONSTRATED)
    assert check["evidence"]["disposition_counts"]["RECOMMENDED"] == 0


@pytest.mark.parametrize("field, value", [
    ("order_matches_canonical", False),
    ("run_id_matches_canonical", False),
    ("run_fingerprint_matches_canonical", False),
    ("rank_positions_contiguous", False),
    ("integrity_ok", False),
    ("item_count", 2),
    ("orphan_opportunity_count", 1),
    ("duplicate_opportunity_count", 1),
    ("item_identity_mismatch_count", 1),
    ("assessment_fingerprint_mismatch_count", 1),
    ("disposition_mismatch_count", 1),
])
def test_broken_product_continuity_fails_the_recommendation_read_path(field, value):
    evidence = healthy()
    evidence["recommendation"]["surface"][field] = value

    check = checks_of(evidence)["PRODUCT_RECOMMENDATION_READ_PATH"]

    assert (check["integrity"], check["demonstrability"], check["severity"]) == (FAIL, NOT_DEMONSTRATED, BLOCKER)


@pytest.mark.parametrize("status, state", [("NOT_SYNCED", NEVER_RUN), ("INCOMPLETE", "INCOMPLETE")])
def test_no_ready_recommendation_is_not_demonstrated_and_passes(status, state):
    evidence = healthy()
    evidence["recommendation"]["canonical"].update(status=status, current_run_id=None)
    evidence["recommendation"]["surface"] = {
        "available": True, "error_code": None, "status": status, "integrity_ok": True,
        "status_matches_canonical": True, "run_present_in_both": True,
    }
    evidence["recommendation"]["explanations"] = None
    evidence["recommendation"]["urls"] = None

    checks = checks_of(evidence)

    read_path = checks["PRODUCT_RECOMMENDATION_READ_PATH"]
    assert (read_path["integrity"], read_path["demonstrability"], read_path["operational_state"]) == (PASS, NOT_DEMONSTRATED, state)
    explanation = checks["PRODUCT_EXPLANATION_EVIDENCE"]
    assert (explanation["integrity"], explanation["demonstrability"]) == (PASS, NOT_ASSESSED)


def test_a_surface_that_disagrees_with_the_canonical_state_fails():
    evidence = healthy()
    evidence["recommendation"]["surface"] = {
        "available": True, "error_code": None, "status": "NOT_SYNCED", "integrity_ok": True,
        "status_matches_canonical": False, "run_present_in_both": False,
    }

    assert checks_of(evidence)["PRODUCT_RECOMMENDATION_READ_PATH"]["integrity"] == FAIL


@pytest.mark.parametrize("part, name", [
    ("canonical", "PRODUCT_RECOMMENDATION_READ_PATH"),
    ("surface", "PRODUCT_RECOMMENDATION_READ_PATH"),
])
def test_a_refused_recommendation_read_fails_and_is_not_assessed(part, name):
    evidence = healthy()
    evidence["recommendation"][part] = {"available": False, "error_code": "TestOnlyError"}

    check = checks_of(evidence)[name]

    assert (check["integrity"], check["demonstrability"], check["evidence"]) == (FAIL, NOT_ASSESSED, {"error_code": "TestOnlyError"})


# --------------------------------------------------------------------------
# explanations: UNKNOWN is a question, not a failure
# --------------------------------------------------------------------------


def test_unknown_reasons_everywhere_are_not_a_failure():
    evidence = healthy()
    evidence["recommendation"]["explanations"].update(
        reason_code_counts={"strengths": 0, "confirmed_gaps": 0, "unknowns": 9},
        items_with_reason={"strengths": 0, "confirmed_gaps": 0, "unknowns": 3},
    )

    check = checks_of(evidence)["PRODUCT_EXPLANATION_EVIDENCE"]

    assert (check["integrity"], check["demonstrability"]) == (PASS, DEMONSTRATED)


@pytest.mark.parametrize("field", ["items_with_code_outside_partition", "explanation_not_persisted_payload_count"])
def test_an_explanation_that_is_not_the_persisted_one_fails(field):
    evidence = healthy()
    evidence["recommendation"]["explanations"][field] = 1

    assert checks_of(evidence)["PRODUCT_EXPLANATION_EVIDENCE"]["integrity"] == FAIL


def test_the_reason_partition_is_the_owner_partition():
    from services.recommendation.models import CONFIRMED_GAP_CODES, STRENGTH_CODES, UNKNOWN_CODES

    assert flow.REASON_PARTITIONS["strengths"] == {code.value for code in STRENGTH_CODES}
    assert flow.REASON_PARTITIONS["confirmed_gaps"] == {code.value for code in CONFIRMED_GAP_CODES}
    assert flow.REASON_PARTITIONS["unknowns"] == {code.value for code in UNKNOWN_CODES}


# --------------------------------------------------------------------------
# URLs: shape and linkage, never liveness
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value, shape", [
    ("https://example.invalid/jobs/1", "WELL_FORMED"),
    ("http://example.invalid", "WELL_FORMED"),
    ("  https://example.invalid/x  ", "WELL_FORMED"),
    ("", "MISSING"),
    ("   ", "MISSING"),
    (None, "MISSING"),
    ("not a url", "MALFORMED"),
    ("ftp://example.invalid/file", "MALFORMED"),
    ("javascript:alert(1)", "MALFORMED"),
    ("https://", "MALFORMED"),
    ("http://[::1", "MALFORMED"),
])
def test_url_shape_is_parsed_locally(value, shape):
    assert flow.classify_url(value) == shape


def test_url_evidence_requires_a_persisted_link_and_never_assesses_liveness():
    links = {
        1: {"values": {"https://example.invalid/1"}, "observations": 1},
        2: {"values": {"https://example.invalid/2"}, "observations": 0},
        3: {"values": set(), "observations": 0},
    }
    exposed = [(1, "https://example.invalid/1"), (2, "https://example.invalid/invented"), (3, "")]

    evidence = flow.url_evidence(exposed, links)

    assert evidence["network_liveness"] == NOT_ASSESSED
    assert (evidence["missing_count"], evidence["malformed_count"]) == (1, 0)
    assert evidence["not_a_persisted_link_opportunity_ids"] == [2]
    assert evidence["with_source_observation_count"] == 1
    assert "example.invalid" not in json.dumps(evidence)


@pytest.mark.parametrize("field", ["missing_count", "malformed_count", "not_a_persisted_link_count"])
def test_an_invalid_exposed_link_fails_url_evidence_on_any_surface(field):
    for surface in ("recommendation", "explorer", "applications"):
        evidence = healthy()
        target = evidence[surface]["urls"]
        target[field] = 1

        check = checks_of(evidence)["PRODUCT_REAL_URL_EVIDENCE"]

        assert check["integrity"] == FAIL
        assert check["evidence"]["network_liveness"] == NOT_ASSESSED


def test_well_formed_links_pass_while_liveness_stays_not_assessed():
    check = checks_of(healthy())["PRODUCT_REAL_URL_EVIDENCE"]

    assert (check["integrity"], check["demonstrability"]) == (PASS, DEMONSTRATED)
    assert check["evidence"]["network_liveness"] == NOT_ASSESSED
    assert "URL_NETWORK_LIVENESS" in {name for name, _ in flow.NOT_ASSESSED_DIMENSIONS}


def test_no_exposed_link_anywhere_is_not_assessed_and_passes():
    evidence = healthy()
    for surface in ("recommendation", "explorer", "applications"):
        evidence[surface]["urls"] = None

    check = checks_of(evidence)["PRODUCT_REAL_URL_EVIDENCE"]

    assert (check["integrity"], check["demonstrability"]) == (PASS, NOT_ASSESSED)


# --------------------------------------------------------------------------
# Explorer
# --------------------------------------------------------------------------


@pytest.mark.parametrize("field", [
    "duplicate_opportunity_count", "orphan_opportunity_count", "without_phase8_row_count",
    "outside_persisted_universe_count", "malformed_phase8_row_count",
    "fine_category_not_persisted_value_count", "opportunity_type_not_persisted_value_count",
])
def test_an_explorer_item_not_backed_by_its_persisted_phase8_row_fails(field):
    evidence = healthy()
    evidence["explorer"]["surface"][field] = 1

    assert checks_of(evidence)["EXPLORER_PRODUCT_READ_PATH"]["integrity"] == FAIL


def test_explorer_pages_that_do_not_reach_the_total_fail():
    evidence = healthy()
    evidence["explorer"]["surface"]["pages_read_to_total"] = False

    assert checks_of(evidence)["EXPLORER_PRODUCT_READ_PATH"]["integrity"] == FAIL


def test_an_empty_explorer_is_not_demonstrated_and_passes():
    evidence = healthy()
    evidence["explorer"]["surface"].update(total=0, items_read=0)

    check = checks_of(evidence)["EXPLORER_PRODUCT_READ_PATH"]

    assert (check["integrity"], check["demonstrability"]) == (PASS, NOT_DEMONSTRATED)


def test_the_explorer_clock_is_never_a_real_clock():
    with pytest.raises(RuntimeError, match="EXPLORER_CLOCK_CONSULTED"):
        flow._explorer_clock()


# --------------------------------------------------------------------------
# Application Tracking
# --------------------------------------------------------------------------


def test_no_persisted_application_is_not_demonstrated_and_passes():
    evidence = healthy()
    evidence["applications"]["surface"].update(application_count=0, status_counts={}, event_count=0)

    check = checks_of(evidence)["APPLICATION_TRACKING_EVIDENCE"]

    assert (check["integrity"], check["demonstrability"], check["severity"]) == (PASS, NOT_DEMONSTRATED, WARNING)


@pytest.mark.parametrize("field", ["history_incoherent_count", "opportunity_identity_mismatch_count"])
def test_an_incoherent_application_fails(field):
    evidence = healthy()
    evidence["applications"]["surface"][field] = 1

    assert checks_of(evidence)["APPLICATION_TRACKING_EVIDENCE"]["integrity"] == FAIL


def event(kind, from_status=None, to_status=None):
    return SimpleNamespace(event_type=kind, from_status=from_status, to_status=to_status)


@pytest.mark.parametrize("status, events, coherent", [
    ("SAVED", [event("APPLICATION_CREATED", None, "SAVED")], True),
    ("SUBMITTED", [event("APPLICATION_CREATED", None, "SAVED"), event("STATUS_CHANGED", "SAVED", "SUBMITTED"),
                   event("TRACKING_UPDATED")], True),
    ("REJECTED", [event("APPLICATION_CREATED", None, "SAVED"), event("STATUS_CHANGED", "SAVED", "SUBMITTED")], False),
    ("SUBMITTED", [event("STATUS_CHANGED", "SAVED", "SUBMITTED")], False),
    ("SUBMITTED", [event("APPLICATION_CREATED", None, "SAVED"), event("STATUS_CHANGED", "PREPARING", "SUBMITTED")], False),
    ("SAVED", [event("APPLICATION_CREATED", None, "SAVED"), event("APPLICATION_CREATED", None, "SAVED")], False),
    ("SAVED", [], False),
])
def test_application_history_must_walk_from_creation_to_the_current_status(status, events, coherent):
    assert flow._history_coherent(status, tuple(events)) is coherent


# --------------------------------------------------------------------------
# Proactivity: STALE, NEVER_RUN and UNKNOWN are states, not corruption
# --------------------------------------------------------------------------


def test_current_proactivity_is_ready_and_demonstrated():
    check = checks_of(healthy())["PROACTIVITY_REUSE_STATE"]

    assert (check["integrity"], check["demonstrability"], check["operational_state"]) == (PASS, DEMONSTRATED, READY)


@pytest.mark.parametrize("comparison", [
    "notification_cursor_is_current_portfolio_run", "latest_digest_is_current_portfolio_run",
    "portfolio_built_from_current_matching_run", "portfolio_matching_run_is_recommendation_source_run",
])
def test_an_older_upstream_run_is_stale_and_never_a_failure(comparison):
    evidence = healthy()
    evidence["proactivity"]["comparisons"][comparison] = False

    check = checks_of(evidence)["PROACTIVITY_REUSE_STATE"]

    assert (check["integrity"], check["demonstrability"], check["operational_state"], check["severity"]) == (
        PASS, NOT_DEMONSTRATED, STALE, WARNING)


def test_an_older_portfolio_matching_basis_alone_is_pass_stale_not_demonstrated():
    evidence = healthy()
    evidence["proactivity"]["comparisons"].update(
        portfolio_built_from_current_matching_run=False, portfolio_matching_run_is_recommendation_source_run=False,
    )

    check = checks_of(evidence)["PROACTIVITY_REUSE_STATE"]

    assert (check["integrity"], check["demonstrability"], check["operational_state"]) == (PASS, NOT_DEMONSTRATED, STALE)
    assert check["evidence"]["violated_notification_policy_invariants"] == []


@pytest.mark.parametrize("invariant", [
    "pointers_complete", "pointers_reference_existing_runs", "policy_version_matches_running_policy",
    "high_water_not_behind_baseline", "high_water_not_behind_cursor",
])
def test_an_owner_invalid_notification_policy_fails_and_is_never_ordinary_stale(invariant):
    evidence = healthy()
    evidence["proactivity"]["notification_policy"][invariant] = False

    check = checks_of(evidence)["PROACTIVITY_REUSE_STATE"]

    assert (check["integrity"], check["demonstrability"], check["severity"]) == (FAIL, NOT_DEMONSTRATED, BLOCKER)
    assert check["operational_state"] != STALE
    assert check["evidence"]["violated_notification_policy_invariants"] == [invariant]


def test_a_policy_row_missing_an_invariant_flag_is_not_healthy():
    evidence = healthy()
    del evidence["proactivity"]["notification_policy"]["high_water_not_behind_cursor"]

    assert checks_of(evidence)["PROACTIVITY_REUSE_STATE"]["integrity"] == FAIL


def policy_of(baseline, cursor, highest, version=None, runs=(1, 2, 3)):
    from services.notifications.policy import NOTIFICATION_POLICY_VERSION

    return flow.notification_policy_evidence(
        (baseline, cursor, highest, NOTIFICATION_POLICY_VERSION if version is None else version), set(runs)
    )


def violated(policy):
    return [name for name in flow.NOTIFICATION_POLICY_INVARIANTS if policy[name] is not True]


def test_a_well_formed_policy_row_violates_nothing():
    assert violated(policy_of(1, 2, 3)) == []
    assert violated(policy_of(2, 2, 2)) == []


@pytest.mark.parametrize("state, expected", [
    ((1, 2, 3, "TEST-ONLY-other-policy"), ["policy_version_matches_running_policy"]),
    ((3, 2, 2, None), ["high_water_not_behind_baseline"]),
    ((1, 3, 2, None), ["high_water_not_behind_cursor"]),
    ((1, 2, 99, None), ["pointers_reference_existing_runs"]),
    ((None, 2, 3, None), ["pointers_complete", "pointers_reference_existing_runs",
                          "high_water_not_behind_baseline", "high_water_not_behind_cursor"]),
    ((1, None, 3, None), ["pointers_complete", "pointers_reference_existing_runs",
                          "high_water_not_behind_baseline", "high_water_not_behind_cursor"]),
    ((1, 2, None, None), ["pointers_complete", "pointers_reference_existing_runs",
                          "high_water_not_behind_baseline", "high_water_not_behind_cursor"]),
    ((0, 2, 3, None), ["pointers_complete", "pointers_reference_existing_runs",
                       "high_water_not_behind_baseline", "high_water_not_behind_cursor"]),
    ((True, 2, 3, None), ["pointers_complete", "pointers_reference_existing_runs",
                          "high_water_not_behind_baseline", "high_water_not_behind_cursor"]),
])
def test_owner_refused_policy_rows_are_detected_and_null_pointers_never_look_valid(state, expected):
    assert violated(policy_of(*state)) == expected


def test_a_policy_without_a_current_portfolio_still_fails():
    evidence = healthy()
    evidence["proactivity"]["portfolio_current_run_id"] = None

    check = checks_of(evidence)["PROACTIVITY_REUSE_STATE"]

    assert (check["integrity"], check["operational_state"]) == (FAIL, None)


def test_no_policy_state_and_no_digest_is_never_run():
    evidence = healthy()
    proactivity = evidence["proactivity"]
    proactivity["notification_policy"] = None
    proactivity["digest"] = {"total_count": 0, "status_counts": {}, "digests_referencing_missing_portfolio_run": 0}
    proactivity["comparisons"] = dict.fromkeys(proactivity["comparisons"])

    check = checks_of(evidence)["PROACTIVITY_REUSE_STATE"]

    assert (check["integrity"], check["demonstrability"], check["operational_state"]) == (PASS, NOT_DEMONSTRATED, NEVER_RUN)


def test_a_digest_without_a_current_portfolio_is_unknown_and_not_false():
    evidence = healthy()
    proactivity = evidence["proactivity"]
    proactivity["notification_policy"] = None
    proactivity["portfolio_current_run_id"] = None
    proactivity["comparisons"] = dict.fromkeys(proactivity["comparisons"])

    check = checks_of(evidence)["PROACTIVITY_REUSE_STATE"]

    assert (check["integrity"], check["demonstrability"], check["operational_state"]) == (PASS, NOT_DEMONSTRATED, UNKNOWN)


@pytest.mark.parametrize("mutate", [
    lambda p: p["notification_policy"].update(pointers_reference_existing_runs=False),
    lambda p: p.update(portfolio_current_run_id=None),
    lambda p: p["digest"].update(digests_referencing_missing_portfolio_run=1),
    lambda p: p.update(push_watermarks_above_event_stream=1),
])
def test_a_broken_proactivity_reference_fails(mutate):
    evidence = healthy()
    mutate(evidence["proactivity"])

    assert checks_of(evidence)["PROACTIVITY_REUSE_STATE"]["integrity"] == FAIL


@pytest.mark.parametrize("area, name", [
    ("explorer", "EXPLORER_PRODUCT_READ_PATH"),
    ("applications", "APPLICATION_TRACKING_EVIDENCE"),
])
def test_a_refused_product_read_fails_and_is_not_assessed(area, name):
    evidence = healthy()
    evidence[area] = {"surface": {"available": False, "error_code": "TestOnlyError"}, "urls": None}

    check = checks_of(evidence)[name]

    assert (check["integrity"], check["demonstrability"]) == (FAIL, NOT_ASSESSED)


def test_a_refused_proactivity_read_fails_and_is_not_assessed():
    evidence = healthy()
    evidence["proactivity"] = {"available": False, "error_code": "TestOnlyError"}

    check = checks_of(evidence)["PROACTIVITY_REUSE_STATE"]

    assert (check["integrity"], check["demonstrability"]) == (FAIL, NOT_ASSESSED)


def test_database_and_query_only_failures_fail_integrity():
    evidence = healthy()
    evidence["database"].update(query_only=0, unchanged=False, identical=False)

    checks = checks_of(evidence)

    assert checks["SQLITE_QUERY_ONLY"]["integrity"] == FAIL
    assert checks["DATABASE_UNCHANGED"]["integrity"] == FAIL


def test_the_demonstrability_summary_lists_the_dimensions_never_assessed():
    summary = flow.demonstrability_summary(flow.build_checks(healthy()))

    assert {name for name, _ in flow.NOT_ASSESSED_DIMENSIONS} <= set(summary[NOT_ASSESSED])


# --------------------------------------------------------------------------
# static tripwires
# --------------------------------------------------------------------------


FORBIDDEN_NAMES = {
    # computation and synchronization
    "sync_recommendations", "build_recommendation_batch", "assemble_recommendation_inputs",
    "sync_matching", "build_matching_assessments", "select_matching_opportunity_ids",
    "classify_opportunity", "classify_fine_categories", "persist_qualifications",
    "resolve_location_text", "synchronize_location_resolutions", "audit_opportunities", "compare_pair",
    "sync_priority", "sync_portfolio", "sync_notification_policy",
    # sending and delivery
    "materialize_daily_digest", "drain_gmail_digests", "claim_due_digest", "record_delivery_success",
    "GmailApiSender", "drain_notification_deliveries", "materialize_notification_deliveries",
    "materialize_delivery_batches", "WebPushDeliverySender", "HttpxWebPushTransport", "build_web_push_request",
    # application and subscription writes
    "apply_action", "change_status", "update_tracking", "insert_application", "update_status", "insert_event",
    "subscribe_push_subscription", "unsubscribe_push_subscription", "revoke_push_subscription_by_id",
    "insert_frozen_digest", "store_notification_events",
    # public surfaces that open their own connections, and writable connections
    "read_recommendation_surface", "read_explorer_surface", "create_application_surface",
    "connect_database", "connect_configured_database", "apply_migrations", "_connect_write",
    # network clients (their modules are refused below as well)
    "urlopen", "Request", "create_connection", "Client", "AsyncClient",
}
FORBIDDEN_MODULE_PREFIXES = (
    "evaluation", "requests", "httpx", "urllib.request", "http.client", "socket", "smtplib",
    "googleapiclient", "services.gmail_digest.sender", "services.gmail_digest.delivery",
    "services.gmail_digest.oauth", "services.notifications.web_push", "services.notifications.delivery",
    "services.notifications.sync", "services.applications.service", "services.recommendation.engine",
    "services.recommendation.sync", "services.recommendation.input_assembly", "services.collector.matching.engine",
    "services.collector.matching.sync", "services.collector.qualification.classifier",
    "services.collector.qualification.fine_classifier", "services.collector.qualification.persistence",
    "services.collector.collectors", "services.collector.agent",
)


def _imports_and_names(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules, names = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return tree, modules, names


@pytest.mark.parametrize("path", [MODULE_PATH, Path(cli.__file__)])
def test_no_computing_writing_sending_or_network_name_is_imported_or_called(path):
    _tree, modules, names = _imports_and_names(path)

    assert not names & FORBIDDEN_NAMES, names & FORBIDDEN_NAMES
    for module in modules:
        assert not any(module == prefix or module.startswith(prefix + ".") for prefix in FORBIDDEN_MODULE_PREFIXES), module


def test_only_the_url_parser_is_taken_from_urllib():
    _tree, modules, _names = _imports_and_names(MODULE_PATH)

    assert {module for module in modules if module.startswith("urllib")} == {"urllib.parse"}


def test_no_write_statement_appears_in_the_module():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            words = set(node.value.upper().replace("(", " ").replace(",", " ").split())
            assert not words & {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "COMMIT", "REPLACE"}, node.value


def test_no_profile_country_or_sensitive_column_is_hardcoded():
    source = MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in ('"MA"', "Maroc", "Morocco", "profile_id = 1", "profile_id=1", "p256dh", "endpoint,",
                      "notes", "next_action", "body_text", "body_html", "subject", "recipient_fingerprint",
                      "cv_sha256", "email", "canonical_title", "organization"):
        assert forbidden not in source.replace("# ", "").split('"""', 2)[2], forbidden


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
    return {"schema_version": flow.SCHEMA_VERSION, "checks": checks, "integrity_result": result}


def test_the_cli_exits_zero_when_integrity_passes(environment):
    code, stdout, stderr, calls = run_cli(evidence_with(flow.build_checks(healthy())))

    assert (code, stderr, calls) == (cli.EXIT_PASS, "", [(environment, 3)])
    assert json.loads(stdout)["integrity_result"] == PASS


def test_not_demonstrated_not_assessed_stale_never_run_and_unknown_keep_exit_zero(environment):
    evidence = healthy()
    evidence["applications"]["surface"]["application_count"] = 0
    evidence["proactivity"]["comparisons"]["portfolio_built_from_current_matching_run"] = False
    for surface in ("recommendation", "explorer", "applications"):
        evidence[surface]["urls"] = None
    evidence["recommendation"]["explanations"] = None
    checks = flow.build_checks(evidence)
    assert {item["integrity"] for item in checks} == {PASS}
    assert {item["operational_state"] for item in checks} >= {STALE}

    code, _stdout, _stderr, _calls = run_cli(evidence_with(checks))

    assert code == cli.EXIT_PASS


def test_the_cli_exits_one_on_an_integrity_failure(environment):
    evidence = healthy()
    evidence["explorer"]["surface"]["orphan_opportunity_count"] = 1

    code, stdout, _stderr, _calls = run_cli(evidence_with(flow.build_checks(evidence)))

    assert code == cli.EXIT_FAIL
    assert json.loads(stdout)["integrity_result"] == FAIL


def test_a_read_path_refusal_logged_during_validation_never_reaches_stdout(environment):
    import logging

    evidence = healthy()
    evidence["recommendation"]["surface"] = {"available": False, "error_code": "RecommendationApiReadError"}
    checks = flow.build_checks(evidence)

    def validate(_path, _profile_id):
        logging.getLogger("services.collector.api.recommendation").error(
            "TEST-ONLY refusal", extra={"event": "test_only_refusal"}
        )
        return evidence_with(checks)

    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        code = cli.main(stdout=stdout, stderr=stderr, validate=validate)
    finally:
        from services.collector.logging_config import configure_logging

        configure_logging()

    assert code == cli.EXIT_FAIL
    assert json.loads(stdout.getvalue())["integrity_result"] == FAIL
    assert "test_only_refusal" in stderr.getvalue()


def test_the_cli_exits_two_with_a_stable_code_when_validation_cannot_run(environment):
    code, stdout, stderr, _calls = run_cli(error=OperationalStateError("PROFILE_NOT_FOUND"))

    assert (code, stdout) == (cli.EXIT_ERROR, "")
    assert json.loads(stderr) == {"error_code": "PROFILE_NOT_FOUND", "result": "ERROR", "schema_version": flow.SCHEMA_VERSION}


@pytest.mark.parametrize("variable, value, code", [
    ("DATABASE_BACKEND", "turso", "SQLITE_BACKEND_REQUIRED"),
    ("SQLITE_DATABASE_PATH", "", "SQLITE_DATABASE_PATH_REQUIRED"),
    ("OPPORTUNITY_RADAR_PROFILE_ID", None, "PROFILE_ID_REQUIRED"),
    ("OPPORTUNITY_RADAR_PROFILE_ID", "-1", "INVALID_PROFILE_ID"),
])
def test_the_cli_refuses_incomplete_configuration_without_validating(environment, monkeypatch, variable, value, code):
    if value is None:
        monkeypatch.delenv(variable)
    else:
        monkeypatch.setenv(variable, value)

    result, _stdout, stderr, calls = run_cli(evidence_with([]))

    assert (result, calls) == (cli.EXIT_ERROR, [])
    assert json.loads(stderr)["error_code"] == code
