"""Strict, read-only views of persisted recommendation runs.

Phase 9B.1 gave an already-computed `RecommendationBatchResult` somewhere to
live. This module is the other half of that boundary: it reads what was stored
and hands it back exactly as persisted, or refuses to hand back anything at all.

Nothing here writes, and nothing here repairs. A run whose stored ranking has a
hole in it, a state row that claims READY while carrying readiness issues, a
profile with recommendation history but no state row — each of those is reported
as a `RecommendationReadError` rather than smoothed into a plausible-looking
answer. A read model that silently corrects its own storage is a read model that
cannot be used as evidence of anything.

Three questions are answered, and no more:

    read_recommendation_run         one stored run, with its ranking in the
                                    persisted order
    list_recommendation_runs        this profile's recommendation history, and
                                    which run is the current one
    read_current_recommendation     what this profile's recommendation state is
                                    right now: NOT_SYNCED, READY or INCOMPLETE

What this module deliberately does **not** do, and the distinction matters:

    it does not recompute the three recommendation digests
        `recommendation_assessment_fingerprint`,
        `recommendation_batch_fingerprint` and `recommendation_run_fingerprint`
        prove that a stored history is *internally* what it claims to be. That
        is a whole-history cryptographic statement, it is expensive, and it
        belongs to the Phase 9B.2b persistence audit. What is verified here is
        only what a *safe read* requires: the shapes, the ranking, the business
        contract of the columns, and that every exposed digest at least has the
        form of a SHA-256 digest.

    it does not judge freshness
        a run whose `source_matching_run_id` is no longer the profile's current
        Matching run is still perfectly valid **history**. Comparing the two is
        how a reader would quietly turn "this is what we recommended in March"
        into "this is wrong", and it belongs to Phase 9B.3's synchronization,
        not to a reader. Nothing below ever looks at
        `matching_profile_state.current_run_id`.

    it does not require the stored versions to be today's versions
        `persistence_version` and `input_assembly_version` are checked for
        internal agreement between a state row and the run it points at. They
        are **not** compared against the constants this code ships with: that
        would be a staleness check, it would make old history unreadable, and it
        is again 9B.3's question.

The JSON payloads come back frozen — objects as `MappingProxyType`, arrays as
tuples, recursively — so a consumer cannot mutate what it was shown and hand the
mutation on as if it had been read from the database.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from services.collector.matching.fingerprint import canonical_json

from .input_assembly import (
    RecommendationReadinessIssue,
    RecommendationReadinessIssueCode,
)
from .models import RecommendationDisposition

__all__ = [
    "RecommendationAssessmentReadModel",
    "RecommendationProfileReadModel",
    "RecommendationReadError",
    "RecommendationRunReadModel",
    "RecommendationRunSummary",
    "list_recommendation_runs",
    "read_current_recommendation",
    "read_recommendation_run",
]

#: The canonical encoding of "no readiness issue", byte for byte as persistence
#: writes it for a READY state. READY is not merely *an empty list*: it is this
#: exact stored text, so an equally empty but differently spelled value is a
#: state nothing in this project wrote and is refused.
_NO_READINESS_ISSUES = canonical_json([])

#: NOT_SYNCED is the absence of a state row, never a stored value. The two the
#: schema does store are below.
_STATUS_NOT_SYNCED = "NOT_SYNCED"
_STATUS_READY = "READY"
_STATUS_INCOMPLETE = "INCOMPLETE"

_DISPOSITIONS = frozenset(item.value for item in RecommendationDisposition)


class RecommendationReadError(RuntimeError):
    """Raised when persisted recommendation data cannot be read safely."""


# --------------------------------------------------------------------------
# value checks
#
# `True` is an `int` in Python and compares equal to `1`. Every operational
# integer below therefore refuses `bool` outright, exactly as Phase 9B.1's
# persistence does: a caller asking for run `True` must not be answered with
# run `1`.
# --------------------------------------------------------------------------


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _positive(value: object, name: str) -> int:
    if not _is_positive_int(value):
        raise RecommendationReadError(f"{name} must be a positive integer")
    return int(value)


def _require_text(value: object, description: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RecommendationReadError(f"{description} is not stored text")
    return value


def _require_fingerprint(value: object, description: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RecommendationReadError(f"{description} is not a SHA-256 digest")
    return value


def _require_ratio(value: object, description: str) -> float:
    """A stored `[0, 1]` number: finite, real, and not a disguised boolean."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        raise RecommendationReadError(f"{description} is not a ratio in [0, 1]")
    return float(value)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _decode_object(raw: object, description: str) -> dict[str, Any]:
    """Decode one stored JSON envelope that the schema promises is an object."""
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise RecommendationReadError(f"invalid {description}") from error
    if not isinstance(decoded, dict):
        raise RecommendationReadError(f"{description} is not an object")
    return decoded


def _query(
    connection: sqlite3.Connection, sql: str, parameters: tuple, description: str
) -> list[tuple]:
    """Run one SELECT, and never let a raw `sqlite3.Error` become the contract.

    A caller of this module handles `RecommendationReadError`. A locked
    database, a missing table or a connection opened read-only against a
    read-write expectation would otherwise surface as a different exception
    type entirely, so each is wrapped — with `from error`, because the cause is
    the useful half of the report and losing it would be the whole problem.
    """
    try:
        return connection.execute(sql, parameters).fetchall()
    except sqlite3.Error as error:
        raise RecommendationReadError(f"cannot read {description}") from error


# --------------------------------------------------------------------------
# read models
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RecommendationAssessmentReadModel:
    """One assessment, at the rank position the engine gave it."""

    opportunity_id: int
    rank_position: int
    disposition: RecommendationDisposition
    recommendation_score: float | None
    evidence_coverage: float
    assessment_fingerprint: str
    created_at: str
    assessment_payload: Mapping[str, Any]


@dataclass(frozen=True)
class RecommendationRunReadModel:
    """One stored run, whole, with its ranking in the persisted order."""

    run_id: int
    profile_id: int
    source_matching_run_id: int
    persistence_version: str
    input_assembly_version: str
    recommendation_engine_version: str
    recommendation_rules_version: str
    source_matching_run_fingerprint: str
    batch_fingerprint: str
    run_fingerprint: str
    assessment_count: int
    created_at: str
    batch_payload: Mapping[str, Any]
    assessments: tuple[RecommendationAssessmentReadModel, ...]


@dataclass(frozen=True)
class RecommendationRunSummary:
    """One line of history: the run's identity, without its assessments.

    Listing a profile's history must not pay for every assessment payload of
    every run it ever produced, so this carries no assessment at all — only
    what identifies the run and whether it is the one the profile currently
    points at.
    """

    run_id: int
    created_at: str
    assessment_count: int
    source_matching_run_id: int
    persistence_version: str
    input_assembly_version: str
    recommendation_engine_version: str
    recommendation_rules_version: str
    source_matching_run_fingerprint: str
    batch_fingerprint: str
    run_fingerprint: str
    is_current: bool


@dataclass(frozen=True)
class RecommendationProfileReadModel:
    """What this profile's recommendation state says, right now.

    `status` is one of NOT_SYNCED, READY or INCOMPLETE. NOT_SYNCED is the
    absence of a state row and carries no version, because nothing has ever
    been persisted to have a version. INCOMPLETE carries the issues that stopped
    a synchronization and points at no run — while `history_count` may still be
    non-zero, since an earlier successful run is not undone by a later failure.
    """

    profile_id: int
    status: str
    persistence_version: str | None
    input_assembly_version: str | None
    current_run_id: int | None
    current_run: RecommendationRunReadModel | None
    history_count: int
    readiness_issues: tuple[RecommendationReadinessIssue, ...]


# --------------------------------------------------------------------------
# readiness issues
# --------------------------------------------------------------------------

#: The order `input_assembly._incomplete` sorts its issues into before they are
#: ever handed on: profile-wide issues first, then per-opportunity ones grouped
#: by posting, then by code. It is restated here as a *verification* key — the
#: stored array must already be in this order — and never used to re-sort what
#: was read. Repairing the order would hide exactly the corruption it detects.
def _readiness_sort_key(issue: RecommendationReadinessIssue) -> tuple:
    return (
        issue.opportunity_id is not None,
        issue.opportunity_id or 0,
        issue.code.value,
    )


def _read_readiness_issues(
    raw: object, profile_id: int
) -> tuple[RecommendationReadinessIssue, ...]:
    """Decode `readiness_issues_json` under the contract Phase 9A already owns.

    Each entry is exactly `{"code", "message", "opportunity_id"}`, the code is a
    `RecommendationReadinessIssueCode` this build knows, and `opportunity_id` is
    either null or a positive integer that is not a boolean. An unknown code is
    refused rather than passed through as a string: a reader that cannot name an
    issue cannot honestly claim to be reporting it.
    """
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise RecommendationReadError(
            f"invalid readiness issues JSON for profile {profile_id}"
        ) from error
    if not isinstance(decoded, list):
        raise RecommendationReadError(
            f"readiness issues for profile {profile_id} are not an array"
        )
    if canonical_json(decoded) != raw:
        raise RecommendationReadError(
            f"readiness issues for profile {profile_id} are not canonical JSON"
        )
    issues: list[RecommendationReadinessIssue] = []
    for entry in decoded:
        if not isinstance(entry, dict) or set(entry) != {
            "code",
            "message",
            "opportunity_id",
        }:
            raise RecommendationReadError(
                f"malformed readiness issue for profile {profile_id}"
            )
        code, message, opportunity_id = (
            entry["code"],
            entry["message"],
            entry["opportunity_id"],
        )
        if not isinstance(code, str):
            raise RecommendationReadError(
                f"readiness issue code for profile {profile_id} is not text"
            )
        try:
            issue_code = RecommendationReadinessIssueCode(code)
        except ValueError as error:
            raise RecommendationReadError(
                f"unknown readiness issue code {code!r} for profile {profile_id}"
            ) from error
        if not isinstance(message, str) or not message.strip():
            raise RecommendationReadError(
                f"readiness issue message for profile {profile_id} is not text"
            )
        if opportunity_id is not None and not _is_positive_int(opportunity_id):
            raise RecommendationReadError(
                f"readiness issue opportunity_id for profile {profile_id} is invalid"
            )
        issues.append(
            RecommendationReadinessIssue(issue_code, message, opportunity_id)
        )
    if sorted(issues, key=_readiness_sort_key) != issues:
        raise RecommendationReadError(
            f"readiness issues for profile {profile_id} are not in assembly order"
        )
    return tuple(issues)


# --------------------------------------------------------------------------
# public reads
# --------------------------------------------------------------------------

_RUN_COLUMNS = (
    "id,profile_id,source_matching_run_id,persistence_version,input_assembly_version,"
    "recommendation_engine_version,recommendation_rules_version,"
    "source_matching_run_fingerprint,batch_fingerprint,run_fingerprint,"
    "assessment_count,created_at,batch_payload_json"
)


def _require_profile(connection: sqlite3.Connection, profile_id: int) -> None:
    if not _query(
        connection, "SELECT 1 FROM profiles WHERE id=?", (profile_id,), "profiles"
    ):
        raise RecommendationReadError(f"profile {profile_id} does not exist")


def _read_assessments(
    connection: sqlite3.Connection, run_id: int, assessment_count: object
) -> tuple[RecommendationAssessmentReadModel, ...]:
    """Load one run's ranking, and prove it is a ranking before returning it.

    `ORDER BY rank_position ASC` reproduces the order Phase 9A produced, and the
    positions are then required to be exactly `1..N` with no hole, which is what
    makes "the row at index i is the i-th recommendation" a fact rather than an
    assumption. A gap would still read as an ordered list, and would silently
    shift every position after it.
    """
    rows = _query(
        connection,
        """SELECT opportunity_id,rank_position,disposition,recommendation_score,
        evidence_coverage,assessment_fingerprint,created_at,assessment_payload_json
        FROM recommendation_assessments WHERE run_id=? ORDER BY rank_position ASC""",
        (run_id,),
        f"assessments of recommendation run {run_id}",
    )
    assessments: list[RecommendationAssessmentReadModel] = []
    for expected_position, row in enumerate(rows, start=1):
        (
            opportunity_id,
            rank_position,
            disposition,
            score,
            coverage,
            fingerprint,
            created_at,
            payload_json,
        ) = row
        where = f"run {run_id}, rank {rank_position}"
        if not _is_positive_int(opportunity_id):
            raise RecommendationReadError(
                f"opportunity_id for {where} is not a positive integer"
            )
        if rank_position != expected_position:
            raise RecommendationReadError(
                f"recommendation run {run_id} has a non-contiguous ranking:"
                f" expected position {expected_position}, stored {rank_position!r}"
            )
        if disposition not in _DISPOSITIONS:
            raise RecommendationReadError(
                f"unknown disposition {disposition!r} for {where}"
            )
        coverage = _require_ratio(coverage, f"evidence_coverage for {where}")
        if score is not None:
            score = _require_ratio(score, f"recommendation_score for {where}")
        # Phase 9A's contract, restated on the way out: no evidence means no
        # score, and a score means some evidence. A `0.0` score under no
        # coverage would read as "measured and bad" rather than "not measured".
        if (coverage == 0.0) != (score is None):
            raise RecommendationReadError(
                f"recommendation score availability mismatch for {where}"
            )
        _require_fingerprint(fingerprint, f"assessment fingerprint for {where}")
        _require_text(created_at, f"created_at for {where}")
        payload = _decode_object(
            payload_json, f"assessment payload JSON for {where}"
        )
        assessments.append(
            RecommendationAssessmentReadModel(
                opportunity_id,
                rank_position,
                RecommendationDisposition(disposition),
                score,
                coverage,
                fingerprint,
                created_at,
                _freeze(payload),
            )
        )
    if len(assessments) != assessment_count:
        raise RecommendationReadError(
            f"assessment count mismatch for recommendation run {run_id}:"
            f" {assessment_count!r} declared, {len(assessments)} stored"
        )
    return tuple(assessments)


def read_recommendation_run(
    connection: sqlite3.Connection, run_id: int
) -> RecommendationRunReadModel:
    """Read one persisted recommendation run, ranking included, or refuse."""
    run_id = _positive(run_id, "run_id")
    rows = _query(
        connection,
        f"SELECT {_RUN_COLUMNS} FROM recommendation_runs WHERE id=?",
        (run_id,),
        "recommendation_runs",
    )
    if not rows:
        raise RecommendationReadError(f"recommendation run {run_id} does not exist")
    (
        stored_id,
        profile_id,
        source_matching_run_id,
        persistence_version,
        input_assembly_version,
        engine_version,
        rules_version,
        source_matching_run_fingerprint,
        batch_fingerprint,
        run_fingerprint,
        assessment_count,
        created_at,
        batch_payload_json,
    ) = rows[0]
    profile_id = _positive(profile_id, f"profile_id of recommendation run {run_id}")
    source_matching_run_id = _positive(
        source_matching_run_id,
        f"source_matching_run_id of recommendation run {run_id}",
    )
    for value, description in (
        (persistence_version, "persistence_version"),
        (input_assembly_version, "input_assembly_version"),
        (engine_version, "recommendation_engine_version"),
        (rules_version, "recommendation_rules_version"),
        (created_at, "created_at"),
    ):
        _require_text(value, f"{description} of recommendation run {run_id}")
    for value, description in (
        (source_matching_run_fingerprint, "source_matching_run_fingerprint"),
        (batch_fingerprint, "batch_fingerprint"),
        (run_fingerprint, "run_fingerprint"),
    ):
        _require_fingerprint(value, f"{description} of recommendation run {run_id}")
    if not _is_positive_int(assessment_count):
        raise RecommendationReadError(
            f"assessment_count of recommendation run {run_id} is not a positive integer"
        )
    batch_payload = _decode_object(
        batch_payload_json, f"batch payload JSON for recommendation run {run_id}"
    )
    return RecommendationRunReadModel(
        int(stored_id),
        profile_id,
        source_matching_run_id,
        persistence_version,
        input_assembly_version,
        engine_version,
        rules_version,
        source_matching_run_fingerprint,
        batch_fingerprint,
        run_fingerprint,
        assessment_count,
        created_at,
        _freeze(batch_payload),
        _read_assessments(connection, run_id, assessment_count),
    )


def _read_state(connection: sqlite3.Connection, profile_id: int) -> tuple | None:
    rows = _query(
        connection,
        """SELECT state,current_run_id,persistence_version,input_assembly_version,
        readiness_issues_json FROM recommendation_profile_state WHERE profile_id=?""",
        (profile_id,),
        "recommendation_profile_state",
    )
    return rows[0] if rows else None


def _history_count(connection: sqlite3.Connection, profile_id: int) -> int:
    return _query(
        connection,
        "SELECT COUNT(*) FROM recommendation_runs WHERE profile_id=?",
        (profile_id,),
        "recommendation_runs",
    )[0][0]


def _current_run_id(
    connection: sqlite3.Connection, profile_id: int, history_count: int
) -> int | None:
    """The run this profile currently points at, or `None`, or a refusal.

    There is no fourth answer. A profile with history and no state row cannot be
    described honestly — "no current run" would be indistinguishable from a
    profile that has never synchronized — and a state whose `state` column and
    `current_run_id` disagree cannot be described at all.

    The readiness issues are deliberately **not** read here, and that is why
    `list_recommendation_runs` accepts one state `read_current_recommendation`
    refuses: an INCOMPLETE row carrying an empty issue array is an incoherent
    *state* — INCOMPLETE is supposed to say what stopped the synchronization —
    but it is not an ambiguous *pointer*. It names no run, so `is_current` is
    still answerable, and listing history is not the place to raise the other
    question. Each read refuses exactly what it cannot answer.
    """
    state = _read_state(connection, profile_id)
    if state is None:
        if history_count:
            raise RecommendationReadError(
                f"profile {profile_id} has recommendation runs without a state row"
            )
        return None
    status, current_run_id = state[0], state[1]
    if status == _STATUS_INCOMPLETE:
        if current_run_id is not None:
            raise RecommendationReadError(
                f"INCOMPLETE state for profile {profile_id} names a current run"
            )
        return None
    if status != _STATUS_READY:
        raise RecommendationReadError(
            f"unknown recommendation state {status!r} for profile {profile_id}"
        )
    return _positive(current_run_id, f"current_run_id for profile {profile_id}")


def list_recommendation_runs(
    connection: sqlite3.Connection, profile_id: int
) -> tuple[RecommendationRunSummary, ...]:
    """This profile's recommendation history, newest first, without payloads.

    Every row returned is valid history, including a run whose
    `source_matching_run_id` is no longer the profile's current Matching run:
    that run *was* produced over that snapshot, and saying otherwise would be
    rewriting the past. Whether a new run is due is Phase 9B.3's question and is
    never asked here.
    """
    profile_id = _positive(profile_id, "profile_id")
    _require_profile(connection, profile_id)
    history_count = _history_count(connection, profile_id)
    current_run_id = _current_run_id(connection, profile_id, history_count)
    rows = _query(
        connection,
        """SELECT id,created_at,assessment_count,source_matching_run_id,
        persistence_version,input_assembly_version,recommendation_engine_version,
        recommendation_rules_version,source_matching_run_fingerprint,
        batch_fingerprint,run_fingerprint FROM recommendation_runs
        WHERE profile_id=? ORDER BY id DESC""",
        (profile_id,),
        "recommendation_runs",
    )
    summaries = tuple(
        RecommendationRunSummary(*row, row[0] == current_run_id) for row in rows
    )
    if current_run_id is not None and not any(item.is_current for item in summaries):
        raise RecommendationReadError(
            f"current run {current_run_id} is not in profile {profile_id}'s history"
        )
    return summaries


def read_current_recommendation(
    connection: sqlite3.Connection, profile_id: int
) -> RecommendationProfileReadModel:
    """What this profile's recommendation state is: NOT_SYNCED, READY, INCOMPLETE."""
    profile_id = _positive(profile_id, "profile_id")
    _require_profile(connection, profile_id)
    history_count = _history_count(connection, profile_id)
    state = _read_state(connection, profile_id)
    if state is None:
        # No row is not a third stored value: it is "nothing has ever run", and
        # it is only true when nothing ever has. History without a state row is
        # a pointer that was lost, and NOT_SYNCED would hide exactly that.
        if history_count:
            raise RecommendationReadError(
                f"profile {profile_id} has recommendation runs without a state row"
            )
        return RecommendationProfileReadModel(
            profile_id, _STATUS_NOT_SYNCED, None, None, None, None, 0, ()
        )
    (
        status,
        current_run_id,
        persistence_version,
        input_assembly_version,
        readiness_issues_json,
    ) = state
    _require_text(persistence_version, f"persistence_version for profile {profile_id}")
    _require_text(
        input_assembly_version, f"input_assembly_version for profile {profile_id}"
    )
    issues = _read_readiness_issues(readiness_issues_json, profile_id)

    if status == _STATUS_INCOMPLETE:
        if current_run_id is not None:
            raise RecommendationReadError(
                f"INCOMPLETE state for profile {profile_id} names a current run"
            )
        if not issues:
            raise RecommendationReadError(
                f"INCOMPLETE state for profile {profile_id} carries no readiness issue"
            )
        # An INCOMPLETE profile may hold a full recommendation history: the last
        # synchronization failed, the ones before it did not, and their runs are
        # still exactly what was recommended then. That is not corruption.
        return RecommendationProfileReadModel(
            profile_id,
            _STATUS_INCOMPLETE,
            persistence_version,
            input_assembly_version,
            None,
            None,
            history_count,
            issues,
        )
    if status != _STATUS_READY:
        raise RecommendationReadError(
            f"unknown recommendation state {status!r} for profile {profile_id}"
        )
    if readiness_issues_json != _NO_READINESS_ISSUES:
        raise RecommendationReadError(
            f"READY state for profile {profile_id} carries readiness issues"
        )
    current_run_id = _positive(
        current_run_id, f"current_run_id for profile {profile_id}"
    )
    run = read_recommendation_run(connection, current_run_id)
    if run.profile_id != profile_id:
        raise RecommendationReadError(
            f"current run {current_run_id} belongs to profile {run.profile_id},"
            f" not {profile_id}"
        )
    # The state row and the run it names must agree about the contract they were
    # written under. They are **not** compared to the versions this build ships:
    # an older run read under a newer build is history, not an error.
    if (persistence_version, input_assembly_version) != (
        run.persistence_version,
        run.input_assembly_version,
    ):
        raise RecommendationReadError(
            f"state versions for profile {profile_id} disagree with"
            f" current run {current_run_id}"
        )
    return RecommendationProfileReadModel(
        profile_id,
        _STATUS_READY,
        persistence_version,
        input_assembly_version,
        current_run_id,
        run,
        history_count,
        issues,
    )
