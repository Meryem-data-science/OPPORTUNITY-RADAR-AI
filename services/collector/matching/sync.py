"""Explicit orchestration from the matching cohort through persistence."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Literal

from .engine import MatchLane, build_matching_assessments
from .inputs import load_opportunity_matching_input, load_profile_matching_input
from .persistence import set_matching_state_empty, store_matching_batch
from .selection import MATCHING_SELECTION_VERSION, select_matching_opportunity_ids


class MatchingSyncError(RuntimeError):
    """Raised when matching synchronization cannot safely start."""


@dataclass(frozen=True)
class MatchingSyncResult:
    profile_id: int
    selection_version: str
    selected_count: int
    state: Literal["READY", "EMPTY"]
    persisted: bool
    created: bool | None
    run_id: int | None
    run_fingerprint: str | None
    batch_fingerprint: str | None
    corpus_fingerprint: str | None
    tfidf_model_fingerprint: str | None
    lane_counts: Mapping[str, int]


def _validate_profile(connection: sqlite3.Connection, profile_id: int) -> None:
    if (
        not isinstance(profile_id, int)
        or isinstance(profile_id, bool)
        or profile_id <= 0
    ):
        raise MatchingSyncError("profile_id must be a positive integer")
    if (
        connection.execute(
            "SELECT 1 FROM profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        is None
    ):
        raise MatchingSyncError(f"profile {profile_id} does not exist")


def _preflight_persistence_schema(connection: sqlite3.Connection) -> None:
    try:
        migrated = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = '0015'"
        ).fetchone()
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
                "('matching_runs', 'matching_assessments', 'matching_profile_state')"
            ).fetchall()
        }
    except sqlite3.Error as error:
        raise MatchingSyncError(
            "matching persistence schema is not ready; migrate the database through 0015 before sync"
        ) from error
    required = {"matching_runs", "matching_assessments", "matching_profile_state"}
    if migrated is None or tables != required:
        raise MatchingSyncError(
            "matching persistence schema is not ready; migrate the database through 0015 before sync"
        )


def _empty_counts() -> dict[str, int]:
    return {lane.value: 0 for lane in MatchLane}


def sync_matching(
    connection: sqlite3.Connection, profile_id: int, *, persist: bool = True
) -> MatchingSyncResult:
    """Synchronize one profile; callers serialize syncs for a profile."""
    _validate_profile(connection, profile_id)
    if persist:
        _preflight_persistence_schema(connection)

    opportunity_ids = select_matching_opportunity_ids(connection)
    if not opportunity_ids:
        if persist:
            set_matching_state_empty(
                connection, profile_id, selection_version=MATCHING_SELECTION_VERSION
            )
        return MatchingSyncResult(
            profile_id,
            MATCHING_SELECTION_VERSION,
            0,
            "EMPTY",
            persist,
            None,
            None,
            None,
            None,
            None,
            None,
            MappingProxyType(_empty_counts()),
        )

    profile = load_profile_matching_input(connection, profile_id)
    opportunities = tuple(
        load_opportunity_matching_input(connection, opportunity_id)
        for opportunity_id in opportunity_ids
    )
    batch = build_matching_assessments(profile, opportunities)
    counts = _empty_counts()
    for assessment in batch.assessments:
        counts[assessment.lane.value] += 1

    stored = None
    if persist:
        stored = store_matching_batch(
            connection,
            profile_id,
            batch,
            selection_version=MATCHING_SELECTION_VERSION,
        )
    return MatchingSyncResult(
        profile_id=profile_id,
        selection_version=MATCHING_SELECTION_VERSION,
        selected_count=len(opportunity_ids),
        state="READY",
        persisted=persist,
        created=None if stored is None else stored.created,
        run_id=None if stored is None else stored.run_id,
        run_fingerprint=None if stored is None else stored.run_fingerprint,
        batch_fingerprint=batch.batch_fingerprint,
        corpus_fingerprint=batch.corpus_fingerprint,
        tfidf_model_fingerprint=batch.tfidf_model_fingerprint,
        lane_counts=MappingProxyType(counts),
    )
