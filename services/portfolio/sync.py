"""Atomic assembly, classification, and persistence for Portfolio."""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .engine import build_portfolio_assessments
from .input_assembly import (
    PORTFOLIO_INPUT_ASSEMBLY_VERSION,
    PortfolioAssemblyIssue,
    PortfolioAssemblyStatus,
    assemble_portfolio_inputs,
)
from .models import PortfolioBucket, PortfolioDisposition
from .persistence import PortfolioPersistenceError, store_portfolio_batch
from services.profile_revision.watermark import (
    SyncPhase,
    advance_sync_watermark_in_transaction,
)


class PortfolioSyncError(RuntimeError):
    """Raised when Portfolio synchronization cannot safely start."""


@dataclass(frozen=True)
class PortfolioSyncResult:
    profile_id: int
    status: PortfolioAssemblyStatus
    input_assembly_version: str
    priority_run_id: int | None
    matching_run_id: int | None
    assessment_count: int
    included_count: int
    excluded_count: int
    bucket_counts: Mapping[str, int]
    persisted: bool
    created: bool | None
    state_changed: bool
    run_id: int | None
    run_fingerprint: str | None
    issues: tuple[PortfolioAssemblyIssue, ...]


def _preflight(connection: sqlite3.Connection) -> None:
    try:
        migrated = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version='0017'"
        ).fetchone()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('portfolio_runs','portfolio_assessments','portfolio_profile_state')"
            )
        }
    except sqlite3.Error as error:
        raise PortfolioSyncError(
            "Portfolio persistence schema is not ready; migrate through 0017 before sync"
        ) from error
    if migrated is None or tables != {
        "portfolio_runs",
        "portfolio_assessments",
        "portfolio_profile_state",
    }:
        raise PortfolioSyncError(
            "Portfolio persistence schema is not ready; migrate through 0017 before sync"
        )


def sync_portfolio(
    connection: sqlite3.Connection, profile_id: int
) -> PortfolioSyncResult:
    if (
        isinstance(profile_id, bool)
        or not isinstance(profile_id, int)
        or profile_id <= 0
    ):
        raise PortfolioSyncError("profile_id must be a positive integer")
    if connection.in_transaction:
        raise PortfolioSyncError(
            "sync_portfolio requires a connection without an active transaction"
        )
    connection.execute("BEGIN IMMEDIATE")
    try:
        _preflight(connection)
        assembly = assemble_portfolio_inputs(connection, profile_id)
        zero = {bucket.value: 0 for bucket in PortfolioBucket}
        if assembly.status is PortfolioAssemblyStatus.INCOMPLETE:
            connection.execute("ROLLBACK")
            return PortfolioSyncResult(
                profile_id,
                assembly.status,
                PORTFOLIO_INPUT_ASSEMBLY_VERSION,
                assembly.priority_run_id,
                assembly.matching_run_id,
                0,
                0,
                0,
                MappingProxyType(zero),
                False,
                None,
                False,
                None,
                None,
                assembly.issues,
            )
        current = connection.execute(
            "SELECT current_run_id FROM priority_profile_state WHERE profile_id=?",
            (profile_id,),
        ).fetchone()
        if current is None or current[0] != assembly.priority_run_id:
            raise PortfolioPersistenceError("Priority run is not the current snapshot")
        assessments = build_portfolio_assessments(assembly.inputs)
        stored = store_portfolio_batch(
            connection,
            profile_id=profile_id,
            priority_run_id=assembly.priority_run_id,
            priority_run_fingerprint=assembly.priority_run_fingerprint,
            matching_run_id=assembly.matching_run_id,
            matching_run_fingerprint=assembly.matching_run_fingerprint,
            assessments=assessments,
        )
        counts = Counter(a.bucket for a in assessments if a.bucket is not None)
        buckets = {bucket.value: counts[bucket] for bucket in PortfolioBucket}
        included = sum(
            a.disposition is PortfolioDisposition.INCLUDED for a in assessments
        )
        # Classified from the priority run this transaction just proved
        # current. Refused while Priority or Matching is behind the active
        # revision, so a portfolio can never be published as current on top of
        # a ranking that still describes the previous CV.
        advance_sync_watermark_in_transaction(
            connection, profile_id=profile_id, phase=SyncPhase.PORTFOLIO
        )
        connection.execute("COMMIT")
        return PortfolioSyncResult(
            profile_id,
            assembly.status,
            PORTFOLIO_INPUT_ASSEMBLY_VERSION,
            assembly.priority_run_id,
            assembly.matching_run_id,
            len(assessments),
            included,
            len(assessments) - included,
            MappingProxyType(buckets),
            True,
            stored.created,
            stored.state_changed,
            stored.run_id,
            stored.run_fingerprint,
            (),
        )
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
