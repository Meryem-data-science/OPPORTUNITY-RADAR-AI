"""Canonical operational identity for persisted Portfolio snapshots."""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping

from services.collector.matching.fingerprint import canonical_json

PORTFOLIO_PERSISTENCE_VERSION = "portfolio-persistence-v1"


def canonical_portfolio_run_payload(
    *,
    input_assembly_version: str,
    profile_id: int,
    priority_run_fingerprint: str,
    matching_run_fingerprint: str,
    portfolio_engine_version: str,
    portfolio_rules_version: str,
    assessment_count: int,
    included_count: int,
    excluded_count: int,
    bucket_counts: Mapping[str, int],
    assessments: Iterable[tuple[int, str]],
    persistence_version: str = PORTFOLIO_PERSISTENCE_VERSION,
    priority_run_id: int | None = None,
    matching_run_id: int | None = None,
) -> dict[str, Any]:
    """Return semantic content; accepted operational run IDs are ignored."""
    del priority_run_id, matching_run_id
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
        "priority_run_fingerprint": priority_run_fingerprint,
        "matching_run_fingerprint": matching_run_fingerprint,
        "portfolio_engine_version": portfolio_engine_version,
        "portfolio_rules_version": portfolio_rules_version,
        "assessment_count": assessment_count,
        "included_count": included_count,
        "excluded_count": excluded_count,
        "bucket_counts": {
            name: bucket_counts[name] for name in ("SAFE", "TARGET", "AMBITIOUS")
        },
        "assessments": ordered,
    }


def portfolio_run_fingerprint(**values: Any) -> str:
    return hashlib.sha256(
        canonical_json(canonical_portfolio_run_payload(**values)).encode()
    ).hexdigest()
