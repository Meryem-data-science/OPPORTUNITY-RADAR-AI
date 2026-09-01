from dataclasses import replace
from datetime import timedelta

from services.collector.matching import MatchLane
from services.collector.qualification.taxonomy import ListingQuality
from services.eligibility import GlobalStatus
from services.priority import (
    PRIORITY_RULES_VERSION,
    PriorityInput,
    build_priority_assessment,
    canonical_priority_assessment_payload,
    priority_assessment_fingerprint,
)
from tests.unit.test_priority_engine import TODAY, upstream


def make(**changes):
    matching, eligibility = upstream(
        match=changes.pop("match", 0.8),
        matching_fp=changes.pop("matching_fp", "match-fp"),
        eligibility_fp=changes.pop("eligibility_fp", "eligibility-fp"),
        eligibility=changes.pop("eligibility", GlobalStatus.ELIGIBLE),
        lane=changes.pop("lane", MatchLane.PRIMARY),
    )
    values = dict(
        matching=matching,
        eligibility=eligibility,
        publication_quality=ListingQuality.NORMAL_LISTING,
        published_at=TODAY,
        deadline=None,
        evaluation_date=TODAY,
    )
    values.update(changes)
    return build_priority_assessment(PriorityInput(**values))


def test_same_semantic_input_is_deterministic():
    assert make().assessment_fingerprint == make().assessment_fingerprint
    assert len(make().assessment_fingerprint) == 64


def test_reason_order_is_canonicalized():
    result = make()
    reordered = replace(result, reason_codes=tuple(reversed(result.reason_codes)))
    assert priority_assessment_fingerprint(result) == priority_assessment_fingerprint(
        reordered
    )


def test_business_changes_change_fingerprint():
    baseline = make().assessment_fingerprint
    variants = (
        make(evaluation_date=TODAY + timedelta(days=1)),
        make(match=0.7),
        make(matching_fp="different-match"),
        make(eligibility=GlobalStatus.UNKNOWN),
        make(eligibility_fp="different-eligibility"),
        make(publication_quality=ListingQuality.INSUFFICIENT_CONTENT),
        make(published_at=TODAY - timedelta(days=1)),
        make(deadline=TODAY + timedelta(days=4)),
    )
    assert all(item.assessment_fingerprint != baseline for item in variants)


def test_priority_version_changes_fingerprint():
    result = make()
    changed = replace(
        result, priority_rules_version=PRIORITY_RULES_VERSION + "-changed"
    )
    assert priority_assessment_fingerprint(changed) != result.assessment_fingerprint


def test_canonical_payload_excludes_runtime_and_persistence_metadata():
    payload_text = str(canonical_priority_assessment_payload(make())).casefold()
    for forbidden in ("database_row_id", "priority_run_id", "created_at", "memory"):
        assert forbidden not in payload_text
