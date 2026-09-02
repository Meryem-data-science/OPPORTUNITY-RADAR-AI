from datetime import date
from types import SimpleNamespace

import pytest

from services.collector.matching import MatchLane
from services.collector.qualification.taxonomy import ListingQuality
from services.eligibility import GlobalStatus
from services.priority.dry_run_cli import main
from services.priority.models import (
    PriorityEligibilitySnapshot,
    PriorityInput,
    PriorityMatchingSnapshot,
)

from services.priority.dry_run import run_priority_dry_run
from services.priority.input_assembly import (
    PRIORITY_INPUT_ASSEMBLY_VERSION,
    PriorityInputAssemblyResult,
    PriorityInputRecord,
    PriorityOpportunityContext,
    PriorityReadinessIssue,
    PriorityReadinessIssueCode,
    PriorityReadinessStatus,
)


def test_incomplete_preflight_never_scores(monkeypatch):
    assembly = PriorityInputAssemblyResult(
        PRIORITY_INPUT_ASSEMBLY_VERSION,
        PriorityReadinessStatus.INCOMPLETE,
        7,
        19,
        2,
        date(2026, 9, 2),
        (
            PriorityReadinessIssue(
                PriorityReadinessIssueCode.INVALID_DEADLINE, "bad", 3
            ),
        ),
        (),
    )
    monkeypatch.setattr(
        "services.priority.dry_run.assemble_priority_inputs", lambda *_: assembly
    )

    def forbidden(_):
        raise AssertionError("scoring must not run")

    monkeypatch.setattr(
        "services.priority.dry_run.build_priority_assessment", forbidden
    )
    report = run_priority_dry_run(object(), 7, date(2026, 9, 2))
    assert report.status is PriorityReadinessStatus.INCOMPLETE
    assert report.assessment_count == 0 and report.lines == ()


def record(opportunity_id, quality=ListingQuality.NORMAL_LISTING):
    matching = PriorityMatchingSnapshot(
        7,
        opportunity_id,
        0.8,
        1.0,
        MatchLane.PRIMARY,
        "a" * 64,
        "matching-engine-v1",
        "matching-rules-v1",
        "semantic-percentile-v1",
    )
    eligibility = PriorityEligibilitySnapshot(
        7, opportunity_id, GlobalStatus.ELIGIBLE, "e" * 64, "eligibility-rules-v1"
    )
    inputs = PriorityInput(
        matching, eligibility, quality, date(2026, 9, 1), None, date(2026, 9, 2)
    )
    context = PriorityOpportunityContext(
        opportunity_id,
        f"Title {opportunity_id}",
        "Org",
        f"https://source/{opportunity_id}",
        None,
        None,
    )
    return PriorityInputRecord(inputs, context)


def ready(records):
    return PriorityInputAssemblyResult(
        PRIORITY_INPUT_ASSEMBLY_VERSION,
        PriorityReadinessStatus.READY,
        7,
        41,
        9,
        date(2026, 9, 2),
        (),
        tuple(records),
    )


def fake_assessment(
    opportunity_id, category, score, coverage, lane, eligibility_status, published=True
):
    return SimpleNamespace(
        opportunity_id=opportunity_id,
        priority_category=None
        if category == "NONE"
        else SimpleNamespace(value=category),
        priority_score=score,
        priority_evidence_coverage=coverage,
        matching_lane=SimpleNamespace(value=lane),
        eligibility_status=SimpleNamespace(value=eligibility_status),
        published_at=date(2026, 9, 1) if published else None,
        deadline=None,
        reason_codes=(SimpleNamespace(value="MATCH_AVAILABLE"),),
        assessment_fingerprint=f"fp-{opportunity_id}",
    )


def test_ready_report_has_complete_counts_statistics_and_diagnostic_order(monkeypatch):
    records = [
        record(1, ListingQuality.NORMAL_LISTING),
        record(2, ListingQuality.INSUFFICIENT_CONTENT),
        record(3, ListingQuality.POSSIBLE_NON_JOB_PAGE),
        record(4),
        record(5),
        record(6),
        record(7),
    ]
    values = {
        1: fake_assessment(1, "HIGH", 0.8, 0.5, "PRIMARY", "ELIGIBLE"),
        2: fake_assessment(2, "URGENT", 0.9, 0.6, "UNCERTAIN", "UNKNOWN"),
        3: fake_assessment(
            3, "MEDIUM", 0.6, 0.7, "OUTSIDE_PREFERENCES", "INELIGIBLE", False
        ),
        4: fake_assessment(4, "LOW", 0.4, 0.8, "PRIMARY", "ELIGIBLE"),
        5: fake_assessment(5, "IGNORE", 0.7, 0.9, "PRIMARY", "ELIGIBLE"),
        6: fake_assessment(6, "NONE", None, 0.0, "PRIMARY", "ELIGIBLE", False),
        7: fake_assessment(7, "HIGH", 0.8, 1.0, "PRIMARY", "ELIGIBLE"),
    }
    monkeypatch.setattr(
        "services.priority.dry_run.assemble_priority_inputs", lambda *_: ready(records)
    )
    monkeypatch.setattr(
        "services.priority.dry_run.build_priority_assessment",
        lambda item: values[item.matching.opportunity_id],
    )
    report = run_priority_dry_run(object(), 7, date(2026, 9, 2))
    assert report.assessment_count == 7
    assert dict(report.category_counts) == {
        "URGENT": 1,
        "HIGH": 2,
        "MEDIUM": 1,
        "LOW": 1,
        "IGNORE": 1,
        "NONE": 1,
    }
    assert dict(report.matching_lane_counts) == {
        "PRIMARY": 5,
        "UNCERTAIN": 1,
        "OUTSIDE_PREFERENCES": 1,
    }
    assert dict(report.eligibility_counts) == {
        "ELIGIBLE": 5,
        "UNKNOWN": 1,
        "INELIGIBLE": 1,
    }
    assert dict(report.freshness_counts) == {"available": 5, "missing": 2}
    assert dict(report.listing_quality_counts) == {
        "NORMAL_LISTING": 5,
        "INSUFFICIENT_CONTENT": 1,
        "POSSIBLE_NON_JOB_PAGE": 1,
    }
    assert (report.coverage_min, report.coverage_mean, report.coverage_max) == (
        0.0,
        0.642857142857,
        1.0,
    )
    assert [line.opportunity_id for line in report.lines] == [2, 1, 7, 3, 4, 5, 6]
    assert report.evaluation_date == date(2026, 9, 2)


def test_ready_empty_report_has_none_statistics(monkeypatch):
    monkeypatch.setattr(
        "services.priority.dry_run.assemble_priority_inputs", lambda *_: ready(())
    )
    report = run_priority_dry_run(object(), 7, date(2026, 9, 2))
    assert report.assessment_count == 0
    assert (report.coverage_min, report.coverage_mean, report.coverage_max) == (
        None,
        None,
        None,
    )


def test_diagnostic_context_does_not_change_fingerprint(monkeypatch):
    first = record(1)
    second = PriorityInputRecord(
        first.priority_input,
        PriorityOpportunityContext(
            1,
            "Different",
            "Elsewhere",
            "https://other",
            "https://apply",
            "https://canonical",
        ),
    )
    monkeypatch.setattr(
        "services.priority.dry_run.assemble_priority_inputs",
        lambda *_: ready((first, second)),
    )
    report = run_priority_dry_run(object(), 7, date(2026, 9, 2))
    assert (
        report.lines[0].assessment_fingerprint == report.lines[1].assessment_fingerprint
    )


def test_cli_requires_strict_evaluation_date():
    with pytest.raises(SystemExit):
        main(["--database", "db", "--profile-id", "1"])
    with pytest.raises(SystemExit):
        main(["--database", "db", "--profile-id", "1", "--evaluation-date", "today"])
