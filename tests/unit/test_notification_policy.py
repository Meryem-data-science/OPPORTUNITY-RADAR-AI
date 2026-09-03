"""Unit tests for the pure Phase 5.3B notification policy and its identities."""

import json

import pytest

from services.collector.matching.fingerprint import canonical_json
from services.notifications import (
    ESCALATIONS,
    NOTIFICATION_POLICY_VERSION,
    NOTIFICATION_TARGET_PATH,
    NotificationEventType,
    NotificationTransition,
    OpportunityNotificationMetadata,
    PortfolioPosition,
    canonical_notification_event_payload,
    evaluate_transition,
    evaluate_transitions,
    notification_event_fingerprint,
)
from services.portfolio import PortfolioBucket, PortfolioDisposition
from services.priority import PriorityCategory

INCLUDED = PortfolioDisposition.INCLUDED
EXCLUDED = PortfolioDisposition.EXCLUDED
URGENT, HIGH, MEDIUM, LOW, IGNORE = (
    PriorityCategory.URGENT,
    PriorityCategory.HIGH,
    PriorityCategory.MEDIUM,
    PriorityCategory.LOW,
    PriorityCategory.IGNORE,
)
NEW = NotificationEventType.NEW_ACTIONABLE_OPPORTUNITY
ESCALATED = NotificationEventType.ATTENTION_ESCALATED


def position(
    disposition=INCLUDED,
    bucket=PortfolioBucket.TARGET,
    priority=MEDIUM,
    opportunity_id=7,
    reason_codes=("REQUIRED_SKILLS_TARGET_COVERAGE",),
):
    return PortfolioPosition(
        opportunity_id,
        disposition,
        None if disposition is EXCLUDED else bucket,
        priority,
        reason_codes,
    )


def metadata(opportunity_id=7):
    return OpportunityNotificationMetadata(
        opportunity_id,
        "Data Engineer",
        "Org",
        "https://example.invalid/7",
        NOTIFICATION_TARGET_PATH,
    )


def test_policy_version_and_target_path_are_explicit():
    assert NOTIFICATION_POLICY_VERSION == "notification-policy-v1"
    assert NOTIFICATION_TARGET_PATH == "/portfolio"


def test_becoming_actionable_from_excluded_or_from_absent_is_one_new_event():
    excluded_before = position(EXCLUDED, priority=LOW)
    assert evaluate_transition(excluded_before, position()) is NEW
    assert evaluate_transition(None, position()) is NEW


@pytest.mark.parametrize(
    "before,after", [(MEDIUM, HIGH), (MEDIUM, URGENT), (HIGH, URGENT)]
)
def test_every_declared_escalation_of_an_actionable_opportunity_is_announced(
    before, after
):
    assert (before, after) in ESCALATIONS
    assert (
        evaluate_transition(position(priority=before), position(priority=after))
        is ESCALATED
    )


def test_a_simultaneous_escalation_is_dropped_in_favour_of_the_new_event():
    """Becoming actionable *and* climbing is still one announcement."""
    before = position(EXCLUDED, priority=MEDIUM)
    after = position(priority=URGENT)
    assert (before.priority_category, after.priority_category) in ESCALATIONS
    assert evaluate_transition(before, after) is NEW


@pytest.mark.parametrize(
    "before,after",
    [
        (MEDIUM, MEDIUM),
        (HIGH, HIGH),
        (URGENT, URGENT),
        (URGENT, HIGH),
        (HIGH, MEDIUM),
        (URGENT, MEDIUM),
        (MEDIUM, LOW),
        (LOW, MEDIUM),
        (IGNORE, LOW),
        (LOW, HIGH),
    ],
)
def test_unchanged_downgraded_and_undeclared_transitions_are_silent(before, after):
    assert evaluate_transition(position(priority=before), position(priority=after)) is (
        None
    )


def test_leaving_the_portfolio_and_staying_out_of_it_are_silent():
    assert evaluate_transition(position(), position(EXCLUDED, priority=LOW)) is None
    assert (
        evaluate_transition(position(EXCLUDED, priority=LOW), position(EXCLUDED))
        is None
    )
    assert evaluate_transition(None, position(EXCLUDED)) is None


def test_a_bucket_move_alone_is_not_an_event():
    before = position(bucket=PortfolioBucket.AMBITIOUS)
    after = position(bucket=PortfolioBucket.SAFE)
    assert evaluate_transition(before, after) is None


def test_transitions_are_ordered_and_only_cover_the_current_cohort():
    previous = {
        1: position(EXCLUDED, priority=LOW, opportunity_id=1),
        2: position(priority=MEDIUM, opportunity_id=2),
        9: position(priority=MEDIUM, opportunity_id=9),
    }
    current = {
        2: position(priority=URGENT, opportunity_id=2),
        1: position(priority=MEDIUM, opportunity_id=1),
        3: position(priority=HIGH, opportunity_id=3),
    }
    transitions = evaluate_transitions(previous, current)

    assert [(item.opportunity_id, item.event_type) for item in transitions] == [
        (1, NEW),
        (2, ESCALATED),
        (3, NEW),
    ]
    # Opportunity 9 left the cohort: it has no current position to announce.
    assert all(item.opportunity_id != 9 for item in transitions)


def test_payload_is_canonical_deterministic_and_free_of_generated_text():
    transition = NotificationTransition(
        7, NEW, position(EXCLUDED, priority=LOW), position(priority=HIGH)
    )
    payload = canonical_notification_event_payload(transition, metadata())

    assert payload == {
        "policy_version": "notification-policy-v1",
        "event_type": "NEW_ACTIONABLE_OPPORTUNITY",
        "opportunity": {
            "opportunity_id": 7,
            "title": "Data Engineer",
            "organization": "Org",
            "original_url": "https://example.invalid/7",
            "target_url": "/portfolio",
        },
        "portfolio": {
            "priority_category": "HIGH",
            "bucket": "TARGET",
            "reason_codes": ["REQUIRED_SKILLS_TARGET_COVERAGE"],
        },
        "transition": {
            "previous": {
                "disposition": "EXCLUDED",
                "bucket": None,
                "priority_category": "LOW",
            },
            "current": {
                "disposition": "INCLUDED",
                "bucket": "TARGET",
                "priority_category": "HIGH",
            },
        },
    }
    serialized = canonical_json(payload)
    assert serialized == serialized.strip() and json.loads(serialized) == payload
    assert (
        canonical_json(canonical_notification_event_payload(transition, metadata()))
        == serialized
    )


def test_payload_records_an_absent_previous_position_as_absent():
    transition = NotificationTransition(7, NEW, None, position())
    payload = canonical_notification_event_payload(transition, metadata())
    assert payload["transition"]["previous"] is None


def test_payload_refuses_metadata_from_another_opportunity():
    transition = NotificationTransition(7, NEW, None, position())
    with pytest.raises(ValueError, match="another opportunity"):
        canonical_notification_event_payload(transition, metadata(8))


def _fingerprint(**changes):
    arguments = {
        "profile_id": 3,
        "previous_portfolio_run_fingerprint": "a" * 64,
        "portfolio_run_fingerprint": "b" * 64,
        "payload": canonical_notification_event_payload(
            NotificationTransition(7, NEW, None, position()), metadata()
        ),
    }
    return notification_event_fingerprint(**(arguments | changes))


def test_fingerprint_is_a_stable_sha256_of_meaning_and_portfolio_provenance():
    value = _fingerprint()
    assert len(value) == 64 and set(value) <= set("0123456789abcdef")
    assert value == _fingerprint()


def test_fingerprint_changes_with_profile_provenance_or_semantic_payload():
    baseline = _fingerprint()
    escalated = canonical_notification_event_payload(
        NotificationTransition(7, ESCALATED, position(), position(priority=URGENT)),
        metadata(),
    )
    assert _fingerprint(profile_id=4) != baseline
    assert _fingerprint(previous_portfolio_run_fingerprint="c" * 64) != baseline
    assert _fingerprint(portfolio_run_fingerprint="c" * 64) != baseline
    assert _fingerprint(payload=escalated) != baseline


def test_fingerprint_ignores_database_ids_and_timestamps_entirely():
    """Nothing operational reaches the hash: only meaning and provenance do."""
    payload = canonical_notification_event_payload(
        NotificationTransition(7, NEW, None, position()), metadata()
    )
    serialized = canonical_json(payload)
    for operational in ("created_at", '"id"', "run_id", "event_id"):
        assert operational not in serialized
