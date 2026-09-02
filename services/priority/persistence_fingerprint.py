"""Canonical operational identity for persisted Priority snapshots."""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any, Iterable

from services.collector.matching.fingerprint import canonical_json

PRIORITY_PERSISTENCE_VERSION = "priority-persistence-v1"


def canonical_priority_run_payload(
    *,
    input_assembly_version: str,
    profile_id: int,
    user_id: int,
    evaluation_date: date,
    matching_run_fingerprint: str,
    priority_engine_version: str,
    priority_rules_version: str,
    freshness_version: str,
    quality_version: str,
    assessments: Iterable[tuple[int, str]],
    persistence_version: str = PRIORITY_PERSISTENCE_VERSION,
) -> dict[str, Any]:
    """Return operational content in canonical opportunity order."""
    ordered = sorted(
        (
            {"opportunity_id": item[0], "assessment_fingerprint": item[1]}
            for item in assessments
        ),
        key=lambda item: item["opportunity_id"],
    )
    return {
        "persistence_version": persistence_version,
        "input_assembly_version": input_assembly_version,
        "profile_id": profile_id,
        "user_id": user_id,
        "evaluation_date": evaluation_date.isoformat(),
        "matching_run_fingerprint": matching_run_fingerprint,
        "priority_engine_version": priority_engine_version,
        "priority_rules_version": priority_rules_version,
        "freshness_version": freshness_version,
        "quality_version": quality_version,
        "assessment_count": len(ordered),
        "assessments": ordered,
    }


def priority_run_fingerprint(**values: Any) -> str:
    payload = canonical_priority_run_payload(**values)
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()
