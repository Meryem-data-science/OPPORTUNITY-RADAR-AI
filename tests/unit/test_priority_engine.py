from datetime import date, timedelta
import math
from types import SimpleNamespace

import pytest

from services.collector.matching import MatchLane
from services.collector.qualification.taxonomy import ListingQuality
from services.eligibility import GlobalStatus
from services.priority import (
    ComponentStatus,
    PriorityCategory,
    PriorityInput,
    PriorityInputError,
    PriorityReasonCode,
    build_priority_assessment,
    freshness_score,
)
import services.priority.engine as priority_engine

TODAY = date(2026, 9, 1)


def upstream(
    *,
    match=0.8,
    coverage=1.0,
    lane=MatchLane.PRIMARY,
    eligibility=GlobalStatus.ELIGIBLE,
    matching_fp="match-fp",
    eligibility_fp="eligibility-fp",
    matching_engine_version="matching-engine-v1",
    matching_rules_version="matching-rules-v1",
    semantic_percentile_version="semantic-percentile-v1",
):
    matching = SimpleNamespace(
        profile_id=7,
        opportunity_id=11,
        match_quality=match,
        evidence_coverage=coverage,
        lane=lane,
        assessment_fingerprint=matching_fp,
        matching_engine_version=matching_engine_version,
        matching_rules_version=matching_rules_version,
        semantic_percentile_version=semantic_percentile_version,
    )
    decision = SimpleNamespace(
        profile_id=7,
        opportunity_id=11,
        status=eligibility,
        input_fingerprint=eligibility_fp,
        engine_version="eligibility-rules-v1",
    )
    return matching, decision


def assess(
    *,
    match=0.8,
    coverage=1.0,
    lane=MatchLane.PRIMARY,
    eligibility=GlobalStatus.ELIGIBLE,
    quality=ListingQuality.NORMAL_LISTING,
    published_at=TODAY,
    deadline=None,
    **provenance,
):
    matching, decision = upstream(
        match=match,
        coverage=coverage,
        lane=lane,
        eligibility=eligibility,
        **provenance,
    )
    return build_priority_assessment(
        PriorityInput(matching, decision, quality, published_at, deadline, TODAY)
    )


@pytest.mark.parametrize(
    ("match", "quality", "published", "expected_score", "expected_coverage"),
    [
        (0.4, ListingQuality.INSUFFICIENT_CONTENT, TODAY, 0.6, 1.0),
        (0.4, None, TODAY, 0.625, 0.8),
        (None, None, TODAY, 1.0, 0.3),
        (None, None, None, None, 0.0),
        (0.0, None, None, 0.0, 0.5),
    ],
)
def test_available_weight_formula(
    match, quality, published, expected_score, expected_coverage
):
    result = assess(match=match, quality=quality, published_at=published)
    assert result.priority_score == expected_score
    assert result.priority_evidence_coverage == expected_coverage
    assert (result.match.status is ComponentStatus.AVAILABLE) == (match is not None)


def test_all_missing_has_no_category_and_zero_is_low():
    missing = assess(match=None, quality=None, published_at=None)
    zero = assess(match=0.0, quality=None, published_at=None)
    assert missing.priority_category is None
    assert zero.priority_category is PriorityCategory.LOW
    assert PriorityReasonCode.NO_NUMERIC_EVIDENCE in missing.reason_codes


@pytest.mark.parametrize("lane", list(MatchLane))
def test_matching_lanes_preserve_score(lane):
    result = assess(lane=lane)
    assert result.priority_score == 0.9
    if lane is MatchLane.OUTSIDE_PREFERENCES:
        assert result.priority_category is PriorityCategory.LOW
        assert result.outside_preferences_cap_applied
        assert (
            PriorityReasonCode.MATCHING_LANE_OUTSIDE_PREFERENCES in result.reason_codes
        )
        assert PriorityReasonCode.OUTSIDE_PREFERENCES_CAP in result.reason_codes


def test_outside_preferences_already_low_does_not_report_cap():
    result = assess(
        match=0.0,
        quality=None,
        published_at=None,
        lane=MatchLane.OUTSIDE_PREFERENCES,
    )
    assert result.priority_category is PriorityCategory.LOW
    assert not result.outside_preferences_cap_applied
    assert PriorityReasonCode.MATCHING_LANE_OUTSIDE_PREFERENCES in result.reason_codes
    assert PriorityReasonCode.OUTSIDE_PREFERENCES_CAP not in result.reason_codes


@pytest.mark.parametrize(
    ("eligibility", "deadline"),
    [
        (GlobalStatus.INELIGIBLE, None),
        (GlobalStatus.ELIGIBLE, TODAY - timedelta(days=1)),
    ],
)
def test_outside_preferences_blocked_does_not_report_cap(eligibility, deadline):
    result = assess(
        lane=MatchLane.OUTSIDE_PREFERENCES,
        eligibility=eligibility,
        deadline=deadline,
    )
    assert result.priority_category is PriorityCategory.IGNORE
    assert not result.outside_preferences_cap_applied
    assert PriorityReasonCode.MATCHING_LANE_OUTSIDE_PREFERENCES in result.reason_codes
    assert PriorityReasonCode.OUTSIDE_PREFERENCES_CAP not in result.reason_codes


def test_uncertain_eligibility_has_no_penalty_and_ineligible_blocks_without_zeroing():
    eligible = assess(eligibility=GlobalStatus.ELIGIBLE)
    unknown = assess(eligibility=GlobalStatus.UNKNOWN)
    blocked = assess(eligibility=GlobalStatus.INELIGIBLE)
    assert unknown.priority_score == eligible.priority_score
    assert unknown.priority_category == eligible.priority_category
    assert blocked.priority_category is PriorityCategory.IGNORE
    assert blocked.priority_score == eligible.priority_score == 0.9


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (0, 1.0),
        (2, 1.0),
        (3, 0.85),
        (7, 0.85),
        (8, 0.65),
        (14, 0.65),
        (15, 0.4),
        (30, 0.4),
        (31, 0.2),
        (60, 0.2),
        (61, 0.1),
    ],
)
def test_freshness_boundaries(age, expected):
    assert freshness_score(TODAY - timedelta(days=age), TODAY) == (expected, age)


def test_missing_and_future_publication_dates():
    assert freshness_score(None, TODAY) == (None, None)
    with pytest.raises(PriorityInputError, match="after evaluation_date"):
        freshness_score(TODAY + timedelta(days=1), TODAY)


@pytest.mark.parametrize(
    ("quality", "expected"),
    [
        (ListingQuality.NORMAL_LISTING, 1.0),
        (ListingQuality.INSUFFICIENT_CONTENT, 0.5),
        (ListingQuality.POSSIBLE_NON_JOB_PAGE, 0.25),
        (None, None),
    ],
)
def test_upstream_quality_mapping(quality, expected):
    assert assess(quality=quality).opportunity_quality.score == expected


def test_high_coverage_guardrail_caps_category_not_score():
    enough = assess(match=1.0, quality=None, published_at=TODAY)
    low = assess(match=1.0, quality=None, published_at=None)
    assert enough.priority_evidence_coverage == 0.8
    assert enough.priority_category is PriorityCategory.HIGH
    assert low.priority_evidence_coverage == 0.5
    assert low.priority_score == 1.0
    assert low.priority_category is PriorityCategory.MEDIUM
    assert low.coverage_guardrail_applied


@pytest.mark.parametrize(
    ("days", "category"),
    [
        (-1, PriorityCategory.IGNORE),
        (0, PriorityCategory.URGENT),
        (1, PriorityCategory.URGENT),
        (3, PriorityCategory.URGENT),
        (4, PriorityCategory.HIGH),
        (None, PriorityCategory.HIGH),
    ],
)
def test_deadline_policies(days, category):
    deadline = None if days is None else TODAY + timedelta(days=days)
    result = assess(deadline=deadline)
    assert result.priority_category is category
    assert result.priority_score == 0.9


def test_urgent_policy_respects_low_lane_and_hard_blocker():
    deadline = TODAY + timedelta(days=1)
    assert (
        assess(
            match=0.0, quality=None, published_at=None, deadline=deadline
        ).priority_category
        is PriorityCategory.LOW
    )
    assert (
        assess(lane=MatchLane.OUTSIDE_PREFERENCES, deadline=deadline).priority_category
        is PriorityCategory.LOW
    )
    assert (
        assess(eligibility=GlobalStatus.INELIGIBLE, deadline=deadline).priority_category
        is PriorityCategory.IGNORE
    )
    assert (
        assess(eligibility=GlobalStatus.UNKNOWN, deadline=deadline).priority_category
        is PriorityCategory.URGENT
    )


@pytest.mark.parametrize("bad", [math.nan, math.inf, -0.01, 1.01])
def test_invalid_numeric_inputs_are_rejected(bad):
    with pytest.raises(PriorityInputError, match=r"within \[0, 1\]"):
        assess(match=bad)


def test_invalid_weight_invariant_is_detectable():
    assert (
        priority_engine.MATCH_QUALITY_WEIGHT
        + priority_engine.FRESHNESS_WEIGHT
        + priority_engine.OPPORTUNITY_QUALITY_WEIGHT
    ) == 1.0
    assert sum((0.5, 0.3, 0.3)) != 1.0


def test_identity_mismatch_and_missing_provenance_are_rejected():
    matching, eligibility = upstream()
    with pytest.raises(PriorityInputError, match="identity mismatch"):
        build_priority_assessment(
            PriorityInput(
                matching,
                replace_compat(eligibility, opportunity_id=12),
                None,
                None,
                None,
                TODAY,
            )
        )
    with pytest.raises(PriorityInputError, match="fingerprint"):
        assess(matching_fp="")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("matching_engine_version", ""),
        ("matching_rules_version", ""),
        ("semantic_percentile_version", ""),
        ("matching_engine_version", "   "),
        ("matching_rules_version", "\t"),
        ("semantic_percentile_version", "\n"),
    ],
)
def test_empty_matching_provenance_versions_are_rejected(field, value):
    with pytest.raises(PriorityInputError, match="matching provenance versions"):
        assess(**{field: value})


def replace_compat(value, **changes):
    return SimpleNamespace(**{**vars(value), **changes})


def test_priority_package_has_no_impure_or_future_dependencies():
    from pathlib import Path

    source = "\n".join(
        path.read_text() for path in Path("services/priority").glob("*.py")
    )
    forbidden = (
        "sqlite",
        "httpx",
        "openai",
        "datetime.now",
        "date.today",
        "notifications",
        "applications",
        "learning",
        "portfolio",
        "llm",
    )
    assert not [token for token in forbidden if token in source.casefold()]
