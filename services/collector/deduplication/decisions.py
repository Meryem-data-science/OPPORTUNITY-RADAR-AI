"""Persistent, human-reviewed decisions for cross-source duplicate candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sqlite3

from services.collector.deduplication.audit import (
    CandidatePair,
    POSSIBLE_CANDIDATE,
    STRONG_CANDIDATE,
)


POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
CONFIRMED_DUPLICATE = "CONFIRMED_DUPLICATE"
NOT_DUPLICATE = "NOT_DUPLICATE"
HUMAN_DECISIONS = frozenset({CONFIRMED_DUPLICATE, NOT_DUPLICATE})
STAGEABLE_CLASSIFICATIONS = frozenset({STRONG_CANDIDATE, POSSIBLE_CANDIDATE})


class DecisionError(ValueError):
    """Raised when a review-registry operation violates its workflow."""


@dataclass(frozen=True)
class Decision:
    id: int
    opportunity_a_id: int
    opportunity_b_id: int
    status: str
    audit_classification: str
    title_similarity: float
    organization_similarity: float
    title_normalized_exact: bool
    organization_normalized_exact: bool
    location_signal: str
    shared_source_url: bool
    shared_application_url: bool
    shared_canonical_url: bool
    date_distance_days: int | None
    reasons: tuple[str, ...]
    first_detected_at: str
    last_detected_at: str
    reviewed_at: str | None
    review_note: str | None
    created_at: str
    updated_at: str
    sources_a: tuple[str, ...] = ()
    sources_b: tuple[str, ...] = ()


def canonicalize_pair(first_id: int, second_id: int) -> tuple[int, int]:
    """Validate and return a stable ascending pair."""
    if isinstance(first_id, bool) or isinstance(second_id, bool):
        raise DecisionError("opportunity ids must be positive integers")
    if not isinstance(first_id, int) or not isinstance(second_id, int):
        raise DecisionError("opportunity ids must be integers")
    if first_id <= 0 or second_id <= 0:
        raise DecisionError("opportunity ids must be positive integers")
    if first_id == second_id:
        raise DecisionError("a duplicate pair must contain two different opportunities")
    return min(first_id, second_id), max(first_id, second_id)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sources(connection: sqlite3.Connection, opportunity_id: int) -> tuple[str, ...]:
    return tuple(
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT source_id FROM opportunity_sources WHERE opportunity_id = ? ORDER BY source_id",
            (opportunity_id,),
        )
    )


def ensure_cross_source_pair(connection: sqlite3.Connection, first_id: int, second_id: int) -> tuple[int, int]:
    """Require two existing opportunities with at least one differing source."""
    a, b = canonicalize_pair(first_id, second_id)
    sources_a, sources_b = _sources(connection, a), _sources(connection, b)
    if not sources_a or not sources_b:
        raise DecisionError("both opportunities must have a persisted source observation")
    if not any(left != right for left in sources_a for right in sources_b):
        raise DecisionError("the pair is not cross-source")
    return a, b


def _row_to_decision(row: sqlite3.Row | tuple, sources_a: tuple[str, ...] = (), sources_b: tuple[str, ...] = ()) -> Decision:
    values = list(row)
    values[7] = bool(values[7])
    values[8] = bool(values[8])
    values[10] = bool(values[10])
    values[11] = bool(values[11])
    values[12] = bool(values[12])
    try:
        values[14] = tuple(json.loads(values[14]))
    except (json.JSONDecodeError, TypeError) as error:
        raise DecisionError("stored reasons_json is invalid") from error
    return Decision(*values, sources_a=sources_a, sources_b=sources_b)


_COLUMNS = """id, opportunity_a_id, opportunity_b_id, status, audit_classification,
title_similarity, organization_similarity, title_normalized_exact,
organization_normalized_exact, location_signal, shared_source_url,
shared_application_url, shared_canonical_url, date_distance_days, reasons_json,
first_detected_at, last_detected_at, reviewed_at, review_note, created_at, updated_at"""


def get_decision(connection: sqlite3.Connection, first_id: int, second_id: int) -> Decision | None:
    a, b = canonicalize_pair(first_id, second_id)
    row = connection.execute(
        f"SELECT {_COLUMNS} FROM deduplication_decisions WHERE opportunity_a_id = ? AND opportunity_b_id = ?",
        (a, b),
    ).fetchone()
    return None if row is None else _row_to_decision(row, _sources(connection, a), _sources(connection, b))


def list_decisions(connection: sqlite3.Connection, status: str | None = None) -> list[Decision]:
    parameters: tuple[str, ...] = ()
    where = ""
    if status is not None:
        if status not in {POSSIBLE_DUPLICATE, *HUMAN_DECISIONS}:
            raise DecisionError(f"invalid status: {status}")
        where, parameters = " WHERE status = ?", (status,)
    rows = connection.execute(f"SELECT {_COLUMNS} FROM deduplication_decisions{where} ORDER BY id", parameters).fetchall()
    return [_row_to_decision(row, _sources(connection, row[1]), _sources(connection, row[2])) for row in rows]


def _stage_candidate(connection: sqlite3.Connection, candidate: CandidatePair, detected_at: str | None = None) -> Decision | None:
    if candidate.classification not in STAGEABLE_CLASSIFICATIONS:
        return None
    a, b = ensure_cross_source_pair(connection, candidate.opportunity_a.id, candidate.opportunity_b.id)
    now = detected_at or _utc_now()
    reasons_json = json.dumps(list(candidate.reasons), ensure_ascii=False)
    values = (
        a, b, POSSIBLE_DUPLICATE, candidate.classification,
        candidate.title_similarity, candidate.organization_similarity,
        int(candidate.title_normalized_exact), int(candidate.organization_normalized_exact),
        candidate.location_signal, int(candidate.shared_source_url),
        int(candidate.shared_application_url), int(candidate.shared_canonical_url),
        candidate.date_distance_days, reasons_json, now, now, now, now,
    )
    connection.execute(
            """
            INSERT INTO deduplication_decisions (
                opportunity_a_id, opportunity_b_id, status, audit_classification,
                title_similarity, organization_similarity, title_normalized_exact,
                organization_normalized_exact, location_signal, shared_source_url,
                shared_application_url, shared_canonical_url, date_distance_days,
                reasons_json, first_detected_at, last_detected_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(opportunity_a_id, opportunity_b_id) DO UPDATE SET
                audit_classification = excluded.audit_classification,
                title_similarity = excluded.title_similarity,
                organization_similarity = excluded.organization_similarity,
                title_normalized_exact = excluded.title_normalized_exact,
                organization_normalized_exact = excluded.organization_normalized_exact,
                location_signal = excluded.location_signal,
                shared_source_url = excluded.shared_source_url,
                shared_application_url = excluded.shared_application_url,
                shared_canonical_url = excluded.shared_canonical_url,
                date_distance_days = excluded.date_distance_days,
                reasons_json = excluded.reasons_json,
                last_detected_at = excluded.last_detected_at,
                updated_at = excluded.updated_at
            """,
        values,
    )
    return get_decision(connection, a, b)


def stage_candidate(connection: sqlite3.Connection, candidate: CandidatePair, detected_at: str | None = None) -> Decision | None:
    """Insert or refresh one candidate transactionally, preserving human status."""
    with connection:
        return _stage_candidate(connection, candidate, detected_at)


def stage_candidates(connection: sqlite3.Connection, candidates: list[CandidatePair], detected_at: str | None = None) -> list[Decision]:
    """Stage an entire scan atomically; weak candidates are ignored."""
    staged: list[Decision] = []
    with connection:
        for candidate in candidates:
            decision = _stage_candidate(connection, candidate, detected_at)
            if decision is not None:
                staged.append(decision)
    return staged


def decide_pair(connection: sqlite3.Connection, first_id: int, second_id: int, decision: str, note: str | None = None) -> Decision:
    """Record an explicit human confirmation or rejection."""
    if decision not in HUMAN_DECISIONS:
        raise DecisionError("decision must be CONFIRMED_DUPLICATE or NOT_DUPLICATE")
    a, b = ensure_cross_source_pair(connection, first_id, second_id)
    now = _utc_now()
    with connection:
        cursor = connection.execute(
            """UPDATE deduplication_decisions
               SET status = ?, reviewed_at = ?, review_note = ?, updated_at = ?
               WHERE opportunity_a_id = ? AND opportunity_b_id = ?""",
            (decision, now, note, now, a, b),
        )
        if cursor.rowcount != 1:
            raise DecisionError(f"no staged decision exists for pair ({a}, {b})")
    result = get_decision(connection, a, b)
    assert result is not None
    return result


def confirm_pair(connection: sqlite3.Connection, first_id: int, second_id: int, note: str | None = None) -> Decision:
    return decide_pair(connection, first_id, second_id, CONFIRMED_DUPLICATE, note)


def reject_pair(connection: sqlite3.Connection, first_id: int, second_id: int, note: str | None = None) -> Decision:
    return decide_pair(connection, first_id, second_id, NOT_DUPLICATE, note)
