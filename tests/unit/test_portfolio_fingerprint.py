from dataclasses import replace

import pytest

from services.collector.matching import MatchLane
from services.eligibility import GlobalStatus
from services.portfolio import build_portfolio_assessment
from services.priority import PriorityCategory

from .test_portfolio_engine import portfolio_input


def fingerprint(**changes: object) -> str:
    return build_portfolio_assessment(portfolio_input(**changes)).assessment_fingerprint


def test_fingerprint_is_deterministic_lowercase_sha256_and_ignores_ids() -> None:
    first = fingerprint()
    assert first == fingerprint()
    assert first == fingerprint(profile_id=99, opportunity_id=999)
    assert len(first) == 64 and first == first.lower()


@pytest.mark.parametrize(
    "changes",
    [
        {"required_skill_matched_count": 2, "required_skill_score": round(2 / 3, 12)},
        {"priority_category": PriorityCategory.MEDIUM},
        {"eligibility_status": GlobalStatus.UNKNOWN},
        {"matching_lane": MatchLane.UNCERTAIN},
        {"priority_assessment_fingerprint": "c" * 64},
        {"matching_assessment_fingerprint": "d" * 64},
        {"priority_rules_version": "priority-rules-v2"},
        {"matching_engine_version": "matching-engine-v2"},
        {"semantic_percentile_version": "semantic-percentile-v2"},
    ],
)
def test_business_semantics_change_fingerprint(changes: dict[str, object]) -> None:
    assert fingerprint(**changes) != fingerprint()


def test_result_semantics_are_covered_by_fingerprint() -> None:
    assessment = build_portfolio_assessment(portfolio_input())
    changed = replace(assessment, bucket=None, reason_codes=())
    from services.portfolio import portfolio_assessment_fingerprint

    assert (
        portfolio_assessment_fingerprint(changed) != assessment.assessment_fingerprint
    )
