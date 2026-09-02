"""Atomic assembly, scoring, and persistence orchestration for Priority."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Mapping

from .engine import build_priority_assessment
from .input_assembly import (
    PriorityReadinessIssue,
    PriorityReadinessStatus,
    assemble_priority_inputs,
)
from .models import PriorityCategory
from .persistence import PriorityPersistenceError, store_priority_batch


class PrioritySyncError(RuntimeError):
    """Raised when Priority synchronization cannot safely start."""


@dataclass(frozen=True)
class PrioritySyncResult:
    profile_id: int
    user_id: int | None
    evaluation_date: date
    status: PriorityReadinessStatus
    matching_run_id: int | None
    assessment_count: int
    persisted: bool
    created: bool | None
    state_changed: bool
    run_id: int | None
    run_fingerprint: str | None
    category_counts: Mapping[str, int]
    issues: tuple[PriorityReadinessIssue, ...]


def _preflight(connection: sqlite3.Connection) -> None:
    try:
        migrated = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version='0016'"
        ).fetchone()
        tables = {
            r[0]
            for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('priority_runs','priority_assessments','priority_profile_state')"
            )
        }
    except sqlite3.Error as error:
        raise PrioritySyncError(
            "Priority persistence schema is not ready; migrate through 0016 before sync"
        ) from error
    if migrated is None or tables != {
        "priority_runs",
        "priority_assessments",
        "priority_profile_state",
    }:
        raise PrioritySyncError(
            "Priority persistence schema is not ready; migrate through 0016 before sync"
        )


def sync_priority(
    connection: sqlite3.Connection, profile_id: int, evaluation_date: date
) -> PrioritySyncResult:
    if (
        not isinstance(profile_id, int)
        or isinstance(profile_id, bool)
        or profile_id <= 0
    ):
        raise PrioritySyncError("profile_id must be a positive integer")
    if not isinstance(evaluation_date, date) or isinstance(evaluation_date, datetime):
        raise PrioritySyncError("evaluation_date must be a date")
    if connection.in_transaction:
        raise PrioritySyncError(
            "sync_priority requires a connection without an active transaction"
        )
    connection.execute("BEGIN IMMEDIATE")
    try:
        _preflight(connection)
        assembly = assemble_priority_inputs(connection, profile_id, evaluation_date)
        counts = {category.value: 0 for category in PriorityCategory} | {"NONE": 0}
        if assembly.status is PriorityReadinessStatus.INCOMPLETE:
            connection.execute("ROLLBACK")
            return PrioritySyncResult(
                profile_id,
                assembly.user_id,
                evaluation_date,
                assembly.status,
                assembly.matching_run_id,
                0,
                False,
                None,
                False,
                None,
                None,
                MappingProxyType(counts),
                assembly.issues,
            )
        assessments = tuple(
            build_priority_assessment(record.priority_input)
            for record in assembly.records
        )
        for assessment in assessments:
            counts[
                "NONE"
                if assessment.priority_category is None
                else assessment.priority_category.value
            ] += 1
        matching = connection.execute(
            "SELECT profile_id,run_fingerprint FROM matching_runs WHERE id=?",
            (assembly.matching_run_id,),
        ).fetchone()
        if matching is None or matching[0] != profile_id:
            raise PriorityPersistenceError("matching run provenance is inconsistent")
        current = connection.execute(
            "SELECT current_run_id FROM matching_profile_state WHERE profile_id=? AND state='READY'",
            (profile_id,),
        ).fetchone()
        if current is None or current[0] != assembly.matching_run_id:
            raise PriorityPersistenceError(
                "matching run is not the current READY snapshot"
            )
        stored = store_priority_batch(
            connection,
            profile_id=profile_id,
            user_id=assembly.user_id,
            matching_run_id=assembly.matching_run_id,
            matching_run_fingerprint=matching[1],
            evaluation_date=evaluation_date,
            assessments=assessments,
        )
        connection.execute("COMMIT")
        return PrioritySyncResult(
            profile_id,
            assembly.user_id,
            evaluation_date,
            assembly.status,
            assembly.matching_run_id,
            len(assessments),
            True,
            stored.created,
            stored.state_changed,
            stored.run_id,
            stored.run_fingerprint,
            MappingProxyType(counts),
            (),
        )
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
