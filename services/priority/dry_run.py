"""Deterministic, non-persisting diagnostic execution of Priority v1."""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date

from .engine import build_priority_assessment
from .input_assembly import (
    PriorityInputAssemblyResult,
    PriorityReadinessIssue,
    PriorityReadinessStatus,
    assemble_priority_inputs,
)


@dataclass(frozen=True)
class PriorityDryRunLine:
    opportunity_id: int
    canonical_title: str
    organization: str
    priority_score: float | None
    priority_category: str | None
    priority_evidence_coverage: float
    eligibility_status: str
    matching_lane: str
    published_at: date | None
    deadline: date | None
    source_url: str
    application_url: str | None
    canonical_url: str | None
    reason_codes: tuple[str, ...]
    assessment_fingerprint: str


@dataclass(frozen=True)
class PriorityDryRunReport:
    assembly_version: str
    status: PriorityReadinessStatus
    profile_id: int
    user_id: int | None
    matching_run_id: int | None
    evaluation_date: date
    issues: tuple[PriorityReadinessIssue, ...]
    assessment_count: int
    category_counts: tuple[tuple[str, int], ...]
    matching_lane_counts: tuple[tuple[str, int], ...]
    eligibility_counts: tuple[tuple[str, int], ...]
    freshness_counts: tuple[tuple[str, int], ...]
    listing_quality_counts: tuple[tuple[str, int], ...]
    coverage_min: float | None
    coverage_mean: float | None
    coverage_max: float | None
    lines: tuple[PriorityDryRunLine, ...]


_CATEGORIES = ("URGENT", "HIGH", "MEDIUM", "LOW", "IGNORE", "NONE")
_LANES = ("PRIMARY", "UNCERTAIN", "OUTSIDE_PREFERENCES")
_ELIGIBILITY = ("ELIGIBLE", "UNKNOWN", "INELIGIBLE")
_QUALITIES = ("NORMAL_LISTING", "INSUFFICIENT_CONTENT", "POSSIBLE_NON_JOB_PAGE")


def _empty_report(assembly: PriorityInputAssemblyResult) -> PriorityDryRunReport:
    return PriorityDryRunReport(
        assembly.assembly_version,
        assembly.status,
        assembly.profile_id,
        assembly.user_id,
        assembly.matching_run_id,
        assembly.evaluation_date,
        assembly.issues,
        0,
        tuple((key, 0) for key in _CATEGORIES),
        tuple((key, 0) for key in _LANES),
        tuple((key, 0) for key in _ELIGIBILITY),
        (("available", 0), ("missing", 0)),
        tuple((key, 0) for key in _QUALITIES),
        None,
        None,
        None,
        (),
    )


def run_priority_dry_run(
    connection: sqlite3.Connection, profile_id: int, evaluation_date: date
) -> PriorityDryRunReport:
    """Preflight the entire cohort and score it only when it is wholly ready."""
    assembly = assemble_priority_inputs(connection, profile_id, evaluation_date)
    if assembly.status is not PriorityReadinessStatus.READY:
        return _empty_report(assembly)
    assessments = [
        (record, build_priority_assessment(record.priority_input))
        for record in assembly.records
    ]
    categories = Counter(
        "NONE" if item.priority_category is None else item.priority_category.value
        for _, item in assessments
    )
    lanes = Counter(item.matching_lane.value for _, item in assessments)
    eligibility = Counter(item.eligibility_status.value for _, item in assessments)
    freshness = Counter(
        "missing" if item.published_at is None else "available"
        for _, item in assessments
    )
    qualities = Counter(
        record.priority_input.publication_quality.value for record, _ in assessments
    )
    lines = [
        PriorityDryRunLine(
            item.opportunity_id,
            record.context.canonical_title,
            record.context.organization,
            item.priority_score,
            None if item.priority_category is None else item.priority_category.value,
            item.priority_evidence_coverage,
            item.eligibility_status.value,
            item.matching_lane.value,
            item.published_at,
            item.deadline,
            record.context.source_url,
            record.context.application_url,
            record.context.canonical_url,
            tuple(code.value for code in item.reason_codes),
            item.assessment_fingerprint,
        )
        for record, item in assessments
    ]
    category_rank = {value: index for index, value in enumerate(_CATEGORIES)}
    # Diagnostic order only; this is not a product ranking contract.
    lines.sort(
        key=lambda item: (
            category_rank[item.priority_category or "NONE"],
            item.priority_score is None,
            -(item.priority_score or 0),
            item.opportunity_id,
        )
    )
    coverages = [item.priority_evidence_coverage for _, item in assessments]
    return PriorityDryRunReport(
        assembly.assembly_version,
        assembly.status,
        assembly.profile_id,
        assembly.user_id,
        assembly.matching_run_id,
        assembly.evaluation_date,
        (),
        len(assessments),
        tuple((key, categories[key]) for key in _CATEGORIES),
        tuple((key, lanes[key]) for key in _LANES),
        tuple((key, eligibility[key]) for key in _ELIGIBILITY),
        tuple((key, freshness[key]) for key in ("available", "missing")),
        tuple((key, qualities[key]) for key in _QUALITIES),
        min(coverages) if coverages else None,
        round(sum(coverages) / len(coverages), 12) if coverages else None,
        max(coverages) if coverages else None,
        tuple(lines),
    )
