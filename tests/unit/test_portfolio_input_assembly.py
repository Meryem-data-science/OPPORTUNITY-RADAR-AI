from types import SimpleNamespace

import pytest

from services.collector.matching import MatchLane, MatchingReadError
from services.eligibility import GlobalStatus
from services.portfolio import (
    PORTFOLIO_INPUT_ASSEMBLY_VERSION,
    PortfolioAssemblyIssueCode,
    PortfolioAssemblyStatus,
    assemble_portfolio_inputs,
    dry_run_portfolio,
)
from services.portfolio.input_assembly import _required_skill
from services.priority import PriorityCategory, PriorityProfileAuditStatus


def test_phase_version_and_issue_contracts_are_exact():
    assert PORTFOLIO_INPUT_ASSEMBLY_VERSION == "portfolio-input-assembly-v1"
    assert {item.value for item in PortfolioAssemblyIssueCode} == {
        "PRIORITY_NOT_SYNCED",
        "PRIORITY_AUDIT_CORRUPT",
        "MATCHING_RUN_MISSING",
        "MATCHING_PROFILE_MISMATCH",
        "MATCHING_RUN_FINGERPRINT_MISMATCH",
        "MATCHING_RUN_AUDIT_FAILED",
        "PORTFOLIO_COHORT_MISMATCH",
        "MATCHING_ASSESSMENT_PROVENANCE_MISMATCH",
        "MATCHING_LANE_PROVENANCE_MISMATCH",
        "MATCHING_VERSION_PROVENANCE_MISMATCH",
        "REQUIRED_SKILL_SNAPSHOT_INVALID",
    }


@pytest.mark.parametrize(
    "score,matched,total", [(1.0, 3, 3), (2 / 3, 2, 3), (0.0, 0, 3), (None, 0, 0)]
)
def test_required_skill_snapshot_values_are_preserved(score, matched, total):
    assert _required_skill(
        {
            "required_skill": {
                "normalized_score": score,
                "matched_count": matched,
                "total_count": total,
            }
        }
    ) == (score, matched, total)


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"required_skill": []},
        {
            "required_skill": {
                "normalized_score": 1.0,
                "matched_count": 0,
                "total_count": 0,
            }
        },
        {
            "required_skill": {
                "normalized_score": 0.5,
                "matched_count": 2,
                "total_count": 1,
            }
        },
        {
            "required_skill": {
                "normalized_score": 0.5,
                "matched_count": 2,
                "total_count": 3,
            }
        },
    ],
)
def test_required_skill_snapshot_rejects_malformed_or_incoherent_values(value):
    assert _required_skill(value) is None


def _priority_item(opportunity_id, **changes):
    match = {
        "lane": "PRIMARY",
        "upstream_fingerprint": "b" * 64,
        "matching_engine_version": "matching-engine-v1",
        "matching_rules_version": "matching-rules-v1",
        "semantic_percentile_version": "semantic-percentile-v1",
    }
    values = dict(
        opportunity_id=opportunity_id,
        priority_category=PriorityCategory.MEDIUM,
        eligibility_status=GlobalStatus.ELIGIBLE,
        matching_lane=MatchLane.PRIMARY,
        assessment_fingerprint="a" * 64,
        assessment_payload={"match": match},
    )
    values.update(changes)
    return SimpleNamespace(**values)


def _matching_item(opportunity_id, **changes):
    values = dict(
        opportunity_id=opportunity_id,
        lane="PRIMARY",
        assessment_fingerprint="b" * 64,
        assessment_payload={
            "required_skill": {
                "normalized_score": 1.0,
                "matched_count": 3,
                "total_count": 3,
            }
        },
    )
    values.update(changes)
    return SimpleNamespace(**values)


def _runs(ids=(2, 1)):
    priority_items = tuple(_priority_item(item) for item in ids)
    matching_items = tuple(_matching_item(item) for item in ids)
    priority = SimpleNamespace(
        run_id=7,
        matching_run_id=1,
        matching_run_fingerprint="c" * 64,
        run_fingerprint="d" * 64,
        assessment_count=len(ids),
        assessments=priority_items,
        priority_engine_version="priority-engine-v1",
        priority_rules_version="priority-rules-v1",
        freshness_version="freshness-v1",
        quality_version="quality-v1",
    )
    matching = SimpleNamespace(
        run_id=1,
        profile_id=9,
        run_fingerprint="c" * 64,
        assessments=matching_items,
        matching_engine_version="matching-engine-v1",
        matching_rules_version="matching-rules-v1",
        semantic_percentile_version="semantic-percentile-v1",
    )
    return priority, matching


def _invoke(
    monkeypatch,
    *,
    audit_status=PriorityProfileAuditStatus.READY,
    priority=None,
    matching=None,
    run_ok=True,
):
    priority, default_matching = _runs() if priority is None else (priority, _runs()[1])
    matching = default_matching if matching is None else matching
    monkeypatch.setattr(
        "services.portfolio.input_assembly.audit_current_priority",
        lambda *_: SimpleNamespace(status=audit_status),
    )
    monkeypatch.setattr(
        "services.portfolio.input_assembly.read_current_priority",
        lambda *_: SimpleNamespace(current_run=priority),
    )
    monkeypatch.setattr(
        "services.portfolio.input_assembly.read_matching_run", lambda *_: matching
    )
    monkeypatch.setattr(
        "services.portfolio.input_assembly.audit_matching_profile_history",
        lambda *_: SimpleNamespace(
            current_run_id=2, runs=(SimpleNamespace(run_id=1, ok=run_ok),)
        ),
    )
    return assemble_portfolio_inputs(object(), 9)


def _codes(result):
    assert result.status is PortfolioAssemblyStatus.INCOMPLETE
    assert result.inputs == () and result.assessment_count == 0
    return [item.code for item in result.issues]


def test_priority_audit_corrupt_fails_closed(monkeypatch):
    result = _invoke(monkeypatch, audit_status=PriorityProfileAuditStatus.CORRUPT)
    assert _codes(result) == [PortfolioAssemblyIssueCode.PRIORITY_AUDIT_CORRUPT]


def test_matching_run_absent_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "services.portfolio.input_assembly.audit_current_priority",
        lambda *_: SimpleNamespace(status=PriorityProfileAuditStatus.READY),
    )
    priority, _ = _runs()
    monkeypatch.setattr(
        "services.portfolio.input_assembly.read_current_priority",
        lambda *_: SimpleNamespace(current_run=priority),
    )
    monkeypatch.setattr(
        "services.portfolio.input_assembly.read_matching_run",
        lambda *_: (_ for _ in ()).throw(MatchingReadError("missing")),
    )
    assert _codes(assemble_portfolio_inputs(object(), 9)) == [
        PortfolioAssemblyIssueCode.MATCHING_RUN_MISSING
    ]


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"profile_id": 10}, PortfolioAssemblyIssueCode.MATCHING_PROFILE_MISMATCH),
        (
            {"run_fingerprint": "e" * 64},
            PortfolioAssemblyIssueCode.MATCHING_RUN_FINGERPRINT_MISMATCH,
        ),
    ],
)
def test_matching_run_provenance_fails_closed(monkeypatch, change, expected):
    _, matching = _runs()
    for name, value in change.items():
        setattr(matching, name, value)
    assert expected in _codes(_invoke(monkeypatch, matching=matching))


def test_exact_matching_run_audit_failure_fails_closed(monkeypatch):
    assert _codes(_invoke(monkeypatch, run_ok=False)) == [
        PortfolioAssemblyIssueCode.MATCHING_RUN_AUDIT_FAILED
    ]


def test_cohort_mismatch_fails_closed(monkeypatch):
    priority, matching = _runs()
    matching.assessments = matching.assessments[:1]
    assert _codes(_invoke(monkeypatch, priority=priority, matching=matching)) == [
        PortfolioAssemblyIssueCode.PORTFOLIO_COHORT_MISMATCH
    ]


@pytest.mark.parametrize(
    "priority_change,matching_change,expected",
    [
        (
            {
                "assessment_payload": {
                    "match": {
                        "lane": "PRIMARY",
                        "upstream_fingerprint": "f" * 64,
                        "matching_engine_version": "matching-engine-v1",
                        "matching_rules_version": "matching-rules-v1",
                        "semantic_percentile_version": "semantic-percentile-v1",
                    }
                }
            },
            {},
            PortfolioAssemblyIssueCode.MATCHING_ASSESSMENT_PROVENANCE_MISMATCH,
        ),
        (
            {"matching_lane": MatchLane.UNCERTAIN},
            {},
            PortfolioAssemblyIssueCode.MATCHING_LANE_PROVENANCE_MISMATCH,
        ),
        (
            {
                "assessment_payload": {
                    "match": {
                        "lane": "PRIMARY",
                        "upstream_fingerprint": "b" * 64,
                        "matching_engine_version": "old",
                        "matching_rules_version": "matching-rules-v1",
                        "semantic_percentile_version": "semantic-percentile-v1",
                    }
                }
            },
            {},
            PortfolioAssemblyIssueCode.MATCHING_VERSION_PROVENANCE_MISMATCH,
        ),
        (
            {},
            {
                "assessment_payload": {
                    "required_skill": {
                        "normalized_score": 0.5,
                        "matched_count": 2,
                        "total_count": 3,
                    }
                }
            },
            PortfolioAssemblyIssueCode.REQUIRED_SKILL_SNAPSHOT_INVALID,
        ),
        (
            {},
            {"assessment_payload": {}},
            PortfolioAssemblyIssueCode.REQUIRED_SKILL_SNAPSHOT_INVALID,
        ),
    ],
)
def test_assessment_preflight_branches_fail_closed(
    monkeypatch, priority_change, matching_change, expected
):
    priority, matching = _runs(ids=(1,))
    priority.assessments = (_priority_item(1, **priority_change),)
    matching.assessments = (_matching_item(1, **matching_change),)
    assert expected in _codes(
        _invoke(monkeypatch, priority=priority, matching=matching)
    )


def test_issue_order_is_deterministic(monkeypatch):
    priority, matching = _runs(ids=(2, 1))
    priority.assessments = tuple(
        _priority_item(item, matching_lane=MatchLane.UNCERTAIN) for item in (2, 1)
    )
    result = _invoke(monkeypatch, priority=priority, matching=matching)
    assert [(item.opportunity_id, item.code.value) for item in result.issues] == [
        (1, "MATCHING_LANE_PROVENANCE_MISMATCH"),
        (2, "MATCHING_LANE_PROVENANCE_MISMATCH"),
    ]


def test_historical_matching_run_is_used_and_ready_output_is_sorted(monkeypatch):
    seen = []
    priority, matching = _runs(ids=(2, 1))
    monkeypatch.setattr(
        "services.portfolio.input_assembly.audit_current_priority",
        lambda *_: SimpleNamespace(status=PriorityProfileAuditStatus.READY),
    )
    monkeypatch.setattr(
        "services.portfolio.input_assembly.read_current_priority",
        lambda *_: SimpleNamespace(current_run=priority),
    )
    monkeypatch.setattr(
        "services.portfolio.input_assembly.read_matching_run",
        lambda _, run_id: seen.append(run_id) or matching,
    )
    monkeypatch.setattr(
        "services.portfolio.input_assembly.audit_matching_profile_history",
        lambda *_: SimpleNamespace(
            current_run_id=2,
            runs=(
                SimpleNamespace(run_id=2, ok=True),
                SimpleNamespace(run_id=1, ok=True),
            ),
        ),
    )
    result = assemble_portfolio_inputs(object(), 9)
    assert result.status is PortfolioAssemblyStatus.READY
    assert seen == [1] and result.matching_run_id == 1
    assert [item.opportunity_id for item in result.inputs] == [1, 2]


def test_incomplete_dry_run_never_classifies_partial_inputs(monkeypatch):
    result = SimpleNamespace(
        profile_id=9,
        status=PortfolioAssemblyStatus.INCOMPLETE,
        input_assembly_version=PORTFOLIO_INPUT_ASSEMBLY_VERSION,
        priority_run_id=7,
        priority_run_fingerprint="d" * 64,
        matching_run_id=1,
        matching_run_fingerprint="c" * 64,
        issues=(),
        inputs=(),
    )
    monkeypatch.setattr(
        "services.portfolio.dry_run.assemble_portfolio_inputs", lambda *_: result
    )
    monkeypatch.setattr(
        "services.portfolio.dry_run.build_portfolio_assessments",
        lambda *_: pytest.fail("classifier called"),
    )
    report = dry_run_portfolio(object(), 9)
    assert report.assessments == () and report.assessment_count == 0
    assert report.included_count == report.excluded_count == 0
