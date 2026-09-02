from dataclasses import replace

import pytest

from services.collector.matching import MatchLane
from services.eligibility import GlobalStatus
from services.portfolio import (
    PortfolioBucket,
    PortfolioDisposition,
    PortfolioInput,
    PortfolioInputError,
    PortfolioReasonCode,
    build_portfolio_assessment,
    build_portfolio_assessments,
)
from services.priority import PriorityCategory


def portfolio_input(**changes: object) -> PortfolioInput:
    values = dict(
        profile_id=1,
        opportunity_id=10,
        priority_category=PriorityCategory.HIGH,
        eligibility_status=GlobalStatus.ELIGIBLE,
        matching_lane=MatchLane.PRIMARY,
        required_skill_score=1.0,
        required_skill_matched_count=3,
        required_skill_total_count=3,
        priority_assessment_fingerprint="a" * 64,
        matching_assessment_fingerprint="b" * 64,
        priority_engine_version="priority-engine-v1",
        priority_rules_version="priority-rules-v1",
        priority_freshness_version="priority-freshness-v1",
        priority_quality_version="priority-quality-v1",
        matching_engine_version="matching-engine-v1",
        matching_rules_version="matching-rules-v1",
        semantic_percentile_version="semantic-percentile-v1",
    )
    values.update(changes)
    return PortfolioInput(**values)


@pytest.mark.parametrize(
    "category",
    [PriorityCategory.URGENT, PriorityCategory.HIGH, PriorityCategory.MEDIUM],
)
def test_priority_candidates_can_be_safe(category: PriorityCategory) -> None:
    result = build_portfolio_assessment(portfolio_input(priority_category=category))
    assert (result.disposition, result.bucket) == (
        PortfolioDisposition.INCLUDED,
        PortfolioBucket.SAFE,
    )


@pytest.mark.parametrize(
    "category", [PriorityCategory.LOW, PriorityCategory.IGNORE, None]
)
def test_non_candidate_priority_is_excluded(category: PriorityCategory | None) -> None:
    result = build_portfolio_assessment(portfolio_input(priority_category=category))
    assert result.disposition is PortfolioDisposition.EXCLUDED
    assert result.bucket is None
    assert result.reason_codes == (
        PortfolioReasonCode.PRIORITY_NOT_PORTFOLIO_CANDIDATE,
    )


@pytest.mark.parametrize(
    ("matched", "total", "bucket", "reason"),
    [
        (3, 3, PortfolioBucket.SAFE, PortfolioReasonCode.REQUIRED_SKILLS_FULL_COVERAGE),
        (
            2,
            3,
            PortfolioBucket.TARGET,
            PortfolioReasonCode.REQUIRED_SKILLS_TARGET_COVERAGE,
        ),
        (
            3,
            4,
            PortfolioBucket.TARGET,
            PortfolioReasonCode.REQUIRED_SKILLS_TARGET_COVERAGE,
        ),
        (
            1,
            2,
            PortfolioBucket.AMBITIOUS,
            PortfolioReasonCode.REQUIRED_SKILLS_AMBITIOUS_GAP,
        ),
        (
            2,
            4,
            PortfolioBucket.AMBITIOUS,
            PortfolioReasonCode.REQUIRED_SKILLS_AMBITIOUS_GAP,
        ),
        (
            0,
            3,
            PortfolioBucket.AMBITIOUS,
            PortfolioReasonCode.REQUIRED_SKILLS_AMBITIOUS_GAP,
        ),
    ],
)
def test_required_skill_policy(matched, total, bucket, reason) -> None:
    result = build_portfolio_assessment(
        portfolio_input(
            required_skill_matched_count=matched,
            required_skill_total_count=total,
            required_skill_score=round(matched / total, 12),
        )
    )
    assert result.bucket is bucket
    assert result.reason_codes == (reason,)


def test_missing_required_evidence_is_neutral_target() -> None:
    result = build_portfolio_assessment(
        portfolio_input(
            required_skill_matched_count=0,
            required_skill_total_count=0,
            required_skill_score=None,
        )
    )
    assert result.bucket is PortfolioBucket.TARGET
    assert result.reason_codes == (PortfolioReasonCode.REQUIRED_SKILL_EVIDENCE_MISSING,)


@pytest.mark.parametrize(
    ("eligibility", "lane", "reasons"),
    [
        (
            GlobalStatus.UNKNOWN,
            MatchLane.PRIMARY,
            (PortfolioReasonCode.SAFE_CAP_ELIGIBILITY_UNKNOWN,),
        ),
        (
            GlobalStatus.ELIGIBLE,
            MatchLane.UNCERTAIN,
            (PortfolioReasonCode.SAFE_CAP_MATCHING_UNCERTAIN,),
        ),
        (
            GlobalStatus.UNKNOWN,
            MatchLane.UNCERTAIN,
            (
                PortfolioReasonCode.SAFE_CAP_ELIGIBILITY_UNKNOWN,
                PortfolioReasonCode.SAFE_CAP_MATCHING_UNCERTAIN,
            ),
        ),
    ],
)
def test_safe_caps(eligibility, lane, reasons) -> None:
    result = build_portfolio_assessment(
        portfolio_input(eligibility_status=eligibility, matching_lane=lane)
    )
    assert result.bucket is PortfolioBucket.TARGET
    assert result.safe_cap_applied
    assert result.reason_codes == (
        PortfolioReasonCode.REQUIRED_SKILLS_FULL_COVERAGE,
        *reasons,
    )


def test_unknown_and_uncertain_do_not_penalize_non_safe_buckets() -> None:
    target = build_portfolio_assessment(
        portfolio_input(
            eligibility_status=GlobalStatus.UNKNOWN,
            required_skill_matched_count=2,
            required_skill_total_count=3,
            required_skill_score=round(2 / 3, 12),
        )
    )
    ambitious = build_portfolio_assessment(
        portfolio_input(
            matching_lane=MatchLane.UNCERTAIN,
            required_skill_matched_count=1,
            required_skill_total_count=3,
            required_skill_score=round(1 / 3, 12),
        )
    )
    assert (target.bucket, target.safe_cap_applied) == (PortfolioBucket.TARGET, False)
    assert (ambitious.bucket, ambitious.safe_cap_applied) == (
        PortfolioBucket.AMBITIOUS,
        False,
    )


def test_all_exclusions_are_reported_in_stable_order() -> None:
    result = build_portfolio_assessment(
        portfolio_input(
            priority_category=PriorityCategory.LOW,
            eligibility_status=GlobalStatus.INELIGIBLE,
            matching_lane=MatchLane.OUTSIDE_PREFERENCES,
        )
    )
    assert result.reason_codes == (
        PortfolioReasonCode.PRIORITY_NOT_PORTFOLIO_CANDIDATE,
        PortfolioReasonCode.ELIGIBILITY_INELIGIBLE,
        PortfolioReasonCode.MATCHING_OUTSIDE_PREFERENCES,
    )
    assert result.bucket is None


@pytest.mark.parametrize(
    "changes",
    [
        {"profile_id": True},
        {"opportunity_id": 0},
        {"required_skill_matched_count": True},
        {"required_skill_total_count": -1},
        {"required_skill_matched_count": 4},
        {
            "required_skill_total_count": 0,
            "required_skill_matched_count": 0,
            "required_skill_score": 0.0,
        },
        {"required_skill_score": None},
        {"required_skill_score": 0.5},
        {"required_skill_score": float("nan")},
        {"required_skill_score": float("inf")},
        {"priority_assessment_fingerprint": "A" * 64},
        {"matching_assessment_fingerprint": "bad"},
        {"priority_engine_version": " "},
    ],
)
def test_strict_input_validation(changes: dict[str, object]) -> None:
    with pytest.raises(PortfolioInputError):
        build_portfolio_assessment(portfolio_input(**changes))


def test_batch_validates_identity_and_sorts_all_results() -> None:
    values = [portfolio_input(opportunity_id=20), portfolio_input(opportunity_id=10)]
    assert [item.opportunity_id for item in build_portfolio_assessments(values)] == [
        10,
        20,
    ]
    with pytest.raises(PortfolioInputError):
        build_portfolio_assessments([])
    with pytest.raises(PortfolioInputError):
        build_portfolio_assessments([values[0], replace(values[1], profile_id=2)])
    with pytest.raises(PortfolioInputError):
        build_portfolio_assessments([values[0], replace(values[1], opportunity_id=20)])
