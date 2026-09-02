"""Read-only diagnostic execution of the Portfolio classifier."""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass

from .engine import build_portfolio_assessments
from .input_assembly import (
    PortfolioAssemblyIssue,
    PortfolioAssemblyStatus,
    assemble_portfolio_inputs,
)
from .models import PortfolioAssessment, PortfolioBucket, PortfolioDisposition


@dataclass(frozen=True)
class PortfolioDryRunResult:
    profile_id: int
    status: PortfolioAssemblyStatus
    input_assembly_version: str
    priority_run_id: int | None
    priority_run_fingerprint: str | None
    matching_run_id: int | None
    matching_run_fingerprint: str | None
    assessment_count: int
    included_count: int
    excluded_count: int
    bucket_counts: tuple[tuple[str, int], ...]
    assessments: tuple[PortfolioAssessment, ...]
    issues: tuple[PortfolioAssemblyIssue, ...]


def dry_run_portfolio(
    connection: sqlite3.Connection, profile_id: int
) -> PortfolioDryRunResult:
    """Classify every input only after the complete upstream preflight succeeds."""
    assembly = assemble_portfolio_inputs(connection, profile_id)
    if assembly.status is not PortfolioAssemblyStatus.READY:
        return PortfolioDryRunResult(
            assembly.profile_id,
            assembly.status,
            assembly.input_assembly_version,
            assembly.priority_run_id,
            assembly.priority_run_fingerprint,
            assembly.matching_run_id,
            assembly.matching_run_fingerprint,
            0,
            0,
            0,
            tuple((bucket.value, 0) for bucket in PortfolioBucket),
            (),
            assembly.issues,
        )
    assessments = build_portfolio_assessments(assembly.inputs)
    counts = Counter(item.bucket for item in assessments if item.bucket is not None)
    included = sum(
        item.disposition is PortfolioDisposition.INCLUDED for item in assessments
    )
    return PortfolioDryRunResult(
        assembly.profile_id,
        assembly.status,
        assembly.input_assembly_version,
        assembly.priority_run_id,
        assembly.priority_run_fingerprint,
        assembly.matching_run_id,
        assembly.matching_run_fingerprint,
        len(assessments),
        included,
        len(assessments) - included,
        tuple((bucket.value, counts[bucket]) for bucket in PortfolioBucket),
        assessments,
        (),
    )
