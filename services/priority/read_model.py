"""Strict, read-only views of persisted Priority snapshots."""

from __future__ import annotations

import json
import math
import sqlite3

from services.profile_revision.watermark import SyncPhase, phase_is_current
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from services.collector.matching import MatchLane
from services.eligibility import GlobalStatus

from .models import PriorityCategory


class PriorityReadError(RuntimeError):
    """Raised when persisted Priority data cannot be read safely."""


class PriorityProfileReadStatus(StrEnum):
    NOT_SYNCED = "NOT_SYNCED"
    READY = "READY"


def _positive(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PriorityReadError(f"{name} must be a positive integer")
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class PriorityAssessmentReadModel:
    opportunity_id: int
    priority_score: float | None
    priority_evidence_coverage: float
    priority_category: PriorityCategory | None
    eligibility_status: GlobalStatus
    matching_lane: MatchLane
    assessment_fingerprint: str
    created_at: str
    assessment_payload: Mapping[str, Any]


@dataclass(frozen=True)
class PriorityRunReadModel:
    run_id: int
    profile_id: int
    user_id: int
    matching_run_id: int
    persistence_version: str
    input_assembly_version: str
    priority_engine_version: str
    priority_rules_version: str
    freshness_version: str
    quality_version: str
    evaluation_date: date
    matching_run_fingerprint: str
    run_fingerprint: str
    assessment_count: int
    created_at: str
    run_payload: Mapping[str, Any]
    assessments: tuple[PriorityAssessmentReadModel, ...]


@dataclass(frozen=True)
class PriorityRunSummary:
    run_id: int
    evaluation_date: date
    matching_run_id: int
    assessment_count: int
    persistence_version: str
    input_assembly_version: str
    priority_engine_version: str
    priority_rules_version: str
    freshness_version: str
    quality_version: str
    run_fingerprint: str
    matching_run_fingerprint: str
    created_at: str
    is_current: bool


@dataclass(frozen=True)
class PriorityProfileReadModel:
    profile_id: int
    status: PriorityProfileReadStatus
    persistence_version: str | None
    input_assembly_version: str | None
    current_run_id: int | None
    current_run: PriorityRunReadModel | None
    history_count: int


def _require_profile(connection: sqlite3.Connection, profile_id: int) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM profiles WHERE id=?", (profile_id,)
        ).fetchone()
        is None
    ):
        raise PriorityReadError(f"profile {profile_id} does not exist")


def _parse_date(value: object, context: str) -> date:
    if not isinstance(value, str):
        raise PriorityReadError(f"invalid evaluation date for {context}")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise PriorityReadError(f"invalid evaluation date for {context}") from error
    if parsed.isoformat() != value:
        raise PriorityReadError(f"invalid evaluation date for {context}")
    return parsed


def _number(value: object, *, nullable: bool, context: str) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PriorityReadError(f"invalid {context}")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise PriorityReadError(f"invalid {context}")
    return result


def _enum(enum_type: type[Any], value: object, context: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as error:
        raise PriorityReadError(f"invalid {context}") from error


def read_priority_run(
    connection: sqlite3.Connection, run_id: int
) -> PriorityRunReadModel:
    run_id = _positive(run_id, "run_id")
    row = connection.execute(
        """SELECT id,profile_id,user_id,matching_run_id,persistence_version,
        input_assembly_version,priority_engine_version,priority_rules_version,
        freshness_version,quality_version,evaluation_date,matching_run_fingerprint,
        run_fingerprint,assessment_count,created_at,run_payload_json
        FROM priority_runs WHERE id=?""",
        (run_id,),
    ).fetchone()
    if row is None:
        raise PriorityReadError(f"priority run {run_id} does not exist")
    try:
        run_payload = json.loads(row[15])
    except (json.JSONDecodeError, TypeError) as error:
        raise PriorityReadError(
            f"invalid run JSON for priority run {run_id}"
        ) from error
    if not isinstance(run_payload, dict):
        raise PriorityReadError(
            f"run payload for priority run {run_id} is not an object"
        )

    assessments = []
    rows = connection.execute(
        """SELECT opportunity_id,priority_score,priority_evidence_coverage,
        priority_category,eligibility_status,matching_lane,assessment_fingerprint,
        created_at,assessment_payload_json FROM priority_assessments
        WHERE run_id=? ORDER BY opportunity_id ASC""",
        (run_id,),
    ).fetchall()
    for item in rows:
        opportunity_id = item[0]
        try:
            payload = json.loads(item[8])
        except (json.JSONDecodeError, TypeError) as error:
            raise PriorityReadError(
                f"invalid assessment JSON for run {run_id}, opportunity {opportunity_id}"
            ) from error
        if not isinstance(payload, dict):
            raise PriorityReadError(
                f"assessment payload for run {run_id}, opportunity {opportunity_id} is not an object"
            )
        category = (
            None
            if item[3] is None
            else _enum(
                PriorityCategory,
                item[3],
                f"priority category for run {run_id}, opportunity {opportunity_id}",
            )
        )
        assessments.append(
            PriorityAssessmentReadModel(
                opportunity_id,
                _number(
                    item[1],
                    nullable=True,
                    context=f"priority score for run {run_id}, opportunity {opportunity_id}",
                ),
                _number(
                    item[2],
                    nullable=False,
                    context=f"priority coverage for run {run_id}, opportunity {opportunity_id}",
                ),
                category,
                _enum(
                    GlobalStatus,
                    item[4],
                    f"eligibility status for run {run_id}, opportunity {opportunity_id}",
                ),
                _enum(
                    MatchLane,
                    item[5],
                    f"matching lane for run {run_id}, opportunity {opportunity_id}",
                ),
                item[6],
                item[7],
                _freeze(payload),
            )
        )
    if len(assessments) != row[13]:
        raise PriorityReadError(f"assessment count mismatch for priority run {run_id}")
    return PriorityRunReadModel(
        *row[:10],
        _parse_date(row[10], f"priority run {run_id}"),
        *row[11:15],
        _freeze(run_payload),
        tuple(assessments),
    )


def list_priority_runs(
    connection: sqlite3.Connection, profile_id: int
) -> tuple[PriorityRunSummary, ...]:
    profile_id = _positive(profile_id, "profile_id")
    _require_profile(connection, profile_id)
    state = connection.execute(
        "SELECT current_run_id FROM priority_profile_state WHERE profile_id=?",
        (profile_id,),
    ).fetchone()
    current_run_id = None if state is None else state[0]
    if not phase_is_current(connection, profile_id, SyncPhase.PRIORITY):
        # The whole history is still listed; none of it is `is_current`.
        current_run_id = None
    rows = connection.execute(
        """SELECT id,evaluation_date,matching_run_id,assessment_count,persistence_version,
        input_assembly_version,priority_engine_version,priority_rules_version,
        freshness_version,quality_version,run_fingerprint,matching_run_fingerprint,created_at
        FROM priority_runs WHERE profile_id=? ORDER BY id DESC""",
        (profile_id,),
    ).fetchall()
    return tuple(
        PriorityRunSummary(
            row[0],
            _parse_date(row[1], f"priority run {row[0]}"),
            *row[2:],
            row[0] == current_run_id,
        )
        for row in rows
    )


def read_current_priority(
    connection: sqlite3.Connection, profile_id: int
) -> PriorityProfileReadModel:
    profile_id = _positive(profile_id, "profile_id")
    _require_profile(connection, profile_id)
    history_count = connection.execute(
        "SELECT COUNT(*) FROM priority_runs WHERE profile_id=?", (profile_id,)
    ).fetchone()[0]
    if not phase_is_current(connection, profile_id, SyncPhase.PRIORITY):
        # A CV activation moved the profile to a new revision and Priority, or
        # the Matching it is scored from, has not been recomputed for it. The
        # stored state row and every run are left untouched — this answer is
        # derived, nothing is deleted — and the history stays readable through
        # `read_priority_run` and `list_priority_runs`. `NOT_SYNCED` is the
        # existing vocabulary for "no result to present as current", so no new
        # status reaches the API or the page.
        return PriorityProfileReadModel(
            profile_id,
            PriorityProfileReadStatus.NOT_SYNCED,
            None,
            None,
            None,
            None,
            history_count,
        )
    state = connection.execute(
        """SELECT current_run_id,persistence_version,input_assembly_version
        FROM priority_profile_state WHERE profile_id=?""",
        (profile_id,),
    ).fetchone()
    if state is None:
        if history_count:
            raise PriorityReadError(
                f"profile {profile_id} has priority runs without profile state"
            )
        return PriorityProfileReadModel(
            profile_id, PriorityProfileReadStatus.NOT_SYNCED, None, None, None, None, 0
        )
    current_run_id, persistence_version, input_assembly_version = state
    if (
        not isinstance(current_run_id, int)
        or isinstance(current_run_id, bool)
        or current_run_id <= 0
    ):
        raise PriorityReadError(f"invalid priority state for profile {profile_id}")
    run = read_priority_run(connection, current_run_id)
    if run.profile_id != profile_id:
        raise PriorityReadError(
            f"current run {current_run_id} belongs to another profile"
        )
    if (persistence_version, input_assembly_version) != (
        run.persistence_version,
        run.input_assembly_version,
    ):
        raise PriorityReadError(
            f"priority state version mismatch for profile {profile_id}"
        )
    return PriorityProfileReadModel(
        profile_id,
        PriorityProfileReadStatus.READY,
        persistence_version,
        input_assembly_version,
        current_run_id,
        run,
        history_count,
    )
