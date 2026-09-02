"""Transaction-neutral, append-only persistence for Priority snapshots."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date

from services.collector.matching import MatchLane
from services.collector.matching.fingerprint import canonical_json
from services.eligibility import GlobalStatus

from .fingerprint import (
    canonical_priority_assessment_payload,
    priority_assessment_fingerprint,
)
from .input_assembly import PRIORITY_INPUT_ASSEMBLY_VERSION
from .models import (
    PRIORITY_ENGINE_VERSION,
    PRIORITY_FRESHNESS_VERSION,
    PRIORITY_QUALITY_VERSION,
    PRIORITY_RULES_VERSION,
    PriorityAssessment,
    PriorityCategory,
)
from .persistence_fingerprint import (
    PRIORITY_PERSISTENCE_VERSION,
    canonical_priority_run_payload,
    priority_run_fingerprint,
)


class PriorityPersistenceError(RuntimeError):
    """Raised when a Priority snapshot cannot be persisted safely."""


@dataclass(frozen=True)
class PriorityStoreResult:
    run_id: int
    run_fingerprint: str
    created: bool
    state_changed: bool


def _sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def _prepare(profile_id: int, assessments: tuple[PriorityAssessment, ...]):
    if (
        not isinstance(profile_id, int)
        or isinstance(profile_id, bool)
        or profile_id <= 0
    ):
        raise PriorityPersistenceError("profile_id must be a positive integer")
    if not assessments:
        raise PriorityPersistenceError("assessment batch must be non-empty")
    ids: set[int] = set()
    prepared = []
    versions = (
        PRIORITY_ENGINE_VERSION,
        PRIORITY_RULES_VERSION,
        PRIORITY_FRESHNESS_VERSION,
        PRIORITY_QUALITY_VERSION,
    )
    for item in assessments:
        if (
            not isinstance(item, PriorityAssessment)
            or item.profile_id != profile_id
            or item.opportunity_id <= 0
        ):
            raise PriorityPersistenceError("assessment operational ID mismatch")
        if item.opportunity_id in ids:
            raise PriorityPersistenceError("duplicate opportunity IDs")
        ids.add(item.opportunity_id)
        if (
            item.priority_engine_version,
            item.priority_rules_version,
            item.freshness_version,
            item.quality_version,
        ) != versions:
            raise PriorityPersistenceError("unexpected Priority assessment versions")
        if not isinstance(item.eligibility_status, GlobalStatus) or not isinstance(
            item.matching_lane, MatchLane
        ):
            raise PriorityPersistenceError("invalid upstream assessment enum")
        if item.priority_category is not None and not isinstance(
            item.priority_category, PriorityCategory
        ):
            raise PriorityPersistenceError("invalid Priority category")
        for value, name in (
            (item.priority_score, "score"),
            (item.priority_evidence_coverage, "coverage"),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise PriorityPersistenceError(f"invalid Priority {name}")
        expected = priority_assessment_fingerprint(item)
        if (
            not _sha(item.assessment_fingerprint)
            or item.assessment_fingerprint != expected
        ):
            raise PriorityPersistenceError(
                "assessment fingerprint does not match canonical payload"
            )
        prepared.append(
            (item, canonical_json(canonical_priority_assessment_payload(item)))
        )
    return sorted(prepared, key=lambda value: value[0].opportunity_id)


def store_priority_batch(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    user_id: int,
    matching_run_id: int,
    matching_run_fingerprint: str,
    evaluation_date: date,
    assessments: tuple[PriorityAssessment, ...],
) -> PriorityStoreResult:
    """Store/audit a batch and promote state; caller owns BEGIN/COMMIT."""
    if not connection.in_transaction:
        raise PriorityPersistenceError(
            "store_priority_batch requires an active transaction"
        )
    if (
        not isinstance(user_id, int)
        or isinstance(user_id, bool)
        or user_id <= 0
        or not isinstance(matching_run_id, int)
        or matching_run_id <= 0
    ):
        raise PriorityPersistenceError("invalid operational provenance IDs")
    if not isinstance(evaluation_date, date) or isinstance(
        evaluation_date, __import__("datetime").datetime
    ):
        raise PriorityPersistenceError("evaluation_date must be a date")
    if not _sha(matching_run_fingerprint):
        raise PriorityPersistenceError("invalid matching run fingerprint")
    prepared = _prepare(profile_id, assessments)
    owner = connection.execute(
        "SELECT user_id FROM profiles WHERE id=?", (profile_id,)
    ).fetchone()
    if owner is None or owner[0] != user_id:
        raise PriorityPersistenceError(
            "Priority profile/user provenance is inconsistent"
        )
    matching_run = connection.execute(
        "SELECT profile_id,run_fingerprint FROM matching_runs WHERE id=?",
        (matching_run_id,),
    ).fetchone()
    if (
        matching_run is None
        or matching_run[0] != profile_id
        or not _sha(matching_run[1])
        or matching_run[1] != matching_run_fingerprint
    ):
        raise PriorityPersistenceError(
            "Priority matching run provenance is inconsistent"
        )
    run_values = dict(
        input_assembly_version=PRIORITY_INPUT_ASSEMBLY_VERSION,
        profile_id=profile_id,
        user_id=user_id,
        evaluation_date=evaluation_date,
        matching_run_fingerprint=matching_run_fingerprint,
        priority_engine_version=PRIORITY_ENGINE_VERSION,
        priority_rules_version=PRIORITY_RULES_VERSION,
        freshness_version=PRIORITY_FRESHNESS_VERSION,
        quality_version=PRIORITY_QUALITY_VERSION,
        assessments=tuple(
            (a.opportunity_id, a.assessment_fingerprint) for a, _ in prepared
        ),
    )
    payload = canonical_json(canonical_priority_run_payload(**run_values))
    fingerprint = priority_run_fingerprint(**run_values)
    found = connection.execute(
        "SELECT id,profile_id,user_id,matching_run_id,persistence_version,input_assembly_version,priority_engine_version,priority_rules_version,freshness_version,quality_version,evaluation_date,matching_run_fingerprint,assessment_count,run_payload_json FROM priority_runs WHERE run_fingerprint=?",
        (fingerprint,),
    ).fetchone()
    expected_meta = (
        profile_id,
        user_id,
        matching_run_id,
        PRIORITY_PERSISTENCE_VERSION,
        PRIORITY_INPUT_ASSEMBLY_VERSION,
        PRIORITY_ENGINE_VERSION,
        PRIORITY_RULES_VERSION,
        PRIORITY_FRESHNESS_VERSION,
        PRIORITY_QUALITY_VERSION,
        evaluation_date.isoformat(),
        matching_run_fingerprint,
        len(prepared),
        payload,
    )
    created = found is None
    if found is not None:
        if tuple(found[1:]) != expected_meta:
            raise PriorityPersistenceError("existing Priority run metadata is corrupt")
        run_id = int(found[0])
        stored = connection.execute(
            "SELECT opportunity_id,priority_score,priority_evidence_coverage,priority_category,eligibility_status,matching_lane,assessment_fingerprint,assessment_payload_json FROM priority_assessments WHERE run_id=? ORDER BY opportunity_id",
            (run_id,),
        ).fetchall()
        wanted = [
            (
                a.opportunity_id,
                a.priority_score,
                a.priority_evidence_coverage,
                None if a.priority_category is None else a.priority_category.value,
                a.eligibility_status.value,
                a.matching_lane.value,
                a.assessment_fingerprint,
                p,
            )
            for a, p in prepared
        ]
        if stored != wanted:
            raise PriorityPersistenceError(
                "existing Priority run assessments are corrupt"
            )
    else:
        insert_values = (
            profile_id,
            user_id,
            matching_run_id,
            PRIORITY_PERSISTENCE_VERSION,
            PRIORITY_INPUT_ASSEMBLY_VERSION,
            PRIORITY_ENGINE_VERSION,
            PRIORITY_RULES_VERSION,
            PRIORITY_FRESHNESS_VERSION,
            PRIORITY_QUALITY_VERSION,
            evaluation_date.isoformat(),
            matching_run_fingerprint,
            fingerprint,
            len(prepared),
            payload,
        )
        row = connection.execute(
            "INSERT INTO priority_runs (profile_id,user_id,matching_run_id,persistence_version,input_assembly_version,priority_engine_version,priority_rules_version,freshness_version,quality_version,evaluation_date,matching_run_fingerprint,run_fingerprint,assessment_count,run_payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
            insert_values,
        ).fetchone()
        run_id = int(row[0])
        connection.executemany(
            "INSERT INTO priority_assessments (run_id,opportunity_id,priority_score,priority_evidence_coverage,priority_category,eligibility_status,matching_lane,assessment_fingerprint,assessment_payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    run_id,
                    a.opportunity_id,
                    a.priority_score,
                    a.priority_evidence_coverage,
                    None if a.priority_category is None else a.priority_category.value,
                    a.eligibility_status.value,
                    a.matching_lane.value,
                    a.assessment_fingerprint,
                    p,
                )
                for a, p in prepared
            ],
        )
    if connection.execute(
        "SELECT COUNT(*) FROM priority_assessments WHERE run_id=?", (run_id,)
    ).fetchone()[0] != len(prepared):
        raise PriorityPersistenceError("stored assessment count mismatch")
    state = connection.execute(
        "SELECT current_run_id,persistence_version,input_assembly_version FROM priority_profile_state WHERE profile_id=?",
        (profile_id,),
    ).fetchone()
    wanted_state = (
        run_id,
        PRIORITY_PERSISTENCE_VERSION,
        PRIORITY_INPUT_ASSEMBLY_VERSION,
    )
    state_changed = state != wanted_state
    if state is None:
        connection.execute(
            "INSERT INTO priority_profile_state (profile_id,current_run_id,persistence_version,input_assembly_version) VALUES (?,?,?,?)",
            (profile_id, *wanted_state),
        )
    elif state_changed:
        connection.execute(
            "UPDATE priority_profile_state SET current_run_id=?,persistence_version=?,input_assembly_version=?,updated_at=CURRENT_TIMESTAMP WHERE profile_id=?",
            (*wanted_state, profile_id),
        )
    return PriorityStoreResult(run_id, fingerprint, created, state_changed)
