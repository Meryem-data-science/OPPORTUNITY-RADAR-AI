"""Strict, read-only views of persisted matching snapshots."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


class MatchingReadError(RuntimeError):
    """Raised when persisted matching data cannot be read safely."""


def _positive(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise MatchingReadError(f"{name} must be a positive integer")
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class MatchingAssessmentReadModel:
    opportunity_id: int
    lane: str
    match_quality: float | None
    evidence_coverage: float
    assessment_fingerprint: str
    created_at: str
    assessment_payload: Mapping[str, Any]


@dataclass(frozen=True)
class MatchingRunReadModel:
    run_id: int
    profile_id: int
    persistence_version: str
    selection_version: str
    matching_engine_version: str
    matching_rules_version: str
    semantic_percentile_version: str
    corpus_fingerprint: str
    tfidf_model_fingerprint: str
    batch_fingerprint: str
    run_fingerprint: str
    assessment_count: int
    created_at: str
    batch_payload: Mapping[str, Any]
    assessments: tuple[MatchingAssessmentReadModel, ...]


@dataclass(frozen=True)
class MatchingRunSummary:
    run_id: int
    created_at: str
    assessment_count: int
    persistence_version: str
    selection_version: str
    matching_engine_version: str
    matching_rules_version: str
    semantic_percentile_version: str
    run_fingerprint: str
    batch_fingerprint: str
    is_current: bool


@dataclass(frozen=True)
class MatchingProfileReadModel:
    profile_id: int
    status: str
    persistence_version: str | None
    selection_version: str | None
    current_run_id: int | None
    current_run: MatchingRunReadModel | None
    history_count: int


def _require_profile(connection: sqlite3.Connection, profile_id: int) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM profiles WHERE id=?", (profile_id,)
        ).fetchone()
        is None
    ):
        raise MatchingReadError(f"profile {profile_id} does not exist")


def read_matching_run(
    connection: sqlite3.Connection, run_id: int
) -> MatchingRunReadModel:
    run_id = _positive(run_id, "run_id")
    row = connection.execute(
        """SELECT id,profile_id,persistence_version,selection_version,
        matching_engine_version,matching_rules_version,semantic_percentile_version,
        corpus_fingerprint,tfidf_model_fingerprint,batch_fingerprint,run_fingerprint,
        assessment_count,created_at,batch_payload_json FROM matching_runs WHERE id=?""",
        (run_id,),
    ).fetchone()
    if row is None:
        raise MatchingReadError(f"matching run {run_id} does not exist")
    try:
        batch_payload = json.loads(row[13])
    except (json.JSONDecodeError, TypeError) as error:
        raise MatchingReadError(
            f"invalid batch JSON for matching run {run_id}"
        ) from error
    if not isinstance(batch_payload, dict):
        raise MatchingReadError(
            f"batch payload for matching run {run_id} is not an object"
        )
    assessments = []
    for item in connection.execute(
        """SELECT opportunity_id,lane,match_quality,evidence_coverage,
        assessment_fingerprint,created_at,assessment_payload_json
        FROM matching_assessments WHERE run_id=? ORDER BY opportunity_id ASC""",
        (run_id,),
    ):
        try:
            payload = json.loads(item[6])
        except (json.JSONDecodeError, TypeError) as error:
            raise MatchingReadError(
                f"invalid assessment JSON for run {run_id}, opportunity {item[0]}"
            ) from error
        if not isinstance(payload, dict):
            raise MatchingReadError(
                f"assessment payload for run {run_id}, opportunity {item[0]} is not an object"
            )
        assessments.append(MatchingAssessmentReadModel(*item[:6], _freeze(payload)))
    if len(assessments) != row[11]:
        raise MatchingReadError(f"assessment count mismatch for matching run {run_id}")
    return MatchingRunReadModel(*row[:13], _freeze(batch_payload), tuple(assessments))


def list_matching_runs(
    connection: sqlite3.Connection, profile_id: int
) -> tuple[MatchingRunSummary, ...]:
    profile_id = _positive(profile_id, "profile_id")
    _require_profile(connection, profile_id)
    state = connection.execute(
        "SELECT current_run_id FROM matching_profile_state WHERE profile_id=?",
        (profile_id,),
    ).fetchone()
    current_run_id = None if state is None else state[0]
    rows = connection.execute(
        """SELECT id,created_at,assessment_count,persistence_version,selection_version,
        matching_engine_version,matching_rules_version,semantic_percentile_version,
        run_fingerprint,batch_fingerprint FROM matching_runs
        WHERE profile_id=? ORDER BY id DESC""",
        (profile_id,),
    ).fetchall()
    return tuple(MatchingRunSummary(*row, row[0] == current_run_id) for row in rows)


def read_current_matching(
    connection: sqlite3.Connection, profile_id: int
) -> MatchingProfileReadModel:
    profile_id = _positive(profile_id, "profile_id")
    _require_profile(connection, profile_id)
    history_count = connection.execute(
        "SELECT COUNT(*) FROM matching_runs WHERE profile_id=?", (profile_id,)
    ).fetchone()[0]
    state = connection.execute(
        """SELECT state,current_run_id,persistence_version,selection_version
        FROM matching_profile_state WHERE profile_id=?""",
        (profile_id,),
    ).fetchone()
    if state is None:
        if history_count:
            raise MatchingReadError(
                f"profile {profile_id} has matching runs without profile state"
            )
        return MatchingProfileReadModel(
            profile_id, "NOT_SYNCED", None, None, None, None, history_count
        )
    status, current_run_id, persistence_version, selection_version = state
    if status == "EMPTY" and current_run_id is None:
        return MatchingProfileReadModel(
            profile_id,
            status,
            persistence_version,
            selection_version,
            None,
            None,
            history_count,
        )
    if status != "READY" or not isinstance(current_run_id, int):
        raise MatchingReadError(f"invalid matching state for profile {profile_id}")
    run = read_matching_run(connection, current_run_id)
    if run.profile_id != profile_id:
        raise MatchingReadError(
            f"current run {current_run_id} belongs to another profile"
        )
    return MatchingProfileReadModel(
        profile_id,
        status,
        persistence_version,
        selection_version,
        current_run_id,
        run,
        history_count,
    )
