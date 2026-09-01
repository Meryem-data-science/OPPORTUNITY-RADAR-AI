"""Canonical operational identity for persisted matching runs."""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

from .fingerprint import canonical_json

MATCHING_PERSISTENCE_VERSION = "matching-persistence-v1"


def canonical_matching_run_payload(
    *,
    profile_id: int,
    selection_version: str,
    matching_engine_version: str,
    matching_rules_version: str,
    semantic_percentile_version: str,
    corpus_fingerprint: str,
    tfidf_model_fingerprint: str,
    batch_fingerprint: str,
    assessments: Iterable[tuple[int, str]],
    persistence_version: str = MATCHING_PERSISTENCE_VERSION,
) -> dict[str, Any]:
    """Return operational content, including IDs, in canonical order."""
    ordered = sorted(
        (
            {"opportunity_id": identifier, "assessment_fingerprint": fingerprint}
            for identifier, fingerprint in assessments
        ),
        key=lambda item: item["opportunity_id"],
    )
    return {
        "persistence_version": persistence_version,
        "selection_version": selection_version,
        "profile_id": profile_id,
        "matching_engine_version": matching_engine_version,
        "matching_rules_version": matching_rules_version,
        "semantic_percentile_version": semantic_percentile_version,
        "corpus_fingerprint": corpus_fingerprint,
        "tfidf_model_fingerprint": tfidf_model_fingerprint,
        "batch_fingerprint": batch_fingerprint,
        "assessment_count": len(ordered),
        "assessments": ordered,
    }


def matching_run_fingerprint(**values: Any) -> str:
    payload = canonical_matching_run_payload(**values)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
