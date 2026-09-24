"""Strict, immutable, read-only views of persisted Portfolio snapshots."""

from __future__ import annotations

import json
import math
import sqlite3

from services.profile_revision.watermark import SyncPhase, phase_is_current
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from services.collector.matching import MatchLane
from services.eligibility import GlobalStatus
from services.priority import PriorityCategory

from .models import PortfolioBucket, PortfolioDisposition


class PortfolioReadError(RuntimeError):
    """Raised when persisted Portfolio data cannot be read safely."""


class PortfolioProfileReadStatus(StrEnum):
    NOT_SYNCED = "NOT_SYNCED"
    READY = "READY"


def _positive(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PortfolioReadError(f"{name} must be a positive integer")
    return value


def _require_profile(connection: sqlite3.Connection, profile_id: int) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM profiles WHERE id=?", (profile_id,)
        ).fetchone()
        is None
    ):
        raise PortfolioReadError(f"profile {profile_id} does not exist")


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _object(raw: object, context: str) -> Mapping[str, Any]:
    if not isinstance(raw, str):
        raise PortfolioReadError(f"invalid {context} JSON")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as error:
        raise PortfolioReadError(f"invalid {context} JSON") from error
    if not isinstance(value, dict):
        raise PortfolioReadError(f"{context} payload is not an object")
    return _freeze(value)


def _enum(kind: type[Any], value: object, context: str) -> Any:
    try:
        return kind(value)
    except (TypeError, ValueError) as error:
        raise PortfolioReadError(f"invalid {context}") from error


@dataclass(frozen=True)
class PortfolioAssessmentReadModel:
    opportunity_id: int
    disposition: PortfolioDisposition
    bucket: PortfolioBucket | None
    priority_category: PriorityCategory | None
    eligibility_status: GlobalStatus
    matching_lane: MatchLane
    required_skill_score: float | None
    required_skill_matched_count: int
    required_skill_total_count: int
    safe_cap_applied: bool
    assessment_fingerprint: str
    created_at: str
    assessment_payload: Mapping[str, Any]


@dataclass(frozen=True)
class PortfolioRunReadModel:
    run_id: int
    profile_id: int
    priority_run_id: int
    matching_run_id: int
    persistence_version: str
    input_assembly_version: str
    portfolio_engine_version: str
    portfolio_rules_version: str
    priority_run_fingerprint: str
    matching_run_fingerprint: str
    run_fingerprint: str
    assessment_count: int
    included_count: int
    excluded_count: int
    safe_count: int
    target_count: int
    ambitious_count: int
    created_at: str
    run_payload: Mapping[str, Any]
    assessments: tuple[PortfolioAssessmentReadModel, ...]


@dataclass(frozen=True)
class PortfolioRunSummary:
    run_id: int
    priority_run_id: int
    matching_run_id: int
    assessment_count: int
    included_count: int
    excluded_count: int
    safe_count: int
    target_count: int
    ambitious_count: int
    persistence_version: str
    input_assembly_version: str
    portfolio_engine_version: str
    portfolio_rules_version: str
    priority_run_fingerprint: str
    matching_run_fingerprint: str
    run_fingerprint: str
    created_at: str
    is_current: bool


@dataclass(frozen=True)
class PortfolioProfileReadModel:
    profile_id: int
    status: PortfolioProfileReadStatus
    persistence_version: str | None
    input_assembly_version: str | None
    current_run_id: int | None
    current_run: PortfolioRunReadModel | None
    history_count: int


def _assessment(row: tuple, run_id: int) -> PortfolioAssessmentReadModel:
    oid = row[0]
    disposition = _enum(PortfolioDisposition, row[1], "Portfolio disposition")
    bucket = (
        None if row[2] is None else _enum(PortfolioBucket, row[2], "Portfolio bucket")
    )
    if (disposition is PortfolioDisposition.INCLUDED) != (bucket is not None):
        raise PortfolioReadError(
            f"invalid disposition/bucket for run {run_id}, opportunity {oid}"
        )
    category = (
        None if row[3] is None else _enum(PriorityCategory, row[3], "Priority category")
    )
    eligibility = _enum(GlobalStatus, row[4], "Eligibility status")
    lane = _enum(MatchLane, row[5], "Matching lane")
    score, matched, total = row[6:9]
    if (
        any(
            isinstance(v, bool) or not isinstance(v, int) or v < 0
            for v in (matched, total)
        )
        or matched > total
    ):
        raise PortfolioReadError("invalid required-skill counts")
    if total == 0:
        valid = matched == 0 and score is None
        normalized = None
    else:
        valid = (
            not isinstance(score, bool)
            and isinstance(score, (int, float))
            and math.isfinite(score)
            and 0 <= score <= 1
            and round(float(score), 12) == round(matched / total, 12)
        )
        normalized = float(score) if valid else None
    if not valid:
        raise PortfolioReadError("invalid required-skill score")
    if row[9] not in (0, 1) or isinstance(row[9], bool):
        raise PortfolioReadError("invalid safe_cap_applied")
    payload = _object(row[12], f"assessment for run {run_id}, opportunity {oid}")
    return PortfolioAssessmentReadModel(
        oid,
        disposition,
        bucket,
        category,
        eligibility,
        lane,
        normalized,
        matched,
        total,
        bool(row[9]),
        row[10],
        row[11],
        payload,
    )


def read_portfolio_run(
    connection: sqlite3.Connection, run_id: int
) -> PortfolioRunReadModel:
    run_id = _positive(run_id, "run_id")
    row = connection.execute(
        """SELECT id,profile_id,priority_run_id,matching_run_id,persistence_version,
        input_assembly_version,portfolio_engine_version,portfolio_rules_version,
        priority_run_fingerprint,matching_run_fingerprint,run_fingerprint,assessment_count,
        included_count,excluded_count,safe_count,target_count,ambitious_count,created_at,run_payload_json
        FROM portfolio_runs WHERE id=?""",
        (run_id,),
    ).fetchone()
    if row is None:
        raise PortfolioReadError(f"portfolio run {run_id} does not exist")
    payload = _object(row[18], f"run {run_id}")
    rows = connection.execute(
        """SELECT opportunity_id,disposition,bucket,priority_category,
        eligibility_status,matching_lane,required_skill_score,required_skill_matched_count,
        required_skill_total_count,safe_cap_applied,assessment_fingerprint,created_at,
        assessment_payload_json FROM portfolio_assessments WHERE run_id=? ORDER BY opportunity_id ASC""",
        (run_id,),
    ).fetchall()
    assessments = tuple(_assessment(item, run_id) for item in rows)
    stored = tuple(row[index] for index in range(11, 17))
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in stored
    ):
        raise PortfolioReadError(f"invalid counts for portfolio run {run_id}")
    counts = Counter(item.disposition.value for item in assessments)
    buckets = Counter(
        None if item.bucket is None else item.bucket.value for item in assessments
    )
    expected = (
        len(assessments),
        counts["INCLUDED"],
        counts["EXCLUDED"],
        buckets["SAFE"],
        buckets["TARGET"],
        buckets["AMBITIOUS"],
    )
    if (
        stored != expected
        or row[12] + row[13] != row[11]
        or row[14] + row[15] + row[16] != row[12]
    ):
        raise PortfolioReadError(f"count mismatch for portfolio run {run_id}")
    return PortfolioRunReadModel(*row[:18], payload, assessments)


def list_portfolio_runs(
    connection: sqlite3.Connection, profile_id: int
) -> tuple[PortfolioRunSummary, ...]:
    profile_id = _positive(profile_id, "profile_id")
    _require_profile(connection, profile_id)
    state = connection.execute(
        "SELECT current_run_id FROM portfolio_profile_state WHERE profile_id=?",
        (profile_id,),
    ).fetchone()
    current = None if state is None else state[0]
    if not phase_is_current(connection, profile_id, SyncPhase.PORTFOLIO):
        # The whole history is still listed; none of it is `is_current`.
        current = None
    rows = connection.execute(
        """SELECT id,priority_run_id,matching_run_id,assessment_count,
        included_count,excluded_count,safe_count,target_count,ambitious_count,persistence_version,
        input_assembly_version,portfolio_engine_version,portfolio_rules_version,
        priority_run_fingerprint,matching_run_fingerprint,run_fingerprint,created_at
        FROM portfolio_runs WHERE profile_id=? ORDER BY id DESC""",
        (profile_id,),
    ).fetchall()
    return tuple(PortfolioRunSummary(*row, row[0] == current) for row in rows)


def read_current_portfolio(
    connection: sqlite3.Connection, profile_id: int
) -> PortfolioProfileReadModel:
    profile_id = _positive(profile_id, "profile_id")
    _require_profile(connection, profile_id)
    history = connection.execute(
        "SELECT COUNT(*) FROM portfolio_runs WHERE profile_id=?", (profile_id,)
    ).fetchone()[0]
    if not phase_is_current(connection, profile_id, SyncPhase.PORTFOLIO):
        # A CV activation moved the profile to a new revision and Portfolio, or
        # the Priority or Matching it is built from, has not been recomputed for
        # it. The stored state row and every run stay exactly as they are — this
        # answer is derived, nothing is deleted — and the history stays readable
        # through `read_portfolio_run` and `list_portfolio_runs`. `NOT_SYNCED`
        # is the existing vocabulary for "no result to present as current", so
        # no new status reaches the API or the page.
        return PortfolioProfileReadModel(
            profile_id,
            PortfolioProfileReadStatus.NOT_SYNCED,
            None,
            None,
            None,
            None,
            history,
        )
    state = connection.execute(
        "SELECT current_run_id,persistence_version,input_assembly_version FROM portfolio_profile_state WHERE profile_id=?",
        (profile_id,),
    ).fetchone()
    if state is None:
        if history:
            raise PortfolioReadError(
                f"profile {profile_id} has Portfolio runs without state"
            )
        return PortfolioProfileReadModel(
            profile_id, PortfolioProfileReadStatus.NOT_SYNCED, None, None, None, None, 0
        )
    current = _positive(state[0], "current_run_id")
    run = read_portfolio_run(connection, current)
    if run.profile_id != profile_id:
        raise PortfolioReadError("current Portfolio run belongs to another profile")
    if (state[1], state[2]) != (run.persistence_version, run.input_assembly_version):
        raise PortfolioReadError("Portfolio state versions do not match current run")
    return PortfolioProfileReadModel(
        profile_id,
        PortfolioProfileReadStatus.READY,
        state[1],
        state[2],
        current,
        run,
        history,
    )
